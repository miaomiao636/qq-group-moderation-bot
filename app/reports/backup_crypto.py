"""AES-256-GCM objects: magic + random 96-bit nonce + ciphertext + 128-bit tag.

Decryption targets must be private temporary files: plaintext is unauthenticated
until finalize succeeds. Callers never publish a recovery before full validation.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from contextlib import nullcontext
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

MAGIC = b"QQB1"
CHUNK = 1024 * 1024


def copy_plain(
    source: Path, target: Path | None, *, check: Callable[[], None] = lambda: None
) -> tuple[str, int]:
    """Unencrypted immutable copy (or streaming hash); no key is involved."""
    digest, size = hashlib.sha256(), 0
    with (
        source.open("rb") as src,
        target.open("xb") if target is not None else nullcontext() as dst,
    ):
        while data := src.read(CHUNK):
            check()
            digest.update(data)
            size += len(data)
            if dst is not None:
                dst.write(data)
        if dst is not None:
            dst.flush()
            os.fsync(dst.fileno())
    return digest.hexdigest(), size


def seal_file(
    source: Path,
    target: Path,
    key: bytes,
    context: bytes,
    *,
    check: Callable[[], None] = lambda: None,
) -> tuple[str, int]:
    if len(key) != 32:
        raise ValueError("KEY_LENGTH")
    nonce = os.urandom(12)
    cipher = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    cipher.authenticate_additional_data(MAGIC + context)
    digest, size = hashlib.sha256(), 0
    with source.open("rb") as src, target.open("xb") as dst:
        dst.write(MAGIC + nonce)
        while data := src.read(CHUNK):
            check()
            digest.update(data)
            size += len(data)
            dst.write(cipher.update(data))
        dst.write(cipher.finalize())
        dst.write(cipher.tag)
        dst.flush()
        os.fsync(dst.fileno())
    return digest.hexdigest(), size


def unseal_file(
    source: Path,
    target: Path | None,
    key: bytes,
    context: bytes,
    *,
    check: Callable[[], None] = lambda: None,
) -> tuple[str, int]:
    if len(key) != 32 or source.stat().st_size < 32:
        raise ValueError("INVALID_OBJECT")
    digest, size = hashlib.sha256(), 0
    with source.open("rb") as src:
        header = src.read(16)
        if header[:4] != MAGIC:
            raise ValueError("OBJECT_VERSION")
        src.seek(-16, os.SEEK_END)
        tag = src.read(16)
        remaining = src.tell() - 32
        src.seek(16)
        cipher = Cipher(algorithms.AES(key), modes.GCM(header[4:], tag)).decryptor()
        cipher.authenticate_additional_data(MAGIC + context)
        with target.open("xb") if target is not None else nullcontext() as dst:
            while remaining:
                check()
                data = src.read(min(CHUNK, remaining))
                if not data:
                    raise ValueError("TRUNCATED_OBJECT")
                remaining -= len(data)
                plain = cipher.update(data)
                digest.update(plain)
                size += len(plain)
                if dst is not None:
                    dst.write(plain)
            cipher.finalize()
            if dst is not None:
                dst.flush()
                os.fsync(dst.fileno())
    return digest.hexdigest(), size
