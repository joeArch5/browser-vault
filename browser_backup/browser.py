"""Browser profile archive (streamed), wipe and restore."""
from __future__ import annotations

import getpass
import io
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Callable

from .browsers import BrowserSpec

Progress = Callable[[str], None]

MANIFEST = "__bbm_manifest__.json"   # first member: browser, user, PC
TRAILER = "__bbm_end__.json"         # last member: file count, proves the stream is complete

# Regenerable caches: skipped to keep the backup small. Only matched near the top of the
# tree (User Data/<x> or User Data/<profile>/<x>) so extension files are never skipped.
CACHE_DIRS = {
    "Cache", "Code Cache", "GPUCache", "GrShaderCache", "GraphiteDawnCache", "DawnCache",
    "DawnGraphiteCache", "DawnWebGPUCache", "ShaderCache", "Crashpad", "BrowserMetrics",
    "Crash Reports", "component_crx_cache", "extensions_crx_cache",
    "optimization_guide_model_store",
}
CACHE_NESTED = {("Service Worker", "CacheStorage"), ("Service Worker", "ScriptCache")}
SKIP_FILES_PREFIX = ("Singleton", "lockfile")


def find_root(spec: BrowserSpec) -> Path:
    if not spec.root.exists():
        raise FileNotFoundError(f"{spec.name} data folder not found:\n{spec.root}")
    return spec.root


# ---------------------------------------------------------------- process control
def _no_window() -> int:
    return 0x08000000 if sys.platform == "win32" else 0


def is_running(spec: BrowserSpec) -> bool:
    if sys.platform != "win32":
        return False
    out = subprocess.run(["tasklist", "/NH", "/FO", "CSV"], capture_output=True, text=True,
                         creationflags=_no_window()).stdout.lower()
    return any(f'"{p}"' in out for p in spec.processes)


def close_browser(spec: BrowserSpec, timeout: float = 8.0) -> bool:
    """Force-close all processes of the browser (including background ones)."""
    if sys.platform == "win32":
        for proc in spec.processes:
            subprocess.run(["taskkill", "/F", "/T", "/IM", proc],
                           capture_output=True, creationflags=_no_window())
    end = time.time() + timeout
    while time.time() < end:
        if not is_running(spec):
            return True
        time.sleep(0.5)
    return not is_running(spec)


# ---------------------------------------------------------------- archive (streamed)
def _skip_dir(rel: Path) -> bool:
    parts = rel.parts
    if parts[-1] in CACHE_DIRS and len(parts) <= 2:
        return True
    return len(parts) <= 3 and tuple(parts[-2:]) in CACHE_NESTED


def _add_bytes(tar: tarfile.TarFile, name: str, payload: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(payload)
    info.mtime = int(time.time())
    tar.addfile(info, io.BytesIO(payload))


def _plain(info: tarfile.TarInfo) -> tarfile.TarInfo:
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def create_profile_archive(spec: BrowserSpec, out, progress: Progress | None = None
                           ) -> tuple[int, list[str]]:
    """Stream the browser's whole data folder as tar.gz into the file-like `out`.

    `out` is normally an EncryptWriter, so nothing unencrypted is ever written to disk.
    Returns (file_count, unreadable_files).
    """
    root = find_root(spec)
    count, skipped = 0, []
    manifest = {
        "browser": spec.key,
        "created": datetime.now(timezone.utc).isoformat(),
        "windows_user": getpass.getuser(),
        "computer": platform.node(),
    }
    with tarfile.open(fileobj=out, mode="w|gz", compresslevel=6) as tar:
        _add_bytes(tar, MANIFEST, json.dumps(manifest, indent=2).encode())
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            here = Path(dirpath)
            rel_dir = here.relative_to(root)
            dirnames[:] = [d for d in dirnames
                           if not _skip_dir(rel_dir / d) and not (here / d).is_symlink()]
            if rel_dir.parts:
                tar.addfile(_plain(tar.gettarinfo(str(here), arcname=rel_dir.as_posix())))
            for name in filenames:
                if name.startswith(SKIP_FILES_PREFIX):
                    continue
                path = here / name
                if path.is_symlink():
                    continue
                arc = (rel_dir / name).as_posix()
                try:
                    # Open first: an unreadable (locked) file is skipped cleanly. Stat the open
                    # handle so the size cannot differ from what is actually read.
                    with path.open("rb") as fh:
                        info = _plain(tar.gettarinfo(arcname=arc, fileobj=fh))
                        try:
                            tar.addfile(info, fh)
                        except OSError as exc:
                            raise RuntimeError(
                                f"'{arc}' could not be archived ({exc}). Make sure {spec.name} "
                                "is fully closed and that the backup drive has enough free "
                                "space, then try again.") from exc
                except OSError:
                    skipped.append(arc)
                    continue
                count += 1
                if progress and count % 25 == 0:
                    progress(f"Archiving… {count} files")
        _add_bytes(tar, TRAILER, json.dumps({"file_count": count}).encode())
    return count, skipped


def _safe_name(name: str) -> str:
    member = Path(name)
    if member.is_absolute() or ".." in member.parts or ":" in name or name.startswith(("/", "\\")):
        raise ValueError("Backup contains an unsafe file path.")
    return name


def _read_member(tar: tarfile.TarFile, member: tarfile.TarInfo) -> IO[bytes]:
    """Open a regular archive member; anything else means the backup layout is damaged."""
    src = tar.extractfile(member)
    if src is None:
        raise ValueError("This backup has an unexpected layout.")
    return src


def verify_stream(reader, expected_files: int) -> None:
    """Read the whole backup stream and check it is complete and has the expected file count."""
    seen, trailer = 0, None
    with tarfile.open(fileobj=reader, mode="r|gz") as tar:
        for member in tar:
            if member.name == TRAILER:
                trailer = json.loads(_read_member(tar, member).read())
            elif member.isreg():
                src = _read_member(tar, member)
                while src.read(1024 * 1024):
                    pass
                if member.name != MANIFEST:
                    seen += 1
    reader.drain()  # authenticate every remaining chunk, including the last one
    if trailer is None or trailer.get("file_count") != expected_files or seen != expected_files:
        raise RuntimeError(f"Backup verification failed ({seen} of {expected_files} files).")


# ---------------------------------------------------------------- wipe
def _force_remove(func, path, _exc):
    os.chmod(path, stat.S_IWRITE)
    func(path)


def _rmtree(path: Path) -> None:
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_force_remove)
    else:
        shutil.rmtree(path, onerror=_force_remove)


def wipe_profile(spec: BrowserSpec, progress: Progress | None = None) -> None:
    """Delete everything in the browser's data folder so it starts like a fresh install."""
    root = find_root(spec)
    if is_running(spec):
        raise RuntimeError(f"{spec.name} is running. Close it before wiping.")
    failures = []
    for child in list(root.iterdir()):
        try:
            if child.is_dir() and not child.is_symlink():
                _rmtree(child)
            else:
                child.unlink()
        except OSError as exc:
            failures.append(f"{child.name}: {exc}")
        if progress:
            progress(f"Wiping… {child.name}")
    if failures:
        raise RuntimeError("Could not delete everything:\n" + "\n".join(failures[:8]))


# ---------------------------------------------------------------- restore
def _os_warning(spec: BrowserSpec, manifest: dict) -> str | None:
    notes = []
    if spec.os_bound and manifest and (manifest.get("windows_user") != getpass.getuser()
                                       or manifest.get("computer") != platform.node()):
        notes.append("This backup was made by a different Windows user/PC. Cookies and saved "
                     "passwords are bound to the original Windows account, so those sites may "
                     "ask you to log in again.")
    if spec.app_bound:
        notes.append(f"{spec.name} protects cookies with app-bound encryption. After a restore "
                     "some sites may still ask you to sign in again; check a few important "
                     "accounts before you delete anything.")
    return "\n\n".join(notes) or None


def _check_browser(spec: BrowserSpec, manifest: dict) -> None:
    source = manifest.get("browser")
    if source != spec.key:
        raise ValueError(f"This backup was made from a different browser ('{source}'), "
                         f"not {spec.name}. Select the matching browser in the list.")


def _staging_for(spec: BrowserSpec) -> Path:
    staging = spec.root.parent / (spec.root.name + ".restoring")
    if staging.exists():
        _rmtree(staging)
    staging.mkdir(parents=True)
    return staging


def _swap_in(spec: BrowserSpec, staging: Path, keep_old: bool = False) -> Path | None:
    """Replace the live profile by `staging` without a moment where neither exists.

    The current profile is renamed aside first (instant, same drive), the new one is renamed in,
    and only then is the old one deleted (or kept when keep_old). If anything fails, the original
    is put back. Returns the folder holding the previous data when it still exists, else None.
    """
    root = spec.root
    aside = None
    if root.exists():
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        aside = root.parent / f"{root.name}.before-restore-{stamp}"
        n = 2
        while aside.exists():
            aside = root.parent / f"{root.name}.before-restore-{stamp}_{n}"
            n += 1
        root.rename(aside)  # fails cleanly (nothing changed) if a file is still in use
    root.parent.mkdir(parents=True, exist_ok=True)
    try:
        staging.rename(root)
    except OSError:
        if aside is not None:
            aside.rename(root)  # put the original back
        raise
    if aside is None:
        return None
    if keep_old:
        return aside
    try:
        _rmtree(aside)
    except OSError:
        return aside  # restored fine, but the old copy could not be fully deleted
    return None


def restore_stream(spec: BrowserSpec, reader, progress: Progress | None = None,
                   keep_old: bool = False) -> tuple[int, str | None, Path | None]:
    """Restore from a DecryptReader. Extracts into a staging folder first and only swaps it
    in after the whole stream has been read and authenticated, so a bad file changes nothing."""
    if is_running(spec):
        raise RuntimeError(f"{spec.name} is running. Close it before restoring.")
    staging, staging_resolved = None, None
    manifest: dict = {}
    trailer = None
    kept = None
    count = 0
    try:
        with tarfile.open(fileobj=reader, mode="r|gz") as tar:
            members = iter(tar)
            first = next(members, None)
            if first is None or first.name != MANIFEST:
                raise ValueError("This backup has an unexpected layout.")
            manifest = json.loads(_read_member(tar, first).read())
            _check_browser(spec, manifest)  # before anything is created
            staging = _staging_for(spec)
            staging_resolved = staging.resolve()
            for member in members:
                if member.name == TRAILER:
                    trailer = json.loads(_read_member(tar, member).read())
                    continue
                _safe_name(member.name)
                target = (staging / member.name).resolve()
                if staging_resolved not in target.parents:
                    raise ValueError("Backup contains an unsafe file path.")
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                elif member.isreg():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with _read_member(tar, member) as src, target.open("wb") as dst:
                        shutil.copyfileobj(src, dst, 1024 * 1024)
                    try:
                        os.utime(target, (member.mtime, member.mtime))
                    except OSError:
                        pass
                    count += 1
                    if progress and count % 50 == 0:
                        progress(f"Restoring… {count} files")
                else:
                    raise ValueError("Backup contains an unsupported entry type.")
        reader.drain()  # authenticate the rest of the file (detects truncation)
        if staging is None or trailer is None or trailer.get("file_count") != count:
            raise ValueError("Incorrect password or damaged backup file.")
        kept = _swap_in(spec, staging, keep_old)  # only now is the live profile replaced
    except BaseException:
        if staging is not None and staging.exists():
            _rmtree(staging)
        raise
    return count, _os_warning(spec, manifest), kept
