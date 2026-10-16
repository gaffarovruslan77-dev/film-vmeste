"""Доменные модели. Room — агрегат: все изменения комнаты идут только через его методы,
поэтому бизнес-правила (кто может голосовать, когда считается совпадение) живут в одном месте."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime

from kinomatch.domain import errors
from kinomatch.domain.events import (
    DomainEvent,
    MatchFound,
    ParticipantJoined,
    ParticipantLeft,
    RoomFinished,
    VotingStarted,
)

KINOPOISK_FILM_URL = "https://www.kinopoisk.ru/film/{id}/"


class RoomStatus(enum.StrEnum):
    LOBBY = "lobby"  # собираем участников
    VOTING = "voting"  # свайпаем
    FINISHED = "finished"


@dataclass(frozen=True, slots=True)
class Movie:
    id: int
    title: str
    year: int | None
    genres: frozenset[str]
    popularity: int  # число статей о фильме в разных языковых Википедиях
    duration_min: int | None = None
    kinopoisk_id: int | None = None
    wikidata_id: str | None = None
    description: str | None = None

    @property
    def kinopoisk_url(self) -> str | None:
        if self.kinopoisk_id is None:
            return None
        return KINOPOISK_FILM_URL.format(id=self.kinopoisk_id)


@dataclass(frozen=True, slots=True)
class RoomFilters:
    genres: frozenset[str] = frozenset()  # фильм подходит, если есть хотя бы один из жанров
    year_from: int | None = None
    year_to: int | None = None
    max_duration: int | None = None

    def accepts(self, movie: Movie) -> bool:
        if self.genres and not (self.genres & movie.genres):
            return False
        if self.year_from is not None and (movie.year is None or movie.year < self.year_from):
            return False
        if self.year_to is not None and (movie.year is None or movie.year > self.year_to):
            return False
        return not (
            self.max_duration is not None
            and movie.duration_min is not None
            and movie.duration_min > self.max_duration
        )


@dataclass(slots=True)
class User:
    id: int
    display_name: str
    created_at: datetime
    provider: str | None = None  # "telegram", "alice", ... ; None — гость из веба
    external_id: str | None = None


@dataclass(slots=True)
class Participant:
    user_id: int
    display_name: str
    joined_at: datetime
    active: bool = True


@dataclass(frozen=True, slots=True)
class Swipe:
    user_id: int
    movie_id: int
    liked: bool
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Match:
    movie_id: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class MovieTally:
    movie_id: int
    likes: int
    dislikes: int


@dataclass(eq=False)
class Room:
    code: str
    owner_id: int
    filters: RoomFilters
    created_at: datetime
    stop_on_match: bool = True
    status: RoomStatus = RoomStatus.LOBBY
    candidate_ids: list[int] = field(default_factory=list)
    participants: dict[int, Participant] = field(default_factory=dict)
    swipes: dict[tuple[int, int], Swipe] = field(default_factory=dict)  # (user_id, movie_id)
    matches: dict[int, Match] = field(default_factory=dict)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    id: int | None = None
    _events: list[DomainEvent] = field(default_factory=list, init=False, repr=False)

    @classmethod
    def create(
        cls,
        *,
        code: str,
        owner: User,
        filters: RoomFilters,
        stop_on_match: bool,
        now: datetime,
    ) -> Room:
        room = cls(
            code=code,
            owner_id=owner.id,
            filters=filters,
            stop_on_match=stop_on_match,
            created_at=now,
        )
        room.participants[owner.id] = Participant(owner.id, owner.display_name, now)
        return room

    # ---------- запросы ----------

    @property
    def is_finished(self) -> bool:
        return self.status is RoomStatus.FINISHED

    @property
    def active_ids(self) -> frozenset[int]:
        return frozenset(p.user_id for p in self.participants.values() if p.active)

    def is_participant(self, user_id: int) -> bool:
        participant = self.participants.get(user_id)
        return participant is not None and participant.active

    def swiped_movie_ids(self, user_id: int) -> set[int]:
        return {movie_id for (uid, movie_id) in self.swipes if uid == user_id}

    def tally(self, movie_id: int) -> MovieTally:
        """Голоса за фильм. Учитываются только активные участники."""
        likes = dislikes = 0
        for uid in self.active_ids:
            swipe = self.swipes.get((uid, movie_id))
            if swipe is None:
                continue
            if swipe.liked:
                likes += 1
            else:
                dislikes += 1
        return MovieTally(movie_id, likes, dislikes)

    def leaderboard(self, limit: int = 5) -> list[MovieTally]:
        """«Почти совпадения»: фильмы, которые понравились большинству."""
        liked = {movie_id for (_, movie_id), s in self.swipes.items() if s.liked}
        tallies = [t for t in map(self.tally, liked) if t.likes > 0]
        tallies.sort(key=lambda t: (-t.likes, t.dislikes, t.movie_id))
        return tallies[:limit]

    def pull_events(self) -> list[DomainEvent]:
        events, self._events = self._events, []
        return events

    # ---------- команды ----------

    def join(self, user: User, now: datetime) -> None:
        self._ensure_not_finished()
        participant = self.participants.get(user.id)
        if participant is not None and participant.active:
            return  # повторный вход — не ошибка
        if participant is not None:
            participant.active = True
            participant.display_name = user.display_name
        else:
            self.participants[user.id] = Participant(user.id, user.display_name, now)
        self._events.append(ParticipantJoined(self.code, user.id, user.display_name))

    def leave(self, user_id: int, now: datetime) -> None:
        self._ensure_not_finished()
        participant = self._active_participant(user_id)
        participant.active = False
        self._events.append(ParticipantLeft(self.code, user_id, participant.display_name))

        active = [p for p in self.participants.values() if p.active]
        if not active:
            self._finish(now, reason="empty")
            return
        if user_id == self.owner_id:
            self.owner_id = min(active, key=lambda p: p.joined_at).user_id
        if self.status is RoomStatus.VOTING:
            # Ушёл человек, который ещё не проголосовал, — какие-то фильмы могли стать общими.
            liked = sorted({m for (_, m), s in self.swipes.items() if s.liked})
            for movie_id in liked:
                self._check_match(movie_id, now)
                if self.is_finished:
                    break

    def start(
        self,
        user_id: int,
        candidate_ids: list[int],
        *,
        min_participants: int,
        now: datetime,
    ) -> None:
        self._ensure_owner(user_id)
        if self.status is not RoomStatus.LOBBY:
            raise errors.InvalidRoomState("Голосование уже началось или закончилось")
        if len(self.active_ids) < min_participants:
            raise errors.NotEnoughParticipants(
                f"Нужно хотя бы {min_participants} участника, сейчас: {len(self.active_ids)}"
            )
        if not candidate_ids:
            raise errors.NoCandidates()
        self.candidate_ids = list(dict.fromkeys(candidate_ids))
        self.status = RoomStatus.VOTING
        self.started_at = now
        self._events.append(VotingStarted(self.code, len(self.candidate_ids)))

    def swipe(self, user_id: int, movie_id: int, liked: bool, now: datetime) -> Match | None:
        if self.status is not RoomStatus.VOTING:
            raise errors.InvalidRoomState("Голосование сейчас не идёт")
        self._active_participant(user_id)
        if movie_id not in self.candidate_ids:
            raise errors.MovieNotInRoom()
        key = (user_id, movie_id)
        if key in self.swipes:
            return None  # идемпотентность: двойной клик не меняет первый голос
        self.swipes[key] = Swipe(user_id, movie_id, liked, now)
        return self._check_match(movie_id, now) if liked else None

    def finish(self, user_id: int, now: datetime) -> None:
        self._ensure_owner(user_id)
        self._ensure_not_finished()
        self._finish(now, reason="owner")

    # ---------- внутреннее ----------

    def _check_match(self, movie_id: int, now: datetime) -> Match | None:
        if movie_id in self.matches or self.status is not RoomStatus.VOTING:
            return None
        active = self.active_ids
        if not active:
            return None
        for uid in active:
            swipe = self.swipes.get((uid, movie_id))
            if swipe is None or not swipe.liked:
                return None
        match = Match(movie_id, now)
        self.matches[movie_id] = match
        self._events.append(MatchFound(self.code, movie_id))
        if self.stop_on_match:
            self._finish(now, reason="match")
        return match

    def _finish(self, now: datetime, *, reason: str) -> None:
        self.status = RoomStatus.FINISHED
        self.finished_at = now
        self._events.append(RoomFinished(self.code, reason))

    def _ensure_owner(self, user_id: int) -> None:
        if user_id != self.owner_id:
            raise errors.NotRoomOwner()

    def _ensure_not_finished(self) -> None:
        if self.status is RoomStatus.FINISHED:
            raise errors.InvalidRoomState("Комната уже закрыта")

    def _active_participant(self, user_id: int) -> Participant:
        participant = self.participants.get(user_id)
        if participant is None or not participant.active:
            raise errors.NotParticipant()
        return participant
