from __future__ import annotations

import base64
import concurrent.futures
import json
import logging
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from orion_mapper.models.item import ContentType
from orion_mapper.models.mapping import CanonicalMapping
from orion_mapper.models.orion import (
    decode_provider_key,
    encode_provider_key,
)
from orion_mapper.storage.master import MasterMappingStore, atomic_write_json
from orion_mapper.storage.orion_exporter import OrionExporter

# ==============================================================================
# 1. ATOMIC FILE WRITES, PROCESS INTERRUPTION & CLEANUP HARNESS
# ==============================================================================

class TestAtomicWritesAdversarial:
    """Stress testing atomic writes, simulated failures, disk full, and cleanup."""

    def test_simulated_disk_full_during_write(self, tmp_path: Path):
        """Simulate ENOSPC (No space left on device) during file write."""
        target_file = tmp_path / "data" / "movies.json"

        # Write initial valid file
        atomic_write_json(target_file, [{"tmdb_id": "1", "title": "Original"}])
        initial_content = target_file.read_text(encoding="utf-8")
        assert "Original" in initial_content

        # Attempt atomic write with simulated ENOSPC in write()
        original_open = open

        def mock_open_disk_full(*args, **kwargs):
            handle = original_open(*args, **kwargs)
            if ".tmp-" in str(args[0]):
                def failing_write(data):
                    raise OSError(28, "No space left on device")
                handle.write = failing_write
            return handle

        with patch("builtins.open", side_effect=mock_open_disk_full):
            with pytest.raises(OSError) as exc_info:
                atomic_write_json(target_file, [{"tmdb_id": "2", "title": "New Movie"}])
            assert "No space left on device" in str(exc_info.value) or exc_info.value.errno == 28

        # Invariant 1: Target file must remain untouched and completely intact
        assert target_file.read_text(encoding="utf-8") == initial_content
        # Invariant 2: No .tmp files left in the directory
        tmp_files = list((tmp_path / "data").glob("*.tmp-*"))
        assert len(tmp_files) == 0

    def test_simulated_fsync_crash_and_cleanup(self, tmp_path: Path):
        """Simulate fsync system call failure and verify temp file cleanup."""
        target_file = tmp_path / "test.json"

        with patch("os.fsync", side_effect=OSError(5, "Input/output error")):
            with pytest.raises(OSError) as exc_info:
                atomic_write_json(target_file, {"key": "value"})
            assert "Input/output error" in str(exc_info.value)

        assert not target_file.exists()
        tmp_files = list(tmp_path.glob("*.tmp-*"))
        assert len(tmp_files) == 0

    def test_simulated_os_replace_failure(self, tmp_path: Path):
        """Simulate failure during atomic rename/replace (e.g. EPERM / EXDEV)."""
        target_file = tmp_path / "test.json"

        with patch("os.replace", side_effect=PermissionError("Permission denied")):
            with pytest.raises(PermissionError):
                atomic_write_json(target_file, {"hello": "world"})

        assert not target_file.exists()
        tmp_files = list(tmp_path.glob("*.tmp-*"))
        assert len(tmp_files) == 0

    def test_temp_file_unlink_oserror_handled(self, tmp_path: Path):
        """Simulate OSError during temp_file.unlink() in finally block."""
        target_file = tmp_path / "unlink_err.json"

        with patch("os.replace", side_effect=OSError("Replace error")):
            with patch.object(Path, "unlink", side_effect=OSError("Unlink permission error")):
                with pytest.raises(OSError):
                    atomic_write_json(target_file, {"data": 123})

    def test_non_serializable_data_does_not_create_file(self, tmp_path: Path):
        """Passing non-serializable objects must fail during json.dumps before file creation."""
        target_file = tmp_path / "invalid.json"

        # Sets are not JSON-serializable by standard json.dumps
        with pytest.raises(TypeError):
            atomic_write_json(target_file, {"invalid_set": {1, 2, 3}})

        assert not target_file.exists()
        tmp_files = list(tmp_path.glob("*.tmp-*"))
        assert len(tmp_files) == 0

    def test_deeply_nested_directory_creation(self, tmp_path: Path):
        """Ensure deeply nested parent directories (10 levels) are created cleanly."""
        nested_dir = tmp_path / "a" / "b" / "c" / "d" / "e" / "f" / "g" / "h" / "i" / "j"
        target_file = nested_dir / "target.json"

        bytes_written = atomic_write_json(target_file, {"nested": True})
        assert target_file.exists()
        assert bytes_written > 0
        assert json.loads(target_file.read_text(encoding="utf-8")) == {"nested": True}

    def test_rapid_sequential_writes_to_same_file(self, tmp_path: Path):
        """Perform 500 rapid atomic writes sequentially to test descriptor lifecycle and replacement."""
        target_file = tmp_path / "rapid.json"

        for i in range(500):
            data = {"iteration": i, "timestamp": time.time_ns()}
            bytes_written = atomic_write_json(target_file, data)
            assert bytes_written > 0

        final_data = json.loads(target_file.read_text(encoding="utf-8"))
        assert final_data["iteration"] == 499
        tmp_files = list(tmp_path.glob("*.tmp-*"))
        assert len(tmp_files) == 0

    def test_atomic_write_preserves_original_on_write_failure(self, tmp_path: Path):
        """
        Verify that atomic_write_json does NOT corrupt or truncate an existing file
        if an exception occurs prior to atomic rename.
        """
        target = tmp_path / "movies.json"
        original_data = [{"title": "Original Fight Club", "type": "movie", "tmdb_id": "550"}]
        atomic_write_json(target, original_data)
        original_bytes = target.read_bytes()

        # Simulate exception during os.replace
        with patch("os.replace", side_effect=PermissionError("Simulated write permission error")):
            with pytest.raises(PermissionError):
                atomic_write_json(target, [{"title": "Corrupted Data", "type": "movie"}])

        # Target file must remain untouched with original data
        assert target.read_bytes() == original_bytes
        # Temp files must be cleanly removed
        assert len(list(tmp_path.glob("*.tmp*"))) == 0


# ==============================================================================
# 2. MALFORMED & CORRUPTED FILES IN STORAGE DIRECTORY
# ==============================================================================

class TestMalformedAndCorruptedFiles:
    """Adversarial stress tests for malformed, corrupted, and invalid files in mappings storage."""

    @pytest.mark.parametrize(
        ("filename", "corrupted_content"),
        [
            ("movies.json", '[\n  {"title": "Fight Club", "type": "movie", "tmdb_'),  # Truncated key
            ("movies.json", '[\n  {"title": "Fight Club", "type": "movie"}'),  # Missing closing bracket
            ("movies.json", '[\n  {"title": "Fight Club", "type": "movie"},]'),  # Trailing comma
            ("movies.json", "["),  # Lone opening bracket
            ("movies.json", '{"movies": [{"title": "Fight Club", "type": "movie"}]}'),  # Root dict, not list
            ("movies.json", "42"),  # Root integer
            ("movies.json", '"just a raw string"'),  # Root string
            ("movies.json", "true"),  # Root boolean
            ("movies.json", "null"),  # Root null
            ("movies.json", "invalid json syntax {{{"),  # Complete garbage
            ("series.json", '[\n  {"title": "Zombieland Saga", "type": "series", "tmdb_'),
            ("series.json", '{"series": []}'),
            ("series.json", "12345"),
        ],
    )
    def test_load_corrupted_json_syntax_handled_gracefully(
        self, tmp_path: Path, filename: str, corrupted_content: str, caplog
    ):
        """
        Verify that corrupted/partial/non-array JSON in movies.json or series.json
        is handled gracefully via logging and does not raise an unhandled exception or crash the store.
        """
        file_path = tmp_path / filename
        file_path.write_text(corrupted_content, encoding="utf-8")

        with caplog.at_level(logging.WARNING):
            store = MasterMappingStore(storage_dir=tmp_path)

        assert store.count() == 0
        assert store.all_mappings() == []
        if filename == "movies.json":
            assert store.count("movie") == 0
            assert store.get_by_tmdb("550", "movie") is None
        else:
            assert store.count("series") == 0
            assert store.get_by_tmdb("82856", "series") is None

    @pytest.mark.parametrize(
        "invalid_item",
        [
            {},  # Missing title and type
            {"title": "No Type"},  # Missing type
            {"type": "movie"},  # Missing title
            {"title": "Bad Type", "type": "podcast"},  # Invalid ContentType
            {"title": "Negative Year", "type": "movie", "year": -50},  # Year < 1880
            {"title": "Futuristic Year", "type": "movie", "year": 3000},  # Year > 2100
            {"title": "Ancient Year", "type": "movie", "year": 1700},  # Year < 1880
        ],
    )
    def test_load_invalid_schema_records_handled_gracefully(
        self, tmp_path: Path, invalid_item: dict, caplog
    ):
        """
        Verify that records failing Pydantic model validation during store.load()
        are logged as warnings and do not crash MasterMappingStore initialization.
        """
        movies_file = tmp_path / "movies.json"
        movies_file.write_text(json.dumps([invalid_item]), encoding="utf-8")

        with caplog.at_level(logging.WARNING):
            store = MasterMappingStore(storage_dir=tmp_path)

        assert store.count("movie") == 0
        assert store.all_mappings() == []

    @pytest.mark.parametrize(
        ("record", "expected_providers", "expected_title", "expected_tmdb"),
        [
            (
                {"title": "Bad Providers List", "type": "movie", "providers": ["serieskao", "slug"]},
                {},
                "Bad Providers List",
                None,
            ),
            (
                {"title": 12345, "type": "movie", "tmdb_id": 9999},
                {},
                "12345",
                "9999",
            ),
            (
                {"title": "String Providers", "type": "movie", "providers": "invalid_string"},
                {},
                "String Providers",
                None,
            ),
        ],
    )
    def test_load_coercible_records_normalized_safely(
        self, tmp_path: Path, record: dict, expected_providers: dict, expected_title: str, expected_tmdb: str | None
    ):
        """
        Verify that records with non-dict providers or integer titles/IDs are safely
        coerced and normalized by CanonicalMapping without crashing.
        """
        movies_file = tmp_path / "movies.json"
        movies_file.write_text(json.dumps([record]), encoding="utf-8")

        store = MasterMappingStore(storage_dir=tmp_path)
        assert store.count("movie") == 1
        loaded = store.all_mappings("movie")[0]
        assert loaded.title == expected_title
        assert loaded.providers == expected_providers
        assert loaded.tmdb_id == expected_tmdb

    def test_load_non_utf8_binary_garbage(self, tmp_path: Path, caplog):
        """Verify that binary garbage in storage files is caught and logged."""
        movies_file = tmp_path / "movies.json"
        movies_file.write_bytes(b"\x80\x81\xff\xfe\x00\x01\x02\x03")

        with caplog.at_level(logging.WARNING):
            store = MasterMappingStore(storage_dir=tmp_path)

        assert store.count("movie") == 0
        assert "Error loading movies" in caplog.text

    def test_load_null_bytes_in_json(self, tmp_path: Path, caplog):
        """Verify that JSON with embedded null bytes is safely handled."""
        movies_file = tmp_path / "movies.json"
        movies_file.write_bytes(b"[\x00\x00\x00]")

        with caplog.at_level(logging.WARNING):
            store = MasterMappingStore(storage_dir=tmp_path)

        assert store.count("movie") == 0

    @pytest.mark.parametrize("content", ["", "   ", "\n\n\t\n  \r\n"])
    def test_load_empty_or_whitespace_files(self, tmp_path: Path, content: str):
        """Verify that empty files or whitespace-only files load as 0 mappings without error."""
        (tmp_path / "movies.json").write_text(content, encoding="utf-8")
        (tmp_path / "series.json").write_text(content, encoding="utf-8")

        store = MasterMappingStore(storage_dir=tmp_path)
        assert store.count() == 0
        assert store.all_mappings() == []

    def test_load_when_target_file_is_a_directory(self, tmp_path: Path, caplog):
        """Verify graceful handling if movies.json is inadvertently created as a directory."""
        movies_dir = tmp_path / "movies.json"
        movies_dir.mkdir(parents=True, exist_ok=True)

        with caplog.at_level(logging.WARNING):
            store = MasterMappingStore(storage_dir=tmp_path)

        assert store.count("movie") == 0
        assert "Error loading movies" in caplog.text


# ==============================================================================
# 3. PROVIDER KEY ENCODING, URL SAFETY & SPECIAL CHARACTERS
# ==============================================================================

class TestProviderKeyEncodingAndDecodings:
    """Adversarial tests for provider key encoding/decoding, slug edge cases, and path safety."""

    def test_empty_provider_dictionary_normalization(self):
        m1 = CanonicalMapping(title="Test", type="movie", providers={})
        assert m1.providers == {}

        m2 = CanonicalMapping(title="Test", type="movie", providers=None)
        assert m2.providers == {}

        m3 = CanonicalMapping.model_validate({"title": "Test", "type": "movie", "providers": 12345})
        assert m3.providers == {}

    def test_add_provider_ignores_empty_and_whitespace_values(self):
        m = CanonicalMapping(title="Test", type="movie")
        initial_updated_at = m.updated_at

        m.add_provider("", "")
        m.add_provider("   ", "   ")
        m.add_provider("serieskao", "")
        m.add_provider("", "valid-slug")

        assert m.providers == {}
        assert m.updated_at == initial_updated_at

        m.add_provider("  SeriesKao  ", "  /fight-club/  ")
        assert m.providers == {"serieskao": "fight-club"}
        assert m.updated_at >= initial_updated_at

    def test_exporter_with_empty_providers(self, tmp_path: Path):
        exporter = OrionExporter(output_dir=tmp_path)
        m = CanonicalMapping(
            tmdb_id="550",
            imdb_id="tt0137523",
            title="Fight Club",
            type="movie",
            providers={},
        )
        summary = exporter.export_mappings([m])

        assert summary.imdb_count == 1
        assert summary.tmdb_count == 1
        assert summary.provider_count == 0
        assert summary.total_files == 2
        assert len(list((tmp_path / "providers").glob("*.json"))) == 0

    @pytest.mark.parametrize(
        ("provider", "slug"),
        [
            ("serieskao", "el-niño-y-la-garza"),
            ("poseidonhd2", "película-canción-del-mar-año-2023"),
            ("gnula", "ñandú-y-cigüeña-en-español"),
            ("allcalidad", "corazón-de-dragón-1080p"),
            ("serieskao", "tokyo-ghoul-√a"),
            ("serieskao", "★-special-edition-🎬-🍿-✨"),
            ("poseidonhd2", "movie/with/slashes/and/subpaths"),
            ("gnula", "title?query=123&action=watch#fragment"),
            ("allcalidad", "title with spaces & special chars: [1080p]!@$^"),
            ("custom_prov", "../../../etc/passwd"),
            ("custom_prov", "C:\\Windows\\System32\\cmd.exe"),
            ("custom_prov", "very-long-slug-" + "x" * 2048),
            ("custom_prov", "~!@#$%^&*()_+`-={}|[]\\:\";'<>?,./"),
            ("  serieskao  ", "  slug-with-spaces  "),
            ("provider", ""),
            ("arabic_stream", "الأب-الروحي-الجزء-الأول"),
            ("cyrillic_hub", "брат-2-1999-фильм"),
            ("hindi_portal", "दंगल-full-movie-hd"),
        ],
    )
    def test_encode_decode_provider_key_adversarial_characters(self, provider: str, slug: str):
        """Verify URL-safe Base64 encoding without padding across diverse unicode strings."""
        encoded = encode_provider_key(provider, slug)

        assert "=" not in encoded
        assert "+" not in encoded
        assert "/" not in encoded

        dec_prov, dec_slug = decode_provider_key(encoded)
        assert dec_prov == provider.lower().strip()
        assert dec_slug == slug.strip()

    def test_provider_key_path_traversal_prevention_on_disk(self, tmp_path: Path):
        """Verify that malicious slugs attempting path traversal are safely encoded into flat filename keys."""
        exporter = OrionExporter(output_dir=tmp_path)
        mapping = CanonicalMapping(
            tmdb_id="999",
            title="Path Traversal Test",
            type="movie",
            providers={"evil_provider": "../../../etc/passwd"},
        )
        exporter.export_mappings([mapping])

        provider_files = list((tmp_path / "providers").iterdir())
        assert len(provider_files) == 1
        assert provider_files[0].is_file()
        assert provider_files[0].parent == tmp_path / "providers"

    def test_decode_provider_key_malformed_inputs(self):
        """Verify decode_provider_key behavior when given malformed or invalid base64."""
        with pytest.raises(ValueError):
            decode_provider_key("invalid_base64_symbols!@#$%^&*()")

        raw_no_colon = base64.urlsafe_b64encode(b"onlyprovidername").decode("ascii").rstrip("=")
        with pytest.raises(ValueError):
            decode_provider_key(raw_no_colon)


# ==============================================================================
# 4. CONCURRENCY, MULTI-THREADING & RACE CONDITIONS
# ==============================================================================

class TestConcurrencyAndRaceConditions:
    """Stress testing concurrent access, simultaneous store mutations, and multi-thread reads/writes."""

    def test_concurrent_multithreaded_add_or_update(self, tmp_path: Path):
        """50 concurrent threads adding 50 distinct mappings to the same MasterMappingStore."""
        store = MasterMappingStore(storage_dir=tmp_path / "concurrent_store")
        num_threads = 50

        def worker(idx: int):
            mapping = CanonicalMapping(
                tmdb_id=str(idx),
                imdb_id=f"tt{idx:07d}",
                title=f"Movie {idx}",
                type=ContentType.MOVIE,
                providers={"serieskao": f"slug-{idx}"},
            )
            store.add_or_update(mapping)

        with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [executor.submit(worker, i) for i in range(1, num_threads + 1)]
            for f in concurrent.futures.as_completed(futures):
                f.result()

        assert store.count("movie") == num_threads
        for i in range(1, num_threads + 1):
            assert store.get_by_tmdb(str(i), "movie") is not None
            assert store.get_by_imdb(f"tt{i:07d}", "movie") is not None
            assert store.get_by_provider_slug("serieskao", f"slug-{i}") is not None

    def test_concurrent_mutations_and_lookups(self, tmp_path: Path):
        """Simultaneous writers (distinct keys) and readers on the same MasterMappingStore instance."""
        store = MasterMappingStore(storage_dir=tmp_path / "concurrent_rw")
        stop_flag = False
        read_errors = []

        def writer(thread_id: int):
            for i in range(25):
                idx = thread_id * 1000 + i
                store.add_or_update(
                    CanonicalMapping(
                        tmdb_id=str(idx),
                        imdb_id=f"tt{idx:07d}",
                        title=f"Movie {idx}",
                        type=ContentType.MOVIE,
                        providers={"serieskao": f"slug-{idx}"},
                    )
                )
                time.sleep(0.001)

        def reader():
            while not stop_flag:
                try:
                    all_m = store.all_mappings()
                    for m in all_m:
                        if m.tmdb_id:
                            lookup = store.get_by_tmdb(m.tmdb_id, "movie")
                            assert lookup is not None
                except Exception as e:
                    read_errors.append(e)
                time.sleep(0.002)

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            r_futures = [executor.submit(reader) for _ in range(4)]
            w_futures = [executor.submit(writer, tid) for tid in range(4)]

            # Wait for writers to complete
            for wf in concurrent.futures.as_completed(w_futures):
                wf.result()

            stop_flag = True
            for rf in concurrent.futures.as_completed(r_futures):
                rf.result()

        assert len(read_errors) == 0
        assert store.count("movie") == 100

    def test_concurrent_save_and_add_or_update(self, tmp_path: Path):
        """Concurrent calls to save() while new items are being added via add_or_update()."""
        store = MasterMappingStore(storage_dir=tmp_path / "concurrent_save")
        errors = []

        def saver():
            for _ in range(20):
                try:
                    store.save()
                except Exception as e:
                    errors.append(e)
                time.sleep(0.005)

        def adder(start_idx: int):
            for i in range(start_idx, start_idx + 50):
                try:
                    store.add_or_update(
                        CanonicalMapping(
                            tmdb_id=str(i),
                            title=f"Title {i}",
                            type=ContentType.MOVIE,
                            providers={"prov": f"slug-{i}"},
                        )
                    )
                except Exception as e:
                    errors.append(e)
                time.sleep(0.002)

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
            futures = [
                executor.submit(saver),
                executor.submit(saver),
                executor.submit(adder, 1),
                executor.submit(adder, 100),
                executor.submit(adder, 200),
                executor.submit(adder, 300),
            ]
            for f in concurrent.futures.as_completed(futures):
                f.result()

        assert len(errors) == 0
        store.save()

        store2 = MasterMappingStore(storage_dir=tmp_path / "concurrent_save")
        assert store2.count("movie") == 200

    def test_concurrent_transitive_bridging_race(self, tmp_path: Path):
        """Multiple threads concurrently bridging disjoint TMDB and IMDb records."""
        store = MasterMappingStore(storage_dir=tmp_path / "bridging_race")

        # Add 10 disjoint pairs
        for i in range(10):
            store.add_or_update(CanonicalMapping(tmdb_id=str(1000 + i), title=f"M{i}", type="movie"))
            store.add_or_update(CanonicalMapping(imdb_id=f"tt{1000 + i}", title=f"M{i}", type="movie"))

        assert store.count("movie") == 20

        # Concurrently bridge all 10 pairs
        def bridge_worker(i: int):
            store.add_or_update(
                CanonicalMapping(
                    tmdb_id=str(1000 + i),
                    imdb_id=f"tt{1000 + i}",
                    title=f"M{i} Bridged",
                    type="movie",
                    providers={"prov": f"slug-{i}"},
                )
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(bridge_worker, i) for i in range(10)]
            for f in concurrent.futures.as_completed(futures):
                f.result()

        assert store.count("movie") == 10
        for i in range(10):
            m = store.get_by_tmdb(str(1000 + i), "movie")
            assert m is not None
            assert m.imdb_id == f"tt{1000 + i}"
            assert m.providers == {"prov": f"slug-{i}"}


# ==============================================================================
# 5. MULTI-PROVIDER TRANSITIVE BRIDGING & MERGE INVARIANTS
# ==============================================================================

class TestMultiProviderTransitiveBridging:
    """Adversarial tests for multi-provider transitive bridging under complex sequential influx."""

    def test_five_provider_linear_chain_sequential_bridging(self, tmp_path: Path):
        """
        Sequential update scenario across 5 providers:
        Step 1: SeriesKao provides TMDB ID 550 only
        Step 2: Gnula provides IMDb ID tt0137523 only
        Step 3: PoseidonHD2 bridges TMDB 550 and IMDb tt0137523
        Step 4: AllCalidad attaches to IMDb tt0137523
        Step 5: FutureProvider attaches to SeriesKao slug
        Verify all 5 providers merge into a single canonical entry with 100% lookup consistency.
        """
        store = MasterMappingStore(storage_dir=tmp_path)

        # Step 1: TMDB 550 only
        s1 = store.add_or_update(
            CanonicalMapping(
                tmdb_id="550",
                title="Fight Club",
                type="movie",
                providers={"serieskao": "fight-club-sk"},
                updated_at=1000,
            )
        )
        assert store.count("movie") == 1
        assert store.get_by_provider_slug("serieskao", "fight-club-sk") == s1

        # Step 2: IMDb tt0137523 only
        s2 = store.add_or_update(
            CanonicalMapping(
                imdb_id="tt0137523",
                title="El Club de la Lucha",
                type="movie",
                providers={"gnula": "fight-club-gn"},
                updated_at=2000,
            )
        )
        assert store.count("movie") == 2
        assert store.get_by_provider_slug("gnula", "fight-club-gn") == s2

        # Step 3: Bridge TMDB 550 and IMDb tt0137523
        s3 = store.add_or_update(
            CanonicalMapping(
                tmdb_id="550",
                imdb_id="tt0137523",
                title="Fight Club (1999)",
                type="movie",
                providers={"poseidonhd2": "fight-club-pos"},
                updated_at=3000,
            )
        )
        assert store.count("movie") == 1
        assert s3.tmdb_id == "550"
        assert s3.imdb_id == "tt0137523"
        assert s3.providers == {
            "serieskao": "fight-club-sk",
            "gnula": "fight-club-gn",
            "poseidonhd2": "fight-club-pos",
        }

        # Step 4: AllCalidad attaches via IMDb ID
        s4 = store.add_or_update(
            CanonicalMapping(
                imdb_id="tt0137523",
                title="Fight Club",
                type="movie",
                providers={"allcalidad": "fight-club-all"},
                updated_at=4000,
            )
        )
        assert store.count("movie") == 1
        assert "allcalidad" in s4.providers

        # Step 5: FutureProvider attaches via SeriesKao slug
        s5 = store.add_or_update(
            CanonicalMapping(
                title="Fight Club",
                type="movie",
                providers={"serieskao": "fight-club-sk", "futureprov": "fight-club-fut"},
                updated_at=5000,
            )
        )
        assert store.count("movie") == 1
        assert len(s5.providers) == 5
        assert s5.providers == {
            "serieskao": "fight-club-sk",
            "gnula": "fight-club-gn",
            "poseidonhd2": "fight-club-pos",
            "allcalidad": "fight-club-all",
            "futureprov": "fight-club-fut",
        }
        assert s5.updated_at == 5000

        # Verify all O(1) indexes return the same merged entity
        assert store.get_by_tmdb("550") == s5
        assert store.get_by_imdb("tt0137523") == s5
        for prov, slug in s5.providers.items():
            assert store.get_by_provider_slug(prov, slug) == s5

    def test_movie_and_series_segregation_under_identical_ids(self, tmp_path: Path):
        """Verify that a movie and a series sharing identical TMDB ID are strictly segregated."""
        store = MasterMappingStore(storage_dir=tmp_path)

        movie = CanonicalMapping(
            tmdb_id="100",
            imdb_id="tt0000100",
            title="The 100 Movie",
            type=ContentType.MOVIE,
            providers={"serieskao": "movie-100"},
        )
        series = CanonicalMapping(
            tmdb_id="100",
            imdb_id="tt0000100",
            title="The 100 Series",
            type=ContentType.SERIES,
            providers={"serieskao": "series-100"},
        )

        store.add_or_update(movie)
        store.add_or_update(series)

        assert store.count("movie") == 1
        assert store.count("series") == 1
        assert store.count() == 2

        store.save()

        movies_data = json.loads((tmp_path / "movies.json").read_text())
        series_data = json.loads((tmp_path / "series.json").read_text())

        assert len(movies_data) == 1
        assert len(series_data) == 1
        assert movies_data[0]["title"] == "The 100 Movie"
        assert series_data[0]["title"] == "The 100 Series"


# ==============================================================================
# 6. HUGE DATASETS (10,000 MAPPINGS) SCALE & DETERMINISTIC SORTING
# ==============================================================================

class TestHugeDatasetsScaleAndPerformance:
    """Stress testing 10,000 mappings serialization, O(1) index lookup latency, and export."""

    @pytest.fixture
    def large_dataset(self) -> list[CanonicalMapping]:
        """Generates 10,000 synthetic canonical mappings with mixed attributes."""
        items = []
        for i in range(1, 10001):
            c_type = ContentType.MOVIE if i % 2 == 0 else ContentType.SERIES
            items.append(
                CanonicalMapping(
                    tmdb_id=str(i),
                    imdb_id=f"tt{i:07d}",
                    title=f"Synthetic Media Title #{i} — Özel Bölüm 🌟",
                    type=c_type,
                    year=1980 + (i % 45),
                    providers={
                        "serieskao": f"synthetic-slug-{i}",
                        "poseidonhd2": f"media-id-{i}",
                        "gnula": f"pelicula-{i}",
                    },
                    updated_at=1700000000000 + i,
                )
            )
        return items

    def test_10k_mappings_store_save_and_load_performance(self, tmp_path: Path, large_dataset: list[CanonicalMapping]):
        """Benchmark 10,000 mappings serialization, atomic write, and deserialization."""
        store = MasterMappingStore(storage_dir=tmp_path / "scale_10k")

        for item in large_dataset:
            store.add_or_update(item)

        assert store.count("movie") == 5000
        assert store.count("series") == 5000
        assert store.count() == 10000

        # Benchmark save()
        t0 = time.perf_counter()
        store.save()
        save_duration = time.perf_counter() - t0

        movies_size = (tmp_path / "scale_10k" / "movies.json").stat().st_size
        series_size = (tmp_path / "scale_10k" / "series.json").stat().st_size

        assert movies_size > 1_000_000
        assert series_size > 1_000_000
        assert save_duration < 3.0, f"save() took too long: {save_duration:.3f}s"

        # Benchmark load()
        store2 = MasterMappingStore(storage_dir=tmp_path / "scale_10k")
        t1 = time.perf_counter()
        store2.load()
        load_duration = time.perf_counter() - t1

        assert store2.count() == 10000
        assert load_duration < 3.0, f"load() took too long: {load_duration:.3f}s"

    def test_10k_mappings_o1_lookup_latency(self, tmp_path: Path, large_dataset: list[CanonicalMapping]):
        """Verify that indexed lookups on 10,000 items remain truly O(1) (< 50 microseconds per lookup)."""
        store = MasterMappingStore(storage_dir=tmp_path / "lookup_latency")
        for item in large_dataset:
            store.add_or_update(item)

        sample_ids = [1, 500, 2500, 5000, 7500, 9999, 10000]

        t0 = time.perf_counter()
        for idx in sample_ids:
            c_type = "movie" if idx % 2 == 0 else "series"
            res_tmdb = store.get_by_tmdb(str(idx), c_type)
            res_imdb = store.get_by_imdb(f"tt{idx:07d}", c_type)
            res_prov = store.get_by_provider_slug("serieskao", f"synthetic-slug-{idx}")

            assert res_tmdb is not None
            assert res_imdb is not None
            assert res_prov is not None

        total_time = time.perf_counter() - t0
        avg_time_per_lookup = total_time / (len(sample_ids) * 3)
        assert avg_time_per_lookup < 0.001, f"Lookup took too long: {avg_time_per_lookup * 1e6:.2f} µs"

    def test_orion_exporter_1000_mappings_scale(self, tmp_path: Path):
        """Export 1,000 mappings (4,000 files) via OrionExporter and verify filesystem integrity."""
        exporter = OrionExporter(output_dir=tmp_path / "orion_export_1k")
        mappings = []
        for i in range(1, 1001):
            mappings.append(
                CanonicalMapping(
                    tmdb_id=str(i),
                    imdb_id=f"tt{i:07d}",
                    title=f"Export Item {i}",
                    type=ContentType.MOVIE,
                    providers={"serieskao": f"sk-{i}", "gnula": f"gn-{i}"},
                )
            )

        summary = exporter.export_mappings(mappings)

        assert summary.imdb_count == 1000
        assert summary.tmdb_count == 1000
        assert summary.provider_count == 2000
        assert summary.total_files == 4000
        assert summary.total_bytes > 0
        assert summary.duration_ms > 0

        assert (tmp_path / "orion_export_1k" / "tmdb" / "1.json").exists()
        assert (tmp_path / "orion_export_1k" / "tmdb" / "1000.json").exists()
        assert (tmp_path / "orion_export_1k" / "imdb" / "tt0000001.json").exists()
        assert (tmp_path / "orion_export_1k" / "imdb" / "tt0001000.json").exists()

        tmp_files = list((tmp_path / "orion_export_1k").rglob("*.tmp-*"))
        assert len(tmp_files) == 0

    def test_deterministic_sort_ordering_at_scale(self, tmp_path: Path):
        """Verify sorting invariants when TMDB IDs contain numeric strings, None, and title tie-breakers."""
        store = MasterMappingStore(storage_dir=tmp_path / "sort_scale")

        items = [
            CanonicalMapping(tmdb_id=None, imdb_id="tt0000003", title="Zebra", type="movie"),
            CanonicalMapping(tmdb_id="100", imdb_id="tt0000002", title="Beta", type="movie"),
            CanonicalMapping(tmdb_id="5", imdb_id="tt0000001", title="Gamma", type="movie"),
            CanonicalMapping(tmdb_id=None, imdb_id="tt0000004", title="Delta", type="movie"),
            CanonicalMapping(tmdb_id="25", imdb_id="tt0000005", title="Alpha", type="movie"),
            CanonicalMapping(tmdb_id=None, imdb_id="tt0000006", title="Alpha", type="movie"),
        ]

        for it in items:
            store.add_or_update(it)

        store.save()

        saved_data = json.loads((tmp_path / "sort_scale" / "movies.json").read_text(encoding="utf-8"))

        # Expected sort order:
        # 1. TMDB 5 ("Gamma")
        # 2. TMDB 25 ("Alpha")
        # 3. TMDB 100 ("Beta")
        # 4. None TMDB ("Alpha", imdb tt0000006)
        # 5. None TMDB ("Delta", imdb tt0000004)
        # 6. None TMDB ("Zebra", imdb tt0000003)
        expected_titles = ["Gamma", "Alpha", "Beta", "Alpha", "Delta", "Zebra"]
        actual_titles = [m["title"] for m in saved_data]
        assert actual_titles == expected_titles


# ==============================================================================
# 7. UNICODE & FORMATTING COMPLIANCE
# ==============================================================================

class TestUnicodeAndFormattingCompliance:
    """Stress testing complex multilingual unicode, emoji, and JSON format standards."""

    def test_json_uniqueness_and_ensure_ascii_false(self, tmp_path: Path):
        """Verify UTF-8 characters are saved verbatim without \\uXXXX escaping."""
        target_file = tmp_path / "unicode.json"
        unicode_data = {
            "spanish": "El laberinto del fauno: Niños, Sueños & Monstruos",
            "japanese": "新世紀エヴァンゲリオン",
            "emoji": "🔥🍿🚀🌟",
            "arabic": "مرحبا بالعالم",
        }

        atomic_write_json(target_file, unicode_data)
        raw_bytes = target_file.read_bytes()
        raw_text = target_file.read_text(encoding="utf-8")

        assert b"\\u" not in raw_bytes
        assert "新世紀エヴァンゲリオン" in raw_text
        assert "🔥🍿🚀🌟" in raw_text
        assert "Niños" in raw_text

    def test_json_formatting_compliance_indent_and_trailing_newline(self, tmp_path: Path):
        """Verify standard 2-space indentation and POSIX trailing newline."""
        target_file = tmp_path / "format_check.json"
        atomic_write_json(target_file, {"b_key": 2, "a_key": 1})

        raw_text = target_file.read_text(encoding="utf-8")
        assert raw_text.endswith("\n")
        assert not raw_text.endswith("\n\n")

        lines = raw_text.splitlines()
        assert lines[0] == "{"
        assert lines[1] == '  "a_key": 1,'
        assert lines[2] == '  "b_key": 2'
        assert lines[3] == "}"

    def test_extreme_and_malformed_lookup_inputs(self, tmp_path: Path):
        """Verify get_by_tmdb, get_by_imdb, and get_by_provider_slug with malformed inputs."""
        store = MasterMappingStore(storage_dir=tmp_path / "extreme_lookups")
        mapping = CanonicalMapping(
            tmdb_id="550",
            imdb_id="tt0137523",
            title="Fight Club",
            type="movie",
            providers={"serieskao": "fight-club"},
        )
        store.add_or_update(mapping)

        # Extreme TMDB lookups
        assert store.get_by_tmdb(None) is None
        assert store.get_by_tmdb("") is None
        assert store.get_by_tmdb("   ") is None
        assert store.get_by_tmdb(0) is None
        assert store.get_by_tmdb(-550) is None
        assert store.get_by_tmdb("  550  ") == mapping
        assert store.get_by_tmdb(550) == mapping

        # Extreme IMDb lookups
        assert store.get_by_imdb(None) is None
        assert store.get_by_imdb("") is None
        assert store.get_by_imdb("   ") is None
        assert store.get_by_imdb("tt") is None
        assert store.get_by_imdb("0137523") == mapping
        assert store.get_by_imdb("  TT0137523  ") == mapping

        # Extreme Provider lookups
        assert store.get_by_provider_slug(None, None) is None
        assert store.get_by_provider_slug("serieskao", None) is None
        assert store.get_by_provider_slug(None, "fight-club") is None
        assert store.get_by_provider_slug("", "") is None
        assert store.get_by_provider_slug("  SeriesKao  ", "  /fight-club/  ") == mapping
