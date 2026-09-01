# Project: OrionMapper

## Architecture
OrionMapper is an automated, high-performance cross-provider identity mapper and scraper system for movies and series (SeriesKao, PoseidonHD2, Gnula, AllCalidad, and future providers) in Python 3.12+. It resolves provider slugs to canonical TMDB numeric IDs and IMDb `tt...` IDs, persisting data into both a centralized Git-tracked dataset (`Fribb/anime-lists` style) and decomposed fast-lookup index files for OrionServer (`FileIdentityMappingStore`).

### System Component Architecture
```
┌─────────────────────────────────────────────────────────────────────────────┐
│                             CLI Entry (main.py)                             │
│                    [scrape]   [match]   [export]   [sync]                   │
└────────┬──────────────────────┬──────────────────────┬──────────────────────┘
         │                      │                      │
┌────────▼──────────────┐┌──────▼──────────────┐┌──────▼──────────────────────┐
│   Provider Scrapers   ││ TMDB/IMDb Resolver  ││    Storage & Exporters      │
│  (src/scrapers/)      ││ (src/matcher/ &     ││    (src/storage/)           │
│  - BaseScraper        ││  src/resolver/)     ││ - MasterStore (Fribb format)│
│  - SeriesKao (HTML/LD)││ - Direct ID extract ││   (data/mappings/*.json)    │
│  - PoseidonHD2 (Next) ││ - TmdbClient (40r/s)││ - OrionExporter             │
│  - Gnula (Next.js)    ││ - TitleNormalizer   ││   (data/orion_mappings/     │
│  - AllCalidad (REST)  ││ - Fuzzy Scorer      ││    imdb/, tmdb/, providers/)│
│  - ScraperRegistry    ││ - Reconciler        ││                             │
└────────┬──────────────┘└──────┬──────────────┘└──────┬──────────────────────┘
         │                      │                      │
┌────────▼──────────────────────▼──────────────────────▼──────────────────────┐
│                    Core Engine & Cross-Cutting Concerns                     │
│  - Async HTTP Client (httpx with connection pooling, user-agent rotation)   │
│  - Token Bucket Rate Limiting (40 req/s for TMDB, 5 req/s per provider)     │
│  - Jittered Exponential Backoff & Retry Handling                            │
│  - Pydantic v2 Models (ScrapedItem, ScrapedDetail, CanonicalMapping, etc.)  │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Feature Inventory
| # | Feature | Description | Milestone | Source |
|---|---------|-------------|-----------|--------|
| 1 | `BaseScraper` Abstract Contract | Async interface (`fetch_catalog`, `fetch_detail`, `crawl_catalog`) with generic typing | M1 | ORIGINAL_REQUEST §R1.1, Survey |
| 2 | Pydantic v2 Data Models | `ScrapedItem`, `ScrapedDetail`, `CanonicalMapping`, `IdentityMappingExport`, `ImdbIdentityIndexExport`, `TmdbIdentityIndexExport` | M1 | ORIGINAL_REQUEST §R1.1, Survey |
| 3 | Resilient Async HTTP Stack | `httpx.AsyncClient` with connection pooling, user-agent rotation, jittered exponential backoff retries | M1 | ORIGINAL_REQUEST §R1, Survey |
| 4 | Token Bucket Rate Limiter | Async token bucket limiter enforcing max 40 req/s for TMDB and configurable provider rates | M1 | ORIGINAL_REQUEST §R2.2, Survey |
| 5 | SeriesKao Scraper | HTML / JSON-LD parser and `/vidurl/(tt\d{6,10})/` player regex extractor | M2 | ORIGINAL_REQUEST §R1.2, Survey |
| 6 | PoseidonHD2 Scraper | Next.js `__NEXT_DATA__` extractor for embedded `TMDbId` and `IMDbId` from catalog & detail | M2 | ORIGINAL_REQUEST §R1.2, Survey |
| 7 | Gnula Scraper | Next.js `__NEXT_DATA__` extractor for `post.TMDbId` / `post.IMDbId` with slug probe fallback | M2 | ORIGINAL_REQUEST §R1.2, Survey |
| 8 | AllCalidad Scraper | Fast REST API client (`/api/rest/listing`, `/api/rest/single`, `/api/rest/search`) | M2 | ORIGINAL_REQUEST §R1.2, Survey |
| 9 | Scraper Registry & Factory | Dynamic provider scraper discovery and instantiation | M2 | ORIGINAL_REQUEST §R1.3, Survey |
| 10 | Direct Identifier Extraction Priority | Prioritize direct IMDb/TMDB extraction over search to minimize external API calls | M3 | ORIGINAL_REQUEST §R2.1, Survey |
| 11 | Async TMDB API Client | TMDB client with `/3/find/{imdb_id}`, `/3/{type}/{id}/external_ids`, `/3/search/{type}` and fallback key `34fafb223263c2461f8f88a3489cb92e` | M3 | ORIGINAL_REQUEST §R2.2, Survey |
| 12 | Title Normalizer & Spanish Handling | Strip Spanish diacritics, catalog noise words, season ordinals, and noise punctuation | M3 | ORIGINAL_REQUEST §R2.2, TitleMatcher.kt |
| 13 | Weighted Fuzzy Matcher | Token overlap * 70 + Year delta bonus/penalty + Media type bonus/penalty with score >= 88 | M3 | ORIGINAL_REQUEST §R2.2, TitleMatcher.kt |
| 14 | Identity Reconciler | Multi-provider item merging under canonical TMDB/IMDb entities | M3 | ORIGINAL_REQUEST §R2.3, Survey |
| 15 | Master Dataset Storage (Fribb format) | Read/write `data/mappings/movies.json` and `data/mappings/series.json` with atomic writes & sorted keys | M4 | ORIGINAL_REQUEST §R3.1, Survey |
| 16 | OrionServer FileIdentityMappingStore Exporter | Export `imdb/{imdb_id}.json`, `tmdb/{tmdb_id}.json`, and unpadded Base64 URL-safe `providers/{base64url}.json` | M4 | ORIGINAL_REQUEST §R3.2, FileIdentityMappingStore.kt |
| 17 | Unified CLI Interface | Subcommands `scrape`, `match`, `export`, `sync` with options (`--provider`, `--limit`, `--unmapped-only`, `--dry-run`, `--target`, `--tmdb-key`) | M5 | ORIGINAL_REQUEST §R4.1, Survey |
| 18 | GitHub Actions Sync Workflow | `.github/workflows/sync-mappings.yml` with daily cron (`0 6 * * *`), manual dispatch, test runner, sync runner, and Git auto-commit | M5 | ORIGINAL_REQUEST §R4.2, Survey |
| 19 | Opaque-Box E2E Testing Suite | Multi-tier test suite (Tiers 1-4) verifying end-to-end functionality independently | M6 | ORIGINAL_REQUEST Acceptance Criteria |
| 20 | Adversarial Coverage Hardening | White-box stress testing, boundary condition verification, and gap coverage (Tier 5) | M6 | Orchestrator Protocol |

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| M1 | Core Architecture & Base Scraper | Pydantic models, Async HTTP client pool, Rate limiter, `BaseScraper` contract, project dependencies | none | DONE |
| M2 | Provider Scrapers | SeriesKao, PoseidonHD2, Gnula, AllCalidad scrapers, offline fixtures, provider registry | M1 | DONE |
| M3 | TMDB Resolver & Identity Matcher | TMDB API client, TitleNormalizer, Fuzzy scoring, Multi-provider Reconciler | M1 | DONE |
| M4 | Storage & OrionServer Exporter | Fribb-style Master dataset manager, OrionServer `FileIdentityMappingStore` exporter (Base64 URL unpadded) | M1 | DONE |
| M5 | CLI Interface & GitHub Actions | `main.py` CLI (`scrape`, `match`, `export`, `sync`), `.github/workflows/sync-mappings.yml` | M2, M3, M4 | DONE |
| M6 | E2E Testing & Hardening | Pass 100% E2E test suite (Tiers 1-4) and complete Tier 5 adversarial hardening | M5 | DONE |

## Interface Contracts

### 1. `BaseScraper` Interface (`src/orion_mapper/scrapers/base.py`)
```python
class BaseScraper(ABC):
    name: str
    base_url: str
    supported_types: list[ContentType] = ["movie", "series"]
    page_size: int = 24

    def __init__(self, http_client: AsyncHttpClient): ...
    async def fetch_catalog(self, content_type: ContentType, page: int = 1, genre: str | None = None) -> list[ScrapedItem]: ...
    async def fetch_detail(self, slug: str, content_type: ContentType) -> ScrapedDetail | None: ...
    async def crawl_catalog(self, content_type: ContentType, max_pages: int | None = None, genre: str | None = None) -> AsyncIterator[ScrapedItem]: ...
```

### 2. TMDB Client Contract (`src/orion_mapper/resolver/tmdb.py`)
```python
class TmdbClient:
    def __init__(self, api_key: str | None = None, rate_limiter: TokenBucketLimiter | None = None): ...
    async def find_by_imdb_id(self, imdb_id: str) -> dict[str, Any] | None: ...
    async def get_external_ids(self, tmdb_id: str | int, media_type: Literal["movie", "tv"]) -> dict[str, Any] | None: ...
    async def search(self, title: str, media_type: Literal["movie", "tv"], year: int | None = None, language: str = "es-MX") -> list[dict[str, Any]]: ...
```

### 3. Reconciler Contract (`src/orion_mapper/matcher/reconciler.py`)
```python
class IdentityReconciler:
    def __init__(self, tmdb_client: TmdbClient, matcher: IdentityMatcher): ...
    async def reconcile_item(self, item: ScrapedItem | ScrapedDetail, master_store: MasterMappingStore) -> CanonicalMapping | None: ...
    async def reconcile_batch(self, items: list[ScrapedItem | ScrapedDetail], master_store: MasterMappingStore) -> list[CanonicalMapping]: ...
```

### 4. OrionServer Exporter Contract (`src/orion_mapper/storage/orion_exporter.py`)
```python
class OrionExporter:
    def __init__(self, output_dir: Path): ...
    def export_mappings(self, mappings: list[CanonicalMapping]) -> ExportSummary: ...
    @staticmethod
    def encode_provider_key(provider: str, slug: str) -> str:
        # Returns urlsafe_b64encode(f"{provider.lower()}:{slug}".encode("utf-8")).decode("ascii").rstrip("=")
        ...
```

## Code Layout
```
/home/lautaroturina/Projects/Orion/OrionMapper/
├── pyproject.toml
├── requirements.txt
├── README.md
├── main.py
├── src/
│   └── orion_mapper/
│       ├── __init__.py
│       ├── core/
│       │   ├── __init__.py
│       │   ├── config.py
│       │   ├── http.py
│       │   └── rate_limiter.py
│       ├── models/
│       │   ├── __init__.py
│       │   ├── item.py
│       │   ├── mapping.py
│       │   └── orion.py
│       ├── scrapers/
│       │   ├── __init__.py
│       │   ├── base.py
│       │   ├── serieskao.py
│       │   ├── poseidonhd2.py
│       │   ├── gnula.py
│       │   └── allcalidad.py
│       ├── resolver/
│       │   ├── __init__.py
│       │   └── tmdb.py
│       ├── matcher/
│       │   ├── __init__.py
│       │   ├── normalizer.py
│       │   ├── scoring.py
│       │   └── reconciler.py
│       ├── storage/
│       │   ├── __init__.py
│       │   ├── master.py
│       │   └── orion_exporter.py
│       └── cli/
│           ├── __init__.py
│           └── commands.py
├── tests/
│   ├── conftest.py
│   ├── fixtures/
│   │   ├── serieskao/
│   │   ├── poseidonhd2/
│   │   ├── gnula/
│   │   └── allcalidad/
│   ├── unit/
│   │   ├── test_models.py
│   │   ├── test_http.py
│   │   ├── test_scrapers.py
│   │   ├── test_tmdb_resolver.py
│   │   ├── test_matcher.py
│   │   ├── test_reconciler.py
│   │   └── test_storage_exporter.py
│   └── e2e/
│       ├── test_e2e_cli.py
│       ├── test_e2e_sync.py
│       └── test_orion_store_compat.py
├── data/
│   ├── mappings/
│   │   ├── movies.json
│   │   └── series.json
│   └── orion_mappings/
│       ├── imdb/
│       ├── tmdb/
│       └── providers/
└── .github/
    └── workflows/
        └── sync-mappings.yml
```
