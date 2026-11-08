"""Small path helpers."""
from __future__ import annotations

import os


def _norm(path) -> str:
    return os.path.normcase(os.path.realpath(os.fspath(path)))


def is_inside(path, folder) -> bool:
    """True if `path` is `folder` itself or lies anywhere below it (case-insensitive on Windows,
    symlinks resolved)."""
    a, b = _norm(path), _norm(folder)
    return a == b or a.startswith(b.rstrip(os.sep) + os.sep)
