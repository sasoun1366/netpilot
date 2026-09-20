"""The core ``App`` facade: the single entry point every front end shares."""

from __future__ import annotations

import asyncio
import time

import pytest

from netpilot.core import App
from netpilot.models import Device, Event, Job, Template
from netpilot.monitoring.checks import CheckResult


# ── construction & lifecycle ────────────────────────────────────────────────────────


def test_app_creates_its_data_directory(tmp_path):
    data_dir = tmp_path / "fresh"
    app = App(data_dir=data_dir, start_monitor=False)
    try:
        assert data_dir.exists()
        assert app.store.path.exists()
        assert app.monitor.running is False
    finally:
        app.store.close()


def test_start_and_stop_are_async_and_idempotent(tmp_path):
    app = App(data_dir=tmp_path / "lifecycle", start_monitor=True)

    async def scenario():
        await app.start()
        assert app.monitor.running is True
        await app.start()  # second start must not spawn a second engine
        assert app.monitor.running is True
        await app.stop()
        assert app.monitor.running is False
        await app.stop()  # stopping twice is harmless

    try:
        asyncio.run(scenario())
    finally:
        app.store.close()


def test_start_does_not_run_the_monitor_when_disabled(app):
    async def scenario():
        await app.start()
        assert app.monitor.running is False

    asyncio.run(scenario())


def test_stop_without_start_is_harmless(app):
    asyncio.run(app.stop())


# ── events fan-out ──────────────────────────────────────────────────────────────────


def test_event_subscribers_receive_events(app):
    seen: list[Event] = []
    app.events.on_event.append(seen.append)
    app._on_monitor_event(Event(kind="device", severity="info", message="hello"))
    assert [e.message for e in seen] == ["hello"]


def test_a_broken_subscriber_does_not_break_the_others(app):
    """One bad UI callback must never take the monitoring loop down with it."""
    good: list[Event] = []

    def explodes(_event: Event) -> None:
        raise RuntimeError("callback bug")

    app.events.on_event.append(explodes)
    app.events.on_event.append(good.append)

    app._on_monitor_event(Event(kind="device", severity="warning", message="still delivered"))
    assert [e.message for e in good] == ["still delivered"]


def test_job_and_result_channels_are_separate(app):
    jobs: list[Job] = []
    results: list[object] = []
    app.events.on_job.append(jobs.append)
    app.events.on_result.append(results.append)

    app.events.emit_job(Job(id=7, template_name="t", vendor="mikrotik", total=1))
    app.events.emit_result(CheckResult(ok=True, latency_ms=1.0, message="up"))

    assert jobs[0].id == 7
    assert results[0].ok is True


def test_progress_callback_may_be_absent(app):
    """The deploy engine's on_progress hook is optional; the app must supply one."""
    assert app.deploy.on_progress is not None


# ── settings round trip ─────────────────────────────────────────────────────────────


def test_settings_snapshot_is_json_serialisable(app):
    import json

    snapshot = app.settings_snapshot()
    json.dumps(snapshot)  # must not raise
    assert isinstance(snapshot, dict) and snapshot


def test_alert_settings_survive_a_reload(app):
    app.save_alerts({"enabled": True, "min_severity": "warning", "webhook_url": "http://127.0.0.1:9/x"})
    app.reload_alerts()
    assert app.alerts.config.enabled is True
    assert app.alerts.config.min_severity == "warning"
    assert app.alerts.config.webhook_url.endswith("/x")


def test_saving_alert_settings_persists_to_storage(app, tmp_path):
    app.save_alerts({"enabled": True, "min_severity": "critical"})
    stored = app.store.get_setting("notify")
    assert stored["enabled"] is True
    assert stored["min_severity"] == "critical"


# ── devices ─────────────────────────────────────────────────────────────────────────


def test_adding_a_device_attaches_monitoring_immediately(app):
    """The product promise: one add action gives you monitoring *and* config push."""
    device = asyncio.run(app.add_device({"name": "r1", "host": "10.10.0.1", "vendor": "mikrotik"}))
    checks = app.store.list_checks(device_id=device.id)
    assert checks, "adding a device must attach default monitors"
    assert device.vendor == "mikrotik"
    assert app.store.get_device(device.id) is not None


def test_auto_checks_can_be_turned_off(app):
    device = asyncio.run(
        app.add_device({"name": "bare", "host": "10.10.0.2", "vendor": "cisco"}, auto_checks=False)
    )
    assert app.store.list_checks(device_id=device.id) == []


def test_device_detail_bundles_everything_a_ui_needs(app):
    device = asyncio.run(app.add_device({"name": "r1", "host": "10.10.0.3", "vendor": "mikrotik"}))
    detail = app.device_detail(device.id)
    assert detail is not None
    for key in ("checks", "templates", "history", "events", "state", "backups", "results"):
        assert key in detail, key
    # the payload is the device itself, flattened, plus its related rows
    assert detail["name"] == "r1"
    assert detail["id"] == device.id


def test_device_detail_of_a_missing_device_is_none(app):
    assert app.device_detail(9999) is None


def test_deleting_a_device_removes_its_checks(app):
    device = asyncio.run(app.add_device({"name": "doomed", "host": "10.10.0.4", "vendor": "mikrotik"}))
    assert app.store.list_checks(device_id=device.id)
    app.delete_device(device.id)
    assert app.store.get_device(device.id) is None
    assert app.store.list_checks(device_id=device.id) == []


def test_suggest_checks_proposes_vendor_appropriate_monitors(app):
    device = asyncio.run(app.add_device({"name": "r1", "host": "10.10.0.5", "vendor": "cisco"},
                                        auto_checks=False))
    suggestions = app.suggest_checks(device.id)
    kinds = {s["kind"] for s in suggestions}
    assert "icmp" in kinds, "every device gets a reachability monitor"
    assert "tcp" in kinds, "and a port monitor so an up-but-unreachable host is caught"
    assert all(s["device_id"] == device.id for s in suggestions)


# ── checks ──────────────────────────────────────────────────────────────────────────


def test_check_crud_through_the_app(app):
    device = asyncio.run(app.add_device({"name": "r1", "host": "10.10.0.6", "vendor": "mikrotik"}))
    check = app.add_check({"device_id": device.id, "kind": "tcp", "params": {"port": 443}})
    assert check.id is not None

    updated = app.update_check(check.id, {"interval_sec": 120, "enabled": False})
    assert updated.interval_sec == 120
    assert updated.enabled is False

    app.delete_check(check.id)
    assert app.store.get_check(check.id) is None


# ── templates ───────────────────────────────────────────────────────────────────────


def test_template_library_is_vendor_filtered(app):
    all_templates = app.template_library()
    assert len(all_templates) >= 10
    mikrotik = app.template_library("mikrotik")
    assert all(t["vendor"] in ("mikrotik", "generic") for t in mikrotik)


def test_saved_template_shadows_a_builtin_of_the_same_name(app):
    saved = app.save_template(
        {"name": "NTP servers", "vendor": "mikrotik", "body": "/system ntp client set enabled=yes"}
    )
    found = app.find_template(template_name="NTP servers", vendor="mikrotik")
    assert found is not None
    assert found.id == saved.id, "a database template must win over the built-in"


def test_find_template_by_id(app):
    saved = app.save_template({"name": "mine", "vendor": "cisco", "body": "hostname x"})
    assert app.find_template(template_id=saved.id).id == saved.id


def test_find_template_returns_none_for_a_miss(app):
    assert app.find_template(template_name="nope", vendor="mikrotik") is None
    assert app.find_template(template_id=4242) is None


def test_preview_template_reports_commands_without_touching_a_device(app):
    preview = app.preview_template(None, "/system ntp client set enabled=yes", "mikrotik", {})
    assert preview["commands"] == ["/system ntp client set enabled=yes"]
    assert preview["command_count"] == 1


def test_preview_template_flags_unresolved_variables(app):
    """A preview shows what *would* be sent — and names the gap loudly."""
    preview = app.preview_template(None, "/ip address add address={{ cidr }}", "mikrotik", {})
    assert preview["unresolved"] == ["cidr"]
    assert preview["commands"] == ["/ip address add address={{ cidr }}"]


def test_a_deploy_with_unresolved_variables_skips_the_device(app):
    """Never let an unsubstituted placeholder reach a router."""
    asyncio.run(app.add_device({"name": "r1", "host": "10.70.0.1", "vendor": "mikrotik"}))
    job = app.create_deploy(
        body="/ip address add address={{ cidr }}",
        vendor="mikrotik",
        match_all=True,
        options={"dry_run": True},
    )
    target = app.job_detail(job.id)["targets"][0]
    assert target["status"] == "skipped"
    assert "unresolved variables" in target["error"]
    assert target["commands"] == []


def test_deleting_a_builtin_template_is_a_no_op(app):
    """Built-ins are code, not rows: a stray delete must not explode."""
    app.delete_template(9999)


# ── targeting ───────────────────────────────────────────────────────────────────────


def test_resolve_targets_by_id_tag_and_all(app):
    first = asyncio.run(app.add_device({"name": "a", "host": "10.20.0.1", "vendor": "mikrotik",
                                        "tags": ["core"]}))
    second = asyncio.run(app.add_device({"name": "b", "host": "10.20.0.2", "vendor": "mikrotik",
                                         "tags": ["edge"]}))

    assert {d.id for d in app.resolve_targets([first.id], None, False)} == {first.id}
    assert {d.id for d in app.resolve_targets(None, ["core"], False)} == {first.id}
    assert {d.id for d in app.resolve_targets(None, None, True)} == {first.id, second.id}
    assert app.resolve_targets(None, None, False) == []


def test_resolve_targets_rejects_an_empty_selection(app):
    """An accidental empty form must not deploy to the whole inventory."""
    asyncio.run(app.add_device({"name": "r1", "host": "10.71.0.1", "vendor": "mikrotik"}))
    assert app.resolve_targets(None, None, False) == []
    assert app.resolve_targets([], [], False) == []


def test_resolve_targets_with_an_unknown_id(app):
    assert app.resolve_targets([987654], None, False) == []


def test_resolve_targets_by_tag_requires_every_tag_when_match_all(app):
    asyncio.run(app.add_device({"name": "a", "host": "10.71.0.2", "vendor": "mikrotik",
                                "tags": ["core", "edge"]}))
    asyncio.run(app.add_device({"name": "b", "host": "10.71.0.3", "vendor": "mikrotik",
                                "tags": ["edge"]}))
    assert len(app.resolve_targets(None, ["core", "edge"], True)) == 1, "match_all = every tag"
    assert len(app.resolve_targets(None, ["core", "edge"], False)) == 2, "otherwise any tag"


# ── deploy create ───────────────────────────────────────────────────────────────────


def test_create_deploy_refuses_an_empty_selection(app):
    with pytest.raises(ValueError):
        app.create_deploy(body="/x", vendor="mikrotik", device_ids=[])


def test_create_deploy_rejects_monitoring_only_devices(app):
    asyncio.run(app.add_device({"name": "shell", "host": "10.30.0.1", "vendor": "generic"}))
    with pytest.raises(ValueError) as excinfo:
        app.create_deploy(body="ls", vendor="generic", match_all=True)
    assert "configuration push" in str(excinfo.value)


def test_create_deploy_marks_generic_devices_as_skipped(app):
    good = asyncio.run(app.add_device({"name": "r1", "host": "10.30.0.2", "vendor": "mikrotik"}))
    asyncio.run(app.add_device({"name": "shell", "host": "10.30.0.3", "vendor": "generic"}))

    job = app.create_deploy(
        body="/system ntp client set enabled=yes",
        vendor="mikrotik",
        match_all=True,
        options={"dry_run": True},
    )
    # The generic shell is excluded from the push but still listed on the job, so the
    # operator sees why their device was not touched.
    assert job.total == 2
    assert job.skipped == 1
    detail = app.job_detail(job.id)
    by_name = {t["device_name"]: t for t in detail["targets"]}
    assert "monitoring only" in by_name["shell"]["error"]
    assert by_name["r1"]["status"] == "pending"


def test_create_deploy_renders_per_target_commands(app):
    asyncio.run(app.add_device({"name": "r1", "host": "10.30.0.4", "vendor": "mikrotik"}))
    job = app.create_deploy(
        body="/system ntp client set enabled=yes",
        vendor="mikrotik",
        match_all=True,
        options={"dry_run": True},
    )
    detail = app.job_detail(job.id)
    assert detail["targets"][0]["commands"] == ["/system ntp client set enabled=yes"]


def test_create_deploy_uses_template_defaults(app):
    asyncio.run(app.add_device({"name": "r1", "host": "10.30.0.5", "vendor": "mikrotik"}))
    job = app.create_deploy(
        body="",
        vendor="mikrotik",
        match_all=True,
        template_name="NTP servers",
        options={"dry_run": True},
    )
    commands = app.job_detail(job.id)["targets"][0]["commands"]
    assert any("pool.ntp.org" in command for command in commands)
    assert not any("{{" in command for command in commands)


def test_create_deploy_honours_caller_variable_overrides(app):
    asyncio.run(app.add_device({"name": "r1", "host": "10.30.0.6", "vendor": "mikrotik"}))
    job = app.create_deploy(
        body="",
        vendor="mikrotik",
        match_all=True,
        template_name="NTP servers",
        variables={"ntp1": "10.9.9.9"},
        options={"dry_run": True},
    )
    commands = app.job_detail(job.id)["targets"][0]["commands"]
    assert any("10.9.9.9" in command for command in commands)
    assert any("time.cloudflare.com" in command for command in commands), "other defaults kept"


def test_job_detail_and_diff_for_a_missing_job(app):
    assert app.job_detail(4242) is None
    assert app.job_target_diff(4242) is None


# ── overview ────────────────────────────────────────────────────────────────────────


def test_overview_counts_devices_events_and_jobs(app):
    asyncio.run(app.add_device({"name": "r1", "host": "10.40.0.1", "vendor": "mikrotik"}))
    asyncio.run(app.add_device({"name": "r2", "host": "10.40.0.2", "vendor": "cisco"}))

    overview = app.overview()
    assert overview["devices_total"] == 2
    assert overview["states"]["unknown"] == 2
    assert overview["unacknowledged_events"] >= 0
    assert overview["backups_total"] == 0


def test_overview_is_stable_on_an_empty_database(app):
    overview = app.overview()
    assert overview["devices_total"] == 0
    assert overview["monitor_running"] is False
    import json

    json.dumps(overview)


# ── front-end parity ────────────────────────────────────────────────────────────────


def test_web_and_desktop_share_the_same_facade():
    """Both UIs must be thin: assert the entry points take an App, not a private path."""
    import inspect

    pytest.importorskip("PyQt6", reason="the desktop extra is optional")
    from netpilot.desktop import bridge
    from netpilot.web import app as web_app

    assert "create_app" in dir(web_app)
    signature = inspect.signature(web_app.create_app)
    assert "data_dir" in signature.parameters
    assert hasattr(bridge, "CoreThread")


def test_a_device_and_its_template_library_agree(app):
    """A device must only be offered templates its own adapter can actually render."""
    device = asyncio.run(app.add_device({"name": "c1", "host": "10.50.0.1", "vendor": "cisco"}))
    detail = app.device_detail(device.id)
    for template in detail["templates"]:
        assert template["vendor"] in ("cisco", "generic"), template


def test_monitor_and_deploy_share_one_event_channel(app):
    """A UI subscribing once must see both monitoring and deploy activity."""
    seen: list[Event] = []
    app.events.on_event.append(seen.append)
    assert app.monitor.on_event == app._on_monitor_event
    assert app.deploy.on_event == app._on_monitor_event


def test_started_at_is_recorded(app):
    assert isinstance(app._started_at, float)
    assert app._started_at <= time.time()


def test_store_and_app_share_one_file(tmp_path):
    app = App(data_dir=tmp_path / "shared", start_monitor=False)
    try:
        app.store.add_device(Device(name="d", host="10.60.0.1", vendor="mikrotik"))
        second = App(data_dir=tmp_path / "shared", start_monitor=False)
        try:
            assert [d.name for d in second.store.list_devices()] == ["d"]
        finally:
            second.store.close()
    finally:
        app.store.close()


# ── mixed vendors in one job ────────────────────────────────────────────────────────


def test_one_template_name_renders_per_vendor(app):
    """The library ships "NTP servers" for both vendors under one name.

    A group containing a RouterOS box and an IOS box must get each one's own syntax —
    not one vendor's commands sprayed across the fleet.
    """
    mikrotik = asyncio.run(
        app.add_device({"name": "r1", "host": "10.80.0.1", "vendor": "mikrotik"})
    )
    cisco = asyncio.run(app.add_device({"name": "s1", "host": "10.80.0.2", "vendor": "cisco"}))

    job = app.create_deploy(
        body="",
        vendor="mikrotik",
        device_ids=[mikrotik.id, cisco.id],
        template_name="NTP servers",
        options={"dry_run": True},
    )
    commands = {t["device_name"]: t["commands"] for t in app.job_detail(job.id)["targets"]}

    assert any(c.startswith("/system ntp") for c in commands["r1"])
    assert any(c.startswith("ntp server") for c in commands["s1"])
    assert not any(c.startswith("/system") for c in commands["s1"])


def test_a_device_whose_vendor_has_no_variant_is_skipped(app):
    """A RouterOS-only template selected against an IOS device: skip it, say why."""
    asyncio.run(app.add_device({"name": "r1", "host": "10.80.0.6", "vendor": "mikrotik"}))
    asyncio.run(app.add_device({"name": "s1", "host": "10.80.0.3", "vendor": "cisco"}))
    job = app.create_deploy(
        body="",
        vendor="mikrotik",
        device_ids=[1, 2],
        template_name="DHCP server for a VLAN",  # RouterOS only
        options={"dry_run": True},
    )
    targets = {t["device_name"]: t for t in app.job_detail(job.id)["targets"]}
    assert targets["r1"]["status"] == "pending"
    assert targets["s1"]["status"] == "skipped"
    assert "no 'DHCP server for a VLAN' template" in targets["s1"]["error"]


def test_a_vendor_specific_template_never_targets_another_vendor(app):
    asyncio.run(app.add_device({"name": "s1", "host": "10.80.0.4", "vendor": "cisco"}))
    with pytest.raises(ValueError, match="support configuration push"):
        app.create_deploy(
            body="",
            vendor="mikrotik",
            device_ids=[1],
            template_id=app.find_template(template_name="DHCP server for a VLAN",
                                          vendor="mikrotik").id,
            options={"dry_run": True},
        )


async def _no_backup(*_args, **_kwargs):
    return {"ok": True, "bytes": 0, "lines": 0}


def test_the_planned_commands_are_what_runs(app, monkeypatch):
    """No surprise between the reviewed plan and the executed command list."""
    asyncio.run(app.add_device({"name": "r1", "host": "10.80.0.5", "vendor": "mikrotik"}))
    job = app.create_deploy(
        body="", vendor="mikrotik", device_ids=[1],
        template_name="NTP servers", options={"dry_run": False, "backup_before": False,
                                             "auto_rollback": False, "save_config": False},
    )
    planned = app.job_detail(job.id)["targets"][0]["commands"]

    seen: list[list[str]] = []

    class RecordingAdapter:
        name = "mikrotik"
        label = "MikroTik"
        supports_deploy = True
        rollback_support = "manual"

        def __init__(self, device, credential=None, timeout=60.0):  # noqa: ANN001
            self.device = device

        def connect(self) -> None:
            pass

        def close(self) -> None:
            pass

        def fetch_config(self) -> str:
            return ""

        def apply_commands(self, commands, save=True):  # noqa: ANN001
            seen.append(list(commands))
            return []

        def save(self) -> bool:
            return True

    import netpilot.deploy.engine as engine

    monkeypatch.setattr(engine, "get_adapter", lambda _vendor: RecordingAdapter)
    monkeypatch.setattr(engine, "capture_backup", _no_backup)
    asyncio.run(app.run_deploy(job.id))
    assert seen == [planned]
