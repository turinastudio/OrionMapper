from __future__ import annotations

import time

import pytest

from orion_mapper.matcher.reconciler import IdentityReconciler
from orion_mapper.models.item import ContentType, ScrapedItem
from orion_mapper.models.mapping import CanonicalMapping
from orion_mapper.resolver.tmdb import TmdbClient


class MockStore:
    def __init__(self):
        self.by_tmdb: dict[tuple[str, str], CanonicalMapping] = {}
        self.by_imdb: dict[tuple[str, str], CanonicalMapping] = {}

    def get_by_tmdb(self, tmdb_id: str, content_type: str) -> CanonicalMapping | None:
        return self.by_tmdb.get((str(tmdb_id), str(content_type).lower()))

    def get_by_imdb(self, imdb_id: str, content_type: str) -> CanonicalMapping | None:
        return self.by_imdb.get((str(imdb_id), str(content_type).lower()))

    def save_mapping(self, mapping: CanonicalMapping) -> None:
        c_type = str(mapping.type).lower()
        if mapping.tmdb_id:
            self.by_tmdb[(str(mapping.tmdb_id), c_type)] = mapping
        if mapping.imdb_id:
            self.by_imdb[(str(mapping.imdb_id), c_type)] = mapping


@pytest.mark.asyncio
async def test_reconciler_priority1_direct_both_ids():
    class FailingTmdbClient(TmdbClient):
        async def find_by_imdb_id(self, imdb_id):
            raise AssertionError("Should not make network call")

        async def get_external_ids(self, tmdb_id, media_type):
            raise AssertionError("Should not make network call")

        async def search(self, title, media_type, year=None, language="es-MX"):
            raise AssertionError("Should not make network call")

        async def get_details(self, tmdb_id, media_type, language="es-419"):
            return {"title": "Fight Club", "release_date": "1999-10-15"}

    reconciler = IdentityReconciler(tmdb_client=FailingTmdbClient())
    item = ScrapedItem(
        provider="serieskao",
        slug="fight-club",
        title="Fight Club",
        type=ContentType.MOVIE,
        year=1999,
        imdb_id="tt0137523",
        tmdb_id="550",
    )
    mapping = await reconciler.reconcile_item(item)
    assert mapping is not None
    assert mapping.tmdb_id == "550"
    assert mapping.imdb_id == "tt0137523"
    assert mapping.providers["serieskao"] == "fight-club"


@pytest.mark.asyncio
async def test_reconciler_priority2_direct_imdb_only(mock_http_client):
    tmdb = TmdbClient(http_client=mock_http_client)
    reconciler = IdentityReconciler(tmdb_client=tmdb)

    item = ScrapedItem(
        provider="serieskao",
        slug="fight-club",
        title="Fight Club",
        type=ContentType.MOVIE,
        imdb_id="tt0137523",
    )
    mapping = await reconciler.reconcile_item(item)
    assert mapping is not None
    assert mapping.tmdb_id == "550"
    assert mapping.imdb_id == "tt0137523"
    assert mapping.providers["serieskao"] == "fight-club"


@pytest.mark.asyncio
async def test_reconciler_priority3_direct_tmdb_only(mock_http_client):
    tmdb = TmdbClient(http_client=mock_http_client)
    reconciler = IdentityReconciler(tmdb_client=tmdb)

    item = ScrapedItem(
        provider="poseidonhd2",
        slug="fight-club",
        title="Fight Club",
        type=ContentType.MOVIE,
        tmdb_id="550",
    )
    mapping = await reconciler.reconcile_item(item)
    assert mapping is not None
    assert mapping.tmdb_id == "550"
    assert mapping.imdb_id == "tt0137523"
    assert mapping.providers["poseidonhd2"] == "fight-club"


@pytest.mark.asyncio
async def test_reconciler_priority4_fuzzy_search_success(mock_http_client):
    tmdb = TmdbClient(http_client=mock_http_client)
    reconciler = IdentityReconciler(tmdb_client=tmdb, allow_title_match=True)

    item = ScrapedItem(
        provider="gnula",
        slug="pelicula-el-club-de-la-lucha",
        title="Fight Club",
        type=ContentType.MOVIE,
        year=1999,
    )
    mapping = await reconciler.reconcile_item(item)
    assert mapping is not None
    assert mapping.tmdb_id == "550"
    assert mapping.imdb_id == "tt0137523"
    assert mapping.providers["gnula"] == "pelicula-el-club-de-la-lucha"


@pytest.mark.asyncio
async def test_reconciler_priority4_unresolvable_title_returns_none(mock_http_client):
    tmdb = TmdbClient(http_client=mock_http_client)
    reconciler = IdentityReconciler(tmdb_client=tmdb, allow_title_match=True)

    item = ScrapedItem(
        provider="gnula",
        slug="unknown-random-movie-12345",
        title="Unknown Random Movie 12345",
        type=ContentType.MOVIE,
        year=1920,
    )
    mapping = await reconciler.reconcile_item(item)
    assert mapping is None


@pytest.mark.asyncio
async def test_reconciler_merge_with_master_store(mock_http_client):
    store = MockStore()
    tmdb = TmdbClient(http_client=mock_http_client)
    reconciler = IdentityReconciler(tmdb_client=tmdb)

    item1 = ScrapedItem(
        provider="serieskao",
        slug="fight-club-sk",
        title="Fight Club",
        type=ContentType.MOVIE,
        imdb_id="tt0137523",
    )
    m1 = await reconciler.reconcile_item(item1, master_store=store)
    store.save_mapping(m1)

    item2 = ScrapedItem(
        provider="allcalidad",
        slug="fight-club-ac",
        title="Fight Club",
        type=ContentType.MOVIE,
        tmdb_id="550",
    )
    m2 = await reconciler.reconcile_item(item2, master_store=store)
    store.save_mapping(m2)

    loaded = store.get_by_tmdb("550", "movie")
    assert loaded is not None
    assert loaded.providers.get("serieskao") == "fight-club-sk"
    assert loaded.providers.get("allcalidad") == "fight-club-ac"


@pytest.mark.asyncio
async def test_reconciler_idempotent_provider_submission(mock_http_client):
    store = MockStore()
    tmdb = TmdbClient(http_client=mock_http_client)
    reconciler = IdentityReconciler(tmdb_client=tmdb)

    item = ScrapedItem(
        provider="serieskao",
        slug="fight-club",
        title="Fight Club",
        type=ContentType.MOVIE,
        imdb_id="tt0137523",
    )
    m1 = await reconciler.reconcile_item(item, master_store=store)
    store.save_mapping(m1)
    m2 = await reconciler.reconcile_item(item, master_store=store)
    assert m2.providers["serieskao"] == "fight-club"


@pytest.mark.asyncio
async def test_reconciler_batch_empty_list(mock_http_client):
    tmdb = TmdbClient(http_client=mock_http_client)
    reconciler = IdentityReconciler(tmdb_client=tmdb)
    res = await reconciler.reconcile_batch([])
    assert res == []


@pytest.mark.asyncio
async def test_reconciler_batch_coalesces_same_entity(mock_http_client):
    tmdb = TmdbClient(http_client=mock_http_client)
    reconciler = IdentityReconciler(tmdb_client=tmdb)

    items = [
        ScrapedItem(provider="serieskao", slug="fc-sk", title="Fight Club", type=ContentType.MOVIE, imdb_id="tt0137523"),
        ScrapedItem(provider="poseidonhd2", slug="fc-pos", title="Fight Club", type=ContentType.MOVIE, tmdb_id="550"),
        ScrapedItem(provider="allcalidad", slug="fc-ac", title="Fight Club", type=ContentType.MOVIE, tmdb_id="550"),
    ]
    mappings = await reconciler.reconcile_batch(items)
    assert len(mappings) == 1
    m = mappings[0]
    assert m.tmdb_id == "550"
    assert m.imdb_id == "tt0137523"
    assert len(m.providers) == 3
    assert m.providers["serieskao"] == "fc-sk"
    assert m.providers["poseidonhd2"] == "fc-pos"
    assert m.providers["allcalidad"] == "fc-ac"


@pytest.mark.asyncio
async def test_reconciler_batch_multiple_distinct_entities(mock_http_client):
    tmdb = TmdbClient(http_client=mock_http_client)
    reconciler = IdentityReconciler(tmdb_client=tmdb)

    items = [
        ScrapedItem(provider="serieskao", slug="fc-sk", title="Fight Club", type=ContentType.MOVIE, imdb_id="tt0137523"),
        ScrapedItem(provider="serieskao", slug="zb-sk", title="Zombieland Saga", type=ContentType.SERIES, imdb_id="tt15486"),
    ]
    mappings = await reconciler.reconcile_batch(items)
    assert len(mappings) == 2


@pytest.mark.asyncio
async def test_reconciler_updated_at_timestamp_freshness(mock_http_client):
    tmdb = TmdbClient(http_client=mock_http_client)
    reconciler = IdentityReconciler(tmdb_client=tmdb)

    before = int(time.time() * 1000)
    item = ScrapedItem(provider="serieskao", slug="fc", title="Fight Club", type=ContentType.MOVIE, imdb_id="tt0137523")
    m = await reconciler.reconcile_item(item)
    assert m.updated_at >= before


@pytest.mark.asyncio
async def test_reconciler_tv_media_type_detection(mock_http_client):
    tmdb = TmdbClient(http_client=mock_http_client)
    reconciler = IdentityReconciler(tmdb_client=tmdb)

    item = ScrapedItem(provider="serieskao", slug="zombieland", title="Zombieland Saga", type=ContentType.SERIES, imdb_id="tt15486")
    m = await reconciler.reconcile_item(item)
    assert m.type == ContentType.SERIES
    assert m.tmdb_id == "82856"


@pytest.mark.asyncio
async def test_reconciler_exception_shielding_network_error():
    class CrashingTmdbClient(TmdbClient):
        async def find_by_imdb_id(self, imdb_id):
            raise ConnectionError("Simulated network failure")

        async def get_external_ids(self, tmdb_id, media_type):
            raise TimeoutError("Simulated timeout failure")

        async def get_details(self, tmdb_id, media_type, language="es-419"):
            raise TimeoutError("Simulated details timeout")

        async def search(self, title, media_type, year=None, language="es-MX"):
            raise RuntimeError("Simulated search error")

    reconciler = IdentityReconciler(tmdb_client=CrashingTmdbClient())

    # Item with imdb_id only -> find_by_imdb_id crashes -> handled gracefully
    item_imdb = ScrapedItem(provider="p1", slug="s1", title="Title", type=ContentType.MOVIE, imdb_id="tt12345")
    m1 = await reconciler.reconcile_item(item_imdb)
    assert m1 is not None
    assert m1.imdb_id == "tt12345"

    # Item with tmdb_id only -> get_external_ids crashes -> handled gracefully
    item_tmdb = ScrapedItem(provider="p2", slug="s2", title="Title", type=ContentType.MOVIE, tmdb_id="999")
    m2 = await reconciler.reconcile_item(item_tmdb)
    assert m2 is not None
    assert m2.tmdb_id == "999"

    # Item with no IDs -> search crashes -> returns None without crashing batch
    item_search = ScrapedItem(provider="p3", slug="s3", title="Unknown", type=ContentType.MOVIE)
    m3 = await reconciler.reconcile_item(item_search)
    assert m3 is None

    # Batch reconciliation with mixture does not crash
    batch_res = await reconciler.reconcile_batch([item_imdb, item_tmdb, item_search])
    assert len(batch_res) == 2


@pytest.mark.asyncio
async def test_reconciler_transitive_coalescing_order_invariance():
    class StubTmdbClient(TmdbClient):
        async def find_by_imdb_id(self, imdb_id):
            return None

        async def get_external_ids(self, tmdb_id, media_type):
            return None

        async def get_details(self, tmdb_id, media_type, language="es-419"):
            return None

        async def search(self, title, media_type, year=None, language="es-MX"):
            return []

    reconciler = IdentityReconciler(tmdb_client=StubTmdbClient())

    i1 = ScrapedItem(provider="p1", slug="s1", title="A", type=ContentType.MOVIE, tmdb_id="100")
    i2 = ScrapedItem(provider="p2", slug="s2", title="A", type=ContentType.MOVIE, imdb_id="tt200")
    i3 = ScrapedItem(provider="p3", slug="s3", title="A", type=ContentType.MOVIE, tmdb_id="100", imdb_id="tt200")

    # Order 1
    res1 = await reconciler.reconcile_batch([i1, i2, i3])
    assert len(res1) == 1
    assert res1[0].tmdb_id == "100"
    assert res1[0].imdb_id == "tt200"
    assert len(res1[0].providers) == 3

    # Order 2
    res2 = await reconciler.reconcile_batch([i3, i1, i2])
    assert len(res2) == 1
    assert res2[0].tmdb_id == "100"
    assert res2[0].imdb_id == "tt200"
    assert len(res2[0].providers) == 3

    # Order 3
    res3 = await reconciler.reconcile_batch([i2, i3, i1])
    assert len(res3) == 1
    assert len(res3[0].providers) == 3
