"""Тексты и форматирование сообщений (HTML-разметка Telegram)."""

from html import escape
from typing import Any

WELCOME = (
    "🍿 <b>Фильм вместе</b>\n\n"
    "Не можете выбрать, что посмотреть? Создайте комнату, позовите друзей "
    "и листайте фильмы: 👍 — хочу, 👎 — нет.\n"
    "Как только фильм понравится <b>всем</b> — я сообщу о совпадении.\n\n"
    "Чем больше вы свайпаете, тем лучше я понимаю вкус компании и показываю "
    "фильмы, которые с большей вероятностью подойдут всем."
)

HELP = (
    "<b>Как пользоваться</b>\n"
    "1. /new — создать комнату и выбрать жанры\n"
    "2. Отправь друзьям ссылку-приглашение или код комнаты\n"
    "3. Когда все зашли — нажми «Начать голосование»\n"
    "4. Свайпайте 👍/👎, пока не найдётся общий фильм\n\n"
    "/join КОД — войти в комнату по коду\n"
    "Можно просто прислать мне код из 6 символов."
)

GENRE_PICKER = "Выбери жанры (можно несколько) или нажми «Любые жанры»:"


def duration(minutes: int | None) -> str | None:
    if not minutes:
        return None
    hours, mins = divmod(minutes, 60)
    if not hours:
        return f"{mins} мин"
    return f"{hours} ч {mins} мин" if mins else f"{hours} ч"


def movie_text(movie: dict[str, Any]) -> str:
    title = escape(movie["title"])
    year = f" ({movie['year']})" if movie.get("year") else ""
    details = [", ".join(movie.get("genres") or [])]
    if (d := duration(movie.get("duration_min"))) is not None:
        details.append(d)
    lines = [f"🎬 <b>{title}</b>{year}", escape(" · ".join(x for x in details if x))]
    if movie.get("kinopoisk_url"):
        lines.append(f'<a href="{escape(movie["kinopoisk_url"])}">Открыть на Кинопоиске</a>')
    return "\n".join(line for line in lines if line)


def card_text(card: dict[str, Any]) -> str:
    text = movie_text(card["movie"])
    if card["reason"] == "hot":
        text += f"\n\n🔥 Уже понравился другим участникам: {card['liked_by_others']}"
    return text


def room_created(code: str, invite_link: str, genres: list[str]) -> str:
    genres_text = ", ".join(genres) if genres else "любые"
    return (
        f"✅ Комната <code>{code}</code> создана. Жанры: {escape(genres_text)}.\n\n"
        f"Перешли друзьям ссылку:\n{invite_link}\n"
        f"или код комнаты: <code>{code}</code>\n\n"
        "Когда все зайдут — нажми «Начать голосование»."
    )


def joined(room: dict[str, Any]) -> str:
    names = ", ".join(escape(p["display_name"]) for p in room["participants"] if p["active"])
    return (
        f"Ты в комнате <code>{room['code']}</code>.\nУчастники: {names}\n\n"
        "Ждём, когда создатель начнёт голосование."
    )


def results_text(results: dict[str, Any]) -> str:
    parts: list[str] = []
    if results["matches"]:
        parts.append("🎉 <b>Совпадения:</b>")
        parts.extend(movie_text(m) for m in results["matches"])
    else:
        parts.append("Полного совпадения пока нет.")
    if results["top"] and not results["matches"]:
        parts.append("\n<b>Больше всего лайков:</b>")
        for item in results["top"]:
            m = item["movie"]
            year = f" ({m['year']})" if m.get("year") else ""
            parts.append(
                f"• {escape(m['title'])}{year} — 👍 {item['likes']} / 👎 {item['dislikes']}"
            )
    return "\n".join(parts)
