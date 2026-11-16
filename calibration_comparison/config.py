"""Complete stand preset. No .env or edits are required for the supplied bench.

The exposure and UPO modes below were explicitly confirmed by the operator.
LSB codes are register codes, not calibrated current values in amperes.
"""
from dataclasses import asdict, dataclass
from pathlib import Path

import EO_cfg
from comparator_characterization.calibration import (
    load_reference_dac_calibrations, load_threshold_dac_calibrations,
    plan_reference_dac_pairs,
)
from comparator_characterization.parameters import validate_eo_overrides

VERSION = "1.0.0"
PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class ComparisonConfig:
    host: str = "127.0.0.1"
    port: int = 0xBEEB
    timeout_s: float = 8.0
    main_fclk_mhz: int = 100
    measurement_fclk_mhz: int = 5
    shutter_s: float = 0.010
    pwm_frequency_khz: int = 100
    pwm_high_time_ns: int = 5000
    gain_code: int = 4
    windows: tuple = ("CD",)
    lsb_codes: tuple = (500, 250, 750)
    lsb_range_code: int = 3
    trim_anchors: tuple = (0, 16, 31)
    ref_steps_v: tuple = (0.040, 0.075)
    ref_code_limits: tuple = (401, 900)
    noise_repeats: int = 2
    signal_repeats: int = 2
    verification_repeats: int = 3
    coarse_step: int = 16
    anchor_full_noise: bool = True
    fine_margin_codes: int = 8
    max_background_fraction: float = 0.01
    # Exhaustive rescue is restricted to this many problematic pixels/window.
    # Other unresolved searches are explicitly marked, never certified optimal.
    exhaustive_pixel_budget: int = 16
    workers: int = 0
    generate_plots: bool = True
    open_report: bool = True
    representative_pixels: int = 6
    analysis_archive_limit_mb: int = 20

    def validate(self):
        if self.windows != ("CD",):
            raise ValueError("This experiment is restricted to the CD window")
        if self.trim_anchors != (0, 16, 31):
            raise ValueError("This preset uses the established trim anchors 0,16,31")
        if self.lsb_codes[0] != EO_cfg.DEFAULT_FIELD_VALUES["DAC_CMP_BIAS_LSB"]:
            raise ValueError("The first LSB must be the nominal EO default")
        if len(set(self.lsb_codes)) != len(self.lsb_codes):
            raise ValueError("Duplicate LSB codes")
        for code in self.lsb_codes:
            validate_eo_overrides({"DAC_CMP_BIAS_LSB": code,
                                  "DAC_CMP_BIAS_LSB_TR": self.lsb_range_code}, run_scurve=False)
        for name in ("noise_repeats", "signal_repeats", "verification_repeats"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 2:
                raise ValueError(name + " must be an integer >=2")
        if not 1 <= self.coarse_step <= 16 or self.fine_margin_codes < 4:
            raise ValueError("Invalid adaptive scan resolution")
        if not 0 < self.max_background_fraction < 0.5:
            raise ValueError("Invalid background fraction")
        if not 0 <= self.exhaustive_pixel_budget <= 512:
            raise ValueError("Invalid rescue pixel budget")
        if self.analysis_archive_limit_mb < 1 or self.representative_pixels < 1:
            raise ValueError("Invalid export limits")


def preflight(config=None, project_root=None):
    from comparator_characterization.hardware import UpoPwmSettings
    config = config or ComparisonConfig()
    config.validate()
    root = Path(project_root or PROJECT_ROOT).resolve()
    files = {f"DAC_CMP_{x}": root/"calibration"/f"DAC_CMP_{x}.csv" for x in "ABCD"}
    refs = {f"REF{x}": root/"calibration"/f"REF{x}.csv" for x in (1, 2)}
    results, mask = root/"results"/"calibration_comparison", None
    # Existing custom calibration paths and permanent mask remain authoritative.
    # Older unrelated experiment/reference options never control this preset.
    if (root/".env").is_file():
        from comparator_characterization.high_level.env_paths import load_paths
        paths = load_paths(root/".env", project_root=root)
        for x in "ABCD":
            files[f"DAC_CMP_{x}"] = getattr(paths, f"threshold_lut_{x.lower()}") or files[f"DAC_CMP_{x}"]
        for x in (1, 2):
            refs[f"REF{x}"] = getattr(paths, f"ref_lut_{x}") or refs[f"REF{x}"]
        results = (paths.results_dir or root/"results")/"calibration_comparison"
        mask = paths.bad_pixel_mask
    thresholds = load_threshold_dac_calibrations(files)
    references = load_reference_dac_calibrations(refs, voltage_unit="auto")
    plan = plan_reference_dac_pairs(references["DAC_TST_REF1"], references["DAC_TST_REF2"],
        config.ref_steps_v, minimum_reference_code=config.ref_code_limits[0],
        maximum_reference_code=config.ref_code_limits[1], maximum_reference_step_error_v=1e-3)
    if plan.unavailable_steps_v or len(plan.selections) != len(config.ref_steps_v):
        raise ValueError(f"Both preset REF steps must be realizable: {plan.availability}")
    for lut in thresholds.values():
        lut.select_upper_non_limiting_endpoint()
    pwm = UpoPwmSettings(config.pwm_frequency_khz, config.pwm_high_time_ns)
    pwm.validate()
    nominal, exact = pwm.nominal_edge_count(config.shutter_s)
    if nominal > 65534:
        raise ValueError("Exposure exceeds the 16-bit counter capacity")
    if config.timeout_s <= config.shutter_s + pwm.get_shot_timeout_margin_s:
        raise ValueError("Socket timeout is shorter than the shot")
    if mask is not None and not mask.is_file():
        raise FileNotFoundError(mask)
    if len(str(results.resolve())) > 155:
        raise ValueError("Use a shorter RESULTS_DIR (Windows result path budget)")
    return dict(config=config, thresholds=thresholds, references=references,
                pulses=tuple(p.to_pulse_amplitude() for p in plan.selections),
                pwm=pwm, nominal_n=nominal, nominal_exact=exact,
                results_root=results, bad_pixel_mask=mask,
                document={"version": VERSION, "settings": asdict(config),
                    "physical_matrix": [32,32], "owned_columns": [16,31],
                    "owned_rows": [0,31], "population":512,
                    "threshold_luts": {k:v.to_metadata() for k,v in thresholds.items()},
                    "reference_luts": {k:v.to_metadata() for k,v in references.items()},
                    "reference_plan": list(plan.availability),
                    "nominal_n":nominal, "nominal_exact":exact,
                    "exposure_source":"operator_confirmed_UPO_10ms_not_programmable",
                    "reset_kind":"EO_readback_plus_full_PX_reload_not_physical_RESET",
                    "acceptance":"tolerance_sweep_descriptive_not_device_certification",
                    "counter_operator_validated":False,
                    "global_eo_defaults":dict(EO_cfg.DEFAULT_FIELD_VALUES),
                    "pixel_preset":{"PX_GAIN":config.gain_code,"PX_SHT":2,
                        "PX_SH_EN":0,"PX_BUF_NEN":1,"PX_CMPA_TR":16,
                        "PX_CMPB_TR":16,"PX_CMPC_TR":16,"PX_CMPD_TR":16},
                    "gain_equalized":False, "new_dependencies":[]})
