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

    def __init__(self):
        super().__init__()
        self.client: MGPDClient | None = None
        self.cfg: Configuration | None = None
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

        return self.read_chip_snapshot()

    def load_chip_defaults(self) -> dict:
        cfg = self._require_chip()
        if not cfg.set_default():
            raise RuntimeError("Failed to load default registers")
        self._log("INFO", "Default chip configuration loaded")
        return self.read_chip_snapshot()

    def set_ctrl_static(self, state: int) -> bool:
        cfg = self._require_chip()
        ok = cfg.set_ctrl(state)
        if not ok:
            raise RuntimeError(f"Failed to set CTRL={state}")
        self._log("INFO", f"CTRL <- {state} OK")
        return True

    def set_ctrl_pwm(self, frequency_khz: int, width_ns: int) -> float:
        cfg = self._require_chip()
        if not cfg.set_ctrl_pwm(frequency_khz, width_ns):
            raise RuntimeError("Failed to set CTRL PWM")
        real_frequency = MGPDClient.ctrl_pwm_real_frequency_khz(frequency_khz)
        self._log(
            "INFO",
            f"CTRL PWM <- F={frequency_khz} kHz, W={width_ns} ns "
            f"(real F={real_frequency:g} kHz) OK",
        )
        return real_frequency

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
    ) -> dict:
        """Capture one oscilloscope waveform for each selected AMUX signal.

        The current oscilloscope configuration is intentionally left untouched.
        The AMUX state present before the sweep is restored even if acquisition
        fails part-way through the sequence.
        """
        cfg = self._require_chip()
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

        # Preserve the exact raw TEST_MUX state, not only the decoded one-hot
        # signal. This lets us restore even a non-standard/invalid mux state.
        mux_addresses = [addr for addr, _, _ in cfg.regs_fields["TEST_MUX"]]
        mux_cache = cfg.read_registers(mux_addresses)
        previous_amux_raw = cfg.get_data("TEST_MUX", register_cache=mux_cache)
        previous_amux_name = cfg.get_amux(register_cache=mux_cache)

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

                if delay_s > 0:
                    time.sleep(delay_s)

                # read_waveform() already contains the 5 s waveform timeout and
                # frequent readiness polling from the oscilloscope driver.
                voltage, x_origin, x_increment = osc.read_waveform(osc_channel)
                if not voltage:
                    raise RuntimeError(f"Empty waveform from CH{osc_channel}.")

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
            try:
                if not cfg.set_data("TEST_MUX", previous_amux_raw):
                    raise RuntimeError("Failed to restore previous TEST_MUX state")
                restored = previous_amux_name or f"raw 0x{previous_amux_raw:X}"
                self._log("INFO", f"AMUX restored to {restored}")
            except Exception as error:
                restore_error = error
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
