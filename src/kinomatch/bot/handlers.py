from __future__ import annotations

import logging
import re

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, LinkPreviewOptions, Message, User

from kinomatch.bot import keyboards as kb
from kinomatch.bot import texts
from kinomatch.bot.api_client import ApiError, KinomatchApi, TgUser

log = logging.getLogger(__name__)
router = Router()

ROOM_CODE = re.compile(r"^[A-Za-z0-9]{6}$")
MAX_GENRES_IN_PICKER = 16


def tg_user(user: User) -> TgUser:
    return TgUser(id=user.id, name=user.full_name)


def preview(url: str | None) -> LinkPreviewOptions:
    # Превью ссылки на Кинопоиск показывает постер фильма.
    return LinkPreviewOptions(url=url, is_disabled=url is None)


async def send_card(message: Message, api: KinomatchApi, user: TgUser, code: str) -> None:
    card = await api.next_card(user, code)
    if card is None:
        await message.answer(
            "Фильмы в подборке закончились 🤷 Посмотри итоги или подожди остальных.",
            reply_markup=kb.deck_empty(code),
        )
        return
    await message.answer(
        texts.card_text(card),
        reply_markup=kb.vote(code, card["movie"]["id"]),
        link_preview_options=preview(card["movie"].get("kinopoisk_url")),
    )


# ---------- старт и меню ----------


@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, api: KinomatchApi) -> None:
    assert message.from_user is not None
    if command.args and command.args.startswith("join_"):
        await join_room(message, api, tg_user(message.from_user), command.args[5:])
        return
    await message.answer(texts.WELCOME, reply_markup=kb.main_menu())


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(texts.HELP)


@router.callback_query(kb.MenuCb.filter(F.action == "help"))
async def menu_help(callback: CallbackQuery) -> None:
    assert isinstance(callback.message, Message)
    await callback.message.answer(texts.HELP)
    await callback.answer()


# ---------- создание комнаты ----------


async def show_genre_picker(message: Message, api: KinomatchApi, state: FSMContext) -> None:
    genres = [g["name"] for g in await api.genres()][:MAX_GENRES_IN_PICKER]
    await state.update_data(genres=genres, selected=[])
    await message.answer(texts.GENRE_PICKER, reply_markup=kb.genre_picker(genres, set()))


@router.message(Command("new"))
async def cmd_new(message: Message, api: KinomatchApi, state: FSMContext) -> None:
    await show_genre_picker(message, api, state)


@router.callback_query(kb.MenuCb.filter(F.action == "new"))
async def menu_new(callback: CallbackQuery, api: KinomatchApi, state: FSMContext) -> None:
    assert isinstance(callback.message, Message)
    await show_genre_picker(callback.message, api, state)
    await callback.answer()


@router.callback_query(kb.GenreCb.filter())
async def pick_genre(
    callback: CallbackQuery,
    callback_data: kb.GenreCb,
    api: KinomatchApi,
    state: FSMContext,
    bot_username: str,
) -> None:
    assert isinstance(callback.message, Message)
    data = await state.get_data()
    genres: list[str] = data.get("genres", [])
    selected: list[str] = data.get("selected", [])
    if not genres:
        await callback.answer("Выбор устарел, начни заново: /new", show_alert=True)
        return

    if callback_data.idx >= 0:
        genre = genres[callback_data.idx]
        selected = [g for g in selected if g != genre] if genre in selected else [*selected, genre]
        await state.update_data(selected=selected)
        await callback.message.edit_reply_markup(
            reply_markup=kb.genre_picker(genres, set(selected))
        )
        await callback.answer()
        return

    chosen = [] if callback_data.idx == kb.GENRES_ANY else selected
    try:
        room = await api.create_room(tg_user(callback.from_user), chosen)
    except ApiError as e:
        await callback.answer(e.message, show_alert=True)
        return
    await state.clear()
    code = room["code"]
    invite = f"https://t.me/{bot_username}?start=join_{code}"
    await callback.message.edit_text(
        texts.room_created(code, invite, chosen),
        reply_markup=kb.lobby(code),
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )
    await callback.answer("Комната создана")


# ---------- вход в комнату ----------


async def join_room(message: Message, api: KinomatchApi, user: TgUser, code: str) -> None:
    try:
        room = await api.room_action(user, code.upper(), "join")
    except ApiError as e:
        await message.answer(f"Не получилось войти: {e.message}")
        return
    if room["status"] == "voting":
        await message.answer(f"Ты в комнате <code>{room['code']}</code>, голосование уже идёт!")
        await send_card(message, api, user, room["code"])
    else:
        await message.answer(texts.joined(room))


@router.message(Command("join"))
async def cmd_join(message: Message, command: CommandObject, api: KinomatchApi) -> None:
    assert message.from_user is not None
    if not command.args or not ROOM_CODE.match(command.args.strip()):
        await message.answer("Пришли код комнаты так: /join ABC123")
        return
    await join_room(message, api, tg_user(message.from_user), command.args.strip())


@router.message(F.text.regexp(ROOM_CODE))
async def code_as_text(message: Message, api: KinomatchApi) -> None:
    assert message.from_user is not None and message.text is not None
    await join_room(message, api, tg_user(message.from_user), message.text)


# ---------- действия в комнате ----------


@router.callback_query(kb.RoomCb.filter())
async def room_action(callback: CallbackQuery, callback_data: kb.RoomCb, api: KinomatchApi) -> None:
    assert isinstance(callback.message, Message)
    user, code = tg_user(callback.from_user), callback_data.code
    try:
        if callback_data.action == "results":
            results = await api.results(code)
            await callback.message.answer(texts.results_text(results))
            await callback.answer()
            return
        await api.room_action(user, code, callback_data.action)
    except ApiError as e:
        await callback.answer(e.message, show_alert=True)
        return
    if callback_data.action == "start":
        # Карточки всем участникам разошлёт обработчик события voting_started.
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer("Поехали! 🎬")
    else:
        await callback.answer()


@router.callback_query(kb.VoteCb.filter())
async def vote(callback: CallbackQuery, callback_data: kb.VoteCb, api: KinomatchApi) -> None:
    assert isinstance(callback.message, Message)
    user, code = tg_user(callback.from_user), callback_data.code
    try:
        outcome = await api.swipe(user, code, callback_data.movie_id, callback_data.liked)
    except ApiError as e:
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer(e.message, show_alert=True)
        return
    await callback.answer("👍" if callback_data.liked else "👎")

    if outcome["room_status"] == "finished":
        # О совпадении всем сообщит обработчик события match_found.
        await callback.message.edit_reply_markup(reply_markup=None)
        return
    try:
        card = await api.next_card(user, code)
    except ApiError as e:
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(e.message)
        return
    if card is None:
        await callback.message.edit_text(
            "Фильмы в подборке закончились 🤷 Посмотри итоги или подожди остальных.",
            reply_markup=kb.deck_empty(code),
        )
        return
    await callback.message.edit_text(
        texts.card_text(card),
        reply_markup=kb.vote(code, card["movie"]["id"]),
        link_preview_options=preview(card["movie"].get("kinopoisk_url")),
    )
