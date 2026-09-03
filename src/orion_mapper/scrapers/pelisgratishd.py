from __future__ import annotations

from typing import ClassVar

from orion_mapper.models.item import ContentType
from orion_mapper.scrapers.serieskao import SeriesKaoScraper


class PelisGratisHDScraper(SeriesKaoScraper):
    """
    Scraper for PelisGratisHD (https://pelisgratishd.zip).

    SeriesKao clone: same HTML catalog/detail layout and player identifier
    scheme, with ``article.cc-item`` catalog cards. Reuses the SeriesKao
    parser; only the provider identity and card selectors differ.
    """

    name = "pelisgratishd"
    base_url = "https://pelisgratishd.zip"
    supported_types: ClassVar[list[ContentType]] = [ContentType.MOVIE, ContentType.SERIES]
    page_size = 24
    default_rate_limit = 5.0
    card_selectors: ClassVar[str] = "article.cc-item"
