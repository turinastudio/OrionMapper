from __future__ import annotations

import base64
import time

from pydantic import BaseModel, ConfigDict, Field


def encode_provider_key(provider: str, slug: str) -> str:
    """
    Computes unpadded Base64 URL-safe encoding of 'provider:slug' matching
    Kotlin: Base64.getUrlEncoder().withoutPadding().encodeToString("$provider:$slug".toByteArray(Charsets.UTF_8))
    """
    raw = f"{provider.strip().lower()}:{slug.strip()}".encode()
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_provider_key(encoded: str) -> tuple[str, str]:
    """Decodes an unpadded Base64 URL-safe key back into (provider, slug)."""
    padding = "=" * ((4 - len(encoded) % 4) % 4)
    raw = base64.urlsafe_b64decode((encoded + padding).encode("ascii")).decode("utf-8")
    provider, slug = raw.split(":", 1)
    return provider, slug


class IdentityMappingExport(BaseModel):
    """Corresponds to OrionServer's org.orion.core.identity.IdentityMapping."""
    model_config = ConfigDict(populate_by_name=True)

    provider: str
    slug: str
    imdb_id: str | None = None
    tmdb_id: str | None = None
    type: str | None = None
    updatedAt: int = Field(default_factory=lambda: int(time.time() * 1000))

    def get_export_filename(self) -> str:
        return f"providers/{encode_provider_key(self.provider, self.slug)}.json"


class ImdbIdentityIndexExport(BaseModel):
    """Corresponds to OrionServer's org.orion.core.identity.ImdbIdentityIndex."""
    model_config = ConfigDict(populate_by_name=True)

    imdb_id: str
    tmdb_id: str | None = None
    type: str | None = None
    providers: dict[str, str] = Field(default_factory=dict)
    updatedAt: int = Field(default_factory=lambda: int(time.time() * 1000))

    def get_export_filename(self) -> str:
        return f"imdb/{self.imdb_id.strip().lower()}.json"


class TmdbIdentityIndexExport(BaseModel):
    """Corresponds to OrionServer's org.orion.core.identity.TmdbIdentityIndex."""
    model_config = ConfigDict(populate_by_name=True)

    tmdb_id: str
    imdb_id: str | None = None
    updatedAt: int = Field(default_factory=lambda: int(time.time() * 1000))

    def get_export_filename(self) -> str:
        return f"tmdb/{self.tmdb_id.strip()}.json"
