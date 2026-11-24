#!/usr/bin/env bash
# jra-archiveブランチ（データ専用）をDIRに用意する。無ければ空のブランチを作る。
set -euo pipefail
dir="$1"
git config --global user.name "github-actions[bot]"
git config --global user.email "github-actions[bot]@users.noreply.github.com"
if git fetch --depth 1 origin jra-archive; then
  git worktree add -B jra-archive "$dir" FETCH_HEAD
else
  git worktree add --orphan -b jra-archive "$dir"
fi
