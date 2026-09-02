from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from orion_mapper.core.config import settings
from orion_mapper.models.mapping import CanonicalMapping
from orion_mapper.models.orion import (
    IdentityMappingExport,
    ImdbIdentityIndexExport,
    TmdbIdentityIndexExport,
    decode_provider_key,
    encode_provider_key,
)
from orion_mapper.storage.master import atomic_write_json

if TYPE_CHECKING:
    from orion_mapper.storage.master import MasterMappingStore


class ExportSummary(BaseModel):
    imdb_count: int = Field(default=0, description="Total IMDb index files generated")
    tmdb_count: int = Field(default=0, description="Total TMDB index files generated")
    provider_count: int = Field(default=0, description="Total Provider mapping files generated")
    total_files: int = Field(default=0, description="Total index files written")
    total_bytes: int = Field(default=0, description="Total bytes written to disk")
    duration_ms: float = Field(default=0.0, description="Export execution duration in milliseconds")


class OrionExporter:
    """
    Exports canonical mappings into the OrionServer FileIdentityMappingStore filesystem tree.
    Generates:
      - imdb/{imdb_id.lower()}.json
      - tmdb/{tmdb_id}.json
      - providers/{unpadded_base64url}.json
    """

    def __init__(self, output_dir: Path | str | None = None) -> None:
        self.output_dir = (
            Path(output_dir) if output_dir is not None else settings.orion_mappings_dir
        )

    @staticmethod
    def encode_provider_key(provider: str, slug: str) -> str:
        """Computes unpadded Base64 URL-safe key for provider:slug."""
        return encode_provider_key(provider, slug)

    @staticmethod
    def decode_provider_key(encoded: str) -> tuple[str, str]:
        """Decodes unpadded Base64 URL-safe key back into (provider, slug)."""
        return decode_provider_key(encoded)

    def export_mappings(self, mappings: list[CanonicalMapping]) -> ExportSummary:
        """
        Exports a list of CanonicalMapping instances to the OrionServer directory structure.
        """
        start_time = time.perf_counter()

        imdb_dir = self.output_dir / "imdb"
        tmdb_dir = self.output_dir / "tmdb"
        providers_dir = self.output_dir / "providers"

        imdb_dir.mkdir(parents=True, exist_ok=True)
        tmdb_dir.mkdir(parents=True, exist_ok=True)
        providers_dir.mkdir(parents=True, exist_ok=True)

        imdb_count = 0
        tmdb_count = 0
        provider_count = 0
        total_bytes = 0

        for m in mappings:
            # 1. Export IMDb Index
            if m.imdb_id:
                norm_imdb = m.imdb_id.strip().lower()
                c_type_str = (
                    str(m.type.value if hasattr(m.type, "value") else m.type).lower()
                    if m.type
                    else None
                )
                imdb_export = ImdbIdentityIndexExport(
                    imdb_id=norm_imdb,
                    tmdb_id=m.tmdb_id,
                    type=c_type_str,
                    providers=m.providers,
                    updatedAt=m.updated_at,
                )
                target = imdb_dir / f"{norm_imdb}.json"
                bytes_w = atomic_write_json(target, imdb_export.model_dump(mode="json"))
                imdb_count += 1
                total_bytes += bytes_w

            # 2. Export TMDB Index
            if m.tmdb_id:
                norm_tmdb = str(m.tmdb_id).strip()
                tmdb_export = TmdbIdentityIndexExport(
                    tmdb_id=norm_tmdb,
                    imdb_id=m.imdb_id.strip().lower() if m.imdb_id else None,
                    updatedAt=m.updated_at,
                )
                target = tmdb_dir / f"{norm_tmdb}.json"
                bytes_w = atomic_write_json(target, tmdb_export.model_dump(mode="json"))
                tmdb_count += 1
                total_bytes += bytes_w

            # 3. Export Provider Identity Mappings
            for prov_name, slug_values in m.all_provider_slugs().items():
                norm_prov = prov_name.strip().lower()
                for slug_val in slug_values:
                    norm_slug = slug_val.strip()
                    prov_key = self.encode_provider_key(norm_prov, norm_slug)
                    c_type_str = (
                        str(m.type.value if hasattr(m.type, "value") else m.type).lower()
                        if m.type
                        else None
                    )
                    prov_export = IdentityMappingExport(
                        provider=norm_prov,
                        slug=norm_slug,
                        imdb_id=m.imdb_id.strip().lower() if m.imdb_id else None,
                        tmdb_id=m.tmdb_id,
                        type=c_type_str,
                        updatedAt=m.updated_at,
                    )
                    target = providers_dir / f"{prov_key}.json"
                    bytes_w = atomic_write_json(target, prov_export.model_dump(mode="json"))
                    provider_count += 1
                    total_bytes += bytes_w

        duration_ms = (time.perf_counter() - start_time) * 1000.0

        return ExportSummary(
            imdb_count=imdb_count,
            tmdb_count=tmdb_count,
            provider_count=provider_count,
            total_files=imdb_count + tmdb_count + provider_count,
            total_bytes=total_bytes,
            duration_ms=round(duration_ms, 2),
        )

    def export_store(self, master_store: MasterMappingStore) -> ExportSummary:
        """Exports all canonical mappings from the MasterMappingStore."""
        return self.export_mappings(master_store.all_mappings())
