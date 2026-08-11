class DomainError(Exception):
    """Базовая доменная ошибка. Слой web превращает её в HTTP-ответ."""

    http_status = 400
    code = "bad_request"

    def __init__(
        self,
        message: str | None = None,
        code: str | None = None,
        details: dict | None = None,
    ):
        self.message = message or self.__class__.__name__
        if code:
            self.code = code
        #: Подробности отказа для экрана. Пусто у подавляющего большинства
        #: ошибок, и это правильно: код и текст отвечают на вопрос «что не так».
        #:
        #: Заведены ради отказов, после которых человек обязан ВЫБРАТЬ. «Клиент
        #: нашёлся не один» без списка кандидатов — тупик: экран знает, что не
        #: так, и не знает, что предложить. Второй ручкой «а покажи кандидатов»
        #: это не решается — она отвечала бы на другой запрос и могла бы найти
        #: уже других.
        #:
        #: Кладём сюда только то, что и так вернул бы обычный ответ. Отказ — не
        #: лазейка мимо прав: `web/api/deps.require_perm` отрабатывает раньше,
        #: чем сервис доходит до этой строки.
        self.details = details or {}
        super().__init__(self.message)


class NotFoundError(DomainError):
    http_status = 404
    code = "not_found"


class ConflictError(DomainError):
    http_status = 409
    code = "conflict"


class ValidationError(DomainError):
    http_status = 422
    code = "validation_error"


class AuthError(DomainError):
    http_status = 401
    code = "unauthorized"


class ForbiddenError(DomainError):
    http_status = 403
    code = "forbidden"


class RateLimitedError(DomainError):
    http_status = 429
    code = "rate_limited"
