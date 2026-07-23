class DomainError(Exception):
    """Базовая доменная ошибка. Слой web превращает её в HTTP-ответ."""

    http_status = 400
    code = "bad_request"

    def __init__(self, message: str | None = None, code: str | None = None):
        self.message = message or self.__class__.__name__
        if code:
            self.code = code
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
