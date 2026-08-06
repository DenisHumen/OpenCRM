"""Посредники: режим обслуживания и заголовки безопасности.

Оба висят на каждом запросе и оба — про правила, а не про данные: кого пускать
при закрытом сайте и какие заголовки обязан нести ответ. Раньше они лежали
внутри `create_app` вместе с регистрацией двух десятков маршрутов, и найти в
той сотне строк, почему витрина не кэшируется, а бланк кэшируется, было
негде — приходилось читать всё подряд.

Правила заголовков (CSP, кэш) в одном месте ещё и потому, что ошибиться в них
легко, а заметить ошибку — нет: слишком строгая политика ломает витрину сразу,
слишком мягкая не ломает ничего и годами выглядит рабочей.

**Посредники здесь написаны на голом ASGI, а не через `@app.middleware("http")`,
и это не вкусовщина.** Тот удобный вид разворачивается в `BaseHTTPMiddleware`,
который запускает нижележащее приложение отдельной задачей и отдаёт ответ
потоком: **ответ уходит клиенту, пока задача ещё не закончилась**. А заканчивает
её `get_db` — фиксацией транзакции. То есть браузер получал «готово» раньше, чем
запись доезжала до базы, и следующий его запрос своей же записи не видел.

Найдено пробой на живом стенде, воспроизводится детерминированно:

    завели клиента → сразу читаем карточку:  404 в 6 случаях из 8
    перевели заявку по этапу → сразу читаем: прежний этап в 3 из 4
    вошли → первый же запрос:                401 «сеанс недействителен», 5 из 5

Пауза в 50 мс лечила всё. Человек видел другое: вошёл — выкинуло на форму входа,
создал клиента и открыл — «не найдено», передвинул заявку — отскочила назад.
Всё это выглядит как «программа глючит», и на быстрой сети случается постоянно.

Голый ASGI-посредник ничего не разветвляет: он вызывает нижележащее приложение
и ждёт, пока то полностью отработает. Порядок восстанавливается сам и для всех
запросов разом.
"""

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from core.services import auth_service, maintenance_mode, settings_service
from database.models.user import ROLE_ROOT
from database.session import SessionLocal
from web.api.deps import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE
from web.public import routes as public_routes


MUTATING_METHODS = {"POST", "PATCH", "PUT", "DELETE"}

#: Потолок на тело запроса в JSON — один мегабайт.
#:
#: Загрузку файлов он не трогает: та идёт multipart и меряется своей меркой
#: (`max_upload_mb`, по умолчанию 200). Здесь речь про обычные запросы, у
#: которых самое большое тело — матрица прав или настройки сайта, то есть
#: десятки килобайт. Мегабайт для них с запасом.
#:
#: Зачем потолок вообще: nginx пропускает 220 МБ (иначе не загрузить файл), и
#: до этой правки столько же принимала **форма запроса доступа** — открытая в
#: интернет и не требующая входа. Замер на стенде: одно тело в 38 МБ стоило
#: процессу 76 МБ памяти (сырое тело, разобранная строка, объекты pydantic), и
#: Python возвращает её операционной системе неохотно. Десяток таких запросов
#: подряд — и на VPS с гигабайтом приложение убивает ядро. Ни одной ошибки в
#: логе при этом нет: процесс просто исчезает.
MAX_JSON_BODY = 1024 * 1024

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


class MaintenanceMode:
    """Сайт закрыт на работы — кого пускать и что показывать остальным."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)
        path = request.url.path
        if path in MAINTENANCE_OPEN_PATHS or path.startswith(MAINTENANCE_OPEN_PREFIXES):
            await self.app(scope, receive, send)
            return

        db = SessionLocal()
        try:
            mode = maintenance_mode.state(db)
            if not mode["enabled"]:
                await self.app(scope, receive, send)
                return
            # Режим включён: root проходит в CRM как обычно, остальные — на
            # заглушку. На клиентские страницы не проходит никто.
            if not path.startswith(MAINTENANCE_PUBLIC_PREFIXES):
                token = request.cookies.get(SESSION_COOKIE)
                user = auth_service.get_user_by_session(db, token) if token else None
                if user is not None and user.role == ROLE_ROOT:
                    await self.app(scope, receive, send)
                    return
            note = mode["note"]
            locale = settings_service.get_all(db).get("showcase_locale", "en")
        finally:
            db.close()

        if path.startswith("/api/"):
            response: Response = JSONResponse(
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
        else:
            response = _maintenance_page(note, locale)
        await response(scope, receive, send)


class SecurityHeaders:
    """Потолок тела, проверка CSRF и заголовки безопасности на каждом ответе."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)
        refusal = self._refuse(request)
        if refusal is not None:
            await refusal(scope, receive, send)
            return

        path = request.url.path

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                self._decorate(MutableHeaders(scope=message), path)
            await send(message)

        await self.app(scope, receive, send_with_headers)

    def _refuse(self, request: Request) -> Response | None:
        """Отказ до того, как запрос дошёл до приложения. None — пропускаем."""
        # Тело не по размеру отсекаем ДО чтения: разбирать 200 МБ, чтобы затем
        # ответить «слишком длинное имя», — уже проигранная память.
        if request.method in MUTATING_METHODS:
            declared = request.headers.get("content-length")
            content_type = request.headers.get("content-type", "")
            if declared and "json" in content_type and int(declared) > MAX_JSON_BODY:
                return JSONResponse(
                    status_code=413,
                    content={
                        "error": {
                            "code": "body_too_large",
                            "message": "Request body is too large",
                        }
                    },
                )

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
        return None

    def _decorate(self, headers: MutableHeaders, path: str) -> None:
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        if path.startswith("/static/"):
            # Шрифты витрины лежат под постоянными именами и не меняются: без
            # max-age браузер переспрашивал бы их на каждой загрузке страницы
            # (в логе — вереница 304 Not Modified вместо попадания в кэш).
            headers.setdefault("Cache-Control", "public, max-age=31536000, immutable")
            headers.setdefault("Content-Security-Policy", CSP_MEDIA)
        elif path.startswith("/assets/"):
            # Сборка SPA: имя файла содержит хэш содержимого, поэтому изменённый
            # файл приезжает под новым именем, а старое имя навсегда означает
            # старое содержимое. Такое кэшируется бессрочно и без переспроса —
            # ровно то, ради чего хэш в имени и заведён.
            headers.setdefault("Cache-Control", "public, max-age=31536000, immutable")
        elif path.startswith(("/media/", "/branding/", "/avatars/")):
            headers.setdefault("Content-Security-Policy", CSP_MEDIA)
        elif path.startswith("/b/") or path.startswith("/d/") or path.endswith("/print"):
            # Бланк и страница состояния — серверный HTML со встроенными
            # стилями, как витрина: строгая политика приложения их ломает.
            headers.setdefault("Content-Security-Policy", CSP_SHOWCASE)
            # Кэшировать эти страницы нельзя. Ссылку на витрину отзывают, срок
            # действия истекает, сайт закрывают на работы — и во всех трёх
            # случаях страница обязана перестать открываться. Без заголовка
            # браузер вправе показать её из кэша, а по кнопке «назад» покажет
            # наверняка: отозванная ссылка продолжала бы работать у того, кто
            # успел её открыть.
            headers.setdefault("Cache-Control", "no-store, must-revalidate")
        else:
            headers.setdefault("Content-Security-Policy", CSP_APP)
            headers.setdefault("X-Frame-Options", "DENY")


def register(app: FastAPI) -> None:
    """Навесить посредников. Заголовки безопасности — снаружи всех.

    `add_middleware` кладёт нового посредника поверх прежних, поэтому режим
    обслуживания добавляется первым, а заголовки вторым: так они достаются и
    ответу заглушки, а не только обычным страницам.
    """
    app.add_middleware(MaintenanceMode)
    app.add_middleware(SecurityHeaders)
