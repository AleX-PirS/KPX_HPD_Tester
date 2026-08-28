"""Верхнеуровневый запуск офлайн-анализа и всех графиков."""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from comparator_characterization.__main__ import main


if __name__ == "__main__":
    raise SystemExit(main())
