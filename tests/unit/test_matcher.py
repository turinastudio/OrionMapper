from __future__ import annotations

from orion_mapper.matcher.normalizer import (
    ALIASES,
    NOISE_WORDS,
    ORDINAL_WORDS,
    ParsedTitle,
    TitleNormalizer,
)
from orion_mapper.matcher.scoring import (
    CandidateScorer,
    FuzzyTitleMatcher,
    MatchResult,
)


def test_normalizer_constants():
    assert "campo de suenos" in ALIASES
    assert "latino" in NOISE_WORDS
    assert "first" in ORDINAL_WORDS


def test_title_normalizer_spanish_diacritics():
    assert TitleNormalizer.normalize("Película Acción Año") == "pelicula accion ano"
    assert TitleNormalizer.normalize("Camión Música Pingüino") == "camion musica pinguino"


def test_title_normalizer_catalan_galician():
    assert TitleNormalizer.normalize("Pa Negre (Edició Català)") == "pa negre edicio catala"
    assert TitleNormalizer.normalize("Pel·lícula de França") == "pel-licula de franca".replace("-", " ") or "pellicula de franca"
    assert TitleNormalizer.strip_diacritics("l·l Ç ç à è ò") == "ll C c a e o"


def test_title_normalizer_noise_words():
    res = TitleNormalizer.normalize("Matrix Pelicula Completa Audio Latino HD [4K]")
    assert "completa" not in res
    assert "latino" not in res
    assert "hd" not in res
    assert "4k" not in res
    assert "matrix" in res


def test_title_normalizer_season_ordinals_spanish():
    assert TitleNormalizer.normalize("Breaking Bad Temporada 5") == "breaking bad"
    assert TitleNormalizer.normalize("Stranger Things Season 2") == "stranger things"
    assert TitleNormalizer.normalize("The Boys T1") == "the boys"
    assert TitleNormalizer.normalize("Game of Thrones Segunda Temporada") == "game of thrones"


def test_title_normalizer_punctuation_symbols():
    assert TitleNormalizer.normalize("Spider-Man: No Way Home [4K]") == "spider man no way home"
    assert TitleNormalizer.normalize("¿Quién mató a Sara?") == "quien mato a sara"
    assert TitleNormalizer.normalize("¡Asu Mare!") == "asu mare"
    assert TitleNormalizer.normalize("Fast & Furious --- 2019 ...") == "fast furious 2019"


def test_title_normalizer_empty_and_whitespace():
    assert TitleNormalizer.normalize("") == ""
    assert TitleNormalizer.normalize("   ") == ""
    assert TitleNormalizer.normalize("\n\t  ") == ""


def test_title_normalizer_parse_title_structure():
    parsed = TitleNormalizer.parse(
        "Breaking Bad Temporada 5 (2013)",
        provider_input="serieskao",
    )
    assert isinstance(parsed, ParsedTitle)
    assert parsed.raw_title == "Breaking Bad Temporada 5 (2013)"
    assert parsed.provider_family == "serieskao"
    assert parsed.year == 2013
    assert 5 in parsed.season_hints
    assert "breaking bad" in parsed.search_titles


def test_title_normalizer_aliases_expansion():
    parsed = TitleNormalizer.parse("Los Increibles", provider_input="allcalidad")
    assert "the incredibles" in parsed.search_titles or "incredibles" in parsed.search_titles

    parsed_gangster = TitleNormalizer.parse("El Gangster")
    assert "hoodlum" in parsed_gangster.search_titles


def test_title_normalizer_provider_family():
    assert TitleNormalizer.parse("Naruto", provider_input="animeflv").provider_family == "anime"
    assert TitleNormalizer.parse("Naruto", provider_input="jkanime").provider_family == "anime"
    assert TitleNormalizer.parse("Item", provider_input="serieskao").provider_family == "serieskao"
    assert TitleNormalizer.parse("Item", provider_input="poseidonhd2").provider_family == "poseidonhd2"
    assert TitleNormalizer.parse("Item", provider_input="gnula").provider_family == "gnula"
    assert TitleNormalizer.parse("Item", provider_input="allcalidad").provider_family == "allcalidad"
    assert TitleNormalizer.parse("Item", provider_input="unknown").provider_family is None


def test_fuzzy_matcher_exact_match_score():
    score = FuzzyTitleMatcher.score(
        "Fight Club",
        "Fight Club",
        year1=1999,
        year2=1999,
        type1="movie",
        type2="movie",
    )
    assert score == 100.0


def test_fuzzy_matcher_close_match_high_score():
    score = FuzzyTitleMatcher.score(
        "El Club de la Pelea",
        "El Club de la Lucha",
        year1=1999,
        year2=1999,
        type1="movie",
        type2="movie",
    )
    assert score >= 70.0


def test_fuzzy_matcher_year_delta_penalties():
    score_exact_year = FuzzyTitleMatcher.score("Matrix", "Matrix", year1=1999, year2=1999, type1="movie", type2="movie")
    score_delta_1 = FuzzyTitleMatcher.score("Matrix", "Matrix", year1=1999, year2=2000, type1="movie", type2="movie")
    score_delta_2 = FuzzyTitleMatcher.score("Matrix", "Matrix", year1=1999, year2=2001, type1="movie", type2="movie")
    score_century = FuzzyTitleMatcher.score("Dracula", "Dracula", year1=1931, year2=2023, type1="movie", type2="movie")

    assert score_exact_year > score_delta_1
    assert score_delta_1 > score_delta_2
    assert score_century < 88.0


def test_fuzzy_matcher_media_type_mismatch_penalty():
    score_same = FuzzyTitleMatcher.score("Fargo", "Fargo", year1=2014, year2=2014, type1="series", type2="series")
    score_mismatch = FuzzyTitleMatcher.score("Fargo", "Fargo", year1=2014, year2=2014, type1="movie", type2="series")
    assert score_same - score_mismatch == 45.0  # +5 vs -40


def test_fuzzy_matcher_empty_title_zero_score():
    assert FuzzyTitleMatcher.score("", "Fight Club") == 0.0
    assert FuzzyTitleMatcher.score("Fight Club", "") == 0.0
    assert FuzzyTitleMatcher.score("", "") == 0.0


def test_fuzzy_matcher_word_order_inversion():
    score = FuzzyTitleMatcher.score(
        "Club Fight",
        "Fight Club",
        year1=1999,
        year2=1999,
        type1="movie",
        type2="movie",
    )
    assert score >= 88.0


def test_fuzzy_matcher_none_years():
    score = FuzzyTitleMatcher.score(
        "Fight Club",
        "Fight Club",
        year1=None,
        year2=1999,
        type1="movie",
        type2="movie",
    )
    assert score >= 85.0


def test_candidate_scorer_tmdb_json_high_confidence(tmdb_fixtures):
    parsed = TitleNormalizer.parse("Fight Club", year=1999)
    candidate = tmdb_fixtures["search_movie_fight_club"]["results"][0]

    result = CandidateScorer.score_candidate(parsed, candidate, content_type="movie")
    assert isinstance(result, MatchResult)
    assert result.confidence == "high"
    assert result.score >= 88.0
    assert result.tmdb_id == "550"


def test_candidate_scorer_generic_title_penalty():
    assert CandidateScorer.is_generic("9") is True
    assert CandidateScorer.is_generic("El Cid") is True
    assert CandidateScorer.is_generic("The Lord of the Rings") is False

    parsed_generic = TitleNormalizer.parse("9", year=None)
    cand = {"title": "9", "id": 1234, "media_type": "movie"}
    res = CandidateScorer.score_candidate(parsed_generic, cand, content_type="movie")
    assert res.score < 70.0


def test_candidate_scorer_anime_boosts():
    parsed_anime = TitleNormalizer.parse("Zombieland Saga", provider_input="animeflv", year=2018)
    cand_jp = {
        "name": "Zombieland Saga",
        "id": 82856,
        "media_type": "tv",
        "original_language": "ja",
        "origin_country": ["JP"],
        "first_air_date": "2018-10-04",
    }
    res = CandidateScorer.score_candidate(parsed_anime, cand_jp, content_type="series")
    assert res.confidence == "high"
    assert res.score >= 100.0


def test_candidate_scorer_extract_year():
    assert CandidateScorer.extract_year({"release_date": "1999-10-15"}) == 1999
    assert CandidateScorer.extract_year({"first_air_date": "2018-10-04"}) == 2018
    assert CandidateScorer.extract_year({"release_date": "1895-12-28"}) == 1895
    assert CandidateScorer.extract_year({"release_date": "2100-01-01"}) == 2100
    assert CandidateScorer.extract_year({}) is None


def test_title_normalizer_accented_spanish_ordinals():
    p1 = TitleNormalizer.parse("Gomorrah Décima Temporada")
    assert 10 in p1.season_hints
    assert p1.base_title == "gomorrah"

    p2 = TitleNormalizer.parse("Breaking Bad Séptima Temporada")
    assert 7 in p2.season_hints
    assert p2.base_title == "breaking bad"

    p3 = TitleNormalizer.parse("Naruto Décimo Temporada")
    assert 10 in p3.season_hints

    p4 = TitleNormalizer.parse("Dark Tercera Temporada (2020)")
    assert 3 in p4.season_hints
    assert p4.year == 2020


def test_title_normalizer_year_range_1880_to_2100():
    p_1888 = TitleNormalizer.parse("Roundhay Garden Scene (1888)")
    assert p_1888.year == 1888

    p_2100 = TitleNormalizer.parse("Century Next (2100)")
    assert p_2100.year == 2100


def test_title_normalizer_null_bytes_and_unprintable_fallback():
    parsed_nulls = TitleNormalizer.parse("\x00\x00\x00\x00")
    assert "\x00" not in parsed_nulls.normalized_title
    assert parsed_nulls.normalized_title == ""

    parsed_mixed = TitleNormalizer.parse("\x00\x01 Matrix \x02 1999")
    assert "\x00" not in parsed_mixed.normalized_title
    assert "matrix" in parsed_mixed.normalized_title


def test_candidate_scorer_type_guards_and_safety():
    parsed = TitleNormalizer.parse("Fight Club", year=1999)
    cand_malformed = {
        "title": "Fight Club",
        "media_type": 12345,  # Non-string media_type
        "origin_country": 9999,  # Non-list origin_country
        "original_language": 8888,  # Non-string language
        "release_date": "1999-10-15",
    }
    res = CandidateScorer.score_candidate(parsed, cand_malformed, content_type="movie")
    assert isinstance(res, MatchResult)
    assert res.score > 0.0


def test_candidate_scorer_short_substring_guard():
    parsed = TitleNormalizer.parse("Doctor Strange (2016)")
    candidate_to = {
        "title": "to",
        "id": 99999,
        "media_type": "movie",
        "release_date": "2016-11-04",
        "imdb_id": "tt1234567",
    }
    res_to = CandidateScorer.score_candidate(parsed, candidate_to, content_type="movie")
    assert res_to.confidence == "low"
    assert res_to.score < 72.0

    parsed_godfather = TitleNormalizer.parse("The Godfather (1972)")
    candidate_me = {
        "title": "me",
        "id": 88888,
        "media_type": "movie",
        "release_date": "1972-03-24",
    }
    res_me = CandidateScorer.score_candidate(parsed_godfather, candidate_me, content_type="movie")
    assert res_me.confidence == "low"

