"""Build and validate v2 configuration. No device is opened during preflight."""
from __future__ import annotations

import copy
from contextlib import nullcontext
from dataclasses import asdict
import json
import logging
import math
from pathlib import Path
from typing import Any

from comparator_characterization import (
    CharacterizationSettings, FRAMEWORK_VERSION, UpoPwmSettings, UpoPwmShotExecutor,
    load_gain_map_csv, load_reference_dac_calibrations, plan_reference_dac_pairs,
)
from comparator_characterization.models import MATRIX_ROWS, OWNED_COLUMNS, get_window_spec, resolve_pixels
from . import characterization_config as cfg

TESTS = ("full", "noise", "equalize", "trim_sweep", "scurve", "clock_noise", "crosstalk",
         "gain_equalization", "offline", "all_windows", "ref_preview", "reference_verification", "dashboard", "eo_sweep")
HARDWARE_TESTS = set(TESTS) - {"gain_equalization", "offline", "ref_preview", "dashboard"}
SCURVE_TESTS = {"full", "all_windows", "scurve", "clock_noise", "crosstalk", "eo_sweep"}
NOISE_TESTS = {"full", "all_windows", "noise", "equalize", "trim_sweep", "eo_sweep"}


def configure_runtime_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s", datefmt="%H:%M:%S")


def require_hardware_run_enabled() -> None:
    if cfg.RUN.hardware_enabled is not True:
        raise RuntimeError("Аппаратный запуск заблокирован. Установите RUN.hardware_enabled=True в characterization_config.py")


def normalized_ctrl_source() -> str:
    source = str(cfg.ACQUISITION.ctrl_source).strip().lower()
    if source not in {"upo_pwm", "keysight_burst"}:
        raise ValueError("ACQUISITION.ctrl_source должен быть upo_pwm или keysight_burst")
    return source


def threshold_calibration_files() -> dict:
    return {f"DAC_CMP_{letter}": cfg.PATHS.require(f"threshold_lut_{letter.lower()}", kind="file") for letter in "ABCD"}


def reference_calibration_files() -> dict:
    return {f"REF{index}": cfg.PATHS.require(f"ref_lut_{index}", kind="file") for index in (1, 2)}


def base_pixel_config() -> Path | None:
    return cfg.PATHS.require("base_pixel_config", kind="file") if cfg.PATHS.base_pixel_config is not None else None


def noise_reference_path() -> Path:
    path = cfg.PATHS.require("scurve_noise_reference", kind="directory")
    if not (path / "metadata.json").is_file():
        raise FileNotFoundError(f"SCURVE_NOISE_REFERENCE: нет metadata.json: {path}")
    return path


def _resume_path(attribute: str, metadata_name: str) -> Path | None:
    if not cfg.RUN.resume:
        return None
    path = cfg.PATHS.require(attribute, kind="directory")
    metadata = path / metadata_name
    if not metadata.is_file():
        raise FileNotFoundError(f"{cfg.PATHS.key(attribute)}: нет {metadata_name}: {path}")
    document = json.loads(metadata.read_text(encoding="utf-8"))
    version = document.get("contract", {}).get("framework_version") if metadata_name == "sweep.json" else document.get("comparator_characterization_version")
    if version != FRAMEWORK_VERSION:
        raise ValueError("Аппаратное resume допускается только для версии 2. Старые результаты можно анализировать и использовать как референсы.")
    return path


def resume_experiment_path() -> Path | None:
    return _resume_path("resume_experiment", "metadata.json")


def resume_sweep_path() -> Path | None:
    return _resume_path("eo_sweep_resume", "sweep.json")


def configured_gain_codes() -> tuple[int, ...]:
    value = cfg.SCURVE.gain
    values = (value,) if isinstance(value, int) and not isinstance(value, bool) else value
    if not isinstance(values, (list, tuple)) or not values:
        raise TypeError("SCURVE.gain должен быть кодом или непустым list/tuple кодов")
    if any(not isinstance(code, int) or isinstance(code, bool) or not 0 <= code <= 31 for code in values):
        raise ValueError("SCURVE.gain: каждый код должен быть целым в 0..31")
    return tuple(dict.fromkeys(values))


def gain_sweep_enabled() -> bool:
    return cfg.SCURVE.gain_source == "uniform" and isinstance(cfg.SCURVE.gain, (list, tuple))


def gain_hardware_arguments() -> dict:
    if cfg.SCURVE.gain_source == "csv":
        if isinstance(cfg.SCURVE.gain, (list, tuple)):
            raise ValueError("GAIN-свип нельзя объединять с gain_source='csv'; оставьте скалярный gain")
        return {"gain_map": load_gain_map_csv(cfg.PATHS.require("gain_map_csv", kind="file"))}
    if cfg.SCURVE.gain_source != "uniform":
        raise ValueError("SCURVE.gain_source должен быть uniform или csv")
    codes = configured_gain_codes()
    if gain_sweep_enabled():
        return {"gain_sweep_codes": codes}
    return {"gain_map": {(column, row): codes[0] for row in range(MATRIX_ROWS) for column in OWNED_COLUMNS}}


def injection_voltage_steps_v() -> tuple[float, ...]:
    return tuple(float(value) * 1e-3 for value in cfg.REFERENCE.steps_mv)


def build_analysis_settings():
    style = copy.deepcopy(cfg.metadata.ANALYSIS)
    if cfg.PLOTS.geometry not in {"matrix_square", "pixel_square"}:
        raise ValueError("PLOTS.geometry должен быть matrix_square или pixel_square")
    style.square_physical_pixels = cfg.PLOTS.geometry == "pixel_square"
    style.plot_language = cfg.PLOTS.language
    style.plot_pixels, style.plot_injection_patterns = cfg.PLOTS.pixels, cfg.PLOTS.patterns
    style.representative_pixels = cfg.PLOTS.representative_pixels
    style.plot_dpi, style.save_pdf_plots = cfg.PLOTS.dpi, cfg.PLOTS.save_pdf
    style.validate()
    return style


def build_settings(*, injection_patterns=None, scan_all_trim_codes: bool = False) -> CharacterizationSettings:
    settings = CharacterizationSettings(
        noise=copy.deepcopy(cfg.metadata.NOISE), equalization=copy.deepcopy(cfg.metadata.EQUALIZATION),
        scurve=copy.deepcopy(cfg.metadata.SCURVE), analysis=build_analysis_settings(),
    )
    settings.noise.noise_repeats = cfg.NOISE.repeats
    settings.noise.shutter_duration_s = cfg.ACQUISITION.noise_shutter_s
    settings.noise.coarse_start, settings.noise.coarse_stop, settings.noise.coarse_step = cfg.NOISE.coarse_range
    if cfg.NOISE.manual_range is None:
        settings.noise.dac_start = settings.noise.dac_stop = settings.noise.dac_step = None
    else:
        settings.noise.dac_start, settings.noise.dac_stop, settings.noise.dac_step = cfg.NOISE.manual_range
    settings.equalization.scan_all_trim_codes = scan_all_trim_codes
    settings.scurve.n_injections = cfg.ACQUISITION.burst_injections if normalized_ctrl_source() == "keysight_burst" else 1
    settings.scurve.shutter_duration_s = cfg.ACQUISITION.scurve_shutter_s
    settings.scurve.injection_patterns = tuple(injection_patterns or cfg.SCURVE.patterns)
    settings.scurve.repeats = cfg.SCURVE.repeats
    settings.scurve.background_mode = cfg.SCURVE.background_mode
    settings.scurve.tile_mode = cfg.SCURVE.tile_mode
    settings.scurve.coarse_low_code, settings.scurve.coarse_high_code = cfg.SCURVE.dac_range
    settings.scurve.minimum_reference_code, settings.scurve.maximum_reference_code = cfg.REFERENCE.code_limits
    settings.validate()
    return settings


def build_reference_verification_settings():
    settings = copy.deepcopy(cfg.metadata.REFERENCE_VERIFICATION)
    settings.enabled = cfg.REFERENCE.verify_with_scope
    settings.plot_language = cfg.PLOTS.language
    settings.validate()
    return settings


def build_all_window_settings():
    options = copy.deepcopy(cfg.metadata.ALL_WINDOWS)
    options.final_ref_sweep_enabled = cfg.ALL_WINDOWS.final_q_sweep and cfg.REFERENCE.mode == "lut" and not gain_sweep_enabled()
    options.final_ref_step_count, options.final_ref_repeats = cfg.ALL_WINDOWS.q_steps, cfg.ALL_WINDOWS.repeats
    options.final_ref_injection_pattern = cfg.ALL_WINDOWS.pattern
    options.validate()
    return options


def reference_hardware_arguments(oscilloscope: Any, *, required_for_scurve: bool, injection_steps_mv=None) -> dict:
    if not (required_for_scurve or cfg.REFERENCE.verify_with_scope):
        return {}
    mode = str(cfg.REFERENCE.mode).strip().lower()
    if mode == "lut":
        arguments = {"reference_calibration_files": reference_calibration_files(),
                     "injection_voltage_steps_v": tuple(float(value)*1e-3 for value in (
                         cfg.REFERENCE.steps_mv if injection_steps_mv is None else injection_steps_mv)),
                     "reference_calibration_voltage_unit": cfg.REFERENCE.lut_voltage_unit}
    elif mode == "manual":
        arguments = {"manual_reference_configuration": {"DAC_TST_REF1": cfg.REFERENCE.manual_ref1,
                     "DAC_TST_REF2": cfg.REFERENCE.manual_ref2, "voltage_step_v": float(cfg.REFERENCE.manual_step_mv)*1e-3}}
    else:
        raise ValueError("REFERENCE.mode должен быть lut или manual")
    if cfg.REFERENCE.verify_with_scope:
        if oscilloscope is None:
            raise RuntimeError("REFERENCE.verify_with_scope=True, но осциллограф не открыт")
        arguments.update(reference_step_oscilloscope=oscilloscope,
                         reference_step_verification_settings=build_reference_verification_settings(),
                         reference_verification_pwm_frequency_khz=cfg.ACQUISITION.pwm_frequency_khz,
                         reference_verification_pwm_high_time_ns=cfg.ACQUISITION.pwm_high_time_ns)
    return arguments


def build_upo_client():
    from mgpd import MGPDClient
    return MGPDClient(host=cfg.CONNECTION.host, port=cfg.CONNECTION.port, timeout=cfg.CONNECTION.timeout_s,
                      reconnect_attempts=cfg.metadata.NOISE.upo_reconnect_attempts,
                      reconnect_backoff_s=cfg.metadata.NOISE.upo_reconnect_backoff_s)


def build_oscilloscope():
    if not cfg.REFERENCE.verify_with_scope:
        return nullcontext(None)
    from oscilloscope_cfg import Oscilloscope
    return Oscilloscope(osc_address=cfg.CONNECTION.oscilloscope_address, idn_substring=cfg.CONNECTION.oscilloscope_idn,
                        timeout_ms=cfg.metadata.OSCILLOSCOPE_TIMEOUT_MS)


def build_generator():
    if normalized_ctrl_source() == "upo_pwm":
        return nullcontext(None)
    from generator_cfg import TwoChannelGenerator
    return TwoChannelGenerator(gen_address=cfg.CONNECTION.generator_address,
                               max_amplitude_v=3.3, max_abs_level_v=3.3, min_output_v=0.0)


def build_burst_settings():
    settings = copy.deepcopy(cfg.metadata.BURST)
    settings.validate()
    return settings


def build_upo_pwm_settings():
    settings = UpoPwmSettings(frequency_khz=cfg.ACQUISITION.pwm_frequency_khz,
                             high_time_ns=cfg.ACQUISITION.pwm_high_time_ns,
                             edge_count_uncertainty=cfg.metadata.UPO_PWM_EDGE_COUNT_UNCERTAINTY)
    settings.validate()
    return settings


def injection_hardware_arguments(generator: Any) -> dict:
    if normalized_ctrl_source() == "upo_pwm":
        if generator is not None:
            raise RuntimeError("upo_pwm не использует внешний генератор")
        return {"shot_executor": UpoPwmShotExecutor(build_upo_pwm_settings())}
    if generator is None:
        raise RuntimeError("keysight_burst требует внешний генератор")
    return {"keysight_generator": generator, "keysight_burst_settings": build_burst_settings()}


def configuration_snapshot(test: str | None = None) -> dict:
    blocks = ("RUN", "CONNECTION", "ACQUISITION", "NOISE", "SCURVE", "REFERENCE", "CLOCK_NOISE",
              "GAIN_EQUALIZATION", "ALL_WINDOWS", "EO_SWEEP", "PLOTS", "DASHBOARD")
    return {"configuration_version": 2, "selected_test": test or cfg.RUN.test,
            **cfg.PATHS.snapshot(), "blocks": {name: asdict(getattr(cfg, name)) for name in blocks},
            "metadata": {name: asdict(getattr(cfg.metadata, name)) for name in (
                "NOISE", "EQUALIZATION", "SCURVE", "ANALYSIS", "GAIN_EQUALIZATION", "ALL_WINDOWS",
                "REFERENCE_VERIFICATION", "BURST")},
            "gain_check": {"allow_unresolved": cfg.metadata.GAIN_CHECK_ALLOW_UNRESOLVED,
                           "background_mode": cfg.metadata.GAIN_CHECK_BACKGROUND_MODE},
            "instrument_metadata": {"oscilloscope_timeout_ms": cfg.metadata.OSCILLOSCOPE_TIMEOUT_MS,
                                    "upo_pwm_edge_count_uncertainty": cfg.metadata.UPO_PWM_EDGE_COUNT_UNCERTAINTY}}


def validate_configuration(test: str | None = None, *, check_paths: bool = True, require_hardware: bool = False,
                           source_override: Path | None = None) -> dict:
    """Check only the links used by this test. Unused bookmarks never win."""
    from mgpd import MGPDClient
    from comparator_characterization import load_threshold_dac_calibrations
    from comparator_characterization.workflow import _ordered_scurve_codes, _preflight_scan_coverage
    from comparator_characterization.parameters import validate_eo_overrides
    from comparator_characterization.sweep import _grid_combinations

    selected = test or cfg.RUN.test
    if selected not in TESTS:
        raise ValueError(f"RUN.test: неизвестный тест {selected}; варианты: {', '.join(TESTS)}")
    for block, name in ((cfg.RUN, "resume"), (cfg.RUN, "hardware_enabled"), (cfg.PLOTS, "generate"),
                        (cfg.PLOTS, "save_pdf"), (cfg.GAIN_EQUALIZATION, "check_map"),
                        (cfg.GAIN_EQUALIZATION, "check_all_windows"), (cfg.REFERENCE, "verify_with_scope")):
        if not isinstance(getattr(block, name), bool):
            raise TypeError(f"{type(block).__name__}.{name} должен быть bool")
    physical = selected in HARDWARE_TESTS or (selected == "gain_equalization" and cfg.GAIN_EQUALIZATION.check_map)
    if require_hardware and physical:
        require_hardware_run_enabled()
    if not physical and selected in {"offline", "dashboard", "gain_equalization"}:
        build_analysis_settings()
        attribute = "gain_sweep_source" if selected == "gain_equalization" else "offline_source"
        source = Path(source_override) if source_override is not None else getattr(cfg.PATHS, attribute)
        if check_paths:
            if source is None:
                cfg.PATHS.require(attribute)
            elif not source.exists():
                raise FileNotFoundError(f"Не найден источник {source}")
        if selected == "gain_equalization":
            _validate_gain_targets()
        return {"configuration_version": 2, "test": selected, "window": cfg.RUN.window,
                "hardware_requested": False, "hardware_enabled": cfg.RUN.hardware_enabled,
                "resume": False, "active_paths": {cfg.PATHS.key(attribute): str(source) if source else None},
                "references": {}, "warnings": ["Параметры аппаратного стенда не используются"]}
    window = "ALL" if selected == "all_windows" else str(cfg.RUN.window).upper()
    if window not in {"AB", "BC", "CD", "ALL"}:
        raise ValueError("RUN.window должен быть AB, BC, CD или ALL")
    if selected in {"clock_noise", "crosstalk", "eo_sweep", "reference_verification"} and window == "ALL":
        raise ValueError(f"{selected}: выберите RUN.window=AB, BC или CD; ALL здесь не поддерживается")
    if not isinstance(cfg.RUN.resume, bool) or not isinstance(cfg.RUN.hardware_enabled, bool):
        raise TypeError("RUN.resume и RUN.hardware_enabled должны быть bool")
    settings = build_settings(scan_all_trim_codes=selected == "trim_sweep")
    if window == "ALL":
        build_all_window_settings()
    for name in ("main_fclk_mhz", "measurement_fclk_mhz"):
        clock = getattr(cfg.ACQUISITION, name)
        if not isinstance(clock, int) or isinstance(clock, bool) or clock == 0 or clock not in MGPDClient.FCLK_ALLOWED_MHZ:
            raise ValueError(f"ACQUISITION.{name}: недопустимая ненулевая частота УПО")
    for exposure in (cfg.ACQUISITION.noise_shutter_s, cfg.ACQUISITION.scurve_shutter_s):
        if not math.isfinite(float(exposure)) or float(exposure) <= 0:
            raise ValueError("Время shutter должно быть конечным и положительным")
    if normalized_ctrl_source() == "upo_pwm":
        build_upo_pwm_settings()
    else:
        build_burst_settings()
    resolve_pixels(cfg.RUN.pixels)
    validate_eo_overrides(cfg.RUN.eo_overrides, run_scurve=selected in SCURVE_TESTS)
    if selected == "eo_sweep":
        if cfg.RUN.eo_overrides:
            raise ValueError("eo_sweep: уберите RUN.eo_overrides, используйте EO_SWEEP.grid")
        if not cfg.EO_SWEEP.grid:
            raise ValueError("eo_sweep: задайте EO_SWEEP.grid")
        _grid_combinations(cfg.EO_SWEEP.grid, run_scurve=True)
    if selected == "reference_verification" and not cfg.REFERENCE.verify_with_scope:
        raise ValueError("reference_verification: включите REFERENCE.verify_with_scope")
    active, warnings = {}, []
    if selected in SCURVE_TESTS:
        if check_paths or cfg.SCURVE.gain_source == "uniform":
            gain_hardware_arguments()
        if cfg.SCURVE.gain_source == "csv":
            active["GAIN_MAP_CSV"] = cfg.PATHS.gain_map_csv
        elif cfg.PATHS.gain_map_csv is not None:
            warnings.append("GAIN_MAP_CSV не используется: SCURVE.gain_source='uniform'")
    if selected in {"scurve", "crosstalk"}:
        if check_paths:
            noise_reference_path()
        active["SCURVE_NOISE_REFERENCE"] = cfg.PATHS.scurve_noise_reference
    elif cfg.PATHS.scurve_noise_reference is not None:
        warnings.append("SCURVE_NOISE_REFERENCE не используется выбранным тестом")
    if selected == "clock_noise":
        if not isinstance(cfg.CLOCK_NOISE.fclk_mhz, (list, tuple)) or not cfg.CLOCK_NOISE.fclk_mhz or any(
            not isinstance(clock, int) or isinstance(clock, bool) or clock == 0 or clock not in MGPDClient.FCLK_ALLOWED_MHZ
            for clock in cfg.CLOCK_NOISE.fclk_mhz
        ):
            raise ValueError("CLOCK_NOISE.fclk_mhz: нужен непустой набор разрешенных ненулевых частот")
        if cfg.PATHS.clock_trim_reference is not None:
            active["CLOCK_TRIM_REFERENCE"] = cfg.PATHS.clock_trim_reference
            if check_paths:
                path = cfg.PATHS.require("clock_trim_reference", kind="directory")
                if not (path / "metadata.json").is_file():
                    raise FileNotFoundError("CLOCK_TRIM_REFERENCE: нужен эксперимент с metadata.json")
        else:
            warnings.append("CLOCK_TRIM_REFERENCE не задан: исходные стандартные trims=16")
    if selected in {"gain_equalization", "offline", "dashboard"}:
        attribute = "gain_sweep_source" if selected == "gain_equalization" else "offline_source"
        source = Path(source_override) if source_override is not None else getattr(cfg.PATHS, attribute)
        if check_paths and source_override is None:
            source = cfg.PATHS.require(attribute)
        elif check_paths and not source.exists():
            raise FileNotFoundError(f"Не найден источник {source}")
        active[cfg.PATHS.key(attribute)] = source
    if selected == "gain_equalization":
        _validate_gain_targets()
        if cfg.GAIN_EQUALIZATION.check_map and cfg.GAIN_EQUALIZATION.check_all_windows and cfg.GAIN_EQUALIZATION.map_reference_window not in {"AB", "BC", "CD"}:
            raise ValueError("GAIN_EQUALIZATION.map_reference_window: для общей карты явно выберите AB, BC или CD")
    if physical and selected != "gain_equalization":
        if cfg.PATHS.results_dir is None:
            raise ValueError("Заполните RESULTS_DIR в .env")
        if cfg.PATHS.results_dir.is_file():
            raise ValueError("RESULTS_DIR должен быть каталогом, не файлом")
        active["RESULTS_DIR"] = cfg.PATHS.results_dir
        for attribute in ("base_pixel_config", "bad_pixel_mask"):
            if getattr(cfg.PATHS, attribute) is not None:
                active[cfg.PATHS.key(attribute)] = cfg.PATHS.require(attribute, kind="file") if check_paths else getattr(cfg.PATHS, attribute)
        if selected != "gain_equalization":
            if cfg.RUN.resume:
                attribute = "eo_sweep_resume" if selected == "eo_sweep" else "resume_experiment"
                active[cfg.PATHS.key(attribute)] = (resume_sweep_path() if selected == "eo_sweep" else resume_experiment_path()) if check_paths else getattr(cfg.PATHS, attribute)
            elif cfg.PATHS.resume_experiment is not None or cfg.PATHS.eo_sweep_resume is not None:
                warnings.append("Resume-ссылки не используются: RUN.resume=False")
    if physical:
        if check_paths:
            calibrations = load_threshold_dac_calibrations(threshold_calibration_files())
            for child in ("AB", "BC", "CD") if window == "ALL" else (window,):
                calibration = calibrations[get_window_spec(child).threshold_dac]
                if selected in NOISE_TESTS:
                    _preflight_scan_coverage(calibration, settings)
                if selected in SCURVE_TESTS:
                    _ordered_scurve_codes(calibration, settings.scurve)
        active.update({f"THRESHOLD_LUT_{letter}": getattr(cfg.PATHS, f"threshold_lut_{letter.lower()}") for letter in "ABCD"})
    refs_needed = selected == "ref_preview" or selected in SCURVE_TESTS or (physical and cfg.REFERENCE.verify_with_scope and selected != "gain_equalization")
    ref_info = {}
    if refs_needed:
        if cfg.REFERENCE.verify_with_scope:
            build_reference_verification_settings()
        mode = str(cfg.REFERENCE.mode).strip().lower()
        if mode == "manual":
            codes = (cfg.REFERENCE.manual_ref1, cfg.REFERENCE.manual_ref2)
            step = float(cfg.REFERENCE.manual_step_mv)
            if any(not isinstance(code, int) or isinstance(code, bool) or not 0 <= code <= 1023 for code in codes) or codes[0] == codes[1] or not math.isfinite(step) or step <= 0:
                raise ValueError("REFERENCE manual: нужны разные коды 0..1023 и положительная ступенька")
            ref_info = {"mode": mode, "codes": codes, "step_mv": step, "ref_lut_used": False}
        elif mode == "lut":
            steps = (cfg.CLOCK_NOISE.step_mv,) if selected == "clock_noise" else cfg.REFERENCE.steps_mv
            ref_info = {"mode": mode, "requested_steps_mv": list(steps)}
            active.update({f"REF_LUT_{index}": getattr(cfg.PATHS, f"ref_lut_{index}") for index in (1, 2)})
            if check_paths:
                calibrations = load_reference_dac_calibrations(reference_calibration_files(), voltage_unit=cfg.REFERENCE.lut_voltage_unit)
                plan = plan_reference_dac_pairs(calibrations["DAC_TST_REF1"], calibrations["DAC_TST_REF2"],
                    tuple(float(step)*1e-3 for step in steps), minimum_reference_code=cfg.REFERENCE.code_limits[0],
                    maximum_reference_code=cfg.REFERENCE.code_limits[1],
                    minimum_reference_voltage_v=settings.scurve.minimum_reference_voltage_v,
                    maximum_reference_step_error_v=settings.scurve.maximum_reference_step_error_v)
                ref_info.update(realizable_steps_mv=[1000*pair.requested_voltage_step_v for pair in plan.selections],
                                excluded_steps_mv=[1000*row["requested_voltage_step_v"] for row in plan.availability if not row["realizable"]])
                if not plan.selections:
                    raise ValueError("REFERENCE: ни одна запрошенная ступенька не реализуема по REF LUT")
        else:
            raise ValueError("REFERENCE.mode должен быть lut или manual")
    if window == "ALL" and selected in {"full", "all_windows"} and cfg.ALL_WINDOWS.final_q_sweep:
        if cfg.REFERENCE.mode != "lut" or gain_sweep_enabled():
            warnings.append("Финальный ALL Q-sweep отключен: нужны LUT REF и один GAIN, не свип")
    return {"configuration_version": 2, "test": selected, "window": window,
            "hardware_requested": physical, "hardware_enabled": cfg.RUN.hardware_enabled,
            "resume": cfg.RUN.resume, "active_paths": {key: str(value) if value is not None else None for key, value in active.items()},
            "references": ref_info, "warnings": warnings}


def _validate_gain_targets():
    cfg.metadata.GAIN_EQUALIZATION.validate()
    codes = cfg.GAIN_EQUALIZATION.target_codes
    if not isinstance(codes, (list, tuple)) or not codes or any(
        not isinstance(code, int) or isinstance(code, bool) or not 0 <= code <= 31 for code in codes
    ):
        raise ValueError("GAIN_EQUALIZATION.target_codes: нужны коды 0..31")


def run_characterization(client, calibration_files, *, test_mode: str | None = None, eo_sweep: bool = False, **kwargs):
    from comparator_characterization import characterize_all_windows, characterize_comparator, characterize_parameter_sweep
    requested_window = str(kwargs.pop("window", cfg.RUN.window)).upper()
    additional = dict(kwargs.pop("additional_metadata", {}) or {})
    additional["analysis_configuration_v2"] = configuration_snapshot("eo_sweep" if eo_sweep else test_mode)
    kwargs["additional_metadata"] = additional
    kwargs.setdefault("generate_analysis_plots", cfg.PLOTS.generate)
    if eo_sweep:
        return characterize_parameter_sweep(client, calibration_files, window=requested_window,
            eo_parameter_grid=cfg.EO_SWEEP.grid, resume_sweep=resume_sweep_path(), **kwargs)
    if requested_window == "ALL":
        options = build_all_window_settings()
        kwargs.setdefault("eo_overrides", cfg.RUN.eo_overrides)
        return characterize_all_windows(client, calibration_files, resume_experiment=resume_experiment_path(),
                                       all_window_settings=options, **kwargs)
    kwargs.setdefault("eo_overrides", cfg.RUN.eo_overrides)
    return characterize_comparator(client, calibration_files, window=requested_window,
        resume_experiment=resume_experiment_path(), **kwargs)


def print_recommendation_paths(analysis_path):
    if analysis_path is None:
        return
    print(f"Анализ и графики: {analysis_path}")
    for method in ("fit", "centroid", "maximum"):
        path = Path(analysis_path) / f"trim_recommendations_{method}.csv"
        if path.is_file():
            print(f"{method}: trim {path}; маска {Path(analysis_path) / f'bad_pixels_suggested_{method}.json'}")
    print("Новые предложения trim/масок автоматически не применяются.")


def print_result_paths(result):
    if hasattr(result, "window_results"):
        for window, child in result.window_results.items():
            print(f"Окно {window}: {child.experiment_path}")
            print_recommendation_paths(child.analysis_path)
        print(f"Совместный отчет: {result.analysis_path / 'REPORT.md'}")
        for method in ("fit", "centroid", "maximum"):
            path = result.analysis_path / f"combined_trim_map_{method}.csv"
            if path.is_file():
                print(f"Совместная trim-карта {method}: {path}")
    elif hasattr(result, "combinations"):
        for entry in result.combinations:
            print(f"EO {entry['eo_overrides']}: {entry['status']}")
        print(f"Сводка серии: {result.experiment_path / 'sweep_summary.csv'}")
    else:
        print_recommendation_paths(result.analysis_path)
