# Original User Request

## 2026-09-01T13:39:58Z

An automated high-performance cross-provider identity mapper and scraper system for movies and series (SeriesKao, PoseidonHD2, Gnula, AllCalidad, and future providers) that maps provider slugs to TMDB and IMDb IDs using a centralized Git-tracked mapping dataset (inspired by `Fribb/anime-lists`), integrated with OrionServer and automated via GitHub Actions.

Working directory: /home/lautaroturina/Projects/Orion/OrionMapper
Integrity mode: development

## Reference Material
- Architecture specification: `/home/lautaroturina/Downloads/indice peliculas y series slugs tmdb id`
- OrionServer Identity Store: `/home/lautaroturina/Projects/Orion/OrionServer/core/src/jvmMain/kotlin/org/orion/core/identity/FileIdentityMappingStore.kt`
- OrionServer Identity Model: `/home/lautaroturina/Projects/Orion/OrionServer/core/src/commonMain/kotlin/org/orion/core/identity/IdentityMapping.kt`
- Dataset design reference: [Fribb/anime-lists](https://github.com/Fribb/anime-lists)

## Requirements

### R1. Modular Async Scraper Architecture (Python 3.12+)
1. Implement a clean, extensible base `BaseScraper` class with async interfaces (`fetch_catalog`, `fetch_detail`, `extract_identifiers`) and Pydantic models for validated data (`ScrapedItem`, `ScrapedDetail`).
2. Implement scrapers for initial providers:
   - **SeriesKao**: HTML/LD-JSON parser and player URL identifier extractor (`/vidurl/tt...` IMDb regex extraction).
   - **PoseidonHD2**: Next.js `__NEXT_DATA__` JSON extractor (extracting embedded `TMDbId` / `IMDbId` directly from page props).
   - **Gnula**: Next.js `__NEXT_DATA__` and catalog/genre crawler with embedded identifier extraction.
   - **AllCalidad**: Fast REST API JSON client (`/api/rest` or WP REST API endpoints).
3. The scraper architecture must make creating new provider scrapers as simple as implementing `BaseScraper` in `scrapers/{provider_name}.py`.

### R2. TMDB/IMDb Resolver & Identity Matcher
1. **Direct Identifier Extraction**: Prioritize explicit IMDb (`tt...`) and TMDB numeric IDs already present in provider HTML / `__NEXT_DATA__` payloads to minimize external API calls.
2. **TMDB API Integration**:
   - Use `TMDB_API_KEY` (configured via `.env` or environment variable, with default fallback `34fafb223263c2461f8f88a3489cb92e`) with async rate-limiting (e.g. max 40 req/s with token bucket / semaphore).
   - Use TMDB Find endpoint (`/3/find/{imdb_id}?external_source=imdb_id`) for instant IMDb <-> TMDB ID conversion.
   - Use TMDB External IDs endpoint (`/3/movie/{tmdb_id}/external_ids`, `/3/tv/{tmdb_id}/external_ids`) for TMDB -> IMDb resolution.
   - Use TMDB Search (`/3/search/movie`, `/3/search/tv`) with title, year, and fuzzy scoring for unmapped items.
3. **Identity Reconciliation**: Merge multi-provider representations of the same movie or series into a single canonical entry.

### R3. Data Storage & OrionServer Export
1. **Master Mapping Files (`Fribb/anime-lists` style)**:
   - `data/mappings/movies.json` and `data/mappings/series.json` (or consolidated `mappings.json`) containing entries structured as:
     ```json
     {
       "tmdb_id": "21048",
       "imdb_id": "tt15486",
       "title": "Zombieland Saga",
       "type": "series",
       "year": 2018,
       "providers": {
         "serieskao": "zombieland-saga",
         "poseidonhd2": "zombieland-saga",
         "gnula": "pelicula-zombieland-saga",
         "allcalidad": "zombieland-saga"
       },
       "updated_at": 1787140795482
     }
     ```
2. **OrionServer FileIdentityMappingStore Exporter**:
   Generate output files matching OrionServer's `FileIdentityMappingStore` contract:
   - `data/orion_mappings/imdb/{imdb_id}.json` (`ImdbIdentityIndex`)
   - `data/orion_mappings/tmdb/{tmdb_id}.json` (`TmdbIdentityIndex`)
   - `data/orion_mappings/providers/{base64(provider:slug)}.json` (`IdentityMapping`)

### R4. CLI & GitHub Actions Automation
1. **CLI Commands**:
   - `python main.py scrape --provider [all|<name>] [--limit N]`
   - `python main.py match [--unmapped-only]`
   - `python main.py export --target /path/to/OrionServer/data/mappings`
   - `python main.py sync` (orchestrates full scrape -> match -> export flow)
2. **GitHub Actions Workflow** (`.github/workflows/sync-mappings.yml`):
   - Scheduled cron job (e.g. daily) + manual dispatch.
   - Runs scraping, resolves new entries via TMDB, updates Git-tracked mapping files, and commits changes.

## Acceptance Criteria

### Scrapers & Architecture
- [ ] Working asynchronous scrapers for SeriesKao, PoseidonHD2, Gnula, and AllCalidad.
- [ ] `BaseScraper` class enables adding new scrapers by implementing a standard interface.
- [ ] Resilient network handling with timeouts, retries, and rate limits.

### TMDB Resolution
- [ ] Converts IMDb IDs to TMDB IDs via `/3/find/{imdb_id}` without redundant scraping.
- [ ] Fallback fuzzy matching correctly associates title + year with TMDB entries.
- [ ] TMDB API calls respect rate limits and handle missing/invalid keys gracefully.

### Output Compatibility
- [ ] Master `movies.json` / `series.json` generated adhering to the Fribb/anime-lists specification.
- [ ] Exporter produces valid JSONs for OrionServer `FileIdentityMappingStore` (IMDb index, TMDB index, and Base64 provider keys).

### Testing & Quality
- [ ] Offline test suite with HTML/JSON fixtures for all 4 providers.
- [ ] Matching logic unit tests verifying title normalization, year tolerance, and ID conversion.
- [ ] CLI runs end-to-end locally with `--dry-run` or `--limit`.
- [ ] Workflow file `.github/workflows/sync-mappings.yml` ready for deployment.
