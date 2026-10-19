"""Local device inspection. Sending remains a separate, unverified capability."""
from __future__ import annotations

import base64
import json
import shutil
import subprocess
import uuid
from pathlib import Path
from urllib.parse import urlparse

import httpx


class DesktopInspector:
    def status(self):
        binary = shutil.which("cua-driver")
        if not binary:
            return "not_installed", "Cua Driver is not installed in this desktop. Install it inside the VM during the Windows pilot."
        try:
            result = subprocess.run([binary,"call","list_apps","{}"],capture_output=True,text=True,timeout=20,check=False)
            if result.returncode:
                return "error", "Cua is installed but cannot inspect the desktop. Check its permissions and interactive session."
            json.loads(result.stdout)
            return "ready", "Cua can list desktop apps. This does not verify website navigation or sending."
        except (subprocess.TimeoutExpired, OSError, ValueError):
            return "error", "Cua inspection failed or timed out; check the local desktop session."


class AndroidInspector:
    def __init__(self, base_url: str):
        parsed = urlparse(base_url)
        # An administrator may configure a VM-private bridge, never a public URL.
        import ipaddress
        hostname = parsed.hostname or ""
        allowed = hostname in {"localhost","host.docker.internal"}
        try:
            address = ipaddress.ip_address(hostname)
            allowed = address.is_loopback or (address.is_private and not address.is_link_local)
        except ValueError:
            pass
        if not allowed or parsed.scheme not in {"http","https"} or parsed.username or parsed.password:
            raise ValueError("Appium must use a loopback or explicit private IP address")
        self.base_url = base_url.rstrip("/")

    def status(self):
        try:
            with httpx.Client(timeout=5,trust_env=False) as client:
                response = client.get(self.base_url+"/status")
                response.raise_for_status()
                payload = response.json().get("value",{})
            if payload.get("ready"):
                return "ready", "Appium is reachable. App installation, sign-in and delivery are not yet verified."
            return "error", "Appium responded but is not ready."
        except (httpx.HTTPError, ValueError):
            return "not_connected", "Appium is not reachable at the configured private endpoint."

    def capture(self, session_id: str, folder: Path) -> dict:
        if not session_id or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_." for ch in session_id):
            raise ValueError("Provide the ID of an existing Appium inspection session")
        try:
            with httpx.Client(timeout=20,trust_env=False) as client:
                source = client.get(f"{self.base_url}/session/{session_id}/source")
                source.raise_for_status()
                screenshot = client.get(f"{self.base_url}/session/{session_id}/screenshot")
                screenshot.raise_for_status()
                xml = source.json()["value"]
                image = base64.b64decode(screenshot.json()["value"],validate=True)
            if not image.startswith(b"\x89PNG\r\n\x1a\n") or len(image)>5_000_000 or not isinstance(xml,str):
                raise ValueError("Unsupported screenshot")
            folder.mkdir(parents=True,exist_ok=True)
            identifier = uuid.uuid4().hex
            (folder/f"{identifier}.png").write_bytes(image)
            (folder/f"{identifier}.xml").write_text(xml[:500000],encoding="utf-8")
            return {"id":identifier,"bytes":len(image),"source_length":len(xml)}
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            raise ValueError("Could not capture the existing Appium session. Check its device connection and permissions.") from None


class PlatformSender:
    """Explicit boundary: no platform has a verified sender in this release."""
    def send(self, *args, **kwargs):
        raise NotImplementedError("Live platform sending is unavailable. Send on the platform and record the exact message manually.")
