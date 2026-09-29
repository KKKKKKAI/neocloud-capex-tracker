#!/usr/bin/env bash
# Open the admin panel: forward localhost:8081 to the server's
# 127.0.0.1:8081 over SSH, then browse http://localhost:8081 (from a
# Windows browser too: WSL shares localhost with Windows). The SSH key is
# the only way in; the panel itself has no password.
#
#   scripts/admin_tunnel.sh [ssh-host]    # default host alias: capex
#
# The `capex` alias comes from scripts/ssh_config.example.
set -euo pipefail

host="${1:-capex}"
port="${CAPEX_ADMIN_PORT:-8081}"

if (exec 3<>"/dev/tcp/127.0.0.1/${port}") 2>/dev/null; then
  echo "localhost:${port} is already in use (another tunnel still open?)" >&2
  exit 1
fi
echo "admin panel: http://localhost:${port}   (Ctrl-C closes the tunnel)"
exec ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 \
  -L "${port}:127.0.0.1:${port}" "${host}"
