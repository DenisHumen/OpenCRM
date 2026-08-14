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

import pytest

from core import exceptions as errors
from core import ratelimit, redis_client
from core.ratelimit import SlidingWindowLimiter


class FakeRedis:
    """Подставной Redis: сортированные множества в словаре.

    Знает `zadd`, `zremrangebyscore`, `zcard`, `expire`, `delete` и `scan_iter`
    — весь словарь ограничителя. Конвейер (`pipeline`) исполняется по-честному
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
