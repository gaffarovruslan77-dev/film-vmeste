import pytest

from kinomatch.domain import errors
from kinomatch.domain.events import MatchFound, ParticipantJoined, RoomFinished, VotingStarted
from kinomatch.domain.models import RoomStatus
from tests.factories import NOW, make_room, make_user


def test_match_requires_all_active_participants() -> None:
    room = make_room(1, 2, 3, candidates=[10, 11])

    assert room.swipe(1, 10, liked=True, now=NOW) is None
    assert room.swipe(2, 10, liked=True, now=NOW) is None
    match = room.swipe(3, 10, liked=True, now=NOW)

    assert match is not None and match.movie_id == 10
    assert room.status is RoomStatus.FINISHED
    assert [type(e) for e in room.pull_events()] == [MatchFound, RoomFinished]


def test_one_dislike_blocks_match() -> None:
    room = make_room(1, 2, candidates=[10])
    room.swipe(1, 10, liked=True, now=NOW)
    assert room.swipe(2, 10, liked=False, now=NOW) is None
    assert room.matches == {}


def test_repeated_swipe_is_idempotent() -> None:
    room = make_room(1, 2, candidates=[10])
    room.swipe(1, 10, liked=False, now=NOW)
    room.swipe(1, 10, liked=True, now=NOW)  # двойной клик — первый голос остаётся
    assert room.swipes[(1, 10)].liked is False


def test_continue_after_match_when_stop_disabled() -> None:
    room = make_room(1, 2, candidates=[10, 11], stop_on_match=False)
    for uid in (1, 2):
        room.swipe(uid, 10, liked=True, now=NOW)
    assert room.status is RoomStatus.VOTING
    assert set(room.matches) == {10}


def test_leaving_participant_can_create_match() -> None:
    room = make_room(1, 2, 3, candidates=[10])
    room.swipe(1, 10, liked=True, now=NOW)
    room.swipe(2, 10, liked=True, now=NOW)

    room.leave(3, NOW)  # третий так и не проголосовал — теперь все оставшиеся «за»

    assert 10 in room.matches


def test_owner_leaving_passes_ownership() -> None:
    room = make_room(1, 2)
    room.leave(1, NOW)
    assert room.owner_id == 2


def test_last_participant_leaving_closes_room() -> None:
    room = make_room(1)
    room.leave(1, NOW)
    assert room.status is RoomStatus.FINISHED


def test_only_owner_can_start() -> None:
    room = make_room(1, 2)
    with pytest.raises(errors.NotRoomOwner):
        room.start(2, [10], min_participants=1, now=NOW)


def test_start_requires_enough_participants() -> None:
    room = make_room(1)
    with pytest.raises(errors.NotEnoughParticipants):
        room.start(1, [10], min_participants=2, now=NOW)


def test_start_emits_event() -> None:
    room = make_room(1, 2)
    room.start(1, [10, 11, 10], min_participants=2, now=NOW)
    assert room.candidate_ids == [10, 11]
    assert room.pull_events() == [VotingStarted("ROOM42", 2)]


def test_cannot_swipe_movie_outside_pool() -> None:
    room = make_room(1, 2, candidates=[10])
    with pytest.raises(errors.MovieNotInRoom):
        room.swipe(1, 99, liked=True, now=NOW)


def test_non_participant_cannot_swipe() -> None:
    room = make_room(1, 2, candidates=[10])
    with pytest.raises(errors.NotParticipant):
        room.swipe(7, 10, liked=True, now=NOW)


def test_rejoin_is_idempotent() -> None:
    room = make_room(1, 2)
    room.join(make_user(2), NOW)
    assert room.pull_events() == []
    room.leave(2, NOW)
    room.join(make_user(2), NOW)
    assert isinstance(room.pull_events()[-1], ParticipantJoined)


def test_leaderboard_counts_only_active() -> None:
    room = make_room(1, 2, 3, candidates=[10, 11], stop_on_match=False)
    room.swipe(1, 10, liked=True, now=NOW)
    room.swipe(2, 10, liked=True, now=NOW)
    room.swipe(1, 11, liked=True, now=NOW)
    room.leave(2, NOW)

    top = room.leaderboard()
    assert [(t.movie_id, t.likes) for t in top] == [(10, 1), (11, 1)]
