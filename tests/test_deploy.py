"""The bulk deployment engine.

The engine is tested against a *fake* adapter registered in the real registry. That keeps
the tests hermetic while still exercising the real code path — template rendering, the
backup step, the rollback branch, job bookkeeping and concurrency — rather than a mock of
the engine itself.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from netpilot.adapters import Adapter, CommandResult, register
from netpilot.core import App
from netpilot.deploy.engine import DeployEngine, DeployOptions, capture_backup, restore_backup
from netpilot.models import (
    JOB_CANCELLED,
    JOB_DONE,
    JOB_FAILED,
    TARGET_FAILED,
    TARGET_OK,
    TARGET_ROLLED_BACK,
    TARGET_SKIPPED,
    Credential,
    Device,
    Job,
    JobTarget,
    Template,
)


# ── the fake device family ───────────────────────────────────────────────────────────


class FakeAdapter(Adapter):
    """An adapter that talks to an in-memory device instead of a network box."""

    name = "fake"
    label = "Fake Device"
    prompt_patterns = (r"[>#]",)
    supports_deploy = True
    rollback_support = "native"

    #: Set by tests: command text that should be rejected by the "device".
    reject_commands: set[str] = set()
    #: Set by tests: raise on connect.
    fail_connect = False
    #: Records every instance so tests can inspect what happened.
    instances: list["FakeAdapter"] = []

    def __init__(self, device, credential, timeout=20.0):
        super().__init__(device, credential, timeout)
        self.applied: list[str] = []
        self.rollback_points: list[str] = []
        self.cleaned_rollback_points: list[str] = []
        self.restored: list[str] = []
        self.saved = False
        FakeAdapter.instances.append(self)

    def connect(self):
        if FakeAdapter.fail_connect:
            raise RuntimeError("connection refused by the fake device")
        return self.info

    def send(self, command, expect_prompt=True):
        return CommandResult(command=command, ok=True, output="")

    def fetch_config(self):
        return "hostname fake\ninterface ethernet1\n"

    def apply_commands(self, commands, save=True):
        results = []
        for command in commands:
            if command in FakeAdapter.reject_commands:
                results.append(
                    CommandResult(command=command, ok=False, error="syntax error (fake)")
                )
                break
            self.applied.append(command)
            results.append(CommandResult(command=command, ok=True, output="ok"))
        if save and all(r.ok for r in results):
            self.save_config()
            results.append(self.save_config())
        return results

    def save_config(self):
        self.saved = True
        return CommandResult(command="<save>", ok=True, output="saved")

    def create_rollback_point(self, tag):
        self.rollback_points.append(tag)
        return True, f"checkpoint-{tag}"

    def restore_rollback_point(self, name):
        self.restored.append(name)
        return CommandResult(command=f"<restore {name}>", ok=True, output="restored")

    def cleanup_rollback_point(self, name):
        self.cleaned_rollback_points.append(name)

    @classmethod
    def reset(cls):
        cls.reject_commands = set()
        cls.fail_connect = False
        cls.instances = []


class ManualRollbackAdapter(FakeAdapter):
    """Same, but the vendor offers no native checkpoint (like IOS)."""

    name = "fake-manual"
    label = "Fake Device (manual rollback)"
    rollback_support = "manual"


class NoDeployAdapter(FakeAdapter):
    """Monitoring only — must never be pushed to."""

    name = "fake-raw"
    label = "Fake Device (monitoring only)"
    supports_deploy = False


register(FakeAdapter)
register(ManualRollbackAdapter)
register(NoDeployAdapter)


@pytest.fixture(autouse=True)
def _reset_fakes():
    FakeAdapter.reset()
    yield
    FakeAdapter.reset()


@pytest.fixture()
def fake_device(store):
    return store.add_device(Device(name="fake-1", host="10.9.9.1", vendor="fake", tags=["lab"]))


@pytest.fixture()
def sample_template(store):
    return store.add_template(
        Template(
            name="test-ntp",
            vendor="fake",
            body="set ntp {{ server }}",
            variables={"server": "pool.ntp.org"},
        )
    )


def _engine(store) -> DeployEngine:
    return DeployEngine(store)


def _options(**overrides) -> DeployOptions:
    base = {"dry_run": False, "backup_before": True, "auto_rollback": True, "max_parallel": 4}
    base.update(overrides)
    return DeployOptions(**base)


# ── planning & preview ──────────────────────────────────────────────────────────────


def test_plan_creates_a_job_with_one_target_per_device(store, fake_device, sample_template):
    engine = _engine(store)
    job = engine.plan(sample_template, sample_template.body, [fake_device], {}, _options())
    assert job.id is not None
    assert job.total == 1
    assert job.status == "pending"
    targets = store.list_job_targets(job.id)
    assert len(targets) == 1
    assert targets[0].device_name == fake_device.name
    assert targets[0].status == "pending"


def test_preview_renders_without_connecting(store, fake_device, sample_template):
    engine = _engine(store)
    preview = engine.preview(sample_template, fake_device, {"server": "time.example.com"})
    assert preview["commands"] == ["set ntp time.example.com"]
    assert preview["command_count"] == 1
    assert preview["supports_deploy"] is True
    assert preview["rollback_support"] == "native"
    assert preview["unresolved"] == []
    assert FakeAdapter.instances == []  # nothing was connected to


def test_preview_reports_unresolved_variables(store, fake_device):
    engine = _engine(store)
    template = Template(name="t", vendor="fake", body="set x {{ missing }}", variables={})
    preview = engine.preview(template, fake_device, {})
    assert preview["unresolved"] == ["missing"]


def test_preview_reports_vendor_rollback_capability(store):
    manual_device = store.add_device(Device(name="m", host="10.2.2.2", vendor="fake-manual"))
    engine = _engine(store)
    template = Template(name="t", vendor="fake-manual", body="set x 1")
    assert engine.preview(template, manual_device, {})["rollback_support"] == "manual"


def test_preview_of_the_generic_vendor_is_marked_monitoring_only(store):
    device = store.add_device(Device(name="g", host="10.0.0.9", vendor="generic"))
    engine = _engine(store)
    preview = engine.preview(Template(name="t", vendor="generic", body="ls"), device, {})
    assert preview["supports_deploy"] is False


# ── dry run ─────────────────────────────────────────────────────────────────────────


def test_dry_run_touches_nothing(store, fake_device, sample_template):
    engine = _engine(store)
    job = engine.plan(
        sample_template, sample_template.body, [fake_device], {}, _options(dry_run=True)
    )
    asyncio.run(engine.run(job.id))

    detail = store.get_job(job.id)
    assert detail.status == JOB_DONE
    assert detail.succeeded == 1

    target = store.list_job_targets(job.id)[0]
    assert target.status == TARGET_OK
    assert "dry-run" in target.output
    assert "set ntp pool.ntp.org" in target.output
    # no adapter was ever constructed, so nothing could have been written
    assert FakeAdapter.instances == []
    assert store.list_backups() == []


def test_dry_run_still_reports_the_planned_commands(store, fake_device, sample_template):
    engine = _engine(store)
    job = engine.plan(
        sample_template, sample_template.body, [fake_device], {}, _options(dry_run=True)
    )
    asyncio.run(engine.run(job.id))
    target = store.list_job_targets(job.id)[0]
    assert target.commands == ["set ntp pool.ntp.org"]


# ── real run ────────────────────────────────────────────────────────────────────────


def test_a_successful_run_applies_commands_and_saves(store, fake_device, sample_template):
    engine = _engine(store)
    job = engine.plan(sample_template, sample_template.body, [fake_device], {}, _options())
    asyncio.run(engine.run(job.id))

    detail = store.get_job(job.id)
    assert detail.status == JOB_DONE
    assert detail.succeeded == 1 and detail.failed == 0

    adapter = FakeAdapter.instances[-1]
    assert adapter.applied == ["set ntp pool.ntp.org"]
    assert adapter.saved is True


def test_the_pre_change_config_is_captured_and_stored(store, fake_device, sample_template):
    engine = _engine(store)
    job = engine.plan(sample_template, sample_template.body, [fake_device], {}, _options())
    asyncio.run(engine.run(job.id))

    backups = store.list_backups(device_id=fake_device.id)
    assert len(backups) == 1
    assert backups[0]["source"] == f"job:{job.id}"
    stored = store.get_backup(backups[0]["id"])
    assert "hostname fake" in stored["config"]


def test_backup_can_be_skipped(store, fake_device, sample_template):
    engine = _engine(store)
    job = engine.plan(
        sample_template, sample_template.body, [fake_device], {}, _options(backup_before=False)
    )
    asyncio.run(engine.run(job.id))
    assert store.list_backups(device_id=fake_device.id) == []


def test_variables_are_substituted_at_apply_time(store, fake_device, sample_template):
    engine = _engine(store)
    job = engine.plan(
        sample_template, sample_template.body, [fake_device], {"server": "10.5.5.5"}, _options()
    )
    asyncio.run(engine.run(job.id))
    assert FakeAdapter.instances[-1].applied == ["set ntp 10.5.5.5"]


def test_a_native_rollback_point_is_created_and_cleaned_up_on_success(
    store, fake_device, sample_template
):
    engine = _engine(store)
    job = engine.plan(sample_template, sample_template.body, [fake_device], {}, _options())
    asyncio.run(engine.run(job.id))
    adapter = FakeAdapter.instances[-1]
    assert len(adapter.rollback_points) == 1
    assert adapter.cleaned_rollback_points == [f"checkpoint-{adapter.rollback_points[0]}"]


def test_multiple_devices_are_all_processed(store, sample_template):
    devices = [
        store.add_device(Device(name=f"fake-{index}", host=f"10.9.9.{index}", vendor="fake"))
        for index in range(2, 6)
    ]
    engine = _engine(store)
    job = engine.plan(sample_template, sample_template.body, devices, {}, _options(max_parallel=2))
    asyncio.run(engine.run(job.id))

    detail = store.get_job(job.id)
    assert detail.total == 4
    assert detail.succeeded == 4
    assert all(t.status == TARGET_OK for t in store.list_job_targets(job.id))


def test_events_are_emitted_for_start_and_finish(store, fake_device, sample_template):
    engine = _engine(store)
    job = engine.plan(sample_template, sample_template.body, [fake_device], {}, _options())
    asyncio.run(engine.run(job.id))
    messages = [e.message for e in store.list_events()]
    assert any("started" in m for m in messages)
    assert any("finished" in m for m in messages)


# ── failures & rollback ─────────────────────────────────────────────────────────────


def test_a_rejected_command_fails_the_target_and_rolls_back(store, fake_device, sample_template):
    FakeAdapter.reject_commands = {"set ntp pool.ntp.org"}
    engine = _engine(store)
    job = engine.plan(sample_template, sample_template.body, [fake_device], {}, _options())
    asyncio.run(engine.run(job.id))

    target = store.list_job_targets(job.id)[0]
    assert target.status == TARGET_ROLLED_BACK
    assert "rolled back" in target.error
    adapter = FakeAdapter.instances[-1]
    assert adapter.restored, "the checkpoint should have been restored"
    assert "restored checkpoint" in target.output


def test_manual_rollback_vendor_is_told_to_use_the_stored_backup(store, sample_template):
    device = store.add_device(Device(name="manual-1", host="10.8.8.1", vendor="fake-manual"))
    FakeAdapter.reject_commands = {"set ntp pool.ntp.org"}
    engine = _engine(store)
    job = engine.plan(sample_template, sample_template.body, [device], {}, _options())
    asyncio.run(engine.run(job.id))

    target = store.list_job_targets(job.id)[0]
    assert target.status == TARGET_FAILED
    assert "no native checkpoint" in target.output
    # the pre-change config is still on disk for a manual restore
    assert store.list_backups(device_id=device.id)


def test_rollback_can_be_turned_off(store, fake_device, sample_template):
    FakeAdapter.reject_commands = {"set ntp pool.ntp.org"}
    engine = _engine(store)
    job = engine.plan(
        sample_template, sample_template.body, [fake_device], {}, _options(auto_rollback=False)
    )
    asyncio.run(engine.run(job.id))
    target = store.list_job_targets(job.id)[0]
    assert target.status == TARGET_FAILED
    assert FakeAdapter.instances[-1].restored == []


def test_a_failed_backup_aborts_before_any_change(store, fake_device, sample_template):
    class Exploding(FakeAdapter):
        name = "fake-explode"

        def fetch_config(self):
            raise RuntimeError("export command not permitted")

    register(Exploding)
    device = store.add_device(Device(name="boom", host="10.7.7.7", vendor="fake-explode"))
    engine = _engine(store)
    job = engine.plan(sample_template, sample_template.body, [device], {}, _options())
    asyncio.run(engine.run(job.id))

    target = store.list_job_targets(job.id)[0]
    assert target.status == TARGET_FAILED
    assert "nothing was applied" in target.error
    assert Exploding.instances[-1].applied == []


def test_a_connect_failure_is_recorded_not_raised(store, fake_device, sample_template):
    FakeAdapter.fail_connect = True
    engine = _engine(store)
    job = engine.plan(sample_template, sample_template.body, [fake_device], {}, _options())
    asyncio.run(engine.run(job.id))
    target = store.list_job_targets(job.id)[0]
    assert target.status == TARGET_FAILED
    assert "connection refused" in target.error


def test_monitoring_only_devices_are_skipped_with_an_explanation(store, sample_template):
    device = store.add_device(Device(name="raw", host="10.6.6.6", vendor="fake-raw"))
    engine = _engine(store)
    job = engine.plan(sample_template, sample_template.body, [device], {}, _options())
    asyncio.run(engine.run(job.id))
    target = store.list_job_targets(job.id)[0]
    assert target.status == TARGET_SKIPPED
    assert "no modelled configuration push" in target.error


def test_a_template_that_renders_to_nothing_is_skipped(store, fake_device):
    engine = _engine(store)
    template = Template(name="empty", vendor="fake", body="# only a comment\n")
    job = engine.plan(template, template.body, [fake_device], {}, _options())
    asyncio.run(engine.run(job.id))
    target = store.list_job_targets(job.id)[0]
    assert target.status == TARGET_SKIPPED
    assert "zero commands" in target.error


def test_stop_on_first_failure_skips_the_rest(store, sample_template):
    devices = [
        store.add_device(Device(name=f"d{index}", host=f"10.4.4.{index}", vendor="fake"))
        for index in range(1, 5)
    ]
    FakeAdapter.reject_commands = {"set ntp pool.ntp.org"}
    engine = _engine(store)
    job = engine.plan(
        sample_template,
        sample_template.body,
        devices,
        {},
        _options(max_parallel=1, stop_on_first_failure=True),
    )
    asyncio.run(engine.run(job.id))
    statuses = [t.status for t in store.list_job_targets(job.id)]
    assert statuses.count(TARGET_SKIPPED) >= 1
    assert store.get_job(job.id).status == JOB_FAILED


def test_job_status_is_failed_when_everything_failed(store, fake_device, sample_template):
    FakeAdapter.fail_connect = True
    engine = _engine(store)
    job = engine.plan(sample_template, sample_template.body, [fake_device], {}, _options())
    asyncio.run(engine.run(job.id))
    assert store.get_job(job.id).status == JOB_FAILED


# ── cancellation ────────────────────────────────────────────────────────────────────


def test_cancelling_a_job_marks_the_remaining_targets(store, sample_template):
    devices = [
        store.add_device(Device(name=f"c{index}", host=f"10.3.3.{index}", vendor="fake"))
        for index in range(1, 5)
    ]
    engine = _engine(store)
    job = engine.plan(sample_template, sample_template.body, devices, {}, _options(max_parallel=1))

    async def scenario():
        engine._cancelled.add(job.id)  # simulate the operator pressing Cancel
        await engine.run(job.id)

    asyncio.run(scenario())
    statuses = [t.status for t in store.list_job_targets(job.id)]
    assert statuses.count(TARGET_SKIPPED) == len(devices)
    assert store.get_job(job.id).status == JOB_CANCELLED


# ── backups & restore ───────────────────────────────────────────────────────────────


def test_capture_backup_stores_the_config(store, fake_device):
    result = asyncio.run(capture_backup(store, fake_device))
    assert result["ok"] is True
    assert result["device"] == fake_device.name
    stored = store.get_backup(result["backup_id"])
    assert "hostname fake" in stored["config"]


def test_capture_backup_reports_connect_failures(store, fake_device):
    FakeAdapter.fail_connect = True
    result = asyncio.run(capture_backup(store, fake_device))
    assert result["ok"] is False
    assert "connection refused" in result["error"]


def test_restore_dry_run_lists_the_commands(store, fake_device):
    backup_id = store.add_backup(
        fake_device.id, fake_device.name, fake_device.host, "hostname restored\nset x 1\n"
    )
    result = asyncio.run(restore_backup(store, backup_id, dry_run=True))
    assert result["ok"] is True
    assert result["command_count"] == 2
    assert FakeAdapter.instances == []


def test_restore_pushes_the_commands_through_the_apply_flow(store, fake_device):
    backup_id = store.add_backup(
        fake_device.id, fake_device.name, fake_device.host, "hostname restored\nset x 1\n"
    )
    result = asyncio.run(restore_backup(store, backup_id, dry_run=False))
    assert result["ok"] is True
    assert result["applied"] == 2
    assert FakeAdapter.instances[-1].applied == ["hostname restored", "set x 1"]


def test_restore_reports_a_missing_backup(store):
    result = asyncio.run(restore_backup(store, 999))
    assert result["ok"] is False
    assert "not found" in result["error"]


def test_restore_reports_device_rejection(store, fake_device):
    backup_id = store.add_backup(fake_device.id, fake_device.name, fake_device.host, "bad command\n")
    FakeAdapter.reject_commands = {"bad command"}
    result = asyncio.run(restore_backup(store, backup_id, dry_run=False))
    assert result["ok"] is False
    assert "rejected" in result["error"]


# ── App-level orchestration ─────────────────────────────────────────────────────────


def test_create_deploy_rejects_an_empty_target_set(app):
    with pytest.raises(ValueError, match="no devices matched"):
        app.create_deploy(body="set x 1", vendor="fake", device_ids=[])


def test_create_deploy_rejects_a_monitoring_only_selection(app):
    device = app.store.add_device(Device(name="g", host="10.0.0.9", vendor="generic"))
    with pytest.raises(ValueError, match="support configuration push"):
        app.create_deploy(body="ls", vendor="generic", device_ids=[device.id])


def test_create_deploy_targets_by_tag(app):
    app.store.add_device(Device(name="a", host="10.1.1.1", vendor="fake", tags=["core"]))
    app.store.add_device(Device(name="b", host="10.1.1.2", vendor="fake", tags=["edge"]))
    job = app.create_deploy(body="set x 1", vendor="fake", tags=["core"])
    assert job.total == 1
    assert app.store.list_job_targets(job.id)[0].host == "10.1.1.1"


def test_run_deploy_returns_the_finished_job(app):
    app.store.add_device(Device(name="a", host="10.1.1.1", vendor="fake"))
    job = app.create_deploy(
        body="set x 1", vendor="fake", match_all=True, options={"dry_run": True}
    )
    finished = asyncio.run(app.run_deploy(job.id))
    assert finished.status == JOB_DONE
    assert finished.succeeded == 1


def test_job_detail_and_target_diff(app):
    app.store.add_device(Device(name="a", host="10.1.1.1", vendor="fake"))
    job = app.create_deploy(body="set x 1", vendor="fake", match_all=True,
                            options={"dry_run": False})
    asyncio.run(app.run_deploy(job.id))

    detail = app.job_detail(job.id)
    assert detail["total"] == 1
    assert len(detail["targets"]) == 1
    assert "backup" not in detail["targets"][0]  # payloads stay small

    diff = app.job_target_diff(detail["targets"][0]["id"])
    assert diff["backup_available"] is True
    assert diff["commands"] == ["set x 1"]
    assert app.job_target_diff(99999) is None


def test_job_detail_of_a_missing_job(app):
    assert app.job_detail(99999) is None


def test_deploy_options_are_clamped():
    assert DeployOptions.from_dict({"max_parallel": 999}).max_parallel == 32
    assert DeployOptions.from_dict({"max_parallel": 0}).max_parallel == 1
    assert DeployOptions.from_dict({}).dry_run is True  # safe default
    assert DeployOptions.from_dict(None).dry_run is True
