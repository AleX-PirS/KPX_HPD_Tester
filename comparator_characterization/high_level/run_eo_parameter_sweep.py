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
    run_full_characterization(eo_sweep=True)


if __name__ == "__main__":
    main()
