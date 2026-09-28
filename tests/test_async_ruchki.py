"""Ручка `async def` работает в самом цикле событий процесса.

Синхронная база, Redis, запись на диск или разжатие картинки в ней останавливают
все запросы процесса разом, включая `/healthz`, по которому обновление решает об
откате. Так стояли загрузки файлов и вебхуки (разбор 28.09.2026). Ручка `def`
уходит в пул потоков сама; тело запроса ей даёт зависимость `telo_zaprosa`.
"""

import inspect
import threading
import time

from fastapi.routing import APIRoute, iter_route_contexts

from tests.conftest import API
from web.main import app

#: Ручки, которым цикл событий нужен по делу, и почему они его не держат.
ASYNC_RAZRESHENY: dict[str, str] = {
    "/api/v1/live": "поток живых обновлений: Redis и база — через `asyncio.to_thread`",
    "/api/v1/telegram/stream": "поток мессенджера: подписка и выборка — через `asyncio.to_thread`",
    "/api/v1/system/monitoring": "опрос соседних контейнеров асинхронным `httpx`",
}


def _async_ruchki() -> dict[str, str]:
    return {
        rc.path: rc.original_route.endpoint.__name__
        for rc in iter_route_contexts(app.routes)
        if isinstance(rc.original_route, APIRoute)
        and inspect.iscoroutinefunction(rc.original_route.endpoint)
    }


def test_async_ruchki_tolko_iz_spiska():
    lishnie = sorted(set(_async_ruchki()) - set(ASYNC_RAZRESHENY))
    assert not lishnie, f"async-ручки держат цикл событий: {lishnie} — сделать `def`"


def test_spisok_ne_zarastaet():
    """Строка списка без ручки страхует чужой будущий адрес — её надо убрать."""
    assert set(ASYNC_RAZRESHENY) <= set(_async_ruchki())


def test_zagruzka_ne_ostanavlivaet_ostalnye_zaprosy(base_client, manager_client, monkeypatch):
    """Медленная приёмка файла клиента не держит `/healthz` соседнего запроса.

    Оба запроса — через `base_client`: у него один цикл событий на все запросы,
    как у боевого процесса. У прочих клиентов цикл свой на каждый запрос, и
    остановленный цикл на них не виден вовсе.
    """
    from core.services import client_service

    klient = manager_client.post(f"{API}/clients", json={"name": "Медленная загрузка"}).json()
    nastoyashchaya = client_service.add_file

    def medlenno(*args, **kwargs):
        time.sleep(1.5)
        return nastoyashchaya(*args, **kwargs)

    monkeypatch.setattr(client_service, "add_file", medlenno)
    otvety = []
    zagruzka = threading.Thread(
        target=lambda: otvety.append(
            base_client.post(
                f"{API}/clients/{klient['id']}/files",
                files={"file": ("brief.pdf", b"%PDF-1.4 x", "application/pdf")},
                cookies=dict(manager_client.cookies),
                headers={"X-CSRF-Token": manager_client.headers["X-CSRF-Token"]},
            ).status_code
        )
    )
    bylo = dict(base_client.cookies)
    try:
        zagruzka.start()
        time.sleep(0.3)
        nachalo = time.perf_counter()
        assert base_client.get("/healthz").status_code == 200
        zhdali = time.perf_counter() - nachalo
        zagruzka.join()
    finally:
        # Клиент общий на весь набор и анонимный: cookie сотрудника в нём сделали
        # бы соседние проверки «без входа» проверками под входом.
        base_client.cookies.clear()
        base_client.cookies.update(bylo)
    assert otvety == [201]
    assert zhdali < 0.8, f"/healthz ждал загрузку {zhdali:.2f} с"
