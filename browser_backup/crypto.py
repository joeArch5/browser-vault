"""Streaming authenticated encryption (AES-256-GCM) for backup containers.

Container layout:
    MAGIC(4) | salt(16) | nonce_prefix(8) | { len(4, big-endian) | ciphertext+tag }*

The key is derived from the password with scrypt (128 MiB, memory-hard). Each chunk uses
nonce = prefix || chunk_counter and AAD = MAGIC || last_flag, so chunks cannot be reordered,
dropped, or the file truncated without failing authentication. The payload is a tar.gz stream
written/read on the fly, so no plaintext file is ever on disk.
"""
from __future__ import annotations

import os
import struct

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

MAGIC = b"BBM4"
SALT_SIZE = 16
PREFIX_SIZE = 8
KEY_SIZE = 32
TAG_SIZE = 16
CHUNK = 4 * 1024 * 1024
HEADER_SIZE = 4 + SALT_SIZE + PREFIX_SIZE
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 17, 8, 1

BAD = "Incorrect password or damaged backup file."


def _derive_key(password: str, salt: bytes) -> bytes:
    if not password:
        raise ValueError("A backup password is required.")
    kdf = Scrypt(salt=salt, length=KEY_SIZE, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P)
    return kdf.derive(password.encode("utf-8"))


def _nonce(prefix: bytes, counter: int) -> bytes:
    return prefix + struct.pack(">I", counter)


def _aad(last: bool) -> bytes:
    return MAGIC + (b"\x01" if last else b"\x00")


class EncryptWriter:
    """File-like object: everything written is encrypted into `dest` chunk by chunk.

    Call close() when done (it writes the final, flagged chunk). Does not close `dest`.
    """

    def __init__(self, dest, password: str):
        self._dest = dest
        salt = os.urandom(SALT_SIZE)
        self._prefix = os.urandom(PREFIX_SIZE)
        self._aes = AESGCM(_derive_key(password, salt))
        self._counter = 0
        self._buf = bytearray()
        self._closed = False
        dest.write(MAGIC + salt + self._prefix)

    def _emit(self, plaintext: bytes, last: bool) -> None:
        ct = self._aes.encrypt(_nonce(self._prefix, self._counter), plaintext,
                               _aad(last))
        self._dest.write(struct.pack(">I", len(ct)))
        self._dest.write(ct)
        self._counter += 1

    def write(self, data) -> int:
        if self._closed:
            raise ValueError("write to closed EncryptWriter")
        self._buf += data
        # Always keep at least one byte back so close() can mark the real last chunk.
        while len(self._buf) > CHUNK:
            self._emit(bytes(self._buf[:CHUNK]), False)
            del self._buf[:CHUNK]
        return len(data)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._emit(bytes(self._buf), True)
            self._buf.clear()


class DecryptReader:
    """File-like object: reads and authenticates the container, yielding plaintext.

    The first chunk is decrypted immediately, so a wrong password fails right here, before the
    caller has done anything. Later corruption/truncation is detected as the stream is read; read
    to EOF (drain()) to be sure the whole file was authenticated.
    """

    def __init__(self, src, password: str):
        head = src.read(HEADER_SIZE)
        if len(head) < HEADER_SIZE or head[:4] != MAGIC:
            raise ValueError("This is not a valid Browser Backup Manager file.")
        salt = head[4:4 + SALT_SIZE]
        self._prefix = head[4 + SALT_SIZE:]
        self._aes = AESGCM(_derive_key(password, salt))
        self._src = src
        self._counter = 0
        self._buf = bytearray()
        self._eof = False
        first_hdr = src.read(4)
        if len(first_hdr) != 4:
            raise ValueError(BAD)
        self._hdr: bytes | None = first_hdr
        self._load_next()

    def _load_next(self) -> None:
        if self._hdr is None:
            self._eof = True
            return
        (n,) = struct.unpack(">I", self._hdr)
        if n < TAG_SIZE or n > CHUNK + TAG_SIZE:
            raise ValueError(BAD)
        ct = self._src.read(n)
        if len(ct) != n:
            raise ValueError(BAD)
        nxt = self._src.read(4)
        if nxt and len(nxt) != 4:
            raise ValueError(BAD)
        last = not nxt
        try:
            self._buf += self._aes.decrypt(_nonce(self._prefix, self._counter), ct,
                                           _aad(last))
        except InvalidTag as exc:
            raise ValueError(BAD) from exc
        self._counter += 1
        self._hdr = None if last else nxt
        if last:
            self._eof = True

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            while not self._eof:
                self._load_next()
            size = len(self._buf)
        while len(self._buf) < size and not self._eof:
            self._load_next()
        out = bytes(self._buf[:size])
        del self._buf[:size]
        return out

    def drain(self) -> None:
        """Read everything that is left, so every chunk (including the last) is authenticated."""
        while self.read(CHUNK):
            pass
