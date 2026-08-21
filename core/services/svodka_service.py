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
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from core import modules as core_modules
from core.utils import now_utc
from core.services import (
    finance_service,
    modules_service,
    settings_service,
    telegram_service,
)
from database.repositories import settings as settings_repo
from database.repositories import svodka as svodka_repo

logger = logging.getLogger(__name__)

#: Когда сводка ушла в последний раз. В настройках, а не в отдельной таблице:
#: это одна строка на всю систему, и заводить ради неё таблицу не за что.
SETTING_POSLEDNYAYA = "telegram_digest_last_sent_at"

#: Часовой пояс, по которому считаются «сутки» и подписывается заголовок.
#:
#: Не UTC, хотя сервер живёт в нём. Сводка — сообщение человеку, а человек
#: живёт по своему времени: «за вчера» обязано означать его вчера, иначе
#: утренние цифры включают чужой вечер и не включают свой.
#:
#: Именованная зона, а НЕ `timezone(timedelta(hours=3))`. Киев летом +3, зимой
#: +2, и записанное числом смещение верно ровно полгода: с последнего
#: воскресенья октября по последнее воскресенье марта окно «за прошедшие сутки»
#: съезжало на час вперёд. Цифры при этом оставались правдоподобными — просто
#: включали чужой вечерний час и теряли свой, — то есть ошибка была ровно того
#: сорта, который не замечают.
#:
#: База поясов обязана быть на месте: без неё `ZoneInfo` бросает
#: `ZoneInfoNotFoundError` прямо здесь, на импорте модуля, и сводка не
#: собирается ВООБЩЕ. В образе она есть от системы (debian-slim тянет tzdata),
#: но полагаться на это нельзя — на Windows системной базы нет вовсе, — поэтому
#: пакет `tzdata` записан в зависимости pyproject.toml явно.
POYAS = ZoneInfo("Europe/Kyiv")

#: Источник заявок с сайта. Тот же ключ, что в `lead_service`.
ISTOCHNIK_SAYT = "site"


def _sutki(seychas: datetime | None = None) -> tuple[datetime, datetime, str]:
    """Окно прошедших суток в UTC и подпись для заголовка.

    Окно считается по местному времени, а границы отдаются в UTC — база живёт в
    нём. Перепутать эти две системы здесь легче всего, и цена ошибки — цифры,
    сдвинутые на несколько часов, которые выглядят правдоподобно.

    Вычитание суток идёт по СТЕННЫМ часам, а не по 24 часам, и это намеренно:
    `nachalo_dnya - timedelta(days=1)` при именованной зоне даёт вчерашнюю
    полночь, а смещение каждой границы `astimezone` разрешает уже по её
    собственной дате. В ночь перевода стрелок окно поэтому выходит 23 или 25
    часов — ровно столько, сколько эти сутки и длились. Вычти мы 24 часа, одна
    из границ встала бы на 23:00 или 01:00 и сводка захватила бы чужой час.
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
    # Чем меряется выручка — тем же одним местом, что у отчёта и у главной.
    # Утренняя сводка была ТРЕТЬИМ независимым счётом: починив экран и оставив
    # её, мы получили бы ту же беду заново — только спорить стали бы сообщение и
    # экран, а сообщение нельзя открыть и перепроверить.
    itog["bazis"] = finance_service.bazis_vyruchki(db)
    itog["postupilo"] = bezopasno(
        "поступления в кассу",
        lambda: (finance_service.postupleniya_po_mesyatsam(db, [(ot, do)]) or {})
        .get(0, {})
        .get("total"),
    )
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
    vyigrano = delo.get("vyigrano_minor", 0)
    postupilo = dannye.get("postupilo")

    stroki = [
        f"☀️ <b>Сводка за {e(dannye['den'])}</b>",
        "",
    ]

    if dannye.get("delo") is None:
        stroki.append("▸ Работа за сутки — не сосчиталась")
    else:
        # Деньги и заявки — врозь. «Выручка» означает полученное (решение
        # владельца: банк один), а сумма выигранных заявок деньгами не является:
        # под одним словом они и дали пустой месяц при полной кассе. Кассы нет —
        # говорим «выиграно на сумму», не выдавая одно за другое.
        if postupilo is None:
            dengi = f"выиграно на сумму: <b>{e(_dengi(vyigrano, valyuta))}</b>"
        else:
            dengi = (
                f"получено: <b>{e(_dengi(postupilo, valyuta))}</b> · "
                f"выиграно на сумму: <b>{e(_dengi(vyigrano, valyuta))}</b>"
            )
        stroki.append(
            f"▸ Заявок новых: <b>{delo.get('novyh_zayavok', 0)}</b> · "
            f"закрыто: <b>{zakryto}</b> · {dengi}"
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

    # Отметка времени УСПЕШНОЙ отправки. Уходит метрикой, по метрике стоит
    # тревога — иначе неприход сводки неотличим от спокойного дня.
    #
    # Заметить нечем: отказ отправки виден в ответе, в журнале и в коде
    # возврата, а вот «скрипт не запустился вовсе» (таймер сорвался, контейнер
    # лежал) не оставляет вообще ничего. Сводка при этом сама была задумана как
    # признак живого канала — то есть её тишина обязана шуметь.
    settings_repo.write_many(
        db, {SETTING_POSLEDNYAYA: now_utc().replace(tzinfo=None).isoformat(timespec="seconds")}
    )
    # Фиксируем ЗДЕСЬ, а не оставляем зовущему. Главный зовущий — ночной скрипт
    # (`scripts/svodka.py`), и он открывает сессию блоком `with SessionLocal()`
    # без единого `commit`: отметка сбросилась бы в базу и откатилась вместе с
    # закрытием сессии. Проверка это и поймала — иначе метрика не появилась бы
    # НИКОГДА, тревога на молчание не сработала бы ни разу, и вся эта затея
    # тихо не работала бы ровно так же, как то, что она стережёт.
    db.commit()
    return {"status": "sent", "otkazy": dannye["otkazy"]}


def kogda_poslednyaya(db: Session) -> str:
    """Когда сводка уходила в последний раз. Пусто — ни разу."""
    stroka = settings_repo.get_row(db, SETTING_POSLEDNYAYA)
    return (stroka.value if stroka else "") or ""
