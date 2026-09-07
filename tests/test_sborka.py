"""Открытая вкладка узнаёт, что вышло обновление.

**Беда.** Боевой сервер обновляется сам. Новый `index.html` браузер переспросит
— он отдаётся с `no-cache`, — но только при ЗАГРУЗКЕ страницы. У того, у кого
CRM просто открыта, продолжает работать вчерашняя сборка: она уже в памяти
вкладки, и обновляться ей неоткуда.

Дальше по-разному: старый экран либо отстаёт от нового сервера и показывает
неправду, либо сошлётся на файл, который обновление унесло, и встанет насмерть.
И то и другое человек читает как «программа сломалась», а лечится нажатием F5,
о котором надо догадаться. Найдено владельцем 07.09.2026 на боевом сервере.

Разбор — `web/sborka.py`.
"""

from tests.conftest import API
from web import sborka


def test_otmetka_beryotsya_iz_imeni_sobrannogo_skripta():
    """Хэш в имени файла меняется ровно тогда, когда меняется фронтенд.

    Дата сборки или номер коммита не годятся: первая меняется от пересборки без
    единой правки, второй — от правки сервера, которой экрану знать незачем.
    Лишняя смена отметки — это полоса «вышло обновление» на ровном месте, а её
    показывают всем и сразу.
    """
    otmetka = sborka.otmetka()
    assert otmetka, "отметка сборки пуста — по ней не отличить обновление от потери заголовка"
    assert otmetka == sborka.otmetka(), "отметка меняется от вызова к вызову"
    if otmetka != sborka.BEZ_SBORKI:
        assert otmetka.startswith("index-") and otmetka.endswith(".js"), otmetka


def test_otvet_api_nesyot_otmetku_sborki(root_client):
    """Заголовком на каждом ответе API, а не своей ручкой.

    Своя ручка означала бы опрос: запрос в минуту с каждой открытой вкладки
    ради «не вышло ли обновление». Экран и так ходит в API постоянно.
    """
    otvet = root_client.get(f"{API}/auth/me")
    assert otvet.status_code == 200, otvet.text
    assert otvet.headers.get("X-OpenCRM-Build") == sborka.otmetka()


def test_stranitsy_vne_api_otmetkoy_ne_obveshivayutsya(root_client):
    """Витрина и бланки открываются посторонними, и версия сборки CRM — не их
    дело: лишний заголовок наружу рассказывает о внутренностях, ничего не давая."""
    otvet = root_client.get("/healthz")
    assert "X-OpenCRM-Build" not in otvet.headers


def test_ekran_sravnivaet_otmetku_a_ne_prosto_zapominaet():
    """Сторож на разбор: полоса обязана всплывать по СМЕНЕ, а не по первому
    ответу. Иначе она показывалась бы каждому при входе."""
    import pathlib

    koren = pathlib.Path(__file__).resolve().parent.parent
    api = (koren / "web" / "frontend" / "crm" / "src" / "lib" / "api.ts").read_text(encoding="utf-8")
    assert "X-OpenCRM-Build" in api, "экран перестал читать отметку сборки"
    assert "if (!sborka)" in api, "первая отметка больше не запоминается молча"
    assert "otmetka !== sborka" in api, "сравнения отметок больше нет"
