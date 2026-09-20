"""MikroTik RouterOS adapter.

RouterOS is a menu-based CLI: every command is either ``/path/to/menu <verb> key=value``
or a two-step "enter menu, then act". This adapter always uses the fully-qualified
one-line form, which is stateless and therefore far safer to run in bulk — there is no
"left the device sitting in a submenu" failure mode.

Rollback is *native*: before a bulk push we ask the router to write a binary backup with
``/system backup save``, and if the push fails we load it back with ``/system backup load``.
RouterOS backups do not include the backup file itself, so this is a faithful restore of
the pre-change state.
"""

from __future__ import annotations

import re
import time
from typing import Any

from ..models import Credential, Device
from .base import CommandResult
from .ssh import SSHAdapter

# RouterOS prints the identity block as a comma-separated table; these regexes pull fields
# out of `/system resource print` and friends.
_UPTIME = re.compile(r"uptime:\s*(.+)")
_VERSION = re.compile(r"version:\s*([^\s]+)")
_BOARD = re.compile(r"board-name:\s*(.+)")
_MODEL = re.compile(r"model:\s*(.+)")
_SERIAL = re.compile(r"serial-number:\s*(.+)")
_ARCH = re.compile(r"architecture-name:\s*(.+)")
_FREE_MEM = re.compile(r"free-memory:\s*(\d+)")
_CPU_LOAD = re.compile(r"cpu-load:\s*(\d+)")
_CPU_COUNT = re.compile(r"cpu-count:\s*(\d+)")


class MikroTikAdapter(SSHAdapter):
    """Drives MikroTik RouterOS over SSH."""

    name = "mikrotik"
    label = "MikroTik RouterOS"
    #: RouterOS prompts look like ``[admin@CCR2004] >`` or ``[admin@CCR2004] /ip>``.
    prompt_patterns = (r"\[[^\]]+\]\s*(?:[^\r\n>]*)?>\s*$", r"\[[^\]]+\]\s*>")
    error_patterns = (
        "syntax error",
        "no such item",
        "bad command name",
        "invalid value",
        "expected end of command",
        "unknown parameter",
        "failure:",
        "error:",
        "cannot find",
        "ambiguous",
        "already have",
        "not enough permissions",
    )
    rollback_support = "native"
    #: Command that returns the full exportable configuration.
    export_command = "export terse show-sensitive"
    #: Used when ``show-sensitive`` is unsupported (RouterOS < 6.43).
    export_fallback = "export terse"

    # -- identity --------------------------------------------------------------------

    def probe_identity(self) -> Any:
        from .base import SessionInfo

        raw: dict[str, Any] = {}
        resource = self.send("/system resource print")
        raw["resource"] = resource.output

        identity = self.send("/system identity print")
        hostname = ""
        for line in identity.output.splitlines():
            if line.strip().lower().startswith("name:"):
                hostname = line.split(":", 1)[1].strip()

        board = self.send("/system routerboard print")
        raw["routerboard"] = board.output

        def pick(pattern: re.Pattern[str], text: str) -> str:
            match = pattern.search(text)
            return match.group(1).strip() if match else ""

        text = resource.output
        model = pick(_BOARD, text) or pick(_MODEL, board.output)
        version = pick(_VERSION, text)
        version = version.rsplit("(", 1)[0].strip() if version else ""
        loads = _CPU_LOAD.search(text)
        mem = _FREE_MEM.search(text)
        if loads:
            raw["cpu_load_pct"] = int(loads.group(1))
        if mem:
            raw["free_memory_bytes"] = int(mem.group(1))
        cores = _CPU_COUNT.search(text)
        if cores:
            raw["cpu_count"] = int(cores.group(1))

        return SessionInfo(
            hostname=hostname or self.device.name,
            model=model,
            version=version,
            uptime=pick(_UPTIME, text),
            serial=pick(_SERIAL, board.output),
            banner=(identity.output or resource.output)[:400],
            raw=raw,
        )

    # -- config ----------------------------------------------------------------------

    def fetch_config(self) -> str:
        result = self.send(self.export_command)
        if not result.ok or "syntax error" in result.output.lower():
            result = self.send(self.export_fallback)
        if not result.ok:
            raise RuntimeError(f"export failed: {result.error or result.output[:200]}")
        return result.output.strip()

    def apply_commands(self, commands: list[str], save: bool = True) -> list[CommandResult]:
        """Apply commands one at a time so a partial push is observable.

        RouterOS applies changes immediately and they are already persistent, so ``save``
        has no equivalent to IOS's ``write memory`` — we only use it to decide whether to
        snapshot a rollback point.
        """
        results: list[CommandResult] = []
        for command in commands:
            result = self.send(command)
            results.append(result)
            if not result.ok:
                break
        return results

    def save_config(self) -> CommandResult:
        return CommandResult(
            command="<save>",
            ok=True,
            output="RouterOS configuration is persistent; no write step required.",
        )

    # -- rollback --------------------------------------------------------------------

    def create_rollback_point(self, tag: str) -> tuple[bool, str]:
        """Write a binary backup on the router so the push can be undone."""
        name = f"netpilot-{tag}"
        result = self.send(f"/system backup save name={name} dont-encrypt=yes")
        ok = result.ok and ("saved" in result.output.lower() or result.output.strip() == "")
        return ok, name if ok else (result.error or result.output[:200])

    def restore_rollback_point(self, name: str) -> CommandResult:
        # RouterOS reboots after loading a backup — expect the session to drop.
        result = self.send(f"/system backup load name={name}")
        if "reboot" in result.output.lower():
            result.ok = True
        return result

    def cleanup_rollback_point(self, name: str) -> None:
        try:
            self.send(f"/file remove [find name={name}.backup]")
        except Exception:  # noqa: BLE001 - cleanup is best-effort
            pass

    # -- convenience -----------------------------------------------------------------

    def reboot(self) -> CommandResult:
        return self.send("/system reboot")

    def list_interfaces(self) -> list[str]:
        result = self.send("/interface print terse")
        names: list[str] = []
        for line in result.output.splitlines():
            match = re.search(r"name=([^\s]+)", line)
            if match:
                names.append(match.group(1))
        return names

    def traffic(self, interface: str) -> dict[str, int]:
        """One-shot rx/tx byte counters for a monitored interface."""
        result = self.send(f"/interface monitor-traffic {interface} once")
        out = result.output
        rx = re.search(r"rx-byte:\s*(\d+)", out)
        tx = re.search(r"tx-byte:\s*(\d+)", out)
        return {
            "rx_bytes": int(rx.group(1)) if rx else 0,
            "tx_bytes": int(tx.group(1)) if tx else 0,
        }


ADAPTER = MikroTikAdapter
