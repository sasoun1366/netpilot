"""The monitoring scheduler and its state machine.

These tests drive :meth:`Monitor._record` directly with synthetic results. That is
deliberate: the hysteresis rules *are* the product here, and they must be verifiable
without a network.
"""

from __future__ import annotations

import asyncio
import dataclasses
import time

import pytest

from netpilot.models import (
    STATE_DEGRADED,
    STATE_DOWN,
    STATE_UNKNOWN,
    STATE_UP,
    CheckConfig,
    CheckResult,
    Device,
)
from netpilot.monitoring.monitor import Monitor, bootstrap_checks


@pytest.fixture()
def monitor(store):
    return Monitor(store)


def _device(store, name="r1", host="10.0.0.1") -> Device:
    return store.add_device(Device(name=name, host=host, vendor="mikrotik"))


def _check(store, device, failures_to_down=2, successes_to_up=1, degraded_latency_ms=None) -> CheckConfig:
    return store.add_check(
        CheckConfig(
            device_id=device.id,
            kind="icmp",
            failures_to_down=failures_to_down,
            successes_to_up=successes_to_up,
            degraded_latency_ms=degraded_latency_ms,
        )
    )


def _record(monitor, device, check, ok, latency=1.0, message="ok"):
    monitor._record(device, check, CheckResult(device_id=device.id, ok=ok, latency_ms=latency, message=message))


# ── state transitions ───────────────────────────────────────────────────────────────


def test_first_success_promotes_unknown_to_up(store, monitor):
    device = _device(store)
    check = _check(store, device)
    _record(monitor, device, check, True)
    assert store.get_state(device.id).state == STATE_UP


def test_single_failure_is_degraded_not_down(store, monitor):
    """One dropped packet must not page anyone."""
    device = _device(store)
    check = _check(store, device, failures_to_down=3)
    _record(monitor, device, check, True)
    _record(monitor, device, check, False, None, "timed out")
    state = store.get_state(device.id)
    assert state.state == STATE_DEGRADED
    assert state.last_error == "timed out"


def test_failures_reaching_the_threshold_declare_down(store, monitor):
    device = _device(store)
    check = _check(store, device, failures_to_down=3)
    _record(monitor, device, check, True)
    for _ in range(3):
        _record(monitor, device, check, False, None, "no reply")
    assert store.get_state(device.id).state == STATE_DOWN


def test_recovery_requires_the_configured_successes(store, monitor):
    device = _device(store)
    check = _check(store, device, failures_to_down=1, successes_to_up=3)
    _record(monitor, device, check, False, None, "down")
    assert store.get_state(device.id).state == STATE_DOWN
    _record(monitor, device, check, True)
    assert store.get_state(device.id).state == STATE_DOWN
    _record(monitor, device, check, True)
    assert store.get_state(device.id).state == STATE_DOWN
    _record(monitor, device, check, True)
    assert store.get_state(device.id).state == STATE_UP


def test_failure_counter_resets_on_success(store, monitor):
    device = _device(store)
    check = _check(store, device, failures_to_down=3)
    _record(monitor, device, check, True)
    _record(monitor, device, check, False, None, "x")
    _record(monitor, device, check, False, None, "x")
    _record(monitor, device, check, True)
    _record(monitor, device, check, False, None, "x")
    # three consecutive failures were never reached
    assert store.get_state(device.id).state != STATE_DOWN


def test_high_latency_marks_a_device_degraded(store, monitor):
    device = _device(store)
    check = _check(store, device, degraded_latency_ms=50.0)
    _record(monitor, device, check, True, latency=10.0)
    assert store.get_state(device.id).state == STATE_UP
    _record(monitor, device, check, True, latency=250.0)
    assert store.get_state(device.id).state == STATE_DEGRADED
    _record(monitor, device, check, True, latency=12.0)
    assert store.get_state(device.id).state == STATE_UP


# ── multi-check aggregation ─────────────────────────────────────────────────────────


def test_device_is_down_when_any_check_is_down(store, monitor):
    device = _device(store)
    ping = _check(store, device, failures_to_down=1)
    port = store.add_check(
        CheckConfig(device_id=device.id, kind="tcp", failures_to_down=1, params={"port": 22})
    )
    _record(monitor, device, ping, True)
    _record(monitor, device, port, True)
    assert store.get_state(device.id).state == STATE_UP

    _record(monitor, device, port, False, None, "connection refused")
    assert store.get_state(device.id).state == STATE_DOWN

    _record(monitor, device, port, True)
    assert store.get_state(device.id).state == STATE_UP


def test_device_is_degraded_when_one_check_is_degraded(store, monitor):
    device = _device(store)
    ping = _check(store, device, failures_to_down=5)
    port = store.add_check(
        CheckConfig(device_id=device.id, kind="tcp", failures_to_down=5, params={"port": 22})
    )
    _record(monitor, device, ping, True)
    _record(monitor, device, port, True)
    _record(monitor, device, port, False, None, "flapping")
    assert store.get_state(device.id).state == STATE_DEGRADED


def test_device_stays_unknown_until_something_is_observed(store, monitor):
    device = _device(store)
    _check(store, device)
    assert store.get_state(device.id).state == STATE_UNKNOWN


def test_disabled_checks_are_excluded_from_aggregation(store, monitor):
    device = _device(store)
    active = _check(store, device, failures_to_down=1)
    disabled = store.add_check(
        CheckConfig(device_id=device.id, kind="tcp", failures_to_down=1, enabled=False, params={"port": 22})
    )
    _record(monitor, device, active, True)
    _record(monitor, device, disabled, False, None, "irrelevant")
    assert store.get_state(device.id).state == STATE_UP


# ── events ──────────────────────────────────────────────────────────────────────────


def test_transitions_emit_events_with_sensible_severities(store, monitor):
    device = _device(store)
    check = _check(store, device, failures_to_down=1)
    _record(monitor, device, check, False, None, "no reply")
    _record(monitor, device, check, True)

    events = store.list_events()
    assert len(events) == 2
    down_event = next(e for e in events if e.severity == "critical")
    up_event = next(e for e in events if e.severity == "info")
    assert "DOWN" in down_event.message
    assert "recovered" in up_event.message


def test_no_event_when_the_state_does_not_change(store, monitor):
    device = _device(store)
    check = _check(store, device, failures_to_down=5)
    for _ in range(4):
        _record(monitor, device, check, True)
    # exactly one transition (unknown -> up), not four
    assert len(store.list_events()) == 1


def test_first_reachability_is_an_info_not_a_recovery(store, monitor):
    device = _device(store)
    check = _check(store, device)
    _record(monitor, device, check, True)
    event = store.list_events()[0]
    assert event.severity == "info"
    assert "reachable" in event.message


def test_event_details_carry_the_transition(store, monitor):
    device = _device(store)
    check = _check(store, device, failures_to_down=1)
    _record(monitor, device, check, False, None, "no reply")
    event = store.list_events()[0]
    assert event.details["state"] == STATE_DOWN
    assert event.details["previous"] in (STATE_UNKNOWN, STATE_UP)
    assert event.details["check_kind"] == "icmp"


# ── counters and bookkeeping ────────────────────────────────────────────────────────


def test_results_are_persisted(store, monitor):
    device = _device(store)
    check = _check(store, device)
    for _ in range(4):
        _record(monitor, device, check, True)
    assert len(store.recent_results(device.id)) == 4
    assert monitor.results_written == 4


def test_device_counters_track_success_rate(store, monitor):
    device = _device(store)
    check = _check(store, device, failures_to_down=99)
    _record(monitor, device, check, True)
    _record(monitor, device, check, True)
    _record(monitor, device, check, False, None, "blip")
    state = store.get_state(device.id)
    assert state.total_checks == 3
    assert state.up_checks == 2
    assert state.uptime_pct == pytest.approx(66.67, abs=0.01)


def test_check_state_is_persisted_per_check(store, monitor):
    device = _device(store)
    check = _check(store, device, failures_to_down=2)
    _record(monitor, device, check, False, None, "x")
    stored = store.get_check_state(check.id)
    assert stored["consecutive_failures"] == 1
    assert stored["state"] == STATE_DEGRADED
    assert stored["last_ok"] == 0


# ── scheduler plumbing ──────────────────────────────────────────────────────────────


def test_reload_schedules_every_enabled_check(store, monitor):
    device = _device(store)
    for index in range(3):
        store.add_check(CheckConfig(device_id=device.id, kind="icmp", label=f"c{index}"))
    store.add_check(CheckConfig(device_id=device.id, kind="tcp", enabled=False))
    assert monitor.reload() == 3
    assert len(monitor.next_runs()) == 4


def test_next_runs_reports_countdowns(store, monitor):
    device = _device(store)
    store.add_check(CheckConfig(device_id=device.id, kind="icmp", interval_sec=30))
    monitor.reload()
    rows = monitor.next_runs()
    assert rows[0]["interval_sec"] == 30
    assert rows[0]["next_run_in"] is not None


def test_check_device_runs_everything_and_records(store, monitor):
    device = _device(store)
    store.add_check(CheckConfig(device_id=device.id, kind="tcp", params={"port": 1}, timeout_sec=0.5))
    results = asyncio.run(monitor.check_device(device.id))
    assert len(results) == 1
    assert results[0].ok is False
    assert len(store.recent_results(device.id)) == 1


def test_check_now_returns_none_for_a_missing_check(store, monitor):
    assert asyncio.run(monitor.check_now(99999)) is None


def test_bootstrap_checks_attaches_defaults(store, monitor):
    device = _device(store)
    created = bootstrap_checks(store, device)
    kinds = sorted(c.kind for c in created)
    assert kinds == ["icmp", "tcp"]
    assert all(c.id is not None for c in created)
    assert all(c.device_id == device.id for c in created)


def test_monitor_start_and_stop(store):
    async def scenario():
        monitor = Monitor(store)
        monitor.start()
        assert monitor.running is True
        await asyncio.sleep(0.05)
        await monitor.stop()
        assert monitor.running is False

    asyncio.run(scenario())


def test_scheduler_executes_a_check_end_to_end(store):
    """The full loop: schedule -> probe -> state -> event, against a real socket."""
    import socket

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(4)
    port = server.getsockname()[1]
    try:
        device = store.add_device(Device(name="local", host="127.0.0.1", vendor="generic"))
        store.add_check(
            CheckConfig(device_id=device.id, kind="tcp", params={"port": port}, interval_sec=5, timeout_sec=1.0)
        )

        async def scenario():
            monitor = Monitor(store)
            monitor.start()
            for _ in range(60):
                await asyncio.sleep(0.1)
                if store.get_state(device.id).state == STATE_UP:
                    break
            await monitor.stop()
            return monitor

        monitor = asyncio.run(scenario())
        assert store.get_state(device.id).state == STATE_UP
        assert monitor.checks_run >= 1
        assert store.get_state(device.id).last_latency_ms is not None
    finally:
        server.close()


def test_concurrency_is_bounded(store):
    monitor = Monitor(store, concurrency=4)
    assert monitor.concurrency == 4
    assert monitor._sem._value == 4


# ── a delete that lands mid-probe ──────────────────────────────────────────────────
# Reported from the desktop app: deleting a device made the window look dead. The probe
# that was already in flight wrote its result to a foreign key that had just been
# removed, the write raised, and the check was never queued again — so that device (and
# everything recorded from it) went quiet for good.


def test_recording_a_result_for_a_deleted_check_is_a_no_op(store, monitor):
    device = _device(store)
    check = _check(store, device)
    store.delete_device(device.id)  # deleted while the probe was in flight

    _record(monitor, device, check, True)  # must not raise

    assert store.list_events() == [], "nothing may be written for a device that is gone"


def test_recording_a_result_for_a_deleted_device_is_a_no_op(store, monitor):
    device = _device(store)
    check = _check(store, device)
    store.delete_check(check.id)
    _record(monitor, device, check, True)  # keyed on the check: gone, so do nothing


def test_a_check_is_requeued_even_when_recording_fails(store, monitor):
    """Monitoring must not stop because one write went wrong."""
    device = _device(store)
    check = _check(store, device)
    monitor.reload()

    def explode(*_args, **_kwargs):
        raise RuntimeError("disk on fire")

    monitor._record = explode  # type: ignore[method-assign]

    async def fake_run_check(*_args, **_kwargs):
        return CheckResult(device_id=device.id, ok=True, latency_ms=1.0, message="ok")

    import netpilot.monitoring.monitor as monitor_mod

    original = monitor_mod.run_check
    monitor_mod.run_check = fake_run_check  # type: ignore[assignment]
    try:
        asyncio.run(monitor._execute(check))
    finally:
        monitor_mod.run_check = original  # type: ignore[assignment]

    assert check.id in monitor._next_run, "the check was dropped from the schedule"
    assert any(entry[2] == check.id for entry in monitor._queue)


def test_a_deleted_check_is_not_requeued(store, monitor):
    device = _device(store)
    check = _check(store, device)
    monitor.reload()

    async def fake_run_check(*_args, **_kwargs):
        return CheckResult(device_id=device.id, ok=True, latency_ms=1.0, message="ok")

    import netpilot.monitoring.monitor as monitor_mod

    original = monitor_mod.run_check
    monitor_mod.run_check = fake_run_check  # type: ignore[assignment]
    try:
        store.delete_check(check.id)
        queued_before = len(monitor._queue)
        asyncio.run(monitor._execute(check))
    finally:
        monitor_mod.run_check = original  # type: ignore[assignment]

    assert check.id not in monitor._next_run, "a deleted check keeps being probed"
    assert len(monitor._queue) == queued_before, "a deleted check was queued again"


def test_one_bad_pass_does_not_stop_the_monitor(store, monitor, monkeypatch):
    """The scheduler loop must outlive any single failed pass."""
    monkeypatch.setattr(monitor, "reload", lambda: 0)
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient database error")
        monitor._running = False  # second pass ends the test

    monkeypatch.setattr(monitor, "_tick", flaky)
    asyncio.run(monitor._loop())

    assert calls["n"] >= 2, "the loop gave up after the first failure"


def test_a_disabled_device_is_left_alone(store, monitor):
    device = _device(store)
    check = _check(store, device)
    monitor.reload()
    store.update_device(dataclasses.replace(device, enabled=False))

    async def fake_run_check(*_args, **_kwargs):  # pragma: no cover - must not be called
        raise AssertionError("a disabled device must not be probed")

    import netpilot.monitoring.monitor as monitor_mod

    original = monitor_mod.run_check
    monitor_mod.run_check = fake_run_check  # type: ignore[assignment]
    try:
        asyncio.run(monitor._execute(check))
    finally:
        monitor_mod.run_check = original  # type: ignore[assignment]

    assert check.id not in monitor._next_run
