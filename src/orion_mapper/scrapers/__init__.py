from __future__ import annotations

from typing import Any

from orion_mapper.core.http import AsyncHttpClient
from orion_mapper.core.rate_limiter import TokenBucketLimiter
from orion_mapper.scrapers.allcalidad import AllCalidadScraper
from orion_mapper.scrapers.base import BaseScraper
from orion_mapper.scrapers.gnula import GnulaScraper
from orion_mapper.scrapers.poseidonhd2 import PoseidonHD2Scraper, PoseidonScraper
from orion_mapper.scrapers.serieskao import SeriesKaoScraper

_DEFAULT_REGISTRY: dict[str, type[BaseScraper]] = {
    "serieskao": SeriesKaoScraper,
    "poseidonhd2": PoseidonHD2Scraper,
    "gnula": GnulaScraper,
    "allcalidad": AllCalidadScraper,
}

_SCRAPER_REGISTRY: dict[str, type[BaseScraper]] = dict(_DEFAULT_REGISTRY)

_DEFAULT_ALIASES: dict[str, str] = {
    "poseidon": "poseidonhd2",
    "series-kao": "serieskao",
    "all-calidad": "allcalidad",
}

_PROVIDER_ALIASES: dict[str, str] = dict(_DEFAULT_ALIASES)


def register_scraper(
    name: str,
    scraper_cls: type[BaseScraper],
    aliases: list[str] | tuple[str, ...] | None = None,
) -> None:
    """Register or override a scraper class in the global registry."""
    key = name.strip().lower()
    _SCRAPER_REGISTRY[key] = scraper_cls
    if aliases:
        for alias in aliases:
            a_key = alias.strip().lower()
            if a_key:
                _PROVIDER_ALIASES[a_key] = key


def reset_registry() -> None:
    """Reset the registry to default built-in scrapers."""
    global _SCRAPER_REGISTRY, _PROVIDER_ALIASES
    _SCRAPER_REGISTRY = dict(_DEFAULT_REGISTRY)
    _PROVIDER_ALIASES = dict(_DEFAULT_ALIASES)


def get_scraper(
    name: str,
    http_client: AsyncHttpClient | Any = None,
    rate_limiter: TokenBucketLimiter | None = None,
    **kwargs: Any,
) -> BaseScraper:
    """
    Factory function to instantiate a scraper by provider name.
    Lookup is case-insensitive, trims surrounding whitespace, and resolves aliases.
    """
    if not isinstance(name, str):
        raise ValueError(f"Provider name must be a string, got {type(name).__name__}")

    key = name.strip().lower()
    if not key:
        available = sorted(_SCRAPER_REGISTRY.keys())
        raise ValueError(f"Empty provider name provided. Available providers: {available}")

    key = _PROVIDER_ALIASES.get(key, key)

    if key not in _SCRAPER_REGISTRY:
        available = sorted(_SCRAPER_REGISTRY.keys())
        raise ValueError(f"Unknown provider '{name}'. Available providers: {available}")

    scraper_cls = _SCRAPER_REGISTRY[key]
    return scraper_cls(http_client=http_client, rate_limiter=rate_limiter, **kwargs)


def get_registered_providers() -> list[str]:
    """Return a sorted list of registered provider names."""
    return sorted(_SCRAPER_REGISTRY.keys())


def list_scrapers() -> list[str]:
    """Alias for get_registered_providers()."""
    return get_registered_providers()


__all__ = [
    "AllCalidadScraper",
    "BaseScraper",
    "GnulaScraper",
    "PoseidonHD2Scraper",
    "PoseidonScraper",
    "SeriesKaoScraper",
    "get_registered_providers",
    "get_scraper",
    "list_scrapers",
    "register_scraper",
    "reset_registry",
]
