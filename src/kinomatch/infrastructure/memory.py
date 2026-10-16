"""In-memory реализация портов. Используется в тестах и для быстрого локального запуска
без PostgreSQL. Повторяет семантику транзакций: изменения видны другим только после commit,
а get(for_update=True) держит блокировку комнаты до конца единицы работы."""

from __future__ import annotations

import asyncio
import copy
import functools
import itertools
from collections import Counter, defaultdict
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from types import TracebackType
from typing import Any, Self

from kinomatch.domain.events import DomainEvent
from kinomatch.domain.models import Movie, Room, RoomFilters, User


@dataclass
class StoredEvent:
    id: int
    type: str
    payload: dict[str, Any]
    created_at: datetime


@dataclass
class InMemoryStore:
    movies: dict[int, Movie] = field(default_factory=dict)
    rooms: dict[str, Room] = field(default_factory=dict)
    users: dict[int, User] = field(default_factory=dict)
    token_hashes: dict[str, int] = field(default_factory=dict)
    events: list[StoredEvent] = field(default_factory=list)
    room_locks: defaultdict[str, asyncio.Lock] = field(
        default_factory=lambda: defaultdict(asyncio.Lock)
    )
    _ids: itertools.count[int] = field(default_factory=lambda: itertools.count(1))

    def next_id(self) -> int:
        return next(self._ids)


class _Movies:
    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    async def get(self, movie_id: int) -> Movie | None:
        return self._store.movies.get(movie_id)

    async def get_many(self, ids: Collection[int]) -> dict[int, Movie]:
        return {i: self._store.movies[i] for i in ids if i in self._store.movies}

    async def find_candidate_ids(self, filters: RoomFilters, limit: int) -> list[int]:
        found = [m for m in self._store.movies.values() if filters.accepts(m)]
        found.sort(key=lambda m: (-m.popularity, m.id))
        return [m.id for m in found[:limit]]

    async def genre_counts(self) -> list[tuple[str, int]]:
        counts = Counter(g for m in self._store.movies.values() for g in m.genres)
        return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))

    async def upsert_many(self, movies: Sequence[Movie]) -> int:
        by_wikidata = {m.wikidata_id: m.id for m in self._store.movies.values()}
        for movie in movies:
            movie_id = by_wikidata.get(movie.wikidata_id) or self._store.next_id()
            self._store.movies[movie_id] = replace(movie, id=movie_id)
        return len(movies)


class _Rooms:
    def __init__(self, uow: InMemoryUnitOfWork) -> None:
        self._uow = uow
        self._store = uow.store

    async def add(self, room: Room) -> None:
        room.id = self._store.next_id()
        await self.save(room)

    async def get(self, code: str, *, for_update: bool = False) -> Room | None:
        if for_update:
            await self._uow.lock_room(code)
        room = self._store.rooms.get(code)
        return copy.deepcopy(room) if room is not None else None

    async def save(self, room: Room) -> None:
        snapshot = copy.deepcopy(room)
        snapshot.pull_events()  # события уходят в outbox, в хранимом состоянии их быть не должно
        self._uow.stage(lambda: self._store.rooms.__setitem__(room.code, snapshot))

    async def exists(self, code: str) -> bool:
        return code in self._store.rooms


class _Users:
    def __init__(self, uow: InMemoryUnitOfWork) -> None:
        self._uow = uow
        self._store = uow.store

    async def add(self, *, display_name: str, token_hash: str | None, now: datetime) -> User:
        user = User(id=self._store.next_id(), display_name=display_name, created_at=now)

        def apply() -> None:
            self._store.users[user.id] = user
            if token_hash:
                self._store.token_hashes[token_hash] = user.id

        self._uow.stage(apply)
        return user

    async def by_token_hash(self, token_hash: str) -> User | None:
        user_id = self._store.token_hashes.get(token_hash)
        return self._store.users.get(user_id) if user_id is not None else None

    async def get_or_create_external(
        self, *, provider: str, external_id: str, display_name: str, now: datetime
    ) -> User:
        for user in self._store.users.values():
            if user.provider == provider and user.external_id == external_id:
                if user.display_name != display_name:
                    updated = replace(user, display_name=display_name)
                    self._uow.stage(
                        functools.partial(self._store.users.__setitem__, updated.id, updated)
                    )
                    return updated
                return user
        user = User(
            id=self._store.next_id(),
            display_name=display_name,
            created_at=now,
            provider=provider,
            external_id=external_id,
        )
        self._uow.stage(lambda: self._store.users.__setitem__(user.id, user))
        return user

    async def get_many(self, ids: Collection[int]) -> dict[int, User]:
        return {i: self._store.users[i] for i in ids if i in self._store.users}


class _Outbox:
    def __init__(self, uow: InMemoryUnitOfWork) -> None:
        self._uow = uow
        self._store = uow.store

    async def put(self, events: Sequence[DomainEvent], now: datetime) -> None:
        for event in events:
            stored = StoredEvent(self._store.next_id(), event.type, event.payload(), now)
            self._uow.stage(functools.partial(self._store.events.append, stored))


class InMemoryUnitOfWork:
    def __init__(self, store: InMemoryStore) -> None:
        self.store = store
        self.movies = _Movies(store)
        self.rooms = _Rooms(self)
        self.users = _Users(self)
        self.outbox = _Outbox(self)
        self._staged: list[Callable[[], None]] = []
        self._locks: list[asyncio.Lock] = []

    def stage(self, op: Callable[[], None]) -> None:
        self._staged.append(op)

    async def lock_room(self, code: str) -> None:
        lock = self.store.room_locks[code]
        if lock not in self._locks:
            await lock.acquire()
            self._locks.append(lock)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._staged.clear()  # не закоммитили — откат
        for lock in self._locks:
            lock.release()
        self._locks.clear()

    async def commit(self) -> None:
        for op in self._staged:
            op()
        self._staged.clear()


def in_memory_uow_factory(store: InMemoryStore) -> Callable[[], InMemoryUnitOfWork]:
    return lambda: InMemoryUnitOfWork(store)
