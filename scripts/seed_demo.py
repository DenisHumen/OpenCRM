"""Демо-данные через реальное API (живой прогон бэкенда).

Запуск: сервер должен работать на http://localhost:8000
    .venv\\Scripts\\python.exe scripts\\seed_demo.py
"""

import io
import math
import random
import sys

import httpx
from PIL import Image, ImageDraw

BASE = "http://localhost:8000"
API = f"{BASE}/api/v1"

ROOT_EMAIL = "root@opencrm.local"
ROOT_INITIAL = "root-changeme"
ROOT_PASSWORD = "demo-root-password-1"

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

    # клиент CRM
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
    client_id = created.json()["id"]
    client.post(
        f"/clients/{client_id}/notes",
        json={"kind": "call", "body": "Обсудили айдентику: тёплая палитра, без засечек в лого."},
    ).raise_for_status()
    print(f"клиент создан: id={client_id}")

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

    print("\nГотово.")
    print(f"открытая витрина:  {share['url']}")
    print(f"витрина с PIN:     {pin_share['url']} (код 4821)")


if __name__ == "__main__":
    try:
        main()
    except httpx.HTTPStatusError as exc:
        print("Ошибка API:", exc.response.status_code, exc.response.text)
        sys.exit(1)
