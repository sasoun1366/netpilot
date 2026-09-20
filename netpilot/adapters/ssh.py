"""Shared SSH transport for CLI-style network devices.

Interactive shells are the common denominator across RouterOS and IOS, so the adapter
family is built on a single, carefully-written prompt reader:

* every read is bounded by a deadline, so a wedged device fails instead of hanging the
  deploy worker,
* ANSI/VT100 escape sequences and RouterOS backspace artefacts are stripped,
* ``send`` returns a :class:`CommandResult` that is marked failed when the device echoes
  a syntax error, not merely when the socket stayed open.

Only this module imports ``paramiko``, which keeps the dependency optional for users who
only want the monitoring half of netpilot.
"""

from __future__ import annotations

import re
import socket
import time
from typing import Any

from ..models import Credential, Device
from .base import Adapter, AdapterError, CommandResult, SessionInfo

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[()][B0]")
CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
#: Pagination prompts seen on Cisco (``--More--``), RouterOS (``--- more ---``) and junos.
PAGER_RE = re.compile(r"--+\s*more\s*--+|--More--|\(more \d+%\)", re.IGNORECASE)


def collapse_backspaces(text: str) -> str:
    """Apply backspaces the way a terminal does — as a cursor moving left over a buffer.

    RouterOS in particular redraws lines this way (``foo\\x08\\x08\\x08bar`` means "bar"),
    and a naive regex gets the interleaving wrong once several backspaces appear in a row.
    """
    if "\x08" not in text:
        return text
    buffer: list[str] = []
    for char in text:
        if char == "\x08":
            if buffer:
                buffer.pop()
        else:
            buffer.append(char)
    return "".join(buffer)


def clean_terminal(text: str) -> str:
    """Strip ANSI escapes, control characters and CR/backspace overdraw artifacts.

    Order matters: backspaces are resolved *before* control characters are removed,
    otherwise the ``\\x08`` bytes would be gone before they could move the cursor.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = ANSI_RE.sub("", text)
    text = collapse_backspaces(text)
    text = CTRL_RE.sub("", text)
    return text


class SSHAdapter(Adapter):
    """Adapter base class for devices driven over an interactive SSH shell."""

    #: Sent right after login, before anything else (e.g. disable pagers).
    post_login: tuple[str, ...] = ()
    default_port = 22
    connect_timeout = 15.0

    def __init__(self, device: Device, credential: Credential | None, timeout: float = 20.0) -> None:
        super().__init__(device, credential, timeout)
        self.channel: Any = None
        self._buffer = ""

    # -- transport -------------------------------------------------------------------

    def _paramiko(self):
        try:
            import paramiko  # noqa: PLC0415 - optional dependency, imported on demand
        except ImportError as exc:  # pragma: no cover - depends on install extras
            raise AdapterError(
                "paramiko is required for device sessions — install it with: pip install paramiko"
            ) from exc
        return paramiko

    def _connect_client(self) -> Any:
        paramiko = self._paramiko()
        cred = self.credential
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        kwargs: dict[str, Any] = {
            "hostname": self.device.host,
            "port": self.device.ssh_port or self.default_port,
            "username": (cred.username if cred else "") or self.device.host,
            "timeout": min(self.connect_timeout, self.timeout),
            "banner_timeout": self.connect_timeout,
            "auth_timeout": self.connect_timeout,
            "allow_agent": False,
            "look_for_keys": False,
        }
        if cred and cred.key_path:
            kwargs["key_filename"] = cred.key_path
            if cred.key_passphrase:
                kwargs["passphrase"] = cred.key_passphrase
        elif cred and cred.password:
            kwargs["password"] = cred.password
        else:
            # Last resort: default key locations (~/.ssh/id_*).
            kwargs["look_for_keys"] = True

        try:
            client.connect(**kwargs)
        except paramiko.AuthenticationException as exc:
            raise AdapterError(f"authentication failed for {kwargs['username']}@{self.device.host}: {exc}") from exc
        except (paramiko.SSHException, socket.error, OSError) as exc:
            raise AdapterError(f"SSH connection to {self.device.host}:{kwargs['port']} failed: {exc}") from exc
        return client

    @property
    def prompt_re(self) -> re.Pattern[str]:
        return re.compile("|".join(f"(?:{p})" for p in self.prompt_patterns))

    def _read_until_prompt(self, deadline: float, extra_patterns: list[str] | None = None) -> str:
        """Drain the channel until a prompt (or an extra pattern) is seen, or time runs out."""
        patterns = list(self.prompt_patterns if isinstance(self.prompt_patterns, tuple) else [self.prompt_patterns])
        patterns += list(extra_patterns or [])
        matcher = re.compile("|".join(f"(?:{p})" for p in patterns))
        chunks: list[str] = []
        while time.time() < deadline:
            if self.channel.recv_ready():
                data = self.channel.recv(65536)
                if not data:
                    break
                chunks.append(data.decode("utf-8", "replace"))
                cleaned = clean_terminal("".join(chunks))
                tail = cleaned[-400:]
                # Some CLIs still paginate even with the pager disabled: answer the
                # prompt with a space and keep reading rather than truncating output.
                if PAGER_RE.search(tail):
                    self.channel.sendall(b" ")
                    chunks.append("")
                    continue
                if matcher.search(tail):
                    break
            else:
                if self.channel.exit_status_ready() and not self.channel.recv_ready():
                    if chunks:
                        break
                time.sleep(0.05)
        else:
            pass
        return clean_terminal("".join(chunks))

    def _drain(self, seconds: float = 0.3) -> str:
        deadline = time.time() + seconds
        chunks: list[str] = []
        while time.time() < deadline:
            if self.channel.recv_ready():
                chunks.append(self.channel.recv(65536).decode("utf-8", "replace"))
            else:
                time.sleep(0.05)
        return clean_terminal("".join(chunks))

    # -- session ---------------------------------------------------------------------

    def connect(self) -> SessionInfo:
        self.client = self._connect_client()
        transport = self.client.get_transport()
        if transport is not None:
            transport.set_keepalive(20)
        self.channel = self.client.invoke_shell(width=220, height=1000)
        self.channel.settimeout(1.0)
        self._drain(0.4)
        banner = self._read_until_prompt(time.time() + self.timeout)
        self.log.append(banner.strip())

        for line in self.post_login:
            self.send(line, expect_prompt=True)

        self.info = self.probe_identity()
        return self.info

    def send(self, command: str, expect_prompt: bool = True) -> CommandResult:
        if self.channel is None:
            raise AdapterError("not connected — call connect() first")
        started = time.time()
        payload = (command + "\n").encode()
        try:
            self.channel.sendall(payload)
        except (OSError, EOFError) as exc:
            raise AdapterError(f"connection lost while sending {command!r}: {exc}") from exc

        deadline = time.time() + self.timeout
        output = self._read_until_prompt(deadline) if expect_prompt else self._drain(1.0)
        duration = int((time.time() - started) * 1000)

        text = self._strip_echo(output, command)
        error = self.detect_errors(text)
        return CommandResult(
            command=command,
            output=text,
            error=error,
            ok=not error,
            duration_ms=duration,
        )

    @staticmethod
    def _strip_echo(output: str, command: str) -> str:
        """Remove the echoed command line and a trailing prompt from *output*."""
        lines = output.split("\n")
        if lines and lines[0].strip().endswith(command.strip()):
            lines = lines[1:]
        while lines and not lines[-1].strip():
            lines.pop()
        if lines and re.search(r"[>#\]]\s*$", lines[-1].strip()):
            lines.pop()
        return "\n".join(lines).strip()

    def close(self) -> None:
        if self.channel is not None:
            try:
                self.channel.close()
            except Exception:  # noqa: BLE001
                pass
            self.channel = None
        super().close()

    def verify(self) -> tuple[bool, str]:
        try:
            with socket.create_connection(
                (self.device.host, self.device.ssh_port or self.default_port),
                timeout=min(self.timeout, 5.0),
            ):
                return True, "ssh port reachable"
        except OSError as exc:
            return False, str(exc)


def interactive_probe(device: Device, credential: Credential | None, timeout: float = 10.0) -> dict[str, Any]:
    """Connect, read identity, disconnect — used by the "Test connection" button."""
    from .registry import get_adapter

    adapter_cls = get_adapter(device.vendor)
    adapter = adapter_cls(device, credential, timeout=timeout)
    started = time.time()
    try:
        info = adapter.connect()
        return {
            "ok": True,
            "elapsed_ms": int((time.time() - started) * 1000),
            "info": info.to_dict(),
            "summary": info.summary,
        }
    except AdapterError as exc:
        return {"ok": False, "elapsed_ms": int((time.time() - started) * 1000), "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - the UI must always get an answer
        return {"ok": False, "elapsed_ms": int((time.time() - started) * 1000), "error": f"{exc!r}"}
    finally:
        adapter.close()
