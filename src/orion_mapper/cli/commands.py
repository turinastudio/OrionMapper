"""OrionMapper CLI commands and dispatcher."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from orion_mapper.core.rate_limiter import TokenBucketLimiter
from orion_mapper.matcher.normalizer import TitleNormalizer
from orion_mapper.matcher.reconciler import IdentityReconciler
from orion_mapper.matcher.scoring import FuzzyTitleMatcher
from orion_mapper.models.item import ContentType, ScrapedDetail, ScrapedItem
from orion_mapper.models.mapping import CanonicalMapping
from orion_mapper.resolver.tmdb import TmdbClient
from orion_mapper.resolver.allcalidad_md5 import AllCalidadMd5Resolver, extract_md5
from orion_mapper.scrapers import BaseScraper, get_registered_providers, get_scraper
from orion_mapper.storage.master import MasterMappingStore, atomic_write_json
from orion_mapper.storage.orion_exporter import OrionExporter

logger = logging.getLogger("orion_mapper.cli")

# AllCalidad requires its MD5 identity table and Gnula is currently excluded
# from the operational sync. Keep both implementations available for
# explicit/manual runs, but exclude them from automatic ``all`` executions.
DISABLED_AUTOMATIC_PROVIDERS: set[str] = set()
UNRESOLVED_DIR = Path("data/unresolved")


def _load_sync_state(path: Path) -> dict[str, dict[str, int]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_sync_state(path: Path, state: dict[str, dict[str, int]]) -> None:
    atomic_write_json(path, state)


def _record_unresolved_records(
    provider: str,
    records_to_add: list[dict[str, object]],
    output_dir: Path = UNRESOLVED_DIR,
) -> None:
    """Persist all provider entries that were not incorporated."""
    if not records_to_add:
        return
    provider = provider.strip().lower()
    path = output_dir / f"{provider}.json"
    try:
        existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    except (json.JSONDecodeError, OSError):
        existing = []
    records = {
        (str(record.get("type", "")), str(record.get("slug", ""))): record
        for record in existing if isinstance(record, dict)
    }
    for record in records_to_add:
        records[(str(record.get("type", "")), str(record.get("slug", "")))] = record
    atomic_write_json(path, sorted(records.values(), key=lambda r: (str(r["type"]), str(r["slug"]))))


def _load_unresolved_slugs(
    provider: str,
    content_type: str,
    output_dir: Path = UNRESOLVED_DIR,
) -> set[str]:
    """Return slugs already recorded as pending for a provider/type.

    These are cases a previous run could not resolve; reprocessing them on
    every audit wastes provider and TMDB quota for the same outcome.
    """
    path = output_dir / f"{provider.strip().lower()}.json"
    try:
        existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    except (json.JSONDecodeError, OSError):
        return set()
    return {
        str(record.get("slug", "")).strip().strip("/")
        for record in existing
        if isinstance(record, dict) and str(record.get("type", "")) == content_type
    }


def _prune_unresolved_slugs(
    provider: str,
    slugs: list[str],
    content_type: str,
    output_dir: Path = UNRESOLVED_DIR,
) -> int:
    """Remove recovered slugs from the unresolved backlog. Returns count removed."""
    if not slugs:
        return 0
    provider = provider.strip().lower()
    path = output_dir / f"{provider}.json"
    try:
        existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    except (json.JSONDecodeError, OSError):
        return 0
    doomed = {s.strip().strip("/") for s in slugs}
    kept = [
        record for record in existing
        if not (
            isinstance(record, dict)
            and str(record.get("type", "")) == content_type
            and str(record.get("slug", "")).strip().strip("/") in doomed
        )
    ]
    removed = len(existing) - len(kept)
    if removed:
        atomic_write_json(path, sorted(kept, key=lambda r: (str(r["type"]), str(r["slug"]))))
    return removed


def _record_unresolved_items(items: list[ScrapedItem], output_dir: Path = UNRESOLVED_DIR) -> None:
    """Persist provider items that lack both canonical identifiers."""
    grouped: dict[str, list[ScrapedItem]] = {}
    for item in items:
        if item.imdb_id or item.tmdb_id:
            continue
        grouped.setdefault(item.provider.strip().lower(), []).append(item)

    for provider, provider_items in grouped.items():
        _record_unresolved_records(provider, [{
                "provider": item.provider,
                "slug": item.slug,
                "title": item.title,
                "year": item.year,
                "type": item.type.value,
                "reason": "missing_ids",
            } for item in provider_items], output_dir=output_dir)


def _resolve_allcalidad_md5(items: list[ScrapedItem]) -> int:
    """Fill missing TMDB IDs for AllCalidad items from poster image MD5s.

    AllCalidad embeds ``md5(str(tmdb_id))`` in image URLs; reversal is
    offline via the sorted index (see resolver/allcalidad_md5). Items
    that already carry a TMDB ID are untouched.
    """
    targets = [
        item for item in items
        if item.provider.strip().lower() == "allcalidad"
        and not item.tmdb_id
        and item.poster_url
    ]
    if not targets:
        return 0
    resolver = AllCalidadMd5Resolver()
    resolved = 0
    try:
        for item in targets:
            tmdb_id = resolver.resolve(extract_md5(item.poster_url))
            if tmdb_id:
                item.tmdb_id = tmdb_id
                resolved += 1
    finally:
        resolver.close()
    if resolved:
        logger.info("Resolved %d/%d AllCalidad TMDB IDs from image MD5", resolved, len(targets))
    else:
        logger.warning("No AllCalidad TMDB IDs resolved from image MD5 (%d candidates)", len(targets))
    return resolved


def _audit_slug_sets(
    catalog_slugs: list[str],
    mapped_slugs: set[str],
) -> dict[str, object]:
    """Compare catalog slugs with mappings without consulting TMDB."""
    normalized_catalog = [slug.strip().strip("/") for slug in catalog_slugs if slug.strip()]
    catalog_counts = Counter(normalized_catalog)
    unique_catalog = set(catalog_counts)
    return {
        "catalog_entries": len(normalized_catalog),
        "catalog_unique_slugs": len(unique_catalog),
        "catalog_duplicate_slugs": sum(count - 1 for count in catalog_counts.values() if count > 1),
        "mapped_slugs": len(mapped_slugs),
        "missing_slugs": sorted(unique_catalog - mapped_slugs),
        "stale_mapped_slugs": sorted(mapped_slugs - unique_catalog),
    }


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

    # 2. AUDIT
    audit_parser = subparsers.add_parser(
        "audit",
        help="Compare provider catalog slugs with stored mappings",
        description="Audits catalog coverage without fetching item details or querying TMDB",
    )
    audit_parser.add_argument(
        "--provider",
        "-p",
        type=str,
        default="all",
        help="Provider to audit: serieskao, poseidonhd2, or all (default: all)",
    )
    audit_parser.add_argument(
        "--type",
        "-t",
        type=str,
        choices=["movie", "series"],
        default=None,
        help="Content type filter: movie, series (default: both)",
    )
    audit_parser.add_argument(
        "--max-pages",
        type=int,
        default=1000,
        help="Maximum catalog pages to inspect per provider/type (default: 1000)",
    )
    audit_parser.add_argument(
        "--mappings-dir",
        "-m",
        type=str,
        default=None,
        help="Directory containing movies.json and series.json (default: data/mappings)",
    )
    audit_parser.add_argument(
        "--rate-limit",
        "-r",
        type=float,
        default=None,
        help="Override provider HTTP rate limit (req/s)",
    )
    audit_parser.add_argument(
        "--output",
        "-o",
        type=str,
        default="data/audit_report.json",
        help="Audit report path (default: data/audit_report.json)",
    )

    # 2. RECOVER AUDIT FINDINGS
    recover_parser = subparsers.add_parser(
        "recover-audit",
        help="Recover catalog entries missing from an audit",
        description="Fetches missing item details, validates their identity, and optionally adds only confirmed mappings",
    )
    recover_parser.add_argument(
        "--provider", "-p", type=str, default=None,
        help="Provider to recover (default: provider from the audit report)",
    )
    recover_parser.add_argument(
        "--type", "-t", type=str, choices=["movie", "series"], default=None,
        help="Content type to recover (default: type from the audit report)",
    )
    recover_parser.add_argument(
        "--report", "-i", type=str, default="data/audit_report.json",
        help="Audit report to read (default: data/audit_report.json)",
    )
    recover_parser.add_argument(
        "--output", "-o", type=str, default="data/audit_recovery.json",
        help="Recovery report path (default: data/audit_recovery.json)",
    )
    recover_parser.add_argument(
        "--limit", "-l", type=int, default=None,
        help="Maximum missing slugs to process",
    )
    recover_parser.add_argument(
        "--mappings-dir", "-m", type=str, default=None,
        help="Directory for master mappings (default: data/mappings)",
    )
    recover_parser.add_argument(
        "--tmdb-key", "-k", type=str, default=None,
        help="TMDB v3 API Key override",
    )
    recover_parser.add_argument(
        "--rate-limit", "-r", type=float, default=None,
        help="HTTP/TMDB rate limit (req/s)",
    )
    recover_parser.add_argument(
        "--fuzzy-threshold", "-f", type=float, default=88.0,
        help="Confidence threshold for TMDB matching (default: 88.0)",
    )
    recover_parser.add_argument(
        "--dry-run", action="store_true", default=False,
        help="Validate and report recoveries without modifying mappings",
    )
    recover_parser.add_argument(
        "--retry-pending", action="store_true", default=False,
        help="Reprocess slugs already recorded in data/unresolved (default: skip them to save quota)",
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
        help="Global safety limit for catalog pages per provider/type (default: 1000)",
    )
    sync_parser.add_argument(
        "--pages-per-run",
        type=int,
        default=50,
        help="Historical catalog pages to scan per provider/type in this run (default: 50)",
    )
    sync_parser.add_argument(
        "--head-pages",
        type=int,
        default=5,
        help="Newest catalog pages checked every run (default: 5)",
    )
    sync_parser.add_argument(
        "--history-overlap",
        type=int,
        default=5,
        help="Historical pages to overlap between runs (default: 5)",
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


async def execute_audit(args: argparse.Namespace) -> int:
    """Audit provider catalog coverage by comparing provider slugs."""
    provider_arg = (getattr(args, "provider", None) or "all").strip().lower()
    if provider_arg == "all":
        provider_names = [
            name
            for name in get_registered_providers()
            if name not in DISABLED_AUTOMATIC_PROVIDERS
        ]
    else:
        provider_names = [provider_arg]

    content_types = (
        [ContentType(args.type)]
        if getattr(args, "type", None)
        else [ContentType.MOVIE, ContentType.SERIES]
    )
    max_pages = max(1, int(getattr(args, "max_pages", 1000) or 1000))
    custom_rate = getattr(args, "rate_limit", None)
    mappings_dir = getattr(args, "mappings_dir", None)
    output_path = Path(getattr(args, "output", None) or "data/audit_report.json")
    store = MasterMappingStore(storage_dir=mappings_dir)

    mapped_by_provider_type: dict[tuple[str, str], set[str]] = {}
    for mapping in store.all_mappings():
        mapping_type = str(
            mapping.type.value if hasattr(mapping.type, "value") else mapping.type
        ).strip().lower()
        for provider, slugs in mapping.all_provider_slugs().items():
            key = (provider.strip().lower(), mapping_type)
            mapped_by_provider_type.setdefault(key, set()).update(
                slug.strip().strip("/") for slug in slugs
            )

    reports: list[dict[str, object]] = []
    for provider_name in provider_names:
        limiter = (
            TokenBucketLimiter(rate=custom_rate, capacity=int(max(1, custom_rate)))
            if custom_rate and custom_rate > 0
            else None
        )
        scraper = get_scraper(provider_name, rate_limiter=limiter)
        for content_type in content_types:
            if content_type not in scraper.supported_types:
                continue

            catalog_slugs: list[str] = []
            duplicate_slugs: set[str] = set()
            seen_slugs: set[str] = set()
            exhausted = False
            pages_scanned = 0
            logger.info(
                "Starting catalog audit for %s (%s), up to %d pages",
                provider_name,
                content_type.value,
                max_pages,
            )
            for page in range(1, max_pages + 1):
                pages_scanned = page
                if page == 1 or page % 10 == 0:
                    logger.info(
                        "Auditing %s (%s): catalog page %d/%d",
                        provider_name,
                        content_type.value,
                        page,
                        max_pages,
                    )
                try:
                    page_items = await scraper.fetch_catalog(
                        content_type=content_type,
                        page=page,
                    )
                except Exception as exc:
                    logger.warning(
                        "Audit failed fetching %s %s page %d: %s",
                        provider_name,
                        content_type.value,
                        page,
                        exc,
                    )
                    break

                if not page_items:
                    exhausted = True
                    break

                for item in page_items:
                    slug = item.slug.strip().strip("/")
                    if slug in seen_slugs:
                        duplicate_slugs.add(slug)
                    seen_slugs.add(slug)
                    catalog_slugs.append(slug)

            result = _audit_slug_sets(
                catalog_slugs,
                mapped_by_provider_type.get(
                    (provider_name.strip().lower(), content_type.value), set()
                ),
            )
            result["provider"] = provider_name
            result["type"] = content_type.value
            result["pages_scanned"] = pages_scanned
            result["catalog_exhausted"] = exhausted
            result["duplicate_slugs"] = sorted(duplicate_slugs)
            reports.append(result)
            logger.info("Catalog audit: %s", json.dumps(result, ensure_ascii=False, sort_keys=True))

    atomic_write_json(output_path, reports)
    logger.info("Audit report saved to %s", output_path)
    return 0


def _recovery_title_is_safe(source: ScrapedDetail, canonical: CanonicalMapping) -> bool:
    """Reject clearly unrelated direct-ID results while allowing translations."""
    source_title = TitleNormalizer.normalize(source.title)
    canonical_title = TitleNormalizer.normalize(canonical.title)
    if not source_title or not canonical_title:
        return False
    if source_title == canonical_title:
        return True
    overlap = FuzzyTitleMatcher.token_overlap(source_title, canonical_title)
    if overlap > 0:
        return True
    # A translated title can have no shared tokens; retain it only when the
    # release year corroborates the identity. Otherwise leave it for review.
    return bool(source.year and canonical.year and source.year == canonical.year)


async def execute_recover_audit(args: argparse.Namespace) -> int:
    """Recover missing catalog slugs from a persisted audit report."""
    report_path = Path(getattr(args, "report", None) or "data/audit_report.json")
    output_path = Path(getattr(args, "output", None) or "data/audit_recovery.json")
    try:
        report_text = await asyncio.to_thread(report_path.read_text, encoding="utf-8")
        raw_report = json.loads(report_text)
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        logger.error("Could not read audit report %s: %s", report_path, exc)
        return 1
    if not isinstance(raw_report, list):
        logger.error("Audit report must contain an array of reports")
        return 1

    provider_filter = (getattr(args, "provider", None) or "").strip().lower()
    type_filter = getattr(args, "type", None)
    reports = [
        report for report in raw_report
        if isinstance(report, dict)
        and (not provider_filter or str(report.get("provider", "")).lower() == provider_filter)
        and (not type_filter or report.get("type") == type_filter)
    ]
    if not reports:
        logger.error("No matching report found in %s", report_path)
        return 1

    custom_rate = getattr(args, "rate_limit", None)
    limiter = (
        TokenBucketLimiter(rate=custom_rate, capacity=int(max(1, custom_rate)))
        if custom_rate and custom_rate > 0 else None
    )
    store = MasterMappingStore(storage_dir=getattr(args, "mappings_dir", None))
    tmdb_client = TmdbClient(api_key=getattr(args, "tmdb_key", None), rate_limiter=limiter)
    reconciler = IdentityReconciler(
        tmdb_client=tmdb_client,
        confidence_threshold=getattr(args, "fuzzy_threshold", 88.0) or 88.0,
    )
    limit = getattr(args, "limit", None)
    dry_run = getattr(args, "dry_run", False)
    retry_pending = getattr(args, "retry_pending", False)
    recovery_reports: list[dict[str, object]] = []

    try:
        for report in reports:
            provider = str(report["provider"]).strip().lower()
            content_type = ContentType(str(report["type"]))
            slugs = [str(slug).strip().strip("/") for slug in report.get("missing_slugs", [])]
            if limit is not None:
                slugs = slugs[:max(0, limit)]
            # Skip slugs a previous run already failed on, unless forced.
            # They live in data/unresolved/<provider>.json with the same outcome.
            skipped: list[dict[str, object]] = []
            if not retry_pending:
                known_pending = _load_unresolved_slugs(provider, content_type.value)
                if known_pending:
                    kept = [s for s in slugs if s not in known_pending]
                    skipped = [{"slug": s, "reason": "already_pending"} for s in slugs if s in known_pending]
                    if skipped:
                        logger.info(
                            "Skipping %d already-pending %s/%s slugs (use --retry-pending to force)",
                            len(skipped), provider, content_type.value,
                        )
                    slugs = kept
            scraper = get_scraper(provider, rate_limiter=limiter)
            recovered: list[dict[str, object]] = []
            pending: list[dict[str, object]] = []
            unresolved_items: list[ScrapedItem] = []

            for index, slug in enumerate(slugs, 1):
                logger.info("Recovering %s/%s: %s", index, len(slugs), slug)
                detail = await scraper.fetch_detail(slug, content_type)
                if detail is None:
                    pending.append({"slug": slug, "reason": "detail_not_found"})
                    continue
                _resolve_allcalidad_md5([detail])
                if not detail.imdb_id and not detail.tmdb_id:
                    unresolved_items.append(detail)
                if detail.type != content_type:
                    pending.append({
                        "slug": slug,
                        "reason": "provider_type_mismatch",
                        "provider_type": detail.type.value,
                        "requested_type": content_type.value,
                        "title": detail.title,
                    })
                    continue

                # Resolve independently first. This prevents a candidate that
                # fails validation from mutating an existing store entry.
                mapping = await reconciler.reconcile_item(detail)
                if mapping is None:
                    pending.append({"slug": slug, "reason": "tmdb_unresolved", "title": detail.title})
                    continue
                if not _recovery_title_is_safe(detail, mapping):
                    pending.append({
                        "slug": slug,
                        "reason": "title_year_mismatch",
                        "provider_title": detail.title,
                        "provider_year": detail.year,
                        "resolved_title": mapping.title,
                        "resolved_year": mapping.year,
                        "imdb_id": mapping.imdb_id,
                        "tmdb_id": mapping.tmdb_id,
                    })
                    continue

                recovered.append({
                    "slug": slug,
                    "title": detail.title,
                    "requested_type": content_type.value,
                    "reclassified": mapping.type != content_type,
                    "imdb_id": mapping.imdb_id,
                    "tmdb_id": mapping.tmdb_id,
                    "mapping": mapping.model_dump(mode="json"),
                })
                if not dry_run:
                    store.add_or_update(mapping)

            recovery_reports.append({
                "provider": provider,
                "type": content_type.value,
                "requested": len(slugs) + len(skipped),
                "recovered": recovered,
                "pending": pending,
                "skipped": skipped,
                "dry_run": dry_run,
            })
            if not dry_run:
                _prune_unresolved_slugs(
                    provider,
                    [str(entry.get("slug", "")) for entry in recovered],
                    content_type.value,
                )
                _record_unresolved_items(unresolved_items)
                _record_unresolved_records(
                    provider,
                    [
                        {
                            "provider": provider,
                            "slug": str(entry.get("slug", "")),
                            "title": entry.get("title") or entry.get("provider_title"),
                            "year": entry.get("provider_year"),
                            "type": content_type.value,
                            "reason": entry.get("reason", "unresolved"),
                            **{
                                key: entry[key]
                                for key in ("imdb_id", "tmdb_id", "resolved_title", "resolved_year")
                                if key in entry
                            },
                        }
                        for entry in pending
                    ],
                )

        if not dry_run:
            store.save()
        atomic_write_json(output_path, recovery_reports)
        recovered_count = sum(len(r["recovered"]) for r in recovery_reports)
        pending_count = sum(len(r["pending"]) for r in recovery_reports)
        skipped_count = sum(len(r.get("skipped", [])) for r in recovery_reports)
        logger.info(
            "Audit recovery finished: %d recovered, %d pending, %d skipped (already pending); report saved to %s",
            recovered_count, pending_count, skipped_count, output_path,
        )
    finally:
        if tmdb_client._owns_http_client and hasattr(tmdb_client.http_client, "close"):
            await tmdb_client.http_client.close()

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
    pages_per_run = max(1, int(getattr(args, "pages_per_run", 50) or 50))
    head_pages = max(1, int(getattr(args, "head_pages", 5) or 5))
    history_overlap = max(0, int(getattr(args, "history_overlap", 5) or 0))
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
                historical_start = max(
                    effective_head_pages + 1,
                    historical_cursor - history_overlap,
                )
                historical_pages = list(
                    range(
                        historical_start,
                        min(historical_cursor + pages_per_run, max_pages + 1),
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
        _resolve_allcalidad_md5(all_scraped)
        reconciled_mappings = await reconciler.reconcile_batch(all_scraped, master_store=store)

        for m in reconciled_mappings:
            store.add_or_update(m)

        if not dry_run:
            _record_unresolved_items(all_scraped)

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
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
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
        elif args.command == "audit":
            return asyncio.run(execute_audit(args))
        elif args.command == "recover-audit":
            return asyncio.run(execute_recover_audit(args))
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
