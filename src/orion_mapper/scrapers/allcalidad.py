from __future__ import annotations

import logging
from typing import Any, ClassVar

from pydantic import ValidationError

from orion_mapper.models.item import ContentType, ScrapedDetail, ScrapedItem
from orion_mapper.scrapers.base import BaseScraper

logger = logging.getLogger(__name__)


def _is_error_payload(data: Any) -> bool:
    """Check if a response body represents an error envelope."""
    if not isinstance(data, dict):
        return True
    status = data.get("status")
    if status is not None:
        if isinstance(status, str) and status.strip().lower() in ("error", "fail", "failed"):
            return True
        if isinstance(status, int) and status >= 400:
            return True
        if status in ("error", 400, 404, 500):
            return True
    if data.get("error"):
        return True
    return False


class AllCalidadScraper(BaseScraper):
    """
    Scraper for AllCalidad (https://allcalidad.ms).
    Interacts with JSON REST API endpoints (/api/rest/listing, /api/rest/movie, /api/rest/series, /api/rest/single)
    to retrieve items with direct TMDb and IMDb identifier payloads.
    """

    name = "allcalidad"
    base_url = "https://allcalidad.ms"
    supported_types: ClassVar[list[ContentType]] = [ContentType.MOVIE, ContentType.SERIES]
    page_size = 24
    default_rate_limit = 5.0

    def extract_identifiers(
        self, raw_data: dict[str, Any] | str
    ) -> tuple[str | None, str | None]:
        """Extract (imdb_id, tmdb_id) from raw provider data."""
        if isinstance(raw_data, dict):
            imdb = raw_data.get("imdb_id") or raw_data.get("IMDbId")
            tmdb = raw_data.get("tmdb_id") or raw_data.get("TMDbId")
            imdb_val = str(imdb).strip().lower() if imdb is not None else None
            tmdb_val = str(tmdb).strip() if tmdb is not None else None
            return (
                imdb_val if (imdb_val and imdb_val.startswith("tt")) else None,
                tmdb_val if (tmdb_val and tmdb_val.isdigit()) else None,
            )
        return None, None

    async def fetch_catalog(
        self,
        content_type: ContentType | str,
        page: int = 1,
        genre: str | None = None,
    ) -> list[ScrapedItem]:
        """Fetch a page of catalog items from AllCalidad REST API."""
        ctype = ContentType(content_type)
        url = self.build_url("/api/rest/listing")
        params: dict[str, Any] = {"page": page, "type": ctype.value}
        if genre:
            params["genre"] = genre

        try:
            res = await self.http_client.get(url, params=params)
            if res.status_code != 200:
                return []
            data = res.json()
        except Exception as exc:
            logger.warning("[%s] Catalog fetch failed for %s: %s", self.name, url, exc)
            return []

        if not isinstance(data, dict) or _is_error_payload(data):
            return []

        raw_items = data.get("items", [])
        if not isinstance(raw_items, list):
            return []

        items: list[ScrapedItem] = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            try:
                slug = raw.get("slug", "")
                if not slug or not isinstance(slug, str):
                    continue
                title = raw.get("title", slug)
                raw_type = str(raw.get("type", "")).lower()
                item_type = (
                    ContentType.SERIES
                    if raw_type in ("series", "tv", "serie")
                    else ContentType.MOVIE
                )

                year: int | None = None
                if raw.get("year") is not None:
                    try:
                        year = int(str(raw["year"]).strip())
                    except ValueError:
                        year = None
                elif raw.get("release_date"):
                    d_str = str(raw["release_date"]).strip()
                    if len(d_str) >= 4 and d_str[:4].isdigit():
                        year = int(d_str[:4])

                tmdb_id = str(raw["tmdb_id"]) if raw.get("tmdb_id") is not None else None
                imdb_id = str(raw["imdb_id"]).lower() if raw.get("imdb_id") is not None else None
                poster = raw.get("poster") or raw.get("poster_url")

                items.append(
                    ScrapedItem(
                        provider=self.name,
                        slug=slug,
                        title=title,
                        type=item_type,
                        year=year,
                        url=self.build_url(
                            f"/pelicula/{slug}"
                            if item_type == ContentType.MOVIE
                            else f"/serie/{slug}"
                        ),
                        poster_url=poster,
                        imdb_id=imdb_id,
                        tmdb_id=tmdb_id,
                        raw_data=raw,
                    )
                )
            except (ValidationError, Exception) as exc:
                logger.warning("[%s] Malformed item skipped in catalog: %s", self.name, exc)
                continue

        return items

    async def fetch_detail(
        self,
        slug: str,
        content_type: ContentType | str,
    ) -> ScrapedDetail | None:
        """Fetch detailed metadata for a given slug from AllCalidad REST API."""
        ctype = ContentType(content_type)
        endpoint = (
            f"/api/rest/movie/{slug}"
            if ctype == ContentType.MOVIE
            else f"/api/rest/series/{slug}"
        )

        data: dict[str, Any] | None = None
        # Try specialized REST endpoint first
        try:
            res = await self.http_client.get(self.build_url(endpoint))
            if res.status_code == 200:
                body = res.json()
                if isinstance(body, dict) and not _is_error_payload(body):
                    data = body
        except Exception:
            pass

        # Fallback to /api/rest/single query endpoint
        if not data:
            url = self.build_url("/api/rest/single")
            params = {"slug": slug, "type": ctype.value}
            try:
                res = await self.http_client.get(url, params=params)
                if res.status_code == 200:
                    body = res.json()
                    if isinstance(body, dict) and not _is_error_payload(body):
                        data = body
            except Exception:
                pass

        if not data or not isinstance(data, dict) or _is_error_payload(data):
            return None

        title = data.get("title", slug)
        original_title = data.get("original_title") or data.get("originalTitle")
        raw_type = str(data.get("type", "")).lower()
        item_type = (
            ContentType.SERIES
            if raw_type in ("series", "tv", "serie")
            else ctype
        )

        year: int | None = None
        if data.get("year") is not None:
            try:
                year = int(str(data["year"]).strip())
            except ValueError:
                year = None
        elif data.get("release_date"):
            d_str = str(data["release_date"]).strip()
            if len(d_str) >= 4 and d_str[:4].isdigit():
                year = int(d_str[:4])

        tmdb_id = str(data["tmdb_id"]) if data.get("tmdb_id") is not None else None
        imdb_id = str(data["imdb_id"]).lower() if data.get("imdb_id") is not None else None
        overview = data.get("overview")
        poster = data.get("poster") or data.get("poster_url")
        genres = data.get("genres", [])
        if isinstance(genres, str):
            genres = [genres]
        elif not isinstance(genres, list):
            genres = []

        return ScrapedDetail(
            provider=self.name,
            slug=slug,
            title=title,
            original_title=original_title,
            type=item_type,
            year=year,
            url=self.build_url(
                f"/pelicula/{slug}"
                if item_type == ContentType.MOVIE
                else f"/serie/{slug}"
            ),
            poster_url=poster,
            imdb_id=imdb_id,
            tmdb_id=tmdb_id,
            overview=overview,
            genres=genres,
            release_date=data.get("release_date"),
            raw_data=data,
        )
