"""Запросы модуля «Файлы»: свои папки, свои файлы и перепись чужих.

Перепись чужих файлов живёт здесь, а не в пяти репозиториях-хозяевах, потому
что отвечает на ОДИН вопрос — «что лежит на диске и сколько его». Разложить
его по хозяевам значило бы собирать дерево из пяти ответов, каждый из которых
про своё, и повторять склейку на каждом экране.

**Счётчики считаются запросом, а строки — страницей.** Дерево показывает
числа: посчитать их, вычитав все строки в память, — то, что работает первый
год и перестаёт на третий, когда файлов десятки тысяч.
"""

from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from database.models import (
    Board,
    Client,
    ClientFile,
    Document,
    DocumentFile,
    FileFolder,
    FileLink,
    FileLinkView,
    Product,
    ProductPhoto,
    StoredFile,
    Task,
    TaskFile,
    Work,
)

# --- свои папки ---------------------------------------------------------------


def papki(db: Session) -> list[FileFolder]:
    """Все папки разом. Их не бывает тысяч — это раскладка бумаг фирмы, а не
    данные; дерево строится в службе из одного ответа."""
    return list(db.execute(select(FileFolder).order_by(FileFolder.name, FileFolder.id)).scalars())


def papka(db: Session, folder_id: int) -> FileFolder | None:
    return db.execute(select(FileFolder).where(FileFolder.id == folder_id)).scalar_one_or_none()


def sozdat_papku(db: Session, parent_id: int | None, name: str, author_id: int | None) -> FileFolder:
    novaya = FileFolder(parent_id=parent_id, name=name, created_by=author_id)
    db.add(novaya)
    db.flush()
    return novaya


def sosedka_s_takim_imenem(db: Session, parent_id: int | None, name: str) -> bool:
    """Есть ли в той же папке соседка с тем же именем.

    Две «Договоры» рядом отличить нельзя ничем, кроме номера, которого человек
    не видит: он откроет не ту и решит, что файлы пропали.
    """
    usloviye = (
        FileFolder.parent_id.is_(None) if parent_id is None else FileFolder.parent_id == parent_id
    )
    return bool(
        db.execute(
            select(func.count(FileFolder.id)).where(usloviye, FileFolder.name == name)
        ).scalar_one()
    )


def udalit_papku(db: Session, papka_: FileFolder) -> None:
    db.delete(papka_)
    db.flush()


# --- свои файлы ---------------------------------------------------------------


def sozdat_fayl(
    db: Session,
    folder_id: int | None,
    file_uid: str,
    original_name: str,
    mime: str,
    size_bytes: int,
    author_id: int | None,
) -> StoredFile:
    novyy = StoredFile(
        folder_id=folder_id,
        file_uid=file_uid,
        original_name=original_name,
        mime=mime,
        size_bytes=size_bytes,
        uploaded_by=author_id,
    )
    db.add(novyy)
    db.flush()
    return novyy


def fayl(db: Session, file_id: int) -> StoredFile | None:
    return db.execute(select(StoredFile).where(StoredFile.id == file_id)).scalar_one_or_none()


def udalit_fayl(db: Session, fayl_: StoredFile) -> None:
    db.delete(fayl_)
    db.flush()


def svoi_po_papkam(db: Session) -> dict[int | None, int]:
    """Сколько своих файлов в каждой папке. Одним запросом на всё дерево."""
    stroki = db.execute(
        select(StoredFile.folder_id, func.count(StoredFile.id)).group_by(StoredFile.folder_id)
    ).all()
    return {folder_id: skolko for folder_id, skolko in stroki}


def svoi_fayly(db: Session, folder_ids: list[int | None], smeshchenie: int, skolko: int):
    """Страница своих файлов названных папок. `None` в списке — корень."""
    usloviya = []
    if None in folder_ids:
        usloviya.append(StoredFile.folder_id.is_(None))
    nomera = [x for x in folder_ids if x is not None]
    if nomera:
        usloviya.append(StoredFile.folder_id.in_(nomera))
    if not usloviya:
        return [], 0
    gde = or_(*usloviya)
    vsego = db.execute(select(func.count(StoredFile.id)).where(gde)).scalar_one()
    stroki = list(
        db.execute(
            select(StoredFile)
            .where(gde)
            .order_by(StoredFile.created_at.desc(), StoredFile.id.desc())
            .offset(smeshchenie)
            .limit(skolko)
        ).scalars()
    )
    return stroki, vsego


# --- перепись чужих файлов: счётчики ------------------------------------------


def schyot_rabot(db: Session) -> list[tuple[int, str, str, int]]:
    """Работы живых досок: доска, её название, вид файла и сколько их."""
    return [
        (board_id, title, kind, skolko)
        for board_id, title, kind, skolko in db.execute(
            select(Board.id, Board.title, Work.kind, func.count(Work.id))
            .join(Work, Work.board_id == Board.id)
            .where(Board.deleted_at.is_(None))
            .group_by(Board.id, Board.title, Work.kind)
            .order_by(Board.title, Board.id)
        ).all()
    ]


def schyot_blankov(db: Session) -> list[tuple[str, int]]:
    """Вложения бланков по виду бланка: актов столько, накладных столько."""
    return [
        (kind, skolko)
        for kind, skolko in db.execute(
            select(Document.kind, func.count(DocumentFile.id))
            .join(DocumentFile, DocumentFile.document_id == Document.id)
            .group_by(Document.kind)
            .order_by(Document.kind)
        ).all()
    ]


def schyot_klientov(db: Session) -> int:
    return db.execute(
        select(func.count(ClientFile.id))
        .join(Client, Client.id == ClientFile.client_id)
        .where(Client.deleted_at.is_(None))
    ).scalar_one()


def schyot_zadach(db: Session) -> int:
    return db.execute(
        select(func.count(TaskFile.id)).join(Task, Task.id == TaskFile.task_id)
    ).scalar_one()


def schyot_tovarov(db: Session) -> int:
    return db.execute(
        select(func.count(ProductPhoto.id))
        .join(Product, Product.id == ProductPhoto.product_id)
        .where(Product.deleted_at.is_(None))
    ).scalar_one()


# --- перепись чужих файлов: строки --------------------------------------------


def raboty(db: Session, board_id: int | None, kind: str | None, smeshchenie: int, skolko: int):
    gde = [Board.deleted_at.is_(None)]
    if board_id is not None:
        gde.append(Work.board_id == board_id)
    if kind is not None:
        gde.append(Work.kind == kind)
    vsego = db.execute(
        select(func.count(Work.id)).join(Board, Board.id == Work.board_id).where(*gde)
    ).scalar_one()
    stroki = list(
        db.execute(
            select(Work, Board.title)
            .join(Board, Board.id == Work.board_id)
            .where(*gde)
            .order_by(Work.created_at.desc(), Work.id.desc())
            .offset(smeshchenie)
            .limit(skolko)
        ).all()
    )
    return stroki, vsego


def vlozheniya_klientov(db: Session, smeshchenie: int, skolko: int):
    gde = Client.deleted_at.is_(None)
    vsego = db.execute(
        select(func.count(ClientFile.id)).join(Client, Client.id == ClientFile.client_id).where(gde)
    ).scalar_one()
    stroki = list(
        db.execute(
            select(ClientFile, Client.name)
            .join(Client, Client.id == ClientFile.client_id)
            .where(gde)
            .order_by(ClientFile.created_at.desc(), ClientFile.id.desc())
            .offset(smeshchenie)
            .limit(skolko)
        ).all()
    )
    return stroki, vsego


def vlozheniya_zadach(db: Session, smeshchenie: int, skolko: int):
    vsego = db.execute(
        select(func.count(TaskFile.id)).join(Task, Task.id == TaskFile.task_id)
    ).scalar_one()
    stroki = list(
        db.execute(
            select(TaskFile, Task.title)
            .join(Task, Task.id == TaskFile.task_id)
            .order_by(TaskFile.created_at.desc(), TaskFile.id.desc())
            .offset(smeshchenie)
            .limit(skolko)
        ).all()
    )
    return stroki, vsego


def vlozheniya_blankov(db: Session, kind: str | None, smeshchenie: int, skolko: int):
    gde = [] if kind is None else [Document.kind == kind]
    vsego = db.execute(
        select(func.count(DocumentFile.id))
        .join(Document, Document.id == DocumentFile.document_id)
        .where(*gde)
    ).scalar_one()
    stroki = list(
        db.execute(
            select(DocumentFile, Document.number, Document.kind)
            .join(Document, Document.id == DocumentFile.document_id)
            .where(*gde)
            .order_by(DocumentFile.created_at.desc(), DocumentFile.id.desc())
            .offset(smeshchenie)
            .limit(skolko)
        ).all()
    )
    return stroki, vsego


def snimki_tovarov(db: Session, smeshchenie: int, skolko: int):
    gde = Product.deleted_at.is_(None)
    vsego = db.execute(
        select(func.count(ProductPhoto.id))
        .join(Product, Product.id == ProductPhoto.product_id)
        .where(gde)
    ).scalar_one()
    stroki = list(
        db.execute(
            select(ProductPhoto, Product.name)
            .join(Product, Product.id == ProductPhoto.product_id)
            .where(gde)
            .order_by(ProductPhoto.id.desc())
            .offset(smeshchenie)
            .limit(skolko)
        ).all()
    )
    return stroki, vsego


# --- ссылки наружу ------------------------------------------------------------


def zavesti_ssylku(
    db: Session,
    *,
    stored_file_id: int | None,
    work_id: int | None,
    token: str,
    rezhim: str,
    krug: str,
    expires_at,
    pin_hash: str | None,
    author_id: int | None,
) -> FileLink:
    ssylka = FileLink(
        stored_file_id=stored_file_id,
        work_id=work_id,
        token=token,
        rezhim=rezhim,
        krug=krug,
        expires_at=expires_at,
        pin_hash=pin_hash,
        created_by=author_id,
    )
    db.add(ssylka)
    db.flush()
    return ssylka


def ssylka(db: Session, link_id: int) -> FileLink | None:
    return db.execute(select(FileLink).where(FileLink.id == link_id)).scalar_one_or_none()


def ssylka_po_tokenu(db: Session, token: str) -> FileLink | None:
    return db.execute(select(FileLink).where(FileLink.token == token)).scalar_one_or_none()


def ssylka_fayla(db: Session, *, stored_file_id: int | None, work_id: int | None) -> FileLink | None:
    """Живая ссылка файла. По одной на файл: вторая означала бы два разных
    набора условий на одни байты, и отозвать пришлось бы обе, помня о второй."""
    gde = (
        FileLink.stored_file_id == stored_file_id
        if stored_file_id is not None
        else FileLink.work_id == work_id
    )
    return db.execute(
        select(FileLink).where(gde).order_by(FileLink.id.desc()).limit(1)
    ).scalar_one_or_none()


def ssylki_faylov(
    db: Session, stored_ids: list[int], work_ids: list[int]
) -> tuple[dict[int, FileLink], dict[int, FileLink]]:
    """Ссылки сразу для страницы файлов: значок в строке нужен у каждой, и по
    запросу на строку это по обращению к базе на строку списка."""
    svoi: dict[int, FileLink] = {}
    raboty_: dict[int, FileLink] = {}
    if stored_ids:
        for s in db.execute(
            select(FileLink).where(FileLink.stored_file_id.in_(stored_ids))
        ).scalars():
            svoi[s.stored_file_id] = s
    if work_ids:
        for s in db.execute(select(FileLink).where(FileLink.work_id.in_(work_ids))).scalars():
            raboty_[s.work_id] = s
    return svoi, raboty_


def udalit_ssylku(db: Session, ssylka_: FileLink) -> None:
    db.delete(ssylka_)
    db.flush()


def otmetit_otkrytie(db: Session, link_id: int, ip_hash: str, user_agent: str) -> None:
    db.add(FileLinkView(link_id=link_id, ip_hash=ip_hash, user_agent=user_agent[:300]))


def schyot_otkrytiy(db: Session, link_id: int) -> tuple[int, int, object | None]:
    """Сколько открытий, сколько разных смотрящих и когда открывали последний раз."""
    vsego = db.execute(
        select(func.count(FileLinkView.id)).where(FileLinkView.link_id == link_id)
    ).scalar_one()
    raznyh = db.execute(
        select(func.count(func.distinct(FileLinkView.ip_hash))).where(
            FileLinkView.link_id == link_id
        )
    ).scalar_one()
    posledniy = db.execute(
        select(FileLinkView.viewed_at)
        .where(FileLinkView.link_id == link_id)
        .order_by(FileLinkView.viewed_at.desc(), FileLinkView.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    return vsego, raznyh, posledniy


def rabota_po_nomeru(db: Session, work_id: int) -> Work | None:
    return db.execute(select(Work).where(Work.id == work_id)).scalar_one_or_none()
