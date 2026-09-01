from __future__ import annotations

import asyncio
import json
import threading
from typing import ClassVar

import httpx
import pytest

from orion_mapper.models.item import ContentType, ScrapedItem
from orion_mapper.scrapers import (
    AllCalidadScraper,
    BaseScraper,
    GnulaScraper,
    PoseidonHD2Scraper,
    SeriesKaoScraper,
    get_registered_providers,
    get_scraper,
    register_scraper,
    reset_registry,
)


@pytest.fixture(autouse=True)
def _ensure_clean_registry():
    reset_registry()
    yield
    reset_registry()


# ==============================================================================
# Helper Mock Transports for Adversarial Paging & Chaos
# ==============================================================================
class DynamicPagingTransport(httpx.AsyncBaseTransport):
    """
    Simulates dynamic paginated responses for any provider with customizable page count,
    error injection, empty page triggers, and malformed payload injection.
    """

    def __init__(
        self,
        provider: str,
        total_pages: int = 5,
        items_per_page: int = 4,
        error_on_page: int | None = None,
        error_status: int = 500,
        malformed_on_page: int | None = None,
        empty_on_page: int | None = None,
    ):
        self.provider = provider
        self.total_pages = total_pages
        self.items_per_page = items_per_page
        self.error_on_page = error_on_page
        self.error_status = error_status
        self.malformed_on_page = malformed_on_page
        self.empty_on_page = empty_on_page
        self.requested_pages: list[int] = []

    def _extract_page_from_request(self, request: httpx.Request) -> int:
        query_page = request.url.params.get("page")
        if query_page and query_page.isdigit():
            return int(query_page)
        import urllib.parse
        parsed = urllib.parse.parse_qs(
            request.url.query.decode("utf-8")
            if isinstance(request.url.query, bytes)
            else str(request.url.query)
        )
        if "page" in parsed and parsed["page"] and parsed["page"][0].isdigit():
            return int(parsed["page"][0])
        return 1

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        page = self._extract_page_from_request(request)
        self.requested_pages.append(page)

        if self.error_on_page is not None and page == self.error_on_page:
            return httpx.Response(self.error_status, text="Injected server error", request=request)

        if self.empty_on_page is not None and page >= self.empty_on_page:
            return self._build_empty_response(request)

        if page > self.total_pages:
            return self._build_empty_response(request)

        if self.malformed_on_page is not None and page == self.malformed_on_page:
            return httpx.Response(200, text="<broken><html>Not valid json or markup", request=request)

        return self._build_valid_page_response(page, request)

    def _build_empty_response(self, request: httpx.Request) -> httpx.Response:
        if self.provider == "allcalidad":
            return httpx.Response(200, json={"status": "success", "items": []}, request=request)
        elif self.provider in ("poseidonhd2", "gnula"):
            payload = {"props": {"pageProps": {"items": [], "posts": [], "data": []}}}
            html = f'<html><body><script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script></body></html>'
            return httpx.Response(200, text=html, request=request)
        else:  # serieskao
            return httpx.Response(200, text="<html><body><div class='item-list'></div></body></html>", request=request)

    def _build_valid_page_response(self, page: int, request: httpx.Request) -> httpx.Response:
        start_idx = (page - 1) * self.items_per_page
        item_indices = list(range(start_idx, start_idx + self.items_per_page))

        if self.provider == "allcalidad":
            items = [
                {
                    "slug": f"movie-{idx}",
                    "title": f"Movie {idx}",
                    "type": "movie",
                    "year": 2000 + (idx % 24),
                    "tmdb_id": 1000 + idx,
                    "imdb_id": f"tt{1000000 + idx}",
                    "poster": f"https://img.example.com/{idx}.jpg",
                }
                for idx in item_indices
            ]
            return httpx.Response(200, json={"status": "success", "items": items}, request=request)

        elif self.provider == "poseidonhd2":
            items = [
                {
                    "slug": f"movie-{idx}",
                    "title": f"Movie {idx}",
                    "type": "movie",
                    "year": 2000 + (idx % 24),
                    "TMDbId": 1000 + idx,
                    "IMDbId": f"tt{1000000 + idx}",
                    "poster": f"https://img.example.com/{idx}.jpg",
                }
                for idx in item_indices
            ]
            payload = {"props": {"pageProps": {"data": items}}}
            html = f'<html><body><script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script></body></html>'
            return httpx.Response(200, text=html, request=request)

        elif self.provider == "gnula":
            items = [
                {
                    "slug": f"pelicula-movie-{idx}",
                    "title": f"Movie {idx}",
                    "type": "movie",
                    "year": 2000 + (idx % 24),
                    "TMDbId": 1000 + idx,
                    "IMDbId": f"tt{1000000 + idx}",
                    "poster": f"https://img.example.com/{idx}.jpg",
                }
                for idx in item_indices
            ]
            payload = {"props": {"pageProps": {"posts": items}}}
            html = f'<html><body><script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script></body></html>'
            return httpx.Response(200, text=html, request=request)

        else:  # serieskao
            card_html = "".join(
                f"""
                <article class="item">
                    <a href="/pelicula/movie-{idx}" title="Movie {idx}">
                        <img src="https://img.example.com/{idx}.jpg" />
                        <h2 class="title">Movie {idx}</h2>
                        <span class="year">{2000 + (idx % 24)}</span>
                        <span class="type">Pelicula</span>
                    </a>
                </article>
                """
                for idx in item_indices
            )
            html = f"<html><body><div class='item-list'>{card_html}</div></body></html>"
            return httpx.Response(200, text=html, request=request)


# ==============================================================================
# 1. Scraper Registry Adversarial & Concurrency Tests
# ==============================================================================

def test_registry_multithreaded_high_concurrency():
    """
    Stress test: 20 concurrent OS threads rapidly registering, querying,
    listing, and resetting the registry.
    Verifies that dictionary mutation does not cause fatal race conditions.
    """
    errors: list[Exception] = []

    class DummyCustom(BaseScraper):
        name = "custom"
        base_url = "https://custom.test"
        async def fetch_catalog(self, *args, **kwargs): return []
        async def fetch_detail(self, *args, **kwargs): return None

    def worker(tid: int):
        try:
            for i in range(50):
                custom_name = f"provider_{tid}_{i}"
                register_scraper(custom_name, DummyCustom)
                providers = get_registered_providers()
                assert isinstance(providers, list)
                assert custom_name.lower() in providers

                scraper = get_scraper(custom_name, http_client=None)
                assert isinstance(scraper, DummyCustom)

                if i % 10 == 0:
                    reset_registry()
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Thread concurrency errors encountered: {errors}"
    reset_registry()


@pytest.mark.asyncio
async def test_registry_asyncio_concurrent_queries():
    """
    Stress test: 200 concurrent asyncio tasks acquiring scrapers and verifying consistency.
    """
    names = ["serieskao", "POSEIDONHD2", "  gnula  ", "AllCalidad", "poseidon", "series-kao", "all-calidad"]

    async def get_and_check(name: str):
        scraper = get_scraper(name, http_client=None)
        assert isinstance(scraper, BaseScraper)
        return scraper.name

    tasks = [get_and_check(names[i % len(names)]) for i in range(200)]
    results = await asyncio.gather(*tasks)
    assert len(results) == 200
    assert set(results) == {"serieskao", "poseidonhd2", "gnula", "allcalidad"}


def test_registry_alias_shadowing_behavior():
    """
    Adversarial test: Registering a custom scraper with an alias name (e.g. 'poseidon').
    Observe that _PROVIDER_ALIASES resolves 'poseidon' -> 'poseidonhd2', thus shadowing
    the explicitly registered 'poseidon' unless 'poseidonhd2' is registered instead.
    """
    class CustomPoseidon(BaseScraper):
        name = "custom_poseidon"
        base_url = "https://customposeidon.test"
        async def fetch_catalog(self, *args, **kwargs): return []
        async def fetch_detail(self, *args, **kwargs): return None

    # Register under alias name
    register_scraper("poseidon", CustomPoseidon)

    # get_scraper resolves 'poseidon' -> 'poseidonhd2' via _PROVIDER_ALIASES line 59
    # which points to PoseidonHD2Scraper in _SCRAPER_REGISTRY
    scraper = get_scraper("poseidon", http_client=None)
    assert isinstance(scraper, PoseidonHD2Scraper), (
        "Alias resolution shadows custom registration under alias key 'poseidon'"
    )

    # Overriding canonical name 'poseidonhd2'
    register_scraper("poseidonhd2", CustomPoseidon)
    scraper_overridden = get_scraper("poseidon", http_client=None)
    assert isinstance(scraper_overridden, CustomPoseidon)

    reset_registry()


def test_registry_invalid_and_empty_names():
    """
    Adversarial test: Passing invalid/empty provider names to get_scraper and register_scraper.
    """
    # get_scraper with invalid types
    for bad_input in [None, 123, 45.6, [], {}, True]:
        with pytest.raises(ValueError, match="Provider name must be a string"):
            get_scraper(bad_input, http_client=None)  # type: ignore

    # get_scraper with empty/whitespace strings
    for bad_name in ["", "   ", "\t\n  \r"]:
        with pytest.raises(ValueError, match="Empty provider name provided"):
            get_scraper(bad_name, http_client=None)

    # Unknown provider
    with pytest.raises(ValueError, match="Unknown provider 'unregistered_provider'"):
        get_scraper("unregistered_provider", http_client=None)


def test_registry_register_non_subclass_behavior():
    """
    Adversarial test: Registering non-subclasses of BaseScraper or classes missing required attributes.
    """
    class NotASubclass:
        def __init__(self, *args, **kwargs):
            self.name = "not_a_subclass"

    # Registering non-subclass (currently allowed by register_scraper without validation)
    register_scraper("not_a_subclass", NotASubclass)  # type: ignore
    assert "not_a_subclass" in get_registered_providers()

    inst = get_scraper("not_a_subclass", http_client=None)
    assert isinstance(inst, NotASubclass)

    # Registering a subclass of BaseScraper missing required 'name' or 'base_url'
    class BrokenScraper(BaseScraper):
        async def fetch_catalog(self, *args, **kwargs): return []
        async def fetch_detail(self, *args, **kwargs): return None

    register_scraper("broken", BrokenScraper)
    with pytest.raises(ValueError, match="must define 'name'"):
        get_scraper("broken", http_client=None)


def test_registry_abstract_class_registration_instantiation_failure():
    """
    Adversarial test: Registering an abstract subclass of BaseScraper that leaves methods unimplemented.
    """
    class AbstractScraper(BaseScraper):
        name = "abstract_provider"
        base_url = "https://abstract.test"
        # fetch_catalog and fetch_detail not implemented

    register_scraper("abstract", AbstractScraper)  # type: ignore
    with pytest.raises(TypeError, match="Can't instantiate abstract class"):
        get_scraper("abstract", http_client=None)


def test_base_scraper_missing_base_url():
    """
    Test that a scraper with a name but missing base_url raises ValueError upon initialization.
    """
    class NoBaseUrlScraper(BaseScraper):
        name = "no_url"
        async def fetch_catalog(self, *args, **kwargs): return []
        async def fetch_detail(self, *args, **kwargs): return None

    with pytest.raises(ValueError, match="must define 'base_url'"):
        NoBaseUrlScraper(http_client=None)  # type: ignore


# ==============================================================================
# 2. crawl_catalog Pagination Edge Cases Across All 4 Scrapers
# ==============================================================================

@pytest.mark.asyncio
@pytest.mark.parametrize("provider_name,scraper_cls", [
    ("serieskao", SeriesKaoScraper),
    ("poseidonhd2", PoseidonHD2Scraper),
    ("gnula", GnulaScraper),
    ("allcalidad", AllCalidadScraper),
])
async def test_crawl_catalog_max_pages_zero_and_negative(provider_name: str, scraper_cls: type[BaseScraper]):
    """
    Boundary test: crawl_catalog with max_pages=0 and max_pages=-1.
    Should terminate immediately and yield 0 items without issuing HTTP requests.
    """
    transport = DynamicPagingTransport(provider_name, total_pages=5)
    client = httpx.AsyncClient(transport=transport)
    scraper = scraper_cls(http_client=client)

    # max_pages = 0
    items_zero: list[ScrapedItem] = []
    async for item in scraper.crawl_catalog(ContentType.MOVIE, max_pages=0):
        items_zero.append(item)
    assert len(items_zero) == 0
    assert len(transport.requested_pages) == 0

    # max_pages = -5
    items_neg: list[ScrapedItem] = []
    async for item in scraper.crawl_catalog(ContentType.MOVIE, max_pages=-5):
        items_neg.append(item)
    assert len(items_neg) == 0
    assert len(transport.requested_pages) == 0

    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_name,scraper_cls", [
    ("serieskao", SeriesKaoScraper),
    ("poseidonhd2", PoseidonHD2Scraper),
    ("gnula", GnulaScraper),
    ("allcalidad", AllCalidadScraper),
])
async def test_crawl_catalog_max_pages_exact_cutoff(provider_name: str, scraper_cls: type[BaseScraper]):
    """
    Boundary test: crawl_catalog with max_pages=1, max_pages=3 on a catalog of 10 pages.
    Verifies that iteration terminates exactly when max_pages is reached.
    """
    transport = DynamicPagingTransport(provider_name, total_pages=10, items_per_page=5)
    client = httpx.AsyncClient(transport=transport)
    scraper = scraper_cls(http_client=client)

    # max_pages = 1
    items_1: list[ScrapedItem] = []
    async for item in scraper.crawl_catalog(ContentType.MOVIE, max_pages=1):
        items_1.append(item)
    assert len(items_1) == 5
    assert transport.requested_pages == [1]

    # max_pages = 3
    transport.requested_pages.clear()
    items_3: list[ScrapedItem] = []
    async for item in scraper.crawl_catalog(ContentType.MOVIE, max_pages=3):
        items_3.append(item)
    assert len(items_3) == 15
    assert transport.requested_pages == [1, 2, 3]

    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_name,scraper_cls", [
    ("serieskao", SeriesKaoScraper),
    ("poseidonhd2", PoseidonHD2Scraper),
    ("gnula", GnulaScraper),
    ("allcalidad", AllCalidadScraper),
])
async def test_crawl_catalog_natural_end_of_catalog(provider_name: str, scraper_cls: type[BaseScraper]):
    """
    Natural termination test: Catalog has 3 pages; page 4 returns empty list.
    crawl_catalog(max_pages=None) should fetch all 3 pages and terminate gracefully at page 4.
    """
    transport = DynamicPagingTransport(provider_name, total_pages=3, items_per_page=4)
    client = httpx.AsyncClient(transport=transport)
    scraper = scraper_cls(http_client=client)

    items: list[ScrapedItem] = []
    async for item in scraper.crawl_catalog(ContentType.MOVIE, max_pages=None):
        items.append(item)

    assert len(items) == 12
    assert transport.requested_pages == [1, 2, 3, 4]

    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_name,scraper_cls", [
    ("serieskao", SeriesKaoScraper),
    ("poseidonhd2", PoseidonHD2Scraper),
    ("gnula", GnulaScraper),
    ("allcalidad", AllCalidadScraper),
])
async def test_crawl_catalog_empty_first_page(provider_name: str, scraper_cls: type[BaseScraper]):
    """
    Edge case: Page 1 itself is empty.
    crawl_catalog should terminate after 1 request and yield 0 items.
    """
    transport = DynamicPagingTransport(provider_name, total_pages=0, items_per_page=0, empty_on_page=1)
    client = httpx.AsyncClient(transport=transport)
    scraper = scraper_cls(http_client=client)

    items: list[ScrapedItem] = []
    async for item in scraper.crawl_catalog(ContentType.MOVIE):
        items.append(item)

    assert len(items) == 0
    assert transport.requested_pages == [1]

    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_name,scraper_cls", [
    ("serieskao", SeriesKaoScraper),
    ("poseidonhd2", PoseidonHD2Scraper),
    ("gnula", GnulaScraper),
    ("allcalidad", AllCalidadScraper),
])
async def test_crawl_catalog_server_error_resilience(provider_name: str, scraper_cls: type[BaseScraper]):
    """
    Fault tolerance test: Server returns HTTP 500 / 503 on page 3.
    crawl_catalog should yield items from pages 1 and 2, log error, and terminate without throwing.
    """
    transport = DynamicPagingTransport(
        provider_name,
        total_pages=5,
        items_per_page=3,
        error_on_page=3,
        error_status=500,
    )
    client = httpx.AsyncClient(transport=transport)
    scraper = scraper_cls(http_client=client)

    items: list[ScrapedItem] = []
    async for item in scraper.crawl_catalog(ContentType.MOVIE):
        items.append(item)

    assert len(items) == 6  # Page 1 (3 items) + Page 2 (3 items)
    assert transport.requested_pages == [1, 2, 3]

    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_name,scraper_cls", [
    ("serieskao", SeriesKaoScraper),
    ("poseidonhd2", PoseidonHD2Scraper),
    ("gnula", GnulaScraper),
    ("allcalidad", AllCalidadScraper),
])
async def test_crawl_catalog_malformed_html_or_json_on_page_2(provider_name: str, scraper_cls: type[BaseScraper]):
    """
    Malformed response test: Page 2 returns invalid JSON or broken HTML.
    crawl_catalog should gracefully handle parsing failure on page 2 and terminate.
    """
    transport = DynamicPagingTransport(
        provider_name,
        total_pages=5,
        items_per_page=4,
        malformed_on_page=2,
    )
    client = httpx.AsyncClient(transport=transport)
    scraper = scraper_cls(http_client=client)

    items: list[ScrapedItem] = []
    async for item in scraper.crawl_catalog(ContentType.MOVIE):
        items.append(item)

    assert len(items) == 4  # Page 1 succeeded (4 items), Page 2 returned empty/error and stopped
    assert transport.requested_pages == [1, 2]

    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_name,scraper_cls", [
    ("serieskao", SeriesKaoScraper),
    ("poseidonhd2", PoseidonHD2Scraper),
    ("gnula", GnulaScraper),
    ("allcalidad", AllCalidadScraper),
])
async def test_crawl_catalog_early_consumer_break(provider_name: str, scraper_cls: type[BaseScraper]):
    """
    Generator resource test: Consumer breaks early after consuming 2 items.
    Generator should close cleanly without executing further page requests.
    """
    transport = DynamicPagingTransport(provider_name, total_pages=10, items_per_page=5)
    client = httpx.AsyncClient(transport=transport)
    scraper = scraper_cls(http_client=client)

    items: list[ScrapedItem] = []
    async for item in scraper.crawl_catalog(ContentType.MOVIE, max_pages=10):
        items.append(item)
        if len(items) == 2:
            break

    assert len(items) == 2
    assert transport.requested_pages == [1]

    await client.aclose()


@pytest.mark.asyncio
async def test_crawl_catalog_unsupported_content_type():
    """
    Test crawl_catalog with an unsupported ContentType.
    Should log warning and yield 0 items.
    """
    class MovieOnlyScraper(BaseScraper):
        name = "movie_only"
        base_url = "https://movie.test"
        supported_types: ClassVar[list[ContentType]] = [ContentType.MOVIE]

        async def fetch_catalog(self, *args, **kwargs):
            return [ScrapedItem(provider="movie_only", slug="test", title="Test", type=ContentType.MOVIE)]

        async def fetch_detail(self, *args, **kwargs):
            return None

    scraper = MovieOnlyScraper(http_client=None)  # type: ignore
    items: list[ScrapedItem] = []
    async for item in scraper.crawl_catalog(ContentType.SERIES):
        items.append(item)

    assert len(items) == 0


@pytest.mark.asyncio
async def test_serieskao_anime_genre_pagination_urls():
    """
    SeriesKao specific test: Verify pagination URL routing for anime genre vs series.
    - Page 1 anime: /animes/populares
    - Page 2 anime: /animes?page=2
    - Page 1 series: /series
    - Page 2 series: /series?page=2
    """
    captured_urls: list[str] = []

    class UrlCaptureTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            captured_urls.append(str(request.url))
            if len(captured_urls) > 3:
                return httpx.Response(200, text="<html><body></body></html>", request=request)
            card = '<article class="item"><a href="/serie/item-1"><h2 class="title">Item 1</h2></a></article>'
            return httpx.Response(200, text=f"<html><body>{card}</body></html>", request=request)

    client = httpx.AsyncClient(transport=UrlCaptureTransport())
    scraper = SeriesKaoScraper(http_client=client)

    # Anime genre crawl
    items_anime: list[ScrapedItem] = []
    async for item in scraper.crawl_catalog(ContentType.SERIES, max_pages=2, genre="anime"):
        items_anime.append(item)

    assert any("/animes/populares" in u for u in captured_urls)
    assert any("/animes?page=2" in u for u in captured_urls)

    captured_urls.clear()

    # Standard series crawl
    items_series: list[ScrapedItem] = []
    async for item in scraper.crawl_catalog(ContentType.SERIES, max_pages=2):
        items_series.append(item)

    assert any("/series" in u and "page=" not in u for u in captured_urls)
    assert any("/series?page=2" in u for u in captured_urls)

    await client.aclose()


@pytest.mark.asyncio
async def test_allcalidad_catalog_corrupted_items_filtering():
    """
    Adversarial test: AllCalidad catalog payload containing various corrupt items
    (None items, non-dict items, items missing slug, invalid year formats).
    Verify that invalid items are safely skipped and valid items are yielded.
    """
    corrupt_listing = {
        "status": "success",
        "items": [
            None,
            "not_a_dict",
            12345,
            {"title": "No Slug Movie"},  # missing slug
            {"slug": "", "title": "Empty Slug"},  # empty slug
            {"slug": "valid-1", "title": "Valid 1", "year": "invalid_year", "type": "movie"},
            {"slug": "valid-2", "title": "Valid 2", "year": " 2022 ", "type": "series"},
            {"slug": "valid-3", "title": "Valid 3", "release_date": "2024-05-10", "type": "movie"},
        ],
    }

    class StaticJsonTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            page = request.url.params.get("page", "1")
            if page == "1":
                return httpx.Response(200, json=corrupt_listing, request=request)
            return httpx.Response(200, json={"status": "success", "items": []}, request=request)

    client = httpx.AsyncClient(transport=StaticJsonTransport())
    scraper = AllCalidadScraper(http_client=client)

    items: list[ScrapedItem] = []
    async for item in scraper.crawl_catalog(ContentType.MOVIE):
        items.append(item)

    assert len(items) == 3
    assert items[0].slug == "valid-1"
    assert items[0].year is None
    assert items[1].slug == "valid-2"
    assert items[1].year == 2022
    assert items[1].type == ContentType.SERIES
    assert items[2].slug == "valid-3"
    assert items[2].year == 2024

    await client.aclose()


@pytest.mark.asyncio
async def test_poseidonhd2_and_gnula_corrupted_next_data_items():
    """
    Adversarial test: PoseidonHD2 and Gnula __NEXT_DATA__ payloads containing non-list or corrupt arrays.
    Verify resilience and graceful degradation.
    """
    for ScraperCls in (PoseidonHD2Scraper, GnulaScraper):
        payload_non_list = {"props": {"pageProps": {"data": "not_a_list", "posts": 12345}}}
        html_non_list = f'<html><body><script id="__NEXT_DATA__" type="application/json">{json.dumps(payload_non_list)}</script></body></html>'

        class BadPropsTransport(httpx.AsyncBaseTransport):
            def __init__(self, html: str) -> None:
                self.html = html

            async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                return httpx.Response(200, text=self.html, request=request)

        client = httpx.AsyncClient(transport=BadPropsTransport(html_non_list))
        scraper = ScraperCls(http_client=client)

        items = await scraper.fetch_catalog(ContentType.MOVIE, page=1)
        assert items == []
        await client.aclose()


@pytest.mark.asyncio
async def test_high_volume_pagination_stream():
    """
    Stress test: Stream 20 pages with 10 items per page (200 total items)
    through AllCalidadScraper.crawl_catalog to ensure no memory leak or performance degradation.
    """
    transport = DynamicPagingTransport("allcalidad", total_pages=20, items_per_page=10)
    client = httpx.AsyncClient(transport=transport)
    scraper = AllCalidadScraper(http_client=client)

    items: list[ScrapedItem] = []
    async for item in scraper.crawl_catalog(ContentType.MOVIE):
        items.append(item)

    assert len(items) == 200
    assert transport.requested_pages == list(range(1, 22))
    await client.aclose()
