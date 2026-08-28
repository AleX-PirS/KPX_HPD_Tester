from __future__ import annotations

import json
import math
from pathlib import Path
import re
from statistics import NormalDist
from typing import Any

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from .models import AnalysisSettings


_NORMAL = NormalDist()


def _save_figure(
    figure: plt.Figure,
    directory: Path,
    stem: str,
    settings: AnalysisSettings,
) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    outputs = [directory / f"{stem}.png"]
    figure.savefig(outputs[0], dpi=settings.plot_dpi, bbox_inches="tight")
    if settings.save_pdf_plots:
        outputs.append(directory / f"{stem}.pdf")
        figure.savefig(outputs[-1], bbox_inches="tight")
    plt.close(figure)
    return outputs


def _finite_values(frame: pd.DataFrame, column: str) -> np.ndarray:
    if frame.empty or column not in frame:
        return np.array([], dtype=float)
    values = pd.to_numeric(frame[column], errors="coerce").to_numpy(dtype=float)
    return values[np.isfinite(values)]


def _numeric_series(frame: pd.DataFrame, column: str) -> pd.Series:
    if column not in frame:
        return pd.Series(np.nan, index=frame.index, dtype=float)
    return pd.to_numeric(frame[column], errors="coerce")


def _stage_frame(frame: pd.DataFrame, stage: str) -> pd.DataFrame:
    if frame.empty or "stage" not in frame:
        return frame.iloc[0:0].copy()
    return frame[frame["stage"] == stage].copy()


def _heatmap(
    frame: pd.DataFrame,
    *,
    value_column: str,
    title: str,
    colorbar_label: str,
    cmap: str = "viridis",
    center_zero: bool = False,
) -> plt.Figure | None:
    if frame.empty or value_column not in frame:
        return None
    data = frame[["column", "row", value_column]].copy()
    data[value_column] = pd.to_numeric(data[value_column], errors="coerce")
    data = data.dropna(subset=[value_column])
    if data.empty:
        return None
    pivot = data.pivot_table(index="row", columns="column", values=value_column, aggfunc="first")
    columns = np.arange(int(data["column"].min()), int(data["column"].max()) + 1)
    rows = np.arange(0, 32)
    pivot = pivot.reindex(index=rows, columns=columns)
    array = pivot.to_numpy(dtype=float)
    kwargs: dict[str, Any] = {}
    if center_zero:
        limit = float(np.nanmax(np.abs(array))) if np.any(np.isfinite(array)) else 1.0
        kwargs.update(vmin=-limit, vmax=limit)
    figure, axis = plt.subplots(figsize=(7.2, 6.0))
    image = axis.imshow(
        array,
        origin="lower",
        aspect="auto",
        interpolation="nearest",
        extent=(columns[0] - 0.5, columns[-1] + 0.5, -0.5, 31.5),
        cmap=cmap,
        **kwargs,
    )
    axis.set_xlabel("Physical ASIC column")
    axis.set_ylabel("Physical ASIC row")
    axis.set_title(title)
    colorbar = figure.colorbar(image, ax=axis, pad=0.02)
    colorbar.set_label(colorbar_label)
    return figure


def _representative_coordinates(
    frame: pd.DataFrame,
    value_column: str,
    count: int,
) -> list[tuple[int, int]]:
    if frame.empty or any(
        column not in frame for column in ("column", "row", value_column)
    ):
        return []
    data = frame[["column", "row", value_column]].copy()
    data[value_column] = pd.to_numeric(data[value_column], errors="coerce")
    data = data.dropna(subset=[value_column]).sort_values(value_column)
    if data.empty:
        return []
    indices = np.unique(np.rint(np.linspace(0, len(data) - 1, min(count, len(data)))).astype(int))
    return [
        (int(data.iloc[index]["column"]), int(data.iloc[index]["row"]))
        for index in indices
    ]


def _plot_coordinates(
    settings: AnalysisSettings,
    frame: pd.DataFrame,
    value_column: str,
) -> list[tuple[int, int]]:
    if frame.empty or "column" not in frame or "row" not in frame:
        return []
    if settings.plot_pixels:
        available = {
            (int(row["column"]), int(row["row"]))
            for _, row in frame[["column", "row"]].drop_duplicates().iterrows()
        }
        return [coordinate for coordinate in settings.plot_pixels if coordinate in available]
    return _representative_coordinates(
        frame, value_column, settings.representative_pixels
    )


def _matrix_array(
    frame: pd.DataFrame,
    value_column: str,
) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
    if frame.empty or value_column not in frame:
        return None
    data = frame[["column", "row", value_column]].copy()
    data[value_column] = pd.to_numeric(data[value_column], errors="coerce")
    data = data.dropna(subset=[value_column])
    if data.empty:
        return None
    columns = np.arange(int(data["column"].min()), int(data["column"].max()) + 1)
    rows = np.arange(int(data["row"].min()), int(data["row"].max()) + 1)
    pivot = data.pivot_table(
        index="row", columns="column", values=value_column, aggfunc="first"
    ).reindex(index=rows, columns=columns)
    return pivot.to_numpy(dtype=float), columns, rows


def _safe_stem(value: Any) -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value)).strip("._")
    return text or "unnamed"


def _number(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return float("nan")
    return result if math.isfinite(result) else float("nan")


def _amplitude_label(frame: pd.DataFrame) -> str:
    if not frame.empty:
        for column, scale, suffix in (
            ("injection_charge_electrons", 1e-3, "ke"),
            ("injection_voltage_step_v", 1e3, "mV"),
        ):
            if column in frame:
                values = pd.to_numeric(frame[column], errors="coerce").dropna()
                if len(values):
                    value = float(values.iloc[0]) * scale
                    return f"{value:.4g} {suffix}"
        if "pulse_amplitude_native" in frame:
            raw = str(frame.iloc[0]["pulse_amplitude_native"])
            try:
                decoded = json.loads(raw)
                if isinstance(decoded, dict):
                    if "requested_voltage_step_v" in decoded:
                        return f"requested {1000 * float(decoded['requested_voltage_step_v']):.4g} mV"
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
            return raw if len(raw) <= 48 else raw[:45] + "..."
    return "unknown amplitude"


def _noise_prediction(fit: pd.Series, voltage: np.ndarray) -> np.ndarray | None:
    center = _number(fit.get("center_fit_v"))
    sigma = _number(fit.get("sigma_fit_v"))
    amplitude = _number(fit.get("fit_amplitude_count"))
    background = _number(fit.get("fit_background_count"))
    if not all(math.isfinite(value) for value in (center, sigma, amplitude, background)) or sigma <= 0:
        return None
    model = str(fit.get("fit_model", ""))
    if model.startswith("gaussian"):
        return background + amplitude * np.exp(-0.5 * ((voltage - center) / sigma) ** 2)
    if model == "edge_probit":
        direction = 1 if fit.get("fit_transition_direction") == "ascending" else -1
        return background + amplitude * np.array(
            [_NORMAL.cdf(direction * (value - center) / sigma) for value in voltage]
        )
    return None


def _scurve_prediction(fit: pd.Series, voltage: np.ndarray) -> np.ndarray | None:
    v50 = _number(fit.get("v50_v"))
    sigma = _number(fit.get("sigma_v"))
    if not math.isfinite(v50) or not math.isfinite(sigma) or sigma <= 0:
        return None
    sign = 1 if fit.get("transition_direction") == "ascending" else -1
    return np.array([_NORMAL.cdf(sign * (value - v50) / sigma) for value in voltage])


def generate_diagnostic_plots(
    *,
    analysis_directory: Path,
    noise_statistics: pd.DataFrame,
    noise_fits: pd.DataFrame,
    trim_characterization: pd.DataFrame,
    trim_characterization_summary: pd.DataFrame,
    scurve_efficiency: pd.DataFrame,
    scurve_results: pd.DataFrame,
    scurve_amplitude_summary: pd.DataFrame,
    scurve_gain_results: pd.DataFrame,
    crosstalk_pixel_metrics: pd.DataFrame,
    crosstalk_summary: pd.DataFrame,
    target_voltage: float | None,
    settings: AnalysisSettings,
) -> dict[str, list[Path]]:
    """Generate every plot exclusively from saved/processed experiment data."""

    plot_directory = analysis_directory / "plots"
    outputs: dict[str, list[Path]] = {}
    plt.rcParams.update(
        {
            "font.size": 10,
            "axes.labelsize": 10,
            "axes.titlesize": 11,
            "legend.fontsize": 9,
            "figure.dpi": 120,
            "savefig.facecolor": "white",
            "axes.grid": True,
            "grid.alpha": 0.25,
        }
    )

    if settings.plot_injection_patterns:
        selected_patterns = set(settings.plot_injection_patterns)

        def selected_pattern_rows(frame: pd.DataFrame) -> pd.DataFrame:
            if frame.empty or "injection_pattern" not in frame:
                return frame
            return frame[frame["injection_pattern"].isin(selected_patterns)].copy()

        scurve_efficiency = selected_pattern_rows(scurve_efficiency)
        scurve_results = selected_pattern_rows(scurve_results)
        scurve_amplitude_summary = selected_pattern_rows(scurve_amplitude_summary)
        scurve_gain_results = selected_pattern_rows(scurve_gain_results)
        crosstalk_pixel_metrics = selected_pattern_rows(crosstalk_pixel_metrics)
        crosstalk_summary = selected_pattern_rows(crosstalk_summary)

    stage_styles = {
        "trim_00": ("Trim = 0", "#1f77b4"),
        "trim_31": ("Trim = 31", "#d62728"),
        "equalized_final": ("Equalized", "#2ca02c"),
    }
    available_distributions: dict[str, np.ndarray] = {}
    for stage in stage_styles:
        values = _finite_values(
            _stage_frame(noise_fits, stage), "center_selected_v"
        )
        if len(values):
            available_distributions[stage] = values
    if available_distributions:
        combined = np.concatenate(list(available_distributions.values()))
        bins = np.histogram_bin_edges(combined, bins="auto")
        if len(bins) < 8:
            bins = np.linspace(float(combined.min()), float(combined.max()), 16)
        figure, axis = plt.subplots(figsize=(7.2, 4.8))
        for stage, values in available_distributions.items():
            label, color = stage_styles[stage]
            axis.hist(values, bins=bins, histtype="step", linewidth=1.8, label=label, color=color)
        if target_voltage is not None:
            axis.axvline(float(target_voltage), color="black", linestyle="--", label="Target")
        axis.set_xlabel("Effective threshold voltage, V")
        axis.set_ylabel("Pixel count")
        axis.set_title("Threshold distributions before and after equalization")
        axis.legend()
        outputs["threshold_distributions"] = _save_figure(
            figure, plot_directory, "threshold_distributions", settings
        )

        figure, axis = plt.subplots(figsize=(7.2, 4.8))
        for stage in ("trim_00", "trim_31", "equalized_final"):
            if stage not in available_distributions:
                continue
            values = available_distributions[stage]
            label, color = stage_styles[stage]
            axis.hist(
                values,
                bins=bins,
                alpha=0.28 if stage != "equalized_final" else 0.55,
                label=label,
                color=color,
            )
        if target_voltage is not None:
            axis.axvline(float(target_voltage), color="black", linestyle="--", linewidth=1.2)
        axis.set_xlabel("Effective threshold voltage, V")
        axis.set_ylabel("Pixel count")
        axis.set_title("Medipix-style threshold equalization")
        axis.legend()
        outputs["medipix_equalization"] = _save_figure(
            figure, plot_directory, "medipix_equalization", settings
        )

        figure, axis = plt.subplots(figsize=(7.2, 4.8))
        for stage in ("trim_00", "trim_31", "equalized_final"):
            if stage not in available_distributions:
                continue
            values = available_distributions[stage]
            label, color = stage_styles[stage]
            axis.hist(
                values,
                bins=bins,
                histtype="step",
                linewidth=1.7,
                label=label,
                color=color,
            )
        axis.set_yscale("log")
        axis.set_ylim(bottom=0.8)
        axis.set_xlabel("Effective threshold voltage, V")
        axis.set_ylabel("Pixel count, log scale")
        axis.set_title("Threshold equalization distributions, logarithmic view")
        axis.legend()
        outputs["threshold_distributions_log"] = _save_figure(
            figure, plot_directory, "threshold_distributions_log", settings
        )

        dispersion_rows = []
        for stage, values in available_distributions.items():
            median = float(np.median(values))
            dispersion_rows.append(
                {
                    "stage": stage,
                    "standard_deviation": float(np.std(values, ddof=1)) if len(values) > 1 else 0.0,
                    "mad": float(np.median(np.abs(values - median))),
                    "peak_to_peak": float(np.ptp(values)),
                }
            )
        dispersion = pd.DataFrame(dispersion_rows)
        if not dispersion.empty:
            figure, axis = plt.subplots(figsize=(7.4, 4.6))
            positions = np.arange(len(dispersion))
            width = 0.25
            for metric_index, (column, label) in enumerate(
                (("standard_deviation", "Std"), ("mad", "MAD"), ("peak_to_peak", "Peak-to-peak"))
            ):
                axis.bar(
                    positions + (metric_index - 1) * width,
                    1000 * dispersion[column],
                    width=width,
                    label=label,
                )
            axis.set_xticks(
                positions,
                [stage_styles[stage][0] for stage in dispersion["stage"]],
            )
            axis.set_ylabel("Threshold dispersion, mV")
            axis.set_title("Equalization improvement metrics")
            axis.legend()
            outputs["equalization_improvement"] = _save_figure(
                figure, plot_directory, "equalization_improvement", settings
            )

        figure, axis = plt.subplots(figsize=(7.2, 4.6))
        plotted_widths = False
        for stage, (label, color) in stage_styles.items():
            widths = 1000 * _finite_values(_stage_frame(noise_fits, stage), "sigma_fit_v")
            if not len(widths):
                continue
            axis.hist(
                widths,
                bins="auto",
                histtype="step",
                linewidth=1.6,
                color=color,
                label=label,
            )
            plotted_widths = True
        if plotted_widths:
            axis.set_xlabel("Fitted noise width, mV")
            axis.set_ylabel("Pixel count")
            axis.set_title("Noise-width distributions before and after equalization")
            axis.legend()
            outputs["noise_width_distributions"] = _save_figure(
                figure, plot_directory, "noise_width_distributions", settings
            )
        else:
            plt.close(figure)

        overview_order = ("trim_00", "equalized_final", "trim_31")
        if all(stage in available_distributions for stage in overview_order):
            overview_frames = {
                stage: _stage_frame(noise_fits, stage) for stage in overview_order
            }
            arrays = {
                stage: _matrix_array(frame, "center_selected_v")
                for stage, frame in overview_frames.items()
            }
            if all(value is not None for value in arrays.values()):
                figure = plt.figure(figsize=(12.8, 8.2), layout="constrained")
                grid = figure.add_gridspec(
                    2, 3, height_ratios=(0.85, 1.15), hspace=0.16
                )
                histogram_axis = figure.add_subplot(grid[0, :])
                for stage in overview_order:
                    values = available_distributions[stage]
                    label, color = stage_styles[stage]
                    histogram_axis.hist(
                        values,
                        bins=bins,
                        histtype="stepfilled",
                        alpha=0.18,
                        linewidth=1.5,
                        color=color,
                        label=label,
                    )
                    histogram_axis.hist(
                        values,
                        bins=bins,
                        histtype="step",
                        linewidth=1.5,
                        color=color,
                    )
                if target_voltage is not None:
                    histogram_axis.axvline(
                        float(target_voltage), color="black", linestyle="--", linewidth=1.2
                    )
                histogram_axis.set_xlabel("Effective threshold voltage, V")
                histogram_axis.set_ylabel("Pixel count")
                histogram_axis.set_title("Baseline distributions and spatial maps")
                histogram_axis.legend(ncol=3)

                common_values = np.concatenate(
                    [
                        arrays[stage][0][np.isfinite(arrays[stage][0])]
                        for stage in overview_order
                    ]
                )
                vmin = float(np.min(common_values))
                vmax = float(np.max(common_values))
                image = None
                for index, stage in enumerate(overview_order):
                    array, columns, rows = arrays[stage]
                    axis = figure.add_subplot(grid[1, index])
                    image = axis.imshow(
                        array,
                        origin="lower",
                        aspect="auto",
                        interpolation="nearest",
                        extent=(
                            columns[0] - 0.5,
                            columns[-1] + 0.5,
                            rows[0] - 0.5,
                            rows[-1] + 0.5,
                        ),
                        cmap="coolwarm",
                        vmin=vmin,
                        vmax=vmax,
                    )
                    axis.set_xlabel("Physical column")
                    axis.set_ylabel("Physical row" if index == 0 else "")
                    axis.set_title(stage_styles[stage][0])
                if image is not None:
                    colorbar = figure.colorbar(
                        image,
                        ax=figure.axes[1:],
                        pad=0.02,
                        fraction=0.025,
                    )
                    colorbar.set_label("Effective threshold, V")
                outputs["noise_equalization_overview"] = _save_figure(
                    figure,
                    plot_directory,
                    "noise_equalization_overview",
                    settings,
                )

        for stage, (label, _) in stage_styles.items():
            stage_data = _stage_frame(noise_fits, stage)
            if stage_data.empty:
                continue
            center_map = _heatmap(
                stage_data,
                value_column="center_selected_v",
                title=f"{label}: effective-threshold map",
                colorbar_label="Effective threshold, V",
                cmap="viridis",
            )
            if center_map is not None:
                outputs[f"threshold_map_{stage}"] = _save_figure(
                    center_map,
                    plot_directory,
                    f"effective_threshold_map_{stage}",
                    settings,
                )
            centers = pd.to_numeric(
                stage_data["center_selected_v"], errors="coerce"
            )
            median_center = float(centers.median())
            offset_data = stage_data.copy()
            offset_data["spatial_offset_v"] = centers - median_center
            offset_map = _heatmap(
                offset_data,
                value_column="spatial_offset_v",
                title=f"{label}: spatial offset from stage median",
                colorbar_label="Offset, V",
                cmap="coolwarm",
                center_zero=True,
            )
            if offset_map is not None:
                outputs[f"spatial_offset_map_{stage}"] = _save_figure(
                    offset_map,
                    plot_directory,
                    f"spatial_offset_map_{stage}",
                    settings,
                )

    matrix_curve_stages = [
        stage for stage in ("trim_00", "trim_31", "equalized_final")
        if not _stage_frame(noise_statistics, stage).empty
    ]
    if matrix_curve_stages:
        figure, axis = plt.subplots(figsize=(7.2, 4.8))
        for stage in matrix_curve_stages:
            data = _stage_frame(noise_statistics, stage)
            envelope = (
                data.groupby("threshold_voltage_v")["mean_count"]
                .agg(
                    median="median",
                    q10=lambda values: values.quantile(0.10),
                    q90=lambda values: values.quantile(0.90),
                )
                .reset_index()
                .sort_values("threshold_voltage_v")
            )
            label, color = stage_styles[stage]
            x = envelope["threshold_voltage_v"].to_numpy(dtype=float)
            axis.plot(x, envelope["median"], color=color, linewidth=1.5, label=label)
            axis.fill_between(
                x,
                envelope["q10"].to_numpy(dtype=float),
                envelope["q90"].to_numpy(dtype=float),
                color=color,
                alpha=0.12,
            )
        axis.set_xlabel("Threshold voltage, V")
        axis.set_ylabel("Decoded counter value")
        axis.set_title("Matrix noise response: median and 10-90% pixel band")
        axis.legend()
        outputs["noise_matrix_curves"] = _save_figure(
            figure, plot_directory, "noise_matrix_curves", settings
        )

    if not trim_characterization_summary.empty:
        summary = trim_characterization_summary.sort_values("trim_code")
        trim_codes = pd.to_numeric(summary["trim_code"], errors="coerce").to_numpy(dtype=float)
        figure, axes = plt.subplots(1, 3, figsize=(13.5, 4.2), sharex=True)
        axes[0].plot(trim_codes, summary["center_median_v"], marker="o", markersize=3)
        axes[0].fill_between(
            trim_codes,
            pd.to_numeric(summary["center_q10_v"], errors="coerce").to_numpy(dtype=float),
            pd.to_numeric(summary["center_q90_v"], errors="coerce").to_numpy(dtype=float),
            alpha=0.22,
            label="10-90% pixels",
        )
        axes[0].set_ylabel("Effective threshold, V")
        axes[0].legend()
        axes[1].plot(
            trim_codes,
            1000 * pd.to_numeric(summary["noise_sigma_median_v"], errors="coerce"),
            marker="o",
            markersize=3,
            color="#d95f02",
        )
        axes[1].fill_between(
            trim_codes,
            1000 * pd.to_numeric(summary["noise_sigma_q10_v"], errors="coerce").to_numpy(dtype=float),
            1000 * pd.to_numeric(summary["noise_sigma_q90_v"], errors="coerce").to_numpy(dtype=float),
            alpha=0.22,
            color="#d95f02",
        )
        axes[1].set_ylabel("Noise width, mV")
        axes[2].plot(
            trim_codes,
            pd.to_numeric(summary["successful_fit_fraction"], errors="coerce"),
            marker="o",
            markersize=3,
            color="#1b9e77",
        )
        axes[2].set_ylim(-0.02, 1.02)
        axes[2].set_ylabel("Successful-fit fraction")
        for axis in axes:
            axis.set_xlabel("Uniform trim code")
            axis.set_xlim(-0.5, 31.5)
        figure.suptitle("Uniform-trim response of the owned matrix")
        outputs["uniform_trim_response_summary"] = _save_figure(
            figure, plot_directory, "uniform_trim_response_summary", settings
        )

    if not trim_characterization.empty and trim_characterization["trim_code"].nunique() > 2:
        trim_data = trim_characterization.copy()
        trim_data["trim_code"] = pd.to_numeric(trim_data["trim_code"], errors="coerce")
        trim_data["center_selected_v"] = pd.to_numeric(
            trim_data["center_selected_v"], errors="coerce"
        )
        trim_data = trim_data.dropna(subset=["trim_code", "center_selected_v"])
        if not trim_data.empty:
            centers = trim_data["center_selected_v"].to_numpy(dtype=float)
            bins = np.histogram_bin_edges(centers, bins="auto")
            if len(bins) < 12:
                bins = np.linspace(float(np.min(centers)), float(np.max(centers)), 24)
            figure, axis = plt.subplots(figsize=(8.0, 5.2))
            color_map = plt.get_cmap("viridis")
            for trim_code, group in trim_data.groupby("trim_code", sort=True):
                axis.hist(
                    group["center_selected_v"],
                    bins=bins,
                    histtype="step",
                    linewidth=0.8,
                    color=color_map(float(trim_code) / 31.0),
                    alpha=0.9,
                )
            scalar = plt.cm.ScalarMappable(cmap=color_map, norm=plt.Normalize(0, 31))
            colorbar = figure.colorbar(scalar, ax=axis, pad=0.02)
            colorbar.set_label("Uniform trim code")
            axis.set_xlabel("Effective threshold voltage, V")
            axis.set_ylabel("Pixel count")
            axis.set_title("Threshold distributions for every uniform trim code")
            outputs["uniform_trim_distributions"] = _save_figure(
                figure, plot_directory, "uniform_trim_distributions", settings
            )

            coordinates = _plot_coordinates(
                settings, trim_data, "center_selected_v"
            )
            for column, row in coordinates:
                pixel = trim_data[
                    (trim_data["column"] == column) & (trim_data["row"] == row)
                ].sort_values("trim_code")
                if pixel.empty:
                    continue
                figure, axes = plt.subplots(1, 2, figsize=(10.4, 4.2))
                axes[0].plot(
                    pixel["trim_code"], pixel["center_selected_v"], marker="o", markersize=3
                )
                axes[0].set_ylabel("Effective threshold, V")
                axes[1].plot(
                    pixel["trim_code"],
                    1000 * pd.to_numeric(pixel["sigma_fit_v"], errors="coerce"),
                    marker="o",
                    markersize=3,
                    color="#d95f02",
                )
                axes[1].set_ylabel("Noise width, mV")
                for axis in axes:
                    axis.set_xlabel("Uniform trim code")
                    axis.set_xlim(-0.5, 31.5)
                figure.suptitle(f"Pixel C{column:02d} R{row:02d}: trim transfer")
                stem = f"pixel_C{column:02d}_R{row:02d}_trim_transfer"
                outputs[stem] = _save_figure(figure, plot_directory, stem, settings)

            if settings.plot_all_trim_heatmaps:
                for trim_code, group in trim_data.groupby("trim_code", sort=True):
                    figure = _heatmap(
                        group,
                        value_column="center_selected_v",
                        title=f"Uniform trim {int(trim_code):02d}: effective threshold",
                        colorbar_label="Effective threshold, V",
                        cmap="viridis",
                    )
                    if figure is not None:
                        stem = f"uniform_trim_{int(trim_code):02d}_threshold_map"
                        outputs[stem] = _save_figure(
                            figure, plot_directory, stem, settings
                        )

        uniform_stages = noise_statistics[
            noise_statistics["stage"].astype(str).isin(("trim_00", "trim_31"))
            | noise_statistics["stage"].astype(str).str.startswith("trim_full_")
        ].copy()
        if not uniform_stages.empty:
            figure, axis = plt.subplots(figsize=(8.0, 5.2))
            color_map = plt.get_cmap("viridis")
            plotted_matrix_trim = False
            for trim_code, group in uniform_stages.groupby("local_trim_code", sort=True):
                curve = (
                    group.groupby("threshold_voltage_v")["mean_count"]
                    .median()
                    .sort_index()
                )
                if curve.empty:
                    continue
                axis.plot(
                    curve.index,
                    curve.values,
                    linewidth=0.85,
                    color=color_map(float(trim_code) / 31.0),
                )
                plotted_matrix_trim = True
            if plotted_matrix_trim:
                scalar = plt.cm.ScalarMappable(cmap=color_map, norm=plt.Normalize(0, 31))
                colorbar = figure.colorbar(scalar, ax=axis, pad=0.02)
                colorbar.set_label("Uniform trim code")
                axis.set_xlabel("Threshold voltage, V")
                axis.set_ylabel("Matrix-median counter value")
                axis.set_title("Matrix-median noise curves for every trim code")
                outputs["matrix_all_trim_noise_curves"] = _save_figure(
                    figure, plot_directory, "matrix_all_trim_noise_curves", settings
                )
            else:
                plt.close(figure)

            coordinates = _plot_coordinates(
                settings, trim_characterization, "center_selected_v"
            )
            for column, row in coordinates:
                pixel_curves = uniform_stages[
                    (uniform_stages["column"] == column)
                    & (uniform_stages["row"] == row)
                ].copy()
                if pixel_curves.empty:
                    continue
                figure, axis = plt.subplots(figsize=(8.0, 5.2))
                color_map = plt.get_cmap("viridis")
                for trim_code, curve in pixel_curves.groupby("local_trim_code", sort=True):
                    curve = curve.sort_values("threshold_voltage_v")
                    axis.plot(
                        curve["threshold_voltage_v"],
                        curve["mean_count"],
                        linewidth=0.85,
                        color=color_map(float(trim_code) / 31.0),
                    )
                scalar = plt.cm.ScalarMappable(cmap=color_map, norm=plt.Normalize(0, 31))
                colorbar = figure.colorbar(scalar, ax=axis, pad=0.02)
                colorbar.set_label("Uniform trim code")
                axis.set_xlabel("Threshold voltage, V")
                axis.set_ylabel("Decoded counter value")
                axis.set_title(f"Pixel C{column:02d} R{row:02d}: noise curves for all trims")
                stem = f"pixel_C{column:02d}_R{row:02d}_all_trim_noise_curves"
                outputs[stem] = _save_figure(figure, plot_directory, stem, settings)

    final = _stage_frame(noise_fits, "equalized_final")
    if not final.empty:
        trims = _finite_values(final, "local_trim_code")
        if len(trims):
            figure, axis = plt.subplots(figsize=(7.2, 4.5))
            axis.hist(trims, bins=np.arange(-0.5, 32.5, 1), color="#4c78a8", edgecolor="white")
            axis.set_xlim(-0.5, 31.5)
            axis.set_xlabel("Selected local trim code")
            axis.set_ylabel("Pixel count")
            axis.set_title("Equalization trim-code distribution")
            outputs["trim_histogram"] = _save_figure(
                figure, plot_directory, "trim_code_histogram", settings
            )

        heatmap_specs = (
            ("local_trim_code", "Selected trim map", "Trim code", "viridis", False, "trim_map"),
            (
                "center_selected_v",
                "Equalized effective-threshold map",
                "Effective threshold, V",
                "viridis",
                False,
                "effective_threshold_map",
            ),
            (
                "sigma_fit_v",
                "Threshold-domain noise map",
                "Fitted width, V",
                "magma",
                False,
                "noise_map",
            ),
        )
        if target_voltage is not None:
            final["residual_v"] = final["center_selected_v"] - float(target_voltage)
            heatmap_specs += (
                (
                    "residual_v",
                    "Equalized threshold residual map",
                    "Residual, V",
                    "coolwarm",
                    True,
                    "residual_map",
                ),
            )
        for value, title, label, cmap, centered, stem in heatmap_specs:
            figure = _heatmap(
                final,
                value_column=value,
                title=title,
                colorbar_label=label,
                cmap=cmap,
                center_zero=centered,
            )
            if figure is not None:
                outputs[stem] = _save_figure(figure, plot_directory, stem, settings)

    representative_stage = "equalized_final" if not final.empty else "trim_00"
    representative_fits = _stage_frame(noise_fits, representative_stage)
    representatives = _plot_coordinates(
        settings, representative_fits, "center_selected_v"
    )
    if representatives and not noise_statistics.empty:
        figure, axis = plt.subplots(figsize=(7.4, 5.0))
        for column, row in representatives:
            curve = noise_statistics[
                (noise_statistics["stage"] == representative_stage)
                & (noise_statistics["column"] == column)
                & (noise_statistics["row"] == row)
            ].sort_values("threshold_voltage_v")
            if curve.empty:
                continue
            axis.errorbar(
                curve["threshold_voltage_v"],
                curve["mean_count"],
                yerr=curve["sem_count"],
                marker=".",
                markersize=3,
                linewidth=0.9,
                capsize=1.5,
                label=f"C{column:02d} R{row:02d}",
            )
        axis.set_xlabel("Threshold voltage, V")
        axis.set_ylabel("Decoded counter value")
        axis.set_title(f"Representative raw noise curves: {representative_stage}")
        axis.legend(ncol=2)
        outputs["representative_noise_curves"] = _save_figure(
            figure, plot_directory, "representative_noise_curves", settings
        )

        for column, row in representatives:
            figure, axis = plt.subplots(figsize=(7.6, 5.0))
            plotted = False
            for stage in ("trim_00", "trim_31", "equalized_final"):
                curve = noise_statistics[
                    (noise_statistics["stage"] == stage)
                    & (noise_statistics["column"] == column)
                    & (noise_statistics["row"] == row)
                ].sort_values("threshold_voltage_v")
                if curve.empty:
                    continue
                label, color = stage_styles[stage]
                axis.errorbar(
                    curve["threshold_voltage_v"],
                    curve["mean_count"],
                    yerr=curve.get("sem_count"),
                    marker=".",
                    markersize=3,
                    linewidth=0,
                    elinewidth=0.7,
                    capsize=1.2,
                    color=color,
                    alpha=0.75,
                    label=f"{label}: data",
                )
                fit = noise_fits[
                    (noise_fits["stage"] == stage)
                    & (noise_fits["column"] == column)
                    & (noise_fits["row"] == row)
                ]
                if not fit.empty:
                    voltage_grid = np.linspace(
                        float(curve["threshold_voltage_v"].min()),
                        float(curve["threshold_voltage_v"].max()),
                        400,
                    )
                    predicted = _noise_prediction(fit.iloc[0], voltage_grid)
                    if predicted is not None:
                        axis.plot(
                            voltage_grid,
                            predicted,
                            color=color,
                            linewidth=1.4,
                            label=f"{label}: fit",
                        )
                plotted = True
            if plotted:
                axis.set_xlabel("Threshold voltage, V")
                axis.set_ylabel("Decoded counter value")
                axis.set_title(f"Pixel C{column:02d} R{row:02d}: noise before/after equalization")
                axis.legend(ncol=2)
                stem = f"pixel_C{column:02d}_R{row:02d}_noise_before_after"
                outputs[stem] = _save_figure(figure, plot_directory, stem, settings)
            else:
                plt.close(figure)

    if not noise_fits.empty:
        r2 = _finite_values(noise_fits, "fit_r2")
        if len(r2):
            figure, axis = plt.subplots(figsize=(7.2, 4.5))
            axis.hist(r2, bins=30, color="#7a5195", edgecolor="white")
            axis.axvline(settings.gaussian_min_r2, color="black", linestyle="--")
            axis.set_xlabel("Noise-curve fit $R^2$")
            axis.set_ylabel("Fit count")
            axis.set_title("Noise fit-quality distribution")
            outputs["noise_fit_quality"] = _save_figure(
                figure, plot_directory, "noise_fit_quality", settings
            )

    background_stage = "equalized_final" if "equalized_final" in set(noise_statistics.get("stage", [])) else "trim_00"
    background = _stage_frame(noise_statistics, background_stage)
    if not background.empty:
        envelope = background.groupby("threshold_voltage_v")["mean_count"].agg(
            median="median",
            q10=lambda values: values.quantile(0.10),
            q90=lambda values: values.quantile(0.90),
        ).reset_index().sort_values("threshold_voltage_v")
        figure, axis = plt.subplots(figsize=(7.2, 4.5))
        x = envelope["threshold_voltage_v"].to_numpy(dtype=float)
        axis.plot(x, envelope["median"], color="#1f77b4", label="Pixel median")
        axis.fill_between(
            x,
            envelope["q10"].to_numpy(dtype=float),
            envelope["q90"].to_numpy(dtype=float),
            alpha=0.25,
            color="#1f77b4",
            label="10-90% pixels",
        )
        axis.set_xlabel("Threshold voltage, V")
        axis.set_ylabel("Background count")
        axis.set_title("Background occupancy versus threshold")
        axis.legend()
        outputs["background_occupancy"] = _save_figure(
            figure, plot_directory, "background_occupancy", settings
        )

    if not scurve_efficiency.empty:
        valid = scurve_efficiency[
            scurve_efficiency["fit_valid"].astype(str).str.lower().isin(("true", "1"))
        ].copy()
        if not valid.empty:
            amplitude_order = (
                valid.groupby("pulse_amplitude_native", dropna=False, as_index=False)
                .agg(
                    requested_step=("requested_injection_voltage_step_v", "median"),
                    actual_step=("injection_voltage_step_v", "median"),
                    charge=("injection_charge_electrons", "median"),
                )
            )
            amplitude_order["sort_value"] = pd.to_numeric(
                amplitude_order["actual_step"], errors="coerce"
            )
            missing_sort = amplitude_order["sort_value"].isna()
            amplitude_order.loc[missing_sort, "sort_value"] = np.arange(
                int(missing_sort.sum()), dtype=float
            ) + 1e6
            amplitudes = amplitude_order.sort_values("sort_value")[
                "pulse_amplitude_native"
            ].tolist()
            patterns = list(valid["injection_pattern"].drop_duplicates())
            patterns = sorted(patterns, key=lambda value: (value != "all", str(value)))
            color_map = plt.get_cmap("viridis")

            for pattern in patterns:
                pattern_data = valid[valid["injection_pattern"] == pattern]
                pattern_results = (
                    scurve_results[scurve_results["injection_pattern"] == pattern]
                    if not scurve_results.empty
                    else pd.DataFrame()
                )
                coordinates = _plot_coordinates(
                    settings,
                    pattern_results if not pattern_results.empty else pattern_data,
                    "v50_v" if not pattern_results.empty else "threshold_voltage_v",
                )
                for column, row in coordinates:
                    figure, axis = plt.subplots(figsize=(7.8, 5.2))
                    plotted = False
                    for amplitude_index, amplitude in enumerate(amplitudes):
                        points_frame = pattern_data[
                            (pattern_data["pulse_amplitude_native"] == amplitude)
                            & (pattern_data["column"] == column)
                            & (pattern_data["row"] == row)
                        ]
                        if points_frame.empty:
                            continue
                        points = (
                            points_frame.groupby("threshold_voltage_v")["efficiency"]
                            .mean()
                            .sort_index()
                        )
                        color = color_map(
                            amplitude_index / max(len(amplitudes) - 1, 1)
                        )
                        label = _amplitude_label(points_frame)
                        axis.plot(
                            points.index,
                            points.values,
                            marker="o",
                            markersize=2.8,
                            linewidth=0,
                            color=color,
                            label=label,
                        )
                        fit = pattern_results[
                            (pattern_results["pulse_amplitude_native"] == amplitude)
                            & (pattern_results["column"] == column)
                            & (pattern_results["row"] == row)
                        ]
                        if not fit.empty and fit.iloc[0]["fit_status"] in (
                            "ok",
                            "poor_quality",
                        ):
                            voltage_grid = np.linspace(
                                float(points.index.min()), float(points.index.max()), 350
                            )
                            predicted = _scurve_prediction(fit.iloc[0], voltage_grid)
                            if predicted is not None:
                                axis.plot(
                                    voltage_grid,
                                    predicted,
                                    color=color,
                                    linewidth=1.2,
                                )
                        plotted = True
                    if plotted:
                        axis.set_ylim(-0.05, 1.05)
                        axis.set_xlabel("Threshold voltage, V")
                        axis.set_ylabel("Detection efficiency")
                        axis.set_title(
                            f"Pixel C{column:02d} R{row:02d}: S-curves, {pattern}"
                        )
                        axis.legend(ncol=2, title="Injected amplitude")
                        stem = (
                            f"pixel_C{column:02d}_R{row:02d}_"
                            f"scurves_{_safe_stem(pattern)}"
                        )
                        outputs[stem] = _save_figure(
                            figure, plot_directory, stem, settings
                        )
                    else:
                        plt.close(figure)

                figure, axes = plt.subplots(1, 2, figsize=(12.2, 4.7))
                plotted_matrix = False
                for amplitude_index, amplitude in enumerate(amplitudes):
                    data = pattern_data[
                        pattern_data["pulse_amplitude_native"] == amplitude
                    ]
                    if data.empty:
                        continue
                    envelope = (
                        data.groupby("threshold_voltage_v")["efficiency"]
                        .agg(
                            median="median",
                            q10=lambda values: values.quantile(0.10),
                            q90=lambda values: values.quantile(0.90),
                        )
                        .reset_index()
                        .sort_values("threshold_voltage_v")
                    )
                    x = envelope["threshold_voltage_v"].to_numpy(dtype=float)
                    y = envelope["median"].to_numpy(dtype=float)
                    color = color_map(
                        amplitude_index / max(len(amplitudes) - 1, 1)
                    )
                    label = _amplitude_label(data)
                    axes[0].plot(x, y, color=color, linewidth=1.25, label=label)
                    axes[0].fill_between(
                        x,
                        envelope["q10"].to_numpy(dtype=float),
                        envelope["q90"].to_numpy(dtype=float),
                        color=color,
                        alpha=0.08,
                    )
                    if len(x) >= 3:
                        order = np.argsort(x)
                        derivative = np.abs(np.gradient(y[order], x[order]))
                        axes[1].plot(
                            x[order], derivative, color=color, linewidth=1.25, label=label
                        )
                    plotted_matrix = True
                if plotted_matrix:
                    axes[0].set_ylim(-0.05, 1.05)
                    axes[0].set_xlabel("Threshold voltage, V")
                    axes[0].set_ylabel("Median efficiency")
                    axes[0].set_title("S-curves, median and 10-90% pixel band")
                    axes[1].set_xlabel("Threshold voltage, V")
                    axes[1].set_ylabel("|d efficiency / dV|")
                    axes[1].set_title("Derivative of the matrix-median S-curve")
                    axes[0].legend(ncol=2, title="Injected amplitude")
                    figure.suptitle(f"Owned-matrix response, pattern {pattern}")
                    stem = f"matrix_scurves_{_safe_stem(pattern)}"
                    outputs[stem] = _save_figure(
                        figure, plot_directory, stem, settings
                    )
                else:
                    plt.close(figure)

            if not scurve_results.empty:
                for pattern in patterns:
                    for amplitude_index, amplitude in enumerate(amplitudes):
                        data = scurve_results[
                            (scurve_results["injection_pattern"] == pattern)
                            & (scurve_results["pulse_amplitude_native"] == amplitude)
                            & scurve_results["fit_status"].isin(("ok", "poor_quality"))
                        ].copy()
                        if data.empty:
                            continue
                        v50_map = _matrix_array(data, "v50_v")
                        sigma_map = _matrix_array(data, "sigma_v")
                        if v50_map is None or sigma_map is None:
                            continue
                        figure, axes = plt.subplots(
                            2, 2, figsize=(10.7, 8.4), layout="constrained"
                        )
                        v50_array, columns, rows = v50_map
                        sigma_array, sigma_columns, sigma_rows = sigma_map
                        image_v50 = axes[0, 0].imshow(
                            v50_array,
                            origin="lower",
                            aspect="auto",
                            interpolation="nearest",
                            extent=(columns[0] - 0.5, columns[-1] + 0.5, rows[0] - 0.5, rows[-1] + 0.5),
                            cmap="viridis",
                        )
                        figure.colorbar(image_v50, ax=axes[0, 0], pad=0.02, label="V50, V")
                        image_sigma = axes[0, 1].imshow(
                            1000 * sigma_array,
                            origin="lower",
                            aspect="auto",
                            interpolation="nearest",
                            extent=(sigma_columns[0] - 0.5, sigma_columns[-1] + 0.5, sigma_rows[0] - 0.5, sigma_rows[-1] + 0.5),
                            cmap="magma",
                        )
                        figure.colorbar(image_sigma, ax=axes[0, 1], pad=0.02, label="Sigma, mV")
                        axes[1, 0].hist(
                            pd.to_numeric(data["v50_v"], errors="coerce").dropna(),
                            bins="auto",
                            color="#4c78a8",
                            edgecolor="white",
                        )
                        axes[1, 0].set_xlabel("V50, V")
                        axes[1, 0].set_ylabel("Pixel count")
                        axes[1, 1].hist(
                            1000 * pd.to_numeric(data["sigma_v"], errors="coerce").dropna(),
                            bins="auto",
                            color="#e45756",
                            edgecolor="white",
                        )
                        axes[1, 1].set_xlabel("S-curve width, mV")
                        axes[1, 1].set_ylabel("Pixel count")
                        for axis in axes[0, :]:
                            axis.set_xlabel("Physical column")
                            axis.set_ylabel("Physical row")
                        label = _amplitude_label(data)
                        figure.suptitle(f"S-curve parameters: {label}, pattern {pattern}")
                        stem = (
                            f"scurve_parameters_{_safe_stem(pattern)}_"
                            f"amplitude_{amplitude_index:03d}"
                        )
                        outputs[stem] = _save_figure(
                            figure, plot_directory, stem, settings
                        )

    if not scurve_amplitude_summary.empty:
        summary = scurve_amplitude_summary.copy()
        actual_step = _numeric_series(summary, "injection_voltage_step_v")
        requested_step = _numeric_series(
            summary, "requested_injection_voltage_step_v"
        )
        if actual_step.notna().any():
            figure, axes = plt.subplots(1, 2, figsize=(10.7, 4.2))
            pair_table = (
                summary.assign(actual_step=actual_step, requested_step=requested_step)
                .dropna(subset=["actual_step"])
                .drop_duplicates("pulse_amplitude_native")
                .sort_values("actual_step")
            )
            if not pair_table.empty:
                x = 1000 * pair_table["requested_step"].fillna(pair_table["actual_step"])
                y = 1000 * pair_table["actual_step"]
                axes[0].plot(x, y, marker="o", linewidth=1.0)
                low = float(min(x.min(), y.min()))
                high = float(max(x.max(), y.max()))
                axes[0].plot([low, high], [low, high], color="black", linestyle="--", linewidth=1)
                axes[0].set_xlabel("Requested REF1-REF2 step, mV")
                axes[0].set_ylabel("Selected measured step, mV")
                axes[1].bar(
                    np.arange(len(pair_table)),
                    1000 * (pair_table["actual_step"] - pair_table["requested_step"]),
                    color="#4c78a8",
                )
                axes[1].axhline(0, color="black", linewidth=0.8)
                axes[1].set_xlabel("Amplitude index")
                axes[1].set_ylabel("Selection error, mV")
                figure.suptitle("REF LUT pair-selection accuracy")
                outputs["reference_pair_selection"] = _save_figure(
                    figure, plot_directory, "reference_pair_selection", settings
                )
            else:
                plt.close(figure)

        for pattern, data in summary.groupby("injection_pattern", sort=True):
            charge = _numeric_series(data, "injection_charge_electrons")
            step = _numeric_series(data, "injection_voltage_step_v")
            if charge.notna().all() and charge.nunique() > 1:
                x = charge.to_numpy(dtype=float) / 1000.0
                x_label = "Nominal injected charge, ke"
            elif step.notna().all() and step.nunique() > 1:
                x = 1000 * step.to_numpy(dtype=float)
                x_label = "Measured REF1-REF2 step, mV"
            else:
                continue
            order = np.argsort(x)
            x = x[order]
            data = data.iloc[order]
            figure, axes = plt.subplots(1, 2, figsize=(10.9, 4.3))
            axes[0].plot(x, data["v50_median_v"], marker="o")
            axes[0].fill_between(
                x,
                pd.to_numeric(data["v50_q10_v"], errors="coerce").to_numpy(dtype=float),
                pd.to_numeric(data["v50_q90_v"], errors="coerce").to_numpy(dtype=float),
                alpha=0.22,
                label="10-90% pixels",
            )
            axes[0].set_ylabel("V50, V")
            axes[0].legend()
            axes[1].plot(
                x,
                1000 * pd.to_numeric(data["sigma_median_v"], errors="coerce"),
                marker="o",
                color="#d95f02",
            )
            axes[1].fill_between(
                x,
                1000 * pd.to_numeric(data["sigma_q10_v"], errors="coerce").to_numpy(dtype=float),
                1000 * pd.to_numeric(data["sigma_q90_v"], errors="coerce").to_numpy(dtype=float),
                alpha=0.22,
                color="#d95f02",
            )
            axes[1].set_ylabel("S-curve width, mV")
            for axis in axes:
                axis.set_xlabel(x_label)
            figure.suptitle(f"Matrix amplitude response, pattern {pattern}")
            stem = f"matrix_amplitude_response_{_safe_stem(pattern)}"
            outputs[stem] = _save_figure(figure, plot_directory, stem, settings)

    if not scurve_gain_results.empty:
        for pattern, data in scurve_gain_results.groupby("injection_pattern", sort=True):
            gain = pd.to_numeric(data["nominal_gain_mv_per_ke"], errors="coerce")
            usable = data[gain.notna()].copy()
            if usable.empty:
                continue
            gain_map = _matrix_array(usable, "nominal_gain_mv_per_ke")
            if gain_map is None:
                continue
            array, columns, rows = gain_map
            figure, axes = plt.subplots(1, 2, figsize=(10.6, 4.5))
            image = axes[0].imshow(
                array,
                origin="lower",
                aspect="auto",
                interpolation="nearest",
                extent=(columns[0] - 0.5, columns[-1] + 0.5, rows[0] - 0.5, rows[-1] + 0.5),
                cmap="viridis",
            )
            figure.colorbar(image, ax=axes[0], pad=0.02, label="Nominal gain, mV/ke")
            axes[0].set_xlabel("Physical column")
            axes[0].set_ylabel("Physical row")
            axes[1].hist(
                pd.to_numeric(usable["nominal_gain_mv_per_ke"], errors="coerce").dropna(),
                bins="auto",
                color="#59a14f",
                edgecolor="white",
            )
            axes[1].set_xlabel("Nominal gain, mV/ke")
            axes[1].set_ylabel("Pixel count")
            figure.suptitle(
                f"Per-pixel nominal charge response, pattern {pattern}"
            )
            stem = f"nominal_gain_{_safe_stem(pattern)}"
            outputs[stem] = _save_figure(figure, plot_directory, stem, settings)

    if not scurve_results.empty:
        usable_results = scurve_results[
            scurve_results["fit_status"].isin(("ok", "poor_quality"))
        ].copy()
        if not usable_results.empty:
            coordinates = _plot_coordinates(settings, usable_results, "v50_v")
            for pattern, pattern_data in usable_results.groupby(
                "injection_pattern", sort=True
            ):
                for column, row in coordinates:
                    pixel = pattern_data[
                        (pattern_data["column"] == column)
                        & (pattern_data["row"] == row)
                    ].copy()
                    if len(pixel) < 2:
                        continue
                    charge = _numeric_series(pixel, "injection_charge_electrons")
                    step = _numeric_series(pixel, "injection_voltage_step_v")
                    if charge.notna().all() and charge.nunique() > 1:
                        x = charge.to_numpy(dtype=float) / 1000.0
                        x_label = "Nominal injected charge, ke"
                    elif step.notna().all() and step.nunique() > 1:
                        x = 1000 * step.to_numpy(dtype=float)
                        x_label = "Measured REF1-REF2 step, mV"
                    else:
                        continue
                    order = np.argsort(x)
                    figure, axis = plt.subplots(figsize=(6.5, 4.5))
                    axis.errorbar(
                        x[order],
                        pd.to_numeric(pixel["v50_v"], errors="coerce").to_numpy(dtype=float)[order],
                        yerr=_numeric_series(
                            pixel, "v50_uncertainty_v"
                        ).to_numpy(dtype=float)[order],
                        marker="o",
                        capsize=2,
                    )
                    axis.set_xlabel(x_label)
                    axis.set_ylabel("V50, V")
                    axis.set_title(
                        f"Pixel C{column:02d} R{row:02d}: amplitude response, {pattern}"
                    )
                    stem = (
                        f"pixel_C{column:02d}_R{row:02d}_"
                        f"amplitude_response_{_safe_stem(pattern)}"
                    )
                    outputs[stem] = _save_figure(
                        figure, plot_directory, stem, settings
                    )

        r2 = _finite_values(scurve_results, "fit_r2")
        if len(r2):
            figure, axis = plt.subplots(figsize=(7.2, 4.5))
            axis.hist(r2, bins=30, color="#ef5675", edgecolor="white")
            axis.axvline(0.8, color="black", linestyle="--")
            axis.set_xlabel("S-curve fit $R^2$")
            axis.set_ylabel("Fit count")
            axis.set_title("S-curve fit-quality distribution")
            outputs["scurve_fit_quality"] = _save_figure(
                figure, plot_directory, "scurve_fit_quality", settings
            )

    if not crosstalk_pixel_metrics.empty:
        pixel_metrics = crosstalk_pixel_metrics.copy()
        pixel_metrics["delta_v50_mv"] = 1000 * pd.to_numeric(
            pixel_metrics["delta_v50_v"], errors="coerce"
        )
        pixel_metrics["sigma_fractional_change"] = pd.to_numeric(
            pixel_metrics["sigma_ratio_to_reference"], errors="coerce"
        ) - 1.0
        metric_groups = pixel_metrics.groupby(
            ["pulse_amplitude_native", "injection_pattern"],
            dropna=False,
            sort=True,
        )
        for metric_index, ((amplitude, pattern), data) in enumerate(metric_groups):
            reference_patterns = set(data["reference_injection_pattern"].dropna().astype(str))
            if str(pattern) in reference_patterns:
                continue
            delta_map = _matrix_array(data, "delta_v50_mv")
            sigma_map = _matrix_array(data, "sigma_fractional_change")
            if delta_map is None or sigma_map is None:
                continue
            delta_array, columns, rows = delta_map
            sigma_array, sigma_columns, sigma_rows = sigma_map
            figure, axes = plt.subplots(
                2, 2, figsize=(10.8, 8.4), layout="constrained"
            )
            delta_limit = (
                float(np.nanmax(np.abs(delta_array)))
                if np.any(np.isfinite(delta_array))
                else 1.0
            )
            sigma_limit = (
                float(np.nanmax(np.abs(sigma_array)))
                if np.any(np.isfinite(sigma_array))
                else 0.1
            )
            if delta_limit == 0:
                delta_limit = 1e-6
            if sigma_limit == 0:
                sigma_limit = 1e-6
            image_delta = axes[0, 0].imshow(
                delta_array,
                origin="lower",
                aspect="auto",
                interpolation="nearest",
                extent=(columns[0] - 0.5, columns[-1] + 0.5, rows[0] - 0.5, rows[-1] + 0.5),
                cmap="coolwarm",
                vmin=-delta_limit,
                vmax=delta_limit,
            )
            figure.colorbar(image_delta, ax=axes[0, 0], pad=0.02, label="Delta V50, mV")
            image_sigma = axes[0, 1].imshow(
                sigma_array,
                origin="lower",
                aspect="auto",
                interpolation="nearest",
                extent=(sigma_columns[0] - 0.5, sigma_columns[-1] + 0.5, sigma_rows[0] - 0.5, sigma_rows[-1] + 0.5),
                cmap="coolwarm",
                vmin=-sigma_limit,
                vmax=sigma_limit,
            )
            figure.colorbar(
                image_sigma,
                ax=axes[0, 1],
                pad=0.02,
                label="Sigma / reference sigma - 1",
            )
            axes[1, 0].hist(
                _finite_values(data, "delta_v50_mv"),
                bins="auto",
                color="#4c78a8",
                edgecolor="white",
            )
            axes[1, 0].set_xlabel("Delta V50, mV")
            axes[1, 0].set_ylabel("Pixel count")
            axes[1, 1].hist(
                _finite_values(data, "sigma_fractional_change"),
                bins="auto",
                color="#f28e2b",
                edgecolor="white",
            )
            axes[1, 1].set_xlabel("Sigma / reference sigma - 1")
            axes[1, 1].set_ylabel("Pixel count")
            for axis in axes[0, :]:
                axis.set_xlabel("Physical column")
                axis.set_ylabel("Physical row")
            figure.suptitle(
                f"Injection-density shift: {_amplitude_label(data)}, pattern {pattern}"
            )
            stem = (
                f"crosstalk_pixel_maps_{_safe_stem(pattern)}_"
                f"amplitude_{metric_index:03d}"
            )
            outputs[stem] = _save_figure(figure, plot_directory, stem, settings)

    if not crosstalk_summary.empty and crosstalk_summary["injection_pattern"].nunique() > 1:
        amplitudes = list(
            crosstalk_summary["pulse_amplitude_native"].drop_duplicates()
        )
        for amplitude_index, amplitude in enumerate(amplitudes):
            data = crosstalk_summary[
                crosstalk_summary["pulse_amplitude_native"] == amplitude
            ].copy()
            data = data.sort_values("median_active_pixels_per_shot")
            x = pd.to_numeric(
                data["median_active_pixels_per_shot"], errors="coerce"
            ).to_numpy(dtype=float)
            positive_x = x[np.isfinite(x) & (x > 0)]
            if not len(positive_x):
                continue
            figure, axes = plt.subplots(1, 3, figsize=(13.2, 4.2))
            axes[0].plot(
                x,
                1000
                * pd.to_numeric(
                    data["median_abs_delta_v50_v_to_reference"], errors="coerce"
                ),
                marker="o",
            )
            axes[0].set_ylabel("Median |delta V50|, mV")
            axes[1].plot(
                x,
                pd.to_numeric(
                    data["median_sigma_ratio_to_reference"], errors="coerce"
                ),
                marker="o",
            )
            axes[1].axhline(1.0, color="black", linestyle="--", linewidth=1)
            axes[1].set_ylabel("Median sigma / reference sigma")
            axes[2].plot(
                x,
                pd.to_numeric(
                    data["inactive_excess_hit_fraction_p95"], errors="coerce"
                ),
                marker="o",
            )
            axes[2].set_ylabel("Inactive excess hit fraction, p95")
            for axis in axes:
                if len(np.unique(positive_x)) > 1:
                    axis.set_xscale("log", base=2)
                    axis.set_xlim(
                        positive_x.min() / 1.2, positive_x.max() * 1.2
                    )
                else:
                    center = float(positive_x[0])
                    axis.set_xlim(center * 0.8, center * 1.2)
                axis.set_xlabel("Active pixels per shot")
            figure.suptitle(
                f"Injection-density coupling diagnostics, amplitude {amplitude}"
            )
            suffix = f"_{amplitude_index:03d}" if len(amplitudes) > 1 else ""
            outputs[f"injection_crosstalk{suffix}"] = _save_figure(
                figure,
                plot_directory,
                f"injection_crosstalk_metrics{suffix}",
                settings,
            )

    return outputs
