#!/usr/bin/env bash
# /usr/local/bin/capex on the server: the live release's CLI, run as the
# capex user with the services' environment, so on the host
#     capex server jobs        capex llm ping        capex server health
# behave exactly like the scheduler's jobs. Installed by bootstrap.sh and
# refreshed by capex-deploy.
set -euo pipefail

if [ "$(id -un)" != capex ]; then
  exec sudo -u capex -H "$0" "$@"
fi
set -a
# shellcheck disable=SC1091
. /etc/capex/capex.conf
if [ -r /run/capex/capex.env ]; then
  # shellcheck disable=SC1091
  . /run/capex/capex.env
fi
set +a
cd "${CAPEX_HOME:-/var/lib/capex}"
exec /opt/capex/current/.venv/bin/capex "$@"
