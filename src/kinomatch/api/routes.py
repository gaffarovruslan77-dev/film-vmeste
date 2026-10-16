from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect

from kinomatch.api import schemas
from kinomatch.api.deps import ContainerDep, UserDep, require_service
from kinomatch.api.metrics import MATCHES, ROOMS_CREATED, SWIPES
from kinomatch.domain import errors
from kinomatch.infrastructure.events import Event, room_stream

router = APIRouter(prefix="/api/v1")


# ---------- пользователи ----------


@router.post("/auth/guest", response_model=schemas.TokenOut, tags=["auth"])
async def register_guest(body: schemas.GuestIn, c: ContainerDep) -> schemas.TokenOut:
    """Гостевой вход для веб/десктоп клиентов. Токен показывается один раз."""
    user, token = await c.auth.register_guest(body.display_name)
    return schemas.TokenOut(user_id=user.id, token=token)


@router.get("/me", response_model=schemas.UserOut, tags=["auth"])
async def me(user: UserDep) -> schemas.UserOut:
    return schemas.UserOut.of(user)


# ---------- каталог ----------


@router.get("/genres", response_model=list[schemas.GenreOut], tags=["catalog"])
async def genres(c: ContainerDep) -> list[schemas.GenreOut]:
    return [schemas.GenreOut(name=g, movies=n) for g, n in await c.rooms.genres()]


@router.get("/movies/{movie_id}", response_model=schemas.MovieOut, tags=["catalog"])
async def movie(movie_id: int, c: ContainerDep) -> schemas.MovieOut:
    return schemas.MovieOut.of(await c.rooms.movie(movie_id))


# ---------- комнаты ----------


@router.post("/rooms", response_model=schemas.RoomOut, status_code=201, tags=["rooms"])
async def create_room(
    body: schemas.CreateRoomIn, user: UserDep, c: ContainerDep
) -> schemas.RoomOut:
    room = await c.rooms.create_room(
        user, body.filters.to_domain(), stop_on_match=body.stop_on_match
    )
    ROOMS_CREATED.inc()
    return schemas.RoomOut.of(room)


@router.get("/rooms/{code}", response_model=schemas.RoomOut, tags=["rooms"])
async def get_room(code: str, c: ContainerDep) -> schemas.RoomOut:
    return schemas.RoomOut.of(await c.rooms.get_room(code))


@router.post("/rooms/{code}/join", response_model=schemas.RoomOut, tags=["rooms"])
async def join_room(code: str, user: UserDep, c: ContainerDep) -> schemas.RoomOut:
    return schemas.RoomOut.of(await c.rooms.join(code, user))


@router.post("/rooms/{code}/leave", response_model=schemas.RoomOut, tags=["rooms"])
async def leave_room(code: str, user: UserDep, c: ContainerDep) -> schemas.RoomOut:
    return schemas.RoomOut.of(await c.rooms.leave(code, user))


@router.post("/rooms/{code}/start", response_model=schemas.RoomOut, tags=["rooms"])
async def start_voting(code: str, user: UserDep, c: ContainerDep) -> schemas.RoomOut:
    return schemas.RoomOut.of(await c.rooms.start(code, user))


@router.post("/rooms/{code}/finish", response_model=schemas.RoomOut, tags=["rooms"])
async def finish_voting(code: str, user: UserDep, c: ContainerDep) -> schemas.RoomOut:
    return schemas.RoomOut.of(await c.rooms.finish(code, user))


@router.get("/rooms/{code}/next", response_model=schemas.NextCardOut, tags=["voting"])
async def next_card(code: str, user: UserDep, c: ContainerDep) -> schemas.NextCardOut:
    rec = await c.rooms.next_card(code, user)
    return schemas.NextCardOut(card=schemas.CardOut.of(rec) if rec else None)


@router.post("/rooms/{code}/swipes", response_model=schemas.SwipeOut, tags=["voting"])
async def swipe(
    code: str, body: schemas.SwipeIn, user: UserDep, c: ContainerDep
) -> schemas.SwipeOut:
    outcome = await c.rooms.swipe(code, user, body.movie_id, body.liked)
    SWIPES.labels(str(body.liked).lower()).inc()
    if outcome.match:
        MATCHES.inc()
    return schemas.SwipeOut(
        match=schemas.MovieOut.of(outcome.match) if outcome.match else None,
        room_status=outcome.status,
    )


@router.get("/rooms/{code}/results", response_model=schemas.ResultsOut, tags=["voting"])
async def results(code: str, c: ContainerDep) -> schemas.ResultsOut:
    return schemas.ResultsOut.of(await c.rooms.results(code))


@router.websocket("/rooms/{code}/events")
async def room_events(
    websocket: WebSocket, code: str, token: Annotated[str, Query()], c: ContainerDep
) -> None:
    """Живые события комнаты для веб/десктоп клиентов: ?token=<токен гостя>."""
    try:
        user = await c.auth.authenticate(token)
        room = await c.rooms.get_room(code)
    except errors.DomainError:
        await websocket.close(code=4401)
        return
    if not room.is_participant(user.id) or c.redis is None:
        await websocket.close(code=4403)
        return
    await websocket.accept()
    stream, last_id = room_stream(room.code), "$"
    try:
        while True:
            response: Any = await c.redis.xread({stream: last_id}, block=15_000, count=50)
            for _, messages in response:
                for message_id, fields in messages:
                    last_id = message_id
                    event = Event.from_fields(fields)
                    await websocket.send_json({"id": event.id, "type": event.type, **event.payload})
    except WebSocketDisconnect:
        pass


# ---------- для доверенных сервисов ----------


@router.get(
    "/internal/rooms/{code}",
    response_model=schemas.InternalRoomOut,
    dependencies=[Depends(require_service)],
    tags=["internal"],
)
async def internal_room(code: str, c: ContainerDep) -> schemas.InternalRoomOut:
    """Участники с внешними id — чтобы бот знал, кому в Telegram отправить уведомление."""
    room, users = await c.rooms.participants(code)
    return schemas.InternalRoomOut(
        code=room.code,
        status=room.status,
        owner_id=room.owner_id,
        participants=[
            schemas.ExternalParticipantOut(
                user_id=p.user_id,
                display_name=p.display_name,
                active=p.active,
                provider=users[p.user_id].provider if p.user_id in users else None,
                external_id=users[p.user_id].external_id if p.user_id in users else None,
            )
            for p in room.participants.values()
        ],
    )
