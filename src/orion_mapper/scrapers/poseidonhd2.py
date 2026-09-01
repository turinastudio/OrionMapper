from __future__ import annotations

import json
import logging
from typing import Any, ClassVar

from bs4 import BeautifulSoup
from pydantic import ValidationError

from orion_mapper.models.item import ContentType, ScrapedDetail, ScrapedItem
from orion_mapper.scrapers.base import BaseScraper

logger = logging.getLogger(__name__)


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


class PoseidonHD2Scraper(BaseScraper):
    """
    Scraper for PoseidonHD2 (https://poseidonhd2.co).
    Extracts structured catalog and metadata directly from Next.js __NEXT_DATA__ props,
    including embedded TMDbId and IMDbId identifiers.
    """

    name = "poseidonhd2"
    base_url = "https://poseidonhd2.co"
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
        """Fetch a page of catalog items from PoseidonHD2."""
        ctype = ContentType(content_type)
        path = f"/movies?page={page}" if ctype == ContentType.MOVIE else f"/series?page={page}"
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

        raw_items = (
            page_props.get("data")
            or page_props.get("movies")
            or page_props.get("items")
            or []
        )
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
                    if raw_type in ("tv", "series", "serie")
                    else ContentType.MOVIE
                )

                year = raw.get("year")
                if year is not None:
                    try:
                        year = int(str(year).strip())
                    except ValueError:
                        year = None

                tmdb_id = str(raw["TMDbId"]) if raw.get("TMDbId") is not None else None
                imdb_id = str(raw["IMDbId"]).lower() if raw.get("IMDbId") is not None else None
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
        """Fetch detailed metadata for a given slug from PoseidonHD2."""
        ctype = ContentType(content_type)
        prefix = "/pelicula" if ctype == ContentType.MOVIE else "/serie"
        url = self.build_url(f"{prefix}/{slug}")

        try:
            res = await self.http_client.get(url)
            if res.status_code != 200:
                return None
            html = res.text
        except Exception as exc:
            logger.warning("[%s] Detail fetch failed for %s: %s", self.name, url, exc)
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
        data = (
            page_props.get("data")
            or page_props.get("thisMovie")
            or page_props.get("thisSerie")
            or {}
        )
        if not isinstance(data, dict) or not data:
            return None

        title = data.get("title", slug)
        original_title = data.get("originalTitle") or data.get("original_title")
        raw_type = str(data.get("type", "")).lower()
        item_type = (
            ContentType.SERIES
            if raw_type in ("tv", "series", "serie")
            else ctype
        )

        year = data.get("year")
        if year is not None:
            try:
                year = int(str(year).strip())
            except ValueError:
                year = None

        tmdb_id = str(data["TMDbId"]) if data.get("TMDbId") is not None else None
        imdb_id = str(data["IMDbId"]).lower() if data.get("IMDbId") is not None else None
        overview = data.get("overview") or data.get("synopsis")
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
            url=url,
            poster_url=poster,
            imdb_id=imdb_id,
            tmdb_id=tmdb_id,
            overview=overview,
            genres=genres,
            raw_data=data,
        )


PoseidonScraper = PoseidonHD2Scraper
