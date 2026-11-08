"""High-level encrypted backup / restore / wipe workflows.

Backups are streamed: the profile is archived, compressed and encrypted in one pass, so no
unencrypted copy of it is ever written to disk.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

from .browser import create_profile_archive, restore_stream, verify_stream, wipe_profile
from .browsers import BrowserSpec
from .crypto import DecryptReader, EncryptWriter
from .library import write_meta
from .paths import is_inside

Progress = Callable[[str], None]


def create_backup(spec: BrowserSpec, output_path: Path, password: str, wipe: bool = True,
                  progress: Progress | None = None, label: str = ""
                  ) -> tuple[int, bool, list[str]]:
    """Back up a browser, verify the backup by decrypting it again, then (optionally) wipe it.

    The backup is written to `<name>.part`, flushed to disk, verified, and only then renamed to
    its final name; so a crash never leaves a half-written file that looks like a backup.
    Nothing is deleted unless every file was archived and the encrypted backup re-opened
    successfully with the password. Returns (file_count, wiped, unreadable_files).
    """
    say = progress or (lambda _m: None)
    if is_inside(output_path, spec.root):
        raise ValueError(f"The backup folder is inside the {spec.name} data folder, so a reset "
                         f"would delete the backup too.\nChoose a folder outside:\n{spec.root}")
    part = output_path.with_name(output_path.name + ".part")
    try:
        say("Archiving and encrypting…")
        with part.open("wb") as fh:
            writer = EncryptWriter(fh, password)
            count, skipped = create_profile_archive(spec, writer, say)
            writer.close()
            fh.flush()
            os.fsync(fh.fileno())  # on disk, not just in the OS cache, before anything is wiped
        if wipe and skipped:
            raise RuntimeError(f"Some files could not be read, so this backup was discarded and "
                               f"{spec.name} was NOT reset. Make sure {spec.name} is fully "
                               "closed and try again:\n" + "\n".join(skipped[:8]))

        say("Verifying backup…")
        with part.open("rb") as fh:
            verify_stream(DecryptReader(fh, password), count)
        os.replace(part, output_path)
    except BaseException:
        part.unlink(missing_ok=True)  # never leave a half-written or unverified backup
        raise
    write_meta(output_path, spec.key, label, count)  # only after verification succeeded
    if wipe:
        wipe_profile(spec, say)
    return count, wipe, skipped


def wipe_browser(spec: BrowserSpec, progress: Progress | None = None) -> None:
    """Delete ALL data of the browser (no backup is made)."""
    wipe_profile(spec, progress)


def restore_backup(spec: BrowserSpec, backup_path: Path, password: str,
                   progress: Progress | None = None, keep_old: bool = False
                   ) -> tuple[int, str | None, Path | None]:
    """Returns (files, warning, folder_with_previous_data_or_None)."""
    say = progress or (lambda _m: None)
    say("Checking password…")
    with backup_path.open("rb") as fh:
        reader = DecryptReader(fh, password)  # wrong password fails here, before any change
        say("Decrypting and restoring…")
        return restore_stream(spec, reader, say, keep_old)
