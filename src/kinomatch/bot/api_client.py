"""HTTP-клиент к API «Фильм вместе». Бот ходит как доверенный сервис: сервисный ключ +
id пользователя Telegram. Так же может работать навык Алисы или любой другой клиент."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx


@dataclass(frozen=True, slots=True)
class TgUser:
    id: int
    name: str


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class KinomatchApi:
    def __init__(self, base_url: str, service_key: str) -> None:
        self._service_key = service_key
        self._http = httpx.AsyncClient(base_url=base_url.rstrip("/") + "/api/v1", timeout=10)

    async def aclose(self) -> None:
        await self._http.aclose()

    def _headers(self, user: TgUser | None) -> dict[str, str]:
        headers = {"X-Service-Key": self._service_key}
        if user is not None:
            headers["X-External-User"] = f"telegram:{user.id}"
            headers["X-User-Name"] = quote(user.name[:64])
        return headers

    async def _call(
        self,
        method: str,
        path: str,
        user: TgUser | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        response = await self._http.request(method, path, headers=self._headers(user), json=json)
        if response.is_error:
            try:
                error = response.json()["error"]
                raise ApiError(response.status_code, error["code"], error["message"])
            except (ValueError, KeyError, TypeError):
                raise ApiError(response.status_code, "http_error", response.text) from None
        return response.json()

    async def genres(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = await self._call("GET", "/genres")
        return result

    async def create_room(self, user: TgUser, genres: list[str]) -> dict[str, Any]:
        body = {"filters": {"genres": genres}, "stop_on_match": True}
        result: dict[str, Any] = await self._call("POST", "/rooms", user, body)
        return result

    async def room_action(self, user: TgUser, code: str, action: str) -> dict[str, Any]:
        """action: join | leave | start | finish"""
        result: dict[str, Any] = await self._call("POST", f"/rooms/{code}/{action}", user)
        return result

    async def next_card(self, user: TgUser, code: str) -> dict[str, Any] | None:
        result = await self._call("GET", f"/rooms/{code}/next", user)
        card: dict[str, Any] | None = result["card"]
        return card

    async def swipe(self, user: TgUser, code: str, movie_id: int, liked: bool) -> dict[str, Any]:
        body = {"movie_id": movie_id, "liked": liked}
        result: dict[str, Any] = await self._call("POST", f"/rooms/{code}/swipes", user, body)
        return result

    async def results(self, code: str) -> dict[str, Any]:
        result: dict[str, Any] = await self._call("GET", f"/rooms/{code}/results")
        return result

    async def movie(self, movie_id: int) -> dict[str, Any]:
        result: dict[str, Any] = await self._call("GET", f"/movies/{movie_id}")
        return result

    async def internal_room(self, code: str) -> dict[str, Any]:
        result: dict[str, Any] = await self._call("GET", f"/internal/rooms/{code}")
        return result
