from __future__ import annotations

import re
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class ContentType(StrEnum):
    MOVIE = "movie"
    SERIES = "series"


IMDB_ID_REGEX = re.compile(r"^tt\d{1,10}$", re.IGNORECASE)
TMDB_ID_REGEX = re.compile(r"^\d{1,10}$")


class ScrapedEpisode(BaseModel):
    season: int = Field(..., ge=0, description="Season number (0 for specials)")
    episode: int = Field(..., ge=0, description="Episode number")
    title: str | None = Field(default=None, description="Episode title")
    slug: str | None = Field(default=None, description="Episode slug or identifier")
    extra_data: dict[str, Any] = Field(default_factory=dict, description="Provider-specific episode metadata")


class ScrapedItem(BaseModel):
    provider: str = Field(..., description="Provider name lowercase (e.g., 'gnula', 'serieskao')")
    slug: str = Field(..., description="Provider slug (e.g., 'pelicula-zombieland-saga')")
    title: str = Field(..., description="Item title as scraped from catalog")
    type: ContentType = Field(..., description="'movie' or 'series'")
    year: int | None = Field(default=None, ge=1880, le=2100, description="Release year")
    url: str | None = Field(default=None, description="Direct URL to item detail")
    poster_url: str | None = Field(default=None, description="Item poster or thumbnail URL")
    imdb_id: str | None = Field(default=None, description="IMDb ID if directly extractable (e.g., 'tt15486')")
    tmdb_id: str | None = Field(default=None, description="TMDB ID if directly extractable (e.g., '21048')")
    raw_data: dict[str, Any] = Field(default_factory=dict, description="Raw metadata for debugging/extension")

    @field_validator("provider", mode="before")
    @classmethod
    def normalize_provider(cls, v: Any) -> str:
        return str(v).strip().lower() if v is not None else ""

    @field_validator("slug", mode="before")
    @classmethod
    def normalize_slug(cls, v: Any) -> str:
        return str(v).strip().strip("/") if v is not None else ""

    @field_validator("title", mode="before")
    @classmethod
    def normalize_title(cls, v: Any) -> str:
        return str(v).strip() if v is not None else ""

    @field_validator("imdb_id", mode="before")
    @classmethod
    def normalize_imdb_id(cls, v: Any) -> str | None:
        if v is None:
            return None
        cleaned = str(v).strip().lower()
        if not cleaned:
            return None
        if not cleaned.startswith("tt") and cleaned.isdigit():
            cleaned = f"tt{cleaned}"
        if IMDB_ID_REGEX.match(cleaned):
            return cleaned
        return None

    @field_validator("tmdb_id", mode="before")
    @classmethod
    def normalize_tmdb_id(cls, v: Any) -> str | None:
        if v is None:
            return None
        s = str(v).strip()
        if not s:
            return None
        if TMDB_ID_REGEX.match(s):
            return s
        return None


class ScrapedDetail(ScrapedItem):
    original_title: str | None = Field(default=None, description="Original language title if available")
    overview: str | None = Field(default=None, description="Plot synopsis or description")
    genres: list[str] = Field(default_factory=list, description="Genre tags")
    episodes: list[ScrapedEpisode] = Field(default_factory=list, description="List of episodes if series")
    seasons_count: int | None = Field(default=None, ge=0, description="Total season count")
    release_date: str | None = Field(default=None, description="Release date string (YYYY-MM-DD)")
    extra_identifiers: dict[str, str] = Field(
        default_factory=dict,
        description="Additional IDs (e.g., player URLs, internal IDs)",
    )
