"""Tier 5 Adversarial Coverage Hardening Test Suite.

Comprehensive stress-testing and boundary verification across all OrionMapper subsystems:
1. Core HTTP: AsyncHttpClient retry exhaustion, connection bursts, rate limiting, and h2 fallback.
2. Scrapers: BaseScraper invariants, registry overrides/aliases, error payload envelopes, Next.js hydration parsing,
   JSON-LD schemas, and regex fallback extractors for AllCalidad, Gnula, PoseidonHD2, and SeriesKao.
3. TMDB Resolver: 404/500/network error resilience, IMDb lookup, external ID resolution, search parameters,
   and lifecycle cleanup.
4. Normalizer & Scorer: Season regex branches (ordinal words, ordinal numbers, short formats),
   diacritics stripping, noise reduction, sub-token fuzzy overlaps, and year delta weighting.
5. Reconciler: TMDB external ID exceptions, TV media type resolution, field coalescence on existing records,
   and multi-item transitive graph reconciliation.
6. Master Store & Orion Exporter: In-memory unindexing, corruption tolerance on load(), comparator sort keys,
   Base64 URL-safe key parity, and schema compliance.
7. CLI & Entry Point: Limit boundaries, unsupported content type filtering, help/dispatch branching,
   and main.py entrypoint execution.
"""

from __future__ import annotations

import argparse
import asyncio
import builtins
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from orion_mapper.cli.commands import (
    app,
    execute_scrape,
    execute_sync,
    main,
)
from orion_mapper.core.config import Settings
from orion_mapper.core.http import AsyncHttpClient, MaxRetriesExceededError
from orion_mapper.core.rate_limiter import TokenBucketLimiter
from orion_mapper.matcher.normalizer import TitleNormalizer
from orion_mapper.matcher.reconciler import IdentityReconciler
from orion_mapper.matcher.scoring import CandidateScorer, FuzzyTitleMatcher
from orion_mapper.models.item import ContentType, ScrapedDetail, ScrapedItem
from orion_mapper.models.mapping import CanonicalMapping
from orion_mapper.models.orion import (
    IdentityMappingExport,
    ImdbIdentityIndexExport,
    TmdbIdentityIndexExport,
    decode_provider_key,
    encode_provider_key,
)
from orion_mapper.resolver.tmdb import TmdbClient
from orion_mapper.scrapers import (
    AllCalidadScraper,
    BaseScraper,
    GnulaScraper,
    PoseidonHD2Scraper,
    SeriesKaoScraper,
    get_registered_providers,
    get_scraper,
    list_scrapers,
    register_scraper,
    reset_registry,
)
from orion_mapper.scrapers.allcalidad import _is_error_payload
from orion_mapper.scrapers.gnula import _extract_next_data as gnula_extract_next_data
from orion_mapper.scrapers.poseidonhd2 import _extract_next_data as poseidon_extract_next_data
from orion_mapper.storage.master import MasterMappingStore
from orion_mapper.storage.orion_exporter import OrionExporter

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


# ==============================================================================
# 1. CORE HTTP SUBSYSTEM ADVERSARIAL HARDENING
# ==============================================================================
class TestCoreHttpAdversarialHardening:
    """Stress tests and edge case coverage for AsyncHttpClient and RateLimiter."""

    def test_http_client_initialization_without_h2(self):
        """AsyncHttpClient must fall back gracefully to HTTP/1.1 if h2 module is not imported."""
        orig_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "h2":
                raise ImportError("No module named 'h2'")
            return orig_import(name, *args, **kwargs)

        cfg = Settings(http_timeout=5.0)
        with patch("builtins.__import__", side_effect=mock_import):
            client = AsyncHttpClient(config=cfg)
            assert client is not None
            assert hasattr(client, "_client")

    @pytest.mark.asyncio
    async def test_http_request_max_retries_exceeded_on_retryable_status(self):
        """AsyncHttpClient must raise MaxRetriesExceededError when retryable status codes persist."""
        cfg = Settings(http_max_retries=2, http_backoff_factor=0.01, http_backoff_max=0.05)
        client = AsyncHttpClient(config=cfg)

        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 503
        mock_resp.headers = {}

        with patch.object(client._client, "request", new=AsyncMock(return_value=mock_resp)):
            with pytest.raises(MaxRetriesExceededError) as exc_info:
                await client.request("GET", "https://api.example.com/status503")
            assert "HTTP 503 for GET https://api.example.com/status503 after 3 attempts" in str(exc_info.value)
        await client.close()

    @pytest.mark.asyncio
    async def test_http_request_retry_after_header_parsing(self):
        """AsyncHttpClient must parse numeric Retry-After headers and handle non-numeric fallback."""
        cfg = Settings(http_max_retries=1, http_backoff_factor=0.01)
        client = AsyncHttpClient(config=cfg)

        # 1. Numeric Retry-After
        resp_numeric = MagicMock(spec=httpx.Response)
        resp_numeric.status_code = 429
        resp_numeric.headers = {"Retry-After": "1"}

        resp_ok = MagicMock(spec=httpx.Response)
        resp_ok.status_code = 200
        resp_ok.raise_for_status = MagicMock()

        with patch.object(client._client, "request", new=AsyncMock(side_effect=[resp_numeric, resp_ok])):
            with patch("asyncio.sleep", new=AsyncMock()) as mock_sleep:
                res = await client.request("GET", "https://api.example.com/rate-limited")
                assert res.status_code == 200
                mock_sleep.assert_called_once_with(1.0)

        # 2. Non-numeric Retry-After fallback
        resp_non_numeric = MagicMock(spec=httpx.Response)
        resp_non_numeric.status_code = 429
        resp_non_numeric.headers = {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}

        with patch.object(client._client, "request", new=AsyncMock(side_effect=[resp_non_numeric, resp_ok])):
            with patch("asyncio.sleep", new=AsyncMock()) as mock_sleep:
                res = await client.request("GET", "https://api.example.com/rate-limited-date")
                assert res.status_code == 200
                assert mock_sleep.called
                assert mock_sleep.call_args[0][0] < 1.0

        await client.close()

    @pytest.mark.asyncio
    async def test_http_request_network_exception_retry_exhaustion(self):
        """AsyncHttpClient must raise MaxRetriesExceededError when network exceptions exhaust retries."""
        cfg = Settings(http_max_retries=2, http_backoff_factor=0.01)
        client = AsyncHttpClient(config=cfg)

        net_err = httpx.ConnectError("Connection refused")
        with patch.object(client._client, "request", new=AsyncMock(side_effect=net_err)):
            with patch("asyncio.sleep", new=AsyncMock()):
                with pytest.raises(MaxRetriesExceededError) as exc_info:
                    await client.request("GET", "https://api.example.com/unreachable")
                assert "Failed GET https://api.example.com/unreachable after 3 attempts" in str(exc_info.value)
        await client.close()

    @pytest.mark.asyncio
    async def test_token_bucket_burst_concurrency(self):
        """TokenBucketLimiter must safely handle concurrent bursts without deadlock."""
        limiter = TokenBucketLimiter(rate=50.0, capacity=20)
        assert limiter.tokens <= 20.0

        async def worker():
            await limiter.acquire(1.0)
            return True

        tasks = [asyncio.create_task(worker()) for _ in range(25)]
        results = await asyncio.gather(*tasks)
        assert all(results)
        assert len(results) == 25


# ==============================================================================
# 2. SCRAPERS SUBSYSTEM ADVERSARIAL HARDENING
# ==============================================================================
class TestScrapersAdversarialHardening:
    """Stress tests and coverage for base scrapers, registry, and provider implementations."""

    @pytest.mark.asyncio
    async def test_base_scraper_abstract_methods_and_validation(self):
        """BaseScraper subclass validation and abstract methods."""
        # Missing name
        class MissingNameScraper(BaseScraper):
            base_url = "https://example.com"
            async def fetch_catalog(self, *args, **kwargs): return []
            async def fetch_detail(self, *args, **kwargs): return None

        with pytest.raises(ValueError, match="must define 'name'"):
            MissingNameScraper(http_client=MagicMock())

        # Missing base_url
        class MissingUrlScraper(BaseScraper):
            name = "valid"
            async def fetch_catalog(self, *args, **kwargs): return []
            async def fetch_detail(self, *args, **kwargs): return None

        with pytest.raises(ValueError, match="must define 'base_url'"):
            MissingUrlScraper(http_client=MagicMock())

        # Concrete subclass testing abstract calls
        class BareScraper(BaseScraper):
            name = "bare"
            base_url = "https://example.com"
            async def fetch_catalog(self, content_type: ContentType, page: int = 1, genre: str | None = None) -> list[ScrapedItem]:
                return await super().fetch_catalog(content_type, page, genre)
            async def fetch_detail(self, slug: str, content_type: ContentType) -> ScrapedDetail | None:
                return await super().fetch_detail(slug, content_type)

        bare = BareScraper(http_client=MagicMock())
        assert bare.extract_identifiers({}) == (None, None)

        with pytest.raises(NotImplementedError):
            await bare.fetch_catalog(ContentType.MOVIE)

        with pytest.raises(NotImplementedError):
            await bare.fetch_detail("test", ContentType.MOVIE)

    @pytest.mark.asyncio
    async def test_base_scraper_crawl_catalog_unsupported_type_and_exceptions(self):
        """BaseScraper.crawl_catalog should handle unsupported types and fetch errors gracefully."""
        class DummyScraper(BaseScraper):
            name = "dummy"
            base_url = "https://dummy.org"
            supported_types = [ContentType.MOVIE]

            def __init__(self):
                super().__init__(http_client=MagicMock())

            async def fetch_catalog(self, content_type: ContentType, page: int = 1, genre: str | None = None) -> list[ScrapedItem]:
                if page == 1:
                    return [ScrapedItem(provider="dummy", slug="item1", title="Item 1", type="movie")]
                elif page == 2:
                    raise RuntimeError("Catalog fetch network failure")
                return []

            async def fetch_detail(self, slug: str, content_type: ContentType) -> ScrapedDetail | None:
                return None

        scraper = DummyScraper()

        # 1. Unsupported content type yields nothing
        series_items = [item async for item in scraper.crawl_catalog(ContentType.SERIES)]
        assert len(series_items) == 0

        # 2. Exception in fetch_catalog breaks loop gracefully
        movie_items = [item async for item in scraper.crawl_catalog(ContentType.MOVIE, max_pages=5)]
        assert len(movie_items) == 1
        assert movie_items[0].slug == "item1"

    def test_scraper_registry_aliases_and_error_handling(self):
        """Scraper registry must handle custom aliases, resets, and invalid lookups."""
        reset_registry()

        class CustomProvider(BaseScraper):
            name = "custom"
            base_url = "https://custom.com"
            async def fetch_catalog(self, *args, **kwargs): return []
            async def fetch_detail(self, *args, **kwargs): return None

        register_scraper("custom", CustomProvider, aliases=["custom-alias", "my-prov"])
        assert "custom" in get_registered_providers()
        assert "custom" in list_scrapers()

        scraper_from_alias = get_scraper("custom-alias", http_client=MagicMock())
        assert isinstance(scraper_from_alias, CustomProvider)

        # Invalid queries
        with pytest.raises(ValueError, match="Provider name must be a string"):
            get_scraper(123)  # type: ignore

        with pytest.raises(ValueError, match="Empty provider name provided"):
            get_scraper("   ")

        with pytest.raises(ValueError, match="Unknown provider 'nonexistent'"):
            get_scraper("nonexistent")

        # Reset registry restores defaults
        reset_registry()
        assert "custom" not in get_registered_providers()

    def test_allcalidad_error_payload_detection_matrix(self):
        """AllCalidad _is_error_payload handles all truthy and status-code variations."""
        assert _is_error_payload("non-dict") is True
        assert _is_error_payload(None) is True
        assert _is_error_payload(123) is True
        assert _is_error_payload({"status": "error"}) is True
        assert _is_error_payload({"status": "fail"}) is True
        assert _is_error_payload({"status": "failed"}) is True
        assert _is_error_payload({"status": 500}) is True
        assert _is_error_payload({"status": 404}) is True
        assert _is_error_payload({"status": 400}) is True
        assert _is_error_payload({"error": "Resource not found"}) is True
        assert _is_error_payload({"error": True}) is True
        assert _is_error_payload({"status": "success", "items": []}) is False
        assert _is_error_payload({"items": []}) is False

    @pytest.mark.asyncio
    async def test_allcalidad_scraper_catalog_and_detail_adversarial_paths(self):
        """AllCalidad catalog and detail recovery across error payloads, bad formats, and fallbacks."""
        mock_http = MagicMock()
        scraper = AllCalidadScraper(http_client=mock_http)

        # 1. extract_identifiers non-dict or malformed
        assert scraper.extract_identifiers("invalid string") == (None, None)
        assert scraper.extract_identifiers({"imdb_id": "not_tt", "tmdb_id": "abc"}) == (None, None)
        assert scraper.extract_identifiers({"imdb_id": "tt1234567", "tmdb_id": "999"}) == ("tt1234567", "999")

        # 2. fetch_catalog non-200 or error envelope
        mock_res_error = MagicMock(status_code=500)
        mock_http.get = AsyncMock(return_value=mock_res_error)
        assert await scraper.fetch_catalog(ContentType.MOVIE) == []

        # 3. fetch_catalog with non-list items or malformed item models
        mock_res_bad_items = MagicMock(status_code=200)
        mock_res_bad_items.json.return_value = {"items": "not a list"}
        mock_http.get = AsyncMock(return_value=mock_res_bad_items)
        assert await scraper.fetch_catalog(ContentType.MOVIE) == []

        # 4. fetch_catalog skipping invalid items while retaining valid ones
        mock_res_mixed = MagicMock(status_code=200)
        mock_res_mixed.json.return_value = {
            "items": [
                {"slug": "valid-movie", "title": "Valid Movie", "type": "movie", "year": "2023", "tmdb_id": "100", "imdb_id": "tt100"},
                "not-a-dict",
                {"slug": ""},  # empty slug skipped
                {"slug": "valid-series", "title": "Valid Series", "type": "tv", "release_date": "2024-05-01"},
            ]
        }
        mock_http.get = AsyncMock(return_value=mock_res_mixed)
        items = await scraper.fetch_catalog(ContentType.MOVIE)
        assert len(items) == 2
        assert items[0].slug == "valid-movie"
        assert items[0].year == 2023
        assert items[1].slug == "valid-series"
        assert items[1].year == 2024

        # 5. fetch_detail fallback: primary endpoint fails (500) -> single endpoint succeeds
        mock_res_500 = MagicMock(status_code=500)
        mock_res_single = MagicMock(status_code=200)
        mock_res_single.json.return_value = {
            "title": "Fallback Title",
            "type": "serie",
            "year": "2022",
            "tmdb_id": 555,
            "imdb_id": "tt5555555",
            "genres": "Drama",  # string genre normalized to list
        }
        mock_http.get = AsyncMock(side_effect=[mock_res_500, mock_res_single])
        detail = await scraper.fetch_detail("fallback-slug", ContentType.SERIES)
        assert detail is not None
        assert detail.title == "Fallback Title"
        assert detail.type == ContentType.SERIES
        assert detail.genres == ["Drama"]
        assert detail.tmdb_id == "555"
        assert detail.imdb_id == "tt5555555"

    @pytest.mark.asyncio
    async def test_gnula_scraper_adversarial_paths_and_slug_probing(self):
        """Gnula NEXT_DATA extraction and candidate slug probing."""
        mock_http = MagicMock()
        scraper = GnulaScraper(http_client=mock_http)

        # 1. extract_identifiers non-dict
        assert scraper.extract_identifiers(123) == (None, None)  # type: ignore
        assert gnula_extract_next_data("<html>no next data</html>") is None
        assert gnula_extract_next_data('<html><script id="__NEXT_DATA__">invalid json</script></html>') is None

        # 2. fetch_catalog missing pageProps or posts
        mock_res_empty_props = MagicMock(status_code=200, text='<html><script id="__NEXT_DATA__">{"props": {}}</script></html>')
        mock_http.get = AsyncMock(return_value=mock_res_empty_props)
        assert await scraper.fetch_catalog(ContentType.MOVIE) == []

        # 3. fetch_detail multi-candidate probing: first candidate 404, second succeeds
        mock_res_404 = MagicMock(status_code=404)
        mock_res_found = MagicMock(
            status_code=200,
            text='<html><script id="__NEXT_DATA__">{"props": {"pageProps": {"post": {"title": "Found Movie", "slug": "found-movie", "TMDbId": 777, "IMDbId": "tt7777777", "year": "2021", "genres": ["Action", "Sci-Fi"]}}}}</script></html>'
        )
        mock_http.get = AsyncMock(side_effect=[mock_res_404, mock_res_found])
        detail = await scraper.fetch_detail("found-movie", ContentType.MOVIE)
        assert detail is not None
        assert detail.title == "Found Movie"
        assert detail.tmdb_id == "777"
        assert detail.imdb_id == "tt7777777"
        assert detail.genres == ["Action", "Sci-Fi"]

        # 4. fetch_detail when all candidate paths return 404
        mock_http.get = AsyncMock(return_value=mock_res_404)
        assert await scraper.fetch_detail("nonexistent", ContentType.MOVIE) is None

    @pytest.mark.asyncio
    async def test_poseidonhd2_scraper_adversarial_paths(self):
        """PoseidonHD2 catalog parsing with diverse pageProps schemas."""
        mock_http = MagicMock()
        scraper = PoseidonHD2Scraper(http_client=mock_http)

        # 1. extract_identifiers and next_data extraction
        assert scraper.extract_identifiers("not dict") == (None, None)
        assert poseidon_extract_next_data("<html/>") is None

        # 2. fetch_catalog with pageProps.movies schema
        html_movies = '<html><script id="__NEXT_DATA__">{"props": {"pageProps": {"movies": [{"slug": "poseidon-movie", "title": "Poseidon Movie", "year": "2020", "TMDbId": 888, "IMDbId": "tt8888888"}]}}}</script></html>'
        mock_http.get = AsyncMock(return_value=MagicMock(status_code=200, text=html_movies))
        items = await scraper.fetch_catalog(ContentType.MOVIE)
        assert len(items) == 1
        assert items[0].slug == "poseidon-movie"
        assert items[0].tmdb_id == "888"

        # 3. fetch_detail with pageProps.thisSerie schema and invalid year
        html_serie = '<html><script id="__NEXT_DATA__">{"props": {"pageProps": {"thisSerie": {"slug": "poseidon-serie", "title": "Poseidon Serie", "type": "serie", "year": "invalid_year", "TMDbId": 999, "IMDbId": "tt9999999", "genres": "SingleGenre"}}}}</script></html>'
        mock_http.get = AsyncMock(return_value=MagicMock(status_code=200, text=html_serie))
        detail = await scraper.fetch_detail("poseidon-serie", ContentType.SERIES)
        assert detail is not None
        assert detail.title == "Poseidon Serie"
        assert detail.year is None
        assert detail.genres == ["SingleGenre"]
        assert detail.tmdb_id == "999"

    @pytest.mark.asyncio
    async def test_serieskao_scraper_adversarial_paths(self):
        """SeriesKao HTML parsing, JSON-LD schemas, and regex extractors."""
        mock_http = MagicMock()
        scraper = SeriesKaoScraper(http_client=mock_http)

        # 1. extract_identifiers from string and dict
        assert scraper.extract_identifiers('<iframe src="/vidurl/tt1122334/"></iframe>') == ("tt1122334", None)
        assert scraper.extract_identifiers({"identifier": "tt9988776"}) == ("tt9988776", None)
        assert scraper.extract_identifiers({"identifier": "invalid"}) == (None, None)
        assert scraper.extract_identifiers(456) == (None, None)  # type: ignore

        # 2. fetch_catalog anime genre URL routing and slug exclusion
        html_catalog = """
        <div class="item-list">
            <div class="item"><a href="/peliculas/pelicula">Invalid Slug</a></div>
            <div class="item"><a href="/series/naruto" title="Naruto"><span class="title">Naruto</span><span class="year">2002</span><span class="type">Serie</span></a></div>
        </div>
        """
        mock_http.get = AsyncMock(return_value=MagicMock(status_code=200, text=html_catalog))
        items = await scraper.fetch_catalog(ContentType.SERIES, genre="anime")
        assert len(items) == 1
        assert items[0].slug == "naruto"
        assert items[0].year == 2002
        assert items[0].type == ContentType.SERIES

        # 3. fetch_detail with JSON-LD and player iframe regex fallback
        html_detail = """
        <html>
        <head>
            <script type="application/ld+json">
            [
                {"@type": "WebPage", "name": "Site Page"},
                {"@type": "Movie", "name": "LD Movie Title", "identifier": "tt1234000", "dateCreated": "2019-10-15", "genre": ["Action", "Thriller"], "description": "LD Description"}
            ]
            </script>
        </head>
        <body>
            <iframe src="/vidurl/tt1234000/"></iframe>
        </body>
        </html>
        """
        mock_http.get = AsyncMock(return_value=MagicMock(status_code=200, text=html_detail))
        detail = await scraper.fetch_detail("ld-movie", ContentType.MOVIE)
        assert detail is not None
        assert detail.title == "LD Movie Title"
        assert detail.imdb_id == "tt1234000"
        assert detail.year == 2019
        assert detail.genres == ["Action", "Thriller"]
        assert detail.overview == "LD Description"


# ==============================================================================
# 3. TMDB RESOLVER SUBSYSTEM ADVERSARIAL HARDENING
# ==============================================================================
class TestResolverTmdbAdversarialHardening:
    """Stress tests and coverage for TmdbClient resolver."""

    @pytest.mark.asyncio
    async def test_tmdb_request_404_and_http_status_exceptions(self):
        """TmdbClient._request returns None on 404 status codes and handles HTTP status errors."""
        mock_http = MagicMock()
        client = TmdbClient(api_key="test_key", http_client=mock_http)

        # 1. Direct 404 response
        mock_res_404 = MagicMock(status_code=404)
        mock_http.request = AsyncMock(return_value=mock_res_404)
        assert await client._request("GET", "/3/movie/999999") is None

        # 2. HTTPStatusError with 404 status code
        err_404 = httpx.HTTPStatusError(message="Not found", request=MagicMock(), response=mock_res_404)
        mock_res_raise_404 = MagicMock(status_code=404)
        mock_res_raise_404.raise_for_status.side_effect = err_404
        mock_http.request = AsyncMock(return_value=mock_res_raise_404)
        assert await client._request("GET", "/3/movie/999999") is None

        # 3. HTTPStatusError with 500 status code
        mock_res_500 = MagicMock(status_code=500)
        err_500 = httpx.HTTPStatusError(message="Server error", request=MagicMock(), response=mock_res_500)
        mock_res_raise_500 = MagicMock(status_code=500)
        mock_res_raise_500.raise_for_status.side_effect = err_500
        mock_http.request = AsyncMock(return_value=mock_res_raise_500)
        assert await client._request("GET", "/3/movie/500") is None

        # 4. Generic network exception
        mock_http.request = AsyncMock(side_effect=httpx.ConnectError("Connection drop"))
        assert await client._request("GET", "/3/movie/500") is None

    @pytest.mark.asyncio
    async def test_tmdb_find_by_imdb_id_edge_cases(self):
        """TmdbClient.find_by_imdb_id boundary inputs and result routing."""
        mock_http = MagicMock()
        client = TmdbClient(api_key="test_key", http_client=mock_http)

        # Empty / whitespace input
        assert await client.find_by_imdb_id("") is None
        assert await client.find_by_imdb_id("   ") is None

        # Empty find results
        mock_res_empty = MagicMock(status_code=200)
        mock_res_empty.json.return_value = {"movie_results": [], "tv_results": []}
        mock_http.request = AsyncMock(return_value=mock_res_empty)
        assert await client.find_by_imdb_id("tt0000000") is None

        # Movie find result sets media_type="movie"
        mock_res_movie = MagicMock(status_code=200)
        mock_res_movie.json.return_value = {"movie_results": [{"id": 101, "title": "Movie 101"}], "tv_results": []}
        mock_http.request = AsyncMock(return_value=mock_res_movie)
        res_movie = await client.find_by_imdb_id("tt0000101")
        assert res_movie is not None
        assert res_movie["id"] == 101
        assert res_movie["media_type"] == "movie"

        # TV find result sets media_type="tv"
        mock_res_tv = MagicMock(status_code=200)
        mock_res_tv.json.return_value = {"movie_results": [], "tv_results": [{"id": 202, "name": "TV Show 202"}]}
        mock_http.request = AsyncMock(return_value=mock_res_tv)
        res_tv = await client.find_by_imdb_id("tt0000202")
        assert res_tv is not None
        assert res_tv["id"] == 202
        assert res_tv["media_type"] == "tv"

    @pytest.mark.asyncio
    async def test_tmdb_get_external_ids_and_search_routing(self):
        """TmdbClient external_ids and search routing for movie vs tv."""
        mock_http = MagicMock()
        client = TmdbClient(api_key="test_key", http_client=mock_http)

        # Empty TMDB ID
        assert await client.get_external_ids("", "movie") is None
        assert await client.get_external_ids("   ", "tv") is None

        # Search with empty query
        assert await client.search("", "movie") == []
        assert await client.search("   ", "tv") == []

        # Search with year parameter formatting
        mock_res_search = MagicMock(status_code=200)
        mock_res_search.json.return_value = {"results": [{"id": 303, "title": "Searched Item"}]}
        mock_http.request = AsyncMock(return_value=mock_res_search)

        # 1. Movie search uses 'year'
        await client.search("Inception", "movie", year=2010)
        mock_http.request.assert_called_with(
            method="GET",
            url="https://api.themoviedb.org/3/search/movie",
            params={"api_key": "test_key", "query": "Inception", "language": "es-MX", "year": "2010"},
        )

        # 2. TV search uses 'first_air_date_year'
        await client.search("Breaking Bad", "series", year=2008)
        mock_http.request.assert_called_with(
            method="GET",
            url="https://api.themoviedb.org/3/search/tv",
            params={"api_key": "test_key", "query": "Breaking Bad", "language": "es-MX", "first_air_date_year": "2008"},
        )

    @pytest.mark.asyncio
    async def test_tmdb_client_lifecycle_and_context_manager(self):
        """TmdbClient async context manager and close lifecycle."""
        async with TmdbClient(api_key="test_key") as client:
            assert client is not None
            assert client._owns_http_client is True
        # Closed after exit
        assert client.http_client._client.is_closed is True


# ==============================================================================
# 4. NORMALIZER & SCORING ADVERSARIAL HARDENING
# ==============================================================================
class TestMatcherNormalizerAndScoringAdversarialHardening:
    """Stress tests and coverage for title normalization and scoring rules."""

    def test_normalizer_season_ordinal_and_short_formats(self):
        """TitleNormalizer.parse handles ordinal number and short season patterns."""
        # 1. SEASON_ORD_REGEX (e.g. 2nd season, 3rd season)
        parsed_ord = TitleNormalizer.parse("Attack on Titan 2nd season")
        assert 2 in parsed_ord.season_hints
        assert "attack on titan" in parsed_ord.normalized_title

        # 2. SEASON_SHORT_REGEX (e.g. t1, t02)
        parsed_short = TitleNormalizer.parse("Dark T2 2020")
        assert 2 in parsed_short.season_hints
        assert parsed_short.year == 2020

        # 3. ORDINAL_SEASON_PATTERNS Spanish words
        parsed_es = TitleNormalizer.parse("La Casa de Papel Tercera Temporada")
        assert 3 in parsed_es.season_hints

    def test_fuzzy_matcher_short_token_overlap_branches(self):
        """FuzzyTitleMatcher handles short words (<4 chars) and token subsets."""
        # Substring containment with length < 4
        score_short = FuzzyTitleMatcher.score("it", "it chapter two")
        assert score_short > 0.0

        # Disjoint short tokens
        score_disjoint = FuzzyTitleMatcher.score("up", "down")
        assert score_disjoint == 0.0

        # Missing years comparison
        score_missing_years = FuzzyTitleMatcher.score("The Matrix", "The Matrix", year1=None, year2=1999)
        assert score_missing_years >= 85.0

        # Year present on query but missing on candidate
        score_query_has_year = FuzzyTitleMatcher.score("The Matrix", "The Matrix", year1=1999, year2=None)
        assert score_query_has_year == 65.0  # 70 - 5

    def test_candidate_scorer_non_dict_and_generic_edge_cases(self):
        """CandidateScorer gracefully handles non-dict candidates and short generic titles."""
        parsed = TitleNormalizer.parse("Avatar 2009")

        # 1. Non-dict candidate
        assert CandidateScorer.extract_year("invalid candidate") is None  # type: ignore
        res_empty = CandidateScorer.score_candidate(parsed, "invalid", "movie")  # type: ignore
        assert res_empty.score <= 0.0
        assert res_empty.confidence == "low"

        # 2. Generic title detection
        assert CandidateScorer.is_generic("Up") is True
        assert CandidateScorer.is_generic("The Dark Knight Rises") is False


# ==============================================================================
# 5. RECONCILER ADVERSARIAL HARDENING
# ==============================================================================
class TestMatcherReconcilerAdversarialHardening:
    """Stress tests and coverage for IdentityReconciler and transitive batch bridging."""

    @pytest.mark.asyncio
    async def test_reconcile_item_external_id_exception_handling(self):
        """IdentityReconciler logs warning and recovers when TMDB get_external_ids raises an exception."""
        mock_tmdb = MagicMock(spec=TmdbClient)
        mock_tmdb.find_by_imdb_id = AsyncMock(return_value=None)
        mock_tmdb.search = AsyncMock(
            return_value=[{"id": 444, "title": "Inception", "release_date": "2010-07-16", "media_type": "movie"}]
        )
        # External IDs API fails with 500 error
        mock_tmdb.get_external_ids = AsyncMock(side_effect=RuntimeError("TMDB external_ids timeout"))

        reconciler = IdentityReconciler(tmdb_client=mock_tmdb)
        item = ScrapedItem(provider="allcalidad", slug="inception-2010", title="Inception", type="movie", year=2010)

        mapping = await reconciler.reconcile_item(item)
        assert mapping is not None
        assert mapping.tmdb_id == "444"
        assert mapping.imdb_id is None
        assert mapping.title == "Inception"
        assert mapping.providers == {"allcalidad": "inception-2010"}

    @pytest.mark.asyncio
    async def test_reconcile_item_media_type_tv_resolution(self):
        """IdentityReconciler resolves candidate media_type='tv' to ContentType.SERIES."""
        mock_tmdb = MagicMock(spec=TmdbClient)
        mock_tmdb.find_by_imdb_id = AsyncMock(return_value=None)
        mock_tmdb.search = AsyncMock(
            return_value=[{"id": 555, "name": "Chernobyl", "first_air_date": "2019-05-06", "media_type": "tv"}]
        )
        mock_tmdb.get_external_ids = AsyncMock(return_value={"imdb_id": "tt5550000"})

        reconciler = IdentityReconciler(tmdb_client=mock_tmdb)
        item = ScrapedItem(provider="serieskao", slug="chernobyl", title="Chernobyl", type="series", year=2019)

        mapping = await reconciler.reconcile_item(item)
        assert mapping is not None
        assert mapping.type == ContentType.SERIES
        assert mapping.tmdb_id == "555"
        assert mapping.imdb_id == "tt5550000"

    @pytest.mark.asyncio
    async def test_reconcile_existing_mapping_field_enrichment(self):
        """Reconciling an item matching existing store mapping enriches missing fields."""
        store = MasterMappingStore()
        existing = CanonicalMapping(
            tmdb_id=None,
            imdb_id="tt7777777",
            title="",
            type="movie",
            year=None,
            providers={"gnula": "old-slug"},
        )
        store.add_or_update(existing)

        mock_tmdb = MagicMock(spec=TmdbClient)
        mock_tmdb.find_by_imdb_id = AsyncMock(
            return_value={"id": 777, "title": "Enriched Title", "release_date": "2018-05-20", "media_type": "movie"}
        )

        reconciler = IdentityReconciler(tmdb_client=mock_tmdb)
        item = ScrapedItem(
            provider="poseidonhd2",
            slug="new-slug",
            title="Enriched Title",
            type="movie",
            imdb_id="tt7777777",
        )

        updated = await reconciler.reconcile_item(item, master_store=store)
        assert updated is not None
        assert updated.tmdb_id == "777"
        assert updated.imdb_id == "tt7777777"
        assert updated.title == "Enriched Title"
        assert updated.year == 2018
        assert "poseidonhd2" in updated.providers
        assert "gnula" in updated.providers

    @pytest.mark.asyncio
    async def test_reconcile_batch_transitive_multi_provider_bridging(self):
        """reconcile_batch merges disparate items and re-points by_tmdb/by_imdb when bridging occurs."""
        mock_tmdb = MagicMock(spec=TmdbClient)
        mock_tmdb.get_external_ids = AsyncMock(return_value={"imdb_id": None})
        mock_tmdb.find_by_imdb_id = AsyncMock(return_value=None)
        reconciler = IdentityReconciler(tmdb_client=mock_tmdb)

        # Item 1: TMDB 100
        item1 = ScrapedItem(provider="allcalidad", slug="movie-100", title="Bridge Movie", type="movie", tmdb_id="100", year=2020)
        # Item 2: TMDB 999 and IMDb tt200 (creates entry in by_tmdb for secondary)
        item2 = ScrapedItem(provider="gnula", slug="movie-200", title="Bridge Movie", type="movie", tmdb_id="999", imdb_id="tt200", year=2020)
        # Item 3: Bridges TMDB 100 and IMDb tt200
        item3 = ScrapedItem(provider="poseidonhd2", slug="movie-300", title="Bridge Movie", type="movie", tmdb_id="100", imdb_id="tt200", year=2020)

        batch_result = await reconciler.reconcile_batch([item1, item2, item3])
        assert len(batch_result) == 1
        merged = batch_result[0]
        assert merged.tmdb_id in ("100", "999")
        assert merged.imdb_id == "tt200"
        assert len(merged.providers) == 3


# ==============================================================================
# 6. MASTER STORE & EXPORTER ADVERSARIAL HARDENING
# ==============================================================================
class TestStorageMasterAndExporterAdversarialHardening:
    """Stress tests and coverage for MasterMappingStore and OrionExporter."""

    def test_master_store_unindex_and_coalesce_multiple_records(self, tmp_path: Path):
        """MasterMappingStore unindexes old references when coalescing 3 records into 1."""
        store = MasterMappingStore(storage_dir=tmp_path)

        # Mapping 1: TMDB 111, Provider prov1
        m1 = CanonicalMapping(tmdb_id="111", imdb_id=None, title="Entity One", type="movie", providers={"prov1": "slug1"})
        store.add_or_update(m1)

        # Mapping 2: TMDB 222, IMDb tt222, Provider prov2
        m2 = CanonicalMapping(tmdb_id="222", imdb_id="tt222", title="Entity Two", type="movie", providers={"prov2": "slug2"})
        store.add_or_update(m2)

        assert store.count() == 2

        # Mapping 3: Has TMDB 111, IMDb tt222, and Provider prov3
        m3 = CanonicalMapping(tmdb_id="111", imdb_id="tt222", title="Entity One", type="movie", providers={"prov3": "slug3"})
        coalesced = store.add_or_update(m3)

        assert store.count() == 1
        assert len(coalesced.providers) == 3
        assert store.get_by_tmdb("111", "movie") is coalesced
        assert store.get_by_imdb("tt222", "movie") is coalesced
        assert store.get_by_provider_slug("prov1", "slug1") is coalesced
        assert store.get_by_provider_slug("prov2", "slug2") is coalesced
        assert store.get_by_provider_slug("prov3", "slug3") is coalesced

    def test_master_store_load_corruption_and_malformed_entries(self, tmp_path: Path):
        """MasterMappingStore.load() must skip malformed / non-dict records in movies.json and series.json."""
        store_dir = tmp_path / "corrupt_store"
        store_dir.mkdir(parents=True)

        movies_file = store_dir / "movies.json"
        series_file = store_dir / "series.json"

        # movies.json with mixed valid and invalid records
        movies_file.write_text(
            json.dumps([
                {"tmdb_id": "10", "imdb_id": "tt0000010", "title": "Valid Movie", "type": "movie"},
                "not a dict string",
                {"title": "Missing Type Movie"},  # Missing type causes validation failure
                {"tmdb_id": "20", "imdb_id": "tt0000020", "title": "Valid Movie 2", "type": "movie"},
            ]),
            encoding="utf-8",
        )

        # series.json with non-list JSON payload
        series_file.write_text(json.dumps({"error": "not a list"}), encoding="utf-8")

        store = MasterMappingStore(storage_dir=store_dir)
        # Should load the 2 valid movies and 0 series without raising an unhandled exception
        assert store.count(ContentType.MOVIE) == 2
        assert store.count(ContentType.SERIES) == 0

    def test_master_store_sorting_comparator_boundary_values(self):
        """MasterMappingStore sorting key handles non-numeric TMDB IDs and None values."""
        m_none_tmdb = CanonicalMapping(tmdb_id=None, imdb_id="tt1", title="B Title", type="movie")
        m_num_tmdb = CanonicalMapping(tmdb_id="50", imdb_id="tt2", title="A Title", type="movie")
        m_alpha_tmdb = CanonicalMapping(tmdb_id="invalid", imdb_id="tt3", title="C Title", type="movie")

        k_none = MasterMappingStore._sort_key(m_none_tmdb)
        k_num = MasterMappingStore._sort_key(m_num_tmdb)
        k_alpha = MasterMappingStore._sort_key(m_alpha_tmdb)

        assert k_num[0] == 50
        assert k_none[0] == float("inf")
        assert k_alpha[0] == float("inf")

    def test_orion_exporter_base64_url_safe_key_encoding_parity(self, tmp_path: Path):
        """OrionExporter encode_provider_key / decode_provider_key parity with Kotlin."""
        test_cases = [
            ("gnula", "pelicula-123", "Z251bGE6cGVsaWN1bGEtMTIz"),
            ("poseidonhd2", "serie/breaking-bad", "cG9zZWlkb25oZDI6c2VyaWUvYnJlYWtpbmctYmFk"),
            ("allcalidad", "español/123", "YWxsY2FsaWRhZDplc3Bhw7FvbC8xMjM"),
        ]

        for prov, slug, expected_key in test_cases:
            encoded = encode_provider_key(prov, slug)
            assert encoded == expected_key
            decoded_prov, decoded_slug = decode_provider_key(encoded)
            assert decoded_prov == prov
            assert decoded_slug == slug

        exporter = OrionExporter(output_dir=tmp_path)
        mappings = [
            CanonicalMapping(
                tmdb_id="999",
                imdb_id="tt9999999",
                title="Export Test Movie",
                type="movie",
                providers={"gnula": "export-test-slug"},
            )
        ]
        summary = exporter.export_mappings(mappings)
        assert summary.total_files == 3
        assert summary.imdb_count == 1
        assert summary.tmdb_count == 1
        assert summary.provider_count == 1

        # Verify generated files exist and validate against OrionServer schemas
        imdb_file = tmp_path / "imdb" / "tt9999999.json"
        tmdb_file = tmp_path / "tmdb" / "999.json"
        prov_file = tmp_path / "providers" / f"{encode_provider_key('gnula', 'export-test-slug')}.json"

        assert imdb_file.exists()
        assert tmdb_file.exists()
        assert prov_file.exists()

        ImdbIdentityIndexExport.model_validate_json(imdb_file.read_text())
        TmdbIdentityIndexExport.model_validate_json(tmdb_file.read_text())
        IdentityMappingExport.model_validate_json(prov_file.read_text())


# ==============================================================================
# 7. CLI COMMANDS & ENTRY POINT ADVERSARIAL HARDENING
# ==============================================================================
class TestCliCommandsAndMainAdversarialHardening:
    """Stress tests and coverage for CLI subcommands, limits, and entry points."""

    @pytest.mark.asyncio
    async def test_cli_scrape_limit_zero_or_negative_and_unsupported_types(self):
        """execute_scrape skips providers with unsupported content type or limit <= 0."""
        mock_scraper = MagicMock()
        mock_scraper.supported_types = [ContentType.MOVIE]
        mock_scraper.fetch_catalog = AsyncMock(return_value=[])

        with patch("orion_mapper.cli.commands.get_scraper", return_value=mock_scraper):
            # 1. Limit <= 0
            args_zero = argparse.Namespace(provider="allcalidad", type="movie", limit=0, output_dir=None, dry_run=True, rate_limit=None)
            assert await execute_scrape(args_zero) == 0
            mock_scraper.fetch_catalog.assert_not_called()

            # 2. Unsupported content type (e.g. series on movie-only scraper)
            args_unsupported = argparse.Namespace(provider="allcalidad", type="series", limit=10, output_dir=None, dry_run=True, rate_limit=None)
            assert await execute_scrape(args_unsupported) == 0
            mock_scraper.fetch_catalog.assert_not_called()

    @pytest.mark.asyncio
    async def test_cli_scrape_page_item_truncation_when_limit_reached(self):
        """execute_scrape truncates items exactly when limit is reached across pages."""
        items_page1 = [
            ScrapedItem(provider="allcalidad", slug=f"item-{i}", title=f"Item {i}", type="movie")
            for i in range(5)
        ]
        items_page2 = [
            ScrapedItem(provider="allcalidad", slug=f"item-{i}", title=f"Item {i}", type="movie")
            for i in range(5, 10)
        ]

        mock_scraper = MagicMock()
        mock_scraper.supported_types = [ContentType.MOVIE]
        mock_scraper.fetch_catalog = AsyncMock(side_effect=[items_page1, items_page2])

        with patch("orion_mapper.cli.commands.get_scraper", return_value=mock_scraper):
            args = argparse.Namespace(
                provider="allcalidad",
                type="movie",
                limit=7,
                output_dir=None,
                dry_run=True,
                rate_limit=None,
            )
            assert await execute_scrape(args) == 0
            assert mock_scraper.fetch_catalog.call_count == 2

    @pytest.mark.asyncio
    async def test_cli_sync_limit_boundary_and_unsupported_types(self):
        """execute_sync handles limit <= 0 and unsupported types during scraping phase."""
        mock_scraper = MagicMock()
        mock_scraper.supported_types = [ContentType.MOVIE]
        mock_scraper.fetch_catalog = AsyncMock(return_value=[])

        mock_reconciler = MagicMock()
        mock_reconciler.reconcile_batch = AsyncMock(return_value=[])

        mock_store = MagicMock()
        mock_store.count.return_value = 0

        with patch("orion_mapper.cli.commands.get_scraper", return_value=mock_scraper):
            with patch("orion_mapper.cli.commands.IdentityReconciler", return_value=mock_reconciler):
                with patch("orion_mapper.cli.commands.MasterMappingStore", return_value=mock_store):
                    args = argparse.Namespace(
                        provider="allcalidad",
                        type="series",  # Unsupported on mock_scraper
                        limit=0,        # Limit <= 0
                        store_dir=None,
                        export_dir=None,
                        dry_run=True,
                        rate_limit=None,
                    )
                    assert await execute_sync(args) == 0
                    mock_scraper.fetch_catalog.assert_not_called()

    def test_cli_main_dispatch_and_help_branches(self):
        """main() handles empty argv, --help, invalid commands, and exception catching."""
        # 1. Empty args
        assert main([]) == 0

        # 2. Help flag raises SystemExit(0)
        with pytest.raises(SystemExit) as exc_info:
            main(["--help"])
        assert exc_info.value.code == 0

        # 3. Invalid command
        with patch("argparse.ArgumentParser.parse_args") as mock_parse:
            mock_parse.return_value = argparse.Namespace(command="invalid_command")
            assert main(["invalid_command"]) == 1

        # 4. Unhandled exception in command execution
        with patch("orion_mapper.cli.commands.execute_export", side_effect=RuntimeError("Export crash")):
            assert main(["export"]) == 1

    def test_main_py_entrypoint_via_subprocess(self):
        """Invoking python main.py --help via subprocess executes cleanly with exit code 0."""
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "main.py"), "--help"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "OrionMapper CLI" in result.stdout or "usage:" in result.stdout.lower()

    def test_app_console_entry_point(self):
        """app() calls sys.exit(main())."""
        with patch("orion_mapper.cli.commands.main", return_value=0):
            with pytest.raises(SystemExit) as exc_info:
                app()
            assert exc_info.value.code == 0

    @pytest.mark.asyncio
    async def test_cli_scrape_and_sync_with_no_limit(self):
        """execute_scrape and execute_sync when limit is None extend all items."""
        mock_items = [
            ScrapedItem(provider="allcalidad", slug="no-limit-1", title="No Limit 1", type="movie"),
            ScrapedItem(provider="allcalidad", slug="no-limit-2", title="No Limit 2", type="movie"),
        ]
        mock_scraper = MagicMock()
        mock_scraper.supported_types = [ContentType.MOVIE]
        mock_scraper.fetch_catalog = AsyncMock(side_effect=[mock_items, []])

        mock_reconciler = MagicMock()
        mock_reconciler.reconcile_batch = AsyncMock(return_value=[])

        mock_store = MagicMock()
        mock_store.count.return_value = 0

        # 1. Scrape with limit=None
        with patch("orion_mapper.cli.commands.get_scraper", return_value=mock_scraper):
            args_scrape = argparse.Namespace(provider="allcalidad", type="movie", limit=None, output_dir=None, dry_run=True, rate_limit=None)
            assert await execute_scrape(args_scrape) == 0

        # 2. Sync with limit=None
        mock_scraper.fetch_catalog = AsyncMock(side_effect=[mock_items, []])
        with patch("orion_mapper.cli.commands.get_scraper", return_value=mock_scraper):
            with patch("orion_mapper.cli.commands.IdentityReconciler", return_value=mock_reconciler):
                with patch("orion_mapper.cli.commands.MasterMappingStore", return_value=mock_store):
                    args_sync = argparse.Namespace(provider="allcalidad", type="movie", limit=None, store_dir=None, export_dir=None, dry_run=True, rate_limit=None)
                    assert await execute_sync(args_sync) == 0

    def test_cli_main_implicit_sys_argv_and_missing_command(self):
        """main(None) parses sys.argv, and missing subcommand prints help and returns 0."""
        # 1. sys.argv default handling
        with patch.object(sys, "argv", ["main.py"]):
            assert main(None) == 0

        # 2. Namespace without command
        with patch("argparse.ArgumentParser.parse_args") as mock_parse:
            mock_parse.return_value = argparse.Namespace(command=None)
            assert main(["--version"]) == 0

    def test_scoring_candidate_year_delta_one_and_substring_branches(self):
        """CandidateScorer delta 1 (+10) and sub-token fuzzy branches."""
        parsed = TitleNormalizer.parse("Spider-Man 2021")
        cand_delta1 = {
            "title": "Spider-Man",
            "release_date": "2022-01-01",
            "media_type": "movie",
            "id": 1234,
        }
        res = CandidateScorer.score_candidate(parsed, cand_delta1, "movie")
        # delta 1 gives +10 bonus instead of +25
        assert res.score >= 70.0
        assert res.confidence in ("candidate", "high")

        # FuzzyTitleMatcher substring length >= 4
        score_sub = FuzzyTitleMatcher.score("spider", "spider man")
        assert score_sub >= 60.0

    @pytest.mark.asyncio
    async def test_reconciler_empty_title_and_existing_imdb_enrichment(self):
        """IdentityReconciler resolves empty title from TMDB find and enriches existing store mapping."""
        mock_tmdb = MagicMock(spec=TmdbClient)
        mock_tmdb.find_by_imdb_id = AsyncMock(
            return_value={"id": 888, "title": "Resolved Find Title", "release_date": "2020-01-01", "media_type": "movie"}
        )

        store = MasterMappingStore()
        existing = CanonicalMapping(
            tmdb_id="888",
            imdb_id=None,
            title="Existing",
            type="movie",
            providers={"gnula": "gnula-slug"},
        )
        store.add_or_update(existing)

        reconciler = IdentityReconciler(tmdb_client=mock_tmdb)
        item = ScrapedItem(
            provider="allcalidad",
            slug="new-allcalidad",
            title="",  # Empty title
            type="movie",
            imdb_id="tt8888888",
        )

        resolved = await reconciler.reconcile_item(item, master_store=store)
        assert resolved is not None
        assert resolved.tmdb_id == "888"
        assert resolved.imdb_id == "tt8888888"
        assert resolved.title == "Existing"  # Preserved existing non-empty title
        assert "allcalidad" in resolved.providers

    def test_master_store_series_load_malformed_and_non_dict_items(self, tmp_path: Path):
        """MasterMappingStore skips invalid and non-dict records in series.json."""
        store_dir = tmp_path / "series_corrupt"
        store_dir.mkdir(parents=True)
        series_file = store_dir / "series.json"

        series_file.write_text(
            json.dumps([
                {"tmdb_id": "301", "imdb_id": "tt0000301", "title": "Valid Series 1", "type": "series"},
                12345,  # Non-dict skipped
                {"title": "Invalid Series No Type"},  # Pydantic validation error skipped
                {"tmdb_id": "302", "imdb_id": "tt0000302", "title": "Valid Series 2", "type": "series"},
            ]),
            encoding="utf-8",
        )

        store = MasterMappingStore(storage_dir=store_dir)
        assert store.count(ContentType.SERIES) == 2
        assert store.get_by_tmdb("301", ContentType.SERIES) is not None
        assert store.get_by_tmdb("302", ContentType.SERIES) is not None

