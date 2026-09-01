from typing import ClassVar

import pytest

from orion_mapper.core.http import AsyncHttpClient
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


def test_scraped_item_normalization():
    item = ScrapedItem(
        provider=" SeriesKao ",
        slug="/zombieland-saga/ ",
        title="  Zombieland Saga  ",
        type=ContentType.SERIES,
        year=2018,
        imdb_id="TT015486",
        tmdb_id=" 21048 ",
    )
    assert item.provider == "serieskao"
    assert item.slug == "zombieland-saga"
    assert item.title == "Zombieland Saga"
    assert item.imdb_id == "tt015486"
    assert item.tmdb_id == "21048"
    assert item.type == ContentType.SERIES


def test_scraped_item_numeric_imdb_conversion():
    item = ScrapedItem(
        provider="gnula",
        slug="test-item",
        title="Test Item",
        type=ContentType.MOVIE,
        imdb_id="1234567",
    )
    assert item.imdb_id == "tt1234567"


def test_scraped_item_invalid_identifiers_handling():
    item = ScrapedItem(
        provider="gnula",
        slug="sample-movie",
        title="Sample",
        type=ContentType.MOVIE,
        imdb_id="invalid_id",
        tmdb_id="not_a_number",
    )
    assert item.imdb_id is None
    assert item.tmdb_id is None


def test_scraped_detail_episodes_and_fields():
    detail = ScrapedDetail(
        provider="allcalidad",
        slug="game-of-thrones",
        title="Game of Thrones",
        type=ContentType.SERIES,
        genres=["Action", "Drama"],
        episodes=[
            ScrapedEpisode(season=1, episode=1, title="Winter is Coming"),
            ScrapedEpisode(season=1, episode=2, title="The Kingsroad"),
        ],
        seasons_count=8,
        release_date="2011-04-17",
        extra_identifiers={"player_url": "https://vid.example.com/play/1"},
    )
    assert len(detail.episodes) == 2
    assert detail.episodes[0].season == 1
    assert detail.episodes[0].episode == 1
    assert detail.episodes[1].title == "The Kingsroad"
    assert "Drama" in detail.genres
    assert detail.seasons_count == 8
    assert detail.extra_identifiers["player_url"] == "https://vid.example.com/play/1"


def test_canonical_mapping_add_provider_and_merge():
    m1 = CanonicalMapping(
        tmdb_id="21048",
        imdb_id=None,
        title="Zombieland Saga",
        type=ContentType.SERIES,
        year=2018,
        providers={"gnula": "/pelicula-zombieland-saga/"},
    )
    assert m1.providers["gnula"] == "pelicula-zombieland-saga"

    m1.add_provider("serieskao", " /zombieland-saga/ ")
    assert m1.providers["serieskao"] == "zombieland-saga"

    m2 = CanonicalMapping(
        tmdb_id="21048",
        imdb_id="tt15486",
        title="",
        type=ContentType.SERIES,
        year=2018,
        providers={"poseidonhd2": "zombieland-saga"},
    )

    merged = m1.merge(m2)
    assert merged.imdb_id == "tt15486"
    assert merged.tmdb_id == "21048"
    assert merged.title == "Zombieland Saga"
    assert len(merged.providers) == 3
    assert merged.providers["gnula"] == "pelicula-zombieland-saga"
    assert merged.providers["serieskao"] == "zombieland-saga"
    assert merged.providers["poseidonhd2"] == "zombieland-saga"


def test_base64url_unpadded_encoding():
    provider = "gnula"
    slug = "pelicula-zombieland-saga"
    encoded = encode_provider_key(provider, slug)

    # Must be unpadded (no '=')
    assert "=" not in encoded

    # Round-trip verification
    p_dec, s_dec = decode_provider_key(encoded)
    assert p_dec == provider
    assert s_dec == slug


def test_base64url_special_characters():
    provider = "serieskao"
    slug = "ver-pelicula-123_45~test"
    encoded = encode_provider_key(provider, slug)
    assert "=" not in encoded
    p_dec, s_dec = decode_provider_key(encoded)
    assert p_dec == provider
    assert s_dec == slug


def test_orion_export_models_and_paths():
    export_item = IdentityMappingExport(
        provider="gnula",
        slug="pelicula-zombieland-saga",
        imdb_id="tt15486",
        tmdb_id="21048",
        type="series",
    )
    filename = export_item.get_export_filename()
    assert filename.startswith("providers/")
    assert filename.endswith(".json")

    json_data = export_item.model_dump(by_alias=True)
    assert json_data["provider"] == "gnula"
    assert json_data["updatedAt"] > 0
    assert json_data["imdb_id"] == "tt15486"
    assert json_data["tmdb_id"] == "21048"

    imdb_index = ImdbIdentityIndexExport(
        imdb_id="tt15486",
        tmdb_id="21048",
        type="series",
        providers={"gnula": "pelicula-zombieland-saga"},
    )
    assert imdb_index.get_export_filename() == "imdb/tt15486.json"

    tmdb_index = TmdbIdentityIndexExport(
        tmdb_id="21048",
        imdb_id="tt15486",
    )
    assert tmdb_index.get_export_filename() == "tmdb/21048.json"


@pytest.mark.asyncio
async def test_base_scraper_abc_contract(test_settings):
    class IncompleteScraper(BaseScraper):
        pass

    with pytest.raises(TypeError):
        IncompleteScraper(http_client=AsyncHttpClient(config=test_settings))

    class ValidDummyScraper(BaseScraper):
        name = "dummy"
        base_url = "https://dummy.example.com"

        async def fetch_catalog(self, content_type, page=1, genre=None):
            if page > 2:
                return []
            return [
                ScrapedItem(
                    provider=self.name,
                    slug=f"item-{page}",
                    title=f"Item {page}",
                    type=content_type,
                )
            ]

        async def fetch_detail(self, slug, content_type):
            return ScrapedDetail(
                provider=self.name,
                slug=slug,
                title=f"Detail for {slug}",
                type=content_type,
            )

    client = AsyncHttpClient(config=test_settings)
    scraper = ValidDummyScraper(http_client=client)

    assert scraper.build_url("/test") == "https://dummy.example.com/test"
    assert scraper.extract_identifiers({}) == (None, None)

    # Test crawl_catalog generator
    items = []
    async for item in scraper.crawl_catalog(ContentType.MOVIE, max_pages=5):
        items.append(item)

    await client.close()


@pytest.mark.asyncio
async def test_base_scraper_missing_name_or_base_url(test_settings):
    client = AsyncHttpClient(config=test_settings)

    class MissingNameScraper(BaseScraper):
        base_url = "https://example.com"

        async def fetch_catalog(self, content_type, page=1, genre=None):
            return []

        async def fetch_detail(self, slug, content_type):
            return None

    with pytest.raises(ValueError, match="must define 'name'"):
        MissingNameScraper(http_client=client)

    class MissingBaseUrlScraper(BaseScraper):
        name = "test"

        async def fetch_catalog(self, content_type, page=1, genre=None):
            return []

        async def fetch_detail(self, slug, content_type):
            return None

    with pytest.raises(ValueError, match="must define 'base_url'"):
        MissingBaseUrlScraper(http_client=client)

    await client.close()


@pytest.mark.asyncio
async def test_base_scraper_crawl_unsupported_type_and_error_handling(test_settings):
    client = AsyncHttpClient(config=test_settings)

    class FlakyScraper(BaseScraper):
        name = "flaky"
        base_url = "https://example.com"
        supported_types: ClassVar[list[ContentType]] = [ContentType.MOVIE]

        async def fetch_catalog(self, content_type, page=1, genre=None):
            if page == 1:
                return [ScrapedItem(provider=self.name, slug="m1", title="M1", type=content_type)]
            raise RuntimeError("Scraping failed")

        async def fetch_detail(self, slug, content_type):
            return None

    scraper = FlakyScraper(http_client=client)

    # Unsupported content type should yield nothing
    series_items = [item async for item in scraper.crawl_catalog(ContentType.SERIES)]
    assert len(series_items) == 0

    # Failing page stops gracefully
    movie_items = [item async for item in scraper.crawl_catalog(ContentType.MOVIE)]
    assert len(movie_items) == 1

    await client.close()


def test_models_none_and_edge_inputs():
    item = ScrapedItem(
        provider="TEST",
        slug="TEST_SLUG",
        title="",
        type=ContentType.MOVIE,
        imdb_id=None,
        tmdb_id=None,
    )
    assert item.provider == "test"
    assert item.slug == "TEST_SLUG"
    assert item.title == ""

    m = CanonicalMapping(
        title="Title",
        type=ContentType.SERIES,
        imdb_id="invalid",
        tmdb_id="invalid",
        providers=None,
    )
    assert m.imdb_id is None
    assert m.tmdb_id is None
    assert m.providers == {}

