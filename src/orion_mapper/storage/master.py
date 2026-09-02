from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any

from orion_mapper.core.config import settings
from orion_mapper.models.item import ContentType
from orion_mapper.models.mapping import CanonicalMapping

logger = logging.getLogger(__name__)


def atomic_write_json(file_path: Path, data: Any) -> int:
    """
    Atomically writes serialized JSON data to the target file path.
    Ensures directory provisioning, sorted keys, 2-space indentation,
    POSIX trailing newline, and atomic replacement via temporary file.

    Returns the number of bytes written.
    """
    file_path = Path(file_path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    json_str = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    content_bytes = json_str.encode("utf-8")
    temp_file = file_path.parent / f".{file_path.name}.tmp-{uuid.uuid4().hex}"
    try:
        with open(temp_file, "wb") as f:
            f.write(content_bytes)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_file, file_path)
        return len(content_bytes)
    finally:
        if temp_file.exists():
            try:
                temp_file.unlink()
            except OSError:
                pass


class MasterMappingStore:
    """
    Centralized Git-Tracked Master Dataset Storage in Fribb/anime-lists format.
    Maintains movies.json and series.json with in-memory O(1) indexes.
    """

    def __init__(self, storage_dir: Path | str | None = None) -> None:
        self.storage_dir = Path(storage_dir) if storage_dir is not None else settings.mappings_dir
        self._movies: dict[str, CanonicalMapping] = {}
        self._series: dict[str, CanonicalMapping] = {}
        self._by_tmdb: dict[tuple[str, str], CanonicalMapping] = {}
        self._by_imdb: dict[tuple[str, str], CanonicalMapping] = {}
        self._by_provider: dict[tuple[str, str], CanonicalMapping] = {}

        self.storage_dir.mkdir(parents=True, exist_ok=True)
        if (self.storage_dir / "movies.json").exists() or (
            self.storage_dir / "series.json"
        ).exists():
            self.load()

    @staticmethod
    def _sort_key(mapping: CanonicalMapping) -> tuple[int | float, str, str]:
        if mapping.tmdb_id and mapping.tmdb_id.strip().isdigit():
            tmdb_val: int | float = int(mapping.tmdb_id.strip())
        else:
            tmdb_val = float("inf")
        title_val = (mapping.title or "").strip().lower()
        imdb_val = (mapping.imdb_id or "").strip().lower()
        return (tmdb_val, title_val, imdb_val)

    def _index_mapping(self, mapping: CanonicalMapping) -> None:
        c_type = str(mapping.type.value if hasattr(mapping.type, "value") else mapping.type).lower()
        if mapping.tmdb_id:
            self._by_tmdb[(str(mapping.tmdb_id).strip(), c_type)] = mapping
        if mapping.imdb_id:
            self._by_imdb[(str(mapping.imdb_id).strip().lower(), c_type)] = mapping
        for prov, slugs in mapping.all_provider_slugs().items():
            for slug in slugs:
                self._by_provider[(str(prov).strip().lower(), str(slug).strip().strip("/"))] = mapping

    def _unindex_mapping(self, mapping: CanonicalMapping) -> None:
        c_type = str(mapping.type.value if hasattr(mapping.type, "value") else mapping.type).lower()
        if mapping.tmdb_id:
            tmdb_key = (str(mapping.tmdb_id).strip(), c_type)
            if self._by_tmdb.get(tmdb_key) is mapping:
                del self._by_tmdb[tmdb_key]
        if mapping.imdb_id:
            imdb_key = (str(mapping.imdb_id).strip().lower(), c_type)
            if self._by_imdb.get(imdb_key) is mapping:
                del self._by_imdb[imdb_key]
        for prov, slugs in mapping.all_provider_slugs().items():
            for slug in slugs:
                prov_key = (str(prov).strip().lower(), str(slug).strip().strip("/"))
                if self._by_provider.get(prov_key) is mapping:
                    del self._by_provider[prov_key]

    def add_or_update(self, mapping: CanonicalMapping) -> CanonicalMapping:
        """
        Adds a new mapping or merges with existing entry/entries using in-memory indexes.
        If multiple distinct existing mappings match (via TMDB, IMDb, or provider slugs),
        they are transitively coalesced into a single primary CanonicalMapping.
        Returns the canonical mapping stored.
        """
        c_type = str(mapping.type.value if hasattr(mapping.type, "value") else mapping.type).lower()
        target_dict = self._movies if c_type == "movie" else self._series

        matches: list[CanonicalMapping] = []

        def _add_match(candidate: CanonicalMapping | None) -> None:
            if candidate is not None and candidate not in matches:
                matches.append(candidate)

        if mapping.tmdb_id:
            _add_match(self._by_tmdb.get((str(mapping.tmdb_id).strip(), c_type)))

        if mapping.imdb_id:
            _add_match(self._by_imdb.get((str(mapping.imdb_id).strip().lower(), c_type)))

        for prov, slug in mapping.providers.items():
            _add_match(self._by_provider.get((str(prov).strip().lower(), str(slug).strip().strip("/"))))

        if not matches:
            entry_key = uuid.uuid4().hex
            target_dict[entry_key] = mapping
            self._index_mapping(mapping)
            return mapping

        primary = matches[0]
        for secondary in matches[1:]:
            self._unindex_mapping(secondary)
            keys_to_remove = [k for k, v in target_dict.items() if v is secondary]
            for k in keys_to_remove:
                del target_dict[k]
            primary.merge(secondary)

        primary.merge(mapping)
        self._index_mapping(primary)
        return primary

    def save_mapping(self, mapping: CanonicalMapping) -> None:
        """Adds or updates the mapping in-memory and immediately persists to disk."""
        self.add_or_update(mapping)
        self.save()

    def load(self) -> None:
        """Loads and deserializes movies.json and series.json, rebuilding in-memory indexes."""
        self.clear()
        movies_path = self.storage_dir / "movies.json"
        if movies_path.exists():
            try:
                content = movies_path.read_text(encoding="utf-8").strip()
                if content:
                    items = json.loads(content)
                    if isinstance(items, list):
                        for item_data in items:
                            if isinstance(item_data, dict):
                                try:
                                    mapping = CanonicalMapping.model_validate(item_data)
                                    key = uuid.uuid4().hex
                                    self._movies[key] = mapping
                                    self._index_mapping(mapping)
                                except Exception as exc:
                                    logger.warning(
                                        "Skipping malformed movie record in %s: %s",
                                        movies_path,
                                        exc,
                                    )
                            else:
                                logger.warning(
                                    "Skipping non-dict movie item in %s: %s",
                                    movies_path,
                                    item_data,
                                )
                    else:
                        logger.warning(
                            "Expected JSON array in %s, got %s",
                            movies_path,
                            type(items).__name__,
                        )
            except Exception as e:
                logger.warning("Error loading movies from %s: %s", movies_path, e)

        series_path = self.storage_dir / "series.json"
        if series_path.exists():
            try:
                content = series_path.read_text(encoding="utf-8").strip()
                if content:
                    items = json.loads(content)
                    if isinstance(items, list):
                        for item_data in items:
                            if isinstance(item_data, dict):
                                try:
                                    mapping = CanonicalMapping.model_validate(item_data)
                                    key = uuid.uuid4().hex
                                    self._series[key] = mapping
                                    self._index_mapping(mapping)
                                except Exception as exc:
                                    logger.warning(
                                        "Skipping malformed series record in %s: %s",
                                        series_path,
                                        exc,
                                    )
                            else:
                                logger.warning(
                                    "Skipping non-dict series item in %s: %s",
                                    series_path,
                                    item_data,
                                )
                    else:
                        logger.warning(
                            "Expected JSON array in %s, got %s",
                            series_path,
                            type(items).__name__,
                        )
            except Exception as e:
                logger.warning("Error loading series from %s: %s", series_path, e)

    def save(self) -> None:
        """Persists movies and series datasets to disk with deterministic formatting."""
        sorted_movies = sorted(self._movies.values(), key=self._sort_key)
        movies_data = [m.model_dump(mode="json") for m in sorted_movies]
        atomic_write_json(self.storage_dir / "movies.json", movies_data)

        sorted_series = sorted(self._series.values(), key=self._sort_key)
        series_data = [m.model_dump(mode="json") for m in sorted_series]
        atomic_write_json(self.storage_dir / "series.json", series_data)

    def get_by_tmdb(
        self,
        tmdb_id: str | int | None,
        content_type: ContentType | str | None = None,
    ) -> CanonicalMapping | None:
        """O(1) lookup by TMDB numeric ID and optional content type."""
        if tmdb_id is None:
            return None
        tmdb_str = str(tmdb_id).strip()
        if not tmdb_str:
            return None
        if content_type is not None:
            c_type = str(
                content_type.value if hasattr(content_type, "value") else content_type
            ).lower()
            return self._by_tmdb.get((tmdb_str, c_type))
        return self._by_tmdb.get((tmdb_str, "movie")) or self._by_tmdb.get((tmdb_str, "series"))

    def get_by_imdb(
        self,
        imdb_id: str | None,
        content_type: ContentType | str | None = None,
    ) -> CanonicalMapping | None:
        """O(1) lookup by IMDb ID (tt...) and optional content type."""
        if imdb_id is None:
            return None
        imdb_str = str(imdb_id).strip().lower()
        if not imdb_str:
            return None
        if not imdb_str.startswith("tt") and imdb_str.isdigit():
            imdb_str = f"tt{imdb_str}"
        if content_type is not None:
            c_type = str(
                content_type.value if hasattr(content_type, "value") else content_type
            ).lower()
            return self._by_imdb.get((imdb_str, c_type))
        return self._by_imdb.get((imdb_str, "movie")) or self._by_imdb.get((imdb_str, "series"))

    def get_by_provider_slug(
        self,
        provider: str | None,
        slug: str | None,
    ) -> CanonicalMapping | None:
        """O(1) lookup by provider name and slug."""
        if not provider or not slug:
            return None
        prov_str = str(provider).strip().lower()
        slug_str = str(slug).strip().strip("/")
        return self._by_provider.get((prov_str, slug_str))

    def all_mappings(
        self,
        content_type: ContentType | str | None = None,
    ) -> list[CanonicalMapping]:
        """Returns all cached mappings, optionally filtered by content type."""
        if content_type is not None:
            c_type = str(
                content_type.value if hasattr(content_type, "value") else content_type
            ).lower()
            if c_type == "movie":
                return list(self._movies.values())
            if c_type == "series":
                return list(self._series.values())
        return list(self._movies.values()) + list(self._series.values())

    def load_all(
        self,
        content_type: ContentType | str | None = None,
    ) -> list[CanonicalMapping]:
        """Alias for all_mappings()."""
        return self.all_mappings(content_type=content_type)

    def count(
        self,
        content_type: ContentType | str | None = None,
    ) -> int:
        """Returns the total number of mappings."""
        return len(self.all_mappings(content_type=content_type))

    def clear(self) -> None:
        """Clears all in-memory collections and lookup indexes."""
        self._movies.clear()
        self._series.clear()
        self._by_tmdb.clear()
        self._by_imdb.clear()
        self._by_provider.clear()
