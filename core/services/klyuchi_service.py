"""Блок «Ключи»: хранилище вторых факторов и коды к ним.

**Секрет не выходит из сервера.** Коды считаются здесь и уезжают на экран уже
цифрами; сам ключ отдаётся ровно в одном месте — окне переноса на телефон, и
только root, и отдельной записью в журнале. Отдай мы секрет вместе с карточкой
— он осел бы в кэше вкладки, в снимке экрана и в отчёте об ошибке браузера,
причём у всех, кто карточку открывал.

**Время берётся здесь же.** Код зависит от часов, а часы на машине человека
идут как получится; поэтому экран получает и код, и сколько ему осталось жить,
а сам только отсчитывает. Разбор — `docs/bloki/27-klyuchi.md` §5.

**Кто что видит — вопрос запроса, а не отбора после него.** Условие живёт в
`database/repositories/klyuchi._vidno`; здесь только правила, которых в SQL
нет: root проходит мимо, создатель не снимается со своего ключа, закрытая
категория поимённых доступов не принимает.
"""

from __future__ import annotations

import json
import re
import secrets
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from core import exceptions as errors
from core.security import secretbox, totp
from core.services import audit_service, codes, modules_service, permissions_service, znaki_service
from core.utils import now_utc
from database.models import TwoFactorKey, User
from database.models.audit import SOURCE_MANUAL
from database.models.task import VAZHNOSTI, VAZHNOST_PO_UMOLCHANIYU
from database.models.user import ROLE_ROOT, STATUS_ACTIVE
from database.repositories import audit as audit_repo
from database.repositories import klyuchi as klyuchi_repo
from database.repositories import tasks as tasks_repo
from database.repositories import users as users_repo

#: Назначения шифрования. Разные у секрета и у запасных кодов: один ключ в двух
#: ролях — классическая ошибка, и `secretbox` разводит их именно `purpose`.
SECRET_PURPOSE = "2fa-key-secret"
BACKUP_PURPOSE = "2fa-backup-codes"

MAX_NAZVANIE = 120
MAX_UCHYOTKA = 120
MAX_SERVIS = 80
MAX_KATEGORIYA = 60
MAX_ZAMETKA = 20_000
#: Запасных кодов сервисы дают до нескольких десятков; сотня — уже не список, а
#: вставленный по ошибке кусок файла.
MAX_ZAPASNYH = 100
MAX_DLINA_ZAPASNOGO = 64

#: Сколько живёт окно переноса на телефон. Минута — не украшение: снятый код
#: продолжает открывать сервис и после закрытия доступа в CRM, и держать его на
#: экране «пока не закроют» значит оставить его открытым на ночь.
PERENOS_SEKUND = 60

#: Через сколько показ ТОГО ЖЕ ключа ТЕМ ЖЕ человеком пишется в журнал заново.
#:
#: Прежде это решал параметр запроса `silent`, то есть сам вызывающий: строка
#: `POST /keys/7/code?silent=1` из консоли браузера снимала код без единого
#: следа — а `key.shown` и есть единственный след того, что код сняли. Окном
#: решает сервер, и выключить его снаружи нечем; повторы внутри окна сходятся в
#: одну запись, чтобы открытый экран не давал сто двадцать строк в час.
POKAZ_OKNO_SEKUND = 300

_PROBELY = re.compile(r"\s+")


# --- разбор и проверки -------------------------------------------------------


def _stroka(znachenie, predel: int) -> str:
    return _PROBELY.sub(" ", str(znachenie or "").strip())[:predel]


def _vazhnost(znachenie) -> str:
    slovo = str(znachenie or "").strip().lower()
    if slovo and slovo not in VAZHNOSTI:
        raise errors.ValidationError("Unknown importance", code="key_bad_importance")
    return slovo or VAZHNOST_PO_UMOLCHANIYU


def _zametka(znachenie) -> str:
    tekst = str(znachenie or "")
    if len(tekst) > MAX_ZAMETKA:
        raise errors.ValidationError("Note is too long", code="key_note_too_long")
    return tekst


def razobrat(stroka: str) -> dict:
    """Что система поняла из строки сервиса — ДО сохранения.

    Показывается человеку сразу: вставил не то — видно тут же, а не через
    тридцать секунд по коду, который никуда не подходит. Проверочный код
    считается здесь же, на настоящем секрете.
    """
    try:
        razbor = totp.razobrat(stroka)
    except totp.NeTaStroka as beda:
        raise errors.ValidationError(str(beda), code="key_bad_secret") from None
    seychas = int(now_utc().timestamp())
    znak = znaki_service.nayti(razbor["servis"])
    return {
        "issuer": razbor["servis"],
        "account": razbor["uchyotka"],
        "digits": razbor["cifr"],
        "period": razbor["shag"],
        "algorithm": razbor["algoritm"],
        "znak": znak,
        "proverka": totp.kod(
            razbor["sekret"],
            seychas=seychas,
            cifr=razbor["cifr"],
            shag=razbor["shag"],
            algoritm=razbor["algoritm"],
        ),
    }


# --- права -------------------------------------------------------------------


def _root(actor: User) -> bool:
    return actor.role == ROLE_ROOT


def _kto_vidit(actor: User) -> int | None:
    """`None` — смотрящий проходит мимо условия видимости (root)."""
    return None if _root(actor) else actor.id


def dostat(db: Session, actor: User, key_id: int) -> TwoFactorKey:
    """Ключ, который этому человеку вправду видно. Иначе — «нет такого».

    Именно «нет такого», а не «нельзя»: отказ по правам сам по себе рассказал
    бы, что ключ с таким номером существует, — а список чужих вторых факторов
    это тоже сведения.
    """
    klyuch = klyuchi_repo.get(db, key_id)
    if klyuch is None:
        raise errors.NotFoundError("Key not found", code="key_not_found")
    if not _root(actor) and not klyuchi_repo.vidit_li(db, actor.id, key_id):
        raise errors.NotFoundError("Key not found", code="key_not_found")
    return klyuch


def _pravit(actor: User, klyuch: TwoFactorKey) -> None:
    """Править и раздавать доступ может создатель или root."""
    if _root(actor) or klyuch.created_by == actor.id:
        return
    raise errors.ForbiddenError("Only the key owner can change it", code="key_not_owner")


# --- список ------------------------------------------------------------------


def _znak(klyuch: TwoFactorKey) -> dict | None:
    """Фирменный знак сервиса. Не хранится: он производное от имени сервиса и
    набора значков, а производное в этой системе не хранят."""
    return znaki_service.nayti(klyuch.issuer or klyuch.title)


def _ne_otkrylis(klyuch: TwoFactorKey) -> bool:
    """Запасные коды ЛЕЖАТ, но нынешним ключом шифрования не открываются.

    Третье состояние, и его нельзя схлопывать в «не заводили»: так выглядит
    сменившийся `OPENCRM_SECRET_KEY` (или недоехавшая перекладка после заливки
    копии с чужой машины). Скажи мы «их нет» — человек не пойдёт искать старый
    ключ, пока старая машина ещё жива, а именно в этом весь смысл вопроса
    «войти нечем, где запасные». Тем же способом отвечает почта:
    `mail_password_undecryptable`.
    """
    if not klyuch.backup_codes_encrypted:
        return False
    try:
        secretbox.decrypt(klyuch.backup_codes_encrypted, BACKUP_PURPOSE)
    except (secretbox.SecretBoxError, ValueError):
        return True
    return False


def _zapasnye(klyuch: TwoFactorKey) -> list[dict]:
    if not klyuch.backup_codes_encrypted:
        return []
    try:
        spisok = json.loads(secretbox.decrypt(klyuch.backup_codes_encrypted, BACKUP_PURPOSE))
    except (secretbox.SecretBoxError, ValueError):
        # Ронять карточку из-за запасных кодов нельзя — сам ключ при этом может
        # быть цел. Что список НЕ ПУСТ, а нечитаем, говорит `_ne_otkrylis`.
        return []
    return [x for x in spisok if isinstance(x, dict) and x.get("kod")]


def _zadacha(db: Session, klyuch: TwoFactorKey, zadachi: dict | None):
    """Напоминание карточки — или ничего, если блок напоминаний выключен.

    Выключенный блок исчезает целиком (§3 CLAUDE.md): не только меню, но и срок
    на чужой карточке со ссылкой `/tasks?id=…`, ведущей на страницу, которой в
    выключенной системе нет.
    """
    if not klyuch.task_id:
        return None
    if zadachi is not None:
        return zadachi.get(klyuch.task_id)
    if not modules_service.is_enabled(db, "tasks"):
        return None
    return tasks_repo.get(db, klyuch.task_id)


def kratko(
    db: Session,
    actor: User,
    klyuch: TwoFactorKey,
    vidyat: int = 0,
    zadachi: dict | None = None,
) -> dict:
    """Карточка без кода и без секрета — то, что видно в списке.

    `zadachi` — уже собранные напоминания. Без них карточка добирает своё
    запросом, и на списке это двенадцать запросов вместо одного.
    """
    zapasnye = _zapasnye(klyuch)
    zadacha = _zadacha(db, klyuch, zadachi)
    return {
        "note": klyuch.note or "",
        "seen_by": vidyat,
        "id": klyuch.id,
        "title": klyuch.title,
        "issuer": klyuch.issuer,
        "account": klyuch.account,
        "category_id": klyuch.category_id,
        "vazhnost": klyuch.vazhnost,
        "digits": klyuch.digits,
        "period": klyuch.period,
        "znak": _znak(klyuch),
        "mine": klyuch.created_by == actor.id,
        "can_edit": _root(actor) or klyuch.created_by == actor.id,
        "backup_total": len(zapasnye),
        "backup_left": sum(1 for z in zapasnye if not z.get("potrachen")),
        # «Не заводили» и «лежат, но не открываются» — разные ответы: см.
        # `_ne_otkrylis`.
        "backup_zakryty": _ne_otkrylis(klyuch),
        "task": (
            {
                "id": zadacha.id,
                "title": zadacha.title,
                "due_at": zadacha.due_at.isoformat() if zadacha.due_at else None,
                "done": zadacha.done_at is not None,
            }
            if zadacha is not None
            else None
        ),
        "created_at": klyuch.created_at.isoformat() if klyuch.created_at else None,
        "deleted_at": klyuch.deleted_at.isoformat() if klyuch.deleted_at else None,
    }


def spisok(
    db: Session,
    actor: User,
    *,
    category_id: int | None = None,
    tolko_svoi: bool = False,
    poisk: str = "",
    v_korzine: bool = False,
    tolko_prosyat: bool = False,
) -> dict:
    kto = _kto_vidit(actor)
    # Выключенный блок напоминаний уносит с карточек срок, а из шапки — тревогу
    # вместе с отбором по ней (§3 CLAUDE.md: блок исчезает целиком).
    s_napominaniyami = modules_service.is_enabled(db, "tasks")
    teper = now_utc().replace(tzinfo=None)
    klyuchi = klyuchi_repo.spisok(
        db,
        kto,
        category_id=category_id,
        tolko_svoi=tolko_svoi,
        poisk=_stroka(poisk, 120),
        v_korzine=v_korzine,
        # Отбор «просят обновления» — запросом, а не вычитанием из показанного:
        # число в шапке считается по всему разделу, и отбор по одной полке давал
        # бы под ненулевым числом пустой список.
        prosrocheno=teper if (tolko_prosyat and s_napominaniyami) else None,
    )
    po_kategoriyam = klyuchi_repo.po_kategoriyam(db, kto)
    kategorii = []
    for kat in klyuchi_repo.kategorii(db):
        zakryta_dlya_menya = kat.zakrytaya and not _root(actor) and kat.created_by != actor.id
        kategorii.append(
            {
                "id": kat.id,
                "name": kat.name,
                "zakrytaya": kat.zakrytaya,
                # Закрытая чужая видна по имени и числу, но не по содержимому:
                # спрячь её целиком — и рядом заведут вторую такую же.
                "zakryta_dlya_menya": zakryta_dlya_menya,
                "count": po_kategoriyam.get(kat.id, 0),
                "mine": kat.created_by == actor.id,
            }
        )
    vidyat = klyuchi_repo.skolko_vidyat(db, [k.id for k in klyuchi])
    # Пустой словарь, а не `None`: с `None` каждая карточка пошла бы за своим
    # напоминанием сама и вернула бы то, чего в выключенной системе нет.
    zadachi = tasks_repo.po_nomeram(db, [k.task_id for k in klyuchi]) if s_napominaniyami else {}
    return {
        "items": [kratko(db, actor, k, vidyat.get(k.id, 0), zadachi) for k in klyuchi],
        # Тревога одна на весь раздел, а не по выбранной полке: число, меняющееся
        # от выбора категории, читалось бы как «здесь просрочено столько».
        "prosyat": klyuchi_repo.prosyat_obnovleniya(db, kto, teper) if s_napominaniyami else 0,
        "categories": kategorii,
        "total": klyuchi_repo.skolko(db, kto),
        "mine": klyuchi_repo.svoih(db, actor.id),
        "trash": klyuchi_repo.skolko(db, kto, v_korzine=True),
        "bez_kategorii": po_kategoriyam.get(None, 0),
    }


# --- заведение и правка ------------------------------------------------------


def _kategoriya_po_nazvaniyu(db: Session, actor: User, nazvanie: str) -> int | None:
    """Категория по имени: есть — берём, нет — заводим. Пусто — вне категорий.

    Заводить прямо из формы ключа, а не отдельной кнопкой: человек пишет
    «Бухгалтерия» и ждёт, что она появится, а не что ему откажут.

    **Чужая закрытая по имени не берётся.** Её имя видно всем в левой колонке,
    поле категории свободного ввода, а `_vidno.svoya_zakrytaya` пускает хозяина
    категории к ЛЮБОМУ ключу внутри. То есть достаточно было набрать чужое имя,
    чтобы отдать свой второй фактор чужому человеку — молча и не заметив.
    Отказ, а не тихое заведение второй такой же: имя категории одно на систему.
    """
    imya = _stroka(nazvanie, MAX_KATEGORIYA)
    if not imya:
        return None
    est = klyuchi_repo.kategoriya_po_imeni(db, imya)
    if est is not None:
        if est.zakrytaya and not _root(actor) and est.created_by != actor.id:
            raise errors.ValidationError(
                "This category is closed by someone else",
                code="key_category_foreign_closed",
            )
        return est.id
    return klyuchi_repo.sozdat_kategoriyu(db, name=imya, created_by=actor.id).id


def _napominanie(db: Session, actor: User, nomer) -> int | None:
    """Номер напоминания, которое этому человеку вправду можно привязать.

    Без проверки карточка отдавала бы `title` и `due_at` ЛЮБОЙ задачи: перебери
    `task_id` на своём же ключе — и весь список напоминаний фирмы прочитан мимо
    блока, который его охраняет.
    """
    if not nomer:
        return None
    if not modules_service.is_enabled(db, "tasks"):
        raise errors.ValidationError("Reminders module is off", code="key_tasks_off")
    if not permissions_service.has(db, actor, "tasks", "view"):
        raise errors.ForbiddenError("Permission required: tasks.view", code="permission_denied")
    if tasks_repo.get(db, int(nomer)) is None:
        raise errors.NotFoundError("Task not found", code="task_not_found")
    return int(nomer)


def sozdat(db: Session, actor: User, dannye: dict) -> TwoFactorKey:
    try:
        razbor = totp.razobrat(str(dannye.get("secret") or ""))
    except totp.NeTaStroka as beda:
        raise errors.ValidationError(str(beda), code="key_bad_secret") from None

    nazvanie = _stroka(dannye.get("title"), MAX_NAZVANIE) or _stroka(
        razbor["servis"] or razbor["uchyotka"] or "Ключ", MAX_NAZVANIE
    )
    klyuch = klyuchi_repo.sozdat(
        db,
        title=nazvanie,
        issuer=_stroka(dannye.get("issuer") or razbor["servis"], MAX_SERVIS),
        account=_stroka(dannye.get("account") or razbor["uchyotka"], MAX_UCHYOTKA),
        secret_encrypted=secretbox.encrypt(razbor["sekret"], SECRET_PURPOSE),
        digits=razbor["cifr"],
        period=razbor["shag"],
        algorithm=razbor["algoritm"],
        vazhnost=_vazhnost(dannye.get("vazhnost")),
        note=_zametka(dannye.get("note")),
        category_id=_kategoriya_po_nazvaniyu(db, actor, dannye.get("category")),
        created_by=actor.id,
    )
    if dannye.get("backup_codes") is not None:
        _polozhit_zapasnye(klyuch, dannye.get("backup_codes"))
    audit_service.record(
        db,
        actor=actor,
        source=SOURCE_MANUAL,
        action=audit_service.ACTION_KEY_CREATED,
        entity_type=audit_service.ENTITY_TWOFACTOR,
        entity_id=klyuch.id,
        entity_label=klyuch.title,
    )
    return klyuch


def pravit(db: Session, actor: User, key_id: int, dannye: dict) -> TwoFactorKey:
    klyuch = dostat(db, actor, key_id)
    _pravit(actor, klyuch)
    if "title" in dannye:
        novoe = _stroka(dannye.get("title"), MAX_NAZVANIE)
        if not novoe:
            raise errors.ValidationError("Title is empty", code="key_title_empty")
        klyuch.title = novoe
    if "account" in dannye:
        klyuch.account = _stroka(dannye.get("account"), MAX_UCHYOTKA)
    if "issuer" in dannye:
        klyuch.issuer = _stroka(dannye.get("issuer"), MAX_SERVIS)
    if "vazhnost" in dannye:
        klyuch.vazhnost = _vazhnost(dannye.get("vazhnost"))
    if "note" in dannye:
        klyuch.note = _zametka(dannye.get("note"))
    if "category" in dannye:
        klyuch.category_id = _kategoriya_po_nazvaniyu(db, actor, dannye.get("category"))
    if "task_id" in dannye:
        klyuch.task_id = _napominanie(db, actor, dannye.get("task_id"))
    if "backup_codes" in dannye:
        _polozhit_zapasnye(klyuch, dannye.get("backup_codes"))
    audit_service.record(
        db,
        actor=actor,
        source=SOURCE_MANUAL,
        action=audit_service.ACTION_KEY_UPDATED,
        entity_type=audit_service.ENTITY_TWOFACTOR,
        entity_id=klyuch.id,
        entity_label=klyuch.title,
    )
    return klyuch


def udalit(db: Session, actor: User, key_id: int) -> None:
    """В корзину. Секрет остаётся зашифрованным на месте: «удалить» и «стереть»
    здесь разные слова, и второе делает `steret`."""
    klyuch = dostat(db, actor, key_id)
    zapert = klyuchi_repo.zapert(db, key_id)
    if zapert is None:
        raise errors.NotFoundError("Key not found", code="key_not_found")
    _pravit(actor, zapert)
    klyuchi_repo.udalit(db, zapert, now_utc().replace(tzinfo=None))
    audit_service.record(
        db,
        actor=actor,
        source=SOURCE_MANUAL,
        action=audit_service.ACTION_KEY_DELETED,
        entity_type=audit_service.ENTITY_TWOFACTOR,
        entity_id=klyuch.id,
        entity_label=klyuch.title,
    )


def vernut(db: Session, actor: User, key_id: int) -> TwoFactorKey:
    klyuch = dostat(db, actor, key_id)
    _pravit(actor, klyuch)
    klyuchi_repo.vernut(db, klyuch)
    audit_service.record(
        db,
        actor=actor,
        source=SOURCE_MANUAL,
        action=audit_service.ACTION_KEY_RESTORED,
        entity_type=audit_service.ENTITY_TWOFACTOR,
        entity_id=klyuch.id,
        entity_label=klyuch.title,
    )
    return klyuch


def steret(db: Session, actor: User, key_id: int) -> None:
    """Насовсем. Восстановить нечем: секрет не выводится из данных."""
    klyuch = dostat(db, actor, key_id)
    _pravit(actor, klyuch)
    if klyuch.deleted_at is None:
        raise errors.ValidationError("Move the key to the trash first", code="key_not_in_trash")
    nazvanie = klyuch.title
    nomer = klyuch.id
    klyuchi_repo.steret(db, klyuch)
    audit_service.record_deletion(
        db,
        actor=actor,
        entity_type=audit_service.ENTITY_TWOFACTOR,
        entity_id=nomer,
        entity_label=nazvanie,
    )


# --- коды --------------------------------------------------------------------


def kod(db: Session, actor: User, key_id: int) -> dict:
    """Нынешний код и сколько ему осталось. Показ всегда идёт в журнал.

    Повторы того же ключа тем же человеком сходятся в одну запись за окно
    `POKAZ_OKNO_SEKUND` — там и разбор, почему это решает сервер, а не флаг в
    запросе.
    """
    klyuch = dostat(db, actor, key_id)
    if klyuch.deleted_at is not None:
        raise errors.ValidationError("The key is in the trash", code="key_in_trash")
    sekret = secretbox.decrypt(klyuch.secret_encrypted, SECRET_PURPOSE)
    teper = now_utc()
    seychas = int(teper.timestamp())
    uzhe_pisali = audit_repo.bylo_nedavno(
        db,
        actor_id=actor.id,
        action=audit_service.ACTION_KEY_SHOWN,
        entity_type=audit_service.ENTITY_TWOFACTOR,
        entity_id=klyuch.id,
        ne_ranshe=(teper - timedelta(seconds=POKAZ_OKNO_SEKUND)).replace(tzinfo=None),
    )
    if not uzhe_pisali:
        audit_service.record(
            db,
            actor=actor,
            source=SOURCE_MANUAL,
            action=audit_service.ACTION_KEY_SHOWN,
            entity_type=audit_service.ENTITY_TWOFACTOR,
            entity_id=klyuch.id,
            entity_label=klyuch.title,
        )
    return {
        "code": totp.kod(
            sekret,
            seychas=seychas,
            cifr=klyuch.digits,
            shag=klyuch.period,
            algoritm=klyuch.algorithm,
        ),
        "ostalos": totp.ostalos(seychas, klyuch.period),
        "period": klyuch.period,
    }


def sekret(db: Session, actor: User, key_id: int) -> dict:
    """Сам ключ строкой и `otpauth://` для QR — перенос на телефон.

    **Только root, и всегда с записью в журнал** (решение владельца
    06.09.2026). Снятый код живёт своей жизнью: телефон, на который его
    перенесли, открывает сервис и после того, как доступ в CRM закроют.
    """
    if not _root(actor):
        raise errors.ForbiddenError("Only root can read the secret", code="key_secret_root_only")
    klyuch = dostat(db, actor, key_id)
    otkrytyy = secretbox.decrypt(klyuch.secret_encrypted, SECRET_PURPOSE)
    audit_service.record(
        db,
        actor=actor,
        source=SOURCE_MANUAL,
        action=audit_service.ACTION_KEY_SECRET_SHOWN,
        entity_type=audit_service.ENTITY_TWOFACTOR,
        entity_id=klyuch.id,
        entity_label=klyuch.title,
    )
    stroka = totp.otpauth(
        sekret=otkrytyy,
        servis=klyuch.issuer or klyuch.title,
        uchyotka=klyuch.account,
        cifr=klyuch.digits,
        shag=klyuch.period,
        algoritm=klyuch.algorithm,
    )
    return {
        "secret": otkrytyy,
        "otpauth": stroka,
        # QR рисует сервер той же `segno`, что печатает коды на бланках: свой
        # рисовальщик в браузере — это Рид-Соломон и таблицы версий, которых в
        # CRM не пишут (`core/services/codes.py`).
        "qr": codes.qr_svg(stroka, scale=4.0),
        "zakroetsya_cherez": PERENOS_SEKUND,
    }


# --- запасные коды -----------------------------------------------------------


def _polozhit_zapasnye(klyuch: TwoFactorKey, syroe) -> None:
    """Список запасных кодов сервиса. Пустой список — «завели и все потратили»,
    `None` — «не заводили вовсе»; это разные состояния.

    **Нечитаемый список не затирается.** Шифротекст, не открывшийся нынешним
    ключом, — данные восстановимые: старый ключ ещё может найтись, а перекладка
    (`sekrety_service.perelozhit`) для того и написана. Перезаписав его, мы
    делаем потерю окончательной, и делаем это молча.
    """
    if _ne_otkrylis(klyuch):
        raise errors.ValidationError(
            "The stored backup codes cannot be decrypted with the current key",
            code="key_backup_undecryptable",
        )
    if syroe is None:
        klyuch.backup_codes_encrypted = None
        return
    if isinstance(syroe, str):
        syroe = [s for s in re.split(r"[\s,;]+", syroe) if s]
    if not isinstance(syroe, list):
        raise errors.ValidationError("Backup codes must be a list", code="key_bad_backup_codes")
    if len(syroe) > MAX_ZAPASNYH:
        raise errors.ValidationError("Too many backup codes", code="key_too_many_backup_codes")
    spisok = []
    for zapis in syroe:
        if isinstance(zapis, dict):
            tekst, potrachen = str(zapis.get("kod") or ""), bool(zapis.get("potrachen"))
        else:
            tekst, potrachen = str(zapis or ""), False
        tekst = tekst.strip()[:MAX_DLINA_ZAPASNOGO]
        if tekst:
            spisok.append({"kod": tekst, "potrachen": potrachen})
    klyuch.backup_codes_encrypted = (
        secretbox.encrypt(json.dumps(spisok, ensure_ascii=False), BACKUP_PURPOSE)
        if spisok
        else None
    )


def zapasnye(db: Session, actor: User, key_id: int) -> dict:
    klyuch = dostat(db, actor, key_id)
    spisok = _zapasnye(klyuch)
    return {
        "items": spisok,
        "left": sum(1 for z in spisok if not z.get("potrachen")),
        "total": len(spisok),
        "zakryty": _ne_otkrylis(klyuch),
    }


def potratit_zapasnoy(db: Session, actor: User, key_id: int, nomer: int) -> dict:
    """Вычеркнуть код: им уже вошли, и второй раз он не сработает.

    Вычёркивание, а не удаление: список от сервиса один, и дырка в нём вместо
    зачёркнутой строки сдвинула бы остальные — человек читал бы не тот код.

    **Под замком: список переписывается ЦЕЛИКОМ.** Двое, вычеркнувшие разные
    коды разом, читают одинаковый список и пишут его один поверх другого —
    вычеркнутый первым снова показан годным и будет выдан второй раз (§3
    CLAUDE.md: посчитал — заперся — записал).
    """
    dostat(db, actor, key_id)
    klyuch = klyuchi_repo.zapert(db, key_id)
    if klyuch is None:
        raise errors.NotFoundError("Key not found", code="key_not_found")
    spisok = _zapasnye(klyuch)
    if not 0 <= nomer < len(spisok):
        raise errors.NotFoundError("No such backup code", code="key_backup_not_found")
    spisok[nomer]["potrachen"] = True
    _polozhit_zapasnye(klyuch, spisok)
    audit_service.record(
        db,
        actor=actor,
        source=SOURCE_MANUAL,
        action=audit_service.ACTION_KEY_BACKUP_SPENT,
        entity_type=audit_service.ENTITY_TWOFACTOR,
        entity_id=klyuch.id,
        entity_label=klyuch.title,
        # Номер, а не сам код: журнал читают многие, а запасной код открывает
        # сервис ровно так же, как обычный.
        after=f"#{nomer + 1}",
    )
    return zapasnye(db, actor, key_id)


# --- доступ ------------------------------------------------------------------


def kto_vidit(db: Session, actor: User, key_id: int) -> dict:
    """Кто открыт к ключу поимённо — и кого ещё можно открыть."""
    klyuch = dostat(db, actor, key_id)
    _pravit(actor, klyuch)
    otkryty = {d.user_id for d in klyuchi_repo.dostupy_klyucha(db, key_id)}
    lyudi = []
    for chelovek in users_repo.list_staff(db, status=STATUS_ACTIVE):
        sozdatel = chelovek.id == klyuch.created_by
        lyudi.append(
            {
                "id": chelovek.id,
                "name": chelovek.name or chelovek.email,
                "role": chelovek.role,
                "always": sozdatel or chelovek.role == ROLE_ROOT,
                "otkryt": sozdatel or chelovek.role == ROLE_ROOT or chelovek.id in otkryty,
            }
        )
    return {"key_id": key_id, "title": klyuch.title, "people": lyudi}


def otkryt(db: Session, actor: User, key_id: int, user_id: int, *, otkryt_li: bool) -> dict:
    klyuch = dostat(db, actor, key_id)
    _pravit(actor, klyuch)
    if klyuch.category_id is not None:
        kat = klyuchi_repo.kategoriya(db, klyuch.category_id)
        if kat is not None and kat.zakrytaya:
            raise errors.ValidationError(
                "Keys in a closed category are not shared", code="key_category_closed"
            )
    chelovek = users_repo.get_by_id(db, user_id)
    if chelovek is None:
        raise errors.NotFoundError("User not found", code="user_not_found")
    if chelovek.id == klyuch.created_by or chelovek.role == ROLE_ROOT:
        raise errors.ValidationError(
            "This person always sees the key", code="key_access_always"
        )
    if otkryt_li:
        klyuchi_repo.otkryt_klyuch(db, key_id, user_id, actor.id)
    else:
        klyuchi_repo.zakryt_klyuch(db, key_id, user_id)
    audit_service.record(
        db,
        actor=actor,
        source=SOURCE_MANUAL,
        action=audit_service.ACTION_KEY_ACCESS_CHANGED,
        entity_type=audit_service.ENTITY_TWOFACTOR,
        entity_id=klyuch.id,
        entity_label=klyuch.title,
        after=f"{'+' if otkryt_li else '-'}{chelovek.name or chelovek.email}",
    )
    return kto_vidit(db, actor, key_id)


# --- категории ---------------------------------------------------------------


def sozdat_kategoriyu(db: Session, actor: User, dannye: dict) -> dict:
    imya = _stroka(dannye.get("name"), MAX_KATEGORIYA)
    if not imya:
        raise errors.ValidationError("Category name is empty", code="key_category_empty")
    if klyuchi_repo.kategoriya_po_imeni(db, imya) is not None:
        raise errors.ConflictError("Category already exists", code="key_category_taken")
    kat = klyuchi_repo.sozdat_kategoriyu(
        db, name=imya, zakrytaya=bool(dannye.get("zakrytaya")), created_by=actor.id
    )
    audit_service.record(
        db,
        actor=actor,
        source=SOURCE_MANUAL,
        action=audit_service.ACTION_KEY_CATEGORY_CHANGED,
        entity_type=audit_service.ENTITY_KEY_CATEGORY,
        entity_id=kat.id,
        entity_label=kat.name,
        after="created",
    )
    return {"id": kat.id, "name": kat.name, "zakrytaya": kat.zakrytaya}


def _hozyain_kategorii(db: Session, actor: User, category_id: int):
    """Категория, которой этот человек вправе распоряжаться."""
    kat = klyuchi_repo.kategoriya(db, category_id)
    if kat is None:
        raise errors.NotFoundError("Category not found", code="key_category_not_found")
    if not _root(actor) and kat.created_by != actor.id:
        raise errors.ForbiddenError(
            "Only the category owner can change it", code="key_category_not_owner"
        )
    return kat


def kto_vidit_kategoriyu(db: Session, actor: User, category_id: int) -> dict:
    """Кого пустили в категорию — и кого ещё можно пустить.

    Пустить в категорию проще, чем в каждый ключ по отдельности: бухгалтеру
    открывают «Бухгалтерию», а не восемь ключей и потом девятый.
    """
    kat = _hozyain_kategorii(db, actor, category_id)
    otkryty = {d.user_id for d in klyuchi_repo.dostupy_kategorii(db, category_id)}
    lyudi = []
    for chelovek in users_repo.list_staff(db, status=STATUS_ACTIVE):
        hozyain = chelovek.id == kat.created_by
        lyudi.append(
            {
                "id": chelovek.id,
                "name": chelovek.name or chelovek.email,
                "role": chelovek.role,
                "always": hozyain or chelovek.role == ROLE_ROOT,
                "otkryt": hozyain or chelovek.role == ROLE_ROOT or chelovek.id in otkryty,
            }
        )
    return {
        "category_id": kat.id,
        "name": kat.name,
        "zakrytaya": kat.zakrytaya,
        "people": lyudi,
    }


def otkryt_kategoriyu(
    db: Session, actor: User, category_id: int, user_id: int, *, otkryt_li: bool
) -> dict:
    """Пустить человека в категорию или закрыть её ему.

    Закрытая списков не слушает — это её определение (`_vidno` требует
    `zakrytaya = false`), и молча записать в неё доступ значило бы завести
    строку, которая никогда ничего не откроет.
    """
    kat = _hozyain_kategorii(db, actor, category_id)
    if kat.zakrytaya:
        raise errors.ValidationError(
            "A closed category does not take access lists", code="key_category_closed"
        )
    chelovek = users_repo.get_by_id(db, user_id)
    if chelovek is None:
        raise errors.NotFoundError("User not found", code="user_not_found")
    if chelovek.id == kat.created_by or chelovek.role == ROLE_ROOT:
        raise errors.ValidationError(
            "This person always sees the category", code="key_access_always"
        )
    if otkryt_li:
        klyuchi_repo.otkryt_kategoriyu(db, category_id, user_id, actor.id)
    else:
        klyuchi_repo.zakryt_kategoriyu(db, category_id, user_id)
    audit_service.record(
        db,
        actor=actor,
        source=SOURCE_MANUAL,
        action=audit_service.ACTION_KEY_ACCESS_CHANGED,
        entity_type=audit_service.ENTITY_KEY_CATEGORY,
        entity_id=kat.id,
        entity_label=kat.name,
        after=f"{'+' if otkryt_li else '-'}{chelovek.name or chelovek.email}",
    )
    return kto_vidit_kategoriyu(db, actor, category_id)


def udalit_kategoriyu(db: Session, actor: User, category_id: int) -> None:
    """Категория уходит, ключи остаются — им обнуляется категория."""
    kat = klyuchi_repo.zapert_kategoriyu(db, category_id)
    if kat is None:
        raise errors.NotFoundError("Category not found", code="key_category_not_found")
    if not _root(actor) and kat.created_by != actor.id:
        raise errors.ForbiddenError(
            "Only the category owner can remove it", code="key_category_not_owner"
        )
    imya = kat.name
    klyuchi_repo.udalit_kategoriyu(db, kat)
    audit_service.record(
        db,
        actor=actor,
        source=SOURCE_MANUAL,
        action=audit_service.ACTION_KEY_CATEGORY_CHANGED,
        entity_type=audit_service.ENTITY_KEY_CATEGORY,
        entity_id=category_id,
        entity_label=imya,
        after="deleted",
    )


# --- напоминание -------------------------------------------------------------


def zavesti_napominanie(db: Session, actor: User, key_id: int, cherez_dney: int) -> dict:
    """Напоминание «сменить ключ» — обычной задачей из блока напоминаний.

    Своего срока у ключа нет намеренно: он показывался бы и отсчитывался
    вторым способом и разошёлся бы с напоминаниями на первой же правке.
    """
    klyuch = dostat(db, actor, key_id)
    _pravit(actor, klyuch)
    if not modules_service.is_enabled(db, "tasks"):
        raise errors.ValidationError("Reminders module is off", code="key_tasks_off")
    if not 1 <= int(cherez_dney) <= 3650:
        raise errors.ValidationError("Bad reminder period", code="key_bad_reminder")
    srok = (now_utc() + timedelta(days=int(cherez_dney))).replace(tzinfo=None)
    from core.services import task_service

    zadacha = task_service.create(
        db,
        {
            "title": f"Сменить ключ: {klyuch.title}",
            "due_at": srok,
            "vazhnost": klyuch.vazhnost,
        },
        actor,
    )
    klyuch.task_id = zadacha.id
    return {"task_id": zadacha.id, "due_at": srok.isoformat()}


def sluchaynyy_zapasnoy() -> str:
    """Код вида `4f9c-20a1`. Нужен, когда сервис списка не дал, а завести
    свой человек всё равно хочет — например, для внутреннего сервиса."""
    return f"{secrets.token_hex(2)}-{secrets.token_hex(2)}"


def seychas_utc() -> datetime:
    return now_utc()
