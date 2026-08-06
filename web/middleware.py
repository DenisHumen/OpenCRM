"""Посредники: режим обслуживания и заголовки безопасности.

Оба висят на каждом запросе и оба — про правила, а не про данные: кого пускать
при закрытом сайте и какие заголовки обязан нести ответ. Раньше они лежали
внутри `create_app` вместе с регистрацией двух десятков маршрутов, и найти в
той сотне строк, почему витрина не кэшируется, а бланк кэшируется, было
негде — приходилось читать всё подряд.

Правила заголовков (CSP, кэш) в одном месте ещё и потому, что ошибиться в них
легко, а заметить ошибку — нет: слишком строгая политика ломает витрину сразу,
слишком мягкая не ломает ничего и годами выглядит рабочей.
"""

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

from core.services import auth_service, maintenance_mode, settings_service
from database.models.user import ROLE_ROOT
from database.session import SessionLocal
from web.api.deps import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE
from web.public import routes as public_routes


MUTATING_METHODS = {"POST", "PATCH", "PUT", "DELETE"}

# Витрина рендерится сервером и намеренно содержит инлайновые <style>/<script>
# (лайтбокс, декодер blurhash) — им нужен 'unsafe-inline'. Пользовательские данные
# в шаблонах экранируются Jinja2, внешних скриптов нет. CRM (SPA) грузит скрипты
# только из /assets, поэтому там script-src строгий — 'self' без inline.
CSP_SHOWCASE = (
    "default-src 'self'; img-src 'self' data:; media-src 'self'; "
    "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
    "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
)
CSP_APP = (
    "default-src 'self'; img-src 'self' data: blob:; media-src 'self'; "
    "style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; "
    "font-src 'self' data:; object-src 'none'; base-uri 'none'; "
    "frame-ancestors 'none'; form-action 'self'"
)
# Медиа/брендинг — не документы; на случай прямого открытия SVG глушим любой скрипт
# (в дополнение к санитайзингу при загрузке).
CSP_MEDIA = "default-src 'none'; img-src 'self'; media-src 'self'; style-src 'unsafe-inline'; sandbox"


def register(app: FastAPI) -> None:
    """Навесить посредников на приложение. Порядок важен: FastAPI зовёт их
    в обратном порядке регистрации, поэтому заголовки безопасности
    навешиваются последними и достаются даже ответу заглушки."""
    # Пути, которые работают даже при закрытом на обслуживание сайте.
    # /healthz — иначе docker сочтёт контейнер больным и начнёт его дёргать.
    # /login и /assets — чтобы root мог войти: без них он упирается в ту же
    # заглушку, что и все, и снять режим уже неоткуда.
    # /api/v1/auth — сам вход и проверка сессии.
    MAINTENANCE_OPEN_PREFIXES = ("/healthz", "/assets/", "/api/v1/auth/")
    MAINTENANCE_OPEN_PATHS = {"/login"}

    # Страницы, которые видит клиент, а не сотрудник: витрина доски и состояние
    # заказа по QR. Сюда root не проходит даже со своей сессией.
    #
    # Пропуск для root существует ради одного: чтобы он мог войти в CRM и снять
    # режим. К клиентской стороне это не относится, а раньше относилось — и
    # получалось, что root проверяет свою же ссылку, видит работающую витрину и
    # заключает, что режим не работает. Он работал; просто проверяющий был
    # единственным, для кого сайт оставался открыт. Теперь «закрыто» на
    # клиентской стороне означает закрыто для всех, включая root.
    MAINTENANCE_PUBLIC_PREFIXES = ("/b/", "/d/")

    def _maintenance_page(note: str, locale: str) -> Response:
        strings = public_routes.MAINTENANCE_STRINGS.get(
            locale, public_routes.MAINTENANCE_STRINGS["en"]
        )
        html = public_routes.templates.get_template("maintenance_mode.html").render(
            note=note, locale=locale, t=strings
        )
        # 503, а не 200: для поисковика это «зайдите позже», а не «страница
        # стала такой». Retry-After — по той же причине, что у заглушки nginx.
        return HTMLResponse(
            html, status_code=503, headers={"Retry-After": "600", "Cache-Control": "no-store"}
        )

    @app.middleware("http")
    async def maintenance_middleware(request: Request, call_next):
        path = request.url.path
        if path in MAINTENANCE_OPEN_PATHS or path.startswith(MAINTENANCE_OPEN_PREFIXES):
            return await call_next(request)

        db = SessionLocal()
        try:
            mode = maintenance_mode.state(db)
            if not mode["enabled"]:
                return await call_next(request)
            # Режим включён: root проходит в CRM как обычно, остальные — на
            # заглушку. На клиентские страницы не проходит никто.
            if not path.startswith(MAINTENANCE_PUBLIC_PREFIXES):
                token = request.cookies.get(SESSION_COOKIE)
                user = auth_service.get_user_by_session(db, token) if token else None
                if user is not None and user.role == ROLE_ROOT:
                    return await call_next(request)
            note, locale = mode["note"], settings_service.get_all(db).get("showcase_locale", "en")
        finally:
            db.close()

        if path.startswith("/api/"):
            return JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "code": "maintenance_mode",
                        "message": "The site is closed for maintenance",
                        "note": note,
                    }
                },
                headers={"Retry-After": "600"},
            )
        return _maintenance_page(note, locale)

    @app.middleware("http")
    async def security_middleware(request: Request, call_next):
        # CSRF (double-submit cookie): только для cookie-аутентифицированных
        # изменяющих запросов к API
        if (
            request.url.path.startswith("/api/")
            and request.method in MUTATING_METHODS
            and request.cookies.get(SESSION_COOKIE)
        ):
            header = request.headers.get(CSRF_HEADER)
            cookie = request.cookies.get(CSRF_COOKIE)
            if not header or not cookie or header != cookie:
                return JSONResponse(
                    status_code=403,
                    content={"error": {"code": "csrf_failed", "message": "CSRF check failed"}},
                )
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        path = request.url.path
        if path.startswith("/static/"):
            # Шрифты витрины лежат под постоянными именами и не меняются: без
            # max-age браузер переспрашивал бы их на каждой загрузке страницы
            # (в логе — вереница 304 Not Modified вместо попадания в кэш).
            response.headers.setdefault(
                "Cache-Control", "public, max-age=31536000, immutable"
            )
            response.headers.setdefault("Content-Security-Policy", CSP_MEDIA)
        elif path.startswith("/assets/"):
            # Сборка SPA: имя файла содержит хэш содержимого, поэтому изменённый
            # файл приезжает под новым именем, а старое имя навсегда означает
            # старое содержимое. Такое кэшируется бессрочно и без переспроса —
            # ровно то, ради чего хэш в имени и заведён.
            response.headers.setdefault(
                "Cache-Control", "public, max-age=31536000, immutable"
            )
        elif path.startswith(("/media/", "/branding/", "/avatars/")):
            response.headers.setdefault("Content-Security-Policy", CSP_MEDIA)
        elif path.startswith("/b/") or path.startswith("/d/") or path.endswith("/print"):
            # Бланк и страница состояния — серверный HTML со встроенными
            # стилями, как витрина: строгая политика приложения их ломает.
            response.headers.setdefault("Content-Security-Policy", CSP_SHOWCASE)
            # Кэшировать эти страницы нельзя. Ссылку на витрину отзывают, срок
            # действия истекает, сайт закрывают на работы — и во всех трёх
            # случаях страница обязана перестать открываться. Без заголовка
            # браузер вправе показать её из кэша, а по кнопке «назад» покажет
            # наверняка: отозванная ссылка продолжала бы работать у того, кто
            # успел её открыть.
            response.headers.setdefault("Cache-Control", "no-store, must-revalidate")
        else:
            response.headers.setdefault("Content-Security-Policy", CSP_APP)
            response.headers.setdefault("X-Frame-Options", "DENY")
        return response
