from __future__ import annotations

import csv
import math
import shutil
import time
from pathlib import Path
from typing import Callable

from PyQt6.QtCore import QObject, pyqtSignal

import EO_cfg
from configuration import Configuration
from generator_cfg import TwoChannelGenerator
from mgpd import MGPDClient
from oscilloscope_cfg import Oscilloscope
from pixel_matrix import PIXEL_CODEC, PixelMatrixConfiguration


class StandController(QObject):
    """GUI orchestration layer over the existing test-stand drivers.

    The instrument driver API is intentionally not replaced. Automated scripts
    can keep importing and using MGPDClient, Configuration, Oscilloscope and
    TwoChannelGenerator directly. This controller only coordinates GUI actions.
    """

    log_message = pyqtSignal(str, str, bool)  # level, message, low_level
    status_changed = pyqtSignal(str, bool, str)  # device, connected, detail
    generator_output_changed = pyqtSignal(int, bool)  # physical output 1..4, enabled
    amux_sweep_progress = pyqtSignal(int, int, str)  # current, total, signal
    matrix_sweep_progress = pyqtSignal(int, int, int, int)  # current, total, row, col
    pixel_matrix_progress = pyqtSignal(int, int, int, int)  # current, total, row, col
    matrix_read_progress = pyqtSignal(int, int, int, int)  # current, total, row, col
    fclk_changed = pyqtSignal(object)  # int MHz or None when unknown/disconnected
    ctrl_state_changed = pyqtSignal(object, object)  # static state 0/1/None, PWM enabled bool/None

    def __init__(self):
        super().__init__()
        self.client: MGPDClient | None = None
        self.cfg: Configuration | None = None
        self.matrix_cfg: PixelMatrixConfiguration | None = None
        self._fclk_mhz: int | None = None
        self._last_nonzero_fclk_mhz: int | None = None
        self._ctrl_static_state: int | None = None
        self._ctrl_pwm_enabled: bool | None = None
        self.osc: Oscilloscope | None = None
        self.gen: TwoChannelGenerator | None = None
        self._gen_output_states: dict[int, bool] = {1: False, 2: False, 3: False, 4: False}
        self.temp_dir = Path(__file__).resolve().parent / "temp"

    # ------------------------------------------------------------------ logging

    def _log(self, level: str, message: str, low_level: bool = False):
        self.log_message.emit(level, message, low_level)

    def _trace_callback(self, device: str) -> Callable[[str, str], None]:
        def trace(direction: str, message: str):
            self._log("DEBUG", f"{device} {direction}: {message}", True)
        return trace

    # --------------------------------------------------------------- connection

    def connect_chip(
        self,
        host: str = "127.0.0.1",
        port: int = 0xBEEB,
        timeout: float = 5.0,
        auto_enable_kipix: bool = True,
    ) -> dict:
        self.disconnect_chip(silent=True)

        client = MGPDClient(
            host=host,
            port=port,
            timeout=timeout,
            auto_enable_kipix=auto_enable_kipix,
            trace_callback=self._trace_callback("CHIP"),
        )
        client.connect()

        self.client = client
        self.cfg = Configuration(
            client,
            EO_cfg.DEFAULT_REGISTERS,
            EO_cfg.AMUX_SIGNALS,
            EO_cfg.REGS_FIELDS,
            EO_cfg.AMUX_MAP,
        )
        self.matrix_cfg = PixelMatrixConfiguration(client)
        # The new protocol has SET_FCLK but no matching GET_FCLK command, so
        # the actual clock state is unknown immediately after connection.
        self._fclk_mhz = None
        self._last_nonzero_fclk_mhz = None
        self._ctrl_static_state = None
        self._ctrl_pwm_enabled = None
        self.fclk_changed.emit(None)
        self.ctrl_state_changed.emit(None, None)

        self.status_changed.emit("chip", True, f"{host}:{port}")
        self._log("INFO", f"Chip connected: {host}:{port}")

        # User requested an initial complete synchronization after connection.
        snapshot = self.read_chip_snapshot()
        self._log("INFO", "Chip registers loaded into GUI")
        return snapshot

    def disconnect_chip(self, silent: bool = False):
        if self.client is not None:
            try:
                self.client.disconnect()
            finally:
                self.client = None
                self.cfg = None
                self.matrix_cfg = None
                self._fclk_mhz = None
                self._last_nonzero_fclk_mhz = None
                self._ctrl_static_state = None
                self._ctrl_pwm_enabled = None

        self.fclk_changed.emit(None)
        self.ctrl_state_changed.emit(None, None)
        self.status_changed.emit("chip", False, "Disconnected")
        if not silent:
            self._log("INFO", "Chip disconnected")

    def connect_oscilloscope(
        self,
        osc_address: str | None = None,
        idn_substring: str = "DSO9104H",
    ) -> str:
        self.disconnect_oscilloscope(silent=True)

        self.osc = Oscilloscope(
            osc_address=osc_address or None,
            idn_substring=idn_substring,
            trace_callback=self._trace_callback("OSC"),
        )
        detail = self.osc.idn
        self.status_changed.emit("osc", True, detail)
        self._log("INFO", f"Oscilloscope connected: {detail}")
        return detail

    def disconnect_oscilloscope(self, silent: bool = False):
        if self.osc is not None:
            try:
                self.osc.close()
            finally:
                self.osc = None

        self.status_changed.emit("osc", False, "Disconnected")
        if not silent:
            self._log("INFO", "Oscilloscope disconnected")

    def connect_generator(
        self,
        gen_address: str | None = None,
        idn_substring: str = "811",
        max_amplitude_v: float = 1.0,
        max_abs_level_v: float = 1.0,
    ) -> str:
        self.disconnect_generator(silent=True)

        self.gen = TwoChannelGenerator(
            gen_address=gen_address or None,
            idn_substring=idn_substring,
            max_amplitude_v=max_amplitude_v,
            max_abs_level_v=max_abs_level_v,
            trace_callback=self._trace_callback("GEN"),
        )
        detail = self.gen.idn

        # GUI safety policy: establish a known state immediately after connect.
        # This affects only GUI use; the TwoChannelGenerator driver itself keeps
        # its previous standalone behavior for automated scripts.
        for output_channel in (1, 2, 3, 4):
            self.gen.disable_channel(output_channel)
            self._gen_output_states[output_channel] = False
            self.generator_output_changed.emit(output_channel, False)

        self.status_changed.emit("gen", True, detail)
        self._log("INFO", f"Generator connected: {detail}; all physical outputs forced OFF")
        return detail

    def disconnect_generator(self, silent: bool = False):
        if self.gen is not None:
            try:
                self.gen.close()
            finally:
                self.gen = None

        self._gen_output_states = {1: False, 2: False, 3: False, 4: False}
        self.status_changed.emit("gen", False, "Disconnected")
        if not silent:
            self._log("INFO", "Generator disconnected")

    # --------------------------------------------------------------------- chip

    def _require_chip(self) -> Configuration:
        if self.cfg is None or self.client is None or not self.client.connected:
            raise RuntimeError("Chip is not connected")
        return self.cfg

    def _require_matrix(self) -> PixelMatrixConfiguration:
        self._require_chip()
        if self.matrix_cfg is None:
            raise RuntimeError("Pixel matrix controller is not available")
        return self.matrix_cfg

    def _require_fclk_for_configuration(self):
        # There is no GET_FCLK command. Only block operations when this GUI
        # explicitly set FCLK=0 and therefore knows that the clock is disabled.
        if self._fclk_mhz == 0:
            raise RuntimeError(
                "FCLK is disabled (0 MHz). Re-enable FCLK before changing pixel configuration."
            )

    def set_fclk(self, frequency_mhz: int) -> int:
        self._require_chip()
        assert self.client is not None
        if not self.client.set_fclk(frequency_mhz):
            raise RuntimeError(f"Failed to set FCLK={frequency_mhz} MHz")
        self._fclk_mhz = int(frequency_mhz)
        if self._fclk_mhz > 0:
            self._last_nonzero_fclk_mhz = self._fclk_mhz
        self.fclk_changed.emit(self._fclk_mhz)
        if frequency_mhz == 0:
            self._log(
                "WARNING",
                "FCLK disabled. Pixel configuration still requires FCLK; chip constant writes will temporarily restore it automatically.",
            )
        else:
            self._log("INFO", f"FCLK <- {frequency_mhz} MHz OK")
        return self._fclk_mhz

    def _enable_fclk_for_constant_write_if_needed(self) -> bool:
        """Temporarily restore FCLK when GUI knows it is currently OFF.

        Register/constant writes require a running FCLK. If the user explicitly
        disabled FCLK through this GUI, restore the last known non-zero value
        (100 MHz fallback), perform the write, and let the caller switch it back
        to 0 in a finally block. Unknown FCLK state keeps the historical behavior.
        """
        if self._fclk_mhz != 0:
            return False

        restore_mhz = self._last_nonzero_fclk_mhz or 100
        self._log(
            "INFO",
            f"FCLK is OFF; temporarily restoring {restore_mhz} MHz for chip constant write",
        )
        self.set_fclk(restore_mhz)
        return True

    def _restore_fclk_off_after_constant_write(self, temporary_enable: bool):
        if not temporary_enable:
            return
        self.set_fclk(0)
        self._log("INFO", "FCLK returned to OFF after chip constant write")

    def toggle_fclk(self, enable_frequency_mhz: int = 100) -> int:
        """Toggle FCLK between 0 and the selected non-zero frequency.

        If the current FCLK state is unknown, the first toggle establishes a
        known ON state using enable_frequency_mhz.
        """
        if enable_frequency_mhz == 0:
            raise ValueError("enable_frequency_mhz must be non-zero")
        if self._fclk_mhz is not None and self._fclk_mhz > 0:
            return self.set_fclk(0)
        return self.set_fclk(int(enable_frequency_mhz))

    def stage_pixel_config(self, row: int, col: int, raw_config: int) -> dict:
        """Update one owned pixel in MGPDLab virtual memory only."""
        self._require_fclk_for_configuration()
        matrix = self._require_matrix()
        if not matrix.set_pixel(row=row, col=col, raw_config=raw_config):
            raise RuntimeError(
                f"Failed to stage pixel Col={col} Row={row} value=0x{raw_config:08X}"
            )
        self._log(
            "INFO",
            f"Pixel staged in UPO: Col={col} Row={row} <- 0x{raw_config:08X}",
        )
        self._log(
            "WARNING",
            "Pixels changed through SET_PIXEL_CFG are no longer controlled by the "
            "MGPDLab GUI until UPO is restarted.",
        )
        return {"row": row, "col": col, "value": raw_config}

    def stage_pixel_configs(self, pixel_configs: dict[tuple[int, int], int]) -> dict:
        """Stage all supplied per-pixel PX values in MGPDLab virtual memory."""
        self._require_fclk_for_configuration()
        matrix = self._require_matrix()
        pixel_configs = dict(pixel_configs)
        if not pixel_configs:
            return {"pixels": [], "count": 0}

        def progress(current: int, total: int, row: int, col: int):
            self.pixel_matrix_progress.emit(current, total, row, col)

        count = matrix.set_pixels(pixel_configs, progress_callback=progress)
        pixels = [
            {"row": row, "col": col, "value": raw}
            for (row, col), raw in sorted(pixel_configs.items())
        ]
        self._log(
            "INFO",
            f"Staged {count} locally edited matrix pixel(s) in UPO",
        )
        self._log(
            "WARNING",
            "Pixels changed through SET_PIXEL_CFG are no longer controlled by the "
            "MGPDLab GUI until UPO is restarted.",
        )
        return {"pixels": pixels, "count": count}

    def stage_owned_matrix(self, raw_config: int) -> dict:
        """Load one config into every currently enabled matrix pixel.

        For the project GUI this means Rows 0..31, Cols 16..31.
        """
        self._require_fclk_for_configuration()
        matrix = self._require_matrix()

        def progress(current: int, total: int, row: int, col: int):
            self.pixel_matrix_progress.emit(current, total, row, col)

        count = matrix.set_owned_half(raw_config, progress_callback=progress)
        self._log(
            "INFO",
            f"Pixel config 0x{raw_config:08X} staged for active matrix range: "
            f"{count} pixels (Cols {min(matrix.owned_columns)}..{max(matrix.owned_columns)})",
        )
        self._log(
            "WARNING",
            f"The {count} pixels updated through SET_PIXEL_CFG are no longer controlled "
            "by the MGPDLab GUI until UPO is restarted.",
        )
        return {"value": raw_config, "count": count}

    def stage_full_matrix(self, raw_config: int) -> dict:
        """Stage one 32-bit PX word into all 1024 pixels in UPO memory only."""
        self._require_fclk_for_configuration()
        matrix = self._require_matrix()

        def progress(current: int, total: int, row: int, col: int):
            self.pixel_matrix_progress.emit(current, total, row, col)

        count = matrix.set_full_matrix(raw_config, progress_callback=progress)
        self._log(
            "INFO",
            f"Pixel config 0x{raw_config:08X} staged for the complete 32x32 matrix: "
            f"{count} pixels",
        )
        self._log(
            "WARNING",
            "Pixels changed through SET_PIXEL_CFG are no longer controlled "
            "by the MGPDLab GUI until UPO is restarted. WRITE_TO_CHIP was not sent.",
        )
        return {"value": raw_config, "count": count}

    def write_pixel_matrix_to_chip(self) -> bool:
        """Commit MGPDLab's virtual matrix using SET_PIXEL_CFG WRITE_TO_CHIP."""
        self._require_fclk_for_configuration()
        matrix = self._require_matrix()
        if not matrix.write_to_chip():
            raise RuntimeError("Failed to send SET_PIXEL_CFG WRITE_TO_CHIP")
        self._log(
            "INFO",
            "SET_PIXEL_CFG WRITE_TO_CHIP accepted by UPO. Matrix operations "
            "operate on project-owned Cols 16..31. "
            "The protocol-level commit writes the complete virtual matrix.",
        )
        self._log(
            "WARNING",
            "MGPDLab reports only command acceptance here; lower-level matrix-load "
            "errors may be shown in the UPO window.",
        )
        return True

    def run_get_shot(
        self,
        *,
        configure_omr: bool = False,
        mode_cnt: int = 0,
        mode_read: int = 0b010,
        crw_mode: int = 0,
    ) -> bool:
        """Execute GET_SHOT with optional direct OMR pre-configuration.

        Only MODE_CNT, MODE_READ and CRW_MODE are touched by Python when
        configure_omr=True. DCR and ICR are deliberately not written here.
        GET_SHOT itself remains an MGPDLab/UPO command and may internally load
        the register image configured in MGPDLab.
        """
        self._require_chip()
        assert self.client is not None
        if not self.client.get_shot(
            configure_omr=bool(configure_omr),
            mode_cnt=mode_cnt,
            mode_read=mode_read,
            crw_mode=crw_mode,
        ):
            raise RuntimeError("GET_SHOT failed")

        self._log(
            "INFO",
            "GET_SHOT completed"
            + (
                f" after direct OMR setup: MODE_CNT={int(mode_cnt)}, "
                f"MODE_READ=0b{int(mode_read):03b}, CRW_MODE={int(crw_mode)}"
                if configure_omr
                else " using MGPDLab/UPO OMR settings"
            ),
        )
        return True

    def read_pixel_counters(self, row: int, col: int) -> dict:
        """Read one physical pixel counter word through GET_PIXEL."""
        self._require_chip()
        assert self.client is not None
        result = self.client.get_pixel(row=row, col=col)
        if result is None:
            raise RuntimeError(f"GET_PIXEL failed for Col={col} Row={row}")
        self._log(
            "INFO",
            f"GET_PIXEL Col={col} Row={row}: "
            f"Low={result['low']}, Mid={result['mid']}, High={result['high']}",
        )
        return result

    def read_owned_matrix_counters(self) -> dict:
        """Read GET_PIXEL data for all 512 project-owned pixels.

        The operation is intentionally restricted to the project-owned
        Col=16..31 range even though the low-level GET_PIXEL command supports
        all 32 columns. A single failed pixel aborts the scan so a partially
        read matrix is not silently presented as complete.
        """
        matrix = self._require_matrix()
        assert self.client is not None

        coordinates = [
            (row, col)
            for row in range(32)
            for col in matrix.owned_columns
        ]
        total = len(coordinates)
        pixels: dict[tuple[int, int], dict] = {}

        for current, (row, col) in enumerate(coordinates, start=1):
            result = self.client.get_pixel(row=row, col=col)
            if result is None:
                raise RuntimeError(
                    f"GET_PIXEL failed at Col={col} Row={row} "
                    f"({current}/{total})"
                )
            pixels[(row, col)] = result
            self.matrix_read_progress.emit(current, total, row, col)

        self._log(
            "INFO",
            f"Read counters for {total} project-owned pixels "
            f"(Cols {min(matrix.owned_columns)}..{max(matrix.owned_columns)})",
        )
        return {
            "pixels": pixels,
            "count": total,
            "columns": tuple(matrix.owned_columns),
        }

    def read_chip_snapshot(self) -> dict:
        cfg = self._require_chip()
        register_cache = cfg.read_registers()
        fields = cfg.get_all_data(register_cache=register_cache)
        amux_raw = fields.get("TEST_MUX", 0)
        amux_signal = cfg.get_amux(register_cache=register_cache)

        return {
            "fields": fields,
            "registers": register_cache,
            "amux_signal": amux_signal,
            "amux_raw": amux_raw,
        }

    def apply_chip_changes(
        self,
        changed_fields: dict[str, int],
        amux_signal: str | None = None,
    ) -> dict:
        cfg = self._require_chip()
        temporary_fclk = self._enable_fclk_for_constant_write_if_needed()

        try:
            for name, value in changed_fields.items():
                if name == "TEST_MUX":
                    # TEST_MUX is controlled through the dedicated AMUX control.
                    continue
                if not cfg.set_data(name, value):
                    raise RuntimeError(f"Failed to write {name}={value}")
                self._log("INFO", f"CHIP {name} <- {value} OK")

            if amux_signal is not None:
                if not cfg.set_amux(amux_signal):
                    raise RuntimeError(f"Failed to set AMUX={amux_signal}")
                self._log("INFO", f"AMUX <- {amux_signal} OK")
        finally:
            self._restore_fclk_off_after_constant_write(temporary_fclk)

        return self.read_chip_snapshot()

    def load_chip_defaults(self) -> dict:
        cfg = self._require_chip()
        temporary_fclk = self._enable_fclk_for_constant_write_if_needed()
        try:
            if not cfg.set_default():
                raise RuntimeError("Failed to load default registers")
            self._log("INFO", "Default chip configuration loaded")
        finally:
            self._restore_fclk_off_after_constant_write(temporary_fclk)
        return self.read_chip_snapshot()

    def set_ctrl_static(self, state: int) -> bool:
        cfg = self._require_chip()
        if not cfg.set_ctrl(state):
            raise RuntimeError(f"Failed to set CTRL={state}")
        self._ctrl_static_state = int(state)
        self._ctrl_pwm_enabled = False
        self.ctrl_state_changed.emit(self._ctrl_static_state, self._ctrl_pwm_enabled)
        self._log("INFO", f"CTRL <- static {state} OK")
        return True

    def toggle_ctrl_static(self) -> int:
        """Toggle CTRL static state 0/1. Entering static mode disables PWM.

        With an unknown initial state the first click establishes static 0;
        subsequent clicks alternate 0 -> 1 -> 0.
        """
        if self._ctrl_static_state not in (0, 1):
            target = 0
        else:
            target = 0 if self._ctrl_static_state == 1 else 1
        self.set_ctrl_static(target)
        return target

    def set_ctrl_pwm(self, frequency_khz: int, width_ns: int) -> float:
        cfg = self._require_chip()
        if not cfg.set_ctrl_pwm(frequency_khz, width_ns):
            raise RuntimeError("Failed to set CTRL PWM")
        real_frequency = MGPDClient.ctrl_pwm_real_frequency_khz(frequency_khz)
        self._ctrl_pwm_enabled = True
        self.ctrl_state_changed.emit(self._ctrl_static_state, self._ctrl_pwm_enabled)
        self._log(
            "INFO",
            f"CTRL PWM <- F={frequency_khz} kHz, W={width_ns} ns "
            f"(real F={real_frequency:g} kHz) OK",
        )
        return real_frequency

    def disable_ctrl_pwm(self) -> int:
        """Exit PWM mode by returning CTRL to the last known static state.

        The protocol has no dedicated PWM-OFF command, so SET_CTRL_PIN 0/1 is
        the only supported way to leave PWM. If no static state has been set in
        this GUI session, 0 is used as the safe fallback.
        """
        fallback = self._ctrl_static_state if self._ctrl_static_state in (0, 1) else 0
        self.set_ctrl_static(fallback)
        self._log("INFO", f"CTRL PWM disabled -> static {fallback}")
        return fallback

    def toggle_ctrl_pwm(self, frequency_khz: int, width_ns: int) -> dict:
        if self._ctrl_pwm_enabled is True:
            static_state = self.disable_ctrl_pwm()
            return {
                "pwm_enabled": False,
                "static_state": static_state,
                "real_frequency_khz": None,
            }

        real_frequency = self.set_ctrl_pwm(frequency_khz, width_ns)
        return {
            "pwm_enabled": True,
            "static_state": self._ctrl_static_state,
            "real_frequency_khz": real_frequency,
        }

    # --------------------------------------------------------------- oscilloscope

    def _require_osc(self) -> Oscilloscope:
        if self.osc is None:
            raise RuntimeError("Oscilloscope is not connected")
        return self.osc

    def configure_oscilloscope(self, settings: dict) -> bool:
        osc = self._require_osc()
        osc.configure_frame(**settings)
        self._log("INFO", "Oscilloscope settings applied")
        return True

    def measure_dc(self, channel: int) -> float:
        osc = self._require_osc()
        value = osc.read_dc_level(channel)
        self._log("INFO", f"OSC CH{channel} DC = {value:.9g} V")
        return value

    def save_osc_csv(
        self,
        channels: list[int] | tuple[int, ...],
        output_dir: str | Path,
        filename: str | None = None,
    ) -> Path:
        osc = self._require_osc()
        path = osc.save_csv(channels, output_dir, filename)
        self._log("INFO", f"OSC CSV saved: {path}")
        return path

    def save_osc_screenshot(self, output_path: str | Path) -> Path:
        osc = self._require_osc()
        path = osc.save_screenshot(output_path)
        self._log("INFO", f"OSC screenshot saved: {path}")
        return path

    def capture_oscilloscope_screen_temp(self) -> Path:
        """Capture the current oscilloscope display into the project temp folder."""
        osc = self._require_osc()
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        path = self.temp_dir / "oscilloscope_screen.png"
        path = osc.save_screenshot(path)
        self._log("INFO", "Oscilloscope screen refreshed in GUI")
        return Path(path)

    def run_amux_sweep(
        self,
        signals_to_save: tuple[str, ...] | list[str],
        osc_channel: int,
        delay_s: float = 0.1,
        disable_fclk_during_capture: bool = False,
    ) -> dict:
        """Capture one oscilloscope waveform for each selected AMUX signal.

        The current oscilloscope configuration is intentionally left untouched.
        The AMUX state present before the sweep is restored even if acquisition
        fails part-way through the sequence. Optionally FCLK is forced to 0 for
        the settling delay and waveform capture, then restored before the next
        AMUX switch.
        """
        cfg = self._require_chip()
        self._require_fclk_for_configuration()
        osc = self._require_osc()

        signals = tuple(signals_to_save)
        if not signals:
            raise ValueError("Select at least one AMUX signal")
        if osc_channel not in (1, 2, 3, 4):
            raise ValueError("Oscilloscope channel must be 1, 2, 3 or 4")
        if delay_s < 0:
            raise ValueError("AMUX settling delay must be >= 0 s")

        unknown = [signal for signal in signals if signal not in EO_cfg.AMUX_SIGNALS]
        if unknown:
            raise KeyError(f"Unknown AMUX signal(s): {', '.join(unknown)}")

        sweep_dir = self.temp_dir / "amux_sweep"
        if sweep_dir.exists():
            shutil.rmtree(sweep_dir)
        sweep_dir.mkdir(parents=True, exist_ok=True)

        if disable_fclk_during_capture and self._fclk_mhz is None:
            # With no GET_FCLK command there is no way to recover an unknown
            # frequency. Establish the requested deterministic 100 MHz fallback
            # before the first AMUX switch, then use it for every restore.
            self.set_fclk(100)
            self._log(
                "INFO",
                "FCLK state was unknown; established 100 MHz before AMUX sweep",
            )

        # Preserve the exact raw TEST_MUX state, not only the decoded one-hot
        # signal. This lets us restore even a non-standard/invalid mux state.
        mux_addresses = [addr for addr, _, _ in cfg.regs_fields["TEST_MUX"]]
        mux_cache = cfg.read_registers(mux_addresses)
        previous_amux_raw = cfg.get_data("TEST_MUX", register_cache=mux_cache)
        previous_amux_name = cfg.get_amux(register_cache=mux_cache)

        # There is no GET_FCLK command. If this GUI knows the active non-zero
        # frequency, restore that value after every capture. Otherwise use the
        # requested project fallback of 100 MHz.
        fclk_restore_mhz = (
            self._fclk_mhz
            if self._fclk_mhz is not None and self._fclk_mhz > 0
            else 100
        )
        fclk_is_temporarily_off = False

        captured: dict[str, dict] = {}
        primary_error: Exception | None = None
        restore_error: Exception | None = None

        try:
            total = len(signals)
            for index, signal in enumerate(signals, start=1):
                self.amux_sweep_progress.emit(index, total, signal)

                if not cfg.set_amux(signal):
                    raise RuntimeError(f"Failed to set AMUX={signal}")
                self._log("INFO", f"AMUX sweep {index}/{total}: {signal}")

                if disable_fclk_during_capture:
                    # Required ordering: select AMUX while FCLK is running, then
                    # force FCLK low, wait for analog settling, capture, and
                    # restore FCLK before changing AMUX again.
                    self.set_fclk(0)
                    fclk_is_temporarily_off = True
                    self._log(
                        "INFO",
                        f"AMUX {signal}: FCLK OFF for settling/capture",
                    )

                if delay_s > 0:
                    time.sleep(delay_s)

                # read_waveform() already contains the 5 s waveform timeout and
                # frequent readiness polling from the oscilloscope driver.
                voltage, x_origin, x_increment = osc.read_waveform(osc_channel)
                if not voltage:
                    raise RuntimeError(f"Empty waveform from CH{osc_channel}.")

                if disable_fclk_during_capture:
                    self.set_fclk(int(fclk_restore_mhz))
                    fclk_is_temporarily_off = False
                    self._log(
                        "INFO",
                        f"AMUX {signal}: FCLK restored to {fclk_restore_mhz} MHz",
                    )

                raw_path = sweep_dir / f"{signal}.csv"
                with raw_path.open("w", newline="", encoding="utf-8") as file:
                    writer = csv.writer(file)
                    writer.writerow(["time_s", "voltage_v"])
                    # AMUX signals are acquired sequentially. The absolute
                    # oscilloscope x_origin may vary slightly between
                    # acquisitions even though the timebase itself is
                    # unchanged. Store every temporary waveform on a relative
                    # time axis starting at zero.
                    for point_index, value in enumerate(voltage):
                        writer.writerow([point_index * x_increment, value])

                captured[signal] = {
                    "voltage": voltage,
                    # Keep x_origin only as acquisition metadata for possible
                    # diagnostics. It is intentionally NOT used to align or
                    # validate AMUX sweep waveforms.
                    "x_origin": x_origin,
                    "x_increment": x_increment,
                    "raw_csv": raw_path,
                }
                self._log(
                    "INFO",
                    f"AMUX {signal}: CH{osc_channel} captured {len(voltage)} points",
                )

        except Exception as error:
            primary_error = error

        finally:
            # If acquisition failed while the clock was forced low, re-enable it
            # before restoring TEST_MUX. AMUX/register writes require FCLK.
            if disable_fclk_during_capture and fclk_is_temporarily_off:
                try:
                    self.set_fclk(int(fclk_restore_mhz))
                    fclk_is_temporarily_off = False
                    self._log(
                        "INFO",
                        f"FCLK restored to {fclk_restore_mhz} MHz after sweep interruption",
                    )
                except Exception as error:
                    restore_error = error
                    self._log("ERROR", f"FCLK restore failed: {error}")

            try:
                if not cfg.set_data("TEST_MUX", previous_amux_raw):
                    raise RuntimeError("Failed to restore previous TEST_MUX state")
                restored = previous_amux_name or f"raw 0x{previous_amux_raw:X}"
                self._log("INFO", f"AMUX restored to {restored}")
            except Exception as error:
                if restore_error is None:
                    restore_error = error
                else:
                    restore_error = RuntimeError(
                        f"{restore_error}; AMUX restore also failed: {error}"
                    )
                self._log("ERROR", f"AMUX restore failed: {error}")

        if primary_error is not None:
            if restore_error is not None:
                raise RuntimeError(
                    f"AMUX sweep failed: {primary_error}. "
                    f"Additionally, previous AMUX state could not be restored: {restore_error}"
                ) from primary_error
            raise primary_error

        if restore_error is not None:
            raise restore_error

        # AMUX waveforms are acquired sequentially, therefore small changes of
        # oscilloscope x_origin between acquisitions are expected and are
        # intentionally ignored. Only the sampling interval must remain the
        # same so that samples with the same index represent the same relative
        # time. We do not interpolate a changed time grid silently.
        reference_signal = signals[0]
        reference = captured[reference_signal]
        ref_increment = reference["x_increment"]
        min_length = min(len(captured[signal]["voltage"]) for signal in signals)

        if min_length <= 0:
            raise RuntimeError("AMUX sweep produced no waveform samples")

        for signal in signals[1:]:
            item = captured[signal]
            if not math.isclose(
                item["x_increment"],
                ref_increment,
                rel_tol=1e-9,
                abs_tol=1e-15,
            ):
                raise RuntimeError(
                    "Oscilloscope time increment changed during AMUX sweep: "
                    f"{reference_signal}={ref_increment:g} s, "
                    f"{signal}={item['x_increment']:g} s."
                )

        lengths = {signal: len(captured[signal]["voltage"]) for signal in signals}
        if len(set(lengths.values())) > 1:
            self._log(
                "WARNING",
                f"AMUX waveform lengths differ {lengths}; using common first {min_length} points",
            )

        # Relative AMUX sweep time axis. Absolute x_origin is deliberately
        # discarded so independently acquired traces can be overlaid directly.
        time_axis = [index * ref_increment for index in range(min_length)]
        waveforms = {
            signal: captured[signal]["voltage"][:min_length]
            for signal in signals
        }

        combined_csv = sweep_dir / "combined.csv"
        with combined_csv.open("w", newline="", encoding="utf-8") as file:
            writer = csv.writer(file)
            writer.writerow(["time_s", *signals])
            for index, time_value in enumerate(time_axis):
                writer.writerow(
                    [time_value, *[waveforms[signal][index] for signal in signals]]
                )

        self._log(
            "INFO",
            f"AMUX sweep completed: {len(signals)} signal(s), {min_length} points each",
        )

        return {
            "signals": signals,
            "osc_channel": osc_channel,
            "delay_s": delay_s,
            "disable_fclk_during_capture": bool(disable_fclk_during_capture),
            "fclk_restore_mhz": int(fclk_restore_mhz) if disable_fclk_during_capture else None,
            "time_s": time_axis,
            "waveforms": waveforms,
            "combined_csv": combined_csv,
            "raw_dir": sweep_dir,
        }

    def run_matrix_sweep(
        self,
        pixels_to_sweep: tuple[tuple[int, int], ...] | list[tuple[int, int]],
        global_raw: int,
        sweep_raw: int,
        osc_channel: int,
        delay_s: float = 0.1,
        disable_fclk_during_capture: bool = False,
    ) -> dict:
        """Sweep currently enabled matrix pixels and capture one waveform per pixel.

        Hardware invariant during every capture:
            * current pixel -> sweep_raw
            * every other enabled matrix pixel -> global_raw

        To avoid re-sending the complete matrix before every single capture,
        the enabled matrix range is initialized to global_raw once. Then
        the previously swept pixel is restored to global_raw and only the next
        pixel is changed to sweep_raw before WRITE_TO_CHIP. The physical matrix
        state at each capture is therefore identical to a full global reload,
        with dramatically less MGPDLab traffic.

        TST_IN is selected for the duration of the sweep. The exact previous
        TEST_MUX raw value is restored afterward, including non-one-hot states.
        The enabled matrix range is also returned to global_raw at the end of the sweep.
        """
        cfg = self._require_chip()
        self._require_fclk_for_configuration()
        matrix = self._require_matrix()
        osc = self._require_osc()

        PIXEL_CODEC.validate_raw(global_raw)
        PIXEL_CODEC.validate_raw(sweep_raw)

        pixels = tuple(
            dict.fromkeys((int(row), int(col)) for row, col in pixels_to_sweep)
        )
        if not pixels:
            raise ValueError("Select at least one matrix pixel to sweep")
        for row, col in pixels:
            matrix.validate_owned_pixel(row, col)

        if osc_channel not in (1, 2, 3, 4):
            raise ValueError("Oscilloscope channel must be 1, 2, 3 or 4")
        if delay_s < 0:
            raise ValueError("Matrix settling delay must be >= 0 s")
        if "TST_IN" not in EO_cfg.AMUX_SIGNALS:
            raise KeyError("TST_IN is not present in EO_cfg.AMUX_SIGNALS")

        sweep_dir = self.temp_dir / "matrix_sweep"
        if sweep_dir.exists():
            shutil.rmtree(sweep_dir)
        sweep_dir.mkdir(parents=True, exist_ok=True)

        # If the clock is going to be disabled during captures and its current
        # value is unknown, establish the same deterministic fallback used by
        # AMUX sweep so restoration is always well-defined.
        if disable_fclk_during_capture and self._fclk_mhz is None:
            self.set_fclk(100)
            self._log(
                "INFO",
                "FCLK state was unknown; established 100 MHz before matrix sweep",
            )

        fclk_restore_mhz = (
            self._fclk_mhz
            if self._fclk_mhz is not None and self._fclk_mhz > 0
            else 100
        )
        fclk_is_temporarily_off = False

        # Preserve the complete TEST_MUX word, not only its decoded name.
        mux_addresses = [addr for addr, _, _ in cfg.regs_fields["TEST_MUX"]]
        mux_cache = cfg.read_registers(mux_addresses)
        previous_amux_raw = cfg.get_data("TEST_MUX", register_cache=mux_cache)
        previous_amux_name = cfg.get_amux(register_cache=mux_cache)
        amux_changed = previous_amux_name != "TST_IN"

        captured: dict[tuple[int, int], dict] = {}
        active_sweep_pixel: tuple[int, int] | None = None
        baseline_committed = False
        primary_error: Exception | None = None
        restore_errors: list[str] = []

        try:
            if amux_changed:
                if not cfg.set_amux("TST_IN"):
                    raise RuntimeError("Failed to set AMUX=TST_IN")
                self._log("INFO", "Matrix sweep AMUX <- TST_IN")
            else:
                self._log("INFO", "Matrix sweep: AMUX is already TST_IN")

            # Initial deterministic baseline: every owned pixel is Global.
            self._log(
                "INFO",
                f"Matrix sweep: staging Global 0x{global_raw:08X} to all "
                f"{MATRIX_ROWS * len(matrix.owned_columns)} enabled pixels",
            )
            matrix.set_owned_half(global_raw)
            if not matrix.write_to_chip():
                raise RuntimeError("Failed to write initial Global matrix to chip")
            baseline_committed = True
            self._log("INFO", "Matrix sweep: Global baseline written to chip")

            total = len(pixels)
            for index, (row, col) in enumerate(pixels, start=1):
                self.matrix_sweep_progress.emit(index, total, row, col)

                # Restore the previous sweep pixel in UPO virtual memory. There
                # is no need to commit it separately: the following commit also
                # applies the new current sweep pixel in the same transaction.
                if active_sweep_pixel is not None:
                    prev_row, prev_col = active_sweep_pixel
                    if not matrix.set_pixel(prev_row, prev_col, global_raw):
                        raise RuntimeError(
                            f"Failed to restore previous pixel Col={prev_col} Row={prev_row} "
                            "to Global settings"
                        )

                if not matrix.set_pixel(row, col, sweep_raw):
                    raise RuntimeError(
                        f"Failed to stage Sweep settings at Col={col} Row={row}"
                    )
                # From this point cleanup must restore this pixel even if the
                # subsequent WRITE_TO_CHIP or acquisition fails.
                active_sweep_pixel = (row, col)

                if not matrix.write_to_chip():
                    raise RuntimeError(
                        f"Failed to write matrix for Col={col} Row={row}"
                    )
                self._log(
                    "INFO",
                    f"Matrix sweep {index}/{total}: Col={col} Row={row} "
                    f"Sweep=0x{sweep_raw:08X}, others Global=0x{global_raw:08X}",
                )

                if disable_fclk_during_capture:
                    self.set_fclk(0)
                    fclk_is_temporarily_off = True
                    self._log(
                        "INFO",
                        f"Matrix Col={col} Row={row}: FCLK OFF for settling/capture",
                    )

                if delay_s > 0:
                    time.sleep(delay_s)

                voltage, x_origin, x_increment = osc.read_waveform(osc_channel)
                if not voltage:
                    raise RuntimeError(f"Empty waveform from CH{osc_channel}.")

                if disable_fclk_during_capture:
                    self.set_fclk(int(fclk_restore_mhz))
                    fclk_is_temporarily_off = False
                    self._log(
                        "INFO",
                        f"Matrix Col={col} Row={row}: FCLK restored to "
                        f"{fclk_restore_mhz} MHz",
                    )

                raw_path = sweep_dir / f"col_{col:02d}_row_{row:02d}.csv"
                with raw_path.open("w", newline="", encoding="utf-8") as file:
                    writer = csv.writer(file)
                    writer.writerow(["time_s", "voltage_v"])
                    for point_index, value in enumerate(voltage):
                        writer.writerow([point_index * x_increment, value])

                captured[(row, col)] = {
                    "voltage": voltage,
                    "x_origin": x_origin,
                    "x_increment": x_increment,
                    "raw_csv": raw_path,
                }
                self._log(
                    "INFO",
                    f"Matrix Col={col} Row={row}: CH{osc_channel} captured "
                    f"{len(voltage)} points",
                )

        except Exception as error:
            primary_error = error

        finally:
            # Configuration writes require a running FCLK. If acquisition failed
            # while the clock was forced low, restore it before any cleanup.
            if disable_fclk_during_capture and fclk_is_temporarily_off:
                try:
                    self.set_fclk(int(fclk_restore_mhz))
                    fclk_is_temporarily_off = False
                    self._log(
                        "INFO",
                        f"FCLK restored to {fclk_restore_mhz} MHz after matrix-sweep interruption",
                    )
                except Exception as error:
                    restore_errors.append(f"FCLK restore failed: {error}")
                    self._log("ERROR", restore_errors[-1])

            # Leave the enabled matrix range in the requested Global state. After
            # the initial baseline only the last active sweep pixel can differ.
            if baseline_committed and active_sweep_pixel is not None:
                try:
                    row, col = active_sweep_pixel
                    if not matrix.set_pixel(row, col, global_raw):
                        raise RuntimeError(
                            f"failed to restore Col={col} Row={row} to Global settings"
                        )
                    if not matrix.write_to_chip():
                        raise RuntimeError("failed to commit final Global matrix")
                    self._log(
                        "INFO",
                        "Matrix sweep cleanup: enabled matrix range restored to Global settings",
                    )
                except Exception as error:
                    restore_errors.append(f"Matrix restore failed: {error}")
                    self._log("ERROR", restore_errors[-1])

            if amux_changed:
                try:
                    if not cfg.set_data("TEST_MUX", previous_amux_raw):
                        raise RuntimeError("Failed to restore previous TEST_MUX state")
                    restored = previous_amux_name or f"raw 0x{previous_amux_raw:X}"
                    self._log("INFO", f"AMUX restored to {restored}")
                except Exception as error:
                    restore_errors.append(f"AMUX restore failed: {error}")
                    self._log("ERROR", restore_errors[-1])

        if primary_error is not None:
            if restore_errors:
                raise RuntimeError(
                    f"Matrix sweep failed: {primary_error}. Cleanup issue(s): "
                    + "; ".join(restore_errors)
                ) from primary_error
            raise primary_error

        if restore_errors:
            raise RuntimeError("; ".join(restore_errors))

        if not captured:
            raise RuntimeError("Matrix sweep produced no waveform samples")

        reference_pixel = pixels[0]
        reference = captured[reference_pixel]
        ref_increment = reference["x_increment"]
        min_length = min(len(captured[pixel]["voltage"]) for pixel in pixels)
        if min_length <= 0:
            raise RuntimeError("Matrix sweep produced no waveform samples")

        for pixel in pixels[1:]:
            item = captured[pixel]
            if not math.isclose(
                item["x_increment"],
                ref_increment,
                rel_tol=1e-9,
                abs_tol=1e-15,
            ):
                ref_row, ref_col = reference_pixel
                row, col = pixel
                raise RuntimeError(
                    "Oscilloscope time increment changed during matrix sweep: "
                    f"Col={ref_col} Row={ref_row}: {ref_increment:g} s, "
                    f"Col={col} Row={row}: {item['x_increment']:g} s."
                )

        lengths = {pixel: len(captured[pixel]["voltage"]) for pixel in pixels}
        if len(set(lengths.values())) > 1:
            compact_lengths = {
                f"C{col}R{row}": length
                for (row, col), length in lengths.items()
            }
            self._log(
                "WARNING",
                f"Matrix waveform lengths differ {compact_lengths}; "
                f"using common first {min_length} points",
            )

        # Same policy as AMUX sweep: absolute x_origin is intentionally ignored.
        # Independently captured traces are overlaid by sample index on a
        # relative time axis. A changed x_increment remains an error.
        time_axis = [index * ref_increment for index in range(min_length)]
        waveforms = {
            pixel: captured[pixel]["voltage"][:min_length]
            for pixel in pixels
        }

        headers = [f"Col{col}_Row{row}" for row, col in pixels]
        combined_csv = sweep_dir / "combined.csv"
        with combined_csv.open("w", newline="", encoding="utf-8") as file:
            writer = csv.writer(file)
            writer.writerow(["time_s", *headers])
            for index, time_value in enumerate(time_axis):
                writer.writerow(
                    [time_value, *[waveforms[pixel][index] for pixel in pixels]]
                )

        self._log(
            "INFO",
            f"Matrix sweep completed: {len(pixels)} pixel(s), {min_length} points each",
        )

        return {
            "pixels": pixels,
            "global_raw": int(global_raw),
            "sweep_raw": int(sweep_raw),
            "osc_channel": int(osc_channel),
            "delay_s": float(delay_s),
            "disable_fclk_during_capture": bool(disable_fclk_during_capture),
            "fclk_restore_mhz": (
                int(fclk_restore_mhz) if disable_fclk_during_capture else None
            ),
            "time_s": time_axis,
            "waveforms": waveforms,
            "combined_csv": combined_csv,
            "raw_dir": sweep_dir,
        }

    # ---------------------------------------------------------------- generator

    def _require_gen(self) -> TwoChannelGenerator:
        if self.gen is None:
            raise RuntimeError("Generator is not connected")
        return self.gen

    def configure_generator(self, channel_settings: dict[int, dict]) -> bool:
        gen = self._require_gen()

        for channel in sorted(channel_settings):
            settings = dict(channel_settings[channel])

            # Compatibility with GUI v1/v2 payloads. The old checkbox is no
            # longer used, but silently ignore the key if an older caller sends it.
            settings.pop("output_enabled", None)
            settings.pop("enable_after_config", None)

            # Reconfiguration must never happen while either physical output
            # belonging to this source is active. CH1 owns outputs 1 and 3;
            # CH2 owns outputs 2 and 4.
            physical_outputs = (channel, channel + 2)
            for output_channel in physical_outputs:
                gen.disable_channel(output_channel)
                self._gen_output_states[output_channel] = False
                self.generator_output_changed.emit(output_channel, False)

            gen.configure_channel(
                channel=channel,
                enable_after_config=False,
                **settings,
            )

            # Enforce OFF again after all configuration commands.
            for output_channel in physical_outputs:
                gen.disable_channel(output_channel)
                self._gen_output_states[output_channel] = False
                self.generator_output_changed.emit(output_channel, False)

            self._log(
                "INFO",
                f"GEN CH{channel} configured; OUT{channel} and complementary output are OFF",
            )

        return True

    def set_generator_output(self, output_channel: int, enabled: bool) -> bool:
        gen = self._require_gen()
        if output_channel not in (1, 2, 3, 4):
            raise ValueError("Generator output channel must be 1, 2, 3 or 4")

        if enabled:
            gen.enable_channel(output_channel)
        else:
            gen.disable_channel(output_channel)

        self._gen_output_states[output_channel] = bool(enabled)
        self.generator_output_changed.emit(output_channel, bool(enabled))
        self._log(
            "INFO",
            f"GEN physical output {output_channel} -> {'ON' if enabled else 'OFF'}",
        )
        return bool(enabled)

    def toggle_generator_output(self, output_channel: int) -> bool:
        if output_channel not in (1, 2, 3, 4):
            raise ValueError("Generator output channel must be 1, 2, 3 or 4")
        return self.set_generator_output(
            output_channel,
            not self._gen_output_states[output_channel],
        )

    # ---------------------------------------------------------------- shutdown

    def shutdown(self):
        # Close resources without raising during application shutdown.
        for closer in (
            lambda: self.disconnect_generator(silent=True),
            lambda: self.disconnect_oscilloscope(silent=True),
            lambda: self.disconnect_chip(silent=True),
        ):
            try:
                closer()
            except Exception:
                pass
