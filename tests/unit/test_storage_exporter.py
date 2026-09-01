from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from orion_mapper.models.item import ContentType
from orion_mapper.models.mapping import CanonicalMapping
from orion_mapper.storage.master import MasterMappingStore, atomic_write_json
from orion_mapper.storage.orion_exporter import ExportSummary, OrionExporter


class TestMasterMappingStore:
    def test_init_provisions_directory_if_missing(self, tmp_path: Path):
        storage_dir = tmp_path / "new_nested" / "mappings"
        assert not storage_dir.exists()
        store = MasterMappingStore(storage_dir=storage_dir)
        assert storage_dir.exists()
        assert store.count() == 0

    def test_add_or_update_movie_and_series_segregation(self, temp_mappings_dir: Path):
        store = MasterMappingStore(storage_dir=temp_mappings_dir)
        movie = CanonicalMapping(
            tmdb_id="550",
            imdb_id="tt0137523",
            title="Fight Club",
            type=ContentType.MOVIE,
            year=1999,
            providers={"serieskao": "fight-club"},
        )
        series = CanonicalMapping(
            tmdb_id="82856",
            imdb_id="tt15486",
            title="Zombieland Saga",
            type=ContentType.SERIES,
            year=2018,
            providers={"serieskao": "zombieland-saga"},
        )

        store.save_mapping(movie)
        store.save_mapping(series)

        movies_file = temp_mappings_dir / "movies.json"
        series_file = temp_mappings_dir / "series.json"

        assert movies_file.exists()
        assert series_file.exists()

        movies_data = json.loads(movies_file.read_text(encoding="utf-8"))
        series_data = json.loads(series_file.read_text(encoding="utf-8"))

        assert len(movies_data) == 1
        assert movies_data[0]["tmdb_id"] == "550"
        assert movies_data[0]["type"] == "movie"

        assert len(series_data) == 1
        assert series_data[0]["tmdb_id"] == "82856"
        assert series_data[0]["type"] == "series"

    def test_save_mapping_immediate_persistence(self, temp_mappings_dir: Path):
        store = MasterMappingStore(storage_dir=temp_mappings_dir)
        m = CanonicalMapping(
            tmdb_id="550",
            imdb_id="tt0137523",
            title="Fight Club",
            type="movie",
            providers={"serieskao": "fight-club"},
        )
        store.save_mapping(m)

        # New instance pointing to same directory loads persisted data
        new_store = MasterMappingStore(storage_dir=temp_mappings_dir)
        loaded = new_store.get_by_tmdb("550", "movie")
        assert loaded is not None
        assert loaded.title == "Fight Club"
        assert loaded.providers == {"serieskao": "fight-club"}

    def test_get_by_tmdb_lookup(self, temp_mappings_dir: Path):
        store = MasterMappingStore(storage_dir=temp_mappings_dir)
        movie = CanonicalMapping(
            tmdb_id="550",
            imdb_id="tt0137523",
            title="Fight Club",
            type=ContentType.MOVIE,
        )
        series = CanonicalMapping(
            tmdb_id="550",
            title="Fight Club Series",
            type=ContentType.SERIES,
        )
        store.add_or_update(movie)
        store.add_or_update(series)

        # Lookup as string, int, and type-specific
        assert store.get_by_tmdb("550", ContentType.MOVIE) == movie
        assert store.get_by_tmdb(550, "movie") == movie
        assert store.get_by_tmdb("550", ContentType.SERIES) == series
        assert store.get_by_tmdb(550, "series") == series

        # Nonexistent lookup
        assert store.get_by_tmdb("999999") is None
        assert store.get_by_tmdb(None) is None
        assert store.get_by_tmdb("") is None

    def test_get_by_imdb_lookup(self, temp_mappings_dir: Path):
        store = MasterMappingStore(storage_dir=temp_mappings_dir)
        movie = CanonicalMapping(
            tmdb_id="550",
            imdb_id="tt0137523",
            title="Fight Club",
            type=ContentType.MOVIE,
        )
        store.add_or_update(movie)

        assert store.get_by_imdb("tt0137523") == movie
        assert store.get_by_imdb("TT0137523") == movie
        assert store.get_by_imdb("0137523") == movie  # auto-prepends 'tt' for numeric
        assert store.get_by_imdb("tt0137523", "movie") == movie
        assert store.get_by_imdb("tt0137523", "series") is None
        assert store.get_by_imdb("tt9999999") is None
        assert store.get_by_imdb(None) is None

    def test_get_by_provider_slug_lookup(self, temp_mappings_dir: Path):
        store = MasterMappingStore(storage_dir=temp_mappings_dir)
        movie = CanonicalMapping(
            tmdb_id="550",
            title="Fight Club",
            type=ContentType.MOVIE,
            providers={"serieskao": "fight-club-slug", "gnula": "pelicula-fight-club"},
        )
        store.add_or_update(movie)

        assert store.get_by_provider_slug("serieskao", "fight-club-slug") == movie
        assert store.get_by_provider_slug("SeriesKao", "/fight-club-slug/") == movie
        assert store.get_by_provider_slug("GNULA", "pelicula-fight-club") == movie
        assert store.get_by_provider_slug("unknown", "fight-club-slug") is None
        assert store.get_by_provider_slug(None, "fight-club-slug") is None

    def test_add_or_update_merging_providers(self, temp_mappings_dir: Path):
        store = MasterMappingStore(storage_dir=temp_mappings_dir)
        m1 = CanonicalMapping(
            tmdb_id="550",
            title="Fight Club",
            type="movie",
            providers={"serieskao": "slug1"},
            updated_at=1000,
        )
        store.add_or_update(m1)

        m2 = CanonicalMapping(
            tmdb_id="550",
            title="Fight Club",
            type="movie",
            providers={"poseidonhd2": "slug2"},
            updated_at=2000,
        )
        merged = store.add_or_update(m2)

        assert merged.providers == {"serieskao": "slug1", "poseidonhd2": "slug2"}
        assert merged.updated_at == 2000
        assert store.count("movie") == 1
        assert store.get_by_provider_slug("poseidonhd2", "slug2") == merged

    def test_add_or_update_transitive_bridging(self, temp_mappings_dir: Path):
        store = MasterMappingStore(storage_dir=temp_mappings_dir)
        # Entry 1: known TMDB ID but unknown IMDb
        m1 = CanonicalMapping(
            tmdb_id="550",
            imdb_id=None,
            title="Fight Club",
            type="movie",
            providers={"serieskao": "fight-club-sk"},
        )
        store.add_or_update(m1)

        # Entry 2: known IMDb ID but unknown TMDB
        m2 = CanonicalMapping(
            tmdb_id=None,
            imdb_id="tt0137523",
            title="Fight Club",
            type="movie",
            providers={"gnula": "fight-club-gn"},
        )
        store.add_or_update(m2)
        assert store.count("movie") == 2

        # Entry 3: Bridges both IDs
        m3 = CanonicalMapping(
            tmdb_id="550",
            imdb_id="tt0137523",
            title="Fight Club",
            type="movie",
            providers={"poseidonhd2": "fight-club-pos"},
        )
        bridged = store.add_or_update(m3)

        assert store.count("movie") == 1
        assert bridged.tmdb_id == "550"
        assert bridged.imdb_id == "tt0137523"
        assert bridged.providers == {
            "serieskao": "fight-club-sk",
            "gnula": "fight-club-gn",
            "poseidonhd2": "fight-club-pos",
        }
        assert store.get_by_tmdb("550") == bridged
        assert store.get_by_imdb("tt0137523") == bridged

    def test_multi_match_transitive_bridging_across_three_disjoint_mappings(
        self, temp_mappings_dir: Path
    ):
        store = MasterMappingStore(storage_dir=temp_mappings_dir)
        m1 = CanonicalMapping(
            tmdb_id="100",
            title="Entity 100",
            type="movie",
            providers={"serieskao": "sk-100"},
        )
        m2 = CanonicalMapping(
            imdb_id="tt0000100",
            title="Entity 100",
            type="movie",
            providers={"gnula": "gn-100"},
        )
        m3 = CanonicalMapping(
            title="Entity 100",
            type="movie",
            providers={"allcalidad": "all-100"},
        )
        store.add_or_update(m1)
        store.add_or_update(m2)
        store.add_or_update(m3)
        assert store.count("movie") == 3

        # Bridge mapping linking all 3 providers
        m_bridge = CanonicalMapping(
            title="Entity 100",
            type="movie",
            providers={
                "serieskao": "sk-100",
                "gnula": "gn-100",
                "allcalidad": "all-100",
                "poseidonhd2": "pos-100",
            },
        )
        coalesced = store.add_or_update(m_bridge)

        assert store.count("movie") == 1
        assert coalesced.tmdb_id == "100"
        assert coalesced.imdb_id == "tt0000100"
        assert set(coalesced.providers.keys()) == {
            "serieskao",
            "gnula",
            "allcalidad",
            "poseidonhd2",
        }
        assert store.get_by_tmdb("100", "movie") == coalesced
        assert store.get_by_imdb("tt0000100", "movie") == coalesced
        assert store.get_by_provider_slug("serieskao", "sk-100") == coalesced
        assert store.get_by_provider_slug("gnula", "gn-100") == coalesced
        assert store.get_by_provider_slug("allcalidad", "all-100") == coalesced
        assert store.get_by_provider_slug("poseidonhd2", "pos-100") == coalesced

    def test_fault_tolerant_loading_skips_malformed_entries_without_aborting(
        self, temp_mappings_dir: Path
    ):
        movies_file = temp_mappings_dir / "movies.json"
        raw_items = [
            {"title": "Valid Movie 1", "type": "movie", "tmdb_id": "1"},
            {"title": "Malformed Enum", "type": "INVALID_TYPE_ENUM"},
            {"title": "Valid Movie 3", "type": "movie", "tmdb_id": "3"},
            12345,  # Non-dict item
            {"title": "Valid Movie 4", "type": "movie", "tmdb_id": "4"},
        ]
        movies_file.write_text(json.dumps(raw_items), encoding="utf-8")

        store = MasterMappingStore(storage_dir=temp_mappings_dir)
        assert store.count("movie") == 3
        assert store.get_by_tmdb("1", "movie") is not None
        assert store.get_by_tmdb("3", "movie") is not None
        assert store.get_by_tmdb("4", "movie") is not None

        # Verify saving safely retains all valid items
        store.save()
        saved_data = json.loads(movies_file.read_text(encoding="utf-8"))
        assert len(saved_data) == 3

    def test_roundtrip_load_and_save_fidelity(self, temp_mappings_dir: Path):
        store = MasterMappingStore(storage_dir=temp_mappings_dir)
        m1 = CanonicalMapping(
            tmdb_id="550",
            imdb_id="tt0137523",
            title="Fight Club",
            type="movie",
            year=1999,
            providers={"serieskao": "fight-club"},
            updated_at=1700000000000,
        )
        m2 = CanonicalMapping(
            tmdb_id="82856",
            imdb_id="tt15486",
            title="Zombieland Saga",
            type="series",
            year=2018,
            providers={"serieskao": "zombieland-saga"},
            updated_at=1700000000000,
        )
        store.save_mapping(m1)
        store.save_mapping(m2)

        store2 = MasterMappingStore(storage_dir=temp_mappings_dir)
        store2.load()

        assert store2.count() == 2
        assert store2.get_by_tmdb("550", "movie").model_dump() == m1.model_dump()
        assert store2.get_by_tmdb("82856", "series").model_dump() == m2.model_dump()

    def test_deterministic_numeric_tmdb_sorting(self, temp_mappings_dir: Path):
        store = MasterMappingStore(storage_dir=temp_mappings_dir)
        store.save_mapping(
            CanonicalMapping(tmdb_id="1000", title="M1000", type="movie", providers={})
        )
        store.save_mapping(
            CanonicalMapping(tmdb_id="550", title="M550", type="movie", providers={})
        )
        store.save_mapping(CanonicalMapping(tmdb_id="25", title="M25", type="movie", providers={}))
        store.save_mapping(
            CanonicalMapping(tmdb_id=None, title="Alpha", type="movie", providers={})
        )

        raw_data = json.loads((temp_mappings_dir / "movies.json").read_text(encoding="utf-8"))
        tmdb_ids = [item.get("tmdb_id") for item in raw_data]

        assert tmdb_ids == ["25", "550", "1000", None]

    def test_deterministic_sorted_json_keys(self, temp_mappings_dir: Path):
        store = MasterMappingStore(storage_dir=temp_mappings_dir)
        store.save_mapping(
            CanonicalMapping(
                tmdb_id="550",
                imdb_id="tt0137523",
                title="Fight Club",
                type="movie",
                year=1999,
                providers={"z_prov": "z_slug", "a_prov": "a_slug"},
            )
        )

        content = (temp_mappings_dir / "movies.json").read_text(encoding="utf-8")
        assert content.endswith("\n")

        # Top-level item keys must be alphabetical: imdb_id, providers, title, tmdb_id, type, updated_at, year
        data = json.loads(content)
        item = data[0]
        keys = list(item.keys())
        assert keys == sorted(keys)

        # Provider dict keys must also be alphabetical
        prov_keys = list(item["providers"].keys())
        assert prov_keys == ["a_prov", "z_prov"]

    def test_atomic_write_cleanup_on_success_and_failure(self, temp_mappings_dir: Path):
        store = MasterMappingStore(storage_dir=temp_mappings_dir)
        store.save_mapping(
            CanonicalMapping(tmdb_id="550", title="Fight Club", type="movie", providers={})
        )

        # No .tmp files left on success
        tmp_files = list(temp_mappings_dir.glob("*.tmp*"))
        assert len(tmp_files) == 0

        # Simulate write failure during flush/os.replace
        with patch("os.replace", side_effect=OSError("Disk write simulated failure")):
            with pytest.raises(OSError):
                atomic_write_json(temp_mappings_dir / "error.json", {"test": "data"})

        # Verify no orphaned temp files
        tmp_files_after = list(temp_mappings_dir.glob("*.tmp*"))
        assert len(tmp_files_after) == 0

    def test_utf8_non_ascii_preservation(self, temp_mappings_dir: Path):
        store = MasterMappingStore(storage_dir=temp_mappings_dir)
        special_title = "El laberinto del fauno: Niños, Sueños & Monstruos ★ — Español"
        store.save_mapping(
            CanonicalMapping(
                tmdb_id="1417",
                title=special_title,
                type="movie",
                providers={"serieskao": "el-laberinto-del-fauno"},
            )
        )

        raw_text = (temp_mappings_dir / "movies.json").read_text(encoding="utf-8")
        assert "El laberinto del fauno: Niños, Sueños & Monstruos ★ — Español" in raw_text
        # Ensure unicode is not escaped like \u00f1
        assert "\\u00f1" not in raw_text

        loaded = store.get_by_tmdb("1417", "movie")
        assert loaded.title == special_title

    def test_empty_files_handling(self, temp_mappings_dir: Path):
        (temp_mappings_dir / "movies.json").write_text("[]", encoding="utf-8")
        (temp_mappings_dir / "series.json").write_text("", encoding="utf-8")

        store = MasterMappingStore(storage_dir=temp_mappings_dir)
        assert store.load_all() == []
        assert store.count() == 0

    def test_count_and_all_mappings_filtering(self, temp_mappings_dir: Path):
        store = MasterMappingStore(storage_dir=temp_mappings_dir)
        store.add_or_update(
            CanonicalMapping(tmdb_id="1", title="Movie 1", type="movie", providers={})
        )
        store.add_or_update(
            CanonicalMapping(tmdb_id="2", title="Movie 2", type="movie", providers={})
        )
        store.add_or_update(
            CanonicalMapping(tmdb_id="3", title="Series 1", type="series", providers={})
        )

        assert store.count() == 3
        assert store.count("movie") == 2
        assert store.count("series") == 1
        assert len(store.all_mappings("movie")) == 2
        assert len(store.all_mappings("series")) == 1
        assert len(store.all_mappings()) == 3

    def test_clear_resets_all_indexes(self, temp_mappings_dir: Path):
        store = MasterMappingStore(storage_dir=temp_mappings_dir)
        store.add_or_update(
            CanonicalMapping(
                tmdb_id="550",
                imdb_id="tt0137523",
                title="Fight Club",
                type="movie",
                providers={"serieskao": "fight-club"},
            )
        )
        assert store.count() == 1
        store.clear()
        assert store.count() == 0
        assert store.get_by_tmdb("550") is None
        assert store.get_by_imdb("tt0137523") is None
        assert store.get_by_provider_slug("serieskao", "fight-club") is None


class TestOrionExporter:
    def test_encode_decode_roundtrip(self):
        cases = [
            ("serieskao", "el-club-de-la-lucha"),
            ("poseidonhd2", "zombieland-saga"),
            ("gnula", "pelicula-el-club-de-la-lucha"),
            ("allcalidad", "zombieland-saga-season-1"),
            ("custom_prov", "movie/123:special"),
        ]
        for prov, slug in cases:
            encoded = OrionExporter.encode_provider_key(prov, slug)
            assert "=" not in encoded
            decoded_prov, decoded_slug = OrionExporter.decode_provider_key(encoded)
            assert decoded_prov == prov.lower()
            assert decoded_slug == slug

    def test_base64url_no_padding_equals(self):
        for i in range(1, 65):
            slug = "x" * i
            key = OrionExporter.encode_provider_key("provider", slug)
            assert "=" not in key

    def test_export_directory_structure_provisioning(self, temp_orion_dir: Path):
        exporter = OrionExporter(output_dir=temp_orion_dir)
        summary = exporter.export_mappings([])
        assert (temp_orion_dir / "imdb").is_dir()
        assert (temp_orion_dir / "tmdb").is_dir()
        assert (temp_orion_dir / "providers").is_dir()
        assert summary.total_files == 0
        assert summary.total_bytes == 0

    def test_export_imdb_index_json_schema(self, temp_orion_dir: Path):
        exporter = OrionExporter(output_dir=temp_orion_dir)
        mapping = CanonicalMapping(
            tmdb_id="82856",
            imdb_id="tt15486",
            title="Zombieland Saga",
            type="series",
            year=2018,
            providers={"serieskao": "zombieland-saga", "poseidonhd2": "zombieland-saga"},
            updated_at=1700000000000,
        )
        exporter.export_mappings([mapping])

        target_file = temp_orion_dir / "imdb" / "tt15486.json"
        assert target_file.exists()

        data = json.loads(target_file.read_text(encoding="utf-8"))
        assert data["imdb_id"] == "tt15486"
        assert data["tmdb_id"] == "82856"
        assert data["type"] == "series"
        assert data["providers"] == {
            "poseidonhd2": "zombieland-saga",
            "serieskao": "zombieland-saga",
        }
        assert data["updatedAt"] == 1700000000000

    def test_export_tmdb_index_json_schema(self, temp_orion_dir: Path):
        exporter = OrionExporter(output_dir=temp_orion_dir)
        mapping = CanonicalMapping(
            tmdb_id="550",
            imdb_id="tt0137523",
            title="Fight Club",
            type="movie",
            year=1999,
            providers={"serieskao": "fight-club"},
            updated_at=1700000000000,
        )
        exporter.export_mappings([mapping])

        target_file = temp_orion_dir / "tmdb" / "550.json"
        assert target_file.exists()

        data = json.loads(target_file.read_text(encoding="utf-8"))
        assert data["tmdb_id"] == "550"
        assert data["imdb_id"] == "tt0137523"
        assert data["updatedAt"] == 1700000000000

    def test_export_provider_index_json_schema(self, temp_orion_dir: Path):
        exporter = OrionExporter(output_dir=temp_orion_dir)
        mapping = CanonicalMapping(
            tmdb_id="550",
            imdb_id="tt0137523",
            title="Fight Club",
            type="movie",
            year=1999,
            providers={"serieskao": "el-club-de-la-lucha"},
            updated_at=1700000000000,
        )
        exporter.export_mappings([mapping])

        key = OrionExporter.encode_provider_key("serieskao", "el-club-de-la-lucha")
        target_file = temp_orion_dir / "providers" / f"{key}.json"
        assert target_file.exists()

        data = json.loads(target_file.read_text(encoding="utf-8"))
        assert data["provider"] == "serieskao"
        assert data["slug"] == "el-club-de-la-lucha"
        assert data["imdb_id"] == "tt0137523"
        assert data["tmdb_id"] == "550"
        assert data["type"] == "movie"
        assert data["updatedAt"] == 1700000000000

    def test_export_strict_lowercase_normalization(self, temp_orion_dir: Path):
        exporter = OrionExporter(output_dir=temp_orion_dir)
        mapping = CanonicalMapping(
            tmdb_id="550",
            imdb_id="TT0137523",
            title="Fight Club",
            type="movie",
            year=1999,
            providers={"SeriesKao": "Fight-Club-Slug"},
            updated_at=1700000000000,
        )
        exporter.export_mappings([mapping])

        assert (temp_orion_dir / "imdb" / "tt0137523.json").exists()
        assert not (temp_orion_dir / "imdb" / "TT0137523.json").exists()

        expected_key = OrionExporter.encode_provider_key("serieskao", "Fight-Club-Slug")
        assert (temp_orion_dir / "providers" / f"{expected_key}.json").exists()

    def test_export_partial_identifiers(self, temp_orion_dir: Path):
        exporter = OrionExporter(output_dir=temp_orion_dir)
        # Missing IMDb ID
        m1 = CanonicalMapping(
            tmdb_id="550",
            imdb_id=None,
            title="Fight Club",
            type="movie",
            providers={"serieskao": "fight-club"},
        )
        # Missing TMDB ID
        m2 = CanonicalMapping(
            tmdb_id=None,
            imdb_id="tt0137523",
            title="Fight Club",
            type="movie",
            providers={"gnula": "fight-club-gn"},
        )
        summary = exporter.export_mappings([m1, m2])

        assert summary.imdb_count == 1
        assert summary.tmdb_count == 1
        assert summary.provider_count == 2
        assert (temp_orion_dir / "tmdb" / "550.json").exists()
        assert (temp_orion_dir / "imdb" / "tt0137523.json").exists()

    def test_export_multi_provider_fanout(self, temp_orion_dir: Path):
        exporter = OrionExporter(output_dir=temp_orion_dir)
        mapping = CanonicalMapping(
            tmdb_id="550",
            imdb_id="tt0137523",
            title="Fight Club",
            type="movie",
            providers={
                "serieskao": "slug1",
                "poseidonhd2": "slug2",
                "gnula": "slug3",
                "allcalidad": "slug4",
            },
        )
        summary = exporter.export_mappings([mapping])

        assert summary.provider_count == 4
        assert len(list((temp_orion_dir / "providers").glob("*.json"))) == 4

    def test_export_idempotency(self, temp_orion_dir: Path):
        exporter = OrionExporter(output_dir=temp_orion_dir)
        mapping = CanonicalMapping(
            tmdb_id="550",
            imdb_id="tt0137523",
            title="Fight Club",
            type="movie",
            providers={"serieskao": "fight-club"},
        )
        exporter.export_mappings([mapping])
        content_tmdb1 = (temp_orion_dir / "tmdb" / "550.json").read_bytes()
        content_imdb1 = (temp_orion_dir / "imdb" / "tt0137523.json").read_bytes()

        exporter.export_mappings([mapping])
        content_tmdb2 = (temp_orion_dir / "tmdb" / "550.json").read_bytes()
        content_imdb2 = (temp_orion_dir / "imdb" / "tt0137523.json").read_bytes()

        assert content_tmdb1 == content_tmdb2
        assert content_imdb1 == content_imdb2

    def test_export_summary_metrics(self, temp_orion_dir: Path):
        exporter = OrionExporter(output_dir=temp_orion_dir)
        mapping = CanonicalMapping(
            tmdb_id="550",
            imdb_id="tt0137523",
            title="Fight Club",
            type="movie",
            providers={"serieskao": "fight-club"},
        )
        summary = exporter.export_mappings([mapping])

        assert isinstance(summary, ExportSummary)
        assert summary.imdb_count == 1
        assert summary.tmdb_count == 1
        assert summary.provider_count == 1
        assert summary.total_files == 3
        assert summary.total_bytes > 0
        assert summary.duration_ms >= 0.0

    def test_export_store_integration(self, temp_mappings_dir: Path, temp_orion_dir: Path):
        store = MasterMappingStore(storage_dir=temp_mappings_dir)
        store.save_mapping(
            CanonicalMapping(
                tmdb_id="550",
                imdb_id="tt0137523",
                title="Fight Club",
                type="movie",
                providers={"serieskao": "fight-club"},
            )
        )
        store.save_mapping(
            CanonicalMapping(
                tmdb_id="82856",
                imdb_id="tt15486",
                title="Zombieland Saga",
                type="series",
                providers={"serieskao": "zombieland-saga"},
            )
        )

        exporter = OrionExporter(output_dir=temp_orion_dir)
        summary = exporter.export_store(store)

        assert summary.total_files == 6
        assert (temp_orion_dir / "tmdb" / "550.json").exists()
        assert (temp_orion_dir / "tmdb" / "82856.json").exists()
        assert (temp_orion_dir / "imdb" / "tt0137523.json").exists()
        assert (temp_orion_dir / "imdb" / "tt15486.json").exists()
