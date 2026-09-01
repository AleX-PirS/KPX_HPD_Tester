"""Sequential, resumable Cartesian product of logical EO/KIPIX settings."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
import hashlib
import itertools
import json
import logging
from pathlib import Path
from typing import Any, Callable

import EO_cfg
import pandas as pd

from .models import CharacterizationSettings, FRAMEWORK_VERSION, get_window_spec
from .parameters import validate_eo_overrides
from .storage import atomic_write_json, atomic_write_table, file_sha256, utc_now_text
from .workflow import CharacterizationResult, characterize_comparator

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SweepNoiseExposure:
    sweep_path: Path
    combination_index: int
    combination_count: int
    eo_overrides: dict[str, int]
    noise_shutter_duration_s: float


def interactive_noise_exposure_pause(change: SweepNoiseExposure) -> None:
    input(
        f"Комбинация {change.combination_index}/{change.combination_count}: "
        f"{change.eo_overrides}. Установите в УПО noise-экспозицию "
        f"{change.noise_shutter_duration_s * 1e6:g} мкс и нажмите Enter.\n"
    )


@dataclass(frozen=True)
class ParameterSweepResult:
    experiment_path: Path
    combinations: tuple[dict[str, Any], ...]
    status: str


def _freeze(value: Any) -> Any:
    """Stable input fingerprint, without instrument handles or Python reprs."""
    if is_dataclass(value):
        return _freeze(asdict(value))
    if hasattr(value, "to_metadata"):
        return _freeze(value.to_metadata())
    if isinstance(value, Mapping):
        return sorted([[_freeze(k), _freeze(v)] for k, v in value.items()], key=lambda p: str(p[0]))
    if isinstance(value, (tuple, list)):
        return [_freeze(v) for v in value]
    if isinstance(value, Path):
        return {"path": str(value.resolve()), "sha256": file_sha256(value) if value.is_file() else None}
    if isinstance(value, str):
        try:
            if Path(value).is_file():
                return _freeze(Path(value))
        except OSError:
            pass
        return value
    if value is None or isinstance(value, (int, float, bool)):
        return value
    raise TypeError(f"cannot fingerprint sweep input of type {type(value).__name__}")


def _grid_combinations(grid: Mapping[str, Sequence[int]] | None, *, run_scurve: bool) -> list[dict[str, int]]:
    if grid is None:
        return [{}]
    if not isinstance(grid, Mapping):
        raise TypeError("eo_parameter_grid must be a parameter -> sequence mapping")
    if not grid:
        return [{}]
    axes = []
    for name, values in grid.items():
        if isinstance(values, (str, bytes, Mapping)):
            raise TypeError(f"grid axis {name} must contain integer values")
        try:
            values = list(values)
        except TypeError as error:
            raise TypeError(f"grid axis {name} must contain integer values") from error
        if not values:
            raise ValueError(f"empty EO sweep axis: {name}")
        checked = [validate_eo_overrides({name: v}, run_scurve=run_scurve)[name] for v in values]
        if len(set(checked)) != len(checked):
            raise ValueError(f"duplicate values on EO sweep axis: {name}")
        axes.append(checked)
    return [dict(zip(grid, values)) for values in itertools.product(*axes)]


def _inside(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError("sweep entry escapes the sweep directory")
    return path


def _find_experiment(root: Path, entry: Mapping[str, Any]) -> Path | None:
    if entry.get("experiment"):
        return _inside(root, entry["experiment"])
    directory = _inside(root, entry["directory"])
    matches = sorted(directory.glob("*/metadata.json"))
    if len(matches) > 1:
        raise ValueError(f"multiple experiments under {directory}; cannot choose automatically")
    return matches[0].parent if matches else None


def _write_summary(root: Path, manifest: Mapping[str, Any]) -> None:
    rows = []
    for entry in manifest["combinations"]:
        row = {"combination_index": entry["index"], **entry["eo_overrides"],
               "status": entry["status"], "directory": entry["directory"],
               "experiment": entry.get("experiment"), "analysis": entry.get("analysis")}
        if entry.get("analysis"):
            summary = _inside(root, entry["analysis"]) / "summary.csv"
            if summary.is_file():
                frame = pd.read_csv(summary)
                if not frame.empty:
                    row.update(frame.iloc[0].to_dict())
        rows.append(row)
    atomic_write_table(root / "sweep_summary.csv", pd.DataFrame(rows))


def characterize_parameter_sweep(
    client: Any,
    threshold_calibration_files: Mapping[str, Any],
    *,
    eo_parameter_grid: Mapping[str, Sequence[int]] | None = None,
    resume_sweep: str | Path | None = None,
    before_noise: Callable[[SweepNoiseExposure], None] | None = None,
    results_root: str | Path = "results",
    **characterization_kwargs: Any,
) -> ParameterSweepResult:
    """Run each combination as its own full experiment, never in parallel.

    The first mapping key changes slowest. Results are nested as
    PARAM1=value/PARAM2=value/<experiment>/raw, analysis, inputs, metadata.json.
    None/empty grid gives one standard combination. For the legacy flat folder
    layout call characterize_comparator directly (the high-level launcher does
    this automatically when EO_PARAMETER_GRID and RESUME_SWEEP are None).
    """
    if characterization_kwargs.get("resume_experiment") is not None:
        raise ValueError("use resume_sweep for a batch, not resume_experiment")
    if characterization_kwargs.get("eo_overrides"):
        raise ValueError("put all outer EO settings in eo_parameter_grid")
    kwargs = dict(characterization_kwargs)
    kwargs.pop("resume_experiment", None)
    kwargs.pop("eo_overrides", None)
    window = get_window_spec(kwargs.get("window", "AB")).name
    run_scurve = kwargs.get("run_scurve", True)
    combos = _grid_combinations(eo_parameter_grid, run_scurve=run_scurve)
    settings = kwargs.get("settings") or CharacterizationSettings()
    # Reusing one noise reference across different bias points is not valid.
    if len(combos) > 1 and kwargs.get("noise_reference_experiment") is not None:
        raise ValueError("a parameter sweep needs a separate noise scan per combination")
    omitted = {"keysight_generator", "shot_executor", "before_scurve"}
    contract = {
        "framework_version": FRAMEWORK_VERSION,
        "window": window, "combinations": combos,
        "threshold_calibrations": _freeze(threshold_calibration_files),
        "arguments": _freeze({k: v for k, v in kwargs.items() if k not in omitted}),
        "default_settings": _freeze(settings),
        "eo_defaults": _freeze(EO_cfg.DEFAULT_REGISTERS),
        "executor_type": type(kwargs.get("shot_executor")).__qualname__,
        "executor_settings": _freeze(
            getattr(kwargs.get("shot_executor"), "settings", None)
        ),
    }
    digest = hashlib.sha256(json.dumps(contract, sort_keys=True, allow_nan=False).encode()).hexdigest()
    if resume_sweep is not None:
        root = Path(resume_sweep).resolve()
        manifest = json.loads((root / "sweep.json").read_text(encoding="utf-8"))
        if manifest.get("contract_sha256") != digest:
            raise ValueError("resume sweep inputs differ: retain the original grid/settings/calibrations/maps")
        expected_entries = [
            {
                "index": index,
                "eo_overrides": values,
                "directory": "/".join(f"{k}={v}" for k, v in values.items()) or "defaults",
            }
            for index, values in enumerate(combos, 1)
        ]
        entries = manifest.get("combinations")
        if not isinstance(entries, list) or len(entries) != len(expected_entries):
            raise ValueError("sweep manifest combination list is damaged")
        for entry, expected in zip(entries, expected_entries):
            if any(entry.get(key) != value for key, value in expected.items()):
                raise ValueError("sweep manifest combination identity is damaged")
    else:
        parent = Path(results_root).resolve()
        parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
        root = parent / f"{stamp}_{window}_sweep"
        root.mkdir()
        manifest = {
            "format": "kpx_eo_parameter_sweep", "format_version": 1,
            "created_utc": utc_now_text(), "contract": contract,
            "contract_sha256": digest, "status": "in_progress",
            "combinations": [
                {"index": index, "eo_overrides": values, "status": "pending",
                 "directory": "/".join(f"{k}={v}" for k, v in values.items()) or "defaults"}
                for index, values in enumerate(combos, 1)
            ],
        }
    manifest["status"] = "in_progress"
    manifest["updated_utc"] = utc_now_text()
    atomic_write_json(root / "sweep.json", manifest)
    log_stream = logging.FileHandler(root / "sweep.log", encoding="utf-8")
    log_stream.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    logger.addHandler(log_stream)
    try:
        for entry in manifest["combinations"]:
            experiment = _find_experiment(root, entry)
            if entry["status"] == "complete":
                if experiment is None or not (experiment / "metadata.json").is_file():
                    raise ValueError("completed sweep experiment is missing")
                meta = json.loads((experiment / "metadata.json").read_text(encoding="utf-8"))
                if meta.get("eo_overrides", {}) != entry["eo_overrides"]:
                    raise ValueError("completed experiment EO configuration differs")
                if meta.get("status") == "complete" and entry.get("analysis") and (
                    _inside(root, entry["analysis"]) / "analysis_manifest.json"
                ).is_file():
                    logger.info("SWEEP %.1f%% | пропуск завершенной комбинации %s",
                                100 * entry["index"] / len(combos), entry["eo_overrides"])
                    continue
            entry["status"] = "in_progress"
            if experiment is not None:
                entry["experiment"] = experiment.relative_to(root).as_posix()
            atomic_write_json(root / "sweep.json", manifest)
            logger.info("SWEEP %.1f%% | комбинация %s/%s: %s",
                        100 * (entry["index"] - 1) / len(combos), entry["index"], len(combos), entry["eo_overrides"])
            try:
                if kwargs.get("run_noise_scan", True) or kwargs.get("run_equalization", True):
                    if settings.noise.shutter_duration_s is None:
                        raise ValueError("noise exposure is required for a multi-run sweep")
                    (before_noise or interactive_noise_exposure_pause)(SweepNoiseExposure(
                        root, entry["index"], len(combos), entry["eo_overrides"],
                        settings.noise.shutter_duration_s,
                    ))
                result = characterize_comparator(
                    client, threshold_calibration_files,
                    results_root=_inside(root, entry["directory"]),
                    resume_experiment=experiment, eo_overrides=entry["eo_overrides"],
                    **kwargs,
                )
                entry.update(status="complete", experiment=result.experiment_path.relative_to(root).as_posix(),
                             analysis=result.analysis_path.relative_to(root).as_posix() if result.analysis_path else None)
            except BaseException as error:
                entry.update(status="interrupted" if isinstance(error, KeyboardInterrupt) else "failed", error=str(error))
                experiment = _find_experiment(root, entry)
                if experiment is not None:
                    entry["experiment"] = experiment.relative_to(root).as_posix()
                manifest["status"] = entry["status"]
                raise
            finally:
                manifest["updated_utc"] = utc_now_text()
                atomic_write_json(root / "sweep.json", manifest)
                _write_summary(root, manifest)
        manifest["status"] = "complete"
        atomic_write_json(root / "sweep.json", manifest)
        _write_summary(root, manifest)
        logger.info("SWEEP 100.0%% | все комбинации завершены: %s", root)
        return ParameterSweepResult(root, tuple(manifest["combinations"]), "complete")
    finally:
        logger.removeHandler(log_stream)
        log_stream.close()
