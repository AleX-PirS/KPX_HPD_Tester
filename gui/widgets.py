from __future__ import annotations

from datetime import datetime
from typing import Iterable

from PyQt6.QtCore import Qt, QLocale, pyqtSignal
from PyQt6.QtGui import QColor, QDoubleValidator
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .styles import current_theme_colors


class Card(QFrame):
    def __init__(self, title: str | None = None, parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
        self.layout_ = QVBoxLayout(self)
        self.layout_.setContentsMargins(14, 14, 14, 14)
        self.layout_.setSpacing(10)

        if title:
            label = QLabel(title)
            label.setObjectName("SectionTitle")
            self.layout_.addWidget(label)


class FloatEdit(QLineEdit):
    """Compact scientific-notation float input."""

    def __init__(self, value: float | None = None, placeholder: str = "", parent=None):
        super().__init__(parent)
        validator = QDoubleValidator(self)
        validator.setNotation(QDoubleValidator.Notation.ScientificNotation)
        validator.setLocale(QLocale.c())
        validator.setDecimals(15)
        self.setValidator(validator)
        self.setPlaceholderText(placeholder)
        if value is not None:
            self.set_value(value)

    def value(self, allow_empty: bool = False) -> float | None:
        text = self.text().strip()
        if not text:
            if allow_empty:
                return None
            raise ValueError("Numeric field is empty")
        return float(text.replace(",", "."))

    def set_value(self, value: float):
        self.setText(f"{value:g}")


class OutputStateButton(QPushButton):
    """Button whose color/text reflects one physical generator output state."""

    def __init__(self, output_channel: int, label: str, parent=None):
        super().__init__(parent)
        self.output_channel = output_channel
        self.output_label = label
        self.setMinimumHeight(38)
        self.set_state(None)

    def set_state(self, enabled: bool | None):
        if enabled is None:
            state = "unknown"
            text = "UNKNOWN"
        elif enabled:
            state = "on"
            text = "ON"
        else:
            state = "off"
            text = "OFF"

        self.setProperty("outputState", state)
        self.setText(f"{self.output_label}: {text}")

        style = self.style()
        style.unpolish(self)
        style.polish(self)
        self.update()


class StatusBadge(QLabel):
    def __init__(self, name: str, parent=None):
        super().__init__(parent)
        self.name = name
        self.set_status("off", "Disconnected")

    def set_status(self, state: str, detail: str):
        c = current_theme_colors()
        states = {
            "off": (c["badge_off_fg"], c["badge_off_bg"]),
            "ok": (c["badge_ok_fg"], c["badge_ok_bg"]),
            "busy": (c["badge_busy_fg"], c["badge_busy_bg"]),
            "error": (c["badge_error_fg"], c["badge_error_bg"]),
        }
        fg, bg = states.get(state, states["off"])
        self.setText(f"{self.name}: {detail}")
        self.setStyleSheet(
            f"background:{bg};color:{fg};border:1px solid {fg};"
            "border-radius:7px;padding:5px 9px;font-weight:600;"
        )
        self.setToolTip(detail)


class LogPanel(QFrame):
    collapsed_changed = pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("LogPanel")
        self._collapsed = False

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 10)
        root.setSpacing(6)

        top = QHBoxLayout()
        title = QLabel("Log")
        title.setObjectName("SectionTitle")
        top.addWidget(title)
        top.addStretch(1)

        self.low_level = QCheckBox("Low-level commands")
        self.low_level.setChecked(False)
        top.addWidget(self.low_level)

        self.clear_button = QPushButton("Clear")
        self.clear_button.clicked.connect(lambda: self.view.clear())
        top.addWidget(self.clear_button)

        self.collapse_button = QPushButton("Collapse")
        self.collapse_button.setToolTip("Collapse/expand log panel")
        self.collapse_button.clicked.connect(self.toggle_collapsed)
        top.addWidget(self.collapse_button)
        root.addLayout(top)

        self.view = QTextEdit()
        self.view.setReadOnly(True)
        self.view.setMinimumHeight(90)
        root.addWidget(self.view)

    def toggle_collapsed(self):
        self.set_collapsed(not self._collapsed)

    def set_collapsed(self, collapsed: bool):
        collapsed = bool(collapsed)
        if collapsed == self._collapsed:
            return

        self._collapsed = collapsed
        self.view.setVisible(not collapsed)
        self.low_level.setVisible(not collapsed)
        self.clear_button.setVisible(not collapsed)
        self.collapse_button.setText("Expand" if collapsed else "Collapse")
        self.collapsed_changed.emit(collapsed)

    def append(self, level: str, message: str, low_level: bool = False):
        if low_level and not self.low_level.isChecked():
            return

        timestamp = datetime.now().strftime("%H:%M:%S")
        c = current_theme_colors()
        color = {
            "INFO": c["log_info"],
            "DEBUG": c["debug"],
            "WARNING": c["warning"],
            "ERROR": c["error"],
        }.get(level.upper(), c["log_info"])

        escaped = (
            message.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        self.view.append(
            f'<span style="color:{c["log_time"]}">{timestamp}</span> '
            f'<span style="color:{color};font-weight:600">{level.upper():7s}</span> '
            f'<span style="color:{c["log_message"]}">{escaped}</span>'
        )


class RegisterTable(QTableWidget):
    COL_GROUP = 0
    COL_NAME = 1
    COL_VALUE = 2
    COL_DEFAULT = 3
    COL_WIDTH = 4
    COL_STATUS = 5

    def __init__(self, regs_fields: dict, default_values: dict, parent=None):
        super().__init__(parent)
        self.regs_fields = regs_fields
        self.default_values = default_values
        self.baseline: dict[str, int] = {}
        self.invalid_rows: set[int] = set()
        self._updating = False

        self.setColumnCount(6)
        self.setHorizontalHeaderLabels(
            ["Group", "Name", "Value", "Default", "Width", "Status"]
        )
        self.setAlternatingRowColors(True)
        self.setSortingEnabled(False)
        self.verticalHeader().setVisible(False)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.verticalScrollBar().setSingleStep(24)
        self.setMinimumHeight(280)

        names = list(regs_fields.keys())
        self._data_row_count = len(names)

        # One non-data row is kept at the very bottom as scroll padding.
        # This is intentional: with a horizontal scrollbar and a QSplitter
        # directly below the table, some Windows/Qt DPI combinations can make
        # the final real row sit flush against (or partly under) the lower edge.
        # The spacer gives the viewport extra scroll range, so the last register
        # can always be moved fully above the Log panel while the log is open.
        self._bottom_spacer_row = self._data_row_count
        self.setRowCount(self._data_row_count + 1)

        for row, name in enumerate(names):
            width = sum(fragment[2] for fragment in regs_fields[name])
            group = self._group_for(name)
            default = default_values.get(name, "")

            self._set_readonly(row, self.COL_GROUP, group)
            self._set_readonly(row, self.COL_NAME, name)

            value_item = QTableWidgetItem("")
            if name == "TEST_MUX":
                value_item.setFlags(value_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                value_item.setToolTip("TEST_MUX is controlled by the AMUX selector above.")
            self.setItem(row, self.COL_VALUE, value_item)

            self._set_readonly(row, self.COL_DEFAULT, str(default))
            self._set_readonly(row, self.COL_WIDTH, str(width))
            self._set_readonly(row, self.COL_STATUS, "Unknown")

        # Bottom scroll guard. It is not a register and is excluded from every
        # data-processing loop below. A blank 28 px row is enough to keep the
        # last real setting fully visible above the lower table edge.
        spacer = QTableWidgetItem("")
        spacer.setFlags(Qt.ItemFlag.NoItemFlags)
        self.setItem(self._bottom_spacer_row, 0, spacer)
        self.setSpan(self._bottom_spacer_row, 0, 1, self.columnCount())
        self.setRowHeight(self._bottom_spacer_row, 28)

        self.itemChanged.connect(self._on_item_changed)
        self.resizeColumnsToContents()
        self.horizontalHeader().setStretchLastSection(True)
        self.setColumnWidth(self.COL_NAME, 240)
        self.setColumnWidth(self.COL_VALUE, 110)

    def _set_readonly(self, row: int, column: int, text: str):
        item = QTableWidgetItem(text)
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.setItem(row, column, item)

    @staticmethod
    def _group_for(name: str) -> str:
        # DAC_CSA_VB2 belongs to the shaper controls from the user's workflow
        # perspective, even though the historical signal name keeps the CSA prefix.
        if name in ("DAC_CSA_VB2", "DAC_CSA_VB2_TR"):
            return "Shaper"
        if name.startswith("DAC_CSA_"):
            return "CSA"
        if name.startswith("DAC_SH_"):
            return "Shaper"
        if name.startswith("DAC_CMP_") or name.startswith("TEST_CONF_CMP"):
            return "Comparator"
        if name.startswith("DAC_BUF"):
            return "Buffers"
        if name.startswith("DAC_TST_") or name.startswith("DAC_PFB") or name.startswith("TST_REF"):
            return "Injection"
        if name.startswith("BGR_"):
            return "Bandgap"
        if name.startswith("TEST_CONF_"):
            return "Test config"
        if name == "TEST_MUX":
            return "Test mux"
        return "Other"

    @staticmethod
    def _parse_int(text: str) -> int:
        text = text.strip()
        if not text:
            raise ValueError("empty")
        try:
            return int(text, 0)
        except ValueError:
            return int(text, 10)

    def _on_item_changed(self, item: QTableWidgetItem):
        if self._updating or item.column() != self.COL_VALUE:
            return

        row = item.row()
        name = self.item(row, self.COL_NAME).text()
        if name == "TEST_MUX":
            return

        width = int(self.item(row, self.COL_WIDTH).text())
        status = self.item(row, self.COL_STATUS)

        try:
            value = self._parse_int(item.text())
            max_value = (1 << width) - 1
            if not 0 <= value <= max_value:
                raise ValueError(f"range 0..{max_value}")
        except ValueError:
            self.invalid_rows.add(row)
            c = current_theme_colors()
            status.setText("Invalid")
            status.setForeground(QColor(c["error"]))
            item.setBackground(QColor(c["invalid_bg"]))
            return

        self.invalid_rows.discard(row)
        if name in self.baseline and value == self.baseline[name]:
            c = current_theme_colors()
            status.setText("Synced")
            status.setForeground(QColor(c["ok"]))
            item.setBackground(QColor(c["table"]))
        else:
            c = current_theme_colors()
            status.setText("Modified")
            status.setForeground(QColor(c["warning"]))
            item.setBackground(QColor(c["modified_bg"]))

    def set_values(self, values: dict[str, int]):
        self._updating = True
        self.blockSignals(True)
        try:
            self.baseline = dict(values)
            self.invalid_rows.clear()

            for row in range(self._data_row_count):
                name = self.item(row, self.COL_NAME).text()
                if name not in values:
                    continue
                value_item = self.item(row, self.COL_VALUE)
                c = current_theme_colors()
                value_item.setText(str(values[name]))
                value_item.setBackground(QColor(c["table"]))

                status = self.item(row, self.COL_STATUS)
                status.setText("Synced")
                status.setForeground(QColor(c["ok"]))
        finally:
            self.blockSignals(False)
            self._updating = False

    def set_editable(self, editable: bool):
        """Keep the table scrollable while enabling/disabling only value editing."""
        for row in range(self._data_row_count):
            name = self.item(row, self.COL_NAME).text()
            item = self.item(row, self.COL_VALUE)
            flags = item.flags()
            if editable and name != "TEST_MUX":
                flags |= Qt.ItemFlag.ItemIsEditable
            else:
                flags &= ~Qt.ItemFlag.ItemIsEditable
            item.setFlags(flags)

    def dirty_values(self) -> dict[str, int]:
        if self.invalid_rows:
            raise ValueError("There are invalid register values in the table")

        dirty = {}
        for row in range(self._data_row_count):
            name = self.item(row, self.COL_NAME).text()
            if name == "TEST_MUX":
                continue
            text = self.item(row, self.COL_VALUE).text().strip()
            if not text:
                continue
            value = self._parse_int(text)
            if self.baseline.get(name) != value:
                dirty[name] = value
        return dirty

    def filter_rows(self, search: str = "", group: str = "All"):
        search = search.strip().lower()
        for row in range(self._data_row_count):
            row_group = self.item(row, self.COL_GROUP).text()
            name = self.item(row, self.COL_NAME).text()
            visible = (group == "All" or row_group == group)
            if search:
                visible = visible and (search in name.lower() or search in row_group.lower())
            self.setRowHidden(row, not visible)

    def groups(self) -> list[str]:
        return sorted(
            {self.item(row, self.COL_GROUP).text() for row in range(self._data_row_count)}
        )
