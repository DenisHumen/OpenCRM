"""Счётчик попыток общий на все процессы, и он отказывает вслух.

Этот файл — про то, ради чего в продукт заводился Redis. Переезд на MySQL
снимает запрет на несколько рабочих процессов, и вместе с ним снимает
единственное, на чём держалась защита от подбора: счётчик, живший в памяти
процесса. Восемь процессов — восемь независимых счётчиков, порог умножается на
восемь, и НИКАКОЙ ОШИБКИ при этом не возникает. Именно поэтому проверки здесь
устроены так, чтобы ловить не отказ, а МОЛЧАНИЕ.

Живого Redis в наборе тестов нет и не будет — набор гоняется без внешних служб
(то же ограничение, что у MySQL, см. `tests/test_mysql_portability.py`).
Поэтому клиент подменяется: маленький подставной Redis в памяти, знающий ровно
те пять команд, которыми пользуется ограничитель. Подставной он общий на всех,
и это главное его свойство: два ограничителя, взявшие его, изображают два
рабочих процесса с одним хранилищем.
"""

import uuid

import pytest

from core import exceptions as errors
from core import ratelimit, redis_client
from core.ratelimit import SlidingWindowLimiter


class FakeRedis:
    """Подставной Redis: сортированные множества в словаре.

    Знает `zadd`, `zrem`, `zremrangebyscore`, `zcard`, `expire`, `delete`,
    `scan_iter` и `eval` — весь словарь ограничителя. Конвейер (`pipeline`) исполняется по-честному
    по очереди: атомарность здесь изображать нечем и незачем, проверяется
    поведение счётчика, а не гарантии сервера.
    """

    def __init__(self):
        self.data: dict[str, dict[str, float]] = {}

    # --- команды ---
    def zadd(self, key, mapping):
        self.data.setdefault(key, {}).update(mapping)
        return len(mapping)

    def zremrangebyscore(self, key, mn, mx):
        chleny = self.data.get(key)
        if not chleny:
            return 0
        nizhe = float("-inf") if mn == "-inf" else float(mn)
        lishnie = [c for c, s in chleny.items() if nizhe <= s <= float(mx)]
        for c in lishnie:
            del chleny[c]
        if not chleny:
            del self.data[key]
        return len(lishnie)

    def zcard(self, key):
        return len(self.data.get(key, {}))

    def expire(self, key, ttl):
        return 1 if key in self.data else 0

    def delete(self, key):
        return 1 if self.data.pop(key, None) is not None else 0

    def scan_iter(self, match=None, count=None):
        pristavka = match.rstrip("*") if match else ""
        return [k for k in list(self.data) if k.startswith(pristavka)]

    def eval(self, script, numkeys, *args):
        """Единственный скрипт ограничителя (`_ZANYAT`), переписанный на python.

        ЧЕСТНО О ЦЕНЕ. Это КОПИЯ смысла скрипта, а не его исполнение: разойдись
        `_ZANYAT` с этой копией — проверки здесь не заметят, за совпадение
        отвечает живой прогон с настоящим Redis. Настоящее здесь всё остальное,
        и ради него копия и написана: общее хранилище на два ограничителя,
        приставка в ключе, срок, окно, отказ при недоступном сервере.

        Атомарность не изображается — изображать её нечем, а проверяется она
        потоками на том хранилище, которое стоит в бою.
        """
        if "zremrangebyscore" not in script or "zcard" not in script:
            raise NotImplementedError("подставной Redis знает один скрипт — _ZANYAT")
        klyuch = args[0]
        nizhe, ochko, metka, predel, srok = args[1:6]
        self.zremrangebyscore(klyuch, "-inf", nizhe)
        if self.zcard(klyuch) >= int(predel):
            return 1
        self.zadd(klyuch, {metka: float(ochko)})
        self.expire(klyuch, srok)
        return 0

    def zrem(self, key, *chleny):
        hranilishche = self.data.get(key)
        if not hranilishche:
            return 0
        ubrano = 0
        for chlen in chleny:
            if hranilishche.pop(chlen, None) is not None:
                ubrano += 1
        if not hranilishche:
            del self.data[key]
        return ubrano

    # --- конвейер ---
    def pipeline(self, transaction=True):
        return _FakePipeline(self)


class _FakePipeline:
    def __init__(self, client):
        self.client = client
        self.ochered = []

    def __getattr__(self, imya):
        def otlozhit(*args, **kwargs):
            self.ochered.append((imya, args, kwargs))
            return self

        return otlozhit

    def execute(self):
        itogi = []
        for imya, args, kwargs in self.ochered:
            itogi.append(getattr(self.client, imya)(*args, **kwargs))
        self.ochered = []
        return itogi


class BrokenRedis:
    """Redis, который не отвечает. Любая команда — отказ."""

    def __getattr__(self, imya):
        def upast(*args, **kwargs):
            raise ConnectionError("redis не отвечает")

        return upast


@pytest.fixture
def obshchiy(monkeypatch):
    """Общее хранилище на всех, кто спросит клиента."""
    fake = FakeRedis()
    monkeypatch.setattr(redis_client, "get_client", lambda: fake)
    return fake


@pytest.fixture
def slomannyy(monkeypatch):
    monkeypatch.setattr(redis_client, "get_client", lambda: BrokenRedis())


# --- счётчик общий ------------------------------------------------------------


def test_dva_processa_delyat_odin_schyotchik(obshchiy):
    """ТА САМАЯ ТИХАЯ ДЫРА, ради которой всё затевалось.

    Два экземпляра ограничителя — это два рабочих процесса uvicorn: у каждого
    свой объект, своя память, свой замок. Пока счётчик жил в памяти, каждый
    честно считал свои пять попыток, и перебор просто попадал по кругу в разные
    процессы: порог удваивался, а ошибки не возникало ни одной.
    """
    pervyy = SlidingWindowLimiter(3, 60, name="login")
    vtoroy = SlidingWindowLimiter(3, 60, name="login")

    pervyy.record_failure("ivan@example.com")
    pervyy.record_failure("ivan@example.com")
    # Третью неудачу видит СОСЕД — и она обязана добить порог, а не начать счёт
    # заново.
    vtoroy.record_failure("ivan@example.com")

    assert pervyy.is_blocked("ivan@example.com")
    assert vtoroy.is_blocked("ivan@example.com"), (
        "второй процесс считает свои попытки отдельно — порог умножается на "
        "число процессов, и снаружи это выглядит как работающая защита"
    )


def test_uspeshnyy_vhod_snimaet_blokirovku_u_vseh(obshchiy):
    pervyy = SlidingWindowLimiter(2, 60, name="login")
    vtoroy = SlidingWindowLimiter(2, 60, name="login")
    pervyy.record_failure("ivan@example.com")
    pervyy.record_failure("ivan@example.com")
    assert vtoroy.is_blocked("ivan@example.com")

    vtoroy.reset("ivan@example.com")
    assert not pervyy.is_blocked("ivan@example.com"), "сброс дошёл не до всех"


def test_ogranichiteli_ne_smeshivayutsya(obshchiy):
    """У подбора PIN и перебора номеров бланков ключ ОДИН И ТОТ ЖЕ — хэш адреса
    посетителя. Без раздельных отсеков они считались бы в один счётчик, и
    двадцать обращений к бланкам запирали бы человеку доступ по PIN."""
    pin = SlidingWindowLimiter(2, 60, name="pin")
    doc = SlidingWindowLimiter(2, 60, name="document")

    doc.record_failure("hash-adresa")
    doc.record_failure("hash-adresa")

    assert doc.is_blocked("hash-adresa")
    assert not pin.is_blocked("hash-adresa"), "ограничители сложились в один"


def test_okno_otpuskaet_i_v_obshchem_hranilishche(obshchiy):
    limiter = SlidingWindowLimiter(1, 60, name="login")
    limiter.record_failure("ivan@example.com")
    assert limiter.is_blocked("ivan@example.com")

    # Двигаем не часы, а сами отметки: тест про окно, а не про точность сна.
    kluch = f"{redis_client.PREFIX}rl:login:ivan@example.com"
    obshchiy.data[kluch] = {chlen: score - 120 for chlen, score in obshchiy.data[kluch].items()}
    assert not limiter.is_blocked("ivan@example.com"), "окно не отпустило"


def test_chitayushchaya_proverka_nichego_ne_zavodit(obshchiy):
    """Проверка вызывается на КАЖДОМ входе, в том числе удачном с первого раза.

    Заводи она ключ — след в хранилище оставлял бы каждый, кто хоть раз ввёл
    адрес, а адреса приходят с улицы.
    """
    limiter = SlidingWindowLimiter(3, 60, name="login")
    for i in range(50):
        assert not limiter.is_blocked(f"nobody-{i}@example.com")
    assert limiter.tracked() == 0, "проверка завела ключи, которых не было"


def test_u_kazhdogo_klyucha_est_srok(obshchiy, monkeypatch):
    """Уборка по порогу (`SWEEP_AFTER`) в общем хранилище не нужна и не годится:
    ключи чужие, процессов много, и договориться, кто убирает, некому. Вместо
    неё — TTL на каждом ключе, ставящийся при каждой записи."""
    postavleno = []
    nastoyashchiy = obshchiy.expire
    monkeypatch.setattr(
        obshchiy, "expire", lambda key, ttl: (postavleno.append(ttl), nastoyashchiy(key, ttl))[1]
    )
    SlidingWindowLimiter(5, 900, name="login").record_failure("ivan@example.com")
    assert postavleno, "ключ записан без срока — он останется в памяти навсегда"
    assert postavleno[0] >= 900, "срок короче окна: блокировка отпустит досрочно"


# --- Redis недоступен: отказ, а не молчаливое ослабление ----------------------


def test_nedostupnyy_redis_ne_puskaet(slomannyy):
    """РЕШЕНИЕ, КОТОРОЕ НАДО ЗНАТЬ: недоступный счётчик означает ОТКАЗ.

    Выбор был между «пропускать всех, крича в лог» и «отказывать». Пропускать
    нельзя: ограничитель сторожит вход, и без счётчика он не отличит шестую
    попытку от первой — то есть подбор идёт без всякого предела, причём ровно
    тогда, когда за системой никто не смотрит, и бесшумно для подбирающего.
    Атака на Redis превращалась бы в обход защиты входа.
    """
    limiter = SlidingWindowLimiter(5, 900, name="login")
    with pytest.raises(errors.LimiterUnavailableError):
        limiter.is_blocked("ivan@example.com")


def test_otkaz_eto_503_a_ne_429(slomannyy):
    """429 клиент вправе понять как «подожди и повтори с тем же успехом», а
    здесь дело не в стучащем, а в нашей службе."""
    limiter = SlidingWindowLimiter(5, 900, name="login")
    try:
        limiter.is_blocked("ivan@example.com")
    except errors.LimiterUnavailableError as exc:
        assert exc.http_status == 503
        assert exc.code == "limiter_unavailable"
    else:
        pytest.fail("недоступный счётчик никого не остановил")


def test_pro_nedostupnyy_schyotchik_govoryat_vsluh(slomannyy):
    """Тихого варианта здесь нет. Счётчик аварий уезжает в метрики
    (`opencrm_ratelimiter_unavailable_total`), по нему настроена тревога."""
    bylo = ratelimit.unavailable_total()
    limiter = SlidingWindowLimiter(5, 900, name="login")
    with pytest.raises(errors.LimiterUnavailableError):
        limiter.is_blocked("ivan@example.com")
    assert ratelimit.unavailable_total() > bylo, "авария нигде не считается"


def test_otmetka_neudachi_ne_ronyaet_zapros(slomannyy):
    """Вердикт по текущему запросу уже вынесен проверкой, а следующий запрос всё
    равно упрётся в отказ. Превращать бухгалтерию в пятисотую ошибку незачем."""
    limiter = SlidingWindowLimiter(5, 900, name="login")
    limiter.record_failure("ivan@example.com")  # не должно бросить
    limiter.reset("ivan@example.com")  # и это тоже


# --- запрет на несколько процессов без общего счётчика ------------------------


def test_neskolko_processov_bez_redis_otvergayutsya():
    """Разрешение на несколько процессов НЕ выводится из одного признака.

    Пока проверялось одно — «база держит нескольких писателей», — восемь
    процессов разрешались разом и молча. Условий два, и второе — общий счётчик.
    """
    from config.settings import Settings

    bez_redis = Settings(
        _env_file=None,
        workers=4,
        db_url="mysql+pymysql://user:pass@host/opencrm",
        redis_url="",
    )
    zhaloby = " ".join(bez_redis.config_errors())
    assert "OPENCRM_REDIS_URL" in zhaloby, (
        "восемь процессов без общего счётчика поднимаются молча — порог защиты "
        "от подбора умножается на восемь"
    )

    s_redis = Settings(
        _env_file=None,
        workers=4,
        db_url="mysql+pymysql://user:pass@host/opencrm",
        redis_url="redis://:pass@redis:6379/0",
    )
    assert not any("OPENCRM_WORKERS" in stroka for stroka in s_redis.config_errors())


def test_boevaya_ustanovka_bez_redis_ne_startuet():
    """Даже с одним процессом: `OPENCRM_WORKERS` правят снаружи, в docker/.env,
    и правят ровно тогда, когда нагрузка выросла, — то есть в момент, когда про
    ограничитель точно не вспомнят."""
    from config.settings import Settings

    prod = Settings(
        _env_file=None,
        env="production",
        secret_key="a-real-random-secret-value",
        ip_hash_salt="a-real-random-salt",
        root_password="root-password-1",
        base_url="https://studio.example.org",
        redis_url="",
    )
    assert any("OPENCRM_REDIS_URL" in stroka for stroka in prod.config_errors())


# --- /healthz не имеет права ходить в Redis -----------------------------------


def test_healthz_ne_oprashivaet_redis(monkeypatch):
    """Найдено живым прогоном, и это не мелочь.

    С остановленным контейнером разрешение имени `redis` не укладывается в
    пятисекундный потолок проверки здоровья docker: `/healthz` отваливался по
    таймауту, и docker объявлял нездоровым ПРИЛОЖЕНИЕ. Получалась связка «лежит
    соседняя служба → перезапускают сайт», причём на боевом сервере к ней
    прибавился бы откат исправного обновления: `deploy/updater.py` ждёт ровно
    этот ответ.

    Поэтому состояние Redis в `/healthz` берётся по факту работы ограничителя, а
    живой опрос стоит в `/api/v1/metrics`, где медленный ответ стоит одного
    пропущенного среза.
    """
    from fastapi.testclient import TestClient

    from web.main import app

    monkeypatch.setattr(redis_client, "configured", lambda: True)

    def ne_zvat():
        raise AssertionError("/healthz опрашивает Redis — авария соседа уронит сайт")

    monkeypatch.setattr(redis_client, "ping", ne_zvat)

    body = TestClient(app).get("/healthz").json()
    assert body["status"] == "ok"
    assert body["redis"] in ("ok", "down")


def test_healthz_govorit_down_posle_avarii(monkeypatch, slomannyy):
    """А сказать про аварию всё равно обязан — иначе поле бесполезно."""
    from fastapi.testclient import TestClient

    from web.main import app

    monkeypatch.setattr(redis_client, "configured", lambda: True)
    limiter = SlidingWindowLimiter(5, 900, name="login")
    with pytest.raises(errors.LimiterUnavailableError):
        limiter.is_blocked("ivan@example.com")

    assert TestClient(app).get("/healthz").json()["redis"] == "down"


def test_posle_vozvrashcheniya_redis_healthz_perestayot_zhalovatsya(monkeypatch, obshchiy):
    """Отбой тревоги. Проверено живьём: после перезапуска всего стека первый
    вход попал в окно, когда Redis ещё поднимался, — и `/healthz` держал бы
    «down» ещё минуту после того, как служба уже отвечает."""
    from fastapi.testclient import TestClient

    from web.main import app

    monkeypatch.setattr(redis_client, "configured", lambda: True)
    ratelimit._last_failure = ratelimit.time.monotonic()
    assert TestClient(app).get("/healthz").json()["redis"] == "down"

    # Одно удачное обращение к живому счётчику — и жалоба снята.
    SlidingWindowLimiter(5, 900, name="login").is_blocked("ivan@example.com")
    assert TestClient(app).get("/healthz").json()["redis"] == "ok"


# --- переключатель между общим счётчиком и счётчиком в памяти -----------------
#
# ДЫРА, НАЙДЕННАЯ ЛОМАНИЕМ. Подмена `redis_client.configured()` на «всегда
# ложь» не роняла ни одного теста во всём наборе — а это единственный
# переключатель между общим счётчиком и счётчиком в памяти процесса. То есть
# правка в одну строку молча возвращала продукт ровно к тому состоянию, ради
# ухода от которого заводился Redis: восемь процессов, восемь независимых
# счётчиков, порог защиты умножен на восемь, ни единой ошибки.
#
# Не ловилось потому, что все проверки выше подменяют `get_client` НАПРЯМУЮ, и
# `configured()` в них не исполняется вовсе. Удобно для проверки самого
# счётчика — и слепо ровно к тому месту, где решается, будет ли он общим.
#
# Поэтому две проверки ниже идут через НАСТОЯЩИЙ `get_client`, а подменяется в
# них только то, что лежит за границей процесса: строка настроек и конструктор
# клиента redis-py. Парой, а не по одной: «всегда ложь» ловит первая, «всегда
# истина» — вторая, и выключить переключатель нельзя ни в ту, ни в другую
# сторону.


class _Nastroyki:
    """Ровно то, что спрашивает `configured()`."""

    def __init__(self, redis_url: str):
        self.redis_url = redis_url


@pytest.fixture
def bez_klienta():
    """Клиент кэшируется на процесс — забываем его до и после."""
    redis_client.reset_client()
    yield
    redis_client.reset_client()


def test_zadannyy_adres_daet_obshchiy_schyotchik(monkeypatch, bez_klienta):
    """Адрес задан — счётчик общий, и это проверяется по последствию.

    Не «`configured()` вернул True», а «два ограничителя разделили счётчик»:
    проверка на возвращаемое значение переставала бы что-либо значить в тот
    день, когда переключатель переедет в другое место.
    """
    hranilishche = FakeRedis()
    monkeypatch.setattr(redis_client, "get_settings", lambda: _Nastroyki("redis://redis:6379/0"))
    monkeypatch.setattr("redis.Redis.from_url", lambda *args, **kwargs: hranilishche)

    pervyy = SlidingWindowLimiter(3, 900, name="proba")
    vtoroy = SlidingWindowLimiter(3, 900, name="proba")
    for _ in range(3):
        pervyy.record_failure("ivan@example.com")

    assert hranilishche.data, (
        "ограничитель не пошёл в Redis вовсе, хотя адрес задан — счётчик остался "
        "в памяти процесса, и порог защиты от подбора множится на число процессов"
    )
    assert vtoroy.is_blocked("ivan@example.com"), (
        "второй процесс не видит попыток первого — общего счётчика нет"
    )


def test_pustoy_adres_ostavlyaet_schyotchik_v_pamyati(monkeypatch, bez_klienta):
    """Парная проверка: без Redis приложение обязано подниматься.

    Пустой адрес — единственный способ поднять систему без Redis: ноутбук
    разработчика и этот самый набор тестов. Запретить его нельзя, а вот
    незаметно перестать различать пустой и заданный — можно, и тогда набор
    тестов начал бы падать на попытке дозвониться в никуда.
    """
    monkeypatch.setattr(redis_client, "get_settings", lambda: _Nastroyki("   "))

    def ne_zvat(*args, **kwargs):
        raise AssertionError("клиент Redis заводится при пустом адресе")

    monkeypatch.setattr("redis.Redis.from_url", ne_zvat)

    assert redis_client.get_client() is None
    # И счётчик при этом работает — свой, в памяти процесса.
    limiter = SlidingWindowLimiter(2, 900, name="proba")
    limiter.record_failure("ivan@example.com")
    limiter.record_failure("ivan@example.com")
    assert limiter.is_blocked("ivan@example.com")


# --- гонка «проверил → записал» -------------------------------------------------


def test_odnovremennye_popytki_ne_prohodyat_mimo_poroga():
    """Порог держится и при одновременных запросах, а не только при последовательных.

    **Ровно эта дыра и была.** Проверка и отметка были двумя операциями, и между
    ними существовало окно: шестнадцать одновременных запросов читали один и тот
    же счёт (ноль), проходили проверку все шестнадцать, и порог в пять попыток
    превращался в шестнадцать. Подбор идёт именно так — пачкой, а не по одной
    попытке в секунду, — то есть защита не работала ровно в том случае, ради
    которого написана.

    Последовательным набором это не ловится вовсе: там окна не бывает. Поэтому
    проверка бьёт потоками и стартует их по барьеру, чтобы они пришли разом.
    """
    import threading

    predel = 5
    limiter = SlidingWindowLimiter(predel, 60, name=f"gonka-{uuid.uuid4().hex[:8]}")
    limiter.ochistit()

    zheludkov = 24
    razom = threading.Barrier(zheludkov)
    proshli: list[bool] = []
    zamok = threading.Lock()

    def udar():
        razom.wait()
        zanyato = limiter.proverit_i_zanyat("odin-adres")
        with zamok:
            proshli.append(not zanyato)

    potoki = [threading.Thread(target=udar) for _ in range(zheludkov)]
    for p in potoki:
        p.start()
    for p in potoki:
        p.join()

    assert sum(proshli) == predel, (
        f"порог {predel}, а прошло {sum(proshli)} из {zheludkov} одновременных — "
        "между проверкой и отметкой снова окно"
    )
    # И следующая попытка тоже упирается: занятые места никуда не делись.
    assert limiter.proverit_i_zanyat("odin-adres") is True
    limiter.ochistit()


def test_zanyatye_mesta_ne_meshayut_sosednemu_klyuchu():
    """Счёт ведётся по ключу, а не общий: один перебирающий не запирает остальных."""
    limiter = SlidingWindowLimiter(2, 60, name=f"sosedi-{uuid.uuid4().hex[:8]}")
    limiter.ochistit()
    assert limiter.proverit_i_zanyat("adres-a") is False
    assert limiter.proverit_i_zanyat("adres-a") is False
    assert limiter.proverit_i_zanyat("adres-a") is True, "порог у своего ключа не сработал"
    assert limiter.proverit_i_zanyat("adres-b") is False, "чужой ключ заперт вместе с этим"
    limiter.ochistit()


def test_udachnyy_vhod_osvobozhdayet_zanyatoe():
    """Занятое место — не наказание: вошедший обнуляет счёт.

    Без этого считать ПОПЫТКИ вместо неудач было бы нельзя: человек, входящий по
    десять раз на дню, выбирал бы свой же лимит на ровном месте.
    """
    limiter = SlidingWindowLimiter(3, 60, name=f"sbros-{uuid.uuid4().hex[:8]}")
    limiter.ochistit()
    assert limiter.proverit_i_zanyat("chelovek") is False
    assert limiter.proverit_i_zanyat("chelovek") is False
    limiter.reset("chelovek")
    for _ in range(3):
        assert limiter.proverit_i_zanyat("chelovek") is False, "сброс не освободил места"
    assert limiter.proverit_i_zanyat("chelovek") is True
    limiter.ochistit()


# --- занять место: то же обещание, но для того пути, которым ходят в бою ------
#
# ПОЧЕМУ ЭТИ ПРОВЕРКИ ПОЯВИЛИСЬ ОТДЕЛЬНО. Проверки выше бьют в `is_blocked`, а
# после закрытия гонки ни один из пяти путей, обязанных отказывать при лежащем
# счётчике, туда больше не ходит: вход, PIN, заявки, вебхук АТС и страница
# состояния заказа зовут `zanyat_mesto`/`proverit_i_zanyat`. Сторож остался
# сторожить метод, которым это обещание больше никто не исполняет — то есть
# правка «а давайте при недоступном Redis считать в памяти» прошла бы мимо
# всего набора. Найдено разбором самой правки.


def test_zanyat_mesto_pri_nedostupnom_redise_ne_puskaet(slomannyy):
    """То же решение, что и у `is_blocked`, и оно обязано держаться здесь.

    Соблазн конкретный и в одну строку: двумя строками выше в `zanyat_mesto`
    стоит `if client is None: return self._zanyat_mesto_local(key)`, и написать
    то же самое в `except` кажется естественным. Итог — восемь воркеров, восемь
    независимых счётчиков и порог, умноженный на восемь, ровно на время аварии
    и бесшумно для подбирающего.
    """
    limiter = SlidingWindowLimiter(5, 900, name="login")
    with pytest.raises(errors.LimiterUnavailableError) as beda:
        limiter.zanyat_mesto("ivan@example.com")
    assert beda.value.http_status == 503, "429 значит «повтори», а дело не в стучащем"
    assert beda.value.code == "limiter_unavailable"


def test_proverit_i_zanyat_pri_nedostupnom_redise_ne_puskaet(slomannyy):
    """Короткая обёртка обязана нести то же обещание, что и полная."""
    limiter = SlidingWindowLimiter(5, 900, name="pin")
    with pytest.raises(errors.LimiterUnavailableError):
        limiter.proverit_i_zanyat("hash-adresa")


def test_avariya_na_zanyatii_mesta_schitaetsya(slomannyy):
    """Тихого варианта нет и здесь: авария уезжает в метрику, по ней тревога."""
    bylo = ratelimit.unavailable_total()
    limiter = SlidingWindowLimiter(5, 900, name="login")
    with pytest.raises(errors.LimiterUnavailableError):
        limiter.zanyat_mesto("ivan@example.com")
    assert ratelimit.unavailable_total() > bylo, "авария нигде не считается"


def test_vozvrat_popytki_ne_ronyaet_zapros(slomannyy):
    """Возврат места — бухгалтерия, как и отметка неудачи.

    Зовут его тогда, когда запрос УЖЕ рушится (упало чтение пользователя), и
    вторая беда поверх первой ничего не улучшит: человек всё равно получит
    пятисотую, а лишняя занятая попытка отпустит сама по истечении окна.
    """
    limiter = SlidingWindowLimiter(5, 900, name="login")
    limiter.vernut("ivan@example.com", "1.0:abc")  # не должно бросить


def test_zanyatoe_mesto_vozvrashchaetsya_po_odnoy_shtuke(obshchiy):
    """`vernut` снимает ОДНУ попытку, а не сбрасывает счёт.

    Разница не косметическая. `reset` сносит ключ целиком — вместе со всеми
    попытками, набранными до нас. Тот, кто умеет вызвать нашу беду (уронить
    базу), обнулял бы этим весь набранный счёт подбора, и возврат места стал бы
    способом обойти ограничитель.
    """
    limiter = SlidingWindowLimiter(3, 60, name="login")
    pervaya = limiter.zanyat_mesto("ivan@example.com")
    limiter.zanyat_mesto("ivan@example.com")
    moya = limiter.zanyat_mesto("ivan@example.com")
    assert limiter.proverit_i_zanyat("ivan@example.com"), "порог не сработал вовсе"

    limiter.vernut("ivan@example.com", moya)

    assert not limiter.proverit_i_zanyat("ivan@example.com"), "возвращённое место не освободилось"
    assert limiter.proverit_i_zanyat("ivan@example.com"), (
        "вернулась не одна попытка, а больше: чужие, набранные честно, пропали"
    )
    assert pervaya is not None


def test_u_zanyatogo_mesta_est_srok(obshchiy, monkeypatch):
    """TTL ставится и на этом пути, а не только при отметке неудачи.

    Ключи наполняются с улицы (адрес почты, хэш адреса посетителя). Ключ без
    срока в ОБЩЕМ хранилище остаётся там навсегда, и уборка по порогу
    (`SWEEP_AFTER`) здесь не поможет: хранилище чужое, процессов много,
    договориться, кто убирает, некому.
    """
    postavleno = []
    nastoyashchiy = obshchiy.expire
    monkeypatch.setattr(
        obshchiy, "expire", lambda key, ttl: (postavleno.append(ttl), nastoyashchiy(key, ttl))[1]
    )
    SlidingWindowLimiter(5, 900, name="login").zanyat_mesto("ivan@example.com")
    assert postavleno, "ключ записан без срока — он останется в хранилище навсегда"
    assert postavleno[0] >= 900, "срок короче окна: блокировка отпустит досрочно"


def test_mesta_schitayutsya_obshchim_hranilishchem(obshchiy):
    """Два процесса делят один счёт и на этом пути — иначе всё затевалось зря."""
    pervyy = SlidingWindowLimiter(3, 60, name="login")
    vtoroy = SlidingWindowLimiter(3, 60, name="login")

    assert not pervyy.proverit_i_zanyat("ivan@example.com")
    assert not pervyy.proverit_i_zanyat("ivan@example.com")
    # Третье место занимает СОСЕД — и оно обязано добить порог, а не начать
    # счёт заново.
    assert not vtoroy.proverit_i_zanyat("ivan@example.com")

    assert pervyy.proverit_i_zanyat("ivan@example.com")
    assert vtoroy.proverit_i_zanyat("ivan@example.com"), (
        "второй процесс считает свои места отдельно — порог умножается на число "
        "процессов, и снаружи это выглядит как работающая защита"
    )
