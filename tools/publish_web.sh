#!/usr/bin/env sh
set -eu

cd "$(dirname "$0")/.."

if command -v docker >/dev/null 2>&1; then
  docker compose -f docker/docker-compose.yml run --rm arxiv-summary python main.py --step export-web
else
  python main.py --step export-web
fi

if git diff --quiet -- web/data/site-data.json; then
  echo "[publish-web] no web data changes"
  exit 0
fi

git add web/data/site-data.json
git commit -m "Update arXiv web data $(date +%F)"
git push origin HEAD
