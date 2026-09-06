"""Демо-данные через реальное API (живой прогон бэкенда).

Запуск: сервер должен работать на http://localhost:8000
    .venv\\Scripts\\python.exe scripts\\seed_demo.py

Адрес и root — из окружения (`OPENCRM_SEED_URL`, `OPENCRM_SEED_ROOT_EMAIL`,
`OPENCRM_SEED_ROOT_INITIAL`, `OPENCRM_SEED_ROOT_PASSWORD`). Засев заводит бренд,
клиента с лентой и доски с работами, а затем включает блоки и наполняет их:
товары с приходами, заявки по этапам, напоминания, статьи и операции, заказы
во всех состояниях, накладную по заказу, возврат, квитанции. Повторный запуск
ничего не дублирует.
"""

import io
import math
import os
import random
import sys
from datetime import datetime, timedelta, timezone

import httpx
from PIL import Image, ImageDraw

# Адрес и root — из окружения: проба живёт на другом порту и с другим root,
# а править константы под каждый запуск значит однажды засеять боевой.
BASE = os.environ.get("OPENCRM_SEED_URL", "http://localhost:8000")
API = f"{BASE}/api/v1"

ROOT_EMAIL = os.environ.get("OPENCRM_SEED_ROOT_EMAIL", "root@opencrm.local")
ROOT_INITIAL = os.environ.get("OPENCRM_SEED_ROOT_INITIAL", "root-changeme")
ROOT_PASSWORD = os.environ.get("OPENCRM_SEED_ROOT_PASSWORD", "demo-root-password-1")

# тёплая палитра проекта
PALETTES = [
    [(217, 119, 87), (38, 38, 36), (250, 249, 245)],
    [(108, 142, 239), (31, 30, 28), (245, 244, 239)],
    [(76, 175, 110), (22, 21, 20), (232, 162, 61)],
    [(229, 105, 94), (43, 42, 39), (156, 153, 143)],
    [(232, 162, 61), (32, 31, 29), (217, 119, 87)],
]


def art_image(seed: int, width: int, height: int) -> bytes:
    """Генерирует абстрактную «работу» — градиент + геометрия."""
    rnd = random.Random(seed)
    bg, dark, light = rnd.choice(PALETTES)
    im = Image.new("RGB", (width, height), dark)
    draw = ImageDraw.Draw(im)
    # вертикальный градиент
    for y in range(height):
        k = y / height
        color = tuple(int(dark[i] + (bg[i] - dark[i]) * k) for i in range(3))
        draw.line([(0, y), (width, y)], fill=color)
    # круги и дуги
    for _ in range(rnd.randint(3, 6)):
        r = rnd.randint(min(width, height) // 8, min(width, height) // 3)
        x, y = rnd.randint(0, width), rnd.randint(0, height)
        color = rnd.choice([light, bg, dark])
        if rnd.random() < 0.5:
            draw.ellipse([x - r, y - r, x + r, y + r], outline=color, width=rnd.randint(4, 14))
        else:
            draw.ellipse([x - r, y - r, x + r, y + r], fill=color)
    # волна
    points = [
        (x, height // 2 + int(math.sin(x / width * math.pi * rnd.randint(2, 4)) * height // 6))
        for x in range(0, width, 8)
    ]
    draw.line(points, fill=light, width=6)
    buffer = io.BytesIO()
    im.save(buffer, "PNG")
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Наполнение блоков: склад, заказы, накладные, возвраты, финансы, напоминания.
#
# Всё через живое API и повторно запускаемо: товар ищется по артикулу, клиент —
# по имени, статья — по названию, и найденное не заводится второй раз. Засев
# на пробе гоняют не один раз, и после второго прогона экраны не должны
# показывать двух «Ольг Ветровых».

DEN = timedelta(days=1)


def _kogda(dney_nazad: int, chas: int = 11) -> str:
    return (datetime.now(timezone.utc) - dney_nazad * DEN).replace(
        hour=chas, minute=0, second=0, microsecond=0
    ).isoformat()


def _vklyuchit_bloki(client: httpx.Client) -> None:
    # Порядок по зависимостям: заказы и накладные просят бланки, склад — заявки.
    for klyuch in ("documents", "warehouse", "orders", "waybills", "finance", "tasks"):
        client.post(f"/modules/{klyuch}", json={"enabled": True}).raise_for_status()
    print("блоки: бланки, склад, заказы, накладные, финансы, напоминания включены")


def _sklad(client: httpx.Client) -> int:
    sklady = client.get("/warehouses").json()["items"]
    return next((w["id"] for w in sklady if w.get("is_default")), sklady[0]["id"])


TOVARY = [
    # (артикул, название, ед., цена, себестоимость, порог, приход)
    ("LOGO-PACK", "Пакет логотипа (векторы, гайд)", "pcs", 45_000_00, 12_000_00, 0, 0),
    ("CUP-350", "Стакан бумажный 350 мл, брендированный", "pcs", 18_00, 9_00, 500, 2400),
    ("BAG-KRAFT", "Пакет крафт с печатью", "pcs", 35_00, 16_00, 300, 900),
    ("CARD-VIS", "Визитки, 100 шт.", "pack", 900_00, 380_00, 10, 40),
    ("SIGN-LED", "Вывеска световая 1200×400", "pcs", 38_000_00, 21_000_00, 1, 2),
    ("MENU-A4", "Обложка меню А4, кожзам", "pcs", 1_200_00, 540_00, 20, 18),
    ("STICK-R", "Наклейки круглые 50 мм, 500 шт.", "pack", 650_00, 210_00, 5, 12),
    ("TEE-BLK", "Футболка чёрная с печатью", "pcs", 1_100_00, 480_00, 30, 64),
]


def _tovary(client: httpx.Client, sklad_id: int) -> dict[str, dict]:
    """Товары по артикулу; у новых — приход задним числом, чтобы история движений
    и «продано за 30 дней» не были пустыми."""
    po_sku: dict[str, dict] = {}
    for sku, name, unit, price, cost, porog, prikhod in TOVARY:
        naydeno = client.get("/warehouse/products", params={"search": sku}).json()["items"]
        tovar = next((p for p in naydeno if p.get("sku") == sku), None)
        if tovar is None:
            tovar = client.post(
                "/warehouse/products",
                json={
                    "name": name, "sku": sku, "unit": unit, "price": price, "cost": cost,
                    "is_service": prikhod == 0 and porog == 0,
                    "min_stock": str(porog) if porog else None,
                },
            ).json()
            if prikhod:
                client.post(
                    "/warehouse/moves",
                    json={
                        "product_id": tovar["id"], "kind": "in", "quantity": str(prikhod),
                        "cost": cost, "warehouse_id": sklad_id,
                        "comment": "Первый приход от поставщика", "happened_at": _kogda(45, 10),
                    },
                ).raise_for_status()
        po_sku[sku] = tovar
    print(f"товары: {len(po_sku)}")
    return po_sku


KLIENTY = [
    ("Ольга Ветрова", "Пекарня «Утро»", "+7 921 100-20-30", "olga@utro.example", ["упаковка", "вывеска"]),
    ("Игорь Лапин", "Барбершоп «Лапа»", "+7 911 555-01-02", "igor@lapa.example", ["визитки"]),
    ("Анна Кречет", "Цветы «Кречет»", "+7 903 777-88-99", "anna@krechet.example", ["логотип", "сайт"]),
    ("Пётр Смолин", None, "+7 926 300-40-50", "smolin@example.com", ["мерч"]),
    ("Дарья Юсупова", "Йога-студия «Дыши»", "+7 917 123-45-67", "dasha@dyshi.example", ["фирстиль", "мерч"]),
    ("Сергей Громов", "Автосервис «Гром»", "+7 909 000-11-22", None, ["вывеска"]),
]


def _klienty(client: httpx.Client) -> dict[str, dict]:
    po_imeni: dict[str, dict] = {}
    for name, company, phone, email, tags in KLIENTY:
        naydeno = client.get("/clients", params={"search": name}).json()["items"]
        karta = next((c for c in naydeno if c["name"] == name), None)
        if karta is None:
            karta = client.post(
                "/clients",
                json={"name": name, "company": company, "phone": phone, "email": email, "tags": tags},
            ).json()
            zapisi = [
                ("call", "in", "Позвонил, спросил сроки и цену.", 12),
                ("note", "", "Прислали референсы, ждём смету.", 9),
                ("email", "out", "Отправили смету и договор.", 6),
                ("call", "out", "Перезвонили: смету согласовали.", 2),
            ]
            for kind, direction, body, dney in zapisi[: 2 + len(name) % 3]:
                client.post(
                    f"/clients/{karta['id']}/notes",
                    json={"kind": kind, "direction": direction, "body": body, "happened_at": _kogda(dney, 14)},
                ).raise_for_status()
        po_imeni[name] = karta
    print(f"клиенты: {len(po_imeni)}")
    return po_imeni


def _etapy(client: httpx.Client) -> tuple[list[str], str, str]:
    stages = [s for s in client.get("/pipeline/stages").json()["items"] if not s["is_archived"]]
    otkrytye = [s["key"] for s in stages if s["kind"] == "open"]
    won = next(s["key"] for s in stages if s["kind"] == "won")
    lost = next(s["key"] for s in stages if s["kind"] == "lost")
    return otkrytye, won, lost


def _zayavki(client: httpx.Client, klienty: dict[str, dict]) -> dict[str, dict]:
    """Заявки по этапам воронки, одна выиграна, одна проиграна — чтобы дашборд,
    отчёты и карточки клиентов показывали не нули."""
    otkrytye, won, lost = _etapy(client)
    zayavki = [
        ("Упаковка для пекарни «Утро»", "Ольга Ветрова", 86_000_00, 20_000_00, otkrytye[0], 5),
        ("Вывеска пекарни", "Ольга Ветрова", 42_000_00, 0, otkrytye[min(1, len(otkrytye) - 1)], 14),
        ("Визитки барбершопу", "Игорь Лапин", 2_700_00, 2_700_00, otkrytye[-1], 2),
        ("Логотип цветочной", "Анна Кречет", 55_000_00, 25_000_00, otkrytye[min(2, len(otkrytye) - 1)], 20),
        ("Мерч для студии «Дыши»", "Дарья Юсупова", 70_400_00, 30_000_00, won, -3),
        ("Вывеска автосервиса", "Сергей Громов", 40_000_00, 0, lost, -10),
        ("Футболки на мероприятие", "Пётр Смолин", 33_000_00, 0, otkrytye[0], 1),
    ]
    itog: dict[str, dict] = {}
    est = {d["title"]: d for d in client.get("/deals", params={"per_page": 200}).json()["items"]}
    for title, imya, amount, prepaid, stage, srok in zayavki:
        deal = est.get(title)
        if deal is None:
            deal = client.post(
                "/deals",
                json={
                    "title": title, "client_id": klienty[imya]["id"], "amount": amount,
                    "prepaid": prepaid or None, "due_at": _kogda(-srok, 18),
                    "description": "Заведено демо-засевом.",
                },
            ).json()
            if stage in (won, lost):
                client.post(
                    f"/deals/{deal['id']}/move",
                    json={"stage": stage, "lost_reason": "Дорого" if stage == lost else None},
                ).raise_for_status()
            elif stage != deal.get("stage"):
                client.post(f"/deals/{deal['id']}/move", json={"stage": stage}).raise_for_status()
        itog[title] = deal
    print(f"заявки: {len(itog)}")
    return itog


def _napominaniya(client: httpx.Client, klienty: dict[str, dict], zayavki: dict[str, dict]) -> None:
    """Просроченное, на сегодня, на потом и сделанное — все четыре полосы экрана."""
    est = {
        t["title"]
        for scope in ("open", "done")
        for t in client.get("/tasks", params={"scope": scope}).json()["items"]
    }
    plan = [
        ("Позвонить Ольге по макету упаковки", "Ольга Ветрова", "Упаковка для пекарни «Утро»", 2, False),
        ("Отправить смету на вывеску", "Ольга Ветрова", "Вывеска пекарни", 0, False),
        ("Согласовать цвета логотипа", "Анна Кречет", "Логотип цветочной", -1, False),
        ("Забрать визитки из типографии", "Игорь Лапин", "Визитки барбершопу", -3, False),
        ("Выставить счёт за мерч", "Дарья Юсупова", "Мерч для студии «Дыши»", 4, True),
        ("Спросить про размеры футболок", "Пётр Смолин", "Футболки на мероприятие", -7, False),
    ]
    for title, imya, zayavka, dney_nazad, sdelano in plan:
        if title in est:
            continue
        task = client.post(
            "/tasks",
            json={
                "title": title, "client_id": klienty[imya]["id"],
                "deal_id": zayavki[zayavka]["id"], "due_at": _kogda(dney_nazad, 10),
            },
        ).json()
        if sdelano:
            client.patch(f"/tasks/{task['id']}", json={"is_done": True}).raise_for_status()
    print(f"напоминания: {len(plan)}")


STATI = [
    # (название, направление, назначение)
    ("Продажи", "income", "general"),
    ("Услуги дизайна", "income", "general"),
    ("Материалы и печать", "expense", "general"),
    ("Аренда", "expense", "general"),
    ("Налоги", "expense", "tax"),
    ("Зарплата", "expense", "salary"),
]


def _finansy(client: httpx.Client, klienty: dict[str, dict]) -> dict[str, dict]:
    """Статьи, налог с оборота правилом и три месяца операций: экран финансов
    и отчёт прибыли обязаны показывать и приход, и расход, и налог."""
    est = {c["name"]: c for c in client.get("/finance/categories").json()["items"]}
    stati: dict[str, dict] = {}
    novye = False
    for name, direction, purpose in STATI:
        if name not in est:
            est[name] = client.post(
                "/finance/categories", json={"name": name, "direction": direction, "purpose": purpose}
            ).json()
            novye = True
        stati[name] = est[name]
    if novye:
        client.post(
            "/finance/rules",
            json={"name": "Налог с оборота 6%", "base": "income_percent", "rate_bp": 600,
                  "category_id": stati["Налоги"]["id"]},
        ).raise_for_status()
        operatsii = [
            ("Аренда", 25_000_00, "Аренда мастерской", None),
            ("Материалы и печать", 14_300_00, "Бумага и краска", "Ольга Ветрова"),
            ("Зарплата", 60_000_00, "Зарплата дизайнера", None),
            ("Услуги дизайна", 25_000_00, "Аванс за логотип", "Анна Кречет"),
            ("Материалы и печать", 6_800_00, "Заготовки вывески", "Ольга Ветрова"),
        ]
        for mesyats in range(3):
            for statya, amount, comment, imya in operatsii:
                client.post(
                    "/finance/operations",
                    json={
                        "category_id": stati[statya]["id"], "amount": amount, "comment": comment,
                        "client_id": klienty[imya]["id"] if imya else None,
                        "happened_at": _kogda(mesyats * 30 + 5, 12),
                    },
                ).raise_for_status()
    print(f"финансы: статей {len(stati)}")
    return stati


def _zakazy(client: httpx.Client, sklad_id: int, klienty: dict[str, dict],
            tovary: dict[str, dict], stati: dict[str, dict]) -> None:
    """Заказы во всех состояниях, накладная по заказу, возврат по отгруженному,
    оплаты по проведённым — связки блоков видны на экранах, а не в docs/21."""
    def zakaz(kind: str, imya: str | None, stroki: list[tuple[str, int]], note: str) -> dict:
        order = client.post(
            "/orders",
            json={"kind": kind, "client_id": klienty[imya]["id"] if imya else None, "note": note},
        ).json()
        for sku, skolko in stroki:
            client.post(
                f"/orders/{order['id']}/lines",
                json={"product_id": tovary[sku]["id"], "quantity": str(skolko)},
            ).raise_for_status()
        return order

    def oplatit(order: dict, imya: str, amount: int, dney: int) -> None:
        client.post(
            "/finance/payments",
            json={"amount": amount, "category_id": stati["Продажи"]["id"], "document_id": order["id"],
                  "client_id": klienty[imya]["id"], "happened_at": _kogda(dney, 15)},
        ).raise_for_status()

    # Признак прошлого засева — заказы у Дарьи: она заводится только здесь.
    if client.get("/orders", params={"client_id": klienty["Дарья Юсупова"]["id"]}).json()["items"]:
        print("заказы: уже засеяны")
        return

    # заказ поставщику принят — приход на склад
    postavka = zakaz("purchase_order", None, [("CUP-350", 1200), ("BAG-KRAFT", 400)], "Демо: поставка стаканов")
    client.post(f"/orders/{postavka['id']}/close", json={"warehouse_id": sklad_id}).raise_for_status()

    # отгружены со склада прямо заказом, оплачены
    o1 = zakaz("sales_order", "Ольга Ветрова", [("CUP-350", 600), ("BAG-KRAFT", 200)], "Демо: стаканы и пакеты")
    client.post(f"/orders/{o1['id']}/close", json={"warehouse_id": sklad_id}).raise_for_status()
    oplatit(o1, "Ольга Ветрова", 600 * 18_00 + 200 * 35_00, 3)

    o2 = zakaz("sales_order", "Игорь Лапин", [("CARD-VIS", 3)], "Демо: визитки")
    client.post(f"/orders/{o2['id']}/close", json={"warehouse_id": sklad_id}).raise_for_status()
    oplatit(o2, "Игорь Лапин", 3 * 900_00, 1)

    # отгружен накладной: накладная проведена и подтверждена, заказ закрылся сам
    o3 = zakaz("sales_order", "Дарья Юсупова", [("TEE-BLK", 40), ("STICK-R", 4)], "Демо: мерч студии")
    nakladnaya = client.post(f"/waybills/from-order/{o3['id']}").json()
    client.post(f"/waybills/{nakladnaya['id']}/post", json={}).raise_for_status()
    client.post(f"/waybills/{nakladnaya['id']}/confirm", json={"note": "Получено, претензий нет"}).raise_for_status()
    oplatit(o3, "Дарья Юсупова", 40 * 1_100_00 + 4 * 650_00, 0)

    # возврат части футболок по отгруженному заказу — с деньгами обратно
    vozvrat = client.post(f"/orders/{o3['id']}/returns").json()
    for line in client.get(f"/returns/{vozvrat['id']}").json()["lines"]:
        if line["product_id"] == tovary["TEE-BLK"]["id"]:
            client.patch(f"/returns/{vozvrat['id']}/lines/{line['id']}", json={"quantity": "3"}).raise_for_status()
        else:
            client.delete(f"/returns/{vozvrat['id']}/lines/{line['id']}").raise_for_status()
    client.patch(
        f"/returns/{vozvrat['id']}",
        json={"note": "Три футболки с браком печати, деньги вернули.", "refund": 3 * 1_100_00,
              "category_id": stati["Продажи"]["id"]},
    ).raise_for_status()
    client.post(f"/returns/{vozvrat['id']}/post", json={"warehouse_id": sklad_id}).raise_for_status()

    # открытые: собран и ждёт выдачи, только что выписан
    o4 = zakaz("sales_order", "Анна Кречет", [("LOGO-PACK", 1), ("CARD-VIS", 2)], "Демо: логотип и визитки")
    client.post(f"/orders/{o4['id']}/ready").raise_for_status()
    zakaz("sales_order", "Пётр Смолин", [("TEE-BLK", 30)], "Демо: футболки на мероприятие")
    zakaz("sales_order", "Сергей Громов", [("SIGN-LED", 1)], "Демо: вывеска")
    print("заказы: 7, накладная: 1, возврат: 1, оплаты: 3")


def _kvitantsii(client: httpx.Client, klienty: dict[str, dict]) -> None:
    """Квитанции приёмки по состояниям: экран бланков и вкладка клиента."""
    est = client.get("/documents", params={"kind": "intake", "per_page": 200}).json()["items"]
    if any(((d.get("payload") or {}).get("item") or "").startswith("Демо:") for d in est):
        print("квитанции: уже засеяны")
        return
    plan = [
        ("Ольга Ветрова", "Демо: макет вывески на согласование", "issued"),
        ("Сергей Громов", "Демо: старая вывеска на реставрацию", "in_progress"),
        ("Игорь Лапин", "Демо: рамка для сертификата", "ready"),
        ("Анна Кречет", "Демо: штамп с логотипом", "closed"),
    ]
    put = ["in_progress", "ready", "closed"]
    for imya, item, status in plan:
        doc = client.post(
            "/documents",
            json={"client_id": klienty[imya]["id"], "item": item, "condition": "Без повреждений", "locale": "ru"},
        ).json()
        for shag in put[: put.index(status) + 1] if status != "issued" else []:
            client.post(f"/documents/{doc['id']}/status", json={"status": shag}).raise_for_status()
    print(f"квитанции: {len(plan)}")


def napolnit_bloki(client: httpx.Client) -> None:
    _vklyuchit_bloki(client)
    sklad_id = _sklad(client)
    tovary = _tovary(client, sklad_id)
    klienty = _klienty(client)
    zayavki = _zayavki(client, klienty)
    _napominaniya(client, klienty, zayavki)
    stati = _finansy(client, klienty)
    _zakazy(client, sklad_id, klienty, tovary, stati)
    _kvitantsii(client, klienty)


def main() -> None:
    client = httpx.Client(base_url=API, timeout=60)

    # вход root (при первом запуске — обязательная смена пароля)
    response = client.post("/auth/login", json={"email": ROOT_EMAIL, "password": ROOT_INITIAL})
    if response.status_code == 200:
        client.headers["X-CSRF-Token"] = client.cookies.get("opencrm_csrf", "")
        client.post(
            "/auth/me/password",
            json={"old_password": ROOT_INITIAL, "new_password": ROOT_PASSWORD},
        ).raise_for_status()
        print(f"root: пароль сменён на {ROOT_PASSWORD}")
    else:
        response = client.post("/auth/login", json={"email": ROOT_EMAIL, "password": ROOT_PASSWORD})
        response.raise_for_status()
        client.headers["X-CSRF-Token"] = client.cookies.get("opencrm_csrf", "")
        print("root: вход по демо-паролю")

    # бренд студии
    client.patch(
        "/settings",
        json={
            "values": {
                "brand_name": "Форма и Свет",
                "contact_email": "hello@forma-svet.example",
                "contact_phone": "+7 900 000-00-00",
                "social_telegram": "https://t.me/example",
                "showcase_locale": "ru",
            }
        },
    ).raise_for_status()
    print("настройки сайта: бренд задан")

    # клиент CRM — по имени, чтобы второй прогон не завёл вторую Марию
    naydeno = client.get("/clients", params={"search": "Мария Соколова"}).json()["items"]
    mariya = next((c for c in naydeno if c["name"] == "Мария Соколова"), None)
    if mariya is None:
        created = client.post(
            "/clients",
            json={
                "name": "Мария Соколова",
                "company": "Кофейня «Брусника»",
                "phone": "+7 912 345-67-89",
                "email": "maria@brusnika.example",
                "tags": ["логотип", "фирстиль"],
            },
        )
        created.raise_for_status()
        mariya = created.json()
        client.post(
            f"/clients/{mariya['id']}/notes",
            json={"kind": "call", "body": "Обсудили айдентику: тёплая палитра, без засечек в лого."},
        ).raise_for_status()
    client_id = mariya["id"]
    print(f"клиент: id={client_id}")

    doski = {b["title"]: b for b in client.get("/boards", params={"per_page": 200}).json()["items"]}
    if "Айдентика для «Брусники»" in doski and "Закрытый превью-раунд" in doski:
        print("доски: уже есть")
        napolnit_bloki(client)
        print("\nГотово.")
        return

    # открытая доска с работами
    board = client.post(
        "/boards",
        json={
            "title": "Айдентика для «Брусники»",
            "description": "Логотип, палитра и носители фирменного стиля — первый раунд.",
            "client_id": client_id,
        },
    ).json()
    board_id = board["id"]
    sizes = [(1200, 900), (900, 1200), (1200, 1200), (1400, 900), (900, 1300),
             (1200, 800), (1000, 1000), (1300, 900)]
    titles = [
        "Логотип — основной знак", "Вертикальная версия", "Паттерн для упаковки",
        "Визитки", "Вывеска", "Стаканы и пакеты", "Палитра", "Обложка меню",
    ]
    for index, ((w, h), title) in enumerate(zip(sizes, titles)):
        upload = client.post(
            f"/boards/{board_id}/works",
            files={"file": (f"work-{index}.png", art_image(index * 7 + 3, w, h), "image/png")},
        )
        upload.raise_for_status()
        work_id = upload.json()["id"]
        client.patch(
            f"/boards/{board_id}/works/{work_id}", json={"title": title}
        ).raise_for_status()
    client.patch(f"/boards/{board_id}", json={"is_published": True}).raise_for_status()
    share = client.post(f"/boards/{board_id}/shares", json={}).json()
    print(f"доска опубликована: {share['url']}")

    # доска с PIN
    pin_board = client.post(
        "/boards",
        json={"title": "Закрытый превью-раунд", "description": "Только для клиента."},
    ).json()
    for index in range(3):
        client.post(
            f"/boards/{pin_board['id']}/works",
            files={"file": (f"pin-{index}.png", art_image(100 + index, 1100, 900), "image/png")},
        ).raise_for_status()
    client.patch(f"/boards/{pin_board['id']}", json={"is_published": True}).raise_for_status()
    pin_share = client.post(f"/boards/{pin_board['id']}/shares", json={"pin": "4821"}).json()
    print(f"доска с PIN (4821): {pin_share['url']}")

    napolnit_bloki(client)

    print("\nГотово.")
    print(f"открытая витрина:  {share['url']}")
    print(f"витрина с PIN:     {pin_share['url']} (код 4821)")


if __name__ == "__main__":
    try:
        main()
    except httpx.HTTPStatusError as exc:
        print("Ошибка API:", exc.response.status_code, exc.response.text)
        sys.exit(1)
