"""Data model for netpilot.

These are deliberately plain dataclasses with ``from_row``/``to_dict`` helpers so the
storage layer, the monitoring engine, the deploy engine, the web UI and the desktop UI
all speak exactly the same vocabulary.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any

# --------------------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------------------

#: Check kinds understood by :mod:`netpilot.monitoring.checks`.
CHECK_KINDS = ("icmp", "tcp", "http", "https", "snmp", "ssh")

#: Device health states.
STATE_UNKNOWN = "unknown"
STATE_UP = "up"
STATE_DEGRADED = "degraded"
STATE_DOWN = "down"

#: Deployment job statuses.
JOB_PENDING = "pending"
JOB_RUNNING = "running"
JOB_DONE = "done"
JOB_FAILED = "failed"
JOB_CANCELLED = "cancelled"

#: Per-target statuses inside a job.
TARGET_PENDING = "pending"
TARGET_RUNNING = "running"
TARGET_OK = "ok"
TARGET_FAILED = "failed"
TARGET_SKIPPED = "skipped"
TARGET_ROLLED_BACK = "rolled-back"

SEVERITIES = ("info", "warning", "critical")


def _json_loads(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    """Read an optional column.

    Row objects come from ``sqlite3.Row`` (which raises on an unknown key) *and* from
    plain mappings in tests, so both need to work — a database created by an earlier
    version may simply not have the column yet.
    """
    try:
        return row[key] if row[key] is not None else default
    except (IndexError, KeyError):
        return default


def _split_tags(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [part.strip() for part in str(value).split(",") if part.strip()]


# --------------------------------------------------------------------------------------
# Credentials
# --------------------------------------------------------------------------------------


@dataclass
class Credential:
    """An SSH (or SNMP) credential bundle.

    ``password``/``enable_password``/``key_passphrase`` are stored *encrypted* by the
    storage layer; the plaintext is only ever held in memory by the deploy engine.
    """

    id: int | None = None
    name: str = ""
    username: str = ""
    password: str | None = None
    enable_password: str | None = None
    key_path: str | None = None
    key_passphrase: str | None = None
    snmp_community: str | None = None
    snmp_version: str = "2c"
    snmp_auth_protocol: str | None = None
    snmp_priv_protocol: str | None = None
    snmp_priv_password: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    @classmethod
    def from_row(cls, row: Any) -> "Credential":
        return cls(
            id=row["id"],
            name=row["name"],
            username=row["username"] or "",
            password=row["password"],
            enable_password=row["enable_password"],
            key_path=row["key_path"],
            key_passphrase=row["key_passphrase"],
            snmp_community=row["snmp_community"],
            snmp_version=row["snmp_version"] or "2c",
            snmp_auth_protocol=row["snmp_auth_protocol"],
            snmp_priv_protocol=row["snmp_priv_protocol"],
            snmp_priv_password=row["snmp_priv_password"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def to_dict(self, redact: bool = True) -> dict[str, Any]:
        data = asdict(self)
        if redact:
            for key in (
                "password",
                "enable_password",
                "key_passphrase",
                "snmp_community",
                "snmp_priv_password",
            ):
                if data.get(key):
                    data[key] = "********"
        return data


# --------------------------------------------------------------------------------------
# Devices
# --------------------------------------------------------------------------------------


@dataclass
class Device:
    id: int | None = None
    name: str = ""
    host: str = ""
    vendor: str = "mikrotik"
    ssh_port: int = 22
    credential_id: int | None = None
    tags: list[str] = field(default_factory=list)
    notes: str = ""
    site: str = ""
    enabled: bool = True
    snmp_port: int = 161
    mgmt_url: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    @classmethod
    def from_row(cls, row: Any) -> "Device":
        return cls(
            id=row["id"],
            name=row["name"],
            host=row["host"],
            vendor=row["vendor"],
            ssh_port=row["ssh_port"] or 22,
            credential_id=row["credential_id"],
            tags=_split_tags(row["tags"]),
            notes=row["notes"] or "",
            site=row["site"] or "",
            enabled=bool(row["enabled"]),
            snmp_port=row["snmp_port"] or 161,
            mgmt_url=row["mgmt_url"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["tags"] = list(self.tags)
        return data

    def matches_tags(self, tags: list[str], match_all: bool = False) -> bool:
        wanted = {t.lower() for t in tags}
        have = {t.lower() for t in self.tags}
        if not wanted:
            return True
        return wanted.issubset(have) if match_all else bool(wanted & have)


# --------------------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------------------


@dataclass
class CheckConfig:
    """A single monitoring probe attached to a device."""

    id: int | None = None
    device_id: int | None = None
    kind: str = "icmp"
    label: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    interval_sec: int = 60
    timeout_sec: float = 5.0
    enabled: bool = True
    #: Rising-edge thresholds: how many consecutive results flip the device state.
    failures_to_down: int = 2
    successes_to_up: int = 1
    #: Latency (ms) above which a healthy device is marked ``degraded``.
    degraded_latency_ms: float | None = None
    created_at: float = field(default_factory=time.time)

    @classmethod
    def from_row(cls, row: Any) -> "CheckConfig":
        return cls(
            id=row["id"],
            device_id=row["device_id"],
            kind=row["kind"],
            label=row["label"] or "",
            params=_json_loads(row["params"], {}),
            interval_sec=row["interval_sec"] or 60,
            timeout_sec=row["timeout_sec"] or 5.0,
            enabled=bool(row["enabled"]),
            failures_to_down=row["failures_to_down"] or 2,
            successes_to_up=row["successes_to_up"] or 1,
            degraded_latency_ms=row["degraded_latency_ms"],
            created_at=row["created_at"],
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CheckResult:
    id: int | None = None
    check_id: int | None = None
    device_id: int | None = None
    ts: float = field(default_factory=time.time)
    kind: str = "icmp"
    ok: bool = False
    latency_ms: float | None = None
    packet_loss: float | None = None
    status_code: int | None = None
    message: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_row(cls, row: Any) -> "CheckResult":
        return cls(
            id=row["id"],
            check_id=row["check_id"],
            device_id=row["device_id"],
            ts=row["ts"],
            kind=row["kind"],
            ok=bool(row["ok"]),
            latency_ms=row["latency_ms"],
            packet_loss=row["packet_loss"],
            status_code=row["status_code"],
            message=row["message"] or "",
            metrics=_json_loads(row["metrics"], {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DeviceState:
    """Rolled-up health for a device, maintained by the monitoring engine."""

    device_id: int
    state: str = STATE_UNKNOWN
    since: float = field(default_factory=time.time)
    last_check_ts: float | None = None
    last_latency_ms: float | None = None
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    up_checks: int = 0
    total_checks: int = 0
    last_error: str = ""

    @classmethod
    def from_row(cls, row: Any) -> "DeviceState":
        return cls(
            device_id=row["device_id"],
            state=row["state"] or STATE_UNKNOWN,
            since=row["since"],
            last_check_ts=row["last_check_ts"],
            last_latency_ms=row["last_latency_ms"],
            consecutive_failures=row["consecutive_failures"] or 0,
            consecutive_successes=row["consecutive_successes"] or 0,
            up_checks=row["up_checks"] or 0,
            total_checks=row["total_checks"] or 0,
            last_error=row["last_error"] or "",
        )

    @property
    def uptime_pct(self) -> float | None:
        if not self.total_checks:
            return None
        return round(100.0 * self.up_checks / self.total_checks, 2)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["uptime_pct"] = self.uptime_pct
        return data


# --------------------------------------------------------------------------------------
# Events
# --------------------------------------------------------------------------------------


@dataclass
class Event:
    id: int | None = None
    ts: float = field(default_factory=time.time)
    device_id: int | None = None
    device_name: str = ""
    kind: str = "state"
    severity: str = "info"
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    acknowledged: bool = False

    @classmethod
    def from_row(cls, row: Any) -> "Event":
        return cls(
            id=row["id"],
            ts=row["ts"],
            device_id=row["device_id"],
            device_name=row["device_name"] or "",
            kind=row["kind"],
            severity=row["severity"] or "info",
            message=row["message"] or "",
            details=_json_loads(row["details"], {}),
            acknowledged=bool(row["acknowledged"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------------------
# Config templates & jobs
# --------------------------------------------------------------------------------------


@dataclass
class Template:
    """A reusable configuration snippet, possibly with ``{{ variables }}``."""

    id: int | None = None
    name: str = ""
    vendor: str = "generic"
    description: str = ""
    body: str = ""
    variables: dict[str, str] = field(default_factory=dict)
    save_config: bool = True
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    @classmethod
    def from_row(cls, row: Any) -> "Template":
        return cls(
            id=row["id"],
            name=row["name"],
            vendor=row["vendor"] or "generic",
            description=row["description"] or "",
            body=row["body"] or "",
            variables=_json_loads(row["variables"], {}),
            save_config=bool(row["save_config"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Job:
    """A bulk configuration deployment run."""

    id: int | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    template_id: int | None = None
    template_name: str = ""
    vendor: str = ""
    body: str = ""
    variables: dict[str, str] = field(default_factory=dict)
    options: dict[str, Any] = field(default_factory=dict)
    status: str = JOB_PENDING
    total: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    triggered_by: str = "manual"

    @classmethod
    def from_row(cls, row: Any) -> "Job":
        return cls(
            id=row["id"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            template_id=row["template_id"],
            template_name=row["template_name"] or "",
            vendor=row["vendor"] or "",
            body=row["body"] or "",
            variables=_json_loads(row["variables"], {}),
            options=_json_loads(row["options"], {}),
            status=row["status"] or JOB_PENDING,
            total=row["total"] or 0,
            succeeded=row["succeeded"] or 0,
            failed=row["failed"] or 0,
            skipped=_row_get(row, "skipped", 0),
            triggered_by=row["triggered_by"] or "manual",
        )

    @property
    def duration_sec(self) -> float | None:
        if self.started_at is None:
            return None
        end = self.finished_at if self.finished_at is not None else time.time()
        return round(end - self.started_at, 2)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["duration_sec"] = self.duration_sec
        return data


@dataclass
class JobTarget:
    """One device's slice of a :class:`Job` — including its pre-change backup."""

    id: int | None = None
    job_id: int | None = None
    device_id: int | None = None
    device_name: str = ""
    host: str = ""
    status: str = TARGET_PENDING
    started_at: float | None = None
    finished_at: float | None = None
    output: str = ""
    error: str = ""
    backup: str = ""
    commands: list[str] = field(default_factory=list)

    @classmethod
    def from_row(cls, row: Any) -> "JobTarget":
        return cls(
            id=row["id"],
            job_id=row["job_id"],
            device_id=row["device_id"],
            device_name=row["device_name"] or "",
            host=row["host"] or "",
            status=row["status"] or TARGET_PENDING,
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            output=row["output"] or "",
            error=row["error"] or "",
            backup=row["backup"] or "",
            commands=_json_loads(row["commands"], []),
        )

    @property
    def duration_ms(self) -> int | None:
        if self.started_at is None or self.finished_at is None:
            return None
        return int((self.finished_at - self.started_at) * 1000)

    def to_dict(self) -> dict[str, Any]:
        """API/UI shape.

        The captured pre-change configuration is deliberately *not* included: a job can
        cover hundreds of devices and the payloads would be enormous. ``has_backup`` says
        whether one exists; the config itself is fetched from ``/api/backups`` on demand.
        """
        data = asdict(self)
        data["duration_ms"] = self.duration_ms
        data["has_backup"] = bool(self.backup)
        data.pop("backup", None)
        return data
