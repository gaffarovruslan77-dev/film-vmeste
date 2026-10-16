from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

GENRES_DONE = -1
GENRES_ANY = -2


class MenuCb(CallbackData, prefix="m"):
    action: str  # new | help


class GenreCb(CallbackData, prefix="g"):
    idx: int


class RoomCb(CallbackData, prefix="r"):
    action: str  # start | finish | results | leave
    code: str


class VoteCb(CallbackData, prefix="v"):
    code: str
    movie_id: int
    liked: bool


def main_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🎬 Создать комнату", callback_data=MenuCb(action="new"))
    kb.button(text="❓ Как это работает", callback_data=MenuCb(action="help"))
    kb.adjust(1)
    return kb.as_markup()


def genre_picker(genres: list[str], selected: set[str]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for idx, genre in enumerate(genres):
        mark = "✅ " if genre in selected else ""
        kb.button(text=f"{mark}{genre}", callback_data=GenreCb(idx=idx))
    kb.adjust(2)
    kb.row(
        InlineKeyboardButton(text="🎲 Любые жанры", callback_data=GenreCb(idx=GENRES_ANY).pack()),
        InlineKeyboardButton(text="▶️ Создать", callback_data=GenreCb(idx=GENRES_DONE).pack()),
    )
    return kb.as_markup()


def lobby(code: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="▶️ Начать голосование", callback_data=RoomCb(action="start", code=code))
    return kb.as_markup()


def vote(code: str, movie_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="👎 Нет", callback_data=VoteCb(code=code, movie_id=movie_id, liked=False))
    kb.button(text="👍 Хочу", callback_data=VoteCb(code=code, movie_id=movie_id, liked=True))
    kb.button(text="🏁 Итоги", callback_data=RoomCb(action="results", code=code))
    kb.button(text="⏹ Завершить", callback_data=RoomCb(action="finish", code=code))
    kb.adjust(2, 2)
    return kb.as_markup()


def deck_empty(code: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🏁 Итоги", callback_data=RoomCb(action="results", code=code))
    kb.button(text="⏹ Завершить", callback_data=RoomCb(action="finish", code=code))
    return kb.as_markup()
