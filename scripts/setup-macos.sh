#!/bin/bash
set -euo pipefail
if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "Run this setup on the MacBook." >&2
  exit 1
fi
project_dir="$(cd "$(dirname "$0")/.." && pwd)"
cd "$project_dir"
python_bin="${HOMEWARD_PYTHON:-}"
if [[ -z "$python_bin" ]] && command -v python3.12 >/dev/null 2>&1; then
  python_bin="$(command -v python3.12)"
elif [[ -z "$python_bin" ]] && command -v brew >/dev/null 2>&1 && [[ -x "$(brew --prefix)/opt/python@3.12/bin/python3.12" ]]; then
  python_bin="$(brew --prefix)/opt/python@3.12/bin/python3.12"
elif [[ -z "$python_bin" ]] && command -v python3 >/dev/null 2>&1; then
  python_bin="$(command -v python3)"
fi
if [[ -z "$python_bin" ]]; then
  echo "Install Python 3.12, or run bash scripts/install-macos-tools.sh with Homebrew installed." >&2
  exit 1
fi
"$python_bin" -c 'import sys; assert sys.version_info >= (3,11), "Python 3.11+ required"'
"$python_bin" -m venv .venv
.venv/bin/python -m pip install uv
.venv/bin/python -m uv sync --frozen --extra test
if [[ ! -f .env ]]; then cp .env.example .env; fi
echo "Setup complete. Run bash scripts/start-macos.sh to install/start the login services."
