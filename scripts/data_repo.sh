#!/usr/bin/env bash
# Clone or push the PRIVATE data repo that holds the outage log.
# usage: scripts/data_repo.sh clone|push "<commit message>"
# needs env DATA_REPO_TOKEN and DATA_REPO (owner/name)
set -euo pipefail
url="https://x-access-token:${DATA_REPO_TOKEN}@github.com/${DATA_REPO}.git"

if [ "$1" = clone ]; then
  git clone -q --depth 1 "$url" data 2>/dev/null || { echo "::error::cannot clone ${DATA_REPO}; check DATA_REPO_TOKEN"; exit 1; }
  git -C data config user.name "outage-poller"
  git -C data config user.email "outage-poller@users.noreply.github.com"
  exit 0
fi

cd data
git add -A
git diff --cached --quiet && { echo "nothing to commit"; exit 0; }
git commit -q -m "$2"
for i in 1 2 3 4; do
  if git ls-remote --exit-code -q origin main >/dev/null 2>&1; then
    git pull -q --rebase origin main || { git rebase --abort || true; sleep $((i * 5)); continue; }
  fi
  git push -q origin HEAD:main && exit 0
  sleep $((i * 5))
done
echo "::error::push to ${DATA_REPO} failed after 4 tries"; exit 1
