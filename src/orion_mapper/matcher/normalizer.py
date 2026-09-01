from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

# Noise words stripped during lightweight normalization
NORMALIZATION_NOISE_WORDS: set[str] = {
    "audio",
    "latino",
    "castellano",
    "completa",
    "completo",
    "hd",
    "4k",
    "1080p",
    "720p",
    "bluray",
    "dvdrip",
    "rip",
    "webrip",
    "dual",
    "subtitulado",
    "subtitulada",
    "sub",
    "online",
}

# Full noise words stripped during deep parse and query preparation
PARSE_NOISE_WORDS: set[str] = NORMALIZATION_NOISE_WORDS | {
    "anime",
    "espanol",
    "movie",
    "pelicula",
    "serie",
    "series",
    "tv",
}

NOISE_WORDS: set[str] = PARSE_NOISE_WORDS

ALIASES: dict[str, list[str]] = {
    "campo de suenos": ["field of dreams"],
    "dr stone science future": ["dr stone"],
    "el gangster": ["hoodlum"],
    "juego sucio": ["deep cover", "play dirty"],
    "los increibles": ["the incredibles", "incredibles"],
    "mairimashita irumakun": ["welcome to demon school iruma kun"],
    "super mario bros": ["the super mario bros movie", "super mario bros movie"],
    "yami shibai": ["yamishibai", "theatre of darkness yamishibai"],
    "yozakurasan chi no daisakusen": ["mission yozakura family"],
}

ORDINAL_WORDS: dict[str, int] = {
    "first": 1,
    "second": 2,
    "third": 3,
    "fourth": 4,
    "fifth": 5,
    "sixth": 6,
    "seventh": 7,
    "eighth": 8,
    "ninth": 9,
    "tenth": 10,
    "primera": 1,
    "segunda": 2,
    "tercera": 3,
    "cuarta": 4,
    "quinta": 5,
    "sexta": 6,
    "septima": 7,
    "octava": 8,
    "novena": 9,
    "decima": 10,
    "primer": 1,
    "segundo": 2,
    "tercer": 3,
    "cuarto": 4,
    "quinto": 5,
    "sexto": 6,
    "septimo": 7,
    "octavo": 8,
    "noveno": 9,
    "decimo": 10,
    "décima": 10,
    "séptima": 7,
    "décimo": 10,
    "séptimo": 7,
}

YEAR_REGEX = re.compile(r"\b(?:18[89]\d|19\d{2}|20\d{2}|2100)\b")
SEASON_NUM_REGEX = re.compile(r"\b(?:season|temporada)\s*([0-9]+)\b", re.IGNORECASE)
SEASON_ORD_REGEX = re.compile(r"\b([0-9]+)(?:st|nd|rd|th)\s+season\b", re.IGNORECASE)
SEASON_SHORT_REGEX = re.compile(r"\bt([0-9]{1,2})\b", re.IGNORECASE)
TRAILING_ARTICLES_REGEX = re.compile(r"\b(?:el|la|los|las|the|a|an)$", re.IGNORECASE)
APOSTROPHE_REGEX = re.compile(r"['\u2019]")
NON_ALPHANUMERIC_REGEX = re.compile(r"[^a-z0-9]+")
WHITESPACE_COLLAPSE_REGEX = re.compile(r"\s+")

ORDINAL_SEASON_PATTERNS: list[tuple[re.Pattern[str], int]] = [
    (re.compile(rf"\b{re.escape(word)}\s+(?:season|temporada)\b", re.IGNORECASE), num)
    for word, num in ORDINAL_WORDS.items()
]

ORDINAL_SEASON_COMBINED_REGEX = re.compile(
    rf"\b(?:{'|'.join(re.escape(w) for w in ORDINAL_WORDS)})\s+(?:season|temporada)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ParsedTitle:
    """Structured representation of parsed title metadata for matching."""

    raw_title: str
    provider_family: str | None
    normalized_title: str
    base_title: str
    year: int | None
    season_hints: list[int] = field(default_factory=list)
    alias_titles: list[str] = field(default_factory=list)
    search_titles: list[str] = field(default_factory=list)


class TitleNormalizer:
    """High-performance title normalizer with Spanish diacritics and noise handling."""

    @staticmethod
    def strip_diacritics(text: str) -> str:
        """Strip accents, tildes, and Catalan/Galician specific characters."""
        text = text.replace("l·l", "ll").replace("L·L", "LL")
        text = text.replace("ç", "c").replace("Ç", "C")
        nfd = unicodedata.normalize("NFD", text)
        return "".join(c for c in nfd if unicodedata.category(c) != "Mn")

    @classmethod
    def normalize(cls, value: str) -> str:
        """
        Normalize title text: lowercases, decomposes diacritics, strips punctuation,
        removes season tags, and collapses whitespaces.
        """
        if not value:
            return ""

        stripped = cls.strip_diacritics(value.lower())
        # Replace symbols and punctuation
        cleaned = stripped.replace("&", " ").replace("¿", " ").replace("¡", " ")
        cleaned = APOSTROPHE_REGEX.sub("", cleaned)

        # Strip season patterns
        cleaned = SEASON_NUM_REGEX.sub(" ", cleaned)
        cleaned = SEASON_ORD_REGEX.sub(" ", cleaned)
        cleaned = SEASON_SHORT_REGEX.sub(" ", cleaned)
        cleaned = ORDINAL_SEASON_COMBINED_REGEX.sub(" ", cleaned)

        # Convert non-alphanumeric to whitespace
        cleaned = NON_ALPHANUMERIC_REGEX.sub(" ", cleaned)

        # Filter out standalone noise tokens
        words = [w for w in cleaned.split() if w and w not in NORMALIZATION_NOISE_WORDS]
        result = " ".join(words)
        return WHITESPACE_COLLAPSE_REGEX.sub(" ", result).strip()

    @classmethod
    def parse(
        cls,
        input_title: str,
        provider_input: str | None = None,
        year: int | str | None = None,
    ) -> ParsedTitle:
        """
        Parse raw title into normalized representation, extracting year, seasons,
        provider family, and alias expansions for TMDB searching.
        """
        raw = input_title.strip() if input_title else ""
        family: str | None = None
        if provider_input:
            p_lower = provider_input.lower()
            if any(
                k in p_lower
                for k in (
                    "animeflv",
                    "animeav1",
                    "tioanime",
                    "animejara",
                    "henaojara",
                    "jkanime",
                )
            ):
                family = "anime"
            elif "serieskao" in p_lower:
                family = "serieskao"
            elif "allcalidad" in p_lower:
                family = "allcalidad"
            elif "poseidonhd2" in p_lower:
                family = "poseidonhd2"
            elif "gnula" in p_lower:
                family = "gnula"

        normalized = cls.normalize(raw.replace("-", " "))

        parsed_year: int | None = None
        if year is not None:
            match = YEAR_REGEX.search(str(year))
            if match:
                parsed_year = int(match.group(0))
        if parsed_year is None:
            match = YEAR_REGEX.search(raw) or YEAR_REGEX.search(normalized)
            if match:
                parsed_year = int(match.group(0))

        without_year = YEAR_REGEX.sub(" ", normalized)
        without_year = WHITESPACE_COLLAPSE_REGEX.sub(" ", without_year).strip()

        words = [w for w in without_year.split() if w and w not in PARSE_NOISE_WORDS]
        cleaned = " ".join(words)
        cleaned = TRAILING_ARTICLES_REGEX.sub("", cleaned).strip()

        base = cleaned
        seasons: list[int] = []
        raw_stripped = cls.strip_diacritics(raw)

        for m in SEASON_NUM_REGEX.finditer(raw):
            seasons.append(int(m.group(1)))
            base = SEASON_NUM_REGEX.sub(" ", base)
        for m in SEASON_ORD_REGEX.finditer(raw):
            seasons.append(int(m.group(1)))
            base = SEASON_ORD_REGEX.sub(" ", base)
        for m in SEASON_SHORT_REGEX.finditer(raw):
            seasons.append(int(m.group(1)))
            base = SEASON_SHORT_REGEX.sub(" ", base)
        for pattern, num in ORDINAL_SEASON_PATTERNS:
            if pattern.search(raw) or pattern.search(raw_stripped):
                seasons.append(num)
                base = pattern.sub(" ", base)

        base = cls.normalize(base)
        alias_titles = ALIASES.get(base, [])

        search_titles_candidates = [cleaned, base, *alias_titles]
        search_titles: list[str] = []
        for t in search_titles_candidates:
            norm_t = cls.normalize(t)
            if norm_t and norm_t not in search_titles:
                search_titles.append(norm_t)

        clean_raw = "".join(ch for ch in raw if ch.isprintable()).strip()
        clean_norm_title = cleaned or normalized or clean_raw or ""

        return ParsedTitle(
            raw_title=raw,
            provider_family=family,
            normalized_title=clean_norm_title,
            base_title=base,
            year=parsed_year,
            season_hints=sorted(list(set(seasons))),
            alias_titles=[cls.normalize(a) for a in alias_titles],
            search_titles=search_titles,
        )
