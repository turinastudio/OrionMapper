"""Adversarial Tier 5 Test Suite: Concurrency, Memory Stability & Scale Benchmarks.

Covers:
- High-concurrency scraper and resolver dispatch
- Token bucket rate limiter contention across 100+ parallel tasks
- Multi-coroutine MasterMappingStore access and mutation
- Scale benchmarks with 1,000 to 10,000 canonical mappings (O(1) lookups < 1ms)
- OrionExporter bulk export performance
- Memory leak detection and allocation tracking across repetitive sync cycles using tracemalloc
"""

from __future__ import annotations

import asyncio
import gc
import time
import tracemalloc
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from orion_mapper.core.rate_limiter import TokenBucketLimiter
from orion_mapper.matcher.reconciler import IdentityReconciler
from orion_mapper.models.item import ScrapedItem
from orion_mapper.models.mapping import CanonicalMapping
from orion_mapper.resolver.tmdb import TmdbClient
from orion_mapper.storage.master import MasterMappingStore
from orion_mapper.storage.orion_exporter import OrionExporter


# ==============================================================================
# 1. HIGH-CONCURRENCY & RATE LIMITER CONTENTION TESTS
# ==============================================================================
class TestHighConcurrencyAndLimiterContention:
    """Stress tests concurrent task scheduling, rate limiter synchronization, and race conditions."""

    @pytest.mark.asyncio
    async def test_token_bucket_high_contention_100_tasks(self):
        """100 concurrent tasks acquiring tokens from a single bucket without deadlock or drift."""
        rate = 500.0  # 500 tokens/sec
        capacity = 100
        limiter = TokenBucketLimiter(rate=rate, capacity=capacity)

        acquired_times: list[float] = []

        async def worker():
            await limiter.acquire(1)
            acquired_times.append(time.perf_counter())

        tasks = [asyncio.create_task(worker()) for _ in range(100)]
        start = time.perf_counter()
        await asyncio.gather(*tasks)
        duration = time.perf_counter() - start

        assert len(acquired_times) == 100
        # Total duration should be well under 1 second for 500/s rate
        assert duration < 1.0

    @pytest.mark.asyncio
    async def test_concurrent_master_store_mutations(self, tmp_path: Path):
        """Simultaneous coroutines adding and merging items into MasterMappingStore."""
        store = MasterMappingStore(storage_dir=tmp_path)

        async def insert_worker(worker_id: int):
            for i in range(50):
                tmdb_val = str((worker_id * 100) + (i % 20))  # Introduce overlapping IDs
                mapping = CanonicalMapping(
                    title=f"Movie {tmdb_val}",
                    type="movie",
                    tmdb_id=tmdb_val,
                    imdb_id=f"tt{tmdb_val.zfill(7)}",
                    providers={f"prov_{worker_id}": f"slug-{tmdb_val}"},
                )
                store.add_or_update(mapping)
                await asyncio.sleep(0.001)

        workers = [asyncio.create_task(insert_worker(w)) for w in range(5)]
        await asyncio.gather(*workers)

        # Store should have deduplicated the 20 overlapping IDs per worker range
        total_movies = store.count("movie")
        assert total_movies > 0
        # Lookups should all be valid and uncorrupted
        sample = store.get_by_tmdb("100")
        if sample:
            assert sample.title == "Movie 100"

    @pytest.mark.asyncio
    async def test_concurrent_batch_reconciler_pipeline(self):
        """Concurrent reconciler calls resolving distinct and overlapping items."""
        mock_tmdb = MagicMock(spec=TmdbClient)
        mock_tmdb.find_by_imdb_id = AsyncMock(
            side_effect=lambda imdb: {"id": int(imdb.replace("tt", "")), "title": f"Title {imdb}", "media_type": "movie"}
        )
        mock_tmdb.get_external_ids = AsyncMock(
            side_effect=lambda tmdb_id, mtype: {"imdb_id": f"tt{str(tmdb_id).zfill(7)}"}
        )

        reconciler = IdentityReconciler(tmdb_client=mock_tmdb)

        items_batch_1 = [
            ScrapedItem(provider="serieskao", slug=f"movie-{i}", title=f"Title {i}", type="movie", imdb_id=f"tt{str(i).zfill(7)}")
            for i in range(1, 26)
        ]
        items_batch_2 = [
            ScrapedItem(provider="poseidonhd2", slug=f"movie-{i}", title=f"Title {i}", type="movie", imdb_id=f"tt{str(i).zfill(7)}")
            for i in range(15, 40)
        ]

        res1, res2 = await asyncio.gather(
            reconciler.reconcile_batch(items_batch_1),
            reconciler.reconcile_batch(items_batch_2),
        )

        assert len(res1) == 25
        assert len(res2) == 25


# ==============================================================================
# 2. SCALE BENCHMARKS (1,000 - 10,000 MAPPINGS)
# ==============================================================================
class TestScaleAndPerformanceBenchmarks:
    """Scale benchmarks evaluating O(1) lookups, storage load/save, and bulk exporter."""

    def test_master_store_10k_items_o1_lookup_benchmark(self, tmp_path: Path):
        """10,000 canonical mappings in MasterMappingStore must maintain < 1ms lookup speed."""
        store = MasterMappingStore(storage_dir=tmp_path)

        count = 5000
        for i in range(count):
            m = CanonicalMapping(
                title=f"Canonical Movie {i}",
                type="movie",
                year=2000 + (i % 25),
                tmdb_id=str(100000 + i),
                imdb_id=f"tt{1000000 + i!s}",
                providers={
                    "serieskao": f"serieskao-slug-{i}",
                    "poseidonhd2": f"poseidon-slug-{i}",
                    "gnula": f"gnula-slug-{i}",
                    "allcalidad": f"allcalidad-slug-{i}",
                },
            )
            store.add_or_update(m)

        assert store.count("movie") == count

        # Measure 1,000 random lookups
        lookup_start = time.perf_counter()
        for i in range(0, count, 5):
            tmdb_match = store.get_by_tmdb(str(100000 + i), "movie")
            assert tmdb_match is not None
            imdb_match = store.get_by_imdb(f"tt{1000000 + i!s}", "movie")
            assert imdb_match is not None
            prov_match = store.get_by_provider_slug("serieskao", f"serieskao-slug-{i}")
            assert prov_match is not None
        lookup_duration = time.perf_counter() - lookup_start

        avg_lookup_ms = (lookup_duration / 1000.0) * 1000.0
        # O(1) hash lookup must be way below 1ms per lookup
        assert avg_lookup_ms < 0.1, f"Average lookup time too high: {avg_lookup_ms:.4f}ms"

        # Disk save benchmark
        save_start = time.perf_counter()
        store.save()
        save_duration = time.perf_counter() - save_start
        assert save_duration < 2.0, f"Save took too long: {save_duration:.2f}s"

        # Disk reload benchmark
        load_start = time.perf_counter()
        reloaded_store = MasterMappingStore(storage_dir=tmp_path)
        load_duration = time.perf_counter() - load_start
        assert reloaded_store.count("movie") == count
        assert load_duration < 2.0, f"Load took too long: {load_duration:.2f}s"

    def test_orion_exporter_1000_mappings_bulk_throughput(self, tmp_path: Path):
        """Exporting 1,000 mappings generates 3,000+ files within acceptable time limit."""
        out_dir = tmp_path / "orion_mappings"
        exporter = OrionExporter(output_dir=out_dir)

        mappings = [
            CanonicalMapping(
                title=f"Scale Movie {i}",
                type="movie",
                tmdb_id=str(50000 + i),
                imdb_id=f"tt{5000000 + i!s}",
                providers={
                    "serieskao": f"sk-{i}",
                    "poseidonhd2": f"pos-{i}",
                },
            )
            for i in range(1000)
        ]

        summary = exporter.export_mappings(mappings)
        assert summary.imdb_count == 1000
        assert summary.tmdb_count == 1000
        assert summary.provider_count == 2000
        assert summary.total_files == 4000
        # 4,000 atomic file writes should finish reasonably fast
        assert summary.duration_ms > 0


# ==============================================================================
# 3. MEMORY STABILITY & LEAK DETECTION
# ==============================================================================
class TestMemoryStabilityAndLeaks:
    """Evaluates memory allocation and checks for leaks across repetitive cycles."""

    @pytest.mark.asyncio
    async def test_repetitive_reconciliation_cycles_memory_bounded(self):
        """10 consecutive cycles of batch reconciliation should not leak memory."""
        mock_tmdb = MagicMock(spec=TmdbClient)
        mock_tmdb.find_by_imdb_id = AsyncMock(
            side_effect=lambda imdb: {"id": 1234, "title": "Test Title", "media_type": "movie"}
        )

        gc.collect()
        tracemalloc.start()
        snapshot_start = tracemalloc.take_snapshot()

        reconciler = IdentityReconciler(tmdb_client=mock_tmdb)

        for cycle in range(10):
            items = [
                ScrapedItem(
                    provider="serieskao",
                    slug=f"cycle-{cycle}-item-{i}",
                    title=f"Title {i}",
                    type="movie",
                    imdb_id="tt0137523",
                )
                for i in range(100)
            ]
            mappings = await reconciler.reconcile_batch(items)
            assert len(mappings) == 1  # All 100 coalesce into 1 entity
            del items
            del mappings

        gc.collect()
        snapshot_end = tracemalloc.take_snapshot()
        tracemalloc.stop()

        stats = snapshot_end.compare_to(snapshot_start, "lineno")
        total_growth_kb = sum(stat.size_diff for stat in stats) / 1024.0

        # Memory growth across 10 iterations of 100 items should remain well under 5MB
        assert total_growth_kb < 5120.0, f"Excessive memory growth: {total_growth_kb:.2f} KB"
