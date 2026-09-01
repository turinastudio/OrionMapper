from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from orion_mapper.models.item import ContentType
from orion_mapper.scrapers import (
    AllCalidadScraper,
    BaseScraper,
    GnulaScraper,
    PoseidonHD2Scraper,
    PoseidonScraper,
    SeriesKaoScraper,
    get_registered_providers,
    get_scraper,
    list_scrapers,
    register_scraper,
    reset_registry,
)


@pytest.fixture(autouse=True)
def _ensure_clean_registry():
    reset_registry()
    yield
    reset_registry()



# ==============================================================================
# Helper Mock Transports
# ==============================================================================
class StaticResponseTransport(httpx.AsyncBaseTransport):
    """Transport that returns predetermined static responses based on path matching."""

    def __init__(self, responses: dict[str, tuple[int, str | dict[str, Any]]]):
        self.responses = responses

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        path = request.url.path

        for match_key, (status_code, body) in self.responses.items():
            if match_key in url_str or match_key in path:
                if isinstance(body, dict):
                    return httpx.Response(status_code, json=body, request=request)
                return httpx.Response(status_code, text=body, request=request)

        return httpx.Response(404, text="Not Found", request=request)


# ==============================================================================
# SeriesKao Scraper Unit Tests
# ==============================================================================
@pytest.mark.asyncio
async def test_serieskao_catalog_movies_page_1(mock_http_client):
    scraper = SeriesKaoScraper(http_client=mock_http_client)
    items = await scraper.fetch_catalog(ContentType.MOVIE, page=1)

    assert len(items) == 4
    assert items[0].provider == "serieskao"
    assert items[0].slug == "el-club-de-la-lucha"
    assert items[0].title == "El Club de la Lucha"
    assert items[0].type == ContentType.MOVIE
    assert items[0].year == 1999

    assert items[1].slug == "zombieland-saga"
    assert items[1].type == ContentType.SERIES
    assert items[1].year == 2018


@pytest.mark.asyncio
async def test_serieskao_catalog_current_card_layout():
    html = """
    <section class="grid grid--cards">
        <article class="card">
            <a href="/pelicula/current-movie/" class="card__link">
                <img data-src="/images/current.jpg" alt="Current Movie">
                <span class="card__badge card__badge--year">2026</span>
                <h2 class="card__title">Current Movie</h2>
            </a>
        </article>
        <article class="card">
            <a href="/pelicula/second-movie/" class="card__link">
                <img src="https://image.tmdb.org/second.jpg" alt="Second Movie">
                <span class="card__badge--year">2025</span>
                <h2 class="card__title">Second Movie</h2>
            </a>
        </article>
    </section>
    """
    client = httpx.AsyncClient(
        transport=StaticResponseTransport({"/peliculas": (200, html)})
    )
    scraper = SeriesKaoScraper(http_client=client)

    items = await scraper.fetch_catalog(ContentType.MOVIE)

    assert [(item.slug, item.title, item.year) for item in items] == [
        ("current-movie", "Current Movie", 2026),
        ("second-movie", "Second Movie", 2025),
    ]
    assert items[0].poster_url == "https://serieskao.top/images/current.jpg"
    await client.aclose()


@pytest.mark.asyncio
async def test_serieskao_catalog_series_and_anime(mock_http_client):
    scraper = SeriesKaoScraper(http_client=mock_http_client)
    items_series = await scraper.fetch_catalog(ContentType.SERIES, page=1)
    assert len(items_series) > 0

    items_anime = await scraper.fetch_catalog(ContentType.SERIES, page=1, genre="anime")
    assert len(items_anime) > 0


@pytest.mark.asyncio
async def test_serieskao_detail_json_ld_movie(mock_http_client):
    scraper = SeriesKaoScraper(http_client=mock_http_client)
    detail = await scraper.fetch_detail("el-club-de-la-lucha", ContentType.MOVIE)

    assert detail is not None
    assert detail.provider == "serieskao"
    assert detail.slug == "el-club-de-la-lucha"
    assert detail.title == "El Club de la Lucha"
    assert detail.type == ContentType.MOVIE
    assert detail.year == 1999
    assert detail.imdb_id == "tt0137523"
    assert "Drama" in detail.genres
    assert detail.release_date == "1999-10-15"


@pytest.mark.asyncio
async def test_serieskao_detail_json_ld_series(mock_http_client):
    scraper = SeriesKaoScraper(http_client=mock_http_client)
    detail = await scraper.fetch_detail("zombieland-saga", ContentType.SERIES)

    assert detail is not None
    assert detail.slug == "zombieland-saga"
    assert detail.title == "Zombieland Saga"
    assert detail.type == ContentType.SERIES
    assert detail.year == 2018
    assert detail.imdb_id == "tt15486"
    assert "Animación" in detail.genres


@pytest.mark.asyncio
async def test_serieskao_series_detail_resolves_imdb_from_first_episode_player():
    detail_html = """
    <html><body>
        <script type="application/ld+json">
          {"@type":"TVSeries","name":"A Series","datePublished":2026}
        </script>
        <a class="episode-item" href="/serie/a-series/temporada/1/capitulo/1">
            <span class="episode-item__title">Episode 1</span>
        </a>
    </body></html>
    """
    episode_html = '<button class="server-btn" data-url="/vidurl/tt1234567/"></button>'
    responses = {
        "/serie/a-series": (200, detail_html),
        "/serie/a-series/temporada/1/capitulo/1": (200, episode_html),
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        body = responses.get(request.url.path)
        if body is None:
            return httpx.Response(404, request=request)
        return httpx.Response(body[0], text=body[1], request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    scraper = SeriesKaoScraper(http_client=client)

    detail = await scraper.fetch_detail("a-series", ContentType.SERIES)

    assert detail is not None
    assert detail.imdb_id == "tt1234567"
    assert len(detail.episodes) == 1
    assert detail.episodes[0].season == 1
    assert detail.episodes[0].episode == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_serieskao_detail_player_iframe_fallback():
    html = """
    <html>
    <body>
        <div class="movie-info">
            <h1 class="title">Fight Club Regex Only</h1>
            <span class="release-year">1999</span>
            <iframe src="https://player.serieskao.top/vidurl/tt0137523/sub"></iframe>
            <div class="overview">A ticking-time-bomb insomniac...</div>
        </div>
    </body>
    </html>
    """
    client = httpx.AsyncClient(
        transport=StaticResponseTransport({"/pelicula/fight-club-regex": (200, html)})
    )
    scraper = SeriesKaoScraper(http_client=client)
    detail = await scraper.fetch_detail("fight-club-regex", ContentType.MOVIE)

    assert detail is not None
    assert detail.title == "Fight Club Regex Only"
    assert detail.year == 1999
    assert detail.imdb_id == "tt0137523"
    assert detail.overview == "A ticking-time-bomb insomniac..."
    await client.aclose()


@pytest.mark.asyncio
async def test_serieskao_extract_identifiers():
    scraper = SeriesKaoScraper(http_client=None)
    imdb, tmdb = scraper.extract_identifiers("https://player.serieskao.top/vidurl/tt1234567/sub")
    assert imdb == "tt1234567"
    assert tmdb is None

    imdb2, tmdb2 = scraper.extract_identifiers({"identifier": "tt7654321"})
    assert imdb2 == "tt7654321"
    assert tmdb2 is None

    imdb3, tmdb3 = scraper.extract_identifiers({"identifier": "not_an_imdb"})
    assert imdb3 is None
    assert tmdb3 is None


@pytest.mark.asyncio
async def test_serieskao_detail_404_returns_none(mock_http_client):
    scraper = SeriesKaoScraper(http_client=mock_http_client)
    detail = await scraper.fetch_detail("item-999999999", ContentType.MOVIE)
    assert detail is None


@pytest.mark.asyncio
async def test_serieskao_catalog_404_returns_empty_list():
    client = httpx.AsyncClient(transport=StaticResponseTransport({}))
    scraper = SeriesKaoScraper(http_client=client)
    items = await scraper.fetch_catalog(ContentType.MOVIE, page=99)
    assert items == []
    await client.aclose()


@pytest.mark.asyncio
async def test_serieskao_corrupted_json_ld_resilience():
    html = """
    <html>
    <body>
        <script type="application/ld+json">{ broken json </script>
        <h1>Movie Title</h1>
        <span class="year">2021</span>
        <iframe src="https://player.serieskao.top/vidurl/tt5555555/latino"></iframe>
    </body>
    </html>
    """
    client = httpx.AsyncClient(
        transport=StaticResponseTransport({"/pelicula/corrupted": (200, html)})
    )
    scraper = SeriesKaoScraper(http_client=client)
    detail = await scraper.fetch_detail("corrupted", ContentType.MOVIE)

    assert detail is not None
    assert detail.title == "Movie Title"
    assert detail.imdb_id == "tt5555555"
    assert detail.year == 2021
    await client.aclose()


# ==============================================================================
# PoseidonHD2 Scraper Unit Tests
# ==============================================================================
@pytest.mark.asyncio
async def test_poseidon_catalog_movies_and_series(mock_http_client):
    scraper = PoseidonHD2Scraper(http_client=mock_http_client)
    items = await scraper.fetch_catalog(ContentType.MOVIE, page=1)

    assert len(items) == 2
    assert items[0].provider == "poseidonhd2"
    assert items[0].slug == "el-club-de-la-lucha"
    assert items[0].title == "El Club de la Lucha"
    assert items[0].type == ContentType.MOVIE
    assert items[0].tmdb_id == "550"
    assert items[0].imdb_id == "tt0137523"

    assert items[1].slug == "zombieland-saga"
    assert items[1].type == ContentType.SERIES
    assert items[1].tmdb_id == "82856"
    assert items[1].imdb_id == "tt15486"


@pytest.mark.asyncio
async def test_poseidon_detail_movie_extraction(mock_http_client):
    scraper = PoseidonHD2Scraper(http_client=mock_http_client)
    detail = await scraper.fetch_detail("el-club-de-la-lucha", ContentType.MOVIE)

    assert detail is not None
    assert detail.provider == "poseidonhd2"
    assert detail.slug == "el-club-de-la-lucha"
    assert detail.title == "El Club de la Lucha"
    assert detail.original_title == "Fight Club"
    assert detail.type == ContentType.MOVIE
    assert detail.year == 1999
    assert detail.tmdb_id == "550"
    assert detail.imdb_id == "tt0137523"
    assert "empleado de oficina" in str(detail.overview)


@pytest.mark.asyncio
async def test_poseidon_detail_series_extraction(mock_http_client):
    scraper = PoseidonHD2Scraper(http_client=mock_http_client)
    detail = await scraper.fetch_detail("zombieland-saga", ContentType.SERIES)

    assert detail is not None
    assert detail.slug == "zombieland-saga"
    assert detail.type == ContentType.SERIES
    assert detail.year == 2018
    assert detail.tmdb_id == "82856"
    assert detail.imdb_id == "tt15486"


@pytest.mark.asyncio
async def test_poseidon_tv_type_normalization():
    scraper = PoseidonHD2Scraper(http_client=None)
    assert scraper.name == "poseidonhd2"
    assert PoseidonScraper is PoseidonHD2Scraper


@pytest.mark.asyncio
async def test_poseidon_extract_identifiers():
    scraper = PoseidonHD2Scraper(http_client=None)
    imdb, tmdb = scraper.extract_identifiers({"TMDbId": "550", "IMDbId": "tt0137523"})
    assert imdb == "tt0137523"
    assert tmdb == "550"

    imdb2, tmdb2 = scraper.extract_identifiers({"tmdb_id": 82856, "imdb_id": "tt15486"})
    assert imdb2 == "tt15486"
    assert tmdb2 == "82856"


@pytest.mark.asyncio
async def test_poseidon_missing_next_data_handled():
    html = "<html><body><h1>Empty Page</h1></body></html>"
    client = httpx.AsyncClient(
        transport=StaticResponseTransport({"/movies": (200, html), "/pelicula/empty": (200, html)})
    )
    scraper = PoseidonHD2Scraper(http_client=client)

    catalog = await scraper.fetch_catalog(ContentType.MOVIE)
    assert catalog == []

    detail = await scraper.fetch_detail("empty", ContentType.MOVIE)
    assert detail is None
    await client.aclose()


@pytest.mark.asyncio
async def test_poseidon_null_identifiers_handled():
    payload = {
        "props": {
            "pageProps": {
                "data": {
                    "slug": "indie-movie",
                    "title": "Indie Movie",
                    "year": 2020,
                    "type": "movie",
                    "TMDbId": None,
                    "IMDbId": None,
                }
            }
        }
    }
    html = f'<html><body><script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script></body></html>'
    client = httpx.AsyncClient(
        transport=StaticResponseTransport({"/pelicula/indie-movie": (200, html)})
    )
    scraper = PoseidonHD2Scraper(http_client=client)
    detail = await scraper.fetch_detail("indie-movie", ContentType.MOVIE)

    assert detail is not None
    assert detail.tmdb_id is None
    assert detail.imdb_id is None
    assert detail.year == 2020
    await client.aclose()


@pytest.mark.asyncio
async def test_poseidon_detail_404_returns_none(mock_http_client):
    scraper = PoseidonHD2Scraper(http_client=mock_http_client)
    detail = await scraper.fetch_detail("item-999999999", ContentType.MOVIE)
    assert detail is None


# ==============================================================================
# Gnula Scraper Unit Tests
# ==============================================================================
@pytest.mark.asyncio
async def test_gnula_catalog_parsing(mock_http_client):
    scraper = GnulaScraper(http_client=mock_http_client)
    items = await scraper.fetch_catalog(ContentType.MOVIE, page=1)

    assert len(items) == 2
    assert items[0].provider == "gnula"
    assert items[0].slug == "pelicula-el-club-de-la-lucha"
    assert items[0].title == "El Club de la Lucha"
    assert items[0].type == ContentType.MOVIE
    assert items[0].tmdb_id == "550"
    assert items[0].imdb_id == "tt0137523"

    assert items[1].slug == "serie-zombieland-saga"
    assert items[1].type == ContentType.SERIES
    assert items[1].tmdb_id == "82856"
    assert items[1].imdb_id == "tt15486"


@pytest.mark.asyncio
async def test_gnula_detail_movie_extraction(mock_http_client):
    scraper = GnulaScraper(http_client=mock_http_client)
    detail = await scraper.fetch_detail("pelicula-el-club-de-la-lucha", ContentType.MOVIE)

    assert detail is not None
    assert detail.provider == "gnula"
    assert detail.slug == "pelicula-el-club-de-la-lucha"
    assert detail.title == "El Club de la Lucha"
    assert detail.type == ContentType.MOVIE
    assert detail.year == 1999
    assert detail.tmdb_id == "550"
    assert detail.imdb_id == "tt0137523"
    assert "hastiado de su gris" in str(detail.overview)


@pytest.mark.asyncio
async def test_gnula_detail_series_extraction(mock_http_client):
    scraper = GnulaScraper(http_client=mock_http_client)
    detail = await scraper.fetch_detail("serie-zombieland-saga", ContentType.SERIES)

    assert detail is not None
    assert detail.slug == "serie-zombieland-saga"
    assert detail.type == ContentType.SERIES
    assert detail.year == 2018
    assert detail.tmdb_id == "82856"
    assert detail.imdb_id == "tt15486"


@pytest.mark.asyncio
async def test_gnula_slug_candidate_probing():
    payload = {
        "props": {
            "pageProps": {
                "post": {
                    "slug": "pelicula-matrix",
                    "title": "The Matrix",
                    "year": " 1999 ",
                    "type": "movie",
                    "TMDbId": 603,
                    "IMDbId": "tt0133093",
                }
            }
        }
    }
    html = f'<html><body><script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script></body></html>'

    # The server responds 200 ONLY on /pelicula/pelicula-matrix
    client = httpx.AsyncClient(
        transport=StaticResponseTransport({"/pelicula/pelicula-matrix": (200, html)})
    )
    scraper = GnulaScraper(http_client=client)

    # Calling with unprefixed "matrix" should probe candidate paths and resolve successfully
    detail = await scraper.fetch_detail("matrix", ContentType.MOVIE)

    assert detail is not None
    assert detail.slug == "pelicula-matrix"
    assert detail.title == "The Matrix"
    assert detail.year == 1999
    assert detail.tmdb_id == "603"
    assert detail.imdb_id == "tt0133093"
    await client.aclose()


@pytest.mark.asyncio
async def test_gnula_extract_identifiers():
    scraper = GnulaScraper(http_client=None)
    imdb, tmdb = scraper.extract_identifiers({"TMDbId": 550, "IMDbId": "tt0137523"})
    assert imdb == "tt0137523"
    assert tmdb == "550"


@pytest.mark.asyncio
async def test_gnula_detail_404_returns_none(mock_http_client):
    scraper = GnulaScraper(http_client=mock_http_client)
    detail = await scraper.fetch_detail("item-999999999", ContentType.MOVIE)
    assert detail is None


# ==============================================================================
# AllCalidad Scraper Unit Tests
# ==============================================================================
@pytest.mark.asyncio
async def test_allcalidad_listing_parsing(mock_http_client):
    scraper = AllCalidadScraper(http_client=mock_http_client)
    items = await scraper.fetch_catalog(ContentType.MOVIE, page=1)

    assert len(items) == 2
    assert items[0].provider == "allcalidad"
    assert items[0].slug == "el-club-de-la-lucha"
    assert items[0].title == "El Club de la Lucha"
    assert items[0].type == ContentType.MOVIE
    assert items[0].year == 1999
    assert items[0].tmdb_id == "550"
    assert items[0].imdb_id == "tt0137523"

    assert items[1].slug == "zombieland-saga"
    assert items[1].type == ContentType.SERIES
    assert items[1].year == 2018
    assert items[1].tmdb_id == "82856"
    assert items[1].imdb_id == "tt15486"


@pytest.mark.asyncio
async def test_allcalidad_single_movie_parsing(mock_http_client):
    scraper = AllCalidadScraper(http_client=mock_http_client)
    detail = await scraper.fetch_detail("el-club-de-la-lucha", ContentType.MOVIE)

    assert detail is not None
    assert detail.provider == "allcalidad"
    assert detail.slug == "el-club-de-la-lucha"
    assert detail.title == "El Club de la Lucha"
    assert detail.original_title == "Fight Club"
    assert detail.type == ContentType.MOVIE
    assert detail.year == 1999
    assert detail.tmdb_id == "550"
    assert detail.imdb_id == "tt0137523"
    assert detail.release_date == "1999-10-15"


@pytest.mark.asyncio
async def test_allcalidad_single_series_parsing(mock_http_client):
    scraper = AllCalidadScraper(http_client=mock_http_client)
    detail = await scraper.fetch_detail("zombieland-saga", ContentType.SERIES)

    assert detail is not None
    assert detail.slug == "zombieland-saga"
    assert detail.title == "Zombieland Saga"
    assert detail.type == ContentType.SERIES
    assert detail.year == 2018
    assert detail.tmdb_id == "82856"
    assert detail.imdb_id == "tt15486"


@pytest.mark.asyncio
async def test_allcalidad_release_date_to_year_conversion():
    listing = {
        "status": "success",
        "items": [
            {
                "slug": "oppenheimer",
                "title": "Oppenheimer",
                "type": "movie",
                "release_date": "2023-07-21",
                "tmdb_id": 872585,
                "imdb_id": "tt15398776",
            }
        ],
    }
    client = httpx.AsyncClient(
        transport=StaticResponseTransport({"/api/rest/listing": (200, listing)})
    )
    scraper = AllCalidadScraper(http_client=client)
    items = await scraper.fetch_catalog(ContentType.MOVIE)

    assert len(items) == 1
    assert items[0].year == 2023
    assert items[0].tmdb_id == "872585"
    assert items[0].imdb_id == "tt15398776"
    await client.aclose()


@pytest.mark.asyncio
async def test_allcalidad_error_status_handled():
    client = httpx.AsyncClient(
        transport=StaticResponseTransport(
            {
                "/api/rest/listing": (200, {"status": "error", "message": "Failed"}),
                "/api/rest/single": (200, {"status": "error", "message": "Not found"}),
                "/api/rest/movie/invalid-item": (200, {"status": "error", "message": "Not found"}),
            }
        )
    )
    scraper = AllCalidadScraper(http_client=client)

    catalog = await scraper.fetch_catalog(ContentType.MOVIE)
    assert catalog == []

    detail = await scraper.fetch_detail("invalid-item", ContentType.MOVIE)
    assert detail is None
    await client.aclose()


@pytest.mark.asyncio
async def test_allcalidad_extract_identifiers():
    scraper = AllCalidadScraper(http_client=None)
    imdb, tmdb = scraper.extract_identifiers({"tmdb_id": 550, "imdb_id": "tt0137523"})
    assert imdb == "tt0137523"
    assert tmdb == "550"


@pytest.mark.asyncio
async def test_allcalidad_detail_404_returns_none(mock_http_client):
    scraper = AllCalidadScraper(http_client=mock_http_client)
    detail = await scraper.fetch_detail("item-999999999", ContentType.MOVIE)
    assert detail is None


# ==============================================================================
# Scraper Registry Unit Tests
# ==============================================================================
def test_registry_registered_providers_list():
    providers = get_registered_providers()
    assert isinstance(providers, list)
    assert "serieskao" in providers
    assert "poseidonhd2" in providers
    assert "gnula" in providers
    assert "allcalidad" in providers
    assert list_scrapers() == providers


def test_registry_get_scraper_instantiation(mock_http_client):
    sk = get_scraper("serieskao", http_client=mock_http_client)
    assert isinstance(sk, SeriesKaoScraper)
    assert sk.name == "serieskao"

    pos = get_scraper("poseidonhd2", http_client=mock_http_client)
    assert isinstance(pos, PoseidonHD2Scraper)
    assert pos.name == "poseidonhd2"

    gnu = get_scraper("gnula", http_client=mock_http_client)
    assert isinstance(gnu, GnulaScraper)
    assert gnu.name == "gnula"

    allcal = get_scraper("allcalidad", http_client=mock_http_client)
    assert isinstance(allcal, AllCalidadScraper)
    assert allcal.name == "allcalidad"


def test_registry_case_insensitivity_and_whitespace_trimming(mock_http_client):
    s1 = get_scraper("  SERIESKAO  ", http_client=mock_http_client)
    assert isinstance(s1, SeriesKaoScraper)

    s2 = get_scraper("PoseidonHD2", http_client=mock_http_client)
    assert isinstance(s2, PoseidonHD2Scraper)

    s3 = get_scraper("poseidon", http_client=mock_http_client)
    assert isinstance(s3, PoseidonHD2Scraper)


def test_registry_dynamic_registration():
    class DummyCustomScraper(BaseScraper):
        name = "dummy_provider"
        base_url = "https://dummy.example.com"

        async def fetch_catalog(self, *args, **kwargs):
            return []

        async def fetch_detail(self, *args, **kwargs):
            return None

    register_scraper("dummy_provider", DummyCustomScraper)
    registered = get_registered_providers()
    assert "dummy_provider" in registered

    instance = get_scraper("dummy_provider", http_client=None)
    assert isinstance(instance, DummyCustomScraper)
    assert instance.name == "dummy_provider"


def test_registry_unknown_provider_raises_value_error():
    with pytest.raises(ValueError, match="Unknown provider 'nonexistent_provider'"):
        get_scraper("nonexistent_provider", http_client=None)


def test_registry_empty_provider_raises_value_error():
    with pytest.raises(ValueError, match="Empty provider name"):
        get_scraper("", http_client=None)

    with pytest.raises(ValueError, match="Empty provider name"):
        get_scraper("   ", http_client=None)


def test_registry_invalid_type_raises_value_error():
    with pytest.raises(ValueError, match="Provider name must be a string"):
        get_scraper(None, http_client=None)  # type: ignore
