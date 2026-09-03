"""Unit tests for the AllCalidad image-MD5 to TMDB resolver."""

from __future__ import annotations

import hashlib
import struct

import pytest

from orion_mapper.resolver.allcalidad_md5 import AllCalidadMd5Resolver, extract_md5


def _write_mini_index(path, tmdb_ids: list[int]):
    records = sorted(
        (hashlib.md5(str(i).encode("ascii")).digest() + struct.pack(">I", i) for i in tmdb_ids),
        key=lambda r: r[:16],
    )
    path.write_bytes(b"".join(records))
    return path


def test_extract_md5_from_poster():
    assert (
        extract_md5("https://allcalidad.re/thumbs/5821be8fcefffa2cd7d79e6bcdd5e66b_hd.webp")
        == "5821be8fcefffa2cd7d79e6bcdd5e66b"
    )


def test_extract_md5_prefers_first_url():
    assert (
        extract_md5(
            None,
            "",
            "/backdrops/8635d370cc11148464fdcadbaf313767.webp",
            "/logos/01f78be6f7cad02658508fe4616098a9.webp",
        )
        == "8635d370cc11148464fdcadbaf313767"
    )


def test_extract_md5_no_match():
    assert extract_md5(None, "", "https://allcalidad.re/thumbs/poster_hd.webp") is None


def test_resolve_hits_and_misses(tmp_path):
    index = _write_mini_index(tmp_path / "mini.bin", [550, 82856, 982620])
    resolver = AllCalidadMd5Resolver(index)
    try:
        assert resolver.resolve(hashlib.md5(b"550").hexdigest()) == "550"
        assert resolver.resolve(hashlib.md5(b"82856").hexdigest()) == "82856"
        assert resolver.resolve(hashlib.md5(b"982620").hexdigest()) == "982620"
        assert resolver.resolve(hashlib.md5(b"1").hexdigest()) is None
        assert resolver.resolve("not-a-hash") is None
        assert resolver.resolve(None) is None
    finally:
        resolver.close()


def test_resolve_missing_index_returns_none(tmp_path):
    resolver = AllCalidadMd5Resolver(tmp_path / "does-not-exist.bin")
    try:
        assert resolver.resolve(hashlib.md5(b"550").hexdigest()) is None
    finally:
        resolver.close()


@pytest.mark.skipif(
    __import__("pathlib").Path("data/allcalidad/md5-index.bin").exists() is False,
    reason="requires local md5-index.bin",
)
def test_resolve_against_real_index():
    resolver = AllCalidadMd5Resolver("data/allcalidad/md5-index.bin")
    try:
        assert resolver.resolve("01f78be6f7cad02658508fe4616098a9") == "550"
        assert resolver.resolve("8635d370cc11148464fdcadbaf313767") == "82856"
        assert resolver.resolve("5821be8fcefffa2cd7d79e6bcdd5e66b") == "982620"
    finally:
        resolver.close()
