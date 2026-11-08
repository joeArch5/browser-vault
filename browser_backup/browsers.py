"""Supported browsers and where their data lives (Windows)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .paths import is_inside

LOCAL = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
ROAMING = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming"))


@dataclass(frozen=True)
class BrowserSpec:
    key: str
    name: str
    default_root: Path
    processes: tuple[str, ...]      # executable names to detect / close
    os_bound: bool = True           # cookies/passwords encrypted with the Windows account
    app_bound: bool = False         # newer app-bound cookie encryption (Chrome, Edge)

    @property
    def root(self) -> Path:
        # BBM_ROOT_<KEY> overrides the location (used for testing).
        override = os.environ.get(f"BBM_ROOT_{self.key.upper()}")
        return Path(override) if override else self.default_root

    @property
    def installed(self) -> bool:
        return self.root.exists()


BROWSERS: list[BrowserSpec] = [
    BrowserSpec("brave", "Brave", LOCAL / "BraveSoftware/Brave-Browser/User Data", ("brave.exe",)),
    BrowserSpec("chrome", "Google Chrome", LOCAL / "Google/Chrome/User Data", ("chrome.exe",),
                app_bound=True),
    BrowserSpec("edge", "Microsoft Edge", LOCAL / "Microsoft/Edge/User Data", ("msedge.exe",),
                app_bound=True),
    BrowserSpec("vivaldi", "Vivaldi", LOCAL / "Vivaldi/User Data", ("vivaldi.exe",)),
    BrowserSpec("opera", "Opera", ROAMING / "Opera Software/Opera Stable", ("opera.exe",)),
    BrowserSpec("operagx", "Opera GX", ROAMING / "Opera Software/Opera GX Stable", ("opera.exe",)),
    BrowserSpec("firefox", "Mozilla Firefox", ROAMING / "Mozilla/Firefox", ("firefox.exe",),
                os_bound=False),
]


def browser_containing(path) -> BrowserSpec | None:
    """The browser whose data folder contains `path` (a backup stored there would be wiped)."""
    return next((b for b in BROWSERS if is_inside(path, b.root)), None)


def get(key: str) -> BrowserSpec:
    for b in BROWSERS:
        if b.key == key:
            return b
    raise KeyError(key)
