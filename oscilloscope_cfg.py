import csv
import time
from pathlib import Path

import pyvisa as visa


DEFAULT_OSC_IDN_SUBSTRING = "DSO9104H"
DEFAULT_OSC_ADDRESS = None
RESOURCE_TIMEOUT = 5000
RESOURCE_CHUNK_SIZE = 1_000_000


class Oscilloscope:
    def __init__(
        self,
        osc_address: str | None = DEFAULT_OSC_ADDRESS,
        idn_substring: str = DEFAULT_OSC_IDN_SUBSTRING,
        timeout_ms: int = RESOURCE_TIMEOUT,
        chunk_size: int = RESOURCE_CHUNK_SIZE,
    ):
        self.osc_address = osc_address
        self.idn_substring = idn_substring
        self.timeout_ms = timeout_ms
        self.chunk_size = chunk_size

        self.rm = visa.ResourceManager("@py")
        self.osc = self._connect_oscilloscope()
        self._configure_resource(self.osc)

        self.idn = self._query("*IDN?")
        print("Connected oscilloscope:", self.idn)
        print("Oscilloscope ready.")

    # ---------------- low-level VISA ----------------

    def _configure_resource(self, resource):
        resource.timeout = self.timeout_ms
        resource.chunk_size = self.chunk_size
        resource.read_termination = "\n"
        resource.write_termination = "\n"

    def _write(self, command: str):
        self.osc.write(command)

    def _query(self, command: str) -> str:
        return self.osc.query(command).strip()

    # ---------------- connection ----------------

    def _connect_oscilloscope(self):
        if self.osc_address is not None:
            osc = self.rm.open_resource(self.osc_address)
            self._configure_resource(osc)

            idn = osc.query("*IDN?").strip()
            print("Found:", idn)

            if self.idn_substring not in idn:
                osc.close()
                self.rm.close()
                raise RuntimeError(
                    f"Wrong instrument. Expected '{self.idn_substring}', got '{idn}'."
                )

            return osc

        resources = self.rm.list_resources("TCPIP?*")

        print("Detected VISA resources:")
        for resource_name in resources:
            print("  ", resource_name)

        for resource_name in resources:
            try:
                osc = self.rm.open_resource(resource_name)
                self._configure_resource(osc)

                idn = osc.query("*IDN?").strip()
                print(f"{resource_name} -> {idn}")

                if self.idn_substring in idn:
                    return osc

                osc.close()

            except Exception as error:
                print(f"Cannot access {resource_name}: {error}")

        self.rm.close()
        raise RuntimeError(
            f"Oscilloscope with IDN substring '{self.idn_substring}' not found."
        )

    # ---------------- validation/helpers ----------------

    @staticmethod
    def _check_channel(channel: int):
        if channel not in (1, 2, 3, 4):
            raise ValueError("Oscilloscope channel must be 1, 2, 3 or 4.")

    @classmethod
    def _check_channels(cls, channels: list[int] | tuple[int, ...]):
        if len(channels) == 0:
            raise ValueError("At least one channel must be selected.")

        for channel in channels:
            cls._check_channel(channel)

    @staticmethod
    def _normalize_slope(trigger_slope: str) -> str:
        slope = trigger_slope.strip().upper()

        aliases = {
            "POS": "POS",
            "RISE": "POS",
            "RISING": "POS",
            "FRONT": "POS",
            "NEG": "NEG",
            "FALL": "NEG",
            "FALLING": "NEG",
            "SPAD": "NEG",
        }

        if slope not in aliases:
            raise ValueError("trigger_slope must be POS or NEG.")

        return aliases[slope]

    @staticmethod
    def _normalize_input(input_mode: str) -> str:
        mode = input_mode.strip().upper()

        aliases = {
            "DC": "DC",
            "1M": "DC",
            "1MOHM": "DC",
            "1MEG": "DC",
            "DC50": "DC50",
            "50": "DC50",
            "50OHM": "DC50",
        }

        if mode not in aliases:
            raise ValueError("Input mode must be DC or DC50.")

        return aliases[mode]

    @staticmethod
    def _strip_scpi_block_header(data: str) -> str:
        data = data.strip()

        if not data.startswith("#"):
            return data

        digits_count = int(data[1])
        length_start = 2
        length_end = length_start + digits_count

        data_length = int(data[length_start:length_end])
        payload_start = length_end
        payload_end = payload_start + data_length

        return data[payload_start:payload_end]

    @staticmethod
    def _next_csv_path(directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)

        indices = []

        for file in directory.glob("*.csv"):
            if file.stem.isdigit():
                indices.append(int(file.stem))

        next_index = max(indices, default=0) + 1
        return directory / f"{next_index}.csv"

    @staticmethod
    def _read_ieee_binary_block(resource) -> bytes:
        """
        Read IEEE 488.2 definite-length binary block.

        Format:
            #<N><length><payload>
        """
        first_two = resource.read_bytes(
            2,
            break_on_termchar=False,
        )

        if not first_two.startswith(b"#"):
            rest = resource.read_raw()
            return first_two + rest

        digits_count = int(first_two[1:2].decode("ascii"))

        length_bytes = resource.read_bytes(
            digits_count,
            break_on_termchar=False,
        )

        payload_length = int(length_bytes.decode("ascii"))

        payload = resource.read_bytes(
            payload_length,
            break_on_termchar=False,
        )

        old_timeout = resource.timeout

        try:
            resource.timeout = 500
            _ = resource.read_bytes(
                1,
                break_on_termchar=False,
            )
        except Exception:
            pass
        finally:
            resource.timeout = old_timeout

        return payload

    # ---------------- public API ----------------

    def configure_frame(
        self,
        channels: list[int] | tuple[int, ...] = (1,),
        trigger_enabled: bool = True,
        trigger_source: int = 1,
        trigger_level_v: float = 0.05,
        trigger_slope: str = "POS",
        average_count: int = 1,
        time_scale_s: float = 20e-9,
        time_offset_s: float = 0,
        voltage_scale_v: float = 0.25,
        voltage_offset_v: float = 0.0,
        input_modes: dict[int, str] | None = None,
        waveform_points: int = 10000,
        run_after_config: bool = True,
        averaging_enabled: bool = True,
        voltage_scale_dict: dict[int, float] | None = None,
        voltage_offset_dict: dict[int, float] | None = None,
    ):
        """Настроить кадр осциллографа без повторного подключения."""
        self._check_channels(channels)
        self._check_channel(trigger_source)

        if average_count < 1:
            raise ValueError("average_count must be >= 1.")

        if time_scale_s <= 0:
            raise ValueError("time_scale_s must be positive.")

        if voltage_scale_v <= 0:
            raise ValueError("voltage_scale_v must be positive.")

        if waveform_points <= 0:
            raise ValueError("waveform_points must be positive.")

        if input_modes is None:
            input_modes = {}

        if voltage_scale_dict is None:
            voltage_scale_dict = {}

        if voltage_offset_dict is None:
            voltage_offset_dict = {}

        for channel, scale in voltage_scale_dict.items():
            self._check_channel(channel)
            if scale <= 0:
                raise ValueError(
                    f"voltage_scale_dict[{channel}] must be positive."
                )

        for channel in voltage_offset_dict:
            self._check_channel(channel)

        trigger_slope = self._normalize_slope(trigger_slope)

        selected_channels = set(channels)

        for channel in range(1, 5):
            if channel in selected_channels:
                self._write(f":CHAN{channel} ON")
            else:
                self._write(f":CHAN{channel} OFF")

        for channel in channels:
            input_mode = self._normalize_input(input_modes.get(channel, "DC"))

            channel_scale_v = voltage_scale_dict.get(channel, voltage_scale_v)
            channel_offset_v = voltage_offset_dict.get(
                channel, voltage_offset_v)

            self._write(f":CHAN{channel}:INP {input_mode}")
            self._write(f":CHAN{channel}:SCAL {channel_scale_v}")
            self._write(f":CHAN{channel}:OFFS {channel_offset_v}")

        self._write(f":TIM:SCAL {time_scale_s}")
        self._write(f":TIM:POS {time_offset_s}")

        if averaging_enabled:
            self._write(f":ACQ:AVER:COUN {average_count}")
            self._write(":ACQ:AVER ON")
            self._write(":ACQ:MODE AVER")
        else:
            self._write(":ACQ:AVER OFF")
            self._write(":ACQ:MODE NORM")

        self._write(":ACQ:POIN:AUTO ON")

        if trigger_enabled:
            self._write(":TRIG:MODE EDGE")
            self._write(f":TRIG:EDGE:SOUR CHAN{trigger_source}")
            self._write(f":TRIG:LEV CHAN{trigger_source}, {trigger_level_v}")
            self._write(f":TRIG:EDGE:SLOP {trigger_slope}")
            self._write(":TRIG:SWE TRIG")
        else:
            self._write(":TRIG:SWE AUTO")

        first_channel = channels[0]
        self._write(f":WAV:SOUR CHAN{first_channel}")
        self._write(":WAV:FORM ASC")
        self._write(":WAV:POIN:MODE RAW")
        self._write(f":WAV:POIN {waveform_points}")

        if run_after_config:
            self._write(":RUN")

        time.sleep(0.2)

        print("Oscilloscope frame prepared.")
        print(f"Channels: {list(channels)}")
        print(f"Trigger enabled: {trigger_enabled}")
        print(f"Averaging enabled: {averaging_enabled}")
        print(f"Averages: {average_count if averaging_enabled else 'OFF'}")

    def read_waveform_from_channel(self, channel: int):
        self._check_channel(channel)

        self._write(f":WAV:SOUR CHAN{channel}")

        for _ in range(5000):
            try:
                complete = self._query(":WAV:COMP?")
                if complete == "100":
                    break
            except Exception:
                break

        x_origin = float(self._query(":WAV:XOR?"))
        x_increment = float(self._query(":WAV:XINC?"))

        raw = self._query(":WAV:DATA?")
        raw = self._strip_scpi_block_header(raw)

        voltage = [
            float(item)
            for item in raw.split(",")
            if item.strip() != ""
        ]

        if len(voltage) == 0:
            raise RuntimeError(f"Empty waveform from CH{channel}.")

        return voltage, x_origin, x_increment

    def read_waveform(self, channel: int):
        """Короткий alias для read_waveform_from_channel()."""
        return self.read_waveform_from_channel(channel)

    def save_csv(
        self,
        channels: list[int] | tuple[int, ...],
        output_dir: str | Path,
        filename: str | None = None,
    ):
        """Сохранить waveform выбранных каналов в CSV."""
        self._check_channels(channels)

        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        waveforms = {}
        x_origins = {}
        x_increments = {}

        for channel in channels:
            voltage, x_origin, x_increment = self.read_waveform_from_channel(channel)

            waveforms[channel] = voltage
            x_origins[channel] = x_origin
            x_increments[channel] = x_increment

        reference_channel = channels[0]
        reference_length = len(waveforms[reference_channel])
        reference_x_origin = x_origins[reference_channel]
        reference_x_increment = x_increments[reference_channel]

        min_length = min(len(waveforms[channel]) for channel in channels)

        if min_length != reference_length:
            print(
                "Warning: channel waveform lengths are different. "
                f"Saving first {min_length} points."
            )

        if filename is None:
            path = self._next_csv_path(output_dir)
        else:
            path = output_dir / filename

        with path.open("w", newline="", encoding="utf-8") as file:
            writer = csv.writer(file)

            header = ["time_s"]
            for channel in channels:
                header.append(f"channel_{channel}_v")

            writer.writerow(header)

            for index in range(min_length):
                time_s = reference_x_origin + index * reference_x_increment

                row = [time_s]
                for channel in channels:
                    row.append(waveforms[channel][index])

                writer.writerow(row)

        print(f"Saved: {path}")
        return path

    def save_oscilloscope_csv(
        self,
        channels: list[int] | tuple[int, ...],
        output_dir: str | Path,
        filename: str | None = None,
    ):
        """Совместимое имя для save_csv()."""
        return self.save_csv(channels, output_dir, filename)

    def read_dc_level(self, channel: int = 1) -> float:
        """Прочитать среднее значение waveform выбранного канала."""
        voltage, _, _ = self.read_waveform_from_channel(channel)

        if not voltage:
            raise RuntimeError(f"Empty waveform from CH{channel}")

        return sum(voltage) / len(voltage)

    def save_screenshot(self, output_path: str | Path):
        """Сохранить экран осциллографа в PNG."""
        output_path = Path(output_path)

        if output_path.suffix.lower() != ".png":
            output_path = output_path.with_suffix(".png")

        output_path.parent.mkdir(parents=True, exist_ok=True)

        old_timeout = self.osc.timeout
        old_read_termination = self.osc.read_termination
        old_chunk_size = self.osc.chunk_size

        try:
            self.osc.timeout = 30000
            self.osc.chunk_size = 20_000_000
            self.osc.read_termination = None

            commands_to_try = [
                ":DISP:DATA? PNG",
                ":DISP:DATA? PNG,COLOR",
                ":DISP:DATA? PNG, COLOR",
                ":DISPLAY:DATA? PNG",
                ":DISPLAY:DATA? PNG,COLOR",
                ":DISPLAY:DATA? PNG, COLOR",
            ]

            last_error = None

            for command in commands_to_try:
                try:
                    print(f"Trying screenshot command: {command}", flush=True)

                    self.osc.write(command)
                    image_data = self._read_ieee_binary_block(self.osc)

                    if len(image_data) < 100:
                        last_error = (
                            f"Too small response from command {command}: "
                            f"{image_data!r}"
                        )
                        print(last_error, flush=True)
                        continue

                    if not image_data.startswith(b"\x89PNG"):
                        last_error = (
                            f"Response from {command} is not PNG. "
                            f"First bytes: {image_data[:20]!r}"
                        )
                        print(last_error, flush=True)
                        continue

                    with output_path.open("wb") as file:
                        file.write(image_data)

                    print(
                        f"Saved oscilloscope screenshot: {output_path}",
                        flush=True,
                    )
                    return output_path

                except Exception as error:
                    last_error = str(error)
                    print(f"Command failed: {command} | {error}", flush=True)

                    try:
                        self.osc.read_termination = "\n"
                        self.osc.timeout = 1000
                        err = self.osc.query(":SYST:ERR?").strip()
                        print(f"SYST:ERR? -> {err}", flush=True)
                    except Exception:
                        pass
                    finally:
                        self.osc.read_termination = None
                        self.osc.timeout = 30000

            raise RuntimeError(
                "Could not read oscilloscope screenshot. "
                f"Last error: {last_error}"
            )

        finally:
            self.osc.timeout = old_timeout
            self.osc.read_termination = old_read_termination
            self.osc.chunk_size = old_chunk_size

    def save_oscilloscope_screenshot(self, output_path: str | Path):
        """Совместимое имя для save_screenshot()."""
        return self.save_screenshot(output_path)

    def close(self):
        if getattr(self, "osc", None) is not None:
            self.osc.close()
            self.osc = None

        if getattr(self, "rm", None) is not None:
            self.rm.close()
            self.rm = None

        print("Oscilloscope connection closed.")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()



# from oscilloscope_cfg import Oscilloscope

# osc = Oscilloscope()

# osc.configure_frame(
#     channels=(1, 2),
#     trigger_enabled=True,
#     trigger_source=1,
#     trigger_level_v=0.05,
#     trigger_slope="POS",
#     average_count=16,
#     time_scale_s=20e-9,
#     voltage_scale_v=0.1,
#     input_modes={
#         1: "DC50",
#         2: "DC",
#     },
# )

# voltage, x_origin, x_increment = osc.read_waveform(1)

# dc = osc.read_dc_level(1)

# osc.save_csv(
#     channels=(1, 2),
#     output_dir="measurements",
# )

# osc.save_screenshot(
#     "measurements/scope.png"
# )

# osc.close()