"""Повторный анализ по OFFLINE_SOURCE из .env, с общим стилем графиков v2."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from comparator_characterization import analyze_all_windows, analyze_saved_experiment, analyze_saved_noise_statistics
from comparator_characterization.high_level import characterization_config as config
from comparator_characterization.storage import atomic_write_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("experiment", nargs="?", type=Path)
    parser.add_argument("--no-plots", action="store_true")
    arguments = parser.parse_args()
    source = arguments.experiment or config.PATHS.offline_source
    config.validate_configuration("offline", source_override=source)
    config.configure_runtime_logging()
    root = Path(source)
    if root.is_dir() and root.parent.name == "analysis" and (root.parent.parent / "metadata.json").is_file():
        root = root.parent.parent
    settings = config.build_analysis_settings()
    generate = config.PLOTS.generate and not arguments.no_plots
    metadata = root / "metadata.json"
    if metadata.is_file():
        document = json.loads(metadata.read_text(encoding="utf-8"))
        if str(document.get("window", "")).upper() == "ALL":
            result = analyze_all_windows(root, settings=settings,
                all_window_settings=config.build_all_window_settings(),
                reanalyze_children=True, generate_plots=generate)
        else:
            result = analyze_saved_experiment(root, settings=settings,
                target_voltage=config.metadata.EQUALIZATION.target_voltage,
                bad_pixel_map=config.PATHS.bad_pixel_mask, generate_plots=generate)
    else:
        result = analyze_saved_noise_statistics(root, settings=settings,
            target_voltage=config.metadata.EQUALIZATION.target_voltage,
            bad_pixel_map=config.PATHS.bad_pixel_mask, generate_plots=generate)
    snapshot = config.configuration_snapshot("offline")
    snapshot["actual_source"] = str(root.resolve())
    atomic_write_json(Path(result["analysis_directory"]) / "configuration_v2.json", snapshot)
    print(f"Повторный анализ: {result['analysis_directory']}")
    return result


if __name__ == "__main__":
    main()
