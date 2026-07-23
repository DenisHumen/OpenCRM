import secrets
from datetime import timedelta

from sqlalchemy.orm import Session

from config.settings import get_settings
from core import exceptions as errors
from core.security import passwords, tokens
from core.utils import is_valid_email, normalize_email, now_utc
from database.models import User
from database.models.user import (
    ROLE_MANAGER,
    ROLE_ROOT,
    STATUS_ACTIVE,
    STATUS_DISABLED,
    STATUS_PENDING,
    LOCALES,
)
from database.repositories import users as users_repo


def register(db: Session, name: str, email: str, password: str) -> User:
    email = normalize_email(email)
    if not is_valid_email(email):
        raise errors.ValidationError("Invalid email", code="invalid_email")
    if not passwords.is_valid_password(password):
        raise errors.ValidationError(
            f"Password must be at least {passwords.MIN_PASSWORD_LENGTH} characters",
            code="weak_password",
        )
    if not name.strip():
        raise errors.ValidationError("Name is required", code="name_required")
    if users_repo.get_by_email(db, email) is not None:
        raise errors.ConflictError("Email already registered", code="email_taken")
    user = User(
        email=email,
        name=name.strip(),
        password_hash=passwords.hash_password(password),
        role=ROLE_MANAGER,
        status=STATUS_PENDING,
    )
    db.add(user)
    db.flush()
    return user


def login(db: Session, email: str, password: str, limiter) -> tuple[User, str]:
    email = normalize_email(email)
    if limiter.is_blocked(email):
        raise errors.RateLimitedError("Too many attempts, try later", code="login_rate_limited")
    user = users_repo.get_by_email(db, email)
    if user is None or not passwords.verify_password(password, user.password_hash):
        limiter.record_failure(email)
        raise errors.AuthError("Invalid email or password", code="invalid_credentials")
    if user.status == STATUS_PENDING:
        raise errors.ForbiddenError("Account is waiting for approval", code="account_pending")
    if user.status == STATUS_DISABLED:
        raise errors.ForbiddenError("Account is disabled", code="account_disabled")
    limiter.reset(email)

    token = tokens.new_session_token()
    ttl = timedelta(days=get_settings().session_ttl_days)
    users_repo.create_session(db, user.id, tokens.sha256_hex(token), now_utc() + ttl)
    return user, token


def logout(db: Session, token: str) -> None:
    session = users_repo.get_session_by_hash(db, tokens.sha256_hex(token))
    if session is not None:
        users_repo.delete_session(db, session)


def get_user_by_session(db: Session, token: str) -> User | None:
    session = users_repo.get_session_by_hash(db, tokens.sha256_hex(token))
    if session is None or session.expires_at < now_utc():
        return None
    user = users_repo.get_by_id(db, session.user_id)
    if user is None or user.status != STATUS_ACTIVE:
        return None
    # обновляем last_seen не чаще раза в час, чтобы не писать на каждый запрос
    if session.last_seen_at is None or (now_utc() - session.last_seen_at).total_seconds() > 3600:
        session.last_seen_at = now_utc()
    return user


def change_password(
    db: Session, user: User, old_password: str, new_password: str, current_token: str | None = None
) -> None:
    if not passwords.verify_password(old_password, user.password_hash):
        raise errors.ValidationError("Current password is incorrect", code="wrong_password")
    if not passwords.is_valid_password(new_password):
        raise errors.ValidationError(
            f"Password must be at least {passwords.MIN_PASSWORD_LENGTH} characters",
            code="weak_password",
        )
    user.password_hash = passwords.hash_password(new_password)
    user.must_change_password = False
    # смена пароля выкидывает все прочие сессии (например, угнанную): доступ по
    # старым cookie должен прекратиться сразу. Текущую сессию сохраняем.
    if current_token:
        users_repo.delete_other_sessions(db, user.id, tokens.sha256_hex(current_token))
    else:
        users_repo.delete_sessions_for_user(db, user.id)


def update_profile(db: Session, user: User, name: str | None = None, locale: str | None = None) -> User:
    if name is not None:
        if not name.strip():
            raise errors.ValidationError("Name is required", code="name_required")
        user.name = name.strip()
    if locale is not None:
        if locale not in LOCALES:
            raise errors.ValidationError("Unsupported locale", code="bad_locale")
        user.locale = locale
    return user


# --- операции root над сотрудниками ---

def _get_manager(db: Session, user_id: int) -> User:
    user = users_repo.get_by_id(db, user_id)
    if user is None:
        raise errors.NotFoundError("User not found", code="user_not_found")
    if user.role == ROLE_ROOT:
        raise errors.ForbiddenError("Cannot modify root account", code="cannot_modify_root")
    return user


def approve(db: Session, user_id: int) -> User:
    user = _get_manager(db, user_id)
    if user.status != STATUS_PENDING:
        raise errors.ConflictError("Account is not pending", code="not_pending")
    user.status = STATUS_ACTIVE
    user.approved_at = now_utc()
    return user


def reject(db: Session, user_id: int) -> None:
    user = _get_manager(db, user_id)
    if user.status != STATUS_PENDING:
        raise errors.ConflictError("Account is not pending", code="not_pending")
    db.delete(user)


def disable(db: Session, user_id: int) -> User:
    user = _get_manager(db, user_id)
    user.status = STATUS_DISABLED
    users_repo.delete_sessions_for_user(db, user.id)
    return user


def enable(db: Session, user_id: int) -> User:
    user = _get_manager(db, user_id)
    if user.status != STATUS_DISABLED:
        raise errors.ConflictError("Account is not disabled", code="not_disabled")
    user.status = STATUS_ACTIVE
    return user


def reset_password(db: Session, user_id: int) -> tuple[User, str]:
    user = _get_manager(db, user_id)
    temp_password = secrets.token_urlsafe(9)
    user.password_hash = passwords.hash_password(temp_password)
    user.must_change_password = True
    users_repo.delete_sessions_for_user(db, user.id)
    return user, temp_password


# --- bootstrap ---

def bootstrap_root(db: Session) -> User | None:
    """Создаёт root-аккаунт при первом запуске. Возвращает его, если создал."""
    if users_repo.get_root(db) is not None:
        return None
    settings = get_settings()
    root = User(
        email=normalize_email(settings.root_email),
        name="Root",
        password_hash=passwords.hash_password(settings.root_password),
        role=ROLE_ROOT,
        status=STATUS_ACTIVE,
        must_change_password=True,
    )
    db.add(root)
    db.flush()
    return root
