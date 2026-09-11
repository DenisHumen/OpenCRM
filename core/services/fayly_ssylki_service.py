"""Ссылки на файлы наружу: выпустить, настроить, отозвать, открыть.

Механика взята у витрин досок (`share_service`), а не написана заново: токен,
срок, код, счётчик открытий, отзыв — те же. Разница одна и она в `rezhim`:
витрину смотрят, а файл ещё и скачивают, и «смотреть» без «скачать» — отдельное
решение, за которым стоит защита просмотра.

**Файл закрыт теми же проверками, что и страница.** Это не осторожность, а
разбор случившегося: у витрин файлы однажды остались открытыми после отзыва
ссылки, смены кода и снятия с публикации — страница отвечала 401, а картинки
200 (см. `share_service.media_is_public`). Поэтому здесь и страница, и байты
проходят через один `otkryt`, и другой дороги к байтам нет: прямого адреса у
файла не существует, наружу ведёт только `/f/{token}`.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from config.settings import get_settings
from core import bezopasnost
from core import exceptions as errors
from core.security import passwords, tokens
from core.services import audit_service, fayly_service, modules_service
from core.utils import now_utc, to_utc_naive
from database.models import FileLink, User
from database.models.fayl import KRUG_GOSTI, KRUG_KOD, KRUGI, REZHIMY
from database.repositories import fayly as fayly_repo

NE_TRONUTO = object()


def _fayl_ssylki(db: Session, ssylka: FileLink):
    """Чему ссылка принадлежит. `None` — источника больше нет (сняли каскадом
    в другой сессии); ссылка при этом ведёт в никуда и должна молчать."""
    if ssylka.stored_file_id is not None:
        return fayly_repo.fayl(db, ssylka.stored_file_id)
    return fayly_repo.rabota_po_nomeru(db, ssylka.work_id)


def _razobrat_nomer(nomer: str) -> tuple[int | None, int | None]:
    """`stored:12` или `work:7` — в пару номеров. Один из двух, никогда оба."""
    vid, _, hvost = (nomer or "").partition(":")
    if not hvost.isdigit():
        raise errors.ValidationError("Unknown file", code="file_not_found")
    if vid == "stored":
        return int(hvost), None
    if vid == "work":
        return None, int(hvost)
    # Вложения карточек наружу пока не выпускаются: у ссылки два внешних ключа,
    # и третий источник — это третья колонка и миграция, а не строка здесь.
    raise errors.ValidationError("This file cannot be shared yet", code="file_not_shareable")


def _proverit(rezhim: str, krug: str, est_kod: bool, est_gosti: bool = True) -> None:
    """Круг без того, чем он закрыт, — открытая ссылка, которая называется
    закрытой. «По коду» без кода и «приглашённым» с пустым списком — одно и то
    же враньё, и оба отвергаются здесь."""
    if rezhim not in REZHIMY:
        raise errors.ValidationError("Unknown link mode", code="bad_link_mode")
    if krug not in KRUGI:
        raise errors.ValidationError("Unknown audience", code="bad_link_audience")
    if krug == KRUG_KOD and not est_kod:
        raise errors.ValidationError("A code is required", code="link_code_required")
    if krug == KRUG_GOSTI and not est_gosti:
        raise errors.ValidationError(
            "Invite at least one address first", code="link_guests_required"
        )


def pochta(znachenie: str) -> str:
    """Почта к одному виду: без пробелов и в нижнем регистре.

    «Ivan@X.ru» и «ivan@x.ru» — один человек, и пустить первого, отказав
    второму, значило бы сделать список зависящим от того, как гость набрал своё
    имя. Разбирать адрес глубже нечем и незачем: список составляет человек, а
    не форма регистрации.
    """
    chistaya = (znachenie or "").strip().lower()
    if "@" not in chistaya or len(chistaya) < 3 or len(chistaya) > 200:
        raise errors.ValidationError("That does not look like an email", code="bad_email")
    return chistaya


def _srok(expires_at: datetime | None) -> datetime | None:
    """Срок к тому виду, в каком время лежит в базе: naive UTC. Тот же разбор,
    что у витрин: приведение к местной зоне давало ссылке лишние часы жизни."""
    return to_utc_naive(expires_at)


def _kod(pin: str | None) -> str | None:
    if not pin:
        return None
    pin = pin.strip()
    if not passwords.is_valid_pin(pin):
        raise errors.ValidationError("Code must be 4-8 digits", code="bad_pin")
    return passwords.hash_password(pin)


# --- изнутри ------------------------------------------------------------------


def vypustit(
    db: Session,
    actor: User,
    nomer: str,
    *,
    rezhim: str,
    krug: str,
    pin: str | None,
    expires_at: datetime | None,
) -> FileLink:
    """Выпустить ссылку. По одной на файл: вторая означала бы два разных набора
    условий на одни байты, и отзывать пришлось бы обе, помня о второй."""
    stored_id, work_id = _razobrat_nomer(nomer)
    # Список у НОВОЙ ссылки пуст всегда: пригласить некого, пока ссылки нет.
    # Поэтому «приглашённым» с ходу не выпускается — сначала ссылка, потом
    # список, потом круг.
    _proverit(rezhim, krug, bool(pin), est_gosti=krug != KRUG_GOSTI)
    if stored_id is not None and fayly_repo.fayl(db, stored_id) is None:
        raise errors.NotFoundError("File not found", code="file_not_found")
    if work_id is not None and fayly_repo.rabota_po_nomeru(db, work_id) is None:
        raise errors.NotFoundError("File not found", code="file_not_found")

    byla = fayly_repo.ssylka_fayla(db, stored_file_id=stored_id, work_id=work_id)
    if byla is not None:
        return nastroit(db, actor, byla.id, rezhim=rezhim, krug=krug, pin=pin, expires_at=expires_at)

    ssylka = fayly_repo.zavesti_ssylku(
        db,
        stored_file_id=stored_id,
        work_id=work_id,
        token=tokens.new_share_token(),
        rezhim=rezhim,
        krug=krug,
        expires_at=_srok(expires_at),
        # Код заводится только своему кругу — то же правило, что и в `nastroit`.
        # Иначе `{krug: "link", pin: "4079"}` создавал бы ссылку, которая
        # называется открытой и спрашивает код.
        pin_hash=_kod(pin) if krug == KRUG_KOD else None,
        author_id=actor.id,
    )
    audit_service.record(
        db,
        action=audit_service.ACTION_FILE_SHARED,
        actor=actor,
        source=audit_service.SOURCE_MANUAL,
        entity_type=audit_service.ENTITY_SHARE,
        entity_id=ssylka.id,
        entity_label=f"{nomer} → {rezhim}",
    )
    return ssylka


def nastroit(
    db: Session,
    actor: User,
    link_id: int,
    *,
    rezhim=NE_TRONUTO,
    krug=NE_TRONUTO,
    pin=NE_TRONUTO,
    expires_at=NE_TRONUTO,
    is_active=NE_TRONUTO,
) -> FileLink:
    ssylka = fayly_repo.ssylka(db, link_id)
    if ssylka is None:
        raise errors.NotFoundError("Link not found", code="link_not_found")

    novyy_rezhim = ssylka.rezhim if rezhim is NE_TRONUTO else rezhim
    novyy_krug = ssylka.krug if krug is NE_TRONUTO else krug
    # «Не прислали код» и «прислали пустой» — разные просьбы: первая означает
    # «не трогай», вторая «сними». Пока они были одним `None`, смена срока у
    # ссылки под кодом отвечала «нужен код» — то есть поправить срок было
    # нельзя вовсе.
    est_kod = bool(ssylka.pin_hash) if pin is NE_TRONUTO else bool(pin)
    est_gosti = bool(fayly_repo.gosti(db, ssylka.id))
    _proverit(novyy_rezhim, novyy_krug, est_kod, est_gosti)

    ssylka.rezhim = novyy_rezhim
    ssylka.krug = novyy_krug
    if pin is not NE_TRONUTO:
        ssylka.pin_hash = _kod(pin)
    if expires_at is not NE_TRONUTO:
        ssylka.expires_at = _srok(expires_at)
    if is_active is not NE_TRONUTO and is_active is not None:
        if ssylka.is_active and not is_active:
            ssylka.revoked_at = now_utc()
        if not ssylka.is_active and is_active:
            ssylka.revoked_at = None
        ssylka.is_active = bool(is_active)
    # Код принадлежит кругу «по коду» и уходит вместе с ним.
    #
    # Пока кругов было два, снимался он только у «по ссылке», и третий круг
    # получал живой хвост: гость упирался в код, которого ему не давали, а снять
    # его в окне можно было только сделав ссылку открытой для всех.
    if ssylka.krug != KRUG_KOD:
        ssylka.pin_hash = None
    db.flush()
    return ssylka


def otozvat(db: Session, actor: User, link_id: int) -> None:
    """Отозвать насовсем: строка уходит вместе с журналом открытий.

    Отзыв — не «выключить»: человек, отзывающий ссылку, хочет, чтобы её не
    было. Оставленная неактивной строка на следующем экране выглядит как
    «ссылка есть, просто выключена», и её включают обратно по ошибке.
    """
    ssylka = fayly_repo.ssylka(db, link_id)
    if ssylka is None:
        raise errors.NotFoundError("Link not found", code="link_not_found")
    audit_service.record_deletion(
        db,
        actor=actor,
        entity_type=audit_service.ENTITY_SHARE,
        entity_id=ssylka.id,
        entity_label=ssylka.token[:12],
    )
    fayly_repo.udalit_ssylku(db, ssylka)


def ssylka_fayla(db: Session, nomer: str) -> FileLink | None:
    """Живая ссылка этого файла или `None`."""
    stored_id, work_id = _razobrat_nomer(nomer)
    return fayly_repo.ssylka_fayla(db, stored_file_id=stored_id, work_id=work_id)


#: Сколько открытий показываем в журнале. Журнал читают, чтобы решить,
#: отзывать ли ссылку, — для этого хватает последних.
ZHURNALA = 20


def kartochka(db: Session, ssylka: FileLink) -> dict:
    vsego, raznyh, posledniy = fayly_repo.schyot_otkrytiy(db, ssylka.id)
    return {
        "guests": [g.email for g in fayly_repo.gosti(db, ssylka.id)],
        "log": [
            {
                "at": v.viewed_at.isoformat() if v.viewed_at else None,
                # Пусто — круг «по ссылке» или «по коду»: смотрящий анонимен по
                # устройству, и подставлять сюда имя было бы выдумкой.
                "email": v.guest_email or "",
            }
            for v in fayly_repo.otkrytiya(db, ssylka.id, ZHURNALA)
        ],
        "id": ssylka.id,
        "url": adres(ssylka),
        "rezhim": ssylka.rezhim,
        "krug": ssylka.krug,
        "is_active": ssylka.is_active,
        "expires_at": ssylka.expires_at.isoformat() if ssylka.expires_at else None,
        "has_code": ssylka.pin_hash is not None,
        "views_count": vsego,
        "unique_views_count": raznyh,
        "last_viewed_at": posledniy.isoformat() if posledniy else None,
    }


def pozvat(db: Session, actor: User, link_id: int, email: str) -> FileLink:
    """Позвать по почте. Тот же адрес дважды — не ошибка, а «уже позвали»."""
    ssylka = fayly_repo.ssylka(db, link_id)
    if ssylka is None:
        raise errors.NotFoundError("Link not found", code="link_not_found")
    adresok = pochta(email)
    if not fayly_repo.gost_est(db, link_id, adresok):
        fayly_repo.pozvat(db, link_id, adresok)
    return ssylka


def vycherknut(db: Session, actor: User, link_id: int, email: str) -> FileLink:
    """Вычеркнуть из списка. Последнего вычеркнуть у ссылки «приглашённым»
    нельзя: круг остался бы без того, чем он закрыт, — то есть открытым."""
    ssylka = fayly_repo.ssylka(db, link_id)
    if ssylka is None:
        raise errors.NotFoundError("Link not found", code="link_not_found")
    spisok = fayly_repo.gosti(db, link_id)
    if ssylka.krug == KRUG_GOSTI and len(spisok) <= 1:
        raise errors.ValidationError(
            "The last invited address cannot be removed while the link is invite-only",
            code="link_guests_required",
        )
    fayly_repo.vycherknut(db, link_id, pochta(email))
    return ssylka


def gost_pozvan(db: Session, ssylka: FileLink, email: str) -> bool:
    """Есть ли этот адрес в списке. Отказ выглядит так же, как неверный код:
    отвечать «такого адреса не звали» значило бы отдать список по одному."""
    try:
        adresok = pochta(email)
    except errors.ValidationError:
        return False
    return fayly_repo.gost_est(db, ssylka.id, adresok)


def adres(ssylka: FileLink) -> str:
    return f"{get_settings().base_url.rstrip('/')}/f/{ssylka.token}"


def po_faylam(db: Session, nomera: list[str]) -> dict[str, dict]:
    """Состояние ссылок для списка файлов: одним запросом на страницу."""
    svoi_ids = [int(n.split(":")[1]) for n in nomera if n.startswith("stored:")]
    raboty_ids = [int(n.split(":")[1]) for n in nomera if n.startswith("work:")]
    svoi, raboty_ = fayly_repo.ssylki_faylov(db, svoi_ids, raboty_ids)
    itog: dict[str, dict] = {}
    for nomer, ssylka in [(f"stored:{k}", v) for k, v in svoi.items()] + [
        (f"work:{k}", v) for k, v in raboty_.items()
    ]:
        itog[nomer] = {"rezhim": ssylka.rezhim, "is_active": ssylka.is_active}
    return itog


# --- снаружи ------------------------------------------------------------------


def otkryt(db: Session, token: str) -> tuple[FileLink, object] | None:
    """Ссылка живая и источник на месте? Все причины отказа выглядят одинаково.

    Блок выключили — публичные пути обязаны исчезнуть вместе с ним: иначе
    «выключил файлы» означало бы лишь «убрал из меню», а разосланные ссылки
    продолжали бы отдавать бумаги фирмы всему интернету.
    """
    if not modules_service.is_enabled(db, "files"):
        return None
    ssylka = fayly_repo.ssylka_po_tokenu(db, token)
    if ssylka is None or not ssylka.is_active:
        return None
    if ssylka.expires_at is not None and ssylka.expires_at < now_utc():
        return None
    fayl = _fayl_ssylki(db, ssylka)
    if fayl is None:
        return None
    return ssylka, fayl


def imya_fayla(fayl) -> str:
    return getattr(fayl, "original_name", "") or ""


def bayty(db: Session, ssylka: FileLink, fayl):
    """Где лежат байты. У своего файла — своя папка, у работы доски — исходник
    в её каталоге; второй дороги к тем же байтам не заводим."""
    from core.services import media_service

    if ssylka.stored_file_id is not None:
        return fayly_service.fayl_na_diske(fayl)
    return media_service.original_path(fayl.work_uid, fayl.kind, fayl.mime)


#: Что страница умеет показать прямо в браузере. Остальному предпросмотра нет
#: вовсе, и страница говорит это словами: пустая рамка читается как поломка.
POKAZ_KARTINKA = {"jpg", "jpeg", "png", "webp", "gif"}
POKAZ_VIDEO = {"mp4", "webm", "mov"}
POKAZ_BUMAGA = {"pdf"}


def vid_pokaza(imya: str) -> str:
    ext = imya.rsplit(".", 1)[-1].lower() if "." in imya else ""
    if ext in POKAZ_KARTINKA:
        return "image"
    if ext in POKAZ_VIDEO:
        return "video"
    return "pdf" if ext in POKAZ_BUMAGA else "none"


def klyuch_prosmotra(ssylka: FileLink, gost: str = "") -> str:
    """Ключ на десять минут: без него байты не отдаются вовсе.

    У приглашённых в него входит почта: пересланный вместе со страницей ключ
    не откроет файл тому, у кого нет пропуска на ту же почту, — а раньше
    открыл бы на все десять минут.
    """
    return tokens.make_view_key(ssylka.id, gost)


def klyuch_veren(ssylka: FileLink, klyuch: str, gost: str = "") -> bool:
    return tokens.check_view_key(klyuch or "", ssylka.id, gost)


def znak(ssylka: FileLink, gost: str = "") -> str:
    """Что написано водяным знаком поверх файла.

    У приглашённых — почта смотрящего: утёкший снимок экрана показывает, от
    КОГО он ушёл. У «по ссылке» и «по коду» человека за ссылкой нет, она
    анонимна по устройству, и знак несёт хвост токена: он отвечает на другой
    вопрос — какая из разосланных ссылок ушла дальше.
    """
    kto = gost if (ssylka.krug == KRUG_GOSTI and gost) else ssylka.token[-6:]
    return f"{kto} · {now_utc().strftime('%d.%m.%Y')}"


def proverit_kod(db: Session, ssylka: FileLink, pin: str, ip: str, limiter) -> bool:
    """Тот же счётчик попыток, что у витрин: код — четыре цифры."""
    key = f"fl:{ssylka.id}:{tokens.hash_ip(ip)}"
    if limiter.proverit_i_zanyat(key):
        bezopasnost.otmetit("pin_zapert")
        raise errors.RateLimitedError("Too many attempts, try later", code="pin_rate_limited")
    if ssylka.pin_hash and passwords.verify_password(pin.strip(), ssylka.pin_hash):
        limiter.reset(key)
        return True
    bezopasnost.otmetit("pin_promah")
    return False


def otmetit_otkrytie(
    db: Session, ssylka: FileLink, ip: str, user_agent: str, gost: str = ""
) -> None:
    fayly_repo.otmetit_otkrytie(db, ssylka.id, tokens.hash_ip(ip), user_agent or "", gost)
