"""Cisco IOS / IOS-XE adapter.

IOS needs a stateful session: privileged-EXEC for reads, global config mode for writes,
and an explicit ``write memory`` before changes survive a reload. This adapter drives
that state machine explicitly and, crucially, *verifies* each pushed line — IOS happily
accepts a bad line and prints ``% Invalid input`` in the middle of a config block, so
"SSH stayed connected" is not treated as success.

Rollback: IOS has no universally available native checkpoint (``configure replace`` needs
a config file on the device and matching licensing), so this adapter reports
``rollback_support == "manual"``. netpilot still captures a full pre-change
``show running-config``, and the *Restore* action pushes that snapshot back through the
same verified apply flow.
"""

from __future__ import annotations

import re
import time
from typing import Any

from ..models import Credential, Device
from .base import CommandResult, SessionInfo
from .ssh import SSHAdapter

# ``show version`` parsing.
_HOSTNAME = re.compile(r"^\s*(\S+)\s+uptime is", re.MULTILINE)
_UPTIME = re.compile(r"uptime is (.+)")
_VERSION = re.compile(r"Cisco IOS[^\n]*?Version\s+([^\s,]+)")
_IOS_XE = re.compile(r"Cisco IOS XE Software,?\s*Version\s+([^\s,]+)")
_MODEL = re.compile(r"^[Cc]isco\s+(\S+)\s+\(([^)]+)\)\s+processor", re.MULTILINE)
_SERIAL = re.compile(r"[Ss]erial [Nn]umber\s*:?\s*(\S+)")
_SERIAL2 = re.compile(r"System serial number\s*:\s*(\S+)")
_IMAGE = re.compile(r"System image file is \"([^\"]+)\"")
_MEM = re.compile(r"with\s+(\d+[KMGT])B?/?\d*[KMGT]B? of memory")

NEGATION_PREFIXES = (
    "ip access-list",
    "ip route",
    "ip address",
    "interface",
    "vlan",
    "line",
    "router",
    "ntp",
    "snmp-server",
    "logging",
    "username",
    "banner",
    "no ",
)


class CiscoAdapter(SSHAdapter):
    """Drives Cisco IOS / IOS-XE over SSH."""

    name = "cisco"
    label = "Cisco IOS / IOS-XE"
    #: Privileged prompt ``hostname#`` and config prompt ``hostname(config)#``.
    prompt_patterns = (r"[^\r\n]*[#(][^\r\n]*#\s*$", r"[^\r\n]*#\s*$", r"[^\r\n]*>\s*$")
    error_patterns = (
        "% invalid input",
        "% incomplete command",
        "% ambiguous command",
        "% error",
        "% unknown",
        "% authorization failed",
        "% bad",
        "syntax error",
        "% cannot",
        "% not allowed",
    )
    post_login = ("terminal length 0", "terminal width 512")
    rollback_support = "manual"

    def __init__(self, device: Device, credential: Credential | None, timeout: float = 20.0) -> None:
        super().__init__(device, credential, timeout)
        self.privileged = False
        self.in_config = False

    # -- enable mode -----------------------------------------------------------------

    def _enter_privileged(self) -> bool:
        result = self.send("enable")
        lowered = result.output.lower()
        if "password" in lowered:
            password = (self.credential.enable_password or self.credential.password) if self.credential else ""
            if not password:
                return False
            result = self.send(password)
        self.privileged = result.ok and not self.detect_errors(result.output)
        return self.privileged

    def probe_identity(self) -> SessionInfo:
        if not self.privileged:
            self._enter_privileged()

        version = self.send("show version")
        text = version.output

        def pick(pattern: re.Pattern[str]) -> str:
            match = pattern.search(text)
            return match.group(1).strip() if match else ""

        raw: dict[str, Any] = {}
        image = pick(_IMAGE)
        if image:
            raw["image"] = image
        memory = pick(_MEM)
        if memory:
            raw["memory"] = memory

        return SessionInfo(
            hostname=pick(_HOSTNAME) or self.device.name,
            model=pick(_MODEL),
            version=pick(_IOS_XE) or pick(_VERSION),
            uptime=pick(_UPTIME),
            serial=pick(_SERIAL) or pick(_SERIAL2),
            banner=text[:400],
            raw=raw,
        )

    # -- config ----------------------------------------------------------------------

    def fetch_config(self) -> str:
        if not self.privileged:
            self._enter_privileged()
        result = self.send("show running-config")
        if not result.ok:
            raise RuntimeError(f"show running-config failed: {result.error or result.output[:200]}")
        return result.output.strip()

    def _enter_config_mode(self) -> bool:
        if self.in_config:
            return True
        result = self.send("configure terminal")
        self.in_config = result.ok
        return self.in_config

    def _exit_config_mode(self) -> None:
        if self.in_config:
            self.send("end")
            self.in_config = False

    def apply_commands(self, commands: list[str], save: bool = True) -> list[CommandResult]:
        """Push *commands* inside global config mode, verifying every line."""
        if not self.privileged:
            self._enter_privileged()
        if not self._enter_config_mode():
            return [
                CommandResult(
                    command="configure terminal",
                    ok=False,
                    error="could not enter global configuration mode (not privileged?)",
                )
            ]

        results: list[CommandResult] = []
        try:
            # One send per line: sub-modes (``interface Gi0/1``) then work naturally,
            # and the failing line is unambiguous when something goes wrong.
            for command in commands:
                result = self.send(command)
                results.append(result)
                if not result.ok:
                    # Roll back just this configuration session, not the device.
                    self.send("end")
                    self.in_config = False
                    break
        finally:
            self._exit_config_mode()

        if save and all(r.ok for r in results):
            results.append(self.save_config())
        return results

    def save_config(self) -> CommandResult:
        """``write memory`` — the only way IOS changes survive a reload."""
        self._exit_config_mode()
        started = time.time()
        result = self.send("write memory")
        output = result.output.lower()
        ok = result.ok and ("ok" in output or "complete" in output or "building" in output)
        return CommandResult(
            command="write memory",
            output=result.output,
            ok=ok,
            error="" if ok else (result.error or "device did not confirm the write"),
            duration_ms=int((time.time() - started) * 1000),
        )

    # -- rollback --------------------------------------------------------------------

    def rollback_commands(self, previous_config: str) -> list[str]:
        """Best-effort ``no``-form of the previously applied lines.

        Only lines whose inverse is unambiguous are emitted; anything else is skipped
        rather than guessed at.
        """
        commands: list[str] = []
        for raw in (previous_config or "").split("\n"):
            line = raw.strip()
            if not line or line.startswith(("!", "#")):
                continue
            lowered = line.lower()
            if lowered.startswith("no "):
                continue
            if any(lowered.startswith(prefix) for prefix in NEGATION_PREFIXES):
                if lowered.startswith("interface ") or lowered.startswith("line ") or lowered.startswith(
                    "router "
                ):
                    continue  # removing whole interfaces/lines is never safe to guess
                commands.append(f"no {line}")
        return commands

    # -- convenience -----------------------------------------------------------------

    def show(self, command: str) -> str:
        result = self.send(f"show {command}" if not command.startswith("show") else command)
        return result.output

    def write_config(self, filename: str = "netpilot-backup") -> CommandResult:
        """Copy the running config to flash so it can be pulled off the box."""
        self._exit_config_mode()
        return self.send(f"copy running-config flash:{filename}")


ADAPTER = CiscoAdapter
