from __future__ import annotations

from PyQt6.QtCore import QPointF, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter, QPen
from PyQt6.QtWidgets import (
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from pixel_matrix import (
    DEFAULT_PIXEL_CONFIG,
    MATRIX_COLS,
    MATRIX_ROWS,
    OWNED_COLUMNS,
    PIXEL_CODEC,
)
from .styles import current_theme_colors
from .widgets import Card


class MatrixMap(QWidget):
    """Lightweight 32x32 matrix view without creating 1024 child widgets."""

    pixel_selected = pyqtSignal(int, int)  # row, col

    def __init__(self, parent=None):
        super().__init__(parent)
        self._selected = (0, min(OWNED_COLUMNS))
        self._state_provider = None
        self._marker_provider = None
        self.setMinimumSize(470, 470)
        self.setMouseTracking(True)
        self.setToolTip(
            "Click an owned pixel (Cols 16..31). Cols 0..15 belong to the other half."
        )

    def set_state_provider(self, provider):
        self._state_provider = provider
        self.update()

    def set_marker_provider(self, provider):
        self._marker_provider = provider
        self.update()

    def set_selected(self, row: int, col: int):
        self._selected = (row, col)
        self.update()

    def _geometry(self):
        left = 34.0
        top = 28.0
        right = 8.0
        bottom = 8.0
        available_w = max(self.width() - left - right, 1.0)
        available_h = max(self.height() - top - bottom, 1.0)
        cell = min(available_w / MATRIX_COLS, available_h / MATRIX_ROWS)
        grid_w = cell * MATRIX_COLS
        grid_h = cell * MATRIX_ROWS
        x0 = left + max((available_w - grid_w) / 2.0, 0.0)
        y0 = top + max((available_h - grid_h) / 2.0, 0.0)
        return x0, y0, cell, grid_w, grid_h

    def paintEvent(self, event):
        super().paintEvent(event)
        c = current_theme_colors()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)

        x0, y0, cell, grid_w, grid_h = self._geometry()
        selected_row, selected_col = self._selected

        colors = {
            "inactive": QColor(c["button_disabled"]),
            "unknown": QColor(c["table_alt"]),
            "local": QColor(c["warning"]),
            "staged": QColor(c["primary"]),
            "written": QColor(c["ok"]),
        }

        for row in range(MATRIX_ROWS):
            for col in range(MATRIX_COLS):
                if col not in OWNED_COLUMNS:
                    state = "inactive"
                elif self._state_provider is None:
                    state = "unknown"
                else:
                    state = self._state_provider(row, col)

                rect = QRectF(x0 + col * cell, y0 + row * cell, cell, cell)
                painter.fillRect(rect, colors.get(state, colors["unknown"]))

        # Grid lines.
        grid_pen = QPen(QColor(c["table_grid"]))
        grid_pen.setWidthF(0.7)
        painter.setPen(grid_pen)
        for index in range(MATRIX_COLS + 1):
            x = x0 + index * cell
            painter.drawLine(QPointF(x, y0), QPointF(x, y0 + grid_h))
        for index in range(MATRIX_ROWS + 1):
            y = y0 + index * cell
            painter.drawLine(QPointF(x0, y), QPointF(x0 + grid_w, y))

        # PX feature markers. T marks PX_TST_EN=1, B marks the active-low
        # buffer setting PX_BUF_NEN=0. These describe the currently edited
        # 32-bit matrix word and are independent of the state color.
        if self._marker_provider is not None and cell >= 6.0:
            painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
            marker_font = QFont(self.font())
            marker_font.setPixelSize(max(5, min(8, int(cell * 0.52))))
            marker_font.setBold(True)
            painter.setFont(marker_font)
            painter.setPen(QColor(c["text_strong"]))
            for row in range(MATRIX_ROWS):
                for col in OWNED_COLUMNS:
                    tst_enabled, buf_enabled_marker = self._marker_provider(row, col)
                    if not tst_enabled and not buf_enabled_marker:
                        continue
                    x = x0 + col * cell
                    y = y0 + row * cell
                    half = cell / 2.0
                    if tst_enabled:
                        painter.drawText(
                            QRectF(x + 0.5, y, half, cell),
                            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                            "T",
                        )
                    if buf_enabled_marker:
                        painter.drawText(
                            QRectF(x + half - 0.5, y, half - 0.5, cell),
                            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                            "B",
                        )
            painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, False)

        # Strong separator between the two physical halves.
        sep_pen = QPen(QColor(c["text_strong"]))
        sep_pen.setWidthF(2.0)
        painter.setPen(sep_pen)
        split_x = x0 + 16 * cell
        painter.drawLine(QPointF(split_x, y0), QPointF(split_x, y0 + grid_h))

        # Selected pixel outline.
        if 0 <= selected_row < MATRIX_ROWS and selected_col in OWNED_COLUMNS:
            selected_rect = QRectF(
                x0 + selected_col * cell + 1.0,
                y0 + selected_row * cell + 1.0,
                max(cell - 2.0, 1.0),
                max(cell - 2.0, 1.0),
            )
            select_pen = QPen(QColor(c["text_strong"]))
            select_pen.setWidthF(max(2.0, min(cell * 0.18, 4.0)))
            painter.setPen(select_pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(selected_rect)

        # Sparse axis labels keep the map readable.
        painter.setPen(QColor(c["muted"]))
        font = QFont(self.font())
        font.setPointSizeF(max(7.0, min(9.0, cell * 0.55)))
        painter.setFont(font)
        for col in (0, 8, 15, 16, 23, 31):
            center_x = x0 + (col + 0.5) * cell
            painter.drawText(
                QRectF(center_x - 18, y0 - 24, 36, 18),
                Qt.AlignmentFlag.AlignCenter,
                str(col),
            )
        for row in (0, 8, 16, 24, 31):
            center_y = y0 + (row + 0.5) * cell
            painter.drawText(
                QRectF(x0 - 32, center_y - 9, 27, 18),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                str(row),
            )

        painter.end()

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return super().mousePressEvent(event)

        x0, y0, cell, grid_w, grid_h = self._geometry()
        x = event.position().x()
        y = event.position().y()
        if not (x0 <= x < x0 + grid_w and y0 <= y < y0 + grid_h):
            return

        col = int((x - x0) // cell)
        row = int((y - y0) // cell)
        if col not in OWNED_COLUMNS:
            return

        self.pixel_selected.emit(row, col)


class MatrixPage(QWidget):
    """GUI editor for project-owned pixels Col=16..31 of the 32x32 matrix."""

    def __init__(self, parent=None):
        super().__init__(parent)

        self._values: dict[tuple[int, int], int] = {}
        self._upo_values: dict[tuple[int, int], int] = {}
        self._chip_values: dict[tuple[int, int], int] = {}
        self._local_dirty: set[tuple[int, int]] = set()
        self._local_baseline: dict[tuple[int, int], int] = {}
        self._updating_editor = False
        self._connected = False

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 20)
        root.setSpacing(12)

        title = QLabel("Pixel matrix")
        title.setObjectName("Title")
        root.addWidget(title)

        subtitle = QLabel(
            "32x32 pixel configuration. This project controls only Col=16..31. "
            "SET_PIXEL_CFG first changes MGPDLab virtual memory; WRITE_TO_CHIP is a separate commit."
        )
        subtitle.setObjectName("Muted")
        subtitle.setWordWrap(True)
        root.addWidget(subtitle)

        warning = QLabel(
            "Important: after a pixel is changed through SET_PIXEL_CFG, that pixel is no longer "
            "controlled by the MGPDLab GUI until UPO is restarted. Pixel readback is not provided "
            "by the supplied protocol, so GUI states below describe only operations performed in this session."
        )
        warning.setWordWrap(True)
        warning.setObjectName("MatrixWarning")
        root.addWidget(warning)

        body = QGridLayout()
        body.setHorizontalSpacing(12)
        body.setVerticalSpacing(12)
        root.addLayout(body, 1)

        # ---------------------------------------------------------------- map
        map_card = Card("Matrix map")
        coord_row = QHBoxLayout()
        self.row_spin = QSpinBox()
        self.row_spin.setRange(0, MATRIX_ROWS - 1)
        self.row_spin.setValue(0)
        self.col_spin = QSpinBox()
        self.col_spin.setRange(min(OWNED_COLUMNS), max(OWNED_COLUMNS))
        self.col_spin.setValue(min(OWNED_COLUMNS))
        self.coord_label = QLabel("Col=16 Row=0")
        self.coord_label.setObjectName("SectionTitle")
        coord_row.addWidget(QLabel("Row"))
        coord_row.addWidget(self.row_spin)
        coord_row.addWidget(QLabel("Col"))
        coord_row.addWidget(self.col_spin)
        coord_row.addSpacing(10)
        coord_row.addWidget(self.coord_label)
        coord_row.addStretch(1)
        map_card.layout_.addLayout(coord_row)

        self.matrix_map = MatrixMap()
        self.matrix_map.set_state_provider(self.pixel_state)
        self.matrix_map.set_marker_provider(self.pixel_markers)
        map_card.layout_.addWidget(self.matrix_map, 1)

        legend = QHBoxLayout()
        legend.addWidget(QLabel("Cols 0..15: locked"))
        legend.addWidget(QLabel("Unknown"))
        self.legend_local = QLabel("Local edit")
        self.legend_local.setObjectName("MatrixLegendLocal")
        legend.addWidget(self.legend_local)
        self.legend_staged = QLabel("Staged in UPO")
        self.legend_staged.setObjectName("MatrixLegendStaged")
        legend.addWidget(self.legend_staged)
        self.legend_written = QLabel("Written to chip")
        self.legend_written.setObjectName("MatrixLegendWritten")
        legend.addWidget(self.legend_written)
        marker_legend = QLabel("T: PX_TST_EN=1   B: PX_BUF_NEN=0")
        marker_legend.setObjectName("Muted")
        legend.addWidget(marker_legend)
        legend.addStretch(1)
        map_card.layout_.addLayout(legend)
        body.addWidget(map_card, 0, 0, 2, 1)

        # ------------------------------------------------------------- fields
        field_card = Card("32-bit PX configuration")
        fields_grid = QGridLayout()
        fields_grid.setHorizontalSpacing(16)
        field_card.layout_.addLayout(fields_grid)

        self.field_edits: dict[str, QSpinBox] = {}
        field_names = list(PIXEL_CODEC.field_names)
        split = (len(field_names) + 1) // 2
        for index, name in enumerate(field_names):
            column_group = 0 if index < split else 1
            row_index = index if index < split else index - split
            form = None
            key = f"form_{column_group}"
            if not hasattr(self, key):
                form = QFormLayout()
                form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
                setattr(self, key, form)
                fields_grid.addLayout(form, 0, column_group)
            else:
                form = getattr(self, key)

            width = PIXEL_CODEC.width(name)
            edit = QSpinBox()
            edit.setRange(0, (1 << width) - 1)
            edit.setToolTip(f"{name}: {width} bit(s), range 0..{(1 << width) - 1}")
            edit.valueChanged.connect(self._editor_changed)
            form.addRow(f"{name} [{width}b]", edit)
            self.field_edits[name] = edit

        raw_row = QHBoxLayout()
        raw_row.addWidget(QLabel("Raw 32-bit value"))
        self.raw_value = QLineEdit("0x00000000")
        self.raw_value.setPlaceholderText("0x00000000")
        self.raw_value.setMaxLength(10)
        self.raw_value.setToolTip(
            "Enter the complete 32-bit PX word in hexadecimal, for example 0x1234ABCD."
        )
        self.send_raw = QPushButton("Send RAW to selected")
        self.send_raw.setToolTip(
            "Stage this exact 32-bit word for the selected pixel in MGPDLab virtual memory."
        )
        raw_row.addWidget(self.raw_value, 1)
        raw_row.addWidget(self.send_raw)
        field_card.layout_.addLayout(raw_row)

        self.pixel_status = QLabel("Unknown - no pixel readback in protocol")
        self.pixel_status.setObjectName("Muted")
        self.pixel_status.setWordWrap(True)
        field_card.layout_.addWidget(self.pixel_status)

        editor_buttons = QHBoxLayout()
        self.load_defaults = QPushButton("Load PX defaults to editor")
        self.clear_local = QPushButton("Clear local edits")
        self.clear_local.setToolTip(
            "Discard all PX edits that have not been sent to MGPDLab virtual memory."
        )
        self.stage_selected = QPushButton("Update selected in UPO")
        self.stage_selected.setObjectName("PrimaryButton")
        self.stage_local = QPushButton("Update all local edits in UPO")
        self.stage_local.setToolTip(
            "Stage every pixel currently marked Local edit, keeping each pixel's own PX value."
        )
        editor_buttons.addWidget(self.load_defaults)
        editor_buttons.addWidget(self.clear_local)
        editor_buttons.addWidget(self.stage_selected)
        editor_buttons.addWidget(self.stage_local)
        field_card.layout_.addLayout(editor_buttons)
        body.addWidget(field_card, 0, 1)

        # ----------------------------------------------------------- bulk/commit
        operations = Card("Matrix operations")
        note = QLabel(
            "Bulk update sends the current editor value to all 512 owned pixels "
            "(Rows 0..31, Cols 16..31). It does not touch Cols 0..15 in MGPDLab virtual memory."
        )
        note.setObjectName("Muted")
        note.setWordWrap(True)
        operations.layout_.addWidget(note)

        op_buttons = QHBoxLayout()
        self.stage_all = QPushButton("Update owned half in UPO")
        self.write_chip = QPushButton("Write matrix to chip")
        self.write_chip.setObjectName("PrimaryButton")
        op_buttons.addWidget(self.stage_all)
        op_buttons.addWidget(self.write_chip)
        operations.layout_.addLayout(op_buttons)

        self.progress = QProgressBar()
        self.progress.setRange(0, MATRIX_ROWS * len(OWNED_COLUMNS))
        self.progress.setValue(0)
        self.progress.setFormat("Ready")
        operations.layout_.addWidget(self.progress)

        commit_note = QLabel(
            "Protocol limitation: WRITE_TO_CHIP is a full-matrix command. Our software only modifies "
            "Cols 16..31 before that command; the other half remains whatever MGPDLab already stores."
        )
        commit_note.setObjectName("Muted")
        commit_note.setWordWrap(True)
        operations.layout_.addWidget(commit_note)
        body.addWidget(operations, 1, 1)

        body.setColumnStretch(0, 3)
        body.setColumnStretch(1, 2)

        self.row_spin.valueChanged.connect(self._coordinate_spin_changed)
        self.col_spin.valueChanged.connect(self._coordinate_spin_changed)
        self.matrix_map.pixel_selected.connect(self.select_pixel)
        self.load_defaults.clicked.connect(self.load_defaults_to_editor)
        self.clear_local.clicked.connect(self.clear_local_edits)

        self.reset_session()
        self.set_connected(False)

    # ---------------------------------------------------------------- state

    def set_connected(self, connected: bool):
        self._connected = bool(connected)
        for widget in (
            self.stage_selected,
            self.stage_local,
            self.send_raw,
            self.stage_all,
            self.write_chip,
        ):
            widget.setEnabled(self._connected)

    def reset_session(self):
        self._values = {
            (row, col): DEFAULT_PIXEL_CONFIG
            for row in range(MATRIX_ROWS)
            for col in OWNED_COLUMNS
        }
        self._upo_values.clear()
        self._chip_values.clear()
        self._local_dirty.clear()
        self._local_baseline = dict(self._values)
        self.progress.setValue(0)
        self.progress.setFormat("Ready")
        self.select_pixel(0, min(OWNED_COLUMNS))
        self.matrix_map.update()

    def current_coordinate(self) -> tuple[int, int]:
        return self.row_spin.value(), self.col_spin.value()

    def current_raw_value(self) -> int:
        values = {name: edit.value() for name, edit in self.field_edits.items()}
        return PIXEL_CODEC.pack(values)

    def raw_input_value(self) -> int:
        text = self.raw_value.text().strip().replace("_", "")
        if not text:
            raise ValueError("RAW pixel configuration is empty")
        try:
            if text.lower().startswith("0x"):
                raw = int(text, 16)
            else:
                raw = int(text, 16)
        except ValueError as error:
            raise ValueError(
                "RAW pixel configuration must be a 32-bit hexadecimal value "
                "such as 0x1234ABCD"
            ) from error
        PIXEL_CODEC.validate_raw(raw)
        return raw

    def local_edits(self) -> dict[tuple[int, int], int]:
        return {
            coord: self._values[coord]
            for coord in sorted(self._local_dirty)
        }

    def pixel_markers(self, row: int, col: int) -> tuple[bool, bool]:
        if col not in OWNED_COLUMNS:
            return False, False
        raw = self._values.get((row, col), DEFAULT_PIXEL_CONFIG)
        tst = PIXEL_CODEC.extract(raw, "PX_TST_EN") == 1
        # BUF_NEN is active-low, therefore B is shown for value 0.
        buf = PIXEL_CODEC.extract(raw, "PX_BUF_NEN") == 0
        return tst, buf

    def pixel_state(self, row: int, col: int) -> str:
        coord = (row, col)
        if col not in OWNED_COLUMNS:
            return "inactive"
        if coord in self._local_dirty:
            return "local"
        if coord in self._upo_values:
            if self._chip_values.get(coord) == self._upo_values[coord]:
                return "written"
            return "staged"
        return "unknown"

    def _status_text(self, row: int, col: int) -> str:
        state = self.pixel_state(row, col)
        if state == "local":
            return "Local edit - not sent to MGPDLab virtual memory"
        if state == "staged":
            return "Staged in MGPDLab virtual memory - not yet committed to chip"
        if state == "written":
            return "Written to chip in this GUI session"
        return "Unknown - protocol provides no per-pixel readback"

    # ------------------------------------------------------------- selection

    def _coordinate_spin_changed(self):
        self.select_pixel(self.row_spin.value(), self.col_spin.value())

    def select_pixel(self, row: int, col: int):
        if col not in OWNED_COLUMNS or not 0 <= row < MATRIX_ROWS:
            return

        self.row_spin.blockSignals(True)
        self.col_spin.blockSignals(True)
        self.row_spin.setValue(row)
        self.col_spin.setValue(col)
        self.row_spin.blockSignals(False)
        self.col_spin.blockSignals(False)

        self.coord_label.setText(f"Col={col} Row={row}")
        self.matrix_map.set_selected(row, col)

        raw = self._values[(row, col)]
        fields = PIXEL_CODEC.unpack(raw)
        self._updating_editor = True
        try:
            for name, value in fields.items():
                self.field_edits[name].setValue(value)
        finally:
            self._updating_editor = False
        self._refresh_selected_status()

    def _editor_changed(self):
        if self._updating_editor:
            return
        coord = self.current_coordinate()
        raw = self.current_raw_value()
        self._values[coord] = raw
        baseline = self._local_baseline.get(coord, DEFAULT_PIXEL_CONFIG)
        if baseline == raw:
            self._local_dirty.discard(coord)
        else:
            self._local_dirty.add(coord)
        self._refresh_selected_status()
        self.matrix_map.update()

    def load_defaults_to_editor(self):
        fields = PIXEL_CODEC.default_fields()
        self._updating_editor = True
        try:
            for name, value in fields.items():
                self.field_edits[name].setValue(value)
        finally:
            self._updating_editor = False
        self._editor_changed()

    def clear_local_edits(self):
        """Discard every PX edit not yet staged to MGPDLab virtual memory."""
        for coord in tuple(self._local_dirty):
            self._values[coord] = self._local_baseline.get(
                coord, DEFAULT_PIXEL_CONFIG
            )

        self._local_dirty.clear()
        # Also refresh the RAW editor so an unsent manually typed word does not
        # remain visible after Clear.
        self.select_pixel(*self.current_coordinate())
        self.matrix_map.update()

    def _refresh_selected_status(self):
        row, col = self.current_coordinate()
        raw = self._values[(row, col)]
        self.raw_value.setText(f"0x{raw:08X}")
        self.pixel_status.setText(self._status_text(row, col))

    # ----------------------------------------------------------- worker results

    def set_busy(self, busy: bool):
        busy = bool(busy)
        enabled = self._connected and not busy
        self.stage_selected.setEnabled(enabled)
        self.stage_local.setEnabled(enabled)
        self.send_raw.setEnabled(enabled)
        self.stage_all.setEnabled(enabled)
        self.write_chip.setEnabled(enabled)
        self.row_spin.setEnabled(not busy)
        self.col_spin.setEnabled(not busy)
        for edit in self.field_edits.values():
            edit.setEnabled(not busy)
        self.load_defaults.setEnabled(not busy)
        self.clear_local.setEnabled(not busy)
        self.raw_value.setEnabled(not busy)

    def apply_selected_stage_result(self, result: dict):
        row = int(result["row"])
        col = int(result["col"])
        value = int(result["value"])
        coord = (row, col)
        self._values[coord] = value
        self._upo_values[coord] = value
        self._local_baseline[coord] = value
        self._local_dirty.discard(coord)
        if self.current_coordinate() == coord:
            self.select_pixel(row, col)
        else:
            self._refresh_selected_status()
        self.matrix_map.update()

    def apply_local_stage_result(self, result: dict):
        pixels = result.get("pixels", [])
        for item in pixels:
            row = int(item["row"])
            col = int(item["col"])
            value = int(item["value"])
            coord = (row, col)
            self._values[coord] = value
            self._upo_values[coord] = value
            self._local_baseline[coord] = value
            self._local_dirty.discard(coord)
        count = int(result.get("count", len(pixels)))
        self.progress.setValue(count)
        self.progress.setFormat(f"Staged {count} local edit(s) in UPO")
        self.select_pixel(*self.current_coordinate())
        self.matrix_map.update()

    def apply_bulk_stage_result(self, result: dict):
        value = int(result["value"])
        for row in range(MATRIX_ROWS):
            for col in OWNED_COLUMNS:
                coord = (row, col)
                self._values[coord] = value
                self._upo_values[coord] = value
                self._local_baseline[coord] = value
                self._local_dirty.discard(coord)
        self.progress.setValue(self.progress.maximum())
        self.progress.setFormat(f"Staged {result['count']} / {result['count']} pixels in UPO")
        self.select_pixel(*self.current_coordinate())
        self.matrix_map.update()

    def apply_commit_result(self, ok: bool):
        if not ok:
            return
        for coord, value in self._upo_values.items():
            self._chip_values[coord] = value
        self.progress.setFormat("Matrix WRITE_TO_CHIP accepted by UPO")
        self._refresh_selected_status()
        self.matrix_map.update()

    def set_matrix_progress(self, current: int, total: int, row: int, col: int):
        self.progress.setRange(0, total)
        self.progress.setValue(current)
        self.progress.setFormat(
            f"Staging {current}/{total} - Col={col} Row={row}"
        )
