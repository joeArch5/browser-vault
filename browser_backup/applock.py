"""App lock: an OPTIONAL password required to use the app.

The lock can be turned on and off at any time (Security page). On the very first launch the user
chooses between creating a password and continuing without a lock. The "off" choice is remembered
in config.json; a password is "on" exactly when lock.json exists.

Only a salted PBKDF2 hash of the password is stored (never the password itself), in
`lock.json` next to the app settings. This is separate from a backup's encryption password.

Wrong guesses are slowed down: after 3 failures every further failure adds an increasing wait
(5 s, 10 s, 20 s ... up to 15 min). The wait survives restarting the app.

Honest limit: this is a gate on the app's interface. It does not protect the backup files
themselves (they are protected by their own encryption password), and someone with full access
to your Windows account can remove lock.json or run the code directly.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from pathlib import Path

from . import config

MIN_LENGTH = 8
ITERATIONS = 600_000
FREE_ATTEMPTS = 3
MAX_WAIT = 15 * 60
OFF_KEY = "app_lock_off"   # config.json flag: the user chose to use the app without a lock


class LockFileError(Exception):
    """lock.json exists but cannot be read, so the app stays locked."""


def lock_path() -> Path:
    return config.config_dir() / "lock.json"


def _hash(password: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)


def _read() -> dict | None:
    path = lock_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        bytes.fromhex(data["salt"])
        bytes.fromhex(data["hash"])
        int(data["iterations"])
        return data
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise LockFileError(f"The lock file is damaged:\n{path}") from exc


def _write(data: dict) -> None:
    """Atomic write that raises on failure (a lock that silently failed to save is dangerous)."""
    path = lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def is_set() -> bool:
    """True when the lock is ON (a password exists). This always wins over the 'off' flag."""
    return lock_path().exists()


def is_disabled() -> bool:
    """True when the user explicitly chose to run without an app lock."""
    return not is_set() and bool(config.load().get(OFF_KEY))


def _set_off_flag(off: bool) -> None:
    data = config.load()
    if off:
        data[OFF_KEY] = True
    else:
        data.pop(OFF_KEY, None)
    config.save(data, strict=True)  # a security setting that silently failed to save is bad


def skip_setup() -> None:
    """First run: continue without an app lock (can be turned on later)."""
    if is_set():
        raise ValueError("An app password is already set.")
    _set_off_flag(True)


def enable(password: str) -> None:
    """Turn the lock on with a new password."""
    set_password(password)  # validates, then writes lock.json atomically
    try:
        _set_off_flag(False)
    except OSError:
        pass  # lock.json exists, so the lock is on regardless of the flag


def disable(password: str) -> tuple[bool, str]:
    """Turn the lock off. Needs the current password (and honours the wrong-guess delays)."""
    ok, wait = verify(password)
    if not ok:
        return False, (f"Too many attempts. Wait {wait} s." if wait
                       else "The current password is incorrect.")
    _set_off_flag(True)
    try:
        lock_path().unlink(missing_ok=True)
    except OSError:
        try:
            _set_off_flag(False)  # could not remove it: keep the lock consistently ON
        except OSError:
            pass
        raise
    return True, "App lock turned off."


def check_new_password(password: str) -> str | None:
    """Return a problem description, or None if the password is acceptable."""
    if len(password) < MIN_LENGTH:
        return f"Use at least {MIN_LENGTH} characters."
    return None


def set_password(password: str) -> None:
    problem = check_new_password(password)
    if problem:
        raise ValueError(problem)
    salt = os.urandom(16)
    _write({"version": 1, "iterations": ITERATIONS, "salt": salt.hex(),
            "hash": _hash(password, salt, ITERATIONS).hex(), "fails": 0, "locked_until": 0})


def seconds_locked(now: float | None = None) -> int:
    """Seconds the user must still wait before another attempt (0 = may try now)."""
    data = _read()
    if not data:
        return 0
    remaining = float(data.get("locked_until", 0)) - (now if now is not None else time.time())
    return max(0, int(remaining + 0.999))


def verify(password: str) -> tuple[bool, int]:
    """Check a password. Returns (ok, seconds_to_wait). While waiting, attempts are refused."""
    data = _read()
    if data is None:
        return False, 0
    wait = seconds_locked()
    if wait:
        return False, wait
    expected = bytes.fromhex(data["hash"])
    actual = _hash(password, bytes.fromhex(data["salt"]), int(data["iterations"]))
    if hmac.compare_digest(expected, actual):
        if data.get("fails") or data.get("locked_until"):
            data.update(fails=0, locked_until=0)
            _write(data)
        return True, 0
    fails = int(data.get("fails", 0)) + 1
    delay = 0
    if fails >= FREE_ATTEMPTS:
        delay = min(MAX_WAIT, 5 * 2 ** (fails - FREE_ATTEMPTS))
    data.update(fails=fails, locked_until=time.time() + delay if delay else 0)
    _write(data)
    return False, delay


def change_password(old: str, new: str) -> tuple[bool, str]:
    """Returns (changed, message)."""
    problem = check_new_password(new)
    if problem:
        return False, problem
    ok, wait = verify(old)
    if not ok:
        return False, (f"Too many attempts. Wait {wait} s." if wait
                       else "The current password is incorrect.")
    set_password(new)
    return True, "App password changed."
