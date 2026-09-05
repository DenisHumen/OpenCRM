"""Статистика обращений по ключам сайта: запись на каждом запросе, сводка для экрана.

Просьба владельца 05.09.2026: сколько запросов в день, неделю, месяц, среднее,
нагрузка, состав по видам, графики — и живьём. Хранится в базе строкой на
(ключ, час, область): этого хватает на все графики, а поштучный журнал рос
бы быстрее любой другой таблицы. Область запроса (`catalog.read`,
`orders.write`…) и есть вид обращения — своего словаря не заводим.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from core import redis_client
from core.live import collector
from core.live.message import ACTION_UPDATED, Hint
from core.utils import now_utc
from database.models import ApiKey
from database.repositories import api_key_hits as repo

#: Сколько дней держать часовые строки. Графики смотрят на месяц; квартал —
#: запас, чтобы «в прошлом месяце» отвечалось до самой уборки.
HRANIT_DNEY = 90
#: Окно графиков и сводки, дней.
OKNO_DNEY = 30
#: Не чаще одного намёка на ключ за столько секунд: сайт шлёт до двух запросов в
#: секунду, а экран перечитывает сводку по каждому намёку.
NAMYOK_SEKUND = 2


def _chas(moment: datetime) -> datetime:
    return moment.replace(minute=0, second=0, microsecond=0, tzinfo=None)


def zapisat(db: Session, key: ApiKey, category: str, *, rejected: bool = False) -> None:
    """Отметить обращение по ключу и намекнуть экрану ключей — не чаще раза в две секунды."""
    repo.zapisat(db, key.id, _chas(now_utc()), category, rejected=rejected)
    if _mozhno_namyok(key.id):
        collector.announce(db, Hint(topic="api_keys", action=ACTION_UPDATED, id=key.id))


def _mozhno_namyok(key_id: int) -> bool:
    if not redis_client.configured():
        return True
    try:
        return bool(redis_client.get_client().set(f"{redis_client.PREFIX}apihit:{key_id}", "1", nx=True, ex=NAMYOK_SEKUND))
    except Exception:  # noqa: BLE001 — Redis лёг: намёк не важнее записи
        return False


def itogi_po_klyucham(db: Session, key_ids) -> dict[int, int]:
    return repo.itogi_po_klyucham(db, key_ids, _chas(now_utc()) - timedelta(days=OKNO_DNEY))


def svodka(db: Session, key: ApiKey) -> dict:
    """Числа и ряды для экрана: сегодня, неделя, месяц, среднее в день, пик за час,
    по видам, по дням (30) и по часам (24). Всё из часовых строк одним запросом."""
    seychas = now_utc().replace(tzinfo=None)
    chas = _chas(seychas)
    nachalo_dnya = chas.replace(hour=0)
    okno = nachalo_dnya - timedelta(days=OKNO_DNEY - 1)
    stroki = repo.stroki(db, key.id, okno)

    po_dnyam: dict[str, dict[str, int]] = {}
    po_chasam: dict[datetime, int] = {}
    po_vidam: dict[str, int] = {}
    segodnya = nedelya = mesyats = otkazy = 0
    for s in stroki:
        den = s.bucket_at.date().isoformat()
        d = po_dnyam.setdefault(den, {"count": 0, "rejected": 0})
        d["count"] += s.count
        d["rejected"] += s.rejected
        po_chasam[s.bucket_at] = po_chasam.get(s.bucket_at, 0) + s.count
        po_vidam[s.category] = po_vidam.get(s.category, 0) + s.count
        mesyats += s.count
        otkazy += s.rejected
        if s.bucket_at >= nachalo_dnya:
            segodnya += s.count
        if s.bucket_at >= nachalo_dnya - timedelta(days=6):
            nedelya += s.count

    dni = []
    for i in range(OKNO_DNEY):
        den = (okno + timedelta(days=i)).date().isoformat()
        d = po_dnyam.get(den, {"count": 0, "rejected": 0})
        dni.append({"date": den, "count": d["count"], "rejected": d["rejected"]})
    chasy = []
    for i in range(23, -1, -1):
        moment = chas - timedelta(hours=i)
        chasy.append({"hour": moment.isoformat(), "count": po_chasam.get(moment, 0)})
    vidy = sorted(
        ({"category": k, "count": v, "share": round(v / mesyats, 3) if mesyats else 0.0} for k, v in po_vidam.items()),
        key=lambda x: (-x["count"], x["category"]),
    )
    return {
        "today": segodnya,
        "week": nedelya,
        "month": mesyats,
        "rejected_month": otkazy,
        # Среднее — по окну целиком, а не по дням с данными: «сколько в день
        # приходит» спрашивают про календарь, а не про удачные дни.
        "avg_per_day": round(mesyats / OKNO_DNEY, 1),
        # Нагрузка — самый плотный час окна: по нему видно, хватает ли потолка.
        "peak_hour": max(po_chasam.values(), default=0),
        "rate_per_min": key.rate_per_min,
        "by_category": vidy,
        "by_day": dni,
        "by_hour": chasy,
        "since": okno.isoformat(),
    }


def ubrat_starye(db: Session) -> int:
    return repo.purge_older_than(db, _chas(now_utc()) - timedelta(days=HRANIT_DNEY))
