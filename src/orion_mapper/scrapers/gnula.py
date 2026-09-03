from __future__ import annotations

import json
import logging
import re
from typing import Any, ClassVar

from bs4 import BeautifulSoup
from pydantic import ValidationError

from orion_mapper.models.item import ContentType, ScrapedDetail, ScrapedItem
from orion_mapper.scrapers.base import BaseScraper

logger = logging.getLogger(__name__)


def _nested_value(data: dict[str, Any], *keys: str) -> Any:
    value: Any = data
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _extract_year(value: Any) -> int | None:
    if value is None:
        return None
    match = re.search(r"\b(?:18\d{2}|19\d{2}|20\d{2}|2100)\b", str(value))
    return int(match.group(0)) if match else None


def _post_field(post: dict[str, Any], *paths: tuple[str, ...]) -> Any:
    for path in paths:
        value = _nested_value(post, *path)
        if value is not None:
            return value
    return None


def _catalog_slug(post: dict[str, Any]) -> str | None:
    """Extract Gnula's slug from the live archive payload."""
    url_slug = _nested_value(post, "url", "slug")
    if isinstance(url_slug, str) and "/" in url_slug:
        return url_slug.split("/", 1)[1].strip().strip("/") or None
    slug = post.get("slug")
    if isinstance(slug, dict):
        slug = slug.get("name")
    return str(slug).strip().strip("/") if slug else None


def _extract_next_data(html: str) -> dict[str, Any] | None:
    """Extract and parse the __NEXT_DATA__ JSON script tag from HTML."""
    try:
        soup = BeautifulSoup(html, "html.parser")
        tag = soup.find("script", id="__NEXT_DATA__")
        if tag and tag.string:
            data = json.loads(tag.string)
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return None


class GnulaScraper(BaseScraper):
    """
    Scraper for Gnula (https://gnula.life).
    Extracts structured catalog and metadata from Next.js hydration payloads (__NEXT_DATA__)
    with multi-candidate slug path probing (handling pelicula- / serie- slug prefixes).
    """

    name = "gnula"
    base_url = "https://gnula.life"
    supported_types: ClassVar[list[ContentType]] = [ContentType.MOVIE, ContentType.SERIES]
    page_size = 24
    default_rate_limit = 5.0

    def extract_identifiers(
        self, raw_data: dict[str, Any] | str
    ) -> tuple[str | None, str | None]:
        """Extract (imdb_id, tmdb_id) from raw provider data."""
        if isinstance(raw_data, dict):
            imdb = raw_data.get("IMDbId") or raw_data.get("imdb_id")
            tmdb = raw_data.get("TMDbId") or raw_data.get("tmdb_id")
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
        """Fetch a page of catalog items from Gnula."""
        ctype = ContentType(content_type)
        section = "movies" if ctype == ContentType.MOVIE else "series"
        path = f"/archives/{section}" if page == 1 else f"/archives/{section}/page/{page}"
        url = self.build_url(path)

        try:
            res = await self.http_client.get(url)
            if res.status_code != 200:
                return []
            html = res.text
        except Exception as exc:
            logger.warning("[%s] Catalog fetch failed for %s: %s", self.name, url, exc)
            return []

        next_data = _extract_next_data(html)
        if not next_data or not isinstance(next_data, dict):
            return []

        props = next_data.get("props")
        if not isinstance(props, dict):
            return []
        page_props = props.get("pageProps")
        if not isinstance(page_props, dict):
            return []

        results = page_props.get("results")
        posts = (
            _nested_value(results, "data") if isinstance(results, dict) else None
        )
        if posts is None:
            posts = (
                page_props.get("lastMovies") if ctype == ContentType.MOVIE
                else page_props.get("lastSeries")
            )
        posts = posts or page_props.get("posts") or page_props.get("data") or page_props.get("items") or []
        if not isinstance(posts, list):
            return []

        items: list[ScrapedItem] = []
        for post in posts:
            if not isinstance(post, dict):
                continue
            try:
                slug = _catalog_slug(post)
                if not slug:
                    continue
                title = _post_field(post, ("titles", "name"), ("title",)) or slug
                raw_type = str(post.get("type", "")).lower()
                item_type = (
                    ContentType.SERIES
                    if raw_type in ("tv", "series", "serie")
                    else ctype
                )

                year = _extract_year(_post_field(post, ("releaseDate",), ("year",)))

                tmdb_id = str(post["TMDbId"]) if post.get("TMDbId") is not None else None
                imdb_id = str(post["IMDbId"]).lower() if post.get("IMDbId") is not None else None
                poster = _post_field(post, ("images", "poster"), ("poster",), ("poster_url",))

                items.append(
                    ScrapedItem(
                        provider=self.name,
                        slug=slug,
                        title=title,
                        type=item_type,
                        year=year,
                        url=self.build_url(
                            f"/movies/{slug}"
                            if item_type == ContentType.MOVIE
                            else f"/series/{slug}"
                        ),
                        poster_url=poster,
                        imdb_id=imdb_id,
                        tmdb_id=tmdb_id,
                        raw_data=post,
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
        """Fetch detailed metadata for a given slug from Gnula with fallback candidate probing."""
        ctype = ContentType(content_type)
        type_prefix = "movies" if ctype == ContentType.MOVIE else "series"

        # Candidate URL paths to probe for Next.js routes
        legacy_prefix = "pelicula" if ctype == ContentType.MOVIE else "serie"
        candidates: list[str] = [f"/{type_prefix}/{slug}", f"/{legacy_prefix}/{slug}"]
        if not slug.startswith(f"{legacy_prefix}-"):
            candidates.append(f"/{legacy_prefix}/{legacy_prefix}-{slug}")
            candidates.append(f"/{legacy_prefix}-{slug}")
        candidates.append(f"/{slug}")

        html: str | None = None
        final_url: str = ""
        for path in candidates:
            url = self.build_url(path)
            try:
                res = await self.http_client.get(url)
                if res.status_code == 200:
                    html = res.text
                    final_url = url
                    break
            except Exception:
                continue

        if not html:
            return None

        next_data = _extract_next_data(html)
        if not next_data or not isinstance(next_data, dict):
            return None

        props = next_data.get("props")
        if not isinstance(props, dict):
            return None
        page_props = props.get("pageProps")
        if not isinstance(page_props, dict):
            return None
        post = (
            page_props.get("post")
            or page_props.get("data")
            or {}
        )
        if not isinstance(post, dict) or not post:
            return None

        title = _post_field(post, ("titles", "name"), ("title",)) or slug
        original_title = (
            _nested_value(post, "titles", "original", "name")
            or post.get("originalTitle") or post.get("original_title")
        )
        raw_type = str(post.get("type", "")).lower()
        item_type = (
            ContentType.SERIES
            if raw_type in ("tv", "series", "serie")
            else ctype
        )

        year = _extract_year(_post_field(post, ("releaseDate",), ("year",)))

        tmdb_id = str(post["TMDbId"]) if post.get("TMDbId") is not None else None
        imdb_id = str(post["IMDbId"]).lower() if post.get("IMDbId") is not None else None
        overview = post.get("overview") or post.get("synopsis")
        poster = _post_field(post, ("images", "poster"), ("poster",), ("poster_url",))
        genres = post.get("genres", [])
        if isinstance(genres, str):
            genres = [genres]
        elif not isinstance(genres, list):
            genres = []

        if isinstance(genres, list):
            genres = [
                item.get("name", "") if isinstance(item, dict) else str(item)
                for item in genres
            ]

        return ScrapedDetail(
            provider=self.name,
            slug=post.get("slug", slug),
            title=title,
            original_title=original_title,
            type=item_type,
            year=year,
            url=final_url,
            poster_url=poster,
            imdb_id=imdb_id,
            tmdb_id=tmdb_id,
            overview=overview,
            genres=genres,
            raw_data=post,
        )
