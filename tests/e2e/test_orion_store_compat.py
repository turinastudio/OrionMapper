"""OrionServer FileIdentityMappingStore Compatibility Test Suite.
Verifies exact contract compatibility with OrionServer Kotlin data models:
- IdentityMapping (org.orion.core.identity.IdentityMapping)
- ImdbIdentityIndex (org.orion.core.identity.ImdbIdentityIndex)
- TmdbIdentityIndex (org.orion.core.identity.TmdbIdentityIndex)
- FileIdentityMappingStore path layout & unpadded Base64 URL encoding.
"""

import json

import pytest

from tests.conftest import decode_orion_provider_key, encode_orion_provider_key


@pytest.mark.orion_compat
@pytest.mark.e2e
class TestOrionStoreCompatibility:
    def test_provider_key_base64url_unpadded_encoding(self):
        """Contract: Base64.getUrlEncoder().withoutPadding().encodeToString(provider:slug)."""
        test_cases = [
            ("serieskao", "el-club-de-la-lucha", "c2VyaWVza2FvOmVsLWNsdWItZGUtbGEtbHVjaGE"),
            ("poseidonhd2", "zombieland-saga", "cG9zZWlkb25oZDI6em9tYmllbGFuZC1zYWdh"),
            ("gnula", "pelicula-el-club-de-la-lucha", "Z251bGE6cGVsaWN1bGEtZWwtY2x1Yi1kZS1sYS1sdWNoYQ"),
            ("allcalidad", "zombieland-saga", "YWxsY2FsaWRhZDp6b21iaWVsYW5kLXNhZ2E"),
        ]

        for provider, slug, expected_key in test_cases:
            generated = encode_orion_provider_key(provider, slug)
            assert generated == expected_key
            assert "=" not in generated
            # Verify bidirectional decoding
            decoded = decode_orion_provider_key(generated)
            assert decoded == f"{provider.lower()}:{slug}"

    def test_provider_identity_mapping_json_schema(self, temp_orion_dir):
        """Contract: `providers/{key}.json` matches Kotlin IdentityMapping data class schema."""
        try:
            from orion_mapper.storage.orion_exporter import OrionExporter

            from orion_mapper.models.mapping import CanonicalMapping

            exporter = OrionExporter(output_dir=temp_orion_dir)
            mapping = CanonicalMapping(
                tmdb_id="550",
                imdb_id="tt0137523",
                title="El Club de la Lucha",
                type="movie",
                year=1999,
                providers={"serieskao": "el-club-de-la-lucha"},
                updated_at=1700000000000
            )
            exporter.export_mappings([mapping])

            key = encode_orion_provider_key("serieskao", "el-club-de-la-lucha")
            target_file = temp_orion_dir / "providers" / f"{key}.json"
            assert target_file.exists()

            data = json.loads(target_file.read_text(encoding="utf-8"))

            # Kotlin IdentityMapping required & optional fields
            assert "provider" in data and isinstance(data["provider"], str)
            assert data["provider"] == data["provider"].lower()
            assert "slug" in data and isinstance(data["slug"], str)
            assert "imdb_id" in data
            assert data["imdb_id"] == "tt0137523"
            assert "tmdb_id" in data
            assert data["tmdb_id"] == "550"
            assert "type" in data
            assert data["type"] == "movie"
            assert "updatedAt" in data and isinstance(data["updatedAt"], int)
            assert data["updatedAt"] > 0

        except ImportError:
            pytest.skip("orion_mapper not yet implemented")

    def test_imdb_identity_index_json_schema(self, temp_orion_dir):
        """Contract: `imdb/{imdb_id.lowercase()}.json` matches Kotlin ImdbIdentityIndex data class schema."""
        try:
            from orion_mapper.storage.orion_exporter import OrionExporter

            from orion_mapper.models.mapping import CanonicalMapping

            exporter = OrionExporter(output_dir=temp_orion_dir)
            mapping = CanonicalMapping(
                tmdb_id="82856",
                imdb_id="tt15486",
                title="Zombieland Saga",
                type="series",
                year=2018,
                providers={
                    "serieskao": "zombieland-saga",
                    "poseidonhd2": "zombieland-saga"
                },
                updated_at=1700000000000
            )
            exporter.export_mappings([mapping])

            target_file = temp_orion_dir / "imdb" / "tt15486.json"
            assert target_file.exists()

            data = json.loads(target_file.read_text(encoding="utf-8"))

            # Kotlin ImdbIdentityIndex required fields
            assert "imdb_id" in data and data["imdb_id"] == "tt15486"
            assert "tmdb_id" in data and data["tmdb_id"] == "82856"
            assert "type" in data and data["type"] == "series"
            assert "providers" in data and isinstance(data["providers"], dict)
            assert data["providers"]["serieskao"] == "zombieland-saga"
            assert data["providers"]["poseidonhd2"] == "zombieland-saga"
            assert "updatedAt" in data and isinstance(data["updatedAt"], int)

        except ImportError:
            pytest.skip("orion_mapper not yet implemented")

    def test_tmdb_identity_index_json_schema(self, temp_orion_dir):
        """Contract: `tmdb/{tmdb_id}.json` matches Kotlin TmdbIdentityIndex data class schema."""
        try:
            from orion_mapper.storage.orion_exporter import OrionExporter

            from orion_mapper.models.mapping import CanonicalMapping

            exporter = OrionExporter(output_dir=temp_orion_dir)
            mapping = CanonicalMapping(
                tmdb_id="550",
                imdb_id="tt0137523",
                title="Fight Club",
                type="movie",
                year=1999,
                providers={"serieskao": "fight-club"},
                updated_at=1700000000000
            )
            exporter.export_mappings([mapping])

            target_file = temp_orion_dir / "tmdb" / "550.json"
            assert target_file.exists()

            data = json.loads(target_file.read_text(encoding="utf-8"))

            # Kotlin TmdbIdentityIndex required fields
            assert "tmdb_id" in data and data["tmdb_id"] == "550"
            assert "imdb_id" in data and data["imdb_id"] == "tt0137523"
            assert "updatedAt" in data and isinstance(data["updatedAt"], int)

        except ImportError:
            pytest.skip("orion_mapper not yet implemented")

    def test_provider_name_and_imdb_lowercase_normalization(self, temp_orion_dir):
        """Contract: Provider names and IMDb IDs in paths and JSON bodies must always be strictly lowercase."""
        try:
            from orion_mapper.storage.orion_exporter import OrionExporter

            from orion_mapper.models.mapping import CanonicalMapping

            exporter = OrionExporter(output_dir=temp_orion_dir)
            mapping = CanonicalMapping(
                tmdb_id="550",
                imdb_id="TT0137523",  # Uppercase input
                title="Fight Club",
                type="movie",
                year=1999,
                providers={"SeriesKao": "Fight-Club-Slug"},  # Mixed case provider
                updated_at=1700000000000
            )
            exporter.export_mappings([mapping])

            # File names must be lowercase
            assert (temp_orion_dir / "imdb" / "tt0137523.json").exists()
            assert not (temp_orion_dir / "imdb" / "TT0137523.json").exists()

            # Provider key must be encoded from lowercase provider name
            expected_key = encode_orion_provider_key("serieskao", "Fight-Club-Slug")
            assert (temp_orion_dir / "providers" / f"{expected_key}.json").exists()

        except ImportError:
            pytest.skip("orion_mapper not yet implemented")
