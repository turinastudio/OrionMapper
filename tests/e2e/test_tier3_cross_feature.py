"""Tier 3: Cross-Feature Combinations Test Suite.
Verifies pairwise interactions and multi-stage pipelines:
Scraping -> TMDB Resolving -> Normalizing & Matching -> Master Dataset Persistence -> OrionServer Export.
"""

import json

import pytest

from tests.conftest import encode_orion_provider_key


@pytest.mark.tier3
class TestTier3CrossFeaturePipelines:
    @pytest.mark.asyncio
    async def test_serieskao_scrape_resolve_export_pipeline(self, mock_http_client, temp_mappings_dir, temp_orion_dir):
        """Pipeline 1: SeriesKao scraping -> Direct IMDb -> TMDB find -> Reconciler -> MasterStore -> OrionExporter."""
        try:
            from orion_mapper.matcher.reconciler import IdentityReconciler
            from orion_mapper.resolver.tmdb import TmdbClient
            from orion_mapper.scrapers.serieskao import SeriesKaoScraper
            from orion_mapper.storage.master import MasterMappingStore
            from orion_mapper.storage.orion_exporter import OrionExporter

            # 1. Scrape SeriesKao movie detail
            scraper = SeriesKaoScraper(http_client=mock_http_client)
            detail = await scraper.fetch_detail("el-club-de-la-lucha", content_type="movie")
            assert detail is not None
            assert detail.imdb_id == "tt0137523"

            # 2. TMDB Resolver and Reconciler
            tmdb = TmdbClient(http_client=mock_http_client)
            store = MasterMappingStore(storage_dir=temp_mappings_dir)
            reconciler = IdentityReconciler(tmdb_client=tmdb)

            canonical = await reconciler.reconcile_item(detail, store)
            assert canonical is not None
            assert canonical.tmdb_id == "550"
            assert canonical.imdb_id == "tt0137523"
            assert canonical.providers.get("serieskao") == "el-club-de-la-lucha"

            # 3. Master Storage
            store.save_mapping(canonical)
            movies_json = temp_mappings_dir / "movies.json"
            assert movies_json.exists()
            movies_data = json.loads(movies_json.read_text(encoding="utf-8"))
            assert len(movies_data) == 1
            assert movies_data[0]["tmdb_id"] == "550"

            # 4. OrionServer Export
            exporter = OrionExporter(output_dir=temp_orion_dir)
            summary = exporter.export_mappings([canonical])
            assert summary is not None

            # Verify exported Orion files
            imdb_file = temp_orion_dir / "imdb" / "tt0137523.json"
            tmdb_file = temp_orion_dir / "tmdb" / "550.json"
            prov_key = encode_orion_provider_key("serieskao", "el-club-de-la-lucha")
            prov_file = temp_orion_dir / "providers" / f"{prov_key}.json"

            assert imdb_file.exists()
            assert tmdb_file.exists()
            assert prov_file.exists()

            imdb_data = json.loads(imdb_file.read_text(encoding="utf-8"))
            assert imdb_data["imdb_id"] == "tt0137523"
            assert imdb_data["tmdb_id"] == "550"
            assert imdb_data["providers"]["serieskao"] == "el-club-de-la-lucha"

            prov_data = json.loads(prov_file.read_text(encoding="utf-8"))
            assert prov_data["provider"] == "serieskao"
            assert prov_data["slug"] == "el-club-de-la-lucha"
            assert prov_data["imdb_id"] == "tt0137523"
            assert prov_data["tmdb_id"] == "550"

        except ImportError:
            pytest.skip("orion_mapper not yet implemented")

    @pytest.mark.asyncio
    async def test_poseidon_scrape_tmdb_external_ids_export_pipeline(self, mock_http_client, temp_mappings_dir, temp_orion_dir):
        """Pipeline 2: PoseidonHD2 scraping (TMDbId) -> TMDB external IDs -> Reconciler -> Export."""
        try:
            from orion_mapper.matcher.reconciler import IdentityReconciler
            from orion_mapper.resolver.tmdb import TmdbClient
            from orion_mapper.scrapers.poseidonhd2 import PoseidonHD2Scraper
            from orion_mapper.storage.master import MasterMappingStore
            from orion_mapper.storage.orion_exporter import OrionExporter

            scraper = PoseidonHD2Scraper(http_client=mock_http_client)
            detail = await scraper.fetch_detail("zombieland-saga", content_type="series")
            assert detail is not None
            assert detail.tmdb_id == "82856"

            tmdb = TmdbClient(http_client=mock_http_client)
            store = MasterMappingStore(storage_dir=temp_mappings_dir)
            reconciler = IdentityReconciler(tmdb_client=tmdb)

            canonical = await reconciler.reconcile_item(detail, store)
            assert canonical is not None
            assert canonical.tmdb_id == "82856"
            assert canonical.imdb_id == "tt15486"
            assert canonical.type == "series"

            store.save_mapping(canonical)
            exporter = OrionExporter(output_dir=temp_orion_dir)
            exporter.export_mappings([canonical])

            series_json = temp_mappings_dir / "series.json"
            assert series_json.exists()
            assert (temp_orion_dir / "tmdb" / "82856.json").exists()
            assert (temp_orion_dir / "imdb" / "tt15486.json").exists()

        except ImportError:
            pytest.skip("orion_mapper not yet implemented")

    @pytest.mark.asyncio
    async def test_four_provider_aggregation_pipeline(self, mock_http_client, temp_mappings_dir, temp_orion_dir):
        """Pipeline 3: All 4 providers scraped and unified into single canonical mapping with all 4 slugs."""
        try:
            from orion_mapper.matcher.reconciler import IdentityReconciler
            from orion_mapper.resolver.tmdb import TmdbClient
            from orion_mapper.storage.master import MasterMappingStore
            from orion_mapper.storage.orion_exporter import OrionExporter

            from orion_mapper.models.item import ScrapedItem

            tmdb = TmdbClient(http_client=mock_http_client)
            store = MasterMappingStore(storage_dir=temp_mappings_dir)
            reconciler = IdentityReconciler(tmdb_client=tmdb)

            items = [
                ScrapedItem(provider="serieskao", slug="el-club-de-la-lucha", title="El Club de la Lucha", type="movie", imdb_id="tt0137523"),
                ScrapedItem(provider="poseidonhd2", slug="el-club-de-la-lucha", title="El Club de la Lucha", type="movie", tmdb_id="550"),
                ScrapedItem(provider="gnula", slug="pelicula-el-club-de-la-lucha", title="El Club de la Lucha", type="movie", imdb_id="tt0137523", tmdb_id="550"),
                ScrapedItem(provider="allcalidad", slug="el-club-de-la-lucha", title="El Club de la Lucha", type="movie", tmdb_id="550"),
            ]

            for item in items:
                mapped = await reconciler.reconcile_item(item, store)
                store.save_mapping(mapped)

            final_entry = store.get_by_tmdb("550", "movie")
            assert final_entry is not None
            assert len(final_entry.providers) == 4
            assert final_entry.providers["serieskao"] == "el-club-de-la-lucha"
            assert final_entry.providers["poseidonhd2"] == "el-club-de-la-lucha"
            assert final_entry.providers["gnula"] == "pelicula-el-club-de-la-lucha"
            assert final_entry.providers["allcalidad"] == "el-club-de-la-lucha"

            # Export to OrionServer
            exporter = OrionExporter(output_dir=temp_orion_dir)
            exporter.export_mappings([final_entry])

            # Verify all 4 provider index files exist
            for prov, slug in final_entry.providers.items():
                prov_key = encode_orion_provider_key(prov, slug)
                assert (temp_orion_dir / "providers" / f"{prov_key}.json").exists()

            # Verify IMDb index maps all 4 providers
            imdb_data = json.loads((temp_orion_dir / "imdb" / "tt0137523.json").read_text(encoding="utf-8"))
            assert len(imdb_data["providers"]) == 4

        except ImportError:
            pytest.skip("orion_mapper not yet implemented")

    @pytest.mark.asyncio
    async def test_incremental_dataset_preservation_and_update(self, mock_http_client, temp_mappings_dir, temp_orion_dir):
        """Pipeline 4: Incremental update retains historical mappings and extends provider links."""
        try:
            from orion_mapper.matcher.reconciler import IdentityReconciler
            from orion_mapper.resolver.tmdb import TmdbClient
            from orion_mapper.storage.master import MasterMappingStore
            from orion_mapper.storage.orion_exporter import OrionExporter

            from orion_mapper.models.item import ScrapedItem
            from orion_mapper.models.mapping import CanonicalMapping

            store = MasterMappingStore(storage_dir=temp_mappings_dir)

            # Pre-populate historical mapping
            historical = CanonicalMapping(
                tmdb_id="550",
                imdb_id="tt0137523",
                title="Fight Club",
                type="movie",
                year=1999,
                providers={"serieskao": "historical-sk-slug"},
                updated_at=1000000
            )
            store.save_mapping(historical)

            # Reconcile new provider item
            tmdb = TmdbClient(http_client=mock_http_client)
            reconciler = IdentityReconciler(tmdb_client=tmdb)
            new_item = ScrapedItem(provider="allcalidad", slug="allcalidad-slug-550", title="Fight Club", type="movie", tmdb_id="550")

            updated = await reconciler.reconcile_item(new_item, store)
            store.save_mapping(updated)

            assert updated.providers["serieskao"] == "historical-sk-slug"
            assert updated.providers["allcalidad"] == "allcalidad-slug-550"

            # Export and verify both provider keys exist
            exporter = OrionExporter(output_dir=temp_orion_dir)
            exporter.export_mappings([updated])

            key1 = encode_orion_provider_key("serieskao", "historical-sk-slug")
            key2 = encode_orion_provider_key("allcalidad", "allcalidad-slug-550")
            assert (temp_orion_dir / "providers" / f"{key1}.json").exists()
            assert (temp_orion_dir / "providers" / f"{key2}.json").exists()

        except ImportError:
            pytest.skip("orion_mapper not yet implemented")

    @pytest.mark.asyncio
    async def test_mixed_movie_and_series_segregation_pipeline(self, mock_http_client, temp_mappings_dir, temp_orion_dir):
        """Pipeline 5: Multi-type catalog ingestion segregates into movies.json and series.json correctly."""
        try:
            from orion_mapper.matcher.reconciler import IdentityReconciler
            from orion_mapper.resolver.tmdb import TmdbClient
            from orion_mapper.storage.master import MasterMappingStore
            from orion_mapper.storage.orion_exporter import OrionExporter

            from orion_mapper.models.item import ScrapedItem

            tmdb = TmdbClient(http_client=mock_http_client)
            store = MasterMappingStore(storage_dir=temp_mappings_dir)
            reconciler = IdentityReconciler(tmdb_client=tmdb)

            movie_item = ScrapedItem(provider="serieskao", slug="fight-club", title="Fight Club", type="movie", imdb_id="tt0137523")
            series_item = ScrapedItem(provider="serieskao", slug="zombieland", title="Zombieland Saga", type="series", imdb_id="tt15486")

            m1 = await reconciler.reconcile_item(movie_item, store)
            m2 = await reconciler.reconcile_item(series_item, store)

            store.save_mapping(m1)
            store.save_mapping(m2)

            movies = json.loads((temp_mappings_dir / "movies.json").read_text(encoding="utf-8"))
            series = json.loads((temp_mappings_dir / "series.json").read_text(encoding="utf-8"))

            assert len(movies) == 1 and movies[0]["tmdb_id"] == "550"
            assert len(series) == 1 and series[0]["tmdb_id"] == "82856"

            exporter = OrionExporter(output_dir=temp_orion_dir)
            exporter.export_mappings([m1, m2])

            assert (temp_orion_dir / "tmdb" / "550.json").exists()
            assert (temp_orion_dir / "tmdb" / "82856.json").exists()

        except ImportError:
            pytest.skip("orion_mapper not yet implemented")
