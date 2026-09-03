from __future__ import annotations

from typing import ClassVar

from orion_mapper.models.item import ContentType
from orion_mapper.scrapers.allcalidad import AllCalidadScraper


class AllPeliculasScraper(AllCalidadScraper):
    """
    Scraper for AllPeliculas (https://allpeliculas.la).

    AllCalidad clone: same WordPress theme and post payloads (including the
    ``md5(str(tmdb_id))`` image scheme), served under the ``/wp-api/v1``
    API base with path-style listing/detail routes. Mirrors OrionServer's
    ``AllCalidadSite.AllPeliculas`` variant.
    """

    name = "allpeliculas"
    base_url = "https://allpeliculas.la"
    supported_types: ClassVar[list[ContentType]] = [ContentType.MOVIE, ContentType.SERIES]
    page_size = 24
    default_rate_limit = 5.0
    api_base: ClassVar[str] = "/wp-api/v1"
    listing_style: ClassVar[str] = "type_path"
    single_style: ClassVar[str] = "type_path"
