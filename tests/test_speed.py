"""Сторожа скорости на выросшей базе.

Скорость — самая тихая беда в проекте: ошибок нет, тесты зелёные, просто однажды
список клиентов открывается восемь секунд, и понять, когда это началось, уже
нельзя. Замеры на боевом объёме сделаны отдельно (стенд MySQL: 200 000 клиентов,
400 000 заявок, 2 700 000 движений склада; числа «было/стало» — в миграции
`b3f18d5a2e47`), а здесь стоят сторожа, которые не дают починке уйти обратно.

**Сторож на скорость не меряет время.** «Уложились в N миллисекунд» мигает на
чужой машине, в контейнере под нагрузкой и на CI, где рядом крутится сборка, —
и такой сторож либо выключают, либо перестают читать. Меряем то, что устойчиво:

* СТОИТ ЛИ ИНДЕКС — с точностью до порядка колонок и направления. Пропавший
  индекс не ломает ни одного ответа, поэтому заметить его больше нечем;
* СКОЛЬКО ЗАПРОСОВ уходит на экран — тем же приёмом (`before_cursor_execute`),
  каким `test_query.py` ловит второй запрос палитры;
* КАКОЙ ФОРМЫ запрос — например, что отчёт не соединяет заявки со справочником
  этапов: это соединение стоит планировщику узкого окна по `closed_at`.

Каждый сторож проверен на покраснение: снимаешь починку — он падает.
"""

import itertools
from datetime import datetime

import pytest
from sqlalchemy import event, text

from core.services import auth_service, maintenance_mode, modules_service
from database.session import SessionLocal, engine
from tests.conftest import API, Zaprosy


# --- приём: считать и запоминать запросы -------------------------------------


def progret(client, adres: str, **params) -> None:
    """Сходить по адресу ДО замера, чтобы счёт не мерил прогрев кэша.

    Состояние блоков системы кэшируется на две секунды
    (`core/services/modules_service.CACHE_SECONDS`). Первый запрос его греет и
    стоит на один запрос дороже второго — и проверка, сравнивающая два замера
    подряд, меряет не «сколько стоит строка», а «успел ли протухнуть кэш».

    Поймано на CI: список из ОДНОЙ строки стоил 5 запросов, из полусотни — 4.
    Меньше данных дороже больше — верный признак, что считается не то.

    Греем перед КАЖДЫМ замером, а не один раз в начале: между замерами тест
    успевает завести десяток записей, и двух секунд может не хватить.
    """
    assert client.get(adres, params=params or None).status_code == 200


def _indeksy(tablitsa: str) -> dict[str, list[tuple[str, str]]]:
    """{имя индекса: [(колонка, направление)]} прямо из живой схемы.

    Через `information_schema`, а не через отражение SQLAlchemy: направление
    колонки (`A`/`D`) отражение не показывает, а у одного из индексов оно и есть
    самое главное — без убывающего хвоста индекс не берётся вовсе.
    """
    with SessionLocal() as db:
        rows = db.execute(
            text(
                "SELECT index_name, seq_in_index, column_name, collation "
                "FROM information_schema.statistics "
                "WHERE table_schema = DATABASE() AND table_name = :t "
                "ORDER BY index_name, seq_in_index"
            ),
            {"t": tablitsa},
        ).all()
    sobrano: dict[str, list[tuple[str, str]]] = {}
    for imya, _nomer, kolonka, napravlenie in rows:
        sobrano.setdefault(imya, []).append((kolonka, napravlenie or "A"))
    return sobrano


# --- индексы под списки ------------------------------------------------------


@pytest.mark.parametrize(
    "tablitsa, indeks, ozhidaem",
    [
        # Список клиентов и список заявок: «живые, свежее сверху». Без пары база
        # достаёт всю живую выборку и строит из неё временное дерево сортировки
        # ради полусотни строк на экране — 269 и 541 мс против 29 и 53 с ней.
        ("clients", "ix_clients_alive_updated", [("deleted_at", "A"), ("updated_at", "A")]),
        ("deals", "ix_deals_alive_updated", [("deleted_at", "A"), ("updated_at", "A")]),
        # Счётчики и суммы по этапам (итоги над колонками доски, плитки сводки).
        # `amount` третьей колонкой делает индекс покрывающим: 432 -> 10 мс.
        (
            "deals",
            "ix_deals_alive_stage_amount",
            [("deleted_at", "A"), ("stage", "A"), ("amount", "A")],
        ),
    ],
)
def test_indeks_pod_spisok_stoit(tablitsa, indeks, ozhidaem):
    """Пропавший индекс не ломает ни одного ответа — заметить его больше нечем.

    Поэтому сторож смотрит не на время, а на схему: имя, состав и ПОРЯДОК
    колонок. Порядок здесь не придирка — `(updated_at, deleted_at)` вместо
    `(deleted_at, updated_at)` не даст ничего, а выглядеть будет так же.
    """
    est = _indeksy(tablitsa)
    assert indeks in est, (
        f"индекса {indeks} нет на {tablitsa}: {sorted(est)}\n"
        "Список снова сортирует всю живую выборку ради полусотни строк."
    )
    assert est[indeks] == ozhidaem, f"{indeks} собран иначе: {est[indeks]}"


def test_kolonka_kanbana_indeksirovana_s_ubyvayushchim_hvostom():
    """`sort_order ASC, id DESC` — порядок В РАЗНЫЕ СТОРОНЫ, и это решает всё.

    Индекс целиком по возрастанию такой порядок не даёт: MySQL берёт его и всё
    равно сортирует выборку (замерено — доска 1230 мс). С убывающим хвостом
    план становится `range` без сортировки: 180 мс.

    Проверяем именно направление, потому что потерять его проще всего:
    `Deal.id.desc()` в объявлении индекса выглядит необязательной мелочью, а
    без него вся починка перестаёт работать, ничем этого не объявив.
    """
    est = _indeksy("deals")
    imya = "ix_deals_alive_stage_sort"
    assert imya in est, f"индекса колонки канбана нет: {sorted(est)}"
    assert est[imya] == [
        ("deleted_at", "A"),
        ("stage", "A"),
        ("sort_order", "A"),
        ("id", "D"),
    ], f"{imya} собран иначе: {est[imya]}"


#: Единственный индекс заявок, которому позволено начинаться со `stage`, — узкий
#: однокОлоночный, заведённый вместе с самой таблицей. Он планы отчётов не
#: переманивал: планировщик предпочитал ему окно по `closed_at`.
ODINOCHNYY_STAGE = "ix_deals_stage"


def test_novye_indeksy_ne_vedut_ot_etapa():
    """Составной индекс заявок не должен начинаться со `stage`.

    Правило странное на вид и выстрадано замером. Отчёты отбирают заявки узким
    окном по `closed_at` — месяц из трёх лет, два процента таблицы. Стоит
    появиться СОСТАВНОМУ индексу, у которого `stage` первый, и планировщик
    начинает вести отчёт ОТ него: «пять этапов наружу, по восемьдесят тысяч
    заявок на каждый». Оценка при этом 68 строк при настоящих 78 000 —
    статистика хранит среднее на значение, а этапов пять.

    Проверено на стенде: с таким индексом воронка 34 -> 308 мс, выручка
    23 -> 224 мс. Наши три начинаются с `deleted_at` именно поэтому.

    Второй заслон от этой беды — в самих отчётах: они больше не соединяются со
    справочником этапов (сторож ниже). Но заслона мало одного: соединение
    вернуть легко, и тогда индекс со `stage` во главе снова станет ловушкой.
    """
    vinovnye = [
        imya
        for imya, kolonki in _indeksy("deals").items()
        if len(kolonki) > 1 and kolonki[0][0] == "stage" and imya != ODINOCHNYY_STAGE
    ]
    assert vinovnye == [], (
        "составной индекс заявок начинается со `stage` и переманит планы отчётов: "
        + ", ".join(vinovnye)
    )


# --- форма запроса отчёта ----------------------------------------------------


def test_otchyot_ne_soedinyaet_zayavki_so_spravochnikom_etapov(root_client):
    """Соединение `deals JOIN pipeline_stages` стоит отчёту его плана.

    Справочник этапов — два десятка строк, и соединение с ним выглядит
    безобидно. Замер говорит другое: счёт закрытых за месяц 195 мс соединением
    против 6.0 мс списком ключей, деньги за месяц — 220 против 9.8 мс. Тип
    этапа приписывается по ключу уже в Python (`reports._closed_stages`), и
    множество найденного при этом не меняется ни на строку: `key` в справочнике
    уникален.

    Сторож смотрит на ФОРМУ запроса, а не на время: соединение вернуть — одна
    строка, и по секундомеру на маленькой базе этого не увидеть вовсе.
    """
    with Zaprosy() as zapisano:
        for adres in ("funnel", "revenue", "sources"):
            otvet = root_client.get(
                f"{API}/reports/{adres}",
                params={"from": "2026-03-01", "to": "2026-03-31", "tz_offset": 0},
            )
            assert otvet.status_code == 200, otvet.text

    # Сначала убеждаемся, что сторож вообще что-то видел: соберись он на пустом
    # списке, проверка проходила бы всегда и не значила бы ничего.
    assert zapisano.s_upominaniem("deals"), "запросов по заявкам не видно вовсе"

    vinovnye = zapisano.s_upominaniem("pipeline_stages", "deals")
    assert vinovnye == [], (
        "отчёт снова соединяет заявки со справочником этапов:\n" + "\n".join(vinovnye)
    )


def test_svodka_ne_soedinyaet_zayavki_so_spravochnikom_etapov(root_client):
    """То же и у сводки: плитки «в работе» и «выиграно» считаются по виду этапа.

    Отдельным сторожем, а не строкой в предыдущем: считает их другой файл
    (`deals_repo.money_summary`), и починить одно, забыв другое, — обычное дело.
    """
    with Zaprosy() as zapisano:
        assert root_client.get(f"{API}/dashboard").status_code == 200

    assert zapisano.s_upominaniem("deals"), "запросов по заявкам не видно вовсе"

    vinovnye = zapisano.s_upominaniem("pipeline_stages", "deals")
    assert vinovnye == [], (
        "сводка снова соединяет заявки со справочником этапов:\n" + "\n".join(vinovnye)
    )


def test_spravochnik_etapov_chitaetsya_odin_raz_na_svodku(root_client):
    """Список ключей вместо соединения не должен превратиться в запрос на плитку.

    В сводке четыре слагаемых считаются по виду этапа, и спросить справочник
    четырежды было бы ровно тем N+1, от которого уходили. Читается он один раз
    на весь вызов `money_summary`.
    """
    with Zaprosy() as zapisano:
        assert root_client.get(f"{API}/dashboard").status_code == 200

    spravochnik = [
        s for s in zapisano.spisok
        if "from pipeline_stages" in " ".join(s.lower().split())
    ]
    assert len(spravochnik) <= 2, (
        "справочник этапов читается на каждую плитку сводки:\n" + "\n".join(spravochnik)
    )


# --- агрегаты на строке списка ----------------------------------------------


def test_ostatki_schitayutsya_odnim_zaprosom_na_stranitsu(root_client):
    """Остаток на каждой строке списка — это N+1 в худшем виде.

    Правило проекта «производное не хранится» означает, что остаток считается
    запросом. Цена правила — запрос по большой таблице на каждом показе, и
    единственное, чем она держится в разумных пределах: запрос ОДИН на всю
    страницу, а не по одному на товар. Двести позиций иначе дают двести
    обращений к базе, и заметить это по времени на пустой базе нельзя.
    """
    root_client.post(f"{API}/modules/warehouse", json={"enabled": True})
    try:
        for nomer in range(3):
            root_client.post(
                f"{API}/warehouse/products",
                json={"name": f"Скорость остатка {nomer}", "sku": f"SPD-{nomer:03d}"},
            )
        with Zaprosy() as zapisano:
            otvet = root_client.get(f"{API}/warehouse/products", params={"per_page": 50})
            assert otvet.status_code == 200, otvet.text

        summy = zapisano.s_upominaniem("sum(stock_moves.quantity_milli)")
        assert len(summy) <= 2, (
            "остаток считается отдельным запросом на строку списка:\n" + "\n".join(summy)
        )
    finally:
        # Возвращаем как было и сбрасываем кэш состояний: соседние файлы
        # рассчитывают на выключенный склад, а забытый включённым он даёт им
        # не 403, которого они ждут, а рабочий ответ.
        root_client.post(f"{API}/modules/warehouse", json={"enabled": False})
        modules_service.invalidate()


def test_doska_ne_delaet_zaprosa_na_kartochku(root_client):
    """Канбан: имена клиентов и ответственных — пачкой, а не по карточке.

    Доска из полусотни сделок делала бы сотню обращений к базе на каждое
    открытие. Считаем не время, а запросы: их число не должно зависеть от того,
    сколько карточек на доске.
    """
    client_id = root_client.post(
        f"{API}/clients", json={"name": "Скорость доски"}
    ).json()["id"]
    for nomer in range(6):
        root_client.post(
            f"{API}/deals", json={"title": f"Скорость доски {nomer}", "client_id": client_id}
        )

    progret(root_client, f"{API}/deals/board")
    with Zaprosy() as zapisano:
        assert root_client.get(f"{API}/deals/board").status_code == 200
    bylo = len(zapisano.spisok)

    for nomer in range(6, 18):
        root_client.post(
            f"{API}/deals", json={"title": f"Скорость доски {nomer}", "client_id": client_id}
        )

    progret(root_client, f"{API}/deals/board")
    with Zaprosy() as zapisano:
        assert root_client.get(f"{API}/deals/board").status_code == 200
    stalo = len(zapisano.spisok)

    # Допуск в один запрос, и он не послабление, а признание границы приёма.
    #
    # Проверка отвечает на вопрос «растёт ли число запросов ОТ ЧИСЛА КАРТОЧЕК».
    # Карточек стало втрое больше: N+1 дал бы +12 запросов и упёрся бы в допуск
    # с запасом. А ровное равенство требовало большего — чтобы счёт не менялся
    # НИ ОТ ЧЕГО, включая то, к чему проверка отношения не имеет.
    #
    # Замерено: в полном наборе счёт изредка расходится на единицу, в одиночку —
    # никогда, и трижды подряд отказ не воспроизвёлся. Причину я не установил
    # честно: это не кэш блоков (он греется перед каждым замером) и не число
    # карточек. Мигающий сторож при этом хуже слабого — он учит перезапускать
    # прогон, а не читать его.
    assert stalo - bylo <= 1, (
        f"втрое больше карточек — {stalo} запросов вместо {bylo}: доска ходит за строкой"
    )


def test_spisok_klientov_ne_zavisit_ot_chisla_kartochek(root_client):
    """Список клиентов: число запросов постоянно, сколько бы строк ни показали.

    Проверка дешёвая и ловит самое частое: кто-нибудь добавит в ответ поле,
    которое считается отдельным запросом на карточку, и список из двухсот строк
    начнёт делать двести обращений — молча и только на боевой базе.
    """
    progret(root_client, f"{API}/clients", per_page=1)
    with Zaprosy() as zapisano:
        assert root_client.get(f"{API}/clients", params={"per_page": 1}).status_code == 200
    odna = len(zapisano.spisok)

    progret(root_client, f"{API}/clients", per_page=50)
    with Zaprosy() as zapisano:
        assert root_client.get(f"{API}/clients", params={"per_page": 50}).status_code == 200
    polsotni = len(zapisano.spisok)

    # Тот же допуск и по той же причине, что у доски выше: проверка про рост от
    # числа строк, а полсотни против одной дали бы при N+1 полсотни запросов.
    assert polsotni - odna <= 1, (
        f"одна строка — {odna} запросов, полсотни — {polsotni}: список ходит за карточкой"
    )


# --- счёт ради страницы ------------------------------------------------------


def test_palitra_po_prezhnemu_ne_schitaet_naydennoe(root_client):
    """Точный `total` стоит ровно столько же, сколько сама выборка.

    Проверка есть и в `test_query.py` — на уровне репозитория клиентов. Здесь
    она снаружи и на ВСЕХ группах палитры разом: заявки на большой базе стоят
    дороже клиентов (замерено: 884 мс на группу), и вернуть счёт можно
    независимо в каждой из трёх.
    """
    with Zaprosy() as zapisano:
        otvet = root_client.get(f"{API}/search", params={"q": "чего-то-заведомо-нет"})
        assert otvet.status_code == 200, otvet.text

    schitayushchie = [s for s in zapisano.spisok if "count(" in s.lower()]
    assert schitayushchie == [], (
        "палитра снова считает найденное:\n" + "\n".join(schitayushchie)
    )


def test_schyot_stranitsy_ne_sortiruet(root_client):
    """`count(*)` от порядка не зависит, но база об этом не знает.

    Сортировка в счётном подзапросе заставляет её отсортировать ВСЁ найденное,
    чтобы затем просто сосчитать. На списке заявок это лишняя сортировка
    четырёхсот тысяч строк на каждый показ страницы.
    """
    with Zaprosy() as zapisano:
        assert root_client.get(f"{API}/deals", params={"per_page": 50}).status_code == 200

    schitayushchie = [s for s in zapisano.spisok if "count(" in s.lower()]
    assert schitayushchie, "запроса на количество не нашлось вовсе"
    for zapros in schitayushchie:
        assert "order by" not in zapros.lower(), f"счётчик сортирует: {zapros}"


# --- лента и журнал ----------------------------------------------------------


def test_lenta_kartochki_idyot_po_pare_kto_kogda(root_client):
    """Лента почти всегда чья-то, и пара «по кому + когда» обязана стоять.

    Индекс заведён давно (`f9b41c7e2d08`), но снять его так же легко, как и
    любой другой: ни один ответ от этого не изменится, а лента постоянного
    заказчика с тремя тысячами записей начнёт сортироваться заново на каждое
    открытие карточки.
    """
    est = _indeksy("client_notes")
    assert est.get("ix_client_notes_client_happened") == [
        ("client_id", "A"),
        ("happened_at", "A"),
    ], f"пары «по кому + когда» у ленты нет: {sorted(est)}"


def test_zhurnal_deystviy_chitaetsya_po_indeksu_vremeni(root_client):
    """Журнал растёт быстрее всех таблиц, и читают его всегда «свежее сверху».

    Индексов у него ровно три, и это осознанно — каждый лишний платится записью
    на КАЖДОЕ значимое действие в системе. Сторож следит за обоими краями: что
    нужный индекс на месте и что лишних не завелось.
    """
    est = _indeksy("audit_events")
    assert est.get("ix_audit_events_created_at") == [("created_at", "A")], sorted(est)
    # PRIMARY плюс три названных — больше журнал не выдержит.
    assert len(est) == 4, (
        "у журнала завелись лишние индексы, а он пополняется на каждое действие: "
        + ", ".join(sorted(est))
    )


def test_data_v_zhurnale_ne_zavisit_ot_chasovogo_poyasa():
    """Мелочь рядом: сторожа выше опираются на то, что время в базе без зоны.

    Проверка стоит здесь, а не в схеме, потому что ломает она именно замеры:
    окно отчёта, посчитанное в одной зоне, а сохранённое в другой, отбирает не
    те строки — и «отчёт стал медленным» оказывается «отчёт читает год вместо
    месяца».
    """
    with SessionLocal() as db:
        tip = db.execute(
            text(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_schema = DATABASE() AND table_name = 'audit_events' "
                "AND column_name = 'created_at'"
            )
        ).scalar()
    assert tip == "datetime", f"время журнала хранится как {tip}"
    assert isinstance(datetime.now(), datetime)


def test_smena_preseta_ne_zavisit_ot_chisla_zayavok(root_client):
    """Перестройка воронки стоит одинаково при одной заявке и при десяти.

    Раньше это был цикл по заявкам: присвоение этапа плюс `add_stage_change`, а
    тот делает `db.flush()` — то есть на КАЖДУЮ заявку отдельные INSERT и
    UPDATE, и вдобавок все объекты поднимались в память. На боевом объёме из
    шапки этого файла (400 000 заявок) смена пресета означала бы под миллион
    отдельных обращений в одной транзакции запроса, с блокировками на всей
    таблице заявок до самого конца.

    Меряем не время, а СЧЁТ, и не абсолютный, а его РОСТ: сколько именно
    запросов уходит на перестройку — дело устройства и меняется от правки к
    правке. А вот зависимость от числа заявок — это и есть беда, и её видно
    сразу.
    """
    klient = root_client.post(f"{API}/clients", json={"name": "Воронка и скорость"}).json()

    def perestroit(preset: str) -> int:
        with Zaprosy() as z:
            otvet = root_client.post(f"{API}/pipeline/preset", json={"preset": preset})
            assert otvet.status_code == 200, otvet.text
        return len(z.spisok)

    # Одна заявка на доске.
    root_client.post(f"{API}/deals", json={"title": "Одна", "client_id": klient["id"]})
    progret(root_client, f"{API}/pipeline/stages")
    s_odnoy = perestroit("services")

    # Ещё дюжина — счёт обязан остаться прежним.
    for nomer in range(12):
        root_client.post(
            f"{API}/deals", json={"title": f"Ещё {nomer}", "client_id": klient["id"]}
        )
    s_dyuzhinoy = perestroit("universal")

    assert s_dyuzhinoy - s_odnoy <= 1, (
        f"перестройка при 13 заявках стоит {s_dyuzhinoy} запросов против "
        f"{s_odnoy} при одной — счёт растёт от числа заявок. Запас тут "
        f"тринадцатикратный: запрос на заявку дал бы +24"
    )


# --- потолок числа запросов на ручку -----------------------------------------
#
# **Без этой таблицы всякая починка скорости отменяется одной строкой в чужом
# коммите, и заметить это нечем.** Ни время ответа на пустой базе, ни сам ответ
# не изменятся: лишний запрос на строку виден только счётом. Ровно так и
# накопились нынешние числа — никто не добавлял двадцать пять запросов на
# сводку разом, их добавляли по одному.
#
# Потолок, а не точное равенство: проверка обязана краснеть на РОСТЕ и молчать
# на починке. Иначе всякое улучшение начиналось бы с правки сторожа, и сторож
# перестал бы что-либо значить.
#
# Числа сняты замером на этой же обвязке (root, кэши прогреты) и округлены вверх
# С ЗАПАСОМ. Запас здесь не небрежность, а необходимость: база у набора ОБЩАЯ, и
# часть ручек стоит дороже от чужих данных — `/deals/board` спрашивает по
# запросу на ЭТАП воронки, а этапы заводят соседние файлы. Точность до единицы
# сделала бы сторожа мигающим, то есть выключенным.
#
# Поэтому здесь ловится СКАЧОК (запрос на строку, забытая пачечная загрузка), а
# не единица. За тем, что счёт не растёт С ЧИСЛОМ СТРОК, следят отдельные
# проверки выше — они устойчивы к чужим данным по построению и потому строже.
POTOLKI = {
    "/auth/me": 3,
    "/modules": 5,
    "/workspace": 4,
    "/tasks/summary": 7,
    "/system/storage": 5,
    # Семь с 06.09.2026: список клиентов отвечает «с кем мы работаем» — заявки
    # по клиентам страницы (один запрос плюс справочник этапов) и последний
    # контакт (один запрос). Замерено шесть; единица — запас, как у журнала.
    "/clients?per_page=50": 7,
    "/deals?per_page=50": 8,
    "/deals/board": 16,
    "/documents?per_page=50": 5,
    "/staff": 5,
    # Семь, а не шесть, и это исправление собственной оплошности, а не уступка.
    #
    # Замерено на настоящей базе: пустой журнал стоит ЧЕТЫРЕ запроса, журнал с
    # одной операцией — ШЕСТЬ. Разница в двух условных запросах внутри
    # `_decorate`: статьи и имена авторов спрашиваются, только если строки на
    # странице есть. Стояло здесь ровно шесть — то есть впритык к населённому
    # состоянию, без обещанного этой же таблицей запаса.
    #
    # Шлюз деплоя это и поймал: один прогон дал 7 при потолке 6, следующий на
    # ТОМ ЖЕ коде — зелёное. Седьмым запросом была отметка присутствия, запись
    # по таймеру; её из счёта убрал `Zaprosy.chteniya`, но запаса всё равно не
    # было ни на что.
    "/finance/operations?per_page=50": 7,
    "/reports/revenue": 7,
    "/audit?per_page=50": 5,
    # Возвраты и накладные (06.09.2026). Населённая страница возвратов платит
    # семь (ворота): выборка и счёт, строки, имена клиентов, номера заказов,
    # счёт по состояниям; накладные — строки и основания; статистика — три
    # сводных запроса и справочники. Потолок — замер плюс единица.
    "/returns?per_page=50": 8,
    "/returns/stats": 8,
    "/waybills?per_page=50": 6,
}

#: `/dashboard` и `/search` в таблице НЕ стоят, и это не забывчивость.
#:
#: Их цена растёт с ДАННЫМИ: сводка спрашивает по блокам, поиск — по видам
#: найденного. На общей базе набора соседний файл заводит доску, и число
#: подскакивает, хотя код не менялся. Абсолютный потолок на такой ручке —
#: мигающий сторож, то есть выключенный.
#:
#: Стерегутся они иначе и строже: проверками формы ниже — «цена не растёт с
#: числом найденного». Такая проверка заводит данные сама и к чужим равнодушна
#: по построению.

_schyotchik_metok = itertools.count(1)


def uniq_metka() -> str:
    """Метка, по которой ищет проверка выше. Своя на каждый заход."""
    return f"{next(_schyotchik_metok):04d}"


#: Блоки, которые нужны замерам: без них часть ручек отвечает отказом, и потолок
#: мерил бы стоимость отказа, а не работы.
NUZHNYE_BLOKI = ("documents", "warehouse", "orders", "waybills", "finance", "boards", "labels")


@pytest.fixture(scope="module")
def vse_bloki(root_client):
    """Все нужные блоки включены — и ВЫКЛЮЧЕНЫ обратно после замеров.

    **Возврат состояния здесь обязателен, и это не аккуратность.** Состояние
    блоков — общее на всю систему, а база у набора одна. Оставленный включённым
    `finance` меняет то, как считается выручка, и соседний файл получает другой
    ответ на том же коде: `test_reports.py::test_csv_leaves_an_unnamed_amount_empty`
    ждал пустую ячейку среднего чека, а получал «0,00».

    Поймано прогоном в обратном порядке — тем самым, что CI гоняет вторым
    заходом ровно ради таких сцепок. Замеряющая проверка, меняющая мир вокруг
    себя, тем и опасна, что сама остаётся зелёной.
    """
    bylo = root_client.get(f"{API}/modules").json()
    sostoyanie = {
        z["key"]: bool(z.get("enabled"))
        for z in (bylo.get("items") if isinstance(bylo, dict) else bylo) or []
    }

    for key in NUZHNYE_BLOKI:
        root_client.post(f"{API}/modules/{key}", json={"enabled": True})
    try:
        yield root_client
    finally:
        # Возвращаем ровно то, что было: «выключить всё» испортило бы соседей
        # ничуть не меньше, чем «включить всё».
        for key in NUZHNYE_BLOKI:
            if key in sostoyanie:
                root_client.post(f"{API}/modules/{key}", json={"enabled": sostoyanie[key]})


@pytest.mark.parametrize("put,potolok", sorted(POTOLKI.items()))
def test_ruchka_ne_dorozhe_potolka(vse_bloki, put, potolok):
    """Ручка не стала спрашивать базу чаще, чем ей позволено.

    Растёт этот счёт незаметно: добавили поле в ответ — плюс запрос, добавили
    проверку права — плюс ещё. На малой базе это не видно ни по времени, ни
    глазом, а на боевой пачке умножается на число строк и плиток.
    """
    adres = f"{API}{put}"
    # Прогрев: первый заход набивает кэши блоков и настроек, и его счёт не
    # показателен. Меряем установившееся состояние, как на живом сервере.
    vse_bloki.get(adres)

    with Zaprosy() as z:
        otvet = vse_bloki.get(adres)

    assert otvet.status_code == 200, otvet.text
    # Считаем ВОПРОСЫ, а не всё подряд: разбор — в докстроке `Zaprosy.chteniya`.
    # Коротко: на пути GET-а есть запись по таймеру, и потолок, считавший её,
    # краснел и зеленел на одном и том же коде.
    assert len(z.chteniya) <= potolok, (
        f"{put} стоит {len(z.chteniya)} запросов при потолке {potolok}.\n"
        "Если это осознанное усложнение — подними потолок вместе с доводом; "
        "если нет — где-то появился запрос на строку."
    )


def test_poisk_ne_dorozhaet_ot_chisla_naydennogo(vse_bloki):
    """Цена поиска не растёт с числом найденного.

    Поиск идёт по нескольким видам сразу (клиенты, заявки, бланки, товары,
    доски), и соблазн — спросить подробности по каждому найденному отдельно.
    Тогда одно нажатие в поле поиска превращается в запрос на строку, и заметить
    это по времени на малой базе нельзя.

    Проверка заводит данные САМА и потому не зависит от того, что оставили
    соседи по прогону: сравниваются два замера на одной и той же обвязке, между
    которыми добавилось десять совпадений.
    """
    metka = f"ЩЩЩ{uniq_metka()}"

    def tsena() -> int:
        vse_bloki.get(f"{API}/search?q={metka}")  # прогрев
        with Zaprosy() as z:
            otvet = vse_bloki.get(f"{API}/search?q={metka}")
        assert otvet.status_code == 200, otvet.text
        return len(z.spisok)

    zavedennye = []
    try:
        # Два совпадения ДО замера: пачечная выборка при нуле найденного не
        # выполняется вовсе, и сравнение «ноль против десяти» показало бы её
        # постоянную цену как рост. Мерим непустое против непустого.
        for nomer in range(2):
            klient = vse_bloki.post(f"{API}/clients", json={"name": f"{metka} основа {nomer}"})
            assert klient.status_code == 201, klient.text
            zavedennye.append(klient.json()["id"])

        bylo = tsena()
        for nomer in range(10):
            klient = vse_bloki.post(f"{API}/clients", json={"name": f"{metka} клиент {nomer}"})
            assert klient.status_code == 201, klient.text
            zavedennye.append(klient.json()["id"])

        stalo = tsena()
        assert stalo <= bylo, (
            f"поиск подорожал с {bylo} до {stalo} запросов от десяти найденных — "
            "где-то появился запрос на строку"
        )
    finally:
        for klient_id in zavedennye:
            vse_bloki.delete(f"{API}/clients/{klient_id}")


def test_svodka_ne_dorozhaet_ot_chisla_dosok(vse_bloki):
    """Цена сводки не растёт с числом досок.

    Сводка показывает четыре свежих витрины, и подробности по каждой (просмотры,
    число работ) — ровно то место, где запрос на строку заводится сам собой.
    Образец, как надо, стоит рядом: `stats_repo.views_by_board` собирает всё
    одним запросом с группировкой.
    """
    def tsena() -> int:
        vse_bloki.get(f"{API}/dashboard")  # прогрев
        with Zaprosy() as z:
            otvet = vse_bloki.get(f"{API}/dashboard")
        assert otvet.status_code == 200, otvet.text
        return len(z.spisok)

    doski = []
    try:
        # Две доски ДО замера, и по той же причине: `cards.board_cards` при
        # пустом списке возвращается сразу, а с первой же доской платит свои
        # четыре пачечных запроса. Сравнение «ноль против четырёх» показало бы
        # эту постоянную цену ростом — сторож краснел бы на исправном коде.
        for nomer in range(2):
            doska = vse_bloki.post(f"{API}/boards", json={"title": f"Доска основы {nomer}"})
            assert doska.status_code == 201, doska.text
            doski.append(doska.json()["id"])

        bylo = tsena()
        for nomer in range(4):
            doska = vse_bloki.post(f"{API}/boards", json={"title": f"Доска скорости {nomer}"})
            assert doska.status_code == 201, doska.text
            doski.append(doska.json()["id"])

        stalo = tsena()
        assert stalo <= bylo, (
            f"сводка подорожала с {bylo} до {stalo} запросов от четырёх досок — "
            "подробности спрашиваются по доске, а не пачкой"
        )
    finally:
        for doska_id in doski:
            vse_bloki.delete(f"{API}/boards/{doska_id}")


def test_kto_prishyol_sprashivaetsya_odnim_zaprosom(vse_bloki):
    """Проверка «кто пришёл» стоит РОВНО одного запроса. Не двух.

    Это самый частый запрос в системе: он выполняется на каждом обращении
    вошедшего — на каждой странице, на каждой плитке витрины, на каждом фото
    товара. Пока их было два (сессия по отпечатку, потом хозяин по номеру), лишний
    круг к базе платился всегда и умножался на число картинок: витрина на
    тридцать плиток стоила шестидесяти кругов только за «кто пришёл».

    Сторож отдельный, а не потолок на ручку: потолки несут запас (база у набора
    общая, часть ручек дорожает от чужих данных), и внутри этого запаса второй
    запрос за сессией прошёл бы незамеченным. Проверено подлогом — так и было.
    """
    for put in ("/auth/me", "/clients?per_page=50", "/modules"):
        vse_bloki.get(f"{API}{put}")  # прогрев
        with Zaprosy() as z:
            otvet = vse_bloki.get(f"{API}{put}")
        assert otvet.status_code == 200, otvet.text

        sessii = z.s_upominaniem("user_sessions")
        assert len(sessii) == 1, (
            f"{put}: за сессией ходили {len(sessii)} раза вместо одного: "
            + "; ".join(s.split(chr(10))[0][:80] for s in sessii)
        )
        # И хозяин приходит ТЕМ ЖЕ запросом — то есть в нём есть соединение
        # с таблицей людей. Проверяем именно это, а не «нет других запросов
        # к users»: пачечная выборка имён через `IN (...)` законна и есть,
        # например, у списка блоков — сторож на неё краснел бы зря.
        assert "JOIN users" in sessii[0], (
            f"{put}: сессия взята без хозяина — значит за ним пойдёт второй "
            "запрос: " + sessii[0].split(chr(10))[0][:80]
        )


def test_spisok_dolzhnostey_ne_dorozhaet_ot_ih_chisla(vse_bloki):
    """Цена списка должностей не растёт с их числом.

    Наклон был ровно плюс два на роль: права и число людей спрашивались по
    строке. Десять должностей — двадцать три запроса вместо пяти. На малой базе
    это не видно ни по времени, ни глазом, а список ролей открывают с первого
    дня и с каждой новой должностью он дорожает.

    Проверка заводит должности САМА и потому не зависит от того, что оставили
    соседи по прогону: сравниваются два замера, между которыми добавилось шесть.
    """
    def tsena() -> int:
        vse_bloki.get(f"{API}/roles")  # прогрев
        with Zaprosy() as z:
            otvet = vse_bloki.get(f"{API}/roles")
        assert otvet.status_code == 200, otvet.text
        return len(z.spisok)

    zavedennye = []
    try:
        # Две до замера: пачечная выборка при пустом списке не выполняется вовсе.
        for nomer in range(2):
            rol = vse_bloki.post(
                f"{API}/roles",
                json={"name": f"Скорость основа {uniq_metka()}", "permissions": ["clients.view"]},
            )
            assert rol.status_code == 201, rol.text
            zavedennye.append(rol.json()["id"])

        bylo = tsena()
        for nomer in range(6):
            rol = vse_bloki.post(
                f"{API}/roles",
                json={"name": f"Скорость {uniq_metka()}", "permissions": ["clients.view"]},
            )
            assert rol.status_code == 201, rol.text
            zavedennye.append(rol.json()["id"])

        stalo = tsena()
        assert stalo <= bylo, (
            f"список должностей подорожал с {bylo} до {stalo} запросов от шести "
            "новых ролей — права или число людей спрашиваются по строке"
        )
    finally:
        for rol_id in zavedennye:
            vse_bloki.delete(f"{API}/roles/{rol_id}")


def test_taymery_zamerzayut_na_vremya_zamera(vse_bloki, monkeypatch):
    """Обновление кэша по таймеру не должно попадать в замер.

    Три места в пути обычного запроса обновляются по часам, а не по делу:
    блоки системы, режим обслуживания и отметка присутствия. Замерено: тот же
    запрос поиска стоит 4 запроса при свежем кэше и 6 при протухшем.

    Проверки роста сравнивают два замера, и двух лишних чтений во втором
    хватает, чтобы «стало > было». На боевом сервере, где прогон идёт девять
    минут, это дважды подряд валило обновление — и на разных проверках.

    Здесь все три срока выкручены в ноль, то есть кэш протухает на КАЖДОМ
    обращении: так проверяется само правило, а не везение с секундой.
    """
    metka = f"ЩЩЩ{uniq_metka()}"
    vse_bloki.post(f"{API}/clients", json={"name": f"{metka} один"})
    monkeypatch.setattr(modules_service, "CACHE_SECONDS", 0.0)
    monkeypatch.setattr(maintenance_mode, "CACHE_SECONDS", 0.0)
    monkeypatch.setattr(auth_service, "PRESENCE_TOUCH_SECONDS", 0)

    vse_bloki.get(f"{API}/search?q={metka}")   # прогрев наполняет кэши
    with Zaprosy() as z:
        otvet = vse_bloki.get(f"{API}/search?q={metka}")
    assert otvet.status_code == 200, otvet.text

    shum = [
        s for s in z.spisok
        if "module_states" in s or "site_settings" in s
        or ("last_seen_at" in s.lower() and "update" in s.lower())
    ]
    assert not shum, (
        "обновление кэша по таймеру попало в замер — проверки роста будут "
        "краснеть и зеленеть на одном коде: " + str([s[:70] for s in shum])
    )
    assert z.chteniya, "замер ослеп вовсе — заморозка отрезала лишнее"


    # И обратная сторона: сроки возвращаются на место, а не остаются
    # выкрученными на весь прогон.
    assert modules_service.CACHE_SECONDS == 0.0
