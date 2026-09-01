"""Adversarial Tier 5 Test Suite: Data Corruption & Storage Resilience.

Stress-tests MasterMappingStore, atomic_write_json, and OrionExporter against:
- Truncated, malformed, non-JSON, and binary garbage files in data/mappings/
- Root-level JSON schema violations (non-list, primitive values, None)
- Item-level schema corruption (malformed providers, invalid types, invalid IDs)
- Partial corruption survival (preserving valid records while discarding corrupt ones)
- Directory traversal & malicious characters in provider keys and slugs
- In-memory index integrity during transitive merges, duplicate collisions, and unindexing
- Atomic file write resilience and temporary file cleanup
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from orion_mapper.models.mapping import CanonicalMapping
from orion_mapper.storage.master import MasterMappingStore, atomic_write_json
from orion_mapper.storage.orion_exporter import OrionExporter


# ==============================================================================
# 1. MASTER DATASET CORRUPTION & RECOVERY TESTS
# ==============================================================================
class TestMasterDatasetCorruptionRecovery:
    """Tests how MasterMappingStore handles corrupted files in data/mappings/."""

    def test_load_truncated_unclosed_json_survives_gracefully(self, tmp_path: Path):
        """A half-written / interrupted JSON file should not raise uncaught JSONDecodeError."""
        movies_file = tmp_path / "movies.json"
        movies_file.write_text('[{"title": "Fight Club", "type": "movie", "tmdb_id": "550"', encoding="utf-8")

        store = MasterMappingStore(storage_dir=tmp_path)
        # Store should initialize with 0 items without crashing
        assert store.count("movie") == 0

        # Adding new item and saving should overwrite the corrupt file cleanly
        new_item = CanonicalMapping(
            title="Fight Club",
            type="movie",
            tmdb_id="550",
            imdb_id="tt0137523",
            providers={"serieskao": "fight-club"},
        )
        store.save_mapping(new_item)

        # Re-read and verify validity
        reloaded = MasterMappingStore(storage_dir=tmp_path)
        assert reloaded.count("movie") == 1
        assert reloaded.get_by_tmdb("550") is not None

    def test_load_binary_garbage_and_null_bytes(self, tmp_path: Path):
        """Binary junk or null bytes in movies.json/series.json should be handled cleanly."""
        movies_file = tmp_path / "movies.json"
        movies_file.write_bytes(b"\x00\xff\xfe\x01\x02\x03RandomBinaryGarbage")

        series_file = tmp_path / "series.json"
        series_file.write_bytes(b"\xef\xbb\xbf<!DOCTYPE html><html><body>502 Bad Gateway</body></html>")

        store = MasterMappingStore(storage_dir=tmp_path)
        assert store.count() == 0

    def test_load_empty_or_whitespace_files(self, tmp_path: Path):
        """0-byte files or whitespace files should result in empty store without error."""
        (tmp_path / "movies.json").write_text("", encoding="utf-8")
        (tmp_path / "series.json").write_text("   \n\t  \r\n", encoding="utf-8")

        store = MasterMappingStore(storage_dir=tmp_path)
        assert store.count() == 0

    def test_load_non_array_json_roots(self, tmp_path: Path):
        """JSON objects, numbers, booleans, or strings at root level should be safely discarded."""
        (tmp_path / "movies.json").write_text('{"status": "error", "items": []}', encoding="utf-8")
        (tmp_path / "series.json").write_text("42", encoding="utf-8")

        store = MasterMappingStore(storage_dir=tmp_path)
        assert store.count() == 0

    def test_partial_corruption_preserves_valid_records(self, tmp_path: Path):
        """
        Adversarial scenario:
        A 5-item movies.json file contains:
        1. Valid record
        2. Malformed item (provider is an integer instead of dict)
        3. Valid record
        4. Non-dict element (a string)
        5. Valid record
        Expected outcome:
        Store successfully recovers the 3 valid records, skips the 2 corrupt ones,
        and subsequent save produces a completely valid dataset.
        """
        mixed_data = [
            {
                "title": "Movie 1",
                "type": "movie",
                "tmdb_id": "101",
                "imdb_id": "tt0000101",
                "providers": {"prov_a": "slug-1"},
            },
            {
                "title": "Corrupt Type and Year",
                "type": "invalid_media_type",
                "year": 99999,
                "tmdb_id": "102",
                "providers": {"prov_b": "slug-2"},
            },
            {
                "title": "Movie 2",
                "type": "movie",
                "tmdb_id": "103",
                "imdb_id": "tt0000103",
                "providers": {"prov_b": "slug-2"},
            },
            "Not a dictionary record at all",
            {
                "title": "Movie 3",
                "type": "movie",
                "tmdb_id": "104",
                "imdb_id": "tt0000104",
                "providers": {"prov_c": "slug-3"},
            },
        ]

        movies_file = tmp_path / "movies.json"
        movies_file.write_text(json.dumps(mixed_data), encoding="utf-8")

        store = MasterMappingStore(storage_dir=tmp_path)
        assert store.count("movie") == 3
        assert store.get_by_tmdb("101") is not None
        assert store.get_by_tmdb("103") is not None
        assert store.get_by_tmdb("104") is not None
        assert store.get_by_tmdb("102") is None

        # Re-save and verify output is clean JSON array of length 3
        store.save()
        saved_content = json.loads(movies_file.read_text(encoding="utf-8"))
        assert isinstance(saved_content, list)
        assert len(saved_content) == 3
        assert [item["tmdb_id"] for item in saved_content] == ["101", "103", "104"]


# ==============================================================================
# 2. IN-MEMORY INDEX INTEGRITY & TRANSITIVE MERGING STRESS TESTS
# ==============================================================================
class TestInMemoryIndexIntegrity:
    """Stress tests index state, transitive merges, and key collision handling."""

    def test_transitive_coalescing_three_disjoint_entries(self, tmp_path: Path):
        """
        Adversarial Scenario:
        Three initially disjoint mappings:
        A: TMDB 550, providers: {serieskao: fight-club}
        B: IMDb tt0137523, providers: {poseidonhd2: fight-club-1999}
        C: providers: {gnula: club-de-la-pelea}
        When an item arriving with (TMDB 550, IMDb tt0137523, gnula: club-de-la-pelea) is added,
        all three must be coalesced into a single canonical mapping.
        """
        store = MasterMappingStore(storage_dir=tmp_path)

        item_a = CanonicalMapping(
            title="Fight Club",
            type="movie",
            tmdb_id="550",
            providers={"serieskao": "fight-club"},
        )
        item_b = CanonicalMapping(
            title="Fight Club 1999",
            type="movie",
            imdb_id="tt0137523",
            providers={"poseidonhd2": "fight-club-1999"},
        )
        item_c = CanonicalMapping(
            title="El Club de la Pelea",
            type="movie",
            providers={"gnula": "club-de-la-pelea"},
        )

        store.add_or_update(item_a)
        store.add_or_update(item_b)
        store.add_or_update(item_c)

        assert store.count("movie") == 3

        # Bridge mapping uniting A, B, and C
        bridge = CanonicalMapping(
            title="Fight Club",
            type="movie",
            tmdb_id="550",
            imdb_id="tt0137523",
            providers={"gnula": "club-de-la-pelea", "allcalidad": "fight-club-hd"},
        )

        merged = store.add_or_update(bridge)
        assert store.count("movie") == 1
        assert merged.tmdb_id == "550"
        assert merged.imdb_id == "tt0137523"
        assert len(merged.providers) == 4
        assert set(merged.providers.keys()) == {"serieskao", "poseidonhd2", "gnula", "allcalidad"}

        # Lookup by any of the 4 provider slugs must return the same unified entity
        assert store.get_by_provider_slug("serieskao", "fight-club") is merged
        assert store.get_by_provider_slug("poseidonhd2", "fight-club-1999") is merged
        assert store.get_by_provider_slug("gnula", "club-de-la-pelea") is merged
        assert store.get_by_provider_slug("allcalidad", "fight-club-hd") is merged
        assert store.get_by_tmdb("550") is merged
        assert store.get_by_imdb("tt0137523") is merged

    def test_id_formatting_whitespace_and_case_insensitivity(self, tmp_path: Path):
        """IMDb IDs, TMDB IDs, and provider names should normalize whitespace and casing."""
        store = MasterMappingStore(storage_dir=tmp_path)

        mapping = CanonicalMapping(
            title="Test Movie",
            type="movie",
            tmdb_id=" 12345 ",
            imdb_id=" TT0123456 ",
            providers={" SeriesKao ": " /my-slug/ "},
        )
        store.add_or_update(mapping)

        # Lookups with different casings and spacing
        assert store.get_by_tmdb("12345") is not None
        assert store.get_by_imdb("tt0123456") is not None
        assert store.get_by_imdb("TT0123456") is not None
        assert store.get_by_provider_slug("serieskao", "my-slug") is not None
        assert store.get_by_provider_slug("SERIESKAO", "/my-slug/") is not None

    def test_clear_properly_resets_all_indexes(self, tmp_path: Path):
        """Calling clear() must wipe primary store and all secondary indexes."""
        store = MasterMappingStore(storage_dir=tmp_path)
        store.add_or_update(
            CanonicalMapping(
                title="Movie",
                type="movie",
                tmdb_id="1",
                imdb_id="tt0000001",
                providers={"p": "s"},
            )
        )
        assert store.count() == 1
        store.clear()
        assert store.count() == 0
        assert store.get_by_tmdb("1") is None
        assert store.get_by_imdb("tt0000001") is None
        assert store.get_by_provider_slug("p", "s") is None


# ==============================================================================
# 3. ATOMIC WRITE ERROR INJECTION & DIRECTORY PROVISIONING
# ==============================================================================
class TestAtomicWriteFaultTolerance:
    """Stress tests atomic_write_json under I/O errors and permission failures."""

    def test_atomic_write_cleans_up_temp_file_on_fsync_or_replace_error(self, tmp_path: Path):
        """If os.replace fails (e.g. permission or I/O error), temp files should not linger."""
        target_file = tmp_path / "subdir" / "test.json"

        with patch("os.replace", side_effect=OSError("Disk write error")):
            with pytest.raises(OSError, match="Disk write error"):
                atomic_write_json(target_file, {"test": "data"})

        # Target file must not exist
        assert not target_file.exists()
        # No temporary .tmp-* files should be left behind
        temp_files = list(target_file.parent.glob(".test.json.tmp-*"))
        assert len(temp_files) == 0

    def test_atomic_write_nested_nonexistent_directories(self, tmp_path: Path):
        """Writing to deeply nested non-existent directory automatically provisions parents."""
        deep_target = tmp_path / "a" / "b" / "c" / "d" / "output.json"
        bytes_written = atomic_write_json(deep_target, {"status": "ok"})
        assert deep_target.exists()
        assert bytes_written > 0
        assert json.loads(deep_target.read_text(encoding="utf-8")) == {"status": "ok"}


# ==============================================================================
# 4. ORION EXPORTER ADVERSARIAL & SECURITY TESTS
# ==============================================================================
class TestOrionExporterAdversarialSecurity:
    """Tests OrionExporter against malicious input, path traversal, and special characters."""

    def test_exporter_handles_empty_or_sparse_mappings(self, tmp_path: Path):
        """Mappings with only title or missing both IDs should export only provider files safely."""
        out_dir = tmp_path / "orion"
        exporter = OrionExporter(output_dir=out_dir)

        sparse_mapping = CanonicalMapping(
            title="Sparse Movie",
            type="movie",
            tmdb_id=None,
            imdb_id=None,
            providers={"gnula": "sparse-movie"},
        )

        summary = exporter.export_mappings([sparse_mapping])
        assert summary.imdb_count == 0
        assert summary.tmdb_count == 0
        assert summary.provider_count == 1
        assert summary.total_files == 1

        prov_files = list((out_dir / "providers").glob("*.json"))
        assert len(prov_files) == 1
        data = json.loads(prov_files[0].read_text(encoding="utf-8"))
        assert data["slug"] == "sparse-movie"
        assert data["provider"] == "gnula"
        assert data["imdb_id"] is None
        assert data["tmdb_id"] is None

    def test_provider_key_encoding_url_safety_and_unicode_slugs(self, tmp_path: Path):
        """Unicode characters, slashes, and spaces in slugs must be encoded to URL-safe strings."""
        out_dir = tmp_path / "orion"
        exporter = OrionExporter(output_dir=out_dir)

        complex_mapping = CanonicalMapping(
            title="Película Épica: El Regreso / Año 2024",
            type="movie",
            tmdb_id="9999",
            providers={"serieskao": "película/épica-el-regreso:100%_completa"},
        )

        summary = exporter.export_mappings([complex_mapping])
        assert summary.provider_count == 1

        prov_files = list((out_dir / "providers").glob("*.json"))
        assert len(prov_files) == 1
        filename = prov_files[0].name
        # Filename must only contain base64url characters and .json
        assert "/" not in filename.replace(".json", "")
        assert "\\" not in filename
        assert "+" not in filename

        # Decode back
        prov, slug = exporter.decode_provider_key(filename.replace(".json", ""))
        assert prov == "serieskao"
        assert slug == "película/épica-el-regreso:100%_completa"

    def test_directory_traversal_attempts_in_slug_cannot_escape_output_dir(self, tmp_path: Path):
        """Malicious slugs like '../../../../etc/passwd' are base64-encoded and stay inside providers/."""
        out_dir = tmp_path / "orion"
        exporter = OrionExporter(output_dir=out_dir)

        traversal_mapping = CanonicalMapping(
            title="Malicious Slug Test",
            type="movie",
            tmdb_id="111",
            providers={"malicious": "../../../../../etc/passwd"},
        )

        exporter.export_mappings([traversal_mapping])

        # All generated files must strictly be inside out_dir / providers
        prov_files = list((out_dir / "providers").glob("*.json"))
        assert len(prov_files) == 1
        # Target file must reside in out_dir/providers
        assert prov_files[0].parent == out_dir / "providers"
        # No file created outside providers
        assert not (out_dir.parent / "passwd").exists()

    def test_exporter_idempotency_and_deterministic_overwrites(self, tmp_path: Path):
        """Exporting the same dataset multiple times should produce identical files and metrics."""
        out_dir = tmp_path / "orion"
        exporter = OrionExporter(output_dir=out_dir)

        mappings = [
            CanonicalMapping(
                title=f"Movie {i}",
                type="movie",
                tmdb_id=str(1000 + i),
                imdb_id=f"tt{1000000 + i}",
                providers={"prov": f"slug-{i}"},
            )
            for i in range(20)
        ]

        summary1 = exporter.export_mappings(mappings)
        summary2 = exporter.export_mappings(mappings)

        assert summary1.total_files == summary2.total_files == 60  # 20 imdb + 20 tmdb + 20 provider
        assert summary1.total_bytes == summary2.total_bytes
