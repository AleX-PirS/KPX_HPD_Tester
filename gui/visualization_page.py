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
    QVBoxLayout,
    QWidget,
)

import EO_cfg
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
    """Oscilloscope screen preview and AMUX multi-signal visualization."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._chip_connected = False
        self._osc_connected = False
        self._sweep_busy = False
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
            "Preview the oscilloscope screen or sweep selected chip AMUX signals "
            "without changing the current oscilloscope setup."
        )
        subtitle.setObjectName("Muted")
        subtitle.setWordWrap(True)
        root.addWidget(title)
        root.addWidget(subtitle)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.setHandleWidth(5)
        root.addWidget(split, 1)

        # ------------------------------------------------------------------ left
        controls = QWidget()
        controls.setMinimumWidth(390)
        controls.setMaximumWidth(520)
        controls_layout = QVBoxLayout(controls)
        controls_layout.setContentsMargins(0, 0, 6, 0)
        controls_layout.setSpacing(12)

        screen_card = Card("Oscilloscope screen")
        screen_note = QLabel(
            "Capture the current instrument display. Each refresh overwrites "
            "temp/oscilloscope_screen.png."
        )
        screen_note.setObjectName("Muted")
        screen_note.setWordWrap(True)
        self.refresh_screen = QPushButton("Refresh screen")
        self.refresh_screen.setObjectName("PrimaryButton")
        screen_card.layout_.addWidget(screen_note)
        screen_card.layout_.addWidget(self.refresh_screen)
        controls_layout.addWidget(screen_card)

        sweep_card = Card("AMUX sweep")
        form = QFormLayout()
        self.scope_channel = QComboBox()
        self.scope_channel.addItems(["1", "2", "3", "4"])
        self.scope_channel.setCurrentText("1")
        self.delay_s = FloatEdit(0.1)
        self.delay_s.setPlaceholderText("0.1")
        form.addRow("Oscilloscope channel", self.scope_channel)
        form.addRow("AMUX settling delay, s", self.delay_s)
        sweep_card.layout_.addLayout(form)

        signal_actions = QHBoxLayout()
        self.select_all = QPushButton("Select all")
        self.clear_all = QPushButton("Clear")
        signal_actions.addWidget(self.select_all)
        signal_actions.addWidget(self.clear_all)
        signal_actions.addStretch(1)
        sweep_card.layout_.addLayout(signal_actions)

        signal_scroll = QScrollArea()
        signal_scroll.setWidgetResizable(True)
        signal_scroll.setMinimumHeight(330)
        signal_container = QWidget()
        signal_grid = QGridLayout(signal_container)
        # Extra bottom space guarantees that the last AMUX rows are visually
        # separated from the sweep controls below the scroll area.
        signal_grid.setContentsMargins(4, 4, 8, 8)
        signal_grid.setHorizontalSpacing(12)
        signal_grid.setVerticalSpacing(6)

        ordered_signals = sorted(EO_cfg.AMUX_SIGNALS.items(), key=lambda item: item[1])
        self.signal_checks: dict[str, QCheckBox] = {}
        columns = 2
        rows_per_column = (len(ordered_signals) + columns - 1) // columns
        for index, (signal, number) in enumerate(ordered_signals):
            checkbox = QCheckBox(f"{number:02d}  {signal}")
            checkbox.setToolTip(signal)
            self.signal_checks[signal] = checkbox
            column = index // rows_per_column
            row = index % rows_per_column
            signal_grid.addWidget(checkbox, row, column)

        # IMPORTANT: do not rely on QScrollArea/widgetResizable to infer the
        # complete content height. With two columns the final pair (19/39) can
        # otherwise be laid out partly below the viewport while Qt still
        # considers the content to fit. A real spacer row plus an explicit
        # minimum height makes the scroll range include the complete last row.
        bottom_spacer_row = rows_per_column
        signal_grid.setRowMinimumHeight(bottom_spacer_row, 34)
        signal_grid.setRowStretch(bottom_spacer_row, 0)
        signal_grid.setColumnStretch(columns, 1)

        # Calculate the minimum scrollable content height from the actual
        # checkbox size hint rather than using a magic overall container size.
        # This also behaves correctly with Windows DPI/font scaling.
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
            + 34
        )
        signal_container.setMinimumHeight(content_height)
        signal_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        signal_scroll.setWidget(signal_container)
        sweep_card.layout_.addWidget(signal_scroll)

        # Sweep execution controls live in a dedicated footer BELOW the AMUX
        # selection area. Keeping them in their own widget prevents Qt from
        # visually compressing/overlapping the last AMUX rows on short windows.
        sweep_footer = QWidget()
        sweep_footer_layout = QVBoxLayout(sweep_footer)
        sweep_footer_layout.setContentsMargins(0, 12, 0, 0)
        sweep_footer_layout.setSpacing(8)

        self.start_sweep = QPushButton("Start AMUX sweep")
        self.start_sweep.setObjectName("PrimaryButton")
        sweep_footer_layout.addWidget(self.start_sweep)

        self.progress_text = QLabel("Ready")
        self.progress_text.setObjectName("Muted")
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setTextVisible(True)
        sweep_footer_layout.addWidget(self.progress_text)
        sweep_footer_layout.addWidget(self.progress)
        sweep_card.layout_.addWidget(sweep_footer)

        controls_layout.addWidget(sweep_card)
        controls_layout.addStretch(1)

        # The complete left control column is scrollable. This preserves the
        # natural geometry of the AMUX list + footer even on smaller windows
        # instead of forcing Qt to squeeze child widgets into each other.
        controls_scroll = QScrollArea()
        controls_scroll.setWidgetResizable(True)
        controls_scroll.setFrameShape(QFrame.Shape.NoFrame)
        controls_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        controls_scroll.setWidget(controls)
        controls_scroll.setMinimumWidth(400)
        controls_scroll.setMaximumWidth(540)
        split.addWidget(controls_scroll)

        # ----------------------------------------------------------------- right
        preview_card = Card("Preview")
        self.preview_stack = QStackedWidget()
        self.preview_stack.setObjectName("VisualizationPreview")

        self.placeholder = QLabel(
            "Use 'Refresh screen' for the oscilloscope display, or select AMUX "
            "signals and start a sweep."
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
        split.setSizes([430, 850])

        self.select_all.clicked.connect(lambda: self._set_all_signals(True))
        self.clear_all.clicked.connect(lambda: self._set_all_signals(False))
        self._refresh_enabled_state()

    # ---------------------------------------------------------------- selection

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

    # --------------------------------------------------------------- connection

    def set_chip_connected(self, connected: bool):
        self._chip_connected = bool(connected)
        self._refresh_enabled_state()

    def set_osc_connected(self, connected: bool):
        self._osc_connected = bool(connected)
        self._refresh_enabled_state()

    def _refresh_enabled_state(self):
        hardware_busy = self._sweep_busy or self._capture_busy
        self.refresh_screen.setEnabled(self._osc_connected and not hardware_busy)
        self.start_sweep.setEnabled(
            self._chip_connected and self._osc_connected and not hardware_busy
        )

    # ---------------------------------------------------------------- progress

    def set_sweep_busy(self, busy: bool):
        self._sweep_busy = bool(busy)
        for checkbox in self.signal_checks.values():
            checkbox.setEnabled(not busy)
        self.select_all.setEnabled(not busy)
        self.clear_all.setEnabled(not busy)
        self.scope_channel.setEnabled(not busy)
        self.delay_s.setEnabled(not busy)

        if busy:
            self.progress.setRange(0, 0)
            self.progress_text.setText("Starting AMUX sweep...")
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

    # ---------------------------------------------------------------- preview

    def show_screenshot(self, path: str | Path):
        path = Path(path)
        self.screen_image.set_image(path)
        self.current_screenshot_path = path
        self.current_combined_csv_path = None
        self.current_mode = "screenshot"
        self.preview_stack.setCurrentWidget(self.screen_image)
        self.save_figure.setEnabled(True)
        self.save_csv.setEnabled(False)

    def show_amux_result(self, result: dict):
        signals = list(result["signals"])
        time_s = result["time_s"]
        waveforms = result["waveforms"]

        c = current_theme_colors()
        self.figure.clear()
        self.figure.set_facecolor(c["card"])
        ax = self.figure.add_subplot(111)
        ax.set_facecolor(c["table"])

        for signal in signals:
            ax.plot(time_s, waveforms[signal], linewidth=1.1, label=signal)

        ax.set_xlabel("Time, s", color=c["text"])
        ax.set_ylabel("Voltage, V", color=c["text"])
        ax.set_title("AMUX sweep", color=c["text_strong"])
        ax.grid(True, alpha=0.25)
        ax.tick_params(colors=c["text"])
        for spine in ax.spines.values():
            spine.set_color(c["input_border"])

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

    # ------------------------------------------------------------------- saving

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

        if self.current_mode == "amux":
            if not output_path.suffix:
                output_path = output_path.with_suffix(".png")
            self.figure.savefig(output_path, dpi=200, bbox_inches="tight")
            return output_path

        raise RuntimeError("Nothing is currently displayed to save")

    def save_current_csv(self, output_path: str | Path) -> Path:
        if self.current_combined_csv_path is None or not self.current_combined_csv_path.exists():
            raise RuntimeError("No AMUX sweep CSV is available")

        output_path = Path(output_path)
        if output_path.suffix.lower() != ".csv":
            output_path = output_path.with_suffix(".csv")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.current_combined_csv_path, output_path)
        return output_path
