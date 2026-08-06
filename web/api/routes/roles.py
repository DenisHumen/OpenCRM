"""Роли и конструктор доступов.

Матрицу (`GET /roles/matrix`) читает любой сотрудник: интерфейс по ней решает,
что показывать, — как и список блоков. Всё остальное требует `roles.manage`,
которого по умолчанию нет ни у кого, кроме root.
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core import permissions as core_permissions
from core.services import permissions_service
from database.models import User
from web.api import schemas
from web.api.deps import get_db, require_perm, require_staff

router = APIRouter(prefix="/roles", tags=["roles"])

manage = Depends(require_perm("roles", "manage"))


class RoleIn(BaseModel):
    name: str
    permissions: list[str] = []


class RolePatchIn(BaseModel):
    name: str | None = None
    permissions: list[str] | None = None


class RoleFromPresetIn(BaseModel):
    preset: str
    name: str | None = None


class AssignIn(BaseModel):
    #: null — снять должность. Сотрудник останется в системе, но без прав.
    role_id: int | None = None


@router.get("/matrix")
def matrix(_: User = Depends(require_staff)):
    """Из чего собирается роль: строки — разделы, столбцы — действия.

    Строится по реестру блоков на каждый запрос. Появился блок — появилась
    строка, и править здесь ничего не нужно.
    """
    return {
        "areas": permissions_service.matrix(),
        "actions": list(core_permissions.ACTIONS),
        "presets": [
            {"key": key, "name": value["name"], "hint": value["hint"],
             "permissions": list(value["permissions"])}
            for key, value in permissions_service.PRESETS.items()
        ],
    }


@router.get("")
def list_roles(_: User = Depends(require_perm("roles", "view")), db: Session = Depends(get_db)):
    roles = permissions_service.list_roles(db)
    return {
        "items": [
            schemas.role_out(
                role,
                codes=sorted(permissions_service.codes_of_role(db, role.id)),
                users_count=permissions_service.count_users_of(db, role.id),
            )
            for role in roles
        ]
    }


@router.get("/{role_id}")
def get_role(
    role_id: int,
    _: User = Depends(require_perm("roles", "view")),
    db: Session = Depends(get_db),
):
    role = permissions_service.get_role(db, role_id)
    return schemas.role_out(
        role,
        codes=sorted(permissions_service.codes_of_role(db, role.id)),
        users_count=permissions_service.count_users_of(db, role.id),
    )


@router.post("", status_code=201, dependencies=[manage])
def create_role(payload: RoleIn, db: Session = Depends(get_db)):
    role = permissions_service.create_role(db, payload.name, payload.permissions)
    return schemas.role_out(role, codes=sorted(permissions_service.codes_of_role(db, role.id)))


@router.post("/from-preset", status_code=201, dependencies=[manage])
def create_from_preset(payload: RoleFromPresetIn, db: Session = Depends(get_db)):
    """Готовый набор под должность. Пресет — начало, а не приговор: сразу после
    создания роль правится как любая другая."""
    role = permissions_service.create_from_preset(db, payload.preset, payload.name)
    return schemas.role_out(role, codes=sorted(permissions_service.codes_of_role(db, role.id)))


@router.patch("/{role_id}")
def update_role(
    role_id: int,
    payload: RolePatchIn,
    actor: User = Depends(require_perm("roles", "manage")),
    db: Session = Depends(get_db),
):
    """Переписать должность.

    Исполнитель берётся по имени, а не просто проверяется: своей роли новых
    прав не выписывают — иначе запрет «нельзя назначить роль себе» обходится
    тем, что назначать ничего и не нужно.
    """
    role = permissions_service.update_role(
        db, role_id, payload.name, payload.permissions, actor=actor
    )
    return schemas.role_out(role, codes=sorted(permissions_service.codes_of_role(db, role.id)))


@router.post("/{role_id}/default", dependencies=[manage])
def make_default(role_id: int, db: Session = Depends(get_db)):
    """Какую должность получает новый сотрудник при регистрации."""
    role = permissions_service.set_default(db, role_id)
    return schemas.role_out(role, codes=sorted(permissions_service.codes_of_role(db, role.id)))


@router.delete("/{role_id}", dependencies=[manage])
def delete_role(role_id: int, db: Session = Depends(get_db)):
    permissions_service.delete_role(db, role_id)
    return {"message": "Role deleted"}


@router.post("/assign/{user_id}")
def assign(
    user_id: int,
    payload: AssignIn,
    actor: User = Depends(require_perm("roles", "manage")),
    db: Session = Depends(get_db),
):
    """Назначить сотруднику должность.

    Себе — нельзя, и это не вежливость: иначе всякий, кто управляет правами, мог
    бы выдать себе любое другое право или снять с себя ограничение, ради
    которого роль и заводилась.
    """
    user = permissions_service.assign(db, actor, user_id, payload.role_id)
    role = permissions_service.get_role(db, user.role_id) if user.role_id else None
    return schemas.user_out(user, role=role)
