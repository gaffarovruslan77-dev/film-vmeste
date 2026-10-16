import random

from kinomatch.domain.recommender import GroupRecommender, Reason
from tests.factories import NOW, make_movie, make_room


def catalog() -> dict[int, object]:
    movies = [
        make_movie(1, "комедия", popularity=100),
        make_movie(2, "комедия", popularity=10),
        make_movie(3, "ужасы", popularity=90),
        make_movie(4, "ужасы", popularity=80),
        make_movie(5, "драма", popularity=70),
        make_movie(6, "комедия", "драма", popularity=20),
    ]
    return {m.id: m for m in movies}


def test_starts_with_popular_when_no_swipes() -> None:
    movies = catalog()
    room = make_room(1, 2, candidates=list(movies))
    rec = GroupRecommender().next_for(1, room, movies, random.Random(0))
    assert rec is not None
    assert rec.reason is Reason.POPULAR
    assert rec.movie.id == 1


def test_hot_movie_goes_first() -> None:
    movies = catalog()
    room = make_room(1, 2, candidates=list(movies))
    room.swipe(2, 5, liked=True, now=NOW)

    rec = GroupRecommender().next_for(1, room, movies, random.Random(0))

    assert rec is not None
    assert rec.movie.id == 5
    assert rec.reason is Reason.HOT
    assert rec.liked_by_others == 1


def test_vetoed_movie_is_never_shown() -> None:
    movies = catalog()
    room = make_room(1, 2, candidates=[1, 2])
    room.swipe(2, 1, liked=False, now=NOW)

    rec = GroupRecommender().next_for(1, room, movies, random.Random(0))

    assert rec is not None and rec.movie.id == 2


def test_learns_group_taste() -> None:
    movies = {
        m.id: m
        for m in [
            make_movie(1, "комедия"),
            make_movie(2, "комедия"),
            make_movie(3, "ужасы"),
            make_movie(4, "ужасы"),
            make_movie(5, "драма"),
            make_movie(6, "комедия", "драма"),
        ]
    }
    room = make_room(1, 2, candidates=list(movies), stop_on_match=False)
    # Обоим не нравятся ужасы, первому нравится комедия.
    room.swipe(1, 3, liked=False, now=NOW)
    room.swipe(2, 4, liked=False, now=NOW)
    room.swipe(1, 1, liked=True, now=NOW)
    room.swipe(2, 1, liked=True, now=NOW)

    rec = GroupRecommender().next_for(1, room, movies, random.Random(0))

    assert rec is not None
    assert "ужасы" not in rec.movie.genres
    assert "комедия" in rec.movie.genres


def test_returns_none_when_deck_is_exhausted() -> None:
    movies = catalog()
    room = make_room(1, 2, candidates=[1], stop_on_match=False)
    room.swipe(1, 1, liked=True, now=NOW)
    assert GroupRecommender().next_for(1, room, movies, random.Random(0)) is None
