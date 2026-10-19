from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="FLAT_", extra="ignore")

    data_dir: Path = Path("data")
    host: str = "127.0.0.1"
    port: int = 8000
    ui_password: str = ""
    ollama_api_key: str = Field(default="", validation_alias="OLLAMA_API_KEY", repr=False)
    imap_host: str = ""
    imap_port: int = 993
    imap_user: str = ""
    imap_password: str = Field(default="", repr=False)
    imap_folder: str = "INBOX"
    imap_senders: str = ""
    imap_poll_seconds: int = 180
    appium_url: str = "http://127.0.0.1:4723"

    def prepare(self) -> None:
        self.data_dir = self.data_dir.resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.data_dir.chmod(0o700)
        except OSError:
            pass

    @property
    def database_path(self) -> Path:
        return self.data_dir / "tracker.sqlite3"

    def local_secrets(self) -> dict:
        path = self.data_dir / "secrets.json"
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def secret(self, name: str) -> str:
        if name == "OLLAMA_API_KEY" and self.ollama_api_key:
            return self.ollama_api_key
        return str(self.local_secrets().get(name, ""))

    def save_secret(self, name: str, value: str) -> None:
        if name != "OLLAMA_API_KEY":
            raise ValueError("Unsupported secret")
        if "\n" in value or "\r" in value or len(value) > 4096:
            raise ValueError("Invalid key format")
        content = self.local_secrets()
        content[name] = value.strip()
        destination = self.data_dir / "secrets.json"
        temporary = self.data_dir / (".secrets-" + secrets.token_hex(8))
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(content, stream)
        os.replace(temporary, destination)

    def csrf_token(self) -> str:
        path = self.data_dir / ".csrf"
        if not path.exists():
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w") as stream:
                    stream.write(secrets.token_urlsafe(40))
            except FileExistsError:
                pass
        return path.read_text().strip()
