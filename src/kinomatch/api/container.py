"""Сборка зависимостей (composition root): здесь решается, какие адаптеры подключить."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from redis.asyncio import Redis

from kinomatch.application.services import AuthService, RoomService, RoomSettings, UowFactory
from kinomatch.catalog.wikidata import load as load_catalog
from kinomatch.config import Settings
from kinomatch.infrastructure.events import Event, RedisEventPublisher, redis_client
from kinomatch.infrastructure.memory import InMemoryStore, InMemoryUnitOfWork

log = logging.getLogger(__name__)


@dataclass
class Container:
    settings: Settings
    rooms: RoomService
    auth: AuthService
    redis: Redis | None = None
    on_startup: list[Callable[[], Awaitable[None]]] = field(default_factory=list)
    on_shutdown: list[Callable[[], Awaitable[None]]] = field(default_factory=list)


def _services(uow_factory: UowFactory, settings: Settings) -> tuple[RoomService, AuthService]:
    room_settings = RoomSettings(
        min_participants=settings.min_participants, pool_size=settings.pool_size
    )
    return RoomService(uow_factory, settings=room_settings), AuthService(uow_factory)


def build_postgres_container(settings: Settings) -> Container:
    from kinomatch.infrastructure.db.session import create_engine, session_factory
    from kinomatch.infrastructure.db.uow import SqlUnitOfWork

    engine = create_engine(settings.database_url)
    factory = session_factory(engine)
    rooms, auth = _services(lambda: SqlUnitOfWork(factory), settings)
    redis = redis_client(settings.redis_url)

    async def shutdown() -> None:
        await redis.aclose()
        await engine.dispose()

    return Container(settings, rooms, auth, redis, on_shutdown=[shutdown])


def build_memory_container(
    settings: Settings,
    *,
    redis: Redis | None = None,
    store: InMemoryStore | None = None,
) -> Container:
    """Режим без PostgreSQL: каталог грузится из JSON в память, события (если есть Redis)
    публикуются фоновой задачей прямо из процесса API."""
    store = store or InMemoryStore()
    rooms, auth = _services(lambda: InMemoryUnitOfWork(store), settings)
    container = Container(settings, rooms, auth, redis)

    async def load() -> None:
        if not store.movies and settings.catalog_path.exists():
            movies = [item.to_movie() for item in load_catalog(settings.catalog_path)]
            await InMemoryUnitOfWork(store).movies.upsert_many(movies)
            log.info("Каталог загружен в память: %s фильмов", len(movies))

    container.on_startup.append(load)

    if redis is not None:
        publisher = RedisEventPublisher(redis)
        published = 0
        task: asyncio.Task[None] | None = None

        async def relay() -> None:
            nonlocal published
            while True:
                fresh = store.events[published:]
                if fresh:
                    try:
                        await publisher.publish([Event(e.id, e.type, e.payload) for e in fresh])
                        published += len(fresh)
                    except Exception:
                        log.warning("Redis недоступен, события подождут", exc_info=True)
                        await asyncio.sleep(1)
                await asyncio.sleep(0.2)

        async def start_relay() -> None:
            nonlocal task
            task = asyncio.create_task(relay())

        async def stop_relay() -> None:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await redis.aclose()

        container.on_startup.append(start_relay)
        container.on_shutdown.append(stop_relay)
    return container


def build_container(settings: Settings) -> Container:
    if settings.storage == "memory":
        return build_memory_container(settings, redis=redis_client(settings.redis_url))
    return build_postgres_container(settings)
