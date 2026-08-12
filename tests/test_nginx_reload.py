"""nginx перечитывает конфиг — с повторным рендером, а не одним лишь сигналом.

Здесь сторожа на механизм, который на боевом сервере отсутствовал пять суток и
всё это время был неотличим от исправного.

Что случилось 7-12 августа 2026 на sharebranding.xyz. Рабочий конфиг nginx не
лежит в репозитории: он рождается из шаблона подстановкой домена, и делал это
ровно один раз — при старте контейнера. Все три места, которые «применяли»
правки (фоновый цикл nginx, `opencrm.sh`, шаг `nginx-reload` обновления), звали
голый `nginx -s reload`, а он перечитывает УЖЕ ОТРЕНДЕРЕННЫЙ файл и его
include-ы. Отсюда:

* правки шаблонов не применялись никогда — контейнер, поднятый год назад,
  работает с шаблоном годичной давности и молчит об этом;
* коммит `027f2be` дописал `include logging.inc` в ШАБЛОН, а ссылку на
  описанный там формат — в include (`access_log … opencrm_json` в
  locations.inc). Include приехал, шаблон нет, и nginx стал отвергать конфиг
  ЦЕЛИКОМ на каждом reload — `[emerg] unknown log format`;
* ошибка при этом глушилась во всех трёх местах (`2>/dev/null || true`,
  `>/dev/null 2>&1 || true`, `fatal=False`).

Итог, который никто не увидел: `/api/v1/metrics` открыт наружу, в журнал
попадали IP клиентов и ссылки `/b/<токен>`, правило про долю 5xx считало по
пустоте, а панель `/monitoring/` отвечала так, будто мониторинг выключен.

Файловые тесты (`test_monitoring.py`) всё это время были зелёными — они читают
репозиторий, а расхождение было между репозиторием и памятью процесса. Поэтому
здесь проверяется не содержимое конфига, а **механизм его применения**.

Конфиги и скрипты читаются как текст: словарь зависимостей приложения не должен
расти ради тестов (тот же приём, что в `test_deploy_config.py`).
"""

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
NGINX = ROOT / "docker" / "nginx"
RELOAD = NGINX / "reload.sh"
ENTRYPOINT = NGINX / "entrypoint.sh"
TEMPLATES = NGINX / "templates"
LOCATIONS = TEMPLATES / "locations.inc"
LOGGING_INC = TEMPLATES / "logging.inc"
COMPOSE = ROOT / "docker" / "docker-compose.yml"
SCRIPT = ROOT / "opencrm.sh"
UPDATER = ROOT / "deploy" / "updater.py"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _script() -> str:
    """Текст `opencrm.sh`.

    Внутри образа его нет: Dockerfile копирует только нужное приложению, а тесты
    гоняются и там (автообновление зовёт `docker build --target tests`). Красный
    тест, который никто не может починить, приучает не смотреть на цвет.
    """
    if not SCRIPT.exists():
        pytest.skip("вне репозитория: opencrm.sh не входит в образ")
    return _read(SCRIPT)


def _kod(text: str) -> str:
    """Только исполняемые строки: комментарии выброшены.

    В этом проекте комментарии длиннее кода и разбирают ровно те грабли, про
    которые здесь тесты, — то есть содержат и `nginx -s reload`, и `envsubst`
    дословно. Проверять «этого нет в файле» по тексту вместе с комментариями
    значило бы запретить объяснять, почему так делать нельзя.
    """
    return "\n".join(
        stroka for stroka in text.splitlines() if not stroka.lstrip().startswith("#")
    )


def _telo(script: str, name: str) -> str:
    """Тело функции sh: от `name() {` до закрывающей скобки на нулевом отступе."""
    found = re.search(rf"\n{name}\(\) \{{(.+?)\n\}}", script, re.S)
    assert found, f"функция {name} пропала"
    return found.group(1)


def _kod_python(path: Path) -> str:
    """Только исполняемый код Python: докстроки выброшены, аргументы склеены.

    Тот же довод, что у `_kod` для sh, и та же ловушка с другой стороны:
    докстроки в этом проекте разбирают ровно те грабли, про которые тесты, и
    содержат `nginx -s reload` дословно — по тексту файла проверка «этого здесь
    нет» запретила бы объяснять, почему так делать нельзя.

    Кавычки и запятые убираются, чтобы разложенная по аргументам команда
    (`"exec", "-T", "nginx", "-s", "reload"`) искалась так же, как её пишут в
    оболочке.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        telo = getattr(node, "body", None)
        if not isinstance(telo, list) or not telo:
            continue
        pervyy = telo[0]
        if (
            isinstance(pervyy, ast.Expr)
            and isinstance(pervyy.value, ast.Constant)
            and isinstance(pervyy.value.value, str)
        ):
            # Пустой список тела недопустим — ставим заглушку вместо докстроки.
            node.body = telo[1:] or [ast.Pass()]
    return ast.unparse(ast.fix_missing_locations(tree)).replace("'", "").replace(", ", " ")


# --- рендер живёт в одном месте ----------------------------------------------


def test_konfig_renderitsya_rovno_v_odnom_meste():
    """Пока рендер был в entrypoint, а сигнал — у всех остальных, применить
    правку шаблона можно было только пересозданием контейнера. А его не делает
    ни обновление, ни `compose up -d`, ни установщик."""
    assert RELOAD.is_file(), "нет docker/nginx/reload.sh — перечитывать конфиг снова некому"

    assert "envsubst" in _kod(_read(RELOAD)), (
        "reload.sh не рендерит шаблон — значит это тот же голый reload"
    )

    entrypoint = _kod(_read(ENTRYPOINT))
    assert "envsubst" not in entrypoint, (
        "рендер снова в двух местах: старт и перечитывание разойдутся при первой же правке"
    )
    assert "reload.sh" in entrypoint, "старт не пользуется общим скриптом рендера"


def test_perechityvanie_eto_render_potom_proverka_potom_signal():
    """Порядок и есть суть починки.

    Голый `nginx -s reload` перечитывает отрендеренный `default.conf` и его
    include-ы, но заново подставить домен в шаблон не умеет. Тот же приём, что у
    `docker/monitoring/prometheus/entrypoint.sh`: сначала render, потом сигнал.
    """
    reload_sh = _kod(_read(RELOAD))
    for shag in ("envsubst", "nginx -t", "nginx -s reload"):
        assert shag in reload_sh, f"в reload.sh нет шага «{shag}»"

    poryadok = [reload_sh.index(shag) for shag in ("envsubst", "nginx -t", "nginx -s reload")]
    assert poryadok == sorted(poryadok), (
        "рендер, проверка и сигнал идут не по порядку — сигнал уйдёт по непроверенному конфигу"
    )


def test_plokhoy_konfig_ne_stanovitsya_rabochim():
    """Единственное преимущество reload перед restart — цена ошибки.

    Плохой конфиг при reload означает «ничего не изменилось», при restart —
    лежащий сайт в цикле перезапусков (`nginx -t` под `set -e` при
    `restart: unless-stopped`). Преимущество сохраняется, только если скрипт
    возвращает прежний файл на место и говорит о неудаче вслух.
    """
    reload_sh = _kod(_read(RELOAD))
    assert "trap" in reload_sh, "прерванный скрипт оставит на диске непроверенный конфиг"
    assert re.search(r"nginx -t\b", reload_sh), "конфиг применяется без проверки"
    assert "exit 1" in reload_sh, (
        "неудачная проверка не отражается кодом возврата — вызывающий не сможет о ней сказать"
    )


def test_tsikl_perechityvaniya_takoy_zhe_chastyy_kak_u_prometheus():
    """Шесть часов — не срок для правки, приехавшей с обновлением.

    У Prometheus и Alertmanager период равен такту опроса автообновления (пять
    минут), и по той же причине: правка обязана начать действовать не позже, чем
    через один такт после деплоя. У nginx он был в 72 раза больше — и, что хуже,
    без рендера, то есть правки шаблонов не применял вовсе.
    """
    entrypoint = _kod(_read(ENTRYPOINT))
    assert "sleep 300" in entrypoint, "nginx перечитывает конфиг слишком редко"
    assert "sleep 6h" not in entrypoint
    assert not re.search(r"^\s*nginx -s reload", entrypoint, re.M), (
        "фоновый цикл снова шлёт голый сигнал — правки шаблонов им не применяются"
    )


# --- зовут все и одинаково ----------------------------------------------------


def test_vse_kto_prosit_nginx_perechitat_zovut_skript():
    """Голый `nginx -s reload` в проекте остаться не должен нигде, кроме самого
    reload.sh: в любом другом месте он означает «правка шаблона не применилась»."""
    script = _kod(_script())
    telo = _telo(script, "reload_nginx")
    assert "/opencrm/reload.sh" in telo, (
        "opencrm.sh снова просит перечитать конфиг без повторного рендера"
    )
    assert "nginx -s reload" not in script, (
        "в opencrm.sh остался голый сигнал — он не применит правки шаблонов"
    )


def test_obnovlenie_zovyot_tot_zhe_skript_chto_i_ustanovshchik():
    """Сторож выше читал ТОЛЬКО opencrm.sh — и потому ничего не заметил.

    Установщик переехал на `reload.sh`, а обновлятор остался с голым сигналом, и
    ни один тест не покраснел: `deploy/updater.py` не проверял никто. Разошлись
    они молча и разошлись бы снова.

    Цена расхождения именно здесь выше, чем где-либо: обновление — единственный
    момент, когда конфиг на диске меняется сам. Голый `nginx -s reload` при этом
    не рендерит шаблон заново И завершается нулём, не дождавшись разбора конфига:
    мастер конфиг отвергает, а шаг рапортует успех. Провала не существует в
    терминах кода — ровно поэтому пять суток никто ничего не знал.
    """
    kod = _kod_python(UPDATER)
    assert "/opencrm/reload.sh" in kod, (
        "обновление просит перечитать конфиг без повторного рендера — "
        "правки шаблонов не применятся, а отвергнутый конфиг сойдёт за удачу"
    )
    assert "nginx -s reload" not in kod, (
        "в deploy/updater.py остался голый сигнал"
    )


def test_provalennoe_perechityvanie_vidno_v_uvedomlenii():
    """Молчащая ошибка — та же беда, которую здесь чинили.

    Шаг не смертельный (у кого-то свой nginx снаружи), и это правильно. Но «не
    смертельный» превратилось в «незаметный»: результат клался в `detail` только
    на провале, а сам провал терялся под заголовком «OpenCRM обновлён».
    Механически: результат перечитывания обязан попадать в шаг при ЛЮБОМ исходе,
    а сообщение — называть провалившиеся шаги вместе с их detail.
    """
    kod = _kod_python(UPDATER)
    shag = re.search(r"steps\.append\(Step\(nginx-reload[^\n]*", kod)
    assert shag, "шаг nginx-reload больше не заводится — доложить о нём будет нечем"
    assert "result.tail" in shag.group(0), (
        "вывод перечитывания не попадает в шаг: ни в истории, ни в уведомлении "
        "причины не будет"
    )
    assert "step.name" in kod and "step.detail" in kod, (
        "уведомление перестало называть провалившиеся шаги"
    )


def test_perechityvanie_ne_glotaet_oshibku():
    """Настоящая причина инцидента — не протухший конфиг, а три `|| true`.

    Пять суток сервер отдавал наружу метрики и писал в логи адреса клиентов,
    потому что все, кто мог заметить, договорились молчать.
    """
    telo = _telo(_kod(_script()), "reload_nginx")
    assert ">/dev/null 2>&1 || true" not in telo, "ошибка перечитывания снова глотается молча"
    assert "warn" in telo, "о неудачном перечитывании никто не скажет"


def test_skript_primontirovan_v_konteyner_nginx():
    """Скрипт лежит в чекауте и работает внутри контейнера — значит обязан быть
    примонтирован, иначе `compose exec … reload.sh` не найдёт файл."""
    compose = _read(COMPOSE)
    assert "./nginx/reload.sh:/opencrm/reload.sh" in compose, (
        "reload.sh не примонтирован — ни цикл внутри контейнера, ни вызовы снаружи не сработают"
    )


# --- ловушка, на которой всё и разбилось -------------------------------------


def test_include_ne_ssylaetsya_na_to_chego_net_v_shablone():
    """Include-файл не имеет права опираться на то, что описано в шаблоне.

    Include-ы подхватываются перечитыванием сразу, шаблоны — нет. Ссылка из
    первого на второе создаёт состояние, в котором nginx отвергает конфиг
    ЦЕЛИКОМ: `access_log … opencrm_json` приехал, `include logging.inc` — нет.
    Механически: каждый формат журнала, названный в locations.inc, обязан быть
    описан в logging.inc, а logging.inc — подключён обоими шаблонами.
    """
    locations = _read(LOCATIONS)
    logging_inc = _read(LOGGING_INC)

    formaty = set(re.findall(r"^\s*access_log\s+\S+\s+([A-Za-z0-9_]+)\s*;", locations, re.M))
    assert formaty, "в locations.inc не нашлось ни одного именованного формата журнала"
    for fmt in formaty:
        assert re.search(rf"log_format\s+{fmt}\b", logging_inc), (
            f"locations.inc пишет журнал форматом {fmt}, а описания формата нет в logging.inc — "
            "nginx отвергнет такой конфиг целиком"
        )

    for name in ("http.conf.template", "https.conf.template"):
        template = _read(TEMPLATES / name)
        assert "include /opencrm/templates/logging.inc;" in template, (
            f"{name} не подключает logging.inc — формат журнала окажется неизвестным"
        )


def test_prodlyonnyy_sertifikat_popadaet_v_otpechatok():
    """Продление сертификата обязано считаться изменением.

    Прежний цикл слал сигнал раз в шесть часов БЕЗУСЛОВНО и тем самым подхватывал
    новый файл сам. Заменив безусловный сигнал на «только если изменилось», мы
    обязаны считать изменением и продление: иначе nginx продолжит отдавать старый
    сертификат до перезапуска контейнера — примерно три месяца, — и посетители
    увидят красный экран браузера.

    Ловушка тонкая: продление НЕ меняет ни шаблон, ни include-файлы, ни путь к
    файлу. Меняется только содержимое `fullchain.pem`, поэтому оно и обязано
    входить в отпечаток.
    """
    script = _read(RELOAD)
    schityvanie = [
        stroka.strip()
        for stroka in script.splitlines()
        if "md5sum" in stroka and not stroka.strip().startswith("#")
    ]
    assert schityvanie, "строка отпечатка пропала — сравнивать стало нечего"
    assert any("fullchain.pem" in stroka for stroka in schityvanie), (
        "сертификат не входит в отпечаток: продление пройдёт мимо, и nginx будет "
        "отдавать старый до перезапуска контейнера — примерно три месяца. "
        f"Считается сейчас по: {schityvanie}"
    )


def test_render_pri_starte_zovyot_skript_a_ne_upominaet_ego():
    """Рендер при старте — действие, а не комментарий.

    Проверка «в файле встречается reload.sh» довольствовалась упоминанием в
    пояснении: строку вызова можно было снять, и контейнер поднялся бы со стоковым
    конфигом из образа nginx — без наших маршрутов вовсе.
    """
    entrypoint = _read(ENTRYPOINT)
    vyzovy = [
        stroka.strip()
        for stroka in entrypoint.splitlines()
        if "reload.sh" in stroka and not stroka.strip().startswith("#")
    ]
    assert any("render" in v for v in vyzovy), (
        "при старте конфиг не рендерится: осталось одно упоминание в комментарии"
    )
    assert any("if-changed" in v for v in vyzovy), (
        "цикл перечитывания пропал — правки шаблонов перестанут применяться"
    )
