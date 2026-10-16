"""Сценарии использования. Каждый метод — одна транзакция: загрузить агрегат, вызвать
доменный метод, сохранить его и записать события в outbox."""

from __future__ import annotations

import hashlib
import random
import secrets
from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import UTC, datetime

from kinomatch.application.ports import UnitOfWork
from kinomatch.domain import errors
from kinomatch.domain.models import Movie, MovieTally, Room, RoomFilters, RoomStatus, User
from kinomatch.domain.recommender import GroupRecommender, Recommendation

UowFactory = Callable[[], UnitOfWork]
Clock = Callable[[], datetime]

ROOM_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # без 0/O и 1/I — чтобы не путать
ROOM_CODE_LENGTH = 6


def utcnow() -> datetime:
    return datetime.now(UTC)


def generate_room_code() -> str:
    return "".join(secrets.choice(ROOM_CODE_ALPHABET) for _ in range(ROOM_CODE_LENGTH))


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class RoomSettings:
    min_participants: int = 2
    pool_size: int = 200  # сколько фильмов в колоде комнаты
    pool_oversample: int = 3  # берём в 3 раза больше популярных и случайно выбираем pool_size


@dataclass(frozen=True, slots=True)
class SwipeOutcome:
    match: Movie | None
    status: RoomStatus


@dataclass(frozen=True, slots=True)
class RankedMovie:
    movie: Movie
    tally: MovieTally


@dataclass(frozen=True, slots=True)
class RoomResults:
    status: RoomStatus
    matches: list[Movie]
    top: list[RankedMovie]


class RoomService:
    def __init__(
        self,
        uow_factory: UowFactory,
        recommender: GroupRecommender | None = None,
        settings: RoomSettings | None = None,
        *,
        clock: Clock = utcnow,
        code_factory: Callable[[], str] = generate_room_code,
        rng: random.Random | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._recommender = recommender or GroupRecommender()
        self._settings = settings or RoomSettings()
        self._clock = clock
        self._code_factory = code_factory
        self._rng = rng or random.Random()
        # Каталог фильмов во время работы не меняется — держим его в памяти процесса.
        self._movie_cache: dict[int, Movie] = {}

    async def create_room(
        self,
        user: User,
        filters: RoomFilters,
        *,
        stop_on_match: bool = True,
    ) -> Room:
        async with self._uow_factory() as uow:
            if not await uow.movies.find_candidate_ids(filters, limit=1):
                raise errors.NoCandidates()
            code = await self._unique_code(uow)
            room = Room.create(
                code=code,
                owner=user,
                filters=filters,
                stop_on_match=stop_on_match,
                now=self._clock(),
            )
            await uow.rooms.add(room)
            await uow.outbox.put(room.pull_events(), self._clock())
            await uow.commit()
            return room

    async def get_room(self, code: str) -> Room:
        async with self._uow_factory() as uow:
            return await self._load(uow, code)

    async def join(self, code: str, user: User) -> Room:
        async with self._uow_factory() as uow:
            room = await self._load(uow, code, for_update=True)
            room.join(user, self._clock())
            await self._save(uow, room)
            return room

    async def leave(self, code: str, user: User) -> Room:
        async with self._uow_factory() as uow:
            room = await self._load(uow, code, for_update=True)
            room.leave(user.id, self._clock())
            await self._save(uow, room)
            return room

    async def start(self, code: str, user: User) -> Room:
        cfg = self._settings
        async with self._uow_factory() as uow:
            room = await self._load(uow, code, for_update=True)
            if room.owner_id != user.id:
                raise errors.NotRoomOwner()
            ids = await uow.movies.find_candidate_ids(
                room.filters, limit=cfg.pool_size * cfg.pool_oversample
            )
            pool = ids if len(ids) <= cfg.pool_size else self._rng.sample(ids, cfg.pool_size)
            room.start(user.id, pool, min_participants=cfg.min_participants, now=self._clock())
            await self._save(uow, room)
            return room

    async def finish(self, code: str, user: User) -> Room:
        async with self._uow_factory() as uow:
            room = await self._load(uow, code, for_update=True)
            room.finish(user.id, self._clock())
            await self._save(uow, room)
            return room

    async def next_card(self, code: str, user: User) -> Recommendation | None:
        """Следующий фильм для участника. None — колода закончилась."""
        async with self._uow_factory() as uow:
            room = await self._load(uow, code)
            if not room.is_participant(user.id):
                raise errors.NotParticipant()
            if room.status is RoomStatus.LOBBY:
                raise errors.InvalidRoomState("Голосование ещё не началось")
            if room.status is RoomStatus.FINISHED:
                raise errors.InvalidRoomState("Голосование уже закончилось")
            movies = await self._movies(uow, room.candidate_ids)
        # Сид зависит от числа свайпов пользователя: пока он не проголосовал,
        # повторный запрос вернёт ту же карточку (если другие не изменили картину).
        seen = len(room.swiped_movie_ids(user.id))
        rng = random.Random(f"{room.code}:{user.id}:{seen}")
        return self._recommender.next_for(user.id, room, movies, rng)

    async def swipe(self, code: str, user: User, movie_id: int, liked: bool) -> SwipeOutcome:
        async with self._uow_factory() as uow:
            # Блокировка комнаты: два последних лайка, пришедшие одновременно, не «потеряют»
            # совпадение — второй увидит голос первого.
            room = await self._load(uow, code, for_update=True)
            match = room.swipe(user.id, movie_id, liked, self._clock())
            await self._save(uow, room)
            movie = None
            if match is not None:
                movie = (await self._movies(uow, [match.movie_id]))[match.movie_id]
            return SwipeOutcome(match=movie, status=room.status)

    async def results(self, code: str, limit: int = 5) -> RoomResults:
        async with self._uow_factory() as uow:
            room = await self._load(uow, code)
            top = room.leaderboard(limit)
            ids = [*room.matches, *(t.movie_id for t in top)]
            movies = await self._movies(uow, ids)
        return RoomResults(
            status=room.status,
            matches=[movies[m] for m in room.matches if m in movies],
            top=[RankedMovie(movies[t.movie_id], t) for t in top if t.movie_id in movies],
        )

    async def movie(self, movie_id: int) -> Movie:
        async with self._uow_factory() as uow:
            movies = await self._movies(uow, [movie_id])
        if movie_id not in movies:
            raise errors.MovieNotFound()
        return movies[movie_id]

    async def genres(self) -> list[tuple[str, int]]:
        async with self._uow_factory() as uow:
            return await uow.movies.genre_counts()

    async def participants(self, code: str) -> tuple[Room, dict[int, User]]:
        """Для доверенных сервисов (бот): участники вместе с внешними id."""
        async with self._uow_factory() as uow:
            room = await self._load(uow, code)
            users = await uow.users.get_many(list(room.participants))
            return room, users

    # ---------- helpers ----------

    async def _load(self, uow: UnitOfWork, code: str, *, for_update: bool = False) -> Room:
        room = await uow.rooms.get(code.upper(), for_update=for_update)
        if room is None:
            raise errors.RoomNotFound()
        return room

    async def _save(self, uow: UnitOfWork, room: Room) -> None:
        await uow.rooms.save(room)
        await uow.outbox.put(room.pull_events(), self._clock())
        await uow.commit()

    async def _unique_code(self, uow: UnitOfWork) -> str:
        for _ in range(10):
            code = self._code_factory()
            if not await uow.rooms.exists(code):
                return code
        raise RuntimeError("Не удалось сгенерировать уникальный код комнаты")

    async def _movies(self, uow: UnitOfWork, ids: Collection[int]) -> dict[int, Movie]:
        missing = [i for i in ids if i not in self._movie_cache]
        if missing:
            self._movie_cache.update(await uow.movies.get_many(missing))
        return {i: self._movie_cache[i] for i in ids if i in self._movie_cache}


class AuthService:
    def __init__(self, uow_factory: UowFactory, *, clock: Clock = utcnow) -> None:
        self._uow_factory = uow_factory
        self._clock = clock

    async def register_guest(self, display_name: str) -> tuple[User, str]:
        """Гость (веб-клиент): получает случайный токен, в БД хранится только его хэш."""
        token = secrets.token_urlsafe(32)
        async with self._uow_factory() as uow:
            user = await uow.users.add(
                display_name=display_name, token_hash=hash_token(token), now=self._clock()
            )
            await uow.commit()
        return user, token

    async def authenticate(self, token: str) -> User:
        async with self._uow_factory() as uow:
            user = await uow.users.by_token_hash(hash_token(token))
        if user is None:
            raise errors.Unauthorized("Неверный токен")
        return user

    async def resolve_external(self, provider: str, external_id: str, display_name: str) -> User:
        """Пользователь доверенного клиента (Telegram, Алиса): находим или создаём."""
        async with self._uow_factory() as uow:
            user = await uow.users.get_or_create_external(
                provider=provider,
                external_id=external_id,
                display_name=display_name,
                now=self._clock(),
            )
            await uow.commit()
            return user
