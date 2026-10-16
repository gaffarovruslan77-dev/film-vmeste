"""Метрики Prometheus: GET /metrics."""

import time
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

REQUEST_LATENCY = Histogram(
    "kinomatch_http_request_duration_seconds",
    "Время обработки HTTP-запроса",
    ["method", "route", "status"],
)
SWIPES = Counter("kinomatch_swipes_total", "Свайпы", ["liked"])
MATCHES = Counter("kinomatch_matches_total", "Найденные совпадения")
ROOMS_CREATED = Counter("kinomatch_rooms_created_total", "Созданные комнаты")


async def metrics_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    started = time.perf_counter()
    response = await call_next(request)
    route = request.scope.get("route")
    path = getattr(route, "path", "unmatched")  # шаблон пути, а не конкретный код комнаты
    REQUEST_LATENCY.labels(request.method, path, str(response.status_code)).observe(
        time.perf_counter() - started
    )
    return response


def metrics_endpoint() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
