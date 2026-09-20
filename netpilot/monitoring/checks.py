"""Monitoring probes.

Every probe takes a :class:`~netpilot.models.CheckConfig` plus the resolved
:class:`~netpilot.models.Device` and :class:`~netpilot.models.Credential`, and returns a
:class:`~netpilot.models.CheckResult`. All probes are async and never raise — failures are
reported through ``CheckResult.ok``/``message`` so one broken device cannot take down the
scheduler.
"""

from __future__ import annotations

import asyncio
import platform
import re
import socket
import ssl
import subprocess
import time
import urllib.error
import urllib.request
from typing import Any

from ..models import CheckConfig, CheckResult, Credential, Device
from . import icmp

# --------------------------------------------------------------------------------------
# ICMP
# --------------------------------------------------------------------------------------

_IS_WINDOWS = platform.system().lower().startswith("win")

_LINUX_LOSS = re.compile(r"(\d+(?:\.\d+)?)%\s*(?:packet )?loss")
_WIN_LOSS = re.compile(r"\((\d+(?:\.\d+)?)%\s*loss\)")
_RTT = re.compile(r"(?:time|tempo|rtt[^=]*)=([\d.]+)\s*ms")


async def _run(cmd: list[str], timeout: float) -> tuple[int, str, str]:
    def _spawn() -> tuple[int, str, str]:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, proc.stdout, proc.stderr

    try:
        return await asyncio.wait_for(asyncio.to_thread(_spawn), timeout=timeout + 1.0)
    except (asyncio.TimeoutError, subprocess.TimeoutExpired):
        return 124, "", "ping timed out"
    except FileNotFoundError as exc:
        return 127, "", str(exc)


def ping_command(host: str, count: int, timeout_sec: float, size: int = 56) -> list[str]:
    """Build a platform-appropriate ``ping`` argv."""
    if _IS_WINDOWS:
        # Windows: -w is a per-reply timeout in ms.
        return ["ping", "-n", str(count), "-w", str(int(timeout_sec * 1000)), host]
    wait = max(1, int(round(timeout_sec)))
    return ["ping", "-c", str(count), "-W", str(wait), "-s", str(size), host]


def parse_ping(output: str, timeout_sec: float) -> tuple[float | None, float | None]:
    """Return ``(latency_ms, packet_loss_pct)`` parsed from ping output."""
    loss = None
    match = _WIN_LOSS.search(output) or _LINUX_LOSS.search(output)
    if match:
        loss = float(match.group(1))
    rtts = [float(m) for m in _RTT.findall(output)]
    latency = sum(rtts) / len(rtts) if rtts else None
    return latency, loss


async def check_icmp(device: Device, check: CheckConfig, cred: Credential | None = None) -> CheckResult:
    """ICMP echo, with an optional TCP fallback for ICMP-hostile environments.

    Many hardened environments (locked-down containers, cloud VPCs with ICMP blocked at
    the edge) cannot do ICMP at all. Silently reporting "down" there would be worse than
    useless, so when ``params.fallback`` is set (default: a TCP port) we probe that
    instead and say so in the result.
    """
    count = int(check.params.get("count", 3))
    size = int(check.params.get("size", 32))
    timeout = float(check.params.get("timeout", check.timeout_sec))
    fallback = check.params.get("fallback", "auto")

    stats = await asyncio.to_thread(
        icmp.ping, device.host, count, min(timeout, 2.0), size
    )

    if stats.received:
        return CheckResult(
            device_id=device.id,
            kind="icmp",
            ok=True,
            latency_ms=stats.avg_rtt_ms,
            packet_loss=stats.loss_pct,
            message=f"icmp echo ok ({stats.received}/{stats.transmitted} replies)",
            metrics={
                "method": stats.method,
                "min_ms": stats.min_rtt_ms,
                "max_ms": stats.max_rtt_ms,
                "jitter_ms": stats.jitter_ms,
            },
        )

    # ICMP is not usable here at all — fall back rather than crying "down".
    icmp_blocked = "not permitted" in stats.error or "not allowed" in stats.error.lower()
    if fallback and (icmp_blocked or fallback is True or fallback != "auto"):
        port = None
        if isinstance(fallback, (int, float)):
            port = int(fallback)
        elif fallback is True or fallback == "auto":
            port = 443 if device.mgmt_url and device.mgmt_url.lower().startswith("https") else None
        if port is None:
            for candidate in (device.ssh_port or 22, 443, 80, 53):
                probe = await asyncio.to_thread(_tcp_probe, device.host, candidate, min(timeout, 3.0))
                if probe is not None:
                    return CheckResult(
                        device_id=device.id,
                        kind="icmp",
                        ok=True,
                        latency_ms=probe,
                        packet_loss=0.0,
                        message=(
                            f"ICMP unavailable here ({stats.error[:80]}); "
                            f"TCP/{candidate} fallback succeeded"
                        ),
                        metrics={"method": "tcp-fallback", "fallback_port": candidate, "icmp_error": stats.error},
                    )
        else:
            probe = await asyncio.to_thread(_tcp_probe, device.host, port, min(timeout, 3.0))
            if probe is not None:
                return CheckResult(
                    device_id=device.id,
                    kind="icmp",
                    ok=True,
                    latency_ms=probe,
                    message=f"ICMP unavailable here; TCP/{port} fallback succeeded",
                    metrics={"method": "tcp-fallback", "fallback_port": port, "icmp_error": stats.error},
                )

    severity_hint = " (ICMP is blocked in this environment)" if icmp_blocked else ""
    return CheckResult(
        device_id=device.id,
        kind="icmp",
        ok=False,
        packet_loss=100.0,
        message=f"{stats.error or 'no reply'}{severity_hint}",
        metrics={"method": stats.method, "transmitted": stats.transmitted},
    )


def _tcp_probe(host: str, port: int, timeout: float) -> float | None:
    """Return connect time in ms, or ``None`` when the port is closed/unreachable."""
    started = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return round((time.perf_counter() - started) * 1000, 2)
    except OSError:
        return None


# --------------------------------------------------------------------------------------
# TCP
# --------------------------------------------------------------------------------------


async def check_tcp(device: Device, check: CheckConfig, cred: Credential | None = None) -> CheckResult:
    port = int(check.params.get("port", device.ssh_port or 22))
    host = device.host
    started = time.perf_counter()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=check.timeout_sec
        )
        latency = (time.perf_counter() - started) * 1000
        writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
        except Exception:  # noqa: BLE001 - best-effort teardown
            pass
        return CheckResult(
            device_id=device.id,
            kind="tcp",
            ok=True,
            latency_ms=round(latency, 2),
            message=f"tcp/{port} open",
            metrics={"port": port},
        )
    except asyncio.TimeoutError:
        return CheckResult(
            device_id=device.id,
            kind="tcp",
            ok=False,
            message=f"tcp/{port} timed out after {check.timeout_sec:g}s",
            metrics={"port": port},
        )
    except (OSError, socket.gaierror) as exc:
        return CheckResult(
            device_id=device.id,
            kind="tcp",
            ok=False,
            message=f"tcp/{port} refused: {exc}",
            metrics={"port": port},
        )


# --------------------------------------------------------------------------------------
# HTTP / HTTPS
# --------------------------------------------------------------------------------------


def _http_request(url: str, timeout: float, insecure: bool, expect: int | None) -> CheckResult:
    ctx = None
    if url.lower().startswith("https") and insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    started = time.perf_counter()
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": "netpilot/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:  # noqa: S310
            latency = (time.perf_counter() - started) * 1000
            code = int(resp.status)
            ok = code == expect if expect else 200 <= code < 400
            return CheckResult(
                ok=ok,
                latency_ms=round(latency, 2),
                status_code=code,
                message=f"HTTP {code}",
            )
    except urllib.error.HTTPError as exc:
        latency = (time.perf_counter() - started) * 1000
        code = int(exc.code)
        ok = code == expect if expect else False
        return CheckResult(
            ok=ok,
            latency_ms=round(latency, 2),
            status_code=code,
            message=f"HTTP {code}",
        )
    except urllib.error.URLError as exc:
        return CheckResult(ok=False, message=f"HTTP error: {exc.reason}")
    except (OSError, ssl.SSLError) as exc:
        return CheckResult(ok=False, message=f"HTTP error: {exc}")


async def check_http(device: Device, check: CheckConfig, cred: Credential | None = None) -> CheckResult:
    scheme = "https" if check.params.get("scheme", "http") == "https" else "http"
    scheme_default = 443 if scheme == "https" else 80
    port = int(check.params.get("port", scheme_default))
    path = check.params.get("path", "/")
    if not path.startswith("/"):
        path = "/" + path
    port_part = "" if port == scheme_default else f":{port}"
    url = check.params.get("url") or f"{scheme}://{device.host}{port_part}{path}"
    expect = check.params.get("expect_status")
    if expect is not None:
        expect = int(expect)

    result = await asyncio.to_thread(
        _http_request, url, check.timeout_sec, bool(check.params.get("insecure", True)), expect
    )
    result.device_id = device.id
    result.kind = scheme
    result.metrics = {"url": url}
    return result


# --------------------------------------------------------------------------------------
# SNMP
# --------------------------------------------------------------------------------------

#: ``(oid, label, unit)`` tuples scraped for the SNMP probe.
SNMP_OIDS: tuple[tuple[str, str, str], ...] = (
    ("1.3.6.1.2.1.1.3.0", "uptime", "ticks"),
    ("1.3.6.1.2.1.1.5.0", "sysname", ""),
    ("1.3.6.1.2.1.1.1.0", "sysdescr", ""),
    ("1.3.6.1.4.1.2021.10.1.3.1", "load1", ""),
)


def _snmp_credential_args(cred: Credential | None) -> tuple[str, str | None]:
    if cred is None:
        return "public", None
    if cred.snmp_version == "3":
        return "3", None
    return cred.snmp_community or "public", None


def _coerce_oids(oids: Any) -> list[str]:
    if not oids:
        return [oid for oid, _, _ in SNMP_OIDS]
    if isinstance(oids, str):
        return [o.strip() for o in oids.split(",") if o.strip()]
    return [str(o).strip() for o in oids]


async def check_snmp(device: Device, check: CheckConfig, cred: Credential | None = None) -> CheckResult:
    """SNMP GET probe. Requires the optional ``pysnmp`` dependency."""
    try:
        from pysnmp.hlapi.v3arch.asyncio import (  # type: ignore[import-not-found]
            CommunityData,
            ContextData,
            ObjectIdentity,
            ObjectType,
            SnmpEngine,
            UdpTransportTarget,
            UsmUserData,
            get_cmd,
        )
    except ImportError:
        return CheckResult(
            device_id=device.id,
            kind="snmp",
            ok=False,
            message="pysnmp not installed — run: pip install \"netpilot[snmp]\"",
        )

    oids = _coerce_oids(check.params.get("oids"))
    port = int(check.params.get("port", device.snmp_port or 161))
    retries = int(check.params.get("retries", 1))

    if cred is not None and cred.snmp_version == "3":
        auth = {
            "MD5": "usmHMACMD5AuthProtocol",
            "SHA": "usmHMACSHAAuthProtocol",
            "SHA224": "usmHMAC128SHA224AuthProtocol",
            "SHA256": "usmHMAC192SHA256AuthProtocol",
            "SHA384": "usmHMAC256SHA384AuthProtocol",
            "SHA512": "usmHMAC384SHA512AuthProtocol",
            "NONE": "usmNoAuthProtocol",
        }.get((cred.snmp_auth_protocol or "SHA").upper())
        priv = {
            "DES": "usmDESPrivProtocol",
            "3DES": "usm3DESEDEPrivProtocol",
            "AES": "usmAesCfb128Protocol",
            "AES128": "usmAesCfb128Protocol",
            "AES192": "usmAesCfb192Protocol",
            "AES256": "usmAesCfb256Protocol",
            "NONE": "usmNoPrivProtocol",
        }.get((cred.snmp_priv_protocol or "NONE").upper())
        import pysnmp.hlapi.v3arch.asyncio as hlapi

        auth_proto = getattr(hlapi, auth) if auth else hlapi.usmNoAuthProtocol
        priv_proto = getattr(hlapi, priv) if priv else hlapi.usmNoPrivProtocol
        auth_data: Any = UsmUserData(
            cred.username,
            authKey=cred.password or None,
            privKey=cred.snmp_priv_password or None,
            authProtocol=auth_proto,
            privProtocol=priv_proto,
        )
    else:
        community = (cred.snmp_community if cred else None) or check.params.get("community") or "public"
        auth_data = CommunityData(community, mpModel=0 if (cred and cred.snmp_version == "1") else 1)

    started = time.perf_counter()
    engine = SnmpEngine()
    try:
        target = await UdpTransportTarget.create(
            (device.host, port), timeout=check.timeout_sec, retries=retries
        )
        error_indication, error_status, _error_index, var_binds = await get_cmd(
            engine,
            auth_data,
            target,
            ContextData(),
            *[ObjectType(ObjectIdentity(oid)) for oid in oids],
        )
    except Exception as exc:  # noqa: BLE001 - pysnmp raises a wide variety of things
        return CheckResult(
            device_id=device.id,
            kind="snmp",
            ok=False,
            message=f"SNMP probe error: {exc}",
            metrics={"port": port},
        )

    latency = (time.perf_counter() - started) * 1000
    if error_indication:
        return CheckResult(
            device_id=device.id,
            kind="snmp",
            ok=False,
            message=str(error_indication)[:200],
            metrics={"port": port},
        )
    if error_status:
        return CheckResult(
            device_id=device.id,
            kind="snmp",
            ok=False,
            status_code=int(error_status),
            message=f"{error_status.prettyPrint()}",
            metrics={"port": port},
        )

    values: dict[str, Any] = {}
    for name, value in var_binds:
        values[str(name)] = value.prettyPrint()

    metrics: dict[str, Any] = {"port": port, "values": values}
    sysname = values.get("1.3.6.1.2.1.1.5.0")
    if sysname:
        metrics["sysname"] = sysname
    ticks = values.get("1.3.6.1.2.1.1.3.0")
    if ticks and ticks.isdigit():
        metrics["uptime_sec"] = int(ticks) // 100

    return CheckResult(
        device_id=device.id,
        kind="snmp",
        ok=True,
        latency_ms=round(latency, 2),
        message=sysname or "SNMP ok",
        metrics=metrics,
    )


# --------------------------------------------------------------------------------------
# SSH
# --------------------------------------------------------------------------------------


async def check_ssh(device: Device, check: CheckConfig, cred: Credential | None = None) -> CheckResult:
    """TCP-connect plus SSH banner grab — proves the SSH daemon is answering."""
    port = int(check.params.get("port", device.ssh_port or 22))
    started = time.perf_counter()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(device.host, port), timeout=check.timeout_sec
        )
    except (asyncio.TimeoutError, OSError, socket.gaierror) as exc:
        return CheckResult(
            device_id=device.id,
            kind="ssh",
            ok=False,
            message=f"ssh/{port} unreachable: {exc}",
            metrics={"port": port},
        )

    try:
        banner_bytes = await asyncio.wait_for(reader.readline(), timeout=check.timeout_sec)
        banner = banner_bytes.decode("utf-8", "replace").strip()
        latency = (time.perf_counter() - started) * 1000
        ok = banner.startswith("SSH-")
        return CheckResult(
            device_id=device.id,
            kind="ssh",
            ok=ok,
            latency_ms=round(latency, 2),
            message=banner[:180] or "no SSH banner",
            metrics={"port": port, "banner": banner[:180]},
        )
    except asyncio.TimeoutError:
        return CheckResult(
            device_id=device.id,
            kind="ssh",
            ok=False,
            message=f"ssh/{port} connected but sent no banner",
            metrics={"port": port},
        )
    finally:
        writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------------------
# Defaults & dispatch
# --------------------------------------------------------------------------------------

#: Checks auto-created for a new device (the "monitor it as soon as it's added" flow).
DEFAULT_CHECKS: tuple[dict[str, Any], ...] = (
    {"kind": "icmp", "label": "Ping", "interval_sec": 30, "params": {"count": 3}, "failures_to_down": 2},
    {"kind": "tcp", "label": "SSH port", "interval_sec": 60, "params": {"port": 22}, "failures_to_down": 3},
)

PROBES = {
    "icmp": check_icmp,
    "tcp": check_tcp,
    "http": check_http,
    "https": check_http,
    "snmp": check_snmp,
    "ssh": check_ssh,
}


def default_checks_for(device: Device, extra: bool = True) -> list[CheckConfig]:
    """Build the starter check set for a freshly added device."""
    checks: list[CheckConfig] = []
    for spec in DEFAULT_CHECKS:
        params = dict(spec["params"])
        if spec["kind"] == "tcp":
            params["port"] = device.ssh_port or 22
        checks.append(
            CheckConfig(
                device_id=device.id,
                kind=spec["kind"],
                label=spec["label"],
                params=params,
                interval_sec=spec["interval_sec"],
                failures_to_down=spec.get("failures_to_down", 2),
            )
        )
    if extra and device.mgmt_url:
        checks.append(
            CheckConfig(
                device_id=device.id,
                kind="https" if device.mgmt_url.lower().startswith("https") else "http",
                label="Management UI",
                params={"url": device.mgmt_url},
                interval_sec=120,
                failures_to_down=3,
            )
        )
    return checks


async def run_check(
    check: CheckConfig, device: Device, cred: Credential | None = None
) -> CheckResult:
    """Dispatch *check* to the right probe. Never raises."""
    probe = PROBES.get(check.kind)
    if probe is None:
        return CheckResult(
            device_id=device.id,
            check_id=check.id,
            kind=check.kind,
            ok=False,
            message=f"unknown check kind: {check.kind}",
        )
    try:
        result = await probe(device, check, cred)
    except Exception as exc:  # noqa: BLE001 - a probe must never kill the scheduler
        result = CheckResult(
            device_id=device.id, kind=check.kind, ok=False, message=f"probe crashed: {exc!r}"
        )
    result.check_id = check.id
    result.device_id = device.id
    return result
