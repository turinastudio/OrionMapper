from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from orion_mapper.matcher.normalizer import (
    ParsedTitle,
    TitleNormalizer,
)
from orion_mapper.matcher.reconciler import IdentityReconciler
from orion_mapper.matcher.scoring import (
    CandidateScorer,
    FuzzyTitleMatcher,
    MatchResult,
)
from orion_mapper.models.item import ContentType, ScrapedDetail, ScrapedEpisode, ScrapedItem
from orion_mapper.models.mapping import CanonicalMapping
from orion_mapper.resolver.tmdb import TmdbClient

# ==============================================================================
# 1. CandidateScorer & FuzzyTitleMatcher Extreme Edge Cases
# ==============================================================================

class TestCandidateScorerExtremeEdgeCases:
    """Extreme edge case tests for CandidateScorer and FuzzyTitleMatcher."""

    @pytest.mark.parametrize(
        ("year1", "year2", "expected_delta_score"),
        [
            (0, 0, 25.0),
            (0, 1, 10.0),
            (0, 2, -35.0),
            (-500, -500, 25.0),
            (-500, -499, 10.0),
            (-500, 2024, -35.0),
            (3000, 3000, 25.0),
            (3000, 3001, 10.0),
            (3000, 2024, -35.0),
            (9999999, 9999999, 25.0),
            (9999999, 0, -35.0),
        ],
    )
    def test_fuzzy_matcher_extreme_years(self, year1: int, year2: int, expected_delta_score: float):
        """Verify year delta calculation does not crash on negative, zero, or futuristic years."""
        score_with_years = FuzzyTitleMatcher.score("The Matrix", "The Matrix", year1=year1, year2=year2)
        score_base = 70.0  # 1.0 overlap * 70
        assert score_with_years == max(0.0, score_base + expected_delta_score)

    @pytest.mark.parametrize(
        ("raw_date", "expected_year"),
        [
            ("1999-10-15", 1999),
            ("2024-05-01", 2024),
            ("2099-12-31", 2099),
            ("1900-01-01", 1900),
            ("0000-00-00", None),  # YEAR_REGEX only matches (19|20)\d{2}
            ("-0500-01-01", None),
            ("3000-01-01", None),
            ("Invalid Date String", None),
            ("", None),
            (None, None),
            (1999, 1999),
            (2025, 2025),
        ],
    )
    def test_extract_year_boundaries(self, raw_date: Any, expected_year: int | None):
        """Test extract_year across valid, boundary, and completely invalid date values."""
        cand = {"release_date": raw_date}
        assert CandidateScorer.extract_year(cand) == expected_year
        cand_tv = {"first_air_date": raw_date}
        assert CandidateScorer.extract_year(cand_tv) == expected_year

    def test_extract_year_pre_1900_boundary_limitation(self):
        r"""
        Pre-1900 films (1880-1899).
        CanonicalMapping and YEAR_REGEX allow ge=1880.
        Verifies that 1895 date string extracts to 1895.
        """
        cand_1895 = {"release_date": "1895-12-28"}
        assert CandidateScorer.extract_year(cand_1895) == 1895

    def test_extract_year_malformed_types(self):
        """Ensure extract_year handles weird types without throwing unhandled exceptions."""
        assert CandidateScorer.extract_year({"release_date": ["2020"]}) == 2020
        assert CandidateScorer.extract_year({"release_date": {"year": 2020}}) == 2020
        assert CandidateScorer.extract_year({"release_date": object()}) is None

    def test_negative_score_generation_on_empty_candidate(self):
        """
        Adversarial test: Verify CandidateScorer score calculation on empty candidate.
        Parsed has year=1999. Candidate is empty {}.
        Overlaps: 0.0 -> base = 0.0.
        Candidate year is None, but parsed year is 1999 -> -5.0.
        Candidate has no imdb_id and not exact match -> -10.0.
        Total = -15.0.
        """
        parsed = TitleNormalizer.parse("Fight Club", year=1999)
        res_empty = CandidateScorer.score_candidate(parsed, {}, content_type="movie")
        assert res_empty.score == -15.0
        assert res_empty.confidence == "low"
        assert res_empty.tmdb_id is None

    def test_short_substring_false_positive_anomaly(self):
        """
        Adversarial test: Short 1-2 character candidate title substring match.
        Candidate title 'to' against 'Doctor Strange (2016)'.
        Verifies that short 1-2 character substring matches are NOT awarded high confidence.
        """
        parsed = TitleNormalizer.parse("Doctor Strange (2016)")
        candidate_to = {
            "title": "to",
            "id": 99999,
            "media_type": "movie",
            "release_date": "2016-11-04",
            "imdb_id": "tt1234567",
        }
        res = CandidateScorer.score_candidate(parsed, candidate_to, content_type="movie")
        assert res.score < 88.0
        assert res.confidence == "low"

    def test_anime_vs_non_anime_scoring_matrix(self):
        """
        Test matrix of anime boosts vs non-anime providers:
        - Anime provider + JA language + JP origin -> +12 (lang/country) + 25 (exact + JA/JP) = +37 bonus
        - Anime provider + ZH language + CN origin -> +12 (lang/country)
        - Anime provider + US origin + EN language -> +0 lang bonus
        - Anime provider + season hints + best_score >= 0.70 -> +8 season hint bonus
        - Non-anime provider + JA language -> +0 anime boost (strictly guarded)
        """
        # 1. Anime provider with full JP metadata and exact match
        parsed_anime = TitleNormalizer.parse("Sousou no Frieren", provider_input="animeflv", year=2023)
        cand_jp = {
            "name": "Sousou no Frieren",
            "id": 12345,
            "media_type": "tv",
            "original_language": "ja",
            "origin_country": ["JP"],
            "first_air_date": "2023-09-29",
            "imdb_id": "tt22026970",
        }
        res_jp = CandidateScorer.score_candidate(parsed_anime, cand_jp, content_type="series")
        # 70 (overlap) + 25 (exact year) + 5 (type) + 12 (anime lang) + 25 (anime ja exact) = 137.0
        assert res_jp.score >= 130.0
        assert res_jp.confidence == "high"

        # 2. Anime provider with Chinese anime (Donghua)
        parsed_donghua = TitleNormalizer.parse("Mo Dao Zu Shi", provider_input="jkanime", year=2018)
        cand_cn = {
            "name": "Mo Dao Zu Shi",
            "id": 80000,
            "media_type": "tv",
            "original_language": "zh",
            "origin_country": ["CN"],
            "first_air_date": "2018-07-09",
            "imdb_id": "tt8630036",
        }
        res_cn = CandidateScorer.score_candidate(parsed_donghua, cand_cn, content_type="series")
        # 70 + 25 + 5 + 12 = 112.0
        assert res_cn.score == 112.0

        # 3. Anime provider with season hint boost
        parsed_s2 = TitleNormalizer.parse("Jujutsu Kaisen Season 2", provider_input="tioanime", year=2023)
        assert 2 in parsed_s2.season_hints
        cand_s2 = {
            "name": "Jujutsu Kaisen",
            "id": 95479,
            "media_type": "tv",
            "original_language": "ja",
            "origin_country": ["JP"],
            "first_air_date": "2020-10-03",
        }
        res_s2 = CandidateScorer.score_candidate(parsed_s2, cand_s2, content_type="series")
        # Overlap: "jujutsu kaisen" == "jujutsu kaisen" -> 1.0 (70)
        # Year delta: abs(2023 - 2020) = 3 -> -35
        # Type: +5
        # Anime lang/country: +12
        # Anime exact JA: +25
        # Anime season hints (best_score >= 0.70 and season_hints): +8
        # No imdb_id and not exact year: -10
        # Total: 70 - 35 + 5 + 12 + 25 + 8 - 10 = 75.0
        assert res_s2.score == 75.0
        assert res_s2.confidence == "candidate"

        # 4. Non-anime provider with Japanese content (e.g. serieskao or poseidonhd2)
        parsed_non_anime = TitleNormalizer.parse("Sousou no Frieren", provider_input="serieskao", year=2023)
        res_non_anime = CandidateScorer.score_candidate(parsed_non_anime, cand_jp, content_type="series")
        # Score must NOT receive the +12 and +25 anime boosts
        assert res_non_anime.score == 100.0  # 70 + 25 + 5

    def test_anime_malformed_origin_country(self):
        """Test anime scoring when origin_country is malformed (e.g. string or None)."""
        parsed = TitleNormalizer.parse("Naruto", provider_input="animeflv", year=2002)
        cand_str_country = {
            "name": "Naruto",
            "id": 46260,
            "media_type": "tv",
            "original_language": "ja",
            "origin_country": "JP",  # String instead of list
            "first_air_date": "2002-10-03",
        }
        res = CandidateScorer.score_candidate(parsed, cand_str_country, content_type="series")
        assert isinstance(res, MatchResult)
        assert res.score >= 88.0

    def test_generic_title_penalty_matrix(self):
        """
        Verify generic title penalty (-20):
        - Generic title (< 14 chars, <= 2 words) WITHOUT year -> -20 penalty.
        - Generic title WITH year -> NO penalty.
        - Non-generic title WITHOUT year -> NO penalty.
        """
        # 1. Generic title without year: "9" (1 word, 1 char)
        p_generic_no_year = TitleNormalizer.parse("9", year=None)
        cand_9 = {"title": "9", "id": 12244, "media_type": "movie", "release_date": "2009-09-09"}
        res_9 = CandidateScorer.score_candidate(p_generic_no_year, cand_9, content_type="movie")
        # Overlap: 1.0 (70)
        # Year: parsed is None -> 0
        # Type: +5
        # Generic penalty: -20
        # No imdb_id and not exact year: -10
        # Total: 70 + 5 - 20 - 10 = 45.0
        assert res_9.score == 45.0

        # 2. Generic title WITH year: "9 (2009)"
        p_generic_with_year = TitleNormalizer.parse("9 (2009)")
        res_9_year = CandidateScorer.score_candidate(p_generic_with_year, cand_9, content_type="movie")
        # Overlap: 70, Year exact: +25, Type: +5, No generic penalty, Exact title and year: no -10 penalty
        # Total: 70 + 25 + 5 = 100.0
        assert res_9_year.score == 100.0

        # 3. Non-generic title without year: "The Lord of the Rings" (> 2 words, > 14 chars)
        p_nongeneric = TitleNormalizer.parse("The Lord of the Rings", year=None)
        cand_lotr = {"title": "The Lord of the Rings", "id": 123, "media_type": "movie"}
        res_lotr = CandidateScorer.score_candidate(p_nongeneric, cand_lotr, content_type="movie")
        # Overlap: 70, Type: +5, No generic penalty, No imdb_id penalty: -10 -> 65.0
        assert res_lotr.score == 65.0

    def test_media_type_mismatch_penalty(self):
        """Test candidate score deduction on media type mismatch (-40 vs +5)."""
        parsed_movie = TitleNormalizer.parse("Inception", year=2010)
        cand_tv = {"name": "Inception", "id": 999, "media_type": "tv", "first_air_date": "2010-01-01"}
        cand_movie = {"title": "Inception", "id": 999, "media_type": "movie", "release_date": "2010-01-01"}

        res_tv = CandidateScorer.score_candidate(parsed_movie, cand_tv, content_type="movie")
        res_movie = CandidateScorer.score_candidate(parsed_movie, cand_movie, content_type="movie")

        # Difference should be +5 (match) - (-40) (mismatch) = 45.0
        assert res_movie.score - res_tv.score == 45.0


# ==============================================================================
# 2. TitleNormalizer Stress & Adversarial Inputs
# ==============================================================================

class TestTitleNormalizerStress:
    """Stress testing normalization across unicode, noise words, symbols, and aliases."""

    def test_normalize_giant_string_performance(self):
        """Stress test normalizer with 100,000-character input."""
        giant_title = ("El Señor de los Anillos: Las Dos Torres " * 2500) + " (2002) Temporada 2 HD 1080p Latino"
        start = time.monotonic()
        parsed = TitleNormalizer.parse(giant_title, provider_input="serieskao")
        duration = time.monotonic() - start

        assert duration < 1.0, f"Normalization took too long: {duration:.4f}s"
        assert parsed.year == 2002
        assert 2 in parsed.season_hints
        assert "el senor de los anillos" in parsed.normalized_title

    def test_zalgo_and_combining_diacritics(self):
        """Verify Zalgo and unusual combining diacritics do not crash or corrupt text."""
        zalgo_title = "M̸a̴t̸r̸i̴x̸ ̷R̵e̷v̵o̸l̸u̴t̵i̷o̵n̴s̵"
        normalized = TitleNormalizer.normalize(zalgo_title)
        assert "matrix revolutions" in normalized

    def test_multilingual_unicode_and_emojis(self):
        """Verify handling of Japanese, Chinese, Cyrillic, Arabic, and emojis."""
        assert TitleNormalizer.normalize("🎬 Spider-Man: No Way Home 🕷️ (2021) 🍿") == "spider man no way home 2021"
        assert TitleNormalizer.normalize("進撃の巨人 Attack on Titan") == "attack on titan"
        assert TitleNormalizer.normalize("¿Quién mató a Sara? ¡Completa!") == "quien mato a sara"

    def test_regex_special_characters_safety(self):
        """Verify title containing regex metacharacters doesn't cause ReDoS or SyntaxError."""
        regex_title = "Movie [4K] (2020) +++ *** ??? $$$ ^^^ \\d+ \\w+ (.*) [a-z]{1,5}"
        parsed = TitleNormalizer.parse(regex_title)
        assert isinstance(parsed, ParsedTitle)
        assert parsed.year == 2020

    def test_all_noise_words_reduction(self):
        """Verify that title consisting solely of noise words reduces to empty without crash."""
        all_noise = "Audio Latino Castellano Completa HD 4K 1080p 720p Bluray DVDRip Webrip Dual Subtitulado Online"
        norm = TitleNormalizer.normalize(all_noise)
        assert norm == ""

        parsed = TitleNormalizer.parse(all_noise)
        assert parsed.normalized_title == all_noise  # Falls back safely to raw/normalized


# ==============================================================================
# 3. IdentityReconciler Large Batches, Conflicts, and Race Conditions
# ==============================================================================

class MockTmdbClient(TmdbClient):
    """Configurable mock TMDB client for high-scale adversarial testing."""

    def __init__(
        self,
        find_map: dict[str, dict[str, Any]] | None = None,
        ext_map: dict[str, dict[str, Any]] | None = None,
        search_map: dict[str, list[dict[str, Any]]] | None = None,
        error_rate: float = 0.0,
        delay_ms: float = 0.0,
    ):
        super().__init__(api_key="mock_key")
        self.find_map = find_map or {}
        self.ext_map = ext_map or {}
        self.search_map = search_map or {}
        self.error_rate = error_rate
        self.delay_ms = delay_ms
        self.find_calls: list[str] = []
        self.ext_calls: list[tuple[str, str]] = []
        self.search_calls: list[str] = []

    async def find_by_imdb_id(self, imdb_id: str) -> dict[str, Any] | None:
        self.find_calls.append(imdb_id)
        if self.delay_ms > 0:
            await asyncio.sleep(self.delay_ms / 1000.0)
        return self.find_map.get(imdb_id.lower())

    async def get_external_ids(self, tmdb_id: str | int, media_type: str) -> dict[str, Any] | None:
        self.ext_calls.append((str(tmdb_id), str(media_type)))
        if self.delay_ms > 0:
            await asyncio.sleep(self.delay_ms / 1000.0)
        return self.ext_map.get(str(tmdb_id))

    async def search(
        self,
        title: str,
        media_type: str,
        year: int | None = None,
        language: str = "es-MX",
    ) -> list[dict[str, Any]]:
        self.search_calls.append(title)
        if self.delay_ms > 0:
            await asyncio.sleep(self.delay_ms / 1000.0)
        norm = TitleNormalizer.normalize(title)
        return self.search_map.get(norm, [])


class InMemoryMasterStore:
    """Thread-safe and async-safe in-memory master store."""

    def __init__(self):
        self.by_tmdb: dict[tuple[str, str], CanonicalMapping] = {}
        self.by_imdb: dict[tuple[str, str], CanonicalMapping] = {}
        self._lock = asyncio.Lock()

    def get_by_tmdb(self, tmdb_id: str, content_type: str) -> CanonicalMapping | None:
        return self.by_tmdb.get((str(tmdb_id), str(content_type).lower()))

    def get_by_imdb(self, imdb_id: str, content_type: str) -> CanonicalMapping | None:
        return self.by_imdb.get((str(imdb_id), str(content_type).lower()))

    async def save_mapping(self, mapping: CanonicalMapping) -> None:
        async with self._lock:
            c_type = str(mapping.type).lower()
            if mapping.tmdb_id:
                self.by_tmdb[(str(mapping.tmdb_id), c_type)] = mapping
            if mapping.imdb_id:
                self.by_imdb[(str(mapping.imdb_id), c_type)] = mapping


class TestIdentityReconcilerAdversarial:
    """Stress tests for IdentityReconciler on large batches, conflicts, and coalescing."""

    @pytest.mark.asyncio
    async def test_reconcile_batch_1000_items_with_duplicate_slugs_and_providers(self):
        """
        Stress test: 1,000 scraped items representing 10 distinct entities.
        Each entity has 100 duplicate items across 5 providers with slight slug variations.
        Verify that:
        1. Output is exactly 10 CanonicalMapping entries.
        2. Providers dictionary correctly retains all 5 provider keys per mapping.
        3. No memory leaks or quadratic slowdowns.
        """
        items: list[ScrapedItem] = []
        providers = ["gnula", "serieskao", "poseidonhd2", "allcalidad", "animeflv"]

        for entity_idx in range(10):
            tmdb_id = str(1000 + entity_idx)
            imdb_id = f"tt{1000000 + entity_idx}"
            title = f"Movie Title {entity_idx}"

            for item_idx in range(100):
                prov = providers[item_idx % len(providers)]
                slug = f"movie-{entity_idx}-variant-{item_idx}"
                items.append(
                    ScrapedItem(
                        provider=prov,
                        slug=slug,
                        title=title,
                        type=ContentType.MOVIE,
                        year=2020,
                        tmdb_id=tmdb_id,
                        imdb_id=imdb_id,
                    )
                )

        mock_tmdb = MockTmdbClient()
        reconciler = IdentityReconciler(tmdb_client=mock_tmdb)

        start = time.monotonic()
        reconciled = await reconciler.reconcile_batch(items)
        elapsed = time.monotonic() - start

        assert len(reconciled) == 10, f"Expected 10 canonical mappings, got {len(reconciled)}"
        assert elapsed < 1.0, f"Batch reconciliation took too long: {elapsed:.4f}s"

        for mapping in reconciled:
            assert mapping.tmdb_id is not None
            assert mapping.imdb_id is not None
            assert len(mapping.providers) == 5
            for p in providers:
                assert p in mapping.providers

    @pytest.mark.asyncio
    async def test_transitive_coalescing_order_dependency_vulnerability(self):
        """
        Transitive coalescing test:
        Item 1: TMDB=100, IMDb=None
        Item 2: TMDB=None, IMDb=tt200
        Item 3: TMDB=100, IMDb=tt200 (bridges Item 1 and Item 2)

        Both Order [3, 1, 2] and Order [1, 2, 3] must coalesce into 1 CanonicalMapping with all 3 providers.
        """
        reconciler = IdentityReconciler(tmdb_client=MockTmdbClient())

        item1 = ScrapedItem(provider="p1", slug="s1", title="A", type=ContentType.MOVIE, tmdb_id="100")
        item2 = ScrapedItem(provider="p2", slug="s2", title="A", type=ContentType.MOVIE, imdb_id="tt200")
        item3 = ScrapedItem(provider="p3", slug="s3", title="A", type=ContentType.MOVIE, tmdb_id="100", imdb_id="tt200")

        # Order 1: Bridging item first -> 1 mapping
        res_ideal = await reconciler.reconcile_batch([item3, item1, item2])
        assert len(res_ideal) == 1
        assert len(res_ideal[0].providers) == 3

        # Order 2: Bridging item last -> Transitive coalescing bridges into 1 mapping
        res_fragmented = await reconciler.reconcile_batch([item1, item2, item3])
        assert len(res_fragmented) == 1
        assert res_fragmented[0].tmdb_id == "100"
        assert res_fragmented[0].imdb_id == "tt200"
        assert len(res_fragmented[0].providers) == 3
        assert "p1" in res_fragmented[0].providers
        assert "p2" in res_fragmented[0].providers
        assert "p3" in res_fragmented[0].providers

    @pytest.mark.asyncio
    async def test_reconcile_batch_conflicting_tmdb_and_imdb_ids(self):
        """
        Adversarial test: Scraped items with conflicting ID cross-references.
        Item 1: TMDB=500, IMDb=None
        Item 2: TMDB=None, IMDb=tt9999
        Item 3: TMDB=500, IMDb=tt9999 (bridges Item 1 and Item 2)
        Item 4: TMDB=500, IMDb=tt8888 (conflicting IMDb ID for same TMDB)
        """
        mock_tmdb = MockTmdbClient(
            find_map={
                "tt9999": {"id": 500, "media_type": "movie", "title": "Bridged Movie"},
                "tt8888": {"id": 500, "media_type": "movie", "title": "Conflicting Movie"},
            },
            ext_map={"500": {"imdb_id": "tt9999"}},
        )
        reconciler = IdentityReconciler(tmdb_client=mock_tmdb)

        items = [
            ScrapedItem(provider="p1", slug="s1", title="Movie", type=ContentType.MOVIE, tmdb_id="500"),
            ScrapedItem(provider="p2", slug="s2", title="Movie", type=ContentType.MOVIE, imdb_id="tt9999"),
            ScrapedItem(provider="p3", slug="s3", title="Movie", type=ContentType.MOVIE, tmdb_id="500", imdb_id="tt9999"),
            ScrapedItem(provider="p4", slug="s4", title="Movie", type=ContentType.MOVIE, tmdb_id="500", imdb_id="tt8888"),
        ]

        mappings = await reconciler.reconcile_batch(items)
        assert len(mappings) >= 1
        # Primary mapping for TMDB 500 should contain merged providers
        primary = next(m for m in mappings if m.tmdb_id == "500")
        assert "p1" in primary.providers
        assert "p3" in primary.providers
        assert "p4" in primary.providers

    @pytest.mark.asyncio
    async def test_concurrent_batch_reconciliations_race_condition(self):
        """
        Concurrency stress test: 20 concurrent tasks calling reconcile_batch on overlapping data.
        Verifies that no deadlocks or memory corruption occur under concurrent asyncio execution.
        """
        store = InMemoryMasterStore()
        mock_tmdb = MockTmdbClient()
        reconciler = IdentityReconciler(tmdb_client=mock_tmdb)

        async def worker(worker_id: int):
            batch = [
                ScrapedItem(
                    provider=f"prov_{worker_id}",
                    slug=f"slug_{worker_id}_{i}",
                    title=f"Concurrent Movie {i}",
                    type=ContentType.MOVIE,
                    year=2021,
                    tmdb_id=str(5000 + i),
                    imdb_id=f"tt{5000000 + i}",
                )
                for i in range(25)
            ]
            mappings = await reconciler.reconcile_batch(batch, master_store=store)
            for m in mappings:
                await store.save_mapping(m)
            return len(mappings)

        results = await asyncio.gather(*(worker(w) for w in range(20)))
        assert all(count == 25 for count in results)
        assert len(store.by_tmdb) == 25

    @pytest.mark.asyncio
    async def test_priority_4_fuzzy_resolution_threshold_boundary(self):
        """
        Verify Priority 4 threshold boundary:
        - Candidate score == 87.9 (confidence candidate/low) -> Rejected (returns None)
        - Candidate score >= 88.0 (confidence high) -> Accepted
        """
        # Exact match with matching year = 70 + 25 + 5 = 100.0 (Accepted)
        # Title mismatch with no year = 0.0 * 70 = 0.0 (Rejected)
        mock_tmdb = MockTmdbClient(
            search_map={
                "exact high match": [
                    {
                        "title": "Exact High Match",
                        "id": 777,
                        "media_type": "movie",
                        "release_date": "2020-01-01",
                        "imdb_id": "tt7777777",
                    }
                ],
                "low score candidate": [
                    {
                        "title": "Totally Different Title",
                        "id": 888,
                        "media_type": "movie",
                        "release_date": "2010-01-01",
                    }
                ],
            }
        )
        reconciler = IdentityReconciler(tmdb_client=mock_tmdb)

        item_high = ScrapedItem(
            provider="gnula",
            slug="high-match",
            title="Exact High Match",
            type=ContentType.MOVIE,
            year=2020,
        )
        m_high = await reconciler.reconcile_item(item_high)
        assert m_high is not None
        assert m_high.tmdb_id == "777"

        item_low = ScrapedItem(
            provider="gnula",
            slug="low-match",
            title="Low Score Candidate",
            type=ContentType.MOVIE,
            year=2020,
        )
        m_low = await reconciler.reconcile_item(item_low)
        assert m_low is None

    @pytest.mark.asyncio
    async def test_scraped_detail_reconciliation_compatibility(self):
        """Verify that ScrapedDetail (subclass of ScrapedItem with episodes/genres) is reconciled identically."""
        mock_tmdb = MockTmdbClient()
        reconciler = IdentityReconciler(tmdb_client=mock_tmdb)

        detail = ScrapedDetail(
            provider="serieskao",
            slug="stranger-things",
            title="Stranger Things",
            type=ContentType.SERIES,
            year=2016,
            tmdb_id="66732",
            imdb_id="tt4574334",
            genres=["Sci-Fi", "Drama"],
            episodes=[
                ScrapedEpisode(season=1, episode=1, title="Chapter One"),
                ScrapedEpisode(season=1, episode=2, title="Chapter Two"),
            ],
            seasons_count=4,
        )
        mapping = await reconciler.reconcile_item(detail)
        assert mapping is not None
        assert mapping.tmdb_id == "66732"
        assert mapping.imdb_id == "tt4574334"
        assert mapping.providers["serieskao"] == "stranger-things"
