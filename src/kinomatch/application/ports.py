"""Порты — то, что ядру нужно от внешнего мира. Реализации лежат в infrastructure/:
PostgreSQL для продакшена и in-memory для тестов. Ядро не знает, какая из них подключена."""

from collections.abc import Collection, Sequence
from datetime import datetime
from types import TracebackType
from typing import Protocol, Self

from kinomatch.domain.events import DomainEvent
from kinomatch.domain.models import Movie, Room, RoomFilters, User


class MovieRepository(Protocol):
    async def get(self, movie_id: int) -> Movie | None: ...

    async def get_many(self, ids: Collection[int]) -> dict[int, Movie]: ...

    async def find_candidate_ids(self, filters: RoomFilters, limit: int) -> list[int]:
        """Id фильмов под фильтры, самые популярные первыми."""
        ...

    async def genre_counts(self) -> list[tuple[str, int]]: ...

    async def upsert_many(self, movies: Sequence[Movie]) -> int:
        """Загрузка каталога. Ключ — wikidata_id, поле id игнорируется."""
        ...


class RoomRepository(Protocol):
    async def add(self, room: Room) -> None: ...

    async def get(self, code: str, *, for_update: bool = False) -> Room | None:
        """for_update=True — эксклюзивная блокировка комнаты до конца транзакции."""
        ...

    async def save(self, room: Room) -> None: ...

    async def exists(self, code: str) -> bool: ...


class UserRepository(Protocol):
    async def add(
        self,
        *,
        display_name: str,
        token_hash: str | None,
        now: datetime,
    ) -> User: ...

    async def by_token_hash(self, token_hash: str) -> User | None: ...

    async def get_or_create_external(
        self,
        *,
        provider: str,
        external_id: str,
        display_name: str,
        now: datetime,
    ) -> User: ...

    async def get_many(self, ids: Collection[int]) -> dict[int, User]: ...


class Outbox(Protocol):
    async def put(self, events: Sequence[DomainEvent], now: datetime) -> None: ...


class UnitOfWork(Protocol):
    """Одна транзакция. Изменения комнаты и события для outbox коммитятся атомарно."""

    @property
    def movies(self) -> MovieRepository: ...

    @property
    def rooms(self) -> RoomRepository: ...

    @property
    def users(self) -> UserRepository: ...

    @property
    def outbox(self) -> Outbox: ...

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...

    async def commit(self) -> None: ...
