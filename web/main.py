import mimetypes
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import inspect, text
from sqlalchemy.exc import OperationalError, ProgrammingError

from config.settings import generate_secret_hint, get_settings
from core.exceptions import DomainError
# Подписки на события грузятся при первом же событии сами, но импорт здесь
# оставлен нарочно: опечатка в обработчике должна ронять запуск приложения, а
# не всплывать через неделю при первом переводе заявки по воронке.
from core import subscriptions  # noqa: F401
from core.services import (
    auth_service,
    permissions_service,
    pipeline_service,
    settings_service,
)
from core.utils import normalize_email
from database import models  # noqa: F401 — регистрирует модели в metadata
from database.models import User
from database.repositories import users as users_repo
from database.session import Base, SessionLocal, engine
from web.api.routes import (
    arcade,
    audit,
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
    roles,
    search,
    shares,
    site_settings,
    staff,
    system,
    tasks,
    warehouse,
    telephony,
    workspace,
)
from web import middleware
from web.public import routes as public_routes

def _ensure_schema() -> None:
    """Схема на месте — создать недостающее, если её строит не alembic.

    В контейнере схему делает `alembic upgrade head` из entrypoint, и здесь
    остаётся проверкой на пустом месте. Нужна она для запуска без контейнера
    (`uvicorn web.main:app`) — иначе первый же такой запуск встречает пустую базу.

    Терпит соседа. `create_all` смотрит, каких таблиц нет, и создаёт их — то есть
    внутри у него та же «проверил и сделал», что и у посева. При
    `OPENCRM_WORKERS=2` два процесса поднимаются разом, оба видят пустую базу,
    и проигравший падает с «table users already exists», не поднявшись вовсе.

    Молча глотать ошибку нельзя: так же выглядела бы и настоящая поломка схемы.
    Поэтому после отказа смотрим, на месте ли таблицы: есть — сосед успел
    раньше, и это ровно тот итог, который нам нужен; нет — беда наша, и она
    должна быть видна.
    """
    try:
        Base.metadata.create_all(engine)
    except (OperationalError, ProgrammingError):
        if not inspect(engine).has_table(User.__tablename__):
            raise


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
    _ensure_schema()
    db = SessionLocal()
    try:
        created = auth_service.bootstrap_root(db)
        settings_service.seed_defaults(db)
        # Система без единой роли не может принять сотрудника: он вошёл бы в
        # CRM без единого раздела. Кладём должность по умолчанию — как воронку.
        permissions_service.seed_defaults(db)
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

    middleware.register(app)

    api_prefix = "/api/v1"
    app.include_router(dashboard.router, prefix=api_prefix)
    app.include_router(search.router, prefix=api_prefix)
    app.include_router(auth.router, prefix=api_prefix)
    app.include_router(staff.router, prefix=api_prefix)
    app.include_router(roles.router, prefix=api_prefix)
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
    app.include_router(audit.router, prefix=api_prefix)
    app.include_router(site_settings.router, prefix=api_prefix)
    app.include_router(tasks.router, prefix=api_prefix)
    app.include_router(warehouse.router, prefix=api_prefix)
    app.include_router(system.router, prefix=api_prefix)
    app.include_router(workspace.router, prefix=api_prefix)
    app.include_router(telephony.router, prefix=api_prefix)
    # Вебхук АТС — отдельным роутером: он единственный не закрыт блоком
    # телефонии (обоснование — в web/api/routes/telephony.py).
    app.include_router(telephony.webhook_router, prefix=api_prefix)
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
            # `no-cache` — это «спроси перед тем, как показать», а не «не храни»:
            # с ETag переспрос стоит один 304 и ничего не весит.
            #
            # Без заголовка браузер вправе кэшировать страницу по своему
            # усмотрению (эвристика от Last-Modified) и делает это. А index.html
            # — единственный файл сборки без хэша в имени, и внутри него лежат
            # имена тех, у кого хэш есть. Обновление кладёт новую сборку и
            # уносит старые файлы: /assets/index-СТАРЫЙ.js после него отвечает
            # 404. Браузер со вчерашним index.html просит именно его и получает
            # пустой экран вместо CRM — до тех пор, пока человек не догадается
            # обновить страницу с очисткой кэша. Поймано на стенде: после
            # пересборки открытая вкладка продолжала грузить снесённый бандл.
            return FileResponse(
                spa_dist / "index.html", headers={"Cache-Control": "no-cache"}
            )

    return app


app = create_app()
