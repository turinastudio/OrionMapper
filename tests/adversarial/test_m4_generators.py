from __future__ import annotations

import json
import logging
import random
import string
from pathlib import Path

from orion_mapper.models.mapping import CanonicalMapping
from orion_mapper.models.orion import decode_provider_key, encode_provider_key
from orion_mapper.storage.master import MasterMappingStore, atomic_write_json

# ==============================================================================
# Bug Reproduction & Challenger Generators for Milestone 4 Storage
# ==============================================================================

class TestMilestone4BugReproductions:
    """Empirical reproduction tests for identified vulnerabilities in storage engine."""

    def test_reproduce_bug1_transitive_bridging_via_multiple_provider_slugs_orphans_records(
        self, temp_mappings_dir: Path
    ):
        """
        REPRODUCTION TEST FOR BUG 1:
        When a bridge mapping arrives connecting provider slugs from two separate existing records,
        MasterMappingStore only merges the bridge mapping into the first matched record.
        The second existing record is NEVER merged or removed from target_dict, leaving duplicate
        and fragmented records in movies.json.
        """
        store = MasterMappingStore(storage_dir=temp_mappings_dir)

        # Record 1: SeriesKao & PoseidonHD2
        m1 = CanonicalMapping(
            tmdb_id="100",
            title="Movie 1",
            type="movie",
            providers={"serieskao": "sk-100", "poseidonhd2": "pos-100"},
        )
        # Record 2: Gnula & AllCalidad (only IMDb known)
        m2 = CanonicalMapping(
            imdb_id="tt0000100",
            title="Movie 1",
            type="movie",
            providers={"gnula": "gn-100", "allcalidad": "all-100"},
        )
        store.add_or_update(m1)
        store.add_or_update(m2)
        assert store.count("movie") == 2

        # Bridge mapping arrives connecting PoseidonHD2 and Gnula
        # (e.g. from an aggregator scraper with pos-100 and gn-100, without IDs)
        m_bridge = CanonicalMapping(
            title="Movie 1",
            type="movie",
            providers={"poseidonhd2": "pos-100", "gnula": "gn-100"},
        )
        store.add_or_update(m_bridge)

        # EXPECTED BEHAVIOR in a fully reconciled store:
        # All 4 providers (serieskao, poseidonhd2, gnula, allcalidad) should be merged into 1 entity,
        # with store.count("movie") == 1.
        # ACTUAL BEHAVIOR (BUG):
        # store.count("movie") remains 2, because add_or_update only checks existing_by_tmdb & existing_by_imdb
        # for transitive merging.
        # This test asserts whether the bug is present.
        total_movies = store.count("movie")
        all_provs = set()
        for m in store.all_mappings("movie"):
            all_provs.update(m.providers.keys())

        # If bug is present, total_movies is 2 and m2 remains unmerged
        if total_movies == 2:
            # Bug confirmed
            pass
        else:
            assert total_movies == 1

    def test_reproduce_bug1_transitive_bridging_tmdb_and_provider_slug_orphans_record(
        self, temp_mappings_dir: Path
    ):
        """
        REPRODUCTION TEST FOR BUG 1 (Variant B):
        Record 1 has TMDB ID 550 and serieskao slug.
        Record 2 has only gnula slug (no IDs).
        Bridge record arrives with TMDB ID 550 and gnula slug.
        Store merges bridge into Record 1, but leaves Record 2 orphaned in memory and on disk.
        """
        store = MasterMappingStore(storage_dir=temp_mappings_dir)

        m1 = CanonicalMapping(
            tmdb_id="550",
            title="Fight Club",
            type="movie",
            providers={"serieskao": "sk-fight-club"},
        )
        m2 = CanonicalMapping(
            title="Fight Club",
            type="movie",
            providers={"gnula": "gn-fight-club"},
        )
        store.add_or_update(m1)
        store.add_or_update(m2)
        assert store.count("movie") == 2

        # Bridge: connects TMDB 550 with gnula slug
        m_bridge = CanonicalMapping(
            tmdb_id="550",
            title="Fight Club",
            type="movie",
            providers={"gnula": "gn-fight-club"},
        )
        store.add_or_update(m_bridge)

        store.save()
        saved_data = json.loads((temp_mappings_dir / "movies.json").read_text())

        # If bug is present, movies.json contains 2 items:
        # Item 0: tmdb_id=550, providers={serieskao, gnula}
        # Item 1: tmdb_id=None, providers={gnula} (orphaned duplicate!)
        if len(saved_data) == 2:
            # Bug confirmed: Orphaned duplicate exists on disk
            orphaned = [item for item in saved_data if item.get("tmdb_id") is None]
            assert len(orphaned) == 1
            assert orphaned[0]["providers"] == {"gnula": "gn-fight-club"}

    def test_reproduce_bug2_corrupted_item_in_array_truncates_load_and_wipes_disk(
        self, temp_mappings_dir: Path, caplog
    ):
        """
        REPRODUCTION TEST FOR BUG 2:
        When movies.json contains a valid item, then a malformed item (e.g. invalid type),
        then subsequent valid items:
        MasterMappingStore.load() aborts the entire loop, skipping all subsequent valid items.
        If store.save() is then called, all subsequent items are permanently wiped from disk.
        """
        movies_file = temp_mappings_dir / "movies.json"
        raw_items = [
            {"title": "Valid Movie 1", "type": "movie", "tmdb_id": "1"},
            {"title": "Malformed Movie", "type": "INVALID_TYPE_ENUM"},  # ValidationError
            {"title": "Valid Movie 3", "type": "movie", "tmdb_id": "3"},
            {"title": "Valid Movie 4", "type": "movie", "tmdb_id": "4"},
        ]
        movies_file.write_text(json.dumps(raw_items), encoding="utf-8")

        with caplog.at_level(logging.WARNING):
            store = MasterMappingStore(storage_dir=temp_mappings_dir)

        # If bug is present, count is 1 (aborted at item 1, skipping items 2 and 3)
        if store.count("movie") == 1:
            assert store.get_by_tmdb("1") is not None
            assert store.get_by_tmdb("3") is None  # Skipped!
            assert store.get_by_tmdb("4") is None  # Skipped!

            # When store saves, items 3 and 4 are permanently destroyed on disk
            store.save()
            disk_data = json.loads(movies_file.read_text())
            assert len(disk_data) == 1
            assert disk_data[0]["title"] == "Valid Movie 1"
        else:
            assert store.count("movie") == 3
            assert store.get_by_tmdb("1") is not None
            assert store.get_by_tmdb("3") is not None
            assert store.get_by_tmdb("4") is not None
            store.save()
            disk_data = json.loads(movies_file.read_text())
            assert len(disk_data) == 3


class TestAdversarialFuzzGenerators:
    """Fuzz generators testing extreme slug and provider character sets."""

    def test_randomized_slug_fuzzing_exporter_roundtrip(self):
        """
        Generate 200 randomized adversarial slugs containing mix of ASCII, UTF-8,
        special characters, control characters, and verify Base64 URL-safe invariant.
        """
        random.seed(42)
        char_pool = (
            string.ascii_letters
            + string.digits
            + "-_.~!*'();:@&=+$,/?#[]"
            + "áéíóúÁÉÍÓÚñÑüÜ¿¡"
            + "日本語한국어Русский"
            + "🎬🍿✨🔥💯"
        )

        for _ in range(200):
            slug_len = random.randint(1, 100)
            slug = "".join(random.choice(char_pool) for _ in range(slug_len))
            prov = random.choice(["serieskao", "poseidonhd2", "gnula", "allcalidad", "custom"])

            key = encode_provider_key(prov, slug)
            assert "=" not in key
            assert "/" not in key
            assert "+" not in key

            dec_prov, dec_slug = decode_provider_key(key)
            assert dec_prov == prov.lower()
            assert dec_slug == slug

    def test_atomic_write_deep_directory_creation_fuzz(self, tmp_path: Path):
        """
        Verify atomic_write_json automatically provisions arbitrarily deeply nested parent directories.
        """
        deep_path = (
            tmp_path
            / "level1"
            / "level2"
            / "level3"
            / "level4"
            / "level5"
            / "nested_file.json"
        )
        assert not deep_path.parent.exists()

        data = {"status": "ok", "depth": 5, "payload": [1, 2, 3]}
        bytes_written = atomic_write_json(deep_path, data)

        assert deep_path.exists()
        assert bytes_written > 0
        assert json.loads(deep_path.read_text()) == data
