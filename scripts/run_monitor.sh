#!/usr/bin/env bash
# Daily watcher entry — invoked by cron. Catches up any earnings days
# that have been announced but not yet extracted. Idempotent.
#
# Cron line (installed by scripts/install_cron.sh):
#   0 18 * * 1-5  /path/to/repo/scripts/run_monitor.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

LOG_DIR="$REPO_ROOT/data/_logs"
LOCK="$LOG_DIR/monitor.lock"
LOG="$LOG_DIR/monitor.log"
mkdir -p "$LOG_DIR"

# Load .env if present so ALPHA_VANTAGE_API_KEY etc. are visible.
if [ -f "$REPO_ROOT/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$REPO_ROOT/.env"
  set +a
fi

export PYTHONPATH="$REPO_ROOT/src"
export PATH="$HOME/.local/bin:$PATH"   # ensure `claude` CLI is on PATH for cron

# flock prevents a manual run + cron run from overlapping.
exec 9> "$LOCK"
if ! flock -n 9; then
  echo "[$(date -Is)] another monitor run holds $LOCK; skipping" >> "$LOG"
  exit 0
fi

{
  echo "==================================================================="
  echo "[$(date -Is)] capex monitor --catch-up"
  echo "==================================================================="
  python3 -m capex.monitor.run --catch-up
  echo
} >> "$LOG" 2>&1
