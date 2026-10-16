"""События в Redis Streams.

* `kinomatch:events` — общий поток. Бот читает его через consumer group: каждое событие
  обработает ровно один экземпляр бота, необработанные (упал посреди работы) достанутся
  при перезапуске из pending-списка.
* `kinomatch:room:<CODE>` — поток комнаты для WebSocket-клиентов (веб, десктоп).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from redis.asyncio import Redis

EVENTS_STREAM = "kinomatch:events"
# Блокирующие чтения (XREAD/XREADGROUP BLOCK) ждут новых событий до 15 с. В redis-py 8
# таймаут сокета по умолчанию 5 с — без явной настройки каждое «пустое» ожидание
# заканчивалось бы ошибкой TimeoutError. Поэтому таймаут задаём заведомо больше.
MAX_BLOCK_MS = 15_000
REDIS_SOCKET_TIMEOUT = 30
EVENTS_MAXLEN = 100_000
ROOM_STREAM_MAXLEN = 1_000
ROOM_STREAM_TTL = 24 * 3600


def redis_client(url: str) -> Redis:
    return Redis.from_url(url, decode_responses=True, socket_timeout=REDIS_SOCKET_TIMEOUT)


def room_stream(code: str) -> str:
    return f"kinomatch:room:{code}"


@dataclass(frozen=True, slots=True)
class Event:
    id: int  # id из outbox — по нему потребители отбрасывают дубли
    type: str
    payload: dict[str, Any]

    @property
    def room_code(self) -> str:
        return str(self.payload["room_code"])

    def to_fields(self) -> dict[str, str]:
        return {
            "id": str(self.id),
            "type": self.type,
            "payload": json.dumps(self.payload, ensure_ascii=False),
        }

    @classmethod
    def from_fields(cls, fields: dict[str, str]) -> Event:
        return cls(int(fields["id"]), fields["type"], json.loads(fields["payload"]))


class RedisEventPublisher:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def publish(self, events: list[Event]) -> None:
        async with self._redis.pipeline(transaction=False) as pipe:
            for event in events:
                fields: Any = event.to_fields()
                stream = room_stream(event.room_code)
                pipe.xadd(EVENTS_STREAM, fields, maxlen=EVENTS_MAXLEN, approximate=True)
                pipe.xadd(stream, fields, maxlen=ROOM_STREAM_MAXLEN, approximate=True)
                pipe.expire(stream, ROOM_STREAM_TTL)
            await pipe.execute()
