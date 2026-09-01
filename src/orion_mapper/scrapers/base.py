from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any, ClassVar
from urllib.parse import urljoin

from orion_mapper.core.http import AsyncHttpClient
from orion_mapper.core.rate_limiter import RateLimiterRegistry, TokenBucketLimiter
from orion_mapper.models.item import ContentType, ScrapedDetail, ScrapedItem

logger = logging.getLogger(__name__)


class BaseScraper(ABC):
    """
    Abstract base class for all provider scrapers.
    """

    name: str
    base_url: str
    supported_types: ClassVar[list[ContentType]] = [ContentType.MOVIE, ContentType.SERIES]
    page_size: int = 24
    default_rate_limit: float = 5.0

    def __init__(
        self,
        http_client: AsyncHttpClient,
        rate_limiter: TokenBucketLimiter | None = None,
    ) -> None:
        if not getattr(self, "name", None):
            raise ValueError(f"Scraper class {self.__class__.__name__} must define 'name'")
        if not getattr(self, "base_url", None):
            raise ValueError(f"Scraper class {self.__class__.__name__} must define 'base_url'")

        self.http_client = http_client
        self.rate_limiter = rate_limiter or RateLimiterRegistry.get_limiter(
            self.name,
            rate=self.default_rate_limit,
        )

    def build_url(self, path: str) -> str:
        """Construct an absolute URL against the scraper base_url."""
        return urljoin(self.base_url, path)

    def extract_identifiers(
        self, raw_data: dict[str, Any] | str
    ) -> tuple[str | None, str | None]:
        """
        Optional helper to extract (imdb_id, tmdb_id) from raw provider responses.
        Subclasses may override this.
        """
        return None, None

    @abstractmethod
    async def fetch_catalog(
        self,
        content_type: ContentType,
        page: int = 1,
        genre: str | None = None,
    ) -> list[ScrapedItem]:
        """
        Fetch a single page of items from the provider catalog.
        Returns an empty list if the page is out of bounds or empty.
        """
        raise NotImplementedError

    @abstractmethod
    async def fetch_detail(
        self,
        slug: str,
        content_type: ContentType,
    ) -> ScrapedDetail | None:
        """
        Fetch full details and embedded metadata/identifiers for a specific item slug.
        Returns None if the item is not found (404).
        """
        raise NotImplementedError

    async def crawl_catalog(
        self,
        content_type: ContentType,
        max_pages: int | None = None,
        genre: str | None = None,
    ) -> AsyncIterator[ScrapedItem]:
        """
        Async generator iterating through catalog pages until max_pages is reached
        or fetch_catalog returns an empty list.
        """
        if content_type not in self.supported_types:
            logger.warning(
                "Provider %s does not support content type '%s'",
                self.name,
                content_type,
            )
            return

        page = 1
        while True:
            if max_pages is not None and page > max_pages:
                logger.info(
                    "[%s] Reached maximum requested pages (%d) for %s",
                    self.name,
                    max_pages,
                    content_type,
                )
                break

            logger.debug(
                "[%s] Crawling %s page %d (genre=%s)",
                self.name,
                content_type,
                page,
                genre,
            )
            try:
                items = await self.fetch_catalog(
                    content_type=content_type, page=page, genre=genre
                )
            except Exception as exc:
                logger.error(
                    "[%s] Error fetching %s page %d: %s",
                    self.name,
                    content_type,
                    page,
                    exc,
                )
                break

            if not items:
                logger.info(
                    "[%s] No more items found on %s page %d. Ending crawl.",
                    self.name,
                    content_type,
                    page,
                )
                break

            for item in items:
                yield item

            page += 1
