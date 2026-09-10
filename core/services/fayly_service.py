"""Модуль «Файлы»: одно дерево на всё, что лежит на диске.

**Дерево повторяет раскладку `storage/`, а не выдумывает свою.** Работы досок,
вложения клиентов, задач и бланков, снимки товаров — у каждого вида файлов на
диске своя папка, и ветка дерева ровно ей и соответствует. Плюс своя ветка
«Загрузки» — то, что человек принёс через этот модуль и разложил сам.

**Ветка появляется вместе с правом на раздел, из которого файлы.** Без права
на клиентов ветки «Клиенты» нет вовсе — не пустая, а именно нет: файл клиента
это данные клиента, и обойти права на них через менеджер файлов нельзя.

**Дробим ветку только там, где детей заведомо немного.** У досок это доска и
вид файла (доски — портфолио, их десятки), у бланков — вид бланка. Клиенты,
задачи и товары идут плоским списком: узел на каждого клиента — это дерево
из тысяч строк, которое не читается и не грузится.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

from config.settings import get_settings
from core import exceptions as errors
from core.security import tokens
from core.services import (
    audit_service,
    client_service,
    media_service,
    modules_service,
    permissions_service,
    storage_service,
)
from database.models import StoredFile, User
from database.repositories import fayly as fayly_repo

#: Что можно принести через модуль. Тот же перечень, что у вложений карточки:
#: два разных перечня на один вопрос «какие файлы мы принимаем» разошлись бы.
DOPUSTIMYE = client_service.ALLOWED_CLIENT_FILE_EXTS

#: Ключи корней. Строками, а не числами: узел дерева — это адрес, и `board:12`
#: читается в отладке, а `7` не читается.
VSE = "all"
SVOI = "own"
DOSKI = "boards"
KLIENTY = "clients"
ZADACHI = "tasks"
BLANKI = "docs"
TOVARY = "goods"

_KARTINKI = {"jpg", "jpeg", "png", "webp", "gif", "svg", "psd", "ai", "fig", "sketch"}
_VIDEO = {"mp4", "webm", "mov"}
_BUMAGI = {"pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "rtf", "csv"}


def _vid(imya: str) -> str:
    ext = Path(imya).suffix.lstrip(".").lower()
    if ext in _KARTINKI:
        return "image"
    if ext in _VIDEO:
        return "video"
    return "doc" if ext in _BUMAGI else "other"


def _put(fayl: StoredFile) -> Path:
    ext = Path(fayl.original_name).suffix
    return get_settings().stored_files_dir / f"{fayl.file_uid}{ext}"


# --- права на ветки -----------------------------------------------------------


def _vidno(db: Session, actor: User, blok: str | None, oblast: str) -> bool:
    if blok and not modules_service.is_enabled(db, blok):
        return False
    return permissions_service.has(db, actor, oblast, "view")


def _korni(db: Session, actor: User) -> dict[str, bool]:
    return {
        DOSKI: _vidno(db, actor, "boards", "boards"),
        KLIENTY: _vidno(db, actor, None, "clients"),
        ZADACHI: _vidno(db, actor, "tasks", "tasks"),
        BLANKI: _vidno(db, actor, "documents", "documents"),
        TOVARY: _vidno(db, actor, "warehouse", "warehouse"),
    }


# --- дерево -------------------------------------------------------------------


def derevo(db: Session, actor: User) -> dict:
    """Дерево со счётчиками. Счётчики — запросами с группировкой, а не обходом
    строк: файлов десятки тысяч, а чисел на экране два десятка."""
    otkryto = _korni(db, actor)
    vetki: list[dict] = []

    if otkryto[DOSKI]:
        po_doskam: dict[int, dict] = {}
        for board_id, title, kind, skolko in fayly_repo.schyot_rabot(db):
            uzel = po_doskam.setdefault(board_id, {"title": title, "image": 0, "video": 0})
            uzel[kind if kind in ("image", "video") else "image"] += skolko
        deti = []
        for board_id, uzel in po_doskam.items():
            vnutri = []
            if uzel["image"]:
                vnutri.append({"id": f"board:{board_id}:image", "kind": "images", "n": uzel["image"]})
            if uzel["video"]:
                vnutri.append({"id": f"board:{board_id}:video", "kind": "videos", "n": uzel["video"]})
            deti.append(
                {
                    "id": f"board:{board_id}",
                    "name": uzel["title"],
                    "n": uzel["image"] + uzel["video"],
                    "kids": vnutri,
                }
            )
        deti.sort(key=lambda x: x["name"].lower())
        vetki.append({"id": DOSKI, "kind": "boards", "n": sum(d["n"] for d in deti), "kids": deti})

    if otkryto[KLIENTY]:
        vetki.append({"id": KLIENTY, "kind": "clients", "n": fayly_repo.schyot_klientov(db), "kids": []})
    if otkryto[ZADACHI]:
        vetki.append({"id": ZADACHI, "kind": "tasks", "n": fayly_repo.schyot_zadach(db), "kids": []})
    if otkryto[BLANKI]:
        deti = [
            {"id": f"doc:{kind}", "kind": f"doc-{kind}", "n": skolko}
            for kind, skolko in fayly_repo.schyot_blankov(db)
        ]
        vetki.append({"id": BLANKI, "kind": "docs", "n": sum(d["n"] for d in deti), "kids": deti})
    if otkryto[TOVARY]:
        vetki.append({"id": TOVARY, "kind": "goods", "n": fayly_repo.schyot_tovarov(db), "kids": []})

    # Свои папки — деревом любой глубины. Счётчик папки — только её файлы:
    # складывать вложенные значило бы показать одно число дважды.
    po_papkam = fayly_repo.svoi_po_papkam(db)
    papki = fayly_repo.papki(db)
    deti_papok: dict[int | None, list[dict]] = {}
    for p in papki:
        deti_papok.setdefault(p.parent_id, []).append(
            {"id": f"folder:{p.id}", "name": p.name, "n": po_papkam.get(p.id, 0), "kids": []}
        )

    def sobrat(parent_id: int | None) -> list[dict]:
        itog = deti_papok.get(parent_id, [])
        for uzel in itog:
            uzel["kids"] = sobrat(int(uzel["id"].split(":")[1]))
        return itog

    svoi_deti = sobrat(None)
    vetki.append(
        {
            "id": SVOI,
            "kind": "own",
            "n": sum(po_papkam.values()),
            "kids": svoi_deti,
        }
    )

    vsego = sum(v["n"] for v in vetki)
    return {"id": VSE, "kind": "all", "n": vsego, "kids": vetki}


# --- содержимое узла ----------------------------------------------------------


def _stroka(
    nomer: str,
    imya: str,
    mime: str,
    razmer: int,
    kogda,
    gde: str,
    otkryt: str | None,
    thumb: str | None = None,
) -> dict:
    return {
        "id": nomer,
        "name": imya,
        "kind": _vid(imya),
        "mime": mime,
        "size_bytes": razmer or 0,
        "created_at": kogda.isoformat() if kogda else None,
        "where": gde,
        "open": otkryt,
        "thumb": thumb,
    }


def soderzhimoe(db: Session, actor: User, uzel: str, page: int, per_page: int) -> dict:
    """Файлы узла страницей. Узел, на который нет права, отвечает отказом, а не
    пустым списком: пустой список читается как «файлов нет»."""
    smeshchenie = (page - 1) * per_page
    otkryto = _korni(db, actor)

    if uzel.startswith("board"):
        if not otkryto[DOSKI]:
            raise errors.ForbiddenError("Boards are not available", code="permission_denied")
        chasti = uzel.split(":")
        board_id = int(chasti[1]) if len(chasti) > 1 else None
        kind = chasti[2] if len(chasti) > 2 else None
        stroki, vsego = fayly_repo.raboty(db, board_id, kind, smeshchenie, per_page)
        items = [
            _stroka(
                f"work:{work.id}",
                work.original_name,
                work.mime,
                work.size_bytes,
                work.created_at,
                title,
                f"/boards/{work.board_id}",
                (media_service.work_media_urls(work) or {}).get("thumb")
                if work.status == "ready"
                else None,
            )
            for work, title in stroki
        ]
        return {"items": items, "total": vsego, "page": page, "per_page": per_page}

    if uzel == KLIENTY:
        if not otkryto[KLIENTY]:
            raise errors.ForbiddenError("Clients are not available", code="permission_denied")
        stroki, vsego = fayly_repo.vlozheniya_klientov(db, smeshchenie, per_page)
        items = [
            _stroka(
                f"client:{f.id}",
                f.original_name,
                f.mime,
                f.size_bytes,
                f.created_at,
                imya,
                f"/clients/{f.client_id}",
            )
            for f, imya in stroki
        ]
        return {"items": items, "total": vsego, "page": page, "per_page": per_page}

    if uzel == ZADACHI:
        if not otkryto[ZADACHI]:
            raise errors.ForbiddenError("Tasks are not available", code="permission_denied")
        stroki, vsego = fayly_repo.vlozheniya_zadach(db, smeshchenie, per_page)
        items = [
            _stroka(
                f"task:{f.id}", f.original_name, f.mime, f.size_bytes, f.created_at, imya, "/tasks"
            )
            for f, imya in stroki
        ]
        return {"items": items, "total": vsego, "page": page, "per_page": per_page}

    if uzel.startswith(BLANKI) or uzel.startswith("doc:"):
        if not otkryto[BLANKI]:
            raise errors.ForbiddenError("Documents are not available", code="permission_denied")
        kind = uzel.split(":")[1] if ":" in uzel else None
        stroki, vsego = fayly_repo.vlozheniya_blankov(db, kind, smeshchenie, per_page)
        items = [
            _stroka(
                f"doc:{f.id}",
                f.original_name,
                f.mime,
                f.size_bytes,
                f.created_at,
                nomer,
                f"/documents/{f.document_id}",
            )
            for f, nomer, _vid_blanka in stroki
        ]
        return {"items": items, "total": vsego, "page": page, "per_page": per_page}

    if uzel == TOVARY:
        if not otkryto[TOVARY]:
            raise errors.ForbiddenError("Warehouse is not available", code="permission_denied")
        stroki, vsego = fayly_repo.snimki_tovarov(db, smeshchenie, per_page)
        items = [
            _stroka(
                f"product:{p.id}",
                p.original_name,
                p.mime,
                p.size_bytes,
                None,
                imya,
                f"/warehouse/{p.product_id}",
            )
            for p, imya in stroki
        ]
        return {"items": items, "total": vsego, "page": page, "per_page": per_page}

    # Свои папки: корень «Загрузки» — файлы без папки, `folder:N` — её файлы.
    folder_ids: list[int | None] = [None] if uzel == SVOI else []
    if uzel.startswith("folder:"):
        folder_ids = [int(uzel.split(":")[1])]
    if uzel == VSE:
        # «Все файлы» показывают свои: чужие лежат по своим веткам, и валить их
        # в одну кучу значило бы показать одно и то же дважды.
        folder_ids = [None]
    stroki, vsego = fayly_repo.svoi_fayly(db, folder_ids, smeshchenie, per_page)
    items = [
        _stroka(
            f"stored:{f.id}",
            f.original_name,
            f.mime,
            f.size_bytes,
            f.created_at,
            "",
            None,
        )
        for f in stroki
    ]
    return {"items": items, "total": vsego, "page": page, "per_page": per_page}


# --- свои папки ---------------------------------------------------------------


def sozdat_papku(db: Session, actor: User, parent_id: int | None, name: str):
    imya = (name or "").strip()
    if not imya:
        raise errors.ValidationError("Folder name is required", code="folder_name_required")
    if parent_id is not None and fayly_repo.papka(db, parent_id) is None:
        raise errors.NotFoundError("Folder not found", code="folder_not_found")
    if fayly_repo.sosedka_s_takim_imenem(db, parent_id, imya[:120]):
        raise errors.ValidationError("A folder with this name is already here", code="folder_exists")
    return fayly_repo.sozdat_papku(db, parent_id, imya[:120], actor.id)


def udalit_papku(db: Session, actor: User, folder_id: int) -> None:
    """Папка уходит вместе с вложенными и их файлами.

    Файлы с диска убираются ДО удаления строк: упади удаление на полпути —
    лучше остаться с лишними байтами на диске, чем со строками, которым нечего
    открыть.
    """
    papka = fayly_repo.papka(db, folder_id)
    if papka is None:
        raise errors.NotFoundError("Folder not found", code="folder_not_found")
    vse = fayly_repo.papki(db)
    deti: dict[int | None, list[int]] = {}
    for p in vse:
        deti.setdefault(p.parent_id, []).append(p.id)
    vetka: list[int] = []
    ochered = [folder_id]
    while ochered:
        tekushchiy = ochered.pop()
        vetka.append(tekushchiy)
        ochered.extend(deti.get(tekushchiy, ()))
    fayly, _vsego = fayly_repo.svoi_fayly(db, list(vetka), 0, 10_000)
    for f in fayly:
        _put(f).unlink(missing_ok=True)
    audit_service.record_deletion(
        db,
        actor=actor,
        entity_type=audit_service.ENTITY_FILE,
        entity_id=folder_id,
        entity_label=f"{papka.name} ({len(fayly)})",
    )
    fayly_repo.udalit_papku(db, papka)
    storage_service.invalidate_size_cache()


# --- свои файлы ---------------------------------------------------------------


def prinyat(db: Session, actor: User, folder_id: int | None, original_name: str, content: bytes):
    """Принять файл в свою папку. Приёмка — общая с вложениями карточек."""
    if folder_id is not None and fayly_repo.papka(db, folder_id) is None:
        raise errors.NotFoundError("Folder not found", code="folder_not_found")
    ext, content = client_service.proverit_vlozhenie(original_name, content, DOPUSTIMYE)
    fayl = fayly_repo.sozdat_fayl(
        db,
        folder_id=folder_id,
        file_uid=tokens.new_file_uid(),
        original_name=Path(original_name).name[:255],
        # Присланный `Content-Type` не сохраняем: его выбирает тот, кто
        # загружает, а уходит он в заголовок ответа. Считаем из расширения,
        # которое к этому месту уже сверено с содержимым.
        mime=client_service.MIME_PO_RASSHIRENIYU.get(ext, "application/octet-stream"),
        size_bytes=len(content),
        author_id=actor.id,
    )
    katalog = get_settings().stored_files_dir
    katalog.mkdir(parents=True, exist_ok=True)
    _put(fayl).write_bytes(content)
    storage_service.invalidate_size_cache()
    return fayl


def fayl_na_diske(fayl: StoredFile) -> Path:
    return _put(fayl)


def poluchit(db: Session, file_id: int) -> StoredFile:
    fayl = fayly_repo.fayl(db, file_id)
    if fayl is None:
        raise errors.NotFoundError("File not found", code="file_not_found")
    return fayl


def udalit(db: Session, actor: User, file_id: int) -> None:
    fayl = poluchit(db, file_id)
    audit_service.record_deletion(
        db,
        actor=actor,
        entity_type=audit_service.ENTITY_FILE,
        entity_id=fayl.id,
        entity_label=fayl.original_name,
    )
    _put(fayl).unlink(missing_ok=True)
    fayly_repo.udalit_fayl(db, fayl)
    storage_service.invalidate_size_cache()
