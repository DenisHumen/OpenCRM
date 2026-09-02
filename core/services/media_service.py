"""Приём и обработка медиафайлов работ: валидация по сигнатуре,
WebP-превью, blurhash, постеры видео (через ffmpeg, если он установлен)."""

import io
import re
import threading
import shutil
import subprocess
import tempfile
import time
import uuid
import zipfile
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

from PIL import Image

import blurhash as blurhash_lib

from config.settings import get_settings
from core import exceptions as errors
from core import redis_client

# размеры производных изображений (по длинной стороне)
SIZE_LARGE = 1920
SIZE_CARD = 800
SIZE_THUMB = 320

# С какой вытянутости изображение считается «длинным» (лонгрид, инфографика,
# скриншот всей страницы). Обычный вертикальный кадр 2:3 или сторис 9:16 сюда
# не попадают — порог выше их.
LONG_RATIO = 2.5
# Предел стороны в WebP: длинную картинку иначе не удалось бы сохранить
WEBP_MAX_SIDE = 16383


def is_long_image(width: int | None, height: int | None) -> bool:
    return bool(width and height and height >= width * LONG_RATIO)

# имена файлов, которые разрешено отдавать наружу
PUBLIC_FILENAMES = {
    "large.webp", "card.webp", "thumb.webp", "poster.webp",
    "image.svg", "video.mp4", "video.webm",
}


def detect_media(header: bytes, filename: str) -> tuple[str, str, str]:
    """Определяет тип по magic bytes → (kind, ext, mime). Расширению не доверяем."""
    if header.startswith(b"\xff\xd8\xff"):
        return "image", "jpg", "image/jpeg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image", "png", "image/png"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "image", "gif", "image/gif"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "image", "webp", "image/webp"
    if len(header) > 8 and header[4:8] == b"ftyp":
        return "video", "mp4", "video/mp4"
    if header.startswith(b"\x1a\x45\xdf\xa3"):
        return "video", "webm", "video/webm"
    stripped = header.lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if stripped.startswith((b"<?xml", b"<svg")):
        return "svg", "svg", "image/svg+xml"
    raise errors.ValidationError(
        "Unsupported file type. Allowed: JPG, PNG, GIF, WebP, SVG, MP4, WebM",
        code="unsupported_media_type",
    )


def work_dir(work_uid: str) -> Path:
    return get_settings().media_dir / work_uid


def public_file(work_uid: str, filename: str) -> Path | None:
    """Путь к публичному файлу работы — или `None`, если он ведёт наружу.

    **Проверяется итоговый путь, а не имя по частям.** Раньше на входе стоял
    чёрный список разделителей (`"/" in work_uid or "\\" in work_uid`), и он
    пропускал `..`: `media_dir/../large.webp` — уже не каталог работ. Список
    запрещённых кусочков всегда неполон, потому что перечисляет то, о чём
    вспомнили; а каталог назначения один, и «лежит ли файл внутри него» —
    вопрос с однозначным ответом.

    `resolve()` заодно разворачивает символические ссылки: файл внутри каталога
    работ, указывающий наружу, тоже наружу не выпустит.
    """
    if filename not in PUBLIC_FILENAMES:
        return None
    # `work_uid` обязан быть ОДНИМ обычным именем каталога.
    #
    # Это не про выход наружу — про то, что доступ спрашивают по одной строке, а
    # файл читают по другой. Вызывающий передаёт `work_uid` в
    # `share_service.media_is_public`, и тот отвечает про работу с таким
    # опознавателем. Но `rabota-1/..` как путь схлопывается в корень каталога
    # работ, а как опознаватель остаётся отдельной строкой, которой ни одна
    # работа не соответствует. Разрешение выдаётся на одно, чтение идёт из
    # другого — и однажды эти двое разойдутся так, что совпадут.
    if "/" in work_uid or "\\" in work_uid or work_uid in {"", ".", ".."}:
        return None
    root = get_settings().media_dir.resolve()
    candidate = (root / work_uid / filename).resolve()
    if not candidate.is_relative_to(root):
        return None
    return candidate


def original_path(work_uid: str, kind: str, mime: str) -> Path:
    """Где лежит ИСХОДНИК работы — тот самый файл, который загрузили.

    Имя выводится из вида и mime, а не хранится в записи, и это то же правило,
    по которому исходник кладёт `save_original`. Знание было в двух местах:
    здесь клал один код, а фоновая обработка собирала имя заново у себя. Пока
    оба списка совпадают, всё работает; разойдутся — обработка молча не найдёт
    файла, который лежит рядом. Третьей копии (выгрузка исходника менеджеру)
    не завожу — зовут отсюда все трое.

    Путь не проверяется на существование: у работы в состоянии `failed` файл
    бывает и не дописан, а отвечать на вопрос «есть ли он» должен тот, кто
    собрался его читать.
    """
    directory = work_dir(work_uid)
    if mime == "image/svg+xml":
        # SVG не обрабатывается вовсе: он же и исходник, он же и то, что
        # уходит на витрину (после санитайзинга при загрузке).
        return directory / "image.svg"
    if kind == "video":
        return directory / ("video.webm" if mime == "video/webm" else "video.mp4")
    return directory / f"original.{mime.split('/')[-1].replace('jpeg', 'jpg')}"


def save_original(work_uid: str, kind: str, ext: str, content: bytes) -> Path:
    """Кладёт исходник на диск. SVG предварительно санитайзится."""
    directory = work_dir(work_uid)
    directory.mkdir(parents=True, exist_ok=True)
    if kind == "svg":
        content = sanitize_svg(content)
        path = directory / "image.svg"
    elif kind == "video":
        path = directory / f"video.{ext}"
    else:
        path = directory / f"original.{ext}"
    path.write_bytes(content)
    return path


def delete_work_files(work_uid: str) -> None:
    directory = work_dir(work_uid)
    if directory.exists():
        shutil.rmtree(directory, ignore_errors=True)


_SVG_SCRIPT_RE = re.compile(rb"<script\b.*?</script\s*>", re.IGNORECASE | re.DOTALL)
#: Обработчик события. Значение бывает в кавычках и без них — без кавычек
#: (`onload=alert(1)`) прежнее выражение его не видело и пропускало целиком.
_SVG_EVENT_RE = re.compile(rb"\son\w+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", re.IGNORECASE)
#: Ссылка. Режем не по схеме внутри кавычек, а весь атрибут, если значение
#: похоже на скрипт: схему пишут и без кавычек, и числовыми ссылками
#: (`javas&#99;ript:`), и с пробелами — перечислить все написания нельзя.
_SVG_HREF_RE = re.compile(rb"(?:xlink:)?href\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", re.IGNORECASE)
_SVG_BAD_VALUE_RE = re.compile(rb"(javascript|vbscript)\s*:", re.IGNORECASE)
#: `data:` бывает и безобидным: встроенная картинка внутри SVG — обычное дело и
#: выполнить ничего не может. Опасен `data:text/html` и подобное, поэтому режем
#: не схему целиком, а всё, что объявляет себя не картинкой.
_SVG_BAD_DATA_RE = re.compile(rb"data\s*:\s*(?!image/)", re.IGNORECASE)
#: Числовая ссылка на символ: `&#99;` и `&#x63;` — оба вида «c».
_CHAR_REF_RE = re.compile(rb"&#(x?)([0-9a-fA-F]+);?")


def _unescape(value: bytes) -> bytes:
    """Развернуть числовые ссылки на символы: `&#99;` → `c`, `&#x63;` → `c`."""

    def one(match: "re.Match[bytes]") -> bytes:
        digits = match.group(2)
        try:
            code = int(digits, 16 if match.group(1) else 10)
        except ValueError:
            return match.group(0)
        return bytes([code]) if 0 < code < 128 else b""

    return _CHAR_REF_RE.sub(one, value)


def sanitize_svg(content: bytes) -> bytes:
    """Убрать из SVG то, что делает его документом со скриптом.

    **Это второй рубеж, а не первый.** Первый — заголовки: и приложение, и nginx
    отдают такие файлы с `sandbox` в CSP, потому что список запрещённого в SVG
    всегда неполон. Проверка ниже ловит известные обёртки; полагаться на неё как
    на единственную защиту нельзя — а раньше именно так и было: в бою файлы
    отдаёт nginx, и до этой правки он не ставил CSP вовсе.

    Что чинилось здесь после разбора: обработчик события без кавычек
    (`onload=alert(1)`) прежнее выражение не видело вовсе, как и ссылку со
    схемой, записанной числовой ссылкой (`javas&#99;ript:`).
    """
    content = _SVG_SCRIPT_RE.sub(b"", content)
    content = _SVG_EVENT_RE.sub(b"", content)

    def drop_if_script(match):
        # Значение сначала разворачиваем: `javas&#99;ript:` — это «javascript:»,
        # и написать так можно любую букву. Сравнивать сырую строку бессмысленно,
        # вариантов написания больше, чем можно перечислить.
        value = _unescape(match.group(1))
        bad = _SVG_BAD_VALUE_RE.search(value) or _SVG_BAD_DATA_RE.search(value)
        return b"" if bad else match.group(0)

    return _SVG_HREF_RE.sub(drop_if_script, content)


# --- обработка (фоновая задача после загрузки) ---

def _cel(im: Image.Image, box: int) -> tuple[int, int]:
    """Во что ужимаем. Считается ДО всякого выделения памяти.

    Обычный ``thumbnail`` вписывает картинку в квадрат `box`×`box`, то есть
    ужимает **длинную** сторону. Для лонгрида 1:10 это означало бы `card`
    шириной 80px — на витрине сплошное мыло. Поэтому у длинных изображений
    ограничиваем ширину, а высоте позволяем расти (до предела формата).

    Вверх не растягиваем никогда: исходник мельче коробки отдаётся как есть.
    ``thumbnail`` вёл себя так же, и терять это свойство нельзя — растянутая
    вчетверо миниатюра весит больше оригинала и выглядит хуже него.
    """
    if not is_long_image(im.width, im.height):
        k = max(im.width / box, im.height / box, 1.0)
        return max(1, round(im.width / k)), max(1, round(im.height / k))
    width = min(im.width, box)
    height = round(width * im.height / im.width)
    if height > WEBP_MAX_SIDE:
        height = WEBP_MAX_SIDE
        width = max(1, round(height * im.width / im.height))
    return max(1, width), max(1, height)


def derive(im: Image.Image, box: int) -> Image.Image:
    """Производная размером `box` по длинной стороне; у длинных — по ширине.

    **Целевой размер считается заранее, а `resize` делает сразу его.**
    Прежде здесь стоял `im.copy()` с последующим `thumbnail`, и это выделяло
    ПОЛНОРАЗМЕРНУЮ копию оригинала на каждую производную — то есть три копии по
    150 МБ ради трёх картинок, самая большая из которых 1600 точек по стороне.

    Замерено в боевом образе на PNG 7100×7042 (49,99 Мпикс, 0,67 МБ файлом):
    одно разжатие давало пик 612 МБ при 227 МБ до работы, два потока — 1185 МБ.
    Комментарий над ограничителем разжатия при этом обещал «предсказуемый пик
    382 МБ»: он считал только сам разжатый кадр и не знал про копии.

    `resize` выделяет РЕЗУЛЬТАТ и ничего сверх него. Качество то же: `thumbnail`
    внутри зовёт тот же `resize` тем же фильтром, разница только в том, что он
    правит картинку на месте и потому требует копии.
    """
    return im.resize(_cel(im, box), Image.LANCZOS)


#: Сколько мегапикселей позволено РАЗЖАТЬ за раз. Не размер файла и не размер
#: снимка.
#:
#: Цена разжатия замерена и линейна: 3,83 МБ памяти на мегапиксель (4 Мпикс →
#: 15,4 МБ; 16 → 61,1; 36 → 137,8; 89 → 339,7). Считается пиковый RSS процесса,
#: потому что смотрит на него именно OOM-killer.
#:
#: Откуда опасность. У службы `app` в `docker-compose.yml` нет `mem_limit` — он
#: стоит у всех служб наблюдения и отсутствует у трёх главных. Значит верхнюю
#: границу задаёт машина: два гигабайта на всё, включая MySQL. Жертву при
#: нехватке выбирает ядро по наибольшему RSS, то есть скорее всего базу: сайт
#: терял бы MySQL из-за того, что кто-то загрузил альбом.
#:
#: Умолчание Pillow (89,5 Мпикс) от этого не спасало: на 89 Мпикс он лишь
#: предупреждает и разжимает, а отказывает только на вдвое большем.
#:
#: Настоящая приманка — не JPEG, а PNG: 25 Мпикс весят 2,0 МБ файлом и 95,6 МБ
#: в памяти. Маленькая посылка, большой расход — предел `max_upload_mb: 200`
#: такое пропускает не заметив.
#:
#: 50 Мпикс — это 191 МБ худшего случая (замерено: 50 Мпикс PNG дают ровно
#: 191,0 МБ, 51 отвергается без разжатия). Выше любого телефона (48 Мпикс у
#: iPhone 16 Pro) и любой зеркалки массового ряда (61 Мпикс у Sony A7R V), но
#: ниже того, чем можно уронить машину. JPEG сюда почти не упирается: он идёт
#: через `draft` и приходит к проверке уже уменьшенным.
MAX_DECODE_MEGAPIXELS = 50

# --- очередь на разжатие: общая на всю установку -----------------------------
#
# Предел живёт в Redis, а не в памяти процесса. Пока он был
# `threading.BoundedSemaphore(2)`, четыре рабочих процесса давали четыре
# независимых пика против общего `mem_limit: 3g`. Замеры, выбор числа и что
# делаем при лежащем Redis — `docs/08-deployment.md`, «Память на разжатии
# картинок» и «Несколько рабочих процессов».

#: Сколько картинок разжимается ОДНОВРЕМЕННО во всей установке.
#:
#: Замерено в боевом образе на PNG 7100×7042: два разжатия держат пик около
#: 550 МБ при холостых 227, шесть — 937. Два, а не одно, потому что превью
#: строятся в фоне и очередь из двадцати фотографий проходит вдвое быстрее.
ODNOVREMENNO = 2

#: Ключ общей очереди: сортированное множество, член — метка занявшего, очко —
#: момент, после которого место считается брошенным.
KLYUCH_MESTA = f"{redis_client.PREFIX}razzhatie"

#: Через сколько секунд занятое место считается брошенным.
#:
#: Убитый по памяти процесс своего места не отдаст, а место без срока — это
#: загрузка картинок, переставшая работать до перезапуска. Запас стократный:
#: разжатие с тремя производными укладывается в секунду.
SROK_MESTA_SEKUND = 120

#: Сколько ждём свободного места, прежде чем отказаться от работы.
#:
#: Ждать без предела нельзя: обработка идёт в общем пуле потоков Starlette
#: (сорок мест), и вставшая очередь остановила бы все синхронные ручки сразу.
#: Одна работа с пометкой `failed` дешевле вставшего приложения.
OZHIDANIE_SEKUND = 120

#: Шаг опроса очереди и его потолок.
#:
#: Ожидающих бывает до сорока — столько мест в пуле потоков, — и опрос каждые
#: двадцать миллисекунд стоил бы двух тысяч команд в секунду на ровном месте.
_SHAG_SEKUND = 0.02
_SHAG_POTOLOK = 0.25

#: Не чаще раза в столько секунд говорим, что общего предела нет.
KRICHAT_RAZ_V_SEKUND = 30.0

#: Запасной предел в памяти процесса: Redis не задан или не ответил.
_mestnoe = threading.BoundedSemaphore(ODNOVREMENNO)

_bez_obshchego = 0
_krik_zamok = threading.Lock()
_poslednii_krik = 0.0


def bez_obshchego_zamka_total() -> int:
    """Сколько раз разжатие обошлось без общего предела. Читают проверки и `/metrics`."""
    return _bez_obshchego


def _krichat(deystvie: str, exc: Exception) -> None:
    """Общего предела нет — сказать вслух. Тихого варианта здесь не бывает."""
    global _bez_obshchego, _poslednii_krik
    with _krik_zamok:
        _bez_obshchego += 1
        teper = time.monotonic()
        skazat = teper - _poslednii_krik >= KRICHAT_RAZ_V_SEKUND
        if skazat:
            _poslednii_krik = teper
    if skazat:
        print(
            f"[opencrm] ТРЕВОГА: общая очередь на разжатие недоступна ({deystvie}): {exc}. "
            f"Предел {ODNOVREMENNO} держится в каждом процессе отдельно — пик памяти "
            "умножается на число рабочих процессов.",
            flush=True,
        )


#: «Выбросить брошенные места, сосчитать, и если есть свободное — занять его».
#:
#: Скриптом, а не конвейером, по той же причине, что у ограничителя попыток
#: (`core/ratelimit._ZANYAT`): нужно ветвление на ТОЛЬКО ЧТО прочитанном счёте.
#: Конвейер вычислял бы условие снаружи — то есть снова оставлял бы окно.
_ZANYAT_MESTO = """
redis.call('zremrangebyscore', KEYS[1], '-inf', ARGV[1])
local zanyato = redis.call('zcard', KEYS[1])
if zanyato >= tonumber(ARGV[4]) then
  return 1
end
redis.call('zadd', KEYS[1], ARGV[2], ARGV[3])
redis.call('expire', KEYS[1], ARGV[5])
return 0
"""


@contextmanager
def mesto_razzhatiya():
    """Место в общей очереди на разжатие. Отдаётся при любом исходе.

    Redis не ответил — держим предел В СВОЁМ процессе и кричим об этом. Цена
    известна и посчитана (`docs/08-deployment.md`): четыре процесса по два
    места — 2,2 ГБ худшего случая, ровно то, подо что и брался `mem_limit: 3g`.
    Отказать в загрузке дороже, а пропустить без предела нельзя вовсе.
    """
    otdat = _zanyat_mesto()
    try:
        yield
    finally:
        otdat()


def _zanyat_mesto():
    """Занять место и вернуть то, чем его отдают."""
    client = redis_client.get_client()
    if client is None:
        # Адрес не задан — процесс ровно один (шапка `core/redis_client.py`), и
        # предел в его памяти И ЕСТЬ общий. Это не авария, кричать не о чем.
        return _zanyat_mestnoe()
    otdat = _zanyat_obshchee(client)
    return otdat if otdat is not None else _zanyat_mestnoe()


def _zanyat_obshchee(client):
    """Место в общей очереди. `None` — Redis не ответил, держать предел нечем."""
    metka = uuid.uuid4().hex
    shag = _SHAG_SEKUND
    do = time.monotonic() + OZHIDANIE_SEKUND
    while True:
        teper = time.time()
        try:
            polno = client.eval(
                _ZANYAT_MESTO,
                1,
                KLYUCH_MESTA,
                teper,
                teper + SROK_MESTA_SEKUND,
                metka,
                ODNOVREMENNO,
                SROK_MESTA_SEKUND + 1,
            )
        except Exception as exc:  # noqa: BLE001 — сеть, таймаут, отказ сервера
            _krichat("занять место", exc)
            return None
        if not int(polno):
            return lambda: _otdat_obshchee(client, metka)
        if time.monotonic() >= do:
            raise errors.PrehodyashchayaBedaError(
                "Image processing is busy, try again in a minute",
                code="decode_queue_busy",
            )
        time.sleep(shag)
        shag = min(shag * 2, _SHAG_POTOLOK)


def _otdat_obshchee(client, metka: str) -> None:
    """Отдать место. Не падает: неудача стоит одного места до конца его срока."""
    try:
        client.zrem(KLYUCH_MESTA, metka)
    except Exception as exc:  # noqa: BLE001
        _krichat("отдать место", exc)


def _zanyat_mestnoe():
    """Тот же предел, но в памяти процесса. Срок ожидания общий с редисовым."""
    if not _mestnoe.acquire(timeout=OZHIDANIE_SEKUND):
        raise errors.PrehodyashchayaBedaError(
            "Image processing is busy, try again in a minute",
            code="decode_queue_busy",
        )
    return _mestnoe.release


def _proverit_byudzhet(im: Image.Image) -> None:
    """Отказать до разжатия, если картинка не влезает в бюджет памяти."""
    megapikseley = (im.size[0] * im.size[1]) / 1_000_000
    if megapikseley > MAX_DECODE_MEGAPIXELS:
        raise errors.ValidationError(
            f"Image is too large to process: {im.size[0]}x{im.size[1]} "
            f"({megapikseley:.0f} megapixels, limit is {MAX_DECODE_MEGAPIXELS}). "
            "Save it as JPEG or scale it down and upload again.",
            code="image_too_large",
        )


def assert_decodable(content: bytes) -> None:
    """Проверить картинку ДО того, как её примут. Зовётся из загрузки.

    Стоит в запросе, а не в фоновой обработке, ровно затем, чтобы отказ дошёл до
    человека. Превью строятся фоновой задачей, и отказ там пометил бы работу как
    `failed` без единого слова о причине: поля для причины у модели нет.

    Ничего не стоит: `Image.open` читает заголовок, размеры известны сразу
    (замерено: прирост 0,0 МБ), `draft` тоже не разжимает.
    """
    try:
        with Image.open(io.BytesIO(content)) as im:
            im.draft("RGB", (SIZE_LARGE, SIZE_LARGE))
            _proverit_byudzhet(im)
    except errors.ValidationError:
        raise
    except Exception:
        # Не разобралось — не наше дело: тип уже определён по magic bytes, а
        # битый файл честнее отдать обработчику, который пометит работу
        # неудачной, чем отвергать здесь чужой ошибкой.
        return


def process_image(work_uid: str, original: Path) -> dict:
    with Image.open(original) as im:
        # РАЗМЕРЫ ИЗВЕСТНЫ ДО РАЗЖАТИЯ и стоят ноль байт. Снимаем их здесь,
        # потому что дальше `draft` изменит `im.size`, а в метаданных работы
        # обязан лежать размер ОРИГИНАЛА: по нему считаются пропорции,
        # `is_long_image` и `srcset`. Уменьшенный размер там означал бы тихо
        # испорченную вёрстку витрины у всех загруженных работ.
        width, height = im.size

        # Разжать сразу помельче. JPEG умеет отдавать 1/2, 1/4, 1/8, и Pillow
        # выбирает ближайший масштаб НЕ МЕНЬШЕ запрошенного; исходник мельче
        # коробки не трогается вовсе. Замерено на 89 Мпикс: 339,7 МБ → 21,5 МБ
        # (в шестнадцать раз меньше) и при этом БЫСТРЕЕ — 0,95 с против 1,25.
        # Качество не страдает: самая крупная производная всё равно 1920 px.
        im.draft("RGB", (SIZE_LARGE, SIZE_LARGE))

        # Бюджет считается ПОСЛЕ `draft`, и порядок тут — вся суть. Тогда он
        # ограничивает настоящий расход памяти, а не размер снимка: фотография
        # со 100-мегапиксельной камеры придёт сюда уменьшенной и пройдёт, а
        # PNG-бомба останется собой и будет отвергнута — до разжатия, то есть
        # бесплатно (`draft` к PNG неприменим и отвечает `None`).
        _proverit_byudzhet(im)

        with mesto_razzhatiya():
            im.load()
            if im.mode not in ("RGB", "RGBA"):
                im = im.convert(
                    "RGBA" if "transparency" in im.info or im.mode in ("P", "LA") else "RGB"
                )
            directory = original.parent
            for name, size in (("large", SIZE_LARGE), ("card", SIZE_CARD), ("thumb", SIZE_THUMB)):
                derive(im, size).save(directory / f"{name}.webp", "WEBP", quality=85)
            bh = compute_blurhash(im)
    return {"width": width, "height": height, "blurhash": bh}


def compute_blurhash(im: Image.Image) -> str | None:
    try:
        # Ужимаем СНАЧАЛА, преобразуем ПОТОМ.
        #
        # Прежде стояло `im.convert("RGB").copy()`: `convert` выделяет
        # полноразмерную картинку, `.copy()` — ещё одну такую же, и обе живут
        # ради результата в тридцать два пикселя по стороне. На 50 Мпикс это
        # 300 МБ впустую.
        small = im.resize(_cel(im, 32), Image.LANCZOS).convert("RGB")
        w, h = small.size
        pixels = [[list(small.getpixel((x, y))) for x in range(w)] for y in range(h)]
        return blurhash_lib.encode(pixels, components_x=4, components_y=3)
    except Exception:
        # Размытая подложка — украшение на время загрузки. Не сосчиталась —
        # работа всё равно готова, и ронять её обработку из-за подложки
        # значит терять картинку целиком ради её тени.
        return None


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def process_video(work_uid: str, video_path: Path) -> dict:
    """Постер и метаданные видео. Без ffmpeg — видео остаётся без постера."""
    meta: dict = {"width": None, "height": None, "duration_sec": None, "blurhash": None}
    if not ffmpeg_available():
        return meta
    try:
        probe = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height:format=duration",
                "-of", "csv=p=0", str(video_path),
            ],
            capture_output=True, text=True, timeout=60, check=True,
        )
        lines = [ln.strip() for ln in probe.stdout.splitlines() if ln.strip()]
        for ln in lines:
            parts = ln.split(",")
            if len(parts) >= 2 and parts[0].isdigit():
                meta["width"], meta["height"] = int(parts[0]), int(parts[1])
            elif parts[0].replace(".", "", 1).isdigit():
                meta["duration_sec"] = round(float(parts[0]), 2)

        with tempfile.TemporaryDirectory() as tmp:
            frame = Path(tmp) / "frame.png"
            subprocess.run(
                ["ffmpeg", "-y", "-ss", "1", "-i", str(video_path),
                 "-frames:v", "1", str(frame)],
                capture_output=True, timeout=120, check=True,
            )
            if not frame.exists():  # видео короче 1 секунды — берём первый кадр
                subprocess.run(
                    ["ffmpeg", "-y", "-i", str(video_path), "-frames:v", "1", str(frame)],
                    capture_output=True, timeout=120, check=True,
                )
            if frame.exists():
                directory = video_path.parent
                with Image.open(frame) as im:
                    im.load()
                    # Через `derive`, а не своим `copy` + `thumbnail`: у кадра
                    # видео та же цена полноразмерной копии, что у снимка, и
                    # правило про длинные картинки для него верно так же.
                    for name, size in (("poster", SIZE_LARGE), ("card", SIZE_CARD), ("thumb", SIZE_THUMB)):
                        derive(im, size).save(directory / f"{name}.webp", "WEBP", quality=85)
                    meta["blurhash"] = compute_blurhash(im)
    except (subprocess.SubprocessError, OSError):
        pass  # постер — best effort; видео остаётся играбельным
    return meta


def derived_size(width: int | None, height: int | None, box: int) -> tuple[int, int] | None:
    """Размер производной по правилам :func:`derive`, но без открытия файла.

    Нужен для `srcset`: браузеру необходимо знать ширину каждого кандидата,
    а лезть на диск ради этого на каждый рендер витрины не стоит.
    """
    if not width or not height:
        return None
    if is_long_image(width, height):
        target_w = min(width, box)
        target_h = round(target_w * height / width)
        if target_h > WEBP_MAX_SIDE:
            target_h = WEBP_MAX_SIDE
            target_w = max(1, round(target_h * width / height))
        return target_w, target_h
    scale = min(box / width, box / height, 1)
    return max(1, round(width * scale)), max(1, round(height * scale))


def media_url(work_uid: str, filename: str) -> str:
    return f"/media/{work_uid}/{filename}"


def work_srcset(work) -> str:
    """Кандидаты для `srcset` плитки витрины: `card` (800px) и `large` (1920px).

    Место в композиции бывает 500–700 CSS-пикселей, а на экране с двойной
    плотностью это 1000–1400 реальных — `card` там растягивался бы и мылил,
    хотя `large` лежит рядом. Пусть браузер выбирает сам: маленькой плитке
    незачем тянуть полуторамегабайтный файл.
    """
    if work.kind != "image" or work.mime == "image/svg+xml":
        return ""
    candidates: list[str] = []
    widths: set[int] = set()
    for name, box in (("card", SIZE_CARD), ("large", SIZE_LARGE)):
        size = derived_size(work.width, work.height, box)
        # у мелкого оригинала обе производные одного размера — второй кандидат лишний
        if size is None or size[0] in widths:
            continue
        widths.add(size[0])
        candidates.append(f"{media_url(work.work_uid, name + '.webp')} {size[0]}w")
    return ", ".join(candidates)


def work_media_urls(work) -> dict:
    """URL производных файлов работы для API и витрины."""
    uid = work.work_uid
    if work.kind == "image":
        if work.mime == "image/svg+xml":
            svg = media_url(uid, "image.svg")
            return {"thumb": svg, "card": svg, "large": svg}
        return {
            "thumb": media_url(uid, "thumb.webp"),
            "card": media_url(uid, "card.webp"),
            "large": media_url(uid, "large.webp"),
        }
    # video
    ext = "webm" if work.mime == "video/webm" else "mp4"
    directory = work_dir(uid)
    has_poster = (directory / "poster.webp").exists()
    return {
        "thumb": media_url(uid, "thumb.webp") if has_poster else None,
        "card": media_url(uid, "card.webp") if has_poster else None,
        "poster": media_url(uid, "poster.webp") if has_poster else None,
        "video": media_url(uid, f"video.{ext}"),
    }


# --- выгрузка исходников архивом ---------------------------------------------
#
# Архив собирается ПОТОКОМ и никогда не существует целиком — ни в памяти, ни
# файлом во временном каталоге.
#
# Довод тот же, что у бюджета разжатия выше, и он замерен там же: у службы `app`
# нет `mem_limit`, машина — два гигабайта на всё вместе с MySQL, а жертву при
# нехватке выбирает ядро по наибольшему RSS, то есть скорее всего базу. Доска на
# три десятка снимков с телефона — это полтора гигабайта исходников; собери мы
# такой архив в `BytesIO`, и одна кнопка «скачать все» роняла бы базу.
#
# Временный файл на диске не лучше: место на сервере и так считают (см.
# `storage_service.has_room_for` при загрузке), а тут оно тратилось бы на копию
# того, что уже лежит рядом, — и тратилось бы вдвойне при двух менеджерах,
# нажавших кнопку одновременно.

#: Сколько байт читаем за раз. 256 КБ — обычный компромисс: столько же читает
#: `shutil.copyfileobj` по умолчанию (64 КБ) с запасом на сеть, а память под
#: ответ при этом не зависит ни от числа работ, ни от их размера.
_KUSOK = 256 * 1024


class _Nakopitel(io.RawIOBase):
    """Пишущий конец потока: `zipfile` пишет сюда, а мы забираем накопленное.

    `tell` отвечает честным счётчиком, а `seek` унаследован от `io.IOBase` и
    отказывает — по этому отказу `zipfile` сам понимает, что поток непроматываем,
    и дописывает размеры дескриптором ПОСЛЕ каждого файла вместо того, чтобы
    возвращаться в его заголовок. Ровно это и позволяет отдавать архив, не зная
    заранее, сколько он весит.
    """

    def __init__(self) -> None:
        self._kusok = bytearray()
        self._vsego = 0

    def writable(self) -> bool:
        return True

    def write(self, b) -> int:  # type: ignore[override]
        self._kusok += b
        self._vsego += len(b)
        return len(b)

    def tell(self) -> int:
        return self._vsego

    def zabrat(self) -> bytes:
        """Отдать написанное с прошлого раза и забыть его."""
        gotovo = bytes(self._kusok)
        self._kusok.clear()
        return gotovo


def potok_zip(fayly: Iterable[tuple[Path, str]]) -> Iterator[bytes]:
    """Архив из перечисленных файлов, кусок за куском.

    На входе пары «путь на диске — имя внутри архива». Пути обязаны быть
    проверены ДО вызова: генератор работает уже во время отдачи ответа, когда
    сессия базы закрыта, а заголовки ушли клиенту, — отказывать поздно.

    Без сжатия (`ZIP_STORED`) намеренно. Внутри JPEG, PNG, WebP и MP4 — всё это
    уже сжато, и deflate отвоюет проценты за полную загрузку процессора на
    сотнях мегабайт. Архив здесь нужен как один свёрток, а не как способ
    сэкономить место.

    Пропавший файл пропускаем, а не роняем ответ: заголовки уже отданы, и
    оборвать отдачу на середине значит отдать битый архив вместо архива без
    одной работы.
    """
    nakopitel = _Nakopitel()
    with zipfile.ZipFile(nakopitel, "w", zipfile.ZIP_STORED) as arhiv:
        for put, imya in fayly:
            try:
                istochnik = put.open("rb")
            except OSError:
                continue
            # Источник открывается ПЕРВЫМ, и порядок тут не случаен: заведи мы
            # запись в архиве раньше, её пришлось бы закрывать пустой — то есть
            # в архиве появился бы файл нулевого размера вместо пропущенного.
            with istochnik, arhiv.open(imya, "w") as vnutri:
                while True:
                    kusok = istochnik.read(_KUSOK)
                    if not kusok:
                        break
                    vnutri.write(kusok)
                    gotovo = nakopitel.zabrat()
                    if gotovo:
                        yield gotovo
            gotovo = nakopitel.zabrat()
            if gotovo:
                yield gotovo
    # Закрытый `ZipFile` дописал оглавление — оно и уходит последним куском.
    yield nakopitel.zabrat()
