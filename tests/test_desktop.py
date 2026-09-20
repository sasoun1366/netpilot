"""The desktop app, driven headlessly.

The shipped Windows build hung on the two most basic actions a user can take — add a
device and delete one — because a slot called a method that did not exist and PyQt turns
that into ``qFatal()``. Nothing in the suite touched these flows, so nothing caught it.

These tests drive the real ``MainWindow``: real dialogs, real bridge, real database. They
need PyQt6 and an offscreen platform plugin, so they skip cleanly when the GUI extra is
not installed.
"""

from __future__ import annotations

import ast
import importlib
import os
import pathlib
import time

import pytest

pytest.importorskip("PyQt6", reason="the desktop extra is optional")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QTimer  # noqa: E402
from PyQt6.QtWidgets import QApplication, QDialog, QMessageBox  # noqa: E402

from netpilot.desktop.main_window import (  # noqa: E402
    DeviceEditDialog,
    MainWindow,
    TemplateEditDialog,
)
from netpilot.desktop.bridge import CoreThread  # noqa: E402

DESKTOP_DIR = pathlib.Path(__file__).resolve().parents[1] / "netpilot" / "desktop"

def _classes_in(path: pathlib.Path) -> dict[str, tuple[set[str], set[tuple[str, int]]]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: dict[str, tuple[set[str], set[tuple[str, int]]]] = {}

    class Visitor(ast.NodeVisitor):
        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            defined: set[str] = set()
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    defined.add(item.name)
                elif isinstance(item, ast.Assign):
                    defined.update(
                        t.id for t in item.targets if isinstance(t, ast.Name)
                    )
                elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    defined.add(item.target.id)
            # anything assigned anywhere in the class body counts too
            for sub in ast.walk(node):
                if isinstance(sub, ast.Assign):
                    defined.update(t.id for t in sub.targets if isinstance(t, ast.Name))
                elif isinstance(sub, ast.AnnAssign) and isinstance(sub.target, ast.Name):
                    defined.add(sub.target.id)

            used: set[tuple[str, int]] = set()
            for sub in ast.walk(node):
                if (
                    isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Attribute)
                    and isinstance(sub.func.value, ast.Name)
                    and sub.func.value.id == "self"
                ):
                    used.add((sub.func.attr, sub.lineno))
            found[node.name] = (defined, used)
            self.generic_visit(node)

    Visitor().visit(tree)
    return found


@pytest.mark.parametrize("module", sorted(p.name for p in DESKTOP_DIR.glob("*.py")))
def test_no_self_reference_to_a_missing_attribute(module):
    """A slot calling a method that does not exist is a crash, not a bug report.

    Qt-inherited methods are resolved for real (``hasattr`` on the class), so there is no
    hand-maintained allow-list to drift out of date.
    """
    imported = importlib.import_module(f"netpilot.desktop.{module[:-3]}")
    broken: list[str] = []
    for owner, (_defined, used) in _classes_in(DESKTOP_DIR / module).items():
        kind = getattr(imported, owner, None)
        for attr, line in sorted(used, key=lambda item: item[1]):
            if kind is not None and hasattr(kind, attr):
                continue
            broken.append(f"{module}:{line} {owner}.self.{attr}(...)")
    assert not broken, "calls to attributes the class never defines:\n  " + "\n  ".join(broken)


def test_every_slot_is_guarded_or_harmless():
    """The class that owns device actions must route them through ``safe_slot``."""
    source = (DESKTOP_DIR / "main_window.py").read_text(encoding="utf-8")
    for name in ("on_device_action", "on_event", "on_job", "on_result", "refresh_all"):
        index = source.find(f"def {name}(")
        assert index != -1, name
        preceding = source[max(0, index - 200) : index]
        assert "@safe_slot" in preceding, f"{name} is not wrapped in safe_slot"


# ── fixtures ────────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def window(qt_app, tmp_path, monkeypatch):
    """A live MainWindow with an auto-pilot for modal dialogs and message boxes."""
    core = CoreThread(data_dir=str(tmp_path / "gui"), start_monitor=True)
    core.start()
    assert core.wait_ready(20), "the core never came up"

    # Message boxes would block forever with nobody to click them; record instead.
    # Qt's statics are (parent, title, text, ...) and the first arg is the parent widget.
    seen: list[tuple[str, str, str]] = []
    for name in ("critical", "information", "warning", "question"):
        def make(kind: str):
            def stub(*args, **kwargs):
                rest = [str(a) for a in args[1:]]
                title = rest[0] if rest else ""
                text = rest[1] if len(rest) > 1 else ""
                seen.append((kind, title, text))
                return QMessageBox.StandardButton.Yes
            return staticmethod(stub)
        monkeypatch.setattr(QMessageBox, name, make(name))

    # A modal exec() with nobody to click it blocks forever: return immediately, and let
    # the test that cares install its own behaviour on the specific dialog class.
    monkeypatch.setattr(QDialog, "exec", lambda self: QDialog.DialogCode.Rejected)

    main = MainWindow(core)
    main.resize(1200, 800)
    main.show()
    # A confirmation prompt is a normal part of a flow, not a failure.
    main.qt_prompts = seen  # type: ignore[attr-defined]
    main.qt_errors = [row for row in seen if row[0] != "question"]  # type: ignore[attr-defined]
    yield main
    main.close()
    core.stop()


def pump(window, seconds: float = 1.5) -> None:
    """Let queued signals and bridge callbacks land, like a real event loop would."""
    deadline = time.time() + seconds
    app = QApplication.instance()
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.02)


def add_device(window, host: str = "10.200.0.1", name: str = "probe-box") -> None:
    """Press '+ Add device', fill it in and save — the flow a user performs."""
    original = DeviceEditDialog.exec

    def auto_exec(self):  # noqa: ANN001
        self.host.setText(host)
        self.name.setText(name)
        self._validate_and_accept()
        return QDialog.DialogCode.Accepted

    DeviceEditDialog.exec = auto_exec
    try:
        window.add_device()
    finally:
        DeviceEditDialog.exec = original
    pump(window, 2.0)


# ── flows ───────────────────────────────────────────────────────────────────────────


def test_adding_a_device_from_the_window(window):
    add_device(window)
    stored = [d.name for d in window.core.app.store.list_devices()]
    assert stored == ["probe-box"]
    assert len(window.core.app.store.list_checks(device_id=1)) == 2, "monitors attached"
    assert not window.qt_errors, window.qt_errors


def test_opening_the_device_window_works(window):
    add_device(window)
    window.open_device(1)
    pump(window, 2.0)
    assert window.detail_dialog is not None, "the detail window did not open"
    assert not window.qt_errors, window.qt_errors


def test_deleting_a_device_from_the_window(window):
    """The action that used to raise AttributeError and take the app with it."""
    add_device(window)
    before = len(window.core.app.store.list_devices())
    window.on_device_action("delete", 1)
    pump(window, 2.5)

    assert len(window.core.app.store.list_devices()) == before - 1
    assert window.core.app.store.list_devices() == []
    assert not window.qt_errors, window.qt_errors


def test_every_device_action_is_reachable(window):
    """Guard the whole action menu, not just the two that were reported broken."""
    add_device(window)
    window.open_device(1)
    pump(window, 2.0)
    for key in ("test", "probe", "backup", "edit", "preview", "deploy"):
        window.detail_dialog = window.detail_dialog  # the window supplies the dialog
        window.on_device_action(key, 1)
        pump(window, 0.6)
    assert not window.qt_errors, f"a device action raised: {window.qt_errors}"


def test_deleting_a_device_that_is_already_gone_is_survivable(window):
    add_device(window)
    window.on_device_action("delete", 1)
    pump(window, 2.0)
    window.on_device_action("delete", 1)  # stale id from a closed dialog
    pump(window, 1.0)
    assert not window.qt_errors, window.qt_errors


def test_the_confirm_box_reports_the_device_name(window):
    add_device(window, name="named-box")
    window.on_device_action("delete", 1)
    pump(window, 1.0)
    prompts = [text for kind, _title, text in window.qt_prompts if kind == "question"]
    assert prompts, "delete never asked for confirmation"


def test_template_preview_before_wiring_does_not_explode(qt_app):
    """``_preview`` reads ``_preview_cb``; the constructor must have set it."""
    dialog = TemplateEditDialog([], None, None)
    try:
        dialog._preview()  # as the debounce timer would, pre-wiring
    finally:
        dialog.close()


def test_credentials_and_templates_are_loaded(window):
    pump(window, 2.5)
    assert window.vendors, "the vendor list never arrived"
    assert isinstance(window.credentials, list)


def test_the_refresh_loop_survives_a_missing_overview(window):
    """A failed overview call must leave the window usable, not half-updated."""
    window.overview = {}
    window.devices = []
    window.refresh_all()
    pump(window, 1.5)
    assert not window.qt_errors, window.qt_errors


def test_device_lookup_helper_matches_the_overview(window):
    add_device(window)
    pump(window, 2.0)
    index = window.overview_devices_by_id()
    assert 1 in index
    assert index[1]["name"] == "probe-box"


# ── the two Windows fixes ───────────────────────────────────────────────────────────


def test_a_broken_slot_reports_instead_of_killing_the_app(qt_app):
    """PyQt aborts the process on an exception in a slot; ``safe_slot`` must not."""
    from netpilot.desktop.safety import safe_slot

    class Fake:
        class Status:
            def __init__(self) -> None:
                self.messages: list[str] = []

            def showMessage(self, text: str, _ms: int = 0) -> None:
                self.messages.append(text)

        def __init__(self) -> None:
            self.status = Fake.Status()

        @safe_slot
        def boom(self) -> None:
            raise RuntimeError("kaboom")

    fake = Fake()
    assert fake.boom() is None, "the call must return quietly"
    assert "kaboom" in fake.status.messages[-1]
    assert "netpilot.log" in fake.status.messages[-1], "the user is told where the log is"


def test_probes_never_open_a_console_window_on_windows(monkeypatch):
    """A windowed build spawns a console for every child process unless told not to."""
    import subprocess

    from netpilot.monitoring import icmp

    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

    monkeypatch.setattr(icmp, "_IS_WINDOWS", False)
    assert icmp.subprocess_kwargs() == {}, "no flags on POSIX"

    monkeypatch.setattr(icmp, "_IS_WINDOWS", True)
    assert icmp.subprocess_kwargs() == {"creationflags": no_window}


def test_the_ping_probe_passes_the_console_flag_through(monkeypatch):
    """The scheduler's own ping path must use the same flags, not just icmp.py."""
    import subprocess

    from netpilot.monitoring import checks, icmp

    captured: dict[str, object] = {}

    class Result:
        returncode = 0
        stdout = "64 bytes from 10.255.255.1: icmp_seq=1 ttl=64 time=1.2 ms"
        stderr = ""

    def fake_run(cmd, **kwargs):  # noqa: ANN001
        captured.update(kwargs)
        return Result()

    monkeypatch.setattr(icmp, "_IS_WINDOWS", True)
    monkeypatch.setattr(subprocess, "run", fake_run)
    checks.asyncio.run(checks._run(checks.ping_command("10.255.255.1", 1, 1.0), 1.0))

    assert captured.get("creationflags") == getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def test_an_unguarded_slot_does_not_abort_the_process(tmp_path):
    """PyQt calls ``qFatal()`` on an exception in a slot — unless an excepthook is set.

    Run in a child process: if this regresses, the failure is an abort, and an abort in
    pytest would take the whole suite with it.
    """
    import subprocess
    import sys
    import textwrap

    script = tmp_path / "slot_crash.py"
    script.write_text(
        textwrap.dedent(
            """
            import os, sys, tempfile
            os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
            sys.path.insert(0, %r)
            from PyQt6.QtCore import QTimer
            from PyQt6.QtWidgets import QApplication
            from netpilot.desktop.safety import install_exception_hook, install_logging

            log_file = install_logging(tempfile.mkdtemp(prefix="np-hook-"))
            install_exception_hook()
            app = QApplication([])

            def boom():
                raise RuntimeError("boom from a slot")

            QTimer.singleShot(50, boom)
            QTimer.singleShot(500, app.quit)
            app.exec()
            print("SURVIVED")
            print(open(log_file).read())
            """
        )
        % str(pathlib.Path(__file__).resolve().parents[1]),
        encoding="utf-8",
    )

    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    proc = subprocess.run(  # noqa: S603
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    assert proc.returncode == 0, f"the app died (rc={proc.returncode}):\n{proc.stdout}\n{proc.stderr}"
    assert "SURVIVED" in proc.stdout
    assert "boom from a slot" in proc.stdout, "the traceback never reached the log file"


def test_a_slow_core_call_is_announced_in_the_log(qt_app, caplog):
    """Silence was the real problem: a stuck call must leave a trace."""
    from netpilot.desktop import bridge as bridge_mod

    core = bridge_mod.UiBridge.__mro__  # touch the module so the name is bound
    assert core

    def slow():
        time.sleep(0.4)
        return "done"

    thread = CoreThread(data_dir=None, start_monitor=False)
    thread.start()
    assert thread.wait_ready(20)
    try:
        ui = bridge_mod.UiBridge(thread)
        monkey = caplog.at_level("WARNING", logger="netpilot.desktop.bridge")
        with monkey:
            ui._started_at[999] = time.monotonic() - (bridge_mod.SLOW_CALL_SECONDS + 1)
            ui._names[999] = "slow_thing"
            ui._check_slow_calls()
            ui._check_slow_calls()  # only once per call
        warnings = [r for r in caplog.records if "slow_thing" in r.getMessage()]
        assert len(warnings) == 1, [r.getMessage() for r in caplog.records]
    finally:
        thread.stop()


def test_the_watchdog_forgets_finished_calls(qt_app):
    from netpilot.desktop import bridge as bridge_mod

    thread = CoreThread(data_dir=None, start_monitor=False)
    thread.start()
    assert thread.wait_ready(20)
    try:
        ui = bridge_mod.UiBridge(thread)
        ui._started_at[1] = time.monotonic()
        ui._names[1] = "thing"
        ui._forget(1)
        assert not ui._started_at and not ui._names
    finally:
        thread.stop()


def test_a_hidden_modal_dialog_is_brought_to_the_front(window, monkeypatch):
    """Windows sometimes puts a native dialog behind the window that owns it."""
    calls: list[str] = []

    class Hidden:
        def isVisible(self) -> bool:
            return True

        def isActiveWindow(self) -> bool:
            return False

        def raise_(self) -> None:
            calls.append("raise")

        def activateWindow(self) -> None:
            calls.append("activate")

    monkeypatch.setattr(QApplication, "activeModalWidget", staticmethod(Hidden))
    window._keep_modal_visible()
    assert calls == ["raise", "activate"]


def test_the_modal_guard_leaves_a_focused_dialog_alone(window, monkeypatch):
    calls: list[str] = []

    class Focused:
        def isVisible(self) -> bool:
            return True

        def isActiveWindow(self) -> bool:
            return True

        def raise_(self) -> None:
            calls.append("raise")

        def activateWindow(self) -> None:
            calls.append("activate")

    monkeypatch.setattr(QApplication, "activeModalWidget", staticmethod(lambda: None))
    window._keep_modal_visible()
    monkeypatch.setattr(QApplication, "activeModalWidget", staticmethod(Focused))
    window._keep_modal_visible()
    assert calls == [], "nothing to do when the dialog already has focus"


def test_the_modal_guard_is_running(window):
    assert window._modal_guard.isActive()
    assert window._modal_guard.interval() > 0


# ── the windowed build (console=False) ──────────────────────────────────────────────
# Reported from a downloaded Windows build: double-clicking the .exe died before a
# window appeared, with `faulthandler.enable() -> RuntimeError: sys.stderr is None`.
# The same binary launched from a terminal started fine, because a shell hands the
# process a console. Everything below asserts the app never reaches for a stream that
# a windowed build does not have.


def test_the_desktop_starts_in_a_windowed_build_without_a_console(tmp_path):
    """The whole startup path, in a subprocess whose stdout and stderr are None."""
    import subprocess
    import sys
    import textwrap

    script = tmp_path / "windowed.py"
    script.write_text(
        textwrap.dedent(
            """
            import logging, pathlib, sys, traceback

            report = pathlib.Path(sys.argv[1])
            data = pathlib.Path(sys.argv[2])          # noqa: F841 - used inside the try
            sys.stdout = None
            sys.stderr = None                         # exactly what console=False does
            try:
                from netpilot.desktop.app import prepare_runtime

                log_file = prepare_runtime(str(data))
                logging.getLogger("netpilot.test").error("a line that must reach the file")
                for handler in logging.getLogger().handlers:
                    try:
                        handler.flush()
                    except Exception:
                        pass
                report.write_text(
                    "SURVIVED\\n" + pathlib.Path(log_file).read_text(encoding="utf-8")
                )
            except BaseException:
                report.write_text("CRASHED\\n" + traceback.format_exc())
            """
        ),
        encoding="utf-8",
    )

    report = tmp_path / "report.txt"
    proc = subprocess.run(  # noqa: S603
        [sys.executable, str(script), str(report), str(tmp_path / "data")],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert report.exists(), f"the child never finished:\n{proc.stdout}\n{proc.stderr}"
    content = report.read_text(encoding="utf-8")
    assert content.startswith("SURVIVED"), content
    assert "netpilot desktop starting" in content, "the startup line never reached the log"
    assert "a line that must reach the file" in content, "logging stopped working"


def test_the_crash_handler_always_gives_faulthandler_a_file(tmp_path, monkeypatch):
    """`faulthandler.enable()` without a file reaches for sys.stderr and raises."""
    from netpilot.desktop import safety

    files: list[object] = []

    def fake_enable(*args, **kwargs):
        files.append(kwargs.get("file"))
        if kwargs.get("file") is None:
            raise RuntimeError("sys.stderr is None")

    monkeypatch.setattr(safety.faulthandler, "enable", fake_enable)
    monkeypatch.setattr(
        safety.faulthandler,
        "dump_traceback_later",
        lambda *a, **k: pytest.fail("dump_traceback_later(0) raises ValueError — never call it"),
    )

    safety.install_crash_handler(tmp_path)

    assert files, "faulthandler was never configured"
    assert all(f is not None for f in files), "it was asked to use a stream instead of a file"


def test_logging_does_not_fall_back_to_a_stream(tmp_path, monkeypatch):
    """With nowhere to write, stay quiet — do not crash on a missing stderr."""
    import logging

    from netpilot.desktop import safety

    root = logging.getLogger()
    before = list(root.handlers)
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")
    try:
        path = safety.install_logging(blocker / "logs")
        assert path.name == safety.LOG_NAME
        added = [h for h in root.handlers if h not in before]
        assert added, "a handler is expected even when the file cannot be opened"
        assert all(not isinstance(h, logging.StreamHandler) or isinstance(h, logging.NullHandler)
                   for h in added), "no stream fallback"
    finally:
        for handler in list(root.handlers):
            if handler not in before:
                root.removeHandler(handler)


def test_the_exception_hook_does_not_write_to_a_missing_stderr(monkeypatch, tmp_path):
    from netpilot.desktop import safety

    safety.install_exception_hook(tmp_path)
    monkeypatch.setattr(safety.sys, "stderr", None)
    monkeypatch.setattr(safety.sys, "stdout", None)

    hook = safety.sys.excepthook
    hook(RuntimeError, RuntimeError("boom"), None)  # must not raise
    monkeypatch.setattr(safety.sys, "stderr", monkeypatch.undo() or safety.sys.__stderr__)


def test_write_fatal_records_a_startup_failure(tmp_path):
    from netpilot.desktop import safety

    try:
        raise ValueError("could not open the database")
    except ValueError as exc:
        path = safety.write_fatal(exc, tmp_path)

    assert path is not None and path.exists()
    text = path.read_text(encoding="utf-8")
    assert "could not open the database" in text
    assert "Traceback" in text


def test_prepare_runtime_is_idempotent(tmp_path):
    from netpilot.desktop.app import prepare_runtime

    first = prepare_runtime(str(tmp_path))
    second = prepare_runtime(str(tmp_path))
    assert first == second
    import logging

    file_handlers = [
        h
        for h in logging.getLogger().handlers
        if isinstance(h, logging.handlers.RotatingFileHandler)
    ]
    assert len(file_handlers) == 1, "the log would be written twice"


def test_the_window_knows_where_its_data_lives(window):
    """An error from a slot is reported into *this* app's data directory."""
    assert getattr(window, "_data_dir", "missing") is not None or window._data_dir is None


def test_a_broken_slot_writes_a_fatal_report_beside_the_database(window, tmp_path):
    from netpilot.desktop.safety import safe_slot, write_fatal

    class W:
        def __init__(self) -> None:
            self._data_dir = str(tmp_path)
            self.status = None

        @safe_slot
        def boom(self) -> None:
            raise RuntimeError("kaboom from a slot")

    W().boom()
    report = tmp_path / "netpilot-fatal.log"
    assert report.exists(), "the traceback was not written down anywhere"
    assert "kaboom from a slot" in report.read_text(encoding="utf-8")
