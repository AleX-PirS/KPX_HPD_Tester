from __future__ import annotations

import shutil
from pathlib import Path

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSplitter,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

import EO_cfg
from .matrix_sweep_page import PixelSettingsEditor, SweepMatrixMap
from .styles import current_theme_colors
from .widgets import Card, FloatEdit


class AspectRatioImageLabel(QLabel):
    """Display a pixmap without distorting its aspect ratio."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._source_pixmap = QPixmap()
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setObjectName("PreviewArea")
        self.setMinimumSize(500, 320)

    def set_image(self, path: str | Path):
        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            raise RuntimeError(f"Failed to load image: {path}")
        self._source_pixmap = pixmap
        self._rescale()

    def clear_image(self):
        self._source_pixmap = QPixmap()
        self.clear()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._rescale()

    def _rescale(self):
        if self._source_pixmap.isNull() or self.width() <= 2 or self.height() <= 2:
            return
        scaled = self._source_pixmap.scaled(
            self.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.setPixmap(scaled)


class VisualizationPage(QWidget):
    """Shared oscilloscope preview with AMUX and matrix sweep control tabs."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._chip_connected = False
        self._osc_connected = False
        self._amux_busy = False
        self._matrix_busy = False
        self._capture_busy = False
        self.current_mode: str | None = None
        self.current_screenshot_path: Path | None = None
        self.current_combined_csv_path: Path | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(12)

        title = QLabel("Visualize")
        title.setObjectName("Title")
        subtitle = QLabel(
            "Oscilloscope screen preview, AMUX sweep and matrix sweep share one visualization area. "
            "Sweep acquisition uses the current oscilloscope setup."
        )
        subtitle.setObjectName("Muted")
        subtitle.setWordWrap(True)
        root.addWidget(title)
        root.addWidget(subtitle)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.setHandleWidth(5)
        root.addWidget(split, 1)

        # ---------------------------------------------------------------- controls
        controls = QWidget()
        controls.setMinimumWidth(450)
        controls.setMaximumWidth(650)
        controls_layout = QVBoxLayout(controls)
        controls_layout.setContentsMargins(0, 0, 8, 0)
        controls_layout.setSpacing(10)

        # Screenshot remains permanently accessible above both sweep tabs.
        screen_card = Card("Oscilloscope screen")
        screen_row = QHBoxLayout()
        screen_note = QLabel("Capture the current instrument display.")
        screen_note.setObjectName("Muted")
        self.refresh_screen = QPushButton("Refresh screen")
        self.refresh_screen.setObjectName("PrimaryButton")
        screen_row.addWidget(screen_note, 1)
        screen_row.addWidget(self.refresh_screen)
        screen_card.layout_.addLayout(screen_row)
        controls_layout.addWidget(screen_card)

        self.sweep_tabs = QTabWidget()
        self.sweep_tabs.setObjectName("SweepModeTabs")
        # Use two equal-width, button-like tabs instead of the compact native
        # document tabs. The QSS gives the selector a clear active state.
        self.sweep_tabs.setDocumentMode(False)
        self.sweep_tabs.tabBar().setExpanding(True)
        self.sweep_tabs.addTab(self._build_amux_tab(), "AMUX SWEEP")
        self.sweep_tabs.addTab(self._build_matrix_tab(), "MATRIX SWEEP")
        controls_layout.addWidget(self.sweep_tabs, 1)

        controls_scroll = QScrollArea()
        controls_scroll.setWidgetResizable(True)
        controls_scroll.setFrameShape(QFrame.Shape.NoFrame)
        controls_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        controls_scroll.setWidget(controls)
        controls_scroll.setMinimumWidth(460)
        controls_scroll.setMaximumWidth(670)
        split.addWidget(controls_scroll)

        # ---------------------------------------------------------------- preview
        preview_card = Card("Preview")
        self.preview_stack = QStackedWidget()
        self.preview_stack.setObjectName("VisualizationPreview")

        self.placeholder = QLabel(
            "Refresh the oscilloscope screen or run an AMUX / Matrix sweep."
        )
        self.placeholder.setObjectName("PreviewArea")
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.placeholder.setWordWrap(True)
        self.placeholder.setMinimumSize(500, 320)

        self.screen_image = AspectRatioImageLabel()

        self.figure = Figure(figsize=(8, 5), tight_layout=False)
        self.canvas = FigureCanvas(self.figure)
        self.canvas.setObjectName("PlotCanvas")
        self.canvas.setMinimumSize(500, 320)

        self.preview_stack.addWidget(self.placeholder)
        self.preview_stack.addWidget(self.screen_image)
        self.preview_stack.addWidget(self.canvas)
        preview_card.layout_.addWidget(self.preview_stack, 1)

        save_row = QHBoxLayout()
        self.save_figure = QPushButton("Save image / figure...")
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
        split.setSizes([540, 860])

        self._refresh_enabled_state()

    # ================================================================ tab builders

    def _build_amux_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(8, 10, 8, 8)
        layout.setSpacing(10)

        form = QFormLayout()
        self.scope_channel = QComboBox()
        self.scope_channel.addItems(["1", "2", "3", "4"])
        self.scope_channel.setCurrentText("1")
        self.delay_s = FloatEdit(0.1)
        self.delay_s.setPlaceholderText("0.1")
        form.addRow("Oscilloscope channel", self.scope_channel)
        form.addRow("AMUX settling delay, s", self.delay_s)
        layout.addLayout(form)

        self.fclk_off_capture = QCheckBox("FCLK OFF during capture")
        self.fclk_off_capture.setProperty("amuxLegacy", True)
        self.fclk_off_capture.setChecked(False)
        self.fclk_off_capture.setToolTip(
            "For each selected AMUX signal: set AMUX, force FCLK to 0, wait the "
            "settling delay, capture the waveform, then restore FCLK before the next AMUX switch. "
            "If the previous FCLK frequency is unknown, 100 MHz is used."
        )
        layout.addWidget(self.fclk_off_capture)

        signal_actions = QHBoxLayout()
        self.select_all = QPushButton("Select all")
        self.clear_all = QPushButton("Clear")
        signal_actions.addWidget(self.select_all)
        signal_actions.addWidget(self.clear_all)
        signal_actions.addStretch(1)
        layout.addLayout(signal_actions)

        signal_scroll = QScrollArea()
        signal_scroll.setWidgetResizable(True)
        signal_scroll.setMinimumHeight(300)
        signal_container = QWidget()
        signal_grid = QGridLayout(signal_container)
        signal_grid.setContentsMargins(4, 4, 8, 8)
        signal_grid.setHorizontalSpacing(12)
        signal_grid.setVerticalSpacing(6)
        # Keep the signal list compact even when the combined Visualize tab has
        # a tall viewport. Without top alignment QGridLayout distributes the
        # extra height between its rows, producing very large gaps between
        # checkboxes. The old standalone AMUX page kept rows packed at the top.
        signal_grid.setAlignment(Qt.AlignmentFlag.AlignTop)

        ordered_signals = sorted(EO_cfg.AMUX_SIGNALS.items(), key=lambda item: item[1])
        self.signal_checks: dict[str, QCheckBox] = {}
        columns = 2
        rows_per_column = (len(ordered_signals) + columns - 1) // columns
        for index, (signal, number) in enumerate(ordered_signals):
            checkbox = QCheckBox(f"{number:02d}  {signal}")
            # Keep the pre-combined-Visualize AMUX checkbox appearance.
            checkbox.setProperty("amuxLegacy", True)
            checkbox.setToolTip(signal)
            self.signal_checks[signal] = checkbox
            column = index // rows_per_column
            row = index % rows_per_column
            signal_grid.addWidget(checkbox, row, column)

        bottom_spacer_row = rows_per_column
        signal_grid.setRowMinimumHeight(bottom_spacer_row, 26)
        signal_grid.setRowStretch(bottom_spacer_row, 0)
        signal_grid.setColumnStretch(columns, 1)
        sample_checkbox_height = max(
            (checkbox.sizeHint().height() for checkbox in self.signal_checks.values()),
            default=22,
        )
        margins = signal_grid.contentsMargins()
        content_height = (
            margins.top()
            + margins.bottom()
            + rows_per_column * sample_checkbox_height
            + max(0, rows_per_column - 1) * signal_grid.verticalSpacing()
            + 26
        )
        # Do not let widgetResizable stretch the grid vertically. Fix only the
        # content height; the width still follows the scroll viewport. On short
        # windows QScrollArea scrolls normally, while on tall windows the rows
        # remain at their natural compact spacing.
        signal_container.setMinimumHeight(content_height)
        signal_container.setMaximumHeight(content_height)
        signal_scroll.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        signal_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        signal_scroll.setWidget(signal_container)
        layout.addWidget(signal_scroll, 1)

        self.start_sweep = QPushButton("Start AMUX sweep")
        self.start_sweep.setObjectName("PrimaryButton")
        layout.addWidget(self.start_sweep)

        self.progress_text = QLabel("Ready")
        self.progress_text.setObjectName("Muted")
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setTextVisible(True)
        layout.addWidget(self.progress_text)
        layout.addWidget(self.progress)

        self.select_all.clicked.connect(lambda: self._set_all_signals(True))
        self.clear_all.clicked.connect(lambda: self._set_all_signals(False))
        return tab

    def _build_matrix_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(8, 10, 8, 8)
        layout.setSpacing(10)

        self.matrix_global_settings = PixelSettingsEditor("Global settings")
        self.matrix_sweep_settings = PixelSettingsEditor("Sweep settings")
        layout.addWidget(self.matrix_global_settings)
        layout.addWidget(self.matrix_sweep_settings)

        acquisition = Card("Acquisition")
        acq_grid = QGridLayout()
        self.matrix_scope_channel = QComboBox()
        self.matrix_scope_channel.addItems(["1", "2", "3", "4"])
        self.matrix_scope_channel.setCurrentText("1")
        self.matrix_delay_s = FloatEdit(0.1)
        self.matrix_delay_s.setPlaceholderText("0.1")
        self.matrix_fclk_off_capture = QCheckBox("FCLK OFF during capture")
        self.matrix_fclk_off_capture.setChecked(False)
        self.matrix_fclk_off_capture.setToolTip(
            "After the current pixel configuration is written to the chip, set FCLK=0, "
            "wait the settling delay, capture the waveform, then restore the previous known FCLK. "
            "If FCLK was unknown, 100 MHz is established as the restore value."
        )
        acq_grid.addWidget(QLabel("Oscilloscope channel"), 0, 0)
        acq_grid.addWidget(self.matrix_scope_channel, 0, 1)
        acq_grid.addWidget(QLabel("Settling delay, s"), 1, 0)
        acq_grid.addWidget(self.matrix_delay_s, 1, 1)
        acquisition.layout_.addLayout(acq_grid)
        acquisition.layout_.addWidget(self.matrix_fclk_off_capture)
        layout.addWidget(acquisition)

        selector = Card("Pixel selection")
        selection_note = QLabel(
            "Click: select one. Ctrl+click: add/remove. Shift+click: rectangle from the last anchor. "
            "Ctrl+Shift: add a rectangle. No explicit selection = sweep all 1024 pixels."
        )
        selection_note.setObjectName("Muted")
        selection_note.setWordWrap(True)
        selector.layout_.addWidget(selection_note)

        selection_row = QHBoxLayout()
        self.matrix_selection_status = QLabel()
        self.matrix_selection_status.setObjectName("SectionTitle")
        self.matrix_clear_selection = QPushButton("Clear selection (All)")
        self.matrix_clear_selection.setObjectName("NeutralButton")
        selection_row.addWidget(self.matrix_selection_status)
        selection_row.addStretch(1)
        selection_row.addWidget(self.matrix_clear_selection)
        selector.layout_.addLayout(selection_row)

        self.matrix_map = SweepMatrixMap()
        selector.layout_.addWidget(self.matrix_map, 1)
        layout.addWidget(selector, 1)

        self.matrix_start_sweep = QPushButton("Start matrix sweep")
        self.matrix_start_sweep.setObjectName("PrimaryButton")
        layout.addWidget(self.matrix_start_sweep)

        self.matrix_progress_text = QLabel("Ready")
        self.matrix_progress_text.setObjectName("Muted")
        self.matrix_progress = QProgressBar()
        self.matrix_progress.setRange(0, 1)
        self.matrix_progress.setValue(0)
        self.matrix_progress.setTextVisible(True)
        layout.addWidget(self.matrix_progress_text)
        layout.addWidget(self.matrix_progress)

        self.matrix_clear_selection.clicked.connect(self.matrix_map.clear_selection)
        self.matrix_map.selection_changed.connect(self._refresh_matrix_selection_status)
        self._refresh_matrix_selection_status()
        return tab

    # ================================================================ AMUX API

    def _set_all_signals(self, checked: bool):
        for checkbox in self.signal_checks.values():
            checkbox.setChecked(checked)

    def selected_signals(self) -> tuple[str, ...]:
        return tuple(
            signal
            for signal, number in sorted(EO_cfg.AMUX_SIGNALS.items(), key=lambda item: item[1])
            if self.signal_checks[signal].isChecked()
        )

    def selected_scope_channel(self) -> int:
        return int(self.scope_channel.currentText())

    def settling_delay_s(self) -> float:
        value = self.delay_s.value()
        if value is None or value < 0:
            raise ValueError("AMUX settling delay must be >= 0 s")
        return value

    def disable_fclk_during_capture(self) -> bool:
        return self.fclk_off_capture.isChecked()

    # ============================================================ Matrix sweep API

    def matrix_sweep_pixels(self) -> tuple[tuple[int, int], ...]:
        return self.matrix_map.effective_selection()

    def matrix_global_raw(self) -> int:
        return self.matrix_global_settings.raw_value()

    def matrix_sweep_raw(self) -> int:
        return self.matrix_sweep_settings.raw_value()

    def matrix_selected_scope_channel(self) -> int:
        return int(self.matrix_scope_channel.currentText())

    def matrix_settling_delay_s(self) -> float:
        value = self.matrix_delay_s.value()
        if value is None or value < 0:
            raise ValueError("Matrix settling delay must be >= 0 s")
        return value

    def matrix_disable_fclk_during_capture(self) -> bool:
        return self.matrix_fclk_off_capture.isChecked()

    def _refresh_matrix_selection_status(self):
        explicit = self.matrix_map.explicit_selection()
        if explicit:
            self.matrix_selection_status.setText(f"Selected: {len(explicit)}")
        else:
            self.matrix_selection_status.setText("Selected: ALL 1024")

    # ================================================================ connection

    def set_chip_connected(self, connected: bool):
        self._chip_connected = bool(connected)
        self._refresh_enabled_state()

    def set_osc_connected(self, connected: bool):
        self._osc_connected = bool(connected)
        self._refresh_enabled_state()

    def _refresh_enabled_state(self):
        hardware_busy = self._amux_busy or self._matrix_busy or self._capture_busy
        self.refresh_screen.setEnabled(self._osc_connected and not hardware_busy)
        sweep_ready = self._chip_connected and self._osc_connected and not hardware_busy
        self.start_sweep.setEnabled(sweep_ready)
        self.matrix_start_sweep.setEnabled(sweep_ready)

    # ================================================================ progress

    def set_sweep_busy(self, busy: bool):
        """AMUX sweep busy state (kept for existing MainWindow integration)."""
        self._amux_busy = bool(busy)
        enabled = not busy
        for checkbox in self.signal_checks.values():
            checkbox.setEnabled(enabled)
        self.select_all.setEnabled(enabled)
        self.clear_all.setEnabled(enabled)
        self.scope_channel.setEnabled(enabled)
        self.delay_s.setEnabled(enabled)
        self.fclk_off_capture.setEnabled(enabled)

        if busy:
            self.progress.setRange(0, 0)
            self.progress_text.setText("Starting AMUX sweep...")
            self.save_figure.setEnabled(False)
            self.save_csv.setEnabled(False)
        else:
            if self.progress.maximum() == 0:
                self.progress.setRange(0, 1)
                self.progress.setValue(0)
            if self.progress_text.text().startswith("Starting"):
                self.progress_text.setText("Ready")
        self._refresh_enabled_state()

    def set_capture_busy(self, busy: bool):
        self._capture_busy = bool(busy)
        self._refresh_enabled_state()

    def set_sweep_progress(self, current: int, total: int, signal: str):
        total = max(int(total), 1)
        current = max(0, min(int(current), total))
        self.progress.setRange(0, total)
        self.progress.setValue(current)
        self.progress.setFormat(f"{current} / {total}")
        self.progress_text.setText(f"Capturing {signal}")

    def set_matrix_sweep_busy(self, busy: bool):
        self._matrix_busy = bool(busy)
        enabled = not busy
        self.matrix_global_settings.set_editor_enabled(enabled)
        self.matrix_sweep_settings.set_editor_enabled(enabled)
        self.matrix_scope_channel.setEnabled(enabled)
        self.matrix_delay_s.setEnabled(enabled)
        self.matrix_fclk_off_capture.setEnabled(enabled)
        self.matrix_map.setEnabled(enabled)
        self.matrix_clear_selection.setEnabled(enabled)

        if busy:
            self.matrix_progress.setRange(0, 0)
            self.matrix_progress_text.setText("Preparing matrix sweep...")
            self.save_figure.setEnabled(False)
            self.save_csv.setEnabled(False)
        else:
            if self.matrix_progress.maximum() == 0:
                self.matrix_progress.setRange(0, 1)
                self.matrix_progress.setValue(0)
            if self.matrix_progress_text.text().startswith("Preparing"):
                self.matrix_progress_text.setText("Ready")
        self._refresh_enabled_state()

    def set_matrix_sweep_progress(self, current: int, total: int, row: int, col: int):
        total = max(int(total), 1)
        current = max(0, min(int(current), total))
        self.matrix_progress.setRange(0, total)
        self.matrix_progress.setValue(current)
        self.matrix_progress.setFormat(f"{current} / {total}")
        self.matrix_progress_text.setText(f"Capturing Col={col} Row={row}")

    # ================================================================ preview

    def show_screenshot(self, path: str | Path):
        path = Path(path)
        self.screen_image.set_image(path)
        self.current_screenshot_path = path
        self.current_combined_csv_path = None
        self.current_mode = "screenshot"
        self.preview_stack.setCurrentWidget(self.screen_image)
        self.save_figure.setEnabled(True)
        self.save_csv.setEnabled(False)

    def _prepare_plot(self):
        c = current_theme_colors()
        self.figure.clear()
        self.figure.set_facecolor(c["card"])
        ax = self.figure.add_subplot(111)
        ax.set_facecolor(c["table"])
        return c, ax

    @staticmethod
    def _style_plot_axes(ax, c):
        ax.set_xlabel("Time, s", color=c["text"])
        ax.set_ylabel("Voltage, V", color=c["text"])
        ax.grid(True, alpha=0.25)
        ax.tick_params(colors=c["text"])
        for spine in ax.spines.values():
            spine.set_color(c["input_border"])

    def show_amux_result(self, result: dict):
        signals = list(result["signals"])
        time_s = result["time_s"]
        waveforms = result["waveforms"]

        c, ax = self._prepare_plot()
        for signal in signals:
            ax.plot(time_s, waveforms[signal], linewidth=1.1, label=signal)
        self._style_plot_axes(ax, c)
        ax.set_title("AMUX sweep", color=c["text_strong"])

        if signals:
            legend_columns = 2 if len(signals) > 16 else 1
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
            self.figure.subplots_adjust(right=0.73 if legend_columns == 2 else 0.80)
        else:
            self.figure.subplots_adjust(right=0.96)

        self.figure.subplots_adjust(left=0.10, bottom=0.12, top=0.93)
        self.canvas.draw()
        self.current_combined_csv_path = Path(result["combined_csv"])
        self.current_screenshot_path = None
        self.current_mode = "amux"
        self.preview_stack.setCurrentWidget(self.canvas)
        self.save_figure.setEnabled(True)
        self.save_csv.setEnabled(True)
        self.progress_text.setText(f"Completed: {len(signals)} signal(s)")

    def show_matrix_result(self, result: dict):
        pixels = list(result["pixels"])
        time_s = result["time_s"]
        waveforms = result["waveforms"]

        c, ax = self._prepare_plot()
        for row, col in pixels:
            key = (row, col)
            ax.plot(time_s, waveforms[key], linewidth=0.9, label=f"C{col} R{row}")
        self._style_plot_axes(ax, c)
        ax.set_title(f"Matrix sweep - {len(pixels)} pixel(s)", color=c["text_strong"])

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
        self.current_screenshot_path = None
        self.current_mode = "matrix"
        self.preview_stack.setCurrentWidget(self.canvas)
        self.save_figure.setEnabled(True)
        self.save_csv.setEnabled(True)
        self.matrix_progress_text.setText(f"Completed: {len(pixels)} pixel(s)")

    # ================================================================ saving

    def save_current_figure(self, output_path: str | Path) -> Path:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        if self.current_mode == "screenshot":
            if self.current_screenshot_path is None or not self.current_screenshot_path.exists():
                raise RuntimeError("No oscilloscope screenshot is available")
            if output_path.suffix.lower() != ".png":
                output_path = output_path.with_suffix(".png")
            shutil.copy2(self.current_screenshot_path, output_path)
            return output_path

        if self.current_mode in ("amux", "matrix"):
            if not output_path.suffix:
                output_path = output_path.with_suffix(".png")
            self.figure.savefig(output_path, dpi=200, bbox_inches="tight")
            return output_path

        raise RuntimeError("Nothing is currently displayed to save")

    def save_current_csv(self, output_path: str | Path) -> Path:
        if self.current_combined_csv_path is None or not self.current_combined_csv_path.exists():
            raise RuntimeError("No sweep CSV is available")

        output_path = Path(output_path)
        if output_path.suffix.lower() != ".csv":
            output_path = output_path.with_suffix(".csv")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.current_combined_csv_path, output_path)
        return output_path
