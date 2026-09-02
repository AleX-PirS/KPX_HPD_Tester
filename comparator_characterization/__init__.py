"""Comparator characterization, equalization and offline analysis.

The package deliberately keeps hardware acquisition separate from analysis.
Raw experiment directories produced by :func:`characterize_comparator` can be
reprocessed later with :func:`analyze_saved_experiment` without importing or
connecting any instrument driver.
"""

from .analysis import analyze_saved_experiment, analyze_saved_noise_statistics
from .calibration import (
    ReferenceDacCalibration,
    ReferencePairSelection,
    ThresholdDacCalibration,
    load_reference_dac_calibrations,
    load_threshold_dac_calibrations,
    select_reference_dac_pairs,
)
from .hardware import (
    CallableShotExecutor,
    KeysightBurstSettings,
    KeysightBurstShotExecutor,
    MGPDGetShotExecutor,
    MGPDMeasurementBackend,
    ShotExecutionResult,
    ShotExecutor,
    ShotRequest,
    UpoPwmSettings,
    UpoPwmShotExecutor,
)
from .injection import (
    CROSSTALK_INJECTION_PATTERNS,
    InjectionGroup,
    build_injection_groups,
    load_gain_map_csv,
    resolve_gain_map,
)
from .models import (
    AnalysisSettings,
    CharacterizationSettings,
    EqualizationSettings,
    FRAMEWORK_VERSION,
    NoiseScanSettings,
    ScurveSettings,
    WindowSpec,
    get_window_spec,
)
from .pixel_masks import BadPixelMapInput, normalize_bad_pixel_map
from .recommendations import load_recommended_trim_map, propose_noise_trim_maps
from .reference_verification import (
    ReferenceStepVerificationError,
    ReferenceStepVerificationResult,
    ReferenceStepVerificationSettings,
    verify_reference_steps,
)
from .workflow import (
    CharacterizationResult,
    ManualExposureChange,
    characterize_comparator,
    characterize_injection_crosstalk,
    interactive_exposure_pause,
)
from .sweep import (
    ParameterSweepResult, SweepNoiseExposure, characterize_parameter_sweep,
    interactive_noise_exposure_pause,
)

__all__ = [
    "AnalysisSettings",
    "ParameterSweepResult",
    "SweepNoiseExposure",
    "characterize_parameter_sweep",
    "interactive_noise_exposure_pause",
    "BadPixelMapInput",
    "CallableShotExecutor",
    "CROSSTALK_INJECTION_PATTERNS",
    "CharacterizationResult",
    "CharacterizationSettings",
    "EqualizationSettings",
    "FRAMEWORK_VERSION",
    "InjectionGroup",
    "KeysightBurstSettings",
    "KeysightBurstShotExecutor",
    "MGPDGetShotExecutor",
    "MGPDMeasurementBackend",
    "NoiseScanSettings",
    "ReferenceDacCalibration",
    "ReferencePairSelection",
    "ReferenceStepVerificationError",
    "ReferenceStepVerificationResult",
    "ReferenceStepVerificationSettings",
    "ScurveSettings",
    "ShotExecutionResult",
    "ShotExecutor",
    "ShotRequest",
    "UpoPwmSettings",
    "UpoPwmShotExecutor",
    "ThresholdDacCalibration",
    "WindowSpec",
    "analyze_saved_experiment",
    "analyze_saved_noise_statistics",
    "propose_noise_trim_maps",
    "load_recommended_trim_map",
    "build_injection_groups",
    "characterize_comparator",
    "characterize_injection_crosstalk",
    "get_window_spec",
    "load_threshold_dac_calibrations",
    "load_reference_dac_calibrations",
    "load_gain_map_csv",
    "ManualExposureChange",
    "interactive_exposure_pause",
    "resolve_gain_map",
    "normalize_bad_pixel_map",
    "select_reference_dac_pairs",
    "verify_reference_steps",
]
