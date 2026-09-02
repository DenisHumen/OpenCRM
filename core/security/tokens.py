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
