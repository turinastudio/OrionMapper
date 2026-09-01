from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, Field, field_validator

from orion_mapper.models.item import IMDB_ID_REGEX, TMDB_ID_REGEX, ContentType


class CanonicalMapping(BaseModel):
    tmdb_id: str | None = Field(default=None, description="Canonical TMDB numeric ID as string")
    imdb_id: str | None = Field(default=None, description="Canonical IMDb ID (tt...)")
    title: str = Field(..., description="Canonical title")
    type: ContentType = Field(..., description="'movie' or 'series'")
    year: int | None = Field(default=None, ge=1880, le=2100, description="Release year")
    providers: dict[str, str] = Field(default_factory=dict, description="Map of provider name -> provider slug")
    updated_at: int = Field(
        default_factory=lambda: int(time.time() * 1000),
        description="Epoch timestamp in milliseconds",
    )

    @field_validator("title", mode="before")
    @classmethod
    def normalize_title(cls, v: Any) -> str:
        return str(v).strip() if v is not None else ""

    @field_validator("imdb_id", mode="before")
    @classmethod
    def normalize_imdb(cls, v: Any) -> str | None:
        if v is None:
            return None
        cleaned = str(v).strip().lower()
        if not cleaned:
            return None
        if not cleaned.startswith("tt") and cleaned.isdigit():
            cleaned = f"tt{cleaned}"
        return cleaned if IMDB_ID_REGEX.match(cleaned) else None

    @field_validator("tmdb_id", mode="before")
    @classmethod
    def normalize_tmdb(cls, v: Any) -> str | None:
        if v is None:
            return None
        s = str(v).strip()
        if not s:
            return None
        return s if TMDB_ID_REGEX.match(s) else None

    @field_validator("providers", mode="before")
    @classmethod
    def normalize_providers(cls, v: Any) -> dict[str, str]:
        if not isinstance(v, dict):
            return {}
        return {str(k).strip().lower(): str(s).strip().strip("/") for k, s in v.items() if k and s}

    def add_provider(self, provider: str, slug: str) -> None:
        norm_provider = provider.strip().lower()
        norm_slug = slug.strip().strip("/")
        if norm_provider and norm_slug:
            self.providers[norm_provider] = norm_slug
            self.updated_at = int(time.time() * 1000)

    def merge(self, other: CanonicalMapping) -> CanonicalMapping:
        """Merge another canonical mapping for the same entity into self."""
        if self.tmdb_id is None and other.tmdb_id is not None:
            self.tmdb_id = other.tmdb_id
        if self.imdb_id is None and other.imdb_id is not None:
            self.imdb_id = other.imdb_id
        if self.year is None and other.year is not None:
            self.year = other.year
        if not self.title and other.title:
            self.title = other.title
        for p, s in other.providers.items():
            self.providers[p] = s
        self.updated_at = max(self.updated_at, other.updated_at)
        return self
