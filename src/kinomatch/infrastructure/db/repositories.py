"""PostgreSQL-реализации репозиториев. Маппинг строк БД в доменные объекты — явный."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from kinomatch.domain.events import DomainEvent
from kinomatch.domain.models import (
    Match,
    Movie,
    Participant,
    Room,
    RoomFilters,
    RoomStatus,
    Swipe,
    User,
)
from kinomatch.infrastructure.db.tables import (
    MatchRow,
    MovieRow,
    OutboxRow,
    ParticipantRow,
    RoomRow,
    SwipeRow,
    UserRow,
)

UPSERT_BATCH = 1000


def _movie(row: MovieRow) -> Movie:
    return Movie(
        id=row.id,
        title=row.title,
        year=row.year,
        genres=frozenset(row.genres or ()),
        popularity=row.popularity,
        duration_min=row.duration_min,
        kinopoisk_id=row.kinopoisk_id,
        wikidata_id=row.wikidata_id,
        description=row.description,
    )


def _user(row: UserRow) -> User:
    return User(
        id=row.id,
        display_name=row.display_name,
        created_at=row.created_at,
        provider=row.provider,
        external_id=row.external_id,
    )


def filters_to_json(filters: RoomFilters) -> dict[str, Any]:
    return {
        "genres": sorted(filters.genres),
        "year_from": filters.year_from,
        "year_to": filters.year_to,
        "max_duration": filters.max_duration,
    }


def filters_from_json(data: dict[str, Any]) -> RoomFilters:
    return RoomFilters(
        genres=frozenset(data.get("genres") or ()),
        year_from=data.get("year_from"),
        year_to=data.get("year_to"),
        max_duration=data.get("max_duration"),
    )


class SqlMovieRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def get(self, movie_id: int) -> Movie | None:
        row = await self._s.get(MovieRow, movie_id)
        return _movie(row) if row else None

    async def get_many(self, ids: Collection[int]) -> dict[int, Movie]:
        if not ids:
            return {}
        rows = await self._s.scalars(select(MovieRow).where(MovieRow.id.in_(list(ids))))
        return {row.id: _movie(row) for row in rows}

    async def find_candidate_ids(self, filters: RoomFilters, limit: int) -> list[int]:
        stmt = select(MovieRow.id)
        if filters.genres:
            stmt = stmt.where(MovieRow.genres.overlap(sorted(filters.genres)))  # GIN-индекс
        if filters.year_from is not None:
            stmt = stmt.where(MovieRow.year >= filters.year_from)
        if filters.year_to is not None:
            stmt = stmt.where(MovieRow.year <= filters.year_to)
        if filters.max_duration is not None:
            stmt = stmt.where(
                or_(MovieRow.duration_min.is_(None), MovieRow.duration_min <= filters.max_duration)
            )
        stmt = stmt.order_by(MovieRow.popularity.desc(), MovieRow.id).limit(limit)
        return list(await self._s.scalars(stmt))

    async def genre_counts(self) -> list[tuple[str, int]]:
        genre = func.unnest(MovieRow.genres).label("genre")
        sub = select(genre).subquery()
        stmt = (
            select(sub.c.genre, func.count())
            .group_by(sub.c.genre)
            .order_by(func.count().desc(), sub.c.genre)
        )
        return [(g, n) for g, n in (await self._s.execute(stmt)).all()]

    async def upsert_many(self, movies: Sequence[Movie]) -> int:
        # ON CONFLICT не умеет обновить одну строку дважды за запрос — убираем дубли заранее.
        movies = list({m.wikidata_id: m for m in movies}.values())
        for start in range(0, len(movies), UPSERT_BATCH):
            batch = movies[start : start + UPSERT_BATCH]
            stmt = insert(MovieRow).values(
                [
                    {
                        "wikidata_id": m.wikidata_id,
                        "kinopoisk_id": m.kinopoisk_id,
                        "title": m.title,
                        "year": m.year,
                        "duration_min": m.duration_min,
                        "genres": sorted(m.genres),
                        "popularity": m.popularity,
                        "description": m.description,
                    }
                    for m in batch
                ]
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[MovieRow.wikidata_id],
                set_={
                    col: stmt.excluded[col]
                    for col in (
                        "kinopoisk_id",
                        "title",
                        "year",
                        "duration_min",
                        "genres",
                        "popularity",
                        "description",
                    )
                },
            )
            await self._s.execute(stmt)
        return len(movies)


class SqlRoomRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session
        self._rows: dict[str, RoomRow] = {}  # загруженные в этой транзакции — для save()

    async def add(self, room: Room) -> None:
        row = RoomRow(
            code=room.code,
            owner_id=room.owner_id,
            status=room.status.value,
            filters=filters_to_json(room.filters),
            stop_on_match=room.stop_on_match,
            candidate_ids=list(room.candidate_ids),
            created_at=room.created_at,
            started_at=room.started_at,
            finished_at=room.finished_at,
            participants=[],
            swipes=[],
            matches=[],
        )
        self._s.add(row)
        self._rows[room.code] = row
        self._sync_children(row, room)
        await self._s.flush()
        room.id = row.id

    async def get(self, code: str, *, for_update: bool = False) -> Room | None:
        if for_update:
            # Сначала блокируем строку комнаты (SELECT ... FOR UPDATE): все изменения комнаты
            # идут через эту блокировку, поэтому параллельные свайпы выполняются по очереди.
            locked = await self._s.scalar(
                select(RoomRow.id).where(RoomRow.code == code).with_for_update()
            )
            if locked is None:
                return None
        stmt = (
            select(RoomRow)
            .where(RoomRow.code == code)
            .options(
                selectinload(RoomRow.participants),
                selectinload(RoomRow.swipes),
                selectinload(RoomRow.matches),
            )
            .execution_options(populate_existing=True)
        )
        row = await self._s.scalar(stmt)
        if row is None:
            return None
        self._rows[code] = row
        return self._to_domain(row)

    async def save(self, room: Room) -> None:
        row = self._rows[room.code]
        row.owner_id = room.owner_id
        row.status = room.status.value
        row.candidate_ids = list(room.candidate_ids)
        row.started_at = room.started_at
        row.finished_at = room.finished_at
        self._sync_children(row, room)
        await self._s.flush()

    async def exists(self, code: str) -> bool:
        return await self._s.scalar(select(RoomRow.id).where(RoomRow.code == code)) is not None

    @staticmethod
    def _sync_children(row: RoomRow, room: Room) -> None:
        """Дописываем новые свайпы/совпадения и обновляем участников. Свайпы неизменяемы,
        поэтому достаточно добавить те, которых ещё нет в строке."""
        participants = {p.user_id: p for p in row.participants}
        for p in room.participants.values():
            existing = participants.get(p.user_id)
            if existing is None:
                row.participants.append(
                    ParticipantRow(
                        user_id=p.user_id,
                        display_name=p.display_name,
                        joined_at=p.joined_at,
                        active=p.active,
                    )
                )
            else:
                existing.active = p.active
                existing.display_name = p.display_name

        known_swipes = {(s.user_id, s.movie_id) for s in row.swipes}
        for key, swipe in room.swipes.items():
            if key not in known_swipes:
                row.swipes.append(
                    SwipeRow(
                        user_id=swipe.user_id,
                        movie_id=swipe.movie_id,
                        liked=swipe.liked,
                        created_at=swipe.created_at,
                    )
                )

        known_matches = {m.movie_id for m in row.matches}
        for movie_id, match in room.matches.items():
            if movie_id not in known_matches:
                row.matches.append(MatchRow(movie_id=movie_id, created_at=match.created_at))

    @staticmethod
    def _to_domain(row: RoomRow) -> Room:
        return Room(
            id=row.id,
            code=row.code,
            owner_id=row.owner_id,
            filters=filters_from_json(row.filters),
            created_at=row.created_at,
            stop_on_match=row.stop_on_match,
            status=RoomStatus(row.status),
            candidate_ids=list(row.candidate_ids or ()),
            participants={
                p.user_id: Participant(p.user_id, p.display_name, p.joined_at, p.active)
                for p in sorted(row.participants, key=lambda p: p.joined_at)
            },
            swipes={
                (s.user_id, s.movie_id): Swipe(s.user_id, s.movie_id, s.liked, s.created_at)
                for s in row.swipes
            },
            matches={
                m.movie_id: Match(m.movie_id, m.created_at)
                for m in sorted(row.matches, key=lambda m: m.created_at)
            },
            started_at=row.started_at,
            finished_at=row.finished_at,
        )


class SqlUserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(self, *, display_name: str, token_hash: str | None, now: datetime) -> User:
        row = UserRow(display_name=display_name, token_hash=token_hash, created_at=now)
        self._s.add(row)
        await self._s.flush()
        return _user(row)

    async def by_token_hash(self, token_hash: str) -> User | None:
        row = await self._s.scalar(select(UserRow).where(UserRow.token_hash == token_hash))
        return _user(row) if row else None

    async def get_or_create_external(
        self, *, provider: str, external_id: str, display_name: str, now: datetime
    ) -> User:
        # Один запрос без гонок: если пользователь уже есть — обновим имя и вернём его.
        stmt = (
            insert(UserRow)
            .values(
                provider=provider,
                external_id=external_id,
                display_name=display_name,
                created_at=now,
            )
            .on_conflict_do_update(
                constraint="uq_users_external",
                set_={"display_name": display_name},
            )
            .returning(UserRow)
            .execution_options(populate_existing=True)
        )
        row = await self._s.scalar(stmt)
        assert row is not None
        return _user(row)

    async def get_many(self, ids: Collection[int]) -> dict[int, User]:
        if not ids:
            return {}
        rows = await self._s.scalars(select(UserRow).where(UserRow.id.in_(list(ids))))
        return {row.id: _user(row) for row in rows}


class SqlOutbox:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def put(self, events: Sequence[DomainEvent], now: datetime) -> None:
        for event in events:
            self._s.add(OutboxRow(event_type=event.type, payload=event.payload(), created_at=now))
