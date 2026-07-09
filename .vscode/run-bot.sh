#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
"${BASH_SOURCE%/*}/prepare-dev.sh"
set -a
source .env
set +a
export PYTHONPATH="${PWD}/src"
export PATH="${HOME}/.local/bin:${PATH}"
exec /opt/cursor-linux-tg-bot/.venv/bin/python run.py -c config.yaml -v "$@"
