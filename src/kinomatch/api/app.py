from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from kinomatch.api.container import Container, build_container
from kinomatch.api.metrics import metrics_endpoint, metrics_middleware
from kinomatch.api.routes import router
from kinomatch.config import get_settings
from kinomatch.domain import errors

STATUS_BY_ERROR: dict[type[errors.DomainError], int] = {
    errors.RoomNotFound: 404,
    errors.MovieNotFound: 404,
    errors.Unauthorized: 401,
    errors.NotRoomOwner: 403,
    errors.NotParticipant: 403,
    errors.InvalidRoomState: 409,
    errors.NotEnoughParticipants: 409,
    errors.NoCandidates: 422,
    errors.MovieNotInRoom: 422,
}


async def domain_error_handler(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, errors.DomainError)
    status = STATUS_BY_ERROR.get(type(exc), 400)
    return JSONResponse(
        status_code=status, content={"error": {"code": exc.code, "message": str(exc)}}
    )


def create_app(container: Container | None = None) -> FastAPI:
    container = container or build_container(get_settings())

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        for hook in container.on_startup:
            await hook()
        yield
        for hook in container.on_shutdown:
            await hook()

    app = FastAPI(
        title="Фильм вместе",
        description="Групповой выбор фильма свайпами. Концепт фичи для Кинопоиска.",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.container = container
    app.include_router(router)
    app.add_exception_handler(errors.DomainError, domain_error_handler)
    app.middleware("http")(metrics_middleware)
    app.add_api_route("/metrics", metrics_endpoint, include_in_schema=False)

    @app.get("/health", tags=["service"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app
