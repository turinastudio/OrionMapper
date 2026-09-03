"""Unit tests for OrionMapper CLI interface (argparse parser, commands, and dispatcher)."""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orion_mapper.cli.commands import (
    _audit_slug_sets,
    app,
    create_cli_parser,
    execute_export,
    execute_match,
    execute_scrape,
    execute_sync,
    main,
)
from orion_mapper.models.item import ContentType, ScrapedItem
from orion_mapper.models.mapping import CanonicalMapping
from orion_mapper.scrapers import BaseScraper
from orion_mapper.storage.master import MasterMappingStore


# ==============================================================================
# 1. Parser Tests
# ==============================================================================
class TestCliParser:
    def test_parser_prog_and_description(self):
        parser = create_cli_parser()
        assert parser.prog == "orion-mapper"
        assert "OrionMapper CLI" in parser.description

    def test_audit_slug_sets_counts_duplicates_and_differences(self):
        result = _audit_slug_sets(
            ["one", "/two/", "two", "three"],
            {"one", "old"},
        )

        assert result["catalog_entries"] == 4
        assert result["catalog_unique_slugs"] == 3
        assert result["catalog_duplicate_slugs"] == 1
        assert result["missing_slugs"] == ["three", "two"]
        assert result["stale_mapped_slugs"] == ["old"]

    def test_parser_version_flag(self):
        parser = create_cli_parser()
        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args(["--version"])
        assert exc_info.value.code == 0

    def test_parser_scrape_defaults_and_options(self):
        parser = create_cli_parser()

        # Defaults
        args = parser.parse_args(["scrape"])
        assert args.command == "scrape"
        assert args.provider == "all"
        assert args.type is None
        assert args.limit is None
        assert args.output_dir is None
        assert args.dry_run is False
        assert args.rate_limit is None

        # Custom options
        args = parser.parse_args(
            [
                "scrape",
                "-p",
                "gnula",
                "-t",
                "movie",
                "-l",
                "25",
                "-o",
                "/tmp/out",
                "--dry-run",
                "-r",
                "10.5",
            ]
        )
        assert args.provider == "gnula"
        assert args.type == "movie"
        assert args.limit == 25
        assert args.output_dir == "/tmp/out"
        assert args.dry_run is True
        assert args.rate_limit == 10.5

    def test_parser_match_defaults_and_options(self):
        parser = create_cli_parser()

        # Defaults
        args = parser.parse_args(["match"])
        assert args.command == "match"
        assert args.unmapped_only is False
        assert args.limit is None
        assert args.tmdb_key is None
        assert args.rate_limit is None
        assert args.fuzzy_threshold == 88.0
        assert args.source is None
        assert args.dry_run is False

        # Custom options
        args = parser.parse_args(
            [
                "match",
                "--unmapped-only",
                "-l",
                "50",
                "-k",
                "my_custom_key",
                "-r",
                "20.0",
                "-f",
                "92.5",
                "-s",
                "/custom/mappings",
                "--dry-run",
            ]
        )
        assert args.unmapped_only is True
        assert args.limit == 50
        assert args.tmdb_key == "my_custom_key"
        assert args.rate_limit == 20.0
        assert args.fuzzy_threshold == 92.5
        assert args.source == "/custom/mappings"
        assert args.dry_run is True

    def test_parser_export_defaults_and_options(self):
        parser = create_cli_parser()

        # Defaults
        args = parser.parse_args(["export"])
        assert args.command == "export"
        assert args.target is None
        assert args.source is None
        assert args.compress is False
        assert args.dry_run is False

        # Custom options
        args = parser.parse_args(
            [
                "export",
                "-t",
                "/custom/orion",
                "-s",
                "/custom/master",
                "--compress",
                "--dry-run",
            ]
        )
        assert args.target == "/custom/orion"
        assert args.source == "/custom/master"
        assert args.compress is True
        assert args.dry_run is True

    def test_parser_sync_defaults_and_options(self):
        parser = create_cli_parser()

        # Defaults
        args = parser.parse_args(["sync"])
        assert args.command == "sync"
        assert args.provider == "all"
        assert args.type is None
        assert args.limit is None
        assert args.unmapped_only is False
        assert args.target is None
        assert args.mappings_dir is None
        assert args.tmdb_key is None
        assert args.rate_limit is None
        assert args.fuzzy_threshold == 88.0
        assert args.dry_run is False

        # Custom options
        args = parser.parse_args(
            [
                "sync",
                "-p",
                "serieskao",
                "-t",
                "series",
                "-l",
                "15",
                "--unmapped-only",
                "-t",
                "series",
                "--target",
                "/tmp/orion_out",
                "-m",
                "/tmp/master_out",
                "-k",
                "secret_key",
                "-r",
                "30.0",
                "-f",
                "90.0",
                "--dry-run",
            ]
        )
        assert args.provider == "serieskao"
        assert args.limit == 15
        assert args.unmapped_only is True
        assert args.target == "/tmp/orion_out"
        assert args.mappings_dir == "/tmp/master_out"
        assert args.tmdb_key == "secret_key"
        assert args.rate_limit == 30.0
        assert args.fuzzy_threshold == 90.0
        assert args.dry_run is True

    def test_parser_invalid_subcommand_raises_system_exit(self):
        parser = create_cli_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["unknown_command"])

    def test_parser_type_choices_validation(self):
        parser = create_cli_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["scrape", "--type", "invalid_type"])

    def test_parser_negative_limit(self):
        parser = create_cli_parser()
        args = parser.parse_args(["scrape", "--limit", "-10"])
        assert args.limit == -10

    def test_parser_sync_distinct_type_and_target_flags(self):
        parser = create_cli_parser()
        args_type = parser.parse_args(["sync", "-T", "series"])
        assert args_type.type == "series"
        assert args_type.target is None

        args_target = parser.parse_args(["sync", "-t", "/custom/target"])
        assert args_target.target == "/custom/target"
        assert args_target.type is None


# ==============================================================================
# 2. Scrape Command Execution Tests
# ==============================================================================
class TestCliScrapeCommand:
    @pytest.mark.asyncio
    async def test_execute_scrape_single_provider(self, tmp_path: Path):
        mock_items = [
            ScrapedItem(
                provider="serieskao",
                slug="matrix",
                title="The Matrix",
                type="movie",
                year=1999,
                imdb_id="tt0133093",
            )
        ]
        mock_scraper = MagicMock()
        mock_scraper.supported_types = [ContentType.MOVIE, ContentType.SERIES]
        mock_scraper.fetch_catalog = AsyncMock(side_effect=[mock_items, []])

        with patch("orion_mapper.cli.commands.get_scraper", return_value=mock_scraper):
            args = argparse.Namespace(
                provider="serieskao",
                type="movie",
                limit=10,
                output_dir=str(tmp_path),
                dry_run=False,
                rate_limit=5.0,
            )
            exit_code = await execute_scrape(args)
            assert exit_code == 0

            out_file = tmp_path / "serieskao_movie.json"
            assert out_file.exists()
            assert "The Matrix" in out_file.read_text(encoding="utf-8")

    @pytest.mark.asyncio
    async def test_execute_scrape_all_providers(self):
        mock_scraper = MagicMock()
        mock_scraper.supported_types = [ContentType.MOVIE]
        mock_scraper.fetch_catalog = AsyncMock(return_value=[])

        with (
            patch(
                "orion_mapper.cli.commands.get_registered_providers",
                return_value=["serieskao", "gnula"],
            ),
            patch("orion_mapper.cli.commands.get_scraper", return_value=mock_scraper) as mock_get,
        ):
            args = argparse.Namespace(
                provider="all",
                type="movie",
                limit=5,
                output_dir=None,
                dry_run=True,
                rate_limit=None,
            )
            exit_code = await execute_scrape(args)
            assert exit_code == 0
            assert mock_get.call_count == 2
            assert mock_get.call_args_list[0].args[0] == "serieskao"
            assert mock_get.call_args_list[1].args[0] == "gnula"

    @pytest.mark.asyncio
    async def test_execute_scrape_dry_run_no_files_written(self, tmp_path: Path):
        mock_items = [
            ScrapedItem(
                provider="gnula",
                slug="fight-club",
                title="Fight Club",
                type="movie",
                year=1999,
            )
        ]
        mock_scraper = MagicMock()
        mock_scraper.supported_types = [ContentType.MOVIE]
        mock_scraper.fetch_catalog = AsyncMock(side_effect=[mock_items, []])

        with patch("orion_mapper.cli.commands.get_scraper", return_value=mock_scraper):
            args = argparse.Namespace(
                provider="gnula",
                type="movie",
                limit=5,
                output_dir=str(tmp_path),
                dry_run=True,
                rate_limit=None,
            )
            exit_code = await execute_scrape(args)
            assert exit_code == 0
            out_file = tmp_path / "gnula_movie.json"
            assert not out_file.exists()

    @pytest.mark.asyncio
    async def test_execute_scrape_handles_scraper_exception_resiliently(self):
        with patch("orion_mapper.cli.commands.get_scraper", side_effect=ValueError("Scraper init failed")):
            args = argparse.Namespace(
                provider="serieskao",
                type=None,
                limit=None,
                output_dir=None,
                dry_run=False,
                rate_limit=None,
            )
            exit_code = await execute_scrape(args)
            assert exit_code == 0


# ==============================================================================
# 3. Match Command Execution Tests
# ==============================================================================
class TestCliMatchCommand:
    @pytest.mark.asyncio
    async def test_execute_match_basic(self, tmp_path: Path):
        store = MasterMappingStore(storage_dir=tmp_path)
        store.add_or_update(
            CanonicalMapping(
                tmdb_id=None,
                imdb_id="tt0137523",
                title="Fight Club",
                type="movie",
                year=1999,
                providers={"gnula": "fight-club"},
            )
        )
        store.save()

        mock_reconciled = CanonicalMapping(
            tmdb_id="550",
            imdb_id="tt0137523",
            title="Fight Club",
            type="movie",
            year=1999,
            providers={"gnula": "fight-club"},
        )

        with (
            patch(
                "orion_mapper.matcher.reconciler.IdentityReconciler.reconcile_item",
                new=AsyncMock(return_value=mock_reconciled),
            ),
        ):
            args = argparse.Namespace(
                source=str(tmp_path),
                tmdb_key="test_key",
                rate_limit=10.0,
                fuzzy_threshold=88.0,
                unmapped_only=True,
                limit=10,
                dry_run=False,
            )
            exit_code = await execute_match(args)
            assert exit_code == 0

            reloaded_store = MasterMappingStore(storage_dir=tmp_path)
            mapping = reloaded_store.get_by_imdb("tt0137523", "movie")
            assert mapping is not None
            assert mapping.tmdb_id == "550"

    @pytest.mark.asyncio
    async def test_execute_match_dry_run(self, tmp_path: Path):
        store = MasterMappingStore(storage_dir=tmp_path)
        store.add_or_update(
            CanonicalMapping(
                tmdb_id=None,
                imdb_id="tt0137523",
                title="Fight Club",
                type="movie",
                year=1999,
                providers={"gnula": "fight-club"},
            )
        )
        store.save()

        mock_reconciled = CanonicalMapping(
            tmdb_id="550",
            imdb_id="tt0137523",
            title="Fight Club",
            type="movie",
            year=1999,
            providers={"gnula": "fight-club"},
        )

        with (
            patch(
                "orion_mapper.matcher.reconciler.IdentityReconciler.reconcile_item",
                new=AsyncMock(return_value=mock_reconciled),
            ),
        ):
            args = argparse.Namespace(
                source=str(tmp_path),
                tmdb_key=None,
                rate_limit=None,
                fuzzy_threshold=88.0,
                unmapped_only=False,
                limit=None,
                dry_run=True,
            )
            exit_code = await execute_match(args)
            assert exit_code == 0

            # Store on disk should still have tmdb_id=None
            reloaded = MasterMappingStore(storage_dir=tmp_path)
            mapping = reloaded.get_by_imdb("tt0137523", "movie")
            assert mapping is not None
            assert mapping.tmdb_id is None

    @pytest.mark.asyncio
    async def test_execute_match_with_zero_and_negative_limit_processes_zero_items(self, tmp_path: Path):
        store = MasterMappingStore(storage_dir=tmp_path)
        store.add_or_update(
            CanonicalMapping(
                tmdb_id=None,
                imdb_id="tt0137523",
                title="Fight Club",
                type="movie",
                year=1999,
                providers={"gnula": "fight-club"},
            )
        )
        store.save()

        mock_reconcile = AsyncMock()
        with patch("orion_mapper.matcher.reconciler.IdentityReconciler.reconcile_item", new=mock_reconcile):
            # Test limit=0
            args_zero = argparse.Namespace(
                source=str(tmp_path),
                tmdb_key=None,
                rate_limit=None,
                fuzzy_threshold=88.0,
                unmapped_only=False,
                limit=0,
                dry_run=True,
            )
            exit_code = await execute_match(args_zero)
            assert exit_code == 0
            assert mock_reconcile.call_count == 0

            # Test negative limit
            args_neg = argparse.Namespace(
                source=str(tmp_path),
                tmdb_key=None,
                rate_limit=None,
                fuzzy_threshold=88.0,
                unmapped_only=False,
                limit=-5,
                dry_run=True,
            )
            exit_code = await execute_match(args_neg)
            assert exit_code == 0
            assert mock_reconcile.call_count == 0


# ==============================================================================
# 4. Export Command Execution Tests
# ==============================================================================
class TestCliExportCommand:
    def test_execute_export_full(self, tmp_path: Path):
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
            dry_run=False,
        )
        exit_code = execute_export(args)
        assert exit_code == 0

        # Verify index files were written
        assert (target_dir / "imdb" / "tt0137523.json").exists()
        assert (target_dir / "tmdb" / "550.json").exists()
        assert (target_dir / "providers").exists()

    def test_execute_export_dry_run(self, tmp_path: Path):
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
        assert not target_dir.exists()


# ==============================================================================
# 5. Sync Command Execution Tests
# ==============================================================================
class TestCliSyncCommand:
    @pytest.mark.asyncio
    async def test_execute_sync_stops_only_after_a_fully_known_page(self, tmp_path: Path):
        mappings_dir = tmp_path / "mappings"
        store = MasterMappingStore(storage_dir=mappings_dir)
        store.save_mapping(
            CanonicalMapping(
                tmdb_id="550",
                imdb_id="tt0137523",
                title="Known Movie",
                type="movie",
                providers={"serieskao": "known-movie"},
            )
        )

        new_item = ScrapedItem(
            provider="serieskao",
            slug="new-movie",
            title="New Movie",
            type="movie",
            tmdb_id="551",
            imdb_id="tt0137524",
        )
        known_item = ScrapedItem(
            provider="serieskao",
            slug="known-movie",
            title="Known Movie",
            type="movie",
            tmdb_id="550",
            imdb_id="tt0137523",
        )

        class IncrementalScraper(BaseScraper):
            name = "serieskao"
            base_url = "https://serieskao.example"

            def __init__(self):
                self.pages_requested: list[int] = []

            async def fetch_catalog(self, content_type, page=1, genre=None):
                self.pages_requested.append(page)
                if page == 1:
                    return [new_item, known_item]
                if page == 2:
                    return [known_item]
                return []

            async def fetch_detail(self, slug, content_type):
                return None

        scraper = IncrementalScraper()
        reconciled = [
            CanonicalMapping(
                tmdb_id="551",
                imdb_id="tt0137524",
                title="New Movie",
                type="movie",
                providers={"serieskao": "new-movie"},
            )
        ]

        with (
            patch("orion_mapper.cli.commands.get_scraper", return_value=scraper),
            patch(
                "orion_mapper.matcher.reconciler.IdentityReconciler.reconcile_batch",
                new=AsyncMock(return_value=reconciled),
            ),
        ):
            args = argparse.Namespace(
                provider="serieskao",
                type="movie",
                limit=None,
                max_pages=1000,
                unmapped_only=False,
                target=str(tmp_path / "orion"),
                mappings_dir=str(mappings_dir),
                tmdb_key=None,
                rate_limit=None,
                fuzzy_threshold=88.0,
                dry_run=True,
            )
            assert await execute_sync(args) == 0

        # A known page is not a safe stopping boundary: page 3 might contain
        # an unseen item. Continue until the provider reports end-of-catalog.
        assert scraper.pages_requested == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_execute_sync_full_pipeline(self, tmp_path: Path):
        mappings_dir = tmp_path / "mappings"
        target_dir = tmp_path / "orion_mappings"

        mock_scraped = [
            ScrapedItem(
                provider="serieskao",
                slug="zombieland-saga",
                title="Zombieland Saga",
                type="series",
                year=2018,
                imdb_id="tt15486",
                tmdb_id="21048",
            )
        ]
        mock_scraper = MagicMock()
        mock_scraper.supported_types = [ContentType.SERIES]
        mock_scraper.fetch_catalog = AsyncMock(side_effect=[mock_scraped, []])

        mock_reconciled = [
            CanonicalMapping(
                tmdb_id="21048",
                imdb_id="tt15486",
                title="Zombieland Saga",
                type="series",
                year=2018,
                providers={"serieskao": "zombieland-saga"},
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
                limit=5,
                unmapped_only=False,
                target=str(target_dir),
                mappings_dir=str(mappings_dir),
                tmdb_key="test_key",
                rate_limit=10.0,
                fuzzy_threshold=88.0,
                dry_run=False,
            )
            exit_code = await execute_sync(args)
            assert exit_code == 0

            # Master store updated
            assert (mappings_dir / "series.json").exists()
            # Orion indexes exported
            assert (target_dir / "imdb" / "tt15486.json").exists()
            assert (target_dir / "tmdb" / "21048.json").exists()

    @pytest.mark.asyncio
    async def test_execute_sync_dry_run(self, tmp_path: Path):
        mappings_dir = tmp_path / "mappings"
        target_dir = tmp_path / "orion_mappings"

        mock_scraped = [
            ScrapedItem(
                provider="serieskao",
                slug="zombieland-saga",
                title="Zombieland Saga",
                type="series",
                year=2018,
                imdb_id="tt15486",
                tmdb_id="21048",
            )
        ]
        mock_scraper = MagicMock()
        mock_scraper.supported_types = [ContentType.SERIES]
        mock_scraper.fetch_catalog = AsyncMock(side_effect=[mock_scraped, []])

        with (
            patch("orion_mapper.cli.commands.get_scraper", return_value=mock_scraper),
            patch(
                "orion_mapper.matcher.reconciler.IdentityReconciler.reconcile_batch",
                new=AsyncMock(return_value=[]),
            ),
        ):
            args = argparse.Namespace(
                provider="serieskao",
                type="series",
                limit=5,
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
    async def test_execute_sync_passes_custom_rate_limiter_to_scrapers(self, tmp_path: Path):
        captured_limiters = []

        def mock_get_scraper(name, rate_limiter=None):
            captured_limiters.append(rate_limiter)
            scraper = MagicMock()
            scraper.supported_types = [ContentType.MOVIE]
            scraper.fetch_catalog = AsyncMock(return_value=[])
            return scraper

        with (
            patch("orion_mapper.cli.commands.get_registered_providers", return_value=["gnula"]),
            patch("orion_mapper.cli.commands.get_scraper", side_effect=mock_get_scraper),
            patch("orion_mapper.matcher.reconciler.IdentityReconciler.reconcile_batch", new=AsyncMock(return_value=[])),
        ):
            args = argparse.Namespace(
                provider="gnula",
                type="movie",
                limit=5,
                unmapped_only=False,
                target=str(tmp_path / "orion"),
                mappings_dir=str(tmp_path / "mappings"),
                tmdb_key=None,
                rate_limit=15.0,
                fuzzy_threshold=88.0,
                dry_run=True,
            )
            exit_code = await execute_sync(args)
            assert exit_code == 0
            assert len(captured_limiters) == 1
            assert captured_limiters[0] is not None
            assert captured_limiters[0].rate == 15.0


# ==============================================================================
# 6. Main Dispatcher Tests
# ==============================================================================
class TestMainDispatcher:
    def test_main_empty_args_shows_help(self):
        exit_code = main([])
        assert exit_code == 0

    def test_main_scrape_dispatch(self):
        with patch("orion_mapper.cli.commands.execute_scrape", new=AsyncMock(return_value=0)):
            exit_code = main(["scrape", "--provider", "serieskao"])
            assert exit_code == 0

    def test_main_match_dispatch(self):
        with patch("orion_mapper.cli.commands.execute_match", new=AsyncMock(return_value=0)):
            exit_code = main(["match", "--unmapped-only"])
            assert exit_code == 0

    def test_main_export_dispatch(self):
        with patch("orion_mapper.cli.commands.execute_export", return_value=0):
            exit_code = main(["export", "--target", "/tmp/test"])
            assert exit_code == 0

    def test_main_sync_dispatch(self):
        with patch("orion_mapper.cli.commands.execute_sync", new=AsyncMock(return_value=0)):
            exit_code = main(["sync", "--dry-run"])
            assert exit_code == 0

    def test_main_recover_audit_dispatch(self):
        with patch("orion_mapper.cli.commands.execute_recover_audit", new=AsyncMock(return_value=0)):
            exit_code = main(["recover-audit", "--dry-run"])
            assert exit_code == 0

    def test_main_exception_handling_returns_1(self):
        with patch("orion_mapper.cli.commands.execute_export", side_effect=RuntimeError("Fatal error")):
            exit_code = main(["export"])
            assert exit_code == 1

    def test_app_calls_main_and_exits(self):
        with (
            patch("orion_mapper.cli.commands.main", return_value=0) as mock_main,
            patch("sys.exit") as mock_exit,
        ):
            app()
            mock_main.assert_called_once()
            mock_exit.assert_called_once_with(0)
