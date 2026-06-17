from dataclasses import dataclass
import time

import pyvisa as visa


@dataclass
class GeneratorConfig:
    channel: int
    frequency_hz: float
    shape: str
    amplitude_v: float = 0.0
    offset_v: float = 0.0
    rise_time_s: float | None = None
    fall_time_s: float | None = None
    pulse_width_s: float | None = None
    low_level_v: float | None = None
    high_level_v: float | None = None


class TwoChannelGenerator:
    def __init__(
        self,
        gen_address: str | None = None,
        idn_substring: str = "811",
        max_amplitude_v: float = 1.0,
        max_abs_level_v: float = 1.0,
        timeout_ms: int = 5000,
        chunk_size: int = 1_000_000,
        use_source_prefix: bool = True,
    ):
        """
        Generator structure:

            configure_channel(channel=1, ...) -> independent source 1
            configure_channel(channel=2, ...) -> independent source 2

            enable_channel(1) -> OUTP1 ON
            enable_channel(2) -> OUTP2 ON
            enable_channel(3) -> OUTP1:COMP ON
            enable_channel(4) -> OUTP2:COMP ON
        """

        self.gen_address = gen_address
        self.idn_substring = idn_substring
        self.max_amplitude_v = max_amplitude_v
        self.max_abs_level_v = max_abs_level_v
        self.use_source_prefix = use_source_prefix

        self.rm = visa.ResourceManager("@py")
        self.gen = self._connect_generator()

        self.gen.timeout = timeout_ms
        self.gen.chunk_size = chunk_size
        self.gen.read_termination = "\n"
        self.gen.write_termination = "\n"

        self.idn = self._query("*IDN?")
        print("Connected generator:", self.idn)
        print("Generator ready.")

    # ---------------- low-level VISA ----------------

    def _write(self, command: str):
        print("SCPI write:", command)
        self.gen.write(command)

    def _query(self, command: str) -> str:
        return self.gen.query(command).strip()

    # ---------------- connection ----------------

    def _connect_generator(self):
        if self.gen_address is not None:
            gen = self.rm.open_resource(self.gen_address)
            self._configure_resource_before_idn(gen)

            idn = gen.query("*IDN?").strip()
            print("Found:", idn)

            if self.idn_substring not in idn:
                gen.close()
                raise RuntimeError(
                    f"Wrong instrument. Expected '{self.idn_substring}', got '{idn}'."
                )

            return gen

        resources = self.rm.list_resources("TCPIP?*")

        print("Detected VISA resources:")
        for resource_name in resources:
            print(" ", resource_name)

        for resource_name in resources:
            try:
                gen = self.rm.open_resource(resource_name)
                self._configure_resource_before_idn(gen)

                idn = gen.query("*IDN?").strip()
                print(f"{resource_name} -> {idn}")

                if self.idn_substring in idn:
                    return gen

                gen.close()

            except Exception as error:
                print(f"Cannot access {resource_name}: {error}")

        raise RuntimeError(
            f"Generator with IDN substring '{self.idn_substring}' not found."
        )

    @staticmethod
    def _configure_resource_before_idn(resource):
        resource.timeout = 3000
        resource.read_termination = "\n"
        resource.write_termination = "\n"

    # ---------------- validation ----------------

    @staticmethod
    def _normalize_shape(shape: str) -> str:
        shape = shape.strip().upper()

        aliases = {
            "SIN": "SIN",
            "SINE": "SIN",
            "SINUSOID": "SIN",

            "SQU": "SQU",
            "SQUARE": "SQU",
            "SQ": "SQU",

            "PULS": "PULS",
            "PULSE": "PULS",

            "RAMP": "RAMP",

            "NOIS": "NOIS",
            "NOISE": "NOIS",

            "USER": "USER",
            "ARB": "USER",

            "DC": "DC",
        }

        if shape not in aliases:
            raise ValueError(
                "Unsupported shape. Use SIN, SQU, PULS, RAMP, NOIS, USER or DC."
            )

        return aliases[shape]

    @staticmethod
    def _validate_source_channel(channel: int):
        if channel not in (1, 2):
            raise ValueError(
                "configure_channel(...) supports only independent sources 1 and 2. "
                "Use enable_channel(3) or enable_channel(4) for complementary outputs."
            )

    @staticmethod
    def _validate_output_channel(channel: int):
        if channel not in (1, 2, 3, 4):
            raise ValueError(
                "Output channel must be 1, 2, 3 or 4. "
                "1=OUTP1, 2=OUTP2, 3=OUTP1:COMP, 4=OUTP2:COMP."
            )

    @staticmethod
    def _estimate_levels(shape: str, amplitude_v: float, offset_v: float):
        """
        SIN/SQU/RAMP/NOIS/USER:
            amplitude_v is Vpp
            low  = offset - amplitude / 2
            high = offset + amplitude / 2

        PULS:
            amplitude_v is high-low
            low  = offset
            high = offset + amplitude

        DC:
            low = high = offset
        """

        if shape == "PULS":
            low_v = offset_v
            high_v = offset_v + amplitude_v

        elif shape == "DC":
            low_v = offset_v
            high_v = offset_v

        else:
            low_v = offset_v - amplitude_v / 2.0
            high_v = offset_v + amplitude_v / 2.0

        return low_v, high_v

    def _validate_safety(self, shape: str, amplitude_v: float, offset_v: float):
        if amplitude_v < 0:
            raise ValueError("amplitude_v must be non-negative.")

        if amplitude_v > self.max_amplitude_v:
            raise ValueError(
                f"Amplitude limit exceeded: {amplitude_v} V > "
                f"{self.max_amplitude_v} V."
            )

        low_v, high_v = self._estimate_levels(shape, amplitude_v, offset_v)
        self._validate_levels_safety(low_v, high_v)

    def _validate_levels_safety(self, low_v: float, high_v: float):
        if high_v < low_v:
            raise ValueError(
                f"High level must be greater than or equal to low level. "
                f"Got low={low_v} V, high={high_v} V."
            )

        amplitude_v = high_v - low_v

        if amplitude_v > self.max_amplitude_v:
            raise ValueError(
                f"Amplitude limit exceeded: high-low={amplitude_v} V > "
                f"{self.max_amplitude_v} V."
            )

        if abs(low_v) > self.max_abs_level_v or abs(high_v) > self.max_abs_level_v:
            raise ValueError(
                "Output level limit exceeded: "
                f"low={low_v} V, high={high_v} V, "
                f"allowed range is +/-{self.max_abs_level_v} V."
            )

    @staticmethod
    def _square_levels_are_used(
        shape: str,
        low_level_v: float | None,
        high_level_v: float | None,
    ) -> bool:
        return shape == "SQU" and (low_level_v is not None or high_level_v is not None)

    @staticmethod
    def _require_both_square_levels(
        low_level_v: float | None,
        high_level_v: float | None,
    ):
        if low_level_v is None or high_level_v is None:
            raise ValueError(
                "For SQU with level mode, low_level_v and high_level_v "
                "must be specified together."
            )

    # ---------------- SCPI errors ----------------

    def clear_errors(self):
        for _ in range(20):
            try:
                response = self._query(":SYST:ERR?")
            except Exception:
                break

            parts = response.split(",", 1)

            try:
                code = int(parts[0])
            except ValueError:
                break

            if code == 0:
                break

    def read_errors(self):
        errors = []

        for _ in range(20):
            try:
                response = self._query(":SYST:ERR?")
            except Exception:
                break

            parts = response.split(",", 1)

            try:
                code = int(parts[0])
            except ValueError:
                break

            if code == 0:
                break

            errors.append(response)

        return errors

    def check_errors(self):
        errors = self.read_errors()

        if errors:
            raise RuntimeError("Generator SCPI errors:\n" + "\n".join(errors))

    # ---------------- robust command sending ----------------

    def _write_sequence_is_accepted(self, sequence: list[str]) -> bool:
        for command in sequence:
            self.clear_errors()
            self._write(command)
            time.sleep(0.02)

            errors = self.read_errors()
            if errors:
                print("Rejected command:", command)
                print("Reason:", " | ".join(errors))
                return False

        return True

    def _write_first_accepted(self, sequences: list[list[str]], label: str):
        last_sequence = None

        for sequence in sequences:
            last_sequence = sequence

            if self._write_sequence_is_accepted(sequence):
                return sequence

        raise RuntimeError(
            f"No accepted SCPI command variant for {label}. "
            f"Last tried sequence: {last_sequence}"
        )

    @staticmethod
    def _split_root_command(command_body: str):
        """
        'VOLT:OFFS 0.1' -> root='VOLT', rest=':OFFS 0.1'
        'FUNC SIN'      -> root='FUNC', rest=' SIN'
        'FREQ 1e6'      -> root='FREQ', rest=' 1e6'
        """

        colon_pos = command_body.find(":")
        space_pos = command_body.find(" ")

        positions = [pos for pos in (colon_pos, space_pos) if pos != -1]

        if not positions:
            return command_body, ""

        split_pos = min(positions)
        return command_body[:split_pos], command_body[split_pos:]

    def _single_param_sequences(self, channel: int, command_body: str):
        """
        Common SCPI variants for source 1 and source 2.
        """

        sequences = []

        if self.use_source_prefix:
            sequences.append([f":SOUR{channel}:{command_body}"])

        root, rest = self._split_root_command(command_body)
        sequences.append([f":{root}{channel}{rest}"])

        sequences.append([f":INST:NSEL {channel}", f":{command_body}"])

        if channel == 1:
            sequences.append([f":{command_body}"])

        return sequences

    def _send_param(self, channel: int, command_body: str, label: str):
        sequences = self._single_param_sequences(channel, command_body)
        return self._write_first_accepted(sequences, label)

    def _send_pulse_param(
        self,
        channel: int,
        func_pulse_body: str,
        pulse_body: str,
        label: str,
    ):
        sequences = []

        if self.use_source_prefix:
            sequences.append([f":SOUR{channel}:{func_pulse_body}"])
            sequences.append([f":SOUR{channel}:{pulse_body}"])

        root_1, rest_1 = self._split_root_command(func_pulse_body)
        root_2, rest_2 = self._split_root_command(pulse_body)

        sequences.append([f":{root_1}{channel}{rest_1}"])
        sequences.append([f":{root_2}{channel}{rest_2}"])

        sequences.append([f":INST:NSEL {channel}", f":{func_pulse_body}"])
        sequences.append([f":INST:NSEL {channel}", f":{pulse_body}"])

        if channel == 1:
            sequences.append([f":{func_pulse_body}"])
            sequences.append([f":{pulse_body}"])

        return self._write_first_accepted(sequences, label)

    # ---------------- public API ----------------

    def configure_channel(
        self,
        channel: int,
        frequency_hz: float,
        shape: str,
        amplitude_v: float = 0.0,
        offset_v: float = 0.0,
        rise_time_s: float | None = None,
        fall_time_s: float | None = None,
        pulse_width_s: float | None = None,
        enable_after_config: bool = False,
        perec: bool = True,
        low_level_v: float | None = None,
        high_level_v: float | None = None,
    ):
        """
        Configure independent source 1 or 2.

        channel:
            1 or 2

        shape:
            SIN, SQU, PULS, RAMP, NOIS, USER, DC

        amplitude_v:
            SIN/RAMP/NOIS/USER: Vpp
            SQU: Vpp if low_level_v/high_level_v are not used
            PULS: high-low
            DC: normally 0.0

        offset_v:
            SIN/RAMP/NOIS/USER: signal center
            SQU: signal center if low_level_v/high_level_v are not used
            PULS: low level
            DC: constant level

        low_level_v, high_level_v:
            Used for SQU if you want to set low/high directly.
            If they are specified, they override amplitude_v and offset_v for SQU.

        rise_time_s, fall_time_s, pulse_width_s:
            Used only for PULS.
        """

        self._validate_source_channel(channel)

        shape = self._normalize_shape(shape)

        use_square_levels = self._square_levels_are_used(
            shape=shape,
            low_level_v=low_level_v,
            high_level_v=high_level_v,
        )

        if use_square_levels:
            self._require_both_square_levels(low_level_v, high_level_v)
            self._validate_levels_safety(low_level_v, high_level_v)

            amplitude_v = high_level_v - low_level_v
            offset_v = 0.5 * (high_level_v + low_level_v)

        else:
            self._validate_safety(shape, amplitude_v, offset_v)

        if shape != "DC" and frequency_hz <= 0:
            raise ValueError("frequency_hz must be positive.")

        if shape == "PULS":
            if pulse_width_s is None:
                raise ValueError("pulse_width_s is required for PULS shape.")

            if pulse_width_s <= 0:
                raise ValueError("pulse_width_s must be positive.")

            if rise_time_s is not None and rise_time_s <= 0:
                raise ValueError("rise_time_s must be positive.")

            if fall_time_s is not None and fall_time_s <= 0:
                raise ValueError("fall_time_s must be positive.")

        self.clear_errors()

        try:
            self.clear_errors()
            self._write(":ARM:SOUR IMM")
            self.read_errors()
        except Exception:
            pass

        if shape == "DC":
            self._configure_dc(
                channel=channel,
                amplitude_v=amplitude_v,
                offset_v=offset_v,
                perec=perec,
            )

        elif shape == "PULS":
            self._configure_pulse(
                channel=channel,
                frequency_hz=frequency_hz,
                amplitude_v=amplitude_v,
                offset_v=offset_v,
                rise_time_s=rise_time_s,
                fall_time_s=fall_time_s,
                pulse_width_s=pulse_width_s,
            )

        elif shape == "SQU" and use_square_levels:
            self._configure_square_with_levels(
                channel=channel,
                frequency_hz=frequency_hz,
                low_level_v=low_level_v,
                high_level_v=high_level_v,
            )

        else:
            self._configure_standard_shape(
                channel=channel,
                frequency_hz=frequency_hz,
                shape=shape,
                amplitude_v=amplitude_v,
                offset_v=offset_v,
            )

        if enable_after_config:
            self.enable_channel(channel)

        if shape == "SQU" and use_square_levels:
            print(
                f"Configured source {channel}: "
                f"shape={shape}, freq={frequency_hz}, "
                f"low={low_level_v} V, high={high_level_v} V"
            )
        else:
            print(
                f"Configured source {channel}: "
                f"shape={shape}, freq={frequency_hz}, "
                f"amp={amplitude_v} V, offset={offset_v} V"
            )

    def _configure_dc(
        self,
        channel: int,
        amplitude_v: float,
        offset_v: float,
        perec: bool,
    ):
        if perec:
            try:
                self._send_param(channel, "FUNC DC", f"source {channel} DC function")
            except RuntimeError:
                self._send_param(channel, "FUNC USER", f"source {channel} USER function")
                self._send_param(
                    channel,
                    "FUNC:USER CONST",
                    f"source {channel} CONST user function",
                )

        self._send_param(
            channel,
            f"VOLT:OFFS {offset_v}",
            f"source {channel} voltage offset",
        )

        if amplitude_v > 0:
            self._send_param(
                channel,
                f"VOLT {amplitude_v}",
                f"source {channel} voltage amplitude",
            )

    def _configure_standard_shape(
        self,
        channel: int,
        frequency_hz: float,
        shape: str,
        amplitude_v: float,
        offset_v: float,
    ):
        self._send_param(
            channel,
            f"FUNC {shape}",
            f"source {channel} function",
        )

        if shape != "NOIS":
            self._send_param(
                channel,
                f"FREQ {frequency_hz}",
                f"source {channel} frequency",
            )

        self._send_param(
            channel,
            f"VOLT:OFFS {offset_v}",
            f"source {channel} voltage offset",
        )

        self._send_param(
            channel,
            f"VOLT {amplitude_v}",
            f"source {channel} voltage amplitude",
        )

    def _configure_square_with_levels(
        self,
        channel: int,
        frequency_hz: float,
        low_level_v: float,
        high_level_v: float,
    ):
        self._send_param(
            channel,
            "FUNC SQU",
            f"source {channel} square function",
        )

        self._send_param(
            channel,
            f"FREQ {frequency_hz}",
            f"source {channel} frequency",
        )

        self._send_param(
            channel,
            f"VOLT:HIGH {high_level_v}",
            f"source {channel} square high level",
        )

        self._send_param(
            channel,
            f"VOLT:LOW {low_level_v}",
            f"source {channel} square low level",
        )

    def _configure_pulse(
        self,
        channel: int,
        frequency_hz: float,
        amplitude_v: float,
        offset_v: float,
        rise_time_s: float | None,
        fall_time_s: float | None,
        pulse_width_s: float,
    ):
        low_v, high_v = self._estimate_levels("PULS", amplitude_v, offset_v)

        self._send_param(
            channel,
            "FUNC PULS",
            f"source {channel} pulse function",
        )

        self._send_param(
            channel,
            f"FREQ {frequency_hz}",
            f"source {channel} frequency",
        )

        self._send_pulse_param(
            channel,
            f"FUNC:PULS:WIDT {pulse_width_s}",
            f"PULS:WIDT {pulse_width_s}",
            f"source {channel} pulse width",
        )

        if rise_time_s is not None:
            self._send_pulse_param(
                channel,
                f"FUNC:PULS:TRAN {rise_time_s}",
                f"PULS:TRAN {rise_time_s}",
                f"source {channel} pulse rise time",
            )

        if fall_time_s is not None:
            self._send_pulse_param(
                channel,
                f"FUNC:PULS:TRAN:TRA {fall_time_s}",
                f"PULS:TRAN:TRA {fall_time_s}",
                f"source {channel} pulse fall time",
            )

        self._send_param(
            channel,
            f"VOLT:HIGH {high_v}",
            f"source {channel} high voltage",
        )

        self._send_param(
            channel,
            f"VOLT:LOW {low_v}",
            f"source {channel} low voltage",
        )

    def configure_from_object(
        self,
        config: GeneratorConfig,
        enable_after_config: bool = False,
    ):
        self.configure_channel(
            channel=config.channel,
            frequency_hz=config.frequency_hz,
            shape=config.shape,
            amplitude_v=config.amplitude_v,
            offset_v=config.offset_v,
            rise_time_s=config.rise_time_s,
            fall_time_s=config.fall_time_s,
            pulse_width_s=config.pulse_width_s,
            enable_after_config=enable_after_config,
            low_level_v=config.low_level_v,
            high_level_v=config.high_level_v,
        )

    def enable_channel(self, channel: int):
        """
        Enable physical output:

            1 -> OUTP1
            2 -> OUTP2
            3 -> OUTP1:COMP
            4 -> OUTP2:COMP
        """

        self._validate_output_channel(channel)
        self.clear_errors()

        if channel == 1:
            sequences = [[":OUTP1 ON"], [":OUTP ON"]]
            label = "OUTP1 ON"

        elif channel == 2:
            sequences = [[":OUTP2 ON"]]
            label = "OUTP2 ON"

        elif channel == 3:
            sequences = [[":OUTP1:COMP ON"]]
            label = "OUTP1:COMP ON"

        else:
            sequences = [[":OUTP2:COMP ON"]]
            label = "OUTP2:COMP ON"

        self._write_first_accepted(sequences, label)
        print(f"Physical output {channel} ON")

    def disable_channel(self, channel: int):
        """
        Disable physical output:

            1 -> OUTP1
            2 -> OUTP2
            3 -> OUTP1:COMP
            4 -> OUTP2:COMP
        """

        self._validate_output_channel(channel)
        self.clear_errors()

        if channel == 1:
            sequences = [[":OUTP1 OFF"], [":OUTP OFF"]]
            label = "OUTP1 OFF"

        elif channel == 2:
            sequences = [[":OUTP2 OFF"]]
            label = "OUTP2 OFF"

        elif channel == 3:
            sequences = [[":OUTP1:COMP OFF"]]
            label = "OUTP1:COMP OFF"

        else:
            sequences = [[":OUTP2:COMP OFF"]]
            label = "OUTP2:COMP OFF"

        self._write_first_accepted(sequences, label)
        print(f"Physical output {channel} OFF")

    def close(self):
        self.gen.close()
        self.rm.close()
        print("Generator connection closed.")