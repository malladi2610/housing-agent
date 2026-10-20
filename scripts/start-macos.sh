#!/bin/bash
set -euo pipefail
project_dir="$(cd "$(dirname "$0")/.." && pwd)"
if [[ ! -x "$project_dir/.venv/bin/python" ]]; then echo "Run bash scripts/setup-macos.sh first." >&2; exit 1; fi
"$project_dir/.venv/bin/python" "$project_dir/scripts/macos-services.py" install
