"""Справочник ручек API сайта на экране документации свеж относительно `docs/04-api.md`.

Экран показывает ручки API сайта одним списком, и список этот порождён скриптом
`scripts/spravochnik_api.py` из справочника. Порождённое живёт в репозитории,
а не строится при сборке: сборка образа идёт без `docs/`-зависимостей у
фронтенда, и незаметно устаревший файл показал бы читателю ручки полугодовой
давности с уверенным видом. Здесь ловится ровно это: правка справочника без
перезапуска скрипта.
"""

import pathlib
import sys

KOREN = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KOREN))

from scripts import spravochnik_api  # noqa: E402


def test_porozhdyonnyy_fayl_sovpadaet_so_spravochnikom():
    """Правка docs/04-api.md без перезапуска скрипта — красный тест, а не тихая старость."""
    ozhidaetsya = spravochnik_api.porodit(spravochnik_api.SPRAVOCHNIK.read_text(encoding="utf-8"))
    est = spravochnik_api.VYKHOD.read_text(encoding="utf-8")
    assert est == ozhidaetsya, (
        "справочник на экране отстал от docs/04-api.md — запусти "
        "`python scripts/spravochnik_api.py`"
    )


def test_razbor_vidit_kazhduyu_ruchku_spravochnika():
    """Страховка разбора: пустой список сравнил бы пустоту с пустотой.

    Считаем те же строки, что и сторож полноты `test_api_dokumentirovan`:
    ручка, названная в документе, обязана дойти до экрана.
    """
    tekst = spravochnik_api.SPRAVOCHNIK.read_text(encoding="utf-8")
    razdely = spravochnik_api.razobrat(tekst)
    ruchki = [h for r in razdely for h in r["ruchki"]]
    assert len(ruchki) > 250, f"ручек разобрано {len(ruchki)} — разбор сломался"
    assert len(razdely) > 20, f"разделов разобрано {len(razdely)} — заголовки не читаются"
    assert any(h["put"] == "/deals/{id}/lines" for h in ruchki)
    assert any(h["put"] == "/site/orders" and h["vid"] == "klyuch" for h in ruchki)
    assert any(h["put"] == "/b/{token}" and h["vne_api"] for h in ruchki)


def test_na_ekran_idyot_tolko_api_sayta():
    """Внутренние ручки CRM внешнему не нужны; на экране — то, что зовут по ключу.

    Владелец снял весь внутренний API с экрана 05.09.2026: справочник для
    сайтов и маркетплейсов, а не для экрана сотрудника.
    """
    tekst = spravochnik_api.SPRAVOCHNIK.read_text(encoding="utf-8")
    gruppy = spravochnik_api.dlya_sayta(spravochnik_api.razobrat(tekst))
    ruchki = [h for g in gruppy for h in g["ruchki"]]
    puti = {h["put"] for h in ruchki}
    assert "/site/catalog" in puti and "/site/orders" in puti
    assert "/media/product/{filename}" in puti, "снимок товара — часть API сайта"
    assert not any(h["vid"] in ("pravo", "sotrudnik") for h in ruchki), (
        "на экран попала ручка под сессией или правом: " + str([h["put"] for h in ruchki if h["vid"] in ("pravo", "sotrudnik")])
    )
    assert not any(p.startswith("/settings") or p == "/live" or p.startswith("/deals") for p in puti)
    assert next(h for h in ruchki if h["put"].startswith("/media/"))["vne_api"] is True
    assert [g["nazvanie"] for g in gruppy] == ["Чтение", "Запись", "Снимок товара"]


def test_dostup_razobran_a_ne_ostavlen_znachkom():
    """Значки 🔓/👤/🔑 — обозначения документа; экран показывает их подписью.

    Непонятый вид попадает в `inoe` и уходит на экран сырым текстом. Их
    наперечёт — это ручки без сессии с прозаическим описанием защиты, — и
    новый такой должен быть решением, а не следом опечатки в значке.
    """
    tekst = spravochnik_api.SPRAVOCHNIK.read_text(encoding="utf-8")
    inoe = sorted(
        h["put"] for r in spravochnik_api.razobrat(tekst) for h in r["ruchki"] if h["vid"] == "inoe"
    )
    assert inoe == [
        "/alerts/ready",
        "/alerts/webhook",
        "/arcade/leaderboard",
        "/arcade/scores",
        "/media/product/{filename}",
        "/metrics",
    ], f"доступ не разобран у: {inoe}"
