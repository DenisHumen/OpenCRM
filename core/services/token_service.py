"""Токены сотрудников к `/api/v1`: выдача, отзыв, вход, запретные места.

Разбор — `docs/bloki/30-tokeny-i-mcp.md`. Коротко: токен действует правами своего
сотрудника; в базе только отпечаток; отзыв — отметка; куда токену нельзя —
список `ZAKRYTO` ниже, а не права роли.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from sqlalchemy.orm import Session

from core import exceptions as errors
from core.ratelimit import SlidingWindowLimiter
from core.services import audit_service, auth_service, permissions_service
from core.utils import PRESENCE_TOUCH_SECONDS, now_utc
from database.models import User, UserToken
from database.models.audit import SOURCE_MANUAL
from database.models.user import STATUS_ACTIVE
from database.repositories import user_tokens as tokens_repo
from database.repositories import users as users_repo

#: Приставка отличает наш токен в конфиге агента и в сканерах утечек от прочих строк.
PRIPISKA = "ocrm_"
PREFIX_LEN = 12
SROKI = (30, 90, 180, 365, 0)
SROK_UMOLCHANIE = 90
CHTENIE = frozenset({"GET", "HEAD", "OPTIONS"})

#: Потолок запросов одного токена в минуту. Угадывать токен бессмысленно (256 бит),
#: а вот агент в цикле — настоящая беда: так уже валилось боевое обновление.
V_MINUTU = 300
limiter = SlidingWindowLimiter(V_MINUTU, 60, name="token")

_PISHUSHCHIE = frozenset({"POST", "PATCH", "PUT", "DELETE"})

#: Куда токену нельзя, даже если права роли пускают: `(методы или None — все, шаблон пути)`,
#: `*` в конце — со всем, что ниже. Выдача доступов, сейф паролей, копия базы и
#: безвозвратное удаление — только рукой человека: пароль, попавший агенту, уже ушёл.
ZAKRYTO: tuple[tuple[frozenset[str] | None, str], ...] = (
    (None, "/tokens*"),
    (None, "/keys*"),
    (None, "/system/backups*"),
    (None, "/settings/api-keys*"),
    # Раздача прав: роли и должности, root, допуск нового сотрудника.
    (frozenset({"POST", "PATCH", "PUT", "DELETE"}), "/roles*"),
    (None, "/staff/{user_id}/role"),
    (None, "/staff/{user_id}/approve"),
    (None, "/system/storage/purge"),
    (frozenset({"DELETE"}), "/system/files/{work_id}"),
    (None, "/staff/{user_id}/reset-password"),
    (frozenset({"DELETE"}), "/staff/{user_id}"),
    (frozenset({"POST", "PATCH", "PUT", "DELETE"}), "/auth*"),
    # Поток перепроверяет cookie сессии на каждой доставке — токену он закрылся бы молча.
    (None, "/live"),
)


def _otpechatok(raw: str) -> str:
    return hashlib.sha256(raw.encode("ascii")).hexdigest()


def pod_zapretom(zapret, method: str, shablon: str) -> bool:
    metody, put = zapret
    if metody is not None and method not in metody:
        return False
    if put.endswith("*"):
        return shablon == put[:-1] or shablon.startswith(put[:-1] + "/")
    return shablon == put


def zakryto(method: str, shablon: str) -> bool:
    return any(pod_zapretom(zapret, method, shablon) for zapret in ZAKRYTO)


def sostoyanie(tok: UserToken, now=None) -> str:
    now = now or now_utc()
    if tok.revoked_at is not None:
        return "revoked"
    if tok.expires_at is not None and tok.expires_at <= now:
        return "expired"
    return "active"


def token_out(tok: UserToken, user: User) -> dict:
    return {
        "id": tok.id,
        "name": tok.name,
        "prefix": tok.prefix,
        "user_id": user.id,
        "user_name": user.name,
        "user_email": user.email,
        "tolko_chtenie": tok.tolko_chtenie,
        "created_at": tok.created_at.isoformat() if tok.created_at else None,
        "created_by": tok.created_by,
        "expires_at": tok.expires_at.isoformat() if tok.expires_at else None,
        "last_used_at": tok.last_used_at.isoformat() if tok.last_used_at else None,
        "revoked_at": tok.revoked_at.isoformat() if tok.revoked_at else None,
        "state": sostoyanie(tok),
    }


def spisok(db: Session) -> list[dict]:
    return [token_out(tok, user) for tok, user in tokens_repo.spisok(db)]


def zhivyh(db: Session) -> int:
    return tokens_repo.zhivyh(db, now_utc())


def vypustit(db: Session, actor: User, data: dict) -> tuple[dict, str]:
    """Выдать токен. Строка токена — в ответе и больше нигде."""
    name = (data.get("name") or "").strip()
    if not name:
        raise errors.ValidationError("Name is required", code="name_required")
    dni = data.get("days", SROK_UMOLCHANIE)
    if dni not in SROKI:
        raise errors.ValidationError(
            f"Term must be one of {', '.join(map(str, SROKI))} days", code="bad_term"
        )
    user = users_repo.get_by_id(db, data.get("user_id") or actor.id)
    if user is None or user.status != STATUS_ACTIVE:
        raise errors.ValidationError("The employee is not active", code="user_not_active")
    permissions_service.ne_shire_sebya(db, actor, user)
    raw = PRIPISKA + secrets.token_urlsafe(32)
    tok = tokens_repo.dobavit(
        db,
        UserToken(
            user_id=user.id,
            name=name[:100],
            prefix=raw[:PREFIX_LEN],
            token_hash=_otpechatok(raw),
            tolko_chtenie=bool(data.get("tolko_chtenie")),
            created_by=actor.id,
            expires_at=(now_utc() + timedelta(days=dni)) if dni else None,
        ),
    )
    audit_service.record(
        db,
        actor=actor,
        source=SOURCE_MANUAL,
        action=audit_service.ACTION_TOKEN_CREATED,
        entity_type=audit_service.ENTITY_TOKEN,
        entity_id=tok.id,
        entity_label=tok.name,
        after=f"{user.name} <{user.email}>" + (", read-only" if tok.tolko_chtenie else ""),
    )
    return token_out(tok, user), raw


def otozvat(db: Session, actor: User, token_id: int) -> dict:
    tok = tokens_repo.get(db, token_id)
    if tok is None:
        raise errors.NotFoundError("Token not found", code="token_not_found")
    if tok.revoked_at is None:
        tok.revoked_at = now_utc()
        db.flush()
        audit_service.record(
            db,
            actor=actor,
            source=SOURCE_MANUAL,
            action=audit_service.ACTION_TOKEN_REVOKED,
            entity_type=audit_service.ENTITY_TOKEN,
            entity_id=tok.id,
            entity_label=tok.name,
        )
    return token_out(tok, users_repo.get_by_id(db, tok.user_id))


# --- вход по токену ------------------------------------------------------------


def voyti(db: Session, raw: str, method: str, shablon: str) -> tuple[UserToken, User]:
    """Строка из `Authorization: Bearer` → токен и сотрудник, или отказ."""
    raw = (raw or "").strip()
    # ASCII — до хэширования: заголовок Starlette читает как latin-1.
    if not raw.startswith(PRIPISKA) or not raw.isascii() or len(raw) > 128:
        raise errors.AuthError("Bad token", code="bad_token")
    para = tokens_repo.s_hozyainom(db, _otpechatok(raw))
    if para is None:
        raise errors.AuthError("Bad token", code="bad_token")
    tok, user = para
    now = now_utc()
    if tok.revoked_at is not None:
        raise errors.AuthError("Token is revoked", code="token_revoked")
    if tok.expires_at is not None and tok.expires_at <= now:
        raise errors.AuthError("Token has expired", code="token_expired")
    if user.status != STATUS_ACTIVE:
        raise errors.AuthError("The token's employee is not active", code="token_user_inactive")
    if zakryto(method, shablon):
        raise errors.ForbiddenError("Not available with a token", code="token_not_allowed")
    if tok.tolko_chtenie and method not in CHTENIE:
        raise errors.ForbiddenError("The token is read-only", code="token_read_only")
    if limiter.proverit_i_zanyat(str(tok.id)):
        raise errors.RateLimitedError(
            f"Token limit is {V_MINUTU} requests per minute", code="token_rate_limited"
        )
    if tok.last_used_at is None or (now - tok.last_used_at).total_seconds() >= PRESENCE_TOUCH_SECONDS:
        db.info[OTMETKA] = (tok.id, now)
    auth_service.poprosit_prisutstvie(db, user, now)
    db.info[audit_service.PO_TOKENU] = (tok.id, tok.name)
    return tok, user


#: Ключ `db.info`: отметить обращение вплотную к фиксации, как присутствие (`web/api/deps`).
OTMETKA = "opencrm_token_otmetka"


def zapisat_otmetku(db: Session) -> None:
    prosba = db.info.pop(OTMETKA, None)
    if prosba is not None:
        tokens_repo.otmetit(db, *prosba)
