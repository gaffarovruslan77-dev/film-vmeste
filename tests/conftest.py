from collections.abc import AsyncIterator, Callable
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest

from kinomatch.api.app import create_app
from kinomatch.api.container import build_memory_container
from kinomatch.config import Settings
from kinomatch.infrastructure.memory import InMemoryStore
from tests.factories import make_movie

SERVICE_KEY = "test-service-key"


@pytest.fixture
def store() -> InMemoryStore:
    store = InMemoryStore()
    movies = [
        make_movie(1, "комедия", popularity=100),
        make_movie(2, "комедия", "драма", popularity=90),
        make_movie(3, "ужасы", popularity=80, year=1985),
        make_movie(4, "драма", popularity=70, duration=200),
        make_movie(5, "фантастика", popularity=60),
    ]
    store.movies = {m.id: m for m in movies}
    return store


@pytest.fixture
def settings() -> Settings:
    return Settings(
        storage="memory",
        service_key=SERVICE_KEY,
        min_participants=2,
        catalog_path=Path("does-not-exist.json"),
    )


@pytest.fixture
async def client(store: InMemoryStore, settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(build_memory_container(settings, store=store))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture
def tg() -> Callable[[int, str], dict[str, str]]:
    """Заголовки, с которыми ходит бот от имени пользователя Telegram."""

    def headers(user_id: int, name: str = "Гость") -> dict[str, str]:
        return {
            "X-Service-Key": SERVICE_KEY,
            "X-External-User": f"telegram:{user_id}",
            "X-User-Name": quote(name),
        }

    return headers
