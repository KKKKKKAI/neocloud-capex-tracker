#!/usr/bin/env bash
# Set up or refresh the WSL development environment from uv.lock:
# uv, Python 3.12, and a venv (default ~/.venvs/capex, on the Linux file
# system) with every extra, the package installed editable.
#
#   scripts/dev_setup.sh
#   source ~/.venvs/capex/bin/activate
#
# Same locked versions the server runs (plus the dev tools). After a
# dependency change in pyproject.toml, run `uv lock` and commit uv.lock.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
if command -v uv >/dev/null 2>&1; then
  uv=$(command -v uv)
elif [ -x "$HOME/.local/bin/uv" ]; then
  uv=$HOME/.local/bin/uv
else
  curl -LsSf https://astral.sh/uv/install.sh | sh
  uv=$HOME/.local/bin/uv
fi

export UV_PROJECT_ENVIRONMENT=${CAPEX_VENV:-$HOME/.venvs/capex}
"$uv" sync --frozen --all-extras --python 3.12
echo "ready: source $UV_PROJECT_ENVIRONMENT/bin/activate"
