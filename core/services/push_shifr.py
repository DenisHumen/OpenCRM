"""Web Push без посредников: ключ VAPID (RFC 8292) и шифрование содержимого (RFC 8291).

Разбор — `docs/bloki/31-web-push.md`. Криптографию руками не пишем: всё из
`cryptography`, здесь только склейка по RFC, проверенная его же примером.
"""

from __future__ import annotations

import base64
import json
import os
import struct
import time
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

KRIVAYA = ec.SECP256R1()
#: Порядок группы P-256: закрытый ключ обязан лежать в [1, n-1].
_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
ZAPIS = 4096
#: Сколько живёт подпись VAPID; службы принимают до суток.
PODPIS_CHASOV = 12


def b64u(dannye: bytes) -> str:
    return base64.urlsafe_b64encode(dannye).rstrip(b"=").decode("ascii")


def iz_b64u(stroka: str) -> bytes:
    stroka = "".join(stroka.split())
    return base64.urlsafe_b64decode(stroka + "=" * (-len(stroka) % 4))


def _hkdf(salt: bytes, ikm: bytes, info: bytes, dlina: int) -> bytes:
    return HKDF(hashes.SHA256(), dlina, salt, info).derive(ikm)


def _tochka(klyuch: ec.EllipticCurvePublicKey) -> bytes:
    return klyuch.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def klyuch_iz_sekreta(sekret: str) -> ec.EllipticCurvePrivateKey:
    """Ключ VAPID выводится из `OPENCRM_SECRET_KEY`, а не хранится.

    Сменился ключ — браузеры отвергнут старые подписки (`applicationServerKey` не
    тот), поэтому он обязан переживать перезапуск и копию, как сам секрет.
    """
    syroe = _hkdf(b"opencrm", sekret.encode("utf-8"), b"web-push vapid p-256", 32)
    return ec.derive_private_key(int.from_bytes(syroe, "big") % (_N - 1) + 1, KRIVAYA)


def otkrytyy(klyuch: ec.EllipticCurvePrivateKey) -> str:
    """Открытый ключ для `pushManager.subscribe({applicationServerKey})`."""
    return b64u(_tochka(klyuch.public_key()))


def zashifrovat(
    tekst: bytes,
    p256dh: str,
    auth: str,
    *,
    otpravitel: ec.EllipticCurvePrivateKey | None = None,
    sol: bytes | None = None,
) -> bytes:
    """Тело запроса `Content-Encoding: aes128gcm` одной записью (RFC 8291 §3–4)."""
    ua_tochka = iz_b64u(p256dh)
    ua = ec.EllipticCurvePublicKey.from_encoded_point(KRIVAYA, ua_tochka)
    otpravitel = otpravitel or ec.generate_private_key(KRIVAYA)
    as_tochka = _tochka(otpravitel.public_key())
    sol = sol or os.urandom(16)
    obshchiy = otpravitel.exchange(ec.ECDH(), ua)
    ikm = _hkdf(iz_b64u(auth), obshchiy, b"WebPush: info\x00" + ua_tochka + as_tochka, 32)
    cek = _hkdf(sol, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(sol, ikm, b"Content-Encoding: nonce\x00", 12)
    # 0x02 — разделитель последней записи; одна запись, дополнения нет.
    shifr = AESGCM(cek).encrypt(nonce, tekst + b"\x02", None)
    return sol + struct.pack("!IB", ZAPIS, len(as_tochka)) + as_tochka + shifr


def podpis_vapid(klyuch: ec.EllipticCurvePrivateKey, adres: str, kontakt: str, teper: float | None = None) -> str:
    """Заголовок `Authorization: vapid t=…, k=…` для адреса службы (RFC 8292)."""
    chast = urlsplit(adres)
    zayavka = {
        "aud": f"{chast.scheme}://{chast.netloc}",
        "exp": int((time.time() if teper is None else teper) + PODPIS_CHASOV * 3600),
        "sub": kontakt,
    }
    golova = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    telo = b64u(json.dumps(zayavka, separators=(",", ":")).encode())
    der = klyuch.sign(f"{golova}.{telo}".encode("ascii"), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    podpis = b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    return f"vapid t={golova}.{telo}.{podpis}, k={otkrytyy(klyuch)}"
