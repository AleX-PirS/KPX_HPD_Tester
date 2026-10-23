"""Frozen-map experiments and independent offline verification for the paper."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import copy
from datetime import datetime, timezone
import html
import json
import math
import os
from pathlib import Path, PureWindowsPath

import numpy as np
import pandas as pd

from .models import FRAMEWORK_VERSION, get_window_spec, resolve_pixels
from .storage import ExperimentStore, atomic_write_json, atomic_write_table, file_sha256
from .uncertainty import classify_interval


def validate_method_storage(results_root, *, enforce_windows_limit=None):
    """Budget the deepest existing raw temporary naming format before opening devices."""
    parent = PureWindowsPath(str(results_root))
    if not parent.is_absolute():
        parent = PureWindowsPath(str(Path(results_root).resolve()))
    directory = (parent/"method_check"/"20991231T235959Z_99"/"grid"/"CD"/"u1023"/
        "20991231T235959Z_CD_99"/"raw"/"scurve"/
        "pulse_amplitude_999_pattern_tile_8x8"/"adaptive"/"dac_1023")
    temporary = directory/".background_amp_12345678_repeat_999999_1234567890abcdef.csv.abcdefgh.tmp"
    enforce = os.name=="nt" if enforce_windows_limit is None else enforce_windows_limit
    if enforce and (len(str(temporary))>259 or len(str(directory))>247):
        raise ValueError("method_verification: RESULTS_DIR слишком длинный для Windows; "
                         f"ожидается путь до {len(str(temporary))} символов. Укажите короткий путь в .env")
    return {"maximum_file_path_length":len(str(temporary)),"maximum_directory_path_length":len(str(directory))}


@dataclass
class MethodVerificationSettings:
    repeat_scans: int = 3
    alternate_directions: bool = True
    counter_operator_validated: bool = False
    normal_repeat_estimates_validated: bool = False
    threshold_tolerance_v: float | None = None
    systematic_error_bound_v: float | None = None
    confidence_alpha: float = .05
    # Optional working upper-boundary codes, one full lower-DAC scan per code.
    # No physical limits or work thresholds are guessed from an image.
    upper_threshold_codes: dict[str, tuple[int, ...]] = field(default_factory=dict)

    def validate(self):
        if not isinstance(self.repeat_scans, int) or isinstance(self.repeat_scans, bool) or self.repeat_scans < 2:
            raise ValueError("method repeat_scans must be integer >=2")
        for name in ("alternate_directions", "counter_operator_validated", "normal_repeat_estimates_validated"):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"method {name} must be bool")
        for name in ("threshold_tolerance_v", "systematic_error_bound_v"):
            value = getattr(self, name)
            if value is not None and (not math.isfinite(float(value)) or float(value) < 0):
                raise ValueError(f"method {name} must be finite and nonnegative or None")
        for name in ("confidence_alpha",):
            if not 0 < float(getattr(self, name)) < 1:
                raise ValueError(f"method {name} must be in (0,1)")
        for window, codes in self.upper_threshold_codes.items():
            get_window_spec(window)
            if not isinstance(codes, (list, tuple)) or not codes:
                raise ValueError("upper_threshold_codes needs a nonempty sequence")
            if any(not isinstance(c, int) or isinstance(c, bool) or not 0 <= c <= 1023 for c in codes):
                raise ValueError("upper boundary codes must be integers in 0..1023")
            if len(set(codes)) != len(codes):
                raise ValueError("duplicate upper boundary codes")


def prepare_frozen_maps(reference, windows, pixels, *, base_pixel_config=None, calibrations=None):
    """Load all requested comparator maps before any verification acquisition."""
    from pixel_matrix import PIXEL_CODEC
    from .hardware import build_standard_characterization_pixel_configs, load_base_pixel_configs
    from .workflow import _read_trim_reference
    base = (load_base_pixel_configs(base_pixel_config) if base_pixel_config is not None
            else build_standard_characterization_pixel_configs(digital_counting_enabled=True))
    missing = set(pixels)-set(base)
    if missing:
        raise ValueError("base pixel configuration does not cover requested pixels")
    sources, targets = {}, {}
    for window in windows:
        store, trim_path, mapping = _read_trim_reference(reference[window] if isinstance(reference, dict)
                                                         else reference, window, pixels)
        spec = get_window_spec(window)
        if calibrations is not None:
            for name in (spec.threshold_dac, spec.upper_threshold_dac):
                recorded = store.metadata.get("threshold_dac_calibrations", {}).get(name, {}).get("curve_sha256")
                if recorded != calibrations[name].to_metadata()["curve_sha256"]:
                    raise ValueError(f"verification/reference LUT mismatch: {window}, {name}")
        target = store.metadata.get("equalization_target", {}).get("target_voltage_v")
        if target is None or not math.isfinite(float(target)):
            raise ValueError(f"reference {window} has no fixed equalization target")
        targets[window] = float(target)
        for pixel, trim in mapping.items():
            fields = PIXEL_CODEC.unpack(base[pixel])
            fields[spec.pixel_trim_field] = int(trim)
            base[pixel] = PIXEL_CODEC.pack(fields)
        sources[window] = {"path": str(store.root), "metadata_sha256": file_sha256(store.metadata_path),
                           "trim_path": str(trim_path), "trim_sha256": file_sha256(trim_path)}
    return base, sources, targets


def _analysis_table(path, name):
    source = Path(path)/name
    if not source.exists() or source.stat().st_size < 2:
        return pd.DataFrame()
    try:
        return pd.read_csv(source)
    except pd.errors.EmptyDataError:
        return pd.DataFrame()


def analyze_method_verification(root, *, settings=None, plot_settings=None, generate_plots=True):
    """Analyze a saved manifest only, without connecting to any instrument."""
    root = Path(root).resolve()
    document = json.loads((root/"method_manifest.json").read_text(encoding="utf-8"))
    options = settings or MethodVerificationSettings(**document["method_settings"])
    options.validate()
    from .models import AnalysisSettings
    from .bipolar import save_bipolar_plots
    style = plot_settings or AnalysisSettings()
    number = 1
    output = root/"verification"/f"v{number:03d}"
    while output.exists():
        number += 1
        output = root/"verification"/f"v{number:03d}"
    output.mkdir(parents=True)
    records = document.get("runs", [])
    windows, pixels = document["windows"], [tuple(p) for p in document["pixels"]]
    grouped_noise, curves, bipolar = [], [], []
    for record in records:
        if record["kind"] != "verification" or record.get("status") != "complete":
            continue
        path = root/record["analysis_path"]
        noise = _analysis_table(path, "noise_fit_results.csv")
        if not noise.empty:
            noise = noise[noise.stage.eq("baseline_noise")].copy()
            noise["replicate"] = record["replicate"]
            noise["window"] = record["window"]
            grouped_noise.append(noise)
        for name, collection in (("bipolar_points.csv", curves), ("bipolar_results.csv", bipolar)):
            table = _analysis_table(path, name)
            if not table.empty:
                table["replicate"] = record["replicate"]
                collection.append(table)
    noise = pd.concat(grouped_noise, ignore_index=True) if grouped_noise else pd.DataFrame()
    measurements = pd.concat(bipolar, ignore_index=True) if bipolar else pd.DataFrame()
    points = pd.concat(curves, ignore_index=True) if curves else pd.DataFrame()
    diagnostics = _joint_signal_diagnostics(measurements)
    rows = []
    family_size = len(pixels)*len(windows)
    for window in windows:
        for col, row in pixels:
            selected = (noise[(noise.window == window) & (noise.column == col) & (noise.row == row)]
                        if not noise.empty else pd.DataFrame())
            valid = selected[selected.fit_status.eq("ok")] if not selected.empty else selected
            values = pd.to_numeric(valid.get("center_selected_v", pd.Series(dtype=float)),
                                   errors="coerce")
            values = values[np.isfinite(values)].tolist()
            if not valid.empty and valid.replicate.duplicated().any():
                raise ValueError("duplicate independent noise center in verification manifest")
            result = classify_interval(values, target=document["targets_v"][window],
                tolerance=options.threshold_tolerance_v, systematic_bound=options.systematic_error_bound_v,
                alpha=options.confidence_alpha, family_size=family_size,
                expected_repeats=options.repeat_scans, model_validated=options.normal_repeat_estimates_validated)
            result.update(window=window, column=col, row=row,
                          target_v=document["targets_v"][window],
                          repeats_expected=options.repeat_scans)
            result["residual_v"] = result["mean_v"]-result["target_v"]
            rows.append(result)
    status = pd.DataFrame(rows)
    joint = []
    for (col,row), part in status.groupby(["column","row"]):
        states = list(part.status)
        value = ("confirmed_outside" if "confirmed_outside" in states
                 else "confirmed_within" if all(s == "confirmed_within" for s in states)
                 else "not_confirmed")
        joint.append({"column":col,"row":row,"status":value,"boundaries_expected":len(windows)})
    summary = (status.groupby(["window","status"], dropna=False).size()
               .rename("pixels").reset_index())
    for name, table in (("method_noise_centers",noise), ("method_pixel_status",status),
                        ("method_joint_status",pd.DataFrame(joint)), ("method_summary",summary),
                        ("method_bipolar_results",measurements), ("method_bipolar_points",points),
                        ("method_window_diagnostics",diagnostics)):
        atomic_write_table(output/(name+".csv"), table)
    _analyze_window_grid(root, records, output, style, generate_plots)
    if generate_plots:
        _status_maps(status, output/"plots", style)
        # Draw each replicate separately, rather than averaging away temporal drift.
        for replicate, part in points.groupby("replicate") if not points.empty else ():
            save_bipolar_plots(part, measurements[measurements.replicate.eq(replicate)] if not measurements.empty else measurements,
                              output/"plots"/f"r{replicate:03d}", style)
    report = ["# Верификация фиксированных карт", "",
        "Пороговый допуск, неподтвержденность и сигнальные метрики разделены.",
        "Интервалы Стьюдента условны на нормальную стационарную модель повторных оценок.",
        "Систематическая граница должна включать LUT, цель, сетку и модельную ошибку.",
        "Счетные метрики двух краев не доказывают адаптивность ЗЧУ или причину дефекта.", "",
        "```csv\n"+summary.to_csv(index=False)+"```",
        "", "Все исходные пиксели включены в method_pixel_status.csv.",
        "method_bipolar_results.csv содержит оба края, полуразмах и асимметрию относительно off-базы.",
        "Статистическая эффективность P1/P2 не заявляется без отдельной опорной серии R."]
    (output/"README.md").write_text("\n".join(report)+"\n", encoding="utf-8")
    atomic_write_json(output/"verification_settings.json", asdict(options))
    return {"analysis_directory":output, "pixel_status":output/"method_pixel_status.csv",
            "report":output/"README.md"}


def _joint_signal_diagnostics(measurements):
    """Cross-window contrasts are diagnostic, never automatic fault attribution."""
    if measurements.empty:
        return pd.DataFrame()
    keys = [k for k in ("replicate", "column", "row", "pulse_amplitude_native", "injection_pattern",
                        "injection_group_id", "measurement_fclk_mhz", "local_gain_code") if k in measurements]
    frame = measurements.copy()
    frame["threshold_half_span_v"] = pd.to_numeric(frame.threshold_half_span_v, errors="coerce")
    frame.loc[frame.two_edges_status.ne("resolved"), "threshold_half_span_v"] = np.nan
    frame["window_median_half_span_v"] = frame.groupby(keys,dropna=False).threshold_half_span_v.transform("median")
    frame["resolved_windows"] = frame.groupby(keys,dropna=False).threshold_half_span_v.transform("count")
    frame["half_span_window_contrast_v"] = frame.threshold_half_span_v-frame.window_median_half_span_v
    frame["interpretation"] = "descriptive; comparator_or_CSA_cause_not_identified"
    return frame[keys+["window","threshold_half_span_v","window_median_half_span_v",
                       "resolved_windows","half_span_window_contrast_v","interpretation"]]


def _analyze_window_grid(root, records, output, style, generate_plots):
    from .analysis import _as_bool
    from .calibration import ThresholdDacCalibration
    tables = []
    for record in records:
        if record["kind"] != "window_grid" or record.get("status") != "complete":
            continue
        store = ExperimentStore(root/record["experiment_path"])
        data = store.load_raw(measurement_kind="scurve", workers=style.read_workers)
        if data.empty:
            continue
        data = data[data.acquisition_type.eq("signal")].copy()
        for field in ("threshold_voltage_v", "threshold_dac_code"):
            data[field] = pd.to_numeric(data[field],errors="coerce")
        if "active_injection_pixel" in data:
            data = data[_as_bool(data.active_injection_pixel)].copy()
        count = pd.to_numeric(data.selected_count,errors="coerce")
        valid = count.ge(0) & np.isfinite(count)
        for field in ("measurement_valid","counter_decode_valid"):
            if field in data:
                valid &= _as_bool(data[field])
        if "counter_saturated" in data:
            valid &= ~_as_bool(data.counter_saturated)
        data["valid_count"] = count.where(valid)
        keys = [k for k in ("window","column","row","pulse_amplitude_native","injection_pattern",
                            "injection_group_id","measurement_fclk_mhz","local_gain_code",
                            "threshold_dac_code","threshold_voltage_v") if k in data]
        table = data.groupby(keys,dropna=False).agg(mean_count=("valid_count","mean"),
            std_count=("valid_count","std"), valid_repeats=("valid_count","count"),
            attempted_repeats=("valid_count","size")).reset_index()
        spec = get_window_spec(record["window"])
        saved = store.metadata["threshold_dac_calibrations"][spec.upper_threshold_dac]
        upper = ThresholdDacCalibration.from_csv(store.root/saved["experiment_copy"],
            spec.upper_threshold_dac, code_column=saved.get("code_column"),
            voltage_column=saved.get("voltage_column"))
        table["upper_dac_code"] = record["upper_code"]
        table["upper_voltage_v"] = upper.lookup(record["upper_code"]).voltage
        table["interpretation"] = "measured_window_count_response; no_isolated_edge_fit"
        tables.append(table)
    if not tables:
        return
    frame = pd.concat(tables,ignore_index=True)
    atomic_write_table(output/"window_grid_points.csv",frame)
    if not generate_plots:
        return
    import matplotlib.pyplot as plt
    directory = output/"plots"/"window_grid"
    directory.mkdir(parents=True,exist_ok=True)
    pixels = list(style.plot_pixels) or list(dict.fromkeys(zip(frame.column,frame.row)))[:style.representative_pixels]
    context = [k for k in ("window","pulse_amplitude_native","injection_pattern","injection_group_id",
                           "measurement_fclk_mhz","local_gain_code") if k in frame]
    for index, (_, group) in enumerate(frame.groupby(context,dropna=False)):
        for col,row in pixels:
            part = group[(group.column==col)&(group.row==row)]
            if part.empty:
                continue
            image = part.pivot(index="upper_voltage_v",columns="threshold_voltage_v",values="mean_count").sort_index().sort_index(axis=1)
            fig,ax = plt.subplots(figsize=(6,4.8))
            ru = style.plot_language=="ru"
            ax.set(xlabel="Нижний порог, В" if ru else "Lower threshold, V",
                   ylabel="Верхний порог, В" if ru else "Upper threshold, V",
                   title=f"{part.iloc[0].window}, C{col} R{row}")
            if image.shape[0]==1:
                ax.plot(image.columns.to_numpy(float),image.iloc[0].to_numpy(float),".-")
                ax.set_ylabel("Измеренный счет" if ru else "Measured counts")
                ax.set_title(ax.get_title()+f"; U={float(image.index[0]):.6g} V")
                ax.grid(alpha=.18)
            elif image.shape[1]==1:
                ax.plot(image.index.to_numpy(float),image.iloc[:,0].to_numpy(float),".-")
                ax.set(xlabel="Верхний порог, В" if ru else "Upper threshold, V",
                       ylabel="Измеренный счет" if ru else "Measured counts")
                ax.set_title(ax.get_title()+f"; L={float(image.columns[0]):.6g} V")
            else:
                artist = ax.pcolormesh(image.columns.to_numpy(float),image.index.to_numpy(float),
                                      image.to_numpy(float),shading="nearest")
                fig.colorbar(artist,ax=ax,label="Измеренный счет" if ru else "Measured counts")
            fig.tight_layout()
            for extension in ("png","pdf") if style.save_pdf_plots else ("png",):
                fig.savefig(directory/f"grid_{index:03d}_c{col:02d}_r{row:02d}.{extension}",dpi=style.plot_dpi)
            plt.close(fig)


def _status_maps(frame, directory, settings):
    import matplotlib.pyplot as plt
    from pixel_matrix import OWNED_COLUMNS
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    ru = settings.plot_language == "ru"
    windows = sorted(frame.window.unique())
    for field in ("mean_v", "residual_v", "std_v", "half_width_v"):
        finite = pd.to_numeric(frame[field], errors="coerce").dropna()
        if finite.empty:
            continue
        fig, axes = plt.subplots(1,len(windows),squeeze=False,figsize=(4.5*len(windows),5))
        for ax, window in zip(axes[0], windows):
            part = frame[frame.window == window]
            image = part.pivot(index="row",columns="column",values=field).reindex(
                index=range(32),columns=OWNED_COLUMNS).to_numpy(dtype=float)
            im = ax.imshow(image,origin="lower",aspect="equal" if settings.square_physical_pixels else "auto",
                extent=(min(OWNED_COLUMNS)-.5,max(OWNED_COLUMNS)+.5,-.5,31.5),
                vmin=float(finite.min()),vmax=float(finite.max()))
            if not settings.square_physical_pixels:
                ax.set_box_aspect(1)
            ax.set(title=window,xlabel="Столбец" if ru else "Column",ylabel="Строка" if ru else "Row")
            bar = ax.inset_axes([1.03,0,.045,1])
            fig.colorbar(im,cax=bar,label="В" if ru else "V")
        labels = {"mean_v": ("Средний эффективный порог", "Mean effective threshold"),
                  "residual_v": ("Отклонение от фиксированной цели", "Residual from fixed target"),
                  "std_v": ("Разброс между повторными сканами", "Between-scan standard deviation"),
                  "half_width_v": ("Полуширина интервала с систематической границей", "Interval half width including systematic bound")}
        fig.suptitle(labels[field][0 if ru else 1])
        fig.subplots_adjust(wspace=.6,top=.88,bottom=.12,right=.94)
        for extension in ("png","pdf") if settings.save_pdf_plots else ("png",):
            fig.savefig(directory/(field+"."+extension),dpi=settings.plot_dpi,bbox_inches="tight")
        plt.close(fig)


def run_method_verification(client, threshold_calibration_files, *, settings,
                            method_settings=None, windows=("AB", "BC", "CD"),
                            pixels="all", bad_pixel_map=None, base_pixel_config=None,
                            results_root="results", trim_reference=None,
                            hardware_arguments=None, generate_plots=True,
                            configuration_metadata=None):
    """Train once (or reuse a reference), freeze ALL trims, then remeasure.

    Child acquisitions use only the established safe characterization workflow.
    The suite manifest is also saved on failure; completed raw data are retained.
    Training measurements never enter the verification confidence intervals.
    """
    from .calibration import load_threshold_dac_calibrations
    from .pixel_masks import normalize_bad_pixel_map
    from .workflow import characterize_comparator
    options = copy.deepcopy(method_settings or MethodVerificationSettings())
    options.validate()
    validate_method_storage(results_root)
    selected = copy.deepcopy(settings)
    selected.validate()
    windows = tuple(str(w).upper() for w in windows)
    if not windows or len(set(windows)) != len(windows):
        raise ValueError("select distinct AB/BC/CD windows")
    for window in windows:
        get_window_spec(window)
    if set(options.upper_threshold_codes)-set(windows):
        raise ValueError("upper boundary grid contains unrequested windows")
    requested = resolve_pixels(pixels)
    bad = set(normalize_bad_pixel_map(bad_pixel_map))
    active = tuple(p for p in requested if p not in bad)
    if not active:
        raise ValueError("no unmasked verification pixels")
    hardware = dict(hardware_arguments or {})
    if hardware.get("gain_sweep_codes") is not None:
        raise ValueError("method verification freezes one gain configuration; use scalar gain or a map")
    prohibited = {"window", "pixels", "settings", "results_root", "run_noise_scan", "run_equalization",
                  "run_scurve", "base_pixel_config", "trim_reference_experiment", "additional_metadata",
                  "noise_reference_experiment", "upper_non_limiting_code", "upper_threshold_role", "analyze_result"}
    if prohibited.intersection(hardware):
        raise ValueError("hardware_arguments contains workflow control fields")
    # Install the SAME GAIN before noise acquisition as during pulse scans.
    # Passing gain_map to the generic workflow alone programs it at S-curve time.
    from pixel_matrix import PIXEL_CODEC
    from .hardware import build_standard_characterization_pixel_configs, load_base_pixel_configs
    from .injection import resolve_gain_map
    initial_base = (load_base_pixel_configs(base_pixel_config) if base_pixel_config is not None
        else build_standard_characterization_pixel_configs(digital_counting_enabled=True))
    if hardware.get("gain_map") is not None:
        gain = resolve_gain_map(hardware["gain_map"],required_pixels=active)
        for pixel, code in gain.items():
            fields = PIXEL_CODEC.unpack(initial_base[pixel])
            fields["PX_GAIN"] = code
            initial_base[pixel] = PIXEL_CODEC.pack(fields)
    base_pixel_config = initial_base
    calibrations = load_threshold_dac_calibrations(threshold_calibration_files)
    for window in windows:
        spec = get_window_spec(window)
        if any(name not in calibrations for name in (spec.threshold_dac,spec.upper_threshold_dac)):
            raise ValueError(f"missing comparator LUT for {window}")
        for code in options.upper_threshold_codes.get(window, ()):
            calibrations[spec.upper_threshold_dac].lookup(code)
    frozen = None
    if trim_reference is not None:
        frozen = prepare_frozen_maps(trim_reference, windows, active,
                                     base_pixel_config=base_pixel_config, calibrations=calibrations)
    # Keep suite paths short enough for ordinary Windows installations.
    parent = Path(results_root).resolve()/"method_check"
    parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = parent/stamp
    suffix = 1
    while root.exists():
        root = parent/f"{stamp}_{suffix:02d}"
        suffix += 1
    root.mkdir()
    manifest = {"framework_version":FRAMEWORK_VERSION, "protocol":"frozen_maps_v1",
        "status":"running", "windows":list(windows), "pixels":[list(p) for p in requested],
        "bad_pixels":[list(p) for p in sorted(bad.intersection(requested))],
        "method_settings":asdict(options), "configuration":configuration_metadata or {}, "runs":[]}
    def save():
        atomic_write_json(root/"method_manifest.json", manifest)
    def child(kind, window, destination, acquisition_settings, **controls):
        record = {"kind":kind,"window":window,"status":"running", **controls.pop("record", {})}
        manifest["runs"].append(record)
        save()
        child_hardware = dict(hardware)
        if kind == "training":
            # Noise-only training must not receive an S-only REF specification.
            for name in ("reference_calibration_files", "injection_voltage_steps_v",
                         "manual_reference_configuration", "replay_reference_selections",
                         "reference_step_oscilloscope", "reference_step_verification_settings"):
                child_hardware.pop(name, None)
        result = characterize_comparator(client, calibrations, window=window, pixels=requested,
            bad_pixel_map=bad_pixel_map, results_root=root/destination, settings=acquisition_settings,
            generate_analysis_plots=generate_plots,
            additional_metadata={"method_protocol":{
                "kind":kind, "suite_path":str(root), "replicate":record.get("replicate"),
                "counter_operator_validated":options.counter_operator_validated,
                "training_data_excluded_from_intervals":True}}, **controls, **child_hardware)
        record.update(status=result.status, experiment_path=str(result.experiment_path.relative_to(root)),
            analysis_path=str(result.analysis_path.relative_to(root)) if result.analysis_path else None)
        save()
        return result
    save()
    try:
        if frozen is None:
            trained = {}
            for window in windows:
                result = child("training", window, f"train/{window}", copy.deepcopy(selected),
                    base_pixel_config=base_pixel_config, run_noise_scan=True,
                    run_equalization=True, run_scurve=False)
                trained[window] = result.experiment_path
            frozen = prepare_frozen_maps(trained, windows, active,
                base_pixel_config=base_pixel_config, calibrations=calibrations)
        base, sources, targets = frozen
        manifest.update(sources=sources, targets_v=targets)
        snapshot = root/"frozen_pixel_config.csv"
        atomic_write_table(snapshot, pd.DataFrame([
            {"column":c,"row":r,"raw_pixel_config_hex":f"0x{word:08X}"}
            for (c,r),word in sorted(base.items())]))
        manifest["frozen_pixel_config_sha256"] = file_sha256(snapshot)
        manifest["frozen_pixel_config"] = snapshot.name
        save()
        # Full step-1 scans with paired backgrounds and every requested repeat.
        # Only the requested DAC interval is scanned, never an invented range.
        verification = copy.deepcopy(selected)
        verification.scurve.scan_coverage = "full_bipolar"
        verification.scurve.background_mode = "paired"
        verification.scurve.adaptive_repeats = False
        verification.scurve.baseline_noise_stop_enabled = False
        verification.noise.empty_matrix_repeats_to_skip_remaining = None
        verification.validate()
        for replicate in range(options.repeat_scans):
            scan = copy.deepcopy(verification)
            direction = bool(replicate % 2) if options.alternate_directions else selected.scurve.scan_descending
            scan.scurve.scan_descending = direction
            scan.noise.scan_descending = direction
            for window in windows:
                child("verification", window, f"r{replicate:03d}/{window}", scan,
                    record={"replicate":replicate,"descending":direction},
                    base_pixel_config=base, trim_reference_experiment=sources[window]["path"],
                    run_noise_scan=True, run_equalization=False, run_scurve=True)
        # Optional operating-window response grid. These scans are deliberately
        # not passed to the isolated-edge fit or threshold certification.
        for window, upper_codes in options.upper_threshold_codes.items():
            for code in upper_codes:
                child("window_grid", window, f"grid/{window}/u{code:04d}", copy.deepcopy(verification),
                    record={"upper_code":code}, base_pixel_config=base,
                    trim_reference_experiment=sources[window]["path"],
                    run_noise_scan=False, run_equalization=False, run_scurve=True,
                    allow_scurve_without_noise_reference=True, upper_non_limiting_code=code,
                    upper_threshold_role="operating", analyze_result=False)
        manifest["status"] = "complete"
        save()
        result = analyze_method_verification(root, settings=options,
            plot_settings=selected.analysis, generate_plots=generate_plots)
        result["experiment_path"] = root
        return result
    except BaseException as error:
        manifest["status"] = "interrupted" if isinstance(error, KeyboardInterrupt) else "failed"
        manifest["error"] = f"{type(error).__name__}: {error}"
        if manifest["runs"] and manifest["runs"][-1]["status"] == "running":
            manifest["runs"][-1]["status"] = manifest["status"]
        save()
        raise
