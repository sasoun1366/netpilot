"""Desktop entry point for netpilot."""

from __future__ import annotations

import logging
import os
import sys

from .main_window import MainWindow
from .safety import (
    install_crash_handler,
    install_exception_hook,
    install_logging,
    write_fatal,
)

log = logging.getLogger("netpilot.desktop")


def prepare_runtime(data_dir: str | None = None):
    """Everything that must be true before a window can exist.

    Split out of :func:`run_gui` so it can be exercised where it actually has to work:
    a build with no console, which is how the shippped desktop app starts on Windows.
    Returns the log file path.
    """
    log_file = install_logging(data_dir)
    install_crash_handler(data_dir)
    install_exception_hook(data_dir)
    log.info("netpilot desktop starting (data_dir=%s, log=%s)", data_dir, log_file)
    return log_file


def run_gui(data_dir: str | None = None, start_monitor: bool = True) -> int:
    """Launch the desktop application. Returns the process exit code."""
    try:
        log_file = prepare_runtime(data_dir)
    except Exception as exc:  # noqa: BLE001 - there is no window to report through yet
        # Logging itself may be the thing that failed, so write the traceback by hand.
        # Nothing above this line may be assumed to work.
        path = write_fatal(exc, data_dir)
        print(f"netpilot could not start: {exc!r}", file=sys.stderr or sys.stdout)
        if path is not None:
            print(f"the details are in {path}", file=sys.stderr or sys.stdout)
        return 4

    try:
        from PyQt6.QtCore import Qt
        from PyQt6.QtWidgets import QApplication, QMessageBox
    except ImportError as exc:  # pragma: no cover - depends on install extras
        # A windowed build has no stderr at all, so do not assume one is there.
        target = sys.stderr or sys.stdout
        print(
            'netpilot desktop needs PyQt6 — install it with: pip install "netpilot[gui]"',
            file=target,
        )
        print(f"({exc})", file=target)
        write_fatal(exc, data_dir)
        return 2

    from .bridge import CoreThread
    from .main_window import app_icon
    from .widgets import DARK_QSS

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    application = QApplication(sys.argv)
    application.setApplicationName("netpilot")
    application.setOrganizationName("netpilot")
    application.setStyleSheet(DARK_QSS)

    core = CoreThread(data_dir=data_dir, start_monitor=start_monitor)
    core.ready.connect(lambda ok, detail: log.info("core ready=%s (%s)", ok, detail))
    core.start()

    if not core.wait_ready(20.0):
        QMessageBox.critical(
            None,
            "netpilot could not start",
            "The monitoring core did not come up in time.\n\n"
            "Try running `netpilot doctor` in a terminal to see what is wrong.",
        )
        return 3

    window = MainWindow(core)
    window.setWindowIcon(app_icon())
    window.show()
    # Launched from a shortcut or a console-less build, the window can start behind
    # whatever owns the screen; make sure the user actually sees it.
    window.raise_()
    window.activateWindow()

    try:
        code = application.exec()
    except Exception:  # noqa: BLE001 - a crash must leave a traceback behind
        log.exception("the desktop event loop failed")
        code = 1
    finally:
        core.stop()
        log.info("netpilot desktop exiting with code %s", code)
    return code


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    sys.exit(run_gui(os.environ.get("NETPILOT_HOME")))
