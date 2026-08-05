import mimetypes
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from config.settings import generate_secret_hint, get_settings
from core.exceptions import DomainError
from core.services import auth_service, maintenance_mode, pipeline_service, settings_service
from core.utils import normalize_email
from database import models  # noqa: F401 — регистрирует модели в metadata
from database.models.user import ROLE_ROOT
from database.repositories import users as users_repo
from database.session import Base, SessionLocal, engine
from web.api.deps import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE
from web.api.routes import (
    arcade,
    auth,
    boards,
    companies,
    deals,
    documents,
    mail,
    modules,
    people,
    pipeline,
    clients,
    dashboard,
    reports,
    search,
    shares,
    site_settings,
    staff,
    system,
    tasks,
    workspace,
)
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()

    errors = settings.config_errors()
    if errors:
        raise RuntimeError(
            "Небезопасная конфигурация, запуск остановлен:\n"
            + "\n".join(f"  - {message}" for message in errors)
            + f"\n\nСгенерировать значение: {generate_secret_hint()}"
        )
    for message in settings.config_warnings():
        print(f"[opencrm] ВНИМАНИЕ: {message}")

    for directory in (
        settings.media_dir,
        settings.client_files_dir,
        settings.branding_dir,
        settings.avatars_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(engine)
    db = SessionLocal()
    try:
        created = auth_service.bootstrap_root(db)
        settings_service.seed_defaults(db)
        # Пустая воронка означает пустую доску сделок на первом же экране —
        # причину закрыть вкладку. Кладём универсальный набор, менять его на
        # отраслевой можно одним нажатием в настройках.
        pipeline_service.seed_defaults(db)
        users_repo.purge_expired_sessions(db)
        db.commit()
        if created is not None:
            print(
                f"[opencrm] создан root-аккаунт: {created.email} "
                "(при первом входе потребуется смена пароля)"
            )
        else:
            # root создаётся один раз; менять OPENCRM_ROOT_EMAIL после этого бесполезно —
            # без явного сообщения вход под «новым» email выглядит как загадочный 401
            existing = users_repo.get_root(db)
            if existing and existing.email != normalize_email(settings.root_email):
                print(
                    f"[opencrm] ВНИМАНИЕ: root уже существует как {existing.email}, "
                    f"а OPENCRM_ROOT_EMAIL={settings.root_email}. Вход выполняется под "
                    f"{existing.email}. Сменить логин/пароль: python scripts/reset_root.py --help"
                )
    finally:
        db.close()
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="OpenCRM",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/api/docs" if settings.env != "production" else None,
        openapi_url="/api/openapi.json" if settings.env != "production" else None,
    )

    @app.exception_handler(DomainError)
    async def domain_error_handler(_request: Request, exc: DomainError):
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(_request: Request, exc: RequestValidationError):
        """Ответ о неверном запросе не должен содержать сам запрос.

        FastAPI по умолчанию кладёт в ошибку поле `input` — присланное тело
        целиком. На форме «укажите телефон» это безобидно, а на входе в систему
        означает вот что: послали `{"password": "..."}` без адреса — и пароль
        вернулся клиенту в теле ответа 422. Оттуда он попадает в журнал
        обратного прокси, в отчёт об ошибке на фронтенде и в консоль браузера,
        то есть переживает и сам запрос, и того, кто его послал. То же с
        паролём от почтового ящика.

        Клиенту нужно знать, ЧТО не так и ГДЕ, а не что именно он прислал —
        это он и так знает. Поэтому оставляем место ошибки и её вид, а `input`
        и `ctx` (туда Pydantic тоже кладёт значения) отбрасываем целиком.
        Отбрасываем списком «что оставить», а не «что убрать»: при обновлении
        Pydantic новое поле с данными не просочится само.
        """
        details = [
            {
                "loc": [str(part) for part in error.get("loc", ())],
                "type": error.get("type", ""),
                "msg": error.get("msg", ""),
            }
            for error in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_error",
                    "message": "Request body is invalid",
                    "details": details,
                }
            },
        )

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

    api_prefix = "/api/v1"
    app.include_router(dashboard.router, prefix=api_prefix)
    app.include_router(search.router, prefix=api_prefix)
    app.include_router(auth.router, prefix=api_prefix)
    app.include_router(staff.router, prefix=api_prefix)
    app.include_router(clients.router, prefix=api_prefix)
    app.include_router(boards.router, prefix=api_prefix)
    app.include_router(deals.router, prefix=api_prefix)
    app.include_router(companies.router, prefix=api_prefix)
    app.include_router(pipeline.router, prefix=api_prefix)
    app.include_router(people.router, prefix=api_prefix)
    app.include_router(documents.router, prefix=api_prefix)
    app.include_router(reports.router, prefix=api_prefix)
    app.include_router(mail.router, prefix=api_prefix)
    app.include_router(shares.router, prefix=api_prefix)
    app.include_router(modules.router, prefix=api_prefix)
    app.include_router(site_settings.router, prefix=api_prefix)
    app.include_router(tasks.router, prefix=api_prefix)
    app.include_router(system.router, prefix=api_prefix)
    app.include_router(workspace.router, prefix=api_prefix)
    app.include_router(arcade.router, prefix=api_prefix)
    app.include_router(public_routes.router)

    # шрифты витрины (Montserrat, SIL OFL) — раздаём со своего домена, а не с CDN:
    # внешний запрос светил бы IP посетителя стороннему сервису и требовал бы правки CSP
    #
    # .woff2 зарегистрирован в mimetypes не на каждой системе (на Windows — нет),
    # и без этой строки шрифт уезжает как application/octet-stream.
    mimetypes.add_type("font/woff2", ".woff2")
    mimetypes.add_type("font/woff", ".woff")
    static_dir = Path(__file__).parent / "public" / "static"
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=static_dir), name="showcase-static")

    # HEAD — мониторинг и прокси часто проверяют доступность именно им
    @app.api_route("/healthz", methods=["GET", "HEAD"])
    def healthz():
        db = SessionLocal()
        try:
            db.execute(text("SELECT 1"))
        finally:
            db.close()
        return {"status": "ok"}

    # CRM SPA: собранный фронтенд (web/frontend/crm/dist).
    # Catch-all регистрируется последним — все API/публичные роуты имеют приоритет.
    spa_dist = Path(__file__).parent / "frontend" / "crm" / "dist"
    if spa_dist.is_dir():
        app.mount("/assets", StaticFiles(directory=spa_dist / "assets"), name="spa-assets")

        @app.api_route("/{full_path:path}", methods=["GET", "HEAD"], include_in_schema=False)
        def spa(full_path: str):
            # не перехватываем API и публичные пути — для них честный 404
            if full_path.startswith(
                ("api/", "b/", "media/", "branding/", "avatars/", "assets/", "static/")
            ):
                return JSONResponse(
                    {"error": {"code": "not_found", "message": "Not found"}}, status_code=404
                )
            return FileResponse(spa_dist / "index.html")

    return app


app = create_app()
