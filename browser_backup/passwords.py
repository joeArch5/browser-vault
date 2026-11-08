"""Strength check for the backup encryption password.

Length alone is not enough ("aaaaaaaaaaaa" has 12 characters). This is a cheap, offline estimate,
not a guarantee: it rejects the obvious mistakes and leaves real passphrases alone.
"""
from __future__ import annotations

import math

MIN_LENGTH = 12
MIN_BITS = 50
COMMON = ("password", "passw0rd", "qwerty", "azerty", "letmein", "welcome", "admin", "iloveyou",
          "123456", "654321", "abcdef", "monkey", "dragon", "football", "baseball", "login",
          "master", "1q2w3e", "changeme", "secret")


def _longest_run(text: str) -> int:
    """Longest run of characters that go up or down by one (abcdef, 98765)."""
    best = run = 1
    direction = 0
    for prev, cur in zip(text, text[1:]):
        step = ord(cur) - ord(prev)
        if step in (1, -1) and (direction in (0, step)):
            run += 1
            direction = step
        elif step in (1, -1):
            run, direction = 2, step
        else:
            run, direction = 1, 0
        best = max(best, run)
    return best


def check_backup_password(password: str) -> str | None:
    """Return a problem description, or None if the password is acceptable."""
    if len(password) < MIN_LENGTH:
        return f"Use at least {MIN_LENGTH} characters."
    unique = len(set(password))
    if unique < 6:
        return "Too repetitive. Use more different characters."
    low = password.lower()
    # A common word only matters if it makes up a big part of the password ("mastermind-garden-lamp"
    # is fine, "password2026!!" is not).
    if any(word in low and len(word) * 2 >= len(low) for word in COMMON):
        return "It contains a very common password pattern. Choose something less guessable."
    if _longest_run(low) >= 6:
        return "It contains a long sequence like abcdef or 123456. Choose something less guessable."
    pool = (26 * any(c.islower() for c in password) + 26 * any(c.isupper() for c in password)
            + 10 * any(c.isdigit() for c in password)
            + 33 * any(not c.isalnum() for c in password)
            + 100 * any(c.isalpha() and not c.isascii() for c in password))  # Arabic, CJK, accents
    effective = unique + (len(password) - unique) * 0.25   # repeated characters count for less
    if effective * math.log2(max(pool, 2)) < MIN_BITS:
        return ("Too easy to guess. Add more characters or mix letters, digits and symbols "
                "(a few unrelated words also work well).")
    return None
