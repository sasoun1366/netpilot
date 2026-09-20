"""The monitoring scheduler.

A single asyncio task owns a priority queue of due checks. Each probe runs in its own
task so a slow device never blocks the others; results are written to storage and folded
into device state with hysteresis (``failures_to_down`` / ``successes_to_up``) so a single
dropped ICMP packet does not flap the dashboard.

State transitions are recorded as events, which is what the alert feed and the
notification sinks consume.
"""

from __future__ import annotations

import asyncio
import contextlib
import heapq
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from ..db import Store
from ..models import (
    STATE_DEGRADED,
    STATE_DOWN,
    STATE_UNKNOWN,
    STATE_UP,
    CheckConfig,
    CheckResult,
    Credential,
    Device,
    DeviceState,
    Event,
)
from .checks import DEFAULT_CHECKS, CheckConfig as _CheckConfig  # noqa: F401 - re-export
from .checks import run_check

log = logging.getLogger("netpilot.monitor")

#: Maps a device state to the event severity emitted on transition.
TRANSITION_SEVERITY = {
    STATE_DOWN: "critical",
    STATE_DEGRADED: "warning",
    STATE_UP: "info",
    STATE_UNKNOWN: "warning",
}


@dataclass(order=True)
class _Due:
    when: float
    seq: int
    check_id: int = 0

    def __iter__(self):  # pragma: no cover - convenience only
        return iter((self.when, self.seq, self.check_id))


class Monitor:
    """Runs all enabled checks on their own intervals."""

    def __init__(
        self,
        store: Store,
        on_event: Callable[[Event], None] | None = None,
        on_result: Callable[[CheckResult], None] | None = None,
        concurrency: int = 16,
    ) -> None:
        self.store = store
        self.on_event = on_event
        self.on_result = on_result
        self.concurrency = concurrency
        self._sem = asyncio.Semaphore(concurrency)
        self._queue: list[tuple[float, int, int]] = []
        self._seq = 0
        self._next_run: dict[int, float] = {}
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._inflight: set[asyncio.Task[Any]] = set()
        self._running = False
        self.checks_run = 0
        self.results_written = 0

    # -- lifecycle -------------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self.running:
            return
        self._task = asyncio.create_task(self._loop(), name="netpilot-monitor")
        log.info("monitor started")

    async def stop(self) -> None:
        self._running = False
        self._wake.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        for task in list(self._inflight):
            task.cancel()
        if self._inflight:
            await asyncio.gather(*self._inflight, return_exceptions=True)
        self._inflight.clear()
        log.info("monitor stopped")

    # -- scheduling ------------------------------------------------------------------

    def reload(self) -> int:
        """Rebuild the schedule from storage. Returns the number of enabled checks."""
        self._queue.clear()
        self._next_run.clear()
        now = time.time()
        count = 0
        for check in self.store.list_checks(enabled_only=True):
            if check.id is None:
                continue
            # Stagger initial runs over the first interval so startup is not a thundering herd.
            offset = (check.id * 1.7) % max(check.interval_sec, 1)
            heapq.heappush(self._queue, (now + offset, self._next_seq(), check.id))
            self._next_run[check.id] = now + offset
            count += 1
        self._wake.set()
        return count

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def next_runs(self) -> list[dict[str, Any]]:
        """Inspector helper: when each check is next due."""
        rows = []
        for check in self.store.list_checks():
            when = self._next_run.get(check.id)  # type: ignore[arg-type]
            rows.append(
                {
                    "check_id": check.id,
                    "device_id": check.device_id,
                    "kind": check.kind,
                    "label": check.label,
                    "enabled": check.enabled,
                    "interval_sec": check.interval_sec,
                    "next_run_in": round(when - time.time(), 1) if when else None,
                }
            )
        return sorted(rows, key=lambda r: (r["next_run_in"] is None, r["next_run_in"]))

    # -- main loop -------------------------------------------------------------------

    async def _loop(self) -> None:
        self._running = True
        self.reload()
        while self._running:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a monitoring engine stops being useful the
                # moment one bad pass kills it; the next pass is one second away.
                log.exception("monitor pass failed; retrying")
                await asyncio.sleep(1.0)

    async def _tick(self) -> None:
        """One pass: launch everything that is due, then sleep until the next is."""
        now = time.time()
        due: list[int] = []
        while self._queue and self._queue[0][0] <= now:
            _when, _seq, check_id = heapq.heappop(self._queue)
            due.append(check_id)

        for check_id in due:
            check = self.store.get_check(check_id)
            if check is None or not check.enabled:
                self._next_run.pop(check_id, None)
                continue
            self._spawn(check)

        wait = 1.0
        if self._queue:
            wait = max(0.05, min(5.0, self._queue[0][0] - time.time()))
        self._wake.clear()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._wake.wait(), timeout=wait)

    def _spawn(self, check: CheckConfig) -> None:
        task = asyncio.create_task(self._execute(check), name=f"check-{check.id}")
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)

    async def _execute(self, check: CheckConfig) -> None:
        keep_running = True
        try:
            async with self._sem:
                device = self.store.get_device(check.device_id)  # type: ignore[arg-type]
                if device is None or not device.enabled:
                    # Nothing to probe and no point coming back: a disabled device stays
                    # disabled until the next reload().
                    self._next_run.pop(check.id, None)
                    keep_running = False
                    return
                cred = (
                    self.store.get_credential(device.credential_id)
                    if device.credential_id
                    else None
                )
                result = await run_check(check, device, cred)
                self.checks_run += 1
                try:
                    self._record(device, check, result)
                except Exception:
                    # The device can be deleted while its probe is in flight, which makes
                    # the write hit a foreign key that no longer exists. Recording must
                    # never be able to stop the check from running again.
                    log.exception("could not record result for check %s", check.id)
        finally:
            if keep_running:
                self._reschedule(check)

    def _reschedule(self, check: CheckConfig) -> None:
        """Queue the next run — but only while the check still exists."""
        if check.id is None:
            return
        try:
            if self.store.get_check(check.id) is None:
                self._next_run.pop(check.id, None)
                return
        except Exception:  # noqa: BLE001 - a broken lookup must not stop the scheduler
            log.exception("could not look up check %s", check.id)
        interval = max(5, int(check.interval_sec))
        when = time.time() + interval
        self._next_run[check.id] = when
        heapq.heappush(self._queue, (when, self._next_seq(), check.id))
        self._wake.set()

    # -- state machine ---------------------------------------------------------------

    def _record(self, device: Device, check: CheckConfig, result: CheckResult) -> None:
        """Fold one probe result into per-check state and re-derive device health."""
        # The delete-confirm dialog and a probe that is already in flight can overlap;
        # the device or the check may be gone by the time the result comes back.
        if device.id is None or self.store.get_device(device.id) is None:
            return
        if check.id is not None and self.store.get_check(check.id) is None:
            return

        self.store.add_result(result)
        self.results_written += 1

        # 1. Update this check's own hysteresis counters.
        cs = self.store.get_check_state(check.id)  # type: ignore[arg-type]
        failures = int(cs["consecutive_failures"] or 0)
        successes = int(cs["consecutive_successes"] or 0)
        if result.ok:
            successes += 1
            failures = 0
        else:
            failures += 1
            successes = 0

        check_state = cs["state"] or STATE_UNKNOWN
        if result.ok:
            if successes >= check.successes_to_up:
                check_state = STATE_UP
                if (
                    check.degraded_latency_ms
                    and result.latency_ms is not None
                    and result.latency_ms > check.degraded_latency_ms
                ):
                    check_state = STATE_DEGRADED
        else:
            # A single lost packet is not an outage, but it is not healthy either:
            # below the threshold the check reads "degraded" so a flapping link is
            # visible on the dashboard before it is declared down.
            check_state = STATE_DOWN if failures >= check.failures_to_down else STATE_DEGRADED

        self.store.save_check_state(
            check_id=check.id,  # type: ignore[arg-type]
            device_id=device.id,  # type: ignore[arg-type]
            state=check_state,
            consecutive_failures=failures,
            consecutive_successes=successes,
            last_ts=result.ts,
            last_latency_ms=result.latency_ms,
            last_ok=result.ok,
            last_error="" if result.ok else result.message,
        )

        # 2. Re-derive device health from *all* of its enabled checks.
        previous_state = self.store.get_state(device.id)  # type: ignore[arg-type]
        previous = previous_state.state
        device_state = self._aggregate_device_state(device)  # type: ignore[arg-type]

        # 3. Roll the sliding window counters.
        device_state.total_checks += 1
        if result.ok:
            device_state.up_checks += 1
            if result.latency_ms is not None:
                device_state.last_latency_ms = result.latency_ms
            device_state.last_error = ""
        else:
            device_state.last_error = result.message
        device_state.last_check_ts = result.ts
        device_state.consecutive_failures = 0 if result.ok else (
            int(cs["consecutive_failures"] or 0) + 1
        )
        device_state.consecutive_successes = int(cs["consecutive_successes"] or 0) + 1 if result.ok else 0

        if device_state.state != previous:
            device_state.since = time.time()
        self.store.save_state(device_state)

        if self.on_result is not None:
            self.on_result(result)
        if device_state.state != previous:
            self._emit_transition(device, device_state, previous, device_state.state, result)

    def _aggregate_device_state(self, device: Device) -> DeviceState:
        """Worst-state-wins aggregation across a device's enabled checks."""
        state = self.store.get_state(device.id)  # type: ignore[arg-type]
        checks = self.store.list_checks(device_id=device.id, enabled_only=True)  # type: ignore[arg-type]
        if not checks:
            state.state = STATE_UNKNOWN
            return state

        per_check = self.store.check_states_for_device(device.id)  # type: ignore[arg-type]
        observed: list[str] = []
        for check in checks:
            row = per_check.get(check.id)  # type: ignore[arg-type]
            observed.append((row or {}).get("state") or STATE_UNKNOWN)

        if any(s == STATE_DOWN for s in observed):
            state.state = STATE_DOWN
        elif any(s == STATE_DEGRADED for s in observed):
            state.state = STATE_DEGRADED
        elif any(s == STATE_UP for s in observed):
            # A check that has never run reports UNKNOWN; that must not veto a device
            # whose other checks are proving it healthy, or a freshly added device would
            # sit at "unknown" forever next to a perfectly good latency graph.
            state.state = STATE_UP
        else:
            # Nothing is failing and nothing has been proven yet.
            state.state = STATE_UNKNOWN
        return state

    def _emit_transition(
        self,
        device: Device,
        state: DeviceState,
        previous: str,
        new_state: str,
        result: CheckResult,
    ) -> None:
        severity = TRANSITION_SEVERITY.get(new_state, "info")
        if new_state == STATE_UP:
            # Recovery from an alert state is notable; first-ever "up" is not.
            if previous == STATE_UNKNOWN:
                message = f"{device.name} is reachable ({result.latency_ms or '—'} ms)"
                severity = "info"
            else:
                message = f"{device.name} recovered ({result.latency_ms or '—'} ms)"
        elif new_state == STATE_DOWN:
            message = f"{device.name} is DOWN — {result.message or 'no response'}"
        elif new_state == STATE_DEGRADED:
            message = f"{device.name} degraded — {result.message or 'partial failures'}"
        else:
            message = f"{device.name} state changed to {new_state}"

        event = Event(
            device_id=device.id,
            device_name=device.name,
            kind="state",
            severity=severity,
            message=message,
            details={
                "previous": previous,
                "state": new_state,
                "check_kind": result.kind,
                "latency_ms": result.latency_ms,
                "consecutive_failures": state.consecutive_failures,
                "last_error": state.last_error,
            },
        )
        self.store.add_event(event)
        log.info("state %s: %s -> %s", device.name, previous, new_state)
        if self.on_event is not None:
            self.on_event(event)

    # -- on-demand runs --------------------------------------------------------------

    async def check_now(self, check_id: int) -> CheckResult | None:
        """Run one check immediately, out of band from the schedule."""
        check = self.store.get_check(check_id)
        if check is None:
            return None
        device = self.store.get_device(check.device_id)  # type: ignore[arg-type]
        if device is None:
            return None
        cred = self.store.get_credential(device.credential_id) if device.credential_id else None
        result = await run_check(check, device, cred)
        self._record(device, check, result)
        return result

    async def check_device(self, device_id: int) -> list[CheckResult]:
        """Run every enabled check of one device right now (the "Test" button)."""
        results: list[CheckResult] = []
        device = self.store.get_device(device_id)
        if device is None:
            return results
        cred = self.store.get_credential(device.credential_id) if device.credential_id else None
        checks = self.store.list_checks(device_id=device_id, enabled_only=True)
        if not checks:
            return results
        gathered = await asyncio.gather(
            *[run_check(c, device, cred) for c in checks], return_exceptions=True
        )
        for check, outcome in zip(checks, gathered):
            if isinstance(outcome, BaseException):
                outcome = CheckResult(
                    device_id=device_id,
                    check_id=check.id,
                    kind=check.kind,
                    ok=False,
                    message=f"probe crashed: {outcome!r}",
                )
            self._record(device, check, outcome)
            results.append(outcome)
        return results


def bootstrap_checks(store: Store, device: Device, extra: Iterable[CheckConfig] | None = None) -> list[CheckConfig]:
    """Attach the default check set (ping + SSH port [+ mgmt URL]) to a new device."""
    created: list[CheckConfig] = []
    if extra is not None:
        for check in extra:
            check.device_id = device.id
            created.append(store.add_check(check))
        return created
    from .checks import default_checks_for

    for check in default_checks_for(device):
        created.append(store.add_check(check))
    return created
