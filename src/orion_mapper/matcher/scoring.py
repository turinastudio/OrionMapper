from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from orion_mapper.matcher.normalizer import (
    YEAR_REGEX,
    ParsedTitle,
    TitleNormalizer,
)


@dataclass(frozen=True)
class MatchResult:
    """Detailed score evaluation result for a TMDB candidate."""

    score: float
    confidence: Literal["high", "candidate", "low"]
    matched_title: str
    tmdb_id: str | None
    imdb_id: str | None
    candidate: dict[str, Any]


class FuzzyTitleMatcher:
    """Fuzzy comparison between title pairs with year and media type weighting."""

    @staticmethod
    def token_overlap(left: str, right: str) -> float:
        """Calculate Jaccard-style token overlap between two strings."""
        a = set(TitleNormalizer.normalize(left).split())
        b = set(TitleNormalizer.normalize(right).split())
        if not a or not b:
            return 0.0
        return len(a & b) / max(len(a), len(b))

    @classmethod
    def score(
        cls,
        title1: str,
        title2: str,
        year1: int | None = None,
        year2: int | None = None,
        type1: str | None = None,
        type2: str | None = None,
    ) -> float:
        """
        Compute weighted title match score mirroring OrionServer TitleMatcher.kt.
        Base score: overlap * 70 + year delta (+25/0, +10/1, -35/>=2) + type (+5 match, -40 mismatch).
        """
        norm1 = TitleNormalizer.normalize(title1)
        norm2 = TitleNormalizer.normalize(title2)
        if not norm1 or not norm2:
            return 0.0

        overlap = cls.token_overlap(norm1, norm2)
        if norm1 == norm2:
            overlap = 1.0
        elif norm1 in norm2 or norm2 in norm1:
            min_len = min(len(norm1), len(norm2))
            if min_len >= 4:
                overlap = max(overlap, 0.86)
            else:
                tokens1 = set(norm1.split())
                tokens2 = set(norm2.split())
                if tokens1 == tokens2 or (
                    tokens1
                    and tokens2
                    and (tokens1.issubset(tokens2) or tokens2.issubset(tokens1))
                ):
                    overlap = max(overlap, 0.86)

        total = overlap * 70.0

        # Year adjustments
        if year1 is not None and year2 is not None:
            delta = abs(int(year1) - int(year2))
            if delta == 0:
                total += 25.0
            elif delta == 1:
                total += 10.0
            else:
                total -= 35.0
        elif year1 is None and year2 is not None:
            if overlap >= 0.99:
                total += 20.0
        elif year1 is not None and year2 is None:
            total -= 5.0

        # Media type adjustments
        if type1 is not None and type2 is not None:
            norm_t1 = "series" if str(type1).lower() in ("tv", "series") else "movie"
            norm_t2 = "series" if str(type2).lower() in ("tv", "series") else "movie"
            if norm_t1 == norm_t2:
                total += 5.0
            else:
                total -= 40.0

        return max(0.0, total)


class CandidateScorer:
    """Candidate scoring algorithm implementing 100% parity with OrionServer's TitleMatcher.kt."""

    @staticmethod
    def extract_year(candidate: dict[str, Any]) -> int | None:
        """Extract release year from TMDB candidate dictionary."""
        if not isinstance(candidate, dict):
            return None
        raw_date = candidate.get("release_date") or candidate.get("first_air_date") or ""
        match = YEAR_REGEX.search(str(raw_date))
        return int(match.group(0)) if match else None

    @staticmethod
    def is_generic(title: str) -> bool:
        """Check if title is short and generic (<= 2 words and < 14 chars)."""
        normalized = TitleNormalizer.normalize(title)
        tokens = [t for t in normalized.split() if t]
        return len(tokens) <= 2 and len(normalized.replace(" ", "")) < 14

    @classmethod
    def score_candidate(
        cls,
        parsed: ParsedTitle,
        candidate: dict[str, Any],
        content_type: str,
    ) -> MatchResult:
        """Score candidate against parsed query title following Kotlin TitleMatcher algorithm."""
        if not isinstance(candidate, dict):
            candidate = {}

        candidate_titles = [
            candidate.get("title"),
            candidate.get("name"),
            candidate.get("original_title"),
            candidate.get("original_name"),
        ]
        valid_titles = [
            str(t).strip() for t in candidate_titles if t is not None and str(t).strip()
        ]

        target_titles = (
            parsed.search_titles if parsed.search_titles else [parsed.normalized_title]
        )

        best_score = 0.0
        best_title = ""

        for target in target_titles:
            norm_target = TitleNormalizer.normalize(target)
            for value in valid_titles:
                norm_value = TitleNormalizer.normalize(value)
                score = FuzzyTitleMatcher.token_overlap(norm_target, norm_value)
                if norm_value == norm_target:
                    score = 1.0
                elif norm_value in norm_target or norm_target in norm_value:
                    min_len = min(len(norm_value), len(norm_target))
                    if min_len >= 4:
                        score = max(score, 0.86)
                    else:
                        tokens_target = set(norm_target.split())
                        tokens_val = set(norm_value.split())
                        if tokens_val == tokens_target or (
                            tokens_val
                            and tokens_target
                            and (
                                tokens_val.issubset(tokens_target)
                                or tokens_target.issubset(tokens_val)
                            )
                        ):
                            score = max(score, 0.86)

                if score > best_score:
                    best_score = score
                    best_title = value

        total = best_score * 70.0
        candidate_year = cls.extract_year(candidate)

        if parsed.year is not None and candidate_year is not None:
            delta = abs(parsed.year - candidate_year)
            if delta == 0:
                total += 25.0
            elif delta == 1:
                total += 10.0
            else:
                total -= 35.0
        elif parsed.year is not None:
            total -= 5.0

        raw_media_type = candidate.get("media_type")
        if raw_media_type is not None:
            media_type_str = str(raw_media_type).lower()
            target_media = "movie" if str(content_type).lower() == "movie" else "tv"
            if media_type_str == target_media:
                total += 5.0
            else:
                total -= 40.0

        target_base = parsed.base_title or parsed.normalized_title
        if cls.is_generic(target_base) and parsed.year is None:
            total -= 20.0

        if parsed.provider_family == "anime":
            raw_lang = candidate.get("original_language")
            lang = str(raw_lang).lower() if raw_lang is not None else ""
            raw_countries = candidate.get("origin_country")
            countries: list[str] = []
            if isinstance(raw_countries, (list, tuple, set)):
                countries = [str(c).upper() for c in raw_countries if c is not None]
            elif isinstance(raw_countries, str):
                countries = [raw_countries.upper()]

            if lang in ("ja", "zh") or any(c in ("JP", "CN") for c in countries):
                total += 12.0
            if best_score >= 0.99 and (lang == "ja" or "JP" in countries):
                total += 25.0
            if best_score >= 0.70 and parsed.season_hints:
                total += 8.0

        exact_title_and_year = (
            best_score >= 0.99
            and parsed.year is not None
            and candidate_year == parsed.year
        )
        imdb_id = candidate.get("imdb_id")
        if not imdb_id and not exact_title_and_year:
            total -= 10.0

        confidence: Literal["high", "candidate", "low"]
        if total >= 88.0:
            confidence = "high"
        elif total >= 72.0:
            confidence = "candidate"
        else:
            confidence = "low"

        tmdb_id = str(candidate.get("id")) if candidate.get("id") is not None else None

        return MatchResult(
            score=total,
            confidence=confidence,
            matched_title=best_title,
            tmdb_id=tmdb_id,
            imdb_id=imdb_id,
            candidate=candidate,
        )
