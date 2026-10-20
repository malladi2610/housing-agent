import importlib.util
import plistlib
from pathlib import Path


def module():
    path = Path(__file__).resolve().parents[1] / "scripts/macos-services.py"
    spec = importlib.util.spec_from_file_location("homeward_macos_services", path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def test_launchd_plists_preserve_spaces_and_keep_raw_appium_on_loopback(tmp_path):
    root = tmp_path / "Housing agent with spaces"
    tools = {"node": "/opt/homebrew/opt/node@22/bin/node", "appium_bin": str(root / "data/tools/node_modules/.bin/appium"), "JAVA_HOME": "/path to Java", "ANDROID_HOME": "/path to SDK", "APPIUM_HOME": str(root / "data/appium"), "PATH": "/sdk/platform-tools:/opt/homebrew/bin:/usr/bin:/bin"}
    definitions = module().service_definitions(root, tools)
    for role, definition in definitions.items():
        decoded = plistlib.loads(plistlib.dumps(definition))
        assert decoded["WorkingDirectory"] == str(root.resolve())
        assert decoded["KeepAlive"] and decoded["RunAtLoad"]
        assert "OLLAMA_API_KEY" not in decoded["EnvironmentVariables"]
        assert not any(s in decoded["ProgramArguments"] for s in ["sh", "bash", "-c"])
    assert definitions["worker"]["ProgramArguments"][:3] == ["/usr/bin/caffeinate", "-i", str(root / ".venv/bin/python")]
    assert definitions["appium"]["ProgramArguments"][-4:] == ["--address", "127.0.0.1", "--port", "4723"]
    assert definitions["worker"]["EnvironmentVariables"]["ANDROID_HOME"] == "/path to SDK"


def test_tracker_only_launchd_setup_does_not_pretend_appium_is_installed(tmp_path):
    definitions = module().service_definitions(tmp_path)
    assert set(definitions) == {"web", "worker"}
