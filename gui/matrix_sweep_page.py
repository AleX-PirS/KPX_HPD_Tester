from __future__ import annotations

import shutil
from pathlib import Path

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
from PyQt6.QtCore import QPointF, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter, QPen
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from pixel_matrix import (
    MATRIX_ROWS,
    OWNED_COLUMNS,
    PIXEL_CODEC,
)
from .styles import current_theme_colors
from .widgets import Card, FloatEdit


class PixelSettingsEditor(Card):
    """Compact editor for one complete 32-bit PX configuration word."""

    def __init__(self, title: str, parent=None):
        super().__init__(title, parent)
        self.field_edits: dict[str, QSpinBox] = {}

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(6)

        names = list(PIXEL_CODEC.field_names)
        split = (len(names) + 1) // 2
        for index, name in enumerate(names):
            column_group = 0 if index < split else 1
            row = index if column_group == 0 else index - split
            label_col = column_group * 2
            edit_col = label_col + 1

            width = PIXEL_CODEC.width(name)
            edit = QSpinBox()
            edit.setRange(0, (1 << width) - 1)
            edit.setValue(PIXEL_CODEC.default_fields()[name])
            edit.setToolTip(f"{name}: {width} bit(s)")
            self.field_edits[name] = edit

            display_name = name[3:] if name.startswith("PX_") else name
            grid.addWidget(QLabel(display_name), row, label_col)
            grid.addWidget(edit, row, edit_col)

        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        self.layout_.addLayout(grid)

        footer = QHBoxLayout()
        self.raw_label = QLabel()
        self.raw_label.setObjectName("Muted")
        self.load_default = QPushButton("Load default")
        self.load_default.setObjectName("NeutralButton")
        footer.addWidget(self.raw_label)
        footer.addStretch(1)
        footer.addWidget(self.load_default)
        self.layout_.addLayout(footer)

        for edit in self.field_edits.values():
            edit.valueChanged.connect(self._refresh_raw)
        self.load_default.clicked.connect(self.load_defaults)
        self._refresh_raw()

    def raw_value(self) -> int:
        return PIXEL_CODEC.pack(
            {name: edit.value() for name, edit in self.field_edits.items()}
        )

    def load_defaults(self):
        defaults = PIXEL_CODEC.default_fields()
        for name, edit in self.field_edits.items():
            edit.setValue(defaults[name])
        self._refresh_raw()

    def _refresh_raw(self):
        self.raw_label.setText(f"RAW: 0x{self.raw_value():08X}")

    def set_editor_enabled(self, enabled: bool):
        for edit in self.field_edits.values():
            edit.setEnabled(enabled)
        self.load_default.setEnabled(enabled)


class SweepMatrixMap(QWidget):
    """Selectable view of only the project-owned 16x32 matrix half.

    Selection rules intentionally mimic ordinary desktop selection:
      * click: replace selection with one pixel;
      * Ctrl+click: toggle one pixel;
      * Shift+click: rectangle from anchor to clicked pixel;
      * Ctrl+Shift+click: add that rectangle to the existing selection.

    An empty explicit selection means ALL owned pixels will be swept.
    """

    selection_changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._selection: set[tuple[int, int]] = set()
        self._anchor: tuple[int, int] | None = None
        self.setMinimumSize(360, 500)
        self.setMouseTracking(True)
        self.setToolTip(
            "Click = one pixel; Ctrl+click = add/remove; Shift+click = rectangular range; "
            "Ctrl+Shift = add rectangle. Empty selection means all 512 owned pixels."
        )

    def explicit_selection(self) -> tuple[tuple[int, int], ...]:
        return tuple(sorted(self._selection))

    def effective_selection(self) -> tuple[tuple[int, int], ...]:
        if self._selection:
            return tuple(sorted(self._selection))
        return tuple(
            (row, col)
            for row in range(MATRIX_ROWS)
            for col in OWNED_COLUMNS
        )

    def clear_selection(self):
        self._selection.clear()
        self._anchor = None
        self.update()
        self.selection_changed.emit()

    @staticmethod
    def _visual_row(row: int) -> int:
        """Display Row=0 at the bottom and Row=31 at the top."""
        return MATRIX_ROWS - 1 - int(row)

    @staticmethod
    def _logical_row(visual_row: int) -> int:
        return MATRIX_ROWS - 1 - int(visual_row)

    def _geometry(self):
        left = 40.0
        top = 28.0
        right = 8.0
        bottom = 10.0
        columns = len(OWNED_COLUMNS)
        available_w = max(self.width() - left - right, 1.0)
        available_h = max(self.height() - top - bottom, 1.0)
        cell = min(available_w / columns, available_h / MATRIX_ROWS)
        grid_w = cell * columns
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
        normal = QColor(c["table_alt"])
        selected = QColor(c["selection"])

        for row in range(MATRIX_ROWS):
            for local_col, col in enumerate(OWNED_COLUMNS):
                rect = QRectF(
                    x0 + local_col * cell,
                    y0 + self._visual_row(row) * cell,
                    cell,
                    cell,
                )
                painter.fillRect(
                    rect,
                    selected if (row, col) in self._selection else normal,
                )

        grid_pen = QPen(QColor(c["table_grid"]))
        grid_pen.setWidthF(0.7)
        painter.setPen(grid_pen)
        for index in range(len(OWNED_COLUMNS) + 1):
            x = x0 + index * cell
            painter.drawLine(QPointF(x, y0), QPointF(x, y0 + grid_h))
        for index in range(MATRIX_ROWS + 1):
            y = y0 + index * cell
            painter.drawLine(QPointF(x0, y), QPointF(x0 + grid_w, y))

        if self._anchor is not None:
            row, col = self._anchor
            if col in OWNED_COLUMNS:
                local_col = col - min(OWNED_COLUMNS)
                rect = QRectF(
                    x0 + local_col * cell + 1,
                    y0 + self._visual_row(row) * cell + 1,
                    max(cell - 2, 1),
                    max(cell - 2, 1),
                )
                pen = QPen(QColor(c["text_strong"]))
                pen.setWidthF(max(1.5, min(cell * 0.16, 3.0)))
                painter.setPen(pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRect(rect)

        painter.setPen(QColor(c["muted"]))
        font = QFont(self.font())
        font.setPointSizeF(max(7.0, min(9.0, cell * 0.55)))
        painter.setFont(font)
        column_tick_indices = (0, 4, 8, 12, len(OWNED_COLUMNS) - 1)
        for local_col in column_tick_indices:
            col = OWNED_COLUMNS[local_col]
            center_x = x0 + (local_col + 0.5) * cell
            painter.drawText(
                QRectF(center_x - 18, y0 - 24, 36, 18),
                Qt.AlignmentFlag.AlignCenter,
                str(col),
            )
        for row in (0, 8, 16, 24, 31):
            center_y = y0 + (self._visual_row(row) + 0.5) * cell
            painter.drawText(
                QRectF(x0 - 34, center_y - 9, 29, 18),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                str(row),
            )
        painter.end()

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton or not self.isEnabled():
            return super().mousePressEvent(event)

        x0, y0, cell, grid_w, grid_h = self._geometry()
        x = event.position().x()
        y = event.position().y()
        if not (x0 <= x < x0 + grid_w and y0 <= y < y0 + grid_h):
            return

        local_col = int((x - x0) // cell)
        visual_row = int((y - y0) // cell)
        row = self._logical_row(visual_row)
        col = min(OWNED_COLUMNS) + local_col
        if col not in OWNED_COLUMNS or not 0 <= row < MATRIX_ROWS:
            return

        clicked = (row, col)
        modifiers = event.modifiers()
        ctrl = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
        shift = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)

        if shift and self._anchor is not None:
            anchor_row, anchor_col = self._anchor
            r0, r1 = sorted((anchor_row, row))
            c0, c1 = sorted((anchor_col, col))
            rectangle = {
                (r, c)
                for r in range(r0, r1 + 1)
                for c in range(c0, c1 + 1)
                if c in OWNED_COLUMNS
            }
            if ctrl:
                self._selection.update(rectangle)
            else:
                self._selection = rectangle
        elif ctrl:
            if clicked in self._selection:
                self._selection.remove(clicked)
            else:
                self._selection.add(clicked)
            self._anchor = clicked
        else:
            self._selection = {clicked}
            self._anchor = clicked

        self.update()
        self.selection_changed.emit()


class MatrixSweepPage(QWidget):
    """Sweep individual matrix pixels while all other owned pixels stay global."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._chip_connected = False
        self._osc_connected = False
        self._busy = False
        self.current_combined_csv_path: Path | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(12)

        title = QLabel("Matrix sweep")
        title.setObjectName("Title")
        subtitle = QLabel(
            "Sweep selected pixels in Col=16..31. During each capture exactly one pixel uses "
            "Sweep settings while every other owned pixel uses Global settings. Empty selection means all pixels."
        )
        subtitle.setObjectName("Muted")
        subtitle.setWordWrap(True)
        root.addWidget(title)
        root.addWidget(subtitle)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.setHandleWidth(5)
        root.addWidget(split, 1)

        # --------------------------------------------------------------- controls
        controls = QWidget()
        controls.setMinimumWidth(430)
        controls.setMaximumWidth(620)
        controls_layout = QVBoxLayout(controls)
        controls_layout.setContentsMargins(0, 0, 8, 0)
        controls_layout.setSpacing(12)

        self.global_settings = PixelSettingsEditor("Global settings")
        self.sweep_settings = PixelSettingsEditor("Sweep settings")
        controls_layout.addWidget(self.global_settings)
        controls_layout.addWidget(self.sweep_settings)

        acquisition = Card("Acquisition")
        acq_grid = QGridLayout()
        self.scope_channel = QComboBox()
        self.scope_channel.addItems(["1", "2", "3", "4"])
        self.scope_channel.setCurrentText("1")
        self.delay_s = FloatEdit(0.1)
        self.delay_s.setPlaceholderText("0.1")
        self.fclk_off_capture = QCheckBox("FCLK OFF during capture")
        self.fclk_off_capture.setChecked(False)
        self.fclk_off_capture.setToolTip(
            "After the current pixel configuration is written to the chip, set FCLK=0, "
            "wait the settling delay, capture the waveform, then restore the previous known FCLK. "
            "If FCLK was unknown, 100 MHz is established as the restore value."
        )
        acq_grid.addWidget(QLabel("Oscilloscope channel"), 0, 0)
        acq_grid.addWidget(self.scope_channel, 0, 1)
        acq_grid.addWidget(QLabel("Settling delay, s"), 1, 0)
        acq_grid.addWidget(self.delay_s, 1, 1)
        acquisition.layout_.addLayout(acq_grid)
        acquisition.layout_.addWidget(self.fclk_off_capture)
        controls_layout.addWidget(acquisition)

        selector = Card("Pixel selection")
        selection_note = QLabel(
            "Click: select one. Ctrl+click: add/remove. Shift+click: rectangle from the last anchor. "
            "Ctrl+Shift: add a rectangle. No explicit selection = sweep all 512 pixels."
        )
        selection_note.setObjectName("Muted")
        selection_note.setWordWrap(True)
        selector.layout_.addWidget(selection_note)

        selection_row = QHBoxLayout()
        self.selection_status = QLabel()
        self.selection_status.setObjectName("SectionTitle")
        self.clear_selection = QPushButton("Clear selection (All)")
        self.clear_selection.setObjectName("NeutralButton")
        selection_row.addWidget(self.selection_status)
        selection_row.addStretch(1)
        selection_row.addWidget(self.clear_selection)
        selector.layout_.addLayout(selection_row)

        self.matrix_map = SweepMatrixMap()
        selector.layout_.addWidget(self.matrix_map, 1)
        controls_layout.addWidget(selector)

        self.start_sweep = QPushButton("Start matrix sweep")
        self.start_sweep.setObjectName("PrimaryButton")
        controls_layout.addWidget(self.start_sweep)

        self.progress_text = QLabel("Ready")
        self.progress_text.setObjectName("Muted")
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setTextVisible(True)
        controls_layout.addWidget(self.progress_text)
        controls_layout.addWidget(self.progress)
        controls_layout.addStretch(1)

        controls_scroll = QScrollArea()
        controls_scroll.setWidgetResizable(True)
        controls_scroll.setFrameShape(QFrame.Shape.NoFrame)
        controls_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        controls_scroll.setWidget(controls)
        controls_scroll.setMinimumWidth(440)
        controls_scroll.setMaximumWidth(640)
        split.addWidget(controls_scroll)

        # ---------------------------------------------------------------- preview
        preview_card = Card("Preview")
        self.figure = Figure(figsize=(8, 5), tight_layout=False)
        self.canvas = FigureCanvas(self.figure)
        self.canvas.setObjectName("PlotCanvas")
        self.canvas.setMinimumSize(500, 320)
        preview_card.layout_.addWidget(self.canvas, 1)

        save_row = QHBoxLayout()
        self.save_figure = QPushButton("Save figure...")
        self.save_csv = QPushButton("Save CSV...")
        self.save_figure.setEnabled(False)
        self.save_csv.setEnabled(False)
        save_row.addWidget(self.save_figure)
        save_row.addWidget(self.save_csv)
        save_row.addStretch(1)
        preview_card.layout_.addLayout(save_row)
        split.addWidget(preview_card)

        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([520, 800])

        self.clear_selection.clicked.connect(self.matrix_map.clear_selection)
        self.matrix_map.selection_changed.connect(self._refresh_selection_status)
        self._refresh_selection_status()
        self._draw_placeholder()
        self._refresh_enabled_state()

    def _draw_placeholder(self):
        c = current_theme_colors()
        self.figure.clear()
        self.figure.set_facecolor(c["card"])
        ax = self.figure.add_subplot(111)
        ax.set_facecolor(c["table"])
        ax.text(
            0.5,
            0.5,
            "Configure Global/Sweep settings and start the matrix sweep.",
            ha="center",
            va="center",
            transform=ax.transAxes,
            color=c["muted"],
        )
        ax.set_xticks([])
        ax.set_yticks([])
        for spine in ax.spines.values():
            spine.set_color(c["input_border"])
        self.canvas.draw()

    # ---------------------------------------------------------------- settings

    def selected_scope_channel(self) -> int:
        return int(self.scope_channel.currentText())

    def settling_delay_s(self) -> float:
        value = self.delay_s.value()
        if value is None or value < 0:
            raise ValueError("Matrix settling delay must be >= 0 s")
        return value

    def disable_fclk_during_capture(self) -> bool:
        return self.fclk_off_capture.isChecked()

    def sweep_pixels(self) -> tuple[tuple[int, int], ...]:
        return self.matrix_map.effective_selection()

    def global_raw(self) -> int:
        return self.global_settings.raw_value()

    def sweep_raw(self) -> int:
        return self.sweep_settings.raw_value()

    def _refresh_selection_status(self):
        explicit = self.matrix_map.explicit_selection()
        if explicit:
            self.selection_status.setText(f"Selected: {len(explicit)}")
        else:
            self.selection_status.setText("Selected: ALL 512")

    # --------------------------------------------------------------- connection

    def set_chip_connected(self, connected: bool):
        self._chip_connected = bool(connected)
        self._refresh_enabled_state()

    def set_osc_connected(self, connected: bool):
        self._osc_connected = bool(connected)
        self._refresh_enabled_state()

    def _refresh_enabled_state(self):
        self.start_sweep.setEnabled(
            self._chip_connected and self._osc_connected and not self._busy
        )

    # ---------------------------------------------------------------- progress

    def set_sweep_busy(self, busy: bool):
        self._busy = bool(busy)
        enabled = not self._busy
        self.global_settings.set_editor_enabled(enabled)
        self.sweep_settings.set_editor_enabled(enabled)
        self.scope_channel.setEnabled(enabled)
        self.delay_s.setEnabled(enabled)
        self.fclk_off_capture.setEnabled(enabled)
        self.matrix_map.setEnabled(enabled)
        self.clear_selection.setEnabled(enabled)

        if busy:
            self.progress.setRange(0, 0)
            self.progress_text.setText("Preparing matrix sweep...")
            self.save_figure.setEnabled(False)
            self.save_csv.setEnabled(False)
        else:
            if self.progress.maximum() == 0:
                self.progress.setRange(0, 1)
                self.progress.setValue(0)
            if self.progress_text.text().startswith("Preparing"):
                self.progress_text.setText("Ready")
        self._refresh_enabled_state()

    def set_sweep_progress(self, current: int, total: int, row: int, col: int):
        total = max(int(total), 1)
        current = max(0, min(int(current), total))
        self.progress.setRange(0, total)
        self.progress.setValue(current)
        self.progress.setFormat(f"{current} / {total}")
        self.progress_text.setText(f"Capturing Col={col} Row={row}")

    # ---------------------------------------------------------------- preview

    def show_result(self, result: dict):
        pixels = list(result["pixels"])
        time_s = result["time_s"]
        waveforms = result["waveforms"]

        c = current_theme_colors()
        self.figure.clear()
        self.figure.set_facecolor(c["card"])
        ax = self.figure.add_subplot(111)
        ax.set_facecolor(c["table"])

        for row, col in pixels:
            key = (row, col)
            ax.plot(
                time_s,
                waveforms[key],
                linewidth=0.9,
                label=f"C{col} R{row}",
            )

        ax.set_xlabel("Time, s", color=c["text"])
        ax.set_ylabel("Voltage, V", color=c["text"])
        ax.set_title(f"Matrix sweep - {len(pixels)} pixel(s)", color=c["text_strong"])
        ax.grid(True, alpha=0.25)
        ax.tick_params(colors=c["text"])
        for spine in ax.spines.values():
            spine.set_color(c["input_border"])

        if 0 < len(pixels) <= 32:
            legend_columns = 2 if len(pixels) > 16 else 1
            legend = ax.legend(
                loc="upper left",
                bbox_to_anchor=(1.01, 1.0),
                borderaxespad=0.0,
                fontsize=7,
                ncol=legend_columns,
            )
            frame = legend.get_frame()
            frame.set_facecolor(c["card"])
            frame.set_edgecolor(c["input_border"])
            for text in legend.get_texts():
                text.set_color(c["text"])
            self.figure.subplots_adjust(right=0.72 if legend_columns == 2 else 0.80)
        else:
            self.figure.subplots_adjust(right=0.96)

        self.figure.subplots_adjust(left=0.10, bottom=0.12, top=0.93)
        self.canvas.draw()

        self.current_combined_csv_path = Path(result["combined_csv"])
        self.save_figure.setEnabled(True)
        self.save_csv.setEnabled(True)
        self.progress_text.setText(f"Completed: {len(pixels)} pixel(s)")

    # ------------------------------------------------------------------- saving

    def save_current_figure(self, output_path: str | Path) -> Path:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if not output_path.suffix:
            output_path = output_path.with_suffix(".png")
        self.figure.savefig(output_path, dpi=200, bbox_inches="tight")
        return output_path

    def save_current_csv(self, output_path: str | Path) -> Path:
        if self.current_combined_csv_path is None or not self.current_combined_csv_path.exists():
            raise RuntimeError("No matrix sweep CSV is available")
        output_path = Path(output_path)
        if output_path.suffix.lower() != ".csv":
            output_path = output_path.with_suffix(".csv")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.current_combined_csv_path, output_path)
        return output_path
