"""Подробные очертания планеты: докачка, когда интернет есть.

Блок работает без интернета всегда: грубые очертания (1:110m) вшиты в
исходники. Подробные (1:50m, 756 КБ) — необязательное улучшение: при
включённой докачке система пробует их взять, показывает ход на сводке и
повторяет попытку, пока сеть не появится. Не взяла — планета осталась той же,
что была, и это не отказ.

Разбор — `docs/bloki/25-globus.md` §11.1.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

from config.settings import get_settings
from core.geo.topojson import kontury, upakovat

#: Откуда берём. Natural Earth 1:50m через пакет `world-atlas` (public domain),
#: тот же источник, что у вшитых очертаний, — иначе две планеты разошлись бы.
ISTOCHNIK = "https://cdn.jsdelivr.net/npm/world-atlas@2/countries-50m.json"
#: Подробнее вшитого втрое: на масштабе ×6 берег перестаёт быть многоугольником.
SHAG = 0.08
#: Мелкие острова оставляем: ради них подробный набор и качают.
MELKIY_OSTROV = 0.25
#: Сколько ждём сеть. Дольше — минуту висит поток, который никто не отменит.
SROK_SEKUND = 60
#: Пауза между попытками. Пять минут: сеть появляется не по нашему расписанию,
#: но и долбить чужой сервер каждым обновлением экрана нельзя.
PAUZA_SEKUND = 300
#: Больше этого набор не бывает: защита от подменённого адреса и от диска.
POTOLOK_BAYT = 8 * 1024 * 1024

_ZAMOK = threading.Lock()
#: Ход одной работы. В памяти, а не в базе: работа живёт минуту и переживать
#: перезапуск ей незачем — после него сеть проверят заново.
_RABOTA: dict = {"idet": False, "procent": 0, "oshibka": "", "poslednyaya": 0.0}


def _katalog():
    return get_settings().storage_dir / "globus"


def fayl_kart():
    return _katalog() / "mir-podrobno.json"


def _fayl_opisi():
    return _katalog() / "mir-podrobno.meta.json"


def opis() -> dict | None:
    """Что лежит на диске. `None` — подробных очертаний нет."""
    put = _fayl_opisi()
    if not put.exists() or not fayl_kart().exists():
        return None
    try:
        return json.loads(put.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def sostoyanie(hotim: bool) -> dict:
    """Что показать на экране — и заодно повод начать попытку.

    Проверка сети привязана к вопросу, а не к таймеру: пока никто не смотрит,
    незачем и качать. Экран спрашивает раз в несколько секунд, попытка идёт не
    чаще раза в пять минут.
    """
    gotovo = opis()
    with _ZAMOK:
        idet = _RABOTA["idet"]
        procent = _RABOTA["procent"]
        oshibka = _RABOTA["oshibka"]
        poslednyaya = _RABOTA["poslednyaya"]
    if hotim and gotovo is None and not idet and time.time() - poslednyaya > PAUZA_SEKUND:
        nachat()
        idet, procent, oshibka = True, 0, ""
    return {
        "wanted": hotim,
        "ready": gotovo is not None,
        "running": idet,
        "percent": procent,
        "error": oshibka,
        "rings": (gotovo or {}).get("kolec", 0),
        "at": (gotovo or {}).get("kogda", ""),
        "source": ISTOCHNIK,
    }


def nachat() -> bool:
    """Пустить докачку. `False` — уже идёт."""
    with _ZAMOK:
        if _RABOTA["idet"]:
            return False
        _RABOTA.update({"idet": True, "procent": 0, "oshibka": "", "poslednyaya": time.time()})
    threading.Thread(target=_kachat, daemon=True, name="globus-karta").start()
    return True


def zabyt() -> None:
    """Убрать подробные очертания: планета вернётся к вшитым."""
    for put in (fayl_kart(), _fayl_opisi()):
        put.unlink(missing_ok=True)


def _skachat_bayty() -> bytes:
    kuski: list[bytes] = []
    vsego = 0
    with urllib.request.urlopen(ISTOCHNIK, timeout=SROK_SEKUND) as otvet:
        dlina = int(otvet.headers.get("Content-Length") or 0)
        while True:
            kusok = otvet.read(64 * 1024)
            if not kusok:
                break
            vsego += len(kusok)
            if vsego > POTOLOK_BAYT:
                raise ValueError("набор больше потолка")
            kuski.append(kusok)
            # Скачивание — три четверти работы, разбор — четверть: полоса не
            # должна замирать на 100% и ждать.
            dolya = int(vsego * 75 / dlina) if dlina else 40
            with _ZAMOK:
                _RABOTA["procent"] = min(75, dolya)
    return b"".join(kuski)


def _kachat() -> None:
    try:
        syroe = _skachat_bayty()
        with _ZAMOK:
            _RABOTA["procent"] = 80
        kolca = kontury(json.loads(syroe.decode("utf-8")), shag=SHAG, melkiy=MELKIY_OSTROV)
        with _ZAMOK:
            _RABOTA["procent"] = 95
        katalog = _katalog()
        katalog.mkdir(parents=True, exist_ok=True)
        vremenno = fayl_kart().with_suffix(".tmp")
        vremenno.write_text(
            json.dumps({"rings": [upakovat(k) for k in kolca]}, separators=(",", ":")),
            encoding="utf-8",
        )
        vremenno.replace(fayl_kart())
        _fayl_opisi().write_text(
            json.dumps(
                {
                    "kolec": len(kolca),
                    "tochek": sum(len(k) for k in kolca),
                    "kogda": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "istochnik": ISTOCHNIK,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        with _ZAMOK:
            _RABOTA.update({"idet": False, "procent": 100, "oshibka": ""})
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as beda:
        # Нет сети — не беда, а состояние: попытку повторят через паузу, а
        # планета всё это время работает на вшитых очертаниях.
        with _ZAMOK:
            _RABOTA.update({"idet": False, "procent": 0, "oshibka": str(beda)[:200]})
