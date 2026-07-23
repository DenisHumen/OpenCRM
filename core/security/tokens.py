import hashlib
import secrets

from itsdangerous import BadSignature, URLSafeSerializer

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


def _pin_serializer() -> URLSafeSerializer:
    return URLSafeSerializer(get_settings().secret_key, salt="board-pin-access")


def make_pin_access_cookie(share_link_id: int) -> str:
    """Подписанная cookie: клиент ввёл верный PIN для этой ссылки."""
    return _pin_serializer().dumps({"sid": share_link_id})


def check_pin_access_cookie(value: str, share_link_id: int) -> bool:
    try:
        data = _pin_serializer().loads(value)
    except BadSignature:
        return False
    return isinstance(data, dict) and data.get("sid") == share_link_id
