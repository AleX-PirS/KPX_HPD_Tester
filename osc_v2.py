import csv
import time
from pathlib import Path

import pyvisa as visa


# ---------------- DEFAULT SETTINGS ----------------

DEFAULT_OSC_IDN_SUBSTRING = "DSO9104H"
DEFAULT_OSC_ADDRESS = None

RESOURCE_TIMEOUT = 5000
RESOURCE_CHUNK_SIZE = 1_000_000


# ---------------- LOW LEVEL ----------------

def configure_resource(resource):
    resource.timeout = RESOURCE_TIMEOUT
    resource.chunk_size = RESOURCE_CHUNK_SIZE
    resource.read_termination = "\n"
    resource.write_termination = "\n"


def write(resource, command: str):
    resource.write(command)


def query(resource, command: str) -> str:
    return resource.query(command).strip()


def find_oscilloscope(
    rm: visa.ResourceManager,
    idn_substring: str = DEFAULT_OSC_IDN_SUBSTRING,
):
    resources = rm.list_resources("TCPIP?*")

    print("Detected VISA resources:")
    for resource_name in resources:
        print("  ", resource_name)

    for resource_name in resources:
        try:
            resource = rm.open_resource(resource_name)
            configure_resource(resource)

            idn = query(resource, "*IDN?")
            print(f"{resource_name} -> {idn}")

            if idn_substring in idn:
                return resource

            resource.close()

        except Exception as error:
            print(f"Cannot access {resource_name}: {error}")

    raise RuntimeError(
        f"Oscilloscope with IDN substring '{idn_substring}' not found."
    )


def open_oscilloscope(
    osc_address: str | None = DEFAULT_OSC_ADDRESS,
    idn_substring: str = DEFAULT_OSC_IDN_SUBSTRING,
):
    rm = visa.ResourceManager("@py")

    if osc_address is not None:
        osc = rm.open_resource(osc_address)
        configure_resource(osc)

        idn = query(osc, "*IDN?")
        print("Connected:", idn)

        if idn_substring not in idn:
            osc.close()
            rm.close()
            raise RuntimeError(
                f"Wrong instrument. Expected '{idn_substring}', got '{idn}'."
            )

        return rm, osc

    osc = find_oscilloscope(rm, idn_substring=idn_substring)
    print("Connected:", query(osc, "*IDN?"))
    return rm, osc


def check_channel(channel: int):
    if channel not in (1, 2, 3, 4):
        raise ValueError("Oscilloscope channel must be 1, 2, 3 or 4.")


def check_channels(channels: list[int] | tuple[int, ...]):
    if len(channels) == 0:
        raise ValueError("At least one channel must be selected.")

    for channel in channels:
        check_channel(channel)


def normalize_slope(trigger_slope: str) -> str:
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


def normalize_input(input_mode: str) -> str:
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


def strip_scpi_block_header(data: str) -> str:
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


# ---------------- FIRST MAIN FUNCTION ----------------

def prepare_oscilloscope_frame(
    osc_address: str | None = DEFAULT_OSC_ADDRESS,
    idn_substring: str = DEFAULT_OSC_IDN_SUBSTRING,
    channels: list[int] | tuple[int, ...] = (1,),
    trigger_enabled: bool = True,
    trigger_source: int = 1,
    trigger_level_v: float = 0.05,
    trigger_slope: str = "POS",
    average_count: int = 1,
    time_scale_s: float = 20e-9,
    voltage_scale_v: float = 0.1,
    voltage_offset_v: float = 0.0,
    input_modes: dict[int, str] | None = None,
    waveform_points: int = 10000,
    run_after_config: bool = True,
):
    """
    Первичная подготовка кадра осциллографа.

    Возвращает:
        rm, osc

    Потом osc нужно передать в save_oscilloscope_csv(...).

    Параметры:
        channels:
            какие каналы включить, например (1,), (1, 2), (1, 2, 3)

        trigger_enabled:
            True  -> edge trigger
            False -> free-run / auto sweep

        trigger_source:
            канал триггера

        trigger_level_v:
            уровень триггера

        trigger_slope:
            POS или NEG

        average_count:
            1  -> без усреднения
            >1 -> режим усреднения

        time_scale_s:
            масштаб времени, s/div

        voltage_scale_v:
            общий масштаб напряжения, V/div

        voltage_offset_v:
            общий offset по вертикали для всех включенных каналов

        input_modes:
            словарь нагрузок по каналам:
            {1: "DC50", 2: "DC"}
            Если не задано, для всех выбранных каналов используется DC.

        waveform_points:
            число точек waveform для чтения
    """

    check_channels(channels)
    check_channel(trigger_source)

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

    trigger_slope = normalize_slope(trigger_slope)

    rm, osc = open_oscilloscope(
        osc_address=osc_address,
        idn_substring=idn_substring,
    )

    # Каналы: выбранные включить, остальные выключить
    selected_channels = set(channels)

    for channel in range(1, 5):
        if channel in selected_channels:
            write(osc, f":CHAN{channel} ON")
        else:
            write(osc, f":CHAN{channel} OFF")

    # Единый масштаб и offset для выбранных каналов
    for channel in channels:
        input_mode = normalize_input(input_modes.get(channel, "DC"))

        write(osc, f":CHAN{channel}:INP {input_mode}")
        write(osc, f":CHAN{channel}:SCAL {voltage_scale_v}")
        write(osc, f":CHAN{channel}:OFFS {voltage_offset_v}")

    # Временная база
    write(osc, f":TIM:SCAL {time_scale_s}")
    write(osc, ":TIM:POS 0")

    # Усреднение
    if average_count > 1:
        write(osc, f":ACQ:AVER:COUN {average_count}")
        write(osc, ":ACQ:AVER ON")
        write(osc, ":ACQ:MODE AVER")
    else:
        write(osc, ":ACQ:AVER OFF")
        write(osc, ":ACQ:MODE NORM")

    write(osc, ":ACQ:POIN:AUTO ON")

    # Триггер
    if trigger_enabled:
        write(osc, ":TRIG:MODE EDGE")
        write(osc, f":TRIG:EDGE:SOUR CHAN{trigger_source}")
        write(osc, f":TRIG:LEV CHAN{trigger_source}, {trigger_level_v}")
        write(osc, f":TRIG:EDGE:SLOP {trigger_slope}")

        # Для нормального ожидания события.
        # В твоем рабочем коде применялось TRIG.
        write(osc, ":TRIG:SWE TRIG")
    else:
        # Полностью "выключить" триггер у осциллографа обычно нельзя.
        # Практически это free-run через AUTO sweep.
        write(osc, ":TRIG:SWE AUTO")

    # Настройки чтения waveform
    first_channel = channels[0]
    write(osc, f":WAV:SOUR CHAN{first_channel}")
    write(osc, ":WAV:FORM ASC")
    write(osc, ":WAV:POIN:MODE RAW")
    write(osc, f":WAV:POIN {waveform_points}")

    if run_after_config:
        write(osc, ":RUN")

    time.sleep(0.2)

    print("Oscilloscope frame prepared.")
    print(f"Channels: {list(channels)}")
    print(f"Trigger enabled: {trigger_enabled}")
    print(f"Averages: {average_count}")

    return rm, osc


# ---------------- WAVEFORM READOUT ----------------

def read_waveform_from_channel(osc, channel: int):
    check_channel(channel)

    write(osc, f":WAV:SOUR CHAN{channel}")

    # Проверка готовности waveform.
    # Если команда не поддерживается, чтение все равно будет выполнено.
    for _ in range(5000):
        try:
            complete = query(osc, ":WAV:COMP?")
            if complete == "100":
                break
        except Exception:
            break

    x_origin = float(query(osc, ":WAV:XOR?"))
    x_increment = float(query(osc, ":WAV:XINC?"))

    raw = query(osc, ":WAV:DATA?")
    raw = strip_scpi_block_header(raw)

    voltage = [
        float(item)
        for item in raw.split(",")
        if item.strip() != ""
    ]

    if len(voltage) == 0:
        raise RuntimeError(f"Empty waveform from CH{channel}.")

    return voltage, x_origin, x_increment


def next_csv_path(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)

    indices = []

    for file in directory.glob("*.csv"):
        if file.stem.isdigit():
            indices.append(int(file.stem))

    next_index = max(indices, default=0) + 1
    return directory / f"{next_index}.csv"


# ---------------- SECOND MAIN FUNCTION ----------------

def save_oscilloscope_csv(
    osc,
    channels: list[int] | tuple[int, ...],
    output_dir: str | Path,
    filename: str | None = None,
):
    """
    Сохранение данных из выбранных каналов в CSV.

    Важно:
        Эта функция не подключается заново.
        Эта функция не перенастраивает осциллограф.
        Она использует объект osc, который вернула prepare_oscilloscope_frame(...).

    CSV:
        index, time_s, channel_1_v, channel_2_v, ...
    """

    check_channels(channels)

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    waveforms = {}
    x_origins = {}
    x_increments = {}

    for channel in channels:
        voltage, x_origin, x_increment = read_waveform_from_channel(osc, channel)

        waveforms[channel] = voltage
        x_origins[channel] = x_origin
        x_increments[channel] = x_increment

    # Берем временную ось первого выбранного канала.
    reference_channel = channels[0]
    reference_length = len(waveforms[reference_channel])
    reference_x_origin = x_origins[reference_channel]
    reference_x_increment = x_increments[reference_channel]

    # Если длины разные, сохраняем только общий диапазон.
    min_length = min(len(waveforms[channel]) for channel in channels)

    if min_length != reference_length:
        print(
            "Warning: channel waveform lengths are different. "
            f"Saving first {min_length} points."
        )

    if filename is None:
        path = next_csv_path(output_dir)
    else:
        path = output_dir / filename

    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)

        header = ["index", "time_s"]
        for channel in channels:
            header.append(f"channel_{channel}_v")

        writer.writerow(header)

        for index in range(min_length):
            time_s = reference_x_origin + index * reference_x_increment

            row = [index, time_s]
            for channel in channels:
                row.append(waveforms[channel][index])

            writer.writerow(row)

    print(f"Saved: {path}")
    return path


# ---------------- CLOSE FUNCTION ----------------

def close_oscilloscope(rm, osc):
    osc.close()
    rm.close()
    print("Oscilloscope connection closed.")


# ---------------- EXAMPLE ----------------

if __name__ == "__main__":
    rm, osc = prepare_oscilloscope_frame(
        osc_address=None,
        idn_substring="DSO9104H",

        channels=(1, 2),

        trigger_enabled=True,
        trigger_source=1,
        trigger_level_v=0.05,
        trigger_slope="POS",

        average_count=16,

        time_scale_s=20e-9,

        voltage_scale_v=0.1,
        voltage_offset_v=0.0,

        input_modes={
            1: "DC50",
            2: "DC",
        },

        waveform_points=10000,
    )

    try:
        save_oscilloscope_csv(
            osc=osc,
            channels=(1, 2),
            output_dir="osc_csv",
        )

    finally:
        close_oscilloscope(rm, osc)