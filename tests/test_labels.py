"""Наклейка: состав полей задан РЕЕСТРОМ, и реестр не расходится с остальными.

ПОЧЕМУ ЭТОТ ФАЙЛ ПОЯВИЛСЯ. Состав наклейки — это то, что у каждого дела своё:
мастерской нужна единица измерения, магазину цена, складу порог заказа. Угадать
набор один раз нельзя, и дописывать его придётся ещё не раз, — поэтому поля
живут одной записью в `core/services/barcode_service.POLYA_NAKLEYKI`, а всё
остальное подхватывается само.

«Само» держится ровно на этих проверках. Реестр связан с четырьмя местами, и
все четыре молчат при расхождении:

  * `SETTING_DEFAULTS` — не досеяли ключ, и настройка не сохранится;
  * `i18n.ts` — нет перевода, и на экране висит сам ключ (`labelField_unit`);
  * `LabelSettings.tsx` — переключателя нет, и поле включить нечем;
  * шаблон печати — зона не отрисована, и включённое поле не печатается.

Ни одно из этих расхождений не роняет ни одного теста и не пишет ни строчки в
лог. Увидеть их можно только на отпечатанной ленте — то есть уже потратив её.
"""

import io
import itertools
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.services import barcode_service, codes, modules_service
from core.services.barcode_service import (
    POLYA_NAKLEYKI,
    ZONA_BOK,
    ZONA_NIZ,
    ZONA_STROKA,
    ZONA_VERH,
    nastroyka_polya,
)
from database.models.settings import SETTING_DEFAULTS

KOREN = Path(__file__).resolve().parent.parent
I18N = KOREN / "web" / "frontend" / "crm" / "src" / "lib" / "i18n.ts"
EKRAN = KOREN / "web" / "frontend" / "crm" / "src" / "screens" / "LabelSettings.tsx"
SHABLON = KOREN / "web" / "public" / "templates" / "label_print.html"

#: Имена товаров обязаны быть разными: база у прогона общая.
_counter = itertools.count(1)


def _chitat(put: Path) -> str:
    return io.open(put, encoding="utf-8").read()


# --- реестр не расходится с соседями ------------------------------------------


def test_u_kazhdogo_polya_est_znachenie_po_umolchaniyu():
    """Ключ настройки обязан быть в `SETTING_DEFAULTS`.

    Без него настройка не сеется, а `settings_service.update` пишет только
    известные ключи — то есть переключатель на экране нажимался бы, а после
    перезагрузки возвращался обратно. Молча.
    """
    ne_hvataet = [
        pole.klyuch for pole in POLYA_NAKLEYKI if nastroyka_polya(pole.klyuch) not in SETTING_DEFAULTS
    ]
    assert not ne_hvataet, (
        f"поля реестра без значения по умолчанию: {', '.join(ne_hvataet)}. "
        "Переключатель на экране будет нажиматься и возвращаться обратно"
    )


def test_umolchaniya_reestra_i_nastroek_govoryat_odno():
    """Одно и то же поле не может быть включено в реестре и выключено в настройках.

    Разойдись они — включённость поля зависела бы от того, сеялись настройки до
    или после его появления, то есть от возраста установки. Две установки одной
    версии печатали бы разные наклейки.
    """
    raznitsa = []
    for pole in POLYA_NAKLEYKI:
        v_nastroykah = SETTING_DEFAULTS[nastroyka_polya(pole.klyuch)] == "1"
        if v_nastroykah != pole.po_umolchaniyu:
            raznitsa.append(f"{pole.klyuch}: реестр {pole.po_umolchaniyu}, настройки {v_nastroykah}")
    assert not raznitsa, "; ".join(raznitsa)


def test_u_kazhdogo_polya_est_perevod_na_oboikh_yazykakh():
    """Название и пояснение — на обоих языках.

    Забытая строка ничего видимо не ломает: на экране появится сам ключ
    (`labelField_unit`), и заметит это только тот, кто откроет настройки на
    этом языке.
    """
    text = _chitat(I18N)
    propuski = []
    for pole in POLYA_NAKLEYKI:
        for pristavka in ("labelField_", "labelFieldHint_"):
            klyuch = f"{pristavka}{pole.klyuch}"
            # По две штуки: словарей в файле два, английский и русский.
            skolko = len(re.findall(rf"^\s+{re.escape(klyuch)}:", text, re.M))
            if skolko < 2:
                propuski.append(f"{klyuch} ({skolko} из 2)")
    assert not propuski, (
        "нет перевода: " + ", ".join(propuski) + ". На экране настроек будет виден сам ключ"
    )


def test_u_kazhdoy_zony_est_podpis():
    """Зона без подписи оставляет группу переключателей без заголовка."""
    text = _chitat(I18N)
    for zona in (ZONA_VERH, ZONA_STROKA, ZONA_NIZ, ZONA_BOK):
        assert len(re.findall(rf"^\s+labelZone_{zona}:", text, re.M)) >= 2, (
            f"зона {zona} без подписи хотя бы на одном языке"
        )


def test_ekran_beryot_spisok_poley_u_servera():
    """Какие поля есть, в каком порядке и в какой зоне — решает реестр.

    Собери экран список сам — добавление поля означало бы правку и там, а
    забытая строка дала бы поле, которое есть в настройках и не показывается
    никогда.
    """
    text = _chitat(EKRAN)
    assert "/labels/settings" in text, "экран не спрашивает список полей у сервера"


def test_podpisi_na_ekrane_sovpadayut_s_reestrom():
    """Подписи полей перечислены на экране буквами, и перечень не расходится.

    Буквами — намеренно: ключ, собранный шаблоном из имени поля, делает слепой
    проверку мёртвых переводов (), и она об этом прямо
    предупреждает. Цена — одна строка на поле; чтобы её нельзя было забыть,
    сверяем перечни В ОБЕ СТОРОНЫ.

    Забытая запись — это поле без подписи на экране. Лишняя — подпись поля,
    которого больше нет: перевод к ней живёт, его переводят на второй язык, и
    показывать его некому.
    """
    text = _chitat(EKRAN)
    kusok = re.search(r"POLE_PODPIS[^{]*\{(.*?)\n\};", text, re.S)
    assert kusok, "на экране не нашлось перечня подписей POLE_PODPIS"
    na_ekrane = set(re.findall(r"^\s{2}([a-z][a-z0-9_]*):", kusok.group(1), re.M))
    v_reestre = {pole.klyuch for pole in POLYA_NAKLEYKI}

    assert not (v_reestre - na_ekrane), (
        f"поля без подписи на экране: {sorted(v_reestre - na_ekrane)}. "
        "Переключатель будет подписан внутренним ключом"
    )
    assert not (na_ekrane - v_reestre), (
        f"подписи полей, которых нет в реестре: {sorted(na_ekrane - v_reestre)}. "
        "Их перевод показывать некому, а переводить на второй язык будут"
    )


def test_shablon_pechataet_vse_zony():
    """Зона, которой нет в шаблоне, — это поле, которое включается и не печатается."""
    text = _chitat(SHABLON)
    for zona in (ZONA_VERH, ZONA_STROKA, ZONA_NIZ):
        assert f"item.{zona}" in text, (
            f"шаблон не печатает зону {zona}: поле в ней можно включить, а на ленте его не будет"
        )
    assert "item.qr" in text, "QR некуда встать: зона `bok` в шаблоне не нарисована"


def test_kluchi_polya_prigodny_dlya_imeni_nastroyki():
    """Ключ уезжает в имя настройки и в ключ перевода — значит только простые знаки."""
    plohie = [pole.klyuch for pole in POLYA_NAKLEYKI if not re.fullmatch(r"[a-z][a-z0-9_]*", pole.klyuch)]
    assert not plohie, f"ключи, непригодные для имени настройки: {plohie}"


def test_zony_polya_iz_izvestnykh():
    """Незнакомая зона — это поле, которое не встанет никуда."""
    izvestnye = {ZONA_VERH, ZONA_STROKA, ZONA_NIZ, ZONA_BOK}
    chuzhie = [(p.klyuch, p.zona) for p in POLYA_NAKLEYKI if p.zona not in izvestnye]
    assert not chuzhie, f"поля в несуществующих зонах: {chuzhie}"


# --- значения полей -----------------------------------------------------------


class _Tovar:
    """Товар ровно с теми полями, что читают функции реестра."""

    def __init__(self, **kwargs):
        self.id = kwargs.get("id", 1)
        self.name = kwargs.get("name", "Матрица 15.6")
        self.sku = kwargs.get("sku", "MTX-156")
        self.unit = kwargs.get("unit", "pcs")
        self.price_minor = kwargs.get("price_minor", 349900)
        self.min_stock_milli = kwargs.get("min_stock_milli", None)
        self.note = kwargs.get("note", "")


class _Kod:
    """Основной код товара — ровно с тем, что читают функции реестра."""

    def __init__(self, code: str = "20000127", pack_size_milli: int = 1000):
        self.code = code
        self.pack_size_milli = pack_size_milli


SREDA = {
    "currency": "UAH",
    "company": "Мастерская",
    "base_url": "https://crm.example.com",
    "printed_at": "20.08.2026",
    "units": barcode_service.UNIT_NAMES["ru"],
    "t_min": "мин",
}


def _pole(klyuch: str):
    return next(p for p in POLYA_NAKLEYKI if p.klyuch == klyuch)


def test_shtuki_ne_pechatayutsya():
    """«шт» на наклейке не значит ничего: штука подразумевается по умолчанию.

    Печатать её — тратить миллиметры на слово, которое не сообщает нового. А
    «кг» или «м» меняют смысл всей позиции.
    """
    assert _pole("unit").znachenie(_Tovar(unit="pcs"), _Kod(), SREDA) == ""
    assert _pole("unit").znachenie(_Tovar(unit="kg"), _Kod(), SREDA) == "кг"


def test_dlinnaya_zametka_obrezaetsya():
    """Длинная заметка выталкивает штрихкод за край — и это видно только на ленте."""
    dlinnaya = "очень подробная заметка про этот товар " * 10
    vyshlo = _pole("note").znachenie(_Tovar(note=dlinnaya), _Kod(), SREDA)
    assert len(vyshlo) <= barcode_service.MAX_NOTE_ON_LABEL
    assert vyshlo, "заметка пропала целиком"


def test_perevody_strok_v_zametke_ne_edut_na_nakleyku():
    """Заметку пишут в несколько строк, а на наклейке строка одна."""
    vyshlo = _pole("note").znachenie(_Tovar(note="первая\nвторая\n\nтретья"), _Kod(), SREDA)
    assert "\n" not in vyshlo


def test_porog_zakaza_nazyvaetsya_slovom():
    """Голое число на наклейке не значит ничего — нужна приставка."""
    vyshlo = _pole("min_stock").znachenie(_Tovar(min_stock_milli=5000), _Kod(), SREDA)
    assert vyshlo == "мин 5", vyshlo
    assert _pole("min_stock").znachenie(_Tovar(min_stock_milli=None), _Kod(), SREDA) == ""


def test_tsena_schitaetsya_tselymi():
    """Деньги в этом проекте через float не проходят нигде, и наклейка не исключение."""
    assert _pole("price").znachenie(_Tovar(price_minor=349900), _Kod(), SREDA) == "3499.00 UAH"
    assert _pole("price").znachenie(_Tovar(price_minor=None), _Kod(), SREDA) == ""


def test_qr_vedyot_na_kartochku_tovara():
    """Смысл QR в том, чтобы стоящий у стеллажа навёл телефон и увидел карточку."""
    vyshlo = _pole("qr").znachenie(_Tovar(id=42), _Kod(), SREDA)
    assert vyshlo.lstrip().startswith("<svg"), "QR не отрисовался"


def test_bez_adresa_sayta_qr_ne_pechataetsya():
    """Ссылка на localhost в QR бесполезна: телефон по ней не попадёт никуда.

    Лучше не напечатать код, чем напечатать нерабочий: нерабочий человек
    попробует трижды и решит, что сломан сканер.
    """
    sreda = dict(SREDA, base_url="")
    assert _pole("qr").znachenie(_Tovar(), _Kod(), sreda) == ""


@pytest.mark.parametrize("locale", ["ru", "uk", "en"])
def test_u_kazhdogo_yazyka_est_edinitsy_i_pristavka(locale):
    """Незнакомый язык оставил бы наклейку с внутренними кодами единиц."""
    assert barcode_service.UNIT_NAMES[locale]["kg"]
    assert barcode_service.MIN_STOCK_PREFIX[locale]


def test_upakovka_v_odnu_shtuku_ne_pechataetsya():
    """Упаковка в одну штуку — обычный случай, и говорить о нём нечего."""
    assert _pole("pack").znachenie(_Tovar(), _Kod(pack_size_milli=1000), SREDA) == ""


def test_blok_iz_desyati_nazyvaetsya_na_nakleyke():
    """Две наклейки выглядели одинаково, а значили разное.

    Размер упаковки записан у КОДА, а не у товара: один товар имеет код на
    штуку и код на блок из десяти. Отсканировали блок — в заказ ушло десять, и
    это правильно; но понять, какую наклейку клеить на коробку, было неоткуда.
    """
    assert _pole("pack").znachenie(_Tovar(), _Kod(pack_size_milli=10000), SREDA) == "×10"


def test_tovar_bez_koda_ne_ronyaet_polya():
    """У товара без штрихкода кода нет вовсе, а поля всё равно считаются.

    Наклейка при этом печатается с пометкой «кода нет» — молча пропустить такой
    товар значит отдать пачку, в которой на две наклейки меньше.
    """
    for pole in POLYA_NAKLEYKI:
        pole.znachenie(_Tovar(), None, SREDA)  # не должно бросить


# --- суммы на наклейке -------------------------------------------------------
#
# Часть ниже ходит в живой API, а склад и наклейки по умолчанию выключены —
# включаем на файл. Заодно это проверка, что включение работает: без него всё
# посыпалось бы на 403, и было бы не видно, в чём дело.


@pytest.fixture(scope="module")
def blok_vklyuchen(root_client: TestClient):
    from tests.conftest import API

    for blok in ("warehouse", "labels"):
        otvet = root_client.post(f"{API}/modules/{blok}", json={"enabled": True})
        assert otvet.status_code == 200, otvet.text
    yield
    for blok in ("labels", "warehouse"):
        root_client.post(f"{API}/modules/{blok}", json={"enabled": False})
    modules_service.invalidate()


def test_denezhnye_polya_nazvany_priznakom():
    """Признак `dengi` — единственное, что закрывает суммы от чужих глаз.

    Проверка внутри каждой функции была бы забыта в первой же новой, и дыра
    открылась бы молча. Здесь же забытый признак виден в самой записи реестра.
    """
    denezhnye = {p.klyuch for p in POLYA_NAKLEYKI if p.dengi}
    assert "price" in denezhnye, "цена перестала считаться суммой — печать обойдёт право"


def test_bez_prava_na_summy_tsena_ne_pechataetsya(blok_vklyuchen, root_client):
    """ДЫРА, которая была: печать закрыта только `labels.view` и о суммах не
    спрашивала вовсе.

    Кладовщик без `warehouse.view_amounts` не видел цену на экране — и печатал
    её на наклейке, если владелец включил поле. Право у склада заведено ровно
    затем, чтобы закупочную и продажную видел не всякий, и обходить его печатью
    нельзя.

    Роль заводится ЗДЕСЬ, а не берётся готовая: нужен человек ровно с
    `labels.view` и без `warehouse.view_amounts`. Возьми мы обычного менеджера
    — проверка упёрлась бы в 403 на самих наклейках и пропустилась, то есть
    дыру не стерёг бы никто, а выглядело бы это как зелёный набор.
    """
    from tests.conftest import API, make_manager

    assert root_client.patch(
        f"{API}/settings", json={"values": {"label_show_price": "1"}}
    ).status_code == 200

    tovar = root_client.post(
        f"{API}/warehouse/products",
        json={"name": f"Наклейка {next(_counter)}", "price": 123400},
    )
    assert tovar.status_code == 201, tovar.text
    tovar_id = tovar.json()["id"]

    vidit = root_client.get(f"{API}/labels/print?product_id={tovar_id}&preview=1")
    assert vidit.status_code == 200, vidit.text
    assert "1234.00" in vidit.text, "у владельца цена на наклейке не напечаталась вовсе"

    rol = root_client.post(
        f"{API}/roles",
        json={
            "name": f"Кладовщик без сумм {next(_counter)}",
            "permissions": ["labels.view", "warehouse.view"],
        },
    )
    assert rol.status_code == 201, rol.text
    pochta = f"kladovshchik-{next(_counter)}@test.local"
    kladovshchik = make_manager(root_client, pochta)
    lyudi = root_client.get(f"{API}/staff").json()["items"]
    user_id = next(u["id"] for u in lyudi if u["email"] == pochta)
    assert root_client.post(
        f"{API}/roles/assign/{user_id}", json={"role_id": rol.json()["id"]}
    ).status_code == 200

    try:
        ne_vidit = kladovshchik.get(f"{API}/labels/print?product_id={tovar_id}&preview=1")
        assert ne_vidit.status_code == 200, ne_vidit.text
        assert "1234.00" not in ne_vidit.text, (
            "цена напечаталась тому, кому суммы не положены: право склада обошли печатью"
        )
    finally:
        root_client.delete(f"{API}/staff/{user_id}")
        root_client.delete(f"{API}/roles/{rol.json()['id']}")


# --- картинка ужимается, а не вылезает ----------------------------------------
#
# НАЙДЕНО ЖИВЫМ ОСМОТРОМ НАКЛЕЙКИ, а не чтением кода. Обе библиотеки ставят SVG
# только `width` и `height`. Пока картинку показывают как есть, этого хватает —
# но стоит задать ей размер стилями, и рамка ужимается, а рисунок внутри
# остаётся прежним и просто выходит за неё. Обрезанный штрихкод не читается
# вовсе, а увидеть это можно было только на отпечатанной ленте.
#
# Поймано на двух сразу: QR на рулоне 58×40 вылезал за правый край на 3,7 мм, а
# у штрихкода то же самое оказалось ДАВНИМ — шаблон обещает «ширина во всю
# наклейку», а полоски печатались своей природной шириной 37,4 мм. На 30 мм это
# значило выход за край на 8 мм, то есть на мелких рулонах наклейка не работала
# никогда.


def _viewbox(svg: str) -> tuple[float, float]:
    ramka = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', svg)
    assert ramka, f"у картинки нет viewBox — стилями её не ужать: {svg[:120]}"
    return float(ramka.group(1)), float(ramka.group(2))


def test_u_shtrihkoda_est_ramka():
    """Без `viewBox` полоски печатаются своей шириной, а не шириной наклейки."""
    _viewbox(codes.barcode_svg("4820001234567", label=True))


def test_u_qr_est_ramka():
    """То же и у QR: на наклейке он ужимается до высоты полосок."""
    _viewbox(codes.qr_svg("https://crm.example.com/warehouse/42"))


def test_ramka_shtrihkoda_v_teh_zhe_edinitsah_chto_i_polosy():
    """Единицы `viewBox` — те же, в каких нарисовано СОДЕРЖИМОЕ.

    python-barcode пишет размер в миллиметрах, и полоски внутри — тоже в
    миллиметрах. Миллиметр в пользовательских единицах SVG равен 96/25.4 ≈ 3,78.
    Поставь мы `viewBox` прямо в миллиметрах — рисунок вышел бы ровно в 3,78
    раза крупнее рамки; замерено живьём: полоски вылезали на 134 пикселя при
    рамке в 66.
    """
    svg = codes.barcode_svg("4820001234567", label=True)
    shirina_mm = float(re.search(r'width="([\d.]+)mm"', svg).group(1))
    shirina_ramki, _ = _viewbox(svg)
    ozhidaem = shirina_mm * codes.MM_V_EDINITSAH
    assert abs(shirina_ramki - ozhidaem) < 0.5, (
        f"рамка {shirina_ramki} против ожидаемых {ozhidaem:.1f}: рисунок выйдет "
        f"в {shirina_ramki / ozhidaem:.2f} раза не того размера"
    )


def test_ramka_qr_v_teh_zhe_edinitsah_chto_i_risunok():
    """У segno размер без единиц — значит и рамка теми же числами."""
    svg = codes.qr_svg("https://crm.example.com/warehouse/42")
    shirina = float(re.search(r'width="([\d.]+)"', svg).group(1))
    assert _viewbox(svg)[0] == shirina


def test_chuzhaya_ramka_ne_perepisyvaetsya():
    """Есть своя — не трогаем: библиотека могла посчитать её точнее нашего."""
    svoya = '<svg width="10" height="10" viewBox="0 0 5 5"><rect/></svg>'
    assert codes._s_ramkoy(svoya) == svoya


def test_kartinka_bez_razmera_ne_ronyaet_pechat():
    """Не нашли размера — отдаём как есть. Наклейка без рамки лучше, чем отказ."""
    bez = "<svg><rect/></svg>"
    assert codes._s_ramkoy(bez) == bez
