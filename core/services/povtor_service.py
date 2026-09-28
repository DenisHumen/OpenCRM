"""Повторы напоминаний: правило RFC 5545 (RRULE), посчитанное в поясе напоминания.

Разбор, что взято у платформ и почему, — docs/bloki/29-napominaniya.md §4–§5.
Всё время снаружи — наивный UTC, как в базе; местное живёт только внутри.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil.relativedelta import relativedelta
from dateutil.rrule import rrulestr

from core import exceptions as errors

CHASTOTY = ("DAILY", "WEEKLY", "MONTHLY", "YEARLY")
DNI = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
#: Во сколько звонит напоминание «на весь день».
DEN_CHAS = time(9, 0)
#: Потолок перебора: правило, у которого за столько раз не нашлось следующего, — пустое.
POTOLOK_RAZ = 5000
#: Запас вокруг перевода стрелок: местный час может идти не в ту сторону, что UTC.
ZAPAS = timedelta(hours=3)

_BYDAY = re.compile(r"^([+-]?[1-5])?(MO|TU|WE|TH|FR|SA|SU)$")


def _ploho(soobshchenie: str) -> errors.ValidationError:
    return errors.ValidationError(soobshchenie, code="povtor_ne_ponyat")


def razobrat(pravilo: str | None) -> str | None:
    """Проверить правило и вернуть в каноническом виде. Чего модуль не умеет — отказ, а не молчание.

    Часы и минуты (BYHOUR, BYMINUTE) не принимаются: время раза задаёт срок, а
    правило — только дни. Иначе у одного напоминания было бы два ответа на «когда».
    """
    if pravilo is None or not pravilo.strip():
        return None
    chasti: dict[str, str] = {}
    for kusok in pravilo.strip().upper().removeprefix("RRULE:").split(";"):
        if not kusok:
            continue
        imya, _, znachenie = kusok.partition("=")
        if not znachenie or imya in chasti:
            raise _ploho(f"Bad rule part: {kusok}")
        chasti[imya] = znachenie

    chastota = chasti.pop("FREQ", None)
    if chastota not in CHASTOTY:
        raise _ploho("FREQ must be DAILY, WEEKLY, MONTHLY or YEARLY")
    itog = [f"FREQ={chastota}"]

    if "INTERVAL" in chasti:
        interval = _chislo(chasti.pop("INTERVAL"), 1, 999, "INTERVAL")
        if interval != 1:
            itog.append(f"INTERVAL={interval}")
    if "BYDAY" in chasti:
        dni = chasti.pop("BYDAY").split(",")
        for den in dni:
            nayden = _BYDAY.match(den)
            if not nayden:
                raise _ploho(f"Bad BYDAY: {den}")
            if nayden.group(1) and chastota not in ("MONTHLY", "YEARLY"):
                raise _ploho("A numbered weekday (2TU, -1FR) needs MONTHLY or YEARLY")
        itog.append("BYDAY=" + ",".join(dni))
    if "BYMONTHDAY" in chasti:
        dni = [_chislo(d, -31, 31, "BYMONTHDAY") for d in chasti.pop("BYMONTHDAY").split(",")]
        if 0 in dni:
            raise _ploho("BYMONTHDAY cannot be 0")
        itog.append("BYMONTHDAY=" + ",".join(map(str, dni)))
    if "BYMONTH" in chasti:
        mesyatsy = [_chislo(m, 1, 12, "BYMONTH") for m in chasti.pop("BYMONTH").split(",")]
        itog.append("BYMONTH=" + ",".join(map(str, mesyatsy)))
    if "BYSETPOS" in chasti:
        pozitsii = [_chislo(p, -5, 5, "BYSETPOS") for p in chasti.pop("BYSETPOS").split(",")]
        if 0 in pozitsii:
            raise _ploho("BYSETPOS cannot be 0")
        itog.append("BYSETPOS=" + ",".join(map(str, pozitsii)))
    if "COUNT" in chasti and "UNTIL" in chasti:
        raise _ploho("COUNT and UNTIL together are not allowed")
    if "COUNT" in chasti:
        itog.append(f"COUNT={_chislo(chasti.pop('COUNT'), 1, 1000, 'COUNT')}")
    if "UNTIL" in chasti:
        do = chasti.pop("UNTIL")
        if not re.fullmatch(r"\d{8}(T\d{6}Z)?", do):
            raise _ploho("UNTIL must look like 20271231 or 20271231T235959Z")
        itog.append(f"UNTIL={do}")
    if "WKST" in chasti:
        nachalo_nedeli = chasti.pop("WKST")
        if nachalo_nedeli not in DNI:
            raise _ploho("Bad WKST")
        itog.append(f"WKST={nachalo_nedeli}")
    if chasti:
        raise _ploho("Unsupported rule parts: " + ", ".join(sorted(chasti)))
    return ";".join(itog)


def _chislo(tekst: str, ot: int, do: int, imya: str) -> int:
    try:
        chislo = int(tekst)
    except ValueError:
        raise _ploho(f"{imya} must be a number") from None
    if not ot <= chislo <= do:
        raise _ploho(f"{imya} must be between {ot} and {do}")
    return chislo


def zona(poyas: str) -> ZoneInfo:
    try:
        return ZoneInfo(poyas)
    except (ZoneInfoNotFoundError, ValueError):
        raise errors.ValidationError(f"Unknown time zone: {poyas}", code="poyas_neizvesten") from None


def mestnoe(utc: datetime, poyas: str) -> datetime:
    """Наивный UTC → наивное местное (стрелки на стене)."""
    return utc.replace(tzinfo=timezone.utc).astimezone(zona(poyas)).replace(tzinfo=None)


def v_utc(mestnoe_vremya: datetime, poyas: str) -> datetime:
    """Стрелки на стене → наивный UTC. Час, которого нет (перевод весной), уходит вперёд."""
    return mestnoe_vremya.replace(tzinfo=zona(poyas)).astimezone(timezone.utc).replace(tzinfo=None)


def _pravilo(pravilo: str, nachalo_mestnoe: datetime, poyas: str):
    tekst = pravilo
    if "UNTIL=" in tekst:
        # UNTIL даты («до 31 декабря включительно») — конец того дня по местному.
        do = re.search(r"UNTIL=(\d{8})(T\d{6}Z)?", tekst)
        if do and not do.group(2):
            konets = datetime.combine(datetime.strptime(do.group(1), "%Y%m%d").date(), time(23, 59, 59))
            tekst = tekst.replace(do.group(0), "UNTIL=" + konets.strftime("%Y%m%dT%H%M%S"))
        elif do:
            v_mestnom = mestnoe(datetime.strptime(do.group(1) + do.group(2)[:7], "%Y%m%dT%H%M%S"), poyas)
            tekst = tekst.replace(do.group(0), "UNTIL=" + v_mestnom.strftime("%Y%m%dT%H%M%S"))
    return rrulestr(tekst, dtstart=nachalo_mestnoe)


def sleduyushchiy(
    pravilo: str,
    nachalo: datetime,
    poyas: str,
    posle: datetime,
) -> datetime | None:
    """Следующий раз строго после `posle` по календарю правила. Нет — None (повторы кончились).

    Считается в МЕСТНОМ времени и переводится в UTC у каждого раза отдельно:
    «каждый день в 9:00» остаётся 9:00 и после перевода стрелок, а не 8:00 или 10:00.
    """
    pravilo_mestnoe = _pravilo(pravilo, mestnoe(nachalo, poyas), poyas)
    # Вокруг перевода стрелок местный порядок расходится с UTC на час: берём с
    # запасом и сверяем каждый раз уже в UTC.
    for raz in pravilo_mestnoe.xafter(mestnoe(posle, poyas) - ZAPAS, count=POTOLOK_RAZ):
        moment = v_utc(raz, poyas)
        if moment > posle:
            return moment
    return None


def posle_vypolneniya(
    pravilo: str,
    srok: datetime,
    poyas: str,
    kogda_sdelano: datetime,
    sdelano_raz: int,
) -> datetime | None:
    """Режим «через N после выполнения»: от дня выполнения, в тот же час, что был срок.

    COUNT считается закрытыми разами, UNTIL — датой следующего раза.
    """
    chasti = dict(kusok.split("=", 1) for kusok in pravilo.split(";"))
    shag = int(chasti.get("INTERVAL", "1"))
    if "COUNT" in chasti and sdelano_raz >= int(chasti["COUNT"]):
        return None
    den = mestnoe(kogda_sdelano, poyas).date()
    chas = mestnoe(srok, poyas).time()
    sdvig = {
        "DAILY": relativedelta(days=shag),
        "WEEKLY": relativedelta(weeks=shag),
        "MONTHLY": relativedelta(months=shag),
        "YEARLY": relativedelta(years=shag),
    }[chasti["FREQ"]]
    sleduyushchiy_raz = datetime.combine(den, chas) + sdvig
    if "UNTIL" in chasti:
        do = datetime.strptime(chasti["UNTIL"][:8], "%Y%m%d").date()
        if sleduyushchiy_raz.date() > do:
            return None
    return v_utc(sleduyushchiy_raz, poyas)


def v_okne(
    pravilo: str,
    nachalo: datetime,
    poyas: str,
    s: datetime,
    po: datetime,
    limit: int = 400,
) -> list[datetime]:
    """Все разы правила в окне [s, po) — для календаря: повторяющееся видно и в будущих месяцах."""
    pravilo_mestnoe = _pravilo(pravilo, mestnoe(nachalo, poyas), poyas)
    itog: list[datetime] = []
    for raz in pravilo_mestnoe.xafter(mestnoe(s, poyas) - ZAPAS, count=POTOLOK_RAZ, inc=True):
        moment = v_utc(raz, poyas)
        if moment >= po or len(itog) >= limit:
            break
        if moment >= s:
            itog.append(moment)
    return itog


def ves_den_v_utc(den: date, poyas: str) -> datetime:
    """Срок «на весь день» звонит в `DEN_CHAS` по поясу напоминания."""
    return v_utc(datetime.combine(den, DEN_CHAS), poyas)
