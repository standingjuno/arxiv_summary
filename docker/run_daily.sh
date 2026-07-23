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

if [ "$(config_value daily.run_on_start)" = "true" ]; then
  run_pipeline
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
  sleep "${sleep_seconds}"

  run_pipeline
done
