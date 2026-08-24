from datetime import datetime, timezone

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from database.models import User, UserSession
from database.models.user import ROLE_ROOT


def get_by_id(db: Session, user_id: int) -> User | None:
    return db.get(User, user_id)


def _zapert_vladeltsev(db: Session) -> None:
    """Взять замок на строки владельцев — до проверки «их больше одного».

    **Без этого защита не работает на MySQL, и это проверено гонкой.** Условие
    ниже стоит внутри самой записи и выглядит неуязвимым, но подзапрос обёрнут в
    производную таблицу, а MySQL материализует её ОБЫЧНЫМ чтением, без замков.
    Двое владельцев снимают root друг с друга разом, оба подзапроса считают до
    чужой фиксации, оба видят «двое», построчные замки при этом не сталкиваются
    — правят-то разные строки. Проходят оба. Владельцев остаётся НОЛЬ.

    Пока набор гонялся на файловой базе, беды не было по случайности устройства:
    писатель там один, второй ждёт и пересчитывает уже после чужой записи. То
    есть защиту держала не проверка, а однопоточность движка — и на боевом
    сервере, где движок другой, её не держало ничто.

    Замок берётся на ВСЕ строки владельцев, а не на правимую: спор идёт не о
    строке, а о том, сколько их останется. Второй дуэлянт ждёт первого, а
    дождавшись, видит уже одного владельца — и условие ниже отказывает ему
    честно.

    Цена — один запрос на смену роли и удаление сотрудника, то есть на действие,
    которое случается раз в месяц. За систему, которую иначе открывают правкой
    файла базы, это дёшево.
    """
    db.execute(select(User.id).where(User.role == ROLE_ROOT).with_for_update()).all()


def _more_than_one_root():
    """Подзапрос «владельцев больше одного» — условие внутри самой записи.

    Обёрнут в производную таблицу не для красоты: MySQL запрещает подзапрос по
    той же таблице, которую правит UPDATE или DELETE, а через `(SELECT ... FROM
    (SELECT ...) AS t)` это ограничение снимается.
    """
    roots = select(User.id).where(User.role == ROLE_ROOT).subquery()
    return select(func.count()).select_from(roots).scalar_subquery() > 1


def demote_last_root_guard(db: Session, user: User, new_role: str, values: dict) -> bool:
    """Снять root, только если владельцев больше одного. False — он последний.

    Условие стоит **внутри записи**, а не проверкой перед ней. Проверка перед
    записью читает в одной транзакции, а пишет в другой, и между ними помещается
    вся беда: двое владельцев снимают root друг с друга одновременно, оба видят
    «владельцев двое», обоим отвечают «готово». Воспроизведено — после гонки
    владельцев остаётся **ноль**.

    Стоит это дороже, чем звучит. Без root'а `roles.manage` нет ни у кого,
    `staff.manage` нет ни у кого, и поднять никого нельзя: систему после этого
    открывают только правкой файла базы. А двое совладельцев в ссоре — не
    выдуманный сценарий, это обычный конфликт двух хозяев мастерской.
    """
    _zapert_vladeltsev(db)
    changed = db.execute(
        update(User)
        .where(User.id == user.id, User.role == ROLE_ROOT, _more_than_one_root())
        .values(role=new_role, **values)
    )
    if changed.rowcount == 0:
        return False
    # Объект в памяти помнит прежнюю роль: писали запросом, мимо него.
    db.refresh(user)
    return True


def delete_guarding_last_root(db: Session, user: User) -> bool:
    """Удалить сотрудника; последнего владельца — отказать. Про гонку см. выше.

    Удаляем запросом, а не через ORM, ровно ради условия. Связанное уходит
    каскадом на стороне базы (`ON DELETE CASCADE` у сессий, `SET NULL` у
    авторства) — то есть так же, как и при удалении через ORM: каскады эти
    объявлены в схеме, а не в приложении.
    """
    condition = [User.id == user.id]
    if user.role == ROLE_ROOT:
        _zapert_vladeltsev(db)
        condition.append(_more_than_one_root())
    removed = db.execute(delete(User).where(*condition))
    if removed.rowcount == 0:
        return False
    # Строки уже нет, а объект остался бы в сессии и всплыл бы при следующем
    # обращении к ней — с полями, которым больше ничего не соответствует.
    db.expunge(user)
    return True


def get_many(db: Session, ids) -> list[User]:
    """Пользователи пачкой — одним запросом, а не по одному на строку истории."""
    ids = [i for i in set(ids) if i]
    if not ids:
        return []
    return list(db.scalars(select(User).where(User.id.in_(ids))))


def count_by_role(db: Session, role: str) -> int:
    return db.scalar(select(func.count()).select_from(User).where(User.role == role)) or 0


def get_by_email(db: Session, email: str) -> User | None:
    return db.scalar(select(User).where(User.email == email.strip().lower()))


def get_root(db: Session) -> User | None:
    return db.scalar(select(User).where(User.role == "root"))


def list_staff(db: Session, status: str | None = None) -> list[User]:
    q = select(User).order_by(User.created_at.desc())
    if status:
        q = q.where(User.status == status)
    return list(db.scalars(q))


def create_session(db: Session, user_id: int, token_hash: str, expires_at: datetime) -> UserSession:
    s = UserSession(user_id=user_id, token_hash=token_hash, expires_at=expires_at)
    db.add(s)
    db.flush()
    return s


def get_session_by_hash(db: Session, token_hash: str) -> UserSession | None:
    return db.scalar(select(UserSession).where(UserSession.token_hash == token_hash))


def get_session_with_user(db: Session, token_hash: str) -> tuple[UserSession, User] | None:
    """Сессия ВМЕСТЕ с хозяином — одним запросом. `None` — сессии нет.

    **Это самый частый запрос в системе.** Он выполняется на КАЖДОМ обращении
    вошедшего человека: на каждой странице, на каждой плитке витрины, на каждом
    фото товара. Пока их было два (сессия по отпечатку, потом хозяин по номеру),
    два круга к базе платились всегда и умножались на число картинок: витрина на
    тридцать плиток — это шестьдесят кругов только за проверку «кто пришёл».

    Соединение, а не два запроса, здесь ничего не меняет по смыслу: сессия без
    хозяина и раньше означала «не пустить» — служба проверяла `user is None` и
    отвечала отказом. Разница только в числе кругов.

    Возвращаем ОБА объекта, а не одного хозяина: у сессии спрашивают срок
    (`expires_at`), и второй запрос за ним свёл бы выигрыш на нет.
    """
    stroka = db.execute(
        select(UserSession, User).join(User, User.id == UserSession.user_id).where(
            UserSession.token_hash == token_hash
        )
    ).first()
    return (stroka[0], stroka[1]) if stroka is not None else None


def delete_session(db: Session, session: UserSession) -> None:
    db.delete(session)


def delete_sessions_for_user(db: Session, user_id: int) -> None:
    for s in db.scalars(select(UserSession).where(UserSession.user_id == user_id)):
        db.delete(s)


def delete_other_sessions(db: Session, user_id: int, keep_token_hash: str) -> None:
    """Отзывает все сессии пользователя, кроме текущей (по хэшу токена)."""
    for s in db.scalars(
        select(UserSession).where(
            UserSession.user_id == user_id,
            UserSession.token_hash != keep_token_hash,
        )
    ):
        db.delete(s)


def purge_expired_sessions(db: Session) -> None:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for s in db.scalars(select(UserSession).where(UserSession.expires_at < now)):
        db.delete(s)
