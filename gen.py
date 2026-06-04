import time
from dataclasses import dataclass
import pyvisa as visa

@dataclass
class GeneratorConfig:
    channel: int
    frequency_hz: float
    shape: str
    amplitude_v: float
    offset_v: float = 0.0
    rise_time_s: float | None = None
    fall_time_s: float | None = None
    pulse_width_s: float | None = None

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
        use_source_prefix=True:
        commands like :SOUR1:FUNC PULS, :SOUR2:FREQ 1e6
        use_source_prefix=False:
        commands like :FUNC PULS, :FREQ 1e6
        channel is selected by :INST:NSEL <channel>
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

        print("Generator ready.")
    
    # ---------------- low-level VISA ----------------

    def _write(self, command: str):
        self.gen.write(command)

    def _query(self, command: str) -> str:
        return self.gen.query(command).strip()
    
    def _cmd(self, channel: int, command_body: str) -> str:
        if self.use_source_prefix:
            return f":SOUR{channel}:{command_body}"
        
        return f":{command_body}"
    
    def _select_channel_if_needed(self, channel: int):
        if self.use_source_prefix:
            return
        
        # Для генераторов, где параметры задаются для выбранного канала.
        self._write(f":INST:NSEL {channel}")
    
    # ---------------- connection ----------------

    def _connect_generator(self):
        if self.gen_address is not None:
            gen = self.rm.open_resource(self.gen_address)
            self._configure_resource_before_idn(gen)
            
            idn = gen.query("*IDN?").strip()
            print("Connected:", idn)
        
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
    
    # ---------------- safety ----------------

    @staticmethod
    def _normalize_shape(shape: str) -> str:
        shape = shape.strip().upper()

        aliases = {
            "SIN": "SIN",
            "SINE": "SIN",
            "SINUSOID": "SIN",
            "SQU": "SQU",
            "SQUARE": "SQU",
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

    def _validate_channel(self, channel: int):
        if channel not in (1, 2):
            raise ValueError("Generator channel must be 1 or 2.")
 
    def _estimate_levels(self, shape: str, amplitude_v: float, offset_v: float):
        """
        Для SIN/SQU/RAMP/NOIS/USER:
            amplitude_v считается Vpp:
            low = offset - amplitude / 2
            high = offset + amplitude / 2
        Для PULS:
            amplitude_v считается разностью high-low:
            low = offset
            high = offset + amplitude
        Для DC:
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
                f"Amplitude limit exceeded: {amplitude_v} V > {self.max_amplitude_v} V."
            )
    
        low_v, high_v = self._estimate_levels(shape, amplitude_v, offset_v)

        if abs(low_v) > self.max_abs_level_v or abs(high_v) > self.max_abs_level_v:
            raise ValueError(
                "Output level limit exceeded: "
                f"low={low_v} V, high={high_v} V, "
                f"allowed range is +/-{self.max_abs_level_v} V."
            )

    # ---------------- errors ----------------

    def check_errors(self):
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
        
        if errors:
            raise RuntimeError("Generator SCPI errors:\n" + "\n".join(errors))

    # ---------------- public API ----------------

    def configure_channel(
        self,
        channel: int,
        frequency_hz: float,
        shape: str,
        amplitude_v: float,
        offset_v: float = 0.0,
        rise_time_s: float | None = None,
        fall_time_s: float | None = None,
        pulse_width_s: float | None = None,
        enable_after_config: bool = False,
    ):
        """
        Главный метод настройки канала.
        channel:
            1 или 2
        shape:
            SIN, SQU, PULS, RAMP, NOIS, USER, DC
        amplitude_v:
            Для SIN/SQU/RAMP/NOIS/USER это Vpp.
            Для PULS это high-low.
            Жесткое ограничение: не более 1 В.
        offset_v:
            Для PULS это нижний уровень low.
            Для остальных форм это центр сигнала.
        rise_time_s, fall_time_s, pulse_width_s:
            Используются только для PULS.
        """

        self._validate_channel(channel)

        shape = self._normalize_shape(shape)
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
        
        # Безопаснее выключить канал перед перенастройкой.
        self.disable_channel(channel)
        
        self._select_channel_if_needed(channel)
        
        # Никакого внешнего триггера: непрерывная генерация.
        # Эта команда была в твоем старом генераторном коде и подходит для режима IMM.
        self._write(":ARM:SOUR IMM")
        
        if shape == "DC":
            self._write(self._cmd(channel, "FUNC DC"))
            self._write(self._cmd(channel, f"VOLT:OFFS {offset_v}"))
        
        elif shape == "PULS":
            low_v, high_v = self._estimate_levels(shape, amplitude_v, offset_v)
        
            self._write(self._cmd(channel, "FUNC PULS"))
            self._write(self._cmd(channel, f"FREQ {frequency_hz}"))
        
            self._write(self._cmd(channel, f"FUNC:PULS:WIDT {pulse_width_s}"))
        
            if rise_time_s is not None:
                self._write(self._cmd(channel, f"FUNC:PULS:TRAN {rise_time_s}"))
        
            if fall_time_s is not None:
                self._write(self._cmd(channel, f"FUNC:PULS:TRAN:TRA {fall_time_s}"))
        
            self._write(self._cmd(channel, f"VOLT:HIGH {high_v}"))
            self._write(self._cmd(channel, f"VOLT:LOW {low_v}"))
        
        else:
            self._write(self._cmd(channel, f"FUNC {shape}"))
            
            if shape != "NOIS":
                self._write(self._cmd(channel, f"FREQ {frequency_hz}"))
            
            self._write(self._cmd(channel, f"VOLT:OFFS {offset_v}"))
            self._write(self._cmd(channel, f"VOLT {amplitude_v}"))
        
        self.check_errors()
        
        if enable_after_config:
            self.enable_channel(channel)
        
        print(
            f"Configured CH{channel}: "
            f"shape={shape}, freq={frequency_hz}, "
            f"amp={amplitude_v} V, offset={offset_v} V"
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
    )
    
    def enable_channel(self, channel: int):
        self._validate_channel(channel)
        self._write(f":OUTP{channel} ON")
        self.check_errors()
        print(f"CH{channel} output ON")

    def disable_channel(self, channel: int):
        self._validate_channel(channel)
        self._write(f":OUTP{channel} OFF")
        self.check_errors()
        print(f"CH{channel} output OFF")