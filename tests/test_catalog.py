from kinomatch.catalog.genres import canonical_genres
from kinomatch.catalog.wikidata import ANIMATED_FILM, FILM, parse


def row(film: str, kp: str, links: str, **extra: str) -> dict[str, dict[str, str]]:
    cells = {
        "film": f"http://www.wikidata.org/entity/{film}",
        "kp": kp,
        "title": film,
        "links": links,
    }
    cells |= extra
    return {key: {"value": value} for key, value in cells.items()}


def test_film_with_two_kinopoisk_ids_is_kept_once() -> None:
    items = parse([row("Q1", "500", "30"), row("Q1", "42", "30")])
    assert [(i.wikidata_id, i.kinopoisk_id) for i in items] == [("Q1", 42)]


def test_same_kinopoisk_id_keeps_more_popular_object() -> None:
    items = parse([row("Q1", "42", "10"), row("Q2", "42", "50")])
    assert [i.wikidata_id for i in items] == ["Q2"]


def test_bad_duration_is_dropped_and_animation_detected() -> None:
    [item] = parse([row("Q1", "1", "20", dur="9000.0", types=f"{FILM}|{ANIMATED_FILM}")])
    assert item.duration_min is None
    assert "мультфильм" in item.genres


def test_genre_normalization() -> None:
    labels = [
        "мелодрама",
        "комедия ужасов",
        "научно-фантастический фильм",
        "анимационно-игровой фильм",
    ]
    assert canonical_genres(labels) == ["комедия", "мелодрама", "ужасы", "фантастика"]
