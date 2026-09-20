"""The netpilot desktop application.

Same features as the web dashboard — inventory, live monitoring, template-driven bulk
deployment, backups, notifications — driven by the same :class:`netpilot.core.App`, so the
two UIs can never drift apart in behaviour.

Everything that touches the core goes through :class:`~netpilot.desktop.bridge.CoreThread`;
the GUI thread itself never performs network or database work.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QFont, QIcon, QPixmap, QPainter, QColor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..models import CHECK_KINDS
from .bridge import CoreThread, UiBridge
from .widgets import (
    COLORS,
    DARK_QSS,
    DeviceCard,
    EventRow,
    Sparkline,
    StatePill,
    StatCard,
    horizontal_rule,
    section_label,
    wrap_list,
)

REFRESH_MS = 15000


# ══════════════════════════════════════════════════════════════════════════════════════
# Small shared helpers
# ══════════════════════════════════════════════════════════════════════════════════════


def app_icon() -> QIcon:
    """Draw the mark in code — no asset files to ship or lose."""
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    centre = 32
    for radius, alpha in ((26, 90), (16, 160)):
        pen = painter.pen()
        pen.setColor(QColor(79, 209, 163, alpha))
        pen.setWidth(4)
        painter.setPen(pen)
        painter.drawEllipse(centre - radius, centre - radius, radius * 2, radius * 2)
    painter.setBrush(QColor(79, 209, 163))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawEllipse(centre - 7, centre - 7, 14, 14)
    painter.end()
    return QIcon(pixmap)


def as_dict(value: Any) -> Any:
    """Normalise core return values for the Qt renderers.

    ``App`` mixes dataclasses (``Event``, ``Job``) with plain dicts (``overview``,
    ``settings_snapshot``). UI code should not have to remember which is which.
    """
    return value.to_dict() if hasattr(value, "to_dict") else value


def as_dicts(values: Any) -> list[dict[str, Any]]:
    return [as_dict(value) for value in (values or [])]


def table(headers: list[str]) -> QTableWidget:
    widget = QTableWidget(0, len(headers))
    widget.setHorizontalHeaderLabels(headers)
    widget.verticalHeader().setVisible(False)
    widget.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    widget.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    widget.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    widget.setAlternatingRowColors(False)
    widget.horizontalHeader().setStretchLastSection(True)
    widget.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    widget.setShowGrid(False)
    return widget


def mono_item(text: str) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    font = QFont("monospace")
    font.setStyleHint(QFont.StyleHint.Monospace)
    font.setPointSize(9)
    item.setFont(font)
    return item


def fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 1:
        return f"{int(seconds * 1000)} ms"
    if seconds < 60:
        return f"{seconds:.1f} s"
    minutes = int(seconds // 60)
    return f"{minutes}m {int(seconds - minutes * 60)}s"


def fmt_bytes(size: int | None) -> str:
    if size is None:
        return "—"
    if size < 1024:
        return f"{size} B"
    if size < 1048576:
        return f"{size / 1024:.1f} KiB"
    return f"{size / 1048576:.2f} MiB"


def fmt_uptime(seconds: float | None) -> str:
    if not seconds:
        return "—"
    days = int(seconds // 86400)
    hours = int((seconds % 86400) // 3600)
    minutes = int((seconds % 3600) // 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


# ══════════════════════════════════════════════════════════════════════════════════════
# Device editor / credentials / template dialogs
# ══════════════════════════════════════════════════════════════════════════════════════


class DeviceEditDialog(QDialog):
    """Add or edit a device. Adding one immediately schedules its monitors."""

    def __init__(self, credentials: list[dict[str, Any]], vendors: list[dict[str, Any]], device: dict[str, Any] | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.device = device or {}
        self.setWindowTitle("Edit device" if device else "Add device")
        self.setMinimumWidth(520)

        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setSpacing(9)

        self.host = QLineEdit(self.device.get("host", ""))
        self.host.setPlaceholderText("10.0.0.1")
        if device:
            self.host.setReadOnly(True)
        form.addRow("Host / IP *", self.host)

        self.name = QLineEdit(self.device.get("name", ""))
        self.name.setPlaceholderText("core-router-1")
        form.addRow("Display name", self.name)

        self.vendor = QComboBox()
        for entry in vendors:
            suffix = "" if entry.get("supports_deploy") else "  (monitoring only)"
            self.vendor.addItem(f"{entry['label']}{suffix}", entry["name"])
        current = self.device.get("vendor", "mikrotik")
        index = self.vendor.findData(current)
        if index >= 0:
            self.vendor.setCurrentIndex(index)
        form.addRow("Vendor", self.vendor)

        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(int(self.device.get("ssh_port", 22) or 22))
        form.addRow("SSH port", self.port)

        self.credential = QComboBox()
        self.credential.addItem("— none —", None)
        for cred in credentials:
            self.credential.addItem(f"{cred['name']}  ({cred.get('username') or '—'})", cred["id"])
        index = self.credential.findData(self.device.get("credential_id"))
        if index >= 0:
            self.credential.setCurrentIndex(index)
        form.addRow("Credential", self.credential)

        self.site = QLineEdit(self.device.get("site", ""))
        form.addRow("Site", self.site)

        self.tags = QLineEdit(", ".join(self.device.get("tags", []) or []))
        self.tags.setPlaceholderText("core, datacenter, cisco")
        form.addRow("Tags", self.tags)

        self.mgmt_url = QLineEdit(self.device.get("mgmt_url") or "")
        self.mgmt_url.setPlaceholderText("https://10.0.0.1  (optional, monitored)")
        form.addRow("Management URL", self.mgmt_url)

        self.notes = QPlainTextEdit(self.device.get("notes", ""))
        self.notes.setMaximumHeight(64)
        form.addRow("Notes", self.notes)
        layout.addLayout(form)

        self.enabled = QCheckBox("Monitored")
        self.enabled.setChecked(self.device.get("enabled", True))
        layout.addWidget(self.enabled)

        if not device:
            self.autochecks = QCheckBox("Attach default monitors now (ping + SSH port)")
            self.autochecks.setChecked(True)
            layout.addWidget(self.autochecks)
            hint = QLabel(
                "The device starts being monitored the moment you save — no separate step."
            )
            hint.setObjectName("dim")
            hint.setWordWrap(True)
            layout.addWidget(hint)
        else:
            self.autochecks = None

        layout.addWidget(horizontal_rule())
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _validate_and_accept(self) -> None:
        if not self.host.text().strip():
            QMessageBox.warning(self, "Missing host", "A host or IP address is required.")
            return
        self.accept()

    def payload(self) -> dict[str, Any]:
        data = {
            "host": self.host.text().strip(),
            "name": self.name.text().strip() or self.host.text().strip(),
            "vendor": self.vendor.currentData(),
            "ssh_port": self.port.value(),
            "credential_id": self.credential.currentData(),
            "site": self.site.text().strip(),
            "tags": [t.strip() for t in self.tags.text().split(",") if t.strip()],
            "mgmt_url": self.mgmt_url.text().strip() or None,
            "notes": self.notes.toPlainText().strip(),
            "enabled": self.enabled.isChecked(),
        }
        if self.autochecks is not None:
            data["auto_checks"] = self.autochecks.isChecked()
        return data


class CredentialDialog(QDialog):
    """Manage the encrypted credential store."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Credentials")
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)

        note = QLabel(
            "Secrets are encrypted at rest (Fernet) with a key stored beside the database,\n"
            "or taken from the NETPILOT_KEY environment variable."
        )
        note.setObjectName("dim")
        layout.addWidget(note)

        self.list = QListWidget()
        self.list.setMaximumHeight(150)
        layout.addWidget(self.list)

        form = QFormLayout()
        self.name = QLineEdit()
        self.name.setPlaceholderText("core-switches")
        form.addRow("Name *", self.name)
        self.username = QLineEdit()
        form.addRow("Username", self.username)
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Password", self.password)
        self.enable_password = QLineEdit()
        self.enable_password.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Enable password (Cisco)", self.enable_password)
        self.key_path = QLineEdit()
        self.key_path.setPlaceholderText("/home/you/.ssh/id_ed25519")
        form.addRow("SSH private key path", self.key_path)
        self.community = QLineEdit()
        self.community.setPlaceholderText("public")
        form.addRow("SNMP community", self.community)
        self.snmp_version = QComboBox()
        self.snmp_version.addItems(["1", "2c", "3"])
        self.snmp_version.setCurrentText("2c")
        form.addRow("SNMP version", self.snmp_version)
        layout.addLayout(form)

        row = QHBoxLayout()
        self.add_button = QPushButton("Add credential")
        self.add_button.setObjectName("primary")
        self.add_button.clicked.connect(self._add)
        row.addWidget(self.add_button)
        self.delete_button = QPushButton("Delete selected")
        self.delete_button.setObjectName("danger")
        self.delete_button.clicked.connect(self._delete)
        row.addWidget(self.delete_button)
        row.addStretch(1)
        layout.addLayout(row)

        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject)
        close.accepted.connect(self.accept)
        layout.addWidget(close)

        self.created = False

    def set_credentials(self, credentials: list[dict[str, Any]]) -> None:
        self.list.clear()
        for cred in credentials:
            item = QListWidgetItem(f"{cred['name']}  ·  {cred.get('username') or '—'}")
            item.setData(Qt.ItemDataRole.UserRole, cred["id"])
            self.list.addItem(item)

    def selected_id(self) -> int | None:
        item = self.list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _add(self) -> None:
        self.add_requested()
        self.created = True

    def _delete(self) -> None:
        cred_id = self.selected_id()
        if cred_id is None:
            QMessageBox.information(self, "Nothing selected", "Select a credential to delete.")
            return
        if QMessageBox.question(self, "Delete credential", "Devices using it will lose their login. Continue?") != QMessageBox.StandardButton.Yes:
            return
        self.delete_requested(cred_id)

    #: Set by the main window.
    add_requested: Callable[[], None] = staticmethod(lambda: None)
    delete_requested: Callable[[int], None] = staticmethod(lambda _id: None)

    def payload(self) -> dict[str, Any]:
        return {
            "name": self.name.text().strip(),
            "username": self.username.text().strip(),
            "password": self.password.text() or None,
            "enable_password": self.enable_password.text() or None,
            "key_path": self.key_path.text().strip() or None,
            "snmp_community": self.community.text().strip() or None,
            "snmp_version": self.snmp_version.currentText(),
        }


class TemplateEditDialog(QDialog):
    """Create or edit a configuration template with live rendering."""

    def __init__(self, vendors: list[dict[str, Any]], template: dict[str, Any] | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit template" if template else "New template")
        self.resize(720, 640)
        self._preview_cb: Callable[[dict[str, Any]], None] | None = None

        layout = QVBoxLayout(self)

        top = QFormLayout()
        self.name = QLineEdit((template or {}).get("name", ""))
        top.addRow("Name *", self.name)
        self.vendor = QComboBox()
        for entry in vendors:
            if entry.get("supports_deploy"):
                self.vendor.addItem(entry["label"], entry["name"])
        index = self.vendor.findData((template or {}).get("vendor", "mikrotik"))
        if index >= 0:
            self.vendor.setCurrentIndex(index)
        top.addRow("Vendor", self.vendor)
        self.description = QLineEdit((template or {}).get("description", ""))
        top.addRow("Description", self.description)
        layout.addLayout(top)

        layout.addWidget(section_label("Configuration body"))
        self.body = QPlainTextEdit((template or {}).get("body", ""))
        self.body.setPlaceholderText(
            "One command per line. Use {{ variable }} for values that change per site.\n"
            "# lines starting with # are skipped."
        )
        layout.addWidget(self.body, 1)

        layout.addWidget(section_label("Variables (key=value, one per line)"))
        existing = (template or {}).get("variables") or {}
        self.variables = QPlainTextEdit("\n".join(f"{k}={v}" for k, v in existing.items()))
        self.variables.setMaximumHeight(90)
        layout.addWidget(self.variables)

        self.save_config = QCheckBox("Persist the configuration on the device after applying (write memory)")
        self.save_config.setChecked((template or {}).get("save_config", True))
        layout.addWidget(self.save_config)

        layout.addWidget(horizontal_rule())
        layout.addWidget(section_label("Preview"))
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setMaximumHeight(150)
        self.preview.setPlaceholderText("Click Preview to render the commands for a device.")
        layout.addWidget(self.preview)

        row = QHBoxLayout()
        preview_button = QPushButton("Preview")
        preview_button.clicked.connect(self._preview)
        row.addWidget(preview_button)
        row.addStretch(1)
        layout.addLayout(row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.body.textChanged.connect(self._schedule_preview)

    def on_preview(self, callback: Callable[[dict[str, Any]], None]) -> None:
        self._preview_cb = callback

    def set_preview(self, result: dict[str, Any]) -> None:
        commands = result.get("commands") or []
        unresolved = result.get("unresolved") or []
        text = "\n".join(f"{i + 1:>3}  {c}" for i, c in enumerate(commands)) or "(no commands)"
        if unresolved:
            text += f"\n\nUNRESOLVED: {', '.join(unresolved)}"
        text += f"\n\n{len(commands)} command(s) · rollback: {result.get('rollback_support', 'manual')}"
        self.preview.setPlainText(text)

    def _schedule_preview(self) -> None:
        if not hasattr(self, "_timer"):
            self._timer = QTimer(self)
            self._timer.setSingleShot(True)
            self._timer.timeout.connect(self._preview)
        self._timer.start(400)

    def _preview(self) -> None:
        if self._preview_cb is not None:
            self._preview_cb(self.payload())

    def _validate_and_accept(self) -> None:
        if not self.name.text().strip():
            QMessageBox.warning(self, "Missing name", "A template name is required.")
            return
        if not self.body.toPlainText().strip():
            QMessageBox.warning(self, "Empty body", "The template has no commands.")
            return
        self.accept()

    def payload(self) -> dict[str, Any]:
        variables: dict[str, str] = {}
        for line in self.variables.toPlainText().split("\n"):
            if "=" in line:
                key, _, value = line.partition("=")
                key = key.strip()
                if key:
                    variables[key] = value.strip()
        return {
            "name": self.name.text().strip(),
            "vendor": self.vendor.currentData(),
            "description": self.description.text().strip(),
            "body": self.body.toPlainText(),
            "variables": variables,
            "save_config": self.save_config.isChecked(),
        }


class CheckEditDialog(QDialog):
    """Add or edit one monitoring probe."""

    def __init__(self, existing: dict[str, Any] | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit monitor" if existing else "Add monitor")
        self.setMinimumWidth(460)
        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.kind = QComboBox()
        self.kind.addItems(list(CHECK_KINDS))
        if existing:
            self.kind.setCurrentText(existing.get("kind", "icmp"))
        form.addRow("Kind", self.kind)

        self.label = QLineEdit((existing or {}).get("label", ""))
        self.label.setPlaceholderText("Ping")
        form.addRow("Label", self.label)

        self.interval = QSpinBox()
        self.interval.setRange(5, 86400)
        self.interval.setValue(int((existing or {}).get("interval_sec", 60)))
        form.addRow("Interval (seconds)", self.interval)

        self.timeout = QDoubleSpinBox()
        self.timeout.setRange(0.5, 120.0)
        self.timeout.setSingleStep(0.5)
        self.timeout.setValue(float((existing or {}).get("timeout_sec", 5.0)))
        form.addRow("Timeout (seconds)", self.timeout)

        self.failures = QSpinBox()
        self.failures.setRange(1, 20)
        self.failures.setValue(int((existing or {}).get("failures_to_down", 2)))
        form.addRow("Failures before down", self.failures)

        self.successes = QSpinBox()
        self.successes.setRange(1, 20)
        self.successes.setValue(int((existing or {}).get("successes_to_up", 1)))
        form.addRow("Successes before up", self.successes)

        import json as _json

        self.params = QPlainTextEdit(_json.dumps((existing or {}).get("params") or {"count": 3}))
        self.params.setMaximumHeight(80)
        form.addRow("Parameters (JSON)", self.params)
        layout.addLayout(form)

        examples = QLabel(
            'Examples —  {"count": 3}   ·   {"port": 443}   ·   '
            '{"url": "https://10.0.0.1/status"}   ·   {"oids": "1.3.6.1.2.1.1.3.0"}'
        )
        examples.setObjectName("dim")
        examples.setWordWrap(True)
        layout.addWidget(examples)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _validate_and_accept(self) -> None:
        import json as _json

        try:
            _json.loads(self.params.toPlainText() or "{}")
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid JSON", f"Parameters are not valid JSON:\n{exc}")
            return
        self.accept()

    def payload(self, device_id: int) -> dict[str, Any]:
        import json as _json

        return {
            "device_id": device_id,
            "kind": self.kind.currentText(),
            "label": self.label.text().strip(),
            "interval_sec": self.interval.value(),
            "timeout_sec": self.timeout.value(),
            "failures_to_down": self.failures.value(),
            "successes_to_up": self.successes.value(),
            "params": _json.loads(self.params.toPlainText() or "{}"),
        }


# ══════════════════════════════════════════════════════════════════════════════════════
# Device detail dialog
# ══════════════════════════════════════════════════════════════════════════════════════


class DeviceDetailDialog(QDialog):
    """Everything about one device: state, checks, history, backups, and a deploy box."""

    action = pyqtSignal(str, int)

    def __init__(self, device: dict[str, Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.device = device
        self.setWindowTitle(device.get("name", "Device"))
        self.resize(860, 720)

        layout = QVBoxLayout(self)
        state = device.get("state") or {}

        header = QHBoxLayout()
        title = QLabel(device.get("name", ""))
        title.setObjectName("h1")
        header.addWidget(title)
        header.addWidget(StatePill(state.get("state", "unknown")))
        header.addStretch(1)
        subtitle = QLabel(f"{device.get('host')}:{device.get('ssh_port')}  ·  {device.get('vendor')}")
        subtitle.setObjectName("mono")
        header.addWidget(subtitle)
        layout.addLayout(header)

        actions = QHBoxLayout()
        for label, key, primary in (
            ("Test SSH connection", "test", True),
            ("Run all checks", "probe", False),
            ("Capture config", "backup", False),
            ("Deploy template…", "deploy", False),
            ("Edit", "edit", False),
            ("Delete", "delete", False),
        ):
            button = QPushButton(label)
            if primary:
                button.setObjectName("primary")
            if key == "delete":
                button.setObjectName("danger")
            button.clicked.connect(lambda _checked=False, k=key: self.action.emit(k, int(device["id"])))
            actions.addWidget(button)
        actions.addStretch(1)
        layout.addLayout(actions)

        tabs = QTabWidget()
        layout.addWidget(tabs, 1)

        # -- overview tab -------------------------------------------------------------
        overview = QWidget()
        overview_layout = QVBoxLayout(overview)
        grid = QGridLayout()
        rows = [
            ("State", f"{state.get('state', 'unknown')} since {time.strftime('%Y-%m-%d %H:%M', time.localtime(state.get('since', time.time())))}"),
            ("Last check", time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(state["last_check_ts"])) if state.get("last_check_ts") else "never"),
            ("Latency", f"{state['last_latency_ms']:.1f} ms" if state.get("last_latency_ms") is not None else "—"),
            ("Checks run", f"{state.get('total_checks', 0)} total, {state.get('up_checks', 0)} ok"),
            ("Availability", f"24h {device.get('availability_24h') or '—'}%  ·  7d {device.get('availability_7d') or '—'}%"),
            ("Last error", (state.get("last_error") or "—")[:120]),
            ("Tags", ", ".join(device.get("tags") or []) or "—"),
            ("Site", device.get("site") or "—"),
            ("Notes", (device.get("notes") or "—")[:120]),
        ]
        for index, (key, value) in enumerate(rows):
            key_label = QLabel(key)
            key_label.setObjectName("dim")
            grid.addWidget(key_label, index, 0)
            value_label = QLabel(str(value))
            value_label.setWordWrap(True)
            grid.addWidget(value_label, index, 1)
        grid.setColumnStretch(1, 1)
        overview_layout.addLayout(grid)

        overview_layout.addWidget(section_label("Latency & availability"))
        self.spark = Sparkline(height=70)
        self.spark.set_points(device.get("history") or [])
        overview_layout.addWidget(self.spark)
        overview_layout.addStretch(1)
        tabs.addTab(overview, "Overview")

        # -- monitors tab -------------------------------------------------------------
        monitors = QWidget()
        monitors_layout = QVBoxLayout(monitors)
        self.check_table = table(["Kind", "Label", "Params", "Every", "State", "Last run"])
        self.check_table.setMaximumHeight(200)
        monitors_layout.addWidget(self.check_table)

        monitor_buttons = QHBoxLayout()
        add_check = QPushButton("Add monitor")
        add_check.clicked.connect(lambda: self.action.emit("check-add", int(device["id"])))
        monitor_buttons.addWidget(add_check)
        edit_check = QPushButton("Edit selected")
        edit_check.clicked.connect(self._edit_check)
        monitor_buttons.addWidget(edit_check)
        del_check = QPushButton("Remove selected")
        del_check.setObjectName("danger")
        del_check.clicked.connect(self._delete_check)
        monitor_buttons.addWidget(del_check)
        monitor_buttons.addStretch(1)
        monitors_layout.addLayout(monitor_buttons)

        monitors_layout.addWidget(section_label("Recent probe results"))
        self.result_table = table(["Time", "Kind", "Result", "Latency", "Detail"])
        monitors_layout.addWidget(self.result_table, 1)
        tabs.addTab(monitors, "Monitors")

        # -- deploy tab ---------------------------------------------------------------
        deploy = QWidget()
        deploy_layout = QVBoxLayout(deploy)
        pick = QHBoxLayout()
        pick.addWidget(QLabel("Template"))
        self.template_combo = QComboBox()
        for template in device.get("templates") or []:
            self.template_combo.addItem(f"{template['name']}  ·  {template['vendor']}", template)
        pick.addWidget(self.template_combo, 1)
        deploy_layout.addLayout(pick)

        self.variables_box = QWidget()
        self.variables_layout = QFormLayout(self.variables_box)
        self.variables_layout.setContentsMargins(0, 0, 0, 0)
        self.var_inputs: dict[str, QLineEdit] = {}
        deploy_layout.addWidget(self.variables_box)

        self.deploy_preview = QPlainTextEdit()
        self.deploy_preview.setReadOnly(True)
        deploy_layout.addWidget(self.deploy_preview, 1)

        deploy_options = QHBoxLayout()
        self.dry_run = QCheckBox("Dry run (change nothing)")
        self.dry_run.setChecked(True)
        deploy_options.addWidget(self.dry_run)
        deploy_options.addStretch(1)
        run = QPushButton("Preview & run on this device")
        run.setObjectName("primary")
        run.clicked.connect(lambda: self.action.emit("deploy-one" + ("-real" if not self.dry_run.isChecked() else ""), int(device["id"])))
        deploy_options.addWidget(run)
        deploy_layout.addLayout(deploy_options)

        self.template_combo.currentIndexChanged.connect(lambda _i: self._load_template_vars())
        self._load_template_vars()
        tabs.addTab(deploy, "Deploy")

        # -- backups tab --------------------------------------------------------------
        backups_tab = QWidget()
        backups_layout = QVBoxLayout(backups_tab)
        self.backup_table = table(["#", "When", "Size", "Source"])
        backups_layout.addWidget(self.backup_table)
        backup_row = QHBoxLayout()
        view = QPushButton("View selected")
        view.clicked.connect(self._view_backup)
        backup_row.addWidget(view)
        restore = QPushButton("Restore selected…")
        restore.setObjectName("danger")
        restore.clicked.connect(self._restore_backup)
        backup_row.addWidget(restore)
        backup_row.addStretch(1)
        backups_layout.addLayout(backup_row)
        tabs.addTab(backups_tab, "Backups")

        # -- events tab ---------------------------------------------------------------
        events_tab = QWidget()
        events_layout = QVBoxLayout(events_tab)
        events_layout.addWidget(QLabel(f"Recent events for {device.get('name')}"))
        self.events_list = wrap_list(QListWidget())
        events_layout.addWidget(self.events_list)
        tabs.addTab(events_tab, "Events")

        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject)
        layout.addWidget(close)

        self.populate(device)

    # -- population ------------------------------------------------------------------

    def populate(self, device: dict[str, Any]) -> None:
        self.device = device
        states = device.get("check_states") or {}

        self.check_table.setRowCount(0)
        for check in device.get("checks") or []:
            row = self.check_table.rowCount()
            self.check_table.insertRow(row)
            self.check_table.setItem(row, 0, QTableWidgetItem(check.get("kind", "")))
            self.check_table.setItem(row, 1, QTableWidgetItem(check.get("label", "")))
            self.check_table.setItem(row, 2, mono_item(str(check.get("params") or {})))
            self.check_table.setItem(row, 3, mono_item(f"{check.get('interval_sec')}s"))
            check_state = states.get(check.get("id")) or {}
            self.check_table.setItem(row, 4, QTableWidgetItem(check_state.get("state") or "unknown"))
            last = check_state.get("last_ts")
            self.check_table.setItem(
                row, 5, mono_item(time.strftime("%H:%M:%S", time.localtime(last)) if last else "never")
            )
            self.check_table.item(row, 0).setData(Qt.ItemDataRole.UserRole, check.get("id"))
        self.check_table.resizeColumnsToContents()

        self.result_table.setRowCount(0)
        for result in (device.get("results") or [])[:40]:
            row = self.result_table.rowCount()
            self.result_table.insertRow(row)
            self.result_table.setItem(row, 0, mono_item(time.strftime("%H:%M:%S", time.localtime(result["ts"]))))
            self.result_table.setItem(row, 1, QTableWidgetItem(result.get("kind", "")))
            ok_item = QTableWidgetItem("ok" if result.get("ok") else "fail")
            ok_item.setForeground(
                Qt.GlobalColor.green if result.get("ok") else Qt.GlobalColor.red  # type: ignore[attr-defined]
            )
            self.result_table.setItem(row, 2, ok_item)
            latency = result.get("latency_ms")
            self.result_table.setItem(row, 3, mono_item(f"{latency:.1f} ms" if latency is not None else "—"))
            self.result_table.setItem(row, 4, QTableWidgetItem((result.get("message") or "")[:110]))
        self.result_table.resizeColumnsToContents()

        self.backup_table.setRowCount(0)
        for backup in device.get("backups") or []:
            row = self.backup_table.rowCount()
            self.backup_table.insertRow(row)
            self.backup_table.setItem(row, 0, mono_item(f"#{backup['id']}"))
            self.backup_table.setItem(row, 1, mono_item(time.strftime("%Y-%m-%d %H:%M", time.localtime(backup["created_at"]))))
            self.backup_table.setItem(row, 2, mono_item(fmt_bytes(backup.get("byte_size"))))
            self.backup_table.setItem(row, 3, mono_item(backup.get("source", "")))
            self.backup_table.item(row, 0).setData(Qt.ItemDataRole.UserRole, backup["id"])
        self.backup_table.resizeColumnsToContents()

        self.events_list.clear()
        for event in device.get("events") or []:
            item = QListWidgetItem(f"[{event.get('severity')}] {event.get('message')}")
            item.setData(Qt.ItemDataRole.UserRole, event)
            self.events_list.addItem(item)

        self.spark.set_points(device.get("history") or [])

    def template_payload(self) -> dict[str, Any]:
        template = self.template_combo.currentData() or {}
        variables = {key: field.text() for key, field in self.var_inputs.items()}
        return {
            "template_id": template.get("id"),
            "body": template.get("body", ""),
            "vendor": template.get("vendor", self.device.get("vendor")),
            "variables": variables,
        }

    def set_deploy_preview(self, result: dict[str, Any]) -> None:
        commands = result.get("commands") or []
        text = "\n".join(f"{i + 1:>3}  {c}" for i, c in enumerate(commands)) or "(no commands)"
        unresolved = result.get("unresolved") or []
        if unresolved:
            text += f"\n\nUNRESOLVED: {', '.join(unresolved)}"
        self.deploy_preview.setPlainText(text)

    def _load_template_vars(self) -> None:
        while self.variables_layout.count():
            item = self.variables_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.var_inputs.clear()
        template = self.template_combo.currentData() or {}
        for key, value in (template.get("variables") or {}).items():
            field = QLineEdit(str(value))
            field.textChanged.connect(lambda _t: self._schedule_preview())
            self.var_inputs[key] = field
            self.variables_layout.addRow(key, field)
        self._schedule_preview()

    def _schedule_preview(self) -> None:
        if not hasattr(self, "_preview_timer"):
            self._preview_timer = QTimer(self)
            self._preview_timer.setSingleShot(True)
            self._preview_timer.timeout.connect(lambda: self.action.emit("preview", int(self.device["id"])))
        self._preview_timer.start(350)

    # -- selection helpers -----------------------------------------------------------

    def selected_check_id(self) -> int | None:
        row = self.check_table.currentRow()
        if row < 0:
            return None
        item = self.check_table.item(row, 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def selected_backup_id(self) -> int | None:
        row = self.backup_table.currentRow()
        if row < 0:
            return None
        item = self.backup_table.item(row, 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _edit_check(self) -> None:
        check_id = self.selected_check_id()
        if check_id is None:
            QMessageBox.information(self, "Nothing selected", "Select a monitor first.")
            return
        self.action.emit("check-edit", check_id)

    def _delete_check(self) -> None:
        check_id = self.selected_check_id()
        if check_id is None:
            QMessageBox.information(self, "Nothing selected", "Select a monitor first.")
            return
        self.action.emit("check-delete", check_id)

    def _view_backup(self) -> None:
        backup_id = self.selected_backup_id()
        if backup_id is None:
            QMessageBox.information(self, "Nothing selected", "Select a backup first.")
            return
        self.action.emit("backup-view", backup_id)

    def _restore_backup(self) -> None:
        backup_id = self.selected_backup_id()
        if backup_id is None:
            QMessageBox.information(self, "Nothing selected", "Select a backup first.")
            return
        self.action.emit("backup-restore", backup_id)


# ══════════════════════════════════════════════════════════════════════════════════════
# Main window
# ══════════════════════════════════════════════════════════════════════════════════════


class MainWindow(QMainWindow):
    """The application shell: sidebar, page stack, status bar, live updates."""

    def __init__(self, core: CoreThread, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.core = core
        self.bridge = UiBridge(core, self)
        self.setWindowTitle(f"netpilot {__version__}")
        self.resize(1320, 860)

        self.overview: dict[str, Any] = {}
        self.devices: list[dict[str, Any]] = []
        self.templates: list[dict[str, Any]] = []
        self.credentials: list[dict[str, Any]] = []
        self.vendors: list[dict[str, Any]] = []
        self.cards: dict[int, DeviceCard] = {}
        self.detail_dialog: DeviceDetailDialog | None = None
        self.tag_filter: str = ""
        self.deploy_selection: set[int] = set()

        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_sidebar())

        self.pages = QStackedWidget()
        root.addWidget(self.pages, 1)
        self.pages.addWidget(self._build_dashboard())
        self.pages.addWidget(self._build_devices())
        self.pages.addWidget(self._build_deploy())
        self.pages.addWidget(self._build_templates())
        self.pages.addWidget(self._build_backups())
        self.pages.addWidget(self._build_events())
        self.pages.addWidget(self._build_settings())

        self.status = self.statusBar()
        self.status.showMessage("starting…")

        core.event_received.connect(self.on_event)
        core.job_updated.connect(self.on_job)
        core.result_received.connect(self.on_result)

        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self.refresh_all)
        self._refresh_interval = REFRESH_MS
        self.refresh_timer.start(REFRESH_MS)

        QTimer.singleShot(300, self.refresh_all)
        QTimer.singleShot(450, self.load_meta)
        QTimer.singleShot(700, self.load_templates)
        QTimer.singleShot(900, self.load_jobs)

    # ── chrome ───────────────────────────────────────────────────────────────────────

    def _build_sidebar(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("sidebar")
        panel.setFixedWidth(216)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 16, 0, 12)
        layout.setSpacing(0)

        brand = QHBoxLayout()
        brand.setContentsMargins(16, 0, 16, 14)
        logo = QLabel()
        logo.setPixmap(app_icon().pixmap(28, 28))
        brand.addWidget(logo)
        names = QVBoxLayout()
        names.setSpacing(0)
        title = QLabel("netpilot")
        title.setObjectName("brand")
        names.addWidget(title)
        self.version_label = QLabel(f"v{__version__}")
        self.version_label.setObjectName("dim")
        self.version_label.setStyleSheet(f"color: {COLORS['fg_dimmer']}; font-size: 10px;")
        names.addWidget(self.version_label)
        brand.addLayout(names)
        brand.addStretch(1)
        layout.addLayout(brand)

        self.nav_buttons: list[QPushButton] = []
        pages = [
            ("Dashboard", 0),
            ("Devices", 1),
            ("Deploy config", 2),
            ("Templates", 3),
            ("Backups", 4),
            ("Events", 5),
            ("Settings", 6),
        ]
        for label, index in pages:
            button = QPushButton(label)
            button.setObjectName("navButton")
            button.setCheckable(True)
            button.setAutoExclusive(True)
            button.clicked.connect(lambda _checked=False, i=index: self.show_page(i))
            layout.addWidget(button)
            self.nav_buttons.append(button)
        self.nav_buttons[0].setChecked(True)

        layout.addStretch(1)
        self.live_label = QLabel("● live")
        self.live_label.setStyleSheet(f"color: {COLORS['up']}; font-size: 11px; padding: 0 16px;")
        layout.addWidget(self.live_label)
        self.core_label = QLabel("—")
        self.core_label.setObjectName("dim")
        self.core_label.setStyleSheet(f"color: {COLORS['fg_dimmer']}; font-size: 10px; padding: 4px 16px 0;")
        self.core_label.setWordWrap(True)
        layout.addWidget(self.core_label)
        return panel

    def show_page(self, index: int) -> None:
        self.pages.setCurrentIndex(index)
        if index in (0, 1, 2):
            self.refresh_all()
        elif index == 3:
            self.load_templates()
        elif index == 4:
            self.load_backups()
        elif index == 5:
            self.load_events()
        elif index == 6:
            self.load_settings()

    def _page(self, title: str, subtitle: str) -> tuple[QWidget, QVBoxLayout]:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(10)
        header = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(1)
        heading = QLabel(title)
        heading.setObjectName("h1")
        titles.addWidget(heading)
        sub = QLabel(subtitle)
        sub.setObjectName("sub")
        titles.addWidget(sub)
        header.addLayout(titles)
        header.addStretch(1)
        layout.addLayout(header)
        page.setProperty("_header", header)
        return page, layout

    def _header_layout(self, page: QWidget) -> QHBoxLayout:
        return page.property("_header")

    # ── dashboard page ───────────────────────────────────────────────────────────────

    def _build_dashboard(self) -> QWidget:
        page, layout = self._page(
            "Operations dashboard", "live state of every device in the inventory"
        )
        header = self._header_layout(page)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh_all)
        header.addWidget(refresh)
        add = QPushButton("+ Add device")
        add.setObjectName("primary")
        add.clicked.connect(self.add_device)
        header.addWidget(add)

        self.stat_cards: dict[str, StatCard] = {}
        stats_row = QHBoxLayout()
        stats_row.setSpacing(10)
        for key in ("Devices", "Up", "Down", "Degraded", "Avg latency", "Checks / 24h", "Backups", "Open alerts"):
            card = StatCard(key, "—")
            self.stat_cards[key] = card
            stats_row.addWidget(card)
        layout.addLayout(stats_row)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(section_label("Devices"))

        filters = QHBoxLayout()
        self.dash_search = QLineEdit()
        self.dash_search.setPlaceholderText("Filter by name, host or site…")
        self.dash_search.textChanged.connect(self.render_device_grid)
        filters.addWidget(self.dash_search)
        self.tag_row = QWidget()
        self.tag_layout = QHBoxLayout(self.tag_row)
        self.tag_layout.setContentsMargins(0, 0, 0, 0)
        self.tag_layout.setSpacing(4)
        filters.addWidget(self.tag_row)
        left_layout.addLayout(filters)

        self.device_scroll = QScrollArea()
        self.device_scroll.setWidgetResizable(True)
        self.device_container = QWidget()
        self.device_grid = QGridLayout(self.device_container)
        self.device_grid.setSpacing(10)
        self.device_grid.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.device_scroll.setWidget(self.device_container)
        left_layout.addWidget(self.device_scroll, 1)
        splitter.addWidget(left)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        events_head = QHBoxLayout()
        events_head.addWidget(section_label("Event feed"))
        events_head.addStretch(1)
        ack_all = QPushButton("Acknowledge all")
        ack_all.clicked.connect(self.ack_all_events)
        events_head.addWidget(ack_all)
        right_layout.addLayout(events_head)

        self.dash_events = wrap_list(QListWidget())
        right_layout.addWidget(self.dash_events, 2)

        right_layout.addWidget(section_label("Recent deployments"))
        self.dash_jobs = wrap_list(QListWidget())
        right_layout.addWidget(self.dash_jobs, 1)
        splitter.addWidget(right)
        splitter.setSizes([880, 420])
        layout.addWidget(splitter, 1)
        return page

    def render_device_grid(self) -> None:
        while self.device_grid.count():
            item = self.device_grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.cards.clear()

        needle = self.dash_search.text().lower()
        visible = [
            card
            for card in self.devices
            if (not self.tag_filter or self.tag_filter in [t.lower() for t in card.get("tags") or []])
            and (
                not needle
                or needle in (card.get("name") or "").lower()
                or needle in (card.get("host") or "").lower()
                or needle in (card.get("site") or "").lower()
            )
        ]

        if not visible:
            message = QLabel(
                "No devices match this filter."
                if self.devices
                else "No devices yet — add one and it starts being monitored immediately."
            )
            message.setObjectName("dim")
            self.device_grid.addWidget(message, 0, 0)
            return

        columns = max(1, min(4, self.device_scroll.width() // 290))
        for index, card in enumerate(visible):
            widget = DeviceCard(card)
            widget.clicked.connect(self.open_device)
            self.cards[int(card["id"])] = widget
            self.device_grid.addWidget(widget, index // columns, index % columns)

    def render_tags(self) -> None:
        while self.tag_layout.count():
            item = self.tag_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        tags = (self.overview.get("tags") or [])[:12]
        for tag in [""] + list(tags):
            button = QPushButton("all tags" if not tag else tag)
            button.setCheckable(True)
            button.setChecked(self.tag_filter == tag)
            button.setMaximumHeight(24)
            button.setStyleSheet("padding: 3px 10px; font-size: 11px; border-radius: 12px;")
            button.clicked.connect(lambda _checked=False, t=tag: self.set_tag_filter(t))
            self.tag_layout.addWidget(button)
        self.tag_layout.addStretch(1)

    def set_tag_filter(self, tag: str) -> None:
        self.tag_filter = tag
        self.render_tags()
        self.render_device_grid()

    # ── devices page ─────────────────────────────────────────────────────────────────

    def _build_devices(self) -> QWidget:
        page, layout = self._page(
            "Devices", "adding a device starts monitoring it immediately — and makes it a deploy target"
        )
        header = self._header_layout(page)
        creds = QPushButton("Credentials")
        creds.clicked.connect(self.manage_credentials)
        header.addWidget(creds)
        add = QPushButton("+ Add device")
        add.setObjectName("primary")
        add.clicked.connect(self.add_device)
        header.addWidget(add)

        self.device_table = table(
            ["Name", "Host", "Vendor", "Site", "State", "Latency", "24h", "Tags", "Actions"]
        )
        self.device_table.doubleClicked.connect(self._table_row_activated)
        layout.addWidget(self.device_table, 1)
        return page

    def render_device_table(self) -> None:
        self.device_table.setRowCount(0)
        for card in self.devices:
            row = self.device_table.rowCount()
            self.device_table.insertRow(row)
            self.device_table.setItem(row, 0, QTableWidgetItem(card.get("name", "")))
            self.device_table.setItem(row, 1, mono_item(card.get("host", "")))
            self.device_table.setItem(row, 2, QTableWidgetItem(card.get("vendor", "")))
            self.device_table.setItem(row, 3, QTableWidgetItem(card.get("site") or "—"))
            state = (card.get("state") or {}).get("state", "unknown")
            state_item = QTableWidgetItem(state)
            state_item.setForeground(
                {
                    "up": Qt.GlobalColor.green,  # type: ignore[attr-defined]
                    "down": Qt.GlobalColor.red,  # type: ignore[attr-defined]
                    "degraded": Qt.GlobalColor.yellow,  # type: ignore[attr-defined]
                }.get(state, Qt.GlobalColor.gray)  # type: ignore[attr-defined]
            )
            self.device_table.setItem(row, 4, state_item)
            latency = (card.get("state") or {}).get("last_latency_ms")
            self.device_table.setItem(row, 5, mono_item(f"{latency:.1f} ms" if latency is not None else "—"))
            availability = card.get("availability_24h")
            self.device_table.setItem(row, 6, mono_item(f"{availability}%" if availability is not None else "—"))
            self.device_table.setItem(row, 7, QTableWidgetItem(", ".join(card.get("tags") or [])))
            self.device_table.setItem(row, 8, QTableWidgetItem("double-click to open"))
            self.device_table.item(row, 0).setData(Qt.ItemDataRole.UserRole, card["id"])
        self.device_table.resizeColumnsToContents()

    def _table_row_activated(self, index: Any) -> None:
        row = index.row()
        item = self.device_table.item(row, 0)
        if item is not None:
            self.open_device(item.data(Qt.ItemDataRole.UserRole))

    # ── deploy page ──────────────────────────────────────────────────────────────────

    def _build_deploy(self) -> QWidget:
        page, layout = self._page(
            "Deploy configuration",
            "pick a template, choose targets by tag or individually, preview, then push",
        )
        header = self._header_layout(page)
        dry_note = QLabel("Dry run is on by default — nothing is written until you turn it off.")
        dry_note.setObjectName("dim")
        header.addWidget(dry_note)

        columns = QHBoxLayout()
        columns.setSpacing(12)

        # column 1: template
        left = QFrame()
        left.setObjectName("panel")
        left_layout = QVBoxLayout(left)
        left_layout.addWidget(section_label("1 · Template"))
        self.deploy_template = QComboBox()
        self.deploy_template.currentIndexChanged.connect(self.on_deploy_template_changed)
        left_layout.addWidget(self.deploy_template)
        self.deploy_body = QPlainTextEdit()
        self.deploy_body.setPlaceholderText("Template body — edit freely")
        self.deploy_body.textChanged.connect(lambda: self.deploy_vars_changed())
        left_layout.addWidget(self.deploy_body, 1)
        self.deploy_vars_container = QWidget()
        self.deploy_vars_layout = QFormLayout(self.deploy_vars_container)
        self.deploy_vars_layout.setContentsMargins(0, 0, 0, 0)
        self.deploy_var_inputs: dict[str, QLineEdit] = {}
        left_layout.addWidget(self.deploy_vars_container)
        self.deploy_save = QCheckBox("Persist config on device (write memory)")
        self.deploy_save.setChecked(True)
        left_layout.addWidget(self.deploy_save)
        columns.addWidget(left, 3)

        # column 2: targets
        middle = QFrame()
        middle.setObjectName("panel")
        middle_layout = QVBoxLayout(middle)
        middle_layout.addWidget(section_label("2 · Targets"))
        self.deploy_search = QLineEdit()
        self.deploy_search.setPlaceholderText("Filter devices…")
        self.deploy_search.textChanged.connect(self.render_deploy_targets)
        middle_layout.addWidget(self.deploy_search)
        target_buttons = QHBoxLayout()
        all_button = QPushButton("Select all")
        all_button.clicked.connect(lambda: self.select_targets(True))
        target_buttons.addWidget(all_button)
        none_button = QPushButton("Clear")
        none_button.clicked.connect(lambda: self.select_targets(False))
        target_buttons.addWidget(none_button)
        target_buttons.addStretch(1)
        middle_layout.addLayout(target_buttons)
        self.target_list = QListWidget()
        middle_layout.addWidget(self.target_list, 1)
        self.target_count = QLabel("0 device(s) selected")
        self.target_count.setObjectName("dim")
        middle_layout.addWidget(self.target_count)
        columns.addWidget(middle, 2)

        # column 3: preview & run
        right = QFrame()
        right.setObjectName("panel")
        right_layout = QVBoxLayout(right)
        right_layout.addWidget(section_label("3 · Preview & run"))
        self.opt_dry = QCheckBox("Dry run (change nothing)")
        self.opt_dry.setChecked(True)
        right_layout.addWidget(self.opt_dry)
        self.opt_backup = QCheckBox("Capture running config first")
        self.opt_backup.setChecked(True)
        right_layout.addWidget(self.opt_backup)
        self.opt_rollback = QCheckBox("Create rollback checkpoint")
        self.opt_rollback.setChecked(True)
        right_layout.addWidget(self.opt_rollback)
        self.opt_stop = QCheckBox("Stop after first failure")
        right_layout.addWidget(self.opt_stop)
        parallel_row = QHBoxLayout()
        parallel_row.addWidget(QLabel("Parallel"))
        self.opt_parallel = QSpinBox()
        self.opt_parallel.setRange(1, 32)
        self.opt_parallel.setValue(5)
        parallel_row.addWidget(self.opt_parallel)
        parallel_row.addStretch(1)
        right_layout.addLayout(parallel_row)

        self.deploy_preview = QPlainTextEdit()
        self.deploy_preview.setReadOnly(True)
        self.deploy_preview.setPlaceholderText("Rendered commands appear here.")
        right_layout.addWidget(self.deploy_preview, 1)

        run = QPushButton("Run deployment")
        run.setObjectName("primary")
        run.clicked.connect(self.run_deployment)
        right_layout.addWidget(run)
        self.deploy_hint = QLabel("")
        self.deploy_hint.setObjectName("dim")
        self.deploy_hint.setWordWrap(True)
        right_layout.addWidget(self.deploy_hint)
        columns.addWidget(right, 3)

        layout.addLayout(columns, 3)
        layout.addWidget(section_label("Deployment history"))
        self.job_table = table(["#", "Template", "Status", "Targets", "OK", "Failed", "Duration", "When"])
        self.job_table.doubleClicked.connect(self._open_job_from_table)
        layout.addWidget(self.job_table, 2)
        return page

    def _open_job_from_table(self, index: Any) -> None:
        item = self.job_table.item(index.row(), 0)
        if item is not None:
            self.show_job_detail(int(item.data(Qt.ItemDataRole.UserRole)))

    def on_deploy_template_changed(self) -> None:
        template = self.deploy_template.currentData()
        self.deploy_body.blockSignals(True)
        self.deploy_body.setPlainText((template or {}).get("body", ""))
        self.deploy_body.blockSignals(False)
        self._rebuild_deploy_vars((template or {}).get("variables") or {})
        self.auto_select_targets()
        self.update_deploy_preview()

    def _rebuild_deploy_vars(self, variables: dict[str, str]) -> None:
        while self.deploy_vars_layout.count():
            item = self.deploy_vars_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.deploy_var_inputs.clear()
        for key, value in variables.items():
            field = QLineEdit(str(value))
            field.textChanged.connect(self.update_deploy_preview)
            self.deploy_var_inputs[key] = field
            self.deploy_vars_layout.addRow(key, field)

    def deploy_vars_changed(self) -> None:
        self.update_deploy_preview()

    def deploy_variables(self) -> dict[str, str]:
        return {key: field.text() for key, field in self.deploy_var_inputs.items()}

    def auto_select_targets(self) -> None:
        template = self.deploy_template.currentData() or {}
        vendor = template.get("vendor")
        self.deploy_selection = {
            int(card["id"]) for card in self.devices if card.get("vendor") == vendor
        }
        self.render_deploy_targets()

    def select_targets(self, selected: bool) -> None:
        if selected:
            self.deploy_selection = {
                int(card["id"]) for card in self.devices if card.get("vendor") != "generic"
            }
        else:
            self.deploy_selection.clear()
        self.render_deploy_targets()

    def render_deploy_targets(self) -> None:
        needle = self.deploy_search.text().lower()
        self.target_list.clear()
        for card in self.devices:
            if needle and needle not in (card.get("name") or "").lower() and needle not in (card.get("host") or "").lower():
                continue
            unsupported = card.get("vendor") == "generic"
            item = QListWidgetItem(
                f"{card.get('name')}  ·  {card.get('host')}  ·  {card.get('vendor')}"
                + ("   (monitoring only)" if unsupported else "")
            )
            item.setData(Qt.ItemDataRole.UserRole, int(card["id"]))
            item.setFlags(
                item.flags()
                if unsupported
                else (item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            )
            if not unsupported and int(card["id"]) in self.deploy_selection:
                item.setCheckState(Qt.CheckState.Checked)
            else:
                item.setCheckState(Qt.CheckState.Unchecked)
            self.target_list.addItem(item)
        self.target_list.itemChanged.connect(self._target_toggled)
        self.target_count.setText(f"{len(self.deploy_selection)} device(s) selected")

    def _target_toggled(self, item: QListWidgetItem) -> None:
        device_id = item.data(Qt.ItemDataRole.UserRole)
        if item.checkState() == Qt.CheckState.Checked:
            self.deploy_selection.add(int(device_id))
        else:
            self.deploy_selection.discard(int(device_id))
        self.target_count.setText(f"{len(self.deploy_selection)} device(s) selected")

    def update_deploy_preview(self) -> None:
        body = self.deploy_body.toPlainText()
        template = self.deploy_template.currentData() or {}
        if not body.strip():
            self.deploy_preview.setPlainText("(nothing to render)")
            return
        payload = {
            "template_id": template.get("id"),
            "body": body,
            "vendor": template.get("vendor", "mikrotik"),
            "variables": self.deploy_variables(),
        }
        self.bridge.run(
            lambda: self.core.app.preview_template(
                payload["template_id"], payload["body"], payload["vendor"], payload["variables"]
            ),
            on_done=self._apply_deploy_preview,
            on_error=lambda exc: self.deploy_preview.setPlainText(f"preview failed: {exc}"),
        )

    def _apply_deploy_preview(self, result: dict[str, Any]) -> None:
        commands = result.get("commands") or []
        text = "\n".join(f"{i + 1:>3}  {c}" for i, c in enumerate(commands)) or "(no commands)"
        unresolved = result.get("unresolved") or []
        if unresolved:
            text += f"\n\nUNRESOLVED VARIABLES: {', '.join(unresolved)}"
        text += f"\n\n{len(commands)} command(s) · rollback: {result.get('rollback_support')}"
        self.deploy_preview.setPlainText(text)
        if result.get("rollback_support") == "manual":
            self.deploy_hint.setText(
                "This vendor has no native rollback checkpoint. The pre-change config is still "
                "captured and can be pushed back from the Backups page."
            )
        else:
            self.deploy_hint.setText(
                "A native checkpoint is created before the push, and restored automatically if a "
                "command is rejected."
            )

    def run_deployment(self) -> None:
        body = self.deploy_body.toPlainText()
        template = self.deploy_template.currentData() or {}
        if not body.strip():
            QMessageBox.warning(self, "Nothing to deploy", "Choose a template or write a config block.")
            return
        if not self.deploy_selection:
            QMessageBox.warning(self, "No targets", "Select at least one device.")
            return

        dry_run = self.opt_dry.isChecked()
        if not dry_run:
            answer = QMessageBox.question(
                self,
                "Confirm deployment",
                f"Push configuration to {len(self.deploy_selection)} device(s)?\n\n"
                "A backup is captured first"
                + (" and a rollback checkpoint is created." if self.opt_rollback.isChecked() else ".")
                + "\n\nContinue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        payload = {
            "body": body,
            "vendor": template.get("vendor", "mikrotik"),
            "device_ids": sorted(self.deploy_selection),
            "variables": self.deploy_variables(),
            "template_id": template.get("id"),
            "options": {
                "dry_run": dry_run,
                "save_config": self.deploy_save.isChecked(),
                "backup_before": self.opt_backup.isChecked(),
                "auto_rollback": self.opt_rollback.isChecked(),
                "stop_on_first_failure": self.opt_stop.isChecked(),
                "max_parallel": self.opt_parallel.value(),
            },
            "triggered_by": "desktop",
        }

        self.status.showMessage("starting deployment…")

        def started(job_id: int) -> None:
            self.status.showMessage(f"deployment #{job_id} running…")
            self.watch_job(job_id)

        self.bridge.run(
            lambda: self._create_deploy_async(payload),
            on_done=started,
            on_error=lambda exc: QMessageBox.critical(self, "Deployment failed to start", str(exc)),
        )

    async def _create_deploy_async(self, payload: dict[str, Any]) -> int:
        import asyncio

        job = await asyncio.to_thread(lambda: self.core.app.create_deploy(**payload))
        asyncio.create_task(self.core.app.run_deploy(job.id))
        return job.id

    def watch_job(self, job_id: int) -> None:
        def poll() -> None:
            self.bridge.run(
                lambda: self.core.app.job_detail(job_id),
                on_done=lambda detail: self._job_progress(job_id, detail),
            )

        self._job_timers = getattr(self, "_job_timers", {})
        timer = QTimer(self)
        timer.setInterval(1200)

        def tick() -> None:
            poll()

        timer.timeout.connect(tick)
        self._job_timers[job_id] = timer
        timer.start()
        poll()

    def _job_progress(self, job_id: int, detail: dict[str, Any] | None) -> None:
        if not detail:
            return
        status = detail.get("status")
        self.status.showMessage(
            f"deployment #{job_id}: {status} — {detail.get('succeeded', 0)} ok, {detail.get('failed', 0)} failed of {detail.get('total', 0)}"
        )
        if status not in ("running", "pending"):
            timer = getattr(self, "_job_timers", {}).pop(job_id, None)
            if timer is not None:
                timer.stop()
            self.load_jobs()
            self.refresh_all()
            self.show_job_detail(job_id)

    def show_job_detail(self, job_id: int) -> None:
        def render(detail: dict[str, Any] | None) -> None:
            if not detail:
                QMessageBox.information(self, "Not found", "That deployment no longer exists.")
                return
            dialog = QDialog(self)
            dialog.setWindowTitle(f"Deployment #{detail['id']} — {detail.get('template_name')}")
            dialog.resize(820, 620)
            layout = QVBoxLayout(dialog)

            summary = QLabel(
                f"{detail.get('status')}  ·  {detail.get('total')} targets  ·  "
                f"{detail.get('succeeded')} ok  ·  {detail.get('failed')} failed  ·  "
                f"{fmt_duration(detail.get('duration_sec'))}"
                + ("   [DRY RUN]" if (detail.get("options") or {}).get("dry_run") else "")
            )
            summary.setWordWrap(True)
            layout.addWidget(summary)

            target_table = table(["Device", "Status", "Time", "Commands", "Error"])
            for target in detail.get("targets") or []:
                row = target_table.rowCount()
                target_table.insertRow(row)
                target_table.setItem(row, 0, QTableWidgetItem(target.get("device_name", "")))
                target_table.setItem(row, 1, QTableWidgetItem(target.get("status", "")))
                duration = target.get("duration_ms")
                target_table.setItem(row, 2, mono_item(f"{duration} ms" if duration is not None else "—"))
                target_table.setItem(
                    row, 3, mono_item(f"{len(target.get('commands') or [])} cmd" + ("  +backup" if target.get("has_backup") else ""))
                )
                target_table.setItem(row, 4, QTableWidgetItem((target.get("error") or "")[:140]))
            target_table.resizeColumnsToContents()
            layout.addWidget(target_table, 2)

            layout.addWidget(section_label("Output"))
            output = QTextEdit()
            output.setReadOnly(True)
            chunks = []
            for target in detail.get("targets") or []:
                if target.get("output"):
                    chunks.append(f"===== {target['device_name']} ({target['host']}) =====\n{target['output']}")
            output.setPlainText("\n\n".join(chunks) or "(no output)")
            layout.addWidget(output, 3)

            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
            buttons.rejected.connect(dialog.reject)
            dialog.exec()

        self.bridge.run(lambda: self.core.app.job_detail(job_id), on_done=render)

    def load_jobs(self) -> None:
        def render(value: Any) -> None:
            jobs = value or []
            self.job_table.setRowCount(0)
            for job in jobs:
                row = self.job_table.rowCount()
                self.job_table.insertRow(row)
                id_item = mono_item(f"#{job['id']}")
                id_item.setData(Qt.ItemDataRole.UserRole, job["id"])
                self.job_table.setItem(row, 0, id_item)
                self.job_table.setItem(row, 1, QTableWidgetItem(f"{job.get('template_name')}  [{job.get('vendor')}]"))
                status_item = QTableWidgetItem(job.get("status", ""))
                status_item.setForeground(
                    {
                        "done": Qt.GlobalColor.green,  # type: ignore[attr-defined]
                        "failed": Qt.GlobalColor.red,  # type: ignore[attr-defined]
                        "running": Qt.GlobalColor.yellow,  # type: ignore[attr-defined]
                    }.get(job.get("status"), Qt.GlobalColor.gray)  # type: ignore[attr-defined]
                )
                self.job_table.setItem(row, 2, status_item)
                self.job_table.setItem(row, 3, mono_item(str(job.get("total", 0))))
                self.job_table.setItem(row, 4, mono_item(str(job.get("succeeded", 0))))
                self.job_table.setItem(row, 5, mono_item(str(job.get("failed", 0))))
                self.job_table.setItem(row, 6, mono_item(fmt_duration(job.get("duration_sec"))))
                self.job_table.setItem(
                    row, 7, mono_item(time.strftime("%Y-%m-%d %H:%M", time.localtime(job.get("created_at", time.time()))))
                )
            self.job_table.resizeColumnsToContents()
            if self.job_table.rowCount():
                self.job_table.setItem(0, 8, QTableWidgetItem("double-click to open"))

        self.bridge.run(
            lambda: as_dicts(self.core.app.store.list_jobs(limit=40)), on_done=render
        )

    # ── templates page ───────────────────────────────────────────────────────────────

    def _build_templates(self) -> QWidget:
        page, layout = self._page("Configuration templates", "built-in library plus your own — used by bulk deploys")
        header = self._header_layout(page)
        new = QPushButton("+ New template")
        new.setObjectName("primary")
        new.clicked.connect(self.new_template)
        header.addWidget(new)

        self.template_table = table(["Name", "Vendor", "Origin", "Variables", "Description"])
        self.template_table.doubleClicked.connect(self._open_template_from_table)
        layout.addWidget(self.template_table, 1)
        row = QHBoxLayout()
        edit = QPushButton("Edit selected")
        edit.clicked.connect(self._edit_template_from_table)
        row.addWidget(edit)
        view = QPushButton("Preview selected")
        view.clicked.connect(self._view_template_from_table)
        row.addWidget(view)
        delete = QPushButton("Delete selected")
        delete.setObjectName("danger")
        delete.clicked.connect(self._delete_template_from_table)
        row.addWidget(delete)
        row.addStretch(1)
        layout.addLayout(row)
        return page

    def load_templates(self) -> None:
        def render(value: Any) -> None:
            self.templates = value or []
            self.template_table.setRowCount(0)
            for template in self.templates:
                row = self.template_table.rowCount()
                self.template_table.insertRow(row)
                name_item = QTableWidgetItem(template.get("name", ""))
                name_item.setData(Qt.ItemDataRole.UserRole, template)
                self.template_table.setItem(row, 0, name_item)
                self.template_table.setItem(row, 1, QTableWidgetItem(template.get("vendor", "")))
                self.template_table.setItem(row, 2, QTableWidgetItem("built-in" if template.get("builtin") else "custom"))
                self.template_table.setItem(row, 3, mono_item(str(len(template.get("variables") or {}))))
                self.template_table.setItem(row, 4, QTableWidgetItem(template.get("description", "")))
            self.template_table.resizeColumnsToContents()

            # keep the deploy page in sync
            current = self.deploy_template.currentData()
            self.deploy_template.blockSignals(True)
            self.deploy_template.clear()
            for template in self.templates:
                if template.get("vendor") == "generic":
                    continue
                self.deploy_template.addItem(f"{template['name']}  ·  {template['vendor']}", template)
            self.deploy_template.blockSignals(False)
            if current:
                index = next(
                    (i for i, t in enumerate(self.templates) if t.get("id") and t.get("id") == current.get("id")),
                    None,
                )
                if index is not None:
                    self.deploy_template.setCurrentIndex(index)
            self.on_deploy_template_changed()

        self.bridge.run(lambda: self.core.app.template_library(), on_done=render)

    def _selected_template(self) -> dict[str, Any] | None:
        row = self.template_table.currentRow()
        if row < 0:
            return None
        item = self.template_table.item(row, 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _open_template_from_table(self, index: Any) -> None:
        item = self.template_table.item(index.row(), 0)
        if item is not None:
            self.open_template_editor(item.data(Qt.ItemDataRole.UserRole))

    def _edit_template_from_table(self) -> None:
        template = self._selected_template()
        if template is None:
            QMessageBox.information(self, "Nothing selected", "Select a template first.")
            return
        self.open_template_editor(template)

    def _view_template_from_table(self) -> None:
        template = self._selected_template()
        if template is None:
            QMessageBox.information(self, "Nothing selected", "Select a template first.")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(f"{template['name']}  ·  {template['vendor']}")
        dialog.resize(700, 560)
        layout = QVBoxLayout(dialog)
        description = QLabel(template.get("description", ""))
        description.setWordWrap(True)
        layout.addWidget(description)
        body = QPlainTextEdit(template.get("body", ""))
        body.setReadOnly(True)
        layout.addWidget(body, 1)
        if template.get("variables"):
            layout.addWidget(section_label("Variables"))
            variables = QPlainTextEdit("\n".join(f"{k} = {v}" for k, v in template["variables"].items()))
            variables.setReadOnly(True)
            variables.setMaximumHeight(90)
            layout.addWidget(variables)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()

    def _delete_template_from_table(self) -> None:
        template = self._selected_template()
        if template is None:
            QMessageBox.information(self, "Nothing selected", "Select a template first.")
            return
        if template.get("builtin"):
            QMessageBox.information(
                self,
                "Built-in template",
                "Built-in templates cannot be deleted. Save a template with the same name to override it.",
            )
            return
        if QMessageBox.question(self, "Delete template", f"Delete '{template['name']}'?") != QMessageBox.StandardButton.Yes:
            return
        self.bridge.run(
            lambda: self.core.app.delete_template(template["id"]),
            on_done=lambda _r: self.load_templates(),
        )

    def new_template(self) -> None:
        self.open_template_editor(None)

    def open_template_editor(self, template: dict[str, Any] | None) -> None:
        dialog = TemplateEditDialog(self.vendors, template, self)
        dialog.on_preview(
            lambda payload: self.bridge.run(
                lambda: self.core.app.preview_template(
                    None, payload["body"], payload["vendor"], payload["variables"]
                ),
                on_done=dialog.set_preview,
            )
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            payload = dialog.payload()
            if template and template.get("id"):
                payload["id"] = template["id"]
            elif template:
                # overriding a built-in keeps the same name so it shadows cleanly
                payload["id"] = None

            def saved(result: Any) -> None:
                self.load_templates()
                self.status.showMessage(f"template '{payload['name']}' saved", 4000)

            self.bridge.run(lambda: self.core.app.save_template(payload), on_done=saved,
                            on_error=lambda exc: QMessageBox.critical(self, "Save failed", str(exc)))

    # ── backups page ─────────────────────────────────────────────────────────────────

    def _build_backups(self) -> QWidget:
        page, layout = self._page("Configuration backups", "every deploy stores the pre-change config automatically")
        header = self._header_layout(page)
        capture = QPushButton("Capture from all devices")
        capture.clicked.connect(self.capture_all_backups)
        header.addWidget(capture)

        self.backup_table = table(["#", "Device", "Host", "When", "Size", "Source"])
        self.backup_table.doubleClicked.connect(lambda _i: self.view_selected_backup())
        layout.addWidget(self.backup_table, 1)

        row = QHBoxLayout()
        view = QPushButton("View selected")
        view.clicked.connect(self.view_selected_backup)
        row.addWidget(view)
        restore = QPushButton("Restore selected…")
        restore.setObjectName("danger")
        restore.clicked.connect(self.restore_selected_backup)
        row.addWidget(restore)
        delete = QPushButton("Delete selected")
        delete.clicked.connect(self.delete_selected_backup)
        row.addWidget(delete)
        row.addStretch(1)
        layout.addLayout(row)
        return page

    def load_backups(self) -> None:
        def render(value: Any) -> None:
            self.backup_table.setRowCount(0)
            for backup in value or []:
                row = self.backup_table.rowCount()
                self.backup_table.insertRow(row)
                id_item = mono_item(f"#{backup['id']}")
                id_item.setData(Qt.ItemDataRole.UserRole, backup["id"])
                self.backup_table.setItem(row, 0, id_item)
                self.backup_table.setItem(row, 1, QTableWidgetItem(backup.get("device_name", "")))
                self.backup_table.setItem(row, 2, mono_item(backup.get("host", "")))
                self.backup_table.setItem(
                    row, 3, mono_item(time.strftime("%Y-%m-%d %H:%M", time.localtime(backup.get("created_at", time.time()))))
                )
                self.backup_table.setItem(row, 4, mono_item(fmt_bytes(backup.get("byte_size"))))
                self.backup_table.setItem(row, 5, mono_item(backup.get("source", "")))
            self.backup_table.resizeColumnsToContents()

        self.bridge.run(lambda: self.core.app.store.list_backups(limit=200), on_done=render)

    def selected_backup_id(self) -> int | None:
        row = self.backup_table.currentRow()
        if row < 0:
            return None
        item = self.backup_table.item(row, 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def capture_all_backups(self) -> None:
        self.status.showMessage("capturing configurations from every enabled device…")

        async def run_all() -> list[dict[str, Any]]:
            devices = await __import__("asyncio").to_thread(self.core.app.store.list_devices, True)
            results = []
            for device in devices:
                result = await self.core.app.backup_device(device.id)
                result["device"] = device.name
                results.append(result)
            return results

        def done(results: list[dict[str, Any]]) -> None:
            ok = sum(1 for r in results if r.get("ok"))
            self.status.showMessage(f"{ok}/{len(results)} configurations captured", 6000)
            self.load_backups()

        self.bridge.run(run_all, on_done=done,
                        on_error=lambda exc: QMessageBox.critical(self, "Backup failed", str(exc)))

    def view_selected_backup(self) -> None:
        backup_id = self.selected_backup_id()
        if backup_id is None:
            QMessageBox.information(self, "Nothing selected", "Select a backup first.")
            return

        def render(backup: dict[str, Any] | None) -> None:
            if not backup:
                return
            dialog = QDialog(self)
            dialog.setWindowTitle(f"Backup #{backup['id']} — {backup.get('device_name')}")
            dialog.resize(820, 640)
            layout = QVBoxLayout(dialog)
            layout.addWidget(
                QLabel(
                    f"{backup.get('host')}  ·  "
                    f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(backup['created_at']))}  ·  "
                    f"{fmt_bytes(backup.get('byte_size'))}  ·  {backup.get('source')}"
                )
            )
            text = QPlainTextEdit(backup.get("config", ""))
            text.setReadOnly(True)
            layout.addWidget(text, 1)

            row = QHBoxLayout()
            save = QPushButton("Save to file…")

            def save_file() -> None:
                from PyQt6.QtWidgets import QFileDialog

                path, _ = QFileDialog.getSaveFileName(
                    dialog,
                    "Save configuration",
                    f"netpilot-backup-{backup['id']}-{backup.get('device_name')}.cfg",
                    "Config files (*.cfg *.txt);;All files (*)",
                )
                if path:
                    with open(path, "w") as handle:
                        handle.write(backup.get("config", ""))
                    self.status.showMessage(f"saved to {path}", 5000)

            save.clicked.connect(save_file)
            row.addWidget(save)
            row.addStretch(1)
            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
            buttons.rejected.connect(dialog.reject)
            row.addWidget(buttons)
            layout.addLayout(row)
            dialog.exec()

        self.bridge.run(lambda: self.core.app.store.get_backup(backup_id), on_done=render)

    def restore_selected_backup(self) -> None:
        backup_id = self.selected_backup_id()
        if backup_id is None:
            QMessageBox.information(self, "Nothing selected", "Select a backup first.")
            return

        def preview(result: dict[str, Any]) -> None:
            if not result.get("ok"):
                QMessageBox.critical(self, "Cannot restore", result.get("error", "unknown error"))
                return
            commands = result.get("preview") or []
            answer = QMessageBox.warning(
                self,
                "Confirm restore",
                f"Push this stored configuration back onto {result.get('device')}?\n\n"
                f"{result.get('command_count')} command(s) will be applied through the normal "
                "verified deploy flow, after capturing a fresh backup.\n\n"
                "First commands:\n" + "\n".join(f"  {c}" for c in commands[:10]),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            self.status.showMessage("restoring…")
            self.bridge.run(
                lambda: self.core.app.restore(backup_id, dry_run=False),
                on_done=self._restore_finished,
                on_error=lambda exc: QMessageBox.critical(self, "Restore failed", str(exc)),
            )

        self.bridge.run(lambda: self.core.app.restore(backup_id, dry_run=True), on_done=preview)

    def _restore_finished(self, result: dict[str, Any]) -> None:
        if result.get("ok"):
            QMessageBox.information(self, "Restore complete", f"Applied {result.get('applied')} command(s) to {result.get('device')}.")
        else:
            QMessageBox.critical(self, "Restore failed", result.get("error", "unknown error"))
        self.load_backups()

    def delete_selected_backup(self) -> None:
        backup_id = self.selected_backup_id()
        if backup_id is None:
            QMessageBox.information(self, "Nothing selected", "Select a backup first.")
            return
        if QMessageBox.question(self, "Delete backup", "Delete this stored configuration?") != QMessageBox.StandardButton.Yes:
            return
        self.bridge.run(
            lambda: self.core.app.store.delete_backup(backup_id),
            on_done=lambda _r: self.load_backups(),
        )

    # ── events page ──────────────────────────────────────────────────────────────────

    def _build_events(self) -> QWidget:
        page, layout = self._page("Events", "state transitions, deploys and notifications")
        header = self._header_layout(page)
        self.event_severity = QComboBox()
        self.event_severity.addItems(["all severities", "critical", "warning", "info"])
        self.event_severity.currentIndexChanged.connect(self.load_events)
        header.addWidget(self.event_severity)
        self.event_unacked = QCheckBox("Unacknowledged only")
        self.event_unacked.stateChanged.connect(self.load_events)
        header.addWidget(self.event_unacked)
        ack = QPushButton("Acknowledge all")
        ack.clicked.connect(self.ack_all_events)
        header.addWidget(ack)

        self.events_list = wrap_list(QListWidget())
        layout.addWidget(self.events_list, 1)
        return page

    def load_events(self) -> None:
        severity = self.event_severity.currentText()
        if severity == "all severities":
            severity = None
        unacked = self.event_unacked.isChecked()

        def render(value: Any) -> None:
            self.events_list.clear()
            for event in value or []:
                item = QListWidgetItem(
                    f"[{time.strftime('%m-%d %H:%M:%S', time.localtime(event['ts']))}] "
                    f"{event['severity'].upper():8s} {event['message']}"
                )
                item.setData(Qt.ItemDataRole.UserRole, event)
                if event.get("acknowledged"):
                    item.setForeground(Qt.GlobalColor.gray)  # type: ignore[attr-defined]
                elif event.get("severity") == "critical":
                    item.setForeground(Qt.GlobalColor.red)  # type: ignore[attr-defined]
                elif event.get("severity") == "warning":
                    item.setForeground(Qt.GlobalColor.yellow)  # type: ignore[attr-defined]
                self.events_list.addItem(item)

        self.bridge.run(
            lambda: as_dicts(
                self.core.app.store.list_events(
                    limit=400, severity=severity, unacknowledged_only=unacked
                )
            ),
            on_done=render,
        )

    def ack_all_events(self) -> None:
        self.bridge.run(
            lambda: self.core.app.store.acknowledge_all_events(),
            on_done=lambda _r: (self.refresh_all(), self.load_events()),
        )
        self.status.showMessage("all events acknowledged", 4000)

    # ── settings page ────────────────────────────────────────────────────────────────

    def _build_settings(self) -> QWidget:
        page, layout = self._page("Settings", "notifications, runtime status and the environment netpilot runs in")
        splitter = QSplitter(Qt.Orientation.Horizontal)

        left = QFrame()
        left.setObjectName("panel")
        left_layout = QVBoxLayout(left)
        left_layout.addWidget(section_label("Notifications"))

        self.nt_enabled = QCheckBox("Enable outbound alerts")
        left_layout.addWidget(self.nt_enabled)
        form = QFormLayout()
        self.nt_min = QComboBox()
        self.nt_min.addItems(["info", "warning", "critical"])
        self.nt_min.setCurrentText("warning")
        form.addRow("Minimum severity", self.nt_min)
        self.nt_cooldown = QSpinBox()
        self.nt_cooldown.setRange(0, 86400)
        self.nt_cooldown.setValue(300)
        form.addRow("Repeat cooldown (s)", self.nt_cooldown)
        self.nt_quiet = QLineEdit()
        self.nt_quiet.setPlaceholderText("23:00-07:00")
        form.addRow("Quiet hours", self.nt_quiet)
        self.nt_recovery = QCheckBox("Also notify on recovery")
        self.nt_recovery.setChecked(True)
        form.addRow("", self.nt_recovery)
        left_layout.addLayout(form)

        left_layout.addWidget(section_label("Webhook"))
        webhook_form = QFormLayout()
        self.nt_webhook = QLineEdit()
        self.nt_webhook.setPlaceholderText("https://hooks.slack.com/services/…")
        webhook_form.addRow("URL", self.nt_webhook)
        self.nt_kind = QComboBox()
        self.nt_kind.addItems(["generic", "slack", "discord", "teams"])
        webhook_form.addRow("Format", self.nt_kind)
        left_layout.addLayout(webhook_form)

        left_layout.addWidget(section_label("Email (SMTP)"))
        smtp_form = QFormLayout()
        self.nt_smtp_host = QLineEdit()
        smtp_form.addRow("Host", self.nt_smtp_host)
        self.nt_smtp_port = QSpinBox()
        self.nt_smtp_port.setRange(1, 65535)
        self.nt_smtp_port.setValue(587)
        smtp_form.addRow("Port", self.nt_smtp_port)
        self.nt_smtp_user = QLineEdit()
        smtp_form.addRow("Username", self.nt_smtp_user)
        self.nt_smtp_pass = QLineEdit()
        self.nt_smtp_pass.setEchoMode(QLineEdit.EchoMode.Password)
        smtp_form.addRow("Password", self.nt_smtp_pass)
        self.nt_smtp_from = QLineEdit()
        smtp_form.addRow("From", self.nt_smtp_from)
        self.nt_smtp_to = QLineEdit()
        smtp_form.addRow("To", self.nt_smtp_to)
        left_layout.addLayout(smtp_form)

        left_layout.addWidget(section_label("Syslog"))
        syslog_form = QFormLayout()
        self.nt_syslog_host = QLineEdit()
        syslog_form.addRow("Host", self.nt_syslog_host)
        self.nt_syslog_port = QSpinBox()
        self.nt_syslog_port.setRange(1, 65535)
        self.nt_syslog_port.setValue(514)
        syslog_form.addRow("Port", self.nt_syslog_port)
        left_layout.addLayout(syslog_form)

        buttons = QHBoxLayout()
        save = QPushButton("Save")
        save.setObjectName("primary")
        save.clicked.connect(self.save_notify_settings)
        buttons.addWidget(save)
        test = QPushButton("Send test alert")
        test.clicked.connect(self.test_notification)
        buttons.addWidget(test)
        buttons.addStretch(1)
        left_layout.addLayout(buttons)

        self.notify_result = QLabel("")
        self.notify_result.setWordWrap(True)
        left_layout.addWidget(self.notify_result)
        left_layout.addStretch(1)
        splitter.addWidget(left)

        right = QFrame()
        right.setObjectName("panel")
        right_layout = QVBoxLayout(right)
        right_layout.addWidget(section_label("Runtime"))
        self.runtime_text = QPlainTextEdit()
        self.runtime_text.setReadOnly(True)
        right_layout.addWidget(self.runtime_text, 2)
        right_layout.addWidget(section_label("Delivery status"))
        self.sink_text = QPlainTextEdit()
        self.sink_text.setReadOnly(True)
        right_layout.addWidget(self.sink_text, 1)
        hint = QLabel(
            "The dashboard and the event feed work with no notification sinks configured — "
            "nothing here is required for monitoring."
        )
        hint.setObjectName("dim")
        hint.setWordWrap(True)
        right_layout.addWidget(hint)
        splitter.addWidget(right)

        splitter.setSizes([640, 560])
        layout.addWidget(splitter, 1)
        return page

    def load_settings(self) -> None:
        def render(snapshot: dict[str, Any]) -> None:
            notify = snapshot.get("notify") or {}
            self.nt_enabled.setChecked(bool(notify.get("enabled")))
            self.nt_min.setCurrentText(notify.get("min_severity") or "warning")
            self.nt_cooldown.setValue(int(notify.get("cooldown_sec") or 300))
            self.nt_quiet.setText(notify.get("quiet_hours") or "")
            self.nt_recovery.setChecked(notify.get("notify_on_recovery", True) is not False)
            self.nt_webhook.setText(notify.get("webhook_url") or "")
            self.nt_kind.setCurrentText(notify.get("webhook_kind") or "generic")
            self.nt_smtp_host.setText(notify.get("smtp_host") or "")
            self.nt_smtp_port.setValue(int(notify.get("smtp_port") or 587))
            self.nt_smtp_user.setText(notify.get("smtp_user") or "")
            self.nt_smtp_from.setText(notify.get("smtp_from") or "")
            self.nt_smtp_to.setText(notify.get("smtp_to") or "")
            self.nt_syslog_host.setText(notify.get("syslog_host") or "")
            self.nt_syslog_port.setValue(int(notify.get("syslog_port") or 514))

            stats = snapshot.get("monitor_stats") or {}
            self.runtime_text.setPlainText(
                "\n".join(
                    [
                        f"version            {__version__}",
                        f"uptime             {fmt_uptime(snapshot.get('uptime_sec'))}",
                        f"monitor            {'running' if snapshot.get('monitor_running') else 'stopped'}",
                        f"probe concurrency  {stats.get('concurrency')}",
                        f"checks executed    {stats.get('checks_run')}",
                        f"results stored     {stats.get('results_written')}",
                        f"vendors            {', '.join(v['name'] for v in snapshot.get('vendors') or [])}",
                        f"check kinds        {', '.join(snapshot.get('check_kinds') or [])}",
                        "",
                        f"database           {snapshot.get('db_path')}",
                        f"data directory     {snapshot.get('data_dir')}",
                    ]
                )
            )

            status = snapshot.get("notify_status") or {}
            sinks = status.get("sinks") or []
            if sinks:
                self.sink_text.setPlainText(
                    "\n\n".join(
                        f"{sink['name']} → {sink['target']}\n  {sink['sent']} sent · {sink['failed']} failed"
                        + (f"\n  last error: {sink['last_error']}" if sink.get("last_error") else "")
                        for sink in sinks
                    )
                )
            else:
                self.sink_text.setPlainText("No sinks configured.")

        self.bridge.run(lambda: self.core.app.settings_snapshot(), on_done=render)

    def save_notify_settings(self) -> None:
        payload = {
            "enabled": self.nt_enabled.isChecked(),
            "min_severity": self.nt_min.currentText(),
            "cooldown_sec": self.nt_cooldown.value(),
            "quiet_hours": self.nt_quiet.text(),
            "notify_on_recovery": self.nt_recovery.isChecked(),
            "webhook_url": self.nt_webhook.text(),
            "webhook_kind": self.nt_kind.currentText(),
            "smtp_host": self.nt_smtp_host.text(),
            "smtp_port": self.nt_smtp_port.value(),
            "smtp_user": self.nt_smtp_user.text(),
            "smtp_password": self.nt_smtp_pass.text(),
            "smtp_from": self.nt_smtp_from.text(),
            "smtp_to": self.nt_smtp_to.text(),
            "syslog_host": self.nt_syslog_host.text(),
            "syslog_port": self.nt_syslog_port.value(),
        }
        self.bridge.run(
            lambda: self.core.app.save_alerts(payload),
            on_done=lambda _r: (self.load_settings(), self.status.showMessage("notification settings saved", 4000)),
            on_error=lambda exc: QMessageBox.critical(self, "Save failed", str(exc)),
        )
        self.nt_smtp_pass.clear()

    def test_notification(self) -> None:
        from ..models import Event

        def dispatch() -> dict[str, Any]:
            app = self.core.app
            event = Event(
                device_name="netpilot",
                kind="test",
                severity="warning",
                message="This is a netpilot test notification — routing works.",
                details={"state": "test"},
            )
            original = app.alerts.config.cooldown_sec
            app.alerts.config.cooldown_sec = 0
            try:
                return app.alerts.dispatch(event)
            finally:
                app.alerts.config.cooldown_sec = original

        def done(result: dict[str, Any]) -> None:
            if result.get("sent"):
                self.notify_result.setStyleSheet(f"color: {COLORS['up']};")
                self.notify_result.setText("Test alert delivered.")
            else:
                self.notify_result.setStyleSheet(f"color: {COLORS['degraded']};")
                detail = "; ".join(s.get("error", "") for s in result.get("sinks") or [])
                self.notify_result.setText(f"Not delivered: {result.get('reason')} {detail}")

        self.bridge.run(dispatch, on_done=done, on_error=lambda exc: self.notify_result.setText(str(exc)))

    # ── device actions ───────────────────────────────────────────────────────────────

    def load_meta(self) -> None:
        """Load vendor metadata + runtime paths once at startup."""

        def render(snapshot: dict[str, Any]) -> None:
            self.vendors = snapshot.get("vendors") or []
            self.version_label.setText(f"v{__version__}")
            self.core_label.setText(f"data: {snapshot.get('data_dir') or '—'}")

        self.bridge.run(lambda: self.core.app.settings_snapshot(), on_done=render)
        self.bridge.run(lambda: self.core.app.store.list_credentials(), on_done=self._set_credentials)

    def _set_credentials(self, credentials: list[Any]) -> None:
        self.credentials = [c.to_dict(redact=True) for c in credentials]

    def refresh_all(self) -> None:
        self.load_credentials_and_overview()

    def load_credentials_and_overview(self) -> None:
        def got_overview(overview: dict[str, Any]) -> None:
            self.overview = overview
            self.devices = overview.get("devices") or []
            self.update_stats()
            self.render_tags()
            self.render_device_grid()
            self.render_device_table()
            self.render_dash_events()
            self.render_dash_jobs()
            self.status.showMessage(
                f"{overview.get('devices_total', 0)} devices · "
                f"{'monitor running' if overview.get('monitor_running') else 'monitor stopped'} · "
                f"updated {time.strftime('%H:%M:%S')}"
            )

        def got_credentials(credentials: list[Any]) -> None:
            self.credentials = [c.to_dict(redact=True) for c in credentials]

        self.bridge.run(lambda: self.core.app.overview(), on_done=got_overview)
        self.bridge.run(lambda: self.core.app.store.list_credentials(), on_done=got_credentials)

    def update_stats(self) -> None:
        overview = self.overview
        states = overview.get("states") or {}
        values = {
            "Devices": (str(overview.get("devices_total", 0)), f"{overview.get('devices_enabled', 0)} monitored", None),
            "Up": (str(states.get("up", 0)), "responding", COLORS["up"]),
            "Down": (str(states.get("down", 0)), "not responding", COLORS["down"]),
            "Degraded": (str(states.get("degraded", 0)), "partial failures", COLORS["degraded"]),
            "Avg latency": (
                str(overview.get("avg_latency_ms")) if overview.get("avg_latency_ms") is not None else "—",
                "milliseconds",
                None,
            ),
            "Checks / 24h": (f"{overview.get('checks_24h', 0):,}", f"{overview.get('deploys_24h', 0)} deployments", None),
            "Backups": (str(overview.get("backups_total", 0)), "configs stored", None),
            "Open alerts": (
                str(overview.get("unacknowledged_events", 0)),
                "unacknowledged",
                COLORS["down"] if overview.get("unacknowledged_events") else None,
            ),
        }
        for key, (value, detail, _color) in values.items():
            card = self.stat_cards.get(key)
            if card is not None:
                card.set_values(value, detail)

    def render_dash_events(self) -> None:
        self.dash_events.clear()
        for event in (self.overview.get("events") or [])[:40]:
            item = QListWidgetItem(f"[{event['severity'][:4].upper()}] {event['message']}")
            item.setData(Qt.ItemDataRole.UserRole, event)
            if event.get("severity") == "critical":
                item.setForeground(Qt.GlobalColor.red)  # type: ignore[attr-defined]
            elif event.get("severity") == "warning":
                item.setForeground(Qt.GlobalColor.yellow)  # type: ignore[attr-defined]
            if event.get("acknowledged"):
                item.setForeground(Qt.GlobalColor.gray)  # type: ignore[attr-defined]
            self.dash_events.addItem(item)

    def render_dash_jobs(self) -> None:
        self.dash_jobs.clear()
        for job in (self.overview.get("recent_jobs") or []):
            text = (
                f"#{job['id']} {job.get('template_name')} — "
                f"{job.get('succeeded')}/{job.get('total')} ok"
                + (f", {job.get('failed')} failed" if job.get("failed") else "")
                + (" [dry run]" if (job.get("options") or {}).get("dry_run") else "")
            )
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, job["id"])
            self.dash_jobs.addItem(item)

    def add_device(self) -> None:
        dialog = DeviceEditDialog(self.credentials, self.vendors, None, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        payload = dialog.payload()

        def done(device: Any) -> None:
            self.status.showMessage(
                f"{payload['name']} added — monitoring starts now", 6000
            )
            self.refresh_all()
            self.load_templates()

        def failed(exc: Exception) -> None:
            QMessageBox.critical(self, "Could not add device", str(exc))

        self.bridge.run(lambda: self.core.app.add_device(payload), on_done=done, on_error=failed)

    def open_device(self, device_id: int) -> None:
        self.bridge.run(
            lambda: self.core.app.device_detail(int(device_id)),
            on_done=self._show_device_detail,
        )

    def _show_device_detail(self, detail: dict[str, Any] | None) -> None:
        if detail is None:
            QMessageBox.information(self, "Not found", "That device no longer exists.")
            return
        if self.detail_dialog is not None:
            self.detail_dialog.close()
        dialog = DeviceDetailDialog(detail, self)
        dialog.action.connect(self.on_device_action)
        dialog.finished.connect(lambda _r: setattr(self, "detail_dialog", None))
        self.detail_dialog = dialog
        self.on_device_action("preview", int(detail["id"]))
        dialog.show()

    def on_device_action(self, key: str, device_id: int) -> None:
        dialog = self.detail_dialog

        if key == "test":
            self.status.showMessage("connecting over SSH…")
            self.bridge.run(
                lambda: self.core.app.test_device(device_id),
                on_done=self._show_test_result,
                on_error=lambda exc: QMessageBox.critical(self, "Test failed", str(exc)),
            )
            return

        if key == "probe":
            self.status.showMessage("running all checks…")

            def probed(results: list[dict[str, Any]]) -> None:
                ok = sum(1 for r in results if r.get("ok"))
                self.status.showMessage(f"{ok}/{len(results)} checks passed", 5000)
                self.open_device(device_id)
                self.refresh_all()

            self.bridge.run(lambda: self.core.app.probe_device(device_id), on_done=probed)
            return

        if key == "backup":
            self.status.showMessage("capturing configuration…")

            def backed_up(result: dict[str, Any]) -> None:
                if result.get("ok"):
                    self.status.showMessage(f"captured {result['bytes']} bytes", 5000)
                else:
                    QMessageBox.warning(self, "Backup failed", result.get("error", "unknown"))
                self.open_device(device_id)
                self.load_backups()

            self.bridge.run(lambda: self.core.app.backup_device(device_id), on_done=backed_up)
            return

        if key == "edit":
            detail = self.overview_devices_by_id().get(device_id)
            if detail is None:
                return
            editor = DeviceEditDialog(self.credentials, self.vendors, detail, self)
            if editor.exec() != QDialog.DialogCode.Accepted:
                return
            payload = editor.payload()
            self.bridge.run(
                lambda: self.core.app.update_device(device_id, payload),
                on_done=lambda _r: (self.refresh_all(), self.open_device(device_id)),
                on_error=lambda exc: QMessageBox.critical(self, "Update failed", str(exc)),
            )
            return

        if key == "delete":
            detail = self.overview_devices_by_id().get(device_id) or {}
            if QMessageBox.question(
                self,
                "Delete device",
                f"Delete {detail.get('name')} and its history?",
            ) != QMessageBox.StandardButton.Yes:
                return
            if dialog is not None:
                dialog.close()
            self.bridge.run(
                lambda: self.core.app.delete_device(device_id),
                on_done=lambda _r: self.refresh_all(),
            )
            return

        if key == "deploy":
            self.show_page(2)
            return

        if key == "preview" and dialog is not None:
            payload = dialog.template_payload()
            self.bridge.run(
                lambda: self.core.app.preview_template(
                    payload["template_id"], payload["body"], payload["vendor"], payload["variables"]
                ),
                on_done=dialog.set_deploy_preview,
            )
            return

        if key.startswith("deploy-one") and dialog is not None:
            payload = dialog.template_payload()
            dry = not key.endswith("-real")
            device_name = (dialog.device or {}).get("name")

            def done(job: Any) -> None:
                self.status.showMessage(f"deployment #{job.id} started", 5000)
                if dry:
                    QMessageBox.information(
                        self,
                        "Dry run complete",
                        f"Deployment #{job.id} finished for {device_name}.\n\n"
                        "Nothing was written to the device. Turn off 'Dry run' and run again to apply.",
                    )
                self.watch_job(job.id)

            async def create():
                import asyncio

                template = payload
                app = self.core.app
                job = await asyncio.to_thread(
                    lambda: app.create_deploy(
                        body=template["body"],
                        vendor=template["vendor"],
                        device_ids=[device_id],
                        variables=template["variables"],
                        options={
                            "dry_run": dry,
                            "backup_before": True,
                            "auto_rollback": True,
                            "max_parallel": 1,
                        },
                        template_id=template["template_id"],
                        triggered_by="desktop",
                    )
                )
                asyncio.create_task(app.run_deploy(job.id))
                return job

            self.bridge.run(create, on_done=done,
                            on_error=lambda exc: QMessageBox.critical(self, "Deploy failed", str(exc)))
            return

        if key in ("check-add", "check-edit"):
            existing = None
            if key == "check-edit" and dialog is not None:
                check_id = dialog.selected_check_id()
                if check_id is None:
                    return
                existing = next(
                    (c for c in dialog.device.get("checks") or [] if c.get("id") == check_id), None
                )
            editor = CheckEditDialog(existing, self)
            if editor.exec() != QDialog.DialogCode.Accepted:
                return
            payload = editor.payload(device_id)
            if key == "check-edit" and existing and existing.get("id"):
                self.bridge.run(
                    lambda: self.core.app.update_check(existing["id"], payload),
                    on_done=lambda _r: (self.open_device(device_id), self.refresh_all()),
                    on_error=lambda exc: QMessageBox.critical(self, "Could not save monitor", str(exc)),
                )
            else:
                self.bridge.run(
                    lambda: self.core.app.add_check(payload),
                    on_done=lambda _r: (self.open_device(device_id), self.refresh_all()),
                    on_error=lambda exc: QMessageBox.critical(self, "Could not add monitor", str(exc)),
                )
            return

        if key == "check-delete":
            if dialog is None:
                return
            check_id = dialog.selected_check_id()
            if check_id is None:
                return
            self.bridge.run(
                lambda: self.core.app.delete_check(check_id),
                on_done=lambda _r: (self.open_device(device_id), self.refresh_all()),
            )
            return

        if key == "backup-view":
            if dialog is None:
                return
            backup_id = dialog.selected_backup_id()
            if backup_id is None:
                return

            def render(backup: dict[str, Any] | None) -> None:
                if not backup:
                    return
                viewer = QDialog(self)
                viewer.setWindowTitle(f"Backup #{backup['id']}")
                viewer.resize(760, 560)
                layout = QVBoxLayout(viewer)
                layout.addWidget(QLabel(f"{backup.get('device_name')}  ·  {backup.get('host')}"))
                text = QPlainTextEdit(backup.get("config", ""))
                text.setReadOnly(True)
                layout.addWidget(text, 1)
                buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
                buttons.rejected.connect(viewer.reject)
                layout.addWidget(buttons)
                viewer.exec()

            self.bridge.run(lambda: self.core.app.store.get_backup(backup_id), on_done=render)
            return

        if key == "backup-restore":
            if dialog is None:
                return
            backup_id = dialog.selected_backup_id()
            if backup_id is None:
                return

            def confirm(result: dict[str, Any]) -> None:
                if not result.get("ok"):
                    QMessageBox.critical(self, "Cannot restore", result.get("error", ""))
                    return
                answer = QMessageBox.warning(
                    self,
                    "Confirm restore",
                    f"Push this stored configuration back onto {result.get('device')}?\n\n"
                    f"{result.get('command_count')} command(s), applied through the normal verified flow.",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if answer != QMessageBox.StandardButton.Yes:
                    return
                self.status.showMessage("restoring…")
                self.bridge.run(
                    lambda: self.core.app.restore(backup_id, dry_run=False),
                    on_done=self._restore_finished,
                )

            self.bridge.run(lambda: self.core.app.restore(backup_id, dry_run=True), on_done=confirm)
            return

    def _show_test_result(self, result: dict[str, Any]) -> None:
        if result.get("ok"):
            QMessageBox.information(
                self, "Connection OK", f"Connected in {result['elapsed_ms']} ms\n\n{result.get('summary')}"
            )
            self.status.showMessage(f"connection ok — {result.get('summary')}", 6000)
            info = result.get("info") or {}
            if info.get("hostname") and self.detail_dialog is not None:
                self.detail_dialog.device["notes"] = (
                    (self.detail_dialog.device.get("notes") or "")
                    + f"\n[ssh] {info.get('model', '')} {info.get('version', '')} uptime {info.get('uptime', '')}"
                ).strip()
        else:
            QMessageBox.warning(self, "Connection failed", result.get("error", "unknown error"))
            self.status.showMessage("connection failed", 6000)

    def manage_credentials(self) -> None:
        dialog = CredentialDialog(self)
        dialog.set_credentials(self.credentials)

        def reload() -> None:
            def got(credentials: list[Any]) -> None:
                self.credentials = [c.to_dict(redact=True) for c in credentials]
                dialog.set_credentials(self.credentials)

            self.bridge.run(lambda: self.core.app.store.list_credentials(), on_done=got)

        def add() -> None:
            payload = dialog.payload()
            if not payload.get("name"):
                QMessageBox.warning(dialog, "Missing name", "A credential name is required.")
                return

            def done(_r: Any) -> None:
                dialog.set_credentials([])
                reload()
                self.status.showMessage("credential stored (encrypted)", 4000)

            self.bridge.run(
                lambda: self.core.app.add_credential(payload),
                on_done=done,
                on_error=lambda exc: QMessageBox.critical(dialog, "Could not save", str(exc)),
            )

        def remove(cred_id: int) -> None:
            self.bridge.run(lambda: self.core.app.store.delete_credential(cred_id), on_done=lambda _r: reload())

        dialog.add_requested = add
        dialog.delete_requested = remove
        dialog.exec()
        self.refresh_all()

    # ── live signals ─────────────────────────────────────────────────────────────────

    def on_event(self, event: Any) -> None:
        item = QListWidgetItem(f"[{event.severity[:4].upper()}] {event.message}")
        if event.severity == "critical":
            item.setForeground(Qt.GlobalColor.red)  # type: ignore[attr-defined]
        elif event.severity == "warning":
            item.setForeground(Qt.GlobalColor.yellow)  # type: ignore[attr-defined]
        self.dash_events.insertItem(0, item)
        while self.dash_events.count() > 40:
            self.dash_events.takeItem(self.dash_events.count() - 1)

        if event.severity == "critical":
            self.status.showMessage(event.message, 10000)

    def on_job(self, job: Any) -> None:
        """A deployment changed state — refresh the history table."""
        self.load_jobs()

    def on_result(self, result: Any) -> None:
        """Update the affected card in place — no full refresh, no flicker."""
        card = self.cards.get(int(result.device_id)) if result.device_id else None
        if card is not None:
            state = card.pill.text().replace("● ", "")
            if not result.ok and state == "up":
                card.pill.set_state("degraded")
            elif result.ok and state in ("unknown", "down"):
                card.pill.set_state("up")

    # ── shutdown ─────────────────────────────────────────────────────────────────────

    def closeEvent(self, event: Any) -> None:  # noqa: N802 - Qt naming
        self.refresh_timer.stop()
        for timer in getattr(self, "_job_timers", {}).values():
            timer.stop()
        event.accept()
