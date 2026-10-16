"""Симуляция: сколько свайпов нужно компании, чтобы найти общий фильм.

У каждого виртуального участника есть скрытый вкус (любимые жанры и «не старше N года»).
Сравниваем три стратегии показа карточек:
  * random  — случайный порядок;
  * popular — сначала самые популярные;
  * smart   — GroupRecommender («горячие» фильмы + обучение на вкусе группы).

Запуск:  python scripts/simulate.py --rooms 300
"""

from __future__ import annotations

import argparse
import random
import statistics
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kinomatch.catalog.wikidata import load
from kinomatch.domain.models import Movie, Room, RoomFilters, User
from kinomatch.domain.recommender import GroupRecommender

NOW = datetime(2026, 1, 1, tzinfo=UTC)
TOP_GENRES = [
    "драма", "комедия", "боевик", "мелодрама", "триллер", "криминал",
    "ужасы", "приключения", "фантастика", "фэнтези", "детектив", "мультфильм",
]  # fmt: skip


@dataclass(frozen=True)
class Taste:
    genres: frozenset[str]
    min_year: int

    def likes(self, movie: Movie) -> bool:
        return bool(self.genres & movie.genres) and (movie.year or 0) >= self.min_year


Picker = Callable[[int, Room, dict[int, Movie], random.Random], Movie | None]


def random_picker(
    user_id: int, room: Room, movies: dict[int, Movie], rng: random.Random
) -> Movie | None:
    seen = room.swiped_movie_ids(user_id)
    left = [m for m in room.candidate_ids if m not in seen]
    return movies[rng.choice(left)] if left else None


def popular_picker(
    user_id: int, room: Room, movies: dict[int, Movie], rng: random.Random
) -> Movie | None:
    seen = room.swiped_movie_ids(user_id)
    left = [movies[m] for m in room.candidate_ids if m not in seen]
    return max(left, key=lambda m: m.popularity) if left else None


def smart_picker() -> Picker:
    recommender = GroupRecommender()

    def pick(
        user_id: int, room: Room, movies: dict[int, Movie], rng: random.Random
    ) -> Movie | None:
        rec = recommender.next_for(user_id, room, movies, rng)
        return rec.movie if rec else None

    return pick


def play(
    picker: Picker, tastes: list[Taste], pool: list[int], movies: dict[int, Movie], seed: int
) -> int | None:
    """Возвращает число свайпов до первого совпадения или None, если не нашли."""
    rng = random.Random(seed)
    users = [User(i + 1, f"u{i + 1}", NOW) for i in range(len(tastes))]
    room = Room.create(
        code="SIM", owner=users[0], filters=RoomFilters(), stop_on_match=True, now=NOW
    )
    for user in users[1:]:
        room.join(user, NOW)
    room.start(users[0].id, pool, min_participants=1, now=NOW)
    swipes = 0
    while not room.is_finished:
        progressed = False
        for user, taste in zip(users, tastes, strict=True):
            movie = picker(user.id, room, movies, rng)
            if movie is None:
                continue
            progressed = True
            swipes += 1
            room.swipe(user.id, movie.id, taste.likes(movie), NOW)
            if room.is_finished:
                return swipes
        if not progressed:
            return None
    return swipes


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rooms", type=int, default=300)
    parser.add_argument("--catalog", type=Path, default=Path("data/movies.json"))
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()

    catalog = [item.to_movie() for item in load(args.catalog)]
    movies = {
        i: Movie(i, m.title, m.year, m.genres, m.popularity, m.duration_min)
        for i, m in enumerate(catalog)
    }
    popular_ids = sorted(movies, key=lambda i: -movies[i].popularity)[:600]

    rng = random.Random(args.seed)
    strategies: dict[str, Picker] = {
        "random": random_picker,
        "popular": popular_picker,
        "smart": smart_picker(),
    }
    results: dict[str, list[int | None]] = {name: [] for name in strategies}
    for room_no in range(args.rooms):
        group_size = rng.randint(2, 4)
        tastes = [
            Taste(
                frozenset(rng.sample(TOP_GENRES, rng.randint(2, 4))),
                rng.choice([0, 1990, 2000, 2010]),
            )
            for _ in range(group_size)
        ]
        pool = rng.sample(popular_ids, 200)
        for name, picker in strategies.items():
            results[name].append(play(picker, tastes, pool, movies, seed=room_no))

    print(f"Комнат: {args.rooms}, участников 2–4, в колоде 200 фильмов\n")
    print(f"{'стратегия':<10}{'нашли':>8}{'медиана свайпов':>18}{'среднее':>10}")
    for name, values in results.items():
        found = [v for v in values if v is not None]
        rate = len(found) / len(values)
        print(
            f"{name:<10}{rate:>8.0%}{statistics.median(found):>18.0f}{statistics.mean(found):>10.1f}"
        )


if __name__ == "__main__":
    main()
