"""Outbox relay: переносит события из таблицы outbox в Redis Streams.

Гарантия — at-least-once: если воркер упадёт между отправкой в Redis и отметкой в БД,
событие уйдёт повторно. Поэтому у каждого события есть id, и потребители отбрасывают дубли.
FOR UPDATE SKIP LOCKED позволяет запустить несколько воркеров без двойной обработки.
"""

from __future__ import annotations

import asyncio
import logging

from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kinomatch.application.services import utcnow
from kinomatch.config import get_settings
from kinomatch.infrastructure.db.session import create_engine, session_factory
from kinomatch.infrastructure.db.tables import OutboxRow
from kinomatch.infrastructure.events import Event, RedisEventPublisher

log = logging.getLogger(__name__)

BATCH_SIZE = 100
IDLE_DELAY = 0.3


async def relay_once(
    factory: async_sessionmaker[AsyncSession], publisher: RedisEventPublisher
) -> int:
    async with factory() as session, session.begin():
        rows = list(
            await session.scalars(
                select(OutboxRow)
                .where(OutboxRow.published_at.is_(None))
                .order_by(OutboxRow.id)
                .limit(BATCH_SIZE)
                .with_for_update(skip_locked=True)
            )
        )
        if not rows:
            return 0
        await publisher.publish([Event(r.id, r.event_type, r.payload) for r in rows])
        await session.execute(
            update(OutboxRow)
            .where(OutboxRow.id.in_([r.id for r in rows]))
            .values(published_at=utcnow())
        )
        return len(rows)


async def run(factory: async_sessionmaker[AsyncSession], redis: Redis) -> None:
    publisher = RedisEventPublisher(redis)
    log.info("Outbox relay запущен")
    while True:
        try:
            sent = await relay_once(factory, publisher)
        except Exception:
            log.exception("Ошибка при доставке событий, повтор через секунду")
            await asyncio.sleep(1)
            continue
        if sent:
            log.info("Отправлено событий: %s", sent)
        else:
            await asyncio.sleep(IDLE_DELAY)


async def main() -> None:
    settings = get_settings()
    engine = create_engine(settings.database_url)
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        await run(session_factory(engine), redis)
    finally:
        await redis.aclose()
        await engine.dispose()
