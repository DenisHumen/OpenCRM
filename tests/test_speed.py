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

from datetime import datetime

import pytest
from sqlalchemy import event, text

from core.services import modules_service
from database.session import SessionLocal, engine
from tests.conftest import API


# --- приём: считать и запоминать запросы -------------------------------------


class Zaprosy:
    """Собирает SQL, ушедший в базу за время блока.

    Тот же `before_cursor_execute`, что в `test_query.py`: по времени пропажу
    индекса или лишний запрос на строку не заметить, а по счёту — видно сразу.
    """

    def __init__(self):
        self.spisok: list[str] = []

    def __enter__(self):
        self._slushatel = lambda conn, cursor, statement, *rest: self.spisok.append(statement)
        event.listen(engine, "before_cursor_execute", self._slushatel)
        return self

    def __exit__(self, *_):
        event.remove(engine, "before_cursor_execute", self._slushatel)

    def s_upominaniem(self, *slova: str) -> list[str]:
        """Запросы, в которых встречаются ВСЕ перечисленные слова."""
        return [
            s for s in self.spisok
            if all(slovo.lower() in s.lower() for slovo in slova)
        ]


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
