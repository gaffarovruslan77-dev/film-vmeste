"""Зависимости FastAPI: доступ к контейнеру и аутентификация.

Два способа войти:
* `Authorization: Bearer <token>` — гость из веба/десктопа (токен из POST /auth/guest);
* `X-Service-Key` + `X-External-User: telegram:12345` — доверенный клиент (бот, навык Алисы)
  сам подтверждает личность пользователя. Имя — в `X-User-Name` (URL-encoded, т.к. кириллица).
"""

import hmac
from typing import Annotated
from urllib.parse import unquote

from fastapi import Depends, Header
from starlette.requests import HTTPConnection

from kinomatch.api.container import Container
from kinomatch.domain import errors
from kinomatch.domain.models import User


def get_container(conn: HTTPConnection) -> Container:
    # HTTPConnection, а не Request — чтобы работало и для WebSocket
    container: Container = conn.app.state.container
    return container


ContainerDep = Annotated[Container, Depends(get_container)]


def is_service(container: Container, key: str | None) -> bool:
    return key is not None and hmac.compare_digest(key, container.settings.service_key)


async def current_user(
    container: ContainerDep,
    authorization: Annotated[str | None, Header()] = None,
    x_service_key: Annotated[str | None, Header()] = None,
    x_external_user: Annotated[str | None, Header()] = None,
    x_user_name: Annotated[str | None, Header()] = None,
) -> User:
    if authorization and authorization.lower().startswith("bearer "):
        return await container.auth.authenticate(authorization[7:].strip())
    if x_external_user:
        if not is_service(container, x_service_key):
            raise errors.Unauthorized("Неверный сервисный ключ")
        provider, sep, external_id = x_external_user.partition(":")
        if not sep or not provider or not external_id:
            raise errors.Unauthorized("X-External-User должен быть вида provider:id")
        name = unquote(x_user_name or "") or f"{provider}:{external_id}"
        return await container.auth.resolve_external(provider, external_id, name[:64])
    raise errors.Unauthorized()


async def require_service(
    container: ContainerDep,
    x_service_key: Annotated[str | None, Header()] = None,
) -> None:
    if not is_service(container, x_service_key):
        raise errors.Unauthorized("Нужен сервисный ключ")


UserDep = Annotated[User, Depends(current_user)]
