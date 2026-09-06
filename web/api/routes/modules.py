"""Какие блоки системы включены.

Читать может любой сотрудник, и права на это нет намеренно: интерфейс должен
знать, что показывать в меню, а что скрыть, — иначе он спрячет всё или не
спрячет ничего. Переключать — `settings.manage`: это решение уровня «каким
бизнесом мы занимаемся», а не личная настройка менеджера.
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core import modules
from core.services import modules_service, pipeline_service, settings_service
from database.models import User
from database.repositories import stats as stats_repo
from database.repositories import users as users_repo
from web.api.deps import get_db, require_perm, require_staff

router = APIRouter(prefix="/modules", tags=["modules"])


class ModuleIn(BaseModel):
    enabled: bool


def _with_authors(db: Session, items: list[dict]) -> list[dict]:
    """Подставляет имя того, кто переключил.

    Без имени запись «выключено 3 августа» отвечает на половину вопроса: когда
    раздел пропал из меню, спрашивают не только «когда», но и «кто».
    """
    ids = {item["updated_by"] for item in items if item["updated_by"]}
    names = {u.id: u.name for u in users_repo.get_many(db, ids)} if ids else {}
    for item in items:
        item["updated_by_name"] = names.get(item.pop("updated_by"))
    return items


@router.get("")
def list_modules(
    _: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    return {"items": _with_authors(db, modules_service.details(db))}


class PresetIn(BaseModel):
    key: str
    #: Ставить ли заодно воронку и слово для заявки. По умолчанию да: набор
    #: отвечает на вопрос «чем вы занимаетесь» целиком, а не на его треть.
    apply_pipeline: bool = True
    apply_deal_term: bool = True


@router.get("/records")
def module_records(
    _: User = Depends(require_perm("settings", "manage")),
    db: Session = Depends(get_db),
):
    """Сколько записей стоит за каждым блоком. Отдельной ручкой, а не в
    `GET /modules`: тот спрашивает каждый экран, а счёт по таблицам нужен
    только перед выключателем."""
    return {"items": stats_repo.zapisey_po_blokam(db)}


@router.get("/presets")
def list_presets(
    _: User = Depends(require_perm("settings", "manage")), db: Session = Depends(get_db)
):
    """Наборы блоков под тип дела и что в каждом включится.

    Состав приходит с сервера, а не лежит в экране: список ключей во фронтенде
    разошёлся бы с реестром на первом же новом блоке, и заметить это было бы
    некому — так однажды разошлись значки блоков между меню и настройками.

    Рядом с составом отдаём текущее состояние: экран показывает, что из набора
    уже включено, чтобы выбор не выглядел как «нажму и что-то произойдёт».

    Закрыто тем же правом, что и применение. Список блоков (`GET /modules`)
    открыт любому сотруднику — интерфейсу надо знать, что показывать в меню, —
    а наборы нужны ровно тому, кто решает состав системы, и показывать их
    остальным незачем.
    """
    current = modules_service.state(db)
    return {
        "items": [
            {
                "key": preset.key,
                "modules": list(preset.modules),
                # Чего ещё нет — то и появится. Считает сервер: он же знает
                # зависимости и то, что уже включено.
                "will_enable": [k for k in preset.modules if not current.get(k)],
                "pipeline": preset.pipeline,
                "deal_term": preset.deal_term,
            }
            for preset in modules.PRESETS
        ]
    }


@router.post("/presets")
def apply_preset(
    payload: PresetIn,
    user: User = Depends(require_perm("settings", "manage")),
    db: Session = Depends(get_db),
):
    """Применить набор: блоки, воронка и слово для заявки — одним действием.

    Набор **только включает**. Забрать раздел с данными одним нажатием — самый
    быстрый способ потерять доверие, поэтому блоки вне набора остаются как есть.

    Воронка и слово ставятся вместе с блоками: человек отвечал на вопрос «чем вы
    занимаетесь», а не «какие разделы показать». Отключить обе части можно
    флагами — они нужны тому, кто вернулся к экрану уже с настроенной воронкой.
    """
    result = modules_service.apply_preset(db, payload.key, user)

    if payload.apply_pipeline:
        # Через тот же сервис, что и выбор пресета руками: у воронки своя
        # механика переселения сделок, и обходить её нельзя.
        pipeline_service.apply_preset(db, result["pipeline"], user)
    if payload.apply_deal_term:
        settings_service.update(db, {"deal_term": result["deal_term"]})

    result["items"] = _with_authors(db, modules_service.details(db))
    return result


@router.post("/{key}")
def switch_module(
    key: str,
    payload: ModuleIn,
    user: User = Depends(require_perm("settings", "manage")),
    db: Session = Depends(get_db),
):
    modules_service.set_enabled(db, key, payload.enabled, user)
    return {"items": _with_authors(db, modules_service.details(db))}
