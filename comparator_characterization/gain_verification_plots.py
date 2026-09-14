"""Measured, not predicted, GAIN equalization and coordinated-window diagnostics."""
from __future__ import annotations

from pathlib import Path
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from .gain_sweep import CONTEXT, gain_context_name
from .gain_sweep_plots import _limits, _map, _map_layout, _text
from .models import AnalysisSettings
from .plots import _parallel_figures, _save_figure


def _symmetric(values):
    finite = np.asarray(values, dtype=float)
    finite = finite[np.isfinite(finite)]
    size = max(float(np.max(np.abs(finite))) if finite.size else 0, 1e-6)
    return -size, size


@_parallel_figures
def plot_gain_verification(comparison: pd.DataFrame, *, directory: Path, settings: AnalysisSettings) -> dict:
    outputs = {}
    keys = CONTEXT + ["target_gain_code", "injection_voltage_step_v"]
    for index, (context, group) in enumerate(comparison.groupby(keys, sort=True)):
        group = group.copy()
        name = gain_context_name(dict(zip(CONTEXT, context))) + f"/target_gain_{int(context[3]):02d}"
        captions = (_text(settings, "До", "Before"), _text(settings, "Прогноз", "Prediction"),
                    _text(settings, "Реальное измерение", "Actual measurement"))
        charge = group.target_nominal_gain_mv_per_ke.notna().any()
        gain_suffix = "nominal_gain_mv_per_ke" if charge else "gain_v_per_injection_step_v"
        gain_unit = _text(settings, "мВ/кэ", "mV/ke") if charge else "V/V REF"
        for prefix in ("before", "predicted", "actual"):
            group[f"{prefix}_amplitude_mv"] = 1000 * group[f"{prefix}_amplitude_v"]
        for kind, suffix, label in (
            ("amplitude", "amplitude_mv", _text(settings, "Амплитуда, мВ", "Amplitude, mV")),
            ("gain", gain_suffix, _text(settings, "Усиление", "Gain") + f", {gain_unit}"),
            ("baseline", "baseline_v", _text(settings, "Эффективная база, В", "Effective baseline, V")),
        ):
            columns = [f"{prefix}_{suffix}" for prefix in ("before", "predicted", "actual")]
            limits = _limits(group[columns].to_numpy())
            figure, axes = plt.subplots(1, 3, figsize=(15.6, 6.0))
            for axis, column, caption in zip(axes, columns, captions):
                _map(figure, axis, group, column, caption + "\n" + label, settings, limits=limits)
            figure.suptitle(_text(settings, "Проверка смешанной GAIN-карты", "Mixed GAIN map verification")
                             + f": {context[0]}, TARGET_GAIN={int(context[3])}, FCLK={context[1]:g}, {context[2]}\n"
                             + f"REF={1000*context[4]:g} " + _text(settings, "мВ; серый: нет пригодных данных", "mV; gray: unavailable data"))
            _map_layout(figure)
            stem = f"step_{index:03d}_actual_{kind}_maps"
            outputs[f"{name}/{stem}"] = _save_figure(figure, directory, stem, settings)
        group["actual_residual_mv"] = 1000 * group.actual_amplitude_residual_v
        group["prediction_error_mv"] = 1000 * group.prediction_error_v
        figure, axes = plt.subplots(1, 2, figsize=(11.5, 6.0))
        for axis, column, label in zip(axes, ("actual_residual_mv", "prediction_error_mv"), (
            _text(settings, "Измерение минус цель, мВ", "Measurement minus target, mV"),
            _text(settings, "Измерение минус прогноз, мВ", "Measurement minus prediction, mV"),
        )):
            _map(figure, axis, group, column, label, settings, limits=_symmetric(group[column]), diverging=True)
        figure.suptitle(f"{context[0]}, TARGET_GAIN={int(context[3])}, REF={1000*context[4]:g} mV")
        _map_layout(figure)
        stem = f"step_{index:03d}_actual_residual_maps"
        outputs[f"{name}/{stem}"] = _save_figure(figure, directory, stem, settings)
        paired = group.dropna(subset=["before_amplitude_mv", "predicted_amplitude_mv", "actual_amplitude_mv"])
        figure, axes = plt.subplots(1, 2, figsize=(11.5, 4.8))
        for axis, suffix, label, target_column, factor in (
            (axes[0], "amplitude_mv", _text(settings, "Амплитуда, мВ", "Amplitude, mV"), "target_amplitude_v", 1000),
            (axes[1], gain_suffix, _text(settings, "Усиление", "Gain") + f", {gain_unit}",
             "target_nominal_gain_mv_per_ke" if charge else "target_gain_v_per_injection_step_v", 1),
        ):
            values = [paired[f"{prefix}_{suffix}"].to_numpy() for prefix in ("before", "predicted", "actual")]
            if len(paired) and all(np.isfinite(value).all() for value in values):
                edges = np.histogram_bin_edges(np.concatenate(values), bins="auto")
                axis.hist(values, bins=edges, label=captions, alpha=.65)
                axis.axvline(factor * float(paired.iloc[0][target_column]), color="black", linestyle="--",
                             label=_text(settings, "Цель", "Target"))
                axis.legend(fontsize=8)
            axis.set_xlabel(label)
            axis.set_ylabel(_text(settings, "Число пикселей", "Pixel count"))
        figure.suptitle(_text(settings, "До, прогноз и реальный отклик: одинаковые пиксели", "Before, prediction and actual response: same pixels")
                         + f"\n{context[0]}, TARGET_GAIN={int(context[3])}, REF={1000*context[4]:g} mV, N={len(paired)}")
        figure.subplots_adjust(wspace=.3, top=.78, bottom=.14)
        stem = f"step_{index:03d}_actual_distributions"
        outputs[f"{name}/{stem}"] = _save_figure(figure, directory, stem, settings)
    return outputs


@_parallel_figures
def plot_joint_gain_verification(metrics: pd.DataFrame, *, directory: Path, settings: AnalysisSettings) -> dict:
    outputs = {}
    keys = ["measurement_fclk_mhz", "injection_pattern", "target_gain_code", "injection_voltage_step_v"]
    for index, (context, group) in enumerate(metrics.groupby(keys, sort=True)):
        group = group.copy()
        name = f"target_gain_{int(context[2]):02d}/fclk_{context[0]:g}_{context[1]}"
        for window in ("AB", "BC", "CD"):
            group[f"relative_gain_deviation_{window}_percent"] = 100 * group[f"relative_gain_deviation_{window}"]
            group[f"baseline_offset_{window}_mv"] = 1000 * group[f"baseline_offset_{window}_v"]
            group[f"actual_amplitude_{window}_mv"] = 1000 * group[f"actual_amplitude_{window}_v"]
            group[f"sigma_{window}_mv"] = 1000 * group[f"sigma_{window}_v"]
        for kind, columns, label, diverging in (
            ("amplitude", [f"actual_amplitude_{w}_mv" for w in ("AB", "BC", "CD")],
             _text(settings, "Реальная амплитуда, мВ", "Actual amplitude, mV"), False),
            ("absolute_gain", [f"actual_gain_{w}_mv_per_ke" for w in ("AB", "BC", "CD")],
             _text(settings, "Реальное усиление A/Q, мВ/кэ", "Actual A/Q gain, mV/ke"), False),
            ("absolute_baseline", [f"actual_baseline_{w}_v" for w in ("AB", "BC", "CD")],
             _text(settings, "Реальная база, В", "Actual baseline, V"), False),
            ("transition_width", [f"sigma_{w}_mv" for w in ("AB", "BC", "CD")],
             _text(settings, "Ширина перехода S-кривой, мВ", "S-curve transition width, mV"), False),
            ("gain", [f"relative_gain_deviation_{w}_percent" for w in ("AB", "BC", "CD")],
             _text(settings, "Отклонение усиления от цели, %", "Gain deviation from target, %"), True),
            ("baseline", [f"baseline_offset_{w}_mv" for w in ("AB", "BC", "CD")],
             _text(settings, "Отклонение базы от медианы окна, мВ", "Baseline deviation from window median, mV"), True),
        ):
            figure, axes = plt.subplots(1, 3, figsize=(15.6, 6.0))
            scale = _symmetric(group[columns].to_numpy()) if diverging else _limits(group[columns].to_numpy())
            for axis, column, window in zip(axes, columns, ("AB", "BC", "CD")):
                _map(figure, axis, group, column, window + "\n" + label, settings, limits=scale, diverging=diverging)
            figure.suptitle(_text(settings, "Реальные окна с одной общей GAIN-картой", "Measured windows with one shared GAIN map")
                             + f"\nTARGET_GAIN={int(context[2])}, REF={1000*context[3]:g} mV, FCLK={context[0]:g}, {context[1]}")
            _map_layout(figure)
            stem = f"step_{index:03d}_joint_{kind}_maps"
            outputs[f"{name}/{stem}"] = _save_figure(figure, directory / name, stem, settings)
        valid = group[group.complete_response]
        figure, axes = plt.subplots(1, 2, figsize=(11.5, 4.8))
        axes[0].scatter(100*valid.common_gain_deviation, 100*valid.differential_gain_range, s=10, alpha=.6)
        axes[1].scatter(1000*valid.common_baseline_shift_v, 1000*valid.differential_baseline_range_v, s=10, alpha=.6)
        axes[0].set_xlabel(_text(settings, "Общая составляющая усиления, %", "Common gain deviation, %"))
        axes[0].set_ylabel(_text(settings, "Дифференциальный размах усиления, %", "Differential gain range, %"))
        axes[1].set_xlabel(_text(settings, "Общая составляющая базы, мВ", "Common baseline shift, mV"))
        axes[1].set_ylabel(_text(settings, "Дифференциальный размах базы, мВ", "Differential baseline range, mV"))
        for axis in axes:
            axis.grid(alpha=.2)
        figure.suptitle(_text(settings, "Общие и дифференциальные признаки, не доказательство причины", "Common and differential signs, not proof of cause")
                         + f"\nTARGET_GAIN={int(context[2])}, REF={1000*context[3]:g} mV, N={len(valid)}")
        figure.subplots_adjust(wspace=.4, top=.78, bottom=.16)
        stem = f"step_{index:03d}_joint_components"
        outputs[f"{name}/{stem}"] = _save_figure(figure, directory / name, stem, settings)
    return outputs
