#!/usr/bin/env bash
# One-shot helper: turn this folder into a git working copy of the remote
# repo, stage the scaffold, commit, and push. Run this ONCE, from inside
# this folder, from your local terminal where git auth to GitHub works.
#
#   cd path/to/neocloud-capex-tracker
#   bash scripts/initial_push.sh
#
# The script is idempotent and safe to re-run if the push fails on auth.
set -euo pipefail

REMOTE_URL="https://github.com/KKKKKKAI/neocloud-capex-tracker.git"

if [ ! -d .git ]; then
  echo "Initializing git repo in $(pwd)"
  git init -b main
  git remote add origin "$REMOTE_URL"
  git fetch origin main
  # Reset the index to match the remote's initial README commit without
  # touching files on disk, so our scaffold appears as staged changes on
  # top of the remote history.
  git reset --mixed FETCH_HEAD
fi

git add -A
git status

cat <<'EOF'

If the status above looks right, run:

    git commit -m "scaffold: initial framework shell from design memo v0.5"
    git push -u origin main

EOF
