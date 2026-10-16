import asyncio
import contextlib
import logging
import socket

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import BotCommand
from redis.asyncio import Redis

from kinomatch.bot.api_client import KinomatchApi
from kinomatch.bot.handlers import router
from kinomatch.bot.notifier import EventNotifier
from kinomatch.config import get_settings

log = logging.getLogger(__name__)


async def main() -> None:
    settings = get_settings()
    if not settings.bot_token:
        raise SystemExit("Задайте BOT_TOKEN в .env (токен выдаёт @BotFather)")

    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    api = KinomatchApi(settings.api_url, settings.service_key)
    redis = Redis.from_url(settings.redis_url, decode_responses=True)

    # Состояние диалогов (выбор жанров) хранится в Redis — бот без состояния,
    # его можно перезапускать и запускать в нескольких экземплярах.
    dp = Dispatcher(storage=RedisStorage(Redis.from_url(settings.redis_url)))
    dp.include_router(router)
    me = await bot.get_me()
    dp["api"] = api
    dp["bot_username"] = me.username

    await bot.set_my_commands(
        [
            BotCommand(command="new", description="Создать комнату"),
            BotCommand(command="join", description="Войти по коду"),
            BotCommand(command="help", description="Как это работает"),
        ]
    )

    notifier = EventNotifier(bot, api, redis, consumer=f"bot-{socket.gethostname()}")
    notifier_task = asyncio.create_task(notifier.run())
    log.info("Бот @%s запущен", me.username)
    try:
        await dp.start_polling(bot)
    finally:
        notifier_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await notifier_task
        await api.aclose()
        await redis.aclose()
        await bot.session.close()
