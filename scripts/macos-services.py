"""User launchd services, including Appium and an idle-sleep assertion for the worker.

Plist generation is pure Python and tested on CI. launchctl runs only on macOS.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import plistlib
import re
import subprocess
from pathlib import Path

PREFIX = "com.homeward.housing-agent"


def service_definitions(root: Path, tools=None):
    root = root.resolve()
    python = str(root / ".venv/bin/python")
    data = root / "data"
    tools = tools or {}
    # Explicit absolute paths work in launchd's small environment and with spaces.
    inherited_path = str(tools.get("PATH", "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"))
    environment = {"PATH": inherited_path, "PYTHONUNBUFFERED": "1"}
    for key in ("JAVA_HOME", "ANDROID_HOME", "APPIUM_HOME"):
        if tools.get(key):
            environment[key] = str(tools[key])
    definitions = {}
    for role in ("web", "worker"):
        arguments = [python, "-m", "flat_agent.cli", role]
        if role == "worker":
            arguments = ["/usr/bin/caffeinate", "-i", *arguments]
        definitions[role] = {"Label": PREFIX + "." + role, "ProgramArguments": arguments, "WorkingDirectory": str(root), "EnvironmentVariables": environment.copy(), "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 15, "ExitTimeOut": 300, "StandardOutPath": str(data / f"{role}.stdout.log"), "StandardErrorPath": str(data / f"{role}.stderr.log")}
    if tools.get("node") and tools.get("appium_bin"):
        definitions["appium"] = {**definitions["web"], "Label": PREFIX + ".appium", "ProgramArguments": [str(tools["node"]), str(tools["appium_bin"]), "--address", "127.0.0.1", "--port", "4723"], "StandardOutPath": str(data / "appium.stdout.log"), "StandardErrorPath": str(data / "appium.stderr.log")}
    return definitions


def manage(action, root):
    if platform.system() != "Darwin":
        raise SystemExit("Run launchd management on the MacBook; no host settings were changed.")
    root = root.resolve()
    data = root / "data"
    data.mkdir(parents=True, exist_ok=True)
    data.chmod(0o700)
    tools_path = data / "macos-tools.json"
    tools = json.loads(tools_path.read_text()) if tools_path.exists() else {}
    definitions = service_definitions(root, tools)
    folder = Path.home() / "Library/LaunchAgents"
    domain = f"gui/{os.getuid()}"
    if action == "install":
        if not (root / ".venv/bin/python").is_file():
            raise SystemExit("Run bash scripts/setup-macos.sh first.")
        folder.mkdir(parents=True, exist_ok=True)
        # Restart only this application's own services. Existing data is preserved.
        for role, definition in definitions.items():
            path = folder / (definition["Label"] + ".plist")
            subprocess.run(["launchctl", "bootout", domain + "/" + definition["Label"]], capture_output=True, check=False)
            path.write_bytes(plistlib.dumps(definition))
            path.chmod(0o600)
            result = subprocess.run(["launchctl", "bootstrap", domain, str(path)], capture_output=True, text=True, check=False)
            if result.returncode:
                raise SystemExit(f"Could not start {role}. Check the local LaunchAgents permissions and data/{role}.stderr.log; run from your logged-in macOS terminal.")
            print(f"Started Homeward {role}; restarts after failure and at login.")
        print("Open http://127.0.0.1:8000 (or your configured FLAT_PORT). This does not run before macOS login.")
        if "appium" not in definitions:
            print("Appium is not configured: run bash scripts/install-macos-tools.sh for the USB phone pipeline.")
    elif action == "uninstall":
        for role in ("web", "worker", "appium"):
            label = PREFIX + "." + role
            subprocess.run(["launchctl", "bootout", domain + "/" + label], capture_output=True, check=False)
            (folder / (label + ".plist")).unlink(missing_ok=True)
        print("Homeward services stopped and login entries removed. Tracker data and credentials were preserved.")
    else:
        for role in ("web", "worker", "appium"):
            result = subprocess.run(["launchctl", "print", domain + "/" + PREFIX + "." + role], capture_output=True, text=True, check=False)
            pid = re.search(r"\bpid = (\d+)", result.stdout)
            print(f"{role}: " + (f"running (PID {pid[1]})" if pid else "not running; inspect its local stderr log"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["install", "uninstall", "status"])
    parser.add_argument("--project", type=Path, default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args()
    manage(args.action, args.project)
