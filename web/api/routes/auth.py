import secrets

from fastapi import APIRouter, Depends, Request, Response, UploadFile
from sqlalchemy.orm import Session

from config.settings import get_settings
from core.services import auth_service, avatar_service
from database.models import User
from web.api import schemas
from web.api.deps import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    get_current_user,
    get_db,
    login_limiter,
)

router = APIRouter(prefix="/auth", tags=["auth"])


def _set_auth_cookies(response: Response, session_token: str) -> None:
    settings = get_settings()
    secure = settings.env == "production"
    max_age = settings.session_ttl_days * 24 * 3600
    response.set_cookie(
        SESSION_COOKIE, session_token,
        httponly=True, secure=secure, samesite="lax", max_age=max_age,
    )
    # CSRF-cookie читается фронтендом и возвращается заголовком X-CSRF-Token
    response.set_cookie(
        CSRF_COOKIE, secrets.token_urlsafe(24),
        httponly=False, secure=secure, samesite="lax", max_age=max_age,
    )


@router.post("/register", status_code=201)
def register(payload: schemas.RegisterIn, db: Session = Depends(get_db)):
    user = auth_service.register(db, payload.name, payload.email, payload.password)
    return {
        "message": "Registration submitted. Wait for approval by the administrator.",
        "user": schemas.user_out(user),
    }


@router.post("/login")
def login(payload: schemas.LoginIn, response: Response, db: Session = Depends(get_db)):
    user, token = auth_service.login(db, payload.email, payload.password, login_limiter)
    _set_auth_cookies(response, token)
    return schemas.user_out(user)


@router.post("/logout")
def logout(request: Request, response: Response, db: Session = Depends(get_db)):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        auth_service.logout(db, token)
    response.delete_cookie(SESSION_COOKIE)
    response.delete_cookie(CSRF_COOKIE)
    return {"message": "Logged out"}


@router.get("/me")
def me(user: User = Depends(get_current_user)):
    return schemas.user_out(user)


@router.get("/heartbeat")
def heartbeat(user: User = Depends(get_current_user)):
    """Лёгкий пинг присутствия: сам факт запроса обновляет last_seen (в deps)."""
    return {"is_online": True}


@router.post("/me/avatar", status_code=201)
async def upload_avatar(
    file: UploadFile,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    content = await file.read()
    avatar_service.save_avatar(db, user, content)
    return schemas.user_out(user)


@router.delete("/me/avatar")
def delete_avatar(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    avatar_service.clear_avatar(db, user)
    return schemas.user_out(user)


@router.patch("/me")
def update_me(
    payload: schemas.ProfileIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    updated = auth_service.update_profile(db, user, name=payload.name, locale=payload.locale)
    return schemas.user_out(updated)


@router.post("/me/password")
def change_password(
    payload: schemas.PasswordChangeIn,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    current_token = request.cookies.get(SESSION_COOKIE)
    auth_service.change_password(
        db, user, payload.old_password, payload.new_password, current_token=current_token
    )
    return {"message": "Password changed"}
