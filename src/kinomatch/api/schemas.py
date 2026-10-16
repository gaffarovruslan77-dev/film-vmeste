"""Контракты HTTP API (то, что видят клиенты). Отделены от доменных моделей."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, model_validator

from kinomatch.application.services import RoomResults
from kinomatch.domain.models import Movie, Room, RoomFilters, RoomStatus, User
from kinomatch.domain.recommender import Reason, Recommendation


class ErrorBody(BaseModel):
    code: str
    message: str


class ErrorOut(BaseModel):
    error: ErrorBody


class GuestIn(BaseModel):
    display_name: str = Field(min_length=1, max_length=64)


class TokenOut(BaseModel):
    user_id: int
    token: str


class UserOut(BaseModel):
    id: int
    display_name: str

    @classmethod
    def of(cls, user: User) -> UserOut:
        return cls(id=user.id, display_name=user.display_name)


class GenreOut(BaseModel):
    name: str
    movies: int


class MovieOut(BaseModel):
    id: int
    title: str
    year: int | None
    genres: list[str]
    duration_min: int | None
    kinopoisk_url: str | None
    description: str | None

    @classmethod
    def of(cls, movie: Movie) -> MovieOut:
        return cls(
            id=movie.id,
            title=movie.title,
            year=movie.year,
            genres=sorted(movie.genres),
            duration_min=movie.duration_min,
            kinopoisk_url=movie.kinopoisk_url,
            description=movie.description,
        )


class FiltersIn(BaseModel):
    genres: list[str] = Field(default_factory=list, max_length=20)
    year_from: int | None = Field(default=None, ge=1890, le=2100)
    year_to: int | None = Field(default=None, ge=1890, le=2100)
    max_duration: int | None = Field(default=None, ge=30, le=400)

    @model_validator(mode="after")
    def check_years(self) -> FiltersIn:
        if self.year_from and self.year_to and self.year_from > self.year_to:
            raise ValueError("year_from больше year_to")
        return self

    def to_domain(self) -> RoomFilters:
        return RoomFilters(
            genres=frozenset(self.genres),
            year_from=self.year_from,
            year_to=self.year_to,
            max_duration=self.max_duration,
        )


class CreateRoomIn(BaseModel):
    filters: FiltersIn = Field(default_factory=FiltersIn)
    stop_on_match: bool = True


class ParticipantOut(BaseModel):
    user_id: int
    display_name: str
    active: bool


class RoomOut(BaseModel):
    code: str
    status: RoomStatus
    owner_id: int
    filters: FiltersIn
    stop_on_match: bool
    participants: list[ParticipantOut]
    candidates: int
    match_ids: list[int]
    created_at: datetime

    @classmethod
    def of(cls, room: Room) -> RoomOut:
        return cls(
            code=room.code,
            status=room.status,
            owner_id=room.owner_id,
            filters=FiltersIn(
                genres=sorted(room.filters.genres),
                year_from=room.filters.year_from,
                year_to=room.filters.year_to,
                max_duration=room.filters.max_duration,
            ),
            stop_on_match=room.stop_on_match,
            participants=[
                ParticipantOut(user_id=p.user_id, display_name=p.display_name, active=p.active)
                for p in room.participants.values()
            ],
            candidates=len(room.candidate_ids),
            match_ids=list(room.matches),
            created_at=room.created_at,
        )


class CardOut(BaseModel):
    movie: MovieOut
    reason: Reason
    liked_by_others: int

    @classmethod
    def of(cls, rec: Recommendation) -> CardOut:
        return cls(
            movie=MovieOut.of(rec.movie), reason=rec.reason, liked_by_others=rec.liked_by_others
        )


class NextCardOut(BaseModel):
    card: CardOut | None = Field(description="null — фильмы в колоде закончились")


class SwipeIn(BaseModel):
    movie_id: int
    liked: bool


class SwipeOut(BaseModel):
    match: MovieOut | None
    room_status: RoomStatus


class RankedMovieOut(BaseModel):
    movie: MovieOut
    likes: int
    dislikes: int


class ResultsOut(BaseModel):
    status: RoomStatus
    matches: list[MovieOut]
    top: list[RankedMovieOut]

    @classmethod
    def of(cls, results: RoomResults) -> ResultsOut:
        return cls(
            status=results.status,
            matches=[MovieOut.of(m) for m in results.matches],
            top=[
                RankedMovieOut(
                    movie=MovieOut.of(r.movie), likes=r.tally.likes, dislikes=r.tally.dislikes
                )
                for r in results.top
            ],
        )


class ExternalParticipantOut(BaseModel):
    user_id: int
    display_name: str
    active: bool
    provider: str | None
    external_id: str | None


class InternalRoomOut(BaseModel):
    code: str
    status: RoomStatus
    owner_id: int
    participants: list[ExternalParticipantOut]
