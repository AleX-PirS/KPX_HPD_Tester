"""Единый выбор теста v2 и проверка настроек без аппаратных подключений."""
from __future__ import annotations

import argparse
import importlib
import json
from pathlib import Path
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

MODULES = {
    "full": "run_full_characterization", "noise": "run_noise_scan",
    "equalize": "run_noise_equalization", "trim_sweep": "run_full_trim_sweep",
    "scurve": "run_scurve", "clock_noise": "run_clock_noise", "crosstalk": "run_crosstalk",
    "gain_equalization": "analyze_gain_sweep", "offline": "analyze_experiment",
    "all_windows": "run_all_windows", "ref_preview": "preview_ref_selection",
    "reference_verification": "run_reference_verification", "dashboard": "run_plot_dashboard",
    "eo_sweep": "run_eo_parameter_sweep",
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test", choices=tuple(MODULES), help="переопределить RUN.test на один запуск")
    parser.add_argument("--check-config", action="store_true", help="проверить нужные пути и условия, НЕ открывая приборы")
    parser.add_argument("--no-plots", action="store_true", help="отключить PNG/PDF на один запуск")
    arguments = parser.parse_args()
    try:
        from comparator_characterization.high_level import characterization_config as config
        selected = arguments.test or config.RUN.test
        if arguments.no_plots:
            config.PLOTS.generate = False
        report = config.validate_configuration(selected, require_hardware=not arguments.check_config)
    except (ValueError, TypeError, FileNotFoundError, RuntimeError) as error:
        parser.exit(2, f"Настройки v2: {error}\n")
    print(f"Тест: {selected}; окно: {report['window']}; .env: {config.PATHS.env_file}")
    for key, path in report["active_paths"].items():
        print(f"  {key} = {path}")
    if report["references"]:
        print("REF: " + json.dumps(report["references"], ensure_ascii=False))
    for warning in report["warnings"]:
        print(f"Предупреждение: {warning}")
    if arguments.check_config:
        print("Конфигурация проверена. Приборы не открывались.")
        return 0
    config.RUN.test = selected
    module = importlib.import_module("comparator_characterization.high_level." + MODULES[selected])
    previous = sys.argv
    try:
        sys.argv = [previous[0]]
        module.main()
    finally:
        sys.argv = previous
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
