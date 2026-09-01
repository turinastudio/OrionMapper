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

PLAYER_IMDB_REGEX = re.compile(r"/vidurl/(tt\d{1,10})/", re.IGNORECASE)
IMDB_REGEX = re.compile(r"\b(tt\d{1,10})\b", re.IGNORECASE)
YEAR_REGEX = re.compile(r"\b(19\d\d|20\d\d)\b")


class SeriesKaoScraper(BaseScraper):
    """
    Scraper for SeriesKao (https://serieskao.top).
    Parses HTML catalog and detail pages, extracting metadata via JSON-LD schemas
    and player iframe URL regex patterns (/vidurl/tt...).
    """

    name = "serieskao"
    base_url = "https://serieskao.top"
    supported_types: ClassVar[list[ContentType]] = [ContentType.MOVIE, ContentType.SERIES]
    page_size = 24
    default_rate_limit = 5.0

    def extract_identifiers(
        self, raw_data: dict[str, Any] | str
    ) -> tuple[str | None, str | None]:
        """Extract (imdb_id, tmdb_id) from raw string or dictionary data."""
        if isinstance(raw_data, str):
            match = PLAYER_IMDB_REGEX.search(raw_data) or IMDB_REGEX.search(raw_data)
            if match:
                imdb_val = match.group(1).lower().strip()
                return (imdb_val if imdb_val else None), None
        elif isinstance(raw_data, dict):
            ident = raw_data.get("identifier")
            if ident and isinstance(ident, str):
                cleaned = ident.strip().lower()
                if cleaned.startswith("tt") and len(cleaned) > 2 and cleaned[2:].isdigit():
                    return cleaned, None
        return None, None

    async def fetch_catalog(
        self,
        content_type: ContentType | str,
        page: int = 1,
        genre: str | None = None,
    ) -> list[ScrapedItem]:
        """Fetch a page of catalog items from SeriesKao."""
        ctype = ContentType(content_type)
        if ctype == ContentType.MOVIE:
            path = f"/peliculas?page={page}" if page > 1 else "/peliculas"
        else:
            if genre == "anime":
                path = f"/animes?page={page}" if page > 1 else "/animes/populares"
            else:
                path = f"/series?page={page}" if page > 1 else "/series"

        url = self.build_url(path)
        try:
            res = await self.http_client.get(url)
            if res.status_code != 200:
                return []
            html = res.text
        except Exception as exc:
            logger.warning("[%s] Catalog fetch failed for %s: %s", self.name, url, exc)
            return []

        soup = BeautifulSoup(html, "html.parser")
        items: list[ScrapedItem] = []

        card_elements = soup.select(".item-list .item, .item, article.item")
        for card in card_elements:
            try:
                link = card.select_one("a")
                if not link:
                    continue
                href = link.get("href", "")
                if not href:
                    continue
                slug = href.strip("/").split("/")[-1]
                if not slug or slug.lower() in {"pelicula", "peliculas", "serie", "series", "anime", "animes"}:
                    continue

                title_elem = card.select_one(".title, h2, h3")
                title = title_elem.get_text(strip=True) if title_elem else link.get("title", slug)

                year = None
                year_elem = card.select_one(".year, .release-year, span.year")
                if year_elem:
                    m = YEAR_REGEX.search(year_elem.get_text())
                    if m:
                        year = int(m.group(1))

                img_elem = card.select_one("img")
                poster_url = img_elem.get("src") if img_elem else None

                type_elem = card.select_one(".type, span.type")
                item_type = ctype
                if type_elem:
                    t_str = type_elem.get_text(strip=True).lower()
                    if "pelicula" in t_str or "movie" in t_str:
                        item_type = ContentType.MOVIE
                    elif "serie" in t_str or "tv" in t_str:
                        item_type = ContentType.SERIES

                items.append(
                    ScrapedItem(
                        provider=self.name,
                        slug=slug,
                        title=title,
                        type=item_type,
                        year=year,
                        url=self.build_url(href),
                        poster_url=poster_url,
                    )
                )
            except (ValidationError, Exception) as exc:
                logger.warning("[%s] Failed to parse catalog card: %s", self.name, exc)
                continue

        return items

    async def fetch_detail(
        self,
        slug: str,
        content_type: ContentType | str,
    ) -> ScrapedDetail | None:
        """Fetch detailed metadata for a given slug from SeriesKao."""
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

        soup = BeautifulSoup(html, "html.parser")

        title = slug
        original_title: str | None = None
        year: int | None = None
        imdb_id: str | None = None
        genres: list[str] = []
        overview: str | None = None
        poster_url: str | None = None
        release_date: str | None = None

        # 1. Parse JSON-LD structured data if present
        ld_tags = soup.find_all("script", type="application/ld+json")
        for ld in ld_tags:
            if not ld.string:
                continue
            try:
                parsed = json.loads(ld.string)
                entries = parsed if isinstance(parsed, list) else [parsed]
                for data in entries:
                    if not isinstance(data, dict):
                        continue
                    ld_type = str(data.get("@type", "")).lower()
                    if any(t in ld_type for t in ("movie", "tvseries", "series", "creativework")):
                        if data.get("name"):
                            title = str(data["name"])
                        if data.get("identifier"):
                            ident = str(data["identifier"]).strip().lower()
                            if ident.startswith("tt"):
                                imdb_id = ident
                        dt = str(data.get("dateCreated") or data.get("startDate") or data.get("datePublished") or "")
                        if dt:
                            release_date = dt
                            m = YEAR_REGEX.search(dt)
                            if m:
                                year = int(m.group(1))
                        if data.get("genre"):
                            g = data["genre"]
                            genres = [str(g)] if isinstance(g, str) else [str(x) for x in g]
                        if data.get("description"):
                            overview = str(data["description"])
                        if data.get("image") and isinstance(data["image"], str):
                            poster_url = data["image"]
            except Exception:
                pass

        # 2. Player iframe regex fallback for IMDb ID
        if not imdb_id:
            match = PLAYER_IMDB_REGEX.search(html) or IMDB_REGEX.search(html)
            if match:
                imdb_id = match.group(1).lower()

        # 3. HTML tag fallbacks for missing fields
        if title == slug:
            h1 = soup.select_one("h1.title, .movie-info .title, .series-info .title, h1")
            if h1:
                title = h1.get_text(strip=True)

        if not year:
            y_elem = soup.select_one(".release-year, .year, span.release-year")
            if y_elem:
                m = YEAR_REGEX.search(y_elem.get_text())
                if m:
                    year = int(m.group(1))

        if not overview:
            ov_elem = soup.select_one(".overview, .sinopsis, p.description")
            if ov_elem:
                overview = ov_elem.get_text(strip=True)

        return ScrapedDetail(
            provider=self.name,
            slug=slug,
            title=title,
            original_title=original_title,
            type=ctype,
            year=year,
            url=url,
            poster_url=poster_url,
            imdb_id=imdb_id,
            tmdb_id=None,
            overview=overview,
            genres=genres,
            release_date=release_date,
        )
