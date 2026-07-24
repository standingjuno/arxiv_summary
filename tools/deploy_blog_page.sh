#!/usr/bin/env sh
set -eu

cd "$(dirname "$0")/.."

BLOG_REPO_URL="${BLOG_REPO_URL:-git@github.com:standingjuno/standingjuno.github.io.git}"
BLOG_BRANCH="${BLOG_BRANCH:-main}"
BLOG_SUBDIR="${BLOG_SUBDIR:-paper/arxiv_summary}"
COMMIT_MESSAGE="${COMMIT_MESSAGE:-Update arxiv_summary page $(date +%F)}"
GIT_USER_NAME="${GIT_USER_NAME:-standingjuno}"
GIT_USER_EMAIL="${GIT_USER_EMAIL:-standingjuno@users.noreply.github.com}"

tmpdir="$(mktemp -d)"
cleanup() {
  rm -rf "$tmpdir"
}
trap cleanup EXIT

echo "[deploy-blog-page] clone $BLOG_REPO_URL"
git clone --branch "$BLOG_BRANCH" "$BLOG_REPO_URL" "$tmpdir/site"

mkdir -p "$tmpdir/site/$BLOG_SUBDIR"
rsync -a --delete \
  --exclude '.DS_Store' \
  web/ "$tmpdir/site/$BLOG_SUBDIR/"

git -C "$tmpdir/site" config user.name "$GIT_USER_NAME"
git -C "$tmpdir/site" config user.email "$GIT_USER_EMAIL"

if git -C "$tmpdir/site" diff --quiet -- "$BLOG_SUBDIR"; then
  echo "[deploy-blog-page] no changes"
  exit 0
fi

git -C "$tmpdir/site" add "$BLOG_SUBDIR"
git -C "$tmpdir/site" commit -m "$COMMIT_MESSAGE"
git -C "$tmpdir/site" push origin "$BLOG_BRANCH"

echo "[deploy-blog-page] deployed -> https://standingjuno.github.io/$BLOG_SUBDIR/"
