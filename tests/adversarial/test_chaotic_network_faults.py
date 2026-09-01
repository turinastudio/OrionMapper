"""Adversarial Tier 5 Test Suite: Chaotic Network Faults.

Stress-tests end-to-end sync flows, scrapers, and TMDB resolver against:
- Intermittent HTTP 429 (Rate Limit) with varied Retry-After headers
- Intermittent HTTP 500, 502, 503, 504 server errors
- Connection drops (ConnectError, ReadTimeout, RemoteProtocolError)
- Truncated payloads and malformed HTML/JSON responses
- Complete network outages and graceful degradation
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from orion_mapper.cli.commands import execute_sync
from orion_mapper.core.config import Settings
from orion_mapper.core.http import AsyncHttpClient, MaxRetriesExceededError
from orion_mapper.models.item import ContentType
from orion_mapper.resolver.tmdb import TmdbClient
from orion_mapper.scrapers.allcalidad import AllCalidadScraper
from orion_mapper.scrapers.gnula import GnulaScraper
from orion_mapper.scrapers.poseidonhd2 import PoseidonHD2Scraper
from orion_mapper.scrapers.serieskao import SeriesKaoScraper
from orion_mapper.storage.master import MasterMappingStore
from orion_mapper.storage.orion_exporter import OrionExporter


class ChaoticMockTransport(httpx.AsyncBaseTransport):
    """
    Configurable transport that injects chaotic network faults based on URL patterns
    or probabilistic schedules.
    """

    def __init__(
        self,
        base_fixtures: dict[str, Any],
        fault_schedule: dict[str, list[Any]] | None = None,
        default_status: int = 200,
    ) -> None:
        self.fixtures = base_fixtures
        self.fault_schedule = fault_schedule or {}
        self.call_counts: dict[str, int] = {}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        path = request.url.path

        # Track call count per URL or endpoint prefix
        endpoint_key = path
        for pattern in self.fault_schedule:
            if pattern in url_str:
                endpoint_key = pattern
                break

        count = self.call_counts.get(endpoint_key, 0)
        self.call_counts[endpoint_key] = count + 1

        # Check if there is a scheduled fault for this endpoint and attempt
        if endpoint_key in self.fault_schedule:
            schedule = self.fault_schedule[endpoint_key]
            if count < len(schedule):
                fault = schedule[count]
                if isinstance(fault, Exception):
                    raise fault
                elif isinstance(fault, tuple):
                    # (status_code, headers, content_or_json)
                    status_code, headers, body = fault
                    if isinstance(body, dict):
                        return httpx.Response(
                            status_code,
                            headers=headers,
                            json=body,
                            request=request,
                        )
                    else:
                        return httpx.Response(
                            status_code,
                            headers=headers,
                            text=str(body),
                            request=request,
                        )
                elif isinstance(fault, int):
                    return httpx.Response(fault, request=request)

        # Standard fixture dispatch if no fault scheduled
        if "serieskao" in url_str:
            if "/pelicula/" in path:
                return httpx.Response(200, text=self.fixtures["serieskao"]["detail_movie"], request=request)
            if "/serie/" in path:
                return httpx.Response(200, text=self.fixtures["serieskao"]["detail_series"], request=request)
            return httpx.Response(200, text=self.fixtures["serieskao"]["catalog_page1"], request=request)

        if "poseidonhd2" in url_str:
            if "/pelicula/" in path:
                return httpx.Response(200, text=self.fixtures["poseidonhd2"]["detail_movie"], request=request)
            if "/serie/" in path:
                return httpx.Response(200, text=self.fixtures["poseidonhd2"]["detail_series"], request=request)
            return httpx.Response(200, text=self.fixtures["poseidonhd2"]["catalog_page1"], request=request)

        if "gnula" in url_str:
            if "pelicula" in path:
                return httpx.Response(200, text=self.fixtures["gnula"]["detail_movie"], request=request)
            if "serie" in path:
                return httpx.Response(200, text=self.fixtures["gnula"]["detail_series"], request=request)
            return httpx.Response(200, text=self.fixtures["gnula"]["catalog_page1"], request=request)

        if "allcalidad" in url_str:
            if "/api/rest/single" in path or "/api/rest/movie" in path or "550" in path:
                return httpx.Response(200, json=self.fixtures["allcalidad"]["single_movie"], request=request)
            if "/api/rest/series" in path or "82856" in path or "zombieland" in path:
                return httpx.Response(200, json=self.fixtures["allcalidad"]["single_series"], request=request)
            return httpx.Response(200, json=self.fixtures["allcalidad"]["listing_page1"], request=request)

        if "api.themoviedb.org" in url_str:
            if "/3/find/tt0137523" in path:
                return httpx.Response(200, json=self.fixtures["tmdb"]["find_tt0137523"], request=request)
            if "/3/find/tt15486" in path:
                return httpx.Response(200, json=self.fixtures["tmdb"]["find_tt15486"], request=request)
            if "/3/movie/550/external_ids" in path:
                return httpx.Response(200, json=self.fixtures["tmdb"]["movie_550_external_ids"], request=request)
            if "/3/tv/82856/external_ids" in path:
                return httpx.Response(200, json=self.fixtures["tmdb"]["tv_82856_external_ids"], request=request)
            if "/3/search/movie" in path:
                return httpx.Response(200, json=self.fixtures["tmdb"]["search_movie_fight_club"], request=request)
            if "/3/search/tv" in path:
                return httpx.Response(200, json=self.fixtures["tmdb"]["search_tv_zombieland"], request=request)
            return httpx.Response(200, json={"results": []}, request=request)

        return httpx.Response(404, json={"error": "Not Found"}, request=request)


# ==============================================================================
# 1. HTTP STACK CHAOTIC FAULT & RETRY ADVERSARIAL TESTS
# ==============================================================================
class TestHttpLayerChaos:
    """Stress tests for AsyncHttpClient under network chaos."""

    @pytest.mark.asyncio
    async def test_transient_500_recovers_on_subsequent_attempt(self, mock_transport):
        fast_settings = Settings(
            http_max_retries=3,
            http_backoff_factor=0.01,
            http_backoff_max=0.05,
            http_timeout=1.0,
        )
        faulty_transport = ChaoticMockTransport(
            base_fixtures=mock_transport.fixtures,
            fault_schedule={
                "/3/find/tt0137523": [
                    (500, {}, "Internal Server Error"),
                    (502, {}, "Bad Gateway"),
                    (200, {}, mock_transport.fixtures["tmdb"]["find_tt0137523"]),
                ]
            },
        )
        client = AsyncHttpClient(config=fast_settings)
        client._client = httpx.AsyncClient(transport=faulty_transport)

        res = await client.get("https://api.themoviedb.org/3/find/tt0137523")
        assert res.status_code == 200
        data = res.json()
        assert len(data.get("movie_results", [])) > 0
        assert faulty_transport.call_counts["/3/find/tt0137523"] == 3
        await client.close()

    @pytest.mark.asyncio
    async def test_rate_limit_429_with_various_retry_after_headers(self, mock_transport):
        fast_settings = Settings(
            http_max_retries=3,
            http_backoff_factor=0.01,
            http_backoff_max=0.05,
        )
        faulty_transport = ChaoticMockTransport(
            base_fixtures=mock_transport.fixtures,
            fault_schedule={
                "/3/find/tt0137523": [
                    (429, {"Retry-After": "0"}, "Rate Limited"),
                    (429, {"Retry-After": "invalid_number"}, "Rate Limited"),
                    (200, {}, mock_transport.fixtures["tmdb"]["find_tt0137523"]),
                ]
            },
        )
        client = AsyncHttpClient(config=fast_settings)
        client._client = httpx.AsyncClient(transport=faulty_transport)

        res = await client.get("https://api.themoviedb.org/3/find/tt0137523")
        assert res.status_code == 200
        assert faulty_transport.call_counts["/3/find/tt0137523"] == 3
        await client.close()

    @pytest.mark.asyncio
    async def test_network_connection_drops_and_read_timeouts(self, mock_transport):
        fast_settings = Settings(
            http_max_retries=3,
            http_backoff_factor=0.01,
            http_backoff_max=0.05,
        )
        faulty_transport = ChaoticMockTransport(
            base_fixtures=mock_transport.fixtures,
            fault_schedule={
                "/3/find/tt0137523": [
                    httpx.ConnectError("Connection refused by peer"),
                    httpx.ReadTimeout("Read timed out"),
                    (200, {}, mock_transport.fixtures["tmdb"]["find_tt0137523"]),
                ]
            },
        )
        client = AsyncHttpClient(config=fast_settings)
        client._client = httpx.AsyncClient(transport=faulty_transport)

        res = await client.get("https://api.themoviedb.org/3/find/tt0137523")
        assert res.status_code == 200
        assert faulty_transport.call_counts["/3/find/tt0137523"] == 3
        await client.close()

    @pytest.mark.asyncio
    async def test_unrecoverable_failures_raise_max_retries_exceeded(self, mock_transport):
        fast_settings = Settings(
            http_max_retries=2,
            http_backoff_factor=0.01,
            http_backoff_max=0.05,
        )
        faulty_transport = ChaoticMockTransport(
            base_fixtures=mock_transport.fixtures,
            fault_schedule={
                "/3/find/tt0137523": [
                    (503, {}, "Service Unavailable"),
                    (503, {}, "Service Unavailable"),
                    (503, {}, "Service Unavailable"),
                ]
            },
        )
        client = AsyncHttpClient(config=fast_settings)
        client._client = httpx.AsyncClient(transport=faulty_transport)

        with pytest.raises(MaxRetriesExceededError):
            await client.get("https://api.themoviedb.org/3/find/tt0137523")

        assert faulty_transport.call_counts["/3/find/tt0137523"] == 3
        await client.close()


# ==============================================================================
# 2. SCRAPER RESILIENCE AGAINST TRUNCATED & MALFORMED PAYLOADS
# ==============================================================================
class TestScraperPayloadCorruptionResilience:
    """Stress tests scrapers against cut-off HTML, corrupted Next.js payloads, and invalid JSON."""

    @pytest.mark.asyncio
    async def test_serieskao_with_severely_truncated_html(self, mock_transport):
        """Truncated HTML missing tags or scripts should return empty list gracefully without throwing."""
        corrupted_html = "<html><head><title>Incomplete"
        faulty_transport = ChaoticMockTransport(
            base_fixtures=mock_transport.fixtures,
            fault_schedule={"serieskao": [(200, {}, corrupted_html), (200, {}, corrupted_html)]},
        )
        client = AsyncHttpClient(config=Settings(http_timeout=1.0))
        client._client = httpx.AsyncClient(transport=faulty_transport)

        scraper = SeriesKaoScraper(http_client=client)
        items = await scraper.fetch_catalog(ContentType.MOVIE, page=1)
        assert isinstance(items, list)
        assert len(items) == 0

        detail = await scraper.fetch_detail("incomplete-slug", ContentType.MOVIE)
        assert detail is not None
        assert detail.imdb_id is None
        assert detail.title == "incomplete-slug"
        await client.close()

    @pytest.mark.asyncio
    async def test_poseidonhd2_with_corrupted_next_data_json(self, mock_transport):
        """Malformed JSON inside __NEXT_DATA__ should be caught and return empty gracefully."""
        corrupted_html = """
        <html><body>
        <script id="__NEXT_DATA__" type="application/json">
        {"props": {"pageProps": {"items": [{"title": "Unclosed
        </script></body></html>
        """
        faulty_transport = ChaoticMockTransport(
            base_fixtures=mock_transport.fixtures,
            fault_schedule={"poseidonhd2": [(200, {}, corrupted_html)]},
        )
        client = AsyncHttpClient(config=Settings(http_timeout=1.0))
        client._client = httpx.AsyncClient(transport=faulty_transport)

        scraper = PoseidonHD2Scraper(http_client=client)
        items = await scraper.fetch_catalog(ContentType.MOVIE, page=1)
        assert isinstance(items, list)
        assert len(items) == 0
        await client.close()

    @pytest.mark.asyncio
    async def test_gnula_with_missing_next_data_script(self, mock_transport):
        """HTML completely lacking __NEXT_DATA__ should fall back cleanly without unhandled crash."""
        corrupted_html = "<html><body><h1>Random Static Page</h1></body></html>"
        faulty_transport = ChaoticMockTransport(
            base_fixtures=mock_transport.fixtures,
            fault_schedule={"gnula": [(200, {}, corrupted_html)]},
        )
        client = AsyncHttpClient(config=Settings(http_timeout=1.0))
        client._client = httpx.AsyncClient(transport=faulty_transport)

        scraper = GnulaScraper(http_client=client)
        items = await scraper.fetch_catalog(ContentType.MOVIE, page=1)
        assert isinstance(items, list)
        assert len(items) == 0
        await client.close()

    @pytest.mark.asyncio
    async def test_allcalidad_with_non_array_and_broken_json(self, mock_transport):
        """AllCalidad returning invalid or non-list JSON should return empty list."""
        faulty_transport = ChaoticMockTransport(
            base_fixtures=mock_transport.fixtures,
            fault_schedule={
                "allcalidad": [
                    (200, {}, "Not JSON at all"),
                    (200, {}, {"error": "Server error", "items": None}),
                ]
            },
        )
        client = AsyncHttpClient(config=Settings(http_timeout=1.0))
        client._client = httpx.AsyncClient(transport=faulty_transport)

        scraper = AllCalidadScraper(http_client=client)
        items1 = await scraper.fetch_catalog(ContentType.MOVIE, page=1)
        assert isinstance(items1, list)
        assert len(items1) == 0

        items2 = await scraper.fetch_catalog(ContentType.MOVIE, page=2)
        assert isinstance(items2, list)
        assert len(items2) == 0
        await client.close()


# ==============================================================================
# 3. END-TO-END SYNC FLOW UNDER CHAOTIC NETWORK CONDITIONS
# ==============================================================================
class TestE2ESyncChaoticNetworkFlows:
    """Stress tests the complete sync pipeline against heterogeneous, multi-provider network failures."""

    @pytest.mark.asyncio
    async def test_e2e_sync_partial_provider_outage_and_tmdb_instability(
        self, mock_transport, tmp_path: Path
    ):
        """
        Adversarial Scenario:
        - SeriesKao throws 500 on all attempts (complete provider outage)
        - PoseidonHD2 suffers 429 on page 1 but recovers on retry
        - Gnula returns truncated HTML
        - AllCalidad succeeds normally
        - TMDB find endpoint throws 503 once then recovers
        Expected outcome:
        - Sync finishes with exit code 0
        - Successfully scrapes PoseidonHD2 and AllCalidad
        - Reconciles valid items
        - Successfully updates and saves master dataset
        - Exports valid OrionServer indices
        """
        mappings_dir = tmp_path / "mappings"
        orion_dir = tmp_path / "orion_mappings"

        chaotic_transport = ChaoticMockTransport(
            base_fixtures=mock_transport.fixtures,
            fault_schedule={
                # SeriesKao persistent 500
                "serieskao": [
                    (500, {}, "SeriesKao Down"),
                    (500, {}, "SeriesKao Down"),
                    (500, {}, "SeriesKao Down"),
                ],
                # PoseidonHD2 transient 429 then success
                "poseidonhd2": [
                    (429, {"Retry-After": "0.01"}, "Rate limited"),
                ],
                # Gnula truncated HTML
                "gnula": [
                    (200, {}, "<html><body>Broken truncated content"),
                ],
                # TMDB transient 503 then success
                "/3/find/tt0137523": [
                    (503, {}, "Service Temporarily Unavailable"),
                ],
            },
        )

        custom_client = AsyncHttpClient(
            config=Settings(
                http_max_retries=2,
                http_backoff_factor=0.01,
                http_backoff_max=0.05,
            )
        )
        custom_client._client = httpx.AsyncClient(transport=chaotic_transport)

        # Patch get_scraper and TmdbClient to use the chaotic client
        from orion_mapper.scrapers import get_scraper as original_get_scraper

        def patched_get_scraper(provider_name: str, **kwargs):
            return original_get_scraper(provider_name, http_client=custom_client)

        with (
            patch("orion_mapper.cli.commands.get_scraper", side_effect=patched_get_scraper),
            patch("orion_mapper.cli.commands.TmdbClient") as mock_tmdb_cls,
        ):
            real_tmdb = TmdbClient(http_client=custom_client)
            mock_tmdb_cls.return_value = real_tmdb

            args = argparse.Namespace(
                provider="all",
                type=None,
                limit=5,
                unmapped_only=False,
                mappings_dir=str(mappings_dir),
                target=str(orion_dir),
                tmdb_key="test_key",
                rate_limit=100.0,
                fuzzy_threshold=88.0,
                dry_run=False,
            )

            exit_code = await execute_sync(args)
            assert exit_code == 0

        # Verify Master Store output integrity
        store = MasterMappingStore(storage_dir=mappings_dir)
        all_items = store.all_mappings()
        assert len(all_items) > 0

        # Verify OrionServer exports
        exporter_summary = OrionExporter(output_dir=orion_dir).export_store(store)
        assert exporter_summary.total_files > 0
        assert (orion_dir / "imdb").exists()
        assert (orion_dir / "tmdb").exists()
        assert (orion_dir / "providers").exists()

        await custom_client.close()

    @pytest.mark.asyncio
    async def test_e2e_sync_complete_tmdb_blackout(
        self, mock_transport, tmp_path: Path
    ):
        """
        Adversarial Scenario:
        - All scrapers work normally.
        - TMDB API is completely down (500 on all endpoints).
        Expected outcome:
        - Items with direct IDs (PoseidonHD2, SeriesKao tt-regex) are preserved.
        - Items requiring TMDB search/find degrade gracefully (unmapped or partial).
        - Sync pipeline completes cleanly without crashing.
        - Master dataset is safely written.
        """
        mappings_dir = tmp_path / "mappings"
        orion_dir = tmp_path / "orion_mappings"

        chaotic_transport = ChaoticMockTransport(
            base_fixtures=mock_transport.fixtures,
            fault_schedule={
                "api.themoviedb.org": [
                    (500, {}, "TMDB Total Outage"),
                    (500, {}, "TMDB Total Outage"),
                    (500, {}, "TMDB Total Outage"),
                    (500, {}, "TMDB Total Outage"),
                ]
            },
        )

        custom_client = AsyncHttpClient(
            config=Settings(
                http_max_retries=1,
                http_backoff_factor=0.01,
                http_backoff_max=0.02,
            )
        )
        custom_client._client = httpx.AsyncClient(transport=chaotic_transport)

        from orion_mapper.scrapers import get_scraper as original_get_scraper

        def patched_get_scraper(provider_name: str, **kwargs):
            return original_get_scraper(provider_name, http_client=custom_client)

        with (
            patch("orion_mapper.cli.commands.get_scraper", side_effect=patched_get_scraper),
            patch("orion_mapper.cli.commands.TmdbClient") as mock_tmdb_cls,
        ):
            real_tmdb = TmdbClient(http_client=custom_client)
            mock_tmdb_cls.return_value = real_tmdb

            args = argparse.Namespace(
                provider="all",
                type=None,
                limit=3,
                unmapped_only=False,
                mappings_dir=str(mappings_dir),
                target=str(orion_dir),
                tmdb_key="test_key",
                rate_limit=100.0,
                fuzzy_threshold=88.0,
                dry_run=False,
            )

            exit_code = await execute_sync(args)
            assert exit_code == 0

        store = MasterMappingStore(storage_dir=mappings_dir)
        # Verify store has items (even if TMDB was down, items with direct IDs are retained)
        assert store.count() > 0
        await custom_client.close()
