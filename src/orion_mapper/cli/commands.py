"""OrionMapper CLI commands and dispatcher."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from orion_mapper.core.rate_limiter import TokenBucketLimiter
from orion_mapper.matcher.reconciler import IdentityReconciler
from orion_mapper.models.item import ContentType, ScrapedItem
from orion_mapper.resolver.tmdb import TmdbClient
from orion_mapper.scrapers import BaseScraper, get_registered_providers, get_scraper
from orion_mapper.storage.master import MasterMappingStore, atomic_write_json
from orion_mapper.storage.orion_exporter import OrionExporter

logger = logging.getLogger("orion_mapper.cli")

# AllCalidad requires its MD5 identity table and Gnula is currently excluded
# from the operational sync. Keep both implementations available for
# explicit/manual runs, but exclude them from automatic ``all`` executions.
DISABLED_AUTOMATIC_PROVIDERS = {"allcalidad", "gnula"}


def _load_sync_state(path: Path) -> dict[str, dict[str, int]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_sync_state(path: Path, state: dict[str, dict[str, int]]) -> None:
    atomic_write_json(path, state)


async def _enrich_scraped_items(scraper: BaseScraper, items: list[ScrapedItem]) -> None:
    """Fetch item details so providers can contribute direct identifiers."""
    for item in items:
        try:
            detail = await scraper.fetch_detail(item.slug, item.type)
        except Exception as exc:
            logger.warning(
                "Could not fetch detail for %s:%s: %s",
                item.provider,
                item.slug,
                exc,
            )
            continue

        if detail is None:
            continue

        # Catalog metadata remains authoritative when present; detail metadata
        # fills missing values, especially direct IMDb/TMDB identifiers.
        if detail.imdb_id:
            item.imdb_id = detail.imdb_id
        if detail.tmdb_id:
            item.tmdb_id = detail.tmdb_id
        if not item.year and detail.year:
            item.year = detail.year
        if not item.poster_url and detail.poster_url:
            item.poster_url = detail.poster_url
        if not item.title and detail.title:
            item.title = detail.title


def create_cli_parser() -> argparse.ArgumentParser:
    """Create and configure the main CLI argument parser with all subcommands."""
    parser = argparse.ArgumentParser(
        prog="orion-mapper",
        description="OrionMapper CLI - Automated Cross-Provider Identity Mapper & Exporter for OrionServer",
    )
    parser.add_argument(
        "--version",
        action="version",
        version="%(prog)s 0.1.0",
    )

    subparsers = parser.add_subparsers(
        dest="command",
        title="subcommands",
        description="Available operational commands",
        help="Subcommand to execute",
    )

    # 1. SCRAPE
    scrape_parser = subparsers.add_parser(
        "scrape",
        help="Scrape catalogs from specified provider(s)",
        description="Extracts movie/series items from provider sites without TMDB resolution",
    )
    scrape_parser.add_argument(
        "--provider",
        "-p",
        type=str,
        default="all",
        help="Provider to scrape: serieskao, poseidonhd2, gnula, allcalidad, or all (default: all)",
    )
    scrape_parser.add_argument(
        "--type",
        "-t",
        type=str,
        choices=["movie", "series"],
        default=None,
        help="Content type filter: movie, series (default: both)",
    )
    scrape_parser.add_argument(
        "--limit",
        "-l",
        type=int,
        default=None,
        help="Maximum items to scrape per provider/type",
    )
    scrape_parser.add_argument(
        "--output-dir",
        "-o",
        type=str,
        default=None,
        help="Directory to save raw scraped JSON items",
    )
    scrape_parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Simulate scraping without saving raw output to disk",
    )
    scrape_parser.add_argument(
        "--rate-limit",
        "-r",
        type=float,
        default=None,
        help="Override provider HTTP rate limit (req/s)",
    )

    # 2. MATCH
    match_parser = subparsers.add_parser(
        "match",
        help="Resolve and match items against TMDB / IMDb",
        description="Matches canonical identities using TMDB find, external IDs, and title/year fuzzy scoring",
    )
    match_parser.add_argument(
        "--unmapped-only",
        action="store_true",
        default=False,
        help="Only match entries lacking TMDB or IMDb IDs",
    )
    match_parser.add_argument(
        "--limit",
        "-l",
        type=int,
        default=None,
        help="Maximum items to reconcile",
    )
    match_parser.add_argument(
        "--tmdb-key",
        "-k",
        type=str,
        default=None,
        help="TMDB v3 API Key override",
    )
    match_parser.add_argument(
        "--rate-limit",
        "-r",
        type=float,
        default=None,
        help="TMDB API rate limit (req/s, default: 40.0)",
    )
    match_parser.add_argument(
        "--fuzzy-threshold",
        "-f",
        type=float,
        default=88.0,
        help="Confidence threshold for fuzzy title matching (default: 88.0)",
    )
    match_parser.add_argument(
        "--source",
        "-s",
        type=str,
        default=None,
        help="Source directory for master mappings (default: data/mappings)",
    )
    match_parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Simulate matching without saving changes to disk",
    )

    # 3. EXPORT
    export_parser = subparsers.add_parser(
        "export",
        help="Export master dataset to OrionServer FileIdentityMappingStore format",
        description="Generates imdb/{id}.json, tmdb/{id}.json, and providers/{base64}.json",
    )
    export_parser.add_argument(
        "--target",
        "-t",
        type=str,
        default=None,
        help="Target directory for OrionServer indexes (default: data/orion_mappings)",
    )
    export_parser.add_argument(
        "--source",
        "-s",
        type=str,
        default=None,
        help="Source directory for master mappings (default: data/mappings)",
    )
    export_parser.add_argument(
        "--compress",
        action="store_true",
        default=False,
        help="Enable compact JSON export",
    )
    export_parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Simulate export without writing index files to disk",
    )

    # 4. SYNC
    sync_parser = subparsers.add_parser(
        "sync",
        help="Orchestrate full sync pipeline: scrape -> match -> master store -> Orion export",
        description="Runs automated end-to-end identity synchronization and export",
    )
    sync_parser.add_argument(
        "--provider",
        "-p",
        type=str,
        default="all",
        help="Provider to sync (serieskao, poseidonhd2, gnula, allcalidad, or all)",
    )
    sync_parser.add_argument(
        "--type",
        "-T",
        type=str,
        choices=["movie", "series"],
        default=None,
        help="Content type filter (movie, series, or both)",
    )
    sync_parser.add_argument(
        "--limit",
        "-l",
        type=int,
        default=None,
        help="Maximum items per provider/type to scrape and synchronize",
    )
    sync_parser.add_argument(
        "--max-pages",
        type=int,
        default=1000,
        help="Maximum catalog pages to scan per provider/type in this run (default: 1000)",
    )
    sync_parser.add_argument(
        "--head-pages",
        type=int,
        default=5,
        help="Newest catalog pages checked every run (default: 5)",
    )
    sync_parser.add_argument(
        "--state-file",
        type=str,
        default=None,
        help="Sync cursor file (default: data/sync_state.json)",
    )
    sync_parser.add_argument(
        "--unmapped-only",
        action="store_true",
        default=False,
        help="Only attempt reconciliation for unmapped entries",
    )
    sync_parser.add_argument(
        "--target",
        "-t",
        type=str,
        default=None,
        help="Target directory for OrionServer export (default: data/orion_mappings)",
    )
    sync_parser.add_argument(
        "--mappings-dir",
        "-m",
        type=str,
        default=None,
        help="Directory for master movies.json and series.json (default: data/mappings)",
    )
    sync_parser.add_argument(
        "--tmdb-key",
        "-k",
        type=str,
        default=None,
        help="TMDB v3 API Key override",
    )
    sync_parser.add_argument(
        "--rate-limit",
        "-r",
        type=float,
        default=None,
        help="TMDB API rate limit (req/s)",
    )
    sync_parser.add_argument(
        "--fuzzy-threshold",
        "-f",
        type=float,
        default=88.0,
        help="Confidence threshold for fuzzy title matching (default: 88.0)",
    )
    sync_parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Simulate sync pipeline without persisting any changes to disk",
    )

    return parser


async def execute_scrape(args: argparse.Namespace) -> int:
    """Execute the scrape command."""
    provider_arg = (getattr(args, "provider", None) or "all").strip().lower()
    if provider_arg == "all":
        provider_names = [
            name
            for name in get_registered_providers()
            if name not in DISABLED_AUTOMATIC_PROVIDERS
        ]
    else:
        provider_names = [provider_arg]

    content_types: list[ContentType]
    if getattr(args, "type", None):
        content_types = [ContentType(args.type)]
    else:
        content_types = [ContentType.MOVIE, ContentType.SERIES]

    limit: int | None = getattr(args, "limit", None)
    output_dir_str: str | None = getattr(args, "output_dir", None)
    dry_run: bool = getattr(args, "dry_run", False)
    custom_rate: float | None = getattr(args, "rate_limit", None)

    rate_limiter = (
        TokenBucketLimiter(rate=custom_rate, capacity=int(max(1, custom_rate)))
        if custom_rate and custom_rate > 0
        else None
    )

    output_dir = Path(output_dir_str) if output_dir_str else None
    if output_dir and not dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)

    total_scraped = 0
    for prov_name in provider_names:
        try:
            scraper = get_scraper(prov_name, rate_limiter=rate_limiter)
        except Exception as exc:
            logger.warning("Could not initialize scraper for provider '%s': %s", prov_name, exc)
            continue

        for c_type in content_types:
            if c_type not in scraper.supported_types:
                continue

            if limit is not None and limit <= 0:
                continue

            items: list[ScrapedItem] = []
            page = 1
            max_pages = 500
            while page <= max_pages:
                try:
                    page_items = await scraper.fetch_catalog(content_type=c_type, page=page)
                except Exception as exc:
                    logger.warning(
                        "Error fetching catalog for %s %s page %d: %s",
                        prov_name,
                        c_type.value,
                        page,
                        exc,
                    )
                    break

                if not page_items:
                    break

                if limit is not None:
                    remaining = limit - len(items)
                    items.extend(page_items[:remaining])
                    if len(items) >= limit:
                        break
                else:
                    items.extend(page_items)

                page += 1

            total_scraped += len(items)

            # The catalog contains slugs/titles only for some providers. Fetch
            # details before persisting so direct identifiers exposed there
            # (e.g. SeriesKao's /vidurl/tt... player ID) are not lost.
            if items and isinstance(scraper, BaseScraper):
                await _enrich_scraped_items(scraper, items)

            logger.info("Scraped %d items from %s (%s)", len(items), prov_name, c_type.value)

            if output_dir and not dry_run:
                file_path = output_dir / f"{prov_name}_{c_type.value}.json"
                atomic_write_json(file_path, [item.model_dump() for item in items])

    logger.info("Total items scraped across providers: %d", total_scraped)
    return 0


async def execute_match(args: argparse.Namespace) -> int:
    """Execute the match command."""
    source_dir = getattr(args, "source", None)
    store = MasterMappingStore(storage_dir=source_dir)

    tmdb_key = getattr(args, "tmdb_key", None)
    custom_rate = getattr(args, "rate_limit", None)
    fuzzy_thresh = getattr(args, "fuzzy_threshold", 88.0) or 88.0
    unmapped_only = getattr(args, "unmapped_only", False)
    limit = getattr(args, "limit", None)
    dry_run = getattr(args, "dry_run", False)

    rate_limiter = (
        TokenBucketLimiter(rate=custom_rate, capacity=int(max(1, custom_rate)))
        if custom_rate and custom_rate > 0
        else None
    )
    tmdb_client = TmdbClient(api_key=tmdb_key, rate_limiter=rate_limiter)
    reconciler = IdentityReconciler(tmdb_client=tmdb_client, confidence_threshold=fuzzy_thresh)

    mappings = store.all_mappings()
    if unmapped_only:
        mappings = [m for m in mappings if not m.tmdb_id or not m.imdb_id]

    if limit is not None:
        if limit <= 0:
            mappings = []
        else:
            mappings = mappings[:limit]

    matched_count = 0
    try:
        for m in mappings:
            first_prov = next(iter(m.providers.keys())) if m.providers else "unknown"
            first_slug = next(iter(m.providers.values())) if m.providers else ""
            item = ScrapedItem(
                provider=first_prov,
                slug=first_slug,
                title=m.title,
                type=m.type,
                year=m.year,
                imdb_id=m.imdb_id,
                tmdb_id=m.tmdb_id,
            )
            reconciled = await reconciler.reconcile_item(item, master_store=store)
            if reconciled:
                for p, s in m.providers.items():
                    reconciled.providers[p] = s
                store.add_or_update(reconciled)
                matched_count += 1

        if not dry_run:
            store.save()
            logger.info("Saved %d updated mappings to master dataset", matched_count)
        else:
            logger.info("[Dry Run] Would update %d mappings", matched_count)
    finally:
        if tmdb_client._owns_http_client and hasattr(tmdb_client.http_client, "close"):
            await tmdb_client.http_client.close()

    return 0


def execute_export(args: argparse.Namespace) -> int:
    """Execute the export command."""
    source_dir = getattr(args, "source", None)
    target_dir = getattr(args, "target", None)
    dry_run = getattr(args, "dry_run", False)

    store = MasterMappingStore(storage_dir=source_dir)
    exporter = OrionExporter(output_dir=target_dir)

    if dry_run:
        mappings = store.all_mappings()
        logger.info(
            "[Dry Run] Export would process %d mappings (%d movies, %d series)",
            len(mappings),
            store.count("movie"),
            store.count("series"),
        )
    else:
        summary = exporter.export_store(store)
        logger.info(
            "Export completed in %.2fms: %d total files (%d IMDb, %d TMDB, %d Providers, %d bytes)",
            summary.duration_ms,
            summary.total_files,
            summary.imdb_count,
            summary.tmdb_count,
            summary.provider_count,
            summary.total_bytes,
        )
    return 0


async def execute_sync(args: argparse.Namespace) -> int:
    """Execute the end-to-end sync pipeline: scrape -> match -> master store -> export."""
    mappings_dir = getattr(args, "mappings_dir", None)
    target_dir = getattr(args, "target", None)
    tmdb_key = getattr(args, "tmdb_key", None)
    custom_rate = getattr(args, "rate_limit", None)
    fuzzy_thresh = getattr(args, "fuzzy_threshold", 88.0) or 88.0
    limit = getattr(args, "limit", None)
    max_pages = max(1, int(getattr(args, "max_pages", 1000) or 1000))
    head_pages = max(1, int(getattr(args, "head_pages", 5) or 5))
    dry_run = getattr(args, "dry_run", False)
    provider_arg = (getattr(args, "provider", None) or "all").strip().lower()

    if provider_arg == "all":
        provider_names = [
            name
            for name in get_registered_providers()
            if name not in DISABLED_AUTOMATIC_PROVIDERS
        ]
    else:
        provider_names = [provider_arg]

    content_types: list[ContentType]
    if getattr(args, "type", None):
        content_types = [ContentType(args.type)]
    else:
        content_types = [ContentType.MOVIE, ContentType.SERIES]

    rate_limiter = (
        TokenBucketLimiter(rate=custom_rate, capacity=int(max(1, custom_rate)))
        if custom_rate and custom_rate > 0
        else None
    )

    store = MasterMappingStore(storage_dir=mappings_dir)
    state_path = Path(getattr(args, "state_file", None) or (store.storage_dir.parent / "sync_state.json"))
    sync_state = _load_sync_state(state_path)
    tmdb_client = TmdbClient(api_key=tmdb_key, rate_limiter=rate_limiter)
    reconciler = IdentityReconciler(tmdb_client=tmdb_client, confidence_threshold=fuzzy_thresh)
    exporter = OrionExporter(output_dir=target_dir)

    all_scraped: list[ScrapedItem] = []

    try:
        # Phase 1: Incremental scrape catalogs, provider by provider.
        for prov_name in provider_names:
            try:
                scraper_limiter = (
                    TokenBucketLimiter(rate=custom_rate, capacity=int(max(1, custom_rate)))
                    if custom_rate and custom_rate > 0
                    else None
                )
                scraper = get_scraper(prov_name, rate_limiter=scraper_limiter)
            except Exception as exc:
                logger.warning("Could not initialize scraper for provider '%s': %s", prov_name, exc)
                continue

            for c_type in content_types:
                if c_type not in scraper.supported_types:
                    continue

                if limit is not None and limit <= 0:
                    continue

                known_slugs = {
                    mapping.providers.get(prov_name.strip().lower())
                    for mapping in store.all_mappings(c_type)
                    if prov_name.strip().lower() in mapping.providers
                }
                known_slugs.discard(None)

                items_for_prov: list[ScrapedItem] = []
                state_key = f"{prov_name.strip().lower()}:{c_type.value}"
                provider_state = sync_state.get(state_key, {})
                historical_cursor = max(1, int(provider_state.get("next_page", 1) or 1))
                effective_head_pages = min(head_pages, max_pages)
                if historical_cursor <= effective_head_pages:
                    historical_cursor = effective_head_pages + 1
                historical_pages = list(
                    range(
                        historical_cursor,
                        max_pages + 1,
                    )
                )
                pages_to_scan = list(range(1, effective_head_pages + 1)) + historical_pages
                pages_to_scan = list(dict.fromkeys(pages_to_scan))
                next_cursor = historical_cursor
                catalog_exhausted = False

                for page in pages_to_scan:
                    try:
                        page_items = await scraper.fetch_catalog(content_type=c_type, page=page)
                    except Exception as exc:
                        logger.warning(
                            "Error fetching catalog for %s %s page %d: %s",
                            prov_name,
                            c_type.value,
                            page,
                            exc,
                        )
                        break

                    if not page_items:
                        catalog_exhausted = True
                        break

                    for page_item in page_items:
                        if page_item.slug.strip().strip("/") in known_slugs:
                            continue
                        known_slugs.add(page_item.slug.strip().strip("/"))
                        items_for_prov.append(page_item)
                        if limit is not None and len(items_for_prov) >= limit:
                            next_cursor = page + 1
                            break

                    if limit is not None and len(items_for_prov) >= limit:
                        break

                    if page in historical_pages:
                        next_cursor = page + 1

                if catalog_exhausted or next_cursor > max_pages:
                    next_cursor = 1
                sync_state[state_key] = {"next_page": next_cursor}

                if page == max_pages and not catalog_exhausted:
                    logger.warning(
                        "Reached max-pages=%d for %s (%s); catalog scan was truncated",
                        max_pages,
                        prov_name,
                        c_type.value,
                    )

                if items_for_prov and isinstance(scraper, BaseScraper):
                    await _enrich_scraped_items(scraper, items_for_prov)

                all_scraped.extend(items_for_prov)
                logger.info(
                    "Sync scraped %d items from %s (%s)",
                    len(items_for_prov),
                    prov_name,
                    c_type.value,
                )

        # Phase 2: Match and Reconcile
        logger.info("Reconciling %d total scraped items against TMDB/IMDb", len(all_scraped))
        reconciled_mappings = await reconciler.reconcile_batch(all_scraped, master_store=store)

        for m in reconciled_mappings:
            store.add_or_update(m)

        # Phase 3: Persist Master Dataset & OrionServer Export
        if not dry_run:
            store.save()
            _save_sync_state(state_path, sync_state)
            summary = exporter.export_store(store)
            logger.info(
                "Sync completed successfully: %d mappings in store, exported %d index files in %.2fms",
                store.count(),
                summary.total_files,
                summary.duration_ms,
            )
        else:
            logger.info(
                "[Dry Run] Sync completed without writes. Reconciled %d mappings.",
                len(reconciled_mappings),
            )
    finally:
        if tmdb_client._owns_http_client and hasattr(tmdb_client.http_client, "close"):
            await tmdb_client.http_client.close()

    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Main CLI dispatcher."""
    parser = create_cli_parser()

    if argv is None:
        raw_args = sys.argv[1:]
    else:
        raw_args = list(argv)

    if not raw_args:
        parser.print_help()
        return 0

    args = parser.parse_args(raw_args)

    if not args.command:
        parser.print_help()
        return 0

    try:
        if args.command == "scrape":
            return asyncio.run(execute_scrape(args))
        elif args.command == "match":
            return asyncio.run(execute_match(args))
        elif args.command == "export":
            return execute_export(args)
        elif args.command == "sync":
            return asyncio.run(execute_sync(args))
        else:
            parser.print_help()
            return 1
    except Exception as exc:
        logger.exception("CLI execution failed: %s", exc)
        return 1


def app() -> None:
    """Console script entry point."""
    sys.exit(main())
