# OrionMapper Test Infrastructure Architecture (TEST_INFRA.md)

## 1. Test Philosophy & Principles

OrionMapper uses a **requirement-driven, opaque-box testing framework** designed to validate end-to-end functionality, resilient HTTP execution, cross-provider identity reconciliation, and OrionServer contract compatibility strictly based on `ORIGINAL_REQUEST.md` and `PROJECT.md`.

### Core Principles
1. **Opaque-Box Verification**: Tests validate system behavior through external contracts, CLI commands, input feeds, and output JSON artifacts without coupling to internal private methods.
2. **Deterministic Offline Fixtures**: All network operations (SeriesKao, PoseidonHD2, Gnula, AllCalidad, and TMDB API) are reproducible without live internet connectivity via recorded mock payloads and deterministic response handlers.
3. **Progressive & Multi-Tier Architecture**: Test suites are structured into four rigorous tiers ranging from isolated feature verification to end-to-end multi-provider synchronization pipelines.
4. **Strict Contract Compatibility**: Generated files for OrionServer's `FileIdentityMappingStore` are validated against Kotlin kotlinx.serialization data models (`IdentityMapping`, `ImdbIdentityIndex`, `TmdbIdentityIndex`) and Base64 URL-safe unpadded key specs.
5. **Zero Flakiness & Total Isolation**: Every test creates its own ephemeral storage directory (via `tmp_path`), operates without inter-test dependencies, and properly manages async event loops.

---

## 2. Feature Inventory & Test Coverage Matrix

All 18 functional features from `PROJECT.md` are covered across the 4-Tier Test Architecture:

| # | Feature Name | Description | Primary Tier | Verification Test Suite |
|---|--------------|-------------|--------------|-------------------------|
| 1 | `BaseScraper` Abstract Contract | Async interface (`fetch_catalog`, `fetch_detail`, `crawl_catalog`) | Tier 1, Tier 2 | `tests/e2e/test_tier1_feature_coverage.py`<br>`tests/e2e/test_tier2_boundary_cases.py` |
| 2 | Pydantic v2 Data Models | `ScrapedItem`, `ScrapedDetail`, `CanonicalMapping`, Orion index models | Tier 1, Tier 2 | `tests/e2e/test_tier1_feature_coverage.py`<br>`tests/e2e/test_tier2_boundary_cases.py` |
| 3 | Resilient Async HTTP Stack | Async client pool, UA rotation, retry with backoff, timeouts | Tier 1, Tier 2 | `tests/e2e/test_tier1_feature_coverage.py`<br>`tests/e2e/test_tier2_boundary_cases.py` |
| 4 | Token Bucket Rate Limiter | Async rate limiting (40 req/s TMDB, configurable provider burst) | Tier 1, Tier 2 | `tests/e2e/test_tier1_feature_coverage.py`<br>`tests/e2e/test_tier2_boundary_cases.py` |
| 5 | SeriesKao Scraper | HTML / JSON-LD parser and `/vidurl/(tt\d{6,10})/` extraction | Tier 1, Tier 2, Tier 3 | `tests/e2e/test_tier1_feature_coverage.py`<br>`tests/e2e/test_tier3_cross_feature.py` |
| 6 | PoseidonHD2 Scraper | Next.js `__NEXT_DATA__` extractor for `TMDbId` & `IMDbId` | Tier 1, Tier 2, Tier 3 | `tests/e2e/test_tier1_feature_coverage.py`<br>`tests/e2e/test_tier3_cross_feature.py` |
| 7 | Gnula Scraper | Next.js `__NEXT_DATA__` extractor (`post.TMDbId` / `post.IMDbId`) | Tier 1, Tier 2, Tier 3 | `tests/e2e/test_tier1_feature_coverage.py`<br>`tests/e2e/test_tier3_cross_feature.py` |
| 8 | AllCalidad Scraper | REST API client (`/api/rest/listing`, `/api/rest/single`) | Tier 1, Tier 2, Tier 3 | `tests/e2e/test_tier1_feature_coverage.py`<br>`tests/e2e/test_tier3_cross_feature.py` |
| 9 | Scraper Registry & Factory | Dynamic provider scraper discovery and instantiation | Tier 1, Tier 2 | `tests/e2e/test_tier1_feature_coverage.py`<br>`tests/e2e/test_tier2_boundary_cases.py` |
| 10 | Direct Identifier Extraction Priority | Prioritize direct IMDb/TMDB extraction over search | Tier 1, Tier 2, Tier 3 | `tests/e2e/test_tier1_feature_coverage.py`<br>`tests/e2e/test_tier3_cross_feature.py` |
| 11 | Async TMDB API Client | `/3/find/{imdb_id}`, `/3/{type}/{id}/external_ids`, `/3/search` | Tier 1, Tier 2, Tier 3 | `tests/e2e/test_tier1_feature_coverage.py`<br>`tests/e2e/test_tier2_boundary_cases.py` |
| 12 | Title Normalizer & Spanish Handling | Strip Spanish diacritics, noise words, season ordinals | Tier 1, Tier 2, Tier 3 | `tests/e2e/test_tier1_feature_coverage.py`<br>`tests/e2e/test_tier2_boundary_cases.py` |
| 13 | Weighted Fuzzy Matcher | Token overlap + year delta + media type scoring (threshold >= 88) | Tier 1, Tier 2, Tier 3 | `tests/e2e/test_tier1_feature_coverage.py`<br>`tests/e2e/test_tier2_boundary_cases.py` |
| 14 | Identity Reconciler | Multi-provider item merging under canonical TMDB/IMDb entities | Tier 1, Tier 2, Tier 3 | `tests/e2e/test_tier1_feature_coverage.py`<br>`tests/e2e/test_tier3_cross_feature.py` |
| 15 | Master Dataset Storage (Fribb format) | Read/write `movies.json` / `series.json` with atomic writes | Tier 1, Tier 2, Tier 4 | `tests/e2e/test_tier1_feature_coverage.py`<br>`tests/e2e/test_tier4_real_world.py` |
| 16 | OrionServer Exporter Contract | `imdb/{id}.json`, `tmdb/{id}.json`, `providers/{base64url}.json` | Tier 1, Tier 2, Tier 4 | `tests/e2e/test_tier1_feature_coverage.py`<br>`tests/e2e/test_orion_store_compat.py` |
| 17 | Unified CLI Interface | Subcommands `scrape`, `match`, `export`, `sync` with all options | Tier 1, Tier 2, Tier 4 | `tests/e2e/test_e2e_cli.py`<br>`tests/e2e/test_e2e_sync.py` |
| 18 | GitHub Actions Sync Workflow | Workflow syntax, cron schedule, step order, and secrets | Tier 1, Tier 4 | `tests/e2e/test_tier1_feature_coverage.py`<br>`tests/e2e/test_tier4_real_world.py` |

---

## 3. 4-Tier Test Suite Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          Tier 4: Real-World Scenarios                       │
│  - Multi-provider end-to-end sync pipelines (`main.py sync`)                │
│  - CLI command flows (`scrape`, `match`, `export`, `--dry-run`, `--limit`)   │
│  - OrionServer Kotlin FileIdentityMappingStore byte-for-byte compatibility  │
│  - GitHub Actions Workflow execution simulation                             │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       │
┌──────────────────────────────────────▼──────────────────────────────────────┐
│                    Tier 3: Cross-Feature Combinations                       │
│  - Scraper -> Direct ID -> TMDB Find -> Reconciler -> Export                │
│  - Scraper -> Normalizer -> TMDB Search -> Fuzzy Match -> Master Store      │
│  - 4-Way Multi-Provider Aggregation (SeriesKao + Poseidon + Gnula + AllCal) │
│  - Incremental Master Dataset Update & Preservation of Existing Slugs       │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       │
┌──────────────────────────────────────▼──────────────────────────────────────┐
│                    Tier 2: Boundary & Corner Cases                          │
│  - Malformed HTML, missing __NEXT_DATA__, corrupt JSON-LD                   │
│  - Rate limit bursts, 429 retries with Retry-After, token starvation        │
│  - Punctuation & Spanish diacritic edge cases, year mismatch tolerances     │
│  - Base64 URL-safe unpadded edge strings, atomic write collision handling  │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       │
┌──────────────────────────────────────▼──────────────────────────────────────┐
│                      Tier 1: Feature Coverage                               │
│  - >=5 isolated happy path tests per feature (Features 1 to 18)             │
│  - Core models validation, HTTP client methods, scraper catalogs/details    │
│  - TMDB endpoints, normalizer rules, scoring logic, store read/write        │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Tier 1: Feature Coverage
- **Goal**: Verify happy paths for every feature in isolation.
- **Requirement**: >= 5 distinct test cases per feature (18 features * 5 = >= 90 test cases).
- **Execution File**: `tests/e2e/test_tier1_feature_coverage.py`.

### Tier 2: Boundary & Corner Cases
- **Goal**: Stress limits, edge conditions, invalid inputs, error handling, and recovery mechanisms.
- **Requirement**: >= 5 boundary cases per feature (18 features * 5 = >= 90 test cases).
- **Execution File**: `tests/e2e/test_tier2_boundary_cases.py`.

### Tier 3: Cross-Feature Combinations
- **Goal**: Verify interactions between scraping, resolving, matching, reconciling, and exporting.
- **Coverage**: Direct ID resolution pipelines, fallback search pipelines, 4-way multi-provider aggregation, incremental updating, and content-type segregation.
- **Execution File**: `tests/e2e/test_tier3_cross_feature.py`.

### Tier 4: Real-World Application Scenarios
- **Goal**: Full lifecycle simulations under realistic application conditions.
- **Coverage**: Full sync workflow (`main.py sync`), CLI arguments and execution modes, OrionServer `FileIdentityMappingStore` contract compliance, and CI workflow validation.
- **Execution Files**:
  - `tests/e2e/test_tier4_real_world.py`
  - `tests/e2e/test_e2e_cli.py`
  - `tests/e2e/test_e2e_sync.py`
  - `tests/e2e/test_orion_store_compat.py`

---

## 4. Test Runner Architecture

### Running the Test Suite
Tests can be run via pytest using the local virtual environment:

```bash
# Run all tests
.venv/bin/pytest -v

# Run specific tier
.venv/bin/pytest -v -m tier1
.venv/bin/pytest -v -m tier2
.venv/bin/pytest -v -m tier3
.venv/bin/pytest -v -m tier4

# Run specific integration suites
.venv/bin/pytest -v tests/e2e/test_e2e_cli.py
.venv/bin/pytest -v tests/e2e/test_e2e_sync.py
.venv/bin/pytest -v tests/e2e/test_orion_store_compat.py
```

### Pytest Configuration (`pyproject.toml`)
- `asyncio_mode = "auto"` enables seamless async test coroutine execution.
- `asyncio_default_fixture_loop_scope = "function"` guarantees event loop isolation.
- Custom markers (`tier1`, `tier2`, `tier3`, `tier4`, `e2e`, `orion_compat`, `cli`) allow targeted test execution.

---

## 5. Quality Gates & Coverage Thresholds

| Metric | Required Threshold | Enforcement Method |
|---|---|---|
| **Tier 1 Feature Coverage** | 100% pass (>=5 test cases per feature) | Automated pytest run |
| **Tier 2 Boundary Coverage** | 100% pass (>=5 test cases per feature) | Automated pytest run |
| **Tier 3 Combination Pipeline** | 100% pass | Automated pytest run |
| **Tier 4 Real-World & Orion Compat** | 100% pass | Automated pytest run & schema validation |
| **Test Flakiness** | 0% (zero flaky tests across 5 consecutive runs) | Deterministic mocks & isolated temp dirs |
| **OrionServer Format Compatibility** | Exact match with `FileIdentityMappingStore.kt` | JSON schema & Base64url unpadded verification |
