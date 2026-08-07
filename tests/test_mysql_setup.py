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

pytestmark = pytest.mark.skipif(
    not COMPOSE.exists() or not SCRIPT.exists(), reason="обвязки развёртывания рядом нет"
)


def _compose() -> str:
    return COMPOSE.read_text(encoding="utf-8")


def _script() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _odnoy_strokoy(kusok: str) -> str:
    """Кусок скрипта со склеенными переносами строк.

    Обратная косая в конце строки — не конец команды. Проверка, читающая скрипт
    построчно, разрезала бы одну команду пополам и находила бы её половинки в
    соседних, ничего при этом не стерегя. Найдено нарочной поломкой сторожа.
    """
    return " ".join(kusok.split()).replace("\\ ", "")


def _sluzhba_db() -> str:
    """Кусок compose-файла от службы `db` до следующей службы."""
    text = _compose()
    nachalo = text.index("\n  db:")
    hvost = text[nachalo + 1:]
    konec = hvost.index("\n  nginx:")
    return hvost[:konec]


# --- служба базы --------------------------------------------------------------


def test_baza_zhivyot_pod_profilem():
    """При выборе SQLite службы базы не должно быть в стеке вовсе.

    «Опишем и не будем запускать» здесь не работает: `compose up -d` поднимает
    все описанные службы. Лишний сервер MySQL съедает под себя полгигабайта
    памяти на машине, где вся база лежит одним файлом, — а такие машины у этого
    продукта основные.
    """
    assert 'profiles: ["mysql"]' in _sluzhba_db(), "служба базы поднимается всегда"
    # Профиль включается строкой в docker/.env, которую пишет установщик;
    # без неё выбор MySQL остался бы словами в конфиге.
    assert 'env_set "$DOCKER_ENV" COMPOSE_PROFILES mysql' in _script()


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


# --- выбор базы при установке -------------------------------------------------


def test_vybor_bazy_est_i_vyzyvaetsya_pri_ustanovke():
    text = _script()
    assert "choose_database()" in text, "базу больше не выбирают"
    ustanovka = text[text.index("cmd_install() {"): text.index("need_install() {")]
    assert "choose_database" in ustanovka, "выбор базы выпал из мастера установки"
    # Строго после configure_app_env: раньше config/.env ещё не создан, и выбор
    # ушёл бы в файл мимо шаблона.
    assert ustanovka.index("configure_app_env") < ustanovka.index("choose_database")


def test_parol_bazy_generiruetsya_a_ne_sprashivaetsya():
    """Спрошенный пароль базы придумывают за минуту и повторяют от сервера к
    серверу. Руками его всё равно никто не вводит — он нужен двум контейнерам,
    и оба берут его из файла."""
    text = _script()
    vybor = text[text.index("choose_database() {"): text.index("migrate_sqlite_to_mysql() {")]
    assert "gen_secret" in vybor, "пароль базы больше не генерируется"
    for zapros in ("Пароль базы", "Database password", "OPENCRM_DB_PASSWORD\"" ):
        assert f'ask "{zapros}' not in vybor, "пароль базы снова спрашивают у человека"


def test_parol_popadaet_v_oba_fayla():
    """Пароль нужен двоим: контейнеру базы (docker/.env) и приложению (внутри
    OPENCRM_DB_URL в config/.env). Записать в один — получить «access denied»
    на первом же старте."""
    text = _script()
    vybor = text[text.index("choose_database() {"): text.index("migrate_sqlite_to_mysql() {")]
    assert 'env_set "$DOCKER_ENV" OPENCRM_DB_PASSWORD' in vybor
    assert 'env_set "$DOCKER_ENV" OPENCRM_DB_ROOT_PASSWORD' in vybor
    # Целиком одной командой: `env_set "$APP_ENV" OPENCRM_DB_URL` есть и в ветке
    # SQLite, поэтому проверка «оно где-то встречается» пропускала пропажу
    # именно того env_set, который пишет пароль.
    assert (
        'env_set "$APP_ENV" OPENCRM_DB_URL '
        '"mysql+pymysql://opencrm:$_db_pass@db:3306/opencrm?charset=utf8mb4"'
    ) in _odnoy_strokoy(vybor), "пароль не попадает в config/.env"


def test_docker_env_zakryvaetsya_pravami():
    """С выбором MySQL в docker/.env ложатся пароли базы, а `cp` переносит
    права шаблона (644): файл был бы читаем всем в системе."""
    text = _script()
    nastroyka = text[text.index("configure_docker_env() {"): text.index("create_dirs() {")]
    assert 'chmod 600 "$DOCKER_ENV"' in nastroyka, "docker/.env остаётся читаемым всем"


def test_povtornaya_ustanovka_ne_perespravshivaet_pro_bazu():
    """Ответ «SQLite» на работающей MySQL увёл бы сайт на пустой файл, а новый
    пароль разошёлся бы с пользователем, который в базе уже создан."""
    text = _script()
    vybor = text[text.index("choose_database() {"): text.index("migrate_sqlite_to_mysql() {")]
    assert 'if [ "$(db_engine)" = "mysql" ]; then' in vybor
    assert "return 0" in vybor


def test_perenos_predlagaetsya_i_pro_otkat_skazano():
    """Переезд затевают на живой системе, и главное в нём — что он обратим.

    Пока файл SQLite цел, возврат стоит одной строки в конфиге. Не сказать об
    этом — значит превратить обратимый шаг в необратимый на вид.
    """
    text = _script()
    vybor = text[text.index("choose_database() {"): text.index("migrate_sqlite_to_mysql() {")]
    assert "migrate_to_mysql.py" in vybor, "перенос не предлагают"
    assert "только на чтение" in vybor, "не сказано, что исходная база не меняется"
    perenos = text[text.index("migrate_sqlite_to_mysql() {"): text.index("load_language() {")]
    assert "sqlite:////app/data/opencrm.db" in perenos, "не сказано, чем откатываться"
    # Перенос идёт после того, как стек поднялся: схему в новой базе строят
    # миграции при первом старте приложения, до него её там нет вовсе.
    ustanovka = text[text.index("cmd_install() {"): text.index("need_install() {")]
    assert ustanovka.index("check_health") < ustanovka.index("migrate_sqlite_to_mysql")


def test_parol_ne_uezzhaet_v_komandnuyu_stroku_hosta():
    """Аргументы процесса видит через `ps` любой пользователь сервера.

    Поэтому и пароль базы, и URL с паролем внутри раскрываются ВНУТРИ
    контейнера — из его собственного окружения, — а не подставляются здесь.
    """
    text = _script()
    for kusok in ("dump_mysql() {", "migrate_sqlite_to_mysql() {"):
        blok = text[text.index(kusok):]
        blok = blok[: blok.index("\n}\n")]
        assert "$MYSQL_ROOT_PASSWORD" in blok or "$OPENCRM_DB_URL" in blok
    # Признак ошибки: значение подставлено оболочкой хоста в двойных кавычках
    # прямо в команду docker.
    assert 'exec -T db sh -c "' not in text, "пароль раскрывается на хосте"


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
