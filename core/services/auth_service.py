import secrets
from datetime import timedelta

from sqlalchemy.orm import Session

from config.settings import get_settings
from core import exceptions as errors
from core import uniqueness
from core.security import passwords, tokens
from core.services import audit_service
from core.utils import PRESENCE_TOUCH_SECONDS, is_valid_email, normalize_email, now_utc
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
    # Должность по умолчанию — та, что помечена основной. Без неё одобренный
    # сотрудник входил бы в CRM без единого раздела и без объяснения, почему.
    # Роль при этом обычная: её можно переделать или заменить другой.
    from core.services import permissions_service

    default_role = permissions_service.default_role(db)
    user = User(
        email=email,
        name=name.strip(),
        password_hash=passwords.hash_password(password),
        role=ROLE_MANAGER,
        role_id=default_role.id if default_role else None,
        status=STATUS_PENDING,
    )
    # Между проверкой выше и вставкой есть окно, и попадают в него не
    # злоумышленники, а обычное двойное нажатие: форма запроса доступа открыта в
    # интернет, кнопка отправляет сразу. Оба запроса видели «свободно», один
    # вставил, второй получал 500 — человек смотрел на успешно принятую заявку
    # как на поломку и слал третью.
    #
    # Ответ тот же, что и при обычном занятом адресе: к моменту вставки он и
    # правда был занят. Чем именно — соседним нажатием того же человека или
    # чужой регистрацией — для ответа не важно.
    return uniqueness.insert_unique(
        db,
        user,
        taken=lambda row: users_repo.get_by_email(db, row.email) is not None,
        message="Email already registered",
        code="email_taken",
    )


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
    # присутствие: обновляем last_seen не чаще раза в минуту, чтобы не писать на каждый
    # запрос, но достаточно часто для индикатора «в сети». last_seen на пользователе
    # переживает logout — так остаётся «последний раз в сети».
    now = now_utc()
    if user.last_seen_at is None or (now - user.last_seen_at).total_seconds() > PRESENCE_TOUCH_SECONDS:
        user.last_seen_at = now
        session.last_seen_at = now
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


def _record_access(
    db: Session, actor: User, user: User, action: str, before: str, after: str
) -> None:
    """Выдача и снятие доступа — в журнал, одной формой.

    Доступ — единственное, что человек не может вернуть себе сам, и
    единственное, чего у сотрудника не спросишь задним числом: отключённый в
    систему не войдёт. Поэтому «кто и когда открыл/закрыл» пишется отдельно от
    самого поля статуса, которое каждое следующее изменение затирает.
    """
    audit_service.record(
        db,
        action=action,
        actor=actor,
        source=audit_service.SOURCE_MANUAL,
        entity_type=audit_service.ENTITY_USER,
        entity_id=user.id,
        entity_label=user.name,
        before=before,
        after=after,
    )


def approve(db: Session, actor: User, user_id: int) -> User:
    user = _get_manager(db, user_id)
    if user.status != STATUS_PENDING:
        raise errors.ConflictError("Account is not pending", code="not_pending")
    user.status = STATUS_ACTIVE
    user.approved_at = now_utc()
    db.flush()
    _record_access(
        db, actor, user, audit_service.ACTION_STAFF_APPROVED, STATUS_PENDING, STATUS_ACTIVE
    )
    return user


def reject(db: Session, actor: User, user_id: int) -> None:
    user = _get_manager(db, user_id)
    if user.status != STATUS_PENDING:
        raise errors.ConflictError("Account is not pending", code="not_pending")
    # Снимок имени — до удаления: спросить его потом будет не у кого.
    _record_access(
        db, actor, user, audit_service.ACTION_STAFF_REJECTED, STATUS_PENDING, "deleted"
    )
    db.delete(user)


def disable(db: Session, actor: User, user_id: int) -> User:
    # ВНИМАНИЕ. Инвариант «раздавать права всегда есть кому»
    # (`permissions_service._refuse_if_nobody_left_to_manage_roles`) здесь НЕ
    # проверяется, и в `delete_user` тоже. Он стоит на четырёх дверях из шести:
    # снять `roles.manage` с последней роли нельзя, перевести её единственного
    # носителя на другую должность нельзя, а отключить или удалить его
    # самого — можно, и система остаётся без управляющего доступами.
    #
    # Это не забытая строка, а неразрешённый вопрос, и закрывать его здесь
    # нельзя. Инвариант не считает root'а нарочно, поэтому запрет на отключение
    # сделал бы назначение первого «гендиректора» необратимым: снять с него
    # право уже нельзя, перевести нельзя, а тогда и уволить нельзя — при том,
    # что «только root и никаких управляющих» есть законное состояние, с
    # которого начинается любая установка.
    #
    # Развязок две, и обе — решение о модели прав, а не правка на месте:
    # либо root идёт в счёт при снятии человека (тогда шесть дверей сходятся на
    # «пока жив root, можно всё»), либо появляется явная передача полномочий
    # («уволить, назначив преемника») и запрет становится общим. Подробности —
    # в отчёте по этой ветке.
    user = _get_manager(db, user_id)
    was = user.status
    user.status = STATUS_DISABLED
    users_repo.delete_sessions_for_user(db, user.id)
    db.flush()
    _record_access(
        db, actor, user, audit_service.ACTION_STAFF_DISABLED, was, STATUS_DISABLED
    )
    return user


def enable(db: Session, actor: User, user_id: int) -> User:
    user = _get_manager(db, user_id)
    if user.status != STATUS_DISABLED:
        raise errors.ConflictError("Account is not disabled", code="not_disabled")
    user.status = STATUS_ACTIVE
    db.flush()
    _record_access(
        db, actor, user, audit_service.ACTION_STAFF_ENABLED, STATUS_DISABLED, STATUS_ACTIVE
    )
    return user


def reset_password(db: Session, actor: User, user_id: int) -> tuple[User, str]:
    user = _get_manager(db, user_id)
    temp_password = secrets.token_urlsafe(9)
    user.password_hash = passwords.hash_password(temp_password)
    user.must_change_password = True
    users_repo.delete_sessions_for_user(db, user.id)
    db.flush()
    # Величин у этого действия нет, и выдумывать их нельзя: пароль в журнал не
    # попадает ни в каком виде. Записываем сам факт — он и есть ответ на вопрос
    # «почему я вдруг разлогинился и кто выдал мне временный пароль».
    audit_service.record(
        db,
        action=audit_service.ACTION_STAFF_PASSWORD_RESET,
        actor=actor,
        source=audit_service.SOURCE_MANUAL,
        entity_type=audit_service.ENTITY_USER,
        entity_id=user.id,
        entity_label=user.name,
    )
    return user, temp_password


def set_role(db: Session, actor: User, user_id: int, new_role: str) -> User:
    """Меняет роль сотрудника (root ↔ manager). Только для активных.

    Root'ов может быть несколько (несколько администраторов). Инварианты:
    свою роль менять нельзя (иначе можно случайно снять с себя доступ), и в
    системе всегда остаётся хотя бы один root.
    """
    if new_role not in (ROLE_ROOT, ROLE_MANAGER):
        raise errors.ValidationError("Unknown role", code="bad_role")
    user = users_repo.get_by_id(db, user_id)
    if user is None:
        raise errors.NotFoundError("User not found", code="user_not_found")
    if user.id == actor.id:
        raise errors.ForbiddenError("Cannot change your own role", code="cannot_change_own_role")
    if user.status != STATUS_ACTIVE:
        raise errors.ConflictError("Only active staff can change role", code="not_active")
    # Root'а делает только root. Право `roles.manage` — про должности, которые
    # собираются из реестра и им же ограничены; root не описывается должностью
    # вовсе, у него весь реестр и завтрашние права заодно. Пока эта дверь была
    # открыта, любой, кто раздаёт роли, коротким запросом производил себе
    # сообщника с полным доступом — а вместе со сбросом пароля (временный
    # пароль возвращается в ответе) это и вход под ним.
    #
    # Понижение root'а — там же и по той же причине: снять начальника не должен
    # тот, кого начальник назначил.
    if ROLE_ROOT in (new_role, user.role) and actor.role != ROLE_ROOT:
        raise errors.ForbiddenError(
            "Only root can grant or revoke root", code="only_root_grants_root"
        )
    if user.role == new_role:
        return user
    if user.role == ROLE_ROOT and users_repo.count_by_role(db, ROLE_ROOT) <= 1:
        raise errors.ForbiddenError("At least one root must remain", code="last_root")
    was = user.role
    user.role = new_role
    if new_role == ROLE_ROOT:
        # Root не описывается должностью: права у него все и всегда. Ссылку
        # снимаем, иначе роль висела бы у него мёртвым грузом и всплыла бы при
        # понижении обратно — уже, возможно, с другим набором прав.
        user.role_id = None
    elif user.role_id is None:
        # Понизили бывшего root'а: без должности он остался бы в системе, но
        # без единого раздела. Кладём основную — как новому сотруднику.
        from core.services import permissions_service

        default_role = permissions_service.default_role(db)
        user.role_id = default_role.id if default_role else None
    db.flush()
    _record_access(
        db, actor, user, audit_service.ACTION_STAFF_ROLE_CHANGED, was, new_role
    )
    return user


def delete_user(db: Session, actor: User, user_id: int) -> None:
    """Удаляет аккаунт безвозвратно. Авторство (клиенты, доски, заметки, файлы)
    сохраняется, но обнуляется (FK ``ON DELETE SET NULL``); сессии сносятся каскадом
    (``ON DELETE CASCADE``), поэтому доступ по ним прекращается сразу.
    Нельзя удалить себя и последнего root'а.
    """
    from core.services import avatar_service

    user = users_repo.get_by_id(db, user_id)
    if user is None:
        raise errors.NotFoundError("User not found", code="user_not_found")
    if user.id == actor.id:
        raise errors.ForbiddenError("Cannot delete your own account", code="cannot_delete_self")
    if user.role == ROLE_ROOT and users_repo.count_by_role(db, ROLE_ROOT) <= 1:
        raise errors.ForbiddenError("At least one root must remain", code="last_root")
    # Про инвариант «раздавать права всегда есть кому» — см. `disable`: здесь
    # он тоже не проверяется, и по той же неразрешённой причине.
    avatar_service.clear_avatar(db, user)  # стираем файл аватара с диска
    # Запись об удалении переживает удалённого: `audit_events.actor_id` уходит
    # в NULL вместе с ним, если удаляют самого исполнителя, но имена обоих
    # остались снимками.
    audit_service.record_deletion(
        db,
        actor=actor,
        entity_type=audit_service.ENTITY_USER,
        entity_id=user.id,
        entity_label=user.name,
    )
    db.delete(user)


# --- bootstrap ---

def bootstrap_root(db: Session) -> User | None:
    """Создаёт root-аккаунт при первом запуске. Возвращает его, если создал.

    None означает «уже был» — и его же возвращает проигравший гонку за первый
    запуск: аккаунт есть, создал его сосед, сообщать человеку не о чем.
    """
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
    # Терпим соседа: при `uvicorn --workers 2` оба процесса поднимаются разом и
    # оба видят «root'а нет». Второй получал нарушение уникальности по адресу и
    # не поднимался вовсе — в самый неудачный момент, при первом же запуске.
    placed = uniqueness.insert_or_ignore(
        db, root, taken=lambda row: users_repo.get_by_email(db, row.email) is not None
    )
    return root if placed else None
