#!/bin/bash
set -euo pipefail
project_dir="$(cd "$(dirname "$0")/.." && pwd)"
"$project_dir/.venv/bin/python" "$project_dir/scripts/macos-services.py" uninstall
