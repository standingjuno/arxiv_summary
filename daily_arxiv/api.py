"""HTTP API for on-demand paper loading from the GitHub Pages UI."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import date, datetime
import threading
import json
import time
import uuid
from typing import Iterator
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from config.settings import Settings, load_settings
from daily_arxiv.database import PaperRow, cleanup_old_papers, init_db, save_summarized_papers_to_db
from daily_arxiv.fetch_arxiv import fetch_arxiv_papers
from daily_arxiv.run_state import PipelineAlreadyRunning, pipeline_lock
from daily_arxiv.static_export import calendar_window_start, export_static_site_data, web_data_path
from daily_arxiv.summary_ai import (
    ACTIVE_BATCH_STATUSES,
    RETRYABLE_TERMINAL_BATCH_STATUSES,
    SummaryBatchPending,
    finalize_completed_summary_batches,
    summarize_papers,
)


class DateRunResponse(BaseModel):
    job_id: str
    date: str
    status: str
    progress: int
    message: str
    paper_count: int | None = None


class JobStatusResponse(BaseModel):
    job_id: str
    date: str
    status: str
    progress: int
    message: str
    paper_count: int | None = None
    error: str | None = None


settings = load_settings()
app = FastAPI(title="arxiv_summary API")

cors_origins = list(settings.api_cors_origins)
if cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )

_jobs: dict[str, dict[str, object]] = {}
_date_jobs: dict[str, str] = {}
_jobs_lock = threading.Lock()


def _today(settings: Settings) -> date:
    return datetime.now(ZoneInfo(settings.timezone)).date()


def _parse_listing_date(date_str: str, settings: Settings) -> date:
    try:
        value = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="date must use YYYY-MM-DD format") from exc

    today = _today(settings)
    cutoff = calendar_window_start(today, days=settings.web_export_days)
    if value > today:
        raise HTTPException(status_code=400, detail="future dates are not available")
    if value < cutoff:
        raise HTTPException(status_code=400, detail="date is outside the calendar window")
    if value.weekday() >= 5:
        raise HTTPException(status_code=400, detail="arXiv has no regular weekend listing")
    return value


def _paper_count_for_date(settings: Settings, date_str: str) -> int:
    engine = init_db(settings=settings)
    with Session(engine) as session:
        count = session.scalar(select(func.count()).select_from(PaperRow).where(PaperRow.listing_date == date_str))
    return int(count or 0)


def _set_job(job_id: str, **updates: object) -> None:
    with _jobs_lock:
        job = _jobs.setdefault(job_id, {})
        job.update(updates)


def _job_response(job_id: str) -> JobStatusResponse:
    _refresh_pending_job(job_id)
    with _jobs_lock:
        job = dict(_jobs.get(job_id) or {})
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return JobStatusResponse(job_id=job_id, **job)


def _create_or_get_job(date_str: str) -> tuple[str, dict[str, object], bool]:
    with _jobs_lock:
        existing_job_id = _date_jobs.get(date_str)
        if existing_job_id:
            existing = _jobs.get(existing_job_id)
            if existing and existing.get("status") in {"queued", "running", "pending"}:
                return existing_job_id, existing, False

        job_id = uuid.uuid4().hex
        job = {
            "date": date_str,
            "status": "queued",
            "progress": 1,
            "message": "Queued",
            "paper_count": None,
            "error": None,
        }
        _jobs[job_id] = job
        _date_jobs[date_str] = job_id
        return job_id, job, True


def _load_summary_batch_state(settings: Settings, date_str: str) -> dict[str, object] | None:
    path = settings.batch_dir / f"summary_batch_{date_str}.state.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else None


def _active_summary_batch_dates(settings: Settings, *, exclude: str | None = None) -> list[str]:
    active_dates: list[str] = []
    for path in sorted(settings.batch_dir.glob("summary_batch_*.state.json")):
        state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            continue
        date_str = str(state.get("date") or "").strip()
        if not date_str or date_str == exclude:
            continue
        if str(state.get("status") or "") in ACTIVE_BATCH_STATUSES:
            active_dates.append(date_str)
    return active_dates


def _start_on_demand_thread(job_id: str, date_str: str) -> None:
    thread = threading.Thread(
        target=_run_on_demand_job,
        args=(job_id, date_str),
        daemon=True,
    )
    thread.start()


def _clear_date_job_if_terminal(job_id: str, date_str: str) -> None:
    with _jobs_lock:
        status = _jobs.get(job_id, {}).get("status")
        if _date_jobs.get(date_str) == job_id and status in {"completed", "failed"}:
            _date_jobs.pop(date_str, None)


def _refresh_pending_job(job_id: str) -> None:
    with _jobs_lock:
        job = dict(_jobs.get(job_id) or {})
    if job.get("status") != "pending":
        return

    date_str = str(job.get("date") or "")
    if not date_str:
        return

    job_settings = load_settings()
    if job_settings.api_on_demand_summary_mode != "batch":
        return

    try:
        with pipeline_lock(job_settings):
            _set_job(job_id, progress=65, message="Checking OpenAI batch", error=None)
            summary_settings = replace(job_settings, ai_mode="batch")
            finalized = finalize_completed_summary_batches(settings=summary_settings, save=True)

            if finalized:
                for papers in finalized.values():
                    save_summarized_papers_to_db(papers, settings=job_settings)
                cleanup_old_papers(settings=job_settings)
                export_static_site_data(settings=job_settings)

            paper_count = _paper_count_for_date(job_settings, date_str)
            if paper_count > 0:
                _set_job(
                    job_id,
                    status="completed",
                    progress=100,
                    message="Ready",
                    paper_count=paper_count,
                    error=None,
                )
                _clear_date_job_if_terminal(job_id, date_str)
                return

            state = _load_summary_batch_state(job_settings, date_str) or {}
            status = str(state.get("status") or "")
            active_dates = _active_summary_batch_dates(job_settings, exclude=date_str)
            if status in RETRYABLE_TERMINAL_BATCH_STATUSES and not active_dates:
                _set_job(
                    job_id,
                    status="queued",
                    progress=20,
                    message="Retrying OpenAI batch",
                    error=None,
                )
                _start_on_demand_thread(job_id, date_str)
            elif status in RETRYABLE_TERMINAL_BATCH_STATUSES:
                _set_job(
                    job_id,
                    status="pending",
                    progress=40,
                    message=f"Waiting for OpenAI batch queue ({', '.join(active_dates[:3])})",
                    error=None,
                )
            else:
                _set_job(
                    job_id,
                    status="pending",
                    progress=60,
                    message="OpenAI batch is still processing",
                    error=None,
                )
    except PipelineAlreadyRunning:
        _set_job(job_id, status="pending", progress=60, message="Waiting for another run", error=None)
    except Exception as exc:
        _set_job(job_id, status="failed", progress=100, message="Failed", error=str(exc))
        _clear_date_job_if_terminal(job_id, date_str)


@contextmanager
def _pipeline_lock_for_job(settings: Settings, job_id: str) -> Iterator[None]:
    max_attempts = 90
    for attempt in range(max_attempts):
        try:
            with pipeline_lock(settings):
                yield
                return
        except PipelineAlreadyRunning:
            if attempt >= max_attempts - 1:
                raise
            _set_job(
                job_id,
                status="running",
                progress=5,
                message="Waiting for the daily run to finish",
            )
            time.sleep(10)


def _run_on_demand_job(job_id: str, date_str: str) -> None:
    job_settings = load_settings()
    try:
        with _pipeline_lock_for_job(job_settings, job_id):
            _set_job(job_id, status="running", progress=8, message="Checking database")
            existing_count = _paper_count_for_date(job_settings, date_str)
            if existing_count > 0:
                _set_job(job_id, progress=92, message="Refreshing web data")
                export_static_site_data(settings=job_settings)
                _set_job(
                    job_id,
                    status="completed",
                    progress=100,
                    message="Already available",
                    paper_count=existing_count,
                )
                return

            current_state = _load_summary_batch_state(job_settings, date_str) or {}
            current_status = str(current_state.get("status") or "")
            if current_status in ACTIVE_BATCH_STATUSES:
                _set_job(
                    job_id,
                    status="pending",
                    progress=60,
                    message=f"OpenAI batch is still processing ({current_status})",
                )
                return

            active_dates = _active_summary_batch_dates(job_settings, exclude=date_str)
            if active_dates:
                _set_job(
                    job_id,
                    status="pending",
                    progress=20,
                    message=f"Waiting for OpenAI batch queue ({', '.join(active_dates[:3])})",
                )
                return

            _set_job(job_id, progress=18, message="Fetching arXiv papers")
            raw_papers = fetch_arxiv_papers(date_str=date_str, settings=job_settings, save=True)
            if len(raw_papers) > job_settings.api_on_demand_max_papers:
                raise RuntimeError(
                    f"Too many papers for on-demand run: {len(raw_papers)} "
                    f"> {job_settings.api_on_demand_max_papers}"
                )

            if not raw_papers:
                _set_job(job_id, progress=90, message="No papers found")
                export_static_site_data(settings=job_settings)
                _set_job(job_id, status="completed", progress=100, message="No papers found", paper_count=0)
                return

            summary_settings = replace(job_settings, ai_mode=job_settings.api_on_demand_summary_mode)
            _set_job(job_id, progress=35, message=f"Summarizing {len(raw_papers)} papers")
            summarized = summarize_papers(
                raw_papers,
                date_str=date_str,
                settings=summary_settings,
                save=True,
                resume=True,
            )

            _set_job(job_id, progress=82, message="Saving to database")
            save_summarized_papers_to_db(summarized, settings=job_settings)

            _set_job(job_id, progress=92, message="Refreshing web data")
            cleanup_old_papers(settings=job_settings)
            export_static_site_data(settings=job_settings)

            _set_job(
                job_id,
                status="completed",
                progress=100,
                message="Ready",
                paper_count=len(summarized),
            )
    except SummaryBatchPending as exc:
        _set_job(
            job_id,
            status="pending",
            progress=60,
            message=f"OpenAI batch is still processing ({exc.status})",
            error=None,
        )
    except Exception as exc:
        _set_job(job_id, status="failed", progress=100, message="Failed", error=str(exc))
    finally:
        with _jobs_lock:
            status = _jobs.get(job_id, {}).get("status")
            if _date_jobs.get(date_str) == job_id and status in {"completed", "failed"}:
                _date_jobs.pop(date_str, None)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/data")
def get_site_data() -> dict[str, object]:
    path = web_data_path(settings)
    if not path.exists():
        export_static_site_data(settings=settings)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise HTTPException(status_code=500, detail="site data must be a JSON object")
    return data


@app.post("/api/dates/{date_str}/run", response_model=DateRunResponse)
def run_date(date_str: str) -> DateRunResponse:
    if not settings.api_on_demand_enabled:
        raise HTTPException(status_code=403, detail="on-demand loading is disabled")

    value = _parse_listing_date(date_str, settings)
    normalized_date = value.isoformat()
    existing_count = _paper_count_for_date(settings, normalized_date)
    if existing_count > 0:
        export_static_site_data(settings=settings)
        return DateRunResponse(
            job_id="cached",
            date=normalized_date,
            status="completed",
            progress=100,
            message="Already available",
            paper_count=existing_count,
        )

    job_id, job, created = _create_or_get_job(normalized_date)
    if created:
        _start_on_demand_thread(job_id, normalized_date)
    return DateRunResponse(job_id=job_id, **job)


@app.get("/api/jobs/{job_id}", response_model=JobStatusResponse)
def get_job(job_id: str) -> JobStatusResponse:
    return _job_response(job_id)
