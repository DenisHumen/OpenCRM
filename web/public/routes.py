from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from config.settings import get_settings
from core import bezopasnost
from core import exceptions as errors
from core.security import tokens
from core.services import (
    board_service,
    media_service,
    modules_service,
    settings_service,
    share_service,
)
from core.services import site_service

from database.models.document import KIND_ACT, KIND_INTAKE
from database.repositories import boards as boards_repo
from web.api import schemas
from web.api.deps import client_ip, document_limiter, get_db, pin_limiter
from web.public import layout

router = APIRouter(tags=["public"])

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# Тексты страницы ручного обслуживания (её отдаёт middleware в web/main.py).
# Держим здесь, рядом с остальными строками публичной части и её шаблонами.
MAINTENANCE_STRINGS = {
    # Подвал стоял наоборот: в английской ветке лежала русская фраза, в русской —
    # английская. Обе ветки при этом выглядели заполненными, и ни один прогон об
    # этом не говорил — перепутанное местами не отличить от переведённого, пока
    # не прочтёшь глазами обе строки сразу. Нашлось перебором словарей
    # (`tests/test_zasev_yazyk.py`), а не чтением.
    "en": {
        "title": "Closed for maintenance",
        "text": "The site is temporarily unavailable. Please come back a little later.",
        "foot": "The site is closed for maintenance.",
    },
    "ru": {
        "title": "Технические работы",
        "text": "Сайт временно недоступен. Загляните чуть позже.",
        "foot": "Идут технические работы.",
    },
}

#: Формы слова «работа» по числу — по одной строке на форму, а не на язык.
#:
#: В шаблоне стояло `t.works if works|length != 1 else t.work`, и для
#: английского этого хватает: у него ровно две формы. Русскому нужны три, и
#: витрина говорила «2 работ» и «21 работ» — то есть ошибалась на самых частых
#: числах, а верной была только на единице и на пятёрке.
#:
#: Правила ниже — те же, что у `Intl.PluralRules` во фронтенде (сверено
#: прогоном в node на числах 0,1,2,4,5,11,21,22,25,101,1234). Своими руками
#: здесь потому, что в стандартной библиотеке питона их нет, а тянуть ради
#: одного слова стороннюю зависимость на страницу, которую видит клиент, —
#: размен не в ту сторону.
FORMY_RABOT = {
    "en": {"one": "work", "other": "works"},
    "ru": {"one": "работа", "few": "работы", "many": "работ"},
}


def _forma_chisla(locale: str, n: int) -> str:
    """Какая форма нужна этому числу в этом языке."""
    if locale == "ru":
        desyatki, sotni = n % 10, n % 100
        if desyatki == 1 and sotni != 11:
            return "one"
        if 2 <= desyatki <= 4 and not 12 <= sotni <= 14:
            return "few"
        return "many"
    return "one" if n == 1 else "other"


def slovo_rabot(locale: str, n: int) -> str:
    """Слово «работа» в нужной форме. Зовётся из шаблона витрины."""
    formy = FORMY_RABOT.get(locale, FORMY_RABOT["en"])
    vid = _forma_chisla(locale, n)
    return formy.get(vid) or formy.get("other") or formy["one"]


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
        # Слишком частые обращения к странице заказа. Текст объясняет, что делать
        # (подождать), а не в чём провинился: человек за общим адресом оператора
        # сюда попадает не по своей вине.
        "busy_title": "Too many requests",
        "busy_text": "Please open this page again in a few minutes.",
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
        "busy_title": "Слишком много запросов",
        "busy_text": "Откройте страницу ещё раз через несколько минут.",
        "empty_board": "Работы скоро появятся",
        "made_by": "Собрано в",
        "video": "видео",
        "view_case": "View case",
        "return_to_site": "Return to the site",
        "view_full": "View full",
    },
}


# Помощник отдаётся шаблонам глобалью: тащить его через контекст каждой
# страницы значило бы поминать в пяти местах то, что нужно в одной строке.
templates.env.globals["slovo_rabot"] = slovo_rabot


def _ctx(db: Session) -> tuple[dict, dict]:
    site = settings_service.get_all(db)
    strings = STRINGS.get(site.get("showcase_locale", "en"), STRINGS["en"])
    return site, strings


#: Приставка cookie с пропуском по PIN. Одна ссылка — одна cookie.
PIN_COOKIE_PREFIX = "opencrm_bv_"


def _pin_cookie_name(link_id: int) -> str:
    return f"{PIN_COOKIE_PREFIX}{link_id}"


def _has_pin_access(request: Request, link) -> bool:
    if link.pin_hash is None:
        return True
    value = request.cookies.get(_pin_cookie_name(link.id))
    return bool(value and tokens.check_pin_access_cookie(value, link.id, link.pin_hash or ""))


def _closed_page(request: Request, db: Session, *, status_code: int = 404, busy: bool = False):
    """Страница «сюда нельзя». `busy` — не «нельзя», а «слишком часто».

    Разные слова здесь важнее, чем кажется. «Доступ закрыт, свяжитесь с нами»
    отправит человека звонить в мастерскую из-за того, что он трижды обновил
    страницу; а «попробуйте через несколько минут» он поймёт и подождёт.
    """
    site, strings = _ctx(db)
    if busy:
        strings = {**strings, "closed_title": strings["busy_title"], "closed_text": strings["busy_text"]}
    return templates.TemplateResponse(
        request, "closed.html", {"site": site, "t": strings}, status_code=status_code
    )


@router.get("/b/{token}")
def showcase(token: str, request: Request, db: Session = Depends(get_db)):
    # Блок досок выключили — публичные пути обязаны исчезнуть вместе с ним.
    # Правило проекта: выключенный блок исчезает ЦЕЛИКОМ, а не только из меню.
    # Иначе «выключил доски» означало бы лишь «убрал из интерфейса», а ранее
    # разосланные ссылки продолжали бы отдавать работы клиента всему интернету —
    # то есть ровно то, ради прекращения чего блок и выключают.
    if not modules_service.is_enabled(db, "boards"):
        return _closed_page(request, db)

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
            "locale": site.get("showcase_locale", "en"),
            "board": board,
            "works": works_payload,
            "modules": layout.build_modules(len(works_payload)),
            # работы, которые в своё место не помещаются: им нужна подсказка
            # «открыть целиком» и размытие на срезе. Страница одна на оба
            # экрана, а места на них разные — телефон считается отдельно
            "cropped": layout.cropped_indexes(works_payload),
            "cropped_on_phone": layout.phone_cropped_indexes(works_payload),
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
    # Блок досок выключили — публичные пути обязаны исчезнуть вместе с ним.
    # Правило проекта: выключенный блок исчезает ЦЕЛИКОМ, а не только из меню.
    # Иначе «выключил доски» означало бы лишь «убрал из интерфейса», а ранее
    # разосланные ссылки продолжали бы отдавать работы клиента всему интернету —
    # то есть ровно то, ради прекращения чего блок и выключают.
    if not modules_service.is_enabled(db, "boards"):
        return _closed_page(request, db)

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
    except errors.LimiterUnavailableError:
        # Счётчик попыток недоступен — PIN не проверяем вовсе. Пустить сюда без
        # счётчика значит отдать четырёхзначный PIN на перебор без предела,
        # причём именно тогда, когда за системой никто не смотрит. Текст берём
        # тот же, что у «слишком часто»: посетителю нужно подождать и повторить,
        # а чем именно занята мастерская — не его дело.
        return templates.TemplateResponse(
            request, "pin.html",
            {"site": site, "t": strings, "token": token, "error": strings["pin_rate_limited"]},
            status_code=503,
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
    # Блок досок выключили — публичные пути обязаны исчезнуть вместе с ним.
    # Правило проекта: выключенный блок исчезает ЦЕЛИКОМ, а не только из меню.
    # Иначе «выключил доски» означало бы лишь «убрал из интерфейса», а ранее
    # разосланные ссылки продолжали бы отдавать работы клиента всему интернету —
    # то есть ровно то, ради прекращения чего блок и выключают.
    if not modules_service.is_enabled(db, "boards"):
        return JSONResponse(
            {"error": {"code": "not_found", "message": "Not found"}}, status_code=404
        )

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


def _pins_held(request: Request) -> dict[int, str]:
    """Пропуска, которые посетитель предъявляет: {id ссылки: значение cookie}."""
    held: dict[int, str] = {}
    for name, value in request.cookies.items():
        if not name.startswith(PIN_COOKIE_PREFIX):
            continue
        try:
            held[int(name[len(PIN_COOKIE_PREFIX):])] = value
        except ValueError:
            # Cookie с нашей приставкой и не-числом после неё поставили не мы.
            continue
    return held


def _is_staff(request: Request, db: Session) -> bool:
    """Сотрудник, вошедший в CRM, видит файлы всех досок — как и сами доски.

    Право на доски здесь не спрашивается нарочно: файл работы виден в списке
    досок, в поиске, на дашборде и в палитре, и все эти экраны уже закрыты
    правами. Требовать право повторно на каждой картинке значило бы получить
    экран с рамками вместо изображений там, где сам экран открыт.
    """
    from core.services import auth_service
    from web.api.deps import SESSION_COOKIE

    token = request.cookies.get(SESSION_COOKIE)
    return bool(token and auth_service.get_user_by_session(db, token) is not None)


@router.get("/media/product/{filename}")
def product_photo(filename: str, db: Session = Depends(get_db)):
    """Снимок товара для сайта магазина — адрес без ключа, потому что картинку
    тянет браузер посетителя, а ключ в `<img src>` роздан был бы всем.

    Стоит ВЫШЕ `/media/{work_uid}/{filename}`: иначе `product` читался бы как
    опознаватель работы. Три проверки те же, что у файла работы: блок склада
    выключен — 404, путь собирает сервис, неопубликованный товар — 404, а не
    «есть, но не покажем» (docs/ustroystvo/16-api-sayta.md §3).
    """
    otkaz = JSONResponse({"error": {"code": "not_found", "message": "Not found"}}, status_code=404)
    if not modules_service.is_enabled(db, "warehouse"):
        return otkaz
    path = site_service.photo_path(db, filename)
    if path is None:
        return otkaz
    return FileResponse(
        path,
        media_type="image/webp",
        headers={
            # `public`: за файлом нет проверки, это витрина магазина — открытые
            # данные; имя неизменяемо, повторная загрузка бессмысленна.
            "Cache-Control": "public, max-age=31536000, immutable",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/media/{work_uid}/{filename}")
def media_file(work_uid: str, filename: str, request: Request, db: Session = Depends(get_db)):
    # Блок досок выключили — публичные пути обязаны исчезнуть вместе с ним.
    # Правило проекта: выключенный блок исчезает ЦЕЛИКОМ, а не только из меню.
    # Иначе «выключил доски» означало бы лишь «убрал из интерфейса», а ранее
    # разосланные ссылки продолжали бы отдавать работы клиента всему интернету —
    # то есть ровно то, ради прекращения чего блок и выключают.
    if not modules_service.is_enabled(db, "boards"):
        return JSONResponse({"error": {"code": "not_found", "message": "Not found"}}, status_code=404)

    # Путь собирает и проверяет `media_service.public_file`: он сверяет
    # ИТОГОВЫЙ путь с каталогом работ, а не перебирает запрещённые кусочки
    # имени. Разбор — в докстроке той функции.
    path = media_service.public_file(work_uid, filename)
    if path is None or not path.is_file():
        return JSONResponse({"error": {"code": "not_found", "message": "Not found"}}, status_code=404)

    # Доступ к файлу — тот же, что к витрине; разбор правила в
    # `share_service.media_is_public`. Отказ выглядит как «нет такого»: сказать
    # «есть, но не покажем» значило бы подтвердить, что работа существует.
    staff = _is_staff(request, db)
    if not staff and not share_service.media_is_public(db, work_uid, _pins_held(request)):
        return JSONResponse({"error": {"code": "not_found", "message": "Not found"}}, status_code=404)

    return FileResponse(
        path,
        media_type=_MEDIA_TYPES.get(path.suffix, "application/octet-stream"),
        headers={
            # `private`, а не `public`: за файлом стоит проверка, и общий кэш
            # (прокси провайдера, кэш в офисе) не должен раздавать его тем, кто
            # эту проверку не проходил. Срок оставлен длинным — имя каталога
            # неизменяемо, и повторная загрузка той же картинки бессмысленна.
            "Cache-Control": "private, max-age=31536000, immutable",
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

    **Перебор номеров этим закрыт не до конца, и обещать обратное нельзя.**
    Раньше здесь стояло утверждение, что по ответу нельзя отличить «нет такого
    номера» от «есть, но не показываем». Отличить можно — ровно по коду ответа:
    существующий номер отдаёт страницу, несуществующий — 404. А номера сквозные
    («2026-000001» и дальше по порядку), потому что тот же номер напечатан на
    квитанции и зашит в QR; сделать его неугадываемым, не сменив бумагу на
    руках у людей, невозможно.

    Значит вопрос не в том, возможен ли перебор, а сколько он стоит. Без
    ограничения — нисколько: тысяча запросов подряд отдаёт, сколько у мастерской
    заказов, что приняли по каждому и в каком он состоянии. Ограничитель по
    адресу поднимает эту цену с полуминуты до часов и оставляет живому человеку
    запас в двадцать обращений за десять минут (`web/api/deps.py`).

    Полностью закрыть можно только ключом в самой ссылке — и это осознанно НЕ
    сделано: ключ обесценит все уже напечатанные квитанции, а решать, менять ли
    бумагу на руках у клиентов, не программисту.
    """
    from core.services import document_service
    from web.public.document_strings import strings_for

    # Блок бланков выключили — снаружи это должно выглядеть так же, как
    # несуществующий номер. Иначе «выключено» означало бы лишь «не видно в
    # меню», а старые QR-коды продолжали бы открывать данные заказов.
    if not modules_service.is_enabled(db, "documents"):
        return _closed_page(request, db)

    # Считаем ВСЕ обращения, а не одни промахи. Перебор с первого номера идёт по
    # существующим заказам, то есть по удачным ответам: ограничитель, считающий
    # промахи, не сработал бы ни разу за всю выгрузку.
    #
    # Порядок «сначала спросить, потом отметить» даёт ровно `max_attempts`
    # пропущенных обращений; отметка перед проверкой отняла бы у человека одно
    # из них ни за что. Живёт этот порядок ВНУТРИ одной операции
    # (`proverit_i_zanyat`): двумя вызовами между ними оставалось окно, и пачка
    # одновременных обращений проходила порог целиком — а перебирают номера
    # бланков именно пачками, по одному это никому не нужно.
    visitor = client_ip(request)
    # Общего счётчика нет — отвечаем «занято, попробуйте позже», а не пускаем.
    # Довод целиком — в шапке `core/ratelimit.py`: не имея счётчика,
    # ограничитель не отличит перебор номеров бланков от первого обращения, и
    # «пропустить всех» означает выгрузить чужие заказы, пока Redis лежит.
    # Здесь это страница, а не JSON, поэтому ошибка ловится на месте: человеку
    # с телефоном у квитанции нужен понятный текст, а не тело ответа.
    try:
        if document_limiter.proverit_i_zanyat(visitor):
            bezopasnost.otmetit("blank_zapert")
            return _closed_page(request, db, status_code=429, busy=True)
    except errors.LimiterUnavailableError:
        return _closed_page(request, db, status_code=503, busy=True)

    try:
        doc = document_service.by_number(db, number)
    except errors.NotFoundError:
        # Промах по номеру бланка — самый говорящий из всех счётчиков.
        #
        # Номера сквозные («2026-000001» и дальше по порядку), и человек с
        # квитанцией в руках промахивается разве что опечаткой — то есть почти
        # никогда. Зато перебор состоит из промахов почти целиком: подбирающий
        # идёт по числам подряд и попадает в чужие заказы лишь изредка. Поэтому
        # ровный ручеёк на этом графике означает не «люди путаются», а «кто-то
        # считает от единицы».
        bezopasnost.otmetit("blank_promah")
        return _closed_page(request, db)

    # Снаружи показываются только бумаги клиента — квитанция и акт. Заказ и
    # накладная живут в той же таблице с той же нумерацией, и перебор номеров
    # открывал бы их с чужими словами про «готово к выдаче».
    if doc.kind not in (KIND_INTAKE, KIND_ACT):
        bezopasnost.otmetit("blank_promah")
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
