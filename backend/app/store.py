"""Tiny JSON-file store for workspace preferences. Swap for a real DB later."""

import json
import threading
from pathlib import Path

from pydantic import BaseModel

from .config import settings


class WorkspaceSettings(BaseModel):
    workspace_name: str = "Helix Research"
    organization: str = ""
    contact_email: str = ""
    ollama_base_url: str = "http://127.0.0.1:11434"
    default_model: str = ""
    reduce_motion: bool = False


class SettingsStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def load(self) -> WorkspaceSettings:
        if not self.path.exists():
            return WorkspaceSettings()
        try:
            return WorkspaceSettings.model_validate_json(self.path.read_text("utf-8"))
        except ValueError:
            return WorkspaceSettings()

    def save(self, value: WorkspaceSettings) -> WorkspaceSettings:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(value.model_dump(), indent=2), "utf-8")
            tmp.replace(self.path)
        return value


store = SettingsStore(settings.data_dir / "settings.json")
