"""Individual monitoring probes."""

from __future__ import annotations

import asyncio
import http.server
import socket
import threading

import pytest

from netpilot.models import CheckConfig, Credential, Device
from netpilot.monitoring import checks, icmp


# ── ICMP primitives ──────────────────────────────────────────────────────────────────


def _reference_checksum(buffer: bytes) -> int:
    """Independent implementation, written straight from RFC 1071."""
    if len(buffer) % 2:
        buffer += b"\x00"
    total = 0
    for index in range(0, len(buffer), 2):
        total += (buffer[index] << 8) + buffer[index + 1]
        total = (total & 0xFFFF) + (total >> 16)
    return (~total) & 0xFFFF


def test_checksum_matches_a_known_vector():
    # 0001 + f203 + f4f5 + f6f7 (with end-around carry) = ddf2, so ~ = 220d.
    data = bytes.fromhex("0001f203f4f5f6f7")
    assert icmp.checksum(data) == 0x220D
    assert icmp.checksum(data) == _reference_checksum(data)


@pytest.mark.parametrize(
    "payload",
    [b"", b"\x00", b"\x00\x01", b"the quick brown fox", bytes(range(64))],
)
def test_checksum_matches_the_reference_implementation(payload):
    assert icmp.checksum(payload) == _reference_checksum(payload)


def test_a_packet_verifies_to_all_ones():
    """A buffer carrying its own correct checksum sums to 0xffff — the real invariant."""
    data = bytearray(bytes.fromhex("0001f203f4f5f6f70000"))
    data[2:4] = b"\x00\x00"
    data[2:4] = icmp.checksum(bytes(data)).to_bytes(2, "big")
    assert (0xFFFF ^ _reference_checksum(bytes(data))) == 0xFFFF


def test_build_echo_is_a_valid_icmp_echo_request():
    packet = icmp.build_echo(0x1234, 7, b"payload")
    assert packet[0] == icmp.ICMP_ECHO_REQUEST
    assert packet[1] == 0
    identifier = int.from_bytes(packet[4:6], "big")
    sequence = int.from_bytes(packet[6:8], "big")
    assert identifier == 0x1234
    assert sequence == 7
    # recomputing over the packet with the checksum field zeroed must yield the stored one
    zeroed = packet[:2] + b"\x00\x00" + packet[4:]
    assert icmp.checksum(zeroed) == int.from_bytes(packet[2:4], "big")


def test_icmp_stats_derivations():
    stats = icmp.IcmpStats(transmitted=4, received=3, rtts=[10.0, 20.0, 30.0])
    assert stats.loss_pct == 25.0
    assert stats.avg_rtt_ms == 20.0
    assert stats.min_rtt_ms == 10.0
    assert stats.max_rtt_ms == 30.0
    assert stats.jitter_ms == 20.0


def test_icmp_stats_with_no_replies():
    stats = icmp.IcmpStats(transmitted=3, received=0)
    assert stats.loss_pct == 100.0
    assert stats.avg_rtt_ms is None
    assert stats.jitter_ms is None


def test_icmp_availability_reports_a_reason():
    """Whatever the host's capabilities, the caller gets an explanation, not a mystery."""
    available, reason = icmp.icmp_available()
    assert isinstance(available, bool)
    assert reason


def test_ping_returns_stats_and_never_raises():
    stats = icmp.ping("127.0.0.1", count=1, timeout=0.7)
    assert isinstance(stats, icmp.IcmpStats)
    assert stats.transmitted == 1
    assert stats.method in ("dgram", "raw", "binary")


def test_ping_reports_unresolvable_hosts():
    stats = icmp.ping("this-host-does-not-exist.invalid", count=1, timeout=0.5)
    assert stats.received == 0
    assert stats.error


# ── ping output parsing (used by the binary fallback) ────────────────────────────────


def test_parse_linux_ping_output():
    output = (
        "PING 10.0.0.1 (10.0.0.1) 56(84) bytes of data.\n"
        "64 bytes from 10.0.0.1: icmp_seq=1 ttl=64 time=0.412 ms\n"
        "64 bytes from 10.0.0.1: icmp_seq=2 ttl=64 time=0.510 ms\n"
        "\n--- 10.0.0.1 ping statistics ---\n"
        "2 packets transmitted, 2 received, 0% packet loss, time 1001ms\n"
        "rtt min/avg/max/mdev = 0.412/0.461/0.510/0.049 ms\n"
    )
    latency, loss = checks.parse_ping(output, 1.0)
    assert loss == 0.0
    assert latency is not None and 0.4 < latency < 0.6


def test_parse_windows_ping_output():
    output = (
        "Pinging 10.0.0.1 with 32 bytes of data:\n"
        "Reply from 10.0.0.1: bytes=32 time=1ms TTL=64\n"
        "\nPing statistics for 10.0.0.1:\n"
        "    Packets: Sent = 2, Received = 1, Lost = 1 (50% loss),\n"
    )
    _latency, loss = checks.parse_ping(output, 1.0)
    assert loss == 50.0


def test_parse_ping_with_total_loss():
    output = "3 packets transmitted, 0 received, 100% packet loss, time 2040ms\n"
    _latency, loss = checks.parse_ping(output, 1.0)
    assert loss == 100.0


def test_ping_command_shape_per_platform(monkeypatch):
    monkeypatch.setattr(checks, "_IS_WINDOWS", False)
    argv = checks.ping_command("10.0.0.1", 3, 2.0)
    assert argv[:4] == ["ping", "-c", "3", "-W"]
    assert argv[-1] == "10.0.0.1"

    monkeypatch.setattr(checks, "_IS_WINDOWS", True)
    argv = checks.ping_command("10.0.0.1", 3, 2.0)
    assert argv[:4] == ["ping", "-n", "3", "-w"]
    assert argv[4] == "2000"


# ── real network probes against local sockets ────────────────────────────────────────


def _run(coro):
    return asyncio.run(coro)


def test_tcp_check_against_a_listening_socket(tcp_server):
    server = tcp_server()
    device = Device(id=1, name="d", host="127.0.0.1", vendor="generic")
    check = CheckConfig(device_id=1, kind="tcp", params={"port": server.port}, timeout_sec=3.0)
    result = _run(checks.run_check(check, device))
    assert result.ok is True
    assert result.latency_ms is not None
    assert "open" in result.message


def test_tcp_check_against_a_closed_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        closed_port = sock.getsockname()[1]
    device = Device(id=1, name="d", host="127.0.0.1", vendor="generic")
    check = CheckConfig(device_id=1, kind="tcp", params={"port": closed_port}, timeout_sec=2.0)
    result = _run(checks.run_check(check, device))
    assert result.ok is False
    assert "refused" in result.message or "timed out" in result.message


def test_ssh_check_reads_the_banner(tcp_server):
    server = tcp_server(banner="SSH-2.0-OpenSSH_9.6")
    device = Device(id=1, name="d", host="127.0.0.1", vendor="generic")
    check = CheckConfig(device_id=1, kind="ssh", params={"port": server.port}, timeout_sec=3.0)
    result = _run(checks.run_check(check, device))
    assert result.ok is True
    assert "SSH-2.0-OpenSSH_9.6" in result.message
    assert result.metrics["banner"].startswith("SSH-")


def test_ssh_check_flags_a_non_ssh_banner(tcp_server):
    server = tcp_server(banner="HTTP/1.1 400 Bad Request")
    device = Device(id=1, name="d", host="127.0.0.1", vendor="generic")
    check = CheckConfig(device_id=1, kind="ssh", params={"port": server.port}, timeout_sec=3.0)
    result = _run(checks.run_check(check, device))
    assert result.ok is False
    assert "HTTP" in result.message


class _Handler(http.server.BaseHTTPRequestHandler):
    status = 200

    def do_GET(self):  # noqa: N802 - stdlib naming
        self.send_response(_Handler.status)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):  # noqa: D102 - silence the test output
        return


@pytest.fixture()
def http_server():
    _Handler.status = 200
    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def test_http_check_success(http_server):
    port = http_server.server_address[1]
    device = Device(id=1, name="d", host="127.0.0.1", vendor="generic")
    check = CheckConfig(device_id=1, kind="http", params={"port": port, "scheme": "http"}, timeout_sec=3.0)
    result = _run(checks.run_check(check, device))
    assert result.ok is True
    assert result.status_code == 200
    assert result.kind == "http"


def test_http_check_can_expect_a_specific_status(http_server):
    _Handler.status = 401
    port = http_server.server_address[1]
    device = Device(id=1, name="d", host="127.0.0.1", vendor="generic")
    check = CheckConfig(
        device_id=1,
        kind="http",
        params={"port": port, "expect_status": 401},
        timeout_sec=3.0,
    )
    result = _run(checks.run_check(check, device))
    assert result.ok is True
    assert result.status_code == 401


def test_http_check_fails_on_a_wrong_status(http_server):
    _Handler.status = 500
    port = http_server.server_address[1]
    device = Device(id=1, name="d", host="127.0.0.1", vendor="generic")
    check = CheckConfig(
        device_id=1, kind="http", params={"port": port, "expect_status": 200}, timeout_sec=3.0
    )
    result = _run(checks.run_check(check, device))
    assert result.ok is False


def test_http_url_construction(http_server):
    port = http_server.server_address[1]
    device = Device(id=1, name="d", host="127.0.0.1", vendor="generic")
    check = CheckConfig(
        device_id=1, kind="http", params={"port": port, "path": "status"}, timeout_sec=3.0
    )
    result = _run(checks.run_check(check, device))
    assert result.metrics["url"] == f"http://127.0.0.1:{port}/status"


def test_snmp_check_without_a_device_reports_the_error():
    device = Device(id=1, name="d", host="127.0.0.1", vendor="generic")
    check = CheckConfig(device_id=1, kind="snmp", params={"port": 1}, timeout_sec=0.4)
    result = _run(checks.run_check(check, device))
    assert result.ok is False
    assert result.message


def test_snmp_oid_coercion():
    assert checks._coerce_oids(None) == [oid for oid, _, _ in checks.SNMP_OIDS]
    assert checks._coerce_oids("1.2.3, 4.5.6") == ["1.2.3", "4.5.6"]
    assert checks._coerce_oids(["1.2.3"]) == ["1.2.3"]


def test_unknown_check_kind_is_reported_not_raised():
    device = Device(id=1, name="d", host="127.0.0.1", vendor="generic")
    check = CheckConfig(device_id=1, kind="carrier-pigeon")
    result = _run(checks.run_check(check, device))
    assert result.ok is False
    assert "unknown check kind" in result.message


def test_icmp_check_falls_back_to_tcp_when_icmp_is_unavailable(tcp_server, monkeypatch):
    """The whole point of the fallback: a blocked ICMP must not read as 'down'."""
    server = tcp_server()
    device = Device(id=1, name="d", host="127.0.0.1", vendor="generic", ssh_port=server.port)
    check = CheckConfig(
        device_id=1, kind="icmp", params={"count": 1, "fallback": server.port}, timeout_sec=1.0
    )

    blocked = icmp.IcmpStats(transmitted=1, received=0, error="ICMP sockets are not permitted")
    monkeypatch.setattr(checks.icmp, "ping", lambda *a, **k: blocked)

    result = _run(checks.run_check(check, device))
    assert result.ok is True
    assert "fallback" in result.message
    assert result.metrics["method"] == "tcp-fallback"


def test_icmp_check_reports_the_capability_problem_when_there_is_no_fallback(monkeypatch):
    device = Device(id=1, name="d", host="127.0.0.1", vendor="generic")
    check = CheckConfig(device_id=1, kind="icmp", params={"count": 1, "fallback": None}, timeout_sec=1.0)
    blocked = icmp.IcmpStats(transmitted=1, received=0, error="ICMP sockets are not permitted")
    monkeypatch.setattr(checks.icmp, "ping", lambda *a, **k: blocked)
    result = _run(checks.run_check(check, device))
    assert result.ok is False
    assert "ICMP is blocked in this environment" in result.message


# ── default check set ────────────────────────────────────────────────────────────────


def test_default_checks_include_ping_and_ssh():
    device = Device(id=5, name="d", host="10.0.0.1", vendor="mikrotik", ssh_port=2222)
    generated = checks.default_checks_for(device)
    kinds = [c.kind for c in generated]
    assert "icmp" in kinds and "tcp" in kinds
    tcp_check = next(c for c in generated if c.kind == "tcp")
    assert tcp_check.params["port"] == 2222
    assert all(c.device_id == 5 for c in generated)


def test_default_checks_add_a_management_url_probe_when_present():
    device = Device(id=5, name="d", host="10.0.0.1", vendor="mikrotik", mgmt_url="https://10.0.0.1")
    kinds = [c.kind for c in checks.default_checks_for(device)]
    assert "https" in kinds


def test_probe_registry_covers_every_documented_kind():
    from netpilot.models import CHECK_KINDS

    assert set(CHECK_KINDS).issubset(set(checks.PROBES))
