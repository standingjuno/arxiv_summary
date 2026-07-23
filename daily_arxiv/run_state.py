"""Operational state for locking and failed-run retries."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import json
from pathlib import Path
from typing import Iterator

from config.settings import Settings


class PipelineAlreadyRunning(RuntimeError):
    """Raised when another pipeline process already holds the run lock."""


@dataclass(frozen=True)
class RunSpec:
    date_str: str
    step: str
    categories: tuple[str, ...] | None = None
    limit: int | None = None

    def key(self) -> str:
        categories = ",".join(self.categories or ())
        return f"{self.date_str}|{self.step}|{categories}|{self.limit or ''}"

    def to_dict(self) -> dict[str, object]:
        return {
            "date": self.date_str,
            "step": self.step,
            "categories": list(self.categories or ()),
            "limit": self.limit,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "RunSpec":
        categories_raw = data.get("categories") or []
        categories = tuple(str(item) for item in categories_raw) if isinstance(categories_raw, list) else None
        limit_raw = data.get("limit")
        return cls(
            date_str=str(data["date"]),
            step=str(data["step"]),
            categories=categories or None,
            limit=int(limit_raw) if limit_raw not in (None, "") else None,
        )


@contextmanager
def pipeline_lock(settings: Settings) -> Iterator[None]:
    settings.pipeline_lock_path.parent.mkdir(parents=True, exist_ok=True)
    with settings.pipeline_lock_path.open("w", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise PipelineAlreadyRunning(
                f"Another arxiv_summary run is already active: {settings.pipeline_lock_path}"
            ) from exc

        lock_file.write(datetime.now(timezone.utc).isoformat())
        lock_file.flush()
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _read_failed_runs(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def _write_failed_runs(path: Path, runs: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(runs, ensure_ascii=False, indent=2), encoding="utf-8")


def load_failed_run_specs(settings: Settings) -> list[RunSpec]:
    return [RunSpec.from_dict(item["spec"]) for item in _read_failed_runs(settings.failed_runs_path) if "spec" in item]


def record_failed_run(settings: Settings, spec: RunSpec, error: BaseException) -> None:
    existing = _read_failed_runs(settings.failed_runs_path)
    now = datetime.now(timezone.utc).isoformat()
    updated: list[dict[str, object]] = []
    found = False

    for item in existing:
        item_spec = RunSpec.from_dict(item["spec"]) if "spec" in item else None
        if item_spec and item_spec.key() == spec.key():
            attempts = int(item.get("attempts", 0)) + 1
            item.update(
                {
                    "spec": spec.to_dict(),
                    "attempts": attempts,
                    "last_error": str(error),
                    "last_failed_at": now,
                }
            )
            found = True
        updated.append(item)

    if not found:
        updated.append(
            {
                "spec": spec.to_dict(),
                "attempts": 1,
                "first_failed_at": now,
                "last_failed_at": now,
                "last_error": str(error),
            }
        )

    _write_failed_runs(settings.failed_runs_path, updated)
    print(f"[state] recorded failed run -> {settings.failed_runs_path}")


def clear_failed_run(settings: Settings, spec: RunSpec) -> None:
    existing = _read_failed_runs(settings.failed_runs_path)
    remaining: list[dict[str, object]] = []
    removed = False

    for item in existing:
        item_spec = RunSpec.from_dict(item["spec"]) if "spec" in item else None
        if item_spec and item_spec.key() == spec.key():
            removed = True
            continue
        remaining.append(item)

    if removed:
        _write_failed_runs(settings.failed_runs_path, remaining)
        print(f"[state] cleared failed run date={spec.date_str} step={spec.step}")
