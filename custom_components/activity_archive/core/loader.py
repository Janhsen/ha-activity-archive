"""Format detection and safe decompression in front of the readers."""

from __future__ import annotations

import hashlib
import zlib
from pathlib import Path

from .fit_reader import parse_fit
from .gpx_reader import parse_gpx, parse_tcx
from .model import BadFileError, IntegrityError, RawActivity, UnsafeFileError

MAX_UNPACKED_BYTES = 64 * 2**20  # decompressed size limit per file
MAX_FILE_BYTES = 128 * 2**20  # size limit of a file on disk
MAX_GZIP_DEPTH = 2  # a ``.fit.gz.gz`` is tolerated, more is rejected


def gunzip_bounded(data: bytes, limit: int = MAX_UNPACKED_BYTES) -> bytes:
    """Decompress one gzip stream; refuse results above ``limit`` bytes (decompression bomb)."""
    inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        out = inflater.decompress(data, limit)
        if inflater.unconsumed_tail:
            raise UnsafeFileError(f"gzip expands beyond {limit >> 20} MB: rejected")
        out += inflater.flush()
    except zlib.error as err:
        raise IntegrityError(f"gzip damaged ({err})") from err
    if not inflater.eof:
        raise IntegrityError("gzip truncated")
    if len(out) > limit:
        raise UnsafeFileError(f"gzip expands beyond {limit >> 20} MB: rejected")
    return out


def load_bytes(
    data: bytes, *, strict: bool = True, max_unpacked: int = MAX_UNPACKED_BYTES
) -> RawActivity:
    """Detect the format of ``data`` (FIT, GPX, TCX, optionally gzip) and decode it.

    ``content_hash`` of the result is the SHA-256 of the decompressed bytes, so the same file
    packed differently is recognised as the same.
    """
    depth = 0
    while data[:2] == b"\x1f\x8b":
        depth += 1
        if depth > MAX_GZIP_DEPTH:
            raise UnsafeFileError("gzip nested too deeply")
        data = gunzip_bounded(data, max_unpacked)
    if len(data) >= 12 and data[8:12] == b".FIT":
        raw = parse_fit(data, strict=strict)
    else:
        head = data[:4096].lstrip(b"\xef\xbb\xbf \t\r\n")
        if head.startswith(b"<") and b"<gpx" in data[:4096]:
            raw = parse_gpx(data, strict=strict)
        elif head.startswith(b"<") and b"TrainingCenterDatabase" in data[:4096]:
            raw = parse_tcx(data, strict=strict)
        else:
            raise BadFileError("file format not recognised")
    raw.content_hash = hashlib.sha256(data).hexdigest()
    return raw


def load_path(path: Path, *, strict: bool = True) -> RawActivity:
    """Read and decode a file; very large files are rejected before they are read."""
    if path.stat().st_size > MAX_FILE_BYTES:
        raise UnsafeFileError(f"file larger than {MAX_FILE_BYTES >> 20} MB: rejected")
    return load_bytes(path.read_bytes(), strict=strict)
