"""Сквозная проверка бота без настоящего Telegram.

Код бота (обработчики + рассылка событий) работает как есть, но вместо серверов Telegram —
FakeTelegram, который запоминает все сообщения бота. Два виртуальных пользователя (Аня и Боря)
проходят полный сценарий на настоящем стеке (docker compose up): создание комнаты,
приглашение, голосование, совпадение.

Запуск (стек должен быть поднят):  python scripts/bot_e2e.py
"""

from __future__ import annotations

import asyncio
import itertools
import re
import sys
import time
from dataclasses import dataclass, field
from html import unescape
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import (
    AnswerCallbackQuery,
    EditMessageReplyMarkup,
    EditMessageText,
    GetMe,
    SendMessage,
    SetMyCommands,
    TelegramMethod,
)
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from redis.asyncio import Redis

from kinomatch.bot.api_client import KinomatchApi
from kinomatch.bot.handlers import router
from kinomatch.bot.notifier import EventNotifier
from kinomatch.config import Settings
from kinomatch.infrastructure.events import Event, room_stream

BOT_USERNAME = "film_vmeste2026_bot"
TAG = re.compile(r"<[^>]+>")


def plain(text: str | None) -> str:
    return unescape(TAG.sub("", text or ""))


@dataclass
class Entry:
    chat_id: int
    kind: str  # send | edit | alert
    text: str


@dataclass
class FakeTelegram(BaseSession):
    """Подмена серверов Telegram: хранит сообщения и отвечает как Bot API."""

    log: list[Entry] = field(default_factory=list)
    messages: dict[tuple[int, int], Message] = field(default_factory=dict)
    _ids: itertools.count[int] = field(default_factory=lambda: itertools.count(1000))

    def __post_init__(self) -> None:
        super().__init__()

    def _store(self, bot: Bot, chat_id: int, message_id: int, text: str, markup: Any) -> Message:
        message = Message.model_validate(
            {
                "message_id": message_id,
                "date": int(time.time()),
                "chat": {"id": chat_id, "type": "private"},
                "from": {"id": 1, "is_bot": True, "first_name": "bot"},
                "text": text,
                "reply_markup": markup.model_dump() if markup is not None else None,
            },
            context={"bot": bot},
        )
        self.messages[(chat_id, message_id)] = message
        return message

    async def make_request(  # сигнатуру задаёт aiogram, поэтому параметр timeout
        self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None
    ) -> Any:
        if isinstance(method, GetMe):
            return User(id=1, is_bot=True, first_name="Фильм вместе", username=BOT_USERNAME)
        if isinstance(method, SetMyCommands):
            return True
        if isinstance(method, SendMessage):
            chat_id = int(method.chat_id)
            self.log.append(Entry(chat_id, "send", plain(method.text)))
            return self._store(bot, chat_id, next(self._ids), method.text, method.reply_markup)
        if isinstance(method, EditMessageText):
            chat_id, message_id = int(method.chat_id or 0), int(method.message_id or 0)
            self.log.append(Entry(chat_id, "edit", plain(method.text)))
            return self._store(bot, chat_id, message_id, method.text, method.reply_markup)
        if isinstance(method, EditMessageReplyMarkup):
            chat_id, message_id = int(method.chat_id or 0), int(method.message_id or 0)
            old = self.messages[(chat_id, message_id)]
            return self._store(bot, chat_id, message_id, old.text or "", method.reply_markup)
        if isinstance(method, AnswerCallbackQuery):
            if method.show_alert:
                self.log.append(Entry(0, "alert", method.text or ""))
            return True
        raise NotImplementedError(type(method).__name__)

    async def stream_content(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def close(self) -> None:
        pass

    def last_with_button(self, chat_id: int, label: str) -> Message | None:
        for (cid, _), message in sorted(self.messages.items(), key=lambda kv: -kv[0][1]):
            if cid == chat_id and find_button(message, label):
                return message
        return None


def find_button(message: Message, label: str) -> str | None:
    markup = message.reply_markup
    if markup is None:
        return None
    for row in markup.inline_keyboard:
        for button in row:
            if label in button.text:
                return button.callback_data
    return None


class Scenario:
    def __init__(self, bot: Bot, dp: Dispatcher, tg: FakeTelegram, notifier: EventNotifier,
                 redis: Redis) -> None:  # fmt: skip
        self.bot, self.dp, self.tg, self.notifier, self.redis = bot, dp, tg, notifier, redis
        self._ids = itertools.count(1)
        self.last_event = "0"
        self.code: str | None = None
        self.events: list[str] = []

    async def say(self, user: User, text: str) -> None:
        message = Message(
            message_id=next(self._ids),
            date=int(time.time()),
            chat=Chat(id=user.id, type="private"),
            from_user=user,
            text=text,
        )
        await self.dp.feed_update(self.bot, Update(update_id=next(self._ids), message=message))

    async def press(self, user: User, label: str) -> bool:
        message = self.tg.last_with_button(user.id, label)
        if message is None:
            return False
        data = find_button(message, label)
        query = CallbackQuery(
            id=str(next(self._ids)),
            from_user=user,
            chat_instance="e2e",
            data=data,
            message=message,
        )
        await self.dp.feed_update(self.bot, Update(update_id=next(self._ids), callback_query=query))
        return True

    async def pump_events(self) -> None:
        """Дождаться, пока воркер перенесёт события в Redis, и обработать их нотификатором."""
        assert self.code is not None
        await asyncio.sleep(1.0)
        entries = await self.redis.xrange(room_stream(self.code), min=f"({self.last_event}")
        for entry_id, fields in entries:
            self.last_event = entry_id
            event = Event.from_fields(fields)
            self.events.append(event.type)
            await getattr(self.notifier, f"_on_{event.type}")(event)


def card_movie(text: str) -> tuple[str, int, str]:
    """Название, год и строка жанров из текста карточки."""
    lines = text.splitlines()
    m = re.match(r"🎬 (.+?)(?: \((\d{4})\))?$", lines[0])
    title, year = (m.group(1), int(m.group(2) or 0)) if m else (lines[0], 0)
    return title, year, lines[1] if len(lines) > 1 else ""


async def main() -> None:
    settings = Settings()
    tg = FakeTelegram()
    bot = Bot("1:fake", session=tg, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    api = KinomatchApi("http://localhost:8000", settings.service_key)
    redis = Redis.from_url("redis://localhost:6379/0", decode_responses=True)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    dp["api"] = api
    dp["bot_username"] = BOT_USERNAME
    notifier = EventNotifier(bot, api, redis, consumer="e2e")
    s = Scenario(bot, dp, tg, notifier, redis)

    anya = User(id=7_000_000_001, is_bot=False, first_name="Аня")
    borya = User(id=7_000_000_002, is_bot=False, first_name="Боря")
    names = {anya.id: "Аня", borya.id: "Боря"}

    # Вкусы: Аня хочет комедию или мультфильм, Боря — что-нибудь не старше 2000 года.
    def likes(user: User, genres: str, year: int) -> bool:
        if user is anya:
            return "комедия" in genres or "мультфильм" in genres
        return year >= 2000

    await s.say(anya, "/start")
    await s.say(anya, "/new")
    await s.press(anya, "комедия")
    await s.press(anya, "мультфильм")
    await s.press(anya, "Создать")
    lobby = tg.last_with_button(anya.id, "Начать голосование")
    assert lobby is not None, "бот не прислал кнопку «Начать голосование»"
    s.code = (find_button(lobby, "Начать голосование") or "").split(":")[-1]

    await s.say(borya, f"/start join_{s.code}")
    await s.pump_events()
    await s.press(anya, "Начать голосование")
    await s.pump_events()

    swipes = 0
    for _ in range(40):
        if "match_found" in s.events:
            break
        for user in (anya, borya):
            card = tg.last_with_button(user.id, "Хочу")
            if card is None:
                continue
            _, year, genres = card_movie(plain(card.text))
            await s.press(user, "Хочу" if likes(user, genres, year) else "Нет")
            swipes += 1
            await s.pump_events()
            if "match_found" in s.events:
                break

    out = [f"Комната {s.code}, свайпов: {swipes}, события: {', '.join(s.events)}", ""]
    for entry in tg.log:
        who = names.get(entry.chat_id, "⚠ алерт")
        first = entry.text.splitlines()
        body = " / ".join(line for line in first[:3] if line)
        out.append(f"[{who:>4}] {entry.kind:5} {body[:150]}")
    report = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    text = "\n".join(out)
    if report:
        report.write_text(text, encoding="utf-8")
    else:
        print(text)

    ok = "match_found" in s.events and sum("Совпадение" in e.text for e in tg.log) == 2
    await api.aclose()
    await redis.aclose()
    print("OK: совпадение найдено и разослано обоим" if ok else "FAIL: см. отчёт")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    asyncio.run(main())
