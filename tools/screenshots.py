"""Regenerate the screenshots used in the README.

The desktop window is rendered offscreen, so this works on a headless box (including
CI) as long as PyQt6 is installed:

    QT_QPA_PLATFORM=offscreen python tools/screenshots.py

Everything is drawn from a throwaway database seeded with a small demo network, so no
real device is ever contacted.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PyQt6.QtCore import QTimer  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from netpilot.desktop.bridge import CoreThread  # noqa: E402
from netpilot.desktop.main_window import MainWindow  # noqa: E402
from netpilot.desktop.widgets import DARK_QSS  # noqa: E402
from netpilot.models import CheckResult, Event  # noqa: E402

OUT = ROOT / "docs"
DEMO = [
    {"name": "core-rtr-01", "host": "10.10.0.1", "vendor": "mikrotik", "site": "Tokyo DC",
     "tags": ["core", "prod"]},
    {"name": "core-rtr-02", "host": "10.10.0.2", "vendor": "mikrotik", "site": "Tokyo DC",
     "tags": ["core", "prod"]},
    {"name": "edge-sw-01", "host": "10.20.0.11", "vendor": "cisco", "site": "Osaka POP",
     "tags": ["edge"]},
    {"name": "branch-fw-01", "host": "10.30.0.5", "vendor": "cisco", "site": "Nagoya",
     "tags": ["branch"]},
    {"name": "lab-shell", "host": "10.99.0.9", "vendor": "generic", "site": "Lab",
     "tags": ["lab"]},
]


def seed(core: CoreThread) -> None:
    for spec in DEMO:
        core.submit(core.app.add_device(spec)).result(30)
    # Feed each device a little synthetic history so the dashboard has shape to draw.
    # Pattern per device: how the run of probes should end.
    #   (device_id, results)  — True = reply, False = timeout
    patterns = {
        1: [True] * 40,                     # healthy core router
        2: [True] * 34 + [True, False, True, True, True, True],  # occasional blip
        3: [True] * 20 + [False] * 8,       # failing edge switch
        4: [True] * 12,                     # brand-new branch firewall
    }

    def feed() -> None:
        for device_id, results in patterns.items():
            device = core.app.store.get_device(device_id)
            check = core.app.store.list_checks(device_id=device_id)[0]
            for index, ok in enumerate(results):
                core.app.monitor._record(
                    device,
                    check,
                    CheckResult(
                        device_id=device_id,
                        check_id=check.id,
                        kind=check.kind,
                        ok=ok,
                        latency_ms=(1.8 + (index % 7) * 0.4) if ok else None,
                        message="icmp echo reply" if ok else "no reply within 1000 ms",
                    ),
                )
        for message, severity, device_id in (
            ("edge-sw-01 has been down for 2m", "critical", 3),
            ("core-rtr-02 latency spiked to 42 ms", "warning", 2),
            ("branch-fw-01 is reachable (4.1 ms)", "info", 4),
        ):
            core.app.store.add_event(
                Event(kind="device", severity=severity, device_id=device_id, message=message)
            )

    core.call(feed).result(60)
    core.call(
        core.app.create_deploy,
        body="",
        vendor="mikrotik",
        device_ids=[1, 2],
        template_name="NTP servers",
        options={"dry_run": True},
        triggered_by="demo",
    ).result(30)


def main() -> int:
    app = QApplication(sys.argv)
    app.setStyleSheet(DARK_QSS)

    data_dir = tempfile.mkdtemp(prefix="netpilot-shots-")
    core = CoreThread(data_dir=data_dir, start_monitor=False)
    core.start()
    if not core.wait_ready(30):
        print("core never became ready", file=sys.stderr)
        return 1
    seed(core)

    window = MainWindow(core)
    window.resize(1400, 900)
    window.show()

    # Sidebar order: Dashboard, Devices, Deploy config, Templates, Backups, Events, Settings
    shots = [
        ("dashboard", 0),
        ("devices", 1),
        ("deploy", 2),
        ("templates", 3),
        ("backups", 4),
        ("alerts", 5),
    ]
    OUT.mkdir(exist_ok=True)
    step = {"index": 0}

    def capture() -> None:
        index = step["index"]
        if index >= len(shots):
            app.quit()
            return
        name, page = shots[index]
        window.show_page(page)
        QTimer.singleShot(1500, lambda: save(name, page))

    def save(name: str, page: int) -> None:
        pixmap = window.grab()
        path = OUT / f"screenshot-{name}.png"
        pixmap.save(str(path))
        print(f"wrote {path.relative_to(ROOT)} ({pixmap.width()}x{pixmap.height()})")
        step["index"] += 1
        QTimer.singleShot(400, capture)

    QTimer.singleShot(2500, capture)
    app.exec()
    core.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
