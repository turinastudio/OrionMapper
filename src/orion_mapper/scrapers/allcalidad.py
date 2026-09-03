from __future__ import annotations

import logging
import urllib.parse
from typing import Any, ClassVar

from pydantic import ValidationError

from orion_mapper.models.item import ContentType, ScrapedDetail, ScrapedItem
from orion_mapper.scrapers.base import BaseScraper

logger = logging.getLogger(__name__)


def _norm_slug(slug: str) -> str:
    """Decode percent-encoded slugs (the API double-encodes non-Latin titles)."""
    slug = slug.strip().strip("/")
    for _ in range(2):
        decoded = urllib.parse.unquote(slug)
        if decoded == slug:
            break
        slug = decoded
    return slug


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
    Scraper for AllCalidad (https://allcalidad.re).
    Interacts with JSON REST API endpoints (/api/rest/listing, /api/rest/single)
    using ``post_type=movies|tvshows``. Items carry no direct TMDB/IMDb IDs:
    identity is resolved offline from the MD5 embedded in image URLs
    (``md5(str(tmdb_id))``), see :mod:`orion_mapper.resolver.allcalidad_md5`.
    """

    name = "allcalidad"
    base_url = "https://allcalidad.re"
    supported_types: ClassVar[list[ContentType]] = [ContentType.MOVIE, ContentType.SERIES]
    page_size = 24
    default_rate_limit = 5.0

    @staticmethod
    def _post_type(ctype: ContentType) -> str:
        return "movies" if ctype == ContentType.MOVIE else "tvshows"

    @staticmethod
    def _abs_image(url: str | None, base: str) -> str | None:
        if not url or not isinstance(url, str):
            return None
        url = url.strip()
        if not url:
            return None
        if url.startswith(("http://", "https://")):
            return url
        return base.rstrip("/") + "/" + url.lstrip("/")

    @staticmethod
    def _extract_year(raw: dict[str, Any]) -> int | None:
        for key in ("year", "release_date", "releaseDate"):
            value = raw.get(key)
            if value is None:
                continue
            text = str(value).strip()
            if len(text) >= 4 and text[:4].isdigit():
                year = int(text[:4])
                if 1880 <= year <= 2100:
                    return year
        return None

    @staticmethod
    def _unwrap_posts(body: Any) -> list[dict[str, Any]]:
        if isinstance(body, dict):
            data = body.get("data", body)
            if isinstance(data, dict):
                posts = data.get("posts", [])
            elif isinstance(data, list):
                posts = data
            else:
                posts = []
        elif isinstance(body, list):
            posts = body
        else:
            posts = []
        return [p for p in posts if isinstance(p, dict)]

    @staticmethod
    def _unwrap_single(body: Any) -> dict[str, Any] | None:
        if isinstance(body, dict):
            data = body.get("data", body)
            if isinstance(data, dict):
                return data
        return None

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
        params: dict[str, Any] = {
            "page": page,
            "post_type": self._post_type(ctype),
            "posts_per_page": self.page_size,
        }
        if genre:
            params["genres"] = genre

        try:
            res = await self.http_client.get(url, params=params)
            if res.status_code != 200:
                return []
            data = res.json()
        except Exception as exc:
            logger.warning("[%s] Catalog fetch failed for %s: %s", self.name, url, exc)
            return []

        if _is_error_payload(data):
            return []

        raw_items = self._unwrap_posts(data)

        items: list[ScrapedItem] = []
        for raw in raw_items:
            try:
                slug = raw.get("slug", "")
                if not slug or not isinstance(slug, str):
                    continue
                slug = _norm_slug(slug)
                if not slug:
                    continue
                title = raw.get("title", slug)
                raw_type = str(raw.get("type", "")).lower()
                item_type = (
                    ContentType.SERIES
                    if raw_type in ("series", "tv", "tvshows", "serie")
                    else ContentType.MOVIE
                )

                year = self._extract_year(raw)

                tmdb_id = str(raw["tmdb_id"]) if raw.get("tmdb_id") is not None else None
                imdb_id = str(raw["imdb_id"]).lower() if raw.get("imdb_id") is not None else None
                images = raw.get("images") if isinstance(raw.get("images"), dict) else {}
                poster = self._abs_image(
                    images.get("poster") or raw.get("poster") or raw.get("poster_url"),
                    self.base_url,
                )

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
        slug = _norm_slug(slug)

        data: dict[str, Any] | None = None
        # Canonical single endpoint: /api/rest/single?post_name=&post_type=
        url = self.build_url("/api/rest/single")
        params = {"post_name": slug, "post_type": self._post_type(ctype)}
        try:
            res = await self.http_client.get(url, params=params)
            if res.status_code == 200:
                body = res.json()
                if isinstance(body, dict) and not _is_error_payload(body):
                    data = self._unwrap_single(body)
        except Exception:
            pass

        if not data:
            return None

        title = data.get("title", slug)
        original_title = data.get("original_title") or data.get("originalTitle")
        raw_type = str(data.get("type", "")).lower()
        item_type = (
            ContentType.SERIES
            if raw_type in ("series", "tv", "tvshows", "serie")
            else ctype
        )

        year = self._extract_year(data)

        tmdb_id = str(data["tmdb_id"]) if data.get("tmdb_id") is not None else None
        imdb_id = str(data["imdb_id"]).lower() if data.get("imdb_id") is not None else None
        overview = data.get("overview")
        images = data.get("images") if isinstance(data.get("images"), dict) else {}
        poster = self._abs_image(
            images.get("poster") or data.get("poster") or data.get("poster_url"),
            self.base_url,
        )
        genres = data.get("genres", [])
        if isinstance(genres, str):
            genres = [genres]
        elif not isinstance(genres, list):
            genres = []
        else:
            genres = [str(g) for g in genres]

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
