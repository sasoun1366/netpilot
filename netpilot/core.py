"""Application core — the single object both UIs (web and desktop) drive.

Everything a UI needs is a method here: add a device *and* have it start being monitored,
fire a drill-down probe, render a template preview, run a bulk deploy, restore a backup.
Keeping that logic out of the UIs is what lets the web dashboard and the desktop app stay
feature-identical instead of slowly drifting apart.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from .adapters import available_vendors, get_adapter, interactive_probe, resolve_vendor
from .alerts import AlertRouter, NotifyConfig
from .db import Store, default_data_dir
from .deploy.engine import DeployEngine, DeployOptions, capture_backup, restore_backup
from .deploy.templates import merged_templates
from .models import (
    CHECK_KINDS,
    CheckConfig,
    Credential,
    Device,
    Event,
    Job,
    Template,
)
from .monitoring.monitor import Monitor, bootstrap_checks

log = logging.getLogger("netpilot.core")

#: Per-vendor default ports/monitoring hints used by the "add device" wizard.
VENDOR_DEFAULTS: dict[str, dict[str, Any]] = {
    "mikrotik": {"ssh_port": 22, "mgmt_hint": "http://{host}"},
    "cisco": {"ssh_port": 22, "mgmt_hint": "https://{host}"},
    "generic": {"ssh_port": 22, "mgmt_hint": ""},
}


@dataclass
class AppEvents:
    """Callbacks a UI can subscribe to; all optional."""

    on_event: list[Callable[[Event], None]] = field(default_factory=list)
    on_job: list[Callable[[Job], None]] = field(default_factory=list)
    on_result: list[Callable[[Any], None]] = field(default_factory=list)

    def emit_event(self, event: Event) -> None:
        for callback in list(self.on_event):
            try:
                callback(event)
            except Exception:  # noqa: BLE001 - a bad subscriber must not break the app
                log.exception("event subscriber failed")

    def emit_job(self, job: Job) -> None:
        for callback in list(self.on_job):
            try:
                callback(job)
            except Exception:  # noqa: BLE001
                log.exception("job subscriber failed")

    def emit_result(self, result: Any) -> None:
        for callback in list(self.on_result):
            try:
                callback(result)
            except Exception:  # noqa: BLE001
                log.exception("result subscriber failed")


class App:
    """Owns storage, the monitor, the deploy engine and the alert router."""

    def __init__(
        self,
        data_dir: str | os.PathLike[str] | None = None,
        db_path: str | os.PathLike[str] | None = None,
        start_monitor: bool = True,
    ) -> None:
        self.store = Store(path=db_path, data_dir=data_dir)
        self.events = AppEvents()
        self.alerts = AlertRouter(NotifyConfig.from_env())
        self.monitor = Monitor(
            self.store,
            on_event=self._on_monitor_event,
            on_result=self.events.emit_result,
        )
        self.deploy = DeployEngine(
            self.store,
            on_event=self._on_monitor_event,
            on_progress=self.events.emit_job,
        )
        self.start_monitor = start_monitor
        self._started_at = time.time()

    # -- lifecycle -------------------------------------------------------------------

    async def start(self) -> None:
        self.reload_alerts()
        if self.start_monitor:
            self.monitor.start()

    async def stop(self) -> None:
        if self.monitor.running:
            await self.monitor.stop()

    def _on_monitor_event(self, event: Event) -> None:
        self.events.emit_event(event)
        # Alert delivery is blocking I/O: hand it to a thread so monitoring never stalls.
        if self.alerts.config.enabled:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                self.alerts.dispatch(event)
            else:
                loop.run_in_executor(None, self._safe_dispatch, event)

    def _safe_dispatch(self, event: Event) -> None:
        try:
            report = self.alerts.dispatch(event)
            if report.get("sent"):
                self.events.emit_event(
                    Event(
                        kind="notify",
                        severity="info",
                        message=f"alert delivered for: {event.message[:120]}",
                        details=report,
                    )
                )
        except Exception:  # noqa: BLE001
            log.exception("alert dispatch failed")

    # -- settings --------------------------------------------------------------------

    def reload_alerts(self) -> None:
        stored = self.store.get_setting("notify") or {}
        config = NotifyConfig.from_dict(stored)
        env = NotifyConfig.from_env()
        if env.enabled:
            # Environment variables win so containerised deployments can be configured
            # without a writable database.
            config = env
        self.alerts.config = config

    def save_alerts(self, data: dict[str, Any]) -> NotifyConfig:
        self.alerts.config = NotifyConfig.from_dict(data)
        self.store.set_setting("notify", self.alerts.config.to_public_dict())
        return self.alerts.config

    def settings_snapshot(self) -> dict[str, Any]:
        return {
            "data_dir": str(self.store.data_dir),
            "db_path": str(self.store.path),
            "uptime_sec": round(time.time() - self._started_at, 1),
            "monitor_running": self.monitor.running,
            "monitor_stats": {
                "checks_run": self.monitor.checks_run,
                "results_written": self.monitor.results_written,
                "concurrency": self.monitor.concurrency,
            },
            "vendors": available_vendors(),
            "check_kinds": list(CHECK_KINDS),
            "notify": self.alerts.config.to_public_dict(),
            "notify_status": self.alerts.status(),
        }

    # -- credentials -----------------------------------------------------------------

    def add_credential(self, payload: dict[str, Any]) -> Credential:
        cred = Credential(
            name=(payload.get("name") or "").strip(),
            username=(payload.get("username") or "").strip(),
            password=payload.get("password"),
            enable_password=payload.get("enable_password"),
            key_path=payload.get("key_path"),
            key_passphrase=payload.get("key_passphrase"),
            snmp_community=payload.get("snmp_community"),
            snmp_version=payload.get("snmp_version") or "2c",
            snmp_auth_protocol=payload.get("snmp_auth_protocol"),
            snmp_priv_protocol=payload.get("snmp_priv_protocol"),
            snmp_priv_password=payload.get("snmp_priv_password"),
        )
        if not cred.name:
            raise ValueError("credential name is required")
        return self.store.add_credential(cred)

    def update_credential(self, cred_id: int, payload: dict[str, Any]) -> Credential:
        existing = self.store.get_credential(cred_id)
        if existing is None:
            raise KeyError(f"credential {cred_id} not found")
        for key in (
            "name",
            "username",
            "password",
            "enable_password",
            "key_path",
            "key_passphrase",
            "snmp_community",
            "snmp_version",
            "snmp_auth_protocol",
            "snmp_priv_protocol",
            "snmp_priv_password",
        ):
            if key in payload:
                setattr(existing, key, payload[key])
        return self.store.update_credential(existing)

    # -- devices ---------------------------------------------------------------------

    async def add_device(self, payload: dict[str, Any], auto_checks: bool = True) -> Device:
        """Create a device and immediately start monitoring it.

        This is the heart of the request that shaped netpilot: adding a box to the
        inventory is not a separate chore from watching it — the moment it exists, the
        scheduler has it queued, and the same page can push config to it.
        """
        vendor = resolve_vendor(payload.get("vendor") or "generic")
        if not vendor:
            raise ValueError(f"unknown vendor: {payload.get('vendor')!r}")
        defaults = VENDOR_DEFAULTS.get(vendor, {})
        device = Device(
            name=(payload.get("name") or payload.get("host") or "").strip(),
            host=(payload.get("host") or "").strip(),
            vendor=vendor,
            ssh_port=int(payload.get("ssh_port") or defaults.get("ssh_port", 22)),
            credential_id=payload.get("credential_id") or None,
            tags=list(payload.get("tags") or []),
            notes=payload.get("notes") or "",
            site=payload.get("site") or "",
            enabled=bool(payload.get("enabled", True)),
            snmp_port=int(payload.get("snmp_port") or 161),
            mgmt_url=payload.get("mgmt_url") or None,
        )
        if not device.host:
            raise ValueError("device host is required")
        if not device.name:
            device.name = device.host
        if not device.mgmt_url and defaults.get("mgmt_hint") and vendor == "generic":
            device.mgmt_url = None

        if payload.get("id") or payload.get("device_id"):
            device.id = int(payload.get("id") or payload["device_id"])
            self.store.update_device(device)
        else:
            self.store.add_device(device)

        if auto_checks and not self.store.list_checks(device_id=device.id):
            checks = bootstrap_checks(self.store, device)
            self._log_device_event(device, f"{len(checks)} monitor(s) attached to {device.name}")

        if self.monitor.running:
            self.monitor.reload()
        return device

    async def update_device(self, device_id: int, payload: dict[str, Any]) -> Device:
        existing = self.store.get_device(device_id)
        if existing is None:
            raise KeyError(f"device {device_id} not found")
        for key in ("name", "host", "vendor", "ssh_port", "credential_id", "tags", "notes", "site", "enabled", "snmp_port", "mgmt_url"):
            if key in payload:
                value = payload[key]
                if key == "vendor":
                    value = resolve_vendor(value) or existing.vendor
                if key == "credential_id":
                    value = value or None
                setattr(existing, key, value)
        self.store.update_device(existing)
        if self.monitor.running:
            self.monitor.reload()
        return existing

    def delete_device(self, device_id: int) -> None:
        self.store.delete_device(device_id)
        if self.monitor.running:
            self.monitor.reload()

    async def test_device(self, device_id: int) -> dict[str, Any]:
        """SSH in, read identity, report back — the "Test connection" button."""
        device = self.store.get_device(device_id)
        if device is None:
            return {"ok": False, "error": "device not found"}
        credential = self.store.get_credential(device.credential_id) if device.credential_id else None
        return await asyncio.to_thread(interactive_probe, device, credential, 12.0)

    async def probe_device(self, device_id: int) -> list[dict[str, Any]]:
        """Run every enabled check right now (the "Check now" button)."""
        results = await self.monitor.check_device(device_id)
        return [r.to_dict() for r in results]

    def device_detail(self, device_id: int, history: int = 120) -> dict[str, Any] | None:
        device = self.store.get_device(device_id)
        if device is None:
            return None
        data = device.to_dict()
        data["state"] = self.store.get_state(device_id).to_dict()
        data["checks"] = [c.to_dict() for c in self.store.list_checks(device_id=device_id)]
        data["check_states"] = self.store.check_states_for_device(device_id)
        data["history"] = self.store.result_series(device_id, limit=history)
        data["results"] = [r.to_dict() for r in self.store.recent_results(device_id, limit=50)]
        data["availability_24h"] = self.store.availability(device_id, 86400)
        data["availability_7d"] = self.store.availability(device_id, 7 * 86400)
        data["events"] = [e.to_dict() for e in self.store.list_events(limit=25, device_id=device_id)]
        data["backups"] = self.store.list_backups(device_id=device_id, limit=10)
        # Bodies and variables travel with the list: both UIs render a per-device deploy
        # box straight from this payload, and a built-in template has no id to re-fetch by.
        data["templates"] = [
            {
                "id": t.id,
                "name": t.name,
                "vendor": t.vendor,
                "description": t.description,
                "body": t.body,
                "variables": dict(t.variables),
                "save_config": t.save_config,
            }
            for t in merged_templates(self.store, vendor=device.vendor)
        ]
        return data

    # -- checks ----------------------------------------------------------------------

    def add_check(self, payload: dict[str, Any]) -> CheckConfig:
        check = CheckConfig(
            device_id=int(payload["device_id"]),
            kind=payload.get("kind") or "icmp",
            label=payload.get("label") or "",
            params=payload.get("params") or {},
            interval_sec=int(payload.get("interval_sec") or 60),
            timeout_sec=float(payload.get("timeout_sec") or 5.0),
            enabled=bool(payload.get("enabled", True)),
            failures_to_down=int(payload.get("failures_to_down") or 2),
            successes_to_up=int(payload.get("successes_to_up") or 1),
            degraded_latency_ms=payload.get("degraded_latency_ms"),
        )
        created = self.store.add_check(check)
        if self.monitor.running:
            self.monitor.reload()
        return created

    def update_check(self, check_id: int, payload: dict[str, Any]) -> CheckConfig:
        check = self.store.get_check(check_id)
        if check is None:
            raise KeyError(f"check {check_id} not found")
        for key in (
            "kind",
            "label",
            "params",
            "interval_sec",
            "timeout_sec",
            "enabled",
            "failures_to_down",
            "successes_to_up",
            "degraded_latency_ms",
        ):
            if key in payload:
                setattr(check, key, payload[key])
        self.store.update_check(check)
        if self.monitor.running:
            self.monitor.reload()
        return check

    def delete_check(self, check_id: int) -> None:
        self.store.delete_check(check_id)
        if self.monitor.running:
            self.monitor.reload()

    def suggest_checks(self, device_id: int) -> list[dict[str, Any]]:
        """The starter check set the wizard would create, for one-click acceptance."""
        device = self.store.get_device(device_id)
        if device is None:
            return []
        from .monitoring.checks import default_checks_for

        return [c.to_dict() for c in default_checks_for(device)]

    # -- templates & deploy ----------------------------------------------------------

    def template_library(self, vendor: str | None = None) -> list[dict[str, Any]]:
        out = []
        for template in merged_templates(self.store, vendor=vendor):
            data = template.to_dict()
            data["builtin"] = template.id is None
            out.append(data)
        return out

    def save_template(self, payload: dict[str, Any]) -> Template:
        name = (payload.get("name") or "").strip()
        if not name:
            raise ValueError("template name is required")
        template_id = payload.get("id")
        template = Template(
            id=int(template_id) if template_id else None,
            name=name,
            vendor=resolve_vendor(payload.get("vendor") or "generic") or "generic",
            description=payload.get("description") or "",
            body=payload.get("body") or "",
            variables=payload.get("variables") or {},
            save_config=bool(payload.get("save_config", True)),
        )
        if template.id:
            return self.store.update_template(template)

        # Saving a template whose name matches a built-in *overrides* it rather than
        # creating a confusing duplicate.
        existing = self.store.get_template_by_name(name)
        if existing is not None and existing.vendor == template.vendor:
            template.id = existing.id
            return self.store.update_template(template)
        return self.store.add_template(template)

    def delete_template(self, template_id: int) -> None:
        self.store.delete_template(template_id)

    def find_template(
        self,
        template_id: int | None = None,
        template_name: str | None = None,
        vendor: str | None = None,
    ) -> Template | None:
        """Look a template up by id, falling back to name.

        Built-in templates live in code rather than the database, so they have no id —
        without the name fallback, ``deploy --template "NTP servers"`` would silently lose
        the template's default variables.
        """
        if template_id:
            found = self.store.get_template(int(template_id))
            if found is not None:
                return found
        if template_name:
            resolved = resolve_vendor(vendor) if vendor else None
            for candidate in merged_templates(self.store, vendor=resolved):
                if candidate.name != template_name:
                    continue
                if resolved and candidate.vendor not in (resolved, "generic"):
                    continue
                return candidate
        return None

    def preview_template(self, template_id: int | None, body: str, vendor: str, variables: dict[str, str] | None = None) -> dict[str, Any]:
        template = self.store.get_template(template_id) if template_id else None
        if template is None:
            template = Template(name="(ad-hoc)", vendor=vendor, body=body, variables={})
        test_device = Device(name="preview", host="0.0.0.0", vendor=vendor)
        return self.deploy.preview(template, test_device, variables)

    def resolve_targets(
        self,
        device_ids: Sequence[int] | None,
        tags: Sequence[str] | None = None,
        match_all: bool = False,
    ) -> list[Device]:
        """Turn a UI/CLI selection into devices.

        ``match_all`` is the only thing that may select *everything*: an empty selection
        must never be read as "all devices", or a half-filled form would push config to
        the whole inventory.
        """
        if device_ids:
            devices = [self.store.get_device(i) for i in device_ids]
            return [d for d in devices if d is not None]
        if tags:
            return [
                d
                for d in self.store.list_devices(enabled_only=True)
                if d.matches_tags(list(tags), match_all)
            ]
        if match_all:
            return self.store.list_devices(enabled_only=True)
        return []

    def create_deploy(
        self,
        body: str,
        vendor: str,
        device_ids: Sequence[int] | None = None,
        tags: Sequence[str] | None = None,
        match_all: bool = False,
        variables: dict[str, str] | None = None,
        options: dict[str, Any] | None = None,
        template_id: int | None = None,
        template_name: str | None = None,
        triggered_by: str = "manual",
    ) -> Job:
        template = self.find_template(template_id, template_name, vendor)
        if template is None:
            template = Template(name="(ad-hoc)", vendor=vendor, body=body, variables={})
        elif not (body or "").strip():
            # A caller that names a template should not also have to repeat its body.
            body = template.body
        devices = self.resolve_targets(device_ids, tags, match_all)
        if not devices:
            raise ValueError("no devices matched the selection")
        # Drop devices whose vendor cannot take a push, so the job summary is honest
        # about what will actually happen.
        usable, skipped = [], []
        for device in devices:
            if get_adapter(device.vendor).supports_deploy:
                usable.append(device)
            else:
                skipped.append(device)
        if not usable:
            raise ValueError(
                "none of the selected devices support configuration push "
                f"({len(skipped)} skipped: generic SSH shell)"
            )
        return self.deploy.plan(
            template=template,
            body=body,
            devices=usable,
            variables=variables,
            options=DeployOptions.from_dict(options),
            triggered_by=triggered_by,
        )

    async def run_deploy(self, job_id: int) -> Job | None:
        await self.deploy.run(job_id)
        return self.store.get_job(job_id)

    def job_detail(self, job_id: int) -> dict[str, Any] | None:
        job = self.store.get_job(job_id)
        if job is None:
            return None
        data = job.to_dict()
        data["targets"] = [t.to_dict() for t in self.store.list_job_targets(job_id)]
        return data

    def job_target_diff(self, target_id: int) -> dict[str, Any] | None:
        """Show what changed on one device: stored pre-change config vs. the commands sent."""
        target = self.store.get_job_target(target_id)
        if target is None:
            return None
        return {
            "target": target.to_dict(),
            "backup_available": bool(target.backup),
            "backup_lines": target.backup.count("\n") + 1 if target.backup else 0,
            "commands": target.commands,
        }

    # -- backups ---------------------------------------------------------------------

    async def backup_device(self, device_id: int) -> dict[str, Any]:
        device = self.store.get_device(device_id)
        if device is None:
            return {"ok": False, "error": "device not found"}
        return await capture_backup(self.store, device, source="manual")

    async def restore(self, backup_id: int, dry_run: bool = True) -> dict[str, Any]:
        return await restore_backup(self.store, backup_id, dry_run=dry_run)

    # -- dashboard -------------------------------------------------------------------

    def overview(self) -> dict[str, Any]:
        data = self.store.overview()
        data["devices"] = self.store.device_cards()
        data["events"] = [e.to_dict() for e in self.store.list_events(limit=40)]
        data["recent_jobs"] = [j.to_dict() for j in self.store.list_jobs(limit=8)]
        data["tags"] = self.store.all_tags()
        data["server_time"] = time.time()
        data["monitor_running"] = self.monitor.running
        return data

    def _log_device_event(self, device: Device, message: str) -> None:
        event = Event(
            device_id=device.id,
            device_name=device.name,
            kind="device",
            severity="info",
            message=message,
        )
        self.store.add_event(event)
        self.events.emit_event(event)


def create_app(**kwargs: Any) -> App:
    """Convenience factory used by the CLI and the desktop entry point."""
    return App(**kwargs)


def data_dir_from_env() -> str:
    return str(default_data_dir())
