"""Storage layer."""

from __future__ import annotations

import sqlite3
import time

import pytest

from netpilot.db import DeviceExistsError, Store
from netpilot.models import (
    CheckConfig,
    CheckResult,
    Credential,
    Device,
    Event,
    Job,
    JobTarget,
    Template,
)


# ── devices ──────────────────────────────────────────────────────────────────────────


def test_device_roundtrip(store):
    device = store.add_device(Device(name="r1", host="10.0.0.1", vendor="mikrotik", tags=["a", "b"]))
    assert device.id == 1
    fetched = store.get_device(device.id)
    assert fetched is not None
    assert fetched.name == "r1"
    assert fetched.tags == ["a", "b"]
    assert fetched.enabled is True


def test_duplicate_host_and_port_is_rejected(store):
    store.add_device(Device(name="r1", host="10.0.0.1", ssh_port=22))
    with pytest.raises(DeviceExistsError) as excinfo:
        store.add_device(Device(name="r1-again", host="10.0.0.1", ssh_port=22))
    assert "already in the inventory" in str(excinfo.value)


def test_same_host_different_port_is_a_different_device(store):
    store.add_device(Device(name="r1", host="10.0.0.1", ssh_port=22))
    second = store.add_device(Device(name="r1-alt", host="10.0.0.1", ssh_port=2222))
    assert second.id == 2


def test_search_and_filter(store):
    store.add_device(Device(name="core", host="10.0.0.1", tags=["core"], site="tokyo"))
    store.add_device(Device(name="edge", host="10.0.0.2", tags=["edge"], site="osaka", enabled=False))
    assert len(store.list_devices()) == 2
    assert len(store.list_devices(enabled_only=True)) == 1
    assert len(store.list_devices(search="tokyo")) == 1
    assert len(store.list_devices(tags=["core"])) == 1
    assert store.all_tags() == ["core", "edge"]


def test_tag_matching(store):
    device = store.add_device(Device(name="r1", host="10.0.0.1", tags=["core", "lab"]))
    assert device.matches_tags(["core"]) is True
    assert device.matches_tags(["cor"]) is False
    assert device.matches_tags(["core", "lab"], match_all=True) is True
    assert device.matches_tags(["core", "missing"], match_all=True) is False


def test_delete_device_cascades_to_checks(store):
    device = store.add_device(Device(name="r1", host="10.0.0.1"))
    store.add_check(CheckConfig(device_id=device.id, kind="icmp"))
    assert len(store.list_checks(device_id=device.id)) == 1
    store.delete_device(device.id)
    assert store.list_checks(device_id=device.id) == []


# ── credentials ──────────────────────────────────────────────────────────────────────


def test_credentials_are_encrypted_at_rest(store, tmp_path):
    store.add_credential(Credential(name="lab", username="admin", password="plaintext-secret"))
    raw = sqlite3.connect(store.path).execute("SELECT password FROM credentials").fetchone()[0]
    assert raw is not None
    assert "plaintext-secret" not in raw
    assert raw.startswith("enc:")


def test_decryption_returns_the_original(store):
    cred = store.add_credential(Credential(name="lab", username="admin", password="pw", enable_password="en"))
    fetched = store.get_credential(cred.id)
    assert fetched.password == "pw"
    assert fetched.enable_password == "en"


def test_redacted_dict_hides_secrets(store):
    cred = store.add_credential(Credential(name="lab", username="admin", password="pw"))
    redacted = cred.to_dict(redact=True)
    assert redacted["password"] == "********"


def test_update_keeps_blank_secrets(store):
    """The UI sends blanks when the operator did not retype the password."""
    cred = store.add_credential(Credential(name="lab", username="admin", password="original"))
    cred.password = ""
    cred.name = "lab-renamed"
    store.update_credential(cred)
    assert store.get_credential(cred.id).password == "original"
    assert store.get_credential(cred.id).name == "lab-renamed"


def test_update_replaces_a_new_secret(store):
    cred = store.add_credential(Credential(name="lab", username="admin", password="original"))
    cred.password = "rotated"
    store.update_credential(cred)
    assert store.get_credential(cred.id).password == "rotated"


def test_update_keeps_secret_when_sent_as_stars(store):
    cred = store.add_credential(Credential(name="lab", username="admin", password="original"))
    cred.password = "********"
    store.update_credential(cred)
    assert store.get_credential(cred.id).password == "original"


# ── checks & results ─────────────────────────────────────────────────────────────────


def test_check_params_survive_json_roundtrip(store, device):
    check = store.add_check(
        CheckConfig(device_id=device.id, kind="tcp", params={"port": 2222, "nested": {"a": 1}})
    )
    fetched = store.get_check(check.id)
    assert fetched.params == {"port": 2222, "nested": {"a": 1}}


def test_results_and_series(store, device):
    for index in range(5):
        store.add_result(
            CheckResult(
                device_id=device.id,
                kind="icmp",
                ok=index != 3,
                latency_ms=1.0 + index,
                ts=1000 + index,
            )
        )
    series = store.result_series(device.id, limit=10)
    assert len(series) == 5
    assert series[0]["ts"] == 1000  # oldest first
    assert series[3]["ok"] is False


def test_availability_window(store, device):
    now = time.time()
    for index in range(10):
        store.add_result(CheckResult(device_id=device.id, ok=index < 8, ts=now - index))
    assert store.availability(device.id, 3600) == 80.0


def test_prune_results(store, device):
    store.add_result(CheckResult(device_id=device.id, ok=True, ts=time.time() - 100000))
    store.add_result(CheckResult(device_id=device.id, ok=True, ts=time.time()))
    assert store.prune_results(50000) == 1
    assert len(store.recent_results(device.id)) == 1


# ── state ────────────────────────────────────────────────────────────────────────────


def test_state_defaults_to_unknown(store, device):
    assert store.get_state(device.id).state == "unknown"


def test_state_upsert(store, device):
    state = store.get_state(device.id)
    state.state = "up"
    state.up_checks = 7
    store.save_state(state)
    assert store.get_state(device.id).state == "up"
    assert store.get_state(device.id).up_checks == 7


def test_check_state_upsert(store, device):
    check = store.add_check(CheckConfig(device_id=device.id, kind="icmp"))
    store.save_check_state(check.id, device.id, "up", 0, 3, time.time(), 4.2, True, "")
    stored = store.get_check_state(check.id)
    assert stored["state"] == "up"
    assert stored["consecutive_successes"] == 3
    store.save_check_state(check.id, device.id, "down", 2, 0, time.time(), None, False, "timed out")
    stored = store.get_check_state(check.id)
    assert stored["state"] == "down"
    assert stored["last_error"] == "timed out"


def test_uptime_percentage(store, device):
    state = store.get_state(device.id)
    state.total_checks = 10
    state.up_checks = 9
    assert state.uptime_pct == 90.0


# ── events ───────────────────────────────────────────────────────────────────────────


def test_event_feed_and_acknowledgement(store):
    store.add_event(Event(severity="critical", message="device down", device_name="r1"))
    store.add_event(Event(severity="info", message="device up", device_name="r1"))
    assert len(store.list_events()) == 2
    assert len(store.list_events(severity="critical")) == 1
    assert store.unacknowledged_count() == 2
    store.acknowledge_all_events()
    assert store.unacknowledged_count() == 0


def test_acknowledge_single_event(store):
    event = store.add_event(Event(message="x"))
    store.acknowledge_event(event.id)
    assert store.list_events(unacknowledged_only=True) == []


# ── templates ────────────────────────────────────────────────────────────────────────


def test_template_crud(store):
    template = store.add_template(Template(name="ntp", vendor="mikrotik", body="x", variables={"a": "1"}))
    assert store.get_template(template.id).variables == {"a": "1"}
    assert store.get_template_by_name("ntp").id == template.id
    template.description = "updated"
    store.update_template(template)
    assert store.get_template(template.id).description == "updated"
    store.delete_template(template.id)
    assert store.get_template(template.id) is None


def test_templates_can_be_filtered_by_vendor(store):
    store.add_template(Template(name="m", vendor="mikrotik", body="a"))
    store.add_template(Template(name="c", vendor="cisco", body="b"))
    assert [t.name for t in store.list_templates(vendor="cisco")] == ["c"]


# ── jobs ─────────────────────────────────────────────────────────────────────────────


def test_job_and_targets(store, device):
    job = store.create_job(
        Job(template_name="ntp", vendor="mikrotik", body="/x", options={"dry_run": True}),
        [JobTarget(device_id=device.id, device_name=device.name, host=device.host)],
    )
    assert job.id == 1
    assert job.total == 1
    detail = store.get_job(job.id)
    assert detail.options == {"dry_run": True}
    targets = store.list_job_targets(job.id)
    assert len(targets) == 1
    targets[0].status = "ok"
    targets[0].output = "done"
    store.save_job_target(targets[0])
    assert store.list_job_targets(job.id)[0].output == "done"


def test_job_target_backup_is_stored_but_hidden_from_listings(store, device):
    job = store.create_job(
        Job(template_name="x", body="/x"), [JobTarget(device_id=device.id, device_name="r1")]
    )
    target = store.list_job_targets(job.id)[0]
    target.backup = "hostname r1\n"
    store.save_job_target(target)
    assert store.list_job_targets(job.id)[0].backup == "hostname r1\n"
    public = store.list_job_targets(job.id)[0].to_dict()
    assert "backup" not in public
    assert public["has_backup"] is True


def test_job_target_duration(store, device):
    target = JobTarget(device_id=device.id, started_at=100.0, finished_at=101.5)
    assert target.duration_ms == 1500


# ── backups ──────────────────────────────────────────────────────────────────────────


def test_backups(store, device):
    backup_id = store.add_backup(device.id, device.name, device.host, "hostname r1\n", source="manual")
    listed = store.list_backups()
    assert len(listed) == 1
    assert listed[0]["byte_size"] == len("hostname r1\n")
    assert store.get_backup(backup_id)["config"] == "hostname r1\n"
    assert store.latest_backup(device.id)["id"] == backup_id
    store.delete_backup(backup_id)
    assert store.list_backups() == []


# ── aggregates ───────────────────────────────────────────────────────────────────────


def test_overview_and_device_cards(store, device):
    check = store.add_check(CheckConfig(device_id=device.id, kind="icmp"))
    store.add_result(CheckResult(device_id=device.id, check_id=check.id, ok=True, latency_ms=2.5))

    overview = store.overview()
    assert overview["devices_total"] == 1
    assert overview["devices_enabled"] == 1
    assert overview["checks_24h"] == 1

    cards = store.device_cards()
    assert len(cards) == 1
    assert cards[0]["name"] == device.name
    assert len(cards[0]["checks"]) == 1
    assert cards[0]["sparkline"][0]["latency_ms"] == 2.5


def test_settings_roundtrip(store):
    assert store.get_setting("missing", "fallback") == "fallback"
    store.set_setting("notify", {"enabled": True, "min_severity": "critical"})
    assert store.get_setting("notify")["min_severity"] == "critical"


def test_store_creates_parent_directories(tmp_path):
    nested = Store(data_dir=tmp_path / "a" / "b" / "c")
    assert nested.path.exists()
    nested.close()
