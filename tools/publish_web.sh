#!/usr/bin/env sh
set -eu

cd "$(dirname "$0")/.."

if command -v docker >/dev/null 2>&1; then
  docker compose -f docker/docker-compose.yml run --rm arxiv-summary python main.py --step export-web
else
  python main.py --step export-web
fi

./tools/deploy_blog_page.sh
