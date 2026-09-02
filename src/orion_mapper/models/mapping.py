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
    provider_variants: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Additional slugs for a provider representing the same entity",
    )
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

    @field_validator("provider_variants", mode="before")
    @classmethod
    def normalize_provider_variants(cls, v: Any) -> dict[str, list[str]]:
        if not isinstance(v, dict):
            return {}
        result: dict[str, list[str]] = {}
        for provider, slugs in v.items():
            if not provider or not isinstance(slugs, (list, tuple, set)):
                continue
            normalized = [str(slug).strip().strip("/") for slug in slugs if str(slug).strip()]
            if normalized:
                result[str(provider).strip().lower()] = list(dict.fromkeys(normalized))
        return result

    def all_provider_slugs(self) -> dict[str, list[str]]:
        """Return primary and alternate slugs grouped by provider."""
        result: dict[str, list[str]] = {}
        for provider, slug in self.providers.items():
            result.setdefault(provider, []).append(slug)
        for provider, slugs in self.provider_variants.items():
            result.setdefault(provider, []).extend(slugs)
        for provider, slugs in result.items():
            result[provider] = list(dict.fromkeys(slugs))
        return result

    def add_provider(self, provider: str, slug: str) -> None:
        norm_provider = provider.strip().lower()
        norm_slug = slug.strip().strip("/")
        if norm_provider and norm_slug:
            primary = self.providers.get(norm_provider)
            if primary is None:
                self.providers[norm_provider] = norm_slug
            elif primary != norm_slug:
                variants = self.provider_variants.setdefault(norm_provider, [])
                if norm_slug not in variants:
                    variants.append(norm_slug)
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
            primary = self.providers.get(p)
            if primary is None:
                self.providers[p] = s
            elif primary != s:
                variants = self.provider_variants.setdefault(p, [])
                if s not in variants:
                    variants.append(s)
        for p, slugs in other.provider_variants.items():
            for slug in slugs:
                primary = self.providers.get(p)
                if primary is None:
                    self.providers[p] = slug
                elif primary != slug:
                    variants = self.provider_variants.setdefault(p, [])
                    if slug not in variants:
                        variants.append(slug)
        self.updated_at = max(self.updated_at, other.updated_at)
        return self
