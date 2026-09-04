from fastapi import Depends, Request
from sqlalchemy.orm import Session

from config.settings import get_settings
from core import exceptions as errors
from core import modules as core_modules
from core import permissions as core_permissions
from core.live import collector as live_collector
from core.ratelimit import SlidingWindowLimiter
from core.services import auth_service, modules_service, permissions_service
from database.models import User
from database.session import SessionLocal, snyat_zamki

SESSION_COOKIE = "opencrm_session"
CSRF_COOKIE = "opencrm_csrf"
CSRF_HEADER = "x-csrf-token"

_settings = get_settings()
# Имя — это отсек в общем хранилище. Без него подбор PIN и перебор номеров
# бланков считались бы в один счётчик: ключ у обоих — хэш адреса посетителя, и
# совпадали бы они не иногда, а всегда.
login_limiter = SlidingWindowLimiter(
    _settings.login_max_attempts, _settings.login_lockout_minutes * 60, name="login"
)
pin_limiter = SlidingWindowLimiter(
    _settings.pin_max_attempts, _settings.pin_lockout_minutes * 60, name="pin"
)

#: Сколько раз с одного адреса можно спросить состояние заказа по номеру.
#:
#: Этот ограничитель отличается от двух соседних по существу: те считают
#: НЕУДАЧИ (не тот пароль, не тот PIN), а этот — все обращения подряд. Иначе он
#: не работал бы вовсе. Номера бланков сквозные, «2026-000001» и дальше по
#: порядку, и перебор с начала попадает не в промахи, а в существующие заказы:
#: считая одни промахи, мы пропустили бы ровно тот случай, ради которого всё
#: затевалось.
#:
#: Двадцать за десять минут выбраны по живому поведению: человек подносит
#: телефон к квитанции, открывает страницу и время от времени обновляет её.
#: Двадцать — с запасом даже для нескольких человек за одним адресом мобильного
#: оператора, и при этом полный перебор тысячи заказов растягивается с полуминуты
#: до восьми часов.
DOCUMENT_STATUS_MAX_LOOKUPS = 20
DOCUMENT_STATUS_WINDOW_SECONDS = 600
document_limiter = SlidingWindowLimiter(
    DOCUMENT_STATUS_MAX_LOOKUPS, DOCUMENT_STATUS_WINDOW_SECONDS, name="document"
)


#: Потолок длины строки поиска.
#:
#: Двести знаков — это уже не поиск, а вставленный по ошибке абзац: длиннее не
#: набирают, а `LIKE '%…%'` по такому всё равно ничего не найдёт. Потолок тут не
#: ради безопасности (запрос параметризован), а ради того, чтобы бессмысленная
#: работа не доходила до базы: без него в условие уезжали десять тысяч знаков.
#:
#: Столько же стоит у палитры Ctrl+K — она была единственной, кто себя ограничил.
MAX_SEARCH = 200


def _edinica_raboty():
    """Транзакция запроса: `commit` на выходе, `rollback` на исключении.

    Развилка та же, что была, а вот МОМЕНТ, в который она срабатывает, другой —
    и это единственное, что отличает работающий вход от «ввёл пароль и тут же
    выкинуло».

    FastAPI разбирает зависимости с `yield` в `AsyncExitStack`, и по умолчанию
    (`scope="request"`) этот стек закрывается ПОСЛЕ отправки ответа:

        async with AsyncExitStack() as request_stack:      # сюда попадал get_db
            async with AsyncExitStack() as function_stack:
                response = await f(request)
            await response(scope, receive, send)           # ответ ушёл клиенту
        # и только теперь commit

    `commit` в MySQL — сетевой круг, и следующий запрос браузера его обгоняет.
    Замерено (вход, сразу `GET /auth/me`, двадцать раз): 9 промахов из 20, а с
    паузой в секунду — 0 из 20.
    Тот же самый cookie, только что получивший 401 `session_invalid`, через
    секунду отвечает 200 пять раз из пяти: сессия была записана, просто позже
    ответа.

    Касается это не одного входа, а **всего, что пишет и сразу читается**:
    завели клиента и открыли карточку, перевели заявку и перечитали её.

    Отсюда `scope="function"` ниже: FastAPI кладёт такую зависимость в
    `function_stack`, который закрывается ДО `await response(...)`. Отказ при
    этом по-прежнему откатывает — исключение поднимается из обработчика через
    тот же стек, и ветка `except` срабатывает раньше, чем обработчик доменных
    ошибок построит ответ (сторож — `tests/test_transaction_boundary.py`).

    Побочно чинится ещё одно: упавший `commit` (нарушенная уникальность,
    оборванная сеть) раньше случался, когда клиенту уже отдали 200, и сказать
    об этом было некому. Теперь он превращается в честный отказ.
    """
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        # Именованные замки MySQL снимаются ЗДЕСЬ, и место выбрано, а не
        # случилось. Такой замок держится за соединением: ни `COMMIT`, ни
        # `ROLLBACK`, ни `Session.close()` его не снимают — последняя возвращает
        # соединение в пул, не закрывая. Снять раньше фиксации тоже нельзя:
        # соперник получил бы очередь до того, как чужая запись станет видимой,
        # то есть замок не сделал бы ничего. Между `commit` и `close` — ровно
        # то единственное место, где оба условия выполнены.
        snyat_zamki(db)
        db.close()


def get_db(db: Session = Depends(_edinica_raboty, scope="function")) -> Session:
    """Сессия БД для маршрутов. Спрашивается как обычно: `Depends(get_db)`.

    Обёртка нужна ровно затем, чтобы `scope="function"` был объявлен ОДИН раз.
    Он принадлежит месту, где зависимость спрашивают, а не самой функции, — то
    есть при `get_db` с `yield` его пришлось бы писать в каждом из двух сотен
    `Depends(get_db)`. Забытый в новом маршруте, он вернул бы «вошёл и тут же
    выкинуло» молча и только на этом маршруте; искать такое пришлось бы руками.
    """
    return db


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    """Аутентифицированный активный сотрудник (смена пароля может требоваться)."""
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise errors.AuthError("Not authenticated", code="not_authenticated")
    user = auth_service.get_user_by_session(db, token)
    if user is None:
        raise errors.AuthError("Session is invalid or expired", code="session_invalid")
    # Кто изменил — для намёков живых обновлений (`core/live/collector.py`):
    # сессия базы не знает сотрудника, а слушатель сброса видит только её.
    db.info[live_collector.ACTOR] = user.id
    return user


def require_staff(user: User = Depends(get_current_user)) -> User:
    """Обычные рабочие эндпоинты: доступ закрыт, пока не сменён временный пароль."""
    if user.must_change_password:
        raise errors.ForbiddenError(
            "Password change required before continuing", code="password_change_required"
        )
    return user


# `require_root` здесь больше нет намеренно. Права раздаются ролями, и «нужен
# root» перестало быть ответом на вопрос «кому можно»: должность «гендиректор»
# заводит ящики и переключает блоки, не будучи владельцем системы. Осталась бы
# эта зависимость — она стала бы удобным способом закрыть очередной маршрут мимо
# матрицы доступов, и матрица начала бы врать о том, что настраивает.
#
# Root при этом никуда не делся: `permissions_service.has` отдаёт ему все права
# всегда, поэтому `require_perm` пропускает его в любой раздел.


def require_module(key: str):
    """Закрыть раздел, если его блок выключен.

    Прятать пункт в меню недостаточно: адрес остаётся рабочим, его помнит
    браузер, он лежит в закладках и в старых письмах. Выключенный блок обязан
    отвечать отказом и на прямой запрос, иначе «выключено» означает лишь
    «не видно».

    Ключ проверяется здесь же, при сборке приложения, а не при запросе: опечатка
    в имени блока должна ронять запуск, а не тихо открывать раздел всем.
    """
    if core_modules.get(key) is None:
        raise RuntimeError(f"Unknown module in route guard: {key}")

    def dependency(db: Session = Depends(get_db)) -> None:
        if not modules_service.is_enabled(db, key):
            raise errors.ForbiddenError(
                f"Module '{key}' is switched off", code="module_disabled"
            )

    return dependency


def require_perm(area: str, action: str):
    """Закрыть действие, если у сотрудника нет права на него.

    Спрятать кнопку недостаточно — адрес продолжает работать, его помнит
    браузер и знает всякий, кто открывал раздел вчера. Поэтому право
    проверяется здесь, в API; интерфейс лишь прячет то, что всё равно получит
    отказ.

    **Порядок: блок включён → есть право.** Не наоборот. У сотрудника вполне
    может быть право на склад, который в этом бизнесе выключен, — и тогда
    правдивый ответ «блок выключен», а не «нет права»: второй отправил бы
    владельца искать несуществующую ошибку в матрице доступов. Проверка блока
    стоит внутри этой же зависимости, а не рядом с ней, чтобы порядок не
    зависел от того, в каком месте роутера её однажды пропишут.

    Существование права проверяется при сборке приложения, как ключ блока в
    `require_module`: опечатка в имени должна ронять запуск, а не тихо
    открывать действие всем.
    """
    if not core_permissions.exists(area, action):
        raise RuntimeError(f"Unknown permission in route guard: {area}.{action}")
    module = core_permissions.module_of(area)

    def dependency(
        user: User = Depends(require_staff), db: Session = Depends(get_db)
    ) -> User:
        if module is not None and not modules_service.is_enabled(db, module):
            raise errors.ForbiddenError(
                f"Module '{module}' is switched off", code="module_disabled"
            )
        if not permissions_service.has(db, user, area, action):
            # Причину называем полностью: и что нельзя, и какого права не
            # хватает. Молчаливый отказ превращает настройку доступов в гадание,
            # а «нет права» без имени права — в гадание с подсказкой.
            raise errors.ForbiddenError(
                f"Permission required: {core_permissions.code(area, action)}",
                code="permission_denied",
            )
        return user

    # Метка для переборa маршрутов: по ней тест находит, каким правом закрыта
    # каждая точка API. Раскладка прав по сотне маршрутов делалась руками, и
    # пропуск в одном из них открыл бы раздел всем — заметить такое чтением
    # нельзя, а перебором можно.
    dependency.opencrm_permission = (area, action)  # type: ignore[attr-defined]
    return dependency


def client_ip(request: Request) -> str:
    """Реальный IP клиента с поправкой на доверенные обратные прокси.

    X-Forwarded-For нельзя доверять целиком: клиент может прислать любой префикс.
    nginx дописывает настоящий адрес клиента СПРАВА (``$proxy_add_x_forwarded_for``),
    поэтому берём элемент, добавленный НАШИМ прокси (hops-й с конца), а не левый.
    Без прокси (``trusted_proxy_hops = 0``) заголовок игнорируется полностью —
    используется адрес TCP-пира, который подделать нельзя.

    Важно: uvicorn должен работать с ``--no-proxy-headers`` (docker/entrypoint.sh),
    иначе он сам перепишет ``request.client`` по X-Forwarded-For ещё до этой функции,
    и адрес пира окажется подделан. Тогда определение IP — целиком за нами.

    От этого зависит защита подбора PIN: раньше ротация X-Forwarded-For давала
    атакующему новый бакет rate-limit на каждый запрос и снимала ограничение.
    """
    hops = _settings.trusted_proxy_hops
    if hops > 0:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            parts = [p.strip() for p in forwarded.split(",") if p.strip()]
            if len(parts) >= hops:
                return parts[-hops]
    return request.client.host if request.client else "unknown"
