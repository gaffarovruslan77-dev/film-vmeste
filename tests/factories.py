from datetime import UTC, datetime

from kinomatch.domain.models import Movie, Room, RoomFilters, User

NOW = datetime(2026, 10, 16, 20, 0, tzinfo=UTC)


def make_user(user_id: int, name: str | None = None) -> User:
    return User(id=user_id, display_name=name or f"user{user_id}", created_at=NOW)


def make_movie(
    movie_id: int,
    *genres: str,
    year: int = 2000,
    popularity: int = 50,
    duration: int | None = 120,
) -> Movie:
    return Movie(
        id=movie_id,
        title=f"Фильм {movie_id}",
        year=year,
        genres=frozenset(genres),
        popularity=popularity,
        duration_min=duration,
        kinopoisk_id=movie_id * 10,
        wikidata_id=f"Q{movie_id}",
    )


def make_room(
    *user_ids: int, candidates: list[int] | None = None, stop_on_match: bool = True
) -> Room:
    owner, *others = user_ids
    room = Room.create(
        code="ROOM42",
        owner=make_user(owner),
        filters=RoomFilters(),
        stop_on_match=stop_on_match,
        now=NOW,
    )
    for uid in others:
        room.join(make_user(uid), NOW)
    if candidates is not None:
        room.start(owner, candidates, min_participants=1, now=NOW)
    room.pull_events()
    return room
