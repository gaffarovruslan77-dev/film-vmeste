class DomainError(Exception):
    """Базовая ошибка бизнес-правил. `code` — машиночитаемый идентификатор для клиентов."""

    code = "domain_error"

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.__doc__ or self.code)


class RoomNotFound(DomainError):
    """Комната не найдена."""

    code = "room_not_found"


class MovieNotFound(DomainError):
    """Фильм не найден."""

    code = "movie_not_found"


class NotRoomOwner(DomainError):
    """Это действие доступно только создателю комнаты."""

    code = "not_room_owner"


class NotParticipant(DomainError):
    """Вы не участник этой комнаты."""

    code = "not_participant"


class InvalidRoomState(DomainError):
    """Действие недоступно в текущем состоянии комнаты."""

    code = "invalid_room_state"


class NotEnoughParticipants(DomainError):
    """Недостаточно участников, чтобы начать голосование."""

    code = "not_enough_participants"


class NoCandidates(DomainError):
    """Под выбранные фильтры не нашлось ни одного фильма."""

    code = "no_candidates"


class MovieNotInRoom(DomainError):
    """Этого фильма нет в подборке комнаты."""

    code = "movie_not_in_room"


class Unauthorized(DomainError):
    """Требуется авторизация."""

    code = "unauthorized"
