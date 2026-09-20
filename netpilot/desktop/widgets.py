"""Reusable Qt widgets for the netpilot desktop UI.

Everything here is deliberately small and dependency-free (no charting library): the
sparkline, the state pill and the device card are painted with ``QPainter`` so the desktop
app renders the same information as the web dashboard without pulling in matplotlib.
"""

from __future__ import annotations

from typing import Any, Sequence, TypeVar

from PyQt6.QtCore import QPointF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

T = TypeVar("T")

#: Shared palette so the desktop app and the web dashboard look like one product.
COLORS = {
    "bg": "#0d1117",
    "bg2": "#131a23",
    "bg3": "#182029",
    "line": "#253040",
    "line2": "#2f3d50",
    "fg": "#dce6f1",
    "fg_dim": "#8b9bb0",
    "fg_dimmer": "#61708a",
    "accent": "#4fd1a3",
    "accent_dim": "#2c8468",
    "up": "#3fbf7f",
    "down": "#e0574f",
    "degraded": "#e0a33f",
    "unknown": "#6d7d93",
    "info": "#5aa9e6",
}

STATE_COLORS = {
    "up": COLORS["up"],
    "down": COLORS["down"],
    "degraded": COLORS["degraded"],
    "unknown": COLORS["unknown"],
}

SEVERITY_COLORS = {
    "critical": COLORS["down"],
    "warning": COLORS["degraded"],
    "info": COLORS["info"],
}

DARK_QSS = f"""
QWidget {{
    background: {COLORS["bg"]};
    color: {COLORS["fg"]};
    font-size: 13px;
}}
QMainWindow, QDialog {{ background: {COLORS["bg"]}; }}
QFrame#panel, QFrame#card {{
    background: {COLORS["bg2"]};
    border: 1px solid {COLORS["line"]};
    border-radius: 10px;
}}
QFrame#sidebar {{
    background: {COLORS["bg2"]};
    border-right: 1px solid {COLORS["line"]};
}}
QLabel#h1 {{ font-size: 20px; font-weight: 600; }}
QLabel#h2 {{ font-size: 12px; font-weight: 600; color: {COLORS["fg_dimmer"]};
             text-transform: uppercase; letter-spacing: 1px; }}
QLabel#brand {{ font-size: 16px; font-weight: 700; letter-spacing: .3px; }}
QLabel#cardTitle {{ font-size: 13.5px; font-weight: 600; text-transform: none; letter-spacing: 0; }}
QLabel#dim, QLabel#sub {{ color: {COLORS["fg_dimmer"]}; }}
QLabel#mono {{ font-family: ui-monospace, Menlo, Consolas, monospace; color: {COLORS["fg_dim"]}; }}
QLabel#stat {{ font-size: 24px; font-weight: 600; }}
QLabel#statSmall {{ font-size: 16px; font-weight: 600; }}

QPushButton {{
    background: {COLORS["bg3"]};
    border: 1px solid {COLORS["line2"]};
    border-radius: 8px;
    padding: 7px 14px;
    color: {COLORS["fg"]};
}}
QPushButton:hover {{ border-color: {COLORS["accent_dim"]}; }}
QPushButton:disabled {{ color: {COLORS["fg_dimmer"]}; }}
QPushButton#primary {{
    background: {COLORS["accent"]};
    border-color: {COLORS["accent"]};
    color: #06211a;
    font-weight: 600;
}}
QPushButton#primary:hover {{ background: #5fdcae; }}
QPushButton#danger {{ border-color: #5a2a28; color: #ef8b84; }}
QPushButton#navButton {{
    background: transparent;
    border: 0;
    border-left: 2px solid transparent;
    border-radius: 0;
    padding: 9px 14px;
    text-align: left;
    color: {COLORS["fg_dim"]};
}}
QPushButton#navButton:hover {{ background: {COLORS["bg3"]}; color: {COLORS["fg"]}; }}
QPushButton#navButton:checked {{
    background: {COLORS["bg3"]};
    border-left: 2px solid {COLORS["accent"]};
    color: {COLORS["fg"]};
}}

QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit, QTextEdit {{
    background: {COLORS["bg"]};
    border: 1px solid {COLORS["line2"]};
    border-radius: 8px;
    padding: 6px 9px;
    selection-background-color: {COLORS["accent_dim"]};
}}
QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus, QTextEdit:focus {{
    border-color: {COLORS["accent_dim"]};
}}
QPlainTextEdit, QTextEdit {{ font-family: ui-monospace, Menlo, Consolas, monospace; }}
QComboBox::drop-down {{ border: 0; width: 18px; }}
QComboBox QAbstractItemView {{
    background: {COLORS["bg2"]};
    border: 1px solid {COLORS["line2"]};
    selection-background-color: {COLORS["accent_dim"]};
}}

QTableWidget {{
    background: {COLORS["bg2"]};
    border: 1px solid {COLORS["line"]};
    border-radius: 10px;
    gridline-color: {COLORS["line"]};
    selection-background-color: {COLORS["bg3"]};
    selection-color: {COLORS["fg"]};
}}
QTableWidget::item {{ padding: 6px; border: 0; }}
QHeaderView::section {{
    background: {COLORS["bg2"]};
    color: {COLORS["fg_dimmer"]};
    border: 0;
    border-bottom: 1px solid {COLORS["line"]};
    padding: 8px;
    font-size: 11px;
    font-weight: 600;
}}

QScrollArea {{ border: 0; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {COLORS["line2"]}; border-radius: 5px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {COLORS["accent_dim"]}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; }}
QScrollBar::handle:horizontal {{ background: {COLORS["line2"]}; border-radius: 5px; min-width: 30px; }}

QCheckBox {{ spacing: 7px; }}
QCheckBox::indicator {{
    width: 15px; height: 15px; border-radius: 4px;
    border: 1px solid {COLORS["line2"]}; background: {COLORS["bg"]};
}}
QCheckBox::indicator:checked {{ background: {COLORS["accent"]}; border-color: {COLORS["accent"]}; }}

QTabWidget::pane {{ border: 1px solid {COLORS["line"]}; border-radius: 10px; top: -1px; }}
QTabBar::tab {{
    background: transparent; color: {COLORS["fg_dim"]};
    padding: 8px 14px; border-bottom: 2px solid transparent;
}}
QTabBar::tab:selected {{ color: {COLORS["fg"]}; border-bottom: 2px solid {COLORS["accent"]}; }}

QSplitter::handle {{ background: {COLORS["line"]}; }}
QToolTip {{
    background: {COLORS["bg3"]}; color: {COLORS["fg"]};
    border: 1px solid {COLORS["line2"]}; padding: 5px 8px;
}}
QStatusBar {{ background: {COLORS["bg2"]}; border-top: 1px solid {COLORS["line"]}; }}
"""


class StatePill(QLabel):
    """``● up`` style inline indicator."""

    def __init__(self, state: str = "unknown", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.set_state(state)

    def set_state(self, state: str) -> None:
        colour = STATE_COLORS.get(state, COLORS["unknown"])
        self.setText(f"● {state}")
        self.setStyleSheet(
            f"color: {colour}; font-weight: 600; font-size: 11px; letter-spacing: .5px;"
        )


class Sparkline(QWidget):
    """Latency/availability strip painted from probe history."""

    def __init__(self, height: int = 40, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._points: list[dict[str, Any]] = []
        self.setMinimumHeight(height)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_points(self, points: Sequence[dict[str, Any]]) -> None:
        self._points = list(points)
        self.update()

    def paintEvent(self, event: Any) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        width = self.width()
        height = self.height()
        baseline = height - 2

        painter.setPen(QPen(QColor(COLORS["line"]), 1))
        painter.drawLine(0, baseline, width, baseline)

        if not self._points:
            painter.setPen(QColor(COLORS["fg_dimmer"]))
            painter.drawText(4, height // 2 + 4, "no data yet")
            painter.end()
            return

        points = self._points
        values = [p.get("latency_ms") for p in points if p.get("ok") and p.get("latency_ms") is not None]
        ceiling = (max(values) if values else 1.0) * 1.25 or 1.0
        step = width / max(1, len(points) - 1)

        # Failure markers first, so the latency line draws over them.
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(COLORS["down"]))
        for index, point in enumerate(points):
            if not point.get("ok"):
                painter.drawRect(int(index * step) - 1, 0, 3, height)

        path = QPainterPath()
        started = False
        for index, point in enumerate(points):
            x = index * step
            if point.get("ok"):
                value = point.get("latency_ms") or ceiling * 0.15
                y = height - 4 - (value / ceiling) * (height - 10)
            else:
                y = baseline - 1
            if started:
                path.lineTo(x, y)
            else:
                path.moveTo(x, y)
                started = True

        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(COLORS["accent"]), 2))
        painter.drawPath(path)
        painter.end()


class DeviceCard(QFrame):
    """One device tile on the dashboard."""

    clicked = pyqtSignal(int)

    def __init__(self, card: dict[str, Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.device_id = int(card["id"])
        self.setObjectName("card")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumWidth(250)

        state = (card.get("state") or {}).get("state", "unknown")
        self._accent = STATE_COLORS.get(state, COLORS["unknown"])

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(6)

        top = QHBoxLayout()
        top.setSpacing(8)
        name = QLabel(card.get("name") or card.get("host") or "—")
        name.setObjectName("cardTitle")
        font = QFont()
        font.setBold(True)
        name.setFont(font)
        top.addWidget(name)
        top.addStretch(1)
        self.pill = StatePill(state)
        top.addWidget(self.pill)
        layout.addLayout(top)

        host = QLabel(f"{card.get('host', '')}:{card.get('ssh_port', 22)}")
        host.setObjectName("mono")
        layout.addWidget(host)

        self.spark = Sparkline(height=34)
        self.spark.set_points(card.get("sparkline") or [])
        layout.addWidget(self.spark)

        meta = QHBoxLayout()
        meta.setSpacing(12)
        state_data = card.get("state") or {}
        latency = state_data.get("last_latency_ms")
        availability = card.get("availability_24h")
        self.meta = QLabel(
            f"{'—' if latency is None else f'{latency:.1f} ms'}"
            f"   ·   24h {'—' if availability is None else f'{availability}%'}"
            f"   ·   {card.get('vendor', '')}"
        )
        self.meta.setObjectName("mono")
        meta.addWidget(self.meta)
        meta.addStretch(1)
        layout.addLayout(meta)

        if card.get("tags"):
            tags = QLabel("  ".join(f"#{t}" for t in card["tags"]))
            tags.setObjectName("dim")
            layout.addWidget(tags)

    def update_card(self, card: dict[str, Any]) -> None:
        """Refresh in place so the dashboard does not flicker on every probe."""
        state = (card.get("state") or {}).get("state", "unknown")
        self._accent = STATE_COLORS.get(state, COLORS["unknown"])
        self.pill.set_state(state)
        self.spark.set_points(card.get("sparkline") or [])
        state_data = card.get("state") or {}
        latency = state_data.get("last_latency_ms")
        availability = card.get("availability_24h")
        self.meta.setText(
            f"{'—' if latency is None else f'{latency:.1f} ms'}"
            f"   ·   24h {'—' if availability is None else f'{availability}%'}"
            f"   ·   {card.get('vendor', '')}"
        )
        self.update()

    def paintEvent(self, event: Any) -> None:  # noqa: N802 - Qt naming
        super().paintEvent(event)
        painter = QPainter(self)
        painter.fillRect(0, 0, 3, self.height(), QColor(self._accent))
        painter.end()

    def mousePressEvent(self, event: Any) -> None:  # noqa: N802 - Qt naming
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.device_id)
        super().mousePressEvent(event)


class StatCard(QFrame):
    """A single big number with a caption, for the dashboard header row."""

    def __init__(self, key: str, value: str, detail: str = "", color: str | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("panel")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(2)

        self.key = QLabel(key.upper())
        self.key.setObjectName("dim")
        self.key.setStyleSheet(
            f"color: {COLORS['fg_dimmer']}; font-size: 10px; font-weight: 600; letter-spacing: 1px;"
        )
        layout.addWidget(self.key)

        self.value = QLabel(value)
        self.value.setObjectName("stat" if len(value) <= 6 else "statSmall")
        if color:
            self.value.setStyleSheet(f"color: {color};")
        layout.addWidget(self.value)

        self.detail = QLabel(detail)
        self.detail.setObjectName("dim")
        self.detail.setStyleSheet(f"color: {COLORS['fg_dimmer']}; font-size: 11px;")
        layout.addWidget(self.detail)

    def set_values(self, value: str, detail: str = "") -> None:
        self.value.setText(value)
        self.value.setObjectName("stat" if len(value) <= 6 else "statSmall")
        self.detail.setText(detail)


class EventRow(QFrame):
    """One line in the live event feed."""

    def __init__(self, event: dict[str, Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("panel")
        colour = SEVERITY_COLORS.get(event.get("severity", "info"), COLORS["info"])

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 7, 10, 7)
        layout.setSpacing(10)

        bar = QFrame()
        bar.setFixedWidth(3)
        bar.setStyleSheet(f"background: {colour}; border: 0; border-radius: 1px;")
        layout.addWidget(bar)

        text = QVBoxLayout()
        text.setSpacing(1)
        message = QLabel(event.get("message", ""))
        message.setWordWrap(True)
        text.addWidget(message)

        details = event.get("details") or {}
        if details:
            summary = "   ".join(f"{k}={v}" for k, v in list(details.items())[:5])
            detail_label = QLabel(summary)
            detail_label.setObjectName("mono")
            detail_label.setStyleSheet(f"color: {COLORS['fg_dimmer']}; font-size: 11px;")
            detail_label.setWordWrap(True)
            text.addWidget(detail_label)
        layout.addLayout(text, 1)

        when = QLabel(_relative_time(event.get("ts")))
        when.setObjectName("mono")
        when.setStyleSheet(f"color: {COLORS['fg_dimmer']}; font-size: 11px;")
        layout.addWidget(when)

        if event.get("acknowledged"):
            self.setStyleSheet("QFrame#panel { border-left: 3px solid transparent; }")


def _relative_time(ts: float | None) -> str:
    import time

    if not ts:
        return "—"
    delta = time.time() - ts
    if delta < 60:
        return f"{max(0, int(delta))}s ago"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def wrap_list(widget: T) -> T:
    """Make a list widget wrap long lines instead of clipping them.

    The event feed is prose, not tabular data: a truncated "no reply within 1000 m…"
    is worse than a two-line entry.
    """
    widget.setWordWrap(True)
    widget.setTextElideMode(Qt.TextElideMode.ElideNone)
    widget.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    widget.setResizeMode(QListWidget.ResizeMode.Adjust)
    return widget


def section_label(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("h2")
    return label


def horizontal_rule() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setStyleSheet(f"background: {COLORS['line']}; border: 0; max-height: 1px;")
    return line
