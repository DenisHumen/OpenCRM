"""Одноразовые коды по времени (TOTP, RFC 6238) и разбор строки `otpauth://`.

Своя реализация, а не библиотека, и довод тот же, что у `secretbox`: в
зависимостях проекта криптобиблиотеки нет, а весь алгоритм — это HMAC из
стандартной библиотеки и четыре строки арифметики. Заводить зависимость ради
этого значит платить обновлениями и проверками за то, что здесь помещается на
экран.

**Счётчик — это время, делённое на шаг.** Отсюда главное свойство: код зависит
от часов. Часы сервера идут по NTP, часы на машине человека — как получится,
поэтому остаток секунд экран берёт с сервера, а не считает сам
(`docs/bloki/27-klyuchi.md` §5).

Отсечения по времени (проверки чужого кода) здесь НЕТ намеренно: мы коды
показываем, а не проверяем. Появится проверка — ей понадобится окно в шаг
назад и вперёд, и писать его надо будет вместе с защитой от повторного
предъявления, а не отдельно.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import struct
from urllib.parse import parse_qs, unquote, urlsplit

#: Что понимает `kod`. SHA1 — то, чем пользуются почти все сервисы.
ALGORITMY = ("SHA1", "SHA256", "SHA512")
_HESHI = {"SHA1": hashlib.sha1, "SHA256": hashlib.sha256, "SHA512": hashlib.sha512}

#: Границы, за которыми значение перестаёт быть настройкой и становится бедой.
#: Шесть цифр — почти везде, восемь — у части банков; десять уже не влезает в
#: то, что даёт усечение по RFC 4226.
CIFR = (6, 7, 8)
SHAG_MIN, SHAG_MAX = 5, 300


class NeTaStroka(ValueError):
    """Строка не похожа ни на `otpauth://`, ни на ключ base32."""


def normalizovat_sekret(syroy: str) -> str:
    """Ключ сервиса к виду, который принимает base32: без пробелов и в верхнем.

    Сервисы печатают ключ группами по четыре знака и вразнобой по регистру —
    человек копирует ровно то, что видит.
    """
    return "".join((syroy or "").split()).replace("-", "").upper()


def _dopolnit(sekret: str) -> bytes:
    """base32 с добиванием `=`. Сервисы его не пишут, а `b32decode` требует."""
    ochishchen = normalizovat_sekret(sekret)
    if not ochishchen:
        raise NeTaStroka("ключ пуст")
    hvost = len(ochishchen) % 8
    if hvost:
        ochishchen += "=" * (8 - hvost)
    try:
        return base64.b32decode(ochishchen, casefold=True)
    except (binascii.Error, ValueError) as beda:
        raise NeTaStroka("ключ не читается как base32") from beda


def godnyy_sekret(sekret: str) -> bool:
    """Открывается ли ключ вообще. Сказать об этом надо ДО сохранения."""
    try:
        return len(_dopolnit(sekret)) > 0
    except NeTaStroka:
        return False


def kod(sekret: str, *, seychas: int, cifr: int = 6, shag: int = 30, algoritm: str = "SHA1") -> str:
    """Код на момент `seychas` (секунды эпохи). Строка ровно из `cifr` цифр."""
    if cifr not in CIFR:
        raise NeTaStroka(f"длина кода {cifr} не бывает")
    if not SHAG_MIN <= shag <= SHAG_MAX:
        raise NeTaStroka(f"шаг {shag} с не бывает")
    hesh = _HESHI.get((algoritm or "SHA1").upper())
    if hesh is None:
        raise NeTaStroka(f"алгоритм {algoritm} не знаем")
    schyotchik = struct.pack(">Q", seychas // shag)
    metka = hmac.new(_dopolnit(sekret), schyotchik, hesh).digest()
    # Усечение по RFC 4226: младшие четыре бита последнего байта указывают, с
    # какого места брать четыре байта результата.
    smeshchenie = metka[-1] & 0x0F
    chislo = struct.unpack(">I", metka[smeshchenie : smeshchenie + 4])[0] & 0x7FFF_FFFF
    return str(chislo % (10**cifr)).zfill(cifr)


def ostalos(seychas: int, shag: int = 30) -> int:
    """Сколько секунд живёт нынешний код. Ноль не бывает: на границе это `shag`."""
    return shag - (seychas % shag)


def razobrat(stroka: str) -> dict:
    """`otpauth://totp/Сервис:учётка?secret=…` → поля ключа.

    Принимает и голый ключ base32: половина сервисов показывает под QR-кодом
    именно его, и требовать полную строку значило бы отправлять человека
    собирать её руками.

    Разбор нужен ДО сохранения: вставил не то — видно сразу, а не через
    тридцать секунд по неподходящему коду.
    """
    syroe = (stroka or "").strip()
    if not syroe:
        raise NeTaStroka("пусто")
    if not syroe.lower().startswith("otpauth://"):
        if not godnyy_sekret(syroe):
            raise NeTaStroka("это не строка otpauth:// и не ключ base32")
        return {
            "sekret": normalizovat_sekret(syroe),
            "servis": "",
            "uchyotka": "",
            "cifr": 6,
            "shag": 30,
            "algoritm": "SHA1",
        }

    chasti = urlsplit(syroe)
    if chasti.netloc.lower() != "totp":
        # `otpauth://hotp/` — счётчик, а не время: показывать его «сколько
        # секунд осталось» нечем, и молча принять его значило бы завести ключ,
        # который никогда не даст верного кода.
        raise NeTaStroka("это не totp — по счётчику коды мы не считаем")
    zapros = parse_qs(chasti.query)

    def odno(imya: str) -> str:
        znacheniya = zapros.get(imya) or []
        return znacheniya[0].strip() if znacheniya else ""

    put = unquote(chasti.path).lstrip("/")
    servis, _, uchyotka = put.partition(":")
    if not uchyotka:
        # `otpauth://totp/denis@site?issuer=GitHub` — сервис только в запросе.
        servis, uchyotka = "", servis
    servis = odno("issuer") or servis.strip()

    sekret = normalizovat_sekret(odno("secret"))
    if not godnyy_sekret(sekret):
        raise NeTaStroka("в строке нет ключа или он не читается")

    cifr = _chislo(odno("digits"), 6)
    shag = _chislo(odno("period"), 30)
    algoritm = (odno("algorithm") or "SHA1").upper()
    return {
        "sekret": sekret,
        "servis": servis,
        "uchyotka": uchyotka.strip(),
        "cifr": cifr if cifr in CIFR else 6,
        "shag": shag if SHAG_MIN <= shag <= SHAG_MAX else 30,
        "algoritm": algoritm if algoritm in ALGORITMY else "SHA1",
    }


def _chislo(syroe: str, po_umolchaniyu: int) -> int:
    try:
        return int(syroe)
    except (TypeError, ValueError):
        return po_umolchaniyu


def otpauth(*, sekret: str, servis: str, uchyotka: str, cifr: int, shag: int, algoritm: str) -> str:
    """Обратно в строку — её показывают QR-кодом при переносе на телефон."""
    from urllib.parse import quote

    imya = f"{servis}:{uchyotka}" if servis and uchyotka else (servis or uchyotka or "OpenCRM")
    zapros = [f"secret={normalizovat_sekret(sekret)}"]
    if servis:
        zapros.append(f"issuer={quote(servis, safe='')}")
    zapros += [f"algorithm={algoritm}", f"digits={cifr}", f"period={shag}"]
    return f"otpauth://totp/{quote(imya, safe='')}?" + "&".join(zapros)
