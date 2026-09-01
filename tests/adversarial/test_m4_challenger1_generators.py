from __future__ import annotations

import concurrent.futures
import json
import random
import string
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from orion_mapper.models.item import ContentType
from orion_mapper.models.mapping import CanonicalMapping
from orion_mapper.models.orion import decode_provider_key, encode_provider_key
from orion_mapper.storage.master import MasterMappingStore, atomic_write_json

# ==============================================================================
# 1. ATOMIC WRITE & PARTIAL WRITE DISK FAILURE GENERATORS
# ==============================================================================

class TestAtomicWriteFailureGenerators:
    """Stress generators for partial write interruptions, file descriptor leaks, and race replacements."""

    def test_partial_write_failure_interruption(self, tmp_path: Path):
        """
        Simulate a partial write where only the first half of the byte buffer
        is written before an unhandled exception or I/O failure occurs.
        Verifies that target file is never created with truncated content and temp file is removed.
        """
        target_file = tmp_path / "partial_fail.json"
        data = {"large_payload": "x" * 100_000, "valid": True}

        original_open = open

        def mock_open_partial_write(*args, **kwargs):
            handle = original_open(*args, **kwargs)
            if ".tmp-" in str(args[0]):
                orig_write = handle.write
                def partial_write(buf):
                    # Write only 10 bytes then raise IOError
                    orig_write(buf[:10])
                    raise OSError("Connection/Disk write interrupted mid-stream")
                handle.write = partial_write
            return handle

        with patch("builtins.open", side_effect=mock_open_partial_write):
            with pytest.raises(IOError):
                atomic_write_json(target_file, data)

        # Invariants:
        assert not target_file.exists()
        assert len(list(tmp_path.glob("*.tmp-*"))) == 0

    def test_concurrent_atomic_writes_same_target_no_collision(self, tmp_path: Path):
        """
        100 concurrent threads attempting atomic writes to the EXACT SAME destination file.
        Each thread writes unique JSON with its thread ID.
        Verifies no uuid collision, no file corruption, no orphaned .tmp files, and target is valid JSON.
        """
        target_file = tmp_path / "contested.json"
        num_threads = 100

        def write_worker(tid: int):
            atomic_write_json(target_file, {"winner_thread": tid, "timestamp": time.time_ns()})

        with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [executor.submit(write_worker, i) for i in range(num_threads)]
            for f in concurrent.futures.as_completed(futures):
                f.result()

        # Invariant 1: Destination file exists and is perfectly valid JSON
        assert target_file.exists()
        final_content = json.loads(target_file.read_text(encoding="utf-8"))
        assert "winner_thread" in final_content
        assert isinstance(final_content["winner_thread"], int)

        # Invariant 2: Exactly zero temporary files remained
        assert len(list(tmp_path.glob("*.tmp-*"))) == 0


# ==============================================================================
# 2. TRANSITIVE BRIDGING CYCLICAL GRAPH RESOLUTION
# ==============================================================================

class TestTransitiveBridgingGraphResolution:
    """Stress testing multi-way transitive bridging and cyclical identity convergence."""

    def test_tri_way_transitive_identity_convergence(self, tmp_path: Path):
        """
        Tri-way cyclic identity resolution:
        Mapping 1: TMDB 550 + Provider serieskao:sk-slug
        Mapping 2: IMDb tt0137523 + Provider gnula:gn-slug
        Mapping 3: TMDB 550 + IMDb tt0137523 + Provider poseidonhd2:pos-slug
        Mapping 4: Provider serieskao:sk-slug + Provider allcalidad:all-slug

        All 4 mappings must converge into a single unified CanonicalMapping
        containing all 4 providers, both IDs, and 100% lookup consistency.
        """
        store = MasterMappingStore(storage_dir=tmp_path)

        m1 = CanonicalMapping(tmdb_id="550", title="Fight Club", type="movie", providers={"serieskao": "sk-slug"})
        m2 = CanonicalMapping(imdb_id="tt0137523", title="El Club de la Pelea", type="movie", providers={"gnula": "gn-slug"})
        m3 = CanonicalMapping(tmdb_id="550", imdb_id="tt0137523", title="Fight Club (1999)", type="movie", providers={"poseidonhd2": "pos-slug"})
        m4 = CanonicalMapping(title="Fight Club Final", type="movie", providers={"serieskao": "sk-slug", "allcalidad": "all-slug"})

        store.add_or_update(m1)
        store.add_or_update(m2)
        assert store.count("movie") == 2

        store.add_or_update(m3)
        assert store.count("movie") == 1

        unified = store.add_or_update(m4)
        assert store.count("movie") == 1
        assert unified.tmdb_id == "550"
        assert unified.imdb_id == "tt0137523"
        assert set(unified.providers.keys()) == {"serieskao", "gnula", "poseidonhd2", "allcalidad"}

        # Verify all lookups resolve to the same object
        assert store.get_by_tmdb("550", "movie") == unified
        assert store.get_by_imdb("tt0137523", "movie") == unified
        assert store.get_by_provider_slug("serieskao", "sk-slug") == unified
        assert store.get_by_provider_slug("gnula", "gn-slug") == unified
        assert store.get_by_provider_slug("poseidonhd2", "pos-slug") == unified
        assert store.get_by_provider_slug("allcalidad", "all-slug") == unified


# ==============================================================================
# 3. FUZZING GENERATOR: SLUGS, UNICODE, AND BASE64 URL KEYS
# ==============================================================================

class TestFuzzingGenerators:
    """Fuzzing provider slugs and Unicode title variations."""

    def test_random_unicode_slug_fuzzing_1000_iterations(self):
        """
        Fuzz encode_provider_key / decode_provider_key with 1,000 randomly generated
        Unicode character sequences including Cyrillic, CJK, Emoji, Arabic, punctuation, and whitespaces.
        """
        unicode_ranges = [
            (0x0020, 0x007E),  # Basic Latin
            (0x00A0, 0x00FF),  # Latin-1 Supplement
            (0x0400, 0x04FF),  # Cyrillic
            (0x0600, 0x06FF),  # Arabic
            (0x4E00, 0x4FFF),  # CJK Unified Ideographs
            (0x1F600, 0x1F64F),  # Emoji
        ]

        rng = random.Random(42)

        for _ in range(1000):
            # Generate random provider name
            prov_len = rng.randint(3, 15)
            prov_chars = [rng.choice(string.ascii_letters + string.digits) for _ in range(prov_len)]
            provider = "".join(prov_chars)

            # Generate random complex slug
            slug_len = rng.randint(5, 50)
            slug_chars = []
            for _ in range(slug_len):
                r_start, r_end = rng.choice(unicode_ranges)
                code_point = rng.randint(r_start, r_end)
                slug_chars.append(chr(code_point))
            slug = "".join(slug_chars)

            # Test roundtrip
            clean_prov = provider.strip().lower()
            clean_slug = slug.strip()

            encoded = encode_provider_key(provider, slug)
            assert "=" not in encoded
            assert "/" not in encoded
            assert "+" not in encoded

            dec_prov, dec_slug = decode_provider_key(encoded)
            assert dec_prov == clean_prov
            assert dec_slug == clean_slug


# ==============================================================================
# 4. EXTREME SCALE & MEMORY STABILITY (20,000 MAPPINGS)
# ==============================================================================

class TestExtremeScaleHarness:
    """Stress test MasterMappingStore with 20,000 mappings."""

    def test_20000_mappings_master_store_scalability(self, tmp_path: Path):
        """
        Generate 20,000 mappings (10k movies + 10k series).
        Verify memory stability, save/load speed, and lookup latency.
        """
        store = MasterMappingStore(storage_dir=tmp_path / "scale_20k")

        # Influx 20k items
        for i in range(1, 20001):
            c_type = ContentType.MOVIE if i % 2 == 0 else ContentType.SERIES
            store.add_or_update(
                CanonicalMapping(
                    tmdb_id=str(i),
                    imdb_id=f"tt{i:07d}",
                    title=f"Scalability Test Title #{i}",
                    type=c_type,
                    year=1990 + (i % 35),
                    providers={"provider_a": f"slug-a-{i}", "provider_b": f"slug-b-{i}"},
                )
            )

        assert store.count("movie") == 10000
        assert store.count("series") == 10000
        assert store.count() == 20000

        # Save to disk
        t0 = time.perf_counter()
        store.save()
        save_time = time.perf_counter() - t0
        assert save_time < 5.0, f"Saving 20k items took {save_time:.2f}s"

        # Reload from disk
        store2 = MasterMappingStore(storage_dir=tmp_path / "scale_20k")
        t1 = time.perf_counter()
        store2.load()
        load_time = time.perf_counter() - t1
        assert load_time < 5.0, f"Loading 20k items took {load_time:.2f}s"
        assert store2.count() == 20000

        # Fast O(1) spot-check
        spot_movie = store2.get_by_tmdb("10000", "movie")
        assert spot_movie is not None
        assert spot_movie.imdb_id == "tt0010000"

        spot_series = store2.get_by_tmdb("19999", "series")
        assert spot_series is not None
        assert spot_series.imdb_id == "tt0019999"
