from __future__ import annotations

import pytest

from orion_mapper.core.rate_limiter import TokenBucketLimiter
from orion_mapper.resolver.tmdb import TmdbClient


def test_tmdb_client_default_fallback_api_key():
    client = TmdbClient(api_key=None)
    assert client.api_key == "34fafb223263c2461f8f88a3489cb92e"
    assert client.base_url == "https://api.themoviedb.org"


def test_tmdb_client_custom_api_key():
    client = TmdbClient(api_key="my_custom_key_1234567890abcdef1234")
    assert client.api_key == "my_custom_key_1234567890abcdef1234"


@pytest.mark.asyncio
async def test_tmdb_client_find_by_imdb_id_movie(mock_http_client):
    client = TmdbClient(http_client=mock_http_client)
    res = await client.find_by_imdb_id("tt0137523")
    assert res is not None
    assert res["id"] == 550
    assert res["media_type"] == "movie"


@pytest.mark.asyncio
async def test_tmdb_client_find_by_imdb_id_tv(mock_http_client):
    client = TmdbClient(http_client=mock_http_client)
    res = await client.find_by_imdb_id("tt15486")
    assert res is not None
    assert res["id"] == 82856
    assert res["media_type"] == "tv"


@pytest.mark.asyncio
async def test_tmdb_client_find_by_imdb_id_not_found(mock_http_client):
    client = TmdbClient(http_client=mock_http_client)
    res = await client.find_by_imdb_id("tt999999999")
    assert res is None


@pytest.mark.asyncio
async def test_tmdb_client_find_by_imdb_id_empty(mock_http_client):
    client = TmdbClient(http_client=mock_http_client)
    assert await client.find_by_imdb_id("") is None
    assert await client.find_by_imdb_id("   ") is None


@pytest.mark.asyncio
async def test_tmdb_client_get_external_ids_movie(mock_http_client):
    client = TmdbClient(http_client=mock_http_client)
    res = await client.get_external_ids(550, "movie")
    assert res is not None
    assert res["imdb_id"] == "tt0137523"


@pytest.mark.asyncio
async def test_tmdb_client_get_external_ids_tv(mock_http_client):
    client = TmdbClient(http_client=mock_http_client)
    res = await client.get_external_ids("82856", "series")
    assert res is not None
    assert res["imdb_id"] == "tt15486"


@pytest.mark.asyncio
async def test_tmdb_client_get_external_ids_404_graceful(mock_http_client):
    client = TmdbClient(http_client=mock_http_client)
    res = await client.get_external_ids(999999999, "movie")
    assert res is None


@pytest.mark.asyncio
async def test_tmdb_client_get_external_ids_empty(mock_http_client):
    client = TmdbClient(http_client=mock_http_client)
    assert await client.get_external_ids("", "movie") is None


@pytest.mark.asyncio
async def test_tmdb_client_search_movie_with_year(mock_http_client):
    client = TmdbClient(http_client=mock_http_client)
    results = await client.search("Fight Club", "movie", year=1999)
    assert len(results) >= 1
    assert results[0]["id"] == 550


@pytest.mark.asyncio
async def test_tmdb_client_search_tv_with_year(mock_http_client):
    client = TmdbClient(http_client=mock_http_client)
    results = await client.search("Zombieland Saga", "tv", year=2018)
    assert len(results) >= 1
    assert results[0]["id"] == 82856


@pytest.mark.asyncio
async def test_tmdb_client_search_empty_query(mock_http_client):
    client = TmdbClient(http_client=mock_http_client)
    results = await client.search("", "movie")
    assert results == []


@pytest.mark.asyncio
async def test_tmdb_client_rate_limiter_integration(mock_http_client):
    limiter = TokenBucketLimiter(rate=40.0, capacity=10.0)
    client = TmdbClient(http_client=mock_http_client, rate_limiter=limiter)
    assert client.rate_limiter is limiter

    # Ensure search acquires tokens without error
    results = await client.search("Fight Club", "movie", year=1999)
    assert len(results) >= 1


@pytest.mark.asyncio
async def test_tmdb_client_async_context_manager(mock_http_client):
    async with TmdbClient(http_client=mock_http_client) as client:
        res = await client.find_by_imdb_id("tt0137523")
        assert res is not None
        assert res["id"] == 550
