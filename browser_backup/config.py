"""Small persisted settings file (backups folder)."""
from __future__ import annotations

import json
import os
from pathlib import Path

from .paths import is_inside

CLOUD_FOLDERS = (("onedrive", "OneDrive"), ("dropbox", "Dropbox"), ("google drive", "Google Drive"),
                 ("googledrive", "Google Drive"), ("icloud", "iCloud"), ("mega", "MEGA"),
                 ("pcloud", "pCloud"), ("sync.com", "Sync.com"))


def config_dir() -> Path:
    override = os.environ.get("BBM_CONFIG_DIR")
    if override:
        return Path(override)
    roaming = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming"))
    return roaming / "BrowserBackupManager"


def default_backup_dir() -> Path:
    return Path.home() / "Documents" / "Browser Backups"


def cloud_sync_name(folder: Path) -> str | None:
    """Name of the cloud service that (probably) syncs `folder`, or None."""
    for var in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        root = os.environ.get(var)
        if root and is_inside(folder, root):
            return "OneDrive"
    for part in Path(folder).parts:
        low = part.lower()
        for key, name in CLOUD_FOLDERS:
            if low.startswith(key):
                return name
    return None


def _file() -> Path:
    return config_dir() / "config.json"


def load() -> dict:
    try:
        data = json.loads(_file().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save(data: dict, strict: bool = False) -> None:
    """Atomic write. Errors are swallowed (settings are a convenience; never block a backup
    because of them) unless strict=True, which is used for security-relevant settings."""
    try:
        config_dir().mkdir(parents=True, exist_ok=True)
        tmp = _file().with_name("config.json.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, _file())
    except OSError:
        if strict:
            raise


def get_backup_dir() -> Path:
    raw = load().get("backup_dir")
    folder = Path(raw) if raw else default_backup_dir()
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        folder = default_backup_dir()
        folder.mkdir(parents=True, exist_ok=True)
    return folder


def set_backup_dir(folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    data = load()
    data["backup_dir"] = str(folder)
    save(data, strict=True)
