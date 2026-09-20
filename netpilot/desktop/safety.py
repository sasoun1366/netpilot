"""Keep an unexpected error from taking the whole desktop app down.

PyQt turns an unhandled exception inside a slot into ``qFatal()``. In a windowed
build — no console, so nowhere for the traceback to go — that reads to the user as
"the app froze" or "it just vanished". Two defences:

* :func:`safe_slot` wraps a slot so nothing escapes into Qt in the first place; the
  traceback goes to the log file and a short line goes to the status bar.
* :func:`install_logging` wires up a rotating log file plus ``faulthandler``, so a
  freeze or a hard crash still leaves something to read afterwards.
"""

from __future__ import annotations

import faulthandler
import functools
import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Any, Callable, TypeVar

log = logging.getLogger("netpilot.desktop")

F = TypeVar("F", bound=Callable[..., Any])

#: Where the desktop app writes its log; shown to the user when something goes wrong.
LOG_NAME = "netpilot.log"


def log_path(data_dir: str | Path | None = None) -> Path:
    from ..db import default_data_dir

    base = Path(data_dir) if data_dir else default_data_dir()
    return base / LOG_NAME


def install_logging(data_dir: str | Path | None = None, level: int = logging.INFO) -> Path:
    """Send logs to a rotating file next to the database; return the path."""
    path = log_path(data_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:  # pragma: no cover - unwritable location
        logging.basicConfig(level=level)
        return path

    root = logging.getLogger()
    if not any(isinstance(h, logging.handlers.RotatingFileHandler) for h in root.handlers):
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
        )
        root.addHandler(handler)
        root.setLevel(level)
    return path


def install_crash_handler(data_dir: str | Path | None = None) -> Path:
    """Also dump Python stacks on a hard fault, so a freeze is diagnosable.

    Written to a separate file: a stack dump mid-fault must not fight the log
    handler for the same file descriptor.
    """
    path = log_path(data_dir).with_name("netpilot-crash.log")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(path, "a", encoding="utf-8")  # noqa: SIM115 - lives for the process
        faulthandler.enable(file=handle)
        faulthandler.dump_traceback_later(0, exit=False)  # no-op unless armed later
    except (OSError, ValueError):  # pragma: no cover
        faulthandler.enable()
    return path


def install_exception_hook(data_dir: str | Path | None = None) -> None:
    """Log anything that escapes a Qt callback before Qt decides how to die."""
    previous = sys.excepthook

    def hook(exc_type, exc_value, exc_tb):  # type: ignore[no-untyped-def]
        if issubclass(exc_type, KeyboardInterrupt):
            previous(exc_type, exc_value, exc_tb)
            return
        log.critical(
            "unhandled exception", exc_info=(exc_type, exc_value, exc_tb)
        )
        previous(exc_type, exc_value, exc_tb)

    sys.excepthook = hook


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
