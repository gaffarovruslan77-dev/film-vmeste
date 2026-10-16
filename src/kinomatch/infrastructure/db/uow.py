from types import TracebackType
from typing import Self

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kinomatch.infrastructure.db.repositories import (
    SqlMovieRepository,
    SqlOutbox,
    SqlRoomRepository,
    SqlUserRepository,
)


class SqlUnitOfWork:
    """Одна сессия = одна транзакция. Без commit() всё откатывается при выходе."""

    movies: SqlMovieRepository
    rooms: SqlRoomRepository
    users: SqlUserRepository
    outbox: SqlOutbox

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    async def __aenter__(self) -> Self:
        self.session = self._factory()
        self.movies = SqlMovieRepository(self.session)
        self.rooms = SqlRoomRepository(self.session)
        self.users = SqlUserRepository(self.session)
        self.outbox = SqlOutbox(self.session)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.session.close()  # незакоммиченное откатывается, блокировки снимаются

    async def commit(self) -> None:
        await self.session.commit()
