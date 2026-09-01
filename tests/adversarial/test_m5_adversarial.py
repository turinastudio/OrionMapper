"""Adversarial Test Suite for Milestone 5.

Covers:
1. End-to-end sync CLI execution under dry-run, partial provider failures, and empty database states.
2. GitHub Actions YAML parsing across multiple YAML engines and validating bash syntax in run blocks.
3. Stress testing boundary conditions, parameter expansion matrices, CLI entry points, and resource cleanup.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from orion_mapper.cli.commands import (
    app,
    execute_export,
    execute_match,
    execute_scrape,
    execute_sync,
    main,
)
from orion_mapper.models.item import ContentType, ScrapedItem
from orion_mapper.models.mapping import CanonicalMapping
from orion_mapper.storage.master import MasterMappingStore

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "sync-mappings.yml"


# ==============================================================================
# 1. DRY-RUN ISOLATION & NON-MUTATION ADVERSARIAL TESTS
# ==============================================================================
class TestCliDryRunInvariants:
    """Verify that --dry-run strictly prevents disk mutations and folder creation."""

    @pytest.mark.asyncio
    async def test_scrape_dry_run_never_creates_output_dir_or_files(self, tmp_path: Path):
        """Scrape with dry-run should NOT create non-existent output_dir or write JSON files."""
        non_existent_dir = tmp_path / "should_not_exist" / "scraped"
        mock_items = [
            ScrapedItem(
                provider="serieskao",
                slug="test-item",
                title="Test Item",
                type="movie",
                year=2024,
            )
        ]
        mock_scraper = MagicMock()
        mock_scraper.supported_types = [ContentType.MOVIE]
        mock_scraper.fetch_catalog = AsyncMock(side_effect=[mock_items, []])

        with patch("orion_mapper.cli.commands.get_scraper", return_value=mock_scraper):
            args = argparse.Namespace(
                provider="serieskao",
                type="movie",
                limit=5,
                output_dir=str(non_existent_dir),
                dry_run=True,
                rate_limit=None,
            )
            exit_code = await execute_scrape(args)
            assert exit_code == 0
            assert not non_existent_dir.exists(), "Dry-run scrape must not create output_dir"

    @pytest.mark.asyncio
    async def test_match_dry_run_leaves_store_files_byte_for_byte_unmodified(self, tmp_path: Path):
        """Match with dry-run must leave existing master mapping files completely untouched."""
        store_dir = tmp_path / "mappings"
        store = MasterMappingStore(storage_dir=store_dir)
        store.add_or_update(
            CanonicalMapping(
                tmdb_id=None,
                imdb_id="tt0000001",
                title="Carmencita",
                type="movie",
                year=1894,
                providers={"gnula": "carmencita"},
            )
        )
        store.save()

        movies_file = store_dir / "movies.json"
        original_bytes = movies_file.read_bytes()
        original_hash = hashlib.sha256(original_bytes).hexdigest()
        original_mtime = movies_file.stat().st_mtime_ns

        mock_reconciled = CanonicalMapping(
            tmdb_id="12345",
            imdb_id="tt0000001",
            title="Carmencita",
            type="movie",
            year=1894,
            providers={"gnula": "carmencita"},
        )

        with patch(
            "orion_mapper.matcher.reconciler.IdentityReconciler.reconcile_item",
            new=AsyncMock(return_value=mock_reconciled),
        ):
            args = argparse.Namespace(
                source=str(store_dir),
                tmdb_key=None,
                rate_limit=None,
                fuzzy_threshold=88.0,
                unmapped_only=False,
                limit=None,
                dry_run=True,
            )
            exit_code = await execute_match(args)
            assert exit_code == 0

            # File must remain byte-identical
            new_bytes = movies_file.read_bytes()
            assert hashlib.sha256(new_bytes).hexdigest() == original_hash
            assert movies_file.stat().st_mtime_ns == original_mtime

    def test_export_dry_run_leaves_target_dir_nonexistent(self, tmp_path: Path):
        """Export with dry-run must not create target directory or subfolders."""
        source_dir = tmp_path / "mappings"
        target_dir = tmp_path / "orion_mappings"

        store = MasterMappingStore(storage_dir=source_dir)
        store.add_or_update(
            CanonicalMapping(
                tmdb_id="550",
                imdb_id="tt0137523",
                title="Fight Club",
                type="movie",
                year=1999,
                providers={"gnula": "fight-club"},
            )
        )
        store.save()

        args = argparse.Namespace(
            source=str(source_dir),
            target=str(target_dir),
            compress=False,
            dry_run=True,
        )
        exit_code = execute_export(args)
        assert exit_code == 0
        assert not target_dir.exists(), "Dry-run export must not create target directory"

    @pytest.mark.asyncio
    async def test_sync_dry_run_complete_isolation(self, tmp_path: Path):
        """Sync with dry-run must not create mappings_dir or target_dir on disk."""
        mappings_dir = tmp_path / "master_db"
        target_dir = tmp_path / "orion_export"

        mock_scraped = [
            ScrapedItem(
                provider="serieskao",
                slug="sample-series",
                title="Sample Series",
                type="series",
                year=2022,
                imdb_id="tt9999999",
                tmdb_id="88888",
            )
        ]
        mock_scraper = MagicMock()
        mock_scraper.supported_types = [ContentType.SERIES]
        mock_scraper.fetch_catalog = AsyncMock(side_effect=[mock_scraped, []])

        mock_reconciled = [
            CanonicalMapping(
                tmdb_id="88888",
                imdb_id="tt9999999",
                title="Sample Series",
                type="series",
                year=2022,
                providers={"serieskao": "sample-series"},
            )
        ]

        with (
            patch("orion_mapper.cli.commands.get_scraper", return_value=mock_scraper),
            patch(
                "orion_mapper.matcher.reconciler.IdentityReconciler.reconcile_batch",
                new=AsyncMock(return_value=mock_reconciled),
            ),
        ):
            args = argparse.Namespace(
                provider="serieskao",
                type="series",
                limit=1,
                unmapped_only=False,
                target=str(target_dir),
                mappings_dir=str(mappings_dir),
                tmdb_key=None,
                rate_limit=None,
                fuzzy_threshold=88.0,
                dry_run=True,
            )
            exit_code = await execute_sync(args)
            assert exit_code == 0
            assert not (mappings_dir / "series.json").exists()
            assert not target_dir.exists()

    @pytest.mark.asyncio
    async def test_sync_dry_run_with_existing_database_preserves_content(self, tmp_path: Path):
        """Sync with dry-run over an existing DB must preserve existing content without overwriting."""
        mappings_dir = tmp_path / "master_db"
        store = MasterMappingStore(storage_dir=mappings_dir)
        store.add_or_update(
            CanonicalMapping(
                tmdb_id="111",
                imdb_id="tt1111111",
                title="Original Entry",
                type="movie",
                year=2020,
                providers={"gnula": "original"},
            )
        )
        store.save()

        movies_path = mappings_dir / "movies.json"
        original_content = movies_path.read_text(encoding="utf-8")

        mock_scraped = [
            ScrapedItem(
                provider="gnula",
                slug="new-entry",
                title="New Entry",
                type="movie",
                year=2021,
            )
        ]
        mock_scraper = MagicMock()
        mock_scraper.supported_types = [ContentType.MOVIE]
        mock_scraper.fetch_catalog = AsyncMock(side_effect=[mock_scraped, []])

        mock_reconciled = [
            CanonicalMapping(
                tmdb_id="222",
                imdb_id="tt2222222",
                title="New Entry",
                type="movie",
                year=2021,
                providers={"gnula": "new-entry"},
            )
        ]

        with (
            patch("orion_mapper.cli.commands.get_scraper", return_value=mock_scraper),
            patch(
                "orion_mapper.matcher.reconciler.IdentityReconciler.reconcile_batch",
                new=AsyncMock(return_value=mock_reconciled),
            ),
        ):
            args = argparse.Namespace(
                provider="gnula",
                type="movie",
                limit=5,
                unmapped_only=False,
                target=str(tmp_path / "orion_out"),
                mappings_dir=str(mappings_dir),
                tmdb_key=None,
                rate_limit=None,
                fuzzy_threshold=88.0,
                dry_run=True,
            )
            exit_code = await execute_sync(args)
            assert exit_code == 0
            assert movies_path.read_text(encoding="utf-8") == original_content


# ==============================================================================
# 2. PARTIAL PROVIDER FAILURES & RESILIENCE ADVERSARIAL TESTS
# ==============================================================================
class TestCliPartialProviderFailures:
    """Stress testing sync and scrape pipelines under partial provider failures."""

    @pytest.mark.asyncio
    async def test_sync_resilient_to_mixed_provider_failures(self, tmp_path: Path):
        """Sync should continue when some providers fail to init or fetch while others succeed."""
        mappings_dir = tmp_path / "mappings"
        target_dir = tmp_path / "orion_mappings"

        # Provider 1: Works normally
        scraped_prov1 = [
            ScrapedItem(
                provider="serieskao",
                slug="good-series",
                title="Good Series",
                type="series",
                year=2020,
            )
        ]
        scraper1 = MagicMock()
        scraper1.supported_types = [ContentType.SERIES]
        scraper1.fetch_catalog = AsyncMock(side_effect=[scraped_prov1, []])

        # Provider 2: Throws during fetch_catalog (e.g. 500 server error)
        scraper2 = MagicMock()
        scraper2.supported_types = [ContentType.SERIES]
        scraper2.fetch_catalog = AsyncMock(side_effect=RuntimeError("500 Internal Server Error"))

        # Provider 3: Fails initialization
        def mock_get_scraper(name, **kwargs):
            if name == "serieskao":
                return scraper1
            elif name == "poseidonhd2":
                return scraper2
            elif name == "gnula":
                raise ValueError("Scraper failed to initialize: missing config")
            elif name == "allcalidad":
                # Provider 4: returns empty catalog
                scraper4 = MagicMock()
                scraper4.supported_types = [ContentType.SERIES]
                scraper4.fetch_catalog = AsyncMock(return_value=[])
                return scraper4
            raise ValueError(f"Unknown provider: {name}")

        reconciled_items = [
            CanonicalMapping(
                tmdb_id="9999",
                imdb_id="tt9999",
                title="Good Series",
                type="series",
                year=2020,
                providers={"serieskao": "good-series"},
            )
        ]

        with (
            patch(
                "orion_mapper.cli.commands.get_registered_providers",
                return_value=["serieskao", "poseidonhd2", "gnula", "allcalidad"],
            ),
            patch("orion_mapper.cli.commands.get_scraper", side_effect=mock_get_scraper),
            patch(
                "orion_mapper.matcher.reconciler.IdentityReconciler.reconcile_batch",
                new=AsyncMock(return_value=reconciled_items),
            ) as mock_batch,
        ):
            args = argparse.Namespace(
                provider="all",
                type="series",
                limit=10,
                unmapped_only=False,
                target=str(target_dir),
                mappings_dir=str(mappings_dir),
                tmdb_key=None,
                rate_limit=None,
                fuzzy_threshold=88.0,
                dry_run=False,
            )
            exit_code = await execute_sync(args)
            assert exit_code == 0

            # Verify that only the healthy provider's scraped items were sent to reconciler
            assert mock_batch.call_count == 1
            passed_items = mock_batch.call_args[0][0]
            assert len(passed_items) == 1
            assert passed_items[0].slug == "good-series"

            # Verify persisted data
            store = MasterMappingStore(storage_dir=mappings_dir)
            assert store.count("series") == 1

            # Verify export
            assert (target_dir / "imdb" / "tt9999.json").exists()

    @pytest.mark.asyncio
    async def test_sync_mid_pagination_failure_retains_earlier_pages(self, tmp_path: Path):
        """When a scraper fails on page 2, page 1 items must still be reconciled and saved."""
        mappings_dir = tmp_path / "mappings"
        target_dir = tmp_path / "orion_mappings"

        page1_items = [
            ScrapedItem(provider="gnula", slug=f"movie-{i}", title=f"Movie {i}", type="movie", year=2020)
            for i in range(5)
        ]

        scraper = MagicMock()
        scraper.supported_types = [ContentType.MOVIE]
        scraper.fetch_catalog = AsyncMock(
            side_effect=[page1_items, ConnectionResetError("Connection dropped by peer")]
        )

        mock_reconciled = [
            CanonicalMapping(
                tmdb_id=f"10{i}",
                imdb_id=f"tt10{i}",
                title=f"Movie {i}",
                type="movie",
                year=2020,
                providers={"gnula": f"movie-{i}"},
            )
            for i in range(5)
        ]

        with (
            patch("orion_mapper.cli.commands.get_scraper", return_value=scraper),
            patch(
                "orion_mapper.matcher.reconciler.IdentityReconciler.reconcile_batch",
                new=AsyncMock(return_value=mock_reconciled),
            ),
        ):
            args = argparse.Namespace(
                provider="gnula",
                type="movie",
                limit=20,
                unmapped_only=False,
                target=str(target_dir),
                mappings_dir=str(mappings_dir),
                tmdb_key=None,
                rate_limit=None,
                fuzzy_threshold=88.0,
                dry_run=False,
            )
            exit_code = await execute_sync(args)
            assert exit_code == 0

            store = MasterMappingStore(storage_dir=mappings_dir)
            assert store.count("movie") == 5

    @pytest.mark.asyncio
    async def test_sync_all_providers_failing_exits_cleanly(self, tmp_path: Path):
        """If all providers fail during sync, the pipeline should exit 0 without crashing."""
        mappings_dir = tmp_path / "mappings"
        target_dir = tmp_path / "orion_mappings"

        with (
            patch(
                "orion_mapper.cli.commands.get_registered_providers",
                return_value=["serieskao", "gnula"],
            ),
            patch(
                "orion_mapper.cli.commands.get_scraper",
                side_effect=RuntimeError("Provider unreachable"),
            ),
            patch(
                "orion_mapper.matcher.reconciler.IdentityReconciler.reconcile_batch",
                new=AsyncMock(return_value=[]),
            ),
        ):
            args = argparse.Namespace(
                provider="all",
                type=None,
                limit=10,
                unmapped_only=False,
                target=str(target_dir),
                mappings_dir=str(mappings_dir),
                tmdb_key=None,
                rate_limit=None,
                fuzzy_threshold=88.0,
                dry_run=False,
            )
            exit_code = await execute_sync(args)
            assert exit_code == 0

    @pytest.mark.asyncio
    async def test_scrape_command_partial_provider_failures(self, tmp_path: Path):
        """Scrape command handles one failing provider while saving output for successful provider."""
        good_items = [
            ScrapedItem(
                provider="serieskao",
                slug="test-series",
                title="Test Series",
                type="series",
                year=2021,
            )
        ]
        scraper_good = MagicMock()
        scraper_good.supported_types = [ContentType.SERIES]
        scraper_good.fetch_catalog = AsyncMock(side_effect=[good_items, []])

        scraper_bad = MagicMock()
        scraper_bad.supported_types = [ContentType.SERIES]
        scraper_bad.fetch_catalog = AsyncMock(side_effect=TimeoutError("Request timed out"))

        def mock_get(prov, **kwargs):
            if prov == "serieskao":
                return scraper_good
            return scraper_bad

        with (
            patch(
                "orion_mapper.cli.commands.get_registered_providers",
                return_value=["serieskao", "gnula"],
            ),
            patch("orion_mapper.cli.commands.get_scraper", side_effect=mock_get),
        ):
            args = argparse.Namespace(
                provider="all",
                type="series",
                limit=10,
                output_dir=str(tmp_path),
                dry_run=False,
                rate_limit=None,
            )
            exit_code = await execute_scrape(args)
            assert exit_code == 0

            # serieskao file must exist with items
            assert (tmp_path / "serieskao_series.json").exists()
            data_serieskao = json.loads((tmp_path / "serieskao_series.json").read_text(encoding="utf-8"))
            assert len(data_serieskao) == 1
            assert data_serieskao[0]["slug"] == "test-series"

            # gnula file was written with empty items due to error on page 1 (0 items)
            assert (tmp_path / "gnula_series.json").exists()
            data_gnula = json.loads((tmp_path / "gnula_series.json").read_text(encoding="utf-8"))
            assert len(data_gnula) == 0


# ==============================================================================
# 3. EMPTY DATABASE STATES & BOUNDARY CONDITIONS ADVERSARIAL TESTS
# ==============================================================================
class TestCliEmptyDatabaseStatesAndBoundaries:
    """Stress testing CLI commands against empty databases, missing files, and boundary arguments."""

    @pytest.mark.asyncio
    async def test_match_on_empty_mappings_directory(self, tmp_path: Path):
        """Match command on non-existent or empty mappings directory should succeed with 0 matches."""
        empty_dir = tmp_path / "empty_mappings"
        args = argparse.Namespace(
            source=str(empty_dir),
            tmdb_key=None,
            rate_limit=None,
            fuzzy_threshold=88.0,
            unmapped_only=False,
            limit=None,
            dry_run=False,
        )
        exit_code = await execute_match(args)
        assert exit_code == 0

    def test_export_on_empty_mappings_directory(self, tmp_path: Path):
        """Export command on empty master store should export empty index dirs without error."""
        empty_source = tmp_path / "empty_source"
        target_dir = tmp_path / "orion_export"

        args = argparse.Namespace(
            source=str(empty_source),
            target=str(target_dir),
            compress=False,
            dry_run=False,
        )
        exit_code = execute_export(args)
        assert exit_code == 0
        assert (target_dir / "imdb").exists()
        assert (target_dir / "tmdb").exists()
        assert (target_dir / "providers").exists()

    @pytest.mark.asyncio
    async def test_match_unmapped_only_when_100_percent_mapped(self, tmp_path: Path):
        """Match with --unmapped-only when all items are fully mapped should process 0 items."""
        store = MasterMappingStore(storage_dir=tmp_path)
        store.add_or_update(
            CanonicalMapping(
                tmdb_id="100",
                imdb_id="tt0000100",
                title="Fully Mapped Movie",
                type="movie",
                year=2020,
                providers={"gnula": "fully-mapped"},
            )
        )
        store.save()

        with patch(
            "orion_mapper.matcher.reconciler.IdentityReconciler.reconcile_item",
            new=AsyncMock(),
        ) as mock_reconcile:
            args = argparse.Namespace(
                source=str(tmp_path),
                tmdb_key=None,
                rate_limit=None,
                fuzzy_threshold=88.0,
                unmapped_only=True,
                limit=None,
                dry_run=False,
            )
            exit_code = await execute_match(args)
            assert exit_code == 0
            assert mock_reconcile.call_count == 0

    @pytest.mark.asyncio
    async def test_scrape_with_zero_limit(self, tmp_path: Path):
        """Scrape with limit=0 should terminate immediately without fetching pages."""
        scraper = MagicMock()
        scraper.supported_types = [ContentType.MOVIE]
        scraper.fetch_catalog = AsyncMock()

        with patch("orion_mapper.cli.commands.get_scraper", return_value=scraper):
            args = argparse.Namespace(
                provider="serieskao",
                type="movie",
                limit=0,
                output_dir=str(tmp_path),
                dry_run=False,
                rate_limit=None,
            )
            exit_code = await execute_scrape(args)
            assert exit_code == 0
            assert scraper.fetch_catalog.call_count == 0

    @pytest.mark.asyncio
    async def test_scrape_with_negative_limit(self, tmp_path: Path):
        """Scrape with limit < 0 should terminate immediately without fetching pages."""
        scraper = MagicMock()
        scraper.supported_types = [ContentType.SERIES]
        scraper.fetch_catalog = AsyncMock()

        with patch("orion_mapper.cli.commands.get_scraper", return_value=scraper):
            args = argparse.Namespace(
                provider="serieskao",
                type="series",
                limit=-5,
                output_dir=str(tmp_path),
                dry_run=False,
                rate_limit=None,
            )
            exit_code = await execute_scrape(args)
            assert exit_code == 0
            assert scraper.fetch_catalog.call_count == 0

    @pytest.mark.asyncio
    async def test_sync_with_zero_limit(self, tmp_path: Path):
        """Sync with limit=0 scrapes 0 items and exports cleanly."""
        scraper = MagicMock()
        scraper.supported_types = [ContentType.MOVIE]
        scraper.fetch_catalog = AsyncMock()

        with (
            patch("orion_mapper.cli.commands.get_scraper", return_value=scraper),
            patch(
                "orion_mapper.matcher.reconciler.IdentityReconciler.reconcile_batch",
                new=AsyncMock(return_value=[]),
            ) as mock_batch,
        ):
            args = argparse.Namespace(
                provider="gnula",
                type="movie",
                limit=0,
                unmapped_only=False,
                target=str(tmp_path / "orion"),
                mappings_dir=str(tmp_path / "mappings"),
                tmdb_key=None,
                rate_limit=None,
                fuzzy_threshold=88.0,
                dry_run=False,
            )
            exit_code = await execute_sync(args)
            assert exit_code == 0
            assert scraper.fetch_catalog.call_count == 0
            assert mock_batch.call_args[0][0] == []

    @pytest.mark.asyncio
    async def test_extreme_rate_limits_and_fuzzy_thresholds(self, tmp_path: Path):
        """Sync should handle extreme rate limit values and fuzzy thresholds without crashing."""
        scraper = MagicMock()
        scraper.supported_types = [ContentType.MOVIE]
        scraper.fetch_catalog = AsyncMock(return_value=[])

        with (
            patch("orion_mapper.cli.commands.get_scraper", return_value=scraper),
            patch(
                "orion_mapper.matcher.reconciler.IdentityReconciler.reconcile_batch",
                new=AsyncMock(return_value=[]),
            ),
        ):
            # Very low rate limit
            args_low = argparse.Namespace(
                provider="gnula",
                type="movie",
                limit=1,
                unmapped_only=False,
                target=str(tmp_path / "orion1"),
                mappings_dir=str(tmp_path / "map1"),
                tmdb_key=None,
                rate_limit=0.001,
                fuzzy_threshold=0.0,
                dry_run=False,
            )
            assert await execute_sync(args_low) == 0

            # Very high rate limit
            args_high = argparse.Namespace(
                provider="gnula",
                type="movie",
                limit=1,
                unmapped_only=False,
                target=str(tmp_path / "orion2"),
                mappings_dir=str(tmp_path / "map2"),
                tmdb_key=None,
                rate_limit=50000.0,
                fuzzy_threshold=100.0,
                dry_run=False,
            )
            assert await execute_sync(args_high) == 0


# ==============================================================================
# 4. GITHUB ACTIONS WORKFLOW MULTI-ENGINE YAML & BASH SYNTAX ADVERSARIAL TESTS
# ==============================================================================
class TestGitHubWorkflowAdversarial:
    """Stress testing the GitHub Actions sync workflow YAML parsing and bash script syntax."""

    def test_workflow_file_exists(self):
        """Workflow file must exist at .github/workflows/sync-mappings.yml."""
        assert WORKFLOW_PATH.exists(), f"Workflow file not found at {WORKFLOW_PATH}"

    def test_workflow_yaml_parsing_across_pyyaml_loaders(self):
        """Verify YAML parses without errors across PyYAML SafeLoader, FullLoader, BaseLoader, CSafeLoader."""
        content = WORKFLOW_PATH.read_text(encoding="utf-8")

        loaders = [yaml.SafeLoader, yaml.FullLoader, yaml.BaseLoader]
        if hasattr(yaml, "CSafeLoader"):
            loaders.append(yaml.CSafeLoader)
        if hasattr(yaml, "CLoader"):
            loaders.append(yaml.CLoader)

        for loader in loaders:
            data = yaml.load(content, Loader=loader)
            assert isinstance(data, dict), f"Loader {loader.__name__} failed to produce a dict"
            assert data.get("name") == "Sync Mappings"
            assert "on" in data or True in data  # "on" might parse as boolean True in 1.1 loaders if unquoted

    def test_workflow_schema_invariants_and_triggers(self):
        """Validate critical structural constraints of the sync-mappings workflow."""
        content = WORKFLOW_PATH.read_text(encoding="utf-8")
        data = yaml.safe_load(content)

        # 1. Triggers
        triggers = data.get("on")
        assert triggers is not None, "Workflow missing 'on' trigger"
        assert "schedule" in triggers, "Workflow must have schedule trigger"
        schedule_cron = triggers["schedule"][0]["cron"]
        cron_parts = schedule_cron.split()
        assert len(cron_parts) == 5, f"Invalid cron expression: {schedule_cron}"

        # 2. Workflow dispatch inputs
        assert "workflow_dispatch" in triggers
        inputs = triggers["workflow_dispatch"].get("inputs", {})
        assert "provider" in inputs
        assert "type" in inputs
        assert "limit" in inputs
        assert "dry_run" in inputs

        assert inputs["dry_run"].get("type") == "boolean"
        assert inputs["dry_run"].get("default") is False

        # 3. Permissions
        permissions = data.get("permissions")
        assert permissions == {"contents": "write"}, "Workflow must declare contents: write permission"

        # 4. Sync Job
        jobs = data.get("jobs", {})
        assert "sync" in jobs
        sync_job = jobs["sync"]
        assert sync_job.get("runs-on") == "ubuntu-latest"
        assert sync_job.get("timeout-minutes") == 60

        steps = sync_job.get("steps", [])
        assert len(steps) >= 5

        step_names = [s.get("name") for s in steps]
        assert "Checkout repository" in step_names
        assert "Set up Python 3.12" in step_names
        assert "Install dependencies" in step_names
        assert "Run test suite" in step_names
        assert "Run sync pipeline" in step_names
        assert "Commit and push changes" in step_names

    def test_bash_syntax_validation_on_all_run_blocks(self):
        """Extract all 'run:' script blocks and validate them with 'bash -n' (syntax-check)."""
        content = WORKFLOW_PATH.read_text(encoding="utf-8")
        data = yaml.safe_load(content)

        steps = data["jobs"]["sync"]["steps"]
        run_steps = [s for s in steps if "run" in s]
        assert len(run_steps) >= 4

        for step in run_steps:
            step_name = step.get("name", "unnamed")
            script = step["run"]

            # Replace GitHub Actions expression placeholders with valid mock values for syntax checking
            sanitized_script = script
            while "${{" in sanitized_script and "}}" in sanitized_script:
                start = sanitized_script.find("${{")
                end = sanitized_script.find("}}", start) + 2
                sanitized_script = sanitized_script[:start] + '""' + sanitized_script[end:]

            proc = subprocess.run(
                ["bash", "-n"],
                input=sanitized_script,
                capture_output=True,
                text=True,
            )
            assert proc.returncode == 0, f"Bash syntax error in step '{step_name}':\n{proc.stderr}\nScript:\n{script}"

    @pytest.mark.parametrize(
        ("provider_val", "type_val", "limit_val", "dry_run_val", "expected_args"),
        [
            # Schedule trigger (empty inputs)
            ("", "", "", "", ["sync"]),
            # Workflow dispatch default
            ("all", "", "", "false", ["sync", "--provider", "all"]),
            # Specific provider + type
            ("serieskao", "series", "", "false", ["sync", "--provider", "serieskao", "--type", "series"]),
            # Specific provider + limit
            ("gnula", "movie", "25", "false", ["sync", "--provider", "gnula", "--type", "movie", "--limit", "25"]),
            # Dry run enabled
            ("all", "movie", "10", "true", ["sync", "--provider", "all", "--type", "movie", "--limit", "10", "--dry-run"]),
            # Boolean true variations
            ("allcalidad", "", "", "true", ["sync", "--provider", "allcalidad", "--dry-run"]),
        ],
    )
    def test_sync_step_bash_parameter_expansion_matrix(
        self,
        provider_val: str,
        type_val: str,
        limit_val: str,
        dry_run_val: str,
        expected_args: list[str],
    ):
        """Simulate execution of the 'Run sync pipeline' bash script across all input permutations."""
        bash_script = f"""
        ARGS="sync"
        if [ -n "{provider_val}" ]; then
          ARGS="$ARGS --provider {provider_val}"
        fi
        if [ -n "{type_val}" ]; then
          ARGS="$ARGS --type {type_val}"
        fi
        if [ -n "{limit_val}" ]; then
          ARGS="$ARGS --limit {limit_val}"
        fi
        if [ "{dry_run_val}" = "true" ]; then
          ARGS="$ARGS --dry-run"
        fi
        echo "$ARGS"
        """

        proc = subprocess.run(
            ["bash", "-c", bash_script],
            capture_output=True,
            text=True,
            check=True,
        )
        actual_output_args = proc.stdout.strip().split()
        assert actual_output_args == expected_args

    def test_git_commit_step_dry_run_skip_condition(self):
        """Verify that the commit step's 'if' expression correctly skips execution when dry_run is true."""
        content = WORKFLOW_PATH.read_text(encoding="utf-8")
        data = yaml.safe_load(content)

        steps = data["jobs"]["sync"]["steps"]
        commit_step = next(s for s in steps if s.get("name") == "Commit and push changes")

        condition = commit_step.get("if")
        assert condition is not None
        assert "dry_run" in condition
        assert "!= 'true'" in condition or "!=" in condition


# ==============================================================================
# 5. MAIN ENTRYPOINT & SUBPROCESS ADVERSARIAL TESTS
# ==============================================================================
class TestMainEntrypointAdversarial:
    """Stress testing the main CLI entry point (main.py) and process exit codes."""

    def test_main_py_help_flag_subprocess(self):
        """`python main.py --help` exits 0 with full command listings."""
        proc = subprocess.run(
            [sys.executable, str(REPO_ROOT / "main.py"), "--help"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert proc.returncode == 0
        assert "scrape" in proc.stdout
        assert "match" in proc.stdout
        assert "export" in proc.stdout
        assert "sync" in proc.stdout

    def test_main_py_version_flag_subprocess(self):
        """`python main.py --version` exits 0 with version string."""
        proc = subprocess.run(
            [sys.executable, str(REPO_ROOT / "main.py"), "--version"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert proc.returncode == 0
        assert "0.1.0" in proc.stdout

    def test_main_py_no_args_exits_zero(self):
        """`python main.py` with no args prints help and exits 0."""
        proc = subprocess.run(
            [sys.executable, str(REPO_ROOT / "main.py")],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert proc.returncode == 0
        assert "usage:" in proc.stdout.lower()

    def test_main_py_invalid_subcommand_exits_nonzero(self):
        """`python main.py invalid_subcommand` exits 2 (argparse error)."""
        proc = subprocess.run(
            [sys.executable, str(REPO_ROOT / "main.py"), "non_existent_command"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert proc.returncode != 0

    def test_main_py_sync_dry_run_subprocess_execution(self):
        """`python main.py sync --dry-run --limit 1` runs end-to-end via subprocess and exits 0."""
        proc = subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "main.py"),
                "sync",
                "--dry-run",
                "--provider",
                "serieskao",
                "--limit",
                "1",
            ],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert proc.returncode == 0, f"Sync dry-run failed with stderr: {proc.stderr}"

    def test_main_dispatcher_catches_unhandled_exception_and_returns_1(self):
        """When an unhandled exception occurs inside a command handler, main() returns 1."""
        with patch("orion_mapper.cli.commands.execute_sync", side_effect=Exception("Catastrophic error")):
            exit_code = main(["sync"])
            assert exit_code == 1

    def test_app_calls_main_and_sys_exit(self):
        """The app() entry point delegates to main() and calls sys.exit()."""
        with (
            patch("orion_mapper.cli.commands.main", return_value=42) as mock_main,
            patch("sys.exit") as mock_exit,
        ):
            app()
            mock_main.assert_called_once()
            mock_exit.assert_called_once_with(42)


# ==============================================================================
# 6. RESOURCE CLEANUP & LIFECYCLE ADVERSARIAL TESTS
# ==============================================================================
class TestResourceCleanupAndExceptionHandling:
    """Verify that HTTP sessions and file handles are properly cleaned up even upon catastrophic errors."""

    @pytest.mark.asyncio
    async def test_sync_closes_tmdb_client_when_reconciliation_fails(self, tmp_path: Path):
        """TmdbClient HTTP session must be closed in finally block when reconcile_batch raises."""
        mock_http = AsyncMock()
        mock_http.close = AsyncMock()

        scraper = MagicMock()
        scraper.supported_types = [ContentType.MOVIE]
        scraper.fetch_catalog = AsyncMock(return_value=[])

        with (
            patch("orion_mapper.cli.commands.get_scraper", return_value=scraper),
            patch(
                "orion_mapper.matcher.reconciler.IdentityReconciler.reconcile_batch",
                side_effect=RuntimeError("Reconciler crash"),
            ),
            patch("orion_mapper.cli.commands.TmdbClient") as MockTmdb,
        ):
            client_instance = MagicMock()
            client_instance._owns_http_client = True
            client_instance.http_client = mock_http
            MockTmdb.return_value = client_instance

            args = argparse.Namespace(
                provider="serieskao",
                type="movie",
                limit=1,
                unmapped_only=False,
                target=str(tmp_path / "target"),
                mappings_dir=str(tmp_path / "mappings"),
                tmdb_key=None,
                rate_limit=None,
                fuzzy_threshold=88.0,
                dry_run=False,
            )
            with pytest.raises(RuntimeError, match="Reconciler crash"):
                await execute_sync(args)

            mock_http.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_match_closes_tmdb_client_when_store_fails(self, tmp_path: Path):
        """TmdbClient HTTP session must be closed in finally block when store operations fail."""
        mock_http = AsyncMock()
        mock_http.close = AsyncMock()

        store = MasterMappingStore(storage_dir=tmp_path)
        store.add_or_update(
            CanonicalMapping(
                tmdb_id="123",
                imdb_id="tt123",
                title="Sample",
                type="movie",
                year=2020,
                providers={"gnula": "sample"},
            )
        )
        store.save()

        with (
            patch("orion_mapper.cli.commands.TmdbClient") as MockTmdb,
            patch(
                "orion_mapper.matcher.reconciler.IdentityReconciler.reconcile_item",
                side_effect=RuntimeError("Reconciliation item failure"),
            ),
        ):
            client_instance = MagicMock()
            client_instance._owns_http_client = True
            client_instance.http_client = mock_http
            MockTmdb.return_value = client_instance

            args = argparse.Namespace(
                source=str(tmp_path),
                tmdb_key=None,
                rate_limit=None,
                fuzzy_threshold=88.0,
                unmapped_only=False,
                limit=None,
                dry_run=False,
            )
            with pytest.raises(RuntimeError, match="Reconciliation item failure"):
                await execute_match(args)

            mock_http.close.assert_awaited_once()


# ==============================================================================
# 7. CLI FUZZING & ORACLE STRESS TESTS
# ==============================================================================
class TestCliFuzzingAndOracle:
    """Generator-based fuzzing of CLI argument patterns to ensure dispatcher robustness."""

    @pytest.mark.parametrize(
        "fuzzed_args",
        [
            ["--unknown-flag"],
            ["scrape", "--provider", "nonexistent_provider", "--limit", "-100"],
            ["scrape", "--type", "movie", "--rate-limit", "-5.0"],
            ["match", "--fuzzy-threshold", "999999.9"],
            ["match", "--fuzzy-threshold", "-50.0"],
            ["export", "--compress", "--dry-run"],
            ["export", "--source", "", "--target", ""],
            ["sync", "--provider", "all", "--limit", "0", "--dry-run"],
            ["sync", "--type", "series", "--unmapped-only", "--dry-run"],
            ["sync", "--rate-limit", "0"],
        ],
    )
    def test_cli_fuzzing_robustness(self, fuzzed_args: list[str]):
        """CLI main() should gracefully handle arbitrary fuzzed argument lists without unhandled crashes."""
        with (
            patch("orion_mapper.cli.commands.execute_scrape", new=AsyncMock(return_value=0)),
            patch("orion_mapper.cli.commands.execute_match", new=AsyncMock(return_value=0)),
            patch("orion_mapper.cli.commands.execute_export", return_value=0),
            patch("orion_mapper.cli.commands.execute_sync", new=AsyncMock(return_value=0)),
        ):
            try:
                exit_code = main(fuzzed_args)
                assert exit_code in [0, 1, 2], f"Unexpected exit code {exit_code} for args: {fuzzed_args}"
            except SystemExit as exc:
                assert exc.code in [0, 1, 2], f"Unexpected SystemExit code {exc.code} for args: {fuzzed_args}"


# ==============================================================================
# 8. WORKFLOW GIT SCRIPT SIMULATION HARNESS
# ==============================================================================
class TestGitWorkflowScriptSimulation:
    """Simulate execution of the workflow's git commit & push block in an actual local git repo."""

    def test_git_script_no_changes_detected(self, tmp_path: Path):
        """Simulate git commit step when no files have changed."""
        repo_dir = tmp_path / "mock_repo"
        repo_dir.mkdir()

        subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "tester"], cwd=repo_dir, check=True)
        subprocess.run(["git", "config", "user.email", "tester@example.com"], cwd=repo_dir, check=True)

        # Create directories
        (repo_dir / "data" / "mappings").mkdir(parents=True)
        (repo_dir / "data" / "orion_mappings").mkdir(parents=True)

        # Initial commit
        (repo_dir / "data" / "mappings" / "movies.json").write_text("[]", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
        subprocess.run(["git", "commit", "-m", "initial commit"], cwd=repo_dir, check=True)

        # Run the workflow bash snippet
        bash_script = """
        git config user.name "github-actions[bot]"
        git config user.email "github-actions[bot]@users.noreply.github.com"
        git add data/mappings/ data/orion_mappings/
        if git diff --staged --quiet; then
          echo "No mapping changes detected to commit."
        else
          git commit -m "chore(mappings): auto-sync provider mappings [skip ci]"
        fi
        """
        proc = subprocess.run(["bash", "-c", bash_script], cwd=repo_dir, capture_output=True, text=True)
        assert proc.returncode == 0
        assert "No mapping changes detected to commit." in proc.stdout

    def test_git_script_commits_changes_when_present(self, tmp_path: Path):
        """Simulate git commit step when mapping changes are present."""
        repo_dir = tmp_path / "mock_repo"
        repo_dir.mkdir()

        subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "tester"], cwd=repo_dir, check=True)
        subprocess.run(["git", "config", "user.email", "tester@example.com"], cwd=repo_dir, check=True)

        (repo_dir / "data" / "mappings").mkdir(parents=True)
        (repo_dir / "data" / "orion_mappings").mkdir(parents=True)

        (repo_dir / "data" / "mappings" / "movies.json").write_text("[]", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
        subprocess.run(["git", "commit", "-m", "initial commit"], cwd=repo_dir, check=True)

        # Mutate mapping file
        (repo_dir / "data" / "mappings" / "movies.json").write_text('[{"title": "Matrix"}]', encoding="utf-8")

        bash_script = """
        git config user.name "github-actions[bot]"
        git config user.email "github-actions[bot]@users.noreply.github.com"
        git add data/mappings/ data/orion_mappings/
        if git diff --staged --quiet; then
          echo "No mapping changes detected to commit."
        else
          git commit -m "chore(mappings): auto-sync provider mappings [skip ci]"
        fi
        """
        proc = subprocess.run(["bash", "-c", bash_script], cwd=repo_dir, capture_output=True, text=True)
        assert proc.returncode == 0
        assert "auto-sync provider mappings" in proc.stdout or proc.returncode == 0

        # Check commit log
        log_proc = subprocess.run(["git", "log", "-1", "--oneline"], cwd=repo_dir, capture_output=True, text=True)
        assert "chore(mappings): auto-sync provider mappings" in log_proc.stdout
