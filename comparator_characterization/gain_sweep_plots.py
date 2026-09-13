"""Physical full-owned-matrix maps for measured GAIN and offline proposals."""

from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from .gain_sweep import CONTEXT, gain_context_name
from .models import AnalysisSettings
from .plots import _parallel_figures, _save_figure


def _text(settings, russian, english):
    return russian if settings.plot_language == "ru" else english


def _limits(values):
    finite = np.asarray(values, dtype=float)
    finite = finite[np.isfinite(finite)]
    if not len(finite):
        return None, None
    low, high = float(finite.min()), float(finite.max())
    if low == high:
        margin = max(abs(low) * .01, 1e-6)
        low, high = low - margin, high + margin
    return low, high


def _map(figure, axis, frame, column, label, settings, *, limits=None, diverging=False):
    # Always display the complete owned 16x32 region in physical coordinates.
    array = np.full((32, 16), np.nan)
    if column in frame:
        for row in frame[["column", "row", column]].itertuples(index=False, name=None):
            x, y, value = row
            if 16 <= int(x) <= 31 and 0 <= int(y) <= 31:
                array[int(y), int(x) - 16] = float(value)
    cmap = plt.get_cmap("coolwarm" if diverging else "viridis").copy()
    cmap.set_bad("#d9d9d9")
    low, high = limits if limits is not None else _limits(array)
    image = axis.imshow(
        np.ma.masked_invalid(array), origin="lower", interpolation="nearest",
        extent=(15.5, 31.5, -.5, 31.5),
        aspect="equal" if settings.square_physical_pixels else "auto",
        cmap=cmap, vmin=low, vmax=high,
    )
    axis.set_xlabel(_text(settings, "Физический столбец", "Physical column"))
    axis.set_ylabel(_text(settings, "Физическая строка", "Physical row"))
    axis.set_title(label)
    # Units are already in the title. Repeating a long vertical label crowds
    # the physical-row label of the neighboring matrix.
    figure.colorbar(image, ax=axis, pad=.02)


def _map_layout(figure, *, rows=1):
    figure.subplots_adjust(left=.055, right=.965, bottom=.10,
                           top=.78 if rows == 1 else .87,
                           wspace=.55, hspace=.38)


@_parallel_figures
def plot_gain_sweep(pixel_metrics: pd.DataFrame, *, directory: Path, settings: AnalysisSettings) -> dict:
    outputs = {}
    if pixel_metrics.empty:
        return outputs
    for context, group in pixel_metrics.groupby(CONTEXT, dropna=False, sort=True):
        name = gain_context_name(dict(zip(CONTEXT, context)))
        for step_index, (step, at_step) in enumerate(group.groupby("injection_voltage_step_v", sort=True)):
            valid = at_step[at_step["amplitude_valid"]].copy()
            charge_gain = valid["nominal_gain_mv_per_ke"].notna().any()
            gain_column = "nominal_gain_mv_per_ke" if charge_gain else "gain_v_per_injection_step_v"
            gain_label = _text(settings, "Номинальное усиление, мВ/кэ", "Nominal gain, mV/ke") if charge_gain else _text(settings, "Отклик, В/В ступени REF", "Response, V/V REF step")
            labels = (
                _text(settings, "Эффективная база, В", "Effective baseline, V"),
                _text(settings, "Сигнальная амплитуда, мВ", "Signal amplitude, mV"),
                gain_label,
            )
            columns = ("baseline_v", "amplitude_mv", gain_column)
            limits = [_limits(valid[column]) for column in columns]
            for code, measured in at_step.groupby("gain_sweep_code", sort=True):
                accepted = measured[measured["amplitude_valid"]]
                figure, axes = plt.subplots(1, 3, figsize=(15.6, 6.0))
                for axis, column, label, scale in zip(axes, columns, labels, limits):
                    source = measured if column == "baseline_v" else accepted
                    _map(figure, axis, source, column, label, settings, limits=scale)
                figure.suptitle(_text(settings, "Измеренный свип GAIN", "Measured GAIN sweep") + f": {context[0]}, GAIN={int(code)}, REF={1000*step:g} mV, FCLK={context[1]:g}, {context[2]}\n" + _text(settings, "Серый: нет пригодных данных; тип базы и усиления указан в CSV", "Gray: unavailable data; baseline and gain methods are recorded in CSV"))
                _map_layout(figure)
                stem = f"{name}/gain_{int(code):02d}/step_{step_index:03d}_maps"
                outputs[stem] = _save_figure(figure, directory / name / f"gain_{int(code):02d}", f"step_{step_index:03d}_maps", settings)
            if valid.empty:
                continue
            figure, axes = plt.subplots(1, 2, figsize=(11.5, 4.7))
            for (_, _), pixel in valid.groupby(["column", "row"]):
                pixel = pixel.sort_values("gain_sweep_code")
                axes[0].plot(pixel.gain_sweep_code, pixel.amplitude_mv, color="#4c78a8", alpha=.09, linewidth=.7)
                axes[1].plot(pixel.gain_sweep_code, pixel[gain_column], color="#4c78a8", alpha=.09, linewidth=.7)
            for axis, column, label in zip(axes, ("amplitude_mv", gain_column), labels[1:]):
                curve = valid.groupby("gain_sweep_code")[column].agg(["median", "mean"])
                axis.plot(curve.index, curve["median"], "o-", color="#e45756", label=_text(settings, "Медиана", "Median"))
                axis.plot(curve.index, curve["mean"], "s--", color="black", label=_text(settings, "Среднее", "Mean"))
                axis.set_xlabel("GAIN")
                axis.set_ylabel(label)
                axis.legend()
                axis.grid(alpha=.2)
            figure.suptitle(_text(settings, "Отклик всех пикселей матрицы", "Response of every matrix pixel") + f": {context[0]}, REF={1000*step:g} mV, FCLK={context[1]:g}, {context[2]}")
            stem = f"{name}/step_{step_index:03d}_all_pixel_response"
            outputs[stem] = _save_figure(figure, directory / name, f"step_{step_index:03d}_all_pixel_response", settings)
        slopes = group[group["slope_gain_available"]].drop_duplicates(
            ["gain_sweep_code", "column", "row"]
        )
        if not slopes.empty:
            column = ("slope_nominal_gain_mv_per_ke"
                      if slopes["slope_nominal_gain_mv_per_ke"].notna().any()
                      else "slope_gain_v_per_injection_step_v")
            label = (_text(settings, "Усиление по наклону, мВ/кэ", "Slope gain, mV/ke")
                     if column.endswith("mv_per_ke") else
                     _text(settings, "Наклон V50(dV), В/В", "V50(dV) slope, V/V"))
            scale = _limits(slopes[column])
            for code, measured in slopes.groupby("gain_sweep_code", sort=True):
                figure, axis = plt.subplots(figsize=(7.2, 6.7))
                _map(figure, axis, measured, column, label, settings, limits=scale)
                figure.suptitle(f"{context[0]}, GAIN={int(code)}, FCLK={context[1]:g}, {context[2]}")
                _map_layout(figure)
                stem = f"{name}/gain_{int(code):02d}/slope_gain_map"
                outputs[stem] = _save_figure(
                    figure, directory / name / f"gain_{int(code):02d}",
                    "slope_gain_map", settings,
                )
    return outputs


@_parallel_figures
def plot_gain_equalization(maps: pd.DataFrame, predictions: pd.DataFrame, *, directory: Path, settings: AnalysisSettings) -> dict:
    outputs = {}
    if maps.empty:
        return outputs
    for keys, gain_map in maps.groupby(CONTEXT + ["target_gain_code"], sort=True):
        name = gain_context_name(dict(zip(CONTEXT, keys[:3]))) + f"/target_gain_{int(keys[3]):02d}"
        resolved = gain_map[gain_map["recommendation_status"].eq("measured_code_proposal")].copy()
        resolved["code_delta"] = resolved["gain"] - int(keys[3])
        figure, axes = plt.subplots(1, 2, figsize=(11.0, 6.0))
        _map(figure, axes[0], resolved, "gain", _text(settings, "Предложенный код GAIN", "Proposed GAIN code"), settings, limits=(0, 31))
        limit = max(float(abs(resolved["code_delta"]).max()) if len(resolved) else 0, 1)
        _map(figure, axes[1], resolved, "code_delta", _text(settings, "Изменение кода GAIN", "GAIN code change"), settings, limits=(-limit, limit), diverging=True)
        figure.suptitle(_text(settings, "Предложение эквализации", "Equalization proposal") + f": {keys[0]}, TARGET_GAIN={int(keys[3])}\n" + _text(settings, "Серый: неопределено, в CSV сохранен целевой код; ASIC не проверен", "Gray: unresolved, target code retained in CSV; ASIC not verified"))
        _map_layout(figure)
        outputs[f"{name}/gain_codes"] = _save_figure(figure, directory / name, "gain_codes", settings)
        if predictions.empty:
            continue
        chosen = predictions.copy()
        for column, value in zip(CONTEXT + ["target_gain_code"], keys):
            chosen = chosen[chosen[column] == value]
        for index, (step, response) in enumerate(chosen.groupby("injection_voltage_step_v", sort=True)):
            response = response.copy()
            for column in ("before_amplitude_v", "predicted_amplitude_v", "predicted_amplitude_residual_v"):
                response[column.replace("_v", "_mv")] = 1000.0 * response[column]
            scale = _limits(response[["before_amplitude_mv", "predicted_amplitude_mv"]].to_numpy())
            residual_limit = max(float(abs(response["predicted_amplitude_residual_mv"]).max()), 1e-6)
            figure, axes = plt.subplots(2, 3, figsize=(16.0, 10.5))
            _map(figure, axes[0, 0], response, "before_amplitude_mv", _text(settings, "Амплитуда до, мВ", "Amplitude before, mV"), settings, limits=scale)
            _map(figure, axes[0, 1], response, "predicted_amplitude_mv", _text(settings, "Прогноз амплитуды, мВ", "Predicted amplitude, mV"), settings, limits=scale)
            _map(figure, axes[0, 2], response, "predicted_amplitude_residual_mv", _text(settings, "Остаток от цели, мВ", "Residual to target, mV"), settings, limits=(-residual_limit, residual_limit), diverging=True)
            baseline_scale = _limits(response[["before_baseline_v", "predicted_baseline_v"]].to_numpy())
            _map(figure, axes[1, 0], response, "before_baseline_v", _text(settings, "База до, В", "Baseline before, V"), settings, limits=baseline_scale)
            _map(figure, axes[1, 1], response, "predicted_baseline_v", _text(settings, "Прогноз базы, В", "Predicted baseline, V"), settings, limits=baseline_scale)
            gain_column = "predicted_nominal_gain_mv_per_ke" if response["predicted_nominal_gain_mv_per_ke"].notna().any() else "predicted_gain_v_per_injection_step_v"
            gain_label = _text(settings, "Прогноз усиления, мВ/кэ", "Predicted gain, mV/ke") if gain_column.endswith("mv_per_ke") else _text(settings, "Прогноз отклика, В/В REF", "Predicted response, V/V REF")
            _map(figure, axes[1, 2], response, gain_column, gain_label, settings)
            figure.suptitle(_text(settings, "Прогноз из измеренного равномерного свипа, не проверка смешанной карты", "Prediction from measured uniform sweep, not mixed-map verification") + f"\n{keys[0]}, TARGET_GAIN={int(keys[3])}, REF={1000*step:g} mV, FCLK={keys[1]:g}, {keys[2]}")
            _map_layout(figure, rows=2)
            outputs[f"{name}/step_{index:03d}_prediction"] = _save_figure(figure, directory / name, f"step_{index:03d}_prediction", settings)
            suffix = ("nominal_gain_mv_per_ke"
                      if response["predicted_nominal_gain_mv_per_ke"].notna().any()
                      else "gain_v_per_injection_step_v")
            unit = _text(settings, "мВ/кэ", "mV/ke") if suffix.endswith("mv_per_ke") else "V/V REF"
            before_gain, predicted_gain = f"before_{suffix}", f"predicted_{suffix}"
            target_gain = f"target_{suffix}"
            response["gain_residual"] = response[predicted_gain] - response[target_gain]
            scale = _limits(response[[before_gain, predicted_gain]].to_numpy())
            residual_limit = max(float(abs(response.gain_residual).max()), 1e-6)
            figure, axes = plt.subplots(1, 3, figsize=(15.6, 6.0))
            for axis, column, label in zip(axes[:2], (before_gain, predicted_gain), (
                _text(settings, "Усиление до", "Gain before"),
                _text(settings, "Прогноз усиления", "Predicted gain"),
            )):
                _map(figure, axis, response, column, f"{label}, {unit}", settings, limits=scale)
            _map(figure, axes[2], response, "gain_residual",
                 _text(settings, "Остаток усиления от цели", "Gain residual to target") + f", {unit}",
                 settings, limits=(-residual_limit, residual_limit), diverging=True)
            gain_title = (_text(settings, "Усиление A/Q: до и по прогнозу", "A/Q gain: before and predicted")
                          if suffix.endswith("mv_per_ke") else
                          _text(settings, "Отклик A/dV: до и по прогнозу", "A/dV response: before and predicted"))
            figure.suptitle(gain_title
                             + f"\n{keys[0]}, TARGET_GAIN={int(keys[3])}, REF={1000*step:g} mV, FCLK={keys[1]:g}, {keys[2]}")
            _map_layout(figure)
            stem = f"{name}/step_{index:03d}_gain_prediction"
            outputs[stem] = _save_figure(figure, directory / name, f"step_{index:03d}_gain_prediction", settings)
            paired = response.dropna(subset=["before_amplitude_mv", "predicted_amplitude_mv"])
            figure, axis = plt.subplots(figsize=(7.5, 4.8))
            if not paired.empty:
                values = [paired.before_amplitude_mv.to_numpy(), paired.predicted_amplitude_mv.to_numpy()]
                edges = np.histogram_bin_edges(np.concatenate(values), bins="auto")
                axis.hist(values, bins=edges, label=(_text(settings, "До", "Before"), _text(settings, "Прогноз", "Prediction")), alpha=.65)
                axis.axvline(1000.0 * float(paired.iloc[0].target_amplitude_v), color="black", linestyle="--", label=_text(settings, "Цель", "Target"))
                axis.legend()
            axis.set_xlabel(_text(settings, "Сигнальная амплитуда, мВ", "Signal amplitude, mV"))
            axis.set_ylabel(_text(settings, "Число пикселей", "Pixel count"))
            axis.set_title(_text(settings, "Одинаковые физические пиксели до и в прогнозе", "Same physical pixels before and in prediction") + f"\nTARGET_GAIN={int(keys[3])}, REF={1000*step:g} mV, N={len(paired)}")
            outputs[f"{name}/step_{index:03d}_distribution"] = _save_figure(figure, directory / name, f"step_{index:03d}_distribution", settings)
    return outputs
