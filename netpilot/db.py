"""SQLite storage for netpilot.

A single :class:`Store` object owns the database file. Connections are thread-local so
the monitoring scheduler, the deploy executor and the web request handlers can all use
the same store concurrently (SQLite is opened in WAL mode with a busy timeout).
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import models
from .models import _json_loads  # noqa: PLC2701 - shared JSON helper
from .models import (
    CheckConfig,
    CheckResult,
    Credential,
    Device,
    DeviceState,
    Event,
    Job,
    JobTarget,
    Template,
)
from .security import SecretBox

SCHEMA_VERSION = 1


class StoreError(RuntimeError):
    """Base class for storage-level problems."""


class DeviceExistsError(StoreError):
    """Raised when a device with the same host and SSH port already exists."""

SECRET_FIELDS = (
    "password",
    "enable_password",
    "key_passphrase",
    "snmp_community",
    "snmp_priv_password",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS credentials (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    name               TEXT NOT NULL UNIQUE,
    username           TEXT NOT NULL DEFAULT '',
    password           TEXT,
    enable_password    TEXT,
    key_path           TEXT,
    key_passphrase     TEXT,
    snmp_community     TEXT,
    snmp_version       TEXT NOT NULL DEFAULT '2c',
    snmp_auth_protocol TEXT,
    snmp_priv_protocol TEXT,
    snmp_priv_password TEXT,
    created_at         REAL NOT NULL,
    updated_at         REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS devices (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    host          TEXT NOT NULL,
    vendor        TEXT NOT NULL DEFAULT 'mikrotik',
    ssh_port      INTEGER NOT NULL DEFAULT 22,
    credential_id INTEGER REFERENCES credentials(id) ON DELETE SET NULL,
    tags          TEXT NOT NULL DEFAULT '',
    notes         TEXT NOT NULL DEFAULT '',
    site          TEXT NOT NULL DEFAULT '',
    enabled       INTEGER NOT NULL DEFAULT 1,
    snmp_port     INTEGER NOT NULL DEFAULT 161,
    mgmt_url      TEXT,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_devices_host ON devices(host, ssh_port);

CREATE TABLE IF NOT EXISTS checks (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id          INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    kind               TEXT NOT NULL,
    label              TEXT NOT NULL DEFAULT '',
    params             TEXT NOT NULL DEFAULT '{}',
    interval_sec       INTEGER NOT NULL DEFAULT 60,
    timeout_sec        REAL NOT NULL DEFAULT 5.0,
    enabled            INTEGER NOT NULL DEFAULT 1,
    failures_to_down   INTEGER NOT NULL DEFAULT 2,
    successes_to_up    INTEGER NOT NULL DEFAULT 1,
    degraded_latency_ms REAL,
    created_at         REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_checks_device ON checks(device_id);

CREATE TABLE IF NOT EXISTS check_results (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    check_id    INTEGER,
    device_id   INTEGER NOT NULL,
    ts          REAL NOT NULL,
    kind        TEXT NOT NULL,
    ok          INTEGER NOT NULL,
    latency_ms  REAL,
    packet_loss REAL,
    status_code INTEGER,
    message     TEXT NOT NULL DEFAULT '',
    metrics     TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_results_device_ts ON check_results(device_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_results_ts ON check_results(ts);

CREATE TABLE IF NOT EXISTS device_state (
    device_id            INTEGER PRIMARY KEY REFERENCES devices(id) ON DELETE CASCADE,
    state                TEXT NOT NULL DEFAULT 'unknown',
    since                REAL NOT NULL,
    last_check_ts        REAL,
    last_latency_ms      REAL,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    consecutive_successes INTEGER NOT NULL DEFAULT 0,
    up_checks            INTEGER NOT NULL DEFAULT 0,
    total_checks         INTEGER NOT NULL DEFAULT 0,
    last_error           TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS check_state (
    check_id              INTEGER PRIMARY KEY REFERENCES checks(id) ON DELETE CASCADE,
    device_id             INTEGER NOT NULL,
    state                 TEXT NOT NULL DEFAULT 'unknown',
    consecutive_failures  INTEGER NOT NULL DEFAULT 0,
    consecutive_successes INTEGER NOT NULL DEFAULT 0,
    last_ts               REAL,
    last_latency_ms       REAL,
    last_ok               INTEGER,
    last_error            TEXT NOT NULL DEFAULT '',
    updated_at            REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_check_state_device ON check_state(device_id);

CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           REAL NOT NULL,
    device_id    INTEGER,
    device_name  TEXT NOT NULL DEFAULT '',
    kind         TEXT NOT NULL DEFAULT 'state',
    severity     TEXT NOT NULL DEFAULT 'info',
    message      TEXT NOT NULL DEFAULT '',
    details      TEXT NOT NULL DEFAULT '{}',
    acknowledged INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts DESC);

CREATE TABLE IF NOT EXISTS templates (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    vendor      TEXT NOT NULL DEFAULT 'generic',
    description TEXT NOT NULL DEFAULT '',
    body        TEXT NOT NULL DEFAULT '',
    variables   TEXT NOT NULL DEFAULT '{}',
    save_config INTEGER NOT NULL DEFAULT 1,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    REAL NOT NULL,
    started_at    REAL,
    finished_at   REAL,
    template_id   INTEGER,
    template_name TEXT NOT NULL DEFAULT '',
    vendor        TEXT NOT NULL DEFAULT '',
    body          TEXT NOT NULL DEFAULT '',
    variables     TEXT NOT NULL DEFAULT '{}',
    options       TEXT NOT NULL DEFAULT '{}',
    status        TEXT NOT NULL DEFAULT 'pending',
    total         INTEGER NOT NULL DEFAULT 0,
    succeeded     INTEGER NOT NULL DEFAULT 0,
    failed        INTEGER NOT NULL DEFAULT 0,
    skipped       INTEGER NOT NULL DEFAULT 0,
    triggered_by  TEXT NOT NULL DEFAULT 'manual'
);

CREATE TABLE IF NOT EXISTS job_targets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    device_id   INTEGER,
    device_name TEXT NOT NULL DEFAULT '',
    host        TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'pending',
    started_at  REAL,
    finished_at REAL,
    output      TEXT NOT NULL DEFAULT '',
    error       TEXT NOT NULL DEFAULT '',
    backup      TEXT NOT NULL DEFAULT '',
    commands    TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_targets_job ON job_targets(job_id);

CREATE TABLE IF NOT EXISTS backups (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id   INTEGER,
    device_name TEXT NOT NULL DEFAULT '',
    host        TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL,
    source      TEXT NOT NULL DEFAULT 'manual',
    config      TEXT NOT NULL DEFAULT '',
    byte_size   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_backups_device ON backups(device_id, created_at DESC);
"""


def default_data_dir() -> Path:
    """Return the default per-user data directory (``NETPILOT_HOME`` overrides)."""
    env = os.environ.get("NETPILOT_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".netpilot"


class Store:
    """All persistence for netpilot."""

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        data_dir: str | os.PathLike[str] | None = None,
    ) -> None:
        if path is None:
            base = Path(data_dir) if data_dir else default_data_dir()
            base.mkdir(parents=True, exist_ok=True)
            path = base / "netpilot.db"
        self.path = Path(path)
        self.data_dir = self.path.parent
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.secrets = SecretBox.for_data_dir(self.data_dir)
        self._local = threading.local()
        self._write_lock = threading.RLock()
        self._init_schema()

    # -- connection plumbing ---------------------------------------------------------

    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # Columns added after the first release. ``CREATE TABLE IF NOT EXISTS`` cannot add
    # them to an existing database, so each one is applied explicitly when missing.
    _ADDED_COLUMNS: dict[str, dict[str, str]] = {
        "jobs": {"skipped": "INTEGER NOT NULL DEFAULT 0"},
    }

    def _init_schema(self) -> None:
        with self._write_lock:
            self.conn.executescript(SCHEMA)
            for table, columns in self._ADDED_COLUMNS.items():
                existing = {
                    row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")
                }
                for column, ddl in columns.items():
                    if column not in existing:
                        self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
            self.conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    def _exec(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self._write_lock:
            return self.conn.execute(sql, params)

    def _query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, params).fetchall())

    def _one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        cur = self.conn.execute(sql, params)
        return cur.fetchone()

    # -- settings --------------------------------------------------------------------

    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self._one("SELECT value FROM meta WHERE key=?", (f"setting:{key}",))
        if row is None:
            return default
        return _json_loads(row["value"], default)

    def set_setting(self, key: str, value: Any) -> None:
        self._exec(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
            (f"setting:{key}", json.dumps(value)),
        )

    # -- credentials -----------------------------------------------------------------

    def _enc(self, value: str | None) -> str | None:
        return self.secrets.encrypt(value)

    def _dec(self, value: str | None) -> str | None:
        return self.secrets.decrypt(value)

    def add_credential(self, cred: Credential) -> Credential:
        now = time.time()
        enc = {f: self._enc(getattr(cred, f)) for f in SECRET_FIELDS}
        cur = self._exec(
            """INSERT INTO credentials
               (name, username, password, enable_password, key_path, key_passphrase,
                snmp_community, snmp_version, snmp_auth_protocol, snmp_priv_protocol,
                snmp_priv_password, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                cred.name,
                cred.username,
                enc["password"],
                enc["enable_password"],
                cred.key_path,
                enc["key_passphrase"],
                enc["snmp_community"],
                cred.snmp_version,
                cred.snmp_auth_protocol,
                cred.snmp_priv_protocol,
                enc["snmp_priv_password"],
                now,
                now,
            ),
        )
        cred.id = int(cur.lastrowid)
        cred.created_at = cred.updated_at = now
        return cred

    def update_credential(self, cred: Credential) -> Credential:
        if cred.id is None:
            raise ValueError("credential id is required")
        now = time.time()
        # ``None`` means "keep the stored secret"; the sentinel "********" comes from the
        # UI when the operator did not retype it.
        current = self._one("SELECT * FROM credentials WHERE id=?", (cred.id,))
        if current is None:
            raise KeyError(f"credential {cred.id} not found")
        values: dict[str, Any] = {}
        for f in SECRET_FIELDS:
            new = getattr(cred, f)
            if new is None or new == "" or set(new) == {"*"}:
                values[f] = current[f]
            else:
                values[f] = self._enc(new)
        self._exec(
            """UPDATE credentials SET name=?, username=?, password=?, enable_password=?,
               key_path=?, key_passphrase=?, snmp_community=?, snmp_version=?,
               snmp_auth_protocol=?, snmp_priv_protocol=?, snmp_priv_password=?,
               updated_at=? WHERE id=?""",
            (
                cred.name,
                cred.username,
                values["password"],
                values["enable_password"],
                cred.key_path,
                values["key_passphrase"],
                values["snmp_community"],
                cred.snmp_version,
                cred.snmp_auth_protocol,
                cred.snmp_priv_protocol,
                values["snmp_priv_password"],
                now,
                cred.id,
            ),
        )
        cred.updated_at = now
        return cred

    def get_credential(self, cred_id: int, decrypt: bool = True) -> Credential | None:
        row = self._one("SELECT * FROM credentials WHERE id=?", (cred_id,))
        if row is None:
            return None
        cred = Credential.from_row(row)
        if decrypt:
            for f in SECRET_FIELDS:
                setattr(cred, f, self._dec(getattr(cred, f)))
        return cred

    def list_credentials(self, decrypt: bool = False) -> list[Credential]:
        rows = self._query("SELECT * FROM credentials ORDER BY name COLLATE NOCASE")
        return [self.get_credential(r["id"], decrypt=decrypt) for r in rows]  # type: ignore[misc]

    def delete_credential(self, cred_id: int) -> None:
        self._exec("DELETE FROM credentials WHERE id=?", (cred_id,))

    # -- devices ---------------------------------------------------------------------

    @staticmethod
    def _tags_to_str(tags: Iterable[str]) -> str:
        return ",".join(t.strip() for t in tags if t and t.strip())

    def add_device(self, device: Device) -> Device:
        now = time.time()
        try:
            cur = self._exec(
                """INSERT INTO devices
                   (name, host, vendor, ssh_port, credential_id, tags, notes, site, enabled,
                    snmp_port, mgmt_url, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    device.name,
                    device.host,
                    device.vendor,
                    device.ssh_port,
                    device.credential_id,
                    self._tags_to_str(device.tags),
                    device.notes,
                    device.site,
                    int(device.enabled),
                    device.snmp_port,
                    device.mgmt_url,
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError as exc:
            # host+port is the natural key: the same box should not appear twice.
            raise DeviceExistsError(
                f"{device.host}:{device.ssh_port} is already in the inventory"
            ) from exc
        device.id = int(cur.lastrowid)
        device.created_at = device.updated_at = now
        self.ensure_state(device.id)
        return device

    def update_device(self, device: Device) -> Device:
        if device.id is None:
            raise ValueError("device id is required")
        now = time.time()
        self._exec(
            """UPDATE devices SET name=?, host=?, vendor=?, ssh_port=?, credential_id=?,
               tags=?, notes=?, site=?, enabled=?, snmp_port=?, mgmt_url=?, updated_at=?
               WHERE id=?""",
            (
                device.name,
                device.host,
                device.vendor,
                device.ssh_port,
                device.credential_id,
                self._tags_to_str(device.tags),
                device.notes,
                device.site,
                int(device.enabled),
                device.snmp_port,
                device.mgmt_url,
                now,
                device.id,
            ),
        )
        device.updated_at = now
        return device

    def get_device(self, device_id: int) -> Device | None:
        row = self._one("SELECT * FROM devices WHERE id=?", (device_id,))
        return Device.from_row(row) if row else None

    def list_devices(
        self,
        enabled_only: bool = False,
        tags: Sequence[str] | None = None,
        search: str | None = None,
    ) -> list[Device]:
        sql = "SELECT * FROM devices"
        clauses: list[str] = []
        params: list[Any] = []
        if enabled_only:
            clauses.append("enabled=1")
        if search:
            clauses.append("(name LIKE ? OR host LIKE ? OR site LIKE ? OR tags LIKE ?)")
            like = f"%{search}%"
            params.extend([like, like, like, like])
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY name COLLATE NOCASE"
        devices = [Device.from_row(r) for r in self._query(sql, params)]
        if tags:
            devices = [d for d in devices if d.matches_tags(list(tags))]
        return devices

    def delete_device(self, device_id: int) -> None:
        self._exec("DELETE FROM devices WHERE id=?", (device_id,))

    def all_tags(self) -> list[str]:
        tags: set[str] = set()
        for row in self._query("SELECT tags FROM devices"):
            tags.update(models._split_tags(row["tags"]))
        return sorted(tags, key=str.lower)

    # -- checks ----------------------------------------------------------------------

    def add_check(self, check: CheckConfig) -> CheckConfig:
        now = time.time()
        cur = self._exec(
            """INSERT INTO checks
               (device_id, kind, label, params, interval_sec, timeout_sec, enabled,
                failures_to_down, successes_to_up, degraded_latency_ms, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                check.device_id,
                check.kind,
                check.label,
                json.dumps(check.params),
                int(check.interval_sec),
                float(check.timeout_sec),
                int(check.enabled),
                int(check.failures_to_down),
                int(check.successes_to_up),
                check.degraded_latency_ms,
                now,
            ),
        )
        check.id = int(cur.lastrowid)
        check.created_at = now
        return check

    def update_check(self, check: CheckConfig) -> CheckConfig:
        if check.id is None:
            raise ValueError("check id is required")
        self._exec(
            """UPDATE checks SET kind=?, label=?, params=?, interval_sec=?, timeout_sec=?,
               enabled=?, failures_to_down=?, successes_to_up=?, degraded_latency_ms=?
               WHERE id=?""",
            (
                check.kind,
                check.label,
                json.dumps(check.params),
                int(check.interval_sec),
                float(check.timeout_sec),
                int(check.enabled),
                int(check.failures_to_down),
                int(check.successes_to_up),
                check.degraded_latency_ms,
                check.id,
            ),
        )
        return check

    def get_check(self, check_id: int) -> CheckConfig | None:
        row = self._one("SELECT * FROM checks WHERE id=?", (check_id,))
        return CheckConfig.from_row(row) if row else None

    def list_checks(self, device_id: int | None = None, enabled_only: bool = False) -> list[CheckConfig]:
        sql = "SELECT * FROM checks"
        clauses, params = [], []
        if device_id is not None:
            clauses.append("device_id=?")
            params.append(device_id)
        if enabled_only:
            clauses.append("enabled=1")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id"
        return [CheckConfig.from_row(r) for r in self._query(sql, params)]

    def delete_check(self, check_id: int) -> None:
        self._exec("DELETE FROM checks WHERE id=?", (check_id,))

    # -- results ---------------------------------------------------------------------

    def add_result(self, result: CheckResult) -> CheckResult:
        cur = self._exec(
            """INSERT INTO check_results
               (check_id, device_id, ts, kind, ok, latency_ms, packet_loss, status_code,
                message, metrics)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                result.check_id,
                result.device_id,
                result.ts,
                result.kind,
                int(result.ok),
                result.latency_ms,
                result.packet_loss,
                result.status_code,
                result.message,
                json.dumps(result.metrics),
            ),
        )
        result.id = int(cur.lastrowid)
        return result

    def recent_results(self, device_id: int, limit: int = 60) -> list[CheckResult]:
        rows = self._query(
            "SELECT * FROM check_results WHERE device_id=? ORDER BY ts DESC LIMIT ?",
            (device_id, limit),
        )
        return [CheckResult.from_row(r) for r in rows]

    def results_between(self, device_id: int, start: float, end: float) -> list[CheckResult]:
        rows = self._query(
            """SELECT * FROM check_results WHERE device_id=? AND ts>=? AND ts<=?
               ORDER BY ts""",
            (device_id, start, end),
        )
        return [CheckResult.from_row(r) for r in rows]

    def result_series(self, device_id: int, limit: int = 60) -> list[dict[str, Any]]:
        """Latency/availability series, oldest first — ready for sparklines."""
        rows = self.recent_results(device_id, limit=limit)
        series = [
            {
                "ts": r.ts,
                "ok": r.ok,
                "latency_ms": r.latency_ms,
                "message": r.message,
            }
            for r in rows
        ]
        series.reverse()
        return series

    def availability(self, device_id: int, window_sec: float = 86400.0) -> float | None:
        since = time.time() - window_sec
        row = self._one(
            """SELECT COUNT(*) AS total, COALESCE(SUM(ok),0) AS up
               FROM check_results WHERE device_id=? AND ts>=?""",
            (device_id, since),
        )
        if not row or not row["total"]:
            return None
        return round(100.0 * row["up"] / row["total"], 2)

    def prune_results(self, older_than_sec: float) -> int:
        cutoff = time.time() - older_than_sec
        cur = self._exec("DELETE FROM check_results WHERE ts < ?", (cutoff,))
        return cur.rowcount or 0

    # -- device state ----------------------------------------------------------------

    def ensure_state(self, device_id: int) -> DeviceState:
        row = self._one("SELECT * FROM device_state WHERE device_id=?", (device_id,))
        if row is None:
            self._exec(
                "INSERT OR IGNORE INTO device_state(device_id, state, since) VALUES (?,?,?)",
                (device_id, models.STATE_UNKNOWN, time.time()),
            )
            row = self._one("SELECT * FROM device_state WHERE device_id=?", (device_id,))
        return DeviceState.from_row(row)  # type: ignore[arg-type]

    def save_state(self, state: DeviceState) -> DeviceState:
        self._exec(
            """INSERT INTO device_state
               (device_id, state, since, last_check_ts, last_latency_ms,
                consecutive_failures, consecutive_successes, up_checks, total_checks,
                last_error)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(device_id) DO UPDATE SET
                 state=excluded.state, since=excluded.since,
                 last_check_ts=excluded.last_check_ts,
                 last_latency_ms=excluded.last_latency_ms,
                 consecutive_failures=excluded.consecutive_failures,
                 consecutive_successes=excluded.consecutive_successes,
                 up_checks=excluded.up_checks, total_checks=excluded.total_checks,
                 last_error=excluded.last_error""",
            (
                state.device_id,
                state.state,
                state.since,
                state.last_check_ts,
                state.last_latency_ms,
                state.consecutive_failures,
                state.consecutive_successes,
                state.up_checks,
                state.total_checks,
                state.last_error,
            ),
        )
        return state

    def get_state(self, device_id: int) -> DeviceState:
        return self.ensure_state(device_id)

    # -- per-check state -------------------------------------------------------------

    def get_check_state(self, check_id: int) -> dict[str, Any]:
        row = self._one("SELECT * FROM check_state WHERE check_id=?", (check_id,))
        if row is None:
            return {
                "check_id": check_id,
                "device_id": None,
                "state": models.STATE_UNKNOWN,
                "consecutive_failures": 0,
                "consecutive_successes": 0,
                "last_ts": None,
                "last_latency_ms": None,
                "last_ok": None,
                "last_error": "",
            }
        return dict(row)

    def save_check_state(
        self,
        check_id: int,
        device_id: int,
        state: str,
        consecutive_failures: int,
        consecutive_successes: int,
        last_ts: float | None,
        last_latency_ms: float | None,
        last_ok: bool | None,
        last_error: str,
    ) -> None:
        self._exec(
            """INSERT INTO check_state
               (check_id, device_id, state, consecutive_failures, consecutive_successes,
                last_ts, last_latency_ms, last_ok, last_error, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(check_id) DO UPDATE SET
                 device_id=excluded.device_id, state=excluded.state,
                 consecutive_failures=excluded.consecutive_failures,
                 consecutive_successes=excluded.consecutive_successes,
                 last_ts=excluded.last_ts, last_latency_ms=excluded.last_latency_ms,
                 last_ok=excluded.last_ok, last_error=excluded.last_error,
                 updated_at=excluded.updated_at""",
            (
                check_id,
                device_id,
                state,
                consecutive_failures,
                consecutive_successes,
                last_ts,
                last_latency_ms,
                None if last_ok is None else int(last_ok),
                last_error,
                time.time(),
            ),
        )

    def check_states_for_device(self, device_id: int) -> dict[int, dict[str, Any]]:
        rows = self._query("SELECT * FROM check_state WHERE device_id=?", (device_id,))
        return {r["check_id"]: dict(r) for r in rows}

    def delete_check_state(self, check_id: int) -> None:
        self._exec("DELETE FROM check_state WHERE check_id=?", (check_id,))

    def all_states(self) -> dict[int, DeviceState]:
        return {
            r["device_id"]: DeviceState.from_row(r)
            for r in self._query("SELECT * FROM device_state")
        }

    # -- events ----------------------------------------------------------------------

    def add_event(self, event: Event) -> Event:
        cur = self._exec(
            """INSERT INTO events
               (ts, device_id, device_name, kind, severity, message, details, acknowledged)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                event.ts,
                event.device_id,
                event.device_name,
                event.kind,
                event.severity,
                event.message,
                json.dumps(event.details),
                int(event.acknowledged),
            ),
        )
        event.id = int(cur.lastrowid)
        return event

    def list_events(
        self,
        limit: int = 100,
        device_id: int | None = None,
        severity: str | None = None,
        unacknowledged_only: bool = False,
    ) -> list[Event]:
        sql = "SELECT * FROM events"
        clauses, params = [], []
        if device_id is not None:
            clauses.append("device_id=?")
            params.append(device_id)
        if severity:
            clauses.append("severity=?")
            params.append(severity)
        if unacknowledged_only:
            clauses.append("acknowledged=0")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        return [Event.from_row(r) for r in self._query(sql, params)]

    def acknowledge_event(self, event_id: int, acknowledged: bool = True) -> None:
        self._exec(
            "UPDATE events SET acknowledged=? WHERE id=?", (int(acknowledged), event_id)
        )

    def acknowledge_all_events(self) -> None:
        self._exec("UPDATE events SET acknowledged=1 WHERE acknowledged=0")

    def unacknowledged_count(self) -> int:
        row = self._one("SELECT COUNT(*) AS n FROM events WHERE acknowledged=0")
        return int(row["n"]) if row else 0

    # -- templates -------------------------------------------------------------------

    def add_template(self, template: Template) -> Template:
        now = time.time()
        cur = self._exec(
            """INSERT INTO templates
               (name, vendor, description, body, variables, save_config, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                template.name,
                template.vendor,
                template.description,
                template.body,
                json.dumps(template.variables),
                int(template.save_config),
                now,
                now,
            ),
        )
        template.id = int(cur.lastrowid)
        template.created_at = template.updated_at = now
        return template

    def update_template(self, template: Template) -> Template:
        if template.id is None:
            raise ValueError("template id is required")
        now = time.time()
        self._exec(
            """UPDATE templates SET name=?, vendor=?, description=?, body=?, variables=?,
               save_config=?, updated_at=? WHERE id=?""",
            (
                template.name,
                template.vendor,
                template.description,
                template.body,
                json.dumps(template.variables),
                int(template.save_config),
                now,
                template.id,
            ),
        )
        template.updated_at = now
        return template

    def get_template(self, template_id: int) -> Template | None:
        row = self._one("SELECT * FROM templates WHERE id=?", (template_id,))
        return Template.from_row(row) if row else None

    def get_template_by_name(self, name: str) -> Template | None:
        row = self._one("SELECT * FROM templates WHERE name=?", (name,))
        return Template.from_row(row) if row else None

    def list_templates(self, vendor: str | None = None) -> list[Template]:
        if vendor:
            rows = self._query(
                "SELECT * FROM templates WHERE vendor=? OR vendor='generic' "
                "ORDER BY name COLLATE NOCASE",
                (vendor,),
            )
        else:
            rows = self._query("SELECT * FROM templates ORDER BY name COLLATE NOCASE")
        return [Template.from_row(r) for r in rows]

    def delete_template(self, template_id: int) -> None:
        self._exec("DELETE FROM templates WHERE id=?", (template_id,))

    # -- jobs ------------------------------------------------------------------------

    def create_job(self, job: Job, targets: list[JobTarget]) -> Job:
        with self._write_lock:
            cur = self.conn.execute(
                """INSERT INTO jobs
                   (created_at, started_at, finished_at, template_id, template_name, vendor,
                    body, variables, options, status, total, succeeded, failed, triggered_by)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    job.created_at,
                    job.started_at,
                    job.finished_at,
                    job.template_id,
                    job.template_name,
                    job.vendor,
                    job.body,
                    json.dumps(job.variables),
                    json.dumps(job.options),
                    job.status,
                    len(targets),
                    job.succeeded,
                    job.failed,
                    job.triggered_by,
                ),
            )
            job.id = int(cur.lastrowid)
            job.total = len(targets)
            for target in targets:
                tcur = self.conn.execute(
                    """INSERT INTO job_targets
                       (job_id, device_id, device_name, host, status, output, error, backup,
                        commands)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (
                        job.id,
                        target.device_id,
                        target.device_name,
                        target.host,
                        target.status,
                        target.output,
                        target.error,
                        target.backup,
                        json.dumps(target.commands),
                    ),
                )
                target.id = int(tcur.lastrowid)
                target.job_id = job.id
        return job

    def update_job(self, job: Job) -> Job:
        self._exec(
            """UPDATE jobs SET started_at=?, finished_at=?, status=?, total=?, succeeded=?,
               failed=?, skipped=? WHERE id=?""",
            (
                job.started_at,
                job.finished_at,
                job.status,
                job.total,
                job.succeeded,
                job.failed,
                job.skipped,
                job.id,
            ),
        )
        return job

    def get_job(self, job_id: int) -> Job | None:
        row = self._one("SELECT * FROM jobs WHERE id=?", (job_id,))
        return Job.from_row(row) if row else None

    def list_jobs(self, limit: int = 50) -> list[Job]:
        rows = self._query("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,))
        return [Job.from_row(r) for r in rows]

    def save_job_target(self, target: JobTarget) -> JobTarget:
        self._exec(
            """UPDATE job_targets SET status=?, started_at=?, finished_at=?, output=?,
               error=?, backup=?, commands=? WHERE id=?""",
            (
                target.status,
                target.started_at,
                target.finished_at,
                target.output,
                target.error,
                target.backup,
                json.dumps(target.commands),
                target.id,
            ),
        )
        return target

    def list_job_targets(self, job_id: int) -> list[JobTarget]:
        rows = self._query(
            "SELECT * FROM job_targets WHERE job_id=? ORDER BY id", (job_id,)
        )
        return [JobTarget.from_row(r) for r in rows]

    def get_job_target(self, target_id: int) -> JobTarget | None:
        row = self._one("SELECT * FROM job_targets WHERE id=?", (target_id,))
        return JobTarget.from_row(row) if row else None

    # -- backups ---------------------------------------------------------------------

    def add_backup(
        self,
        device_id: int | None,
        device_name: str,
        host: str,
        config: str,
        source: str = "manual",
    ) -> int:
        cur = self._exec(
            """INSERT INTO backups
               (device_id, device_name, host, created_at, source, config, byte_size)
               VALUES (?,?,?,?,?,?,?)""",
            (
                device_id,
                device_name,
                host,
                time.time(),
                source,
                config,
                len(config.encode("utf-8", "replace")),
            ),
        )
        return int(cur.lastrowid)

    def list_backups(self, device_id: int | None = None, limit: int = 50) -> list[dict[str, Any]]:
        if device_id is not None:
            rows = self._query(
                """SELECT id, device_id, device_name, host, created_at, source, byte_size
                   FROM backups WHERE device_id=? ORDER BY created_at DESC LIMIT ?""",
                (device_id, limit),
            )
        else:
            rows = self._query(
                """SELECT id, device_id, device_name, host, created_at, source, byte_size
                   FROM backups ORDER BY created_at DESC LIMIT ?""",
                (limit,),
            )
        return [dict(r) for r in rows]

    def get_backup(self, backup_id: int) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM backups WHERE id=?", (backup_id,))
        return dict(row) if row else None

    def latest_backup(self, device_id: int) -> dict[str, Any] | None:
        row = self._one(
            "SELECT * FROM backups WHERE device_id=? ORDER BY created_at DESC LIMIT 1",
            (device_id,),
        )
        return dict(row) if row else None

    def delete_backup(self, backup_id: int) -> None:
        self._exec("DELETE FROM backups WHERE id=?", (backup_id,))

    # -- aggregates for the dashboard ------------------------------------------------

    def overview(self) -> dict[str, Any]:
        devices = self.list_devices()
        states = self.all_states()
        counts = {models.STATE_UP: 0, models.STATE_DOWN: 0, models.STATE_DEGRADED: 0, models.STATE_UNKNOWN: 0}
        latencies: list[float] = []
        for device in devices:
            state = states.get(device.id)  # type: ignore[arg-type]
            key = state.state if state else models.STATE_UNKNOWN
            counts[key] = counts.get(key, 0) + 1
            if state and state.last_latency_ms is not None and state.state == models.STATE_UP:
                latencies.append(state.last_latency_ms)

        since = time.time() - 86400
        job_row = self._one(
            "SELECT COUNT(*) AS n FROM jobs WHERE created_at>=? AND options LIKE '%\"dry_run\": false%'",
            (since,),
        )
        return {
            "devices_total": len(devices),
            "devices_enabled": sum(1 for d in devices if d.enabled),
            "states": counts,
            "avg_latency_ms": round(sum(latencies) / len(latencies), 2) if latencies else None,
            "unacknowledged_events": self.unacknowledged_count(),
            "deploys_24h": int(job_row["n"]) if job_row else 0,
            "checks_24h": int(
                (self._one("SELECT COUNT(*) AS n FROM check_results WHERE ts>=?", (since,)) or {"n": 0})["n"]
            ),
            "backups_total": int(
                (self._one("SELECT COUNT(*) AS n FROM backups") or {"n": 0})["n"]
            ),
        }

    def device_cards(self, sparkline_points: int = 40) -> list[dict[str, Any]]:
        """Everything the dashboard needs for one device, in one pass."""
        devices = self.list_devices()
        states = self.all_states()
        checks_by_device: dict[int, list[CheckConfig]] = {}
        for check in self.list_checks():
            checks_by_device.setdefault(check.device_id, []).append(check)  # type: ignore[index]

        cards: list[dict[str, Any]] = []
        for device in devices:
            state = states.get(device.id)  # type: ignore[arg-type]
            card = device.to_dict()
            card["state"] = state.to_dict() if state else DeviceState(device_id=device.id).to_dict()  # type: ignore[arg-type]
            card["checks"] = [c.to_dict() for c in checks_by_device.get(device.id, [])]  # type: ignore[arg-type]
            card["sparkline"] = self.result_series(device.id, limit=sparkline_points)  # type: ignore[arg-type]
            card["availability_24h"] = self.availability(device.id, 86400)  # type: ignore[arg-type]
            cards.append(card)
        return cards
