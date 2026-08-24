from __future__ import annotations

from math import sqrt

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
from PyQt6.QtCore import QPointF, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from pixel_matrix import MATRIX_ROWS, OWNED_COLUMNS
from lfsr_decoder import LFSRDecoder
from .matrix_page import CONTINUOUS_COLORMAPS, HeatmapScale, _interpolate_color
from .styles import current_theme_colors
from .widgets import Card


COUNTER_KEYS = ("low", "mid", "high")
COUNTER_LABELS = {
    "low": "Low",
    "mid": "Mid",
    "high": "High",
}


class CounterMatrixMap(QWidget):
    """Heatmap of GET_PIXEL counter values for the project-owned matrix half."""

    pixel_clicked = pyqtSignal(int, int)  # row, col

    def __init__(self, parent=None):
        super().__init__(parent)
        self._data_provider = None
        self._counter_provider = None
        self._selected_provider = None
        self.setMinimumSize(360, 620)
        self.setMouseTracking(True)

    def set_data_provider(self, provider):
        self._data_provider = provider
        self.update()

    def set_counter_provider(self, provider):
        self._counter_provider = provider
        self.update()

    def set_selected_provider(self, provider):
        self._selected_provider = provider
        self.update()

    @staticmethod
    def _visual_row(row: int) -> int:
        return MATRIX_ROWS - 1 - int(row)

    @staticmethod
    def _logical_row(visual_row: int) -> int:
        return MATRIX_ROWS - 1 - int(visual_row)

    def _geometry(self):
        # Keep the coordinate labels outside the heatmap area. The previous
        # values placed the top column labels directly above the first cells,
        # which caused overlap when the widget was resized.
        left = 48.0
        top = 34.0
        right = 12.0
        bottom = 18.0
        columns = len(OWNED_COLUMNS)
        available_w = max(self.width() - left - right, 1.0)
        available_h = max(self.height() - top - bottom, 1.0)
        cell = min(available_w / columns, available_h / MATRIX_ROWS)
        grid_w = cell * columns
        grid_h = cell * MATRIX_ROWS
        x0 = left + max((available_w - grid_w) / 2.0, 0.0)
        # Keep the matrix aligned to the top of the card. Horizontal centering
        # is retained, but unused vertical space stays below the grid.
        y0 = top
        return x0, y0, cell, grid_w, grid_h

    def _pixel_at(self, position) -> tuple[int, int] | None:
        x0, y0, cell, grid_w, grid_h = self._geometry()
        if not (x0 <= position.x() < x0 + grid_w and y0 <= position.y() < y0 + grid_h):
            return None
        local_col = int((position.x() - x0) // cell)
        visual_row = int((position.y() - y0) // cell)
        row = self._logical_row(visual_row)
        col = min(OWNED_COLUMNS) + local_col
        if col not in OWNED_COLUMNS or not 0 <= row < MATRIX_ROWS:
            return None
        return row, col

    def _snapshot(self):
        data = self._data_provider() if self._data_provider is not None else {}
        counter = self._counter_provider() if self._counter_provider is not None else "low"
        values = [
            int(pixel[counter])
            for pixel in data.values()
            if counter in pixel
        ]
        if values:
            minimum = min(values)
            maximum = max(values)
        else:
            minimum = maximum = 0
        return data, counter, minimum, maximum

    def paintEvent(self, event):
        super().paintEvent(event)
        c = current_theme_colors()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)

        x0, y0, cell, grid_w, grid_h = self._geometry()
        data, counter, minimum, maximum = self._snapshot()
        selected = self._selected_provider() if self._selected_provider is not None else None

        unread_color = QColor(c["table_alt"])
        stops = CONTINUOUS_COLORMAPS["Viridis"]

        for row in range(MATRIX_ROWS):
            for local_col, col in enumerate(OWNED_COLUMNS):
                rect = QRectF(
                    x0 + local_col * cell,
                    y0 + self._visual_row(row) * cell,
                    cell,
                    cell,
                )
                pixel = data.get((row, col))
                if pixel is None:
                    color = unread_color
                else:
                    value = int(pixel[counter])
                    if maximum == minimum:
                        position = 0.5
                    else:
                        position = (value - minimum) / (maximum - minimum)
                    color = _interpolate_color(stops, position)
                painter.fillRect(rect, color)

        grid_pen = QPen(QColor(c["table_grid"]))
        grid_pen.setWidthF(0.7)
        painter.setPen(grid_pen)
        for index in range(len(OWNED_COLUMNS) + 1):
            x = x0 + index * cell
            painter.drawLine(QPointF(x, y0), QPointF(x, y0 + grid_h))
        for index in range(MATRIX_ROWS + 1):
            y = y0 + index * cell
            painter.drawLine(QPointF(x0, y), QPointF(x0 + grid_w, y))

        if selected is not None:
            row, col = selected
            if 0 <= row < MATRIX_ROWS and col in OWNED_COLUMNS:
                local_col = col - min(OWNED_COLUMNS)
                rect = QRectF(
                    x0 + local_col * cell + 0.8,
                    y0 + self._visual_row(row) * cell + 0.8,
                    max(cell - 1.6, 1.0),
                    max(cell - 1.6, 1.0),
                )
                selected_pen = QPen(QColor(c["text_strong"]))
                selected_pen.setWidthF(2.2)
                painter.setPen(selected_pen)
                painter.drawRect(rect)

        painter.setPen(QColor(c["muted"]))
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

        col_labels = (16, 20, 24, 28, 31)
        for col in col_labels:
            if col not in OWNED_COLUMNS:
                continue
            local_col = col - min(OWNED_COLUMNS)
            center_x = x0 + (local_col + 0.5) * cell
            painter.drawText(
                QRectF(center_x - 15, 8, 30, 20),
                Qt.AlignmentFlag.AlignCenter,
                str(col),
            )

        for row in (31, 24, 16, 8, 0):
            center_y = y0 + (self._visual_row(row) + 0.5) * cell
            painter.drawText(
                QRectF(2, center_y - 10, 40, 20),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                str(row),
            )

        painter.end()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            pixel = self._pixel_at(event.position())
            if pixel is not None:
                self.pixel_clicked.emit(*pixel)
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        pixel_coord = self._pixel_at(event.position())
        if pixel_coord is None or self._data_provider is None:
            self.setToolTip("")
            super().mouseMoveEvent(event)
            return

        row, col = pixel_coord
        pixel = self._data_provider().get((row, col))
        if pixel is None:
            self.setToolTip(f"Col={col} Row={row}\nNot read")
        else:
            self.setToolTip(
                f"Col={col} Row={row}\n"
                f"Low={int(pixel['low'])}\n"
                f"Mid={int(pixel['mid'])}\n"
                f"High={int(pixel['high'])}\n"
                f"RAW=0x{pixel['raw_hex']}"
            )
        super().mouseMoveEvent(event)


class MatrixOperationPage(QWidget):
    """GET_SHOT / GET_PIXEL control and counter-data visualization."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._chip_connected = False
        self._busy = False
        self._selected_pixel = (0, min(OWNED_COLUMNS))
        self._pixel_data: dict[tuple[int, int], dict] = {}
        self._decoder_cache = {8: LFSRDecoder(8), 16: LFSRDecoder(16)}

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(12)

        title = QLabel("Matrix operation")
        title.setObjectName("Title")
        subtitle = QLabel(
            "Acquire matrix counters through GET_SHOT / GET_PIXEL. "
            "Only the project-owned half, Col=16..31, is displayed and read by Read all."
        )
        subtitle.setObjectName("Muted")
        subtitle.setWordWrap(True)
        root.addWidget(title)
        root.addWidget(subtitle)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.setHandleWidth(5)
        root.addWidget(split, 1)

        # ---------------------------------------------------------------- left controls
        controls = QWidget()
        controls.setMinimumWidth(320)
        controls.setMaximumWidth(390)
        controls_layout = QVBoxLayout(controls)
        controls_layout.setContentsMargins(0, 0, 6, 0)
        controls_layout.setSpacing(12)

        shot_card = Card("Shot / OMR settings")
        form = QFormLayout()

        self.mode_cnt = QComboBox()
        self.mode_cnt.addItem("16-bit counters", 0)
        self.mode_cnt.addItem("8-bit counters", 1)

        self.mode_read = QComboBox()
        self.mode_read.addItem("Serial x00: Low + High", 0b000)
        self.mode_read.addItem("Serial x01: Low only", 0b001)
        self.mode_read.addItem("SPI 010: Low + Mid + High", 0b010)
        self.mode_read.addItem("SPI 011: Low + High", 0b011)
        self.mode_read.addItem("SPI 110: Low + Mid", 0b110)
        self.mode_read.addItem("SPI 111: Low only", 0b111)
        self.mode_read.setCurrentIndex(self.mode_read.findData(0b010))

        self.crw_mode = QComboBox()
        self.crw_mode.addItem("Sequential readout", 0)
        self.crw_mode.addItem("Continuous read/write (CRW)", 1)

        form.addRow("Counter mode", self.mode_cnt)
        form.addRow("Data readout", self.mode_read)
        form.addRow("Readout operation", self.crw_mode)
        shot_card.layout_.addLayout(form)

        self.configure_omr = QCheckBox("Write these OMR fields before GET_SHOT")
        self.configure_omr.setChecked(False)
        self.configure_omr.setToolTip(
            "When enabled, Python performs read-modify-write for MODE_CNT, "
            "MODE_READ and CRW_MODE immediately before GET_SHOT. DCR and ICR "
            "are not directly modified by this GUI action."
        )
        shot_card.layout_.addWidget(self.configure_omr)

        self.get_shot_button = QPushButton("Get shot")
        self.get_shot_button.setObjectName("PrimaryButton")
        shot_card.layout_.addWidget(self.get_shot_button)
        controls_layout.addWidget(shot_card)

        read_card = Card("Counter readout")
        self.selected_label = QLabel()
        self.selected_label.setObjectName("Muted")
        read_card.layout_.addWidget(self.selected_label)

        button_row = QHBoxLayout()
        self.read_selected_button = QPushButton("Read selected")
        self.read_all_button = QPushButton("Read all")
        button_row.addWidget(self.read_selected_button)
        button_row.addWidget(self.read_all_button)
        read_card.layout_.addLayout(button_row)

        self.selected_values = QLabel("Low: -\nMid: -\nHigh: -\nRAW: -")
        self.selected_values.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        read_card.layout_.addWidget(self.selected_values)

        self.progress_text = QLabel("Ready")
        self.progress_text.setObjectName("Muted")
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setTextVisible(True)
        read_card.layout_.addWidget(self.progress_text)
        read_card.layout_.addWidget(self.progress)
        controls_layout.addWidget(read_card)

        view_card = Card("Visualization")
        view_form = QFormLayout()
        self.counter_view = QComboBox()
        self.counter_view.addItem("Low counter", "low")
        self.counter_view.addItem("Mid counter", "mid")
        self.counter_view.addItem("High counter", "high")
        view_form.addRow("Counter", self.counter_view)
        view_card.layout_.addLayout(view_form)

        self.show_all_counters = QCheckBox("Show all")
        self.show_all_counters.setChecked(False)

        self.decode_lfsr = QCheckBox("Decode LFSR counters")
        self.decode_lfsr.setChecked(True)
        self.show_all_counters.setToolTip(
            "Overlay Low, Mid and High counter histograms with transparency. "
            "The matrix heatmap still follows the Counter selection above."
        )
        view_card.layout_.addWidget(self.show_all_counters)
        view_card.layout_.addWidget(self.decode_lfsr)

        self.stats_label = QLabel("No counter data read")
        self.stats_label.setObjectName("Muted")
        self.stats_label.setWordWrap(True)
        view_card.layout_.addWidget(self.stats_label)
        controls_layout.addWidget(view_card)
        controls_layout.addStretch(1)
        split.addWidget(controls)

        # ---------------------------------------------------------------- matrix
        matrix_card = Card("Counter matrix")
        matrix_card.setMinimumWidth(360)
        matrix_row = QHBoxLayout()
        matrix_row.setContentsMargins(0, 0, 0, 0)
        matrix_row.setSpacing(8)

        self.matrix_map = CounterMatrixMap()
        self.matrix_map.set_data_provider(lambda: self._pixel_data)
        self.matrix_map.set_counter_provider(self.selected_counter)
        self.matrix_map.set_selected_provider(lambda: self._selected_pixel)
        matrix_row.addWidget(self.matrix_map, 1)

        self.scale = HeatmapScale()
        self.scale.setMinimumWidth(48)
        self.scale.setMaximumWidth(54)
        matrix_row.addWidget(self.scale, 0)
        matrix_card.layout_.addLayout(matrix_row)
        split.addWidget(matrix_card)

        # ---------------------------------------------------------------- histogram
        hist_card = Card("Counter distribution")
        self.figure = Figure(figsize=(7, 5), tight_layout=False)
        self.canvas = FigureCanvas(self.figure)
        self.canvas.setMinimumSize(420, 400)
        hist_card.layout_.addWidget(self.canvas, 1)
        split.addWidget(hist_card)

        # Give the counter matrix substantially more horizontal room.  The
        # histogram still expands with the window, but no longer dominates the
        # three-column layout.
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setStretchFactor(2, 1)
        split.setSizes([350, 500, 590])

        self.matrix_map.pixel_clicked.connect(self.set_selected_pixel)
        self.counter_view.currentIndexChanged.connect(self.refresh_visualization)
        self.show_all_counters.stateChanged.connect(self.refresh_visualization)
        self.decode_lfsr.stateChanged.connect(self.refresh_visualization)

        self._refresh_selected_labels()
        self.refresh_visualization()
        self.set_connected(False)

    # ---------------------------------------------------------------- settings

    def selected_counter(self) -> str:
        value = self.counter_view.currentData()
        return str(value) if value in COUNTER_KEYS else "low"

    def omr_settings(self) -> dict:
        return {
            "configure_omr": self.configure_omr.isChecked(),
            "mode_cnt": int(self.mode_cnt.currentData()),
            "mode_read": int(self.mode_read.currentData()),
            "crw_mode": int(self.crw_mode.currentData()),
        }

    def selected_pixel(self) -> tuple[int, int]:
        return self._selected_pixel

    # ---------------------------------------------------------------- state

    def set_connected(self, connected: bool):
        self._chip_connected = bool(connected)
        self._refresh_enabled_state()
        if not connected:
            self.clear_data("Disconnected")

    def set_busy(self, busy: bool, message: str | None = None):
        self._busy = bool(busy)
        if busy:
            self.progress.setRange(0, 0)
            if message:
                self.progress_text.setText(message)
        else:
            if self.progress.maximum() == 0:
                self.progress.setRange(0, 1)
                self.progress.setValue(0)
            if self.progress_text.text().startswith(("Starting", "Running")):
                self.progress_text.setText("Ready")
        self._refresh_enabled_state()

    def _refresh_enabled_state(self):
        enabled = self._chip_connected and not self._busy
        for widget in (
            self.get_shot_button,
            self.read_selected_button,
            self.read_all_button,
            self.mode_cnt,
            self.mode_read,
            self.crw_mode,
            self.configure_omr,
            self.show_all_counters,
            self.decode_lfsr,
        ):
            widget.setEnabled(enabled)
        self.matrix_map.setEnabled(not self._busy)

    def set_selected_pixel(self, row: int, col: int):
        if col not in OWNED_COLUMNS or not 0 <= row < MATRIX_ROWS:
            return
        self._selected_pixel = (int(row), int(col))
        self._refresh_selected_labels()
        self.matrix_map.update()

    def _refresh_selected_labels(self):
        row, col = self._selected_pixel
        self.selected_label.setText(f"Selected: Col={col} Row={row}")
        data = self._pixel_data.get((row, col))
        if data is None:
            self.selected_values.setText("Low: -\nMid: -\nHigh: -\nRAW: -")
        else:
            self.selected_values.setText(
                f"Low: {int(data['low'])}  (0x{int(data['low']):04X})\n"
                f"Mid: {int(data['mid'])}  (0x{int(data['mid']):04X})\n"
                f"High: {int(data['high'])}  (0x{int(data['high']):04X})\n"
                f"RAW: 0x{data['raw_hex']}"
            )

    def clear_data(self, message: str = "Ready"):
        self._pixel_data.clear()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setFormat("%p%")
        self.progress_text.setText(message)
        self._refresh_selected_labels()
        self.refresh_visualization()

    def apply_pixel_result(self, result: dict):
        row = int(result["row"])
        col = int(result["col"])
        self._pixel_data[(row, col)] = dict(result)
        self.set_selected_pixel(row, col)
        self.progress.setRange(0, 1)
        self.progress.setValue(1)
        self.progress.setFormat("1 pixel")
        self.progress_text.setText(f"Read Col={col} Row={row}")
        self.refresh_visualization()

    def apply_read_all_result(self, result: dict):
        pixels = result.get("pixels", {})
        self._pixel_data = {tuple(coord): dict(value) for coord, value in pixels.items()}
        count = len(self._pixel_data)
        self.progress.setRange(0, max(count, 1))
        self.progress.setValue(count)
        self.progress.setFormat(f"{count} / {count}")
        self.progress_text.setText(f"Read complete: {count} pixel(s)")
        self._refresh_selected_labels()
        self.refresh_visualization()

    def set_read_progress(self, current: int, total: int, row: int, col: int):
        total = max(int(total), 1)
        current = max(0, min(int(current), total))
        self.progress.setRange(0, total)
        self.progress.setValue(current)
        self.progress.setFormat(f"{current} / {total}")
        self.progress_text.setText(f"Reading Col={col} Row={row}")

    # ---------------------------------------------------------------- visuals

    def counter_width(self) -> int:
        return 8 if int(self.mode_cnt.currentData()) == 1 else 16

    def display_counter_value(self, pixel: dict, key: str):
        value = int(pixel[key])
        if not self.decode_lfsr.isChecked():
            return value

        decoded = self._decoder_cache[self.counter_width()].decode(value)
        return value if decoded is None else decoded

    def refresh_visualization(self):
        counter = self.selected_counter()
        values = [self.display_counter_value(pixel, counter) for pixel in self._pixel_data.values()]

        if values:
            minimum = min(values)
            maximum = max(values)
            self.scale.set_scale(minimum, maximum, "Viridis")
            self.stats_label.setText(
                f"{len(values)} pixel(s) read | min={minimum} | max={maximum}"
            )
        else:
            self.scale.set_scale(0, 0, "Viridis")
            self.stats_label.setText("No counter data read")

        self.matrix_map.update()
        self._draw_histogram(values, counter)
        self._refresh_selected_labels()

    def _draw_histogram(self, values: list[int], counter: str):
        c = current_theme_colors()
        self.figure.clear()
        self.figure.set_facecolor(c["card"])
        ax = self.figure.add_subplot(111)
        ax.set_facecolor(c["table"])

        show_all = self.show_all_counters.isChecked()

        if show_all and self._pixel_data:
            datasets = {
                key: [self.display_counter_value(pixel, key) for pixel in self._pixel_data.values()]
                for key in COUNTER_KEYS
            }
            combined = [value for dataset in datasets.values() for value in dataset]
            minimum = min(combined)
            maximum = max(combined)

            if minimum == maximum:
                # All three counters are identical. Draw them on top of each other
                # with transparency so the legend still communicates the sources.
                width = 1.0
                for key in COUNTER_KEYS:
                    ax.bar(
                        [minimum],
                        [len(datasets[key])],
                        width=width,
                        alpha=0.35,
                        label=COUNTER_LABELS[key],
                    )
            else:
                total_count = max(len(combined), 1)
                bin_count = min(64, max(8, round(sqrt(total_count) * 2)))
                step = (maximum - minimum) / bin_count
                bin_edges = [minimum + index * step for index in range(bin_count + 1)]
                # Protect the upper edge against floating-point round-off for integer data.
                bin_edges[-1] = maximum
                for key in COUNTER_KEYS:
                    ax.hist(
                        datasets[key],
                        bins=bin_edges,
                        alpha=0.35,
                        label=COUNTER_LABELS[key],
                    )

            ax.legend()
            pixel_count = len(self._pixel_data)
            ax.set_title(
                f"All counter distributions ({pixel_count} pixels)",
                color=c["text_strong"],
            )
        elif values:
            minimum = min(values)
            maximum = max(values)
            if minimum == maximum:
                ax.bar([minimum], [len(values)], width=1.0)
            else:
                bins = min(64, max(8, round(sqrt(len(values)) * 2)))
                ax.hist(values, bins=bins)
            ax.set_title(
                f"{COUNTER_LABELS[counter]} counter distribution ({len(values)} pixels)",
                color=c["text_strong"],
            )
        else:
            ax.text(
                0.5,
                0.5,
                "Read one pixel or use Read all",
                transform=ax.transAxes,
                ha="center",
                va="center",
                color=c["muted"],
            )
            title = (
                "All counter distributions"
                if show_all
                else f"{COUNTER_LABELS[counter]} counter distribution"
            )
            ax.set_title(title, color=c["text_strong"])

        ax.set_xlabel("Counter value", color=c["text"])
        ax.set_ylabel("Pixel count", color=c["text"])
        ax.grid(True, alpha=0.22)
        ax.tick_params(colors=c["text"])
        for spine in ax.spines.values():
            spine.set_color(c["input_border"])
        self.figure.subplots_adjust(left=0.16, right=0.90, bottom=0.10, top=0.86)
        self.canvas.draw()
