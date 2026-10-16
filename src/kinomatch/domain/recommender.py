"""Выбор следующей карточки для участника.

Цель — как можно быстрее найти фильм, который понравится ВСЕМ. Для этого:

1. «Горячие» фильмы (их уже лайкнули другие и никто не дизлайкнул) показываем первыми:
   до совпадения им не хватает только голосов оставшихся участников.
2. Фильмы, которые кто-то из активных участников уже отверг, не показываем вовсе:
   единогласия по ним уже не будет.
3. Остальные ранжируем по вкусу группы. Вкус участника — сглаженная доля лайков по
   признакам фильма (жанры, десятилетие). Вкус группы — смесь среднего и минимума
   по участникам: минимум («least misery») не даёт выбрать фильм, который кому-то противен.
4. Добавляем популярность (её вес падает по мере накопления свайпов) и небольшой шум,
   чтобы исследовать новые жанры, а не крутиться вокруг первых лайков.
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from kinomatch.domain.models import Movie, Room

NEUTRAL = 0.5


class Reason(StrEnum):
    HOT = "hot"  # нравится другим участникам
    TASTE = "taste"  # подходит под вкус группы
    POPULAR = "popular"  # данных о вкусах ещё нет — начинаем с популярного


@dataclass(frozen=True, slots=True)
class RecommenderConfig:
    hot_bonus: float = 1.0
    least_misery_weight: float = 0.5
    popularity_weight: float = 0.3
    popularity_half_life: int = 10  # через сколько свайпов в комнате вес популярности падает вдвое
    exploration: float = 0.08
    prior_strength: float = 1.0  # псевдосчётчики сглаживания: (лайки + a) / (просмотры + 2a)


@dataclass(frozen=True, slots=True)
class Recommendation:
    movie: Movie
    reason: Reason
    score: float
    liked_by_others: int


def movie_features(movie: Movie) -> frozenset[str]:
    features = {f"genre:{g}" for g in movie.genres}
    if movie.year is not None:
        features.add(f"decade:{movie.year // 10 * 10}")
    return frozenset(features)


class GroupRecommender:
    def __init__(self, config: RecommenderConfig | None = None) -> None:
        self.config = config or RecommenderConfig()

    def next_for(
        self,
        user_id: int,
        room: Room,
        movies: Mapping[int, Movie],
        rng: random.Random,
    ) -> Recommendation | None:
        cfg = self.config
        active = room.active_ids
        others = active - {user_id}
        seen = room.swiped_movie_ids(user_id)

        likes_by_others: dict[int, int] = defaultdict(int)
        vetoed: set[int] = set()
        for (uid, movie_id), swipe in room.swipes.items():
            if uid not in others:
                continue
            if swipe.liked:
                likes_by_others[movie_id] += 1
            else:
                vetoed.add(movie_id)

        taste = self._taste_profiles(room, movies, active)
        max_pop = max((m.popularity for m in movies.values()), default=1)
        pop_weight = cfg.popularity_weight * 0.5 ** (len(room.swipes) / cfg.popularity_half_life)

        best: Recommendation | None = None
        for movie_id in room.candidate_ids:
            if movie_id in seen or movie_id in vetoed or movie_id in room.matches:
                continue
            movie = movies.get(movie_id)
            if movie is None:
                continue

            group_taste = self._group_score(movie, taste)
            popularity = math.log1p(movie.popularity) / math.log1p(max_pop)
            score = group_taste + pop_weight * popularity + rng.uniform(0, cfg.exploration)

            liked = likes_by_others.get(movie_id, 0)
            if liked and others:
                score += cfg.hot_bonus * (1 + liked / len(others))
                reason = Reason.HOT
            elif taste:
                reason = Reason.TASTE
            else:
                reason = Reason.POPULAR

            if best is None or score > best.score:
                best = Recommendation(movie, reason, score, liked)
        return best

    def _taste_profiles(
        self,
        room: Room,
        movies: Mapping[int, Movie],
        active: frozenset[int],
    ) -> dict[int, dict[str, float]]:
        """user_id -> {признак -> сглаженная доля лайков}. Только для тех, кто уже свайпал."""
        a = self.config.prior_strength
        likes: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        seen: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for (uid, movie_id), swipe in room.swipes.items():
            movie = movies.get(movie_id)
            if uid not in active or movie is None:
                continue
            for feature in movie_features(movie):
                seen[uid][feature] += 1
                if swipe.liked:
                    likes[uid][feature] += 1
        return {
            uid: {f: (likes[uid][f] + a) / (n + 2 * a) for f, n in counts.items()}
            for uid, counts in seen.items()
        }

    def _group_score(self, movie: Movie, taste: dict[int, dict[str, float]]) -> float:
        if not taste:
            return NEUTRAL
        features = movie_features(movie)
        scores = []
        for profile in taste.values():
            if features:
                scores.append(sum(profile.get(f, NEUTRAL) for f in features) / len(features))
            else:
                scores.append(NEUTRAL)
        lm = self.config.least_misery_weight
        return (1 - lm) * (sum(scores) / len(scores)) + lm * min(scores)
