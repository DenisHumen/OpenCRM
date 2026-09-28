"""Напоминания: личные и поставленные другим, с повторами, звонками и привязками.

Устройство и доводы — docs/bloki/29-napominaniya.md. Здесь коротко, что где:
видимость (§3), люди (§3), срок и повтор (§4–§5), звонки (§6), привязки (§7).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import event as sa_event
from sqlalchemy.orm import Session

from config.settings import get_settings
from core import exceptions as errors
from core import references
from core.security import tokens
from core.services import audit_service, client_service, modules_service, permissions_service
from core.services import povtor_service
from core.utils import now_utc, to_utc_naive
from database.models import Task, TaskEvent, TaskFile, TaskMember, TaskSignal, TaskStep, TaskUrl, User
from database.models.task import POYAS_PO_UMOLCHANIYU, VAZHNOSTI, VAZHNOST_PO_UMOLCHANIYU
from database.repositories import tasks as tasks_repo
from database.repositories import users as users_repo

MAX_TITLE = 300

#: Потолок подробностей. Лишнее не режется молча — отвергается: файл, в котором
#: тихо недостаёт половины, хуже отсутствующего файла.
MAX_NOTE = 20_000

#: Что кладут в карточку: снимок «что привезли» и видео «как гудит».
VLOZHENIYA = {"jpg", "jpeg", "png", "webp", "gif", "mp4", "webm", "mov"}

#: Потолок списка. Счётчики в меню считают мимо него.
LIST_LIMIT = 200
#: Потолок календаря: месяц у самой занятой фирмы, с запасом.
KALENDAR_LIMIT = 2000

#: Ранние звонки: не дальше четырёх недель и не больше пяти штук.
OPOVESHENIE_MAX_MINUT = 40_320
OPOVESHENIY_MAX = 5
#: Настойчивость — из этих шагов; повторов не больше двенадцати на раз.
NASTOYCHIVO = (5, 10, 15, 30, 60)
NASTOYCHIVO_RAZ = 12

MAX_SHAGOV = 50
MAX_SSYLOK = 20
MAX_URL = 2000

#: Сколько минут назад ещё можно позвонить: сервис мог перезапускаться.
OKNO_ZVONKA = timedelta(minutes=15)
#: После срока настойчивость звонит до этого предела.
NASTOYCHIVO_OKNO = timedelta(hours=12)
#: Звонки старше этого показывать смысла нет: их место — просроченное в списке.
ZVONKI_SVEZHIE = timedelta(hours=24)
ZVONKI_HRANIT = timedelta(days=60)

PRIVYAZKI = {
    # колонка: (блок, проверка существования)
    "client_id": ("clients", references.client),
    "deal_id": ("deals", references.deal),
    "document_id": ("documents", references.document),
    "product_id": ("warehouse", references.product),
    "board_id": ("boards", references.board),
}


# --- видимость ----------------------------------------------------------------


def vidit_vse(db: Session, user: User) -> bool:
    """Руководитель (`tasks.view_others`) видит все напоминания фирмы; root — всегда."""
    return permissions_service.has(db, user, "tasks", "view_others")


def mozhet_videt(db: Session, user: User, task: Task) -> bool:
    if task.obshchee or vidit_vse(db, user):
        return True
    return tasks_repo.chlen(db, task.id, user.id) is not None


def get_task(db: Session, task_id: int) -> Task:
    task = tasks_repo.get(db, task_id)
    if task is None:
        raise errors.NotFoundError("Task not found", code="task_not_found")
    return task


def dostupnoe(db: Session, user: User, task_id: int) -> Task:
    """Напоминание, которое человеку видно. Чужое отвечает 404, а не 403:
    отказ «нет прав» подтверждал бы, что такое напоминание существует."""
    task = get_task(db, task_id)
    if not mozhet_videt(db, user, task):
        raise errors.NotFoundError("Task not found", code="task_not_found")
    return task


# --- заведение и правка -------------------------------------------------------


def create(db: Session, data: dict, author: User | None) -> Task:
    """Завести напоминание.

    Без получателей — звонит автору. Без автора (заявка с сайта) — встаёт на
    общую полку, где его видят все и берёт любой.
    """
    title = (data.get("title") or "").strip()
    if not title:
        raise errors.ValidationError("Title is required", code="title_required")

    poyas = data.get("poyas") or POYAS_PO_UMOLCHANIYU
    povtor_service.zona(poyas)
    task = Task(
        title=title[:MAX_TITLE],
        vazhnost=_vazhnost(data["vazhnost"]) if data.get("vazhnost") else VAZHNOST_PO_UMOLCHANIYU,
        note=_zametka(data.get("note")),
        poyas=poyas,
        created_by=author.id if author else None,
    )
    _srok(task, data)
    _zvonki(task, data)
    for kolonka in PRIVYAZKI:
        setattr(task, kolonka, _privyazka(db, kolonka, data.get(kolonka)))

    poluchateli = _poluchateli_iz(data, author)
    task.obshchee = bool(data.get("obshchee")) or (author is None and not poluchateli)
    if task.obshchee and author is not None:
        _nuzhno_pravo_stavit(db, author)
    db.add(task)
    db.flush()

    if author is not None:
        tasks_repo.dobavit(db, TaskMember(
            task_id=task.id, user_id=author.id, vladelets=True,
            poluchaet=author.id in poluchateli or (not poluchateli and not task.obshchee),
        ))
    _lyudi_dobavit(db, task, author, poluchateli, data.get("nablyudateli") or [])
    for tekst in data.get("shagi") or []:
        shag_dobavit(db, task, author, tekst)
    for ssylka in data.get("ssylki") or []:
        ssylka_dobavit(db, task, author, ssylka.get("url"), ssylka.get("title"))
    _sobytie(db, task, author, "created")
    return task


def create_kazhdomu(db: Session, data: dict, author: User) -> list[Task]:
    """«Каждому своё»: по напоминанию на получателя, а не одно на всех.

    Одно на всех закрывает первый закрывший; «всем сдать отчёт» так не работает.
    """
    poluchateli = _poluchateli_iz(data, author)
    if len(poluchateli) < 2:
        return [create(db, data, author)]
    return [create(db, {**data, "poluchateli": [kto]}, author) for kto in poluchateli]


def update(db: Session, task_id: int, data: dict, author: User | None = None) -> Task:
    task = get_task(db, task_id)
    if "title" in data and data["title"] is not None:
        title = data["title"].strip()
        if not title:
            raise errors.ValidationError("Title is required", code="title_required")
        task.title = title[:MAX_TITLE]
    if "vazhnost" in data and data["vazhnost"] is not None:
        task.vazhnost = _vazhnost(data["vazhnost"])
    if "note" in data and data["note"] is not None:
        task.note = _zametka(data["note"])
    if data.get("poyas"):
        povtor_service.zona(data["poyas"])
        task.poyas = data["poyas"]
    if {"due_at", "ves_den", "povtor", "povtor_posle"} & data.keys():
        _srok(task, {**_srok_seychas(task), **data})
    if {"opovesheniya", "nastoychivo"} & data.keys():
        _zvonki(task, {**_zvonki_seychas(task), **data})
    for kolonka in PRIVYAZKI:
        if kolonka in data:
            setattr(task, kolonka, _privyazka(db, kolonka, data[kolonka]))
    if "obshchee" in data and data["obshchee"] is not None and bool(data["obshchee"]) != task.obshchee:
        if author is not None:
            _nuzhno_pravo_stavit(db, author)
        task.obshchee = bool(data["obshchee"])
    if "assignee_id" in data or "poluchateli" in data or "nablyudateli" in data:
        lyudi_zadat(db, task, author, data)
    if "is_done" in data and data["is_done"] is not None:
        if data["is_done"] and task.done_at is None:
            zakryt(db, task, author)
        elif not data["is_done"] and task.done_at is not None:
            otkryt(db, task, author)
    db.flush()
    return task


def _srok_seychas(task: Task) -> dict:
    return {
        "due_at": task.due_at, "ves_den": task.ves_den,
        "povtor": task.povtor, "povtor_posle": task.povtor_posle,
    }


def _zvonki_seychas(task: Task) -> dict:
    return {"opovesheniya": task.opovesheniya, "nastoychivo": task.nastoychivo}


def _srok(task: Task, data: dict) -> None:
    """Срок, «весь день» и повтор — вместе: повтору нужен срок, а «весь день» его меняет."""
    srok = to_utc_naive(data.get("due_at"))
    task.ves_den = bool(data.get("ves_den")) and srok is not None
    if task.ves_den:
        srok = povtor_service.ves_den_v_utc(povtor_service.mestnoe(srok, task.poyas).date(), task.poyas)
    pravilo = povtor_service.razobrat(data.get("povtor"))
    if pravilo and srok is None:
        raise errors.ValidationError("A repeating reminder needs a due date", code="povtor_bez_sroka")
    if srok != task.due_at or pravilo != task.povtor:
        task.povtor_nachalo = srok if pravilo else None
        task.sdelano_raz = 0
    task.due_at = srok
    task.povtor = pravilo
    task.povtor_posle = bool(data.get("povtor_posle")) and pravilo is not None


def _zvonki(task: Task, data: dict) -> None:
    task.opovesheniya = razobrat_opovesheniya(data.get("opovesheniya", "0"))
    nastoychivo = data.get("nastoychivo")
    if nastoychivo in (None, "", 0):
        task.nastoychivo = None
    elif int(nastoychivo) in NASTOYCHIVO:
        task.nastoychivo = int(nastoychivo)
    else:
        raise errors.ValidationError("Bad nag interval", code="nastoychivo_ne_to")


def razobrat_opovesheniya(znachenie) -> str:
    """«0,15,1440» — минуты до срока. Список тоже принимается. Пусто — напоминание молчит."""
    if znachenie is None:
        return ""
    kuski = znachenie if isinstance(znachenie, (list, tuple)) else str(znachenie).split(",")
    minuty: set[int] = set()
    for kusok in kuski:
        if str(kusok).strip() == "":
            continue
        try:
            chislo = int(kusok)
        except (TypeError, ValueError):
            raise errors.ValidationError("Alerts must be minutes", code="opovesheniya_ne_te") from None
        if not 0 <= chislo <= OPOVESHENIE_MAX_MINUT:
            raise errors.ValidationError("Alert is too far ahead", code="opovesheniya_ne_te")
        minuty.add(chislo)
    if len(minuty) > OPOVESHENIY_MAX:
        raise errors.ValidationError("Too many alerts", code="opovesheniya_ne_te")
    return ",".join(str(m) for m in sorted(minuty, reverse=True))


def _minuty(task: Task) -> list[int]:
    return [int(m) for m in task.opovesheniya.split(",") if m.strip()] if task.opovesheniya else []


def _privyazka(db: Session, kolonka: str, znachenie) -> int | None:
    blok, proverka = PRIVYAZKI[kolonka]
    if not znachenie:
        return None
    if not modules_service.is_enabled(db, blok):
        raise errors.ValidationError(f"Module {blok} is off", code="module_disabled")
    return proverka(db, znachenie)


def _vazhnost(slovo) -> str:
    """Важность — одно из четырёх слов. Чужое отвергаем, а не подменяем тихо:
    молча съеденное «срочно» — это напоминание, которое не заметят."""
    if slovo not in VAZHNOSTI:
        raise errors.ValidationError("Unknown importance", code="vazhnost_unknown")
    return slovo


def _zametka(tekst) -> str:
    """Подробности с потолком. Отказ, а не обрезка: человек, вставивший разбор
    на тридцать тысяч знаков, обязан узнать, что половина не сохранилась."""
    tekst = tekst or ""
    if len(tekst) > MAX_NOTE:
        raise errors.ValidationError("Details are too long", code="note_too_long")
    return tekst


# --- люди ---------------------------------------------------------------------


def _poluchateli_iz(data: dict, author: User | None) -> list[int]:
    """Получатели из запроса. Прежнее поле `assignee_id` понимаем: его шлют соседние блоки."""
    if data.get("poluchateli") is not None:
        spisok = data["poluchateli"]
    elif data.get("assignee_id"):
        spisok = [data["assignee_id"]]
    else:
        spisok = []
    return list(dict.fromkeys(int(x) for x in spisok if x))


def _nuzhno_pravo_stavit(db: Session, author: User) -> None:
    if not permissions_service.has(db, author, "tasks", "assign"):
        raise errors.ForbiddenError("Permission denied: tasks.assign", code="permission_denied")


def _lyudi_dobavit(db: Session, task: Task, author: User | None, poluchateli, nablyudateli) -> None:
    chuzhie = [kto for kto in [*poluchateli, *nablyudateli] if author is None or int(kto) != author.id]
    if chuzhie and author is not None:
        _nuzhno_pravo_stavit(db, author)
    novye = []
    for kto, poluchaet in [*((k, True) for k in poluchateli), *((k, False) for k in nablyudateli)]:
        kto = int(kto)
        if author is not None and kto == author.id:
            continue
        if tasks_repo.chlen(db, task.id, kto) is not None:
            continue
        references.user(db, kto, code="assignee_not_found", message="Assignee not found")
        tasks_repo.dobavit(db, TaskMember(task_id=task.id, user_id=kto, vladelets=False, poluchaet=poluchaet))
        if poluchaet:
            novye.append(kto)
    _skazat_poluchatelyam(db, task, author, novye)


def lyudi_zadat(db: Session, task: Task, author: User | None, data: dict) -> None:
    """Заменить получателей и наблюдателей. Владелец остаётся владельцем."""
    poluchateli = _poluchateli_iz(data, author) if ("poluchateli" in data or "assignee_id" in data) else None
    nablyudateli = [int(x) for x in data["nablyudateli"]] if data.get("nablyudateli") is not None else None
    lyudi = tasks_repo.lyudi(db, [task.id]).get(task.id, [])
    if poluchateli is not None:
        for chelovek in lyudi:
            nado = chelovek.user_id in poluchateli
            if chelovek.vladelets:
                chelovek.poluchaet = nado or (not poluchateli and not task.obshchee)
            elif chelovek.poluchaet and not nado:
                tasks_repo.ubrat(db, chelovek)
    if nablyudateli is not None:
        for chelovek in lyudi:
            if not chelovek.vladelets and not chelovek.poluchaet and chelovek.user_id not in nablyudateli:
                tasks_repo.ubrat(db, chelovek)
    db.flush()
    _lyudi_dobavit(db, task, author, poluchateli or [], nablyudateli or [])
    _sobytie(db, task, author, "assigned")


def vzyat_s_polki(db: Session, task: Task, kto: User) -> None:
    """Взять с общей полки: напоминание становится своим и звонит взявшему."""
    if not task.obshchee:
        raise errors.ConflictError("Not on the shared shelf", code="task_ne_na_polke")
    task.obshchee = False
    chelovek = tasks_repo.chlen(db, task.id, kto.id)
    if chelovek is None:
        tasks_repo.dobavit(db, TaskMember(task_id=task.id, user_id=kto.id, vladelets=True, poluchaet=True))
    else:
        chelovek.poluchaet = True
    _sobytie(db, task, kto, "assigned")


def _skazat_poluchatelyam(db: Session, task: Task, author: User | None, komu: list[int]) -> None:
    """Напоминание поставили другому — он узнаёт сразу, а не в срок."""
    from core.services import notification_service

    lyudi = [u for u in users_repo.get_many(db, set(komu)) if author is None or u.id != author.id]
    if lyudi:
        notification_service.notify(
            db, lyudi, "task_assigned", {"title": task.title}, f"/tasks?open={task.id}"
        )


def _sobytie(db: Session, task: Task, kto: User | None, vid: str, srok: datetime | None = None) -> None:
    tasks_repo.dobavit(db, TaskEvent(task_id=task.id, user_id=kto.id if kto else None, vid=vid, srok=srok))


# --- раз: закрыть, открыть, пропустить, отложить ------------------------------


def zakryt(db: Session, task: Task, kto: User | None) -> Task:
    """Закрыть раз. У повторяющегося срок уезжает на следующий раз, а не закрывается.

    Следующий — после сегодняшнего, а не после прошлого срока: закрыв напоминание,
    пролежавшее неделю, человек не должен получить шесть просроченных повторов.
    """
    teper = now_utc()
    srok = task.due_at
    _sobytie(db, task, kto, "done", srok)
    tasks_repo.zvonki_prinyat_vsem(db, task.id, teper)
    sleduyushchiy = _sleduyushchiy_raz(task, teper) if task.povtor else None
    task.sdelano_raz += 1
    if sleduyushchiy is None:
        task.done_at = teper
    else:
        task.due_at = sleduyushchiy
    for chelovek in tasks_repo.lyudi(db, [task.id]).get(task.id, []):
        chelovek.otlozheno_do = None
    _skazat_o_zakrytii(db, task, kto)
    return task


def _sleduyushchiy_raz(task: Task, teper: datetime) -> datetime | None:
    if task.povtor_posle:
        return povtor_service.posle_vypolneniya(
            task.povtor, task.due_at, task.poyas, teper, task.sdelano_raz + 1
        )
    posle = max(task.due_at, teper) if task.due_at else teper
    return povtor_service.sleduyushchiy(task.povtor, task.povtor_nachalo or task.due_at, task.poyas, posle)


def propustit(db: Session, task: Task, kto: User | None) -> Task:
    """Пропустить раз повторяющегося, не засчитав его сделанным."""
    if not task.povtor or task.done_at is not None:
        raise errors.ConflictError("Only an open repeating reminder can skip", code="task_ne_povtor")
    teper = now_utc()
    _sobytie(db, task, kto, "skipped", task.due_at)
    tasks_repo.zvonki_prinyat_vsem(db, task.id, teper)
    sleduyushchiy = povtor_service.sleduyushchiy(
        task.povtor, task.povtor_nachalo or task.due_at, task.poyas, max(task.due_at, teper)
    )
    if sleduyushchiy is None:
        task.done_at = teper
    else:
        task.due_at = sleduyushchiy
    return task


def otkryt(db: Session, task: Task, kto: User | None) -> Task:
    task.done_at = None
    _sobytie(db, task, kto, "reopened")
    return task


def otlozhit(db: Session, task: Task, kto: User, do: datetime) -> Task:
    """«Отложить» — у каждого своё: один отложил, другим звонит как звонило."""
    do = to_utc_naive(do)
    teper = now_utc()
    if do is None or do <= teper:
        raise errors.ValidationError("Snooze must end in the future", code="otlozhit_v_proshloe")
    chelovek = tasks_repo.chlen(db, task.id, kto.id)
    if chelovek is None:
        chelovek = TaskMember(task_id=task.id, user_id=kto.id, vladelets=False, poluchaet=True)
        tasks_repo.dobavit(db, chelovek)
    chelovek.otlozheno_do = do
    tasks_repo.zvonki_prinyat(db, kto.id, teper, task_id=task.id)
    _sobytie(db, task, kto, "snoozed", task.due_at)
    return task


def _skazat_o_zakrytii(db: Session, task: Task, kto: User | None) -> None:
    """Закрыли — узнают владелец и наблюдатели: ради этого их и ставят."""
    from core.services import notification_service

    komu = {
        chelovek.user_id
        for chelovek in tasks_repo.lyudi(db, [task.id]).get(task.id, [])
        if chelovek.vladelets or not chelovek.poluchaet
    }
    lyudi = [u for u in users_repo.get_many(db, komu) if kto is None or u.id != kto.id]
    if lyudi:
        notification_service.notify(
            db, lyudi, "task_done", {"title": task.title, "kto": kto.name if kto else ""},
            f"/tasks?open={task.id}",
        )


# --- шаги и ссылки ------------------------------------------------------------


def shag_dobavit(db: Session, task: Task, kto: User | None, tekst) -> TaskStep:
    tekst = (tekst or "").strip()
    if not tekst:
        raise errors.ValidationError("Step text is required", code="shag_pustoy")
    if len(tasks_repo.shagi(db, task.id)) >= MAX_SHAGOV:
        raise errors.ValidationError("Too many steps", code="shagov_mnogo")
    shag = TaskStep(task_id=task.id, text=tekst[:300], poryadok=tasks_repo.sleduyushchiy_poryadok(db, task.id))
    tasks_repo.dobavit(db, shag)
    return shag


def shag_pravit(db: Session, task: Task, step_id: int, data: dict) -> TaskStep:
    shag = tasks_repo.shag(db, task.id, step_id)
    if shag is None:
        raise errors.NotFoundError("Step not found", code="shag_not_found")
    if data.get("text") is not None:
        tekst = data["text"].strip()
        if not tekst:
            raise errors.ValidationError("Step text is required", code="shag_pustoy")
        shag.text = tekst[:300]
    if data.get("sdelan") is not None:
        shag.sdelan_at = now_utc() if data["sdelan"] else None
    db.flush()
    return shag


def shag_ubrat(db: Session, task: Task, step_id: int) -> None:
    shag = tasks_repo.shag(db, task.id, step_id)
    if shag is None:
        raise errors.NotFoundError("Step not found", code="shag_not_found")
    tasks_repo.ubrat(db, shag)


def ssylka_dobavit(db: Session, task: Task, kto: User | None, url, title=None) -> TaskUrl:
    """Ссылка — только http(s): `javascript:` в ссылке, которую нажимает коллега, — это удар."""
    url = (url or "").strip()
    if not url.lower().startswith(("http://", "https://")) or len(url) > MAX_URL:
        raise errors.ValidationError("Link must start with http:// or https://", code="ssylka_ne_ta")
    if len(tasks_repo.ssylki(db, task.id)) >= MAX_SSYLOK:
        raise errors.ValidationError("Too many links", code="ssylok_mnogo")
    ssylka = TaskUrl(
        task_id=task.id, url=url, title=(title or "").strip()[:200],
        poryadok=tasks_repo.sleduyushchiy_poryadok(db, task.id),
    )
    tasks_repo.dobavit(db, ssylka)
    return ssylka


def ssylka_ubrat(db: Session, task: Task, url_id: int) -> None:
    ssylka = tasks_repo.ssylka(db, task.id, url_id)
    if ssylka is None:
        raise errors.NotFoundError("Link not found", code="ssylka_not_found")
    tasks_repo.ubrat(db, ssylka)


# --- удаление -----------------------------------------------------------------


def delete(db: Session, task_id: int, actor: User | None = None) -> None:
    # Замок, а не просто чтение: пока мы перечисляем вложения, соседний запрос
    # успевает залить ещё одно. Его строку унёс бы каскад, а файл остался бы на
    # диске навсегда — потому что в нашем списке его не было (§3 CLAUDE.md).
    task = tasks_repo.zapert(db, task_id)
    if task is None:
        raise errors.NotFoundError("Task not found", code="task_not_found")
    for file in tasks_repo.files_of(db, task.id):
        audit_service.record_deletion(
            db,
            actor=actor,
            entity_type=audit_service.ENTITY_FILE,
            entity_id=file.id,
            entity_label=file.original_name,
        )
        _snyat_s_diska_posle_fiksatsii(db, file_path_on_disk(file))
    db.delete(task)
    db.flush()


# --- списки, календарь, счётчики ----------------------------------------------


def search(
    db: Session,
    user: User | None = None,
    scope: str = "open",
    kto: str = "vse",
    privyazki: dict | None = None,
    limit: int = LIST_LIMIT,
    **starye,
) -> list[Task]:
    """Списки, которыми пользуются каждый день.

    `scope`: open | overdue | today | week | done. Границы «сегодня» и «недели»
    считаются от текущего момента, а не от полуночи по UTC. Без `user` — все
    (соседние блоки спрашивают «что висит по этой заявке» мимо человека).
    """
    now = now_utc()
    horizon = {"today": timedelta(days=1), "week": timedelta(days=7)}.get(scope)
    privyazki = {**(privyazki or {}), **{k: v for k, v in starye.items() if k in PRIVYAZKI and v}}
    if kto not in tasks_repo.KTO:
        raise errors.ValidationError("Unknown list", code="kto_neizvesten")
    return tasks_repo.search(
        db,
        user_id=user.id if user else 0,
        vse=user is None or vidit_vse(db, user),
        scope=scope,
        kto=kto if user else "vse",
        now=now,
        until=now + horizon if horizon else None,
        privyazki=privyazki,
        limit=limit,
    )


def kalendar(db: Session, user: User, s: datetime, po: datetime) -> list[dict]:
    """Разы напоминаний в окне — по одному на каждый раз повторяющегося.

    Будущие разы повторяющегося — не строки базы, а расчёт по правилу: в базе
    лежит только ближайший открытый. Закрытые разы берутся из истории.
    """
    if po <= s or po - s > timedelta(days=62):
        raise errors.ValidationError("Calendar window is up to two months", code="okno_ne_to")
    zadachi = tasks_repo.v_okne(db, user_id=user.id, vse=vidit_vse(db, user), s=s, po=po, limit=KALENDAR_LIMIT)
    razy: list[dict] = []
    for task in zadachi:
        if task.done_at is not None:
            razy.append({"task": task, "srok": task.due_at or task.done_at, "sdelan": True})
            continue
        if s <= task.due_at < po:
            razy.append({"task": task, "srok": task.due_at, "sdelan": False})
        if task.povtor and not task.povtor_posle:
            for raz in povtor_service.v_okne(
                task.povtor, task.povtor_nachalo or task.due_at, task.poyas, max(s, task.due_at), po
            ):
                if raz > task.due_at:
                    razy.append({"task": task, "srok": raz, "sdelan": False, "budushchiy": True})
    povtory = [t.id for t in zadachi if t.povtor]
    po_nomeru = {t.id: t for t in zadachi}
    for sobytie in tasks_repo.sdelannye_v_okne(db, povtory, s, po):
        razy.append({"task": po_nomeru[sobytie.task_id], "srok": sobytie.srok, "sdelan": True})
    razy.sort(key=lambda r: (r["srok"], r["task"].id))
    return razy


def summary(db: Session, user: User) -> dict:
    """Счётчики для навигации: без них в напоминания заходят «на всякий случай»."""
    now = now_utc()
    return tasks_repo.counters(
        db, user_id=user.id, vse=vidit_vse(db, user), now=now, today_until=now + timedelta(days=1)
    )


# --- звонки -------------------------------------------------------------------


def _poluchateli_zvonka(lyudi: list[TaskMember]) -> list[TaskMember]:
    return [chelovek for chelovek in lyudi if chelovek.poluchaet]


def tick(db: Session, teper: datetime | None = None) -> int:
    """Шаг планировщика: записать звонки, чья минута наступила. Возвращает, сколько позвонило.

    Окно — последние `OKNO_ZVONKA`: процессов несколько и сервис перезапускается,
    а уникальный ключ звонка (docs/bloki/29 §6) не даёт позвонить дважды.
    """
    teper = teper or now_utc()
    if not modules_service.is_enabled(db, "tasks"):
        return 0
    s = teper - OKNO_ZVONKA
    kandidaty = tasks_repo.kandidaty_zvonka(
        db, teper - NASTOYCHIVO_OKNO, teper + timedelta(minutes=OPOVESHENIE_MAX_MINUT)
    )
    lyudi = tasks_repo.lyudi(db, [t.id for t in kandidaty])
    zvonkov = 0
    for task in kandidaty:
        momenty: list[tuple[datetime, str]] = []
        for minut in _minuty(task):
            moment = task.due_at - timedelta(minutes=minut)
            if s < moment <= teper:
                momenty.append((moment, "due" if minut == 0 else "early"))
        if task.nastoychivo and task.due_at < teper:
            for k in range(1, NASTOYCHIVO_RAZ + 1):
                moment = task.due_at + timedelta(minutes=task.nastoychivo * k)
                if s < moment <= teper:
                    momenty.append((moment, "nag"))
        if not momenty:
            continue
        for chelovek in _poluchateli_zvonka(lyudi.get(task.id, [])):
            for moment, vid in momenty:
                # Минута, пришедшаяся на «отложить», не звонит и потом: иначе
                # проснувшийся получал бы разом всё, что копилось за отсрочку.
                if chelovek.otlozheno_do and moment <= chelovek.otlozheno_do:
                    continue
                if vid == "nag" and tasks_repo.prinyal_li(db, task.id, chelovek.user_id, task.due_at):
                    continue
                zvonkov += _pozvonit(db, task, chelovek.user_id, moment, vid)

    for chelovek in tasks_repo.otlozhennye_v_okne(db, s, teper):
        task = tasks_repo.get(db, chelovek.task_id)
        if task is not None and task.done_at is None and chelovek.poluchaet:
            zvonkov += _pozvonit(db, task, chelovek.user_id, chelovek.otlozheno_do, "snooze")
    return zvonkov


def _pozvonit(db: Session, task: Task, user_id: int, moment: datetime, vid: str) -> int:
    """Записать звонок и оповестить. Ключ уже занят — звонил другой процесс: тишина."""
    from core.services import notification_service, push_service

    zapisan = tasks_repo.zvonok_zapisat(
        db, TaskSignal(task_id=task.id, user_id=user_id, moment=moment, srok=task.due_at, vid=vid)
    )
    if not zapisan:
        return 0
    komu = users_repo.get_by_id(db, user_id)
    if komu is not None:
        notification_service.notify(
            db, [komu], "task_signal", {"title": task.title, "vid": vid}, f"/tasks?open={task.id}"
        )
        push_service.v_ochered(db, task, user_id, vid)
    return 1


def zvonki_moi(db: Session, user: User) -> list[TaskSignal]:
    return tasks_repo.zvonki_nerazobrannye(db, user.id, now_utc() - ZVONKI_SVEZHIE)


def zvonki_prinyat(db: Session, user: User, signal_ids) -> int:
    return tasks_repo.zvonki_prinyat(db, user.id, now_utc(), signal_ids=signal_ids)


def zvonki_ubrat_starye(db: Session) -> int:
    return tasks_repo.zvonki_ubrat_starye(db, now_utc() - ZVONKI_HRANIT)


# --- вложения -----------------------------------------------------------------


def files(db: Session, task_id: int) -> list[TaskFile]:
    return tasks_repo.files_of(db, task_id)


def files_counts(db: Session, task_ids) -> dict[int, int]:
    return tasks_repo.counts_of_files(db, task_ids)


def zametki_est(db: Session, task_ids) -> set[int]:
    return tasks_repo.nepustye_zametki(db, task_ids)


def _files_dir(task_id: int) -> Path:
    return get_settings().task_files_dir.joinpath(str(task_id))


def file_path_on_disk(file: TaskFile) -> Path:
    return _files_dir(file.task_id).joinpath(f"{file.file_uid}{Path(file.original_name).suffix}")


def add_file(db: Session, task_id: int, uploader: User, original_name: str, content: bytes) -> TaskFile:
    """Фото или видео к напоминанию. Приёмка общая с файлами клиента.

    Замок на напоминание — пара к такому же в `delete`: без него заливка и
    снос расходятся так, что файл остаётся на диске без строки в базе.
    """
    task = tasks_repo.zapert(db, task_id)
    if task is None:
        raise errors.NotFoundError("Task not found", code="task_not_found")
    ext, content = client_service.proverit_vlozhenie(original_name, content, VLOZHENIYA)
    file = tasks_repo.add_file(
        db,
        TaskFile(
            task_id=task.id,
            uploaded_by=uploader.id,
            file_uid=tokens.new_file_uid(),
            original_name=Path(original_name).name[:255],
            # Присланный `Content-Type` не сохраняем вовсе: его выбирает тот,
            # кто загружает, а уходит он в заголовок ответа сотруднику.
            mime=client_service.MIME_PO_RASSHIRENIYU.get(ext, "application/octet-stream"),
            size_bytes=len(content),
        ),
    )
    directory = _files_dir(task.id)
    directory.mkdir(parents=True, exist_ok=True)
    file_path_on_disk(file).write_bytes(content)
    return file


def mime_dlya_otdachi(file: TaskFile) -> str:
    """Чем отдавать вложение. Считается из имени, а не берётся из записи: заголовок
    ответа не должен зависеть от того, что когда-то положили в строку."""
    ext = Path(file.original_name).suffix.lstrip(".").lower()
    return client_service.MIME_PO_RASSHIRENIYU.get(ext, "application/octet-stream")


def get_file(db: Session, task_id: int, file_id: int) -> TaskFile:
    file = tasks_repo.get_file(db, task_id, file_id)
    if file is None:
        raise errors.NotFoundError("File not found", code="file_not_found")
    return file


def delete_file(db: Session, task_id: int, file_id: int, actor: User) -> None:
    file = get_file(db, task_id, file_id)
    _snyat_s_diska_posle_fiksatsii(db, file_path_on_disk(file))
    audit_service.record_deletion(
        db,
        actor=actor,
        entity_type=audit_service.ENTITY_FILE,
        entity_id=file.id,
        entity_label=file.original_name,
    )
    tasks_repo.drop_file(db, file)


def _snyat_s_diska_posle_fiksatsii(db: Session, path: Path) -> None:
    # После коммита, а не сразу: откат вернул бы строку, а файла уже нет.
    @sa_event.listens_for(db, "after_commit", once=True)
    def _ubrat(_session) -> None:
        path.unlink(missing_ok=True)
        # И пустой каталог следом: напоминания заводятся сами — от пропущенного
        # звонка и заявки с сайта, — и пустые каталоги копились бы тысячами.
        try:
            path.parent.rmdir()
        except OSError:
            pass
