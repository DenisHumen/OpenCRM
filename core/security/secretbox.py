"""Симметричное шифрование прикладных секретов на ключе, производном от OPENCRM_SECRET_KEY.

Зачем вообще: пароль от почтового ящика фирмы нельзя хэшировать, как пароль
сотрудника, — его надо предъявить IMAP-серверу в открытом виде. Значит хранить
приходится обратимо, и вопрос только в том, что увидит тот, кому досталась база.

ЧЕСТНО О СТОЙКОСТИ — читайте до того, как положиться на это.

*От чего защищает.* От утечки одной только базы: копия `opencrm.db`, дамп, старый
бэкап, украденный диск с данными. Без `OPENCRM_SECRET_KEY` содержимое поля
`mail_accounts.password_encrypted` бесполезно.

*От чего НЕ защищает.* От компрометации самого сервера. Ключ лежит в `config/.env`
рядом с базой, процесс держит его в памяти и обязан уметь расшифровать пароль без
участия человека. Кто прочитал `.env` — прочитал и пароли. Это не «хранилище
секретов», это разделение двух артефактов утечки, не более.

*Чем зашифровано.* В зависимостях проекта нет ни `cryptography`, ни другой
криптобиблиотеки, а AES в стандартной библиотеке отсутствует. Поэтому шифр собран
из того, что в stdlib есть и что реализовано на C и проверено временем —
HMAC-SHA256:

  * ключи разведены HKDF-SHA256 (RFC 5869) из `secret_key`: отдельно на шифрование,
    отдельно на аутентификацию — один и тот же ключ в двух ролях это классическая
    ошибка;
  * гамма — HMAC-SHA256(k_enc, nonce ‖ счётчик), то есть PRF в режиме счётчика.
    Конструкция стандартная и опирается на единственное допущение «HMAC-SHA256 —
    псевдослучайная функция», которое лежит и под HKDF, и под TLS PRF;
  * целостность — encrypt-then-MAC: HMAC-SHA256 по версии, nonce и шифротексту,
    сверка через `compare_digest`. Без этого поле в базе можно было бы править
    побитно, а расшифровка молча возвращала бы подделанный текст.

Отдельного примитива (AES, ChaCha20) здесь НЕ написано намеренно: самодельная
реализация шифра — худшее, что можно сделать со стойкостью. Слабое место
конструкции не в математике, а в том, что она собрана вручную и не проходила
стороннего аудита; на боевом стенде с доступной `cryptography` правильный шаг —
заменить внутренности на AES-GCM, сохранив формат токена с меткой версии.

*Чего конструкция не делает.* Не прячет длину секрета (шифротекст ровно длины
исходного текста), не умеет ротацию ключа (смена `OPENCRM_SECRET_KEY` делает все
токены нечитаемыми — пароли ящиков придётся ввести заново), не затирает ключ в
памяти (в Python `bytes` неизменяемы, обнулить их нельзя).
"""

import base64
import hashlib
import hmac
import os
from functools import lru_cache

from config.settings import get_settings

# Метка версии едет внутри токена и попадает под MAC: появится AES-GCM — у него
# будет "v2", а старые записи останутся читаемыми и их можно будет перешифровать.
_VERSION = b"v1"
_NONCE_LEN = 16
_TAG_LEN = 32
_HKDF_SALT = b"opencrm/secretbox"


class SecretBoxError(ValueError):
    """Токен испорчен, подделан или зашифрован другим ключом."""


def encrypt(plaintext: str, purpose: str = "default") -> str:
    """Шифрует строку. `purpose` разводит ключи разных применений."""
    enc_key, mac_key = _keys(get_settings().secret_key, purpose)
    nonce = os.urandom(_NONCE_LEN)
    data = plaintext.encode("utf-8")
    ciphertext = _xor_keystream(enc_key, nonce, data)
    tag = _tag(mac_key, nonce, ciphertext)
    return base64.urlsafe_b64encode(_VERSION + nonce + ciphertext + tag).decode("ascii")


def decrypt(token: str, purpose: str = "default") -> str:
    """Расшифровывает токен `encrypt`. Бросает `SecretBoxError`, если не сходится."""
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii"))
    except Exception as exc:  # noqa: BLE001 — любая порча base64 это одна ситуация
        raise SecretBoxError("Secret is not a valid token") from exc

    head = len(_VERSION) + _NONCE_LEN
    if len(raw) < head + _TAG_LEN or raw[: len(_VERSION)] != _VERSION:
        raise SecretBoxError("Secret is not a valid token")

    nonce = raw[len(_VERSION) : head]
    ciphertext = raw[head : len(raw) - _TAG_LEN]
    tag = raw[len(raw) - _TAG_LEN :]

    enc_key, mac_key = _keys(get_settings().secret_key, purpose)
    # Сверяем MAC ДО расшифровки: расшифрованный, но неаутентичный текст — это
    # текст, который выбрал не тот, кто его записывал.
    if not hmac.compare_digest(tag, _tag(mac_key, nonce, ciphertext)):
        raise SecretBoxError("Secret failed authentication (wrong key or tampered value)")
    return _xor_keystream(enc_key, nonce, ciphertext).decode("utf-8")


def _tag(mac_key: bytes, nonce: bytes, ciphertext: bytes) -> bytes:
    return hmac.new(mac_key, _VERSION + nonce + ciphertext, hashlib.sha256).digest()


def _xor_keystream(enc_key: bytes, nonce: bytes, data: bytes) -> bytes:
    """Гамма = HMAC-SHA256(k, nonce ‖ counter), блоками по 32 байта.

    Счётчик обязателен: без него блоки гаммы повторялись бы и длинный текст
    выдавал бы сам себя (XOR двух блоков шифротекста = XOR двух блоков открытого).
    """
    out = bytearray(len(data))
    for offset in range(0, len(data), 32):
        block = hmac.new(
            enc_key, nonce + (offset // 32).to_bytes(8, "big"), hashlib.sha256
        ).digest()
        chunk = data[offset : offset + 32]
        out[offset : offset + len(chunk)] = bytes(a ^ b for a, b in zip(chunk, block))
    return bytes(out)


@lru_cache(maxsize=8)
def _keys(secret_key: str, purpose: str) -> tuple[bytes, bytes]:
    """HKDF-SHA256 → (ключ шифрования, ключ MAC).

    Кэш по значению ключа, а не по объекту настроек: тесты подменяют
    OPENCRM_SECRET_KEY, и кэш обязан на это реагировать.
    """
    material = _hkdf(secret_key.encode("utf-8"), _HKDF_SALT, purpose.encode("utf-8"), 64)
    return material[:32], material[32:]


def _hkdf(ikm: bytes, salt: bytes, info: bytes, length: int) -> bytes:
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()  # extract
    out, block = b"", b""
    counter = 1
    while len(out) < length:  # expand
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        out += block
        counter += 1
    return out[:length]
