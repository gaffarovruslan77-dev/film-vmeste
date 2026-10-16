from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Все настройки — из переменных окружения или файла .env (см. .env.example)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Хранилище: postgres — основной режим; memory — быстрый запуск без БД (данные в памяти).
    storage: Literal["postgres", "memory"] = "postgres"
    database_url: str = "postgresql+asyncpg://kinomatch:kinomatch@localhost:5432/kinomatch"
    redis_url: str = "redis://localhost:6379/0"
    catalog_path: Path = Path("data/movies.json")

    # Ключ доверенных клиентов (бот, навык Алисы): они передают id пользователя сами.
    service_key: str = "dev-service-key"

    min_participants: int = 2
    pool_size: int = 200

    # Telegram-бот
    bot_token: str = ""
    api_url: str = "http://localhost:8000"

    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
