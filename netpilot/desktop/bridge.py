"""Asyncio ↔ Qt bridge.

The monitoring engine and the deploy engine are asyncio-native; Qt owns the main thread.
Rather than sprinkle ``QTimer`` polling everywhere, this module runs a single asyncio loop
in a worker thread and marshals everything across the boundary:

* ``submit(coro)`` runs a coroutine on the loop and hands back a :class:`concurrent.futures.Future`,
  so a Qt slot can await it via ``done`` callbacks without blocking the UI.
* App events (state transitions, deploy progress, probe results) are forwarded as Qt signals,
  which Qt delivers safely onto the GUI thread.

The result is a desktop app that is genuinely live — a device going down repaints the card
immediately — using exactly the same core as the web dashboard.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import logging
import threading
import time
from typing import Any, Callable, Coroutine

from PyQt6.QtCore import QObject, QThread, QTimer, pyqtSignal

log = logging.getLogger("netpilot.desktop.bridge")

#: How long a core call may be outstanding before the desktop says something about it.
#: Generous on purpose: a forced ICMP probe on Windows can take a few seconds.
SLOW_CALL_SECONDS = 20.0


class CoreThread(QThread):
    """A QThread that owns an asyncio event loop and a :class:`netpilot.core.App`."""

    #: Emitted for every Event produced by the monitor or the deploy engine.
    event_received = pyqtSignal(object)
    #: Emitted whenever a deployment job changes state.
    job_updated = pyqtSignal(object)
    #: Emitted for every probe result (already throttled by the UI).
    result_received = pyqtSignal(object)
    #: Emitted once the core is up (or failed to start).
    ready = pyqtSignal(bool, str)

    def __init__(self, data_dir: str | None = None, start_monitor: bool = True, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.data_dir = data_dir
        self.start_monitor = start_monitor
        self.app: Any = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self._shutdown = threading.Event()
        self._started = threading.Event()

    # -- thread body -----------------------------------------------------------------

    def run(self) -> None:  # noqa: D102 - QThread entry point
        from ..core import App

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.loop = loop

        try:
            self.app = App(data_dir=self.data_dir, start_monitor=self.start_monitor)
            self.app.events.on_event.append(lambda e: self.event_received.emit(e))
            self.app.events.on_job.append(lambda j: self.job_updated.emit(j))
            self.app.events.on_result.append(lambda r: self.result_received.emit(r))
            loop.run_until_complete(self.app.start())
        except Exception as exc:  # noqa: BLE001 - report startup failure to the UI
            log.exception("core failed to start")
            self.ready.emit(False, f"{exc.__class__.__name__}: {exc}")
            loop.close()
            return

        self.ready.emit(True, str(self.app.store.data_dir))
        self._started.set()

        async def idle() -> None:
            while not self._shutdown.is_set():
                await asyncio.sleep(0.1)

        try:
            loop.run_until_complete(idle())
        finally:
            try:
                loop.run_until_complete(self.app.stop())
                self.app.store.close()
            except Exception:  # noqa: BLE001
                log.exception("core failed to shut down cleanly")
            pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()

    # -- public API ------------------------------------------------------------------

    def wait_ready(self, timeout: float = 15.0) -> bool:
        return self._started.wait(timeout)

    def submit(self, coro: Coroutine[Any, Any, Any]) -> concurrent.futures.Future[Any]:
        """Schedule *coro* on the core loop; the returned future resolves off-thread."""
        if self.loop is None or not self._started.is_set():
            future: concurrent.futures.Future[Any] = concurrent.futures.Future()
            future.set_exception(RuntimeError("netpilot core is not running"))
            return future
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def call(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> concurrent.futures.Future[Any]:
        """Run a *sync* core method on the loop thread, keeping SQLite access serialised."""
        return self.submit(_call_sync(fn, args, kwargs))

    def stop(self) -> None:
        self._shutdown.set()
        self.wait(6000)
        if self.isRunning():  # pragma: no cover - only if the loop refuses to exit
            self.terminate()
            self.wait(2000)


async def _call_sync(fn: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    return fn(*args, **kwargs)


class UiBridge(QObject):
    """Convenience wrapper: run a core callable and invoke a Qt callback with the result.

    Qt slots must not block, so every call goes through the core loop and comes back as a
    signal. Errors are routed to ``on_error`` instead of crashing the event loop.
    """

    _completed = pyqtSignal(object, object)

    def __init__(self, thread: CoreThread, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.thread = thread
        self._callbacks: dict[int, Callable[[Any], None]] = {}
        self._errors: dict[int, Callable[[Exception], None]] = {}
        self._counter = 0
        self._started_at: dict[int, float] = {}
        self._names: dict[int, str] = {}
        self._warned: set[int] = set()
        self._completed.connect(self._dispatch)

        # A core call that never comes back used to look exactly like a frozen window:
        # nothing on screen changed and nothing was said. Now it is announced.
        self._watchdog = QTimer(self)
        self._watchdog.setInterval(5000)
        self._watchdog.timeout.connect(self._check_slow_calls)
        self._watchdog.start()

    def run(
        self,
        work: Callable[[], Any],
        on_done: Callable[[Any], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        """Run *work* on the core loop.

        ``work`` may be a coroutine function (awaited on the core loop) or a plain
        callable. Blocking callables are pushed to a worker thread so a slow database
        query can never stall the monitoring scheduler. Either way the result comes back
        on the GUI thread via ``on_done`` — UI code never touches a thread boundary.
        """
        if inspect.iscoroutinefunction(work):
            awaitable: Any = work()
        else:

            async def _wrap() -> Any:
                loop = asyncio.get_running_loop()
                # The lambda itself is cheap; running it off-loop keeps blocking core
                # methods (database reads, etc.) from stalling the scheduler.
                value = await loop.run_in_executor(None, work)
                if inspect.isawaitable(value):
                    value = await value
                return value

            awaitable = _wrap()

        future = self.thread.submit(awaitable)
        token = self._counter
        self._counter += 1
        self._started_at[token] = time.monotonic()
        self._names[token] = getattr(work, "__qualname__", None) or getattr(
            work, "__name__", None
        ) or "core call"
        if on_done is not None:
            self._callbacks[token] = on_done
        if on_error is not None:
            self._errors[token] = on_error

        def _finished(fut: concurrent.futures.Future[Any]) -> None:
            try:
                result = fut.result()
                error = None
            except Exception as exc:  # noqa: BLE001 - delivered to on_error
                result, error = None, exc
            self._completed.emit((token, result), error)

        future.add_done_callback(_finished)

    def _check_slow_calls(self) -> None:
        """Log a call that has been outstanding for a while, once per call."""
        if not self._started_at:
            return
        now = time.monotonic()
        for token, started in list(self._started_at.items()):
            elapsed = now - started
            if elapsed < SLOW_CALL_SECONDS or token in self._warned:
                continue
            self._warned.add(token)
            log.warning(
                "core call %r has been running for %.1fs",
                self._names.get(token, "core call"),
                elapsed,
            )

    def _forget(self, token: int) -> None:
        self._started_at.pop(token, None)
        self._names.pop(token, None)
        self._warned.discard(token)

    def _dispatch(self, payload: object, error: object) -> None:
        token, result = payload  # type: ignore[misc]
        self._forget(token)
        if error is not None:
            handler = self._errors.pop(token, None)
            self._callbacks.pop(token, None)
            if handler is not None:
                handler(error)  # type: ignore[arg-type]
            else:
                log.error("core call failed: %r", error)
            return
        callback = self._callbacks.pop(token, None)
        self._errors.pop(token, None)
        if callback is not None:
            callback(result)
