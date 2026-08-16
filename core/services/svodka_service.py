"""Утренняя сводка: собрать, оформить, отправить.

**Ценность не в содержании, а в самом факте прибытия.** Сломался бот, отозвали
токен, отвалился канал — узнать об этом иначе неоткуда: тревоги в таком случае
тоже не придут, и тишина будет выглядеть как «всё хорошо». Поэтому сводка
уходит каждый день, даже когда в ней нули: «сегодня ничего не произошло» — это
сообщение, а молчание — нет.

**Раздел, который не сосчитался, не роняет сводку.** Каждый счёт обёрнут
отдельно, и упавший пишет в сообщение «не сосчиталось» вместо своих цифр. Иначе
одна сломанная выборка означала бы, что владелец не получит НИЧЕГО — и не
узнает, что канал жив.

**Оформление — то же, что у тревог**: короткая суть сверху, подробности в
сворачиваемом блоке. Разметка HTML, потому что экранировать в ней надо три
знака, а не восемнадцать, как в MarkdownV2; непроэкранированный знак означает
отказ телеграма и несостоявшуюся сводку.
"""

from __future__ import annotations

import html
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from core import modules as core_modules
from core.services import modules_service, settings_service, telegram_service
from database.repositories import svodka as svodka_repo

logger = logging.getLogger(__name__)

#: Часовой пояс, по которому считаются «сутки» и подписывается заголовок.
#:
#: Не UTC, хотя сервер живёт в нём. Сводка — сообщение человеку, а человек
#: живёт по своему времени: «за вчера» обязано означать его вчера, иначе
#: утренние цифры включают чужой вечер и не включают свой.
POYAS = timezone(timedelta(hours=3))

#: Источник заявок с сайта. Тот же ключ, что в `lead_service`.
ISTOCHNIK_SAYT = "site"


def _sutki(seychas: datetime | None = None) -> tuple[datetime, datetime, str]:
    """Окно прошедших суток в UTC и подпись для заголовка.

    Окно считается по местному времени, а границы отдаются в UTC — база живёт в
    нём. Перепутать эти две системы здесь легче всего, и цена ошибки — цифры,
    сдвинутые на несколько часов, которые выглядят правдоподобно.
    """
    mestnoe = (seychas or datetime.now(timezone.utc)).astimezone(POYAS)
    nachalo_dnya = mestnoe.replace(hour=0, minute=0, second=0, microsecond=0)
    vchera = nachalo_dnya - timedelta(days=1)
    return (
        vchera.astimezone(timezone.utc).replace(tzinfo=None),
        nachalo_dnya.astimezone(timezone.utc).replace(tzinfo=None),
        vchera.strftime("%d.%m.%Y"),
    )


def _dengi(minor: int, valyuta: str) -> str:
    """Минорные единицы в человеческий вид. Целыми, без float."""
    znak = "-" if minor < 0 else ""
    minor = abs(minor)
    return f"{znak}{minor // 100}.{minor % 100:02d} {valyuta}"


def sobrat(db: Session, seychas: datetime | None = None) -> dict:
    """Цифры сводки. Раздел, который не сосчитался, помечается, а не роняет всё."""
    ot, do, den = _sutki(seychas)
    vklyucheny = {k for k, on in modules_service.state(db).items() if on}

    itog: dict = {"den": den, "otkazy": []}

    def bezopasno(imya: str, chto):
        try:
            return chto()
        except Exception as beda:  # noqa: BLE001 — один раздел не роняет сводку
            logger.warning("сводка: раздел «%s» не сосчитался: %r", imya, beda)
            itog["otkazy"].append(imya)
            return None

    itog["delo"] = bezopasno("работа за сутки", lambda: svodka_repo.za_sutki(db, ot, do))
    itog["s_sayta"] = bezopasno(
        "заявки с сайта", lambda: svodka_repo.zayavki_s_sayta(db, ot, do, ISTOCHNIK_SAYT)
    )
    itog["prosrocheno"] = bezopasno(
        "просроченные напоминания",
        lambda: svodka_repo.prosrocheno_napominaniy(db, do),
    )
    # Разделы выключенных блоков не считаем вовсе: строка про склад у того, кто
    # склад не ведёт, — это не сведения, а шум, от которого сводку перестают
    # читать целиком.
    if "telephony" in vklyucheny:
        itog["zvonki"] = bezopasno("звонки", lambda: svodka_repo.zvonki(db, ot, do))
    if "warehouse" in vklyucheny:
        itog["sklad"] = bezopasno("склад", lambda: svodka_repo.tovary_v_minuse(db))
    return itog


def oformit(db: Session, dannye: dict) -> str:
    """Сообщение для телеграма. Суть сверху, подробности под нажатием."""
    valyuta = settings_service.get_all(db).get("currency", "USD")
    e = html.escape

    delo = dannye.get("delo") or {}
    zakryto = delo.get("zakryto", 0)
    vyruchka = delo.get("vyruchka_minor", 0)

    stroki = [
        f"☀️ <b>Сводка за {e(dannye['den'])}</b>",
        "",
    ]

    if dannye.get("delo") is None:
        stroki.append("▸ Работа за сутки — не сосчиталась")
    else:
        stroki.append(
            f"▸ Заявок новых: <b>{delo.get('novyh_zayavok', 0)}</b> · "
            f"закрыто: <b>{zakryto}</b> · выручка: <b>{e(_dengi(vyruchka, valyuta))}</b>"
        )
        stroki.append(f"▸ Клиентов новых: <b>{delo.get('novyh_klientov', 0)}</b>")

    zvonki = dannye.get("zvonki")
    if zvonki is not None:
        stroki.append(
            f"▸ Звонков: <b>{zvonki.get('vsego', 0)}</b>"
            + (
                f" (пропущено {zvonki.get('propushcheno', 0)})"
                if zvonki.get("propushcheno")
                else ""
            )
        )

    # «Требует внимания» — отдельным блоком и только когда есть что сказать.
    # Пустой раздел «всё хорошо» приучает пролистывать сводку не читая, и
    # однажды пролистают вместе с непустым.
    vnimanie = []
    s_sayta = dannye.get("s_sayta") or {}
    if s_sayta.get("netronutye"):
        vnimanie.append(
            f"заявок с сайта без ответа: <b>{s_sayta['netronutye']}</b> "
            f"из {s_sayta.get('vsego', 0)}"
        )
    if dannye.get("prosrocheno"):
        vnimanie.append(f"просроченных напоминаний: <b>{dannye['prosrocheno']}</b>")
    if dannye.get("sklad"):
        vnimanie.append(f"товаров в минусе: <b>{dannye['sklad']}</b>")

    if vnimanie:
        stroki.append("")
        stroki.append("<blockquote expandable>⚠️ <b>Требует внимания</b>")
        stroki += [f"· {v}" for v in vnimanie]
        stroki.append("</blockquote>")

    if dannye.get("otkazy"):
        stroki.append("")
        stroki.append(
            "🔧 Не сосчиталось: " + e(", ".join(dannye["otkazy"]))
        )

    return "\n".join(stroki)


def otpravit(db: Session, seychas: datetime | None = None, opener=None) -> dict:
    """Собрать и отправить сводку. Отвечает, что произошло.

    Не настроен бот или не назван чат — молчим и говорим об этом ответом, а не
    исключением: сводка не настроена, и это не поломка.
    """
    if core_modules.get("telegram") is None or not modules_service.is_enabled(db, "telegram"):
        return {"status": "skipped", "reason": "module_off"}

    kluch = telegram_service.token(db)
    chat = telegram_service.digest_chat(db)
    if not kluch or not chat:
        return {"status": "skipped", "reason": "not_configured"}

    dannye = sobrat(db, seychas)
    tekst = oformit(db, dannye)
    try:
        telegram_service.poslat_tekst_razmetkoy(kluch, int(chat), tekst, opener)
    except telegram_service.TelegramOtkaz as beda:
        logger.error("сводка не ушла: %s", beda)
        return {"status": "failed", "error": str(beda)}
    return {"status": "sent", "otkazy": dannye["otkazy"]}
