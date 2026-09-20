"""Desktop entry point for netpilot."""

from __future__ import annotations

import logging
import os
import sys

from .main_window import MainWindow

log = logging.getLogger("netpilot.desktop")


def run_gui(data_dir: str | None = None, start_monitor: bool = True) -> int:
    """Launch the desktop application. Returns the process exit code."""
    try:
        from PyQt6.QtCore import Qt
        from PyQt6.QtWidgets import QApplication, QMessageBox
    except ImportError as exc:  # pragma: no cover - depends on install extras
        print(
            'netpilot desktop needs PyQt6 — install it with: pip install "netpilot[gui]"',
            file=sys.stderr,
        )
        print(f"({exc})", file=sys.stderr)
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

    try:
        return application.exec()
    finally:
        core.stop()


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    sys.exit(run_gui(os.environ.get("NETPILOT_HOME")))
