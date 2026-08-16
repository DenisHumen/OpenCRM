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
from core import ratelimit, redis_client
from core.exceptions import DomainError
# Подписки на события грузятся при первом же событии сами, но импорт здесь
# оставлен нарочно: опечатка в обработчике должна ронять запуск приложения, а
# не всплывать через неделю при первом переводе заявки по воронке.
from core import subscriptions  # noqa: F401
from core.services import (
    auth_service,
    maintenance_mode,
    permissions_service,
    pipeline_service,
    settings_service,
    warehouse_service,
)
from core.utils import normalize_email
from database import models  # noqa: F401 — регистрирует модели в metadata
from database import schema_check
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
    finance,
    labels,
    leads,
    mail,
    metrics,
    modules,
    orders,
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
    templates,
    warehouse,
    telegram,
    telephony,
    workspace,
)
from web import middleware
from web.public import leads as public_leads
from web.public import routes as public_routes

def _ensure_schema() -> None:
    """Схема на месте: пустую базу строим, населённую — проводим миграциями.

    **Различать эти два случая обязательно, и раньше это не делалось.** Прежде
    здесь стоял безусловный `create_all`, и на населённой базе он вёл себя тихо и
    неправильно: новые ТАБЛИЦЫ создавал (не отмечаясь при этом в
    `alembic_version`), а новых КОЛОНОК не добавлял вовсе. Получалось расхождение
    двух видов сразу — база «на старой миграции», но с новой таблицей, отчего
    следующее `alembic upgrade` падало с «table already exists»; и база без новой
    колонки, отчего падал первый же запрос к ней. Оба случая ловились не тестом,
    а живым отказом, и второй — с потерей рабочего времени.

    Теперь путь один и тот же, что на сервере: **миграции**. `create_all`
    остаётся ровно для пустой базы — первый запуск без контейнера, — и сразу
    отмечается `stamp head`, чтобы дальше она шла миграциями, как все.

    В контейнере всё это уже сделал entrypoint (`alembic upgrade head` до
    старта); здесь остаётся быстрая проверка «нечего делать».

    Терпит соседа. При `OPENCRM_WORKERS>1` процессы поднимаются разом, оба видят
    пустую базу, и проигравший падает на создании таблиц. Молча глотать ошибку
    нельзя — так же выглядела бы настоящая поломка, — поэтому после отказа
    смотрим, на месте ли таблицы: есть, значит сосед успел первым, и это тот
    итог, который нужен.
    """
    from alembic import command
    from alembic.config import Config

    config = Config(str(Path(__file__).parent.parent / "alembic.ini"))
    # Alembic обязан работать с той же базой, что и приложение. Без явного
    # указания он взял бы адрес из настроек — и на проверках, где движок
    # подменён, отметил бы чужую базу, а расхождение осталось бы неисправленным.
    config.set_main_option("sqlalchemy.url", engine.url.render_as_string(hide_password=False))

    if schema_check.is_empty(engine):
        try:
            Base.metadata.create_all(engine)
            command.stamp(config, "head")
        except (OperationalError, ProgrammingError):
            if not inspect(engine).has_table(User.__tablename__):
                raise
        return

    # База населена, но не отмечена ни одной миграцией — так выглядит база,
    # построенная прежним безусловным `create_all`. Гнать по ней миграции с
    # нуля нельзя: первая же упрётся в «table site_settings already exists», и
    # приложение не поднимется вовсе.
    #
    # Если схема при этом сходится с моделями, база фактически НА ГОЛОВЕ —
    # просто об этом никто не записал. Отмечаем и идём дальше; со следующего
    # обновления она пойдёт общим путём. Если не сходится — чинить вслепую
    # нечего: неизвестно, каких шагов не хватает, и молчаливая догадка здесь
    # опаснее честной остановки.
    if schema_check.current_revision(engine) is None:
        report = schema_check.check(engine)
        if not report.ok:
            raise RuntimeError(
                "База построена мимо миграций и не совпадает с моделями:\n"
                f"  {report.summary()}\n\n"
                "Починить вслепую нельзя — неизвестно, каких шагов не хватает.\n"
                "Обычный путь: восстановить копию и обновиться заново."
            )
        print("[opencrm] база собрана мимо миграций, но сходится — отмечаю как последнюю")
        command.stamp(config, "head")
        return

    # Дальше — только миграции. `create_all` здесь запрещён: он не умеет
    # добавлять колонки и потому оставляет расхождение вместо того, чтобы его
    # закрыть.
    command.upgrade(config, "head")


def _assert_schema_matches():
    """Схема сходится с моделями — или приложение не поднимается.

    Отказ подняться выглядит сурово и является самым мягким из возможных
    исходов. Обновление на сервере устроено так: контейнер пересобирается,
    ждётся `/healthz`, и **не дождавшись — откатывается вместе с базой**
    (deploy/updater.py). Значит несхождение схемы, замеченное здесь, стоит
    минуты простоя и возврата на прошлую версию — а незамеченное стоит рабочего
    дня, в течение которого раздел отвечает 500, и никто не знает почему.

    Ругаемся только на нехватку. Лишняя таблица или колонка (след отката) ничему
    не мешает: запретить откат значит отобрать главный способ починки.
    """
    report = schema_check.check(engine)
    if report.ok:
        if report.extra_tables or report.extra_columns:
            print(f"[opencrm] {report.summary()}")
        return report
    raise RuntimeError(
        "База не соответствует моделям, запуск остановлен:\n"
        f"  {report.summary()}\n\n"
        "Обычная причина — миграции не доехали. Проверить и починить:\n"
        "  docker compose exec app python -m alembic current\n"
        "  docker compose exec app python -m alembic upgrade head"
    )


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
    # Сверку делаем ПОСЛЕ миграций и ДО посева: сеять в базу, которой не
    # хватает колонок, значит получить непонятный отказ вместо понятного.
    # Отчёт держим в состоянии приложения, а не пересчитываем на каждый запрос:
    # схема не меняется, пока процесс живёт (миграции проходят до старта), а
    # `/healthz` опрашивает докер каждые несколько секунд.
    app.state.schema_report = _assert_schema_matches()
    db = SessionLocal()
    # Посев умолчаний идёт под общим замком.
    #
    # При нескольких рабочих процессах они поднимаются разом и сеют разом.
    # Четыре посева из пяти соседа терпят: они опираются на уникальный индекс
    # (`core/uniqueness.insert_or_ignore` и родня). Пятый — склад «Основной» —
    # опереться не на что: уникального индекса на названии склада нет, и
    # `count_alive() > 0` отвечает «ноль» обоим процессам сразу. В базе
    # появляются два склада «Основной», после чего инвариант «ровно один
    # основной» выполняется наперегонки. Замок стоит вокруг всего блока, а не
    # вокруг одного склада: остальные четыре тоже перестают крутить откаты.
    try:
        with redis_client.startup_lock("seed"):
            created = auth_service.bootstrap_root(db)
            settings_service.seed_defaults(db)
            # Система без единой роли не может принять сотрудника: он вошёл бы в
            # CRM без единого раздела. Кладём должность по умолчанию — как воронку.
            permissions_service.seed_defaults(db)
            # Пустая воронка означает пустую доску сделок на первом же экране —
            # причину закрыть вкладку. Кладём универсальный набор, менять его на
            # отраслевой можно одним нажатием в настройках.
            pipeline_service.seed_defaults(db)
            # Склад без единого места не примет ни одного прихода. На боевом
            # сервере его кладёт миграция; здесь страховка для базы, поднятой
            # `create_all`, и для той, где склады почистили руками.
            warehouse_service.seed_defaults(db)
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
        body = {"code": exc.code, "message": exc.message}
        # `details` появляются только там, где отказ требует ВЫБОРА: «клиент
        # нашёлся не один» без списка кандидатов — тупик. Ключ не добавляем
        # пустым, чтобы форма ответа не менялась у остальных отказов.
        if exc.details:
            body["details"] = exc.details
        return JSONResponse(status_code=exc.http_status, content={"error": body})

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
    app.include_router(finance.router, prefix=api_prefix)
    app.include_router(reports.router, prefix=api_prefix)
    app.include_router(mail.router, prefix=api_prefix)
    app.include_router(labels.router, prefix=api_prefix)
    app.include_router(leads.router, prefix=api_prefix)
    app.include_router(shares.router, prefix=api_prefix)
    app.include_router(modules.router, prefix=api_prefix)
    app.include_router(audit.router, prefix=api_prefix)
    app.include_router(site_settings.router, prefix=api_prefix)
    app.include_router(tasks.router, prefix=api_prefix)
    app.include_router(templates.router, prefix=api_prefix)
    app.include_router(orders.router, prefix=api_prefix)
    app.include_router(warehouse.router, prefix=api_prefix)
    app.include_router(warehouse.places_router, prefix=api_prefix)
    app.include_router(metrics.router, prefix=api_prefix)
    app.include_router(system.router, prefix=api_prefix)
    app.include_router(workspace.router, prefix=api_prefix)
    app.include_router(telegram.router, prefix=api_prefix)
    app.include_router(telephony.router, prefix=api_prefix)
    # Вебхук АТС — отдельным роутером: он единственный не закрыт блоком
    # телефонии (обоснование — в web/api/routes/telephony.py).
    app.include_router(telephony.webhook_router, prefix=api_prefix)
    app.include_router(arcade.router, prefix=api_prefix)
    app.include_router(public_routes.router)
    # Приём заявок с сайта: роутер несёт полный префикс сам
    # (/api/v1/public/leads) — разбор в web/public/leads.py.
    app.include_router(public_leads.router)

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
        """Живо ли приложение. Отвечает 200 — значит и схема в порядке.

        Схему здесь не пересчитываем и подробностей не раскрываем: несхождение
        не даёт приложению подняться вовсе (`_assert_schema_matches`), поэтому
        сам факт ответа 200 уже означает, что база сошлась с моделями. Именно на
        это опирается откат при обновлении: не дождавшись 200, обновлятор
        возвращает прошлую версию вместе с базой.
        """
        db = SessionLocal()
        try:
            db.execute(text("SELECT 1"))
            # Закрытый сайт не имеет права выглядеть здоровым.
            #
            # Режим обслуживания включает не только человек: его включает и
            # снимает переезд базы (`migrate_maintenance` в opencrm.sh). Убитый
            # посреди переноса переезд оставляет сайт закрытым НАВСЕГДА, и
            # проверено делом, что этого не видит НИКТО: `/healthz` отвечал
            # `status ok, schema ok, redis ok`, докер, автообновление и
            # мониторинг молчали. Люди при этом видели разное: обычный
            # сотрудник — 503 даже на чтение списка клиентов, публичная главная
            # — 503, а владелец проходит по устройству режима и ВИДИТ РАБОЧИЙ
            # САЙТ. То есть тот единственный, кто может починить, беды не видел.
            #
            # Кода ответа поле не меняет — по той же причине, что и `redis`
            # ниже: на 200 завязаны проверка здоровья docker и откат
            # обновления, а они умеют одно — заменить контейнер приложения.
            # Закрытый сайт этим не чинится (режим лежит в базе и переживёт
            # замену), зато цикл перезапусков поверх обслуживания — это
            # настоящая авария вместо объявленной.
            maintenance = "on" if maintenance_mode.state(db)["enabled"] else "off"
        finally:
            db.close()
        report = getattr(app.state, "schema_report", None)
        # Состояние Redis показываем строкой, но код ответа им НЕ меняем, и это
        # решение, а не недосмотр. На `/healthz` завязаны две вещи, которые
        # умеют только одно — заменить контейнер приложения: проверка здоровья
        # docker и откат обновления (`deploy/updater.py`). Ни то, ни другое не
        # чинит лежащий Redis, зато откат исправного обновления из-за соседней
        # службы — это лишний простой на ровном месте. Приложение здесь и правда
        # здорово; сломано другое, и говорят об этом метрика
        # `opencrm_ratelimiter_unavailable_total`, тревога и строка ниже.
        #
        # И САМ REDIS ЗДЕСЬ НЕ ОПРАШИВАЕТСЯ. Проверено живьём: с остановленным
        # контейнером разрешение имени `redis` не укладывается в пятисекундный
        # потолок проверки здоровья, `/healthz` отваливается по таймауту — и
        # docker объявляет нездоровым ПРИЛОЖЕНИЕ. Получилась бы ровно та связка,
        # которой этот абзац и избегает: авария соседней службы роняет сайт.
        # Поэтому ответ берётся по факту работы ограничителя (были ли отказы за
        # последнюю минуту), а живой опрос стоит в `/api/v1/metrics`, где
        # медленный ответ стоит одного пропущенного среза, а не перезапуска.
        if not redis_client.configured():
            redis_state = "off"
        else:
            redis_state = "down" if ratelimit.recently_unavailable() else "ok"
        return {
            "status": "ok",
            "schema": "ok" if report is None or report.ok else "mismatch",
            "redis": redis_state,
            "maintenance": maintenance,
        }

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
