from __future__ import annotations

import asyncio
import base64
import binascii
import time
from typing import ClassVar

import httpx
import pytest
import respx
from pydantic import ValidationError

from orion_mapper.core.config import Settings
from orion_mapper.core.http import (
    AsyncHttpClient,
    MaxRetriesExceededError,
)
from orion_mapper.core.rate_limiter import TokenBucketLimiter
from orion_mapper.models.item import (
    ContentType,
    ScrapedDetail,
    ScrapedEpisode,
    ScrapedItem,
)
from orion_mapper.models.mapping import CanonicalMapping
from orion_mapper.models.orion import (
    IdentityMappingExport,
    ImdbIdentityIndexExport,
    TmdbIdentityIndexExport,
    decode_provider_key,
    encode_provider_key,
)
from orion_mapper.scrapers.base import BaseScraper

# ============================================================================
# 1. Base64 URL-safe Encoding & Decoding Adversarial Tests
# ============================================================================


class TestAdversarialEncoding:
    """Stress-test encode_provider_key and decode_provider_key."""

    def test_zero_padding_invariant_on_various_lengths(self):
        """Verify that output NEVER contains '=' padding characters for any byte length."""
        for length in range(1, 500):
            provider = "p" * (length % 7 + 1)
            slug = "s" * length
            encoded = encode_provider_key(provider, slug)
            assert "=" not in encoded, f"Padding character found in encoded key for length {length}: {encoded}"
            # Verify reversible
            dec_prov, dec_slug = decode_provider_key(encoded)
            assert dec_prov == provider
            assert dec_slug == slug

    def test_unicode_and_emojis(self):
        """Stress-test with multi-byte unicode characters, emojis, accents, and CJK text."""
        test_cases = [
            ("serieskao", "película-de-acción-🔥-2024"),
            ("gnula", "ñandú-y-pingüinos-en-el-café"),
            ("poseidonhd2", "ゾンビランドサガ-リベンジ-🧟‍♂️"),
            ("allcalidad", "你好-世界-🎬-🍿-✨"),
            ("custom_provider", "مرحبا-بالعالم-العربي"),
            ("provider_123", "symbols_!@#$%^&*()_+-=[]{}|;:,.<>?/`~"),
            ("unicode_edge", "\u200b\u200c\u200d\ufeff-zero-width-test"),
        ]
        for prov, slug in test_cases:
            encoded = encode_provider_key(prov, slug)
            assert "=" not in encoded
            assert isinstance(encoded, str)
            dec_prov, dec_slug = decode_provider_key(encoded)
            assert dec_prov == prov.lower().strip()
            assert dec_slug == slug.strip()

    def test_slashes_and_colons_in_slug(self):
        """Verify handling of slashes, colons, spaces, and query string like segments."""
        test_cases = [
            ("gnula", "tv/series/season:1/episode:5"),
            ("serieskao", "path/to/item:special:edition"),
            ("poseidonhd2", "slug-with-colon:and:another:colon"),
            ("allcalidad", "/leading/and/trailing/slashes/"),
        ]
        for prov, slug in test_cases:
            encoded = encode_provider_key(prov, slug)
            assert "=" not in encoded
            dec_prov, dec_slug = decode_provider_key(encoded)
            assert dec_prov == prov.lower().strip()
            assert dec_slug == slug.strip()

    def test_extremely_long_strings(self):
        """Stress-test with very large payloads (up to 100,000 characters)."""
        for size in [1_000, 10_000, 100_000]:
            prov = "provider"
            slug = "a" * size
            encoded = encode_provider_key(prov, slug)
            assert "=" not in encoded
            dec_prov, dec_slug = decode_provider_key(encoded)
            assert dec_prov == prov
            assert dec_slug == slug
            assert len(dec_slug) == size

    def test_whitespace_and_casing_normalization(self):
        """Ensure provider is stripped and lowercased, slug is stripped."""
        prov = "   SeriesKAO   "
        slug = "   /my-awesome-movie/   "
        encoded = encode_provider_key(prov, slug)
        dec_prov, dec_slug = decode_provider_key(encoded)
        assert dec_prov == "serieskao"
        assert dec_slug == "/my-awesome-movie/"

    def test_decode_malformed_inputs(self):
        """Ensure invalid base64 or corrupt payload raises appropriate exceptions."""
        # Non-base64 characters
        with pytest.raises(binascii.Error):
            decode_provider_key("invalid!!!base64@@@")

        # Base64 string missing colon delimiter
        valid_b64_no_colon = base64.urlsafe_b64encode(b"nocolonhere").decode("ascii").rstrip("=")
        with pytest.raises(ValueError, match="not enough values to unpack"):
            decode_provider_key(valid_b64_no_colon)


# ============================================================================
# 2. Pydantic Models Adversarial & Edge Case Tests
# ============================================================================


class TestAdversarialModels:
    """Stress-test Pydantic model validation and edge cases."""

    def test_imdb_id_edge_cases(self):
        """Test variations of IMDb IDs: valid, invalid, boundary digits, prefixing."""
        # Valid cases
        valid_cases = [
            ("tt0137523", "tt0137523"),
            ("TT0137523", "tt0137523"),
            ("Tt0137523", "tt0137523"),
            ("tt1", "tt1"),
            ("tt1234567890", "tt1234567890"),  # 10 digits
            ("0137523", "tt0137523"),  # Pure digits auto-prepends 'tt'
            ("12345", "tt12345"),
            ("  tt0137523  ", "tt0137523"),
        ]
        for input_id, expected in valid_cases:
            item = ScrapedItem(
                provider="gnula",
                slug="item",
                title="Title",
                type=ContentType.MOVIE,
                imdb_id=input_id,
            )
            assert item.imdb_id == expected, f"Failed for input {input_id}"

            # Test same validator in CanonicalMapping
            mapping = CanonicalMapping(
                title="Title",
                type=ContentType.MOVIE,
                imdb_id=input_id,
            )
            assert mapping.imdb_id == expected, f"Failed CanonicalMapping for input {input_id}"

        # Invalid cases that should normalize to None
        invalid_cases = [
            "tt",  # No digits
            "tt12345678901",  # 11 digits (exceeds 10 digits max)
            "tt-12345",  # Non-digit
            "tt123a45",  # Infix letter
            "nm0000123",  # Name ID (not title)
            "invalid_string",
            "",
            "   ",
            None,
        ]
        for input_id in invalid_cases:
            item = ScrapedItem(
                provider="gnula",
                slug="item",
                title="Title",
                type=ContentType.MOVIE,
                imdb_id=input_id,
            )
            assert item.imdb_id is None, f"Expected None for invalid IMDb ID {input_id}, got {item.imdb_id}"

            mapping = CanonicalMapping(
                title="Title",
                type=ContentType.MOVIE,
                imdb_id=input_id,
            )
            assert mapping.imdb_id is None, f"Expected None for CanonicalMapping invalid IMDb ID {input_id}"

    def test_tmdb_id_edge_cases(self):
        """Test variations of TMDB IDs: numeric string, integer, invalid strings."""
        # Valid cases
        valid_cases = [
            ("550", "550"),
            (550, "550"),
            ("0", "0"),
            ("1234567890", "1234567890"),  # 10 digits
            ("  82856  ", "82856"),
        ]
        for input_id, expected in valid_cases:
            item = ScrapedItem(
                provider="gnula",
                slug="item",
                title="Title",
                type=ContentType.MOVIE,
                tmdb_id=input_id,
            )
            assert item.tmdb_id == expected, f"Failed for input {input_id}"

            mapping = CanonicalMapping(
                title="Title",
                type=ContentType.MOVIE,
                tmdb_id=input_id,
            )
            assert mapping.tmdb_id == expected, f"Failed CanonicalMapping for TMDB ID {input_id}"

        # Invalid cases normalizing to None
        invalid_cases = [
            "-550",  # Negative
            "550.5",  # Float
            "movie-550",  # Non-numeric prefix
            "12345678901",  # 11 digits (exceeds max 10)
            "abc",
            "",
            "   ",
            None,
        ]
        for input_id in invalid_cases:
            item = ScrapedItem(
                provider="gnula",
                slug="item",
                title="Title",
                type=ContentType.MOVIE,
                tmdb_id=input_id,
            )
            assert item.tmdb_id is None, f"Expected None for invalid TMDB ID {input_id}, got {item.tmdb_id}"

            mapping = CanonicalMapping(
                title="Title",
                type=ContentType.MOVIE,
                tmdb_id=input_id,
            )
            assert mapping.tmdb_id is None, f"Expected None for CanonicalMapping TMDB ID {input_id}"

    def test_year_validation_boundaries(self):
        """Test year field boundary conditions (1880 to 2100)."""
        # Valid boundary years
        for yr in [1880, 1900, 2024, 2099, 2100, None]:
            item = ScrapedItem(
                provider="gnula",
                slug="item",
                title="Title",
                type=ContentType.MOVIE,
                year=yr,
            )
            assert item.year == yr

        # Invalid years raising ValidationError
        for yr in [1879, -1, -2024, 0, 2101, 9999]:
            with pytest.raises(ValidationError):
                ScrapedItem(
                    provider="gnula",
                    slug="item",
                    title="Title",
                    type=ContentType.MOVIE,
                    year=yr,
                )

    def test_mutating_default_factories_isolation(self):
        """Verify that mutable default factories (dict, list) are NOT shared across instances."""
        item1 = ScrapedItem(
            provider="gnula",
            slug="item1",
            title="Item 1",
            type=ContentType.MOVIE,
        )
        item2 = ScrapedItem(
            provider="gnula",
            slug="item2",
            title="Item 2",
            type=ContentType.MOVIE,
        )

        assert item1.raw_data is not item2.raw_data
        item1.raw_data["custom_key"] = "custom_val"
        assert "custom_key" not in item2.raw_data

        detail1 = ScrapedDetail(
            provider="serieskao",
            slug="s1",
            title="S1",
            type=ContentType.SERIES,
        )
        detail2 = ScrapedDetail(
            provider="serieskao",
            slug="s2",
            title="S2",
            type=ContentType.SERIES,
        )

        assert detail1.genres is not detail2.genres
        assert detail1.episodes is not detail2.episodes
        assert detail1.extra_identifiers is not detail2.extra_identifiers

        detail1.genres.append("Action")
        detail1.episodes.append(ScrapedEpisode(season=1, episode=1))
        detail1.extra_identifiers["key"] = "val"

        assert len(detail2.genres) == 0
        assert len(detail2.episodes) == 0
        assert len(detail2.extra_identifiers) == 0

        # CanonicalMapping providers isolation
        map1 = CanonicalMapping(title="M1", type=ContentType.MOVIE)
        map2 = CanonicalMapping(title="M2", type=ContentType.MOVIE)
        assert map1.providers is not map2.providers
        map1.add_provider("gnula", "slug1")
        assert "gnula" not in map2.providers

    def test_canonical_mapping_merge_edge_cases(self):
        """Test CanonicalMapping merge with conflicting, empty, and partial fields."""
        m1 = CanonicalMapping(
            tmdb_id=None,
            imdb_id="tt0137523",
            title="",
            type=ContentType.MOVIE,
            year=None,
            providers={"serieskao": "fight-club"},
        )
        m2 = CanonicalMapping(
            tmdb_id="550",
            imdb_id=None,
            title="Fight Club",
            type=ContentType.MOVIE,
            year=1999,
            providers={"gnula": "el-club-de-la-lucha"},
        )

        merged = m1.merge(m2)
        assert merged.tmdb_id == "550"
        assert merged.imdb_id == "tt0137523"
        assert merged.title == "Fight Club"
        assert merged.year == 1999
        assert merged.providers == {
            "serieskao": "fight-club",
            "gnula": "el-club-de-la-lucha",
        }

    def test_orion_export_model_filenames(self):
        """Verify export filenames for providers, imdb, and tmdb models."""
        export_item = IdentityMappingExport(
            provider="SeriesKAO",
            slug="fight-club-1999",
            imdb_id="tt0137523",
            tmdb_id="550",
        )
        fname = export_item.get_export_filename()
        assert fname.startswith("providers/")
        assert fname.endswith(".json")
        # Extract encoded key and verify decode
        encoded_key = fname.removeprefix("providers/").removesuffix(".json")
        p, s = decode_provider_key(encoded_key)
        assert p == "serieskao"
        assert s == "fight-club-1999"

        imdb_export = ImdbIdentityIndexExport(imdb_id="  TT0137523  ")
        assert imdb_export.get_export_filename() == "imdb/tt0137523.json"

        tmdb_export = TmdbIdentityIndexExport(tmdb_id="  550  ")
        assert tmdb_export.get_export_filename() == "tmdb/550.json"


# ============================================================================
# 3. BaseScraper.crawl_catalog Adversarial Stress Tests
# ============================================================================


class InfiniteCatalogScraper(BaseScraper):
    """Mock scraper that returns infinite pages unless stopped."""

    name = "infinite_mock"
    base_url = "https://mock.example.com"
    supported_types: ClassVar[list[ContentType]] = [ContentType.MOVIE, ContentType.SERIES]
    page_size = 10

    def __init__(self, http_client: AsyncHttpClient, max_available_pages: int = 1000) -> None:
        super().__init__(http_client=http_client)
        self.max_available_pages = max_available_pages
        self.pages_called: list[int] = []

    async def fetch_catalog(
        self,
        content_type: ContentType,
        page: int = 1,
        genre: str | None = None,
    ) -> list[ScrapedItem]:
        self.pages_called.append(page)
        if page > self.max_available_pages:
            return []
        return [
            ScrapedItem(
                provider=self.name,
                slug=f"item-p{page}-i{i}",
                title=f"Item Page {page} #{i}",
                type=content_type,
            )
            for i in range(self.page_size)
        ]

    async def fetch_detail(self, slug: str, content_type: ContentType) -> ScrapedDetail | None:
        return None


class ErrorCatalogScraper(BaseScraper):
    """Mock scraper that throws an exception on a specific page."""

    name = "error_mock"
    base_url = "https://error.example.com"

    def __init__(self, http_client: AsyncHttpClient, fail_on_page: int = 1) -> None:
        super().__init__(http_client=http_client)
        self.fail_on_page = fail_on_page
        self.pages_called: list[int] = []

    async def fetch_catalog(
        self,
        content_type: ContentType,
        page: int = 1,
        genre: str | None = None,
    ) -> list[ScrapedItem]:
        self.pages_called.append(page)
        if page == self.fail_on_page:
            raise RuntimeError(f"Simulated network crash on page {page}")
        return [
            ScrapedItem(
                provider=self.name,
                slug=f"error-item-p{page}",
                title=f"Error Item Page {page}",
                type=content_type,
            )
        ]

    async def fetch_detail(self, slug: str, content_type: ContentType) -> ScrapedDetail | None:
        return None


class TestAdversarialBaseScraperCrawlCatalog:
    """Stress-test BaseScraper.crawl_catalog with infinite generators, errors, and limits."""

    @pytest.mark.asyncio
    async def test_crawl_with_max_pages_limit_on_infinite_generator(self, test_settings: Settings):
        """Ensure infinite generator terminates strictly when max_pages is reached."""
        client = AsyncHttpClient(config=test_settings)
        scraper = InfiniteCatalogScraper(http_client=client, max_available_pages=10_000)

        items: list[ScrapedItem] = []
        max_p = 5
        async for item in scraper.crawl_catalog(ContentType.MOVIE, max_pages=max_p):
            items.append(item)

        # Must yield exactly max_pages * page_size items (5 * 10 = 50)
        assert len(items) == 50
        assert scraper.pages_called == [1, 2, 3, 4, 5]
        await client.close()

    @pytest.mark.asyncio
    async def test_crawl_with_zero_items_on_first_page(self, test_settings: Settings):
        """Ensure scraper that returns 0 items on page 1 terminates immediately."""
        client = AsyncHttpClient(config=test_settings)
        scraper = InfiniteCatalogScraper(http_client=client, max_available_pages=0)

        items: list[ScrapedItem] = []
        async for item in scraper.crawl_catalog(ContentType.MOVIE, max_pages=10):
            items.append(item)

        assert len(items) == 0
        assert scraper.pages_called == [1]
        await client.close()

    @pytest.mark.asyncio
    async def test_crawl_with_zero_items_midway(self, test_settings: Settings):
        """Ensure scraper halts cleanly when an empty page is encountered mid-crawl."""
        client = AsyncHttpClient(config=test_settings)
        scraper = InfiniteCatalogScraper(http_client=client, max_available_pages=2)

        items: list[ScrapedItem] = []
        async for item in scraper.crawl_catalog(ContentType.MOVIE, max_pages=10):
            items.append(item)

        assert len(items) == 20  # 2 pages * 10 items
        assert scraper.pages_called == [1, 2, 3]  # Page 3 returned []
        await client.close()

    @pytest.mark.asyncio
    async def test_crawl_with_exception_on_first_page(self, test_settings: Settings):
        """Ensure exception on page 1 does not raise unhandled crash to caller."""
        client = AsyncHttpClient(config=test_settings)
        scraper = ErrorCatalogScraper(http_client=client, fail_on_page=1)

        items: list[ScrapedItem] = []
        async for item in scraper.crawl_catalog(ContentType.MOVIE, max_pages=5):
            items.append(item)

        assert len(items) == 0
        assert scraper.pages_called == [1]
        await client.close()

    @pytest.mark.asyncio
    async def test_crawl_with_exception_midway(self, test_settings: Settings):
        """Ensure items before the failed page are yielded and crawl halts gracefully."""
        client = AsyncHttpClient(config=test_settings)
        scraper = ErrorCatalogScraper(http_client=client, fail_on_page=3)

        items: list[ScrapedItem] = []
        async for item in scraper.crawl_catalog(ContentType.MOVIE, max_pages=5):
            items.append(item)

        assert len(items) == 2  # Pages 1 and 2 yielded
        assert scraper.pages_called == [1, 2, 3]  # Page 3 raised exception
        await client.close()

    @pytest.mark.asyncio
    async def test_crawl_with_max_pages_zero(self, test_settings: Settings):
        """Ensure max_pages=0 immediately returns without requesting any pages."""
        client = AsyncHttpClient(config=test_settings)
        scraper = InfiniteCatalogScraper(http_client=client)

        items: list[ScrapedItem] = []
        async for item in scraper.crawl_catalog(ContentType.MOVIE, max_pages=0):
            items.append(item)

        assert len(items) == 0
        assert len(scraper.pages_called) == 0
        await client.close()

    @pytest.mark.asyncio
    async def test_crawl_with_unsupported_content_type(self, test_settings: Settings):
        """Ensure crawl on unsupported content type yields 0 items and makes 0 calls."""
        client = AsyncHttpClient(config=test_settings)

        class MovieOnlyScraper(BaseScraper):
            name = "movie_only"
            base_url = "https://movie.example.com"
            supported_types: ClassVar[list[ContentType]] = [ContentType.MOVIE]

            async def fetch_catalog(self, content_type, page=1, genre=None):
                return [ScrapedItem(provider=self.name, slug="m1", title="M1", type=content_type)]

            async def fetch_detail(self, slug, content_type):
                return None

        scraper = MovieOnlyScraper(http_client=client)
        items = [item async for item in scraper.crawl_catalog(ContentType.SERIES, max_pages=5)]
        assert len(items) == 0
        await client.close()


# ============================================================================
# 4. TokenBucketLimiter & HTTP Client Concurrency Stress Tests
# ============================================================================


class TestAdversarialRateLimiterAndHttp:
    """Stress-test rate limiter under concurrent task contention and HTTP client edge conditions."""

    @pytest.mark.asyncio
    async def test_high_concurrency_token_acquisition(self):
        """Run 50 concurrent tasks acquiring tokens from a single limiter."""
        limiter = TokenBucketLimiter(rate=100.0, capacity=20.0)
        start_time = time.monotonic()

        async def worker(worker_id: int):
            for _ in range(5):
                await limiter.acquire(1.0)

        # 50 workers * 5 acquires = 250 tokens
        # At rate 100/s with 20 burst capacity: (250 - 20) / 100 ~= 2.3 seconds
        await asyncio.gather(*(worker(i) for i in range(50)))
        elapsed = time.monotonic() - start_time
        assert elapsed >= 2.0, f"Completed too fast ({elapsed:.2f}s), rate limiter leaked tokens!"

    @pytest.mark.asyncio
    async def test_invalid_limiter_parameters(self):
        """Ensure invalid rate/capacity values raise ValueError."""
        with pytest.raises(ValueError, match="Rate must be > 0"):
            TokenBucketLimiter(rate=0)

        with pytest.raises(ValueError, match="Rate must be > 0"):
            TokenBucketLimiter(rate=-10)

        limiter = TokenBucketLimiter(rate=5.0, capacity=5.0)
        with pytest.raises(ValueError, match="exceeds bucket capacity"):
            await limiter.acquire(10.0)

    @respx.mock
    @pytest.mark.asyncio
    async def test_http_retry_exhaustion_on_network_error(self, test_settings: Settings):
        """Test HTTP client retry exhaustion when facing repeated network failures."""
        respx.get("https://fail.example.com/api").mock(
            side_effect=httpx.ConnectError("Network unreachable")
        )

        client = AsyncHttpClient(config=test_settings)
        with pytest.raises(MaxRetriesExceededError):
            await client.get("https://fail.example.com/api", headers={"X-Custom": "test"})

        await client.close()
