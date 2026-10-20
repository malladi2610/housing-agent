#!/bin/bash
# Uses an existing Homebrew installation. No sudo, remote shell script or global npm install.
set -euo pipefail
if [[ "$(uname -s)" != "Darwin" ]]; then echo "Run this on macOS." >&2; exit 1; fi
if ! command -v brew >/dev/null 2>&1; then
  echo "Install Homebrew using https://brew.sh, then rerun this script." >&2
  exit 1
fi
project_dir="$(cd "$(dirname "$0")/.." && pwd)"
cd "$project_dir"
brew install python@3.12 node@22 openjdk@17
brew install --cask android-commandlinetools
python_bin="$(brew --prefix python@3.12)/bin/python3.12"
node_bin="$(brew --prefix node@22)/bin"
export JAVA_HOME="$(brew --prefix openjdk@17)/libexec/openjdk.jdk/Contents/Home"
export ANDROID_HOME="${ANDROID_HOME:-$HOME/Library/Android/sdk}"
export APPIUM_HOME="$project_dir/data/appium"
export PATH="$node_bin:$ANDROID_HOME/platform-tools:$PATH"
mkdir -p "$project_dir/data/tools" "$ANDROID_HOME"
echo "Review the Android SDK licenses when prompted."
sdkmanager --sdk_root="$ANDROID_HOME" --licenses
sdkmanager --sdk_root="$ANDROID_HOME" "platform-tools" "platforms;android-35" "build-tools;35.0.0"
npm install --prefix "$project_dir/data/tools" appium@3
appium_bin="$project_dir/data/tools/node_modules/.bin/appium"
if ! "$appium_bin" driver list --installed --json | "$python_bin" -c 'import sys,json; raise SystemExit(0 if "uiautomator2" in json.load(sys.stdin) else 1)'; then
  "$appium_bin" driver install uiautomator2
fi
"$appium_bin" driver doctor uiautomator2
"$python_bin" - "$project_dir/data/macos-tools.json" "$node_bin/node" "$appium_bin" "$JAVA_HOME" "$ANDROID_HOME" "$APPIUM_HOME" "$PATH" <<'PY'
import json,os,sys
from pathlib import Path
path=Path(sys.argv[1])
path.parent.mkdir(parents=True,exist_ok=True)
path.parent.chmod(0o700)
values=dict(zip(('node','appium_bin','JAVA_HOME','ANDROID_HOME','APPIUM_HOME','PATH'),sys.argv[2:]))
fd=os.open(path,os.O_CREAT|os.O_TRUNC|os.O_WRONLY,0o600)
with os.fdopen(fd,'w') as stream: json.dump(values,stream)
PY
echo "Android tooling is ready. Connect the phone by USB, enable USB debugging and approve this Mac on the phone."
echo "Next: bash scripts/setup-macos.sh, then bash scripts/start-macos.sh."
