"""Свои юрлица: от чьего имени работаем.

Читать может любой сотрудник: без списка фирм менеджер не выберет, от кого
ведётся заявка, и не увидит, чьи реквизиты уйдут в бланк. Менять — только root.
Это решение уровня «кто мы такие»: налоговый номер и банковский счёт правятся
раз в несколько лет, а ошибка в них всплывает на бумаге, которая уже у клиента.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from core.services import company_service
from database.models import User
from web.api import schemas
from web.api.deps import get_db, require_module, require_root, require_staff

router = APIRouter(
    prefix="/companies",
    tags=["companies"],
    dependencies=[Depends(require_module("companies"))],
)


@router.get("")
def list_companies(_: User = Depends(require_staff), db: Session = Depends(get_db)):
    return {"items": [schemas.company_out(c) for c in company_service.list_companies(db)]}


@router.get("/{company_id}")
def get_company(
    company_id: int,
    _: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    return schemas.company_out(company_service.get_company(db, company_id))


@router.post("", status_code=201)
def create_company(
    payload: schemas.CompanyIn,
    _: User = Depends(require_root),
    db: Session = Depends(get_db),
):
    company = company_service.create(db, payload.model_dump(exclude_unset=True))
    return schemas.company_out(company)


@router.patch("/{company_id}")
def update_company(
    company_id: int,
    payload: schemas.CompanyPatchIn,
    _: User = Depends(require_root),
    db: Session = Depends(get_db),
):
    # exclude_unset: «поле не прислали» и «прислали пустым» — разные вещи.
    # Второе означает «сотри реквизит», и подменять это молчаливым сохранением
    # старого значения нельзя.
    company = company_service.update(db, company_id, payload.model_dump(exclude_unset=True))
    return schemas.company_out(company)


@router.post("/{company_id}/default")
def make_default(
    company_id: int,
    _: User = Depends(require_root),
    db: Session = Depends(get_db),
):
    """Отдельная точка, а не поле в PATCH.

    Назначение основной задевает другую запись — ту, с которой признак
    снимается. Прятать это внутрь обычного сохранения формы значит менять две
    строки там, где человек правил одну.
    """
    company_service.set_default(db, company_id)
    return {"items": [schemas.company_out(c) for c in company_service.list_companies(db)]}


@router.delete("/{company_id}")
def delete_company(
    company_id: int,
    _: User = Depends(require_root),
    db: Session = Depends(get_db),
):
    company_service.delete(db, company_id)
    return {"message": "Company deleted"}
