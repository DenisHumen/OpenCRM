from fastapi import Depends, Request
from sqlalchemy.orm import Session

from config.settings import get_settings
from core import exceptions as errors
from core import modules as core_modules
from core import permissions as core_permissions
from core.ratelimit import SlidingWindowLimiter
from core.services import auth_service, modules_service, permissions_service
from database.models import User
from database.session import SessionLocal

SESSION_COOKIE = "opencrm_session"
CSRF_COOKIE = "opencrm_csrf"
CSRF_HEADER = "x-csrf-token"

_settings = get_settings()
login_limiter = SlidingWindowLimiter(
    _settings.login_max_attempts, _settings.login_lockout_minutes * 60
)
pin_limiter = SlidingWindowLimiter(
    _settings.pin_max_attempts, _settings.pin_lockout_minutes * 60
)


def get_db():
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    """Аутентифицированный активный сотрудник (смена пароля может требоваться)."""
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise errors.AuthError("Not authenticated", code="not_authenticated")
    user = auth_service.get_user_by_session(db, token)
    if user is None:
        raise errors.AuthError("Session is invalid or expired", code="session_invalid")
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
