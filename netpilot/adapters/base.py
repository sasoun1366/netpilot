"""Vendor adapter contract.

Everything that knows how to *talk* to a device lives behind this interface, so the
monitoring engine, the web API and the desktop UI never contain vendor-specific branches.

An adapter owns:

* how to open a session (SSH today, more transports later),
* how to render a template's variables into that vendor's syntax,
* how to decide whether the device accepted the commands (not merely that SSH worked),
* how to fetch ("back up") the running config, and how to push one back.

Adding a vendor = adding one module and registering it. Nothing else changes.
"""

from __future__ import annotations

import re
import socket
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from ..models import Credential, Device


class AdapterError(RuntimeError):
    """Raised when a device session or command cannot be completed."""


@dataclass
class CommandResult:
    """Outcome of one command line sent to a device."""

    command: str
    output: str = ""
    error: str = ""
    ok: bool = True
    duration_ms: int = 0


@dataclass
class SessionInfo:
    """A short, human-readable summary of the box we are talking to."""

    hostname: str = ""
    model: str = ""
    version: str = ""
    uptime: str = ""
    serial: str = ""
    banner: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "hostname": self.hostname,
            "model": self.model,
            "version": self.version,
            "uptime": self.uptime,
            "serial": self.serial,
            "banner": self.banner,
            "raw": self.raw,
        }

    @property
    def summary(self) -> str:
        bits = [b for b in (self.model, self.version, self.hostname) if b]
        return " · ".join(bits) if bits else "unknown device"


#: Substrings that indicate a device rejected a command. Used by ``detect_errors``.
DEFAULT_ERROR_PATTERNS: tuple[str, ...] = (
    "syntax error",
    "invalid input",
    "error:",
    "% error",
    "% invalid",
    "% incomplete",
    "unrecognized",
    "bad command",
    "no such command",
    "ambiguous command",
    "command not found",
    "failure:",
)


class Adapter(ABC):
    """Base class for every vendor adapter."""

    #: Registry key, e.g. ``"mikrotik"``.
    name: str = "base"
    #: Human label used in the UI.
    label: str = "Generic SSH"
    #: Whether this adapter is safe to use for bulk config pushes.
    supports_deploy: bool = True
    #: Terminal prompt patterns, per regex.
    prompt_patterns: tuple[str, ...] = (r"[>#\]]\s*$", r"[\w.\-@()/:]+[>#]\s*$")
    error_patterns: tuple[str, ...] = DEFAULT_ERROR_PATTERNS
    #: ``True`` for paginated CLIs that need a pager disabled first.
    needs_paging_off: bool = False

    def __init__(self, device: Device, credential: Credential | None, timeout: float = 20.0) -> None:
        self.device = device
        self.credential = credential
        self.timeout = timeout
        self.client: Any = None
        self.info = SessionInfo()
        self.log: list[str] = []

    # -- session ---------------------------------------------------------------------

    @abstractmethod
    def connect(self) -> SessionInfo:
        """Open a session and return identity information. Raises :class:`AdapterError`."""

    @abstractmethod
    def send(self, command: str, expect_prompt: bool = True) -> CommandResult:
        """Send one command line and return its output."""

    def close(self) -> None:
        if self.client is not None:
            try:
                self.client.close()
            except Exception:  # noqa: BLE001 - closing must never raise
                pass
            self.client = None

    def __enter__(self) -> "Adapter":
        self.connect()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- helpers ---------------------------------------------------------------------

    def send_many(self, commands: list[str], stop_on_error: bool = True) -> list[CommandResult]:
        results: list[CommandResult] = []
        for command in commands:
            result = self.send(command)
            results.append(result)
            if not result.ok and stop_on_error:
                break
        return results

    def detect_errors(self, output: str) -> str:
        """Return the first matched error line, or an empty string."""
        lowered = output.lower()
        for pattern in self.error_patterns:
            if pattern in lowered:
                for line in output.splitlines():
                    if pattern in line.lower():
                        return line.strip()[:300]
                return pattern
        return ""

    def looks_like_prompt(self, text: str) -> bool:
        tail = text.strip().splitlines()[-1] if text.strip() else ""
        return any(re.search(p, tail) for p in self.prompt_patterns)

    # -- config rendering ------------------------------------------------------------

    @classmethod
    def render_config(cls, body: str, variables: dict[str, str] | None = None) -> str:
        """Substitute ``{{ var }}`` placeholders and normalise line endings.

        Deliberately a classmethod: rendering is pure text work, so the UI can preview a
        template without an adapter instance (and therefore without any chance of a
        network connection).
        """
        text = body or ""
        for key, value in (variables or {}).items():
            text = re.sub(r"\{\{\s*" + re.escape(key) + r"\s*\}\}", str(value), text)
        lines = [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
        return "\n".join(lines).strip()

    @classmethod
    def split_config(cls, rendered: str) -> list[str]:
        """Turn a rendered config block into single-line commands, dropping comments."""
        commands: list[str] = []
        for raw in (rendered or "").split("\n"):
            line = raw.strip()
            if not line or line.startswith("#") or line.startswith("//"):
                continue
            commands.append(line)
        return commands

    def render(self, body: str, variables: dict[str, str] | None = None) -> str:
        """Instance-level convenience wrapper around :meth:`render_config`."""
        return self.render_config(body, variables)

    def split_commands(self, rendered: str) -> list[str]:
        """Instance-level convenience wrapper around :meth:`split_config`."""
        return self.split_config(rendered)

    # -- config management -----------------------------------------------------------

    @abstractmethod
    def fetch_config(self) -> str:
        """Return the device's current/exportable configuration."""

    def apply_commands(self, commands: list[str], save: bool = True) -> list[CommandResult]:
        """Push *commands* to the device. Overridden when a vendor needs a special flow."""
        return self.send_many(commands, stop_on_error=True)

    def save_config(self) -> CommandResult:
        """Persist the running config so it survives a reboot."""
        return CommandResult(command="<save>", ok=True, output="no save step for this vendor")

    def rollback_commands(self, previous_config: str) -> list[str]:
        """Best-effort commands that restore *previous_config*.

        The default implementation is intentionally conservative: vendors override this
        when a true rollback is possible.
        """
        return []

    # -- identity --------------------------------------------------------------------

    def probe_identity(self) -> SessionInfo:
        """Collect the identity block shown on the device page."""
        return self.info

    def verify(self) -> tuple[bool, str]:
        """Cheap reachability check (used before a bulk push)."""
        try:
            with socket.create_connection(
                (self.device.host, self.device.ssh_port or 22), timeout=min(self.timeout, 5.0)
            ):
                return True, "ssh port reachable"
        except OSError as exc:
            return False, str(exc)


def now_ms() -> int:
    return int(time.time() * 1000)
