"""Ключи API сайта из консоли: list / new / show / revoke / rotate.

Обёртка — `./opencrm.sh apikey …`. Консоль нужна не для удобства: экран
настроек может быть недоступен ровно тогда, когда ключ надо отозвать срочно.
`new` печатает ключ ОДИН раз — в базе только отпечаток (docs/16 §8, §12).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import exceptions as errors  # noqa: E402
from core.services import api_key_service  # noqa: E402
from database.repositories import api_keys as keys_repo  # noqa: E402
from database.repositories import users as users_repo  # noqa: E402
from database.session import SessionLocal  # noqa: E402


def _stroka(k: dict) -> str:
    srok = k["expires_at"] or "БЕССРОЧНЫЙ"
    return (
        f"{k['id']:>4}  {k['state']:<8} {k['prefix']}…  {k['name']}\n"
        f"      области: {', '.join(k['scopes'])}; склад: {k['warehouse_id'] or '—'}; "
        f"наличие: {k['stock_mode']}; {k['rate_per_min']}/мин; бронь ≤ {k['max_reserve_minutes']} мин\n"
        f"      срок: {srok}; последнее обращение: {k['last_used_at'] or 'не было'}"
        + (f" с {k['last_used_ip']}" if k["last_used_ip"] else "")
    )


def _pokazat_klyuch(raw: str) -> None:
    print("Ключ (показывается ОДИН раз, второй раз его негде взять):")
    print(f"  {raw}")
    print(f"Заголовок: {api_key_service.HEADER}")


def main(argv: list[str] | None = None) -> int:
    razbor = argparse.ArgumentParser(description="ключи доступа API сайта")
    pod = razbor.add_subparsers(dest="komanda", required=True)
    pod.add_parser("list", help="все ключи, включая отозванные")
    novyy = pod.add_parser("new", help="выдать ключ; печатает его один раз")
    novyy.add_argument("--name", required=True, help="имя, по которому его потом искать")
    novyy.add_argument("--scopes", required=True, help="через запятую: catalog.read,stock.read,orders.write")
    novyy.add_argument("--warehouse", type=int, default=None, help="id склада; обязателен при stock.read")
    novyy.add_argument("--days", type=int, default=api_key_service.DEFAULT_DAYS, help="срок в днях; 0 — бессрочный")
    novyy.add_argument("--stock", default="bucket", choices=("exact", "bucket", "boolean"))
    novyy.add_argument("--few", type=int, default=5000, help="порог few в тысячных")
    novyy.add_argument("--rate", type=int, default=120, help="запросов в минуту")
    novyy.add_argument("--reserve-max", type=int, default=1440, help="потолок брони в минутах")
    novyy.add_argument("--ttl", type=int, default=60, help="сколько секунд сайту верить ответу о наличии")
    pokaz = pod.add_parser("show", help="один ключ подробно")
    pokaz.add_argument("id", type=int)
    otzyv = pod.add_parser("revoke", help="отозвать — отметкой, строка остаётся")
    otzyv.add_argument("id", type=int)
    rot = pod.add_parser("rotate", help="новый ключ, старый живёт ещё --grace часов")
    rot.add_argument("id", type=int)
    rot.add_argument("--grace", type=int, default=api_key_service.GRACE_HOURS)
    dovody = razbor.parse_args(argv)

    with SessionLocal() as db:
        try:
            actor = users_repo.get_root(db)
            if dovody.komanda == "list":
                klyuchi = api_key_service.list_keys(db)
                if not klyuchi:
                    print("ключей нет — наружу не открыто ничего")
                for k in klyuchi:
                    print(_stroka(k))
                return 0
            if dovody.komanda == "show":
                key = api_key_service.get(db, dovody.id)
                print(_stroka(api_key_service.key_out(key, keys_repo.scopes_of(db, key.id))))
                return 0
            if dovody.komanda == "new":
                key, raw = api_key_service.create(
                    db,
                    actor,
                    {
                        "name": dovody.name,
                        "scopes": dovody.scopes.split(","),
                        "warehouse_id": dovody.warehouse,
                        "days": dovody.days,
                        "stock_mode": dovody.stock,
                        "few_threshold_milli": dovody.few,
                        "rate_per_min": dovody.rate,
                        "max_reserve_minutes": dovody.reserve_max,
                        "ttl_sec": dovody.ttl,
                    },
                )
                db.commit()
                print(_stroka(api_key_service.key_out(key, keys_repo.scopes_of(db, key.id))))
                _pokazat_klyuch(raw)
                return 0
            if dovody.komanda == "revoke":
                key = api_key_service.revoke(db, actor, dovody.id)
                db.commit()
                print(f"ключ {key.id} «{key.name}» отозван")
                return 0
            if dovody.komanda == "rotate":
                key, raw = api_key_service.rotate(db, actor, dovody.id, grace_hours=dovody.grace)
                db.commit()
                print(f"новый ключ {key.id} «{key.name}»; старый {dovody.id} живёт ещё {dovody.grace} ч")
                _pokazat_klyuch(raw)
                return 0
        except errors.DomainError as beda:
            print(f"отказ: {beda.message} ({beda.code})", file=sys.stderr)
            return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
