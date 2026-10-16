"""Читает события из Redis Streams и рассылает уведомления участникам в Telegram.

Consumer group: если запущено несколько ботов, каждое событие обработает только один.
После падения бот сначала дочитывает свои необработанные (pending) события.
Дубли (outbox гарантирует at-least-once) отсекаются по id события через SET NX в Redis.
"""

from __future__ import annotations

import asyncio
import logging
from html import escape
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import LinkPreviewOptions
from redis.asyncio import Redis
from redis.exceptions import ResponseError

from kinomatch.bot import keyboards as kb
from kinomatch.bot import texts
from kinomatch.bot.api_client import ApiError, KinomatchApi, TgUser
from kinomatch.infrastructure.events import EVENTS_STREAM, Event

log = logging.getLogger(__name__)

GROUP = "telegram-bot"
READ_BLOCK_MS = 5000  # должно быть меньше таймаута сокета Redis (см. infrastructure/events.py)
DEDUP_TTL = 24 * 3600


class EventNotifier:
    def __init__(self, bot: Bot, api: KinomatchApi, redis: Redis, consumer: str) -> None:
        self._bot = bot
        self._api = api
        self._redis = redis
        self._consumer = consumer

    async def run(self) -> None:
        try:
            await self._redis.xgroup_create(EVENTS_STREAM, GROUP, id="$", mkstream=True)
        except ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise
        last_id = "0"  # сначала — свои pending после перезапуска, затем — новые (">")
        while True:
            try:
                response: Any = await self._redis.xreadgroup(
                    GROUP, self._consumer, {EVENTS_STREAM: last_id}, count=20, block=READ_BLOCK_MS
                )
            except Exception:
                log.exception("Не удалось прочитать события, повтор через 2 с")
                await asyncio.sleep(2)
                continue
            messages = response[0][1] if response else []
            if not messages:
                last_id = ">"
                continue
            for message_id, fields in messages:
                await self._process(Event.from_fields(fields))
                await self._redis.xack(EVENTS_STREAM, GROUP, message_id)

    async def _process(self, event: Event) -> None:
        if not await self._redis.set(f"kinomatch:bot:seen:{event.id}", 1, nx=True, ex=DEDUP_TTL):
            return  # уже обработано
        handler = getattr(self, f"_on_{event.type}", None)
        if handler is None:
            return
        try:
            await handler(event)
        except ApiError:
            log.exception("Ошибка API при обработке события %s", event)
        except Exception:
            log.exception("Ошибка при обработке события %s", event)

    async def _recipients(self, code: str) -> list[dict[str, Any]]:
        room = await self._api.internal_room(code)
        return [
            p
            for p in room["participants"]
            if p["active"] and p["provider"] == "telegram" and p["external_id"]
        ]

    async def _send(self, chat_id: int, text: str, **kwargs: Any) -> None:
        try:
            await self._bot.send_message(chat_id, text, **kwargs)
        except TelegramAPIError as e:  # пользователь заблокировал бота и т.п.
            log.warning("Не удалось отправить сообщение %s: %s", chat_id, e)

    async def _on_participant_joined(self, event: Event) -> None:
        name = escape(event.payload["display_name"])
        for p in await self._recipients(event.room_code):
            if p["user_id"] != event.payload["user_id"]:
                await self._send(int(p["external_id"]), f"👋 В комнату зашёл участник: {name}")

    async def _on_participant_left(self, event: Event) -> None:
        name = escape(event.payload["display_name"])
        for p in await self._recipients(event.room_code):
            await self._send(int(p["external_id"]), f"🚪 {name} покидает комнату")

    async def _on_voting_started(self, event: Event) -> None:
        code = event.room_code
        for p in await self._recipients(code):
            chat_id = int(p["external_id"])
            await self._send(
                chat_id,
                f"🎬 Голосование началось! В подборке {event.payload['candidates']} фильмов.",
            )
            card = await self._api.next_card(TgUser(chat_id, p["display_name"]), code)
            if card is None:
                continue
            url = card["movie"].get("kinopoisk_url")
            await self._send(
                chat_id,
                texts.card_text(card),
                reply_markup=kb.vote(code, card["movie"]["id"]),
                link_preview_options=LinkPreviewOptions(url=url, is_disabled=url is None),
            )

    async def _on_match_found(self, event: Event) -> None:
        movie = await self._api.movie(event.payload["movie_id"])
        text = "🎉 <b>Совпадение!</b> Этот фильм хотят посмотреть все:\n\n" + texts.movie_text(
            movie
        )
        url = movie.get("kinopoisk_url")
        for p in await self._recipients(event.room_code):
            await self._send(
                int(p["external_id"]),
                text,
                link_preview_options=LinkPreviewOptions(url=url, is_disabled=url is None),
            )

    async def _on_room_finished(self, event: Event) -> None:
        if event.payload["reason"] == "match":
            return  # о совпадении уже сообщили
        results = await self._api.results(event.room_code)
        text = "🏁 Голосование завершено.\n\n" + texts.results_text(results)
        for p in await self._recipients(event.room_code):
            await self._send(int(p["external_id"]), text)
