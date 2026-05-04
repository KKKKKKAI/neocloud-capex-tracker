#!/usr/bin/env bash
# Idempotent crontab installer for the capex monitor.
# Reads existing crontab, appends our two lines if absent (matched by
# the `# capex-monitor` marker), shows a diff, asks for confirmation
# before applying. Safe to re-run.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MARKER="# capex-monitor"

WEEKLY_LINE="0 8 * * 0  $REPO_ROOT/scripts/sync_calendar.sh  $MARKER weekly"
DAILY_LINE="0 18 * * 1-5  $REPO_ROOT/scripts/run_monitor.sh  $MARKER daily"

CURRENT="$(crontab -l 2>/dev/null || true)"

if echo "$CURRENT" | grep -qF "$MARKER"; then
  echo "capex-monitor cron entries already present:"
  echo "$CURRENT" | grep -F "$MARKER" | sed 's/^/  /'
  echo
  echo "(re-run after editing the lines manually if you need to change the cadence)"
  exit 0
fi

NEW="$CURRENT"
if [ -n "$NEW" ]; then NEW="$NEW"$'\n'; fi
NEW="${NEW}${WEEKLY_LINE}"$'\n'"${DAILY_LINE}"$'\n'

echo "----- Proposed additions to your crontab -----"
echo "$WEEKLY_LINE"
echo "$DAILY_LINE"
echo "-----------------------------------------------"
echo
read -r -p "Install these into your crontab? [y/N] " ans
case "$ans" in
  y|Y|yes|YES) ;;
  *) echo "aborted."; exit 1 ;;
esac

echo "$NEW" | crontab -
echo "installed. verify with:  crontab -l | grep capex-monitor"

if ! command -v cron >/dev/null 2>&1 && ! pgrep -x cron >/dev/null 2>&1; then
  echo
  echo "warning: cron daemon does not appear to be running."
  echo "in WSL you may need:  sudo service cron start"
fi
