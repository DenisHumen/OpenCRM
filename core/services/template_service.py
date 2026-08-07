"""Шаблоны сообщений: типовой ответ, ссылка на доску, напоминание об оплате.

Сам шаблон — три поля и хранилище к ним. Всё содержание этого файла в другом: в
том, как текст с подстановками превращается в текст для клиента. Здесь четыре
решения, и каждое принято против конкретной беды.

**1. Набор полей закрыт и объявлен здесь, в коде.**

Соблазн сделать «любое поле модели» велик: тогда шаблон писался бы один раз и
навсегда, а новые поля появлялись бы сами. Цена этому — письмо клиенту с
себестоимостью его же заказа. Открытый набор означает, что менеджер, не
знающий, что такое `cost` и `lost_reason`, однажды подставит их в письмо и
узнает об этом от клиента. Поэтому поля перечислены поимённо, значения им
считает `_values`, и `getattr` по присланному имени здесь нет ни одного.

**2. Неизвестная подстановка не проходит молча.**

Молчаливая пустота — худший из возможных ответов: «Здравствуйте, !» уходит
клиенту, и это видят все, кроме автора шаблона. Защита стоит дважды.

- При сохранении: `{clietn_name}` (опечатка) или `{deal_amount}` (нет в наборе)
  — отказ `422 unknown_placeholder` с перечислением виноватых. Это главный
  рубеж: ошибку ловят там, где её сделали, и до клиента она не доезжает.
- При подстановке: неизвестное поле превращается в **видимую** пометку
  `[?имя]`, а не в пустоту. Сюда можно попасть только одним путём — поле убрали
  из набора уже после того, как шаблон написали, — но именно этот путь
  страшнее: шаблон писали год назад, а замолчал он сегодня.

**3. Законно пустое значение — не ошибка, но и не дыра.**

У заявки может не быть доски, у клиента — фирмы. Отказывать здесь нельзя: это
нормальная жизнь, а не поломка шаблона. Оставлять пустоту — тоже нельзя, по
пункту 2. Поэтому пустое значение становится тире (`—`), а `render` **называет
список таких полей** отдельно: экран предупреждает до отправки, и человек
видит, что «Ссылка на доску: —» — это не опечатка, а отсутствующая доска.

Тире, а не подставленное слово («уважаемый клиент»), намеренно: тело шаблона
пишут на своём языке, и английская заплатка посреди русского письма выглядит
хуже честного прочерка. Придумывать за автора слова — не дело подстановки.

**4. Подстановка не становится дырой.**

Тело шаблона — обычный текст, и значение вставляется в него ровно один раз и
буквально. Три свойства держат это:

- замена идёт функцией, а не строкой: `\\1` и `<b>` в имени клиента остаются
  такими, какими их набрали, и ничего не «раскрывают»;
- подставленное **не перечитывается**: клиент по имени «ООО {board_url}» не
  вытащит из шаблона ссылку на чужую доску;
- значение приводится к одной строке: имя с переводами строк не дописывает в
  письмо абзац «P.S. переведите деньги на другой счёт».
"""

import re
from dataclasses import dataclass

from sqlalchemy.orm import Session

from core import exceptions as errors
from core.services import modules_service, settings_service, share_service
from core.utils import now_utc
from core import uniqueness
from database.models import Client, Deal, MessageTemplate, User
from database.models.template import (
    CHANNEL_ANY,
    CHANNELS,
    MAX_TEMPLATE_BODY,
    MAX_TEMPLATE_NAME,
)
from database.repositories import boards as boards_repo
from database.repositories import clients as clients_repo
from database.repositories import companies as companies_repo
from database.repositories import deals as deals_repo
from database.repositories import shares as shares_repo
from database.repositories import templates as templates_repo

# --- подстановки -------------------------------------------------------------

#: Что считается подстановкой: `{` + имя из букв, цифр и подчёркиваний + `}`.
#:
#: Всё остальное в фигурных скобках — обычный текст и остаётся как есть. Так
#: шаблон может содержать `{{` , `{ }` или кусок JSON, не превращаясь в ошибку
#: сохранения из-за скобки, которую человек имел в виду буквально.
PLACEHOLDER = re.compile(r"\{([A-Za-z0-9_]+)\}")

#: Чем заменяется законно пустое значение. Разбор — в шапке, пункт 3.
EMPTY_MARK = "—"


def unknown_mark(key: str) -> str:
    """Видимая пометка вместо поля, которого в наборе нет. Разбор — пункт 2."""
    return f"[?{key}]"


#: Что нужно полю, чтобы у него было значение.
NEEDS_NOTHING = ""
NEEDS_CLIENT = "client"
NEEDS_DEAL = "deal"


@dataclass(frozen=True)
class Field:
    """Одно поле закрытого набора."""

    #: То, что пишут в теле: `{client_name}`.
    key: str
    #: Клиент, заявка или ничего. Экран по этому полю объясняет, почему
    #: подстановка пуста: не «сломалось», а «этот шаблон просит заявку».
    needs: str


#: **Закрытый набор.** Порядок — как показывать подсказку рядом с телом
#: шаблона: сначала то, чем письмо начинается, потом то, ради чего оно
#: пишется, потом подпись.
#:
#: Добавлять сюда можно, и добавлять надо по просьбе, а не про запас: каждое
#: поле — это то, что однажды уедет клиенту. Убирать — можно, но старые шаблоны
#: после этого покажут `[?имя]`, и это правильный ответ (см. пункт 2 в шапке).
FIELDS: tuple[Field, ...] = (
    Field(key="client_name", needs=NEEDS_CLIENT),
    Field(key="client_company", needs=NEEDS_CLIENT),
    Field(key="deal_title", needs=NEEDS_DEAL),
    Field(key="deal_number", needs=NEEDS_DEAL),
    Field(key="board_url", needs=NEEDS_DEAL),
    Field(key="company_name", needs=NEEDS_NOTHING),
)

FIELDS_BY_KEY: dict[str, Field] = {field.key: field for field in FIELDS}


def fields() -> list[dict]:
    """Набор для экрана. Собирается из реестра, а не пишется в интерфейсе."""
    return [{"key": field.key, "needs": field.needs} for field in FIELDS]


def placeholders_in(body: str) -> list[str]:
    """Имена подстановок в теле, по порядку появления и без повторов."""
    seen: list[str] = []
    for match in PLACEHOLDER.finditer(body or ""):
        if match.group(1) not in seen:
            seen.append(match.group(1))
    return seen


# --- значения полей ----------------------------------------------------------


def _one_line(value: str) -> str:
    """Значение занимает ровно одну строку. Разбор — пункт 4 в шапке.

    Схлопываются любые пробельные, а не только переводы строк: имя, набранное
    как «Иван    Петров», в письме выглядит опечаткой автора шаблона, а не
    клиента.
    """
    return " ".join((value or "").split())


def _deal_number(deal: Deal | None) -> str:
    """Номер заявки — это её `id` со знаком номера.

    Своего ряда номеров у заявок нет и не заводится ради шаблона: номер бланка
    (`2026-000123`) существует потому, что его печатают на бумаге и называют по
    телефону, а заявку называют словами («ваш ремонт ноутбука»). `#17` здесь —
    то, что система знает точно и что совпадает с адресом карточки.
    """
    return f"#{deal.id}" if deal is not None else ""


def _board_url(db: Session, deal: Deal | None) -> str:
    """Ссылка на доску заявки — та, которая прямо сейчас работает.

    Блок досок выключен — значения нет, и это не ошибка: выключенный блок
    исчезает целиком, а не подтекает в чужие тексты через шаблон.

    Отозванная и просроченная ссылки не годятся (`live_for_board`): подставить
    такую значит отправить клиента на страницу с отказом. Из живых берём самую
    раннюю — чтобы ответ не плавал между вызовами: молча меняющаяся ссылка
    хуже, чем неудобная.
    """
    if deal is None or not modules_service.is_enabled(db, "boards"):
        return ""
    now = now_utc()
    for board in boards_repo.for_deal(db, deal.id):
        links = shares_repo.live_for_board(db, board.id, now)
        if links:
            return share_service.public_url(min(links, key=lambda link: link.id))
    return ""


def _company_name(db: Session, deal: Deal | None) -> str:
    """От чьего имени пишем.

    Порядок: фирма заявки → основная фирма → название бизнеса из настроек.
    Последняя ступень нужна потому, что блок юрлиц выключают те, у кого фирма
    одна и в бумагах не участвует, — но подпись под письмом им нужна не меньше.
    Читать `companies` при выключенном блоке нельзя по тому же правилу, что и
    доски.

    Удалённая фирма не годится: заявка помнит её `company_id` (у мягкого
    удаления `SET NULL` не срабатывает), и подпись уехала бы старым названием.
    """
    if modules_service.is_enabled(db, "companies"):
        company = None
        if deal is not None and deal.company_id:
            company = companies_repo.get(db, deal.company_id)
        if company is not None and company.deleted_at is not None:
            company = None
        if company is None:
            company = companies_repo.default_company(db)
        if company is not None and company.name:
            return company.name
    return settings_service.get_all(db).get("brand_name", "")


def _values(db: Session, client: Client | None, deal: Deal | None) -> dict[str, str]:
    """Значения всех полей набора разом.

    Словарь строится поимённо и целиком — не по запросу «дай поле X». Так набор
    полей и набор значений не могут разойтись: забытое здесь поле не станет
    пустым, оно упадёт ключом при сборке ответа, и это заметят на первом же
    прогоне.
    """
    return {
        "client_name": client.name if client else "",
        "client_company": client.company if client else "",
        "deal_title": deal.title if deal else "",
        "deal_number": _deal_number(deal),
        "board_url": _board_url(db, deal),
        "company_name": _company_name(db, deal),
    }


# --- подстановка -------------------------------------------------------------


def substitute(body: str, values: dict[str, str]) -> dict:
    """Подставить значения в тело. Возвращает текст и то, чем он не хорош.

    `missing` — поля, у которых значения законно нет; `unknown` — те, которых
    нет в наборе. Списки отдельные, потому что беды разные: первая лечится
    выбором другой заявки, вторая — правкой шаблона.

    Замена идёт **функцией**: `re.sub` со строкой раскрыл бы `\\1` и `\\g<0>` в
    имени клиента, то есть значение управляло бы подстановкой. И она идёт **за
    один проход**: подставленное больше не просматривается, поэтому клиент с
    именем «ООО {board_url}» получит ровно своё имя, а не чужую ссылку.
    """
    missing: list[str] = []
    unknown: list[str] = []

    def replace(match: re.Match) -> str:
        key = match.group(1)
        if key not in FIELDS_BY_KEY:
            if key not in unknown:
                unknown.append(key)
            return unknown_mark(key)
        value = _one_line(values.get(key, ""))
        if not value:
            if key not in missing:
                missing.append(key)
            return EMPTY_MARK
        return value

    return {"text": PLACEHOLDER.sub(replace, body or ""), "missing": missing, "unknown": unknown}


def _pair(
    db: Session, client_id: int | None, deal_id: int | None
) -> tuple[Client | None, Deal | None]:
    """Клиент и заявка, о которых пишем, — и их согласие друг с другом.

    Заявка указана, клиент нет — берём клиента у заявки: заявку выбрал человек,
    а клиент у неё ровно один. Указаны оба и не сходятся — отказ с тем же кодом,
    что у письма и у звонка (`deal_other_client`): одинаковая беда обязана
    отвечать одинаково, иначе интерфейсу приходится разбирать три кода про одно
    и то же. Молча предпочесть одного из двоих нельзя — это ровно тот случай,
    когда клиент получает письмо с чужим именем.
    """
    deal = None
    if deal_id:
        deal = deals_repo.get(db, int(deal_id))
        if deal is None:
            raise errors.NotFoundError("Deal not found", code="deal_not_found")

    client = None
    if client_id:
        client = clients_repo.get(db, int(client_id))
        if client is None:
            raise errors.NotFoundError("Client not found", code="client_not_found")

    if deal is not None:
        if client is not None and deal.client_id != client.id:
            raise errors.ValidationError(
                "Deal belongs to another client", code="deal_other_client"
            )
        if client is None:
            client = clients_repo.get(db, deal.client_id)

    return client, deal


def render(
    db: Session,
    template_id: int,
    *,
    client_id: int | None = None,
    deal_id: int | None = None,
) -> dict:
    """Готовый текст шаблона для этого клиента и этой заявки.

    Без клиента и заявки тоже работает: получится предпросмотр, где все поля
    про них — прочерки, а `missing` называет, каких данных не хватило. Это
    честный ответ на вопрос «как выглядит шаблон», а не отказ отвечать.
    """
    template = get_template(db, template_id)
    client, deal = _pair(db, client_id, deal_id)
    result = substitute(template.body, _values(db, client, deal))
    return {
        "template_id": template.id,
        "name": template.name,
        "channel": template.channel,
        **result,
    }


# --- разбор присланного ------------------------------------------------------


def _clean_name(name: str | None) -> str:
    value = (name or "").strip()
    if not value:
        raise errors.ValidationError("Name is required", code="name_required")
    return value[:MAX_TEMPLATE_NAME]


def _clean_channel(value: str | None) -> str:
    """Где шаблон применим. Незнакомое значение — отказ, а не тихое `any`.

    Тихое приведение к «годится везде» означало бы, что опечатка в канале
    расширяет применимость шаблона, — то есть письмо клиенту предлагалось бы
    там, где его писать не собирались.
    """
    channel = (value or CHANNEL_ANY).strip()
    if channel not in CHANNELS:
        raise errors.ValidationError(
            f"Unknown channel: {channel}", code="unknown_channel"
        )
    return channel


def _clean_body(body: str | None) -> str:
    """Тело шаблона. Неизвестная подстановка — отказ с именами виноватых.

    Отказ, а не «сохраним и разберёмся при отправке»: между сохранением и первым
    применением проходят недели, а между применением и клиентом — ничего.
    Названы **все** неизвестные разом: сообщать по одному значит заставить
    человека сохранять шаблон столько раз, сколько он сделал опечаток.
    """
    value = (body or "").strip()
    if not value:
        raise errors.ValidationError("Body is required", code="body_required")
    if len(value) > MAX_TEMPLATE_BODY:
        raise errors.ValidationError(
            f"Body is too long (max {MAX_TEMPLATE_BODY} characters)", code="body_too_long"
        )
    unknown = [name for name in placeholders_in(value) if name not in FIELDS_BY_KEY]
    if unknown:
        raise errors.ValidationError(
            "Unknown placeholder(s): " + ", ".join(f"{{{name}}}" for name in unknown),
            code="unknown_placeholder",
        )
    return value


# --- чтение и правка ---------------------------------------------------------


def get_template(db: Session, template_id: int) -> MessageTemplate:
    template = templates_repo.get(db, template_id)
    if template is None:
        raise errors.NotFoundError("Template not found", code="template_not_found")
    return template


def search(db: Session, channel: str | None = None) -> list[MessageTemplate]:
    """Шаблоны, годные для этого канала. Пусто — все подряд."""
    return templates_repo.list_all(db, _clean_channel(channel) if channel else None)


def create(db: Session, data: dict, author: User) -> MessageTemplate:
    """Завести шаблон. Занятое название — `409`, а не второй такой же.

    Вставка через `uniqueness.insert_unique`: между проверкой и записью есть
    окно, и попадают в него не злоумышленники, а две вкладки с одной формой.
    """
    name = _clean_name(data.get("name"))
    template = MessageTemplate(
        name=name,
        channel=_clean_channel(data.get("channel")),
        body=_clean_body(data.get("body")),
        created_by=author.id,
    )
    return uniqueness.insert_unique(
        db,
        template,
        taken=lambda row: templates_repo.name_exists(db, row.name),
        message="A template with this name already exists",
        code="template_name_taken",
    )


def update(db: Session, template_id: int, data: dict) -> MessageTemplate:
    template = get_template(db, template_id)
    if "name" in data:
        name = _clean_name(data["name"])
        if templates_repo.name_exists(db, name, exclude_id=template.id):
            raise errors.ConflictError(
                "A template with this name already exists", code="template_name_taken"
            )
        template.name = name
    if "channel" in data:
        template.channel = _clean_channel(data["channel"])
    if "body" in data:
        template.body = _clean_body(data["body"])
    db.flush()
    return template


def delete(db: Session, template_id: int) -> None:
    """Удаление обычное, не мягкое.

    Шаблон — это заготовка, а не запись о случившемся: удалённый не оставляет
    сирот, потому что применённый шаблон давно превратился в самостоятельное
    письмо или запись ленты и связи с ним не хранит. Корзина здесь означала бы
    хранить черновики вечно ради того, чего никто не спросит.
    """
    db.delete(get_template(db, template_id))
    db.flush()
