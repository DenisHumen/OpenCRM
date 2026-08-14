"""Установка на MySQL: служба базы в compose и выбор базы в opencrm.sh.

Всё, что здесь проверяется, ломается молча и обнаруживается на чужом сервере.
Служба базы, поднявшаяся при выборе SQLite, — это полгигабайта памяти в никуда;
проверка здоровья, отвечающая раньше времени, — приложение, стартовавшее до
базы; пароль, попавший в командную строку, — доступ к базе для всех, кто в этот
момент оказался рядом.

Файлы читаются как текст, а не разбираются YAML-разборщиком: словарь
зависимостей приложения не должен расти ради тестов, а формат этих файлов
меняется редко (та же договорённость, что и в `tests/test_deploy_config.py`).
"""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = ROOT / "docker" / "docker-compose.yml"
SCRIPT = ROOT / "opencrm.sh"

#: Перевод строки под рукой: литералы с ним в тестах читаются хуже.
NL = chr(10)

pytestmark = pytest.mark.skipif(
    not COMPOSE.exists() or not SCRIPT.exists(), reason="обвязки развёртывания рядом нет"
)


def _compose() -> str:
    return COMPOSE.read_text(encoding="utf-8")


def _script() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _telo(imya: str) -> str:
    """Тело функции оболочки по имени — от заголовка до закрывающей скобки.

    Отдельным помощником, потому что литерал с переводами строк в каждом
    тесте читается хуже и ломается от любой перестановки.
    """
    text = _script()
    kusok = text[text.index(f"{imya}() {{"):]
    return kusok[: kusok.index(NL + "}" + NL)]


def _odnoy_strokoy(kusok: str) -> str:
    """Кусок скрипта со склеенными переносами строк.

    Обратная косая в конце строки — не конец команды. Проверка, читающая скрипт
    построчно, разрезала бы одну команду пополам и находила бы её половинки в
    соседних, ничего при этом не стерегя. Найдено нарочной поломкой сторожа.
    """
    return " ".join(kusok.split()).replace("\\ ", "")


def _sluzhba(imya: str, sleduyushchaya: str) -> str:
    """Кусок compose-файла от одной службы до следующей.

    Границы называются обе, и это не педантизм: пока хвост резался по
    «до `nginx:`», вставка службы redis между базой и nginx молча утащила бы её
    в кусок про базу — и проверки про базу стали бы проверять redis заодно,
    оставаясь зелёными.
    """
    text = _compose()
    nachalo = text.index(f"\n  {imya}:")
    hvost = text[nachalo + 1:]
    return hvost[: hvost.index(f"\n  {sleduyushchaya}:")]


def _sluzhba_db() -> str:
    return _sluzhba("db", "redis")


def _sluzhba_redis() -> str:
    return _sluzhba("redis", "nginx")


# --- служба базы --------------------------------------------------------------


def test_baza_podnimaetsya_vsegda_bez_profilya():
    """База обязательна, как и redis, — значит профиля у неё быть не должно.

    Пока выбор между SQLite и MySQL существовал, служба стояла под профилем:
    лишний сервер съедал полгигабайта там, где база лежала одним файлом. Выбора
    больше нет, и профиль превратился бы в способ поднять сайт без базы вовсе.
    """
    assert "profiles:" not in _sluzhba_db(), "служба базы снова под профилем"
    # И установщик её профиль больше не включает: включать нечего.
    assert "compose_profile mysql" not in _script(), (
        "установщик всё ещё правит профиль базы — а профиля больше нет"
    )


def test_proverka_zdorovya_idyot_po_tcp():
    """Сокет отвечает РАНЬШЕ, чем сервер начинает слушать порт.

    При первом запуске образ MySQL поднимает временный сервер с выключенной
    сетью, чтобы создать базу и пользователя. Проверено живьём: с проверкой по
    сокету контейнер объявлялся healthy на седьмой секунде, а TCP-подключение в
    этот момент ещё отвергалось. Приложение по такому сигналу стартует раньше
    базы и падает на `alembic upgrade head`, уходя в цикл перезапуска.
    """
    db = _sluzhba_db()
    proverka = db[db.index("healthcheck:"):]
    assert "mysqladmin ping" in proverka, "проверки здоровья базы нет вовсе"
    assert "--protocol=TCP" in proverka, (
        "проверка снова ходит через unix-сокет — он отвечает до того, как "
        "сервер начнёт слушать TCP"
    )
    assert "start_period" in proverka, (
        "нет окна запуска — первое создание каталога данных на медленном диске "
        "объявят поломкой"
    )


def test_prilozhenie_zhdyot_gotovnosti_bazy():
    """И при этом поднимается, когда базы в стеке нет.

    `condition: service_healthy` без `required: false` сломал бы установку на
    SQLite: compose отказался бы стартовать, сославшись на службу, которой в
    стеке нет по замыслу.
    """
    text = _compose()
    app = text[text.index("\n  app:"): text.index("\n  db:")]
    zavisimost = app[app.index("depends_on:"):]
    assert "db:" in zavisimost
    assert "condition: service_healthy" in zavisimost, "приложение стартует раньше базы"
    assert "required: false" in zavisimost, "установка на SQLite не поднимется"


def test_kodirovka_utf8mb4_zadana_serveru():
    """utf8 в MySQL — это три байта на символ.

    Эмодзи в заметке клиента обрывают вставку на полуслове. Кодировка задаётся
    серверу, а не только клиенту: база создаётся при первом старте контейнера,
    и созданная в latin1 останется такой навсегда.
    """
    db = _sluzhba_db()
    assert "--character-set-server=utf8mb4" in db
    assert "utf8mb4" in _script(), "URL приложения собирается без charset=utf8mb4"
    assert "charset=utf8mb4" in _script()


def test_dannye_bazy_lezhat_obychnym_katalogom():
    """Именованный том лежит в /var/lib/docker, его не видно обычными
    средствами и он уезжает вместе с `docker system prune --volumes`.

    Ровно по этой причине всё остальное состояние продукта живёт обычными
    каталогами; база — не то место, где стоит делать исключение.
    """
    db = _sluzhba_db()
    assert "/var/lib/mysql" in db, "каталог данных базы никуда не подключён"
    assert "OPENCRM_HOME" in db, "данные базы лежат внутри Docker, а не в состоянии установки"
    # Раздел volumes верхнего уровня — признак именованного тома.
    assert "\nvolumes:" not in _compose(), "появился именованный том"


def test_url_bazy_ne_perekryvaetsya_compose_om():
    """`environment` перекрывает `env_file` — и это уже ловушка этого файла.

    Строка `OPENCRM_DB_URL:` в `environment` сделала бы выбор базы в config/.env
    бессмысленным: установщик пишет туда MySQL с паролем, а compose молча
    возвращал бы всех на SQLite. Ровно так же однажды не сработал
    OPENCRM_TRUSTED_PROXY_HOPS, только наоборот.
    """
    text = _compose()
    app = text[text.index("\n  app:"): text.index("\n  db:")]
    okruzhenie = app[app.index("environment:"): app.index("volumes:")]
    assert "OPENCRM_DB_URL:" not in okruzhenie, (
        "compose снова задаёт URL базы и перекрывает выбор в config/.env"
    )


# --- база при установке: только MySQL -----------------------------------------
#
# Выбора больше нет. SQLite допускала ровно одного писателя, поэтому на ней был
# невозможен второй рабочий процесс, а значит установка, начатая «попроще»,
# рано или поздно упиралась в переезд — в закрытый сайт и самую опасную
# операцию, какая в проекте была. Её убрали вместе с самим переездом.


def test_baza_nastraivaetsya_i_vyzyvaetsya_pri_ustanovke():
    text = _script()
    assert "nastroit_mysql()" in text, "настройка базы пропала"
    ustanovka = text[text.index("cmd_install() {"): text.index("need_install() {")]
    assert "nastroit_mysql" in ustanovka, "настройка базы выпала из мастера установки"
    # Строго после configure_app_env: раньше config/.env ещё не создан, и адрес
    # ушёл бы в файл мимо шаблона.
    assert ustanovka.index("configure_app_env") < ustanovka.index("nastroit_mysql")


def test_pro_sqlite_nichego_ne_sprashivayut():
    """Вопроса больше нет — и подсказанного ответа тоже.

    Пока вопрос стоял, `--yes` брал умолчание, и одна перестановка пунктов
    меняла бы то, на чём поднимется установка.
    """
    nastroyka = _telo("nastroit_mysql")
    assert "ask " not in nastroyka, "про базу снова спрашивают"
    assert "sqlite" not in nastroyka.lower(), "SQLite вернулась в настройку базы"


def test_parol_bazy_generiruetsya_a_ne_sprashivaetsya():
    """Спрошенный пароль базы придумывают за минуту и повторяют от сервера к
    серверу. Руками его всё равно никто не вводит — он нужен двум контейнерам,
    и оба берут его из файла."""
    nastroyka = _telo("nastroit_mysql")
    assert "gen_secret" in nastroyka, "пароль базы больше не генерируется"
    for zapros in ("Пароль базы", "Database password", "OPENCRM_DB_PASSWORD\"" ):
        assert f'ask "{zapros}' not in nastroyka, "пароль базы снова спрашивают у человека"


def test_parol_popadaet_v_oba_fayla():
    """Пароль нужен двоим: контейнеру базы (docker/.env) и приложению (внутри
    OPENCRM_DB_URL в config/.env). Записать в один — получить «access denied»
    на первом же старте."""
    nastroyka = _telo("nastroit_mysql")
    assert 'env_set "$DOCKER_ENV" OPENCRM_DB_PASSWORD' in nastroyka
    assert 'env_set "$DOCKER_ENV" OPENCRM_DB_ROOT_PASSWORD' in nastroyka
    # Целиком одной строкой: проверка «оно где-то встречается» пропускала бы
    # пропажу самого пароля из адреса.
    assert (
        'env_set "$APP_ENV" OPENCRM_DB_URL "mysql+pymysql://opencrm:$_db_pass@db:3306/opencrm?charset=utf8mb4"'
    ) in _odnoy_strokoy(nastroyka), "пароль не попадает в адрес базы"


def test_povtornaya_ustanovka_ne_pereseivaet_parol():
    """У поднятой базы пользователь уже создан с прежним паролем.

    Новый дал бы «access denied» на первом же соединении — и выглядело бы это
    как сломавшаяся база, а не как повторный запуск установки.
    """
    nastroyka = _telo("nastroit_mysql")
    assert 'env_get "$DOCKER_ENV" OPENCRM_DB_PASSWORD' in nastroyka, (
        "прежний пароль не читается — повторная установка сломает доступ к базе"
    )
    assert 'if [ -z "$_db_pass" ]; then' in nastroyka, (
        "новый пароль сеется без проверки, есть ли прежний"
    )


def test_docker_env_zakryvaetsya_pravami():
    """С выбором MySQL в docker/.env ложатся пароли базы, а `cp` переносит
    права шаблона (644): файл был бы читаем всем в системе."""
    text = _script()
    nastroyka = text[text.index("configure_docker_env() {"): text.index("create_dirs() {")]
    assert 'chmod 600 "$DOCKER_ENV"' in nastroyka, "docker/.env остаётся читаемым всем"


def test_parol_ne_uezzhaet_v_komandnuyu_stroku_hosta():
    """Аргументы процесса видит через `ps` любой пользователь сервера.

    Поэтому и пароль базы, и URL с паролем внутри раскрываются ВНУТРИ
    контейнера — из его собственного окружения, — а не подставляются здесь.
    """
    text = _script()
    damp = text[text.index("dump_mysql() {"):]
    damp = damp[: damp.index("\n}\n")]
    assert "$MYSQL_ROOT_PASSWORD" in damp
    # Признак ошибки: значение раскрыто оболочкой хоста в двойных кавычках
    # прямо в команду docker.
    assert 'exec -T db sh -c "' not in text, "пароль раскрывается на хосте"
    assert 'exec -T app sh -c "' not in text, "пароль раскрывается на хосте"


def test_kopii_i_vosstanovlenie_vidyat_dampy():
    """Смотреть только на db-*.db значило бы на установке с MySQL всегда
    докладывать «ни одной копии» — ровно то сообщение, после которого перестают
    верить всей строке."""
    text = _script()
    doctor = text[text.index("cmd_doctor() {"): text.index("why_down() {")]
    assert "db-*.sql" in doctor, "диагностика не видит копий MySQL"
    assert 'probe "$(tr_ "база" "database")"' in doctor, "не видно, на чём работает база"
    vosstanovlenie = text[text.index("cmd_restore() {"): text.index("cmd_https()")]
    assert "db-*.sql" in vosstanovlenie, "дамп нельзя выбрать для восстановления"


def test_kopiya_ot_chuzhoy_bazy_otvergaetsya_do_ostanovki_sayta():
    """Дамп MySQL не заливается в SQLite, а файл SQLite — в MySQL.

    Сказать об этом до `compose stop app` дешевле, чем после: иначе сайт
    остановлен, восстановление не состоялось, и человек остался с лежащим
    сайтом и без объяснения.
    """
    text = _script()
    vosstanovlenie = text[text.index("cmd_restore() {"): text.index("cmd_https()")]
    # И сама сверка, и её место. Ветки `*.sql:sqlite` остаются на виду, даже
    # если разбирать перестали то, что нужно, — поэтому проверяется и то, ЧТО
    # подставляют в case.
    assert 'case "$_db:$(db_engine)" in' in vosstanovlenie, "вид копии не сверяют с базой"
    proverka = vosstanovlenie.index("*.sql:sqlite")
    ostanovka = vosstanovlenie.index("compose stop app")
    assert proverka < ostanovka, "несовместимость замечают уже после остановки сайта"


def test_bekap_snimaet_damp_v_konteynere_bazy():
    """Клиента mysqldump в образе приложения нет — он лежит в образе базы.

    Скрипт копирования при этом остаётся один на обе базы: имя по дате,
    ротация, ключ шифрования и проверка годности не имеют права разъехаться на
    две реализации.
    """
    text = _script()
    bekap = text[text.index("cmd_backup() {"): text.index("cmd_restore() {")]
    assert "dump_mysql" in bekap, "дамп больше не снимается"
    assert "OPENCRM_DB_DUMP=" in bekap, "снятый дамп не передаётся скрипту копирования"
    assert "scripts/backup.sh" in bekap, "копия перестала сниматься общим скриптом"


# --- Redis: служба, которая не выключается ------------------------------------


def test_redis_podnimaetsya_vsegda_bez_profilya():
    """У `db` профиль есть, у `redis` — нет, и это разница по существу.

    «Выключенная база» — это данные в файле рядом, другой способ сделать то же
    самое. «Выключенный Redis» — это отсутствие общего счётчика попыток, то
    есть порог защиты от подбора, умноженный на число рабочих процессов, без
    единой ошибки и без следа в логах. Второго способа посчитать попытки на все
    процессы у нас нет, поэтому и выбора быть не должно.
    """
    redis = _sluzhba_redis()
    assert "profiles:" not in redis, (
        "у redis появился профиль — значит его можно выключить, а вместе с ним "
        "выключается защита от подбора пароля и PIN"
    )
    assert "image: redis:" in redis


def test_redis_ne_smotrit_naruzhu_i_pod_parolem():
    """Redis без TLS с паролем в конфиге наружу не выставляют.

    Внутри сети compose пароль тоже обязателен: в неё попадает всякий контейнер
    стека, а в Redis лежат ключи вида «этот адрес ошибся паролем пять раз».
    """
    redis = _sluzhba_redis()
    assert "ports:" not in redis, "порт Redis опубликован наружу"
    assert "expose:" in redis
    # Проверяется АРГУМЕНТ КОМАНДЫ целиком, а не вхождение строки. Найдено
    # нарочной поломкой сторожа: переименование `--requirepass` в
    # `--requirepass-OTKLYUCHENO` (redis такой ключ не знает и пускает без
    # пароля) оставляло проверку зелёной — искомая строка в новом имени есть.
    # Тот же приём уже стоит рядом у `--maxmemory-policy`, и по той же причине.
    assert "      - --requirepass\n" in redis, "Redis пускает без пароля"
    # И пароль должен приезжать следующей строкой, непустым. Пустая подстановка
    # для redis означает «пароль не задан» — то есть ту же дыру видом сбоку.
    assert "      - --requirepass\n      - ${OPENCRM_REDIS_PASSWORD" in redis, (
        "за --requirepass идёт не пароль из docker/.env"
    )


def test_u_redis_est_potolok_pamyati_i_vytesnenie():
    """Потолков нужно ДВА, и они про разное.

    `mem_limit` снаружи убивает контейнер, когда тот вылез, — а перезапуск
    Redis обнуляет ВСЕ счётчики разом, то есть делает ровно то, что нужно
    подбирающему. `maxmemory` с вытеснением не даёт вылезти вовсе: ключи
    приходят с улицы (адрес почты, хэш IP), и без потолка перебор набьёт
    столько, сколько дадут.
    """
    redis = _sluzhba_redis()
    assert "mem_limit:" in redis, "нет внешнего потолка — OOM-killer унесёт сайт"
    # Проверяются АРГУМЕНТЫ КОМАНДЫ, а не слова: те же имена стоят в комментарии
    # рядом, и проверка на вхождение строки оставалась зелёной, когда сами
    # аргументы из команды исчезали. Найдено нарочной поломкой сторожа.
    assert "      - --maxmemory\n" in redis, "нет внутреннего потолка"
    assert "      - --maxmemory-policy\n      - allkeys-lru\n" in redis, (
        "политика вытеснения не задана: на переполнении Redis начнёт отказывать "
        "в записи, а отказ записи у нас означает отказ входа"
    )
    assert "healthcheck:" in redis, "нет проверки здоровья"
    assert "logging: *logging" in redis, "логи службы растут без ротации"


def test_prilozhenie_zhdyot_redis_bezuslovno():
    """У зависимости от `db` стоит `required: false`, у `redis` — не должно.

    `required: false` там нужен, потому что на SQLite службы `db` в стеке нет
    вовсе. Redis есть всегда, и «поднялись без него» — состояние, которого не
    должно существовать: приложение с пустым OPENCRM_REDIS_URL в production не
    стартует вовсе.
    """
    text = _compose()
    app = text[text.index("\n  app:"): text.index("\n  db:")]
    zavisimost = app[app.index("depends_on:"):]
    posle_redis = zavisimost[zavisimost.index("redis:"):]
    assert "condition: service_healthy" in posle_redis
    assert "required: false" not in posle_redis, (
        "зависимость от redis объявлена необязательной — стек поднимется без "
        "общего счётчика попыток"
    )


def test_ustanovshchik_pishet_parol_redis_v_oba_fayla():
    """Как и у базы: контейнеру — в docker/.env, приложению — внутрь URL в
    config/.env. Разъехавшись, они дают приложение, отвечающее 503 на вход."""
    text = _script()
    nastroyka = text[text.index("configure_redis() {"):]
    nastroyka = nastroyka[: nastroyka.index("\n}\n")]
    assert 'env_set "$DOCKER_ENV" OPENCRM_REDIS_PASSWORD' in nastroyka
    assert 'env_set "$APP_ENV" OPENCRM_REDIS_URL "redis://:$_redis_pass@redis:6379/0"' in nastroyka
    assert "gen_secret" in nastroyka, "пароль Redis спрашивают у человека"
    # Настраивается ВСЕГДА, а не только при выборе MySQL: иначе на установке с
    # SQLite первый же OPENCRM_WORKERS>1 молча умножил бы порог.
    ustanovka = text[text.index("cmd_install() {"): text.index("need_install() {")]
    assert "configure_redis" in ustanovka
    assert ustanovka.index("configure_redis") < ustanovka.index("build_and_start")


