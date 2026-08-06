from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from config.settings import get_settings
from core import exceptions as errors
from core.security import tokens
from core.services import board_service, media_service, settings_service, share_service
from database.repositories import boards as boards_repo
from web.api import schemas
from web.api.deps import client_ip, get_db, pin_limiter
from web.public import layout

router = APIRouter(tags=["public"])

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# Тексты страницы ручного обслуживания (её отдаёт middleware в web/main.py).
# Держим здесь, рядом с остальными строками публичной части и её шаблонами.
MAINTENANCE_STRINGS = {
    "en": {
        "title": "Closed for maintenance",
        "text": "The site is temporarily unavailable. Please come back a little later.",
        "foot": "Идут технические работы.",
    },
    "ru": {
        "title": "Технические работы",
        "text": "Сайт временно недоступен. Загляните чуть позже.",
        "foot": "The site is closed for maintenance.",
    },
}

# словари витрины: язык задаётся настройкой сайта showcase_locale
STRINGS = {
    "en": {
        "works": "works",
        "work": "work",
        "updated": "updated",
        "pin_title": "This collection is protected",
        "pin_hint": "Enter the code to view it",
        "pin_bottom_hint": "Your manager sent you the code",
        "pin_placeholder": "Access code",
        "pin_submit": "Open",
        "pin_wrong": "That code didn't match — check it and try again",
        "pin_rate_limited": "Too many attempts. Please try again in a few minutes",
        "closed_title": "This collection is not available",
        "closed_text": "The link may have been closed or replaced. Contact us and we will send you a fresh one.",
        "empty_board": "Works are coming soon",
        "made_by": "Curated by",
        "video": "video",
        # надписи кнопок оставляем английскими в обеих локалях — так на референсе,
        # и это часть визуального языка витрины, а не интерфейсный текст
        "view_case": "View case",
        "return_to_site": "Return to the site",
        # подсказка на обрезанной длинной работе: кликом открывается целиком
        "view_full": "View full",
    },
    "ru": {
        "works": "работ",
        "work": "работа",
        "updated": "обновлено",
        "pin_title": "Эта подборка защищена кодом",
        "pin_hint": "Введите код, чтобы открыть её",
        "pin_bottom_hint": "Код вам отправил менеджер",
        "pin_placeholder": "Код доступа",
        "pin_submit": "Открыть",
        "pin_wrong": "Код не подошёл — проверьте и попробуйте ещё раз",
        "pin_rate_limited": "Слишком много попыток. Попробуйте через несколько минут",
        "closed_title": "Доступ к этой подборке закрыт",
        "closed_text": "Возможно, ссылка устарела или была заменена. Свяжитесь с нами — пришлём актуальную.",
        "empty_board": "Работы скоро появятся",
        "made_by": "Собрано в",
        "video": "видео",
        "view_case": "View case",
        "return_to_site": "Return to the site",
        "view_full": "View full",
    },
}


def _ctx(db: Session) -> tuple[dict, dict]:
    site = settings_service.get_all(db)
    strings = STRINGS.get(site.get("showcase_locale", "en"), STRINGS["en"])
    return site, strings


def _pin_cookie_name(link_id: int) -> str:
    return f"opencrm_bv_{link_id}"


def _has_pin_access(request: Request, link) -> bool:
    if link.pin_hash is None:
        return True
    value = request.cookies.get(_pin_cookie_name(link.id))
    return bool(value and tokens.check_pin_access_cookie(value, link.id, link.pin_hash or ""))


def _closed_page(request: Request, db: Session):
    site, strings = _ctx(db)
    return templates.TemplateResponse(
        request, "closed.html", {"site": site, "t": strings}, status_code=404
    )


@router.get("/b/{token}")
def showcase(token: str, request: Request, db: Session = Depends(get_db)):
    resolved = share_service.resolve_public(db, token)
    if resolved is None:
        return _closed_page(request, db)
    link, board = resolved
    site, strings = _ctx(db)

    if not _has_pin_access(request, link):
        # страница PIN не раскрывает ничего о доске
        return templates.TemplateResponse(
            request, "pin.html",
            {"site": site, "t": strings, "token": token, "error": None,
             "og_default": _og_default_url(site)},
        )

    share_service.record_view(db, link, client_ip(request), request.headers.get("user-agent", ""))
    works = boards_repo.list_works(db, board.id, only_ready=True)
    cover = board_service.cover_work(db, board)
    base_url = get_settings().base_url.rstrip("/")
    og_image = None
    if link.pin_hash is None and cover is not None:
        media = media_service.work_media_urls(cover)
        card = media.get("card") or media.get("poster")
        og_image = f"{base_url}{card}" if card else None
    if og_image is None:
        og_image = _og_default_url(site)

    works_payload = [schemas.work_out(w, public=True) for w in works]
    return templates.TemplateResponse(
        request, "showcase.html",
        {
            "site": site,
            "t": strings,
            "board": board,
            "works": works_payload,
            "modules": layout.build_modules(len(works_payload)),
            # работы, которые в своё место не помещаются: им нужна подсказка
            # «открыть целиком» и размытие на срезе
            "cropped": layout.cropped_indexes(works_payload),
            "og_image": og_image,
            "page_url": f"{base_url}/b/{token}",
        },
    )


def _og_default_url(site: dict) -> str | None:
    if not site.get("og_default_image"):
        return None
    return f"{get_settings().base_url.rstrip('/')}{site['og_default_image']}"


@router.post("/b/{token}/pin")
def check_pin(
    token: str,
    request: Request,
    pin: str = Form(default=""),
    db: Session = Depends(get_db),
):
    resolved = share_service.resolve_public(db, token)
    if resolved is None:
        return _closed_page(request, db)
    link, _board = resolved
    site, strings = _ctx(db)

    try:
        ok = share_service.verify_pin(db, link, pin, client_ip(request), pin_limiter)
    except errors.RateLimitedError:
        return templates.TemplateResponse(
            request, "pin.html",
            {"site": site, "t": strings, "token": token, "error": strings["pin_rate_limited"]},
            status_code=429,
        )
    if not ok:
        return templates.TemplateResponse(
            request, "pin.html",
            {"site": site, "t": strings, "token": token, "error": strings["pin_wrong"]},
            status_code=401,
        )
    response = RedirectResponse(url=f"/b/{token}", status_code=303)
    response.set_cookie(
        _pin_cookie_name(link.id),
        tokens.make_pin_access_cookie(link.id, link.pin_hash or ""),
        httponly=True,
        secure=get_settings().cookies_secure,
        samesite="lax",
        # Срок жизни у пропуска теперь есть и в самой подписи
        # (`tokens.PIN_ACCESS_SECONDS`), и здесь: без `max_age` cookie
        # восстанавливается вместе с сессией браузера, а «продолжить с того же
        # места» делает это молча. Совпадение сроков важнее удобства: доступ,
        # который нельзя отозвать, отозвать нельзя.
        max_age=tokens.PIN_ACCESS_SECONDS,
    )
    return response


@router.get("/b/{token}/data")
def showcase_data(token: str, request: Request, db: Session = Depends(get_db)):
    resolved = share_service.resolve_public(db, token)
    if resolved is None:
        return JSONResponse(
            {"error": {"code": "not_found", "message": "Not found"}}, status_code=404
        )
    link, board = resolved
    if not _has_pin_access(request, link):
        return JSONResponse(
            {"error": {"code": "pin_required", "message": "PIN required"}}, status_code=401
        )
    works = boards_repo.list_works(db, board.id, only_ready=True)
    return {
        "board": {"title": board.title, "description": board.description},
        "works": [schemas.work_out(w, public=True) for w in works],
    }


# --- отдача медиа и брендинга (в проде это делает nginx) ---

_MEDIA_TYPES = {
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
}


@router.get("/media/{work_uid}/{filename}")
def media_file(work_uid: str, filename: str):
    if filename not in media_service.PUBLIC_FILENAMES or "/" in work_uid or "\\" in work_uid:
        return JSONResponse({"error": {"code": "not_found", "message": "Not found"}}, status_code=404)
    path = media_service.work_dir(work_uid) / filename
    if not path.is_file():
        return JSONResponse({"error": {"code": "not_found", "message": "Not found"}}, status_code=404)
    return FileResponse(
        path,
        media_type=_MEDIA_TYPES.get(path.suffix, "application/octet-stream"),
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/branding/{filename}")
def branding_file(filename: str):
    allowed = ("logo.", "site-logo.", "og-default.")
    if not filename.startswith(allowed) or "/" in filename or "\\" in filename:
        return JSONResponse({"error": {"code": "not_found", "message": "Not found"}}, status_code=404)
    path = get_settings().branding_dir / filename
    if not path.is_file():
        return JSONResponse({"error": {"code": "not_found", "message": "Not found"}}, status_code=404)
    return FileResponse(path, headers={"Cache-Control": "public, max-age=3600"})


@router.get("/avatars/{filename}")
def avatar_file(filename: str):
    # имя формирует сервер: <uuid>.webp; меняется при каждой смене аватара, поэтому
    # можно кэшировать надолго. Пускаем только .webp и без разделителей пути.
    if not filename.endswith(".webp") or "/" in filename or "\\" in filename:
        return JSONResponse({"error": {"code": "not_found", "message": "Not found"}}, status_code=404)
    path = get_settings().avatars_dir / filename
    if not path.is_file():
        return JSONResponse({"error": {"code": "not_found", "message": "Not found"}}, status_code=404)
    return FileResponse(
        path,
        media_type="image/webp",
        headers={"Cache-Control": "public, max-age=86400", "X-Content-Type-Options": "nosniff"},
    )


@router.get("/d/{number}")
def document_status(number: str, request: Request, db: Session = Depends(get_db)):
    """Состояние заказа по QR с квитанции — открывается без входа в систему.

    Ссылку могут переслать или подобрать, поэтому здесь ровно то, что и так
    напечатано у клиента на руках: номер, что приняли и текущее состояние.
    Ни цены, ни телефона клиента, ни имён сотрудников.
    """
    from core.services import document_service, modules_service
    from web.public.document_strings import strings_for

    # Блок бланков выключили — снаружи это должно выглядеть так же, как
    # несуществующий номер. Иначе «выключено» означало бы лишь «не видно в
    # меню», а старые QR-коды продолжали бы открывать данные заказов.
    if not modules_service.is_enabled(db, "documents"):
        return _closed_page(request, db)

    try:
        doc = document_service.by_number(db, number)
    except errors.NotFoundError:
        # Та же страница, что у закрытой витрины: по ответу нельзя отличить
        # «нет такого номера» от «есть, но не показываем», а значит перебором
        # номеров не узнать, сколько у мастерской заказов.
        return _closed_page(request, db)

    return templates.TemplateResponse(
        request,
        "document_status.html",
        {
            "doc": doc,
            "payload": document_service.payload_of(doc),
            "t": strings_for(doc.locale),
            "created": doc.created_at.strftime("%d.%m.%Y"),
        },
    )
