from fastapi import Depends, Request
from sqlalchemy.orm import Session

from config.settings import get_settings
from core import exceptions as errors
from core.ratelimit import SlidingWindowLimiter
from core.services import auth_service
from database.models import User
from database.models.user import ROLE_ROOT
from database.session import SessionLocal

SESSION_COOKIE = "opencrm_session"
CSRF_COOKIE = "opencrm_csrf"
CSRF_HEADER = "x-csrf-token"

_settings = get_settings()
login_limiter = SlidingWindowLimiter(
    _settings.login_max_attempts, _settings.login_lockout_minutes * 60
)
pin_limiter = SlidingWindowLimiter(
    _settings.pin_max_attempts, _settings.pin_lockout_minutes * 60
)


def get_db():
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    """Аутентифицированный активный сотрудник (смена пароля может требоваться)."""
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise errors.AuthError("Not authenticated", code="not_authenticated")
    user = auth_service.get_user_by_session(db, token)
    if user is None:
        raise errors.AuthError("Session is invalid or expired", code="session_invalid")
    return user


def require_staff(user: User = Depends(get_current_user)) -> User:
    """Обычные рабочие эндпоинты: доступ закрыт, пока не сменён временный пароль."""
    if user.must_change_password:
        raise errors.ForbiddenError(
            "Password change required before continuing", code="password_change_required"
        )
    return user


def require_root(user: User = Depends(require_staff)) -> User:
    if user.role != ROLE_ROOT:
        raise errors.ForbiddenError("Root access required", code="root_required")
    return user


def client_ip(request: Request) -> str:
    """Реальный IP клиента с поправкой на доверенные обратные прокси.

    X-Forwarded-For нельзя доверять целиком: клиент может прислать любой префикс.
    nginx дописывает настоящий адрес клиента СПРАВА (``$proxy_add_x_forwarded_for``),
    поэтому берём элемент, добавленный НАШИМ прокси (hops-й с конца), а не левый.
    Без прокси (``trusted_proxy_hops = 0``) заголовок игнорируется полностью —
    используется адрес TCP-пира, который подделать нельзя.

    Важно: uvicorn должен работать с ``--no-proxy-headers`` (docker/entrypoint.sh),
    иначе он сам перепишет ``request.client`` по X-Forwarded-For ещё до этой функции,
    и адрес пира окажется подделан. Тогда определение IP — целиком за нами.

    От этого зависит защита подбора PIN: раньше ротация X-Forwarded-For давала
    атакующему новый бакет rate-limit на каждый запрос и снимала ограничение.
    """
    hops = _settings.trusted_proxy_hops
    if hops > 0:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            parts = [p.strip() for p in forwarded.split(",") if p.strip()]
            if len(parts) >= hops:
                return parts[-hops]
    return request.client.host if request.client else "unknown"
