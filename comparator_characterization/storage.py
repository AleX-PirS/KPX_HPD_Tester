from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
import csv
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any

import pandas as pd


INDEX_FIELDS = (
    "acquisition_id",
    "acquisition_key",
    "timestamp_utc",
    "measurement_kind",
    "stage",
    "scan_phase",
    "acquisition_type",
    "threshold_dac_code",
    "repeat_index",
    "pulse_amplitude",
    "status",
    "row_count",
    "relative_path",
    "error",
)


def utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "item"):
        try:
            return value.item()
        except (TypeError, ValueError):
            pass
    return value


def atomic_write_json(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(_jsonable(document), ensure_ascii=False, indent=2) + "\n"
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
            temporary = Path(stream.name)
        os.replace(temporary, path)
    except Exception:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_path_component(value: Any) -> str:
    text = str(value).strip()
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip(".")
    if not safe:
        raise ValueError(f"invalid empty path component derived from {text!r}")
    return safe


class ExperimentStore:
    """Crash-tolerant experiment directory with immutable raw acquisitions."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.metadata_path = self.root / "metadata.json"
        self.raw_root = self.root / "raw"
        self.index_path = self.raw_root / "measurement_index.csv"
        self.error_path = self.raw_root / "errors.jsonl"
        if not self.metadata_path.exists():
            raise FileNotFoundError(f"experiment metadata not found: {self.metadata_path}")
        self.metadata = json.loads(self.metadata_path.read_text(encoding="utf-8"))
        self._status_by_key = self._load_status_index()

    @classmethod
    def create(
        cls,
        results_root: str | Path,
        *,
        window: str,
        metadata: Mapping[str, Any],
    ) -> "ExperimentStore":
        parent = Path(results_root).resolve()
        parent.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        stem = f"{timestamp}_{window.upper()}"
        root = parent / stem
        suffix = 1
        while root.exists():
            root = parent / f"{stem}_{suffix:02d}"
            suffix += 1
        (root / "raw").mkdir(parents=True)
        document = {
            "format": "kpx_comparator_characterization",
            "format_version": 2,
            "experiment_id": root.name,
            "created_utc": utc_now_text(),
            "updated_utc": utc_now_text(),
            "status": "in_progress",
            **_jsonable(metadata),
        }
        atomic_write_json(root / "metadata.json", document)
        return cls(root)

    def _load_status_index(self) -> dict[str, str]:
        if not self.index_path.exists():
            return {}
        status: dict[str, str] = {}
        with self.index_path.open("r", newline="", encoding="utf-8") as stream:
            for row in csv.DictReader(stream):
                status[str(row.get("acquisition_key", ""))] = str(row.get("status", ""))
        return status

    @staticmethod
    def acquisition_key(descriptor: Mapping[str, Any]) -> str:
        identity = {
            name: _jsonable(descriptor.get(name))
            for name in (
                "measurement_kind",
                "stage",
                "scan_phase",
                "acquisition_type",
                "threshold_dac_code",
                "repeat_index",
                "pulse_amplitude",
                "injection_pattern",
                "injection_group_id",
            )
        }
        return json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=True)

    @staticmethod
    def acquisition_id(key: str) -> str:
        return hashlib.sha1(key.encode("utf-8"), usedforsecurity=False).hexdigest()[:16]

    def is_complete(self, descriptor: Mapping[str, Any]) -> bool:
        return self._status_by_key.get(self.acquisition_key(descriptor)) == "complete"

    def _relative_acquisition_path(self, descriptor: Mapping[str, Any], acquisition_id: str) -> Path:
        kind = _safe_path_component(descriptor["measurement_kind"])
        stage = _safe_path_component(descriptor["stage"])
        phase = _safe_path_component(descriptor.get("scan_phase", "unspecified"))
        code = int(descriptor["threshold_dac_code"])
        acquisition_type = _safe_path_component(
            descriptor.get("acquisition_type", "measurement")
        )
        repeat = int(descriptor.get("repeat_index", 0))
        amplitude = descriptor.get("pulse_amplitude")
        amplitude_tag = ""
        if amplitude not in (None, ""):
            digest = hashlib.sha1(
                json.dumps(_jsonable(amplitude), sort_keys=True).encode("utf-8"),
                usedforsecurity=False,
            ).hexdigest()[:8]
            amplitude_tag = f"_amp_{digest}"
        filename = (
            f"{acquisition_type}{amplitude_tag}_repeat_{repeat:03d}_"
            f"{acquisition_id}.csv"
        )
        return Path(kind) / stage / phase / f"dac_{code:04d}" / filename

    def write_acquisition(
        self,
        descriptor: Mapping[str, Any],
        rows: Sequence[Mapping[str, Any]],
    ) -> Path:
        if not rows:
            raise ValueError("raw acquisition must contain at least one row")
        key = self.acquisition_key(descriptor)
        acquisition_id = self.acquisition_id(key)
        relative = self._relative_acquisition_path(descriptor, acquisition_id)
        destination = self.raw_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)

        fieldnames: list[str] = []
        seen: set[str] = set()
        for row in rows:
            for field_name in row:
                if field_name not in seen:
                    seen.add(field_name)
                    fieldnames.append(str(field_name))

        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                newline="",
                encoding="utf-8",
                prefix=f".{destination.name}.",
                suffix=".tmp",
                dir=destination.parent,
                delete=False,
            ) as stream:
                writer = csv.DictWriter(stream, fieldnames=fieldnames, extrasaction="raise")
                writer.writeheader()
                for row in rows:
                    writer.writerow({name: _jsonable(row.get(name)) for name in fieldnames})
                stream.flush()
                os.fsync(stream.fileno())
                temporary = Path(stream.name)
            os.replace(temporary, destination)
        except Exception:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            raise

        self._append_index(
            descriptor,
            acquisition_id=acquisition_id,
            key=key,
            status="complete",
            row_count=len(rows),
            relative_path=relative.as_posix(),
            error="",
        )
        return destination

    def record_failed_acquisition(
        self,
        descriptor: Mapping[str, Any],
        error: BaseException | str,
    ) -> None:
        key = self.acquisition_key(descriptor)
        acquisition_id = self.acquisition_id(key)
        error_text = str(error)
        self._append_index(
            descriptor,
            acquisition_id=acquisition_id,
            key=key,
            status="failed",
            row_count=0,
            relative_path="",
            error=error_text,
        )
        self.record_error(
            {
                "timestamp_utc": utc_now_text(),
                "acquisition_id": acquisition_id,
                "descriptor": descriptor,
                "error": error_text,
            }
        )

    def _append_index(
        self,
        descriptor: Mapping[str, Any],
        *,
        acquisition_id: str,
        key: str,
        status: str,
        row_count: int,
        relative_path: str,
        error: str,
    ) -> None:
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        new_file = not self.index_path.exists()
        row = {
            "acquisition_id": acquisition_id,
            "acquisition_key": key,
            "timestamp_utc": utc_now_text(),
            "measurement_kind": descriptor.get("measurement_kind", ""),
            "stage": descriptor.get("stage", ""),
            "scan_phase": descriptor.get("scan_phase", ""),
            "acquisition_type": descriptor.get("acquisition_type", ""),
            "threshold_dac_code": descriptor.get("threshold_dac_code", ""),
            "repeat_index": descriptor.get("repeat_index", ""),
            "pulse_amplitude": json.dumps(
                _jsonable(descriptor.get("pulse_amplitude")), ensure_ascii=False
            ),
            "status": status,
            "row_count": row_count,
            "relative_path": relative_path,
            "error": error,
        }
        with self.index_path.open("a", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=INDEX_FIELDS)
            if new_file:
                writer.writeheader()
            writer.writerow(row)
            stream.flush()
            os.fsync(stream.fileno())
        self._status_by_key[key] = status

    def record_error(self, document: Mapping[str, Any]) -> None:
        self.error_path.parent.mkdir(parents=True, exist_ok=True)
        with self.error_path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(_jsonable(document), ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def update_metadata(self, **updates: Any) -> None:
        self.metadata.update(_jsonable(updates))
        self.metadata["updated_utc"] = utc_now_text()
        atomic_write_json(self.metadata_path, self.metadata)

    def set_status(self, status: str, *, error: str | None = None) -> None:
        updates: dict[str, Any] = {"status": status}
        if status in {"complete", "failed", "interrupted"}:
            updates["finished_utc"] = utc_now_text()
        if error is not None:
            updates["failure_reason"] = error
        self.update_metadata(**updates)

    def copy_input_file(self, source: str | Path, subdirectory: str) -> dict[str, Any]:
        source_path = Path(source).resolve()
        destination_directory = self.root / subdirectory
        destination_directory.mkdir(parents=True, exist_ok=True)
        destination = destination_directory / source_path.name
        if destination.exists():
            stem = destination.stem
            suffix = destination.suffix
            index = 1
            while destination.exists():
                destination = destination_directory / f"{stem}_{index:02d}{suffix}"
                index += 1
        shutil.copy2(source_path, destination)
        return {
            "original_path": str(source_path),
            "experiment_copy": destination.relative_to(self.root).as_posix(),
            "sha256": file_sha256(destination),
            "size_bytes": destination.stat().st_size,
        }

    def load_raw(
        self,
        measurement_kind: str | None = None,
        *,
        stages: Iterable[str] | None = None,
    ) -> pd.DataFrame:
        if not self.index_path.exists():
            return pd.DataFrame()
        index = pd.read_csv(self.index_path, keep_default_na=False)
        index = index[index["status"] == "complete"]
        if measurement_kind is not None:
            index = index[index["measurement_kind"] == measurement_kind]
        if stages is not None:
            index = index[index["stage"].isin(tuple(stages))]
        # Keep only the last complete record for a resumed acquisition key.
        index = index.drop_duplicates(subset=["acquisition_key"], keep="last")
        frames: list[pd.DataFrame] = []
        for relative in index["relative_path"]:
            path = (self.raw_root / str(relative)).resolve()
            try:
                path.relative_to(self.raw_root.resolve())
            except ValueError as error:
                raise ValueError(
                    f"indexed raw path escapes the experiment directory: {relative}"
                ) from error
            if not path.exists():
                raise FileNotFoundError(f"indexed raw acquisition is missing: {path}")
            frame = pd.read_csv(path, keep_default_na=False)
            frame["raw_source_file"] = str(relative)
            frames.append(frame)
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, ignore_index=True, sort=False)

    def next_analysis_directory(self) -> Path:
        parent = self.root / "analysis"
        parent.mkdir(parents=True, exist_ok=True)
        versions = []
        for child in parent.iterdir():
            if child.is_dir() and child.name.startswith("v") and child.name[1:].isdigit():
                versions.append(int(child.name[1:]))
        version = max(versions, default=0) + 1
        destination = parent / f"v{version:03d}"
        destination.mkdir()
        return destination

    def write_table(self, path: Path, rows: pd.DataFrame | Sequence[Mapping[str, Any]]) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        frame = rows if isinstance(rows, pd.DataFrame) else pd.DataFrame(rows)
        temporary = path.with_name(f".{path.name}.tmp")
        frame.to_csv(temporary, index=False)
        with temporary.open("r+b") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        return path
