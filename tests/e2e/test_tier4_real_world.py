"""Tier 4: Real-World Application Scenarios Test Suite.
Verifies full lifecycle application scenarios under realistic conditions:
Cold start bootstrapping, partial provider failure resilience, high-volume batch sync, and diacritic handling.
"""

import time

import httpx
import pytest


@pytest.mark.tier4
class TestTier4RealWorldScenarios:
    @pytest.mark.asyncio
    async def test_scenario_cold_start_bootstrap(self, mock_http_client, temp_mappings_dir, temp_orion_dir):
        """Scenario 1: Cold-start bootstrap scraping all 4 providers and generating complete OrionServer indexes from zero."""
        try:
            from orion_mapper.matcher.reconciler import IdentityReconciler
            from orion_mapper.resolver.tmdb import TmdbClient
            from orion_mapper.scrapers.allcalidad import AllCalidadScraper
            from orion_mapper.scrapers.gnula import GnulaScraper
            from orion_mapper.scrapers.poseidonhd2 import PoseidonHD2Scraper
            from orion_mapper.scrapers.serieskao import SeriesKaoScraper
            from orion_mapper.storage.master import MasterMappingStore
            from orion_mapper.storage.orion_exporter import OrionExporter

            # Initial state: directories are completely empty
            assert len(list(temp_mappings_dir.glob("*.json"))) == 0
            assert len(list(temp_orion_dir.glob("*"))) == 0

            # 1. Scrape all 4 providers
            scrapers = [
                SeriesKaoScraper(http_client=mock_http_client),
                PoseidonHD2Scraper(http_client=mock_http_client),
                GnulaScraper(http_client=mock_http_client),
                AllCalidadScraper(http_client=mock_http_client),
            ]

            all_items = []
            for s in scrapers:
                for content_type in ["movie", "series"]:
                    items = await s.fetch_catalog(content_type=content_type, page=1)
                    all_items.extend(items)

            assert len(all_items) > 0

            # 2. Reconcile all items into MasterMappingStore
            store = MasterMappingStore(storage_dir=temp_mappings_dir)
            tmdb = TmdbClient(http_client=mock_http_client)
            reconciler = IdentityReconciler(tmdb_client=tmdb)

            canonical_mappings = await reconciler.reconcile_batch(all_items, store)
            assert len(canonical_mappings) > 0

            for m in canonical_mappings:
                store.save_mapping(m)

            # Verify master files generated
            assert (temp_mappings_dir / "movies.json").exists()
            assert (temp_mappings_dir / "series.json").exists()

            # 3. Export to OrionServer
            exporter = OrionExporter(output_dir=temp_orion_dir)
            all_canonical = store.load_all()
            exporter.export_mappings(all_canonical)

            # Verify complete Orion index tree
            imdb_files = list((temp_orion_dir / "imdb").glob("*.json"))
            tmdb_files = list((temp_orion_dir / "tmdb").glob("*.json"))
            prov_files = list((temp_orion_dir / "providers").glob("*.json"))

            assert len(imdb_files) >= 2
            assert len(tmdb_files) >= 2
            assert len(prov_files) >= 4

        except ImportError:
            pytest.skip("orion_mapper not yet implemented")

    @pytest.mark.asyncio
    async def test_scenario_partial_provider_failure_resilience(self, serieskao_fixtures, poseidonhd2_fixtures, temp_mappings_dir, temp_orion_dir):
        """Scenario 2: Sync pipeline completes successfully when 1 provider suffers a network failure."""
        class FlakyTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                if "serieskao" in str(request.url):
                    # SeriesKao is temporarily down with 500 error
                    return httpx.Response(500, request=request)
                if "poseidonhd2" in str(request.url):
                    return httpx.Response(200, text=poseidonhd2_fixtures["catalog_page1"], request=request)
                return httpx.Response(404, request=request)

        flaky_client = httpx.AsyncClient(transport=FlakyTransport())

        try:
            from orion_mapper.scrapers.poseidonhd2 import PoseidonHD2Scraper
            from orion_mapper.scrapers.serieskao import SeriesKaoScraper

            sk = SeriesKaoScraper(http_client=flaky_client)
            pos = PoseidonHD2Scraper(http_client=flaky_client)

            # SeriesKao should handle failure gracefully (returning empty list or catching error)
            sk_items = []
            try:
                sk_items = await sk.fetch_catalog("movie", page=1)
            except Exception:
                sk_items = []

            # PoseidonHD2 succeeds
            pos_items = await pos.fetch_catalog("movie", page=1)

            assert len(sk_items) == 0
            assert len(pos_items) > 0

        except ImportError:
            pytest.skip("orion_mapper not yet implemented")

    @pytest.mark.asyncio
    async def test_scenario_high_volume_batch_sync_simulation(self, mock_http_client, temp_mappings_dir, temp_orion_dir):
        """Scenario 3: Ingestion of 50 items with atomic persistence and OrionServer index consistency."""
        try:
            from orion_mapper.storage.master import MasterMappingStore
            from orion_mapper.storage.orion_exporter import OrionExporter

            from orion_mapper.models.mapping import CanonicalMapping

            store = MasterMappingStore(storage_dir=temp_mappings_dir)
            exporter = OrionExporter(output_dir=temp_orion_dir)

            bulk_mappings = []
            for i in range(1, 51):
                m = CanonicalMapping(
                    tmdb_id=str(1000 + i),
                    imdb_id=f"tt{1000000 + i}",
                    title=f"Bulk Movie {i}",
                    type="movie",
                    year=2000 + (i % 25),
                    providers={
                        "serieskao": f"bulk-movie-{i}",
                        "poseidonhd2": f"bulk-movie-{i}",
                    },
                    updated_at=int(time.time() * 1000)
                )
                bulk_mappings.append(m)
                store.save_mapping(m)

            loaded = store.load_all()
            assert len(loaded) == 50

            exporter.export_mappings(bulk_mappings)

            imdb_count = len(list((temp_orion_dir / "imdb").glob("*.json")))
            tmdb_count = len(list((temp_orion_dir / "tmdb").glob("*.json")))
            prov_count = len(list((temp_orion_dir / "providers").glob("*.json")))

            assert imdb_count == 50
            assert tmdb_count == 50
            assert prov_count == 100  # 2 providers per item * 50 items

        except ImportError:
            pytest.skip("orion_mapper not yet implemented")

    @pytest.mark.asyncio
    async def test_scenario_spanish_vs_original_title_matching(self, mock_http_client, temp_mappings_dir):
        """Scenario 4: Reconciles localized Spanish titles to canonical TMDB items."""
        try:
            from orion_mapper.matcher.normalizer import TitleNormalizer
            from orion_mapper.matcher.scoring import FuzzyTitleMatcher

            spanish_title = "El Club de la Pelea (Audio Latino)"
            clean_spanish = TitleNormalizer.normalize(spanish_title)
            assert "audio latino" not in clean_spanish

            score = FuzzyTitleMatcher.score("El Club de la Pelea", "El Club de la Lucha", year1=1999, year2=1999, type1="movie", type2="movie")
            assert score >= 70.0

        except ImportError:
            pytest.skip("orion_mapper not yet implemented")
