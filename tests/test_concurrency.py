"""Гонка: последние два лайка приходят одновременно. Без блокировки комнаты каждая транзакция
не увидит голос другой — и совпадение потеряется. Проверяем, что оно находится ровно один раз."""

import asyncio

from kinomatch.application.services import RoomService, RoomSettings
from kinomatch.domain.models import RoomFilters
from kinomatch.infrastructure.memory import InMemoryStore, in_memory_uow_factory
from tests.factories import make_movie, make_user


async def test_simultaneous_last_likes_produce_single_match() -> None:
    store = InMemoryStore()
    store.movies = {1: make_movie(1, "драма")}
    service = RoomService(in_memory_uow_factory(store), settings=RoomSettings(min_participants=3))
    users = [make_user(i) for i in (1, 2, 3)]

    room = await service.create_room(users[0], RoomFilters())
    for user in users[1:]:
        await service.join(room.code, user)
    await service.start(room.code, users[0])
    await service.swipe(room.code, users[0], 1, liked=True)

    outcomes = await asyncio.gather(
        service.swipe(room.code, users[1], 1, liked=True),
        service.swipe(room.code, users[2], 1, liked=True),
    )

    assert sum(o.match is not None for o in outcomes) == 1
    assert [e.type for e in store.events].count("match_found") == 1
