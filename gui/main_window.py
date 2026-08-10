from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

import EO_cfg
from stand_controller import StandController
from workers import HardwareTaskRunner
from .widgets import Card, FloatEdit, LogPanel, OutputStateButton, RegisterTable, StatusBadge
from .styles import current_theme_colors


# =============================================================================
# Small page helpers
# =============================================================================


def _page_scroll(content: QWidget) -> QScrollArea:
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setWidget(content)
    return scroll


def _title_block(title: str, subtitle: str) -> QWidget:
    widget = QWidget()
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(0, 0, 0, 8)
    layout.setSpacing(3)
    title_label = QLabel(title)
    title_label.setObjectName("Title")
    subtitle_label = QLabel(subtitle)
    subtitle_label.setObjectName("Muted")
    subtitle_label.setWordWrap(True)
    layout.addWidget(title_label)
    layout.addWidget(subtitle_label)
    return widget


# =============================================================================
# Connections page
# =============================================================================


class ConnectionsPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(14)
        root.addWidget(
            _title_block(
                "Connections",
                "Manual connection to MGPDLab, oscilloscope and two-channel generator.",
            )
        )

        grid = QGridLayout()
        grid.setSpacing(12)
        root.addLayout(grid)

        # Chip
        chip = Card("Chip / MGPDLab")
        chip_form = QFormLayout()
        self.chip_host = QLineEdit("127.0.0.1")
        self.chip_port = QLineEdit("0xBEEB")
        self.chip_auto_enable = QCheckBox("Enable KIPIX CONTROL on connect")
        self.chip_auto_enable.setChecked(True)
        chip_form.addRow("Host", self.chip_host)
        chip_form.addRow("Port", self.chip_port)
        chip_form.addRow("", self.chip_auto_enable)
        chip.layout_.addLayout(chip_form)
        chip_buttons = QHBoxLayout()
        self.chip_connect = QPushButton("Connect")
        self.chip_connect.setObjectName("PrimaryButton")
        self.chip_disconnect = QPushButton("Disconnect")
        self.chip_disconnect.setEnabled(False)
        chip_buttons.addWidget(self.chip_connect)
        chip_buttons.addWidget(self.chip_disconnect)
        chip.layout_.addLayout(chip_buttons)
        grid.addWidget(chip, 0, 0)

        # Oscilloscope
        osc = Card("Oscilloscope")
        osc_form = QFormLayout()
        self.osc_address = QLineEdit("")
        self.osc_address.setPlaceholderText("Blank = auto-detect TCPIP VISA resource")
        self.osc_idn = QLineEdit("DSO9104H")
        osc_form.addRow("VISA address", self.osc_address)
        osc_form.addRow("IDN contains", self.osc_idn)
        osc.layout_.addLayout(osc_form)
        osc_buttons = QHBoxLayout()
        self.osc_connect = QPushButton("Connect")
        self.osc_connect.setObjectName("PrimaryButton")
        self.osc_disconnect = QPushButton("Disconnect")
        self.osc_disconnect.setEnabled(False)
        osc_buttons.addWidget(self.osc_connect)
        osc_buttons.addWidget(self.osc_disconnect)
        osc.layout_.addLayout(osc_buttons)
        grid.addWidget(osc, 0, 1)

        # Generator
        gen = Card("Generator")
        gen_form = QFormLayout()
        self.gen_address = QLineEdit("")
        self.gen_address.setPlaceholderText("Blank = auto-detect TCPIP VISA resource")
        self.gen_idn = QLineEdit("811")
        self.gen_max_amp = FloatEdit(1.0)
        self.gen_max_level = FloatEdit(1.0)
        gen_form.addRow("VISA address", self.gen_address)
        gen_form.addRow("IDN contains", self.gen_idn)
        gen_form.addRow("Max amplitude, V", self.gen_max_amp)
        gen_form.addRow("Max |level|, V", self.gen_max_level)
        gen.layout_.addLayout(gen_form)
        gen_buttons = QHBoxLayout()
        self.gen_connect = QPushButton("Connect")
        self.gen_connect.setObjectName("PrimaryButton")
        self.gen_disconnect = QPushButton("Disconnect")
        self.gen_disconnect.setEnabled(False)
        gen_buttons.addWidget(self.gen_connect)
        gen_buttons.addWidget(self.gen_disconnect)
        gen.layout_.addLayout(gen_buttons)
        grid.addWidget(gen, 1, 0, 1, 2)

        root.addStretch(1)


# =============================================================================
# Chip page
# =============================================================================


class ChipPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 24)
        root.setSpacing(12)
        root.addWidget(
            _title_block(
                "Chip control",
                "Edit logical fields locally, then apply only changed values. "
                "TEST_MUX is controlled through the dedicated AMUX selector.",
            )
        )

        top = QGridLayout()
        top.setSpacing(12)
        root.addLayout(top)

        # AMUX and register actions
        amux = Card("Analog test MUX")
        self.amux_combo = QComboBox()
        for signal, number in sorted(EO_cfg.AMUX_SIGNALS.items(), key=lambda x: x[1]):
            self.amux_combo.addItem(f"{number:02d}  {signal}", signal)
        self.amux_current = QLabel("Current: unknown")
        self.amux_current.setObjectName("Muted")
        self.amux_pending = QLabel("No pending change")
        self.amux_pending.setObjectName("Muted")
        amux.layout_.addWidget(self.amux_combo)
        amux.layout_.addWidget(self.amux_current)
        amux.layout_.addWidget(self.amux_pending)
        top.addWidget(amux, 0, 0)

        actions = Card("Configuration actions")
        row = QHBoxLayout()
        self.apply_changes = QPushButton("Apply changes")
        self.apply_changes.setObjectName("PrimaryButton")
        self.read_chip = QPushButton("Read chip")
        self.load_defaults = QPushButton("Load defaults")
        row.addWidget(self.apply_changes)
        row.addWidget(self.read_chip)
        row.addWidget(self.load_defaults)
        actions.layout_.addLayout(row)
        note = QLabel(
            "Apply writes only modified fields. Read chip performs one physical "
            "register scan and decodes all logical constants."
        )
        note.setObjectName("Muted")
        note.setWordWrap(True)
        actions.layout_.addWidget(note)
        top.addWidget(actions, 0, 1)

        # CTRL
        ctrl = Card("CTRL pin")
        ctrl_row = QHBoxLayout()
        self.ctrl_mode = QComboBox()
        self.ctrl_mode.addItems(["Static 0", "Static 1", "PWM"])
        self.ctrl_freq = QSpinBox()
        self.ctrl_freq.setRange(100, 50000)
        self.ctrl_freq.setSingleStep(10)
        self.ctrl_freq.setValue(8000)
        self.ctrl_freq.setSuffix(" kHz")
        self.ctrl_width = QSpinBox()
        self.ctrl_width.setRange(10, 999990)
        self.ctrl_width.setSingleStep(10)
        self.ctrl_width.setValue(70)
        self.ctrl_width.setSuffix(" ns")
        self.ctrl_apply = QPushButton("Apply CTRL")
        self.ctrl_real = QLabel("")
        self.ctrl_real.setObjectName("Muted")
        ctrl_row.addWidget(self.ctrl_mode)
        ctrl_row.addWidget(self.ctrl_freq)
        ctrl_row.addWidget(self.ctrl_width)
        ctrl_row.addWidget(self.ctrl_apply)
        ctrl_row.addWidget(self.ctrl_real)
        ctrl_row.addStretch(1)
        ctrl.layout_.addLayout(ctrl_row)
        root.addWidget(ctrl)

        # Table filters
        filter_row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search constants...")
        self.group_filter = QComboBox()
        self.group_filter.addItem("All")
        filter_row.addWidget(self.search, 1)
        filter_row.addWidget(self.group_filter)
        root.addLayout(filter_row)

        self.table = RegisterTable(EO_cfg.REGS_FIELDS, EO_cfg.DEFAULT_FIELD_VALUES)
        for group in self.table.groups():
            self.group_filter.addItem(group)
        root.addWidget(self.table, 1)

        self._baseline_amux: str | None = None
        self._last_raw_amux = 0
        self.amux_combo.currentIndexChanged.connect(self._update_amux_pending)
        self.search.textChanged.connect(self._apply_filter)
        self.group_filter.currentTextChanged.connect(self._apply_filter)
        self.ctrl_mode.currentTextChanged.connect(self._ctrl_mode_changed)
        self._ctrl_mode_changed(self.ctrl_mode.currentText())

        self.set_connected(False)

    def set_connected(self, connected: bool):
        for widget in (
            self.apply_changes,
            self.read_chip,
            self.load_defaults,
            self.ctrl_apply,
            self.amux_combo,
        ):
            widget.setEnabled(connected)

        # Never disable the table itself: disabled item views do not reliably
        # accept mouse-wheel scrolling.  Before connection it remains a
        # read-only browser of all EO_cfg fields; after connection only the
        # Value column becomes editable.
        self.table.setEnabled(True)
        self.table.set_editable(connected)

    def _apply_filter(self):
        self.table.filter_rows(self.search.text(), self.group_filter.currentText())

    def _ctrl_mode_changed(self, text: str):
        pwm = text == "PWM"
        self.ctrl_freq.setEnabled(pwm)
        self.ctrl_width.setEnabled(pwm)

    def set_snapshot(self, snapshot: dict):
        fields = snapshot["fields"]
        signal = snapshot.get("amux_signal")
        raw = snapshot.get("amux_raw", 0)
        self.table.set_values(fields)

        self._baseline_amux = signal
        self._last_raw_amux = raw

        self.amux_combo.blockSignals(True)
        try:
            if signal is None:
                self.amux_combo.setCurrentIndex(-1)
                self.amux_current.setText(f"Current: invalid/non-one-hot TEST_MUX = 0x{raw:X}")
            else:
                index = self.amux_combo.findData(signal)
                self.amux_combo.setCurrentIndex(index)
                self.amux_current.setText(f"Current: {signal}")
        finally:
            self.amux_combo.blockSignals(False)
        self._update_amux_pending()

    def _update_amux_pending(self):
        selected = self.amux_combo.currentData()
        if selected is None:
            c = current_theme_colors()
            self.amux_pending.setText("Select a signal")
            self.amux_pending.setStyleSheet(f"color:{c['warning']}")
        elif selected == self._baseline_amux:
            c = current_theme_colors()
            self.amux_pending.setText("No pending change")
            self.amux_pending.setStyleSheet(f"color:{c['muted']}")
        else:
            c = current_theme_colors()
            self.amux_pending.setText(f"Pending: {selected}")
            self.amux_pending.setStyleSheet(
                f"color:{c['warning']};font-weight:600"
            )

    def amux_change(self) -> str | None:
        selected = self.amux_combo.currentData()
        if selected is None or selected == self._baseline_amux:
            return None
        return str(selected)


# =============================================================================
# Oscilloscope page
# =============================================================================


class OscilloscopePage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(12)
        root.addWidget(
            _title_block(
                "Oscilloscope",
                "Frame configuration only. Waveforms remain on the physical instrument; "
                "the GUI provides DC measurement and CSV/screenshot capture.",
            )
        )

        # Acquisition + timebase
        globals_grid = QGridLayout()
        globals_grid.setSpacing(12)
        root.addLayout(globals_grid)

        acq = Card("Acquisition")
        form = QFormLayout()
        self.avg_enabled = QCheckBox("Averaging enabled")
        self.avg_enabled.setChecked(True)
        self.avg_count = QSpinBox()
        self.avg_count.setRange(1, 65536)
        self.avg_count.setValue(2)
        self.waveform_points = QSpinBox()
        self.waveform_points.setRange(100, 10_000_000)
        self.waveform_points.setValue(20000)
        self.common_scale = FloatEdit(0.25)
        self.common_offset = FloatEdit(0.0)
        form.addRow("", self.avg_enabled)
        form.addRow("Average count", self.avg_count)
        form.addRow("Waveform points", self.waveform_points)
        form.addRow("Common scale, V/div", self.common_scale)
        form.addRow("Common offset, V", self.common_offset)
        acq.layout_.addLayout(form)
        globals_grid.addWidget(acq, 0, 0)

        time_card = Card("Timebase / trigger")
        form = QFormLayout()
        self.time_scale = FloatEdit(20e-9)
        self.time_offset = FloatEdit(0.0)
        self.trigger_enabled = QCheckBox("Trigger enabled")
        self.trigger_enabled.setChecked(True)
        self.trigger_source = QComboBox()
        self.trigger_source.addItems(["1", "2", "3", "4"])
        self.trigger_level = FloatEdit(0.05)
        self.trigger_slope = QComboBox()
        self.trigger_slope.addItems(["POS", "NEG"])
        form.addRow("Time scale, s/div", self.time_scale)
        form.addRow("Time offset, s", self.time_offset)
        form.addRow("", self.trigger_enabled)
        form.addRow("Trigger source", self.trigger_source)
        form.addRow("Trigger level, V", self.trigger_level)
        form.addRow("Trigger slope", self.trigger_slope)
        time_card.layout_.addLayout(form)
        globals_grid.addWidget(time_card, 0, 1)

        # Per-channel settings
        channels_card = Card("Channels")
        header = QGridLayout()
        labels = ["Use", "CH", "Coupling", "Scale override", "Scale V/div", "Offset override", "Offset V"]
        for col, text in enumerate(labels):
            label = QLabel(text)
            label.setObjectName("Muted")
            header.addWidget(label, 0, col)

        self.channel_widgets: dict[int, dict] = {}
        for row, channel in enumerate(range(1, 5), start=1):
            enabled = QCheckBox()
            enabled.setChecked(channel in (1, 2, 3))
            ch_label = QLabel(f"CH{channel}")
            mode = QComboBox()
            mode.addItem("DC / 1 MΩ", "DC")
            mode.addItem("DC / 50 Ω", "DC50")
            mode.addItem("AC / 1 MΩ", "AC")
            scale_override = QCheckBox()
            scale = FloatEdit(0.25)
            scale.setEnabled(False)
            offset_override = QCheckBox()
            offset = FloatEdit(0.0)
            offset.setEnabled(False)
            scale_override.toggled.connect(scale.setEnabled)
            offset_override.toggled.connect(offset.setEnabled)

            header.addWidget(enabled, row, 0, alignment=Qt.AlignmentFlag.AlignCenter)
            header.addWidget(ch_label, row, 1)
            header.addWidget(mode, row, 2)
            header.addWidget(scale_override, row, 3, alignment=Qt.AlignmentFlag.AlignCenter)
            header.addWidget(scale, row, 4)
            header.addWidget(offset_override, row, 5, alignment=Qt.AlignmentFlag.AlignCenter)
            header.addWidget(offset, row, 6)

            self.channel_widgets[channel] = {
                "enabled": enabled,
                "mode": mode,
                "scale_override": scale_override,
                "scale": scale,
                "offset_override": offset_override,
                "offset": offset,
            }
        channels_card.layout_.addLayout(header)
        root.addWidget(channels_card)

        # Apply
        action_row = QHBoxLayout()
        self.apply_settings = QPushButton("Apply oscilloscope settings")
        self.apply_settings.setObjectName("PrimaryButton")
        action_row.addWidget(self.apply_settings)
        action_row.addStretch(1)
        root.addLayout(action_row)

        # Measurement / save
        measure_grid = QGridLayout()
        measure_grid.setSpacing(12)
        root.addLayout(measure_grid)

        dc = Card("DC level")
        dc_row = QHBoxLayout()
        self.dc_channel = QComboBox()
        self.dc_channel.addItems(["1", "2", "3", "4"])
        self.dc_button = QPushButton("Measure DC")
        self.dc_result = QLabel("-")
        self.dc_result.setStyleSheet(
            f"font-size:14pt;font-weight:650;color:{current_theme_colors()['input_focus']}"
        )
        dc_row.addWidget(QLabel("Channel"))
        dc_row.addWidget(self.dc_channel)
        dc_row.addWidget(self.dc_button)
        dc_row.addWidget(self.dc_result)
        dc_row.addStretch(1)
        dc.layout_.addLayout(dc_row)
        measure_grid.addWidget(dc, 0, 0)

        save = Card("Capture")
        save_channels = QHBoxLayout()
        self.save_checks = {}
        save_channels.addWidget(QLabel("CSV channels"))
        for ch in range(1, 5):
            box = QCheckBox(f"CH{ch}")
            box.setChecked(ch in (1, 2, 3))
            self.save_checks[ch] = box
            save_channels.addWidget(box)
        self.use_active_save = QPushButton("Use active")
        save_channels.addWidget(self.use_active_save)
        save_channels.addStretch(1)
        save.layout_.addLayout(save_channels)

        path_row = QHBoxLayout()
        self.save_dir = QLineEdit(str(Path.cwd() / "measurements"))
        self.save_filename = QLineEdit("")
        self.save_filename.setPlaceholderText("optional filename.csv")
        self.browse_dir = QPushButton("Browse")
        self.save_csv_button = QPushButton("Save CSV")
        path_row.addWidget(self.save_dir, 1)
        path_row.addWidget(self.save_filename)
        path_row.addWidget(self.browse_dir)
        path_row.addWidget(self.save_csv_button)
        save.layout_.addLayout(path_row)

        shot_row = QHBoxLayout()
        self.screenshot_button = QPushButton("Save screenshot")
        shot_row.addWidget(self.screenshot_button)
        shot_row.addStretch(1)
        save.layout_.addLayout(shot_row)
        measure_grid.addWidget(save, 1, 0)

        root.addStretch(1)
        self.use_active_save.clicked.connect(self._sync_save_channels)
        self.browse_dir.clicked.connect(self._browse_dir)
        self.set_connected(False)

    def set_connected(self, connected: bool):
        for widget in (
            self.apply_settings,
            self.dc_button,
            self.save_csv_button,
            self.screenshot_button,
        ):
            widget.setEnabled(connected)

    def _sync_save_channels(self):
        for ch, widgets in self.channel_widgets.items():
            self.save_checks[ch].setChecked(widgets["enabled"].isChecked())

    def _browse_dir(self):
        directory = QFileDialog.getExistingDirectory(self, "Select CSV output directory", self.save_dir.text())
        if directory:
            self.save_dir.setText(directory)

    def active_channels(self) -> list[int]:
        return [ch for ch, widgets in self.channel_widgets.items() if widgets["enabled"].isChecked()]

    def selected_save_channels(self) -> list[int]:
        return [ch for ch, box in self.save_checks.items() if box.isChecked()]

    def settings(self) -> dict:
        channels = self.active_channels()
        if not channels:
            raise ValueError("Select at least one oscilloscope channel")

        input_modes = {}
        scale_dict = {}
        offset_dict = {}
        for ch in channels:
            widgets = self.channel_widgets[ch]
            input_modes[ch] = widgets["mode"].currentData()
            if widgets["scale_override"].isChecked():
                scale_dict[ch] = widgets["scale"].value()
            if widgets["offset_override"].isChecked():
                offset_dict[ch] = widgets["offset"].value()

        return {
            "channels": tuple(channels),
            "trigger_enabled": self.trigger_enabled.isChecked(),
            "trigger_source": int(self.trigger_source.currentText()),
            "trigger_level_v": self.trigger_level.value(),
            "trigger_slope": self.trigger_slope.currentText(),
            "average_count": self.avg_count.value(),
            "time_scale_s": self.time_scale.value(),
            "time_offset_s": self.time_offset.value(),
            "voltage_scale_v": self.common_scale.value(),
            "voltage_offset_v": self.common_offset.value(),
            "input_modes": input_modes,
            "waveform_points": self.waveform_points.value(),
            "run_after_config": True,
            "averaging_enabled": self.avg_enabled.isChecked(),
            "voltage_scale_dict": scale_dict,
            "voltage_offset_dict": offset_dict,
        }


# =============================================================================
# Generator page
# =============================================================================


class GeneratorChannelCard(Card):
    def __init__(self, channel: int, parent=None):
        super().__init__(f"Generator CH{channel}", parent)
        self.channel = channel

        form = QFormLayout()
        self.shape = QComboBox()
        self.shape.addItems(["SIN", "SQU", "PULS", "RAMP", "NOIS", "USER", "DC"])
        self.frequency = FloatEdit(1e6)
        self.amplitude = FloatEdit(0.5)
        self.offset = FloatEdit(0.0)

        self.square_levels = QCheckBox("Use explicit LOW/HIGH for square")
        self.low = FloatEdit(0.0)
        self.high = FloatEdit(0.5)

        self.rise = FloatEdit(None, "optional")
        self.fall = FloatEdit(None, "optional")
        self.width = FloatEdit(None, "required for PULS")

        form.addRow("Shape", self.shape)
        form.addRow("Frequency, Hz", self.frequency)
        form.addRow("Amplitude, V", self.amplitude)
        form.addRow("Offset, V", self.offset)
        form.addRow("", self.square_levels)
        form.addRow("LOW, V", self.low)
        form.addRow("HIGH, V", self.high)
        form.addRow("Rise time, s", self.rise)
        form.addRow("Fall time, s", self.fall)
        form.addRow("Pulse width, s", self.width)
        self.layout_.addLayout(form)

        # Physical output state is controlled separately from waveform setup.
        outputs = QHBoxLayout()
        outputs.addWidget(QLabel("Physical outputs"))
        outputs.addStretch(1)

        main_label = f"OUT{channel}"
        comp_label = f"OUT{channel} COMP"
        self.output_main = OutputStateButton(channel, main_label)
        self.output_comp = OutputStateButton(channel + 2, comp_label)
        outputs.addWidget(self.output_main)
        outputs.addWidget(self.output_comp)
        self.layout_.addLayout(outputs)

        note = QLabel(
            "Applying generator settings always forces both outputs of this "
            "source OFF. Turn them on only with the output buttons above."
        )
        note.setObjectName("Muted")
        note.setWordWrap(True)
        self.layout_.addWidget(note)

        self.shape.currentTextChanged.connect(self._shape_changed)
        self.square_levels.toggled.connect(lambda _: self._shape_changed(self.shape.currentText()))
        self._shape_changed(self.shape.currentText())

    def _shape_changed(self, shape: str):
        is_dc = shape == "DC"
        is_pulse = shape == "PULS"
        is_square_levels = shape == "SQU" and self.square_levels.isChecked()

        self.frequency.setEnabled(not is_dc and shape != "NOIS")
        self.amplitude.setEnabled(not is_square_levels)
        self.offset.setEnabled(not is_square_levels)
        self.square_levels.setEnabled(shape == "SQU")
        self.low.setEnabled(is_square_levels)
        self.high.setEnabled(is_square_levels)
        self.rise.setEnabled(is_pulse)
        self.fall.setEnabled(is_pulse)
        self.width.setEnabled(is_pulse)

    def settings(self) -> dict:
        shape = self.shape.currentText()
        low = high = None
        if shape == "SQU" and self.square_levels.isChecked():
            low = self.low.value()
            high = self.high.value()

        return {
            "frequency_hz": self.frequency.value() if shape not in ("DC", "NOIS") else 1.0,
            "shape": shape,
            "amplitude_v": self.amplitude.value(),
            "offset_v": self.offset.value(),
            "rise_time_s": self.rise.value(allow_empty=True) if shape == "PULS" else None,
            "fall_time_s": self.fall.value(allow_empty=True) if shape == "PULS" else None,
            "pulse_width_s": self.width.value(allow_empty=True) if shape == "PULS" else None,
            "low_level_v": low,
            "high_level_v": high,
        }


class GeneratorPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(12)
        root.addWidget(
            _title_block(
                "Generator",
                "Configure the two source channels first, then control the four physical outputs independently.",
            )
        )

        row = QHBoxLayout()
        row.setSpacing(12)
        self.ch1 = GeneratorChannelCard(1)
        self.ch2 = GeneratorChannelCard(2)
        row.addWidget(self.ch1)
        row.addWidget(self.ch2)
        root.addLayout(row)

        self.output_buttons = {
            1: self.ch1.output_main,
            2: self.ch2.output_main,
            3: self.ch1.output_comp,
            4: self.ch2.output_comp,
        }

        action = QHBoxLayout()
        self.apply_settings = QPushButton("Apply generator settings")
        self.apply_settings.setObjectName("PrimaryButton")
        action.addWidget(self.apply_settings)
        action.addStretch(1)
        root.addLayout(action)
        root.addStretch(1)
        self.set_connected(False)

    def set_connected(self, connected: bool):
        self.apply_settings.setEnabled(connected)
        for button in self.output_buttons.values():
            button.setEnabled(connected)
            if not connected:
                button.set_state(None)

    def set_output_state(self, output_channel: int, enabled: bool):
        button = self.output_buttons.get(output_channel)
        if button is not None:
            button.set_state(enabled)

    def settings(self) -> dict[int, dict]:
        return {1: self.ch1.settings(), 2: self.ch2.settings()}


# =============================================================================
# Main window
# =============================================================================


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Chip Test Stand")
        self.resize(1500, 920)
        self.setMinimumSize(1180, 760)

        self.controller = StandController()
        self.runner = HardwareTaskRunner()
        self._active_workers = []

        central = QWidget()
        central.setObjectName("AppRoot")
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        # Header
        header = QFrame()
        header.setObjectName("Header")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(14, 9, 14, 9)
        app_name = QLabel("Chip Test Stand")
        app_name.setStyleSheet("font-size:13pt;font-weight:700")
        header_layout.addWidget(app_name)
        header_layout.addStretch(1)
        self.badges = {
            "chip": StatusBadge("CHIP"),
            "osc": StatusBadge("OSC"),
            "gen": StatusBadge("GEN"),
        }
        for badge in self.badges.values():
            header_layout.addWidget(badge)
        root.addWidget(header)

        self.vertical_split = QSplitter(Qt.Orientation.Vertical)
        self.vertical_split.setHandleWidth(4)
        self.vertical_split.setStretchFactor(0, 1)
        self.vertical_split.setStretchFactor(1, 0)
        root.addWidget(self.vertical_split, 1)

        top_area = QWidget()
        top_layout = QHBoxLayout(top_area)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.setSpacing(8)

        # Sidebar
        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(175)
        side_layout = QVBoxLayout(sidebar)
        side_layout.setContentsMargins(8, 10, 8, 10)
        side_layout.setSpacing(5)

        self.stack = QStackedWidget()
        self.connections = ConnectionsPage()
        self.chip = ChipPage()
        self.osc = OscilloscopePage()
        self.gen = GeneratorPage()

        self.pages = [
            ("Connections", self.connections),
            ("Chip", self.chip),
            ("Oscilloscope", self.osc),
            ("Generator", self.gen),
        ]
        self.nav_buttons = []
        for index, (name, page) in enumerate(self.pages):
            button = QPushButton(name)
            button.setObjectName("NavButton")
            button.setCheckable(True)
            button.clicked.connect(lambda checked, i=index: self._set_page(i))
            side_layout.addWidget(button)
            self.nav_buttons.append(button)
            # ChipPage contains a large QTableWidget with its own scrollbars.
            # Keeping it out of an outer QScrollArea makes wheel/scrollbar
            # interaction predictable and lets the table use all free height.
            if isinstance(page, ChipPage):
                self.stack.addWidget(page)
            else:
                self.stack.addWidget(_page_scroll(page))
        side_layout.addStretch(1)

        future = QLabel("Automated tests can be added later as a separate page without changing instrument drivers.")
        future.setObjectName("Muted")
        future.setWordWrap(True)
        side_layout.addWidget(future)

        top_layout.addWidget(sidebar)
        top_layout.addWidget(self.stack, 1)
        self.vertical_split.addWidget(top_area)

        self.log = LogPanel()
        self.vertical_split.addWidget(self.log)
        self.vertical_split.setCollapsible(0, False)
        self.vertical_split.setCollapsible(1, False)
        self.vertical_split.setSizes([760, 140])
        self.log.collapsed_changed.connect(self._set_log_collapsed)

        self._set_page(0)
        self._wire_signals()
        self.log.append("INFO", "GUI ready. Connect instruments manually from the Connections page.")

    # ---------------------------------------------------------------- signals

    def _wire_signals(self):
        self.controller.log_message.connect(self.log.append)
        self.controller.status_changed.connect(self._status_changed)
        self.controller.generator_output_changed.connect(self.gen.set_output_state)

        c = self.connections
        c.chip_connect.clicked.connect(self._connect_chip)
        c.chip_disconnect.clicked.connect(self._disconnect_chip)
        c.osc_connect.clicked.connect(self._connect_osc)
        c.osc_disconnect.clicked.connect(self._disconnect_osc)
        c.gen_connect.clicked.connect(self._connect_gen)
        c.gen_disconnect.clicked.connect(self._disconnect_gen)

        self.chip.read_chip.clicked.connect(self._read_chip)
        self.chip.apply_changes.clicked.connect(self._apply_chip)
        self.chip.load_defaults.clicked.connect(self._load_defaults)
        self.chip.ctrl_apply.clicked.connect(self._apply_ctrl)

        self.osc.apply_settings.clicked.connect(self._apply_osc)
        self.osc.dc_button.clicked.connect(self._measure_dc)
        self.osc.save_csv_button.clicked.connect(self._save_csv)
        self.osc.screenshot_button.clicked.connect(self._save_screenshot)

        self.gen.apply_settings.clicked.connect(self._apply_gen)
        for output_channel, button in self.gen.output_buttons.items():
            button.clicked.connect(
                lambda checked=False, ch=output_channel: self._toggle_gen_output(ch)
            )

    def _set_log_collapsed(self, collapsed: bool):
        # QSplitter sizes are content sizes and do not include the handle.
        # Use the real available height instead of an artificial 600 px floor;
        # this prevents the lower pane from competing with the table viewport
        # on smaller windows / Windows DPI scaling.
        total = max(self.vertical_split.height() - self.vertical_split.handleWidth(), 1)
        log_height = 48 if collapsed else 150
        log_height = min(log_height, max(total // 2, 1))
        self.vertical_split.setSizes([max(total - log_height, 1), log_height])

    def _set_page(self, index: int):
        self.stack.setCurrentIndex(index)
        for i, button in enumerate(self.nav_buttons):
            button.setChecked(i == index)

    def _status_changed(self, device: str, connected: bool, detail: str):
        badge = self.badges[device]
        badge.set_status("ok" if connected else "off", "Connected" if connected else "Disconnected")
        badge.setToolTip(detail)

        if device == "chip":
            self.connections.chip_connect.setEnabled(not connected)
            self.connections.chip_disconnect.setEnabled(connected)
            self.chip.set_connected(connected)
        elif device == "osc":
            self.connections.osc_connect.setEnabled(not connected)
            self.connections.osc_disconnect.setEnabled(connected)
            self.osc.set_connected(connected)
        elif device == "gen":
            self.connections.gen_connect.setEnabled(not connected)
            self.connections.gen_disconnect.setEnabled(connected)
            self.gen.set_connected(connected)

    # ------------------------------------------------------------ async helper

    def _run(self, description: str, fn, on_result=None):
        self.log.append("INFO", description)

        def error(message: str, tb: str):
            self.log.append("ERROR", message)
            if self.log.low_level.isChecked():
                self.log.append("DEBUG", tb, True)
            QMessageBox.critical(self, "Hardware operation failed", message)

        worker = self.runner.submit(
            fn,
            on_result=on_result,
            on_error=error,
            on_finished=lambda: self._worker_finished(worker),
        )
        self._active_workers.append(worker)

    def _worker_finished(self, worker):
        if worker in self._active_workers:
            self._active_workers.remove(worker)

    # --------------------------------------------------------------- connect

    def _connect_chip(self):
        try:
            port = int(self.connections.chip_port.text().strip(), 0)
        except ValueError:
            QMessageBox.warning(self, "Invalid port", "Port must be decimal or 0x-prefixed hexadecimal.")
            return

        self.badges["chip"].set_status("busy", "Connecting")
        self._run(
            "Connecting chip...",
            lambda: self.controller.connect_chip(
                host=self.connections.chip_host.text().strip(),
                port=port,
                auto_enable_kipix=self.connections.chip_auto_enable.isChecked(),
            ),
            on_result=self.chip.set_snapshot,
        )

    def _disconnect_chip(self):
        self._run("Disconnecting chip...", self.controller.disconnect_chip)

    def _connect_osc(self):
        self.badges["osc"].set_status("busy", "Connecting")
        self._run(
            "Connecting oscilloscope...",
            lambda: self.controller.connect_oscilloscope(
                self.connections.osc_address.text().strip() or None,
                self.connections.osc_idn.text().strip(),
            ),
        )

    def _disconnect_osc(self):
        self._run("Disconnecting oscilloscope...", self.controller.disconnect_oscilloscope)

    def _connect_gen(self):
        try:
            max_amp = self.connections.gen_max_amp.value()
            max_level = self.connections.gen_max_level.value()
        except ValueError as error:
            QMessageBox.warning(self, "Invalid generator limits", str(error))
            return

        self.badges["gen"].set_status("busy", "Connecting")
        self._run(
            "Connecting generator...",
            lambda: self.controller.connect_generator(
                self.connections.gen_address.text().strip() or None,
                self.connections.gen_idn.text().strip(),
                max_amplitude_v=max_amp,
                max_abs_level_v=max_level,
            ),
        )

    def _disconnect_gen(self):
        self._run("Disconnecting generator...", self.controller.disconnect_generator)

    # ------------------------------------------------------------------ chip

    def _read_chip(self):
        self._run("Reading complete chip configuration...", self.controller.read_chip_snapshot, self.chip.set_snapshot)

    def _apply_chip(self):
        try:
            dirty = self.chip.table.dirty_values()
            amux = self.chip.amux_change()
        except ValueError as error:
            QMessageBox.warning(self, "Invalid register value", str(error))
            return

        if not dirty and amux is None:
            self.log.append("INFO", "No chip configuration changes to apply")
            return

        self._run(
            f"Applying {len(dirty)} changed chip field(s)" + (" + AMUX" if amux else "") + "...",
            lambda: self.controller.apply_chip_changes(dirty, amux),
            self.chip.set_snapshot,
        )

    def _load_defaults(self):
        answer = QMessageBox.question(
            self,
            "Load defaults",
            "Write the complete default register image to the chip?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._run("Loading default chip configuration...", self.controller.load_chip_defaults, self.chip.set_snapshot)

    def _apply_ctrl(self):
        mode = self.chip.ctrl_mode.currentText()
        if mode == "Static 0":
            self._run("Setting CTRL=0...", lambda: self.controller.set_ctrl_static(0))
        elif mode == "Static 1":
            self._run("Setting CTRL=1...", lambda: self.controller.set_ctrl_static(1))
        else:
            freq = self.chip.ctrl_freq.value()
            width = self.chip.ctrl_width.value()

            def show_real(value):
                self.chip.ctrl_real.setText(f"Real F: {value:g} kHz")

            self._run(
                "Setting CTRL PWM...",
                lambda: self.controller.set_ctrl_pwm(freq, width),
                show_real,
            )

    # ------------------------------------------------------------- oscilloscope

    def _apply_osc(self):
        try:
            settings = self.osc.settings()
        except Exception as error:
            QMessageBox.warning(self, "Invalid oscilloscope settings", str(error))
            return
        self._run("Applying oscilloscope settings...", lambda: self.controller.configure_oscilloscope(settings))

    def _measure_dc(self):
        channel = int(self.osc.dc_channel.currentText())

        def show(value):
            self.osc.dc_result.setText(f"{value:.6g} V")

        self._run(f"Measuring CH{channel} DC level...", lambda: self.controller.measure_dc(channel), show)

    def _save_csv(self):
        channels = self.osc.selected_save_channels()
        if not channels:
            QMessageBox.warning(self, "No channels", "Select at least one channel for CSV capture.")
            return

        output_dir = self.osc.save_dir.text().strip()
        filename = self.osc.save_filename.text().strip() or None
        if filename and not filename.lower().endswith(".csv"):
            filename += ".csv"

        self._run(
            f"Saving oscilloscope CSV for channels {channels}...",
            lambda: self.controller.save_osc_csv(channels, output_dir, filename),
        )

    def _save_screenshot(self):
        default = str(Path(self.osc.save_dir.text().strip() or Path.cwd()) / "oscilloscope.png")
        path, _ = QFileDialog.getSaveFileName(self, "Save oscilloscope screenshot", default, "PNG (*.png)")
        if path:
            self._run("Saving oscilloscope screenshot...", lambda: self.controller.save_osc_screenshot(path))

    # ---------------------------------------------------------------- generator

    def _apply_gen(self):
        try:
            settings = self.gen.settings()
        except Exception as error:
            QMessageBox.warning(self, "Invalid generator settings", str(error))
            return
        self._run(
            "Applying generator settings; physical outputs will be forced OFF...",
            lambda: self.controller.configure_generator(settings),
        )

    def _toggle_gen_output(self, output_channel: int):
        button = self.gen.output_buttons[output_channel]
        button.setEnabled(False)

        def restore_enabled(_result=None):
            button.setEnabled(self.controller.gen is not None)

        self._run(
            f"Toggling generator physical output {output_channel}...",
            lambda: self.controller.toggle_generator_output(output_channel),
            on_result=restore_enabled,
        )

        # Also restore the button if the operation fails. The controller signal
        # updates its color/text only after a successful hardware command.
        # Worker completion is serialized, so this single-shot callback is safe.
        if self._active_workers:
            worker = self._active_workers[-1]
            worker.signals.finished.connect(lambda: restore_enabled())

    # ---------------------------------------------------------------- shutdown

    def closeEvent(self, event):
        self.controller.shutdown()
        super().closeEvent(event)
