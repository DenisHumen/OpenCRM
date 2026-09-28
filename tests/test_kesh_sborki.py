"""Кэш сборки на экране «Обслуживание» (docs/ekspluatatsiya/08-razvyortyvanie.md, «Кэш сборки»).

Экран только просит и читает; чистит служба обновления на хосте. Связь — файлы
в общем `data/`, поэтому последний тест гонит просьбу через настоящий обновлятор.
"""

import json

import pytest

from core.services import kesh_sborki_service as svc
from deploy import kesh
from tests.conftest import API
from tests.test_autoupdate import DF_DO, DF_POSLE, FakeShell, _df_po_ocheredi, make_updater
from tests.test_roles import role_maker, staff_maker  # noqa: F401 — фикстуры

KESH = f"{API}/system/build-cache"


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCRM_DATA_DIR", str(tmp_path / "data"))
    return tmp_path / "data"


def test_imena_faylov_sovpadayut_s_obnovlyatorom():
    """Разойдись имя — просьба лежала бы там, куда служба не смотрит, и молча."""
    assert (svc.PAPKA, svc.SOSTOYANIE, svc.ZAPROS) == (kesh.PAPKA, kesh.SOSTOYANIE, kesh.ZAPROS)


def test_bez_sluzhby_chistka_ne_obeshchaetsya(root_client, data):
    otvet = root_client.get(KESH).json()
    assert otvet["sluzhba"] is False and otvet["zapros"] is None


def test_prosba_kladyotsya_odin_raz_i_pishetsya_v_zhurnal(root_client, data):
    pervaya = root_client.post(f"{KESH}/purge")
    assert pervaya.status_code == 202, pervaya.text
    prosba = json.loads((data / kesh.PAPKA / kesh.ZAPROS).read_text(encoding="utf-8"))
    assert prosba["kto"] and prosba["at"].endswith("Z")
    assert root_client.post(f"{KESH}/purge").json()["zapros"]["at"] == prosba["at"]
    zapisi = root_client.get(f"{API}/audit", params={"search": "build-cache"}).json()["items"]
    assert sum(z["action"] == "build_cache.purge_requested" for z in zapisi) >= 1


def test_zabytaya_prosba_vidna(root_client, data):
    (data / kesh.PAPKA).mkdir(parents=True)
    (data / kesh.PAPKA / kesh.ZAPROS).write_text('{"at": "2020-01-01T00:00:00Z", "kto": "x"}', encoding="utf-8")
    assert root_client.get(KESH).json()["zapros_zabyt"] is True


def test_chistka_tolko_s_pravom_nastroek(root_client, role_maker, staff_maker, data):  # noqa: F811
    rol = role_maker("Кэш — без настроек", ["tasks.view"])
    anna = staff_maker("kesh-anna@test.local", rol["id"])
    assert anna.get(KESH).status_code == 403
    assert anna.post(f"{KESH}/purge").status_code == 403
    assert not (data / kesh.PAPKA / kesh.ZAPROS).exists()


def test_prosba_s_ekrana_dohodit_do_sluzhby_i_obratno(root_client, tmp_path, monkeypatch):
    """Экран кладёт просьбу, служба чистит и пишет итог, экран его показывает."""
    shell = FakeShell()
    _df_po_ocheredi(shell, DF_DO, DF_POSLE)
    updater = make_updater(tmp_path, shell=shell)
    monkeypatch.setenv("OPENCRM_DATA_DIR", str(updater.config.data_dir))

    assert root_client.post(f"{KESH}/purge").status_code == 202
    updater._podozhdat(15)

    otvet = root_client.get(KESH).json()
    assert otvet["sluzhba"] is True and otvet["zapros"] is None
    assert otvet["razmer"] == 4_900_000_000
    assert otvet["poslednyaya"]["kak"] == "vruchnuyu" and otvet["poslednyaya"]["ok"] is True
    assert "очищен вручную" in updater.notifier.messages[-1]
