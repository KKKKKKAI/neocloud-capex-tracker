#!/usr/bin/env bash
# Bootstrap a local development environment.
set -euo pipefail

python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
pip install --upgrade pip
pip install -e ".[dev,fetch,read,extract,export]"
echo
echo "Done. Activate with: source .venv/bin/activate"
