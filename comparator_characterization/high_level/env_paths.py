"""Explicit path-only .env reader. Backslashes are never escape sequences."""
from __future__ import annotations

from dataclasses import dataclass, fields
from pathlib import Path, PureWindowsPath

from comparator_characterization.storage import file_sha256

KEYS = {
    "RESULTS_DIR": "results_dir",
    "THRESHOLD_LUT_A": "threshold_lut_a", "THRESHOLD_LUT_B": "threshold_lut_b",
    "THRESHOLD_LUT_C": "threshold_lut_c", "THRESHOLD_LUT_D": "threshold_lut_d",
    "REF_LUT_1": "ref_lut_1", "REF_LUT_2": "ref_lut_2",
    "BASE_PIXEL_CONFIG": "base_pixel_config", "BAD_PIXEL_MASK": "bad_pixel_mask",
    "GAIN_MAP_CSV": "gain_map_csv", "SCURVE_NOISE_REFERENCE": "scurve_noise_reference",
    "CLOCK_TRIM_REFERENCE": "clock_trim_reference", "GAIN_SWEEP_SOURCE": "gain_sweep_source",
    "OFFLINE_SOURCE": "offline_source", "RESUME_EXPERIMENT": "resume_experiment",
    "EO_SWEEP_RESUME": "eo_sweep_resume",
}


@dataclass
class ExperimentPaths:
    env_file: Path
    project_root: Path
    results_dir: Path | None = None
    threshold_lut_a: Path | None = None
    threshold_lut_b: Path | None = None
    threshold_lut_c: Path | None = None
    threshold_lut_d: Path | None = None
    ref_lut_1: Path | None = None
    ref_lut_2: Path | None = None
    base_pixel_config: Path | None = None
    bad_pixel_mask: Path | None = None
    gain_map_csv: Path | None = None
    scurve_noise_reference: Path | None = None
    clock_trim_reference: Path | None = None
    gain_sweep_source: Path | None = None
    offline_source: Path | None = None
    resume_experiment: Path | None = None
    eo_sweep_resume: Path | None = None

    def key(self, attribute: str) -> str:
        return next(key for key, value in KEYS.items() if value == attribute)

    def require(self, attribute: str, *, kind: str = "exists") -> Path:
        value = getattr(self, attribute)
        key = self.key(attribute)
        if value is None:
            raise ValueError(f"Заполните {key} в {self.env_file}")
        path = Path(value)
        valid = path.is_file() if kind == "file" else path.is_dir() if kind == "directory" else path.exists()
        if not valid:
            raise FileNotFoundError(f"{key}: не найден {kind}: {path}")
        return path

    def snapshot(self) -> dict:
        return {"env_file": str(self.env_file), "env_sha256": file_sha256(self.env_file),
                "paths": {self.key(field.name): str(getattr(self, field.name)) if getattr(self, field.name) is not None else None
                          for field in fields(self) if field.name not in {"env_file", "project_root"}}}


def _value(text: str, line: int) -> str:
    text = text.strip()
    if text.startswith(("'", '"')):
        quote = text[0]
        end = text.find(quote, 1)
        if end < 0 or (text[end+1:].strip() and not text[end+1:].strip().startswith("#")):
            raise ValueError(f".env, строка {line}: неправильные кавычки или текст после пути")
        return text[1:end]
    if text.startswith("#"):
        return ""
    # Quote paths containing a whitespace followed by a literal #.
    return text.split(" #", 1)[0].strip()


def load_paths(env_file: str | Path, *, project_root: str | Path) -> ExperimentPaths:
    root = Path(project_root).resolve()
    source = Path(env_file).resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Нет {source}. Скопируйте .env.example в .env и заполните пути.")
    values = {}
    for number, line in enumerate(source.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f".env, строка {number}: ожидается KEY=path")
        key, raw = line.split("=", 1)
        key = key.strip()
        if key not in KEYS:
            raise ValueError(f".env, строка {number}: неизвестный ключ {key}; закомментируйте архивную ссылку")
        name = KEYS[key]
        if name in values:
            raise ValueError(f".env, строка {number}: повтор ключа {key}; оставьте одну активную ссылку")
        value = _value(raw, number)
        if not value:
            values[name] = None
        elif value.lower() in {"none", "null"}:
            raise ValueError(f"{key}: для отключения оставьте значение пустым, не {value}")
        else:
            path = Path(value)
            # A Windows absolute path remains literal even when inspecting a
            # configuration on POSIX. Never prepend the project root to it.
            values[name] = path if path.is_absolute() or PureWindowsPath(value).is_absolute() else root / path
    return ExperimentPaths(env_file=source, project_root=root, **values)
