"""Tiny JSON-file stores for workspace preferences and per-integration config.
Swap for a real DB later."""

import json
import threading
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from .config import settings

M = TypeVar("M", bound=BaseModel)


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), "utf-8")
    tmp.replace(path)


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


class WorkspaceSettings(BaseModel):
    workspace_name: str = "Helix Research"
    organization: str = ""
    contact_email: str = ""
    default_model: str = ""
    reduce_motion: bool = False


class SettingsStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def load(self) -> WorkspaceSettings:
        try:
            return WorkspaceSettings.model_validate(_read_json(self.path))
        except ValueError:
            return WorkspaceSettings()

    def raw(self) -> dict:
        """Unvalidated file contents, used to migrate fields that moved elsewhere."""
        return _read_json(self.path)

    def save(self, value: WorkspaceSettings) -> WorkspaceSettings:
        with self._lock:
            _write_json(self.path, value.model_dump())
        return value


class IntegrationStore:
    """One JSON file holding each integration's config under its id."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def load(self, key: str, model: type[M], fallback: dict | None = None) -> M:
        data = _read_json(self.path).get(key)
        if data is None:
            data = fallback or {}
        try:
            return model.model_validate(data)
        except ValueError:
            return model()

    def save(self, key: str, value: BaseModel) -> None:
        with self._lock:
            data = _read_json(self.path)
            data[key] = value.model_dump()
            _write_json(self.path, data)


store = SettingsStore(settings.data_dir / "settings.json")
integration_store = IntegrationStore(settings.data_dir / "integrations.json")
