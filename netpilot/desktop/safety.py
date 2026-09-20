"""Keep an unexpected error from taking the whole desktop app down.

PyQt turns an unhandled exception inside a slot into ``qFatal()``. In a windowed build
there is no console, so the traceback goes nowhere: the user sees "it froze" or "it just
vanished". Two defences:

* :func:`safe_slot` wraps a slot so nothing escapes into Qt in the first place; the
  traceback goes to the log file and a short line goes to the status bar.
* :func:`install_logging`, :func:`install_crash_handler` and
  :func:`install_exception_hook` make sure a crash leaves something readable behind.

**Nothing in this module may write to ``sys.stdout``/``sys.stderr``.** A windowed build
(``console=False``) starts with both set to ``None``: anything that touches them raises
``RuntimeError`` — including ``faulthandler.enable()`` with no argument — and it happens
before a window exists, so the app dies with no window and no message. That is a real
regression this module shipped once, hence the rule.
"""

from __future__ import annotations

import faulthandler
import functools
import logging
import logging.handlers
import sys
import traceback
from pathlib import Path
from typing import Any, Callable, TypeVar

log = logging.getLogger("netpilot.desktop")

F = TypeVar("F", bound=Callable[..., Any])

#: Where the desktop app writes its log; shown to the user when something goes wrong.
LOG_NAME = "netpilot.log"
#: A hard fault (segfault) dumps stacks here.
CRASH_LOG_NAME = "netpilot-crash.log"
#: A failure that beat the log setup is written here.
FATAL_LOG_NAME = "netpilot-fatal.log"


def data_dir_for(data_dir: str | Path | None) -> Path:
    if data_dir:
        return Path(data_dir)
    from ..db import default_data_dir

    return Path(default_data_dir())


def log_path(data_dir: str | Path | None = None) -> Path:
    return data_dir_for(data_dir) / LOG_NAME


def crash_log_path(data_dir: str | Path | None = None) -> Path:
    return data_dir_for(data_dir) / CRASH_LOG_NAME


def fatal_log_path(data_dir: str | Path | None = None) -> Path:
    return data_dir_for(data_dir) / FATAL_LOG_NAME


def has_console() -> bool:
    """False in a windowed build — the condition this module keeps tripping over."""
    return sys.stderr is not None


def install_logging(data_dir: str | Path | None = None, level: int = logging.INFO) -> Path:
    """Send logs to a rotating file next to the database; return the path.

    Never falls back to a stream: if the file cannot be opened the app keeps running
    quietly rather than crashing on a ``stderr`` that does not exist.
    """
    path = log_path(data_dir)
    root = logging.getLogger()
    for handler in root.handlers:
        if isinstance(handler, logging.handlers.RotatingFileHandler) and \
                getattr(handler, "baseFilename", None) == str(path.resolve()):
            root.setLevel(level)
            return path

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
        )
    except OSError:
        handler = logging.NullHandler()
    root.addHandler(handler)
    root.setLevel(level)
    return path


def install_crash_handler(data_dir: str | Path | None = None) -> Path:
    """Dump Python stacks if the process dies hard, so a freeze leaves evidence.

    A separate file on purpose: a stack dump mid-fault must not fight the log handler
    for the same descriptor. The descriptor stays open for the life of the process,
    which is why it is deliberately not closed here.
    """
    path = crash_log_path(data_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(path, "a", encoding="utf-8")  # noqa: SIM115 - lives for the process
    except OSError:
        return path

    try:
        # The file argument matters: without it faulthandler reaches for sys.stderr and
        # raises RuntimeError in a build that has none.
        faulthandler.enable(file=handle, all_threads=True)
    except (RuntimeError, ValueError, OSError):
        pass
    # No dump_traceback_later() here: it rejects a non-positive timeout, and a timer is
    # only meaningful when somebody deliberately arms one.
    return path


def write_fatal(exc: BaseException, data_dir: str | Path | None = None) -> Path | None:
    """Write a startup failure to disk when logging is not up yet (or is not working)."""
    path = fatal_log_path(data_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "netpilot could not start.\n\n"
            + "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
            encoding="utf-8",
        )
        return path
    except OSError:
        return None


def install_exception_hook(data_dir: str | Path | None = None) -> None:
    """Log anything that escapes a Qt callback before Qt decides how to die."""
    previous = sys.excepthook

    def hook(exc_type, exc_value, exc_tb):  # type: ignore[no-untyped-def]
        if issubclass(exc_type, KeyboardInterrupt):
            _to_console(previous, exc_type, exc_value, exc_tb)
            return
        log.critical("unhandled exception", exc_info=(exc_type, exc_value, exc_tb))
        if not logging.getLogger().handlers and not write_fatal(exc_value, data_dir):
            pass  # nothing was listening and nothing could be written; nothing to do
        _to_console(previous, exc_type, exc_value, exc_tb)

    sys.excepthook = hook


def _to_console(previous: Any, exc_type: Any, exc_value: Any, exc_tb: Any) -> None:
    """Hand the error to the previous hook — but only if there is anywhere to print."""
    if not has_console():
        return
    try:
        previous(exc_type, exc_value, exc_tb)
    except Exception:  # noqa: BLE001 - a reporter must never be the thing that fails
        pass


def safe_slot(func: F) -> F:
    """Wrap a Qt slot so an error is reported instead of aborting the process.

    The wrapped callable keeps its signature; the exception is logged with a full
    traceback and summarised in the window's status bar.
    """

    @functools.wraps(func)
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return func(self, *args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - this is the last line of defence
            log.exception("error in %s", getattr(func, "__qualname__", func))
            write_fatal(exc, getattr(self, "_data_dir", None))
            status = getattr(self, "status", None)
            if status is not None:
                try:
                    status.showMessage(
                        f"{type(exc).__name__}: {exc}  (see netpilot.log)", 15000
                    )
                except Exception:  # noqa: BLE001 - never let the reporter raise
                    pass
            return None

    return wrapper  # type: ignore[return-value]
