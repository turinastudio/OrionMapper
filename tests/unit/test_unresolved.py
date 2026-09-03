"""Unit tests for unresolved-backlog skip/prune helpers."""

from __future__ import annotations

import json

from orion_mapper.cli.commands import (
    _load_unresolved_slugs,
    _prune_unresolved_slugs,
    _record_unresolved_records,
)


def test_load_unresolved_slugs_filters_by_type(tmp_path):
    _record_unresolved_records(
        "someprov",
        [
            {"provider": "someprov", "slug": "a", "type": "movie", "reason": "x"},
            {"provider": "someprov", "slug": "b", "type": "series", "reason": "x"},
        ],
        output_dir=tmp_path,
    )
    assert _load_unresolved_slugs("someprov", "movie", output_dir=tmp_path) == {"a"}
    assert _load_unresolved_slugs("someprov", "series", output_dir=tmp_path) == {"b"}
    assert _load_unresolved_slugs("unknown", "movie", output_dir=tmp_path) == set()


def test_prune_removes_only_recovered(tmp_path):
    _record_unresolved_records(
        "someprov",
        [
            {"provider": "someprov", "slug": "a", "type": "movie", "reason": "x"},
            {"provider": "someprov", "slug": "b", "type": "movie", "reason": "x"},
            {"provider": "someprov", "slug": "c", "type": "series", "reason": "x"},
        ],
        output_dir=tmp_path,
    )
    assert _prune_unresolved_slugs("someprov", ["a"], "movie", output_dir=tmp_path) == 1
    remaining = json.loads((tmp_path / "someprov.json").read_text(encoding="utf-8"))
    assert {(r["slug"], r["type"]) for r in remaining} == {("b", "movie"), ("c", "series")}
    assert _prune_unresolved_slugs("someprov", ["zzz"], "movie", output_dir=tmp_path) == 0
