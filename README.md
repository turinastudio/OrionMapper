# OrionMapper

High-performance, automated cross-provider identity mapper and scraper system for movies and TV series (SeriesKao, PoseidonHD2, Gnula, AllCalidad, and extensible to future providers) written in Python 3.11+.

OrionMapper discovers, parses, normalizes, and reconciles media items from diverse streaming providers to canonical TMDB numeric IDs and IMDb `tt...` identifiers. It maintains both a centralized master dataset (Fribb/anime-lists style) and exports indexed mapping stores directly compatible with [OrionServer](https://github.com/turinastudio/OrionServer) (`FileIdentityMappingStore`).

---

## Features

- **Multi-Provider Scraper Engine**:
  - **SeriesKao**: HTML parser, JSON-LD extraction, and embedded player regex identifier extraction (`/vidurl/(tt\d{6,10})/`).
  - **PoseidonHD2**: Next.js `__NEXT_DATA__` JSON hydration parser extracting embedded `TMDbId` and `IMDbId`.
  - **Gnula**: Next.js `__NEXT_DATA__` parser with fallback slug probing.
  - **AllCalidad**: REST API client querying listing, single item, and search endpoints.
  - Dynamic **ScraperRegistry** supporting automated provider discovery and pagination crawling.

- **Intelligent Identity Resolution & Matching**:
  - **Direct ID Priority**: Extracted IMDb/TMDB identifiers are verified against TMDB external lookup APIs before attempting search.
  - **Spanish Title Normalization**: Strips diacritics, catalog noise words (e.g., *Castellano*, *Latino*, *Subtitulado*), season ordinals, and punctuation.
  - **Weighted Fuzzy Matcher**: Token overlap scoring, release year delta penalties/bonuses, and content-type alignment.
  - **Multi-Provider Reconciliation**: Consolidates provider slugs and IDs into unified canonical entity records.

- **Storage & OrionServer Exporters**:
  - **Master Dataset Store**: Atomic JSON storage for `data/mappings/movies.json` and `data/mappings/series.json`.
  - **OrionServer Exporter**: Generates indexed lookup files conforming to `FileIdentityMappingStore`:
    - `imdb/{imdb_id}.json`
    - `tmdb/{tmdb_id}.json`
    - `providers/{base64url_unpadded}.json` (e.g. Base64 URL-safe encoded `serieskao:movie:slug`)

- **Resilient Asynchronous HTTP Stack**:
  - Built on `httpx` with HTTP/2 support, connection pooling, and user-agent rotation.
  - Async token bucket rate limiter (40 req/s for TMDB, configurable provider limits).
  - Jittered exponential backoff and retry handling.

- **Automated CI/CD**:
  - Scheduled daily synchronization workflow via GitHub Actions (`.github/workflows/sync-mappings.yml`).

---

## Architecture Overview

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
│  - SeriesKao          ││ - Direct ID extract ││   (data/mappings/*.json)    │
│  - PoseidonHD2        ││ - TmdbClient        ││ - OrionExporter             │
│  - Gnula              ││ - TitleNormalizer   ││   (data/orion_mappings/     │
│  - AllCalidad         ││ - Fuzzy Scorer      ││    imdb/, tmdb/, providers/)│
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

---

## Project Structure

```
OrionMapper/
├── .github/
│   └── workflows/
│       └── sync-mappings.yml    # GitHub Actions daily sync workflow
├── data/
│   ├── mappings/
│   │   ├── movies.json          # Master canonical movie mappings (Git-tracked)
│   │   └── series.json          # Master canonical series mappings (Git-tracked)
│   └── orion_mappings/          # Ephemeral exported Orion lookup indices
│       ├── imdb/
│       ├── tmdb/
│       └── providers/
├── src/
│   └── orion_mapper/
│       ├── cli/                 # Typer & Rich CLI subcommands
│       ├── core/                # Config, HTTP client, rate limiter, exceptions
│       ├── matcher/             # Normalizer, fuzzy matcher, reconciler
│       ├── models/              # Pydantic v2 domain schemas
│       ├── resolver/            # TMDB API client & direct extractor
│       ├── scrapers/            # Provider scraper implementations & registry
│       └── storage/             # Master JSON store & Orion exporter
├── tests/
│   ├── adversarial/             # Adversarial edge cases and stress tests
│   ├── e2e/                     # End-to-end integration tests
│   ├── fixtures/                # Mock HTML, JSON-LD, and Next.js payloads
│   └── unit/                    # Isolated unit test suites
├── main.py                      # CLI entry point
├── pyproject.toml               # Build configuration, deps, and tool settings
└── requirements.txt             # Project dependencies
```

---

## Installation

### Prerequisites
- Python 3.11+
- [uv](https://github.com/astral-sh/uv) (recommended) or `pip` / `venv`

### Setup

```bash
# Clone the repository
git clone https://github.com/turinastudio/OrionMapper.git
cd OrionMapper

# Create virtual environment
python -m venv .venv
source .venv/bin/activate

# Install dependencies in editable mode with development packages
pip install -e ".[dev]"
```

---

## Configuration

OrionMapper can be configured via environment variables or a `.env` file in the project root:

| Variable | Default | Description |
|---|---|---|
| `ORION_TMDB_API_KEY` | *(built-in fallback)* | TMDB v3 API Key |
| `ORION_DATA_DIR` | `./data/mappings` | Path to master dataset directory |
| `ORION_EXPORT_DIR` | `./data/orion_mappings` | Path for OrionServer index exports |
| `ORION_HTTP_TIMEOUT` | `15.0` | Timeout (in seconds) for HTTP requests |
| `ORION_LOG_LEVEL` | `INFO` | Logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |

---

## Usage

You can run OrionMapper via `python main.py` or the installed console script `orion-mapper`.

### 1. Scrape Catalog & Details
Scrape catalogs from one or all providers:
```bash
# Scrape movies from a specific provider
python main.py scrape --provider serieskao --type movie --limit 50

# Scrape all providers
python main.py scrape --type both
```

### 2. Match & Reconcile Identities
Match scraped items against TMDB/IMDb and update the master datasets:
```bash
# Match unmapped items and update data/mappings/*.json
python main.py match --unmapped-only

# Match with dry-run mode (no disk writes)
python main.py match --dry-run
```

### 3. Export for OrionServer
Generate decomposed lookup files for OrionServer:
```bash
# Export master datasets to data/orion_mappings/
python main.py export --target data/orion_mappings
```

### 4. Full Sync Pipeline
Execute scrape, match, and export sequentially in one command:
```bash
python main.py sync
```

---

## Testing

Run the comprehensive pytest test suite across all tiers:

```bash
# Run all tests
pytest

# Run tests with coverage reporting
pytest --cov=src/orion_mapper --cov-report=term-missing

# Run specific tiers
pytest -m tier1   # Feature Coverage tests
pytest -m tier2   # Boundary & Corner Case tests
pytest -m tier3   # Cross-Feature tests
pytest -m e2e     # End-to-end integration tests
```

---

## License

This project is licensed under the [MIT License](LICENSE).
