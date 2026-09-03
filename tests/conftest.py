import base64
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from orion_mapper.core.config import Settings
from orion_mapper.core.rate_limiter import RateLimiterRegistry

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def test_settings() -> Settings:
    return Settings(
        tmdb_api_key="test_tmdb_key_12345",
        tmdb_rate_limit=100.0,
        tmdb_rate_burst=100,
        default_provider_rate_limit=100.0,
        default_provider_rate_burst=100,
        http_timeout=2.0,
        http_max_retries=2,
        http_backoff_factor=0.01,
        http_backoff_max=0.1,
    )


@pytest.fixture(autouse=True)
def reset_rate_limiters():
    RateLimiterRegistry.reset()
    yield
    RateLimiterRegistry.reset()


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES_DIR


@pytest.fixture
def serieskao_fixtures(fixtures_dir: Path) -> dict[str, str]:
    sk_dir = fixtures_dir / "serieskao"
    return {
        "catalog_page1": (sk_dir / "catalog_page1.html").read_text(encoding="utf-8") if (sk_dir / "catalog_page1.html").exists() else "",
        "detail_movie": (sk_dir / "detail_movie_tt0137523.html").read_text(encoding="utf-8") if (sk_dir / "detail_movie_tt0137523.html").exists() else "",
        "detail_series": (sk_dir / "detail_series_tt15486.html").read_text(encoding="utf-8") if (sk_dir / "detail_series_tt15486.html").exists() else "",
    }


@pytest.fixture
def poseidonhd2_fixtures(fixtures_dir: Path) -> dict[str, str]:
    pos_dir = fixtures_dir / "poseidonhd2"
    return {
        "catalog_page1": (pos_dir / "catalog_page1.html").read_text(encoding="utf-8") if (pos_dir / "catalog_page1.html").exists() else "",
        "detail_movie": (pos_dir / "detail_movie_fight_club.html").read_text(encoding="utf-8") if (pos_dir / "detail_movie_fight_club.html").exists() else "",
        "detail_series": (pos_dir / "detail_series_zombieland.html").read_text(encoding="utf-8") if (pos_dir / "detail_series_zombieland.html").exists() else "",
    }


@pytest.fixture
def gnula_fixtures(fixtures_dir: Path) -> dict[str, str]:
    gn_dir = fixtures_dir / "gnula"
    return {
        "catalog_page1": (gn_dir / "catalog_page1.html").read_text(encoding="utf-8") if (gn_dir / "catalog_page1.html").exists() else "",
        "detail_movie": (gn_dir / "detail_movie_fight_club.html").read_text(encoding="utf-8") if (gn_dir / "detail_movie_fight_club.html").exists() else "",
        "detail_series": (gn_dir / "detail_series_zombieland.html").read_text(encoding="utf-8") if (gn_dir / "detail_series_zombieland.html").exists() else "",
    }


@pytest.fixture
def allcalidad_fixtures(fixtures_dir: Path) -> dict[str, Any]:
    ac_dir = fixtures_dir / "allcalidad"
    return {
        "listing_page1": json.loads((ac_dir / "listing_page1.json").read_text(encoding="utf-8")) if (ac_dir / "listing_page1.json").exists() else {},
        "single_movie": json.loads((ac_dir / "single_movie_550.json").read_text(encoding="utf-8")) if (ac_dir / "single_movie_550.json").exists() else {},
        "single_series": json.loads((ac_dir / "single_series_82856.json").read_text(encoding="utf-8")) if (ac_dir / "single_series_82856.json").exists() else {},
    }


@pytest.fixture
def tmdb_fixtures(fixtures_dir: Path) -> dict[str, Any]:
    tmdb_dir = fixtures_dir / "tmdb"
    return {
        "find_tt0137523": json.loads((tmdb_dir / "find_tt0137523.json").read_text(encoding="utf-8")) if (tmdb_dir / "find_tt0137523.json").exists() else {},
        "find_tt15486": json.loads((tmdb_dir / "find_tt15486.json").read_text(encoding="utf-8")) if (tmdb_dir / "find_tt15486.json").exists() else {},
        "movie_550_external_ids": json.loads((tmdb_dir / "movie_550_external_ids.json").read_text(encoding="utf-8")) if (tmdb_dir / "movie_550_external_ids.json").exists() else {},
        "tv_82856_external_ids": json.loads((tmdb_dir / "tv_82856_external_ids.json").read_text(encoding="utf-8")) if (tmdb_dir / "tv_82856_external_ids.json").exists() else {},
        "search_movie_fight_club": json.loads((tmdb_dir / "search_movie_fight_club.json").read_text(encoding="utf-8")) if (tmdb_dir / "search_movie_fight_club.json").exists() else {},
        "search_tv_zombieland": json.loads((tmdb_dir / "search_tv_zombieland.json").read_text(encoding="utf-8")) if (tmdb_dir / "search_tv_zombieland.json").exists() else {},
    }


@pytest.fixture
def temp_mappings_dir(tmp_path: Path) -> Path:
    d = tmp_path / "mappings"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def temp_orion_dir(tmp_path: Path) -> Path:
    d = tmp_path / "orion_mappings"
    d.mkdir(parents=True, exist_ok=True)
    return d


class MockTransport(httpx.AsyncBaseTransport):
    """Deterministic HTTP transport for testing without live network."""
    def __init__(self, fixtures: dict[str, Any]):
        self.fixtures = fixtures

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        path = request.url.path

        # 404 tests for non-existent items
        if "999999999" in url_str:
            return httpx.Response(404, json={"status_code": 34, "status_message": "The resource you requested could not be found."}, request=request)

        # SeriesKao endpoints
        if "serieskao" in url_str:
            if "/pelicula/el-club-de-la-lucha" in path:
                return httpx.Response(200, text=self.fixtures["serieskao"]["detail_movie"], request=request)
            if "/serie/zombieland-saga" in path:
                return httpx.Response(200, text=self.fixtures["serieskao"]["detail_series"], request=request)
            return httpx.Response(200, text=self.fixtures["serieskao"]["catalog_page1"], request=request)

        # PoseidonHD2 endpoints
        if "poseidonhd2" in url_str:
            if "/pelicula/el-club-de-la-lucha" in path:
                return httpx.Response(200, text=self.fixtures["poseidonhd2"]["detail_movie"], request=request)
            if "/serie/zombieland-saga" in path:
                return httpx.Response(200, text=self.fixtures["poseidonhd2"]["detail_series"], request=request)
            return httpx.Response(200, text=self.fixtures["poseidonhd2"]["catalog_page1"], request=request)

        # Gnula endpoints
        if "gnula" in url_str:
            if "pelicula-el-club-de-la-lucha" in path:
                return httpx.Response(200, text=self.fixtures["gnula"]["detail_movie"], request=request)
            if "serie-zombieland-saga" in path:
                return httpx.Response(200, text=self.fixtures["gnula"]["detail_series"], request=request)
            return httpx.Response(200, text=self.fixtures["gnula"]["catalog_page1"], request=request)

        # AllCalidad endpoints
        if "allcalidad" in url_str:
            if "zombieland" in url_str:
                return httpx.Response(200, json=self.fixtures["allcalidad"]["single_series"], request=request)
            if "/api/rest/single" in path or "/api/rest/movie" in path or "550" in path:
                return httpx.Response(200, json=self.fixtures["allcalidad"]["single_movie"], request=request)
            if "/api/rest/series" in path or "82856" in path:
                return httpx.Response(200, json=self.fixtures["allcalidad"]["single_series"], request=request)
            return httpx.Response(200, json=self.fixtures["allcalidad"]["listing_page1"], request=request)

        # TMDB API endpoints
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

        # Fallback 404
        return httpx.Response(404, json={"error": "Not Found", "url": url_str}, request=request)


@pytest.fixture
def mock_transport(serieskao_fixtures, poseidonhd2_fixtures, gnula_fixtures, allcalidad_fixtures, tmdb_fixtures) -> MockTransport:
    return MockTransport({
        "serieskao": serieskao_fixtures,
        "poseidonhd2": poseidonhd2_fixtures,
        "gnula": gnula_fixtures,
        "allcalidad": allcalidad_fixtures,
        "tmdb": tmdb_fixtures,
    })


@pytest.fixture
def mock_http_client(mock_transport: MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=mock_transport)


def decode_orion_provider_key(encoded_key: str) -> str:
    """Helper to decode unpadded urlsafe base64 provider key (e.g. 'serieskao:zombieland-saga')."""
    padding = len(encoded_key) % 4
    if padding != 0:
        encoded_key += "=" * (4 - padding)
    return base64.urlsafe_b64decode(encoded_key.encode("ascii")).decode("utf-8")


def encode_orion_provider_key(provider: str, slug: str) -> str:
    """Helper to encode provider key according to OrionServer contract."""
    raw = f"{provider.lower()}:{slug}".encode()
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
