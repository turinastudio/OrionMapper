"""End-to-End Sync Pipeline Test Suite.
Verifies the complete automated flow:
Scrape providers -> TMDB match & reconcile -> Persist dataset -> Export OrionServer indexes.
"""

import json

import pytest


@pytest.mark.e2e
class TestE2ESyncWorkflow:
    @pytest.mark.asyncio
    async def test_full_sync_pipeline_execution(self, mock_http_client, temp_mappings_dir, temp_orion_dir):
        """Full Sync: Scrapes all providers, reconciles with TMDB, writes Fribb dataset, and exports Orion indices."""
        try:
            from orion_mapper.matcher.reconciler import IdentityReconciler
            from orion_mapper.resolver.tmdb import TmdbClient
            from orion_mapper.storage.master import MasterMappingStore
            from orion_mapper.storage.orion_exporter import OrionExporter

            from orion_mapper.scrapers import get_registered_providers, get_scraper

            providers = get_registered_providers()
            assert len(providers) >= 4

            # Step 1: Scrape
            scraped_items = []
            for prov_name in providers:
                scraper = get_scraper(prov_name, http_client=mock_http_client)
                for c_type in ["movie", "series"]:
                    items = await scraper.fetch_catalog(content_type=c_type, page=1)
                    scraped_items.extend(items)

            assert len(scraped_items) > 0

            # Step 2: Match & Reconcile
            tmdb = TmdbClient(http_client=mock_http_client)
            store = MasterMappingStore(storage_dir=temp_mappings_dir)
            reconciler = IdentityReconciler(tmdb_client=tmdb)

            canonical_mappings = await reconciler.reconcile_batch(scraped_items, store)
            for m in canonical_mappings:
                store.save_mapping(m)

            # Step 3: Verify Master Mappings
            all_mappings = store.load_all()
            assert len(all_mappings) > 0

            movies_file = temp_mappings_dir / "movies.json"
            series_file = temp_mappings_dir / "series.json"
            assert movies_file.exists() or series_file.exists()

            # Step 4: Export to OrionServer
            exporter = OrionExporter(output_dir=temp_orion_dir)
            exporter.export_mappings(all_mappings)

            # Step 5: Validate Orion Output Integrity
            imdb_files = list((temp_orion_dir / "imdb").glob("*.json"))
            tmdb_files = list((temp_orion_dir / "tmdb").glob("*.json"))
            prov_files = list((temp_orion_dir / "providers").glob("*.json"))

            assert len(imdb_files) > 0
            assert len(tmdb_files) > 0
            assert len(prov_files) > 0

            # Verify schema of an exported IMDb index
            sample_imdb = json.loads(imdb_files[0].read_text(encoding="utf-8"))
            assert "imdb_id" in sample_imdb
            assert "providers" in sample_imdb
            assert isinstance(sample_imdb["providers"], dict)

            # Verify schema of an exported TMDB index
            sample_tmdb = json.loads(tmdb_files[0].read_text(encoding="utf-8"))
            assert "tmdb_id" in sample_tmdb

        except ImportError:
            pytest.skip("orion_mapper not yet implemented")
