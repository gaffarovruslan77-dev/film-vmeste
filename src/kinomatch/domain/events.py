"""Доменные события. Их пишет агрегат Room, а наружу (в Redis) их доставляет outbox."""

from dataclasses import asdict, dataclass
from typing import Any, ClassVar


@dataclass(frozen=True, slots=True)
class DomainEvent:
    type: ClassVar[str] = "event"
    room_code: str

    def payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ParticipantJoined(DomainEvent):
    type: ClassVar[str] = "participant_joined"
    user_id: int
    display_name: str


@dataclass(frozen=True, slots=True)
class ParticipantLeft(DomainEvent):
    type: ClassVar[str] = "participant_left"
    user_id: int
    display_name: str


@dataclass(frozen=True, slots=True)
class VotingStarted(DomainEvent):
    type: ClassVar[str] = "voting_started"
    candidates: int


@dataclass(frozen=True, slots=True)
class MatchFound(DomainEvent):
    type: ClassVar[str] = "match_found"
    movie_id: int


@dataclass(frozen=True, slots=True)
class RoomFinished(DomainEvent):
    type: ClassVar[str] = "room_finished"
    reason: str  # "match" | "owner" | "empty"
