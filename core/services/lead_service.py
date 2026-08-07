"""Заявка с сайта: клиент со статусом «лид» и работа на первом этапе воронки.

Главное правило блока: **форма заводит клиента, а почта и звонок — нет.**
Разница не в доверии к каналу, а в том, кто совершил действие. В ящик фирмы
валятся рассылки, счета хостера и ответы роботов; на телефон звонят курьеры и
автообзвон — там «клиент» это решение человека, и оба соседних блока честно
оставляют его человеку (`mail_service.store_incoming`,
`telephony_service._link_client`). Здесь человек сам заполнил форму и нажал
«отправить»: он назвал себя клиентом сам, и заводить карточку по такому поводу
— не догадка, а запись факта.

Из того же и следует, чего форме верить НЕЛЬЗЯ. Заполнить её может кто угодно
и сколько угодно раз, поэтому в этом файле три потолка, и каждый закрывает свою
беду:

* повтор той же заявки (`DOUBLE_SUBMIT_SECONDS`) — вторая строка на доске от
  одного нажатия;
* поток новых карточек (`MAX_NEW_CLIENTS_PER_HOUR`) — свалка вместо справочника;
* поток запросов с адреса — он не здесь, а на самой ручке
  (`web/public/leads.py`), потому что считать надо и те запросы, которые до
  сервиса не дошли.

Статуса «лид» отдельной колонкой в системе нет и заводить его ради формы не
станем: карточка получает **метку** `lead` и **источник** `site`. Метка
фильтруется списком клиентов, источник считается отчётом по источникам — то
есть оба вопроса, ради которых статус и заводят («покажи необработанных»,
«сколько дал сайт»), уже отвечаются, и без миграции.
"""

import hmac
import logging
import secrets
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy.orm import Session

from core import events
from core import exceptions as errors
from core.services import client_service, deal_service, settings_service, task_service
from core.utils import is_valid_email, normalize_phone, now_utc
from database.models import Client, Deal, User
from database.models.user import STATUS_ACTIVE
from database.repositories import leads as leads_repo
from database.repositories import settings as settings_repo
from database.repositories import users as users_repo

logger = logging.getLogger("opencrm.leads")

# --- настройки приёма ---
#
# Живут отдельными строками `site_settings`, как настройки телефонии, и по той
# же причине: ключ приёма — секрет, ему нечего делать в общем `GET /settings`, а
# общий `PATCH /settings` не должен уметь его менять.
SETTINGS_PREFIX = "leads_"
SETTING_INTAKE_KEY = "leads_intake_key"
SETTING_MANAGER_ID = "leads_manager_id"

#: Заголовок, в котором приезжает ключ приёма.
INTAKE_KEY_HEADER = "x-opencrm-intake-key"

#: Источник клиента, заведённого формой. Ключом, а не словом: отчёт по
#: источникам складывает строки именно по ключу, а свои значения он показывать
#: умеет (`report_service.by_source`) — заводить ради этого справочник не нужно.
SOURCE_SITE = "site"

#: Метка «необработанный». Английская, как весь текст продукта; фильтр по метке
#: сравнивает её целиком, а не подстрокой (`clients_repo.search`).
LEAD_TAG = "lead"

#: Заявка с сайта принята. Подробности: `client`, `deal`, `is_new_client`.
LEAD_RECEIVED = "lead.received"

# --- потолки ---

#: Сколько НОВЫХ карточек форме позволено завести за час.
#:
#: Ограничитель по адресу этого не закрывает и закрыть не может: тысяча заявок с
#: тысячи адресов — обычная работа ботнета, и каждый запрос в ней первый со
#: своего адреса. Без потолка справочник за час превращается в свалку, из
#: которой настоящие карточки выбирают руками; а справочник, в котором не
#: находят клиента, — это уже не справочник.
#:
#: Шестьдесят в час — с большим запасом: малому делу столько заявок с сайта не
#: приходит и за неделю. Выше потолка форма отвечает «попробуйте позже», и это
#: плохо — настоящая заявка может попасть под чужой шторм. Хуже только
#: обратное: настоящую заявку всё равно не найдут среди тысячи поддельных.
MAX_NEW_CLIENTS_PER_HOUR = 60

#: Окно, в котором повторная заявка того же клиента считается тем же обращением.
#:
#: Пять минут — это «нажал ещё раз, потому что страница долго думала» и «сайт
#: повторил доставку после таймаута». Заявка при этом не теряется: она уже
#: заведена, и вторая строка на доске означала бы двойную работу менеджера, а
#: не двойную работу для мастерской.
#:
#: Дольше делать нельзя: человек, вспомнивший через десять минут, что забыл
#: сказать главное, обращается заново — и это настоящее второе обращение.
DOUBLE_SUBMIT_SECONDS = 300

# --- ширина полей ---
#
# Взяты из колонок (`database/models/client.py`), а не выбраны заново: длинное
# значение доезжает до вставки как есть, SQLite принимает его молча, а боевой
# MySQL на том же месте роняет запрос. То есть беда пряталась бы ровно до дня
# переезда.
MAX_NAME = 200
MAX_EMAIL = 255
MAX_PHONE = 64
#: Текст обращения уезжает в описание заявки (TEXT). Потолок тут не про колонку,
#: а про то, что заявка на пять тысяч знаков — это уже не заявка.
MAX_MESSAGE = 5000
#: Сколько от текста обращения попадает в название заявки на доске.
TITLE_EXCERPT = 120


@dataclass(frozen=True)
class Outcome:
    """Чем кончился приём. Наружу НЕ уходит — см. `web/public/leads.py`."""

    #: created — завели карточку; linked — узнали своего; duplicate — повтор;
    #: ignored — ловушка сработала, не записано ничего.
    status: str
    client: Client | None = None
    deal: Deal | None = None


# --- настройки ---

def get_settings_values(db: Session) -> dict[str, str]:
    values = {SETTING_INTAKE_KEY: "", SETTING_MANAGER_ID: ""}
    for row in settings_repo.rows_with_prefix(db, SETTINGS_PREFIX):
        if row.key in values:
            values[row.key] = row.value
    return values


def public_settings(db: Session) -> dict:
    """Настройки для экрана: сам ключ не отдаём, только факт, что он задан."""
    values = get_settings_values(db)
    manager = responsible(db, silent=True)
    return {
        "has_intake_key": bool(values[SETTING_INTAKE_KEY]),
        "manager_id": manager.id if manager else None,
        "manager_name": manager.name if manager else "",
        "intake_path": "/api/v1/public/leads",
        "intake_header": INTAKE_KEY_HEADER,
    }


def regenerate_intake_key(db: Session) -> str:
    """Новый ключ приёма. Показывается один раз — как секрет вебхука АТС.

    Смена ключа немедленно закрывает форму для всех, кто знал прежний. Это и
    есть единственный способ отозвать доступ: подписи у формы нет (почему —
    в `check_intake_key`), и отличить «наш сайт» от «чужой сайт с нашим старым
    ключом» больше нечем.
    """
    key = secrets.token_urlsafe(32)
    settings_repo.write(db, SETTING_INTAKE_KEY, key)
    return key


def clear_intake_key(db: Session) -> None:
    """Закрыть приём совсем. Пустой ключ — это «ручки нет»."""
    settings_repo.write(db, SETTING_INTAKE_KEY, "")


def set_manager(db: Session, user_id: int | None) -> None:
    """Кому достаются заявки с сайта. `None` — вернуться к владельцу системы."""
    if user_id is None:
        settings_repo.write(db, SETTING_MANAGER_ID, "")
        return
    if users_repo.get_by_id(db, int(user_id)) is None:
        raise errors.NotFoundError("Manager not found", code="manager_not_found")
    settings_repo.write(db, SETTING_MANAGER_ID, str(int(user_id)))


def responsible(db: Session, silent: bool = False) -> User | None:
    """Ответственный за заявки с сайта.

    Заявке нужен живой человек, и не ради красоты: его именем заводится
    карточка, его именем создаётся работа и на него же ставится напоминание.
    «Ничья» заявка не берётся никем — каждый считает, что её возьмёт другой.

    Назначенный уволился или отключён — заявки достаются владельцу системы, а
    не пропадают. Это худший из хороших исходов: root хотя бы точно есть и
    точно увидит, что происходит, — а молчаливая потеря заявки с сайта
    обнаруживается только звонком рассерженного клиента.
    """
    raw = get_settings_values(db)[SETTING_MANAGER_ID].strip()
    user = users_repo.get_by_id(db, int(raw)) if raw.isdigit() else None
    if user is None or user.status != STATUS_ACTIVE:
        user = users_repo.get_root(db)
    if user is None and not silent:
        raise errors.ConflictError(
            "There is nobody to assign the lead to", code="no_responsible"
        )
    return user


# --- ключ приёма ---

def check_intake_key(db: Session, presented: str) -> None:
    """Ключ приёма верен — или запрос дальше не идёт.

    **Это ключ, а не подпись, и разница здесь принципиальная.** Вебхук АТС
    подписывает тело секретом (`telephony_service.verify_signature`), потому что
    у станции есть где хранить секрет. У формы на сайте такого места нет: всё,
    что видит браузер, видит и посетитель. Поэтому договор такой — заявку шлёт
    **сервер сайта**, а не страница в браузере, и ключ живёт у него.

    Что ключ даёт: закрывает ручку от сканеров, которые перебирают адреса, и
    даёт способ отозвать доступ, не переписывая код. Чего не даёт: он не
    доказывает, что тело не подменили по дороге, и не спасает, если его вставили
    прямо в HTML. От перебора и потока защищают ограничитель и потолки, а не он.

    Ключ не задан — приёма нет вовсе. Так и задумано: свежая установка не должна
    держать открытой ручку, о которой владелец ещё не знает.
    """
    expected = get_settings_values(db)[SETTING_INTAKE_KEY]
    if not expected:
        raise errors.AuthError("Lead intake is not configured", code="intake_not_configured")
    candidate = (presented or "").strip()
    # `compare_digest` на не-ASCII бросает TypeError, а заголовки Starlette
    # декодирует как latin-1: один байт 0xFF в заголовке иначе давал бы 500 на
    # запросе, для которого не нужно знать вообще ничего. Урок из телефонии.
    if not candidate.isascii():
        raise errors.AuthError("Bad intake key", code="bad_intake_key")
    # Побайтно и за постоянное время: обычное сравнение строк отвечает тем
    # быстрее, чем раньше разошлись байты, и ключ подбирается посимвольно.
    if not hmac.compare_digest(expected, candidate):
        raise errors.AuthError("Bad intake key", code="bad_intake_key")


# --- приём заявки ---

def receive(db: Session, payload: dict) -> Outcome:
    """Принять заявку с сайта.

    Порядок шагов здесь важнее их содержания:

    1. ловушка — до всего остального. Бот, попавшийся в неё, не должен узнать
       из ответа ни одной подробности, включая то, какие поля мы проверяем;
    2. разбор и проверка полей — до базы;
    3. поиск своего клиента — по почте и номеру;
    4. потолок новых карточек — только если карточку и вправду заводим;
    5. заявка, и лишь потом событие: подписчики обязаны получить уже
       состоявшиеся записи, а не намерение их создать.
    """
    # Ловушка. Поле скрыто стилями, человек его не видит и не заполняет; бот
    # заполняет всё, до чего дотянется.
    #
    # Ответ при этом обязан выглядеть успехом — этим ловушка и живёт. Скажи мы
    # «отказано», и подбирающий за десяток попыток выяснит, какое поле лишнее, и
    # перестанет его трогать; а так он уверен, что заявки доходят, и продолжает
    # слать их в никуда.
    if str(payload.get("website") or "").strip():
        logger.info("заявка с сайта отброшена: заполнено поле-ловушка")
        return Outcome(status="ignored")

    # Имя и текст обрезаем, адрес и номер — нет (почему, см. `_clean_email`).
    # Обрезанное имя остаётся тем же человеком, обрезанный адрес — уже другой.
    name = str(payload.get("name") or "").strip()[:MAX_NAME]
    email = _clean_email(payload.get("email"))
    phone = _clean_phone(payload.get("phone"))
    message = str(payload.get("message") or "").strip()[:MAX_MESSAGE]

    if not email and not phone:
        # Заявка без обратной связи — не заявка: перезвонить некому, написать
        # некуда, и карточка получится пустой строкой в справочнике.
        raise errors.ValidationError(
            "An email address or a phone number is required", code="contact_required"
        )

    manager = responsible(db)
    phone_norm = normalize_phone(phone, _country_code(db))[:32]

    client = leads_repo.find_client(db, email, phone_norm)
    is_new_client = client is None
    if client is None:
        _check_intake_flood(db)
        client = client_service.create_client(
            db,
            {
                "name": name or email or phone,
                "phone": phone,
                "email": email,
                # Метка и источник — вместо колонки «статус», которой в системе
                # нет (разбор в докстроке модуля).
                "tags": [LEAD_TAG],
                "source": SOURCE_SITE,
                # Ответственный не указывается отдельно: `create_client` ставит
                # менеджером автора, а автор здесь и есть ответственный.
            },
            manager,
        )
    else:
        # Известную карточку форма НЕ правит, хотя соблазн есть: клиент без
        # адреса прислал адрес, значит можно дописать. Нельзя. Заполнить форму
        # может кто угодно, и знание чужого номера превратилось бы в способ
        # переписать в чужой карточке почту — то есть увести всю дальнейшую
        # переписку на свой ящик. Новые сведения приезжают в тексте заявки, и
        # переносит их в карточку человек.
        recent = leads_repo.deal_since(
            db, client.id, now_utc() - timedelta(seconds=DOUBLE_SUBMIT_SECONDS)
        )
        if recent is not None:
            # Повтор того же обращения. Ни второй заявки, ни второго
            # напоминания: и то и другое означало бы двойную работу менеджера
            # там, где клиент всего лишь нажал кнопку дважды.
            logger.info("повторная заявка с сайта от клиента %s — заявка %s", client.id, recent.id)
            return Outcome(status="duplicate", client=client, deal=recent)

    deal = deal_service.create_deal(
        db,
        {
            "title": _deal_title(message),
            "client_id": client.id,
            "description": message,
            # Этап не указываем: `create_deal` ставит первый открытый этап
            # воронки, а какой он — дело настроек бизнеса, а не формы.
        },
        manager,
    )

    events.emit(
        LEAD_RECEIVED,
        db=db,
        # Исполнителем стоит ответственный, а не «никто»: записи о заявке уже
        # заведены его именем, и напоминание обязано принадлежать тому же
        # человеку. Своего источника (`site_form`) у события пока нет — в
        # `database/models/audit.py` перечислены три, и добавление четвёртого
        # это правка модели, то есть отдельный заход. Пока его нет, ни один
        # подписчик этой цепочки в журнал не пишет.
        actor=manager,
        reason="request from the website form",
        client=client,
        deal=deal,
        is_new_client=is_new_client,
    )
    return Outcome(status="created" if is_new_client else "linked", client=client, deal=deal)


def _check_intake_flood(db: Session) -> None:
    """Потолок на новые карточки. Разбор — у `MAX_NEW_CLIENTS_PER_HOUR`."""
    made = leads_repo.count_clients_since(db, SOURCE_SITE, now_utc() - timedelta(hours=1))
    if made < MAX_NEW_CLIENTS_PER_HOUR:
        return
    # В журнал — обязательно: снаружи это выглядит как «форма не работает», и
    # владелец должен узнать причину из лога, а не из догадок.
    logger.warning(
        "форма с сайта завела %s карточек за час — приём новых закрыт до конца окна", made
    )
    raise errors.RateLimitedError(
        "Too many new requests from the website form", code="lead_intake_flooded"
    )


def _clean_email(value) -> str:
    """Адрес из формы: либо годный, либо отказ. Обрезать нельзя.

    Обрезанный адрес — это ДРУГОЙ адрес (тот же довод, что у ящиков фирмы в
    `mail_service._clean_address`), а здесь он вдобавок ключ поиска своего
    клиента: по обрезанному заявка не найдёт карточку и заведёт вторую.

    Пустое поле — не ошибка: форма, спрашивающая один телефон, — обычное дело.
    """
    address = str(value or "").strip()
    if not address:
        return ""
    if len(address) > MAX_EMAIL:
        raise errors.ValidationError(
            f"Email address is too long (max {MAX_EMAIL} characters)", code="email_too_long"
        )
    if not is_valid_email(address):
        raise errors.ValidationError("A valid email address is required", code="bad_email")
    return address


def _clean_phone(value) -> str:
    """Номер из формы. Тоже без обрезания и по той же причине, что адрес."""
    phone = str(value or "").strip()
    if len(phone) > MAX_PHONE:
        raise errors.ValidationError(
            f"Phone number is too long (max {MAX_PHONE} characters)", code="phone_too_long"
        )
    return phone


def _deal_title(message: str) -> str:
    """Название заявки на доске.

    Одно «Website request» на всю колонку — доска, по которой нельзя работать:
    карточки неразличимы, и открывать приходится каждую. Поэтому в название
    уезжает первая строка обращения; нет обращения — остаётся общее слово.
    """
    excerpt = message.strip().splitlines()[0].strip() if message.strip() else ""
    if not excerpt:
        return "Website request"
    return f"Website request: {excerpt[:TITLE_EXCERPT]}"


def _country_code(db: Session) -> str:
    return settings_service.get_all(db).get("default_country_code", "")
