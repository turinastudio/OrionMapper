"""AllCalidad image-MD5 to TMDB resolver.

AllCalidad serves posters/backdrops/logos whose filenames embed
``md5(str(tmdb_id))`` (32 lowercase hex chars), e.g.
``/thumbs/5821be8fcefffa2cd7d79e6bcdd5e66b_hd.webp`` -> TMDB 982620.

The MD5 domain is exactly TMDB IDs ``1..MAX_ID``, so reversal is a binary
search over a precomputed sorted index of 20-byte records
(16 bytes md5 + 4 bytes big-endian tmdb id), the same layout OrionServer
uses (``data/allcalidad/md5-index.bin``).

The index file is generated data (44 MB for 2.2M IDs): build it locally
with ``python -m orion_mapper.resolver.allcalidad_md5 --generate`` and
point ``ORION_ALLCALIDAD_MD5_INDEX`` at it if the default location does
not suit. It must NOT be committed.
"""

from __future__ import annotations

import hashlib
import logging
import mmap
import os
import re
import struct
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_ID = 2_200_000
RECORD_SIZE = 20
MD5_RE = re.compile(r"([0-9a-fA-F]{32})")

DEFAULT_INDEX_PATHS: tuple[str, ...] = (
    "data/allcalidad/md5-index.bin",
    "../OrionServer/data/allcalidad/md5-index.bin",
    "../recursos/node/data/allcalidad/md5-index.bin",
)


def default_index_path() -> Path:
    """Return the configured index path, or the first existing candidate."""
    configured = os.environ.get("ORION_ALLCALIDAD_MD5_INDEX")
    if configured:
        return Path(configured)
    for candidate in DEFAULT_INDEX_PATHS:
        if Path(candidate).exists():
            return Path(candidate)
    return Path(DEFAULT_INDEX_PATHS[0])


def extract_md5(*urls: str | None) -> str | None:
    """Return the first 32-hex MD5 found in the given image URLs."""
    for url in urls:
        if not url:
            continue
        match = MD5_RE.search(str(url))
        if match:
            return match.group(1).lower()
    return None


class AllCalidadMd5Resolver:
    """Memory-mapped binary-search resolver of image MD5 -> TMDB id."""

    def __init__(self, index_path: str | Path | None = None) -> None:
        self.index_path = Path(index_path) if index_path is not None else default_index_path()
        self._mmap: mmap.mmap | None = None
        self._records: int = 0

    def _ensure_loaded(self) -> bool:
        if self._mmap is not None:
            return True
        try:
            size = self.index_path.stat().st_size
            if size == 0 or size % RECORD_SIZE != 0:
                logger.warning("Invalid MD5 index at %s (size %d)", self.index_path, size)
                return False
            raw = open(self.index_path, "rb")
            self._mmap = mmap.mmap(raw.fileno(), 0, access=mmap.ACCESS_READ)
            raw.close()
            self._records = size // RECORD_SIZE
            return True
        except FileNotFoundError:
            logger.warning(
                "AllCalidad MD5 index not found at %s; generate it with "
                "`python -m orion_mapper.resolver.allcalidad_md5 --generate`",
                self.index_path,
            )
            return False
        except OSError as exc:
            logger.warning("Could not load MD5 index at %s: %s", self.index_path, exc)
            return False

    def resolve(self, md5_hex: str | None) -> str | None:
        """Resolve a 32-hex MD5 to a TMDB id string, or None."""
        if not md5_hex or not re.fullmatch(r"[0-9a-fA-F]{32}", md5_hex):
            return None
        if not self._ensure_loaded():
            return None
        assert self._mmap is not None
        target = bytes.fromhex(md5_hex)
        current = bytearray(16)
        low, high = 0, self._records - 1
        while low <= high:
            middle = (low + high) >> 1
            offset = middle * RECORD_SIZE
            current[:] = self._mmap[offset : offset + 16]
            if bytes(current) == target:
                (tmdb_id,) = struct.unpack_from(">I", self._mmap, offset + 16)
                return str(tmdb_id)
            if bytes(current) < target:
                low = middle + 1
            else:
                high = middle - 1
        return None

    def close(self) -> None:
        """Release the memory mapping."""
        if self._mmap is not None:
            try:
                self._mmap.close()
            except OSError:
                pass
            self._mmap = None
            self._records = 0


def generate_index(output: str | Path | None = None, max_id: int = MAX_ID) -> Path:
    """Build the sorted MD5 index file (port of recursos/node generator)."""
    out_path = Path(output) if output is not None else default_index_path()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    logger.info("Hashing TMDB IDs 1..%d", max_id)
    records = bytearray((max_id) * RECORD_SIZE)
    digest = hashlib.md5()
    for i in range(1, max_id + 1):
        digest = hashlib.md5(str(i).encode("ascii"))
        offset = (i - 1) * RECORD_SIZE
        records[offset : offset + 16] = digest.digest()
        struct.pack_into(">I", records, offset + 16, i)
    logger.info("Sorting %d records", max_id)
    rec_view = [bytes(records[i : i + RECORD_SIZE]) for i in range(0, len(records), RECORD_SIZE)]
    rec_view.sort(key=lambda r: r[:16])
    with open(out_path, "wb") as f:
        f.write(b"".join(rec_view))
    logger.info("Wrote %s (%d bytes)", out_path, out_path.stat().st_size)
    return out_path


if __name__ == "__main__":
    import argparse
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="AllCalidad MD5 index tools")
    parser.add_argument("--generate", action="store_true", help="Build the index file")
    parser.add_argument("--output", default=None, help="Index path override")
    parser.add_argument("--resolve", default=None, help="Resolve a single MD5 hex")
    args = parser.parse_args()
    if args.generate:
        print(generate_index(args.output))
    elif args.resolve:
        resolver = AllCalidadMd5Resolver(args.output)
        print(resolver.resolve(args.resolve))
        resolver.close()
    else:
        parser.print_help()
        sys.exit(1)
