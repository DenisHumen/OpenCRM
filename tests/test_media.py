"""Очередь на разжатие картинок общая на все рабочие процессы.

Ограничитель разжатия сторожит ПАМЯТЬ. Пока он был
`threading.BoundedSemaphore(2)`, он держал два разжатия в своём процессе и
ничего не знал про соседние: четыре воркера дают четыре независимых пика против
общего `mem_limit: 3g`, и никакой ошибки при этом не возникает — каждый процесс
честно держит свои два места. Это ровно тот класс тихой беды, ради которого
счётчик попыток входа переехал в Redis (`tests/test_ratelimit_shared.py`), и
проверки здесь устроены так же: ловят не отказ, а МОЛЧАНИЕ.

Живого Redis в наборе нет — набор гоняется без внешних служб. Поэтому клиент
подменяется маленьким подставным: он знает ровно две команды, которыми
пользуется очередь. Подставной общий на всех, и это его главное свойство: место,
занятое «соседним процессом», обязано быть видно здесь.
"""

import threading
import time

import pytest

from core import exceptions as errors
from core import redis_client
from core.services import media_service
from tests.conftest import png_bytes


class PodstavnoyRedis:
    """Подставной Redis: одно сортированное множество занятых мест.

    ЧЕСТНО О ЦЕНЕ. `eval` здесь — КОПИЯ смысла скрипта `_ZANYAT_MESTO`, а не его
    исполнение: разойдись скрипт с этой копией, проверки не заметят — за
    совпадение отвечает живой прогон с настоящим Redis. Настоящее здесь всё
    остальное, ради чего копия и написана: общее хранилище на всех, срок у
    брошенного места, срок у ключа, отказ при недоступном сервере.
    """

    def __init__(self):
        self.data: dict[str, dict[str, float]] = {}
        self.sroki: list[int] = []
        self.zanyato_raz = 0
        self._zamok = threading.Lock()

    def eval(self, script, numkeys, *args):
        if "zremrangebyscore" not in script or "zcard" not in script:
            raise NotImplementedError("подставной Redis знает один скрипт — _ZANYAT_MESTO")
        klyuch, teper, do_kogda, metka, predel, ttl = args[:6]
        # Замок и есть атомарность скрипта: у настоящего Redis её даёт сервер.
        with self._zamok:
            mesta = self.data.setdefault(klyuch, {})
            for chlen, srok in list(mesta.items()):
                if srok <= float(teper):
                    del mesta[chlen]
            if len(mesta) >= int(predel):
                return 1
            mesta[metka] = float(do_kogda)
            self.sroki.append(int(ttl))
            self.zanyato_raz += 1
            return 0

    def zrem(self, klyuch, *chleny):
        with self._zamok:
            mesta = self.data.get(klyuch, {})
            return sum(1 for chlen in chleny if mesta.pop(chlen, None) is not None)

    def skolko_zanyato(self, klyuch) -> int:
        with self._zamok:
            return len(self.data.get(klyuch, {}))


class SlomannyyRedis:
    """Redis, который не отвечает. Любая команда — отказ."""

    def __getattr__(self, imya):
        def upast(*args, **kwargs):
            raise ConnectionError("redis не отвечает")

        return upast


@pytest.fixture
def obshchee(monkeypatch):
    """Общее хранилище на всех, кто спросит клиента."""
    hranilishche = PodstavnoyRedis()
    monkeypatch.setattr(redis_client, "get_client", lambda: hranilishche)
    return hranilishche


@pytest.fixture
def slomannyy(monkeypatch):
    monkeypatch.setattr(redis_client, "get_client", lambda: SlomannyyRedis())


@pytest.fixture
def raspahnutyy_mestnyy(monkeypatch):
    """Местный семафор распахнут: держать предел остаётся только общей очереди.

    Без этого проверка ниже была бы зелёной и на старом коде — там предел в два
    места держал именно местный семафор, и отличить «предел общий» от «предел в
    памяти процесса» одними потоками нельзя.
    """
    monkeypatch.setattr(media_service, "_mestnoe", threading.BoundedSemaphore(99))


def _dozhdatsya(uslovie, srok: float = 5.0) -> bool:
    do = time.monotonic() + srok
    while time.monotonic() < do:
        if uslovie():
            return True
        time.sleep(0.01)
    return uslovie()


class _Tolpa:
    """Несколько потоков, разом берущих место и держащих его до отмашки."""

    def __init__(self, skolko: int):
        self.voshli: list[int] = []
        self.bedy: list[BaseException] = []
        self._zamok = threading.Lock()
        self._otpustit = threading.Event()
        self._razom = threading.Barrier(skolko)
        self._potoki = [threading.Thread(target=self._rabota) for _ in range(skolko)]

    def _rabota(self):
        try:
            self._razom.wait(10)
            with media_service.mesto_razzhatiya():
                with self._zamok:
                    self.voshli.append(1)
                self._otpustit.wait(15)
        except BaseException as beda:  # noqa: BLE001 — беду показываем проверке
            with self._zamok:
                self.bedy.append(beda)

    def __enter__(self):
        for potok in self._potoki:
            potok.start()
        return self

    def __exit__(self, *_):
        self._otpustit.set()
        for potok in self._potoki:
            potok.join(20)
        assert not any(p.is_alive() for p in self._potoki), "поток не отпустил место"

    @property
    def vnutri(self) -> int:
        with self._zamok:
            return len(self.voshli)

    def otpustit(self) -> None:
        self._otpustit.set()


# --- дуэль: предел общий, а не свой у каждого процесса ------------------------


def test_duel_dvoe_zanimayut_tretiy_zhdyot(obshchee, raspahnutyy_mestnyy):
    """ДУЭЛЬ. Двое берут место разом, третий обязан ждать.

    Местный семафор здесь распахнут нарочно: если предел держит он, проверка
    зелена и на старом коде. Красной она становится ровно тогда, когда очередь
    перестаёт быть общей, — а это и есть та правка, после которой четыре воркера
    дают четыре независимых пика памяти вместо одного общего.
    """
    with _Tolpa(3) as tolpa:
        assert _dozhdatsya(lambda: tolpa.vnutri >= media_service.ODNOVREMENNO), (
            f"вошло {tolpa.vnutri}, а мест {media_service.ODNOVREMENNO}: очередь не пускает вовсе"
        )
        time.sleep(0.3)  # даём третьему время просочиться, если предел не держит
        assert tolpa.vnutri == media_service.ODNOVREMENNO, (
            f"внутри {tolpa.vnutri} при пределе {media_service.ODNOVREMENNO} — "
            "предел держится в памяти процесса, а не на всю установку"
        )

        # И третий не заперт навсегда: отпущенное место он занимает.
        tolpa.otpustit()
        assert _dozhdatsya(lambda: tolpa.vnutri == 3), (
            f"освободившееся место никто не занял: внутри побывало {tolpa.vnutri} из 3"
        )
    assert not tolpa.bedy, tolpa.bedy


def test_mesto_soseda_vidno_v_etom_processe(obshchee):
    """Место, занятое СОСЕДНИМ процессом, уменьшает то, что можно взять здесь.

    Это и есть смысл переезда, и одними потоками его не проверить: два потока
    одного процесса упирались в старый семафор точно так же. Сосед изображается
    записью прямо в общее хранилище — ровно тем, что оставил бы там другой
    воркер.
    """
    obshchee.data[media_service.KLYUCH_MESTA] = {"sosedniy-process": time.time() + 60}

    with _Tolpa(2) as tolpa:
        assert _dozhdatsya(lambda: tolpa.vnutri >= 1), "очередь не пустила никого"
        time.sleep(0.3)
        assert tolpa.vnutri == 1, (
            f"внутри {tolpa.vnutri}: место соседнего процесса здесь не считается, "
            f"и общий предел {media_service.ODNOVREMENNO} превращается в "
            f"{media_service.ODNOVREMENNO} НА КАЖДЫЙ процесс"
        )
    assert not tolpa.bedy, tolpa.bedy


def test_razzhatie_kartinki_prohodit_cherez_ochered(obshchee, tmp_path):
    """Сама обработка снимка обязана брать место, а не идти мимо очереди.

    Проверки выше стерегут ограничитель, а эта — то, что им пользуются. Убери
    кто-нибудь `with mesto_razzhatiya()` из `process_image`, и все остальные
    остались бы зелёными.
    """
    original = tmp_path / "original.png"
    original.write_bytes(png_bytes())

    media_service.process_image("proba", original)

    assert obshchee.zanyato_raz == 1, "разжатие прошло мимо общей очереди"
    assert obshchee.skolko_zanyato(media_service.KLYUCH_MESTA) == 0, "место не отдано"


def test_mesto_otdayotsya_i_posle_bedy(obshchee):
    """Упавшая обработка обязана отдать место.

    Иначе первая же битая картинка съедала бы место насовсем, а вторая и третья
    закрывали бы загрузку картинок до перезапуска — на срок годности места, если
    он есть, и навсегда, если его забыли.
    """
    with pytest.raises(ZeroDivisionError):
        with media_service.mesto_razzhatiya():
            1 / 0

    assert obshchee.skolko_zanyato(media_service.KLYUCH_MESTA) == 0, (
        "место осталось занятым после падения обработки"
    )


# --- место не остаётся занятым навсегда ---------------------------------------


def test_broshennoe_mesto_otpuskaetsya_po_sroku(obshchee):
    """У места есть срок годности, и без него правка была бы опаснее беды.

    Процесс, убитый ядром по памяти, `finally` не выполняет и место не отдаёт.
    Место без срока в ОБЩЕМ хранилище переживает перезапуск контейнера: загрузка
    картинок перестаёт работать, и починить это можно только руками в Redis.
    """
    brosheno = {
        f"upavshiy-{i}": time.time() - 1 for i in range(media_service.ODNOVREMENNO)
    }
    obshchee.data[media_service.KLYUCH_MESTA] = brosheno

    with media_service.mesto_razzhatiya():
        pass  # брошенные места не должны считаться занятыми

    assert not any(
        metka in obshchee.data.get(media_service.KLYUCH_MESTA, {}) for metka in brosheno
    ), "просроченные места остались в очереди"


def test_u_klyucha_ocheredi_est_srok(obshchee):
    """Срок ставится и на самом ключе: очередь не должна пережить всех, кто в ней стоял."""
    with media_service.mesto_razzhatiya():
        pass

    assert obshchee.sroki, "ключ записан без срока — он останется в хранилище навсегда"
    assert obshchee.sroki[0] >= media_service.SROK_MESTA_SEKUND, (
        "срок ключа короче срока места: место исчезнет раньше, чем протухнет"
    )


def test_ochered_ne_zhdyot_vechno(obshchee, monkeypatch):
    """Ожидание ограничено, и это про доступность всего приложения.

    Фоновая обработка идёт в общем пуле потоков Starlette (сорок мест). Заклинь
    очередь — сорок потоков встали бы в ней навсегда, и вместе с ними встали бы
    ВСЕ синхронные ручки, а не только загрузка картинок.
    """
    monkeypatch.setattr(media_service, "OZHIDANIE_SEKUND", 0.2)
    obshchee.data[media_service.KLYUCH_MESTA] = {
        f"sosed-{i}": time.time() + 600 for i in range(media_service.ODNOVREMENNO)
    }

    with pytest.raises(errors.PrehodyashchayaBedaError) as beda:
        with media_service.mesto_razzhatiya():
            pytest.fail("место выдано, хотя вся очередь занята")
    assert beda.value.http_status == 503, "422 читается как «файл плохой», а дело не в нём"


# --- Redis лёг ----------------------------------------------------------------
#
# РЕШЕНИЕ, КОТОРОЕ НАДО ЗНАТЬ, и оно ДРУГОЕ, чем у ограничителя попыток входа.
# Там недоступный счётчик означает отказ: пустить без счёта значит отдать пароль
# и PIN на перебор. Здесь цена другая — предел сторожит память, а не секрет, и
# запасной путь в память процесса остаётся настоящим пределом, просто своим у
# каждого воркера. Худший случай при четырёх воркерах — 2,2 ГБ, ровно то, подо
# что и брался `mem_limit: 3g`. Отказать в загрузке дороже: авария соседней
# службы выключала бы приём картинок целиком.
#
# Чего делать нельзя ни в каком случае — пропускать без предела вовсе: сорок
# мест в пуле потоков дают сорок разжатий, то есть 7,6 ГБ на машине с восемью.


def test_lyogshiy_redis_ostavlyaet_predel_v_pamyati_processa(slomannyy):
    """Redis не ответил — предел остаётся, просто свой у каждого процесса.

    Соблазн здесь ровно один и в одну строку: поймать отказ и пойти дальше без
    очереди. Итог — сорок одновременных разжатий из пула потоков вместо двух, то
    есть ровно та беда, ради которой ограничитель и стоит.
    """
    with _Tolpa(media_service.ODNOVREMENNO + 1) as tolpa:
        assert _dozhdatsya(lambda: tolpa.vnutri >= media_service.ODNOVREMENNO), (
            f"с лежащим Redis внутрь не пустили никого: {tolpa.vnutri}"
        )
        time.sleep(0.3)
        assert tolpa.vnutri == media_service.ODNOVREMENNO, (
            f"внутри {tolpa.vnutri} при пределе {media_service.ODNOVREMENNO} — "
            "лежащий Redis снял предел вовсе, и пик памяти ограничен только пулом потоков"
        )
    assert not tolpa.bedy, tolpa.bedy


def test_pro_lezhashchiy_redis_govoryat_vsluh(slomannyy, monkeypatch, capfd):
    """Тихого варианта нет: запасной путь общим пределом не является.

    Молча уехать на предел в памяти процесса — значит потерять ту самую
    общность, ради которой всё затевалось, и не узнать об этом. Авария считается
    (`bez_obshchego_zamka_total`, читает `/metrics`) и говорит о себе в журнал.
    """
    monkeypatch.setattr(media_service, "_poslednii_krik", 0.0)  # крик придушен на полминуты
    bylo = media_service.bez_obshchego_zamka_total()

    with media_service.mesto_razzhatiya():
        pass

    assert media_service.bez_obshchego_zamka_total() > bylo, "авария нигде не считается"
    skazano = capfd.readouterr().out
    assert "ТРЕВОГА" in skazano, f"про потерянный общий предел молчат: {skazano!r}"


def test_bez_redisa_predel_ostayotsya_i_ne_krichit(monkeypatch):
    """Адрес не задан — процесс ровно один, и предел в его памяти И ЕСТЬ общий.

    Парная проверка к предыдущей. Без неё «кричать при любом уходе в память»
    выглядело бы правильным — и набор тестов вместе с ноутбуком разработчика
    (где Redis не задан вовсе) залил бы журнал тревогами на ровном месте.
    """
    monkeypatch.setattr(redis_client, "get_client", lambda: None)
    bylo = media_service.bez_obshchego_zamka_total()

    with _Tolpa(media_service.ODNOVREMENNO + 1) as tolpa:
        assert _dozhdatsya(lambda: tolpa.vnutri >= media_service.ODNOVREMENNO)
        time.sleep(0.3)
        assert tolpa.vnutri == media_service.ODNOVREMENNO, "предел не держится вовсе"
    assert not tolpa.bedy, tolpa.bedy

    assert media_service.bez_obshchego_zamka_total() == bylo, (
        "ненастроенный Redis посчитан аварией — тревога будет звонить всегда"
    )
