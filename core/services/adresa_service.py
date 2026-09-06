"""Подсказки адреса: чужой справочник, пока он включён и пока отвечает.

Владелец 06.09.2026: «нужно ещё в карточке клиент привязать google карты или
другие, что бы когда вводить данные в поле "Адрес отправки" оно предлагало».

Правило то же, что у планеты (`globus_karta_service`): установка без интернета
работает как работала, а подсказки — необязательное улучшение. Выключенный
тумблер, пропавшая сеть и отказ чужого сервера дают здесь одно и то же — пустой
список, и ни одно из трёх не беда.

Разбор и выбор источника — `docs/bloki/26-adresa.md`.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from sqlalchemy.orm import Session

from core.geo.dannye import CENTRY_STRAN
from core import exceptions as errors
from core.services import client_service, globus_service, settings_service
from database.models import Client

#: Настройка установки: «1» — спрашиваем, «0» — наружу не ходим вовсе.
KLYUCH = "address_hints"
#: Свой сервер подсказок. Пусто — общий. Настройкой, а не правкой строки в
#: коде: правленое рабочее дерево обновлятор не берёт вовсе (`_preflight`), и
#: «подставьте свой адрес здесь» означало бы «откажитесь от обновлений».
KLYUCH_ISTOCHNIKA = "address_source"

#: Откуда берём. Photon — поиск по OpenStreetMap, сделанный именно под набор по
#: буквам: ключа не просит и кэшировать не запрещает. Почему не Google Places и
#: не Nominatim — `docs/bloki/26-adresa.md` §3.
ISTOCHNIK = "https://photon.komoot.io/api"

#: Чем называемся. Безымянных общие серверы режут первыми, и тогда «нет сети»
#: оказалось бы враньём.
IMYA = "OpenCRM address hints (https://github.com/DenisHumen/OpenCRM)"

#: Короче трёх букв спрашивать нечего: ответ будет про всю планету.
MIN_DLINA = 3
#: Столько вариантов уходит на экран. Длинный список не читают, а выбирают из
#: него мышью — то есть медленнее, чем допечатывают руками.
PREDEL = 7
#: Ждём коротко: человек печатает, и ответ через десять секунд опоздал к тому
#: полю, ради которого спрашивали.
SROK_SEKUND = 4
#: Не чаще раза в секунду наружу. Photon числа не называет («please be fair,
#: extensive usage will be throttled»), секунда взята у Nominatim как разумная
#: мера: десяток запросов на одно слово чужой сервер видит обстрелом.
PAUZA_SEKUND = 1.0
#: Отдых после отказа. Коротко: это поле, в котором печатают, и минута тишины
#: для всех сотрудников из-за одного таймаута — цена выше беды.
OTDYH_SEKUND = 15.0
#: Больше этого ответ не бывает: защита от подменённого адреса.
POTOLOK_BAYT = 512 * 1024
#: Сколько ответов помним и сколько они живут. В памяти, а не на диске: строка
#: поиска — это чужой адрес, и складывать его у себя ради часа мы не станем.
POTOLOK_KESHA = 500
SROK_KESHA = 3600.0

#: Английское название страны → код. Обратный ход по справочнику планеты:
#: `countrycode` Photon отдаёт не в каждой установке, а название — всегда.
KODY_PO_NAZVANIYU = {imya.lower(): kod for kod, (_, _, imya) in CENTRY_STRAN.items()}

#: Что считаем населённым пунктом: у такой записи название лежит в `name`, а
#: поле `city` пусто, и без разбора город уехал бы в строку улицы.
VIDY_GORODOV = ("city", "district", "locality", "county", "state")

_ZAMOK = threading.Lock()
#: Ответы по строке запроса и общий покой. Своё у каждого рабочего процесса: их
#: на установке один-два, а общий счётчик стоил бы похода в Redis на каждую
#: набранную букву — дороже самого запроса наружу.
_KESH: dict[str, tuple[float, list[dict]]] = {}
_POKOY = {"tekst": "", "do": 0.0}


def vklyucheny(db: Session) -> bool:
    return settings_service.get_all(db).get(KLYUCH, "0") == "1"


def istochnik(db: Session) -> str:
    """Куда спрашивать. Свой сервер — если назван и похож на адрес."""
    svoy = (settings_service.get_all(db).get(KLYUCH_ISTOCHNIKA, "") or "").strip()
    return svoy if svoy.startswith(("http://", "https://")) else ISTOCHNIK


def _klyuch(zapros: str) -> str:
    return " ".join((zapros or "").split()).lower()


# --- кэш ----------------------------------------------------------------------


def _iz_kesha(klyuch: str) -> list[dict] | None:
    with _ZAMOK:
        zapis = _KESH.get(klyuch)
        if zapis is None:
            return None
        kogda, najdeno = zapis
        if time.monotonic() - kogda > SROK_KESHA:
            _KESH.pop(klyuch, None)
            return None
        return najdeno


def _v_kesh(klyuch: str, najdeno: list[dict]) -> None:
    with _ZAMOK:
        _KESH[klyuch] = (time.monotonic(), najdeno)
        for lishniy in list(_KESH)[: max(0, len(_KESH) - POTOLOK_KESHA)]:
            _KESH.pop(lishniy, None)


def zabyt() -> None:
    """Забыть ответы и отдых: подсказки выключили либо начинается проверка."""
    with _ZAMOK:
        _KESH.clear()
        _POKOY.update({"tekst": "", "do": 0.0})


# --- спрос --------------------------------------------------------------------


def _ryadom_s_klientom(db: Session, client_id: int | None) -> tuple[float, float] | None:
    """Точка, вокруг которой искать. Своя точка клиента — и больше ничего.

    Центр страны установки на эту роль не годится: проверено 06.09.2026, по
    «Шевченка 10» он поднимает деревни у географического центра вместо городов.
    Точка же самого клиента отвечает на вопрос «эта улица в его городе».
    """
    if not client_id:
        return None
    try:
        client = client_service.get_client(db, client_id)
    except errors.NotFoundError:
        return None
    mesto = globus_service.mesto_klienta(client)
    # Точность страны в привязку не годится: центр страны поднимает деревни у
    # географической середины вместо города, в котором клиент живёт.
    if mesto is None or mesto[2] == globus_service.TOCHNOST_STRANA:
        return None
    return mesto[0] / 1e7, mesto[1] / 1e7


def podskazki(db: Session, zapros: str, *, client_id: int | None = None, opener=None) -> dict:
    """Варианты адреса по набранному. Пустой список — обычное состояние.

    Наружу уходит только то, что человек набрал в поле, и только если такого
    ответа нет в памяти и потолок частоты позволяет. Всё остальное — молчание,
    а не отказ: поле дозаполняют руками, как и до подсказок.
    """
    vklyucheno = vklyucheny(db)
    adres_sluzhby = istochnik(db)
    # `held` — «придержали», а не «не нашли». Без него экран показывал бы
    # пустой список, и человек делал вывод «такого адреса нет».
    itog = {"items": [], "enabled": vklyucheno, "source": adres_sluzhby, "error": "", "held": False}
    # Наружу уходит набранное как есть, а помнится оно в нижнем регистре:
    # «Kyiv» и «kyiv» — один вопрос, и спрашивать чужой сервер дважды незачем.
    chistyy = " ".join((zapros or "").split())
    klyuch = _klyuch(chistyy)
    if not vklyucheno:
        # Выключили — чужие адреса не остаются у нас и в памяти. «Выключил, а
        # оно всё равно лежит» — это не выключил.
        if _KESH:
            zabyt()
        return itog
    if len(klyuch) < MIN_DLINA:
        return itog

    # Привязка входит в ключ памяти: «Соборна» у киевского клиента и у
    # одесского — разные вопросы, и общий ответ подставил бы чужой город.
    ryadom = _ryadom_s_klientom(db, client_id)
    if ryadom is not None:
        klyuch = f"{klyuch}|{ryadom[0]:.1f},{ryadom[1]:.1f}"

    lezhit = _iz_kesha(klyuch)
    if lezhit is not None:
        return {**itog, "items": lezhit}

    with _ZAMOK:
        teper = time.monotonic()
        if teper < _POKOY["do"]:
            # Частим или отдыхаем после отказа. Ждать нельзя: ожидание держало
            # бы рабочий поток ради подсказки, которую человек уже дотачивает.
            return {**itog, "error": _POKOY["tekst"], "held": True}
        _POKOY["do"] = teper + PAUZA_SEKUND

    # Соединение отпускаем ДО сети: сессия запроса живёт до конца обработки, а
    # поход наружу занимает секунды. Четыре таких — четыре занятых соединения
    # из пула, и это ровно та беда, разбор которой стоит в `web/middleware.py`.
    db.close()
    try:
        najdeno = _sprosit(chistyy, ryadom, adres_sluzhby, opener)
    except (urllib.error.URLError, OSError, ValueError, TimeoutError, AttributeError) as beda:
        with _ZAMOK:
            _POKOY.update({"tekst": str(beda)[:200], "do": time.monotonic() + OTDYH_SEKUND})
        return {**itog, "error": str(beda)[:200]}

    with _ZAMOK:
        _POKOY["tekst"] = ""
    _v_kesh(klyuch, najdeno)
    return {**itog, "items": najdeno}


def _sprosit(zapros: str, ryadom=None, adres_sluzhby: str = ISTOCHNIK, opener=None) -> list[dict]:
    polya = {"q": zapros, "limit": PREDEL}
    if ryadom is not None:
        polya["lat"], polya["lon"] = f"{ryadom[0]:.4f}", f"{ryadom[1]:.4f}"
    adres = f"{adres_sluzhby}?{urllib.parse.urlencode(polya)}"
    # Язык не просим: адрес пишется для почтальона в стране получателя, и
    # местное написание он читает, а переведённое — нет.
    pismo = urllib.request.Request(adres, headers={"User-Agent": IMYA})
    otkryt = opener or urllib.request.urlopen
    with otkryt(pismo, timeout=SROK_SEKUND) as otvet:
        syroe = otvet.read(POTOLOK_BAYT + 1)
    if len(syroe) > POTOLOK_BAYT:
        raise ValueError("ответ больше потолка")
    return _razobrat(json.loads(syroe.decode("utf-8")))


def _tekst(znachenie) -> str:
    """Строка либо пусто: чужой сервер волен прислать число или список."""
    return znachenie.strip() if isinstance(znachenie, str) else ""


def _strana(svoystva: dict) -> str:
    kod = _tekst(svoystva.get("countrycode")).upper()
    if len(kod) == 2 and kod.isascii() and kod.isalpha():
        return kod
    return KODY_PO_NAZVANIYU.get(_tekst(svoystva.get("country")).lower(), "")


def _mesto(svoystva: dict) -> tuple[str, str]:
    """Улица с домом и город. Улица с домом одной строкой — так её и пишут."""
    imya = _tekst(svoystva.get("name"))
    gorod = _tekst(svoystva.get("city"))
    ulitsa = _tekst(svoystva.get("street"))
    if _tekst(svoystva.get("type")) in VIDY_GORODOV:
        gorod = gorod or imya
    elif not ulitsa:
        ulitsa = imya
    dom = _tekst(svoystva.get("housenumber"))
    return (f"{ulitsa} {dom}".strip() if ulitsa else dom), gorod


def _razobrat(otvet) -> list[dict]:
    """Ответ чужого сервера — не наш договор, а чужое обещание.

    Каждый уровень проверяется на вид: список вместо словаря, число вместо
    строки, «features» строкой. Иначе `AttributeError` уходил бы наружу
    пятисоткой ровно там, где обещан пустой список.
    """
    najdeno: list[dict] = []
    vidano: set[str] = set()
    if not isinstance(otvet, dict):
        return najdeno
    for zapis in (otvet.get("features") or []):
        if not isinstance(zapis, dict):
            continue
        geometriya = zapis.get("geometry")
        koordinaty = geometriya.get("coordinates") if isinstance(geometriya, dict) else None
        if not isinstance(koordinaty, (list, tuple)) or len(koordinaty) < 2:
            continue
        svoystva = zapis.get("properties")
        if not isinstance(svoystva, dict):
            continue
        ulitsa, gorod = _mesto(svoystva)
        indeks = _tekst(svoystva.get("postcode"))
        podpis = ", ".join(
            chast for chast in (ulitsa, gorod, indeks, _tekst(svoystva.get("country"))) if chast
        )
        if not podpis or podpis in vidano:
            continue
        vidano.add(podpis)
        try:
            shirota, dolgota = float(koordinaty[1]), float(koordinaty[0])
        except (TypeError, ValueError):
            continue
        najdeno.append(
            {
                # Обрезаем по колонкам карточки: длинную улицу сервер сам же и
                # отверг бы при выборе — на подсказке, которую сам и выдал.
                "label": podpis[:300],
                "country_code": _strana(svoystva),
                "city": gorod[:120],
                "postcode": indeks[:20],
                "street": ulitsa[:255],
                "lat": shirota,
                "lon": dolgota,
            }
        )
        # Потолок держим МЫ: `limit` — просьба к чужому серверу, и своя, и
        # изменившаяся чужая установка вольны прислать сколько угодно.
        if len(najdeno) >= PREDEL:
            break
    return najdeno


# --- выбор --------------------------------------------------------------------


def zapisat_vybor(db: Session, client_id: int, vybor: dict) -> Client:
    """Выбранная подсказка: поля адреса и точка одним движением.

    Одним, а не двумя запросами: между ними карточка стояла бы с новым адресом
    и старой точкой, а второй запрос ещё и может не дойти.

    Адрес заменяется целиком, пустыми полями тоже: индекс, оставшийся от
    прежнего адреса, — это два адреса в одной карточке, и на конверт уедет их
    смесь.
    """
    client = client_service.update_client(
        db,
        client_id,
        {
            "country": (vybor.get("country_code") or "").strip(),
            "city": vybor.get("city") or "",
            "zip_code": vybor.get("postcode") or "",
            "address": vybor.get("street") or "",
        },
    )
    # Точка тут не производная от адреса, а такой же источник, как поставленная
    # мышью по планете: пересчитать её из строки нечем — чужой справочник может
    # быть выключен, недоступен и отвечать завтра иначе.
    return globus_service.postavit_tochku(db, client, vybor.get("lat"), vybor.get("lon"))
