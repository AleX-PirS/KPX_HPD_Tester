"""Hardware remeasurement of proposed mixed GAIN maps, separate from prediction."""
from __future__ import annotations

import copy
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path, PureWindowsPath
from typing import Any, Mapping

import numpy as np
import pandas as pd

from mgpd import MGPDClient
from pixel_matrix import PIXEL_CODEC
from .analysis import fit_scurve_gain_results
from .calibration import ReferencePairSelection, load_threshold_dac_calibrations
from .gain_sweep import CONTEXT, GainEqualizationSettings, _source_analysis, build_gain_sweep_metrics, gain_context_name
from .hardware import UpoPwmShotExecutor, build_standard_characterization_pixel_configs
from .injection import ELEMENTARY_CHARGE_C
from .models import CharacterizationSettings, FRAMEWORK_VERSION, get_window_spec, resolve_pixels
from .pixel_masks import normalize_bad_pixel_map
from .reference_replay import replay_reference_amplitudes
from .storage import atomic_write_json, atomic_write_table, atomic_write_text, file_sha256, utc_now_text
from .workflow import _ordered_scurve_codes, _preflight_scan_coverage, characterize_comparator

WINDOWS = ("AB", "BC", "CD")
RESPONSE_KEY = CONTEXT + ["target_gain_code", "column", "row", "injection_voltage_step_v"]
WINDOWS_MAX_FILE_PATH = 259
WINDOWS_MAX_DIRECTORY_PATH = 247


@dataclass
class GainVerificationJob:
    window: str
    clock: int
    pattern: str
    target: int
    map_window: str
    source_root: Path
    source_analysis: Path
    metadata: dict
    gain_map: pd.DataFrame
    base_configs: dict
    settings: CharacterizationSettings
    reference_pairs: tuple[ReferencePairSelection, ...]
    expected: pd.DataFrame
    common_window_configuration: bool = False

    @property
    def name(self) -> str:
        return gain_context_name({"window": self.window, "measurement_fclk_mhz": self.clock,
                                  "injection_pattern": self.pattern}) + f"/target_gain_{self.target:02d}"


def _verification_run_id(index: int, job: GainVerificationJob) -> str:
    return f"r{index:03d}_{job.window}_g{job.target:02d}"


def gain_verification_windows_path_budget(
    verification_root: str | Path, jobs: list[GainVerificationJob]
) -> dict[str, int | str]:
    """Conservative legacy Win32 path estimate for the deepest temporary raw file."""
    root_text = str(verification_root)
    root = PureWindowsPath(root_text)
    if not root.is_absolute():
        root = PureWindowsPath(str(Path(verification_root).resolve()))
    longest_file = PureWindowsPath()
    longest_directory = PureWindowsPath()
    for index, job in enumerate(jobs, 1):
        experiment = (
            root / "v9999" / _verification_run_id(index, job) / "x"
            / f"20991231T235959Z_{job.window}_99"
        )
        stage = f"pulse_amplitude_999_pattern_{job.pattern}"
        directory = (
            experiment / "raw" / "scurve" / stage / "adaptive" / "dac_1023"
        )
        temporary = directory / (
            ".background_amp_12345678_repeat_999999_1234567890abcdef.csv.abcdefgh.tmp"
        )
        if len(str(temporary)) > len(str(longest_file)):
            longest_file = temporary
        if len(str(directory)) > len(str(longest_directory)):
            longest_directory = directory
    return {
        "max_file_path_length": len(str(longest_file)),
        "max_directory_path_length": len(str(longest_directory)),
        "example_path": str(longest_file),
    }


def validate_gain_verification_storage(
    verification_root: str | Path,
    jobs: list[GainVerificationJob],
    *,
    enforce_windows_limit: bool | None = None,
) -> dict[str, int | str]:
    """Reject an unsafe Windows output root before any instrument is opened."""
    if not jobs:
        raise ValueError("GAIN verification has no prepared jobs")
    root = Path(verification_root)
    if root.exists() and not root.is_dir():
        raise ValueError(f"GAIN verification output must be a directory: {root}")
    budget = gain_verification_windows_path_budget(verification_root, jobs)
    enforce = os.name == "nt" if enforce_windows_limit is None else enforce_windows_limit
    if enforce and (
        int(budget["max_file_path_length"]) > WINDOWS_MAX_FILE_PATH
        or int(budget["max_directory_path_length"]) > WINDOWS_MAX_DIRECTORY_PATH
    ):
        raise ValueError(
            "GAIN verification output path is too long for Windows: "
            f"estimated file path {budget['max_file_path_length']} characters, "
            f"directory {budget['max_directory_path_length']}. "
            "Shorten RESULTS_DIR in .env; the check ran before opening instruments."
        )
    return budget


def _select(frame: pd.DataFrame, context: tuple) -> pd.DataFrame:
    selected = frame
    for column, value in zip(CONTEXT + ["target_gain_code"], context):
        selected = selected[selected[column] == value]
    return selected.copy()


def _exact_pairs(metadata: dict, steps: list[float]) -> tuple[ReferencePairSelection, ...]:
    amplitudes = metadata.get("settings", {}).get("scurve", {}).get("pulse_amplitudes", [])
    pairs = []
    for step in steps:
        matching = [amp for amp in amplitudes if isinstance(amp, dict)
                    and math.isclose(float(amp.get("voltage_step_v", math.nan)), step,
                                     rel_tol=1e-9, abs_tol=1e-12)]
        if len(matching) != 1:
            raise ValueError("source must contain one exact REF configuration per target step")
        amp = matching[0]
        requested = float(amp.get("requested_voltage_step_v", step))
        pairs.append(ReferencePairSelection(
            requested_voltage_step_v=requested, actual_voltage_step_v=step,
            voltage_step_error_v=step-requested, absolute_voltage_step_error_v=abs(step-requested),
            ref1_code=amp["DAC_TST_REF1"], ref2_code=amp["DAC_TST_REF2"],
            ref1_voltage_v=float(amp.get("ref1_voltage_v", math.nan)),
            ref2_voltage_v=float(amp.get("ref2_voltage_v", math.nan)),
            reference_common_mode_v=float(amp.get("reference_common_mode_v", math.nan)),
            selection_method=str(amp.get("reference_pair_selection_method", "exact_source_replay")),
            minimum_reference_code=0, maximum_reference_code=1023, minimum_reference_voltage_v=None,
            ref1_shared_across_amplitudes=bool(amp.get("ref1_shared_across_amplitudes", False)),
        ))
    replay_reference_amplitudes(pairs)
    return tuple(pairs)


def _base_configs(root: Path, metadata: dict, metrics: pd.DataFrame, gain_map: pd.DataFrame) -> dict:
    relative = metadata.get("base_pixel_configuration", {}).get("normalized_selected_pixels_csv")
    if not relative:
        raise ValueError("hardware verification requires the frozen source base_pixel_config.csv")
    base = pd.read_csv(root / relative)
    if base.duplicated(["column", "row"]).any():
        raise ValueError("duplicate pixels in frozen base configuration")
    lookup = {(int(row.column), int(row.row)): int(str(row.raw_pixel_config_hex), 0)
              for row in base.itertuples(index=False)}
    selected = {(int(row.column), int(row.row)) for row in gain_map.itertuples(index=False)}
    if not selected.issubset(lookup):
        raise ValueError("source base configuration does not cover the GAIN map")
    trims = metrics[["column", "row", "local_trim_code"]].drop_duplicates()
    if trims.duplicated(["column", "row"]).any():
        raise ValueError("source comparator trims change across GAIN/steps; cannot replay one fixed map")
    trim_lookup = {(int(row.column), int(row.row)): row.local_trim_code for row in trims.itertuples(index=False)}
    field = get_window_spec(str(metadata["window"])).pixel_trim_field
    configs = build_standard_characterization_pixel_configs(digital_counting_enabled=False)
    for row in gain_map.itertuples(index=False):
        pixel = (int(row.column), int(row.row))
        trim = float(trim_lookup.get(pixel, math.nan))
        if not math.isfinite(trim) or trim != int(trim) or not 0 <= trim <= 31:
            raise ValueError(f"source has no unique valid comparator trim for {pixel}")
        fields = PIXEL_CODEC.unpack(lookup[pixel])
        fields.update(PX_GAIN=int(row.gain), PX_TST_EN=0, PX_MASK=1)
        fields[field] = int(trim)
        configs[pixel] = PIXEL_CODEC.pack(fields)
    return configs


def _expected_response(metrics: pd.DataFrame, gain_map: pd.DataFrame, targets: pd.DataFrame) -> pd.DataFrame:
    """Reconstruct prediction also when one window's map is used in all windows."""
    target_code = int(targets.iloc[0].target_gain_code)
    before = metrics[metrics.gain_sweep_code.eq(target_code)].copy()
    chosen = metrics.merge(gain_map[["column", "row", "gain"]], on=["column", "row"], validate="many_to_one")
    chosen = chosen[chosen.gain_sweep_code.eq(chosen.gain)].copy()
    roster = gain_map[["column", "row", "gain", "recommendation_status"]].merge(
        targets, how="cross"
    )
    key = ["column", "row", "injection_voltage_step_v"]
    columns = {"effective_amplitude_v": "amplitude_v", "baseline_v": "baseline_v",
               "nominal_gain_mv_per_ke": "nominal_gain_mv_per_ke", "gain_v_per_injection_step_v": "gain_v_per_injection_step_v"}
    for prefix, frame in (("before", before), ("predicted", chosen)):
        if frame.duplicated(key).any():
            raise ValueError("duplicate source gain/pixel/step response")
        frame = frame.copy()
        valid = frame.amplitude_valid.astype(str).str.lower().isin(("true", "1"))
        frame.loc[~valid, ["effective_amplitude_v", "nominal_gain_mv_per_ke", "gain_v_per_injection_step_v"]] = np.nan
        renamed = {column: f"{prefix}_{suffix}" for column, suffix in columns.items()}
        roster = roster.merge(frame[key + list(columns)].rename(columns=renamed), on=key, how="left", validate="one_to_one")
    roster["target_gain_v_per_injection_step_v"] = roster.target_secant_gain_v_per_injection_step_v
    return roster


def prepare_gain_verification(
    gain_analysis: str | Path | Mapping[str, Any],
    threshold_calibration_files: Mapping,
    *, settings: CharacterizationSettings | None = None,
    all_windows: bool = False, reference_window: str | None = None,
    allow_unresolved: bool = False, background_mode: str = "paired",
    verification_root: str | Path | None = None,
) -> tuple[Path, list[GainVerificationJob]]:
    """Read-only preflight of ALL jobs; call before opening instruments."""
    directory = Path(gain_analysis["analysis_directory"] if isinstance(gain_analysis, Mapping) else gain_analysis).resolve()
    document = json.loads((directory / "gain_equalization_settings.json").read_text(encoding="utf-8"))
    for record in document["source_files"]:
        if file_sha256(Path(record["path"])) != record["sha256"]:
            raise ValueError("source files changed after GAIN analysis; re-run offline analysis")
    sources = {}
    for record in document["source_files"]:
        path = Path(record["path"])
        if path.name == "scurve_results.csv":
            root, analysis, metadata = _source_analysis(path.parent)
            window = str(metadata["window"]).upper()
            if window in sources:
                raise ValueError("more than one source analysis per window")
            sources[window] = (root, analysis, metadata)
    maps = pd.read_csv(directory / "gain_equalization_maps.csv")
    metrics = pd.read_csv(directory / "gain_sweep_pixel_metrics.csv")
    targets = pd.read_csv(directory / "gain_equalization_targets.csv")
    numeric = maps[["column", "row", "gain", "target_gain_code"]].apply(pd.to_numeric, errors="coerce")
    if not np.isfinite(numeric).all().all() or not np.equal(numeric, np.floor(numeric)).all().all():
        raise ValueError("GAIN map coordinates and codes must be finite integers")
    if not numeric.gain.between(0, 31).all() or maps.duplicated(CONTEXT + ["target_gain_code", "column", "row"]).any():
        raise ValueError("invalid or duplicated GAIN map rows")
    if maps.empty or background_mode not in {"paired", "sparse"}:
        raise ValueError("no GAIN maps or invalid verification background mode")
    calibrations = load_threshold_dac_calibrations(threshold_calibration_files)
    if all_windows:
        if reference_window is None or reference_window.upper() not in WINDOWS:
            raise ValueError("ALL-window verification requires an explicit CHECK_EQ_GAIN_MAP_REFERENCE_WINDOW")
        reference_window = reference_window.upper()
        if set(sources) != set(WINDOWS):
            raise ValueError("ALL-window verification requires source sweeps for AB, BC and CD")
        map_source = maps[maps.window.eq(reference_window)]
    else:
        map_source = maps
    jobs = []
    for context, proposed in map_source.groupby(CONTEXT + ["target_gain_code"], sort=True):
        proposed = proposed.copy()
        resolve_pixels(list(zip(proposed.column.astype(int), proposed.row.astype(int))))
        if not allow_unresolved and not proposed.recommendation_status.eq("measured_code_proposal").all():
            raise ValueError("GAIN map has unresolved pixels; inspect them or enable CHECK_EQ_GAIN_MAP_ALLOW_UNRESOLVED")
        clock = float(context[1])
        if clock != int(clock) or int(clock) not in MGPDClient.FCLK_ALLOWED_MHZ or clock == 0:
            raise ValueError("source measurement FCLK must be a supported nonzero integer")
        for window in WINDOWS if all_windows else (context[0],):
            root, analysis, metadata = sources[window]
            job_context = (window, context[1], context[2], context[3])
            target = _select(targets, job_context)
            if target.empty:
                raise ValueError(f"no measured target for {job_context}")
            source_metrics = metrics.copy()
            for column, value in zip(CONTEXT, job_context):
                source_metrics = source_metrics[source_metrics[column].eq(value)]
            selected = {(int(row.column), int(row.row)) for row in proposed.itertuples(index=False)}
            excluded = set(normalize_bad_pixel_map(metadata.get("bad_pixel_mask")))
            required = {(int(row["column"]), int(row["row"])) for row in metadata.get("pixel_selection", [])} - excluded
            if not required or selected != required:
                raise ValueError("GAIN-map and source-window pixel selections must coincide for verification")
            spec = get_window_spec(window)
            options = metadata.get("run_options", {})
            main_clock = options.get("initialization_fclk_mhz")
            upper = metadata.get("upper_non_limiting_selection", {}).get("selected_code")
            if (not isinstance(main_clock, int) or main_clock not in MGPDClient.FCLK_ALLOWED_MHZ
                    or main_clock == 0 or not isinstance(upper, int) or not 0 <= upper <= 1023):
                raise ValueError("source must record valid initialization FCLK and upper non-limiting code")
            for name in (spec.threshold_dac, spec.upper_threshold_dac):
                stored = metadata.get("threshold_dac_calibrations", {}).get(name, {}).get("curve_sha256")
                if name not in calibrations or stored != calibrations[name].to_metadata()["curve_sha256"]:
                    raise ValueError(f"source and verification threshold LUT differ: {window}, {name}")
            prepared = copy.deepcopy(settings or CharacterizationSettings())
            stored_scurve = metadata.get("settings", {}).get("scurve", {})
            for name in ("shutter_duration_s", "injection_capacitance_f", "injection_capacitance_relative_uncertainty", "n_injections"):
                if name not in stored_scurve:
                    raise ValueError(f"source measurement settings missing {name}")
                setattr(prepared.scurve, name, stored_scurve[name])
            prepared.noise.shutter_duration_s = prepared.scurve.shutter_duration_s
            exposure = prepared.scurve.shutter_duration_s
            if exposure is None or not math.isfinite(float(exposure)) or float(exposure) <= 0:
                raise ValueError("source must record a finite positive S-curve shutter exposure")
            requested_exposure = settings.scurve.shutter_duration_s if settings is not None else None
            if requested_exposure is not None and not math.isclose(
                float(requested_exposure), float(exposure), rel_tol=1e-9, abs_tol=1e-12
            ):
                raise ValueError("ACQUISITION.scurve_shutter_s must match the source exposure; configure UPO likewise")
            prepared.scurve.tile_mode = stored_scurve.get("tile_mode", prepared.scurve.tile_mode)
            prepared.scurve.pulse_amplitudes = ()
            prepared.scurve.injection_patterns = (str(context[2]),)
            prepared.scurve.background_mode = background_mode
            prepared.validate()
            calibrations[spec.upper_threshold_dac].lookup(upper)
            _preflight_scan_coverage(calibrations[spec.threshold_dac], prepared)
            _ordered_scurve_codes(calibrations[spec.threshold_dac], prepared.scurve)
            charge = prepared.scurve.injection_capacitance_f * target.injection_voltage_step_v / ELEMENTARY_CHARGE_C
            known_charge = target.target_charge_electrons.notna()
            if known_charge.any() and not np.allclose(charge[known_charge], target.target_charge_electrons[known_charge], rtol=1e-9, atol=1e-6):
                raise ValueError("stored source charge does not match source Cinj and exact REF step")
            base = _base_configs(root, metadata, source_metrics, proposed)
            gain_map = proposed.copy()
            gain_map["window"] = window
            jobs.append(GainVerificationJob(
                window, int(clock), str(context[2]), int(context[3]), str(context[0]),
                root, analysis, metadata, gain_map, base, prepared,
                _exact_pairs(metadata, target.injection_voltage_step_v.tolist()),
                _expected_response(source_metrics, gain_map, target),
            ))
    if not jobs or len({job.settings.scurve.shutter_duration_s for job in jobs}) != 1:
        raise ValueError("verification requires jobs with one common configured UPO shutter exposure")
    if all_windows:
        for clock, pattern, target in sorted({(job.clock, job.pattern, job.target) for job in jobs}):
            related = [job for job in jobs if (job.clock, job.pattern, job.target) == (clock, pattern, target)]
            reference = next(job for job in related if job.window == reference_window)
            reference_steps = sorted(pair.actual_voltage_step_v for pair in reference.reference_pairs)
            for job in related:
                if ((job.metadata.get("eo_overrides") or {}) != (reference.metadata.get("eo_overrides") or {})
                        or job.metadata["run_options"]["initialization_fclk_mhz"] != reference.metadata["run_options"]["initialization_fclk_mhz"]
                        or job.settings.scurve.tile_mode != reference.settings.scurve.tile_mode):
                    raise ValueError("ALL verification requires matching EO, main FCLK and tile modes across windows")
                steps = sorted(pair.actual_voltage_step_v for pair in job.reference_pairs)
                if len(steps) != len(reference_steps) or not np.allclose(steps, reference_steps, rtol=1e-9, atol=1e-12):
                    raise ValueError("ALL verification requires the same REF steps in all source windows")
                if not math.isclose(job.settings.scurve.injection_capacitance_f,
                                    reference.settings.scurve.injection_capacitance_f, rel_tol=1e-9, abs_tol=0):
                    raise ValueError("ALL verification requires the same Cinj/charge in all source windows")
            common_base = dict(reference.base_configs)
            selected_pixels = [(int(row.column), int(row.row)) for row in reference.gain_map.itertuples(index=False)]
            for pixel in selected_pixels:
                fields = PIXEL_CODEC.unpack(common_base[pixel])
                for job in related:
                    source_fields = PIXEL_CODEC.unpack(job.base_configs[pixel])
                    varying = {get_window_spec(window).pixel_trim_field for window in WINDOWS}
                    if any(fields[name] != source_fields[name] for name in fields if name not in varying):
                        raise ValueError("ALL verification requires identical non-trim pixel settings across windows")
                    field = get_window_spec(job.window).pixel_trim_field
                    fields[field] = source_fields[field]
                common_base[pixel] = PIXEL_CODEC.pack(fields)
            for job in related:
                job.base_configs = dict(common_base)
                job.common_window_configuration = True
    if verification_root is not None:
        validate_gain_verification_storage(verification_root, jobs)
    return directory, jobs


def measured_gain_response(job: GainVerificationJob, analysis: Path) -> pd.DataFrame:
    results = pd.read_csv(analysis / "scurve_results.csv")
    noise = pd.read_csv(analysis / "noise_fit_results.csv")
    if results.empty or noise.empty:
        raise ValueError("verification has no saved S-curve or fresh-noise fit results")
    # Logical target is only a grouping key, never a physically uniform GAIN.
    results["gain_sweep_code"] = job.target
    results["window"] = job.window
    lookup = {(int(row.column), int(row.row)): int(row.gain) for row in job.gain_map.itertuples(index=False)}
    field = get_window_spec(job.window).pixel_trim_field
    for row in results.itertuples(index=False):
        if float(row.measurement_fclk_mhz) != job.clock or row.injection_pattern != job.pattern:
            raise ValueError("measured S-curve clock/pattern differs from the verification job")
        if int(row.local_gain_code) != lookup[(int(row.column), int(row.row))]:
            raise ValueError("measured S-curve records do not match the programmed mixed GAIN map")
        if int(row.local_trim_code) != PIXEL_CODEC.extract(job.base_configs[(int(row.column), int(row.row))], field):
            raise ValueError("measured comparator trim differs from the replayed source trim")
    noise["gain_sweep_code"] = job.target
    noise["measurement_fclk_mhz"] = job.clock
    noise["window"] = job.window
    gains = fit_scurve_gain_results(results[results.fit_status.eq("ok")])
    gains["window"] = job.window
    actual, _ = build_gain_sweep_metrics(results, noise, gains, settings=GainEqualizationSettings(allow_shared_noise_baseline=False))
    actual = actual.rename(columns={"gain_sweep_code": "target_gain_code"})
    actual["applied_gain_code"] = [lookup[(int(row.column), int(row.row))] for row in actual.itertuples(index=False)]
    actual["configuration_mode"] = "measured_mixed_gain_map"
    if "sigma_v" in results:
        widths = results[["column", "row", "injection_voltage_step_v", "sigma_v"]]
        actual = actual.merge(widths, on=["column", "row", "injection_voltage_step_v"], how="left", validate="one_to_one")
    actual["baseline_source"] = actual.baseline_source.replace({
        "gain_matched_noise_center": "mixed_map_fresh_noise_center",
        "scurve_zero_charge_intercept": "mixed_map_zero_charge_intercept",
    })
    return actual


def compare_gain_response(expected: pd.DataFrame, actual: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    columns = {"effective_amplitude_v": "actual_amplitude_v", "baseline_v": "actual_baseline_v",
               "nominal_gain_mv_per_ke": "actual_nominal_gain_mv_per_ke", "gain_v_per_injection_step_v": "actual_gain_v_per_injection_step_v",
               "amplitude_valid": "actual_amplitude_valid", "metric_status": "actual_metric_status", "baseline_source": "actual_baseline_source"}
    if "sigma_v" in actual:
        columns["sigma_v"] = "actual_sigma_v"
    if actual.empty:
        subset = pd.DataFrame(columns=RESPONSE_KEY + list(columns.values()))
    else:
        subset = actual[RESPONSE_KEY + list(columns)].rename(columns=columns)
    compared = expected.merge(subset, how="left", on=RESPONSE_KEY, validate="one_to_one")
    valid = compared.actual_amplitude_valid.astype(str).str.lower().isin(("true", "1"))
    compared.loc[~valid, ["actual_amplitude_v", "actual_nominal_gain_mv_per_ke", "actual_gain_v_per_injection_step_v"]] = np.nan
    compared["actual_amplitude_valid"] = valid
    compared["actual_metric_status"] = compared.actual_metric_status.fillna("measurement_result_missing")
    compared["actual_amplitude_residual_v"] = compared.actual_amplitude_v - compared.target_amplitude_v
    compared["prediction_error_v"] = compared.actual_amplitude_v - compared.predicted_amplitude_v
    summaries = []
    for context, group in compared.groupby(CONTEXT + ["target_gain_code", "injection_voltage_step_v"], sort=True):
        summary = dict(zip(CONTEXT + ["target_gain_code", "injection_voltage_step_v"], context))
        paired = group.dropna(subset=["before_amplitude_v", "predicted_amplitude_v", "actual_amplitude_v"])
        summary.update(pixel_count=len(group), valid_actual_pixel_count=int(group.actual_amplitude_valid.sum()),
                       paired_pixel_count=len(paired), missing_or_invalid_pixel_count=int((~group.actual_amplitude_valid).sum()))
        for prefix in ("before", "predicted", "actual"):
            residual = paired[f"{prefix}_amplitude_v"] - paired.target_amplitude_v
            summary[f"{prefix}_paired_std_v"] = paired[f"{prefix}_amplitude_v"].std()
            summary[f"{prefix}_paired_rms_to_target_v"] = float(np.sqrt(np.mean(residual**2))) if len(paired) else math.nan
            summary[f"{prefix}_paired_abs_residual_q95_v"] = residual.abs().quantile(.95)
            summary[f"{prefix}_paired_gain_std_mv_per_ke"] = paired[f"{prefix}_nominal_gain_mv_per_ke"].std()
        summary["paired_prediction_error_rms_v"] = float(np.sqrt(np.mean(paired.prediction_error_v**2))) if len(paired) else math.nan
        summaries.append(summary)
    return compared, pd.DataFrame(summaries)


def joint_window_diagnostics(comparison: pd.DataFrame) -> pd.DataFrame:
    """Common/differential observations, not proof of CSA/comparator fault origin."""
    rows = []
    group_key = ["measurement_fclk_mhz", "injection_pattern", "target_gain_code", "injection_voltage_step_v"]
    for context, group in comparison.groupby(group_key, sort=True):
        if set(group.window) != set(WINDOWS):
            continue
        code_table = group.pivot(index=["column", "row"], columns="window", values="gain")
        if code_table.isna().any().any() or not code_table.nunique(axis=1).eq(1).all():
            continue
        for (column, row), pixel in group.groupby(["column", "row"]):
            if len(pixel) != 3 or pixel.gain.nunique() != 1:
                continue
            if pixel.target_charge_electrons.notna().any() and not np.allclose(
                pixel.target_charge_electrons, pixel.target_charge_electrons.iloc[0], rtol=1e-9, atol=1e-6
            ):
                raise ValueError("joint diagnostics require the same charge in all windows")
            record = {**dict(zip(group_key, context)), "column": column, "row": row,
                      "gain": int(pixel.iloc[0].gain), "complete_response": bool(pixel.actual_amplitude_valid.all()),
                      "common_gain_deviation": math.nan, "differential_gain_range": math.nan,
                      "common_baseline_shift_v": math.nan, "differential_baseline_range_v": math.nan}
            deviations, baseline_offsets = [], []
            for window in WINDOWS:
                measured = pixel[pixel.window.eq(window)].iloc[0]
                deviation = measured.actual_amplitude_v / measured.target_amplitude_v - 1
                population = group[group.window.eq(window) & group.actual_amplitude_valid]
                baseline_offset = measured.actual_baseline_v - population.actual_baseline_v.median()
                record[f"relative_gain_deviation_{window}"] = deviation
                record[f"baseline_offset_{window}_v"] = baseline_offset
                record[f"actual_amplitude_{window}_v"] = measured.actual_amplitude_v
                record[f"actual_baseline_{window}_v"] = measured.actual_baseline_v
                record[f"actual_gain_{window}_mv_per_ke"] = measured.actual_nominal_gain_mv_per_ke
                record[f"sigma_{window}_v"] = measured.get("actual_sigma_v", math.nan)
                deviations.append(deviation)
                baseline_offsets.append(baseline_offset)
            if record["complete_response"]:
                record.update(common_gain_deviation=float(np.median(deviations)),
                              differential_gain_range=float(np.ptp(deviations)),
                              common_baseline_shift_v=float(np.median(baseline_offsets)),
                              differential_baseline_range_v=float(np.ptp(baseline_offsets)))
            rows.append(record)
    return pd.DataFrame(rows)


def verify_gain_equalization(
    client: Any, threshold_calibration_files: Mapping, *,
    prepared: tuple[Path, list[GainVerificationJob]], hardware_arguments: Mapping | None = None,
    generate_plots: bool = True, configuration_metadata: Mapping | None = None,
    verification_root: str | Path | None = None,
) -> dict:
    """Programs real pixel maps through the existing characterization workflow."""
    source, jobs = prepared
    allowed = {"shot_executor", "keysight_generator", "keysight_burst_settings",
               "reference_step_oscilloscope", "reference_step_verification_settings",
               "reference_verification_pwm_frequency_khz", "reference_verification_pwm_high_time_ns"}
    hardware = dict(hardware_arguments or {})
    if set(hardware) - allowed:
        raise ValueError("hardware_arguments contains unsupported acquisition overrides")
    if hardware.get("shot_executor") is None and hardware.get("keysight_generator") is None:
        raise ValueError("hardware verification requires an explicit CTRL shot executor or generator")
    for job in jobs:
        source_ctrl = job.metadata.get("test_injection_configuration", {}).get("ctrl_source")
        is_upo = isinstance(hardware.get("shot_executor"), UpoPwmShotExecutor)
        if source_ctrl not in {"MGPDLab_UPO_PWM", "Keysight_or_custom_executor"}:
            raise ValueError("source must record a known CTRL acquisition method")
        if (source_ctrl == "MGPDLab_UPO_PWM") != is_upo:
            raise ValueError("verification CTRL acquisition method differs from the source sweep")
        if source_ctrl == "MGPDLab_UPO_PWM":
            executor = hardware.get("shot_executor")
            saved = job.metadata.get("acquisition_sequence", {}).get("upo_pwm_settings", {})
            if any(saved.get(name) != getattr(executor.settings, name) for name in ("frequency_khz", "high_time_ns")):
                raise ValueError("verification PWM timing differs from the source sweep")
    parent = Path(verification_root).resolve() if verification_root is not None else source / "hardware_verification"
    validate_gain_verification_storage(parent, jobs)
    version = 1
    while (parent / f"v{version:03d}").exists():
        version += 1
    directory = parent / f"v{version:03d}"
    manifest = {"framework_version": FRAMEWORK_VERSION, "created_utc": utc_now_text(),
                "source_gain_analysis": str(source), "status": "in_progress", "runs": {}}
    if configuration_metadata is not None:
        manifest["analysis_configuration_v2"] = dict(configuration_metadata)
    atomic_write_json(directory / "gain_verification_manifest.json", manifest)
    outputs = {"verification_directory": directory, "runs": {}, "plots": {}}
    comparisons, actual_tables = [], []
    try:
        for run_index, job in enumerate(jobs, 1):
            run_id = _verification_run_id(run_index, job)
            run_root = directory / run_id
            manifest["runs"][job.name] = {
                "status": "prepared", "run_id": run_id, "window": job.window,
                "measurement_fclk_mhz": job.clock, "injection_pattern": job.pattern,
                "target_gain_code": job.target, "map_source_window": job.map_window,
            }
            atomic_write_json(directory / "gain_verification_manifest.json", manifest)
            map_path = atomic_write_table(run_root / "inputs" / "proposed_gain_map.csv", job.gain_map)
            atomic_write_table(run_root / "inputs" / "expected_response.csv", job.expected)
            main_clock = int(job.metadata["run_options"]["initialization_fclk_mhz"])
            upper = int(job.metadata["upper_non_limiting_selection"]["selected_code"])
            result = characterize_comparator(
                client, threshold_calibration_files, window=job.window,
                pixels=list(zip(job.gain_map.column.astype(int), job.gain_map.row.astype(int))),
                bad_pixel_map=job.metadata.get("bad_pixel_mask"), base_pixel_config=job.base_configs,
                gain_map={(int(row.column), int(row.row)): int(row.gain) for row in job.gain_map.itertuples(index=False)},
                results_root=run_root / "x", settings=job.settings,
                run_noise_scan=True, run_equalization=False, run_scurve=True,
                generate_analysis_plots=generate_plots,
                replay_reference_selections=job.reference_pairs,
                initialization_fclk_mhz=main_clock, measurement_fclk_mhz=job.clock,
                upper_non_limiting_code=upper, eo_overrides=job.metadata.get("eo_overrides"),
                additional_metadata={**({"analysis_configuration_v2": dict(configuration_metadata)} if configuration_metadata is not None else {}),
                    "gain_equalization_verification": {
                    "source_analysis": str(source), "target_gain_code": job.target,
                    "gain_map_source_window": job.map_window, "proposed_gain_map_sha256": file_sha256(map_path),
                    "configuration_mode": "mixed_gain_map", "fresh_noise_baseline": True,
                    "comparator_trims_changed": False,
                }}, **hardware,
            )
            if result.status != "complete" or result.analysis_path is None:
                raise RuntimeError("hardware characterization did not finish with saved analysis")
            manifest["runs"][job.name].update(
                status="measurement_complete", experiment_path=str(result.experiment_path),
                analysis_path=str(result.analysis_path), map_source_window=job.map_window,
            )
            atomic_write_json(directory / "gain_verification_manifest.json", manifest)
            actual = measured_gain_response(job, Path(result.analysis_path))
            comparison, summary = compare_gain_response(job.expected, actual)
            actual_tables.append(actual)
            comparisons.append(comparison)
            paths = {}
            for name, table in (("gain_verification_pixel_metrics", actual), ("gain_verification_comparison", comparison), ("gain_verification_summary", summary)):
                paths[name] = atomic_write_table(run_root / f"{name}.csv", table)
            verified_map = job.gain_map.copy().drop(columns=["verified_in_mixed_gain_matrix"], errors="ignore")
            counts = comparison.groupby(["column", "row"]).actual_amplitude_valid.agg(["sum", "size"])
            verified_map = verified_map.merge(counts.rename(columns={"sum": "valid_amplitude_step_count", "size": "required_step_count"}), on=["column", "row"], how="left", validate="one_to_one")
            verified_map["measured_in_mixed_gain_matrix"] = True
            verified_map["all_response_steps_valid"] = verified_map.valid_amplitude_step_count == verified_map.required_step_count
            paths["measured_gain_map"] = atomic_write_table(
                run_root / "measured_gain_map.csv", verified_map
            )
            paths["experiment_path"] = result.experiment_path
            outputs["runs"][job.name] = paths
            if generate_plots:
                from .gain_verification_plots import plot_gain_verification
                outputs["plots"].update(plot_gain_verification(comparison, directory=run_root / "plots", settings=job.settings.analysis))
            manifest["runs"][job.name].update(
                status="complete", experiment_path=str(result.experiment_path),
                analysis_path=str(result.analysis_path), map_source_window=job.map_window,
            )
            atomic_write_json(directory / "gain_verification_manifest.json", manifest)
        combined = pd.concat(comparisons, ignore_index=True)
        outputs["comparison"] = atomic_write_table(directory / "gain_verification_comparison.csv", combined)
        outputs["pixel_metrics"] = atomic_write_table(directory / "gain_verification_pixel_metrics.csv", pd.concat(actual_tables, ignore_index=True))
        _, summary = compare_gain_response(combined.drop(columns=[name for name in combined if name.startswith("actual_") or name == "prediction_error_v"]), pd.concat(actual_tables, ignore_index=True))
        outputs["summary"] = atomic_write_table(directory / "gain_verification_summary.csv", summary)
        joint = joint_window_diagnostics(combined) if all(job.common_window_configuration for job in jobs) else pd.DataFrame()
        if not joint.empty:
            outputs["joint_window_metrics"] = atomic_write_table(directory / "gain_verification_joint_window_metrics.csv", joint)
            if generate_plots:
                from .gain_verification_plots import plot_joint_gain_verification
                outputs["plots"].update(plot_joint_gain_verification(joint, directory=directory / "plots" / "joint_windows", settings=jobs[0].settings.analysis))
        report = (
            "# Реальная проверка смешанных GAIN-карт\n\n"
            "Карты запрограммированы существующим PX WRITE_TO_CHIP workflow. "
            "Для каждой карты заново измерена noise-база и сняты S-кривые с исходными "
            "REF-парами и comparator trim. Усиление A/Q вычисляется также при одной ступеньке. "
            "Исходные прогнозы не перезаписаны.\n\n"
            "[Реальные метрики](gain_verification_pixel_metrics.csv), "
            "[до/прогноз/измерение](gain_verification_comparison.csv), "
            "[сводка одинаковых пикселей](gain_verification_summary.csv). "
            "Непригодные/пропущенные отклики остаются в CSV и учитываются отдельно.\n\n"
            "measured_in_mixed_gain_matrix означает факт измерения, а не достижение "
            "неуказанного допуска эквализации. Автоматического критерия pass/fail по разбросу нет.\n\n"
            "Общая составляющая трех окон совместима с влиянием ЗЧУ, питания или "
            "инжекции; дифференциальная совместима с компараторами/оконными эффектами. "
            "Это диагностические признаки, не однозначное установление причины. "
            "CMP_A данным тестом отдельно не характеризуется.\n"
        )
        if joint.empty:
            report += "\nПолная совместная диагностика не построена: нужны все AB/BC/CD с одной общей GAIN-картой.\n"
        outputs["report"] = atomic_write_text(directory / "REPORT.md", report)
        manifest.update(status="complete", completed_utc=utc_now_text())
    except BaseException as error:
        manifest.update(status="interrupted" if isinstance(error, KeyboardInterrupt) else "failed",
                        error_type=type(error).__name__, error=str(error))
        raise
    finally:
        atomic_write_json(directory / "gain_verification_manifest.json", manifest)
    return outputs
