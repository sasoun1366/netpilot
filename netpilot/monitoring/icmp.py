"""ICMP echo, without depending on a ``ping`` binary or root.

Network tooling usually shells out to ``ping`` and then has to parse five different
output formats (and fails outright in slim containers where the binary is missing, or on
Linux where ``net.ipv4.ping_group_range`` is closed). This module does the echo itself:

1. **Unprivileged datagram ICMP** (``SOCK_DGRAM``/``IPPROTO_ICMP``) — works as a normal
   user wherever ``ping_group_range`` allows it, which is the default on most distros.
2. **Raw socket** — used when running as root or with ``CAP_NET_RAW``.
3. **The system ``ping`` binary** — last resort, for platforms that restrict both of the
   above (notably Windows, whose raw sockets are admin-only).

The documented failure mode is important: when the environment forbids all three, the
caller is told *why* instead of getting a mystery "device down".
"""

from __future__ import annotations

import errno
import os
import platform
import random
import socket
import struct
import subprocess
import time
from dataclasses import dataclass

ICMP_ECHO_REQUEST = 8
ICMP_ECHO_REPLY = 0

_IS_WINDOWS = platform.system().lower().startswith("win")


class IcmpUnavailable(RuntimeError):
    """Raised when this host cannot send ICMP at all (missing capabilities)."""


@dataclass
class IcmpStats:
    """Result of one ICMP echo run."""

    transmitted: int = 0
    received: int = 0
    rtts: list[float] = None  # type: ignore[assignment]
    error: str = ""
    method: str = ""

    def __post_init__(self) -> None:
        if self.rtts is None:
            self.rtts = []

    @property
    def loss_pct(self) -> float:
        if not self.transmitted:
            return 100.0
        return round(100.0 * (self.transmitted - self.received) / self.transmitted, 1)

    @property
    def avg_rtt_ms(self) -> float | None:
        if not self.rtts:
            return None
        return round(sum(self.rtts) / len(self.rtts), 2)

    @property
    def min_rtt_ms(self) -> float | None:
        return round(min(self.rtts), 2) if self.rtts else None

    @property
    def max_rtt_ms(self) -> float | None:
        return round(max(self.rtts), 2) if self.rtts else None

    @property
    def jitter_ms(self) -> float | None:
        if len(self.rtts) < 2:
            return None
        return round(max(self.rtts) - min(self.rtts), 2)


def checksum(data: bytes) -> int:
    """Standard internet checksum (RFC 1071)."""
    if len(data) % 2:
        data += b"\x00"
    total = 0
    for i in range(0, len(data), 2):
        total += (data[i] << 8) + data[i + 1]
    total = (total >> 16) + (total & 0xFFFF)
    total += total >> 16
    return ~total & 0xFFFF


def build_echo(identifier: int, sequence: int, payload: bytes) -> bytes:
    header = struct.pack("!BBHHH", ICMP_ECHO_REQUEST, 0, 0, identifier, sequence)
    packet = header + payload
    csum = checksum(packet)
    return struct.pack("!BBHHH", ICMP_ECHO_REQUEST, 0, csum, identifier, sequence) + payload


def _open_icmp_socket(timeout: float) -> tuple[socket.socket, str]:
    """Return an ICMP socket, preferring the unprivileged variant."""
    last_error: OSError | None = None
    # Unprivileged ping socket (Linux): no capabilities required.
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP)
        sock.settimeout(timeout)
        return sock, "dgram"
    except OSError as exc:
        last_error = exc
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP)
        sock.settimeout(timeout)
        return sock, "raw"
    except OSError as exc:
        last_error = exc
    raise IcmpUnavailable(
        f"ICMP sockets are not permitted in this environment ({last_error}); "
        "try running with CAP_NET_RAW, or widen net.ipv4.ping_group_range"
    )


def _has_privileges(exc: OSError) -> bool:
    return exc.errno in (errno.EPERM, errno.EACCES, errno.EPROTONOSUPPORT, errno.EAFNOSUPPORT)


def ping_socket(
    host: str,
    count: int = 3,
    timeout: float = 1.0,
    interval: float = 0.2,
    size: int = 32,
) -> IcmpStats:
    """Send *count* echo requests using a raw/datagram socket."""
    sock, method = _open_icmp_socket(timeout)
    stats = IcmpStats(method=method)
    identifier = os.getpid() & 0xFFFF
    payload = bytes(random.getrandbits(8) for _ in range(max(0, size - 8)))

    try:
        address = socket.gethostbyname(host)
    except socket.gaierror as exc:
        sock.close()
        stats.error = f"could not resolve {host}: {exc}"
        return stats

    try:
        for sequence in range(1, count + 1):
            packet = build_echo(identifier, sequence, payload)
            stats.transmitted += 1
            sent_at = time.perf_counter()
            try:
                sock.sendto(packet, (address, 0))
            except OSError as exc:
                stats.error = str(exc)
                break

            deadline = time.time() + timeout
            while time.time() < deadline:
                remaining = max(0.01, deadline - time.time())
                sock.settimeout(remaining)
                try:
                    data, _addr = sock.recvfrom(1024)
                except socket.timeout:
                    break
                except OSError as exc:
                    if _has_privileges(exc):
                        stats.error = str(exc)
                        return stats
                    break

                # Raw sockets include the IP header; datagram sockets do not.
                offset = 20 if method == "raw" else 0
                if len(data) < offset + 8:
                    continue
                icmp_type = data[offset]
                if icmp_type != ICMP_ECHO_REPLY:
                    continue
                resp_id, resp_seq = struct.unpack("!HH", data[offset + 4 : offset + 8])
                if resp_seq != sequence:
                    continue
                stats.received += 1
                stats.rtts.append((time.perf_counter() - sent_at) * 1000)
                break

            if sequence < count:
                time.sleep(interval)
    finally:
        sock.close()

    if not stats.received and not stats.error:
        stats.error = f"no ICMP reply from {host}"
    return stats


def ping_binary(host: str, count: int = 3, timeout: float = 1.0, size: int = 32) -> IcmpStats:
    """Fall back to the platform ``ping`` executable."""
    stats = IcmpStats(method="binary")
    if _IS_WINDOWS:
        argv = ["ping", "-n", str(count), "-w", str(int(timeout * 1000)), host]
    else:
        argv = ["ping", "-c", str(count), "-W", str(max(1, int(timeout))), "-s", str(size), host]
    stats.transmitted = count
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=count * (timeout + 1) + 2)  # noqa: S603
    except FileNotFoundError:
        stats.error = "the 'ping' binary is not installed"
        return stats
    except subprocess.TimeoutExpired:
        stats.error = "ping timed out"
        return stats

    output = proc.stdout + proc.stderr
    import re

    loss_match = re.search(r"(\d+(?:\.\d+)?)%\s*(?:packet )?loss", output) or re.search(
        r"\((\d+(?:\.\d+)?)%\s*loss\)", output
    )
    if loss_match:
        lost = float(loss_match.group(1))
        stats.received = int(round(count * (100 - lost) / 100))
    rtts = [float(m) for m in re.findall(r"(?:time|tempo|rtt[^=]*)=([\d.]+)\s*ms", output)]
    if rtts:
        stats.rtts = rtts
        stats.received = max(stats.received, len(rtts))

    if not stats.received:
        lowered = output.lower()
        if "permission" in lowered or "operation not permitted" in lowered or "sock_raw" in lowered:
            stats.error = "the 'ping' binary lacks raw-socket permission (setuid/CAP_NET_RAW)"
        else:
            stats.error = (output.strip().splitlines() or ["no reply"])[-1][:200]
    return stats


def ping(
    host: str,
    count: int = 3,
    timeout: float = 1.0,
    size: int = 32,
    prefer_binary: bool = False,
) -> IcmpStats:
    """Best-effort ICMP echo using every mechanism available on this host."""
    if prefer_binary and not _IS_WINDOWS:
        stats = ping_binary(host, count, timeout, size)
        if stats.received:
            return stats
    try:
        return ping_socket(host, count, timeout, size=size)
    except IcmpUnavailable as exc:
        stats = ping_binary(host, count, timeout, size)
        if not stats.received and not stats.error:
            stats.error = str(exc)
        # Surface the capability problem rather than a misleading "no reply".
        if not stats.received:
            stats.error = f"{stats.error} | {exc}" if stats.error else str(exc)
        return stats


def icmp_available() -> tuple[bool, str]:
    """Probe whether this host can do ICMP at all (used by ``netpilot doctor``)."""
    try:
        sock, method = _open_icmp_socket(0.1)
        sock.close()
        return True, f"{method} socket permitted"
    except IcmpUnavailable as exc:
        return False, str(exc)
