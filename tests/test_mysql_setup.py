"""Установка на MySQL: служба базы в compose и её настройка в opencrm.sh.

Всё, что здесь проверяется, ломается молча и обнаруживается на чужом сервере.
Служба базы под профилем — это сайт, поднявшийся без базы вовсе; проверка
здоровья, отвечающая раньше времени, — приложение, стартовавшее до базы;
пароль, попавший в командную строку, — доступ к базе для всех, кто в этот
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

    Профиль у неё был, пока база могла лежать одним файлом: лишний сервер
    съедал бы тогда полгигабайта памяти в никуда. Такой установки больше не
    существует, и профиль превратился бы в способ поднять сайт без базы вовсе.
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
    """И при этом поднимается, когда база не встала совсем.

    `required: false` у зависимости оставлен сознательно, и цена его проверена
    живьём: с испорченным каталогом данных compose не ждёт базу до упора, а
    пишет предупреждение и стартует приложение. Сайт всё равно не работает, но
    причина видна в логе приложения, а не в вечном «Waiting» без объяснений;
    гонку старта закрывает сам `entrypoint.sh` — он ждёт базу и без compose.
    """
    text = _compose()
    app = text[text.index("\n  app:"): text.index("\n  db:")]
    zavisimost = app[app.index("depends_on:"):]
    assert "db:" in zavisimost
    assert "condition: service_healthy" in zavisimost, "приложение стартует раньше базы"
    assert "required: false" in zavisimost, (
        "compose снова ждёт базу до упора — причина простоя утонет в «Waiting»"
    )


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

    Строка `OPENCRM_DB_URL:` в `environment` сделала бы адрес базы в config/.env
    бессмысленным: установщик пишет туда адрес с паролем, а compose молча увёл
    бы приложение мимо него — и «access denied» на старте пришлось бы искать не
    там, где пароль правят. Ровно так же однажды не сработал
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
# Выбора больше нет. Файловая база допускала ровно одного писателя, поэтому на
# ней был невозможен второй рабочий процесс, а значит установка, начатая
# «попроще», рано или поздно упиралась в переезд — в закрытый сайт и самую
# опасную операцию, какая в проекте была. Её убрали вместе с самим переездом.


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


def test_chuzhaya_kopiya_otvergaetsya_do_ostanovki_sayta():
    """Файл `*.db` от прежней установки заливать некуда.

    Копии от старых времён лежат в том же каталоге, и выбрать такую можно по
    ошибке. Сказать об этом до `compose stop app` дешевле, чем после: иначе
    сайт остановлен, восстановление не состоялось, и человек остался с лежащим
    сайтом и без объяснения.
    """
    text = _script()
    vosstanovlenie = text[text.index("cmd_restore() {"): text.index("cmd_https()")]
    assert "*.db)" in vosstanovlenie, "вид копии не проверяется вовсе"
    proverka = vosstanovlenie.index("*.db)")
    ostanovka = vosstanovlenie.index("compose stop app")
    assert proverka < ostanovka, "несовместимость замечают уже после остановки сайта"

def test_bekap_snimaet_damp_v_konteynere_bazy():
    """Клиента mysqldump в образе приложения нет — он лежит в образе базы.

    Скрипт копирования при этом один на всё: имя по дате, ротация, ключ
    шифрования и проверка годности не имеют права разъехаться на две
    реализации.
    """
    text = _script()
    bekap = text[text.index("cmd_backup() {"): text.index("cmd_restore() {")]
    assert "dump_mysql" in bekap, "дамп больше не снимается"
    assert "OPENCRM_DB_DUMP=" in bekap, "снятый дамп не передаётся скрипту копирования"
    assert "scripts/backup.sh" in bekap, "копия перестала сниматься общим скриптом"


# --- Наблюдатель за базой: пользователь для db-exporter ------------------------
#
# `db-exporter` ходит в MySQL под своим пользователем, и заводит его установщик.
# Не заведён — экспортёр отдаёт `mysql_up 0`, оставаясь при этом ЗДОРОВЫМ
# контейнером: ни цикла перезапусков, ни тревоги, ни строчки в логе. Снаружи
# это неотличимо от работающего мониторинга, а на деле про базу нет ничего —
# ни соединений, ни ожиданий замков, ни буферного пула. Узнают об этом в тот
# единственный день, когда метрики базы понадобились.


def _bez_kommentariev(kusok: str) -> str:
    """Кусок скрипта без строк-комментариев.

    Проверки порядка обязаны читать КОД, а не прозу рядом с ним. Комментарий
    «тоже до monitoring_apply, и по той же причине» стоит выше самого вызова, и
    сторож, ищущий вхождение имени, находил в нём первое упоминание и объявлял
    порядок нарушенным на верном скрипте. Ровно так этот помощник и появился.
    """
    return NL.join(s for s in kusok.splitlines() if not s.strip().startswith("#"))


def _kod(imya: str) -> str:
    """Тело функции оболочки без комментариев."""
    return _bez_kommentariev(_telo(imya))


def _telo_monitoring_on() -> str:
    """Ветка `on` команды `monitoring` — от заголовка функции до ветки `off`.

    Конец ищется в ХВОСТЕ после заголовка, а не во всём файле: то же `off)` с
    тем же отступом стоит выше, у автообновления, и поиск по целому тексту
    возвращал бы кусок до него — то есть пустой. Найдено покраснением сторожа
    на верном скрипте.
    """
    text = _script()
    hvost = text[text.index("cmd_monitoring() {"):]
    return _bez_kommentariev(hvost[: hvost.index(NL + "        off)")])


def test_parol_nablyudatelya_generiruetsya_kak_ostalnye():
    """Тем же способом, что пароль Grafana и пароль базы: генерируется,
    ложится в docker/.env (права 600) и в репозиторий не попадает.

    Спрошенный у человека пароль здесь не нужен никому: его читают ровно два
    участника — экспортёр из docker/.env и сама база, куда его кладёт этот же
    установщик.
    """
    seed = _kod("seed_db_exporter_password")
    assert "gen_secret" in seed, "пароль наблюдателя больше не генерируется"
    odnoy = _odnoy_strokoy(seed)
    assert 'env_set "$DOCKER_ENV" OPENCRM_DB_EXPORTER_PASSWORD' in odnoy, (
        "пароль наблюдателя не записывается в docker/.env"
    )
    assert 'env_set "$DOCKER_ENV" OPENCRM_DB_EXPORTER_USER' in odnoy, (
        "имя наблюдателя не записывается в docker/.env — экспортёр возьмёт "
        "умолчание compose, и разъехаться этим двоим будет негде только пока "
        "умолчания совпадают"
    )
    assert "ask " not in seed, "пароль наблюдателя снова спрашивают у человека"


def test_povtornyy_zapusk_ne_pereseivaet_parol_nablyudatelya():
    """Тот же пароль записан ВНУТРИ базы, у пользователя наблюдателя.

    Перегенерация на повторном запуске развела бы половины пары: экспортёр
    получил бы «access denied», метрики базы исчезли бы с дашборда, и ни одна
    тревога об этом не сказала бы — контейнер-то здоров.
    """
    seed = _kod("seed_db_exporter_password")
    assert 'env_get "$DOCKER_ENV" OPENCRM_DB_EXPORTER_PASSWORD' in seed, (
        "прежний пароль не читается — повторный запуск разведёт файл с базой"
    )
    assert seed.index("OPENCRM_DB_EXPORTER_PASSWORD") < seed.index("gen_secret"), (
        "новый пароль сеется раньше, чем проверено наличие прежнего"
    )


def test_ustanovshchik_i_compose_govoryat_ob_odnom_polzovatele():
    """Имя по умолчанию названо в двух местах, и разъехаться им нельзя.

    Установщик завёл бы `opencrm_exporter`, а экспортёр ходил бы в базу кем-то
    другим — и выглядело бы это ровно как незаведённый пользователь.
    """
    assert "${OPENCRM_DB_EXPORTER_USER:-opencrm_exporter}" in _compose(), (
        "compose берёт имя наблюдателя не из OPENCRM_DB_EXPORTER_USER"
    )
    assert "OPENCRM_DB_EXPORTER_PASSWORD" in _compose(), (
        "compose не передаёт экспортёру пароль из docker/.env"
    )
    imya = _kod("db_exporter_user")
    assert 'env_get "$DOCKER_ENV" OPENCRM_DB_EXPORTER_USER' in imya
    assert "opencrm_exporter" in imya, (
        "умолчание в установщике разошлось с умолчанием в compose"
    )


def test_zapros_zavodit_polzovatelya_i_dovodit_parol():
    """`CREATE USER IF NOT EXISTS` заводит нового, `ALTER USER` доводит пароль
    до уже существующего.

    Без `ALTER` смена пароля в docker/.env расходилась бы с базой навсегда:
    новый пользователь не создаётся (он уже есть), старый остаётся со старым
    паролем, и починить это установщиком стало бы нечем.
    """
    zapros = _odnoy_strokoy(_kod("grant_db_exporter"))
    assert "CREATE USER IF NOT EXISTS" in zapros, "пользователь не заводится"
    assert "ALTER USER" in zapros and "IDENTIFIED BY" in zapros, (
        "пароль не доводится до уже существующего пользователя — смена пароля "
        "в docker/.env останется незамеченной базой"
    )


def test_u_nablyudatelya_rovno_tri_prava():
    """Ни одной таблицы с данными клиентов.

    Утёкший из логов или из метрик пароль наблюдателя не должен открывать
    базу. Права ровно те три, которые нужны mysqld-exporter, и ни одного
    сверх.
    """
    zapros = _odnoy_strokoy(_kod("grant_db_exporter"))
    assert "GRANT PROCESS, REPLICATION CLIENT ON *.*" in zapros
    assert "GRANT SELECT ON performance_schema.*" in zapros
    for lishnee in ("GRANT ALL", "SUPER", "WITH GRANT OPTION", "ON opencrm.*", "ON *.* TO"):
        assert lishnee not in zapros.replace("GRANT PROCESS, REPLICATION CLIENT ON *.* TO", ""), (
            f"наблюдателю выдано лишнее право: {lishnee}"
        )
    assert "MAX_USER_CONNECTIONS" in zapros, (
        "нет потолка соединений — наблюдатель съест последние ровно тогда, "
        "когда их не хватает и он нужнее всего"
    )


def test_paroli_ne_uezzhayut_v_komandnuyu_stroku_hosta():
    """Аргументы процесса видит через `ps` любой пользователь сервера.

    Рутовый пароль раскрывается ВНУТРИ контейнера из его собственного
    окружения (как в dump_mysql), а пароль наблюдателя уходит туда же
    стандартным вводом вместе с запросом — тем же путём, каким заливается дамп
    при восстановлении.
    """
    zapros = _odnoy_strokoy(_kod("grant_db_exporter"))
    assert "$MYSQL_ROOT_PASSWORD" in zapros, "рутовый пароль подставляется на хосте"
    assert "| compose exec -T db" in zapros, (
        "запрос уходит не стандартным вводом — значит попадает в аргументы"
    )
    posle = zapros[zapros.index("| compose exec"):]
    assert "_dxp" not in posle, (
        "пароль наблюдателя уехал в команду docker — его видно в `ps` на хосте"
    )
    # То же самое у проверки: она ходит в базу рутом и своего пароля не носит.
    assert "$MYSQL_ROOT_PASSWORD" in _odnoy_strokoy(_kod("db_exporter_granted"))


def test_polzovatel_zavoditsya_posle_podyoma_bazy_a_parol_do():
    """Порядок здесь не косметика, и он разный для двух половин.

    Пароль пишется ДО подъёма служб: экспортёр читает его при СОЗДАНИИ
    контейнера, и записанный позже подхватился бы только следующим `up`, то
    есть неизвестно когда. Пользователь заводится ПОСЛЕ: до подъёма базы
    просто нет — при установке сборка идёт ниже по списку.
    """
    on = _telo_monitoring_on()
    assert on.index("seed_db_exporter_password") < on.index("monitoring_apply"), (
        "пароль пишется после подъёма — контейнер экспортёра останется с прежним"
    )
    assert on.index("monitoring_apply") < on.index("setup_db_exporter"), (
        "пользователя заводят раньше, чем поднята база"
    )

    text = _script()
    ustanovka = text[text.index("cmd_install() {"): text.index("need_install() {")]
    assert "setup_db_exporter" in ustanovka, (
        "установка включает мониторинг, но пользователя наблюдателя не заводит"
    )
    assert ustanovka.index("build_and_start") < ustanovka.index("setup_db_exporter"), (
        "пользователя заводят до сборки — базы в этот момент ещё нет"
    )


def test_lezhachaya_baza_ne_valit_vklyuchenie_monitoringa():
    """Тревоги, панель, метрики машины и проверка сайта работают и без метрик
    базы. Уронить из-за недоступной базы включение мониторинга целиком значило
    бы обменять всё на часть.

    Но и промолчать нельзя: не заведённый пользователь ничем себя не выдаёт.
    """
    setup = _kod("setup_db_exporter")
    assert "wait_db" in setup, "готовности базы не ждут вовсе"
    assert "die " not in setup, "недоступная база валит включение мониторинга"
    assert "warn " in setup, "о незаведённых метриках базы молчат"
    assert "monitoring on" in setup, "не сказано, чем это доделать"


def test_gotovnost_bazy_sprashivaetsya_po_tcp_a_ne_po_stroke_sostoyaniya():
    """Сокет отвечает раньше, чем сервер начинает слушать порт, — та же
    ловушка, из-за которой проверка здоровья в compose ходит по TCP.

    А в строке `compose ps` слово «healthy» лежит внутри «unhealthy»: проверка
    на вхождение считала бы больную базу здоровой ровно тогда, когда ожидание
    и написано.
    """
    zhdyom = _kod("wait_db")
    assert "--protocol=TCP" in zhdyom, "готовность базы спрашивают через сокет"
    assert "compose ps" not in zhdyom, (
        "готовность определяется разбором строки состояния — «unhealthy» "
        "содержит «healthy»"
    )


def test_doctor_vidit_nezavedyonnye_metriki_bazy():
    """Иначе «пользователя забыли завести» видно только тому, кто откроет
    дашборд и прочтёт там «нет доступа к базе».

    Спрашивается САМА БАЗА: пароль в docker/.env и пользователь в MySQL —
    разные вещи, и расходятся они ровно в том случае, ради которого строка
    нужна (мониторинг включали, пока база лежала).
    """
    text = _script()
    doctor = text[text.index("cmd_doctor() {"): text.index("why_down() {")]
    assert 'tr_ "метрики базы"' in doctor, "в диагностике нет строки про метрики базы"
    kusok = doctor[doctor.index('tr_ "метрики базы"'): doctor.index('tr_ "проверка сайта"')]
    assert "db_exporter_granted" in kusok, (
        "строка верит docker/.env, а не базе — не заведённый пользователь "
        "останется зелёным"
    )
    assert "./opencrm.sh monitoring on" in kusok, "не сказано, чем это чинить"
    # Три исхода различаются: не задан пароль, не заведён пользователь и
    # «спросить не у кого». Слитые в один, они врали бы при лежачей базе.
    assert "не заведён" in kusok
    assert "не проверить" in kusok


def test_sostoyanie_monitoringa_pokazyvaet_eksportyory():
    """Экран состояния — то место, куда идут с вопросом «что с мониторингом».

    Пока в списке стояли только prometheus, alertmanager и grafana, пропажу
    самого контейнера экспортёра нельзя было увидеть ниоткуда: профиль
    включён, панель открывается, а половины дашборда нет.
    """
    text = _script()
    mon = text[text.index("cmd_monitoring() {"): text.index("dump_mysql() {")]
    stroki = [s for s in mon.splitlines() if "compose ps" in s and not s.strip().startswith("#")]
    assert stroki, "экран состояния больше не показывает контейнеры мониторинга"
    assert "db-exporter" in stroki[0] and "redis-exporter" in stroki[0], (
        "экспортёров базы и Redis нет в списке служб на экране состояния"
    )


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

    `required: false` там нужен, чтобы не вставшая база не превращалась в
    вечное «Waiting» без объяснений. У redis такого послабления нет: «поднялись
    без него» — состояние, которого не должно существовать, приложение с пустым
    OPENCRM_REDIS_URL в production не стартует вовсе.
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
    # Настраивается ВСЕГДА, на любой установке: пропусти его — и первый же
    # OPENCRM_WORKERS>1 молча умножил бы порог защиты от подбора.
    ustanovka = text[text.index("cmd_install() {"): text.index("need_install() {")]
    assert "configure_redis" in ustanovka
    assert ustanovka.index("configure_redis") < ustanovka.index("build_and_start")




# --- восстановление из копии ------------------------------------------------


def test_vosstanovlenie_proveryaet_tselost_kopii_do_zalivki():
    """Оборванная копия заливается БЕЗ ЖАЛОБ и оставляет базу без таблиц.

    Проверено живьём: дамп, оборванный ровно на границе оператора — так и
    выглядит кончившееся место, — `mysql` принимает и выходит с нулём. После
    такого «успешного» восстановления в базе не оказалось ни `users`, ни
    `warehouses`, ни `works`, ни `stock_moves`, а человек видел строку
    «восстановлено, сайт отвечает».

    Та же проверка стоит и в `scripts/restore.sh`, но этот путь до неё не
    доходит: меню заливает дамп САМО и зовёт скрипт уже с `OPENCRM_SKIP_DB=1`.
    Значит проверка обязана быть здесь — и **до** заливки, иначе она
    констатирует порчу, а не предотвращает её.
    """
    kod = _kod("cmd_restore")
    assert "Dump completed" in kod, "меню не смотрит на метку конца копии"

    proverka = kod.index("Dump completed")
    zalivka = kod.index("exec mysql --default-character-set")
    assert proverka < zalivka, "проверка целости стоит ПОСЛЕ заливки — она бесполезна"

    # И до остановки сайта: отказ на этом шаге не должен стоить простоя.
    ostanovka = kod.index("compose stop app")
    assert proverka < ostanovka, "сайт останавливают раньше, чем смотрят на копию"


def test_vosstanovlenie_vidit_nedelnye_kopii():
    """Ежедневных хранится семь. Всё, что старше, живёт ТОЛЬКО в weekly.

    Показывая один `daily`, меню объявляло четыре недельные копии
    несуществующими — ровно тогда, когда они и нужны: беду, замеченную через
    десять дней, из ежедневных уже не откатить.
    """
    kod = _kod("cmd_restore")
    assert "backups/weekly" in kod, "меню не знает про недельные копии"

    # Смотрим на ПРИМЕНЕНИЕ, а не на объявление. Первая редакция этого сторожа
    # искала строку «backups/weekly» — то есть саму переменную, — и осталась
    # зелёной, когда поломка убрала её из перечисления: объявление-то на месте.
    # Проверка, довольная объявлением неиспользуемой переменной, не стережёт
    # ничего.
    perechisleniya = [s for s in kod.splitlines() if "ls -1t" in s]
    assert perechisleniya, "в меню больше нет перечисления копий"
    for stroka in perechisleniya:
        assert "_dirw" in stroka, f"недельные копии не попадают в выбор: {stroka.strip()}"

    # Архив ищется рядом с выбранной базой, а не жёстко в daily — иначе к
    # недельной копии «нет пары» на ровном месте.
    assert 'dirname "$_db"' in kod, "пара к базе ищется не там, где лежит сама база"


def test_dampy_na_khoste_ne_chitayutsya_postoronnimi():
    """В дампе вся система целиком, а перенаправление создаёт файл с 0644.

    Копии, которые пишет `scripts/backup.sh`, уже закрыты умаской. Эти два
    файла пишет сам установщик на хосте, мимо неё: снимок перед
    восстановлением и промежуточный дамп команды `backup`.
    """
    for imya, fayl in (("cmd_restore", '"$_before"'), ("cmd_backup", '"$_incoming"')):
        kod = _kod(imya)
        assert f"chmod 600 {fayl}" in kod, f"{imya}: {fayl} остаётся читаемым всем"
