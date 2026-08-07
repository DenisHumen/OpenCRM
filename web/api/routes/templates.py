"""Шаблоны сообщений: справочник заготовок и готовый текст по ним.

Роутер тонкий, вся логика — в `core/services/template_service.py`. Здесь стоит
объяснить ровно одно решение: **подстановка — это чтение, поэтому `render`
сделан `GET`'ом.**

Соблазн взять POST велик: аргументов трое, и телом их слать привычнее. Но
`render` ничего не создаёт и не меняет — он отвечает на вопрос «как этот шаблон
будет выглядеть для этого клиента». POST на такой вопрос означал бы, что точку
придётся закрывать правом `templates.edit` (иначе перебор маршрутов справедливо
скажет, что изменяющий метод закрыт правом на просмотр) — то есть смотреть
готовый текст смог бы только тот, кому разрешено править сами шаблоны. А это
ровно наоборот тому, ради чего блок заводился: шаблоны читают все, кто пишет
клиентам, а правит их тот, кому дано право.
"""

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.services import template_service
from database.models import MessageTemplate, User
from web.api.deps import get_db, require_module, require_perm

router = APIRouter(
    prefix="/templates",
    tags=["templates"],
    dependencies=[Depends(require_module("templates"))],
)


class TemplateIn(BaseModel):
    name: str
    body: str
    #: `any` по умолчанию: шаблон, про который не сказали, где он применим,
    #: годится везде. Незнакомое значение сервис отвергает — тихо приводить его
    #: к «годится везде» значило бы расширять применимость опечаткой.
    channel: str = "any"


class TemplatePatchIn(BaseModel):
    name: str | None = None
    body: str | None = None
    channel: str | None = None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _out(template: MessageTemplate) -> dict:
    """Тело отдаётся и в списке тоже.

    Шаблонов у бизнеса десяток, а экран показывает выдержку из текста в строке
    и открывает правку без второго запроса: экономить здесь было бы экономией
    на том, чего не тратят.
    """
    return {
        "id": template.id,
        "name": template.name,
        "channel": template.channel,
        "body": template.body,
        "created_at": _iso(template.created_at),
        "updated_at": _iso(template.updated_at),
    }


# `/fields` объявлен ДО `/{template_id}`: иначе FastAPI разберёт «fields» как
# номер шаблона и ответит «это не число» вместо набора полей.
@router.get("/fields")
def list_fields(_: User = Depends(require_perm("templates", "view"))):
    """Закрытый набор полей: что можно написать в теле шаблона.

    Отдаётся с сервера, а не дублируется во фронтенде. Список полей во втором
    экземпляре разошёлся бы с реестром на первом же новом поле, и заметить это
    было бы некому — так уже расходились значки блоков между меню и настройками.
    """
    return {"items": template_service.fields()}


@router.get("")
def list_templates(
    channel: str | None = Query(default=None),
    _: User = Depends(require_perm("templates", "view")),
    db: Session = Depends(get_db),
):
    """Шаблоны, годные для канала. Без `channel` — все.

    Универсальный шаблон попадает в выдачу по любому каналу: он для того и есть.
    """
    return {"items": [_out(item) for item in template_service.search(db, channel)]}


@router.get("/{template_id}")
def get_template(
    template_id: int,
    _: User = Depends(require_perm("templates", "view")),
    db: Session = Depends(get_db),
):
    return _out(template_service.get_template(db, template_id))


@router.get("/{template_id}/render")
def render_template(
    template_id: int,
    client_id: int | None = Query(default=None),
    deal_id: int | None = Query(default=None),
    _: User = Depends(require_perm("templates", "view")),
    db: Session = Depends(get_db),
):
    """Готовый текст: `{text, missing, unknown}` плюс имя и канал шаблона.

    Без клиента и заявки тоже отвечает — это предпросмотр, где поля про них
    прочерки, а `missing` называет, каких данных не хватило. Отказывать здесь
    было бы отказом ответить на вопрос «как выглядит шаблон».

    `missing` и `unknown` приходят **всегда**, пустыми списками в том числе:
    форма ответа не зависит от того, повезло ли с данными, — иначе экрану
    пришлось бы угадывать, какие ключи сегодня бывают.
    """
    return template_service.render(db, template_id, client_id=client_id, deal_id=deal_id)


@router.post("", status_code=201)
def create_template(
    payload: TemplateIn,
    user: User = Depends(require_perm("templates", "create")),
    db: Session = Depends(get_db),
):
    return _out(template_service.create(db, payload.model_dump(exclude_unset=True), user))


@router.patch("/{template_id}")
def update_template(
    template_id: int,
    payload: TemplatePatchIn,
    _: User = Depends(require_perm("templates", "edit")),
    db: Session = Depends(get_db),
):
    return _out(
        template_service.update(db, template_id, payload.model_dump(exclude_unset=True))
    )


@router.delete("/{template_id}")
def delete_template(
    template_id: int,
    _: User = Depends(require_perm("templates", "delete")),
    db: Session = Depends(get_db),
):
    template_service.delete(db, template_id)
    return {"message": "Template deleted"}
