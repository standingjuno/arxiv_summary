#!/usr/bin/env sh
set -eu

config_value() {
  python -m config.settings "$1"
}

run_pipeline() {
  daily_date_offset_days="$(config_value daily.date_offset_days)"
  daily_retry_failed="$(config_value daily.retry_failed)"
  daily_step="$(config_value daily.step)"
  run_date="$(date -d "${daily_date_offset_days} days ago" +%F)"

  if [ "${daily_retry_failed}" = "true" ]; then
    echo "[scheduler] retrying failed runs before daily job"
    python main.py --retry-failed-only || echo "[scheduler] failed-run retry pass ended with errors"
  fi

  echo "[scheduler] running pipeline for ${run_date}"
  python main.py --date "${run_date}" --step "${daily_step}" ${DAILY_EXTRA_ARGS:-} \
    || echo "[scheduler] daily pipeline failed; it was recorded for retry"
}

daily_run_date() {
  daily_date_offset_days="$(config_value daily.date_offset_days)"
  date -d "${daily_date_offset_days} days ago" +%F
}

is_weekday() {
  [ "$(date -d "$1" +%u)" -lt 6 ]
}

daily_needs_run() {
  RUN_DATE="$1" python - <<'PY'
import json
import os
import contextlib
import sys
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from config.settings import load_settings
from daily_arxiv.database import PaperRow, init_db

date_str = os.environ["RUN_DATE"]
settings = load_settings()
with contextlib.redirect_stdout(sys.stderr):
    engine = init_db(settings=settings)
with Session(engine) as session:
    count = session.scalar(
        select(func.count()).select_from(PaperRow).where(PaperRow.listing_date == date_str)
    )

if int(count or 0) > 0:
    print("false")
    raise SystemExit

state_path = settings.batch_dir / f"summary_batch_{date_str}.state.json"
if state_path.exists():
    state = json.loads(state_path.read_text(encoding="utf-8"))
    status = str(state.get("status") or "")
    if status in {"validating", "in_progress", "finalizing", "cancelling"}:
        print("false")
        raise SystemExit
    if status == "completed" and not state.get("result_saved"):
        print("false")
        raise SystemExit

print("true")
PY
}

has_pending_scheduler_work() {
  python - <<'PY'
import json
from config.settings import load_settings

settings = load_settings()

if settings.failed_runs_path.exists():
    try:
        failed_runs = json.loads(settings.failed_runs_path.read_text(encoding="utf-8"))
    except Exception:
        failed_runs = []
    if failed_runs:
        print("true")
        raise SystemExit

for path in settings.batch_dir.glob("summary_batch_*.state.json"):
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        continue
    status = str(state.get("status") or "")
    if status in {"validating", "in_progress", "finalizing", "cancelling"}:
        print("true")
        raise SystemExit
    if status == "completed" and not state.get("result_saved"):
        print("true")
        raise SystemExit

print("false")
PY
}

poll_interval_seconds() {
  python - <<'PY'
from config.settings import load_settings

settings = load_settings()
print(max(300, int(float(settings.openai_batch_poll_interval_seconds))))
PY
}

poll_ready_batches() {
  if [ "$(has_pending_scheduler_work)" = "true" ]; then
    echo "[scheduler] checking completed batches and failed runs"
    python main.py --retry-failed-only || echo "[scheduler] background check ended with errors"
  fi
}

run_missed_daily_if_needed() {
  hour="$(config_value daily.run_hour)"
  minute="$(config_value daily.run_minute)"
  target="$(date -d "today ${hour}:${minute}:00" +%s)"
  now="$(date +%s)"

  if [ "${now}" -lt "${target}" ]; then
    return
  fi

  run_date="$(daily_run_date)"
  if ! is_weekday "${run_date}"; then
    echo "[scheduler] missed-run check skipped weekend date=${run_date}"
    return
  fi

  if [ "$(daily_needs_run "${run_date}")" = "true" ]; then
    echo "[scheduler] missed daily run detected for ${run_date}; running catch-up"
    run_pipeline
  else
    echo "[scheduler] missed-run check found existing data or active batch for ${run_date}"
  fi
}

if [ "$(config_value daily.run_on_start)" = "true" ]; then
  run_pipeline
else
  run_missed_daily_if_needed
fi

while true; do
  hour="$(config_value daily.run_hour)"
  minute="$(config_value daily.run_minute)"
  target="$(date -d "today ${hour}:${minute}:00" +%s)"
  now="$(date +%s)"

  if [ "${now}" -ge "${target}" ]; then
    target="$(date -d "tomorrow ${hour}:${minute}:00" +%s)"
  fi

  sleep_seconds=$((target - now))
  next_run="$(date -d "@${target}" "+%Y-%m-%d %H:%M:%S %Z")"
  echo "[scheduler] next run at ${next_run}"

  while [ "${sleep_seconds}" -gt 0 ]; do
    poll_ready_batches
    interval="$(poll_interval_seconds)"
    if [ "${interval}" -gt "${sleep_seconds}" ]; then
      interval="${sleep_seconds}"
    fi
    sleep "${interval}"
    now="$(date +%s)"
    sleep_seconds=$((target - now))
  done

  run_pipeline
done
