from __future__ import annotations

import logging
import time
from typing import Any

from orion_mapper.matcher.normalizer import TitleNormalizer
from orion_mapper.matcher.scoring import CandidateScorer
from orion_mapper.models.item import ContentType, ScrapedDetail, ScrapedItem
from orion_mapper.models.mapping import CanonicalMapping
from orion_mapper.resolver.tmdb import TmdbClient

logger = logging.getLogger(__name__)


class IdentityReconciler:
    """Multi-provider identity reconciler and canonical mapping aggregator."""

    def __init__(
        self,
        tmdb_client: TmdbClient,
        normalizer: type[TitleNormalizer] | None = None,
        scorer: type[CandidateScorer] | None = None,
        confidence_threshold: float = 88.0,
    ) -> None:
        self.tmdb_client = tmdb_client
        self.normalizer = normalizer or TitleNormalizer
        self.scorer = scorer or CandidateScorer
        self.confidence_threshold = confidence_threshold

    async def reconcile_item(
        self,
        item: ScrapedItem | ScrapedDetail,
        master_store: Any | None = None,
    ) -> CanonicalMapping | None:
        """
        Reconcile a single scraped item to canonical TMDB/IMDb IDs using 4-tier priority resolution.
        Merges into existing master store mappings if available.
        """
        # 1. Initial lookup in master store if existing
        existing: CanonicalMapping | None = None
        if master_store is not None:
            if item.tmdb_id and hasattr(master_store, "get_by_tmdb"):
                existing = master_store.get_by_tmdb(item.tmdb_id, item.type)
            if not existing and item.imdb_id and hasattr(master_store, "get_by_imdb"):
                existing = master_store.get_by_imdb(item.imdb_id, item.type)

        tmdb_id: str | None = item.tmdb_id
        imdb_id: str | None = item.imdb_id
        resolved_title: str = item.title
        resolved_year: int | None = item.year
        resolved_type: ContentType = item.type

        # Priority 1: Both IDs present -> zero network requests
        if tmdb_id and imdb_id:
            pass

        # Priority 2: Direct IMDb ID present -> TMDB /3/find/{imdb_id}
        elif imdb_id and not tmdb_id:
            try:
                find_res = await self.tmdb_client.find_by_imdb_id(imdb_id)
            except Exception as exc:
                logger.warning("Error resolving IMDb ID %s via TMDB find: %s", imdb_id, exc)
                find_res = None

            if find_res:
                tmdb_id = str(find_res["id"]) if find_res.get("id") is not None else None
                if not resolved_title:
                    resolved_title = (
                        find_res.get("title")
                        or find_res.get("name")
                        or resolved_title
                    )
                if resolved_year is None:
                    cand_year = self.scorer.extract_year(find_res)
                    if cand_year is not None:
                        resolved_year = cand_year
                media_type = str(find_res.get("media_type") or "").lower()
                if media_type == "tv":
                    resolved_type = ContentType.SERIES
                elif media_type == "movie":
                    resolved_type = ContentType.MOVIE

        # Priority 3: Direct TMDB ID present -> TMDB /3/{type}/{id}/external_ids
        elif tmdb_id and not imdb_id:
            try:
                ext_ids = await self.tmdb_client.get_external_ids(tmdb_id, item.type)
            except Exception as exc:
                logger.warning("Error fetching external IDs for TMDB ID %s: %s", tmdb_id, exc)
                ext_ids = None

            if ext_ids:
                imdb_id = ext_ids.get("imdb_id")

        # Priority 4: Neither ID present -> Title Normalization + Search + Candidate Scoring
        else:
            parsed = self.normalizer.parse(
                item.title, provider_input=item.provider, year=item.year
            )
            search_queries = parsed.search_titles or [parsed.normalized_title]

            candidates: list[dict[str, Any]] = []
            for q in search_queries:
                try:
                    candidates = await self.tmdb_client.search(
                        title=q,
                        media_type=item.type,
                        year=item.year,
                    )
                except Exception as exc:
                    logger.warning("Error searching TMDB for query %r: %s", q, exc)
                    candidates = []
                if candidates:
                    break

            if not candidates:
                return None

            best_match = None
            for cand in candidates:
                match = self.scorer.score_candidate(parsed, cand, item.type)
                if best_match is None or match.score > best_match.score:
                    best_match = match

            if (
                not best_match
                or best_match.score < self.confidence_threshold
                or best_match.confidence != "high"
            ):
                return None

            tmdb_id = best_match.tmdb_id
            imdb_id = best_match.imdb_id
            if not imdb_id and tmdb_id:
                try:
                    ext = await self.tmdb_client.get_external_ids(tmdb_id, item.type)
                    if ext:
                        imdb_id = ext.get("imdb_id")
                except Exception as exc:
                    logger.warning("Error fetching external IDs for TMDB ID %s: %s", tmdb_id, exc)

            resolved_title = best_match.matched_title or item.title
            cand_year = self.scorer.extract_year(best_match.candidate)
            if cand_year is not None:
                resolved_year = cand_year
            cand_media = str(best_match.candidate.get("media_type") or "").lower()
            if cand_media == "tv":
                resolved_type = ContentType.SERIES
            elif cand_media == "movie":
                resolved_type = ContentType.MOVIE

        # 2. Check store again if resolved IDs match existing mapping
        if not existing and master_store is not None:
            if tmdb_id and hasattr(master_store, "get_by_tmdb"):
                existing = master_store.get_by_tmdb(tmdb_id, resolved_type)
            if not existing and imdb_id and hasattr(master_store, "get_by_imdb"):
                existing = master_store.get_by_imdb(imdb_id, resolved_type)

        if existing is not None:
            existing.add_provider(item.provider, item.slug)
            if not existing.tmdb_id and tmdb_id:
                existing.tmdb_id = tmdb_id
            if not existing.imdb_id and imdb_id:
                existing.imdb_id = imdb_id
            if not existing.year and resolved_year:
                existing.year = resolved_year
            if not existing.title and resolved_title:
                existing.title = resolved_title
            existing.updated_at = int(time.time() * 1000)
            return existing

        return CanonicalMapping(
            tmdb_id=tmdb_id,
            imdb_id=imdb_id,
            title=resolved_title,
            type=resolved_type,
            year=resolved_year,
            providers={item.provider: item.slug},
            updated_at=int(time.time() * 1000),
        )

    async def reconcile_batch(
        self,
        items: list[ScrapedItem | ScrapedDetail],
        master_store: Any | None = None,
    ) -> list[CanonicalMapping]:
        """Reconcile a batch of scraped items, merging in-flight entries for same entities."""
        if not items:
            return []

        by_tmdb: dict[tuple[str, str], CanonicalMapping] = {}
        by_imdb: dict[tuple[str, str], CanonicalMapping] = {}
        reconciled_list: list[CanonicalMapping] = []

        for item in items:
            mapping = await self.reconcile_item(item, master_store=master_store)
            if mapping is None:
                continue

            content_type = str(mapping.type).lower()
            key_tmdb = (mapping.tmdb_id, content_type) if mapping.tmdb_id else None
            key_imdb = (mapping.imdb_id, content_type) if mapping.imdb_id else None

            existing_by_tmdb = by_tmdb.get(key_tmdb) if key_tmdb else None
            existing_by_imdb = by_imdb.get(key_imdb) if key_imdb else None

            if existing_by_tmdb is not None and existing_by_imdb is not None:
                if existing_by_tmdb is existing_by_imdb:
                    target = existing_by_tmdb
                    target.merge(mapping)
                else:
                    # Transitive bridging of two previously separate mappings
                    primary = existing_by_tmdb
                    secondary = existing_by_imdb
                    primary.merge(secondary)
                    primary.merge(mapping)
                    if secondary in reconciled_list:
                        reconciled_list.remove(secondary)
                    # Re-point all references from secondary to primary
                    for k, v in list(by_tmdb.items()):
                        if v is secondary:
                            by_tmdb[k] = primary
                    for k, v in list(by_imdb.items()):
                        if v is secondary:
                            by_imdb[k] = primary
                    target = primary
            elif existing_by_tmdb is not None:
                target = existing_by_tmdb
                target.merge(mapping)
            elif existing_by_imdb is not None:
                target = existing_by_imdb
                target.merge(mapping)
            else:
                target = mapping
                reconciled_list.append(target)

            # Update indices with the resolved mapping's identifiers
            if target.tmdb_id:
                by_tmdb[(target.tmdb_id, content_type)] = target
            if target.imdb_id:
                by_imdb[(target.imdb_id, content_type)] = target

        return reconciled_list
