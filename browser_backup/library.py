"""Backups library: finds backups in a folder and keeps a small unencrypted sidecar
(`<backup>.meta.json`) with non-sensitive display info (browser, date, name, file count).

The sidecar is only for the list. Restoring always trusts the encrypted contents, never the sidecar.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .browsers import BROWSERS, BrowserSpec
from .crypto import MAGIC

SUFFIX = ".bbvault"                 # Browser Backup Vault
META_SUFFIX = ".meta.json"


@dataclass
class BackupEntry:
    path: Path
    browser_key: str | None
    label: str
    created: datetime
    size: int
    files: int | None

    @property
    def meta_path(self) -> Path:
        return meta_path(self.path)


def meta_path(path: Path) -> Path:
    return path.with_name(path.name + META_SUFFIX)


def new_backup_path(folder: Path, spec: BrowserSpec) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    path = folder / f"{spec.key}_{stamp}{SUFFIX}"
    n = 2
    while path.exists():
        path = folder / f"{spec.key}_{stamp}_{n}{SUFFIX}"
        n += 1
    return path


def write_meta(path: Path, browser_key: str, label: str, files: int) -> None:
    data = {
        "browser": browser_key,
        "label": label.strip()[:60],
        "created": datetime.now(timezone.utc).isoformat(),
        "files": files,
    }
    try:
        meta_path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass  # list info is optional


def _is_backup(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            return fh.read(4) == MAGIC
    except OSError:
        return False


def entry_for(path: Path) -> BackupEntry:
    """Build an entry for one file. Raises ValueError if it is not a backup file."""
    if not _is_backup(path):
        raise ValueError("This is not a valid Browser Backup Manager file.")
    stat = path.stat()
    browser_key, label, files = None, "", None
    created = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
    try:
        meta = json.loads(meta_path(path).read_text(encoding="utf-8"))
        browser_key = meta.get("browser")
        label = str(meta.get("label") or "")
        files = meta.get("files")
        created = datetime.fromisoformat(meta["created"])
        if created.tzinfo is None:  # keep every date timezone-aware so sorting never fails
            created = created.replace(tzinfo=timezone.utc)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        prefix = path.name.split("_", 1)[0]  # fall back to the file name we generate
        if any(b.key == prefix for b in BROWSERS):
            browser_key = prefix
    if browser_key not in {b.key for b in BROWSERS}:
        browser_key = None
    return BackupEntry(path, browser_key, label, created, stat.st_size,
                       files if isinstance(files, int) else None)


def list_backups(folder: Path) -> list[BackupEntry]:
    entries = []
    try:
        candidates = [p for p in folder.iterdir() if p.is_file() and p.name.endswith(SUFFIX)]
    except OSError:
        return []
    for p in candidates:
        try:
            entries.append(entry_for(p))
        except (ValueError, OSError):
            continue
    entries.sort(key=lambda e: e.created, reverse=True)
    return entries


def delete_backup(entry: BackupEntry) -> None:
    entry.path.unlink()
    entry.meta_path.unlink(missing_ok=True)
