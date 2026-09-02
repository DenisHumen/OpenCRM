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
from database.models import Client, Deal, Document, Product, User  # noqa: E402
from database.models.document import DocumentLine  # noqa: E402
from database.models.document import LINE_KINDS, statuses_for  # noqa: E402
from database.models.user import ROLE_MANAGER, STATUS_ACTIVE  # noqa: E402
from database.repositories import roles as roles_repo  # noqa: E402
from database.session import SessionLocal  # noqa: E402

#: Пачка на одну вставку. Тысяча — там, где выигрыш от укрупнения уже почти
#: исчерпан, а память под список ещё не заметна.
PACHKA = 1000

IMENA = ("Алексей", "Мария", "Дмитрий", "Ольга", "Сергей", "Анна", "Игорь", "Елена")
FAMILII = ("Ковалёв", "Соколова", "Морозов", "Волкова", "Зайцев", "Орлова", "Гусев")
ETAPY = ("new", "in_progress", "won", "lost")
TOVARY = ("Матрица", "Клавиатура", "Блок питания", "Шлейф", "Аккумулятор", "Термопаста")

#: Доли видов бумаг. Не поровну, и это не украшение: списки бумаг отбирают
#: ПО ВИДУ, а на равных долях любой отбор попадает в треть таблицы. На живой
#: базе мастерской квитанций большинство, а накладных единицы — то есть
#: именно тот случай, когда отбор без индекса читает всё подряд ради
#: горстки строк. Равные доли это спрятали бы.
DOLI_VIDOV = (
    ("intake", 55),
    ("act", 20),
    ("sales_order", 14),
    ("purchase_order", 6),
    ("waybill_out", 4),
    ("waybill_in", 1),
)


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


def zaseyat_bumagi(skolko: int, klienty: list[int], sluchay: random.Random) -> None:
    """Бумаги пачками: квитанции, акты, заказы и накладные в одной таблице.

    **Зачем они в засеве.** Списки бумаг отбирают по виду и по состоянию и
    считают над каждой категорией её число — то есть `GROUP BY` на каждый заход.
    Мерить это было не на чем: засев умел клиентов и заявки, а `documents`
    оставалась пустой, и любой замер списка бумаг показывал ноль миллисекунд на
    нулевой таблице.

    **Снимок кладём НАСТОЯЩЕЙ длины.** Поиск по бумагам идёт `LIKE` и по номеру,
    и по снимку (`payload`); на пустом снимке он читал бы вчетверо меньше и
    обещал бы вчетверо больше, чем есть.

    Статус берётся из `statuses_for(вид)`, а не из общего списка: квитанция в
    состоянии «черновик» на живой базе не встречается, и отбор по состоянию на
    выдуманном сочетании мерил бы то, чего не бывает.
    """
    if not klienty:
        raise SystemExit("сначала клиенты: бумаге нужен клиент")
    vidy = [vid for vid, dolya in DOLI_VIDOV for _ in range(dolya)]
    seychas = datetime.now()
    nachalo = time.perf_counter()
    with SessionLocal() as db:
        # Номер сквозной и уникальный — продолжаем с последнего, а не с нуля:
        # повторный засев иначе падал бы на уникальном индексе `number`.
        bylo = db.query(Document.id).count()
        for kusok in range(0, skolko, PACHKA):
            v_pachke = min(PACHKA, skolko - kusok)
            stroki = []
            for n in range(v_pachke):
                nomer = bylo + kusok + n + 1
                vid = sluchay.choice(vidy)
                kogda = seychas - timedelta(days=sluchay.randrange(0, 720))
                stroki.append({
                    "number": f"{kogda.year}-{nomer:06d}",
                    "kind": vid,
                    "status": sluchay.choice(statuses_for(vid)),
                    "locale": "ru",
                    "client_id": sluchay.choice(klienty),
                    "payload": _snimok(nomer, sluchay),
                    "created_at": kogda,
                    "updated_at": kogda,
                })
            db.bulk_insert_mappings(Document, stroki)
            db.commit()
            print(f"  бумаг: {kusok + v_pachke}/{skolko}", end="\r", flush=True)
    print(f"  бумаг: {skolko} за {time.perf_counter() - nachalo:.1f} с" + " " * 20)


def _snimok(nomer: int, sluchay: random.Random) -> str:
    """Снимок бумаги — той длины, какая уезжает на печать."""
    veshch = sluchay.choice(("Ноутбук Asus X515", "Матрица 15.6", "Клавиатура", "Блок питания"))
    return (
        '{"company": {"name": "ФОП Иванов", "phone": "+380 44 123-45-67"}, '
        f'"client": {{"name": "Заказчик {nomer}", "phone": "+380 50 000-00-00"}}, '
        f'"fields": {{"item": "{veshch}", "serial": "SN{nomer:08d}", '
        '"condition": "потёртости корпуса", "problem": "не включается", '
        '"accessories": "зарядное устройство", "terms": "гарантия 30 дней"}}'
    )


def zaseyat_tovary(skolko: int, sluchay: random.Random) -> list[int]:
    """Товары: без них строки бумаг ссылаться не на что."""
    nachalo = time.perf_counter()
    with SessionLocal() as db:
        bylo = db.query(Product.id).count()
        for kusok in range(0, skolko, PACHKA):
            v_pachke = min(PACHKA, skolko - kusok)
            stroki = [
                {
                    "sku": f"N-{bylo + kusok + n:06d}",
                    "name": f"{sluchay.choice(TOVARY)} {bylo + kusok + n}",
                    "unit": sluchay.choice(("pcs", "pcs", "pcs", "kg", "m")),
                    "price_minor": sluchay.randrange(1000, 900_000),
                    "cost_minor": sluchay.randrange(500, 400_000),
                    "is_service": False,
                }
                for n in range(v_pachke)
            ]
            db.bulk_insert_mappings(Product, stroki)
            db.commit()
        nomera = [row[0] for row in db.query(Product.id).all()]
    print(f"  товаров: {skolko} за {time.perf_counter() - nachalo:.1f} с")
    return nomera


def zaseyat_stroki(tovary: list[int], sluchay: random.Random) -> None:
    """Строки перечня ко всем бумагам, у которых они бывают.

    **Зачем отдельно от бумаг.** Резерв (`documents_repo.promised`) считается
    СОЕДИНЕНИЕМ строк с бумагами и отбирает по `documents.kind`. На пустой
    таблице строк он отвечает мгновенно и не мерит ничего — а это самый дорогой
    запрос карточки товара и один из тех, чей план ломает неудачный индекс.

    Строки не у всех бумаг: у квитанции их не бывает (`LINE_KINDS`), и сеять их
    туда значило бы мерить то, чего в системе не существует.
    """
    if not tovary:
        raise SystemExit("сначала товары: строке нужен товар")
    nachalo = time.perf_counter()
    with SessionLocal() as db:
        bumagi = [
            row[0] for row in db.query(Document.id).filter(Document.kind.in_(LINE_KINDS)).all()
        ]
        vsego = 0
        for kusok in range(0, len(bumagi), PACHKA):
            stroki = []
            for bumaga in bumagi[kusok:kusok + PACHKA]:
                for poryadok in range(sluchay.randrange(1, 5)):
                    tovar = sluchay.choice(tovary)
                    stroki.append({
                        "document_id": bumaga,
                        "product_id": tovar,
                        "name_snapshot": f"Позиция {tovar}",
                        "quantity_milli": sluchay.randrange(1, 10) * 1000,
                        "price_minor": sluchay.randrange(1000, 900_000),
                        "cost_minor": sluchay.randrange(500, 400_000),
                        "sort_order": poryadok,
                    })
            db.bulk_insert_mappings(DocumentLine, stroki)
            db.commit()
            vsego += len(stroki)
            print(f"  строк: {vsego}", end="\r", flush=True)
    print(f"  строк: {vsego} за {time.perf_counter() - nachalo:.1f} с" + " " * 20)


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
    razbor.add_argument("--bumag", type=int, default=0,
                        help="квитанции, акты, заказы и накладные в одной таблице")
    razbor.add_argument("--tovarov", type=int, default=0,
                        help="товары; нужны строкам бумаг")
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
    if dovody.bumag:
        zaseyat_bumagi(dovody.bumag, klienty, sluchay)
    if dovody.tovarov:
        tovary = zaseyat_tovary(dovody.tovarov, sluchay)
        zaseyat_stroki(tovary, sluchay)
    print("готово")


if __name__ == "__main__":
    main()
