#!/usr/bin/env bash
# Phase 2 smoke test. Run on the server:
#     sudo bash /opt/capex/current/deploy/smoke_test.sh
# Checks the whole path the tracker depends on (claude with the SSM
# token, SEC, Alpha Vantage, Gmail, S3 -> CloudFront, memory, disk, data
# volume) as the capex user, with the same environment the services get.
# Also seeds a placeholder index.html so the public URL answers.
set -euo pipefail

if ! systemctl is-active --quiet capex-secrets.service; then
  echo "capex-secrets.service is not active:" >&2
  journalctl -u capex-secrets.service -n 20 --no-pager >&2 || true
  exit 1
fi

exec sudo -u capex -H bash -c '
  set -a
  . /etc/capex/capex.conf
  . /run/capex/capex.env
  set +a
  cd "$CAPEX_HOME"
  exec /opt/capex/current/.venv/bin/python -m capex.server.doctor --placeholder
'
