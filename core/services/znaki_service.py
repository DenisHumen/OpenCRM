"""Знак сервиса: фирменный значок по названию — GitHub, Telegram, Stripe.

Откуда взято. `core/znaki/` — выгрузка **Simple Icons** (simpleicons.org),
3459 брендов, лицензия CC0-1.0: класть к себе и раздавать разрешено самими
авторами набора. Версия набора и порядок пересборки — `docs/bloki/27-klyuchi.md`
§8.

**Чего в наборе нет и не появится.** Amazon (AWS), Microsoft, Slack и часть
банков: эти владельцы потребовали убрать свои знаки из свободных наборов, и
класть их в открытый репозиторий значило бы делать ровно то, против чего было
требование. Для них экран рисует буквенную плашку, а хочется настоящий логотип
— его кладут картинкой к самому ключу.

**Ничего не ходит в сеть.** Догрузка значка с сайта сервиса рассказала бы этому
сервису (а с чужим справочником — и третьей стороне), какими сервисами
пользуется фирма. Для хранилища вторых факторов это худший из возможных
рассказов, поэтому набор лежит на диске целиком.

**Пути не держатся в памяти.** Четыре с половиной мегабайта на каждый рабочий
процесс — заметная доля памяти VPS, на котором живёт и сайт. В памяти только
указатель (150 КБ), сам путь читается смещением из файла по запросу и дальше
живёт в кэше браузера.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache

from config.settings import BASE_DIR

KATALOG = BASE_DIR / "core" / "znaki"
UKAZATEL = KATALOG / "znaki.json"
PUTI = KATALOG / "znaki-puti.txt"

#: Как зовут сервис люди против того, как он записан в наборе. Список короткий
#: намеренно: сюда попадает только то, что вправду встречается в поле «сервис»
#: у строки otpauth, а не всё, что можно вообразить.
PSEVDONIMY = {
    "githubcom": "github",
    "gitlabcom": "gitlab",
    "гитхаб": "github",
    "телеграм": "telegram",
    "телеграмм": "telegram",
    "гугл": "google",
    "гуглаккаунт": "google",
    "googleaccount": "google",
    "googleworkspace": "google",
    "клаудфлер": "cloudflare",
    "битбакет": "bitbucket",
    "дискорд": "discord",
    "стим": "steam",
    "фигма": "figma",
    "ноушен": "notion",
    "страйп": "stripe",
    "пейпал": "paypal",
    "бинанс": "binance",
    "эпл": "apple",
    "айклауд": "icloud",
    "дропбокс": "dropbox",
    "вордпресс": "wordpress",
    "твич": "twitch",
    "реддит": "reddit",
    "линкедин": "linkedin",
}

_NELISHNEE = re.compile(r"[^0-9a-zа-яё]+")


def klyuch_poiska(nazvanie: str) -> str:
    """Название к виду, по которому ищем: только буквы и цифры, нижний регистр.

    «Amazon Web Services», «amazon-web-services» и «AmazonWebServices» — одно и
    то же имя, набранное тремя людьми.
    """
    return _NELISHNEE.sub("", (nazvanie or "").strip().lower())


@lru_cache(maxsize=1)
def _spravochnik() -> tuple[dict[str, list], dict[str, str]]:
    """(указатель по слагу, поиск по названию). Читается один раз на процесс."""
    try:
        ukazatel = json.loads(UKAZATEL.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # Набора нет — модуль работает, знаки становятся буквенными. Ронять
        # хранилище ключей из-за картинок нельзя.
        return {}, {}
    po_imeni: dict[str, str] = {}
    for slag, (nazvanie, _cvet, _smeshchenie, _dlina) in ukazatel.items():
        po_imeni.setdefault(klyuch_poiska(nazvanie), slag)
        po_imeni.setdefault(slag, slag)
    return ukazatel, po_imeni


def nayti(nazvanie: str) -> dict | None:
    """{slug, title, hex} по названию сервиса. `None` — знака нет, рисуем буквы."""
    klyuch = klyuch_poiska(nazvanie)
    if not klyuch:
        return None
    ukazatel, po_imeni = _spravochnik()
    slag = PSEVDONIMY.get(klyuch) or po_imeni.get(klyuch)
    if slag is None or slag not in ukazatel:
        return None
    nazvanie_nabora, cvet, _smeshchenie, _dlina = ukazatel[slag]
    return {"slug": slag, "title": nazvanie_nabora, "hex": cvet}


def put(slag: str) -> str | None:
    """Контур значка (атрибут `d` у `<path>`). `None` — такого слага нет."""
    ukazatel, _ = _spravochnik()
    zapis = ukazatel.get(slag or "")
    if zapis is None:
        return None
    _nazvanie, _cvet, smeshchenie, dlina = zapis
    try:
        with PUTI.open("rb") as f:
            f.seek(smeshchenie)
            return f.read(dlina).decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def svg(slag: str) -> str | None:
    """Готовый `<svg>` одним цветом. Цвет ставит экран через `currentColor`:
    на тёмной плите фирменный чёрный Notion был бы дырой."""
    kontur = put(slag)
    if kontur is None:
        return None
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" '
        f'role="img" aria-hidden="true"><path d="{kontur}"/></svg>'
    )


def skolko() -> int:
    """Сколько знаков в наборе. Нужно сторожу: набор, не доехавший в образ,
    молча превратил бы все знаки в буквенные."""
    return len(_spravochnik()[0])
