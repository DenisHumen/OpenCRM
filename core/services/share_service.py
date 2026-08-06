from datetime import datetime

from sqlalchemy.orm import Session

from config.settings import get_settings
from core import exceptions as errors
from core.services import audit_service
from core.security import passwords, tokens
from core.utils import now_utc, to_utc_naive
from database.models import Board, ShareLink, ShareView, User
from database.repositories import boards as boards_repo
from database.repositories import shares as shares_repo

UNSET = object()


def get_share(db: Session, share_id: int) -> ShareLink:
    link = shares_repo.get(db, share_id)
    if link is None:
        raise errors.NotFoundError("Share link not found", code="share_not_found")
    return link


def create_share(
    db: Session,
    board: Board,
    author: User,
    expires_at: datetime | None = None,
    pin: str | None = None,
) -> ShareLink:
    link = ShareLink(
        board_id=board.id,
        token=tokens.new_share_token(),
        is_active=True,
        expires_at=_validate_expiry(expires_at),
        pin_hash=_hash_pin(pin) if pin else None,
        created_by=author.id,
    )
    db.add(link)
    db.flush()
    return link


def update_share(
    db: Session,
    share_id: int,
    is_active=UNSET,
    expires_at=UNSET,
    pin=UNSET,
) -> ShareLink:
    link = get_share(db, share_id)
    if is_active is not UNSET and is_active is not None:
        if link.is_active and not is_active:
            link.revoked_at = now_utc()
        if not link.is_active and is_active:
            link.revoked_at = None
        link.is_active = bool(is_active)
    if expires_at is not UNSET:
        link.expires_at = _validate_expiry(expires_at)
    if pin is not UNSET:
        link.pin_hash = _hash_pin(pin) if pin else None
    return link


def delete_share(db: Session, share_id: int, actor: User) -> None:
    """Удаляет ссылку вместе с журналом её просмотров.

    Файлы работ при этом НЕ трогаются: они принадлежат доске, а не ссылке —
    у доски может быть несколько ссылок (разным клиентам, с разными PIN),
    и удаление одной не должно ломать остальные. Место освобождает удаление
    доски + очистка корзины (core/services/maintenance_service.py).
    """
    link = get_share(db, share_id)
    # Снимок до удаления: у ссылки нет названия, зато есть доска, ради которой
    # её выдавали, — по ней вопрос и задают («почему у клиента перестало
    # открываться»). Токен в журнал не пишем: он и есть ключ к витрине.
    board = boards_repo.get(db, link.board_id, include_deleted=True)
    label = board.title if board is not None else f"доска {link.board_id}"
    link_id = link.id
    db.delete(link)  # share_views уходят каскадом (ondelete=CASCADE)
    db.flush()
    audit_service.record_deletion(
        db,
        actor=actor,
        entity_type=audit_service.ENTITY_SHARE,
        entity_id=link_id,
        entity_label=label,
    )


def regenerate(db: Session, share_id: int, author: User) -> ShareLink:
    """Отзывает текущую ссылку и создаёт новую с теми же настройками."""
    old = get_share(db, share_id)
    if old.is_active:
        old.is_active = False
        old.revoked_at = now_utc()
    new = ShareLink(
        board_id=old.board_id,
        token=tokens.new_share_token(),
        is_active=True,
        expires_at=old.expires_at,
        pin_hash=old.pin_hash,
        created_by=author.id,
    )
    db.add(new)
    db.flush()
    return new


def _validate_expiry(expires_at: datetime | None) -> datetime | None:
    """Срок жизни ссылки — к тому виду, в каком время лежит в базе: naive UTC.

    Раньше здесь стояло `astimezone(tz=None)` — приведение к МЕСТНОЙ зоне
    сервера, — а сравнение при выдаче идёт с `now_utc()`. Ссылка жила дольше
    заявленного ровно на смещение зоны: на машине UTC+3 срок «до 15:00»
    закрывался в 18:00. Тихо: ни в логе, ни в интерфейсе этого не видно, а
    существующий тест брал запас в сутки и трёх часов не замечал.

    То же с временем без зоны (его шлёт поле `datetime-local`): оно считается
    уже UTC — то же соглашение, что и везде в проекте.
    """
    return to_utc_naive(expires_at)


def _hash_pin(pin: str) -> str:
    pin = pin.strip()
    if not passwords.is_valid_pin(pin):
        raise errors.ValidationError("PIN must be 4-8 digits", code="bad_pin")
    return passwords.hash_password(pin)


# --- публичный доступ ---

def resolve_public(db: Session, token: str) -> tuple[ShareLink, Board] | None:
    """Ссылка доступна снаружи? Все причины отказа выглядят одинаково (None)."""
    link = shares_repo.get_by_token(db, token)
    if link is None or not link.is_active:
        return None
    if link.expires_at is not None and link.expires_at < now_utc():
        return None
    board = boards_repo.get(db, link.board_id)
    if board is None or not board.is_published:
        return None
    return link, board


def media_is_public(db: Session, work_uid: str, pins_held: dict[int, str]) -> bool:
    """Можно ли отдать наружу файл этой работы.

    **Файл витрины закрывается теми же пятью ручками, что и сама витрина.**
    Раньше — ни одной. Проверено прогоном: клиент один раз открыл витрину и
    запомнил адреса картинок; после смены PIN, отзыва ссылки, истечения срока,
    снятия доски с публикации и даже удаления ссылки страница отвечала 401/404,
    а файлы — 200, причём анониму и вовсе без PIN. Витрина закрыта, работы
    открыты.

    Смягчало это одно: имя каталога — `token_hex(16)`, его не подобрать. То есть
    беда была не «доступно всем», а «доступно навсегда тому, кому однажды
    показали», — ровно то, от чего отзыв ссылки и заводят.

    Правило: файл виден, если у его доски есть **хотя бы одна живая ссылка**, по
    которой этот посетитель прошёл бы и на саму страницу. Без PIN — проходит
    любой; с PIN — только предъявивший пропуск именно по этой ссылке.
    `pins_held` — пропуска из cookie, по одному на ссылку.

    Ссылок у доски бывает несколько, и хватает одной подходящей: две ссылки —
    это два разных приглашения к одной работе, и закрывать одно из-за другого
    было бы неверно.
    """
    work = boards_repo.get_work_by_uid(db, work_uid)
    if work is None:
        return False
    board = boards_repo.get(db, work.board_id)
    if board is None or not board.is_published:
        return False
    for link in shares_repo.live_for_board(db, board.id, now_utc()):
        if link.pin_hash is None:
            return True
        held = pins_held.get(link.id)
        if held and tokens.check_pin_access_cookie(held, link.id, link.pin_hash):
            return True
    return False


def verify_pin(db: Session, link: ShareLink, pin: str, ip: str, limiter) -> bool:
    key = f"{link.id}:{tokens.hash_ip(ip)}"
    if limiter.is_blocked(key):
        raise errors.RateLimitedError("Too many attempts, try later", code="pin_rate_limited")
    if link.pin_hash and passwords.verify_password(pin.strip(), link.pin_hash):
        limiter.reset(key)
        return True
    limiter.record_failure(key)
    return False


def record_view(db: Session, link: ShareLink, ip: str, user_agent: str) -> None:
    db.add(
        ShareView(
            share_link_id=link.id,
            ip_hash=tokens.hash_ip(ip),
            user_agent=(user_agent or "")[:300],
        )
    )


def share_stats(db: Session, link: ShareLink) -> dict:
    last = shares_repo.last_view(db, link.id)
    return {
        "views_count": shares_repo.views_count(db, link.id),
        "unique_views_count": shares_repo.unique_views_count(db, link.id),
        "last_viewed_at": last.viewed_at.isoformat() if last else None,
    }


def public_url(link: ShareLink) -> str:
    return f"{get_settings().base_url.rstrip('/')}/b/{link.token}"
