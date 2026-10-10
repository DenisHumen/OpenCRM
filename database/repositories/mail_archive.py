"""Запросы архива писем в CRM (docs/bloki/32-pochtovyy-server.md, «Архив писем в CRM»).

Видимость — условием запроса, как у ящиков (`mail_boxes.vidimye`): отбор после
выборки посчитал бы «всего писем» по чужим ящикам.
"""

from datetime import datetime

from sqlalchemy import case, delete, func, or_, select, update
from sqlalchemy.orm import Session

from database.models import MailAccount, MailArchiveFolder, MailArchiveMessage, MailServer
from database.models.mail_server import STATUS_READY
from database.query import contains, page_of

#: Сколько id в одном `IN (...)`: длинный список MySQL разбирает медленнее пачек.
PACHKA = 500
#: Папка не дочитана: сервер раздал номера дальше, чем архив взял.
_NEDOCHITANA = case((MailArchiveFolder.last_uid + 1 < MailArchiveFolder.uidnext, 1), else_=0)


# --- ящики и папки ---------------------------------------------------------------

def yashchiki_dlya_arkhiva(db: Session) -> list[tuple[MailAccount, datetime | None, bool]]:
    """Ящики с архивом: (ящик, когда проходили последний раз, есть ли недочитанное).

    Свой сервер — только готовый; внешний — сам по себе. Давно не проходившие —
    первыми: иначе первый проход большого ящика съедал бы круг у всех остальных.
    """
    proshli = (
        select(
            MailArchiveFolder.account_id.label("account_id"),
            func.max(MailArchiveFolder.synced_at).label("synced_at"),
            func.sum(_NEDOCHITANA).label("hvost"),
        )
        .group_by(MailArchiveFolder.account_id)
        .subquery()
    )
    zapros = (
        select(MailAccount, proshli.c.synced_at, proshli.c.hvost)
        .outerjoin(MailServer, MailServer.id == MailAccount.server_id)
        .outerjoin(proshli, proshli.c.account_id == MailAccount.id)
        .where(
            MailAccount.archive.is_(True),
            MailAccount.is_active.is_(True),
            or_(MailAccount.server_id.is_(None), MailServer.status == STATUS_READY),
        )
        .order_by(proshli.c.synced_at.is_(None).desc(), proshli.c.synced_at, MailAccount.id)
    )
    return [(box, synced, bool(hvost)) for box, synced, hvost in db.execute(zapros)]


def yashchik(db: Session, account_id: int) -> MailAccount | None:
    return db.scalar(select(MailAccount).where(MailAccount.id == account_id))


def papki(db: Session, account_id: int) -> list[MailArchiveFolder]:
    zapros = (
        select(MailArchiveFolder)
        .where(MailArchiveFolder.account_id == account_id)
        .order_by(MailArchiveFolder.folder, MailArchiveFolder.id)
    )
    return list(db.scalars(zapros))


def papka(db: Session, account_id: int, folder: str) -> MailArchiveFolder | None:
    return db.scalar(
        select(MailArchiveFolder).where(
            MailArchiveFolder.account_id == account_id, MailArchiveFolder.folder == folder
        )
    )


def dobavit(db: Session, stroka) -> None:
    db.add(stroka)


# --- письма ----------------------------------------------------------------------

def est_uidy(db: Session, account_id: int, folder: str, uidvalidity: int, uids: list[int]) -> set[int]:
    """Какие из этих UID уже лежат в архиве: повторный круг не задваивает."""
    est: set[int] = set()
    for i in range(0, len(uids), PACHKA):
        est.update(
            db.scalars(
                select(MailArchiveMessage.uid).where(
                    MailArchiveMessage.account_id == account_id,
                    MailArchiveMessage.folder == folder,
                    MailArchiveMessage.uidvalidity == uidvalidity,
                    MailArchiveMessage.uid.in_(uids[i : i + PACHKA]),
                )
            )
        )
    return est


def zhivye(db: Session, account_id: int, folder: str, uidvalidity: int, do_uid: int) -> list[tuple[int, int, str]]:
    """(id, uid, flags) писем папки, которые сервер ещё должен держать."""
    zapros = select(MailArchiveMessage.id, MailArchiveMessage.uid, MailArchiveMessage.flags).where(
        MailArchiveMessage.account_id == account_id,
        MailArchiveMessage.folder == folder,
        MailArchiveMessage.uidvalidity == uidvalidity,
        MailArchiveMessage.uid <= do_uid,
        MailArchiveMessage.removed_at.is_(None),
    )
    return [(i, u, f) for i, u, f in db.execute(zapros)]


def pometit_udalennymi(db: Session, ids: list[int], kogda: datetime) -> None:
    for i in range(0, len(ids), PACHKA):
        db.execute(
            update(MailArchiveMessage)
            .where(MailArchiveMessage.id.in_(ids[i : i + PACHKA]), MailArchiveMessage.removed_at.is_(None))
            .values(removed_at=kogda)
        )


def pometit_staroe_pokolenie(db: Session, account_id: int, folder: str, uidvalidity: int, kogda: datetime) -> None:
    """Папку пересоздали (другой UIDVALIDITY): прежние номера ни на что не указывают."""
    db.execute(
        update(MailArchiveMessage)
        .where(
            MailArchiveMessage.account_id == account_id,
            MailArchiveMessage.folder == folder,
            MailArchiveMessage.uidvalidity != uidvalidity,
            MailArchiveMessage.removed_at.is_(None),
        )
        .values(removed_at=kogda)
    )


def pometit_propavshie_papki(db: Session, account_id: int, zhivye_papki: list[str], kogda: datetime) -> list[str]:
    """Папки, которых на сервере больше нет: их письма — удалённые, отметка папки — прочь."""
    zapros = select(MailArchiveFolder).where(MailArchiveFolder.account_id == account_id)
    if zhivye_papki:
        zapros = zapros.where(MailArchiveFolder.folder.not_in(zhivye_papki))
    propali = list(db.scalars(zapros.order_by(MailArchiveFolder.id)))
    for p in propali:
        db.execute(
            update(MailArchiveMessage)
            .where(
                MailArchiveMessage.account_id == account_id,
                MailArchiveMessage.folder == p.folder,
                MailArchiveMessage.removed_at.is_(None),
            )
            .values(removed_at=kogda)
        )
        db.delete(p)
    return [p.folder for p in propali]


def obnovit_flagi(db: Session, novye: dict[int, str]) -> None:
    if novye:
        db.execute(update(MailArchiveMessage), [{"id": i, "flags": f} for i, f in sorted(novye.items())])


def get(db: Session, archive_id: int) -> MailArchiveMessage | None:
    return db.scalar(select(MailArchiveMessage).where(MailArchiveMessage.id == archive_id))


def _vidno(vidimye: set[int] | None):
    """`None` — все, включая копии убранных из CRM ящиков; иначе только эти ящики."""
    if vidimye is None:
        return None
    return MailArchiveMessage.account_id.in_(sorted(vidimye)) if vidimye else MailArchiveMessage.id < 0


def spisok(
    db: Session,
    *,
    vidimye: set[int] | None,
    box: int | None = None,
    folder: str | None = None,
    q: str | None = None,
    from_: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    removed: bool | None = None,
    page: int = 1,
    per_page: int = 50,
) -> tuple[list[MailArchiveMessage], int]:
    zapros = select(MailArchiveMessage)
    uslovie = _vidno(vidimye)
    if uslovie is not None:
        zapros = zapros.where(uslovie)
    if box is not None:
        zapros = zapros.where(MailArchiveMessage.account_id == box)
    if folder:
        zapros = zapros.where(MailArchiveMessage.folder == folder)
    if q:
        zapros = zapros.where(
            or_(
                contains(MailArchiveMessage.subject, q),
                contains(MailArchiveMessage.from_addr, q),
                contains(MailArchiveMessage.to_addrs, q),
            )
        )
    if from_:
        zapros = zapros.where(contains(MailArchiveMessage.from_addr, from_))
    if date_from is not None:
        zapros = zapros.where(MailArchiveMessage.received_at >= date_from)
    if date_to is not None:
        zapros = zapros.where(MailArchiveMessage.received_at < date_to)
    if removed is True:
        zapros = zapros.where(MailArchiveMessage.removed_at.is_not(None))
    elif removed is False:
        zapros = zapros.where(MailArchiveMessage.removed_at.is_(None))
    zapros = zapros.order_by(MailArchiveMessage.received_at.desc())
    return page_of(db, zapros, page=page, per_page=per_page)


def svodka(db: Session, vidimye: set[int] | None) -> list[tuple]:
    """По ящикам: (account_id, address, писем, байт, удалено на сервере)."""
    zapros = select(
        MailArchiveMessage.account_id,
        func.max(MailArchiveMessage.address),
        func.count(),
        func.coalesce(func.sum(MailArchiveMessage.size), 0),
        func.count(MailArchiveMessage.removed_at),
    )
    uslovie = _vidno(vidimye)
    if uslovie is not None:
        zapros = zapros.where(uslovie)
    zapros = zapros.group_by(MailArchiveMessage.account_id)
    return [tuple(r) for r in db.execute(zapros)]


def otmetki_papok(db: Session, ids: set[int] | None) -> dict[int, tuple]:
    """По ящикам: (последний проход, последняя сверка, папок не дочитано)."""
    zapros = select(
        MailArchiveFolder.account_id,
        func.max(MailArchiveFolder.synced_at),
        func.max(MailArchiveFolder.reconciled_at),
        func.sum(_NEDOCHITANA),
    )
    if ids is not None:
        if not ids:
            return {}
        zapros = zapros.where(MailArchiveFolder.account_id.in_(sorted(ids)))
    zapros = zapros.group_by(MailArchiveFolder.account_id)
    return {r[0]: (r[1], r[2], int(r[3] or 0)) for r in db.execute(zapros)}


def puti_yashchika(db: Session, account_id: int) -> list[str]:
    zapros = (
        select(MailArchiveMessage.raw_path)
        .where(MailArchiveMessage.account_id == account_id, MailArchiveMessage.raw_path != "")
        .order_by(MailArchiveMessage.id)
    )
    return list(db.scalars(zapros))


def udalit_yashchik(db: Session, account_id: int) -> int:
    """Стереть копию ящика целиком: строки писем и отметки папок."""
    itog = db.execute(delete(MailArchiveMessage).where(MailArchiveMessage.account_id == account_id))
    db.execute(delete(MailArchiveFolder).where(MailArchiveFolder.account_id == account_id))
    return int(itog.rowcount or 0)


def udalit(db: Session, stroka: MailArchiveMessage) -> None:
    db.delete(stroka)

