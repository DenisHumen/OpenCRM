"""Сотрудники: одобрение, отключение, сброс пароля, роль.

Два разных права, а не одно. `staff.manage` — завести человека и открыть ему
вход; `roles.manage` — решить, что ему можно. Слитые в одно они означали бы, что
всякий, кто заводит сотрудников, может выдать себе что угодно через новую роль.
"""

from datetime import date

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from core.services import auth_service, permissions_service, report_service
from database.models import User
from database.repositories import deals as deals_repo
from database.repositories import users as users_repo
from web.api import schemas
from web.api.deps import get_db, require_perm

router = APIRouter(prefix="/staff", tags=["staff"])

@router.get("")
def list_staff(
    status: str | None = Query(default=None),
    _: User = Depends(require_perm("staff", "view")),
    db: Session = Depends(get_db),
):
    users = users_repo.list_staff(db, status=status)
    roles = {role.id: role for role in permissions_service.list_roles(db)}
    # Открытые заявки у каждого — одним запросом: «кто перегружен» спрашивают у
    # штата, а не у канбана по одному человеку.
    zayavki = deals_repo.otkrytye_po_menedzheram(db)
    return {
        "items": [
            {**schemas.user_out(u, role=roles.get(u.role_id)), "deals_open": zayavki.get(u.id, 0)}
            for u in users
        ]
    }


@router.get("/export.csv")
def export_staff(
    actor: User = Depends(require_perm("staff", "view")),
    db: Session = Depends(get_db),
):
    """Список сотрудников файлом.

    Право то же, что на просмотр, и это не послабление: выгрузка отдаёт ровно
    те строки, которые человек и так видит на экране. Заведи ей своё право — и
    вышло бы, что смотреть можно, а сохранить нельзя; обошли бы это выделением
    мышью.

    Отдельным адресом с расширением, а не флагом у списка: у списка ответ в
    JSON, здесь — файл целиком.
    """
    yazyk = "ru" if actor.locale == "ru" else "en"
    sostoyaniya = report_service.STAFF_STATE_NAMES[yazyk]
    users = users_repo.list_staff(db)
    roli = {role.id: role for role in permissions_service.list_roles(db)}
    zayavki = deals_repo.otkrytye_po_menedzheram(db)
    stroki = [
        [
            u.name,
            u.email,
            "root" if u.role == "root" else (roli[u.role_id].name if u.role_id in roli else ""),
            sostoyaniya.get(u.status, u.status),
            str(zayavki.get(u.id, 0)),
            u.created_at.strftime("%Y-%m-%d") if u.created_at else "",
            u.last_login_at.strftime("%Y-%m-%d %H:%M") if u.last_login_at else "",
            u.last_seen_at.strftime("%Y-%m-%d %H:%M") if u.last_seen_at else "",
        ]
        for u in users
    ]
    soderzhimoe = report_service.to_csv(stroki, report_service.CSV_HEADERS["staff"][yazyk])
    # Имя с датой: в папке «Загрузки» через месяц лежит пять выгрузок, и
    # «staff.csv (3)» не отвечает, какая из них свежая.
    imya = f"staff-{date.today().isoformat()}.csv"
    return Response(
        content=soderzhimoe,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{imya}"',
            "Cache-Control": "no-store",
        },
    )


# Право и исполнитель берутся одной зависимостью: `require_perm` и проверяет
# доступ, и отдаёт того, кто действует. Исполнитель нужен по имени — выдача и
# снятие доступа уходят в журнал, а журнал без имени того, кто это сделал,
# отвечает на половину вопроса.
@router.post("/{user_id}/approve")
def approve(
    user_id: int,
    actor: User = Depends(require_perm("staff", "manage")),
    db: Session = Depends(get_db),
):
    return schemas.user_out(auth_service.approve(db, actor, user_id))


@router.post("/{user_id}/reject")
def reject(
    user_id: int,
    actor: User = Depends(require_perm("staff", "manage")),
    db: Session = Depends(get_db),
):
    auth_service.reject(db, actor, user_id)
    return {"message": "Registration rejected"}


@router.post("/{user_id}/disable")
def disable(
    user_id: int,
    actor: User = Depends(require_perm("staff", "manage")),
    db: Session = Depends(get_db),
):
    return schemas.user_out(auth_service.disable(db, actor, user_id))


@router.post("/{user_id}/enable")
def enable(
    user_id: int,
    actor: User = Depends(require_perm("staff", "manage")),
    db: Session = Depends(get_db),
):
    return schemas.user_out(auth_service.enable(db, actor, user_id))


@router.post("/{user_id}/reset-password")
def reset_password(
    user_id: int,
    actor: User = Depends(require_perm("staff", "manage")),
    db: Session = Depends(get_db),
):
    user, temp_password = auth_service.reset_password(db, actor, user_id)
    return {"user": schemas.user_out(user), "temp_password": temp_password}


@router.post("/{user_id}/role")
def set_role(
    user_id: int,
    payload: schemas.RoleUpdateIn,
    actor: User = Depends(require_perm("roles", "manage")),
    db: Session = Depends(get_db),
):
    """Сделать сотрудника root'ом или вернуть обратно.

    Это не «роль» в смысле конструктора доступов, а признак владельца системы:
    у root права все и всегда, и набором их не описать. Должность с набором
    прав назначается отдельно — `POST /roles/assign/{user_id}`.
    """
    return schemas.user_out(auth_service.set_role(db, actor, user_id, payload.role))


@router.delete("/{user_id}")
def delete_user(
    user_id: int,
    actor: User = Depends(require_perm("staff", "manage")),
    db: Session = Depends(get_db),
):
    auth_service.delete_user(db, actor, user_id)
    return {"message": "User deleted"}
