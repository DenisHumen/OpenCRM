import hashlib
import secrets

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from config.settings import get_settings


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def new_share_token() -> str:
    # 16 байт = 128 бит энтропии, 22 url-safe символа
    return secrets.token_urlsafe(16)


def new_file_uid() -> str:
    return secrets.token_hex(16)


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def hash_ip(ip: str) -> str:
    settings = get_settings()
    return hashlib.sha256(f"{settings.ip_hash_salt}:{ip}".encode()).hexdigest()


#: Сколько живёт пропуск, выданный за верный PIN.
#:
#: Сутки — это «клиент смотрит работы сегодня». Раньше пропуск был бессрочным:
#: подписанная строка без метки времени, предъявительская, годная навсегда.
#: Скопировал значение — доступ у тебя, и отобрать его можно было только отзывом
#: самой ссылки.
PIN_ACCESS_SECONDS = 24 * 60 * 60


def _pin_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().secret_key, salt="board-pin-access")


def _pin_fingerprint(pin_hash: str) -> str:
    """Короткий отпечаток текущего PIN — чтобы смена кода отзывала пропуска."""
    return sha256_hex(pin_hash or "")[:16]


def make_pin_access_cookie(share_link_id: int, pin_hash: str) -> str:
    """Подписанная cookie: клиент ввёл верный PIN для этой ссылки.

    В неё входит отпечаток самого кода. Без него «сменить PIN» не отрезало
    никого: старые пропуска продолжали работать, и единственным способом закрыть
    доступ был отзыв ссылки — то есть кнопка «сменить код» обещала не то, что
    делала.
    """
    return _pin_serializer().dumps({"sid": share_link_id, "fp": _pin_fingerprint(pin_hash)})


def check_pin_access_cookie(value: str, share_link_id: int, pin_hash: str) -> bool:
    try:
        data = _pin_serializer().loads(value, max_age=PIN_ACCESS_SECONDS)
    except (BadSignature, SignatureExpired):
        return False
    if not isinstance(data, dict) or data.get("sid") != share_link_id:
        return False
    # не-секрет: отпечаток лежит ВНУТРИ уже проверенной подписи, а подделать
    # её нечем — ключ сервера. Подбирающий сравнивает свой же старый пропуск
    # со свежим отпечатком и узнаёт из времени ровно ничего.
    return data.get("fp") == _pin_fingerprint(pin_hash)


#: Сколько живёт ключ просмотра файла по ссылке. Десять минут — столько, чтобы
#: хватило открыть большой файл, и мало, чтобы пересланный адрес картинки
#: перестал работать раньше, чем дойдёт до получателя.
VIEW_KEY_SECONDS = 10 * 60


def _view_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().secret_key, salt="file-view-key")


def make_view_key(link_id: int, gost: str = "") -> str:
    """Ключ на просмотр файла по ссылке.

    Привязан к ссылке, а у приглашённых — ещё и к почте смотрящего. Что он
    вправду закрывает: «скопировал адрес картинки со страницы и переслал
    дальше» — через десять минут такой адрес мёртв, а у приглашённых мёртв и
    раньше, если у получателя нет пропуска на ту же почту. Что не закрывает:
    снимок экрана; об этом на странице владельца написано прямо.
    """
    return _view_serializer().dumps({"lid": link_id, "kto": gost or ""})


def check_view_key(value: str, link_id: int, gost: str = "") -> bool:
    try:
        data = _view_serializer().loads(value, max_age=VIEW_KEY_SECONDS)
    except (BadSignature, SignatureExpired):
        return False
    if not isinstance(data, dict) or data.get("lid") != link_id:
        return False
    # Ключ, выданный одному гостю, не открывает файл другому: пересланная
    # вместе со страницей подпись без пропуска на ту же почту мертва.
    return (data.get("kto") or "") == (gost or "")


def _guest_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().secret_key, salt="file-guest-access")


def make_guest_cookie(link_id: int, email: str) -> str:
    """Пропуск приглашённого: он назвал почту из списка этой ссылки."""
    return _guest_serializer().dumps({"lid": link_id, "kto": email})


def read_guest_cookie(value: str, link_id: int) -> str:
    """Чья почта в пропуске или пусто. Подпись проверяется ключом сервера:
    подставить чужой адрес и получить его в водяном знаке нельзя — знак обязан
    говорить правду, иначе он хуже, чем ничего."""
    try:
        data = _guest_serializer().loads(value, max_age=PIN_ACCESS_SECONDS)
    except (BadSignature, SignatureExpired):
        return ""
    if not isinstance(data, dict) or data.get("lid") != link_id:
        return ""
    return str(data.get("kto") or "")
