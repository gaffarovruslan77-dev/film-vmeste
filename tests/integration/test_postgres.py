"""Интеграционные тесты на настоящих PostgreSQL и Redis.

Запуск: docker compose up -d postgres redis, затем
  TEST_DATABASE_URL=postgresql+asyncpg://kinomatch:kinomatch@localhost:5432/kinomatch_test \
  TEST_REDIS_URL=redis://localhost:6379/15 pytest -m integration
"""

import asyncio
import os
from collections.abc import AsyncIterator

import pytest
from redis.asyncio import Redis
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from kinomatch.application.services import AuthService, RoomService, RoomSettings
from kinomatch.domain.models import RoomFilters, RoomStatus
from kinomatch.infrastructure.db.session import create_engine, session_factory
from kinomatch.infrastructure.db.tables import Base, OutboxRow
from kinomatch.infrastructure.db.uow import SqlUnitOfWork
from kinomatch.infrastructure.events import EVENTS_STREAM, RedisEventPublisher
from kinomatch.worker import relay_once
from tests.factories import make_movie

DB_URL = os.environ.get("TEST_DATABASE_URL")
REDIS_URL = os.environ.get("TEST_REDIS_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DB_URL, reason="TEST_DATABASE_URL не задан"),
]


async def _ensure_database(url: str) -> None:
    base, _, name = url.rpartition("/")
    admin = create_async_engine(f"{base}/postgres", isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        exists = await conn.scalar(
            text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": name}
        )
        if not exists:
            await conn.execute(text(f'CREATE DATABASE "{name}"'))
    await admin.dispose()


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    assert DB_URL is not None
    await _ensure_database(DB_URL)
    engine = create_engine(DB_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield session_factory(engine)
    await engine.dispose()


async def test_concurrent_last_likes_with_row_lock(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    def uow() -> SqlUnitOfWork:
        return SqlUnitOfWork(factory)

    async with uow() as u:
        await u.movies.upsert_many([make_movie(1, "драма"), make_movie(2, "комедия")])
        await u.commit()

    service = RoomService(uow, settings=RoomSettings(min_participants=3))
    auth = AuthService(uow)
    users = [await auth.resolve_external("telegram", str(i), f"u{i}") for i in range(3)]

    room = await service.create_room(users[0], RoomFilters(genres=frozenset({"драма"})))
    for user in users[1:]:
        await service.join(room.code, user)
    room = await service.start(room.code, users[0])
    [movie_id] = room.candidate_ids
    await service.swipe(room.code, users[0], movie_id, liked=True)

    outcomes = await asyncio.gather(
        service.swipe(room.code, users[1], movie_id, liked=True),
        service.swipe(room.code, users[2], movie_id, liked=True),
    )

    assert sum(o.match is not None for o in outcomes) == 1
    reloaded = await service.get_room(room.code)
    assert reloaded.status is RoomStatus.FINISHED
    assert list(reloaded.matches) == [movie_id]
    async with factory() as session:
        types = list(await session.scalars(select(OutboxRow.event_type).order_by(OutboxRow.id)))
    assert types.count("match_found") == 1


async def test_external_users_are_upserted(factory: async_sessionmaker[AsyncSession]) -> None:
    auth = AuthService(lambda: SqlUnitOfWork(factory))
    first = await auth.resolve_external("telegram", "100", "Старое имя")
    second = await auth.resolve_external("telegram", "100", "Новое имя")
    assert first.id == second.id
    assert second.display_name == "Новое имя"


async def test_genre_counts_and_filters(factory: async_sessionmaker[AsyncSession]) -> None:
    async with SqlUnitOfWork(factory) as u:
        await u.movies.upsert_many(
            [
                make_movie(1, "драма", year=1990),
                make_movie(2, "драма", "комедия", year=2015, duration=90),
                make_movie(3, "ужасы", year=2015, duration=150),
            ]
        )
        await u.commit()
        assert (await u.movies.genre_counts())[0] == ("драма", 2)
        ids = await u.movies.find_candidate_ids(
            RoomFilters(genres=frozenset({"драма", "ужасы"}), year_from=2000, max_duration=120),
            limit=10,
        )
        movies = await u.movies.get_many(ids)
        assert [m.title for m in movies.values()] == ["Фильм 2"]


@pytest.mark.skipif(not REDIS_URL, reason="TEST_REDIS_URL не задан")
async def test_outbox_relay_publishes_once(factory: async_sessionmaker[AsyncSession]) -> None:
    assert REDIS_URL is not None
    redis = Redis.from_url(REDIS_URL, decode_responses=True)
    await redis.delete(EVENTS_STREAM)

    def uow() -> SqlUnitOfWork:
        return SqlUnitOfWork(factory)

    async with uow() as u:
        await u.movies.upsert_many([make_movie(1, "драма")])
        await u.commit()
    service = RoomService(uow)
    auth = AuthService(uow)
    owner = await auth.resolve_external("telegram", "1", "a")
    room = await service.create_room(owner, RoomFilters())
    await service.join(room.code, await auth.resolve_external("telegram", "2", "b"))

    publisher = RedisEventPublisher(redis)
    assert await relay_once(factory, publisher) == 1
    assert await relay_once(factory, publisher) == 0  # уже отмечено как отправленное
    entries = await redis.xrange(EVENTS_STREAM)
    assert [fields["type"] for _, fields in entries] == ["participant_joined"]
    await redis.aclose()
