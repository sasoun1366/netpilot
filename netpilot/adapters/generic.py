"""Generic SSH adapter.

For anything that is not RouterOS or IOS — Linux servers, switches from other vendors,
appliances with a plain shell. It keeps the SSH plumbing but makes no assumptions about
syntax, and it deliberately reports ``supports_deploy = False`` so a bulk push needs an
explicit opt-in rather than silently running line-by-line commands on a shell you did not
model.

This is also the extension point for new vendors: copy this file, add prompt/error
patterns and a rollback strategy, then register it in :mod:`netpilot.adapters.registry`.
"""

from __future__ import annotations

from typing import Any

from ..models import Credential, Device
from .base import CommandResult, SessionInfo
from .ssh import SSHAdapter


class GenericSSHAdapter(SSHAdapter):
    """Plain interactive SSH shell — monitoring only, deploy requires opt-in."""

    name = "generic"
    label = "Generic SSH"
    prompt_patterns = (r"[$#>\]]\s*$",)
    error_patterns = (
        "command not found",
        "no such file or directory",
        "permission denied",
        "syntax error",
        "unrecognized",
        "invalid option",
        "not found",
    )
    post_login = ()
    #: A generic shell has no notion of "the device config", so bulk push is opt-in.
    supports_deploy = False
    rollback_support = "manual"

    def probe_identity(self) -> SessionInfo:
        raw: dict[str, Any] = {}
        uname = self.send("uname -a")
        raw["uname"] = uname.output
        hostname = self.send("hostname")
        return SessionInfo(
            hostname=hostname.output.strip().splitlines()[-1].strip() or self.device.name,
            model=uname.output.split()[0] if uname.ok and uname.output else "",
            version="",
            uptime="",
            serial="",
            banner=uname.output[:400],
            raw=raw,
        )

    def fetch_config(self) -> str:
        """Best effort: a handful of well-known config files, if the user is root."""
        chunks = [f"# netpilot generic capture from {self.device.host}"]
        for path in ("/etc/hostname", "/etc/network/interfaces", "/etc/os-release"):
            result = self.send(f"cat {path} 2>/dev/null")
            if result.output.strip():
                chunks.append(f"\n### {path}\n{result.output.strip()}")
        return "\n".join(chunks).strip()

    def apply_commands(self, commands: list[str], save: bool = True) -> list[CommandResult]:
        return self.send_many(commands, stop_on_error=True)


ADAPTER = GenericSSHAdapter
