"""Наполнить базу объёмом, на котором замер что-то значит.

**Зачем отдельно от `scripts/seed_demo.py`.** Тот сеет через живое API и делает
это правильно — так проверяется, что данные проходят все проверки продукта. Но
двадцать тысяч клиентов через API — это часы, а нам нужен объём, а не
достоверность каждой записи.

**И зачем НЕ в `scripts/`.** Этап `app` в `docker/Dockerfile` делает
`COPY scripts/ scripts/`, то есть всё оттуда уезжает в боевой образ. Нагрузочному
инструменту там делать нечего: он нужен на машине разработчика и на стенде.

Сеем через модели проекта, а не сырым SQL: схема меняется, и рукописный
`INSERT` разошёлся бы с ней молча — ровно та беда, от которой в проекте стоит
`schema_check`. `bulk_insert_mappings` даёт тысячи строк в секунду и при этом
знает про колонки то же, что и приложение.

Запуск (база должна существовать и быть мигрированной):

    export OPENCRM_DB_URL='mysql+pymysql://root:пароль@127.0.0.1:3306/opencrm_nagruzka?charset=utf8mb4'
    .venv/Scripts/python.exe nagruzka/zasev.py --klientov 20000 --zayavok 30000
"""

from __future__ import annotations

import argparse
import random
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.security.passwords import hash_password  # noqa: E402
from database.models import Client, Deal, User  # noqa: E402
from database.models.user import ROLE_MANAGER, STATUS_ACTIVE  # noqa: E402
from database.repositories import roles as roles_repo  # noqa: E402
from database.session import SessionLocal  # noqa: E402

#: Пачка на одну вставку. Тысяча — там, где выигрыш от укрупнения уже почти
#: исчерпан, а память под список ещё не заметна.
PACHKA = 1000

IMENA = ("Алексей", "Мария", "Дмитрий", "Ольга", "Сергей", "Анна", "Игорь", "Елена")
FAMILII = ("Ковалёв", "Соколова", "Морозов", "Волкова", "Зайцев", "Орлова", "Гусев")
ETAPY = ("new", "in_progress", "won", "lost")


def zaseyat_klientov(skolko: int, sluchay: random.Random) -> list[int]:
    """Клиенты пачками. Возвращает их номера — они нужны заявкам."""
    nomera: list[int] = []
    nachalo = time.perf_counter()
    with SessionLocal() as db:
        for kusok in range(0, skolko, PACHKA):
            v_pachke = min(PACHKA, skolko - kusok)
            stroki = [
                {
                    "name": f"{sluchay.choice(IMENA)} {sluchay.choice(FAMILII)} {kusok + n}",
                    "phone": f"+7 9{sluchay.randrange(10, 99)} {sluchay.randrange(100, 999)}-"
                             f"{sluchay.randrange(10, 99)}-{sluchay.randrange(10, 99)}",
                    "email": f"klient{kusok + n}@nagruzka.test",
                    "company": sluchay.choice(("", "ООО Ромашка", "ИП Петров", "Студия Свет")),
                }
                for n in range(v_pachke)
            ]
            db.bulk_insert_mappings(Client, stroki)
            db.commit()
            print(f"  клиентов: {kusok + v_pachke}/{skolko}", end="\r", flush=True)

        nomera = [row[0] for row in db.query(Client.id).all()]
    print(f"  клиентов: {skolko} за {time.perf_counter() - nachalo:.1f} с" + " " * 20)
    return nomera


def zaseyat_zayavki(skolko: int, klienty: list[int], sluchay: random.Random) -> None:
    """Заявки пачками, разложенные по времени назад от сегодня.

    Разложенные, а не все с одной датой: отчёты и сводка отбирают по окну, и на
    данных одного дня они мерили бы пустоту либо всё сразу — но не то, что
    бывает на живой базе.
    """
    if not klienty:
        raise SystemExit("сначала клиенты: заявке нужен клиент")
    seychas = datetime.now()
    nachalo = time.perf_counter()
    with SessionLocal() as db:
        for kusok in range(0, skolko, PACHKA):
            v_pachke = min(PACHKA, skolko - kusok)
            stroki = []
            for n in range(v_pachke):
                kogda = seychas - timedelta(days=sluchay.randrange(0, 720))
                stroki.append({
                    "title": f"Заявка {kusok + n}",
                    "client_id": sluchay.choice(klienty),
                    "stage": sluchay.choice(ETAPY),
                    # Деньги целыми в минорных единицах — правило проекта.
                    "amount": sluchay.randrange(0, 500_000) * 100,
                    "created_at": kogda,
                    "updated_at": kogda,
                })
            db.bulk_insert_mappings(Deal, stroki)
            db.commit()
            print(f"  заявок: {kusok + v_pachke}/{skolko}", end="\r", flush=True)
    print(f"  заявок: {skolko} за {time.perf_counter() - nachalo:.1f} с" + " " * 20)


def zaseyat_lyudey(skolko: int, parol: str) -> None:
    """Сотрудники, которые будут работать в замере.

    **Зачем не один root на сто сессий.** Замер «сто пользователей» с одним
    пользователем мерил бы не то: у одного человека один ряд в `users`, одна
    роль, один набор прав — и всё это ложится в те самые памятки и кэши, ради
    которых работа и затевалась. Числа вышли бы красивее правды.

    **Пароль хэшируется ОДИН раз на всех.** Bcrypt по замеру занимает 180 мс, и
    сто честных хэшей — это восемнадцать секунд посева ради нулевой пользы: у
    всех ста пароль всё равно одинаковый. Строка хэша просто копируется. В
    боевом коде так делать нельзя (у каждого своя соль — в этом смысл), здесь
    можно: это стенд, и пароль в нём заведомо общеизвестен.

    `must_change_password` не ставим: смена пароля при первом входе — правильное
    поведение продукта, но в замере она превратила бы вход ста человек в сто
    отказов 403.
    """
    hesh = hash_password(parol)
    nachalo = time.perf_counter()
    with SessionLocal() as db:
        # Роль берём САМУЮ ПРАВАСТУЮ из существующих, а не первую попавшуюся
        # и не «по умолчанию»: сотрудник без прав войдёт и увидит пустую CRM,
        # и замер мерил бы отрисовку пустоты вместо работы со списками.
        vse_roli = roles_repo.list_all(db)
        prava = roles_repo.permissions_by_roles(db, [r.id for r in vse_roli])
        rol = max(vse_roli, key=lambda r: len(prava.get(r.id, [])), default=None)
        est = {e for (e,) in db.query(User.email).filter(User.email.like("sotrudnik%@nagruzka.test"))}
        stroki = [
            {
                "email": f"sotrudnik{n}@nagruzka.test",
                "password_hash": hesh,
                "name": f"Сотрудник {n}",
                "role": ROLE_MANAGER,
                "role_id": rol.id if rol else None,
                "status": STATUS_ACTIVE,
                "must_change_password": False,
                "locale": "ru",
                "avatar_path": "",
            }
            for n in range(skolko)
            if f"sotrudnik{n}@nagruzka.test" not in est
        ]
        if stroki:
            db.bulk_insert_mappings(User, stroki)
            db.commit()
    print(f"  сотрудников: {skolko} (новых {len(stroki)}) за {time.perf_counter() - nachalo:.1f} с")


def main() -> None:
    razbor = argparse.ArgumentParser(description="Наполнить базу объёмом для замера")
    razbor.add_argument("--klientov", type=int, default=20000)
    razbor.add_argument("--zayavok", type=int, default=30000)
    razbor.add_argument("--lyudey", type=int, default=100,
                        help="сотрудников для нагрузочного прогона")
    razbor.add_argument("--parol", default="nagruzka-pass-123")
    razbor.add_argument("--zerno", type=int, default=20260825,
                        help="зерно случайности: два прогона на одном зерне дают одну базу")
    dovody = razbor.parse_args()

    sluchay = random.Random(dovody.zerno)
    print(f"сею в {SessionLocal.kw['bind'].url.render_as_string(hide_password=True)}")
    zaseyat_lyudey(dovody.lyudey, dovody.parol)
    klienty = zaseyat_klientov(dovody.klientov, sluchay)
    zaseyat_zayavki(dovody.zayavok, klienty, sluchay)
    print("готово")


if __name__ == "__main__":
    main()
