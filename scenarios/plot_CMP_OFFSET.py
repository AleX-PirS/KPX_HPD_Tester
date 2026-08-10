import csv
import re
import concurrent.futures
import math
from functools import lru_cache
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence, Union, List

import matplotlib.pyplot as plt
import numpy as np


# =============================================================================
# Folder / metadata conventions
# =============================================================================

# Example:
# csv_data_OFFSET_ANALYSE_CMPD_200_VB5_500_LSB_250_VC5_512
FOLDER_RE = re.compile(
    r"_CMPD_(?P<cmpd>\d+)"
    r"_VB5_(?P<bias>\d+)"
    r"_LSB_(?P<lsb>\d+)"
    r"_VC5_(?P<vc5>\d+)"
    r"$",
    re.IGNORECASE,
)

METADATA_FILENAMES = (
    "test_info.csv",
    "test_metadata.csv",
    "metadata.csv",
    "info.csv",
)

THRESHOLD_WAVEFORM_FILENAMES = (
    "threshold_measurement.csv",
    "threshold.csv",
)


@dataclass(frozen=True)
class RunInfo:
    folder: Path
    cmpd: int
    bias: int
    lsb: int
    vc5: int


# =============================================================================
# Small helpers
# =============================================================================

def _as_path_list(
    value: Union[str, Path, Sequence[Union[str, Path]]]
) -> list[Path]:
    if isinstance(value, (str, Path)):
        return [Path(value)]
    return [Path(v) for v in value]


def _normalize_filter(value):
    """
    None / 'all' -> no filtering.
    int          -> one allowed value.
    iterable     -> several allowed values.
    """
    if value is None:
        return None

    if isinstance(value, str):
        if value.lower() == "all":
            return None
        return {int(value)}

    if isinstance(value, (int, np.integer)):
        return {int(value)}

    return {int(v) for v in value}


def parse_run_folder(folder: Union[str, Path]) -> RunInfo:
    folder = Path(folder)
    match = FOLDER_RE.search(folder.name)

    if match is None:
        raise ValueError(
            "Folder name does not match expected pattern:\n"
            "  ..._CMPD_<code>_VB5_<code>_LSB_<code>_VC5_<code>\n"
            f"Got: {folder.name}"
        )

    return RunInfo(
        folder=folder,
        cmpd=int(match.group("cmpd")),
        bias=int(match.group("bias")),
        lsb=int(match.group("lsb")),
        vc5=int(match.group("vc5")),
    )


def _contains_numeric_csv(folder: Path) -> bool:
    try:
        return any(
            p.is_file() and p.suffix.lower() == ".csv" and p.stem.isdigit()
            for p in folder.iterdir()
        )
    except OSError:
        return False


def discover_folders(
    sources: Union[str, Path, Sequence[Union[str, Path]]],
    recursive: bool = True,
) -> list[str]:
    """
    Accepts either:
      1) data folders themselves;
      2) one or more parent folders containing many test folders.

    Returns all folders matching:
      ..._CMPD_x_VB5_x_LSB_x_VC5_x
    """
    found: dict[str, RunInfo] = {}

    for source in _as_path_list(sources):
        if not source.exists():
            print(f"Warning: path does not exist: {source}")
            continue

        if source.is_dir():
            try:
                info = parse_run_folder(source)
                if _contains_numeric_csv(source):
                    found[str(source.resolve())] = info
            except ValueError:
                pass

            iterator = source.rglob("*") if recursive else source.iterdir()

            for candidate in iterator:
                if not candidate.is_dir():
                    continue

                try:
                    info = parse_run_folder(candidate)
                except ValueError:
                    continue

                if _contains_numeric_csv(candidate):
                    found[str(candidate.resolve())] = info

    infos = sorted(
        found.values(),
        key=lambda r: (r.cmpd, r.bias, r.lsb, r.vc5, r.folder.name),
    )

    return [str(info.folder) for info in infos]


def select_folders(
    sources: Union[str, Path, Sequence[Union[str, Path]]],
    cmpd=None,
    bias=None,
    lsb=None,
    vc5=None,
    recursive: bool = True,
) -> list[str]:
    """
    Select test families by folder parameters.

    Examples
    --------
    All LSB values for threshold=200, BIAS=500:
        select_folders(DATA_ROOTS, cmpd=200, bias=500, lsb="all", vc5=512)

    One exact run:
        select_folders(DATA_ROOTS, cmpd=200, bias=500, lsb=250, vc5=512)

    Everything for threshold=200:
        select_folders(DATA_ROOTS, cmpd=200)
    """
    cmpd_filter = _normalize_filter(cmpd)
    bias_filter = _normalize_filter(bias)
    lsb_filter = _normalize_filter(lsb)
    vc5_filter = _normalize_filter(vc5)

    result = []

    for folder in discover_folders(sources, recursive=recursive):
        info = parse_run_folder(folder)

        if cmpd_filter is not None and info.cmpd not in cmpd_filter:
            continue
        if bias_filter is not None and info.bias not in bias_filter:
            continue
        if lsb_filter is not None and info.lsb not in lsb_filter:
            continue
        if vc5_filter is not None and info.vc5 not in vc5_filter:
            continue

        result.append(folder)

    return result


def print_selection(directories):
    directories = _as_path_list(directories)

    if not directories:
        print("No folders selected.")
        return

    print(f"Selected folders: {len(directories)}")
    for directory in directories:
        try:
            info = parse_run_folder(directory)
            print(
                f"  CMPD={info.cmpd:4d}, "
                f"VB5={info.bias:4d}, "
                f"LSB={info.lsb:4d}, "
                f"VC5={info.vc5:4d}  ->  {info.folder.name}"
            )
        except ValueError:
            print(f"  {directory}")


def find_trim_codes(directory: Union[str, Path]) -> list[int]:
    directory = Path(directory)
    codes = []

    for path in directory.glob("*.csv"):
        if path.stem.isdigit():
            codes.append(int(path.stem))

    return sorted(set(codes))


def _limit_codes(available_codes: Sequence[int], codes="all") -> list[int]:
    if codes is None or codes == "all":
        return list(available_codes)

    allowed = _normalize_filter(codes)
    return [code for code in available_codes if code in allowed]


# =============================================================================
# Measurement file loading
# =============================================================================

def _first_row_is_numeric(path: Path) -> bool:
    with path.open("r", newline="", encoding="utf-8-sig") as file:
        row = next(csv.reader(file), None)

    if row is None:
        return False

    try:
        for value in row:
            float(value)
        return True
    except ValueError:
        return False


@lru_cache(maxsize=32)
def load_file(code, directory: str):
    """
    Load one trim-code measurement.

    Current file format:
        time_s, channel_1_v, channel_2_v

    channel_1_v = comparator output
    channel_2_v = sine input

    Returns:
        time, out, inp
    """
    fname = Path(directory) / f"{code}.csv"

    if not fname.exists():
        raise FileNotFoundError(fname)

    skiprows = 0 if _first_row_is_numeric(fname) else 1
    data = np.loadtxt(fname, delimiter=",", skiprows=skiprows)

    if data.ndim == 1:
        data = data.reshape(1, -1)

    if data.shape[1] < 3:
        raise ValueError(
            f"{fname} must contain at least 3 columns: "
            "time_s, comparator output, sine input."
        )

    return data[:, 0], data[:, 1], data[:, 2]


# =============================================================================
# Threshold metadata
# =============================================================================

@lru_cache(maxsize=256)
def load_test_info(directory: Union[str, Path]) -> dict[str, str]:
    directory = Path(directory)

    metadata_path = None
    for filename in METADATA_FILENAMES:
        candidate = directory / filename
        if candidate.exists():
            metadata_path = candidate
            break

    if metadata_path is None:
        return {}

    info = {}

    with metadata_path.open("r", newline="", encoding="utf-8-sig") as file:
        rows = list(csv.reader(file))

    if not rows:
        return info

    # Expected format:
    # parameter,value
    # DAC_CMP_D_voltage_v,0.4123
    start = 1 if len(rows[0]) >= 2 and rows[0][0].strip().lower() == "parameter" else 0

    for row in rows[start:]:
        if len(row) < 2:
            continue
        key = row[0].strip()
        value = row[1].strip()
        if key:
            info[key] = value

    return info


def _metadata_float(info: dict[str, str], candidates: Sequence[str]):
    normalized = {key.strip().lower(): value for key, value in info.items()}

    for candidate in candidates:
        value = normalized.get(candidate.lower())
        if value is None:
            continue

        try:
            return float(value)
        except ValueError:
            continue

    return None


@lru_cache(maxsize=256)
def load_threshold_voltage(directory: Union[str, Path]) -> float:
    """
    Read the actual measured comparator threshold voltage.

    Preferred source is test_info.csv. Several key names are supported so the
    analyzer stays compatible if the test script changes the exact metadata key.

    If metadata does not contain the threshold, threshold_measurement.csv is
    used as a fallback and the mean voltage is calculated.
    """
    directory = Path(directory)
    info = load_test_info(directory)

    try:
        run = parse_run_folder(directory)
        cmp_key = "D"
    except ValueError:
        run = None
        cmp_key = "D"

    candidates = (
        f"DAC_CMP_{cmp_key}_voltage_v",
        f"DAC_CMP_{cmp_key}_voltage",
        "threshold_voltage_v",
        "threshold_v",
        "thd_voltage_v",
        "thd_v",
        "vth_v",
        "vth",
    )

    threshold = _metadata_float(info, candidates)
    if threshold is not None:
        return threshold

    # More permissive fallback:
    # any metadata parameter containing both "voltage" and either "CMP_D",
    # "threshold", "thd" or "vth".
    for key, raw_value in info.items():
        key_lower = key.lower()

        if "voltage" not in key_lower:
            continue

        if not any(token in key_lower for token in ("cmp_d", "threshold", "thd", "vth")):
            continue

        try:
            return float(raw_value)
        except ValueError:
            pass

    # Fallback to the recorded threshold waveform.
    for filename in THRESHOLD_WAVEFORM_FILENAMES:
        path = directory / filename
        if not path.exists():
            continue

        skiprows = 0 if _first_row_is_numeric(path) else 1
        data = np.loadtxt(path, delimiter=",", skiprows=skiprows)

        if data.ndim == 1:
            data = data.reshape(1, -1)

        if data.shape[1] < 2:
            continue

        return float(np.mean(data[:, 1]))

    raise FileNotFoundError(
        f"Cannot find measured threshold voltage in '{directory}'. "
        "Expected test_info.csv with DAC_CMP_D_voltage_v / threshold_voltage_v "
        "or threshold_measurement.csv."
    )


# =============================================================================
# Comparator switching and offset calculation
# =============================================================================

def _robust_low_high(out: np.ndarray) -> tuple[float, float]:
    """
    Robust approximation of output low/high levels.
    """
    if out.size == 0:
        return np.nan, np.nan

    low = float(np.percentile(out, 10))
    high = float(np.percentile(out, 90))
    return low, high


def resolve_switch_level(
    out: np.ndarray,
    output_supply_v: float | None = None,
    switch_level_fraction: float = 0.5,
) -> float:
    """
    Comparator output switching level.

    If output_supply_v is specified:
        Vswitch = switch_level_fraction * output_supply_v

    This is the mode requested for 0.5 * VDD switching detection.

    If output_supply_v is None, the function falls back to:
        low + fraction * (high - low)

    where low/high are estimated from the measured waveform.
    """
    if not (0.0 < switch_level_fraction < 1.0):
        raise ValueError("switch_level_fraction must be between 0 and 1.")

    if output_supply_v is not None:
        if output_supply_v <= 0:
            raise ValueError("output_supply_v must be positive.")
        return float(output_supply_v * switch_level_fraction)

    low, high = _robust_low_high(out)
    return low + switch_level_fraction * (high - low)


def find_switches(
    out,
    min_amplitude=0.2,
    output_supply_v=None,
    switch_level_fraction=0.5,
):
    """
    Compatibility helper returning transition sample indices.

    Unlike the old implementation, the threshold is controlled explicitly:
        switch_level_fraction * output_supply_v

    If output_supply_v=None, measured low/high levels are used.
    """
    out = np.asarray(out, dtype=float)

    low, high = _robust_low_high(out)
    if not np.isfinite(low) or not np.isfinite(high):
        return np.array([], dtype=int), np.array([], dtype=int)

    if high - low < min_amplitude:
        return np.array([], dtype=int), np.array([], dtype=int)

    level = resolve_switch_level(
        out,
        output_supply_v=output_supply_v,
        switch_level_fraction=switch_level_fraction,
    )

    # Requested switching level must actually lie inside the output swing.
    if not (low <= level <= high):
        return np.array([], dtype=int), np.array([], dtype=int)

    below = out < level
    above_or_equal = out >= level

    rise = np.where(below[:-1] & above_or_equal[1:])[0]
    fall = np.where(above_or_equal[:-1] & below[1:])[0]

    return rise, fall


def _interpolate_crossing_times(
    time: np.ndarray,
    signal: np.ndarray,
    indices: np.ndarray,
    level: float,
) -> np.ndarray:
    """
    Linear interpolation between samples surrounding each threshold crossing.
    """
    if len(indices) == 0:
        return np.array([], dtype=float)

    times = []

    for index in indices:
        if index < 0 or index + 1 >= len(signal):
            continue

        t0 = time[index]
        t1 = time[index + 1]
        y0 = signal[index]
        y1 = signal[index + 1]

        dy = y1 - y0
        if dy == 0:
            times.append(float(t0))
            continue

        fraction = (level - y0) / dy
        times.append(float(t0 + fraction * (t1 - t0)))

    return np.asarray(times, dtype=float)


def _debounce_times(times: np.ndarray, min_interval_s: float) -> np.ndarray:
    if min_interval_s <= 0 or len(times) <= 1:
        return times

    kept = [times[0]]

    for value in times[1:]:
        if value - kept[-1] >= min_interval_s:
            kept.append(value)

    return np.asarray(kept, dtype=float)


def find_switch_crossings(
    time,
    out,
    edge="both",
    min_amplitude=0.2,
    output_supply_v=None,
    switch_level_fraction=0.5,
    min_switch_interval_s=0.0,
):
    """
    Return interpolated comparator output switching times.
    """
    if edge not in ("rise", "fall", "both"):
        raise ValueError("edge must be 'rise', 'fall' or 'both'.")

    time = np.asarray(time, dtype=float)
    out = np.asarray(out, dtype=float)

    if len(time) != len(out):
        raise ValueError("time and out must have the same length.")

    rise_idx, fall_idx = find_switches(
        out,
        min_amplitude=min_amplitude,
        output_supply_v=output_supply_v,
        switch_level_fraction=switch_level_fraction,
    )

    switch_level = resolve_switch_level(
        out,
        output_supply_v=output_supply_v,
        switch_level_fraction=switch_level_fraction,
    )

    rise_times = _interpolate_crossing_times(time, out, rise_idx, switch_level)
    fall_times = _interpolate_crossing_times(time, out, fall_idx, switch_level)

    rise_times = _debounce_times(rise_times, min_switch_interval_s)
    fall_times = _debounce_times(fall_times, min_switch_interval_s)

    if edge == "rise":
        return rise_times
    if edge == "fall":
        return fall_times

    return np.sort(np.concatenate((rise_times, fall_times)))


def compute_offset_events(
    time,
    out,
    threshold_v,
    inp,
    edge="both",
    min_amplitude=0.2,
    output_supply_v=None,
    switch_level_fraction=0.5,
    propagation_delay_s=0.0,
    min_switch_interval_s=0.0,
):
    """
    Calculate offset for every comparator switching event.

    Offset sign is kept compatible with the old analyzer:
        offset = VTH - VIN_at_switch

    Propagation delay correction:
        t_input = t_output_switch - propagation_delay_s

    Therefore a positive propagation_delay_s means the comparator output appears
    later than the physical input threshold crossing.
    """
    time = np.asarray(time, dtype=float)
    out = np.asarray(out, dtype=float)
    inp = np.asarray(inp, dtype=float)

    if len(time) != len(inp) or len(time) != len(out):
        raise ValueError("time, out and inp must have equal length.")

    switch_times = find_switch_crossings(
        time,
        out,
        edge=edge,
        min_amplitude=min_amplitude,
        output_supply_v=output_supply_v,
        switch_level_fraction=switch_level_fraction,
        min_switch_interval_s=min_switch_interval_s,
    )

    if len(switch_times) == 0:
        return np.array([], dtype=float)

    input_times = switch_times - propagation_delay_s

    valid = (
        (input_times >= time[0])
        & (input_times <= time[-1])
    )

    input_times = input_times[valid]
    if len(input_times) == 0:
        return np.array([], dtype=float)

    input_at_switch = np.interp(input_times, time, inp)

    return float(threshold_v) - input_at_switch


def compute_offset(
    time,
    out,
    thd,
    inp,
    edge="both",
    min_amplitude=0.2,
    output_supply_v=None,
    switch_level_fraction=0.5,
    propagation_delay_s=0.0,
    min_switch_interval_s=0.0,
):
    """
    Mean comparator offset.

    The argument name 'thd' is preserved for compatibility with the old script,
    but it is now a scalar measured threshold voltage from test_info.csv.
    """
    offsets = compute_offset_events(
        time=time,
        out=out,
        threshold_v=thd,
        inp=inp,
        edge=edge,
        min_amplitude=min_amplitude,
        output_supply_v=output_supply_v,
        switch_level_fraction=switch_level_fraction,
        propagation_delay_s=propagation_delay_s,
        min_switch_interval_s=min_switch_interval_s,
    )

    if len(offsets) == 0:
        return np.nan

    return float(np.mean(offsets))


# =============================================================================
# Analysis cache
# =============================================================================

_ANALYSIS_CACHE = {}


def _freeze_filter(value):
    if value is None or value == "all":
        return "all"
    if isinstance(value, (int, np.integer)):
        return (int(value),)
    return tuple(sorted(int(v) for v in value))


def clear_analysis_cache():
    """
    Clear all in-memory caches.

    Call this after measurement CSV or metadata files are changed while the
    same Python process is still running.
    """
    _ANALYSIS_CACHE.clear()
    load_file.cache_clear()
    load_test_info.cache_clear()
    load_threshold_voltage.cache_clear()
    print("Analysis caches cleared.")


# =============================================================================
# Batch processing
# =============================================================================

def process_code(args):
    """
    Process one trim code.

    Tuple is used so the function remains executor-friendly.
    """
    (
        folder,
        code,
        threshold_v,
        edge,
        min_amplitude,
        output_supply_v,
        switch_level_fraction,
        propagation_delay_s,
        min_switch_interval_s,
    ) = args

    try:
        time, out, inp = load_file(code, folder)

        offset = compute_offset(
            time,
            out,
            threshold_v,
            inp,
            edge=edge,
            min_amplitude=min_amplitude,
            output_supply_v=output_supply_v,
            switch_level_fraction=switch_level_fraction,
            propagation_delay_s=propagation_delay_s,
            min_switch_interval_s=min_switch_interval_s,
        )

        if np.isfinite(offset):
            return code, offset

    except Exception as error:
        print(f"Warning: {folder}, code {code}: {error}")

    return None


def collect_data_from_folders(
    directories,
    edge="both",
    min_amplitude=0.2,
    max_workers=None,
    output_supply_v=None,
    switch_level_fraction=0.5,
    propagation_delay_s=0.0,
    min_switch_interval_s=0.0,
    codes="all",
    use_cache=True,
):
    """
    Returns:
        {folder: [(trim_code, mean_offset), ...]}

    Only numeric CSV files that actually exist are processed.
    """
    directories = [str(Path(p).resolve()) for p in _as_path_list(directories)]

    cache_key = (
        tuple(directories),
        edge,
        float(min_amplitude),
        None if output_supply_v is None else float(output_supply_v),
        float(switch_level_fraction),
        float(propagation_delay_s),
        float(min_switch_interval_s),
        _freeze_filter(codes),
    )

    if use_cache and cache_key in _ANALYSIS_CACHE:
        cached = _ANALYSIS_CACHE[cache_key]
        return {folder: list(pairs) for folder, pairs in cached.items()}

    jobs = []
    results = {}

    for folder in directories:
        try:
            threshold_v = load_threshold_voltage(folder)
        except Exception as error:
            print(f"Skipping {folder}: {error}")
            continue

        trim_codes = _limit_codes(find_trim_codes(folder), codes=codes)

        if not trim_codes:
            print(f"Skipping {folder}: no trim-code CSV files found.")
            continue

        results[folder] = []

        for code in trim_codes:
            jobs.append(
                (
                    folder,
                    code,
                    threshold_v,
                    edge,
                    min_amplitude,
                    output_supply_v,
                    switch_level_fraction,
                    propagation_delay_s,
                    min_switch_interval_s,
                )
            )

    if not jobs:
        return {}

    # ThreadPool avoids Windows ProcessPool spawn issues and works well here
    # because a large part of the workload is CSV I/O.
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(process_code, job): job[0]
            for job in jobs
        }

        for future in concurrent.futures.as_completed(future_map):
            folder = future_map[future]
            result = future.result()

            if result is not None:
                results[folder].append(result)

    results = {
        folder: sorted(pairs, key=lambda pair: pair[0])
        for folder, pairs in results.items()
        if pairs
    }

    if use_cache:
        _ANALYSIS_CACHE[cache_key] = {
            folder: tuple(pairs)
            for folder, pairs in results.items()
        }

    return results


# =============================================================================
# Plot labels
# =============================================================================

def _varying_fields(directories) -> set[str]:
    infos = []

    for directory in directories:
        try:
            infos.append(parse_run_folder(directory))
        except ValueError:
            pass

    if not infos:
        return set()

    varying = set()

    for field in ("cmpd", "bias", "lsb", "vc5"):
        values = {getattr(info, field) for info in infos}
        if len(values) > 1:
            varying.add(field)

    return varying


def _folder_label(directory, varying=None):
    try:
        info = parse_run_folder(directory)
    except ValueError:
        return Path(directory).name

    if varying is None:
        varying = {"cmpd", "bias", "lsb", "vc5"}

    parts = []

    if "cmpd" in varying:
        parts.append(f"CMPD={info.cmpd}")
    if "bias" in varying:
        parts.append(f"VB5={info.bias}")
    if "lsb" in varying:
        parts.append(f"LSB={info.lsb}")
    if "vc5" in varying:
        parts.append(f"VC5={info.vc5}")

    if not parts:
        parts.append(Path(directory).name)

    return ", ".join(parts)


# =============================================================================
# Original plotting functions, adapted to the new data format
# =============================================================================

def plot_offset_curve(
    directories: Union[str, List[str]] = "./csv_data_CMP",
    edge="both",
    min_amplitude=0.2,
    output_supply_v=None,
    switch_level_fraction=0.5,
    propagation_delay_s=0.0,
    min_switch_interval_s=0.0,
    codes="all",
):
    """
    Calibration curves: trim code -> comparator offset.
    One curve is drawn for every selected folder.
    """
    directories = [str(p) for p in _as_path_list(directories)]

    data = collect_data_from_folders(
        directories,
        edge=edge,
        min_amplitude=min_amplitude,
        output_supply_v=output_supply_v,
        switch_level_fraction=switch_level_fraction,
        propagation_delay_s=propagation_delay_s,
        min_switch_interval_s=min_switch_interval_s,
        codes=codes,
    )

    if not data:
        print("No data to plot.")
        return

    varying = _varying_fields(data.keys())

    plt.figure(figsize=(11, 6))

    for folder, pairs in data.items():
        codes_x = [pair[0] for pair in pairs]
        offsets = [pair[1] for pair in pairs]

        plt.plot(
            codes_x,
            offsets,
            "o-",
            markersize=3,
            linewidth=1,
            label=_folder_label(folder, varying),
        )

    plt.axhline(0.0, linewidth=1)
    plt.xlabel("Trim code")
    plt.ylabel("Offset voltage, V")
    plt.title(
        "Comparator offset calibration "
        f"(edge={edge}, OUT={switch_level_fraction:.3g}*VDD, "
        f"delay={propagation_delay_s:g} s)"
    )
    plt.grid(True)

    if len(data) > 1:
        plt.legend()

    plt.tight_layout()
    plt.show()


def plot_offset_sorted(
    directories: Union[str, List[str]] = "./csv_data_CMP",
    edge="both",
    min_amplitude=0.2,
    label_every=1,
    output_supply_v=None,
    switch_level_fraction=0.5,
    propagation_delay_s=0.0,
    min_switch_interval_s=0.0,
    codes="all",
):
    """
    Same data, sorted by offset instead of trim code.
    """
    directories = [str(p) for p in _as_path_list(directories)]

    data = collect_data_from_folders(
        directories,
        edge=edge,
        min_amplitude=min_amplitude,
        output_supply_v=output_supply_v,
        switch_level_fraction=switch_level_fraction,
        propagation_delay_s=propagation_delay_s,
        min_switch_interval_s=min_switch_interval_s,
        codes=codes,
    )

    if not data:
        print("No data to plot.")
        return

    varying = _varying_fields(data.keys())

    plt.figure(figsize=(12, 6))

    for folder, pairs in data.items():
        pairs = sorted(pairs, key=lambda pair: pair[1])

        sorted_offsets = [pair[1] for pair in pairs]
        x_values = range(1, len(sorted_offsets) + 1)

        plt.plot(
            x_values,
            sorted_offsets,
            "o-",
            markersize=3,
            linewidth=1,
            label=_folder_label(folder, varying),
        )

        if label_every > 0:
            for index, (code, offset) in enumerate(pairs, start=1):
                if (index - 1) % label_every == 0:
                    plt.text(
                        index,
                        offset,
                        f" {code}",
                        fontsize=8,
                        ha="center",
                        va="top",
                    )

    plt.axhline(0.0, linewidth=1)
    plt.xlabel("Order number, min offset to max offset")
    plt.ylabel("Offset voltage, V")
    plt.title(f"Sorted comparator offset (edge={edge})")
    plt.grid(True)

    if len(data) > 1:
        plt.legend()

    plt.tight_layout()
    plt.show()


def plot_offset_histogram(
    code,
    directories: Union[str, List[str]] = "./csv_data_CMP",
    edge="both",
    min_amplitude=0.2,
    bins=20,
    output_supply_v=None,
    switch_level_fraction=0.5,
    propagation_delay_s=0.0,
    min_switch_interval_s=0.0,
):
    """
    Event-by-event offset distribution for one trim code.
    """
    directories = [str(p) for p in _as_path_list(directories)]
    varying = _varying_fields(directories)

    plt.figure(figsize=(9, 5))

    plotted = 0

    for folder in directories:
        try:
            threshold_v = load_threshold_voltage(folder)
            time, out, inp = load_file(code, folder)

            offsets = compute_offset_events(
                time,
                out,
                threshold_v,
                inp,
                edge=edge,
                min_amplitude=min_amplitude,
                output_supply_v=output_supply_v,
                switch_level_fraction=switch_level_fraction,
                propagation_delay_s=propagation_delay_s,
                min_switch_interval_s=min_switch_interval_s,
            )

        except FileNotFoundError:
            print(f"File {code}.csv not found in {folder}")
            continue
        except Exception as error:
            print(f"Skipping {folder}: {error}")
            continue

        if len(offsets) == 0:
            print(f"Code {code} in {folder}: no switching events found.")
            continue

        plt.hist(
            offsets,
            bins=bins,
            alpha=0.5,
            label=_folder_label(folder, varying),
        )
        plotted += 1

    if plotted == 0:
        print("No data to plot.")
        plt.close()
        return

    plt.xlabel("VTH - VIN at switching, V")
    plt.ylabel("Event count")
    plt.title(f"Offset distribution for trim code {code} (edge={edge})")
    plt.grid(True, alpha=0.3)

    if plotted > 1:
        plt.legend()

    plt.tight_layout()
    plt.show()


def plot_offset_histogram_all(
    directories: Union[str, List[str]] = "./csv_data_CMP",
    edge="both",
    min_amplitude=0.2,
    bins=20,
    output_supply_v=None,
    switch_level_fraction=0.5,
    propagation_delay_s=0.0,
    min_switch_interval_s=0.0,
    codes="all",
):
    """
    Distribution of mean offsets across all trim codes.
    """
    directories = [str(p) for p in _as_path_list(directories)]

    data = collect_data_from_folders(
        directories,
        edge=edge,
        min_amplitude=min_amplitude,
        output_supply_v=output_supply_v,
        switch_level_fraction=switch_level_fraction,
        propagation_delay_s=propagation_delay_s,
        min_switch_interval_s=min_switch_interval_s,
        codes=codes,
    )

    if not data:
        print("No data to plot.")
        return

    varying = _varying_fields(data.keys())

    plt.figure(figsize=(9, 5))

    for folder, pairs in data.items():
        offsets = [pair[1] for pair in pairs]

        plt.hist(
            offsets,
            bins=bins,
            alpha=0.5,
            label=_folder_label(folder, varying),
        )

    plt.xlabel("Mean offset voltage, V")
    plt.ylabel("Trim code count")
    plt.title(f"Offset distribution across trim codes (edge={edge})")
    plt.grid(True, alpha=0.3)

    if len(data) > 1:
        plt.legend()

    plt.tight_layout()
    plt.show()



def plot_raw(
    directory,
    code,
    t_lim=None,
    decimate=1,
    layout="overlay",
    show_threshold=True,
    show_switch_level=True,
    mark_switches=False,
    edge="both",
    min_amplitude=0.2,
    output_supply_v=None,
    switch_level_fraction=0.5,
    propagation_delay_s=0.0,
    min_switch_interval_s=0.0,
):
    """
    Plot RAW waveform data from one trim-code test.

    Parameters
    ----------
    directory:
        One measurement folder.
    code:
        Trim code, e.g. 7 -> reads 7.csv.
    t_lim:
        Optional displayed time range: (t_min, t_max).
    decimate:
        Plot every Nth sample. Analysis markers are still calculated from the
        full-resolution waveform. Use 1 for truly raw plotting.
    layout:
        "overlay"  -> OUT and IN on one axes.
        "subplots" -> OUT and IN on two aligned axes.
    show_threshold:
        Draw measured internal DAC threshold VTH from test_info.csv.
    show_switch_level:
        Draw comparator-output detection level, normally 0.5 * VDD.
    mark_switches:
        Mark interpolated OUT switching instants and corrected IN sampling
        instants (t_out - propagation_delay_s).
    """
    if decimate < 1:
        raise ValueError("decimate must be >= 1")
    if layout not in ("overlay", "subplots"):
        raise ValueError("layout must be 'overlay' or 'subplots'.")

    directory = str(Path(directory))
    time, out, inp = load_file(int(code), directory)

    threshold_v = None
    if show_threshold:
        try:
            threshold_v = load_threshold_voltage(directory)
        except Exception as error:
            print(f"Warning: VTH is not available: {error}")

    switch_level_v = None
    if show_switch_level or mark_switches:
        try:
            switch_level_v = resolve_switch_level(
                out,
                output_supply_v=output_supply_v,
                switch_level_fraction=switch_level_fraction,
            )
        except Exception as error:
            print(f"Warning: switching level is not available: {error}")

    plot_time = time[::decimate]
    plot_out = out[::decimate]
    plot_inp = inp[::decimate]

    if t_lim is not None:
        mask = (plot_time >= t_lim[0]) & (plot_time <= t_lim[1])
        plot_time = plot_time[mask]
        plot_out = plot_out[mask]
        plot_inp = plot_inp[mask]

    info_text = Path(directory).name

    switch_times = np.array([], dtype=float)
    if mark_switches:
        switch_times = find_switch_crossings(
            time,
            out,
            edge=edge,
            min_amplitude=min_amplitude,
            output_supply_v=output_supply_v,
            switch_level_fraction=switch_level_fraction,
            min_switch_interval_s=min_switch_interval_s,
        )

        if t_lim is not None:
            switch_times = switch_times[
                (switch_times >= t_lim[0]) & (switch_times <= t_lim[1])
            ]

    if layout == "overlay":
        fig, ax = plt.subplots(figsize=(13, 6))

        ax.plot(plot_time, plot_out, linewidth=1.0, label="OUT comparator")
        ax.plot(plot_time, plot_inp, linewidth=1.0, label="IN sine")

        if threshold_v is not None:
            ax.axhline(
                threshold_v,
                linestyle="--",
                linewidth=1.2,
                label=f"VTH={threshold_v:.6f} V",
            )

        if show_switch_level and switch_level_v is not None:
            ax.axhline(
                switch_level_v,
                linestyle=":",
                linewidth=1.2,
                label=f"OUT switch={switch_level_v:.6f} V",
            )

        if mark_switches and len(switch_times):
            out_at_switch = np.interp(switch_times, time, out)
            input_times = switch_times - propagation_delay_s
            valid = (input_times >= time[0]) & (input_times <= time[-1])
            input_times = input_times[valid]
            switch_times_valid = switch_times[valid]
            input_at_switch = np.interp(input_times, time, inp)

            ax.scatter(
                switch_times_valid,
                np.interp(switch_times_valid, time, out),
                s=18,
                label="OUT crossings",
                zorder=5,
            )
            ax.scatter(
                switch_times_valid,
                input_at_switch,
                s=18,
                marker="x",
                label="VIN used for offset",
                zorder=5,
            )

        ax.set_xlabel("Time, s")
        ax.set_ylabel("Voltage, V")
        ax.set_title(f"RAW comparator test, trim={code}\n{info_text}")
        ax.grid(True, alpha=0.3)
        ax.legend(loc="best")
        fig.tight_layout()
        plt.show()
        return fig, ax

    fig, axes = plt.subplots(
        2,
        1,
        figsize=(13, 8),
        sharex=True,
        constrained_layout=True,
    )

    ax_out, ax_in = axes

    ax_out.plot(plot_time, plot_out, linewidth=1.0, label="OUT comparator")
    if show_switch_level and switch_level_v is not None:
        ax_out.axhline(
            switch_level_v,
            linestyle=":",
            linewidth=1.2,
            label=f"OUT switch={switch_level_v:.6f} V",
        )

    ax_in.plot(plot_time, plot_inp, linewidth=1.0, label="IN sine")
    if threshold_v is not None:
        ax_in.axhline(
            threshold_v,
            linestyle="--",
            linewidth=1.2,
            label=f"VTH={threshold_v:.6f} V",
        )

    if mark_switches and len(switch_times):
        input_times = switch_times - propagation_delay_s
        valid = (input_times >= time[0]) & (input_times <= time[-1])
        input_times = input_times[valid]
        switch_times_valid = switch_times[valid]

        ax_out.scatter(
            switch_times_valid,
            np.interp(switch_times_valid, time, out),
            s=18,
            zorder=5,
            label="OUT crossings",
        )
        ax_in.scatter(
            switch_times_valid,
            np.interp(input_times, time, inp),
            s=18,
            marker="x",
            zorder=5,
            label="VIN used for offset",
        )

    ax_out.set_ylabel("OUT, V")
    ax_in.set_ylabel("IN, V")
    ax_in.set_xlabel("Time, s")

    ax_out.grid(True, alpha=0.3)
    ax_in.grid(True, alpha=0.3)
    ax_out.legend(loc="best")
    ax_in.legend(loc="best")
    fig.suptitle(f"RAW comparator test, trim={code}\n{info_text}")

    plt.show()
    return fig, axes


def _plot_offset_dataset_on_axis(ax, data, title=None, legend=True):
    """Internal helper: plot already calculated offset curves on one axes."""
    if not data:
        return

    varying = _varying_fields(data.keys())

    for folder, pairs in data.items():
        pairs = sorted(pairs, key=lambda pair: pair[0])
        x = [pair[0] for pair in pairs]
        y = [pair[1] for pair in pairs]

        ax.plot(
            x,
            y,
            "o-",
            markersize=3,
            linewidth=1,
            label=_folder_label(folder, varying),
        )

    ax.axhline(0.0, linewidth=1)
    ax.set_xlabel("Trim code")
    ax.set_ylabel("Offset voltage, V")
    if title:
        ax.set_title(title)
    ax.grid(True, alpha=0.3)

    if legend and len(data) > 1:
        ax.legend(fontsize=8)


def plot_grouped_families(
    sources,
    group_by="cmpd",
    mode="subplots",
    cmpd="all",
    bias="all",
    lsb="all",
    vc5=None,
    edge="both",
    min_amplitude=0.2,
    output_supply_v=None,
    switch_level_fraction=0.5,
    propagation_delay_s=0.0,
    min_switch_interval_s=0.0,
    codes="all",
    subplot_cols=3,
    figsize=None,
    show=True,
):
    """
    Flexible family comparison.

    group_by:
        "cmpd", "bias", "lsb" or "vc5".

    mode:
        "separate" -> one matplotlib Figure per group; all figures are created
                      first and shown together at the end.
        "subplots" -> all groups in one Figure as separate axes.
        "overlay"  -> every selected curve on one axes.

    This function calculates the selected data only once, then reuses the
    result for all plotting modes.
    """
    field_map = {
        "cmpd": "cmpd",
        "threshold": "cmpd",
        "bias": "bias",
        "vb5": "bias",
        "lsb": "lsb",
        "vc5": "vc5",
    }

    key = str(group_by).lower()
    if key not in field_map:
        raise ValueError("group_by must be cmpd, bias, lsb or vc5.")
    group_field = field_map[key]

    if mode not in ("separate", "subplots", "overlay"):
        raise ValueError("mode must be 'separate', 'subplots' or 'overlay'.")

    directories = select_folders(
        sources,
        cmpd=cmpd,
        bias=bias,
        lsb=lsb,
        vc5=vc5,
    )

    if not directories:
        print("No folders selected.")
        return None

    print_selection(directories)

    data = collect_data_from_folders(
        directories,
        edge=edge,
        min_amplitude=min_amplitude,
        output_supply_v=output_supply_v,
        switch_level_fraction=switch_level_fraction,
        propagation_delay_s=propagation_delay_s,
        min_switch_interval_s=min_switch_interval_s,
        codes=codes,
    )

    if not data:
        print("No valid offset data.")
        return None

    groups = {}
    for folder, pairs in data.items():
        info = parse_run_folder(folder)
        group_value = getattr(info, group_field)
        groups.setdefault(group_value, {})[folder] = pairs

    groups = dict(sorted(groups.items(), key=lambda item: item[0]))

    group_label = {
        "cmpd": "CMPD",
        "bias": "VB5",
        "lsb": "LSB",
        "vc5": "VC5",
    }[group_field]

    if mode == "overlay":
        if figsize is None:
            figsize = (13, 7)

        fig, ax = plt.subplots(figsize=figsize)
        _plot_offset_dataset_on_axis(
            ax,
            data,
            title=f"All selected families, grouped by {group_label}",
            legend=True,
        )
        fig.tight_layout()

        if show:
            plt.show()

        return fig, ax, data

    if mode == "subplots":
        group_count = len(groups)
        cols = max(1, min(int(subplot_cols), group_count))
        rows = math.ceil(group_count / cols)

        if figsize is None:
            figsize = (6.2 * cols, 4.8 * rows)

        fig, axes = plt.subplots(
            rows,
            cols,
            figsize=figsize,
            squeeze=False,
        )

        flat_axes = axes.ravel()

        for ax, (group_value, group_data) in zip(flat_axes, groups.items()):
            _plot_offset_dataset_on_axis(
                ax,
                group_data,
                title=f"{group_label}={group_value}",
                legend=True,
            )

        for ax in flat_axes[len(groups):]:
            ax.set_visible(False)

        fig.suptitle(
            f"Comparator offset families, group={group_label}, edge={edge}",
            fontsize=13,
        )
        fig.tight_layout()

        if show:
            plt.show()

        return fig, axes, data

    # separate
    figures = []

    for group_value, group_data in groups.items():
        current_figsize = figsize or (11, 6)
        fig, ax = plt.subplots(figsize=current_figsize)

        _plot_offset_dataset_on_axis(
            ax,
            group_data,
            title=f"{group_label}={group_value}",
            legend=True,
        )

        fig.tight_layout()
        figures.append((fig, ax))

    if show:
        # One blocking show for all figures is noticeably more convenient than
        # calling plt.show() inside every loop iteration.
        plt.show()

    return figures, data


def plot_cmpd_families(
    sources,
    cmpds=(200, 512, 850),
    bias="all",
    lsb="all",
    vc5=None,
    mode="subplots",
    **plot_kwargs,
):
    """
    Convenience wrapper for comparing threshold-code groups.

    Typical use:
        - mode="separate": three separate Figures for CMPD=200/512/850
        - mode="subplots": three axes in one Figure
        - mode="overlay": all selected curves on one axes
    """
    return plot_grouped_families(
        sources=sources,
        group_by="cmpd",
        mode=mode,
        cmpd=cmpds,
        bias=bias,
        lsb=lsb,
        vc5=vc5,
        **plot_kwargs,
    )


def plot_bias_families(
    sources,
    cmpd,
    biases="all",
    lsb="all",
    vc5=None,
    mode="subplots",
    **plot_kwargs,
):
    """Compare BIAS groups for one selected threshold code."""
    return plot_grouped_families(
        sources=sources,
        group_by="bias",
        mode=mode,
        cmpd=cmpd,
        bias=biases,
        lsb=lsb,
        vc5=vc5,
        **plot_kwargs,
    )


def plot_overlay(
    codes,
    directory="./csv_data_CMP",
    t_lim=None,
    decimate=1,
    alpha=1.0,
    output_supply_v=None,
    switch_level_fraction=0.5,
):
    """
    Overlay comparator outputs for selected trim codes.

    New format:
      - measured VTH is shown as a horizontal line;
      - channel 2 is shown as IN;
      - channel 1 for each trim code is shown as OUT.
    """
    directory = str(directory)

    if codes == "all":
        codes = find_trim_codes(directory)

    if not codes:
        print("No trim codes selected.")
        return

    threshold_v = load_threshold_voltage(directory)

    first_code = codes[0]
    time_ref, out_ref, inp_ref = load_file(first_code, directory)

    if decimate > 1:
        time_ref = time_ref[::decimate]
        inp_ref = inp_ref[::decimate]

    if t_lim is not None:
        mask = (time_ref >= t_lim[0]) & (time_ref <= t_lim[1])
        time_ref = time_ref[mask]
        inp_ref = inp_ref[mask]

    plt.figure(figsize=(12, 5))

    plt.plot(time_ref, inp_ref, linewidth=1.2, label="IN")
    plt.axhline(
        threshold_v,
        linestyle="--",
        linewidth=1.2,
        label=f"VTH = {threshold_v:.6f} V",
    )

    if len(codes) > 10:
        alpha = min(alpha, 0.5)

    for code in codes:
        time, out, _ = load_file(code, directory)

        if decimate > 1:
            time = time[::decimate]
            out = out[::decimate]

        if t_lim is not None:
            mask = (time >= t_lim[0]) & (time <= t_lim[1])
            time = time[mask]
            out = out[mask]

        plt.plot(
            time,
            out,
            linewidth=0.8,
            alpha=alpha,
            label=f"Trim {code}" if len(codes) <= 10 else None,
        )

    plt.xlabel("Time, s")
    plt.ylabel("Voltage, V")
    plt.title("Comparator switching overlay")
    plt.grid(True, alpha=0.3)
    plt.legend(loc="best")
    plt.tight_layout()
    plt.show()


def save_offset_data_to_csv(
    directories,
    output_filename,
    edge="both",
    min_amplitude=0.2,
    output_supply_v=None,
    switch_level_fraction=0.5,
    propagation_delay_s=0.0,
    min_switch_interval_s=0.0,
    codes="all",
):
    """
    Wide CSV:
        trim_code, folder_1, folder_2, ...
    """
    directories = [str(p) for p in _as_path_list(directories)]

    data = collect_data_from_folders(
        directories,
        edge=edge,
        min_amplitude=min_amplitude,
        output_supply_v=output_supply_v,
        switch_level_fraction=switch_level_fraction,
        propagation_delay_s=propagation_delay_s,
        min_switch_interval_s=min_switch_interval_s,
        codes=codes,
    )

    if not data:
        print("No data to save.")
        return

    folder_offsets = {}
    all_codes = set()

    for folder, pairs in data.items():
        folder_offsets[folder] = {
            code: offset
            for code, offset in pairs
        }
        all_codes.update(folder_offsets[folder])

    sorted_codes = sorted(all_codes)

    headers = ["trim_code"] + [
        Path(folder).name
        for folder in folder_offsets
    ]

    with open(output_filename, "w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(headers)

        for code in sorted_codes:
            row = [code]

            for folder in folder_offsets:
                offset = folder_offsets[folder].get(code)
                row.append("" if offset is None else f"{offset:.9g}")

            writer.writerow(row)

    print(f"Saved {len(sorted_codes)} trim codes to {output_filename}")


def save_offset_long_csv(
    directories,
    output_filename,
    edge="both",
    min_amplitude=0.2,
    output_supply_v=None,
    switch_level_fraction=0.5,
    propagation_delay_s=0.0,
    min_switch_interval_s=0.0,
    codes="all",
):
    """
    Long-form CSV for later pandas/statistical analysis.

    Columns:
        CMPD, VB5, LSB, VC5, threshold_voltage_v, trim_code, offset_v, folder
    """
    directories = [str(p) for p in _as_path_list(directories)]

    data = collect_data_from_folders(
        directories,
        edge=edge,
        min_amplitude=min_amplitude,
        output_supply_v=output_supply_v,
        switch_level_fraction=switch_level_fraction,
        propagation_delay_s=propagation_delay_s,
        min_switch_interval_s=min_switch_interval_s,
        codes=codes,
    )

    if not data:
        print("No data to save.")
        return

    rows = []

    for folder, pairs in data.items():
        info = parse_run_folder(folder)
        threshold_v = load_threshold_voltage(folder)

        for trim_code, offset in pairs:
            rows.append(
                [
                    info.cmpd,
                    info.bias,
                    info.lsb,
                    info.vc5,
                    threshold_v,
                    trim_code,
                    offset,
                    info.folder.name,
                ]
            )

    rows.sort(key=lambda row: (row[0], row[1], row[2], row[3], row[5]))

    with open(output_filename, "w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)

        writer.writerow(
            [
                "CMPD",
                "VB5",
                "LSB",
                "VC5",
                "threshold_voltage_v",
                "trim_code",
                "offset_v",
                "folder",
            ]
        )

        writer.writerows(rows)

    print(f"Saved {len(rows)} rows to {output_filename}")


# =============================================================================
# Family-oriented convenience functions
# =============================================================================

def plot_lsb_family(
    sources,
    cmpd,
    bias,
    vc5=None,
    lsb="all",
    **plot_kwargs,
):
    """
    One graph for one CMPD / BIAS point with a family of LSB curves.
    """
    directories = select_folders(
        sources,
        cmpd=cmpd,
        bias=bias,
        lsb=lsb,
        vc5=vc5,
    )

    if not directories:
        print(
            f"No folders for CMPD={cmpd}, VB5={bias}, "
            f"LSB={lsb}, VC5={vc5}"
        )
        return

    print_selection(directories)
    plot_offset_curve(directories=directories, **plot_kwargs)


def plot_threshold_families(
    sources,
    cmpd,
    vc5=None,
    biases="all",
    lsbs="all",
    mode="separate",
    subplot_cols=3,
    **plot_kwargs,
):
    """
    Backward-compatible family helper for one threshold code.

    mode:
        "separate" -> one Figure per BIAS
        "subplots" -> all BIAS groups in one Figure
        "overlay"  -> all selected BIAS/LSB curves on one axes
    """
    return plot_grouped_families(
        sources=sources,
        group_by="bias",
        mode=mode,
        cmpd=cmpd,
        bias=biases,
        lsb=lsbs,
        vc5=vc5,
        subplot_cols=subplot_cols,
        **plot_kwargs,
    )


# =============================================================================
# QUICK API REFERENCE
# =============================================================================
#
# Folder discovery / selection:
#   discover_folders(sources, recursive=True)
#   select_folders(sources, cmpd=None, bias=None, lsb=None, vc5=None)
#   print_selection(directories)
#
# RAW waveform inspection:
#   plot_raw(directory, code, t_lim=None, decimate=1, layout="overlay", ...)
#
# Offset plots for an already selected folder list:
#   plot_offset_curve(directories, edge="both", ...)
#   plot_offset_sorted(directories, edge="both", label_every=1, ...)
#   plot_offset_histogram(code, directories, edge="both", bins=20, ...)
#   plot_offset_histogram_all(directories, edge="both", bins=20, ...)
#   plot_overlay(codes, directory, t_lim=None, decimate=1, ...)
#
# Family selection / comparison:
#   plot_lsb_family(sources, cmpd, bias, vc5=None, lsb="all", ...)
#   plot_threshold_families(sources, cmpd, vc5=None, biases="all",
#                           lsbs="all", mode="separate|subplots|overlay", ...)
#   plot_cmpd_families(sources, cmpds=(200,512,850), bias="all", lsb="all",
#                      vc5=None, mode="separate|subplots|overlay", ...)
#   plot_bias_families(sources, cmpd, biases="all", lsb="all", vc5=None,
#                      mode="separate|subplots|overlay", ...)
#   plot_grouped_families(sources, group_by="cmpd|bias|lsb|vc5",
#                         mode="separate|subplots|overlay", ...)
#
# Export:
#   save_offset_data_to_csv(directories, output_filename, ...)
#   save_offset_long_csv(directories, output_filename, ...)
#
# Cache control:
#   clear_analysis_cache()
#
# Important analysis parameters:
#   output_supply_v       = actual OUT-stage VDD. Switching is detected at
#                           switch_level_fraction * VDD.
#   switch_level_fraction = 0.5 by default.
#   propagation_delay_s   = 0.0 by default; VIN is sampled at t_OUT - delay.
#

# =============================================================================
# Example configuration
# =============================================================================

# Point this either to:
#   - a parent directory containing all folders from the screenshot, or
#   - a list of such parent directories / individual test folders.
DATA_SOURCES = [
    r"D:\dev\KPX_HPD_Tester_venv\KPX_HPD_Tester\EO_scv_data\cmp_offset",
]

# Comparator OUT switching detection.
# Requested default is 0.5 * VDD.
OUTPUT_SUPPLY_V = None  # Set actual comparator-output VDD for exact 0.5*VDD detection.
SWITCH_LEVEL_FRACTION = 0.5

# Comparator internal propagation delay.
# Positive value means OUT changes this much later than the physical input
# threshold crossing. VIN is therefore sampled at t_OUT - delay.
PROPAGATION_DELAY_S = 100e-9

EDGE = "rise"
MIN_AMPLITUDE_V = 0.5
MIN_SWITCH_INTERVAL_S = 0.0


if __name__ == "__main__":
    # FAST WORKFLOW EXAMPLES
    #
    # RAW one run / one trim code:
    # RAW_DIR = select_folders(DATA_SOURCES, cmpd=512, bias=500, lsb=250, vc5=512)[0]
    # plot_raw(
    #     RAW_DIR,
    #     code=16,
    #     layout="overlay",       # "subplots" or "overlay"
    #     t_lim=(-0.02, 0.02),
    #     output_supply_v=OUTPUT_SUPPLY_V,
    #     switch_level_fraction=SWITCH_LEVEL_FRACTION,
    #     propagation_delay_s=PROPAGATION_DELAY_S,
    #     mark_switches=True,
    # )
    #
    # Three threshold groups in one image:
    # plot_cmpd_families(
    #     DATA_SOURCES,
    #     cmpds=(200, 512, 850),
    #     bias=500,
    #     lsb="all",
    #     vc5=512,
    #     mode="subplots",
    #     output_supply_v=OUTPUT_SUPPLY_V,
    # )
    #
    # Same families overlaid on one axes:
    # plot_cmpd_families(
    #     DATA_SOURCES,
    #     cmpds=(200, 512, 850),
    #     bias=500,
    #     lsb="all",
    #     vc5=512,
    #     mode="overlay",
    #     output_supply_v=OUTPUT_SUPPLY_V,
    # )
    #
    # -------------------------------------------------------------------------
    # 1. Discover all test folders
    # -------------------------------------------------------------------------
    ALL_FOLDERS = discover_folders(DATA_SOURCES)

    print_selection(ALL_FOLDERS)

    # -------------------------------------------------------------------------
    # 2. Example: all LSB values for one threshold and one BIAS
    # -------------------------------------------------------------------------
    FAMILY = select_folders(
        DATA_SOURCES,
        cmpd=200,
        bias=500,
        lsb="all",
        vc5=512,
    )

    # Uncomment:
    #
    # plot_offset_curve(
    #     directories=FAMILY,
    #     edge=EDGE,
    #     min_amplitude=MIN_AMPLITUDE_V,
    #     output_supply_v=OUTPUT_SUPPLY_V,
    #     switch_level_fraction=SWITCH_LEVEL_FRACTION,
    #     propagation_delay_s=PROPAGATION_DELAY_S,
    #     min_switch_interval_s=MIN_SWITCH_INTERVAL_S,
    # )

    # -------------------------------------------------------------------------
    # 3. Example: one exact setting
    # -------------------------------------------------------------------------
    EXACT = select_folders(
        DATA_SOURCES,
        cmpd=200,
        bias=500,
        lsb=250,
        vc5=512,
    )

    # plot_offset_sorted(
    #     directories=EXACT,
    #     edge=EDGE,
    #     output_supply_v=OUTPUT_SUPPLY_V,
    #     switch_level_fraction=SWITCH_LEVEL_FRACTION,
    #     propagation_delay_s=PROPAGATION_DELAY_S,
    #     label_every=0,
    # )

    # -------------------------------------------------------------------------
    # 4. Example: every BIAS / LSB family for one threshold
    # -------------------------------------------------------------------------
    # plot_threshold_families(
    #     DATA_SOURCES,
    #     cmpd=200,
    #     vc5=512,
    #     edge=EDGE,
    #     min_amplitude=MIN_AMPLITUDE_V,
    #     output_supply_v=OUTPUT_SUPPLY_V,
    #     switch_level_fraction=SWITCH_LEVEL_FRACTION,
    #     propagation_delay_s=PROPAGATION_DELAY_S,
    # )

    # -------------------------------------------------------------------------
    # 5. Example: event histogram for trim code 7 in one family
    # -------------------------------------------------------------------------
    # plot_offset_histogram(
    #     code=7,
    #     directories=FAMILY,
    #     edge=EDGE,
    #     output_supply_v=OUTPUT_SUPPLY_V,
    #     switch_level_fraction=SWITCH_LEVEL_FRACTION,
    #     propagation_delay_s=PROPAGATION_DELAY_S,
    #     bins=30,
    # )

    # -------------------------------------------------------------------------
    # 6. Example: save one selected family
    # -------------------------------------------------------------------------
    # save_offset_long_csv(
    #     directories=FAMILY,
    #     output_filename="offset_CMPD200_VB5_500.csv",
    #     edge=EDGE,
    #     output_supply_v=OUTPUT_SUPPLY_V,
    #     switch_level_fraction=SWITCH_LEVEL_FRACTION,
    #     propagation_delay_s=PROPAGATION_DELAY_S,
    # )
