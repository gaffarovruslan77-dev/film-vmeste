from kinomatch.bot.notifier import READ_BLOCK_MS
from kinomatch.infrastructure.events import MAX_BLOCK_MS, redis_client


def test_socket_timeout_exceeds_blocking_reads() -> None:
    """Регрессия: в redis-py 8 таймаут сокета по умолчанию 5 с, а бот ждёт события 5 с —
    каждое пустое ожидание падало с TimeoutError."""
    client = redis_client("redis://localhost:6379/0")
    timeout_ms = client.connection_pool.connection_kwargs["socket_timeout"] * 1000
    assert timeout_ms > max(READ_BLOCK_MS, MAX_BLOCK_MS)
