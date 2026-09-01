"""Полная характеризация для декартовой серии логических параметров EO_CFG."""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from comparator_characterization.high_level import characterization_config as config
from comparator_characterization.high_level.run_full_characterization import (
    main as run_full_characterization,
)


def main() -> None:
    if not config.EO_PARAMETER_GRID and config.RESUME_SWEEP is None:
        raise ValueError(
            "Задайте EO_PARAMETER_GRID или RESUME_SWEEP в characterization_config.py"
        )
    run_full_characterization()


if __name__ == "__main__":
    main()
