#!/usr/bin/env bash
# Weekly Alpha Vantage sync — invoked by cron. Refreshes the
# fiscal_calendar table with the next 3 months of earnings dates.
# Cheap (one HTTP call), idempotent (UPSERT on ticker+fiscal_date).
#
# Manual runs from WSL only. Scheduling moves to the always-on server
# (docs/SERVER_MIGRATION_CHECKLIST.md); this wrapper retires at go-live.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

LOG_DIR="$REPO_ROOT/data/_logs"
LOG="$LOG_DIR/monitor.log"
mkdir -p "$LOG_DIR"

if [ -f "$REPO_ROOT/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$REPO_ROOT/.env"
  set +a
fi

export PYTHONPATH="$REPO_ROOT/src"

{
  echo "==================================================================="
  echo "[$(date -Is)] capex calendar sync (Alpha Vantage)"
  echo "==================================================================="
  python3 -m capex.monitor.calendar
  echo
} >> "$LOG" 2>&1
