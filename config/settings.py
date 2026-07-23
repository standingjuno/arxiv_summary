"""Application settings loaded from TOML config files."""

from __future__ import annotations

from dataclasses import dataclass
import copy
import os
from pathlib import Path
import sys
import tomllib
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT_DIR / "config"
DEFAULT_CONFIG_PATH = CONFIG_DIR / "settings.toml"
EXAMPLE_CONFIG_PATH = CONFIG_DIR / "settings.example.toml"

DEFAULT_FIELD_BY_CATEGORY = {
    "cs.RO": "robotics",
    "cs.LG": "machine_learning",
    "cs.CV": "computer_vision",
    "stat.ML": "machine_learning",
    "cs.AI": "machine_learning",
    "cs.CL": "machine_learning",
}

DEFAULT_KEYWORD_SEEDS = [
    "SLAM",
    "LiDAR",
    "LIO",
    "LO",
    "Retargeting",
    "LLM",
    "VLM",
    "VLN",
    "VLA",
    "RL",
    "IL",
    "Diffusion",
    "Manipulation",
    "Navigation",
    "Sim2Real",
    "Foundation Model",
]

DEFAULT_KEYWORD_ALIASES = {
    "Robot Retargeting": "Retargeting",
    "Vision-Language Model": "VLM",
    "Vision Language Model": "VLM",
    "Vision Language Models": "VLM",
    "Vision-Language Models": "VLM",
    "Vision-Language Navigation": "VLN",
    "Vision Language Navigation": "VLN",
    "Visual Language Navigation": "VLN",
    "Vision-Language-Action": "VLA",
    "Vision Language Action": "VLA",
    "Vision-Language-Action Model": "VLA",
    "Vision Language Action Model": "VLA",
    "Large Language Model": "LLM",
    "Large Language Models": "LLM",
    "Reinforcement Learning": "RL",
    "Imitation Learning": "IL",
    "Diffusion Policy": "Diffusion",
    "Diffusion Model": "Diffusion",
    "Diffusion Models": "Diffusion",
}

DEFAULT_CONFIG: dict[str, Any] = {
    "app": {
        "timezone": "Asia/Seoul",
        "output_dir": "output",
        "debug": False,
    },
    "arxiv": {
        "categories": ["cs.RO", "cs.LG", "cs.CV"],
        "max_results": 2000,
        "page_size": 100,
        "delay_seconds": 5.0,
        "min_request_interval": 5.0,
        "retry_delays": [30.0, 60.0, 180.0, 600.0],
    },
    "ai": {
        "provider": "openai",
        "mode": "batch",
        "openai_api_key": "",
        "openai_model": "gpt-5.4-nano",
        "openai_batch_completion_window": "24h",
        "openai_batch_poll_interval_seconds": 60.0,
        "openai_batch_wait_timeout_seconds": 0.0,
        "summary_sleep_seconds": 0.5,
        "summary_max_retries": 3,
    },
    "database": {
        "url": "",
        "retention_days": 365,
        "auto_cleanup": True,
    },
    "web": {
        "output_dir": "web",
        "base_path": "/arxiv_summary/",
        "api_base_url": "",
        "export_days": 365,
        "auto_export": True,
    },
    "api": {
        "host": "0.0.0.0",
        "port": 8000,
        "cors_origins": [
            "https://standingjuno.github.io",
            "http://localhost:8765",
            "http://127.0.0.1:8765",
        ],
        "on_demand_enabled": True,
        "on_demand_summary_mode": "sync",
        "on_demand_max_papers": 600,
    },
    "paths": {
        "keyword_store": "",
        "pipeline_lock": "",
        "failed_runs": "",
        "batches": "",
    },
    "daily": {
        "run_hour": 11,
        "run_minute": 0,
        "step": "all",
        "date_offset_days": 0,
        "retry_failed": True,
        "run_on_start": False,
    },
    "fields": DEFAULT_FIELD_BY_CATEGORY,
    "keywords": {
        "seed": DEFAULT_KEYWORD_SEEDS,
        "aliases": DEFAULT_KEYWORD_ALIASES,
    },
}


@dataclass(frozen=True)
class Settings:
    root_dir: Path
    config_path: Path | None
    output_dir: Path
    raw_dir: Path
    summary_dir: Path
    batch_dir: Path
    keyword_store_path: Path
    pipeline_lock_path: Path
    failed_runs_path: Path
    timezone: str
    arxiv_categories: tuple[str, ...]
    arxiv_max_results: int
    arxiv_page_size: int
    arxiv_delay_seconds: float
    arxiv_min_request_interval: float
    arxiv_retry_delays: tuple[float, ...]
    field_by_category: dict[str, str]
    ai_provider: str
    ai_mode: str
    openai_api_key: str | None
    openai_model: str
    openai_batch_completion_window: str
    openai_batch_poll_interval_seconds: float
    openai_batch_wait_timeout_seconds: float
    summary_sleep_seconds: float
    summary_max_retries: int
    database_url: str
    database_retention_days: int
    database_auto_cleanup: bool
    web_output_dir: Path
    web_base_path: str
    web_api_base_url: str
    web_export_days: int
    web_auto_export: bool
    api_host: str
    api_port: int
    api_cors_origins: tuple[str, ...]
    api_on_demand_enabled: bool
    api_on_demand_summary_mode: str
    api_on_demand_max_papers: int
    keyword_seed_keywords: tuple[str, ...]
    keyword_aliases: dict[str, str]
    daily_run_hour: int
    daily_run_minute: int
    daily_step: str
    daily_date_offset_days: int
    daily_retry_failed: bool
    run_on_start: bool
    debug: bool


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _resolve_config_path(config_path: str | Path | None = None) -> Path | None:
    raw_path = config_path or os.getenv("CONFIG_PATH")
    if raw_path:
        path = Path(raw_path)
        return path if path.is_absolute() else ROOT_DIR / path
    if DEFAULT_CONFIG_PATH.exists():
        return DEFAULT_CONFIG_PATH
    return EXAMPLE_CONFIG_PATH if EXAMPLE_CONFIG_PATH.exists() else None


def _load_config(config_path: str | Path | None = None) -> tuple[dict[str, Any], Path | None]:
    path = _resolve_config_path(config_path)
    loaded: dict[str, Any] = {}
    if path:
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {path}")
        with path.open("rb") as file:
            loaded = tomllib.load(file)
    return _deep_merge(DEFAULT_CONFIG, loaded), path


def _section(config: dict[str, Any], name: str) -> dict[str, Any]:
    value = config.get(name, {})
    if not isinstance(value, dict):
        raise RuntimeError(f"[{name}] config section must be a table.")
    return value


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _as_string_tuple(value: Any, default: tuple[str, ...]) -> tuple[str, ...]:
    if value in (None, ""):
        return default
    if isinstance(value, str):
        items = [item.strip() for item in value.replace("\n", ",").split(",")]
    elif isinstance(value, list):
        items = [str(item).strip() for item in value]
    else:
        raise RuntimeError("Config value must be a string or list of strings.")
    return tuple(item for item in items if item) or default


def _as_float_tuple(value: Any, default: tuple[float, ...]) -> tuple[float, ...]:
    if value in (None, ""):
        return default
    if isinstance(value, str):
        items: list[Any] = [item.strip() for item in value.replace("\n", ",").split(",")]
    elif isinstance(value, list):
        items = value
    else:
        raise RuntimeError("Config value must be a string or list of numbers.")
    parsed = tuple(float(item) for item in items if str(item).strip())
    return parsed or default


def _as_string_mapping(value: Any, default: dict[str, str]) -> dict[str, str]:
    if value in (None, ""):
        return dict(default)
    if not isinstance(value, dict):
        raise RuntimeError("Config value must be a table of strings.")
    return {str(key): str(item) for key, item in value.items()}


def _resolve_path(value: Any, default: Path) -> Path:
    path = Path(str(value)) if value not in (None, "") else default
    return path if path.is_absolute() else ROOT_DIR / path


def _normalize_database_url(database_url: str) -> str:
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+psycopg://", 1)
    return database_url


def load_settings(config_path: str | Path | None = None) -> Settings:
    config, resolved_config_path = _load_config(config_path)
    app = _section(config, "app")
    arxiv_config = _section(config, "arxiv")
    ai = _section(config, "ai")
    database = _section(config, "database")
    web = _section(config, "web")
    api = _section(config, "api")
    paths = _section(config, "paths")
    daily = _section(config, "daily")
    keywords = _section(config, "keywords")

    output_dir = _resolve_path(app.get("output_dir"), ROOT_DIR / "output")
    raw_dir = output_dir / "raw"
    summary_dir = output_dir / "summaries"
    batch_dir = _resolve_path(paths.get("batches"), output_dir / "batches")
    keyword_store_path = _resolve_path(paths.get("keyword_store"), output_dir / "keywords.json")
    pipeline_lock_path = _resolve_path(paths.get("pipeline_lock"), output_dir / "pipeline.lock")
    failed_runs_path = _resolve_path(paths.get("failed_runs"), output_dir / "failed_runs.json")

    for path in (
        output_dir,
        raw_dir,
        summary_dir,
        batch_dir,
        keyword_store_path.parent,
        pipeline_lock_path.parent,
        failed_runs_path.parent,
    ):
        path.mkdir(parents=True, exist_ok=True)

    database_url = str(database.get("url") or "")
    if not database_url:
        database_url = f"sqlite:///{output_dir / 'arxiv_summary.db'}"

    web_output_dir = _resolve_path(web.get("output_dir"), ROOT_DIR / "web")

    fields = dict(DEFAULT_FIELD_BY_CATEGORY)
    fields.update({str(key): str(value) for key, value in _section(config, "fields").items()})

    return Settings(
        root_dir=ROOT_DIR,
        config_path=resolved_config_path,
        output_dir=output_dir,
        raw_dir=raw_dir,
        summary_dir=summary_dir,
        batch_dir=batch_dir,
        keyword_store_path=keyword_store_path,
        pipeline_lock_path=pipeline_lock_path,
        failed_runs_path=failed_runs_path,
        timezone=str(app.get("timezone") or "Asia/Seoul"),
        arxiv_categories=_as_string_tuple(arxiv_config.get("categories"), ("cs.RO", "cs.LG", "cs.CV")),
        arxiv_max_results=int(arxiv_config.get("max_results", 2000)),
        arxiv_page_size=int(arxiv_config.get("page_size", 100)),
        arxiv_delay_seconds=float(arxiv_config.get("delay_seconds", 5.0)),
        arxiv_min_request_interval=float(arxiv_config.get("min_request_interval", 5.0)),
        arxiv_retry_delays=_as_float_tuple(
            arxiv_config.get("retry_delays"),
            (30.0, 60.0, 180.0, 600.0),
        ),
        field_by_category=fields,
        ai_provider=str(ai.get("provider") or "openai").strip().lower(),
        ai_mode=str(ai.get("mode") or "batch").strip().lower(),
        openai_api_key=str(ai.get("openai_api_key") or "").strip() or None,
        openai_model=str(ai.get("openai_model") or "gpt-5.4-nano"),
        openai_batch_completion_window=str(ai.get("openai_batch_completion_window") or "24h"),
        openai_batch_poll_interval_seconds=float(ai.get("openai_batch_poll_interval_seconds", 60.0)),
        openai_batch_wait_timeout_seconds=float(ai.get("openai_batch_wait_timeout_seconds", 0.0)),
        summary_sleep_seconds=float(ai.get("summary_sleep_seconds", 0.5)),
        summary_max_retries=int(ai.get("summary_max_retries", 3)),
        database_url=_normalize_database_url(database_url),
        database_retention_days=int(database.get("retention_days", 365)),
        database_auto_cleanup=_as_bool(database.get("auto_cleanup"), default=True),
        web_output_dir=web_output_dir,
        web_base_path=str(web.get("base_path") or "/arxiv_summary/"),
        web_api_base_url=str(web.get("api_base_url") or "").rstrip("/"),
        web_export_days=int(web.get("export_days", 365)),
        web_auto_export=_as_bool(web.get("auto_export"), default=True),
        api_host=str(api.get("host") or "0.0.0.0"),
        api_port=int(api.get("port", 8000)),
        api_cors_origins=_as_string_tuple(api.get("cors_origins"), ()),
        api_on_demand_enabled=_as_bool(api.get("on_demand_enabled"), default=True),
        api_on_demand_summary_mode=str(api.get("on_demand_summary_mode") or "sync").strip().lower(),
        api_on_demand_max_papers=int(api.get("on_demand_max_papers", 600)),
        keyword_seed_keywords=_as_string_tuple(
            keywords.get("seed"),
            tuple(DEFAULT_KEYWORD_SEEDS),
        ),
        keyword_aliases=_as_string_mapping(
            keywords.get("aliases"),
            DEFAULT_KEYWORD_ALIASES,
        ),
        daily_run_hour=int(daily.get("run_hour", 11)),
        daily_run_minute=int(daily.get("run_minute", 0)),
        daily_step=str(daily.get("step") or "all"),
        daily_date_offset_days=int(daily.get("date_offset_days", 0)),
        daily_retry_failed=_as_bool(daily.get("retry_failed"), default=True),
        run_on_start=_as_bool(daily.get("run_on_start"), default=False),
        debug=_as_bool(app.get("debug"), default=False),
    )


def require_openai_api_key(settings: Settings) -> str:
    if not settings.openai_api_key:
        config_hint = settings.config_path or DEFAULT_CONFIG_PATH
        raise RuntimeError(f"ai.openai_api_key is required in {config_hint}.")
    return settings.openai_api_key


def _format_cli_value(value: object) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, tuple):
        return ",".join(str(item) for item in value)
    return str(value)


def _setting_value(settings: Settings, key: str) -> object:
    values = {
        "app.timezone": settings.timezone,
        "app.output_dir": settings.output_dir,
        "daily.run_hour": settings.daily_run_hour,
        "daily.run_minute": settings.daily_run_minute,
        "daily.step": settings.daily_step,
        "daily.date_offset_days": settings.daily_date_offset_days,
        "daily.retry_failed": settings.daily_retry_failed,
        "daily.run_on_start": settings.run_on_start,
        "keywords.seed": settings.keyword_seed_keywords,
        "web.output_dir": settings.web_output_dir,
        "web.base_path": settings.web_base_path,
        "web.api_base_url": settings.web_api_base_url,
    }
    if key not in values:
        raise KeyError(f"Unsupported settings key: {key}")
    return values[key]


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        settings = load_settings()
        print(settings.config_path or "built-in defaults")
        return 0

    settings = load_settings()
    print(_format_cli_value(_setting_value(settings, argv[0])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
