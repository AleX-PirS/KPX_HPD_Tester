"""Продвинутые параметры v2. Пути здесь не задаются.

Обычный запуск настраивается в characterization_config.py. Здесь изменяются
точность, критерии fit, ресурсы анализа и детали аппаратного протокола.
Значения используются и записываются в metadata результатов. Основные поля
noise/S-curve, заданные в пользовательских блоках, имеют приоритет.
"""
from comparator_characterization import (
    AnalysisSettings, AllWindowSettings, EqualizationSettings,
    GainEqualizationSettings, KeysightBurstSettings, NoiseScanSettings,
    ReferenceStepVerificationSettings, ScurveSettings,
)

# Декодирование и поведение чтения. Не менять без проверки УПО.
NOISE = NoiseScanSettings(
    settling_time_s=0.1, fine_step=1, fine_margin_codes=16,
    counter_mode_bits=16, decode_lfsr=True, lfsr_direction="left",
    configure_get_shot_omr=False, mode_read=0b010, crw_mode=0,
    continue_after_pixel_read_error=True,
    empty_matrix_repeats_to_skip_remaining=1,
    upo_reconnect_attempts=3, upo_reconnect_backoff_s=0.5,
)

# Trim 0/16/31, локальное уточнение и выбор общего эффективного порога.
EQUALIZATION = EqualizationSettings(
    trim_min=0, trim_reference=16, trim_max=31,
    local_search_radius=1, expanded_search_radius=3,
    verification_margin_codes=24, full_trim_fallback=False, target_voltage=None,
)

# Защита V50/шумового колокола, адаптивные повторы и заряд.
SCURVE = ScurveSettings(
    max_background_fraction=0.01,
    injection_capacitance_f=15e-15, injection_capacitance_relative_uncertainty=0.20,
    minimum_reference_voltage_v=None, maximum_reference_step_error_v=1e-3,
    scan_descending=True, coarse_step=8, fine_step=1, fine_margin_codes=8,
    expand_codes=32, max_expand_rounds=4,
    baseline_noise_stop_enabled=True, baseline_noise_count_multiplier=1.0,
    baseline_noise_pixel_fraction=0.10, baseline_noise_consecutive_codes=2,
    sparse_background_interval_codes=16, adaptive_repeats=True,
    transition_repeat_low_fraction=0.10, transition_repeat_high_fraction=0.90,
    weak_signal_dense_scan_below_v=0.025,
    signal_detection_fraction_of_n=0.01, signal_detection_pixel_fraction=0.02,
)

# Критерии обработки и ресурсы компьютера, НЕ параллелизм GET_SHOT.
ANALYSIS = AnalysisSettings(
    noise_min_points=5, gaussian_min_r2=0.70,
    max_asymmetry_ratio=2.5, plateau_sigma_factor=1.0,
    plot_all_trim_heatmaps=False,
    workers=0, plot_workers=0, read_workers=0,
    parallel_min_groups=128, parallel_batch_size=64,
    infer_upo_pwm_plateau_denominator=True, scurve_plateau_min_codes=3,
    scurve_fit_core_low_fraction=0.05, scurve_fit_core_high_fraction=0.95,
    scurve_plot_zero_tail_points=3, scurve_plot_code_margin=2,
    scurve_plot_noise_peak_search_codes=32, scurve_plot_noise_peak_support_fraction=0.05,
)

# Веса и качество подбора GAIN. При одной ступеньке усиление = A/Q.
GAIN_EQUALIZATION = GainEqualizationSettings(
    target_statistic="median", amplitude_weight=1.0, gain_weight=1.0,
    minimum_gain_fit_r2=0.98, include_poor_fits=False, allow_shared_noise_baseline=True,
)
GAIN_CHECK_ALLOW_UNRESOLVED = False
GAIN_CHECK_BACKGROUND_MODE = "sparse"

ALL_WINDOWS = AllWindowSettings(
    common_shift_z_threshold=3.0, common_shift_max_differential_z=1.0,
    comparator_outlier_z_threshold=3.0, good_fit_r2=0.80,
)

# AMUX + scope, CH1=TST_SIG, CH4=CTRL. Язык следует PLOTS.language.
REFERENCE_VERIFICATION = ReferenceStepVerificationSettings(
    signal_channel=1, trigger_channel=4, trigger_level_v=0.5, trigger_slope="NEG",
    time_scale_s=5e-7, time_offset_s=0.0,
    waveform_points=12_500, averaging_enabled=False, average_count=1,
    signal_scale_v=0.2, signal_offset_v=0.4, trigger_scale_v=0.5, trigger_offset_v=1.5,
    scope_arm_delay_s=0.10, acquisition_time_s=0.25,
    plateau_guard_s=1.5e-7, plateau_window_s=1e-6,
    maximum_scope_step_error_v=1e-3, clock_on_frequency_mhz=50,
    acquisition_retries=2, retry_backoff_s=0.25,
    abort_on_failure=True, save_screenshots=False,
)
OSCILLOSCOPE_TIMEOUT_MS = 5_000
UPO_PWM_EDGE_COUNT_UNCERTAINTY = 1

# Резервный CTRL. Задержка от GET_SHOT, а не от реального shutter.
BURST = KeysightBurstSettings(channel=1, shutter_start_delay_s=0.8, post_burst_guard_s=0.1)
