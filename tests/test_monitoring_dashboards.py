"""Дашборды: панель обязана уметь что-то показать.

Соседний `test_monitoring.py` стережёт сам стек — порты, потолки, профили,
секреты. Здесь про то, что стек показывает человеку, и главная беда тут другая:
**панель, которая физически не может ничего показать, выглядит точно так же, как
панель, которой нечего показать.** «No data» читается как «всё спокойно», и на
этом мониторинг заканчивается, не подав ни одного признака поломки.

Ровно это и случилось с панелью «Ошибки приложения». Запрос
`{service="app", level=~"error|critical|traceback"}` не совпадал почти ни с чем,
потому что метку `level` promtail проставлял **в том регистре, в каком слово
написано в строке**: uvicorn пишет `ERROR`, Python начинает трассировку с
`Traceback`, Redis говорит `WARNING`. Селектор Loki сравнивает точно. Замерено
живьём через `/loki/api/v1/label/level/values`: метка принимала значения
`Error`, `Traceback`, `WARNING`, `Warning`, `error`, `traceback` — и совпадали
только те, что случайно попали внутрь слов (`raise_for_error`,
`with_traceback`). То есть панель показывала обрывки чужих трассировок и молчала
о настоящих `ERROR` и `CRITICAL`.

Отсюда три семейства проверок ниже:

* запрос ссылается на то, что вправду собирается (метрика — на задание сбора,
  метка `level` — на то, что конвейер promtail умеет сделать);
* пустота различима: у панели ошибок есть счётчик всех строк, у панели тревог —
  явное «Спокойно» вместо «нет данных»;
* новая служба мониторинга выключается вместе с профилем и имеет потолок памяти.

Проверки читают только файлы: ни базы, ни сети. Гонять их можно везде, включая
образ `--target tests`, куда `docker/` копируется целиком.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = ROOT / "docker" / "docker-compose.yml"
MONITORING = ROOT / "docker" / "monitoring"
PROMETHEUS = MONITORING / "prometheus" / "prometheus.yml.template"
RULES = MONITORING / "prometheus" / "rules" / "opencrm.yml"
PROMTAIL = MONITORING / "promtail" / "promtail.yml"
DASHBOARDS = MONITORING / "grafana" / "dashboards"
DATASOURCES = MONITORING / "grafana" / "provisioning" / "datasources" / "datasources.yml"
APP_METRICS = ROOT / "web" / "api" / "routes" / "metrics.py"
CONTAINERS_EXPORTER = MONITORING / "containers" / "exporter.py"
ENV_EXAMPLE = ROOT / "docker" / ".env.example"

#: Новые службы сбора и их порты. Список руками — по образцу `SERVICES` в
#: test_monitoring.py: он и есть то, что проверяется. Служба, заведённая мимо
#: него, обязана уронить проверку, а не молча остаться без потолка памяти.
EKSPORTYORY = {
    "db-exporter": 9104,
    "redis-exporter": 9121,
}

#: Откуда берётся метрика: приставка имени → задание сбора в шаблоне Prometheus.
#: Порядок важен, берётся первое совпадение: `opencrm_container_` должен стоять
#: раньше общего `opencrm_`.
ISTOCHNIKI = (
    ("probe_", "site"),
    ("node_", "node"),
    ("opencrm_container_", "containers"),
    ("promtail_custom_", "nginx-logs"),
    ("mysql_", "mysql"),
    ("redis_", "redis"),
    ("opencrm_", "opencrm-app"),
)

#: Ряды, которые рождаются в самом Prometheus и не приходят ни от кого.
#: `ALERTS` и `ALERTS_FOR_STATE` — из правил, `up` — из результата опроса.
#: Важно: `up` не проходит через `metric_relabel_configs`, поэтому «цель не
#: отвечает» видно даже при самом узком списке `keep`.
SVOI_RYADY = {"up", "ALERTS", "ALERTS_FOR_STATE"}

#: Слова PromQL, которые не являются именами метрик.
NE_METRIKI = {"and", "or", "unless", "bool", "offset", "le", "inf", "nan"}


def _chitat(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _dashboards() -> dict[str, dict]:
    return {p.name: json.loads(_chitat(p)) for p in sorted(DASHBOARDS.glob("*.json"))}


def _paneli(dash: dict) -> list[dict]:
    """Все панели, включая вложенные в свёрнутые ряды."""
    out = []
    for panel in dash.get("panels", []):
        out.append(panel)
        out.extend(panel.get("panels", []) or [])
    return out


def _tseli(panel: dict) -> list[dict]:
    return panel.get("targets", []) or []


def _tip_istochnika(panel: dict, target: dict) -> str:
    src = target.get("datasource") or panel.get("datasource") or {}
    return src.get("type", "")


def _metriki_zaprosa(expr: str) -> set[str]:
    """Имена метрик из выражения PromQL.

    Дешёвый разбор вместо настоящего: убираем селекторы меток, окна, строки и
    списки группировки, а из оставшихся слов выбрасываем те, за которыми сразу
    скобка (это функции). Точности хватает — выражения на дашбордах простые, а
    ошибиться проверка может только в сторону «нашла лишнее», то есть в сторону
    красноты, а не молчания.
    """
    t = re.sub(r"\{[^}]*\}", " ", expr)
    t = re.sub(r"\[[^\]]*\]", " ", t)
    t = re.sub(r'"[^"]*"', " ", t)
    t = re.sub(r"\b(by|without|on|ignoring|group_left|group_right)\s*\([^)]*\)", " ", t)
    names: set[str] = set()
    for m in re.finditer(r"[A-Za-z_][A-Za-z0-9_]*", t):
        # Пробел до скобки допустим: `sum by (le) (rate(...))` после вырезания
        # списка группировки превращается в `sum  (rate(...))`.
        if re.match(r"\s*\(", t[m.end() :]):
            continue
        name = m.group(0)
        if name in NE_METRIKI:
            continue
        names.add(name)
    return names


def _prometheus_konfig() -> dict:
    """Шаблон разбирается как обычный YAML: `__SITE_URL__` — просто строка."""
    return yaml.safe_load(_chitat(PROMETHEUS))


def _zadaniya() -> dict[str, dict]:
    return {j["job_name"]: j for j in _prometheus_konfig()["scrape_configs"]}


def _keep_regex(job: str) -> str | None:
    """Список `keep` у задания: чем ограничен набор рядов от экспортёра."""
    for rule in _zadaniya()[job].get("metric_relabel_configs", []) or []:
        if rule.get("action") == "keep" and rule.get("source_labels") == ["__name__"]:
            return rule["regex"].strip()
    return None


def _compose() -> dict:
    return yaml.safe_load(_chitat(COMPOSE))


# --- источники данных ---------------------------------------------------------


def test_dashbordy_tselye_i_ssylayutsya_na_zavedyonnye_istochniki():
    """Панель со ссылкой на несуществующий uid показывает «datasource not found».

    Источники заводятся файлом (`provisioning/datasources.yml`) с явными uid —
    именно чтобы после переустановки Grafana панели не разошлись со своими
    источниками. Проверка держит вторую половину договорённости: uid, на который
    ссылается панель, обязан быть в том файле.
    """
    izvestnye = {
        ds["uid"]: ds["type"]
        for ds in yaml.safe_load(_chitat(DATASOURCES))["datasources"]
    }
    assert izvestnye, "источники данных не заводятся вовсе"

    for name, dash in _dashboards().items():
        for panel in _paneli(dash):
            if panel.get("type") == "row":
                continue
            src = panel.get("datasource") or {}
            uid = src.get("uid")
            assert uid in izvestnye, (
                f"{name}: панель «{panel.get('title')}» смотрит в источник {uid!r}, "
                f"которого нет в datasources.yml"
            )
            assert izvestnye[uid] == src.get("type"), (
                f"{name}: панель «{panel.get('title')}» объявляет тип {src.get('type')!r}, "
                f"а источник {uid} — {izvestnye[uid]!r}"
            )


# --- каждый запрос смотрит на то, что вправду собирается ----------------------


def test_kazhdaya_metrika_na_dashborde_kem_to_sobiraetsya():
    """Иначе панель рисует пустоту, неотличимую от «всё спокойно».

    Проверяется вся цепочка, а не наличие имени в тексте: метрика → задание
    сбора в prometheus.yml.template → тот, кто её отдаёт. Для экспортёров
    MySQL и Redis добавлено третье звено — список `keep`: ряд, не попавший в
    него, до хранилища не доезжает, и панель по нему пуста навсегда.
    """
    zadaniya = _zadaniya()
    app_text = _chitat(APP_METRICS)
    containers_text = _chitat(CONTAINERS_EXPORTER)
    promtail_text = _chitat(PROMTAIL)

    for name, dash in _dashboards().items():
        for panel in _paneli(dash):
            for target in _tseli(panel):
                if _tip_istochnika(panel, target) != "prometheus":
                    continue
                gde = f"{name}: панель «{panel.get('title')}», запрос {target.get('refId')}"
                for metrika in _metriki_zaprosa(target.get("expr", "")):
                    if metrika in SVOI_RYADY:
                        continue

                    job = next(
                        (j for pref, j in ISTOCHNIKI if metrika.startswith(pref)),
                        None,
                    )
                    assert job, f"{gde}: метрику {metrika} никто не собирает и собирать некому"
                    assert job in zadaniya, (
                        f"{gde}: метрика {metrika} ждёт задания сбора {job!r}, "
                        f"а его нет в prometheus.yml.template"
                    )

                    if metrika.startswith("opencrm_container_"):
                        assert metrika in containers_text, (
                            f"{gde}: сборщик контейнеров не отдаёт {metrika}"
                        )
                    elif metrika.startswith("opencrm_"):
                        assert metrika in app_text, (
                            f"{gde}: приложение не отдаёт {metrika} "
                            f"(web/api/routes/metrics.py)"
                        )
                    elif metrika.startswith("promtail_custom_"):
                        # Гистограмма выходит наружу тремя рядами; в конвейере
                        # названа одна.
                        base = re.sub(r"_(bucket|sum|count)$", "", metrika)
                        base = base[len("promtail_custom_") :]
                        assert base in promtail_text, (
                            f"{gde}: promtail не считает метрику {base}"
                        )
                    elif metrika.startswith(("mysql_", "redis_")):
                        keep = _keep_regex(job)
                        assert keep, (
                            f"{gde}: у задания {job} нет списка keep — "
                            f"экспортёр зальёт в хранилище всё подряд"
                        )
                        assert re.fullmatch(keep, metrika), (
                            f"{gde}: метрика {metrika} не попадает в список keep задания "
                            f"{job}, то есть до хранилища не доезжает. Допишите её в "
                            f"metric_relabel_configs в prometheus.yml.template"
                        )


# --- метка уровня: то, ради чего всё это -------------------------------------


def _stadii_urovnya() -> list[dict]:
    """Стадии конвейера promtail, которыми делается метка `level`."""
    konfig = yaml.safe_load(_chitat(PROMTAIL))
    for scrape in konfig["scrape_configs"]:
        for stage in scrape.get("pipeline_stages", []):
            match = stage.get("match")
            if not match or "nginx" not in match.get("selector", ""):
                continue
            stages = match.get("stages", [])
            if any("level" in str(s) for s in stages):
                return stages
    raise AssertionError("в promtail.yml не стало стадии, размечающей уровень")


def _urovni_konveyera() -> set[str]:
    """Значения, которые метка `level` может принять после конвейера."""
    stadii = _stadii_urovnya()
    vyrazhenie = next(
        (s["regex"]["expression"] for s in stadii if "regex" in s),
        None,
    )
    assert vyrazhenie, "уровень перестал выделяться регулярным выражением"
    gruppa = re.search(r"\(\?P<level>([^)]*)\)", vyrazhenie)
    assert gruppa, f"в выражении {vyrazhenie!r} нет группы level"
    return {alt.lower() for alt in gruppa.group(1).split("|")}


def test_uroven_privoditsya_k_odnomu_registru():
    """Без приведения метка `level` бесполезна, и это проверено живьём.

    `(?i)` заставляет regex ИСКАТЬ без разбора регистра, но в группу попадает
    то, что вправду написано в строке. На стенде метка принимала шесть значений
    сразу — `Error`, `Traceback`, `WARNING`, `Warning`, `error`, `traceback`, —
    а селектор Loki сравнивает точно. Снимите стадию `template` ниже, и панель
    «Ошибки приложения» снова замолчит при любых ошибках.
    """
    stadii = _stadii_urovnya()

    regexp = next((s["regex"]["expression"] for s in stadii if "regex" in s), "")
    assert regexp.startswith("(?i)"), (
        "поиск уровня стал разбирать регистр: `ERROR` от uvicorn перестанет находиться вовсе"
    )

    shablony = [s["template"] for s in stadii if "template" in s]
    privedenie = [
        s for s in shablony
        if s.get("source") == "level" and "ToLower" in s.get("template", "")
    ]
    assert privedenie, (
        "исчезла стадия приведения `level` к нижнему регистру. Метка снова примет вид "
        "`ERROR`, `Traceback`, `WARNING` — по тому, как слово написано в строке, — и "
        "запрос панели ошибок не совпадёт ни с одним из них"
    )

    poryadok = [i for i, s in enumerate(stadii) if "regex" in s or "template" in s or "labels" in s]
    imena = [next(iter(stadii[i])) for i in poryadok]
    assert imena.index("regex") < imena.index("template") < imena.index("labels"), (
        "приведение регистра обязано стоять МЕЖДУ выделением и навешиванием метки: "
        "иначе в Loki уедет исходное написание"
    )


def test_zaprosy_po_logam_sprashivayut_urovni_kotorye_konveyer_umeet_delat():
    """Сторож того самого случая: запрос, который не может совпасть никогда.

    Каждое значение из `level=~"..."` на дашборде обязано быть среди тех, что
    конвейер promtail в принципе способен положить в метку. Допишите в панель
    `fatal` или верните `Traceback` с большой буквы — проверка покраснеет здесь,
    а не тишиной на боевом сервере.
    """
    umeet = _urovni_konveyera()
    naydeno = 0

    for name, dash in _dashboards().items():
        for panel in _paneli(dash):
            for target in _tseli(panel):
                if _tip_istochnika(panel, target) != "loki":
                    continue
                expr = target.get("expr", "")
                for prosyat in re.findall(r'level\s*=~\s*"([^"]+)"', expr):
                    naydeno += 1
                    for uroven in prosyat.split("|"):
                        assert uroven in umeet, (
                            f"{name}: панель «{panel.get('title')}» просит level={uroven!r}, "
                            f"а конвейер promtail умеет делать только {sorted(umeet)}. "
                            f"Такой запрос не совпадёт никогда, и панель будет пустой "
                            f"независимо от того, есть ошибки или нет"
                        )
                for prosyat in re.findall(r'level\s*=\s*"([^"]+)"', expr):
                    naydeno += 1
                    assert prosyat in umeet, (
                        f"{name}: панель «{panel.get('title')}» просит level={prosyat!r}, "
                        f"чего конвейер не делает"
                    )

    assert naydeno, "исчезли панели, отбирающие логи по уровню, — проверка перестала что-либо стеречь"


def test_sluzhba_v_zaprose_po_logam_est_sredi_tekh_chi_logi_chitayut():
    """`service="app"` берётся из метки контейнера, а не из воздуха.

    promtail оставляет только контейнеры своего проекта и переносит имя службы
    compose в метку `service`. Панель, спрашивающая службу, которой в compose
    нет, будет пуста всегда.
    """
    sluzhby = set(_compose()["services"])
    promtail = _chitat(PROMTAIL)
    assert "com_docker_compose_service" in promtail, (
        "имя службы перестало браться из метки docker — метка service станет чужой"
    )

    for name, dash in _dashboards().items():
        for panel in _paneli(dash):
            for target in _tseli(panel):
                if _tip_istochnika(panel, target) != "loki":
                    continue
                for sluzhba in re.findall(r'service\s*=\s*"([^"]+)"', target.get("expr", "")):
                    assert sluzhba in sluzhby, (
                        f"{name}: панель «{panel.get('title')}» ждёт логи службы "
                        f"{sluzhba!r}, а такой службы в docker-compose.yml нет"
                    )


def test_pustaya_panel_oshibok_otlichima_ot_slomannoy():
    """«Ошибок нет» и «логи не доезжают» обязаны различаться с одного взгляда.

    Панель ошибок молчит в обоих случаях, и более спокойным выглядит худший.
    Рядом с ней обязан стоять счётчик ВСЕХ строк той же службы: число есть —
    молчание панели ошибок означает спокойствие; счётчик пуст — молчание не
    означает ничего.
    """
    for name, dash in _dashboards().items():
        paneli = _paneli(dash)

        oshibki = [
            p for p in paneli
            if p.get("type") == "logs"
            and any(
                "level" in t.get("expr", "")
                for t in _tseli(p)
                if _tip_istochnika(p, t) == "loki"
            )
        ]
        if not oshibki:
            continue

        sluzhby = set()
        for panel in oshibki:
            for target in _tseli(panel):
                sluzhby.update(re.findall(r'service\s*=\s*"([^"]+)"', target.get("expr", "")))

        for sluzhba in sluzhby:
            schyotchiki = [
                p for p in paneli
                if p.get("type") == "stat"
                and any(
                    f'{{service="{sluzhba}"}}' in t.get("expr", "")
                    and "count_over_time" in t.get("expr", "")
                    for t in _tseli(p)
                    if _tip_istochnika(p, t) == "loki"
                )
            ]
            assert schyotchiki, (
                f"{name}: у панели ошибок службы {sluzhba!r} нет счётчика всех её строк. "
                f"Без него пустая панель читается как «ошибок нет», хотя может означать "
                f"«логи до нас не доезжают» — а это слепота, выглядящая как спокойствие"
            )
            for panel in schyotchiki:
                assert panel["fieldConfig"]["defaults"].get("noValue"), (
                    f"{name}: счётчик строк «{panel.get('title')}» без noValue покажет "
                    f"«No data» — то есть ту же двусмысленность, ради снятия которой стоит"
                )


# --- на что смотреть прямо сейчас --------------------------------------------


def _paneli_trevog(dash: dict) -> list[dict]:
    return [
        p for p in _paneli(dash)
        if any("ALERTS" in t.get("expr", "") for t in _tseli(p))
    ]


def test_panel_trevog_stoit_pervoy_a_ne_posledney():
    """Первое, что попадается на глаза, а не то, до чего долистали."""
    dash = _dashboards()["opencrm-site.json"]
    trevogi = _paneli_trevog(dash)
    assert trevogi, "с дашборда сайта исчезла панель действующих тревог"

    verkh = min(p["gridPos"]["y"] for p in dash["panels"])
    for panel in trevogi:
        assert panel["gridPos"]["y"] == verkh, (
            f"панель «{panel.get('title')}» уехала вниз (y={panel['gridPos']['y']}), "
            f"а верх дашборда — {verkh}"
        )

    skolko = len(trevogi)
    assert [p.get("id") for p in dash["panels"][:skolko]] == [p.get("id") for p in trevogi], (
        "панели тревог перестали идти первыми в списке — в узком окне Grafana покажет "
        "их после всего остального"
    )


#: Панели, которым ЗАКОННО показывать пустоту зелёной, — и почему.
#:
#: Список закрытый, как и прочие реестры в этом наборе: строка без причины здесь
#: и есть та самая тихая эрозия.
ZELYONAYA_PUSTOTA_RAZRESHENA = {
    # «Ошибок за сутки» читается НЕ В ОДИНОЧКУ, и это записано в её же подписи:
    # рядом стоит счётчик ВСЕХ строк приложения. Пусто у обеих — логи не идут;
    # пусто только здесь — ошибок вправду нет. Пару стережёт соседняя проверка
    # `test_pustaya_panel_oshibok_otlichima_ot_slomannoy`.
    "Ошибок за сутки",
}


def test_pustota_ne_vyglyadit_blagopoluchiem():
    """«Копий нет» на зелёном — худшая надпись из возможных, и она была.

    Владелец прислал снимок боевой панели: крупными белыми буквами «копий нет»
    на ярко-зелёном поле. Grafana красит текст `noValue` цветом БАЗОВОГО порога,
    а базовый был зелёным — тем самым, которым красится свежая копия.

    Причин у пустоты две, и обе плохие: копий вправду нет либо метрики
    приложения не доезжают вовсе (у владельца был как раз второй случай —
    `/api/v1/metrics` отвечал 403). Ни в одном прочтении это не хорошая новость.

    Проверка требует: если панель заготовила текст на случай пустоты, этот
    случай не должен краситься зелёным. Либо база порога не зелёная, либо
    пустоты не бывает вовсе (`or vector(...)` даёт число всегда), либо панель
    названа в списке исключений с доводом.
    """
    vinovnye = []
    for name, dash in _dashboards().items():
        for panel in _paneli(dash):
            if panel.get("type") != "stat":
                continue
            zagolovok = panel.get("title", "")
            if zagolovok in ZELYONAYA_PUSTOTA_RAZRESHENA:
                continue
            defaults = panel.get("fieldConfig", {}).get("defaults", {})
            if not defaults.get("noValue"):
                continue
            stupeni = defaults.get("thresholds", {}).get("steps") or []
            baza = stupeni[0].get("color") if stupeni else None
            if baza not in ("green", None):
                continue
            # Пустоты не бывает: запрос всегда возвращает число.
            if any("or vector(" in (t.get("expr") or "") for t in _tseli(panel)):
                continue
            vinovnye.append(f"{name}: «{zagolovok}» → {defaults['noValue']!r} на зелёном")

    assert not vinovnye, (
        "пустота выдаётся за благополучие:\n  " + "\n  ".join(vinovnye)
        + "\n\nЛибо база порога не зелёная, либо `or vector(...)` вместо пустоты, "
        "либо строка в ZELYONAYA_PUSTOTA_RAZRESHENA с доводом."
    )


def test_kopiya_otlichaet_net_kopiy_ot_net_metrik():
    """Две разные беды не имеют права выглядеть одинаково.

    Ряд `opencrm_backup_last_timestamp_seconds` пропадает и когда копий нет, и
    когда метрик нет вовсе. Различитель приложение отдаёт САМО:
    `opencrm_backup_count` пишется всегда, нулём при отсутствии копий, — именно
    затем, и это записано в докстроке `_collect_backups`. Панель им не
    пользовалась.
    """
    dash = _dashboards()["opencrm-site.json"]
    panel = next(
        (p for p in _paneli(dash) if p.get("title") == "Последняя копия"), None
    )
    assert panel, "исчезла панель последней копии"

    vyrazheniya = " ".join(t.get("expr", "") for t in _tseli(panel))
    assert "opencrm_backup_count" in vyrazheniya, (
        "панель копии снова смотрит только на метку времени: «копий нет» и "
        "«метрик нет» станут неразличимы"
    )
    podstanovki = json.dumps(
        panel["fieldConfig"]["defaults"].get("mappings", []), ensure_ascii=False
    )
    for slova in ("копий нет", "метрик нет"):
        assert slova in podstanovki, f"в подстановках панели нет случая «{slova}»"
    assert '"green"' not in podstanovki, (
        "особый случай на панели копии покрашен зелёным — ровно то, что чинилось"
    )


def test_pustaya_panel_trevog_chitaetsya_kak_spokoyno():
    """«No data» на панели тревог — самая опасная надпись из возможных.

    Она означает ровно то же, что и «всё хорошо», и появляется в двух прямо
    противоположных случаях. Поэтому у счётчика тревог стоит `or vector(0)`:
    ответ есть всегда, и ноль — это ответ, а не пустота.
    """
    dash = _dashboards()["opencrm-site.json"]
    trevogi = _paneli_trevog(dash)

    schyotchiki = [p for p in trevogi if p.get("type") == "stat"]
    assert schyotchiki, "исчез счётчик тревог — таблица в пустом виде скажет «No data»"

    for panel in schyotchiki:
        vyrazheniya = [t.get("expr", "") for t in _tseli(panel)]
        assert any("or vector(0)" in e for e in vyrazheniya), (
            f"у панели «{panel.get('title')}» пропал `or vector(0)`: при отсутствии тревог "
            f"запрос вернёт пустоту, и панель напишет «нет данных» вместо «спокойно»"
        )
        defaults = panel["fieldConfig"]["defaults"]
        assert "Спокойно" in json.dumps(defaults, ensure_ascii=False), (
            f"у панели «{panel.get('title')}» ноль больше не подписан «Спокойно» — "
            f"человеку придётся вспоминать, что значит цифра 0"
        )

    for panel in trevogi:
        assert panel["fieldConfig"]["defaults"].get("noValue") == "Спокойно", (
            f"у панели «{panel.get('title')}» нет noValue «Спокойно»"
        )


def test_u_kazhdogo_pravila_est_chelovecheskiy_tekst_v_tablitse():
    """Таблица тревог показывает имя правила, а не то, что случилось.

    `ALERTS` несёт только МЕТКИ правила; `summary` и `description` — это
    аннотации, и в ряд они не попадают вовсе. Поэтому текст лежит подстановкой
    в самой панели, а эта проверка держит её полной: новое правило без строки
    здесь покраснеет тут, а не покажет владельцу голое `SiteDown`.
    """
    imena = set(re.findall(r"^\s*- alert:\s*(\w+)", _chitat(RULES), re.M))
    assert imena, "не разобрались имена правил"

    dash = _dashboards()["opencrm-site.json"]
    tablitsy = [p for p in _paneli_trevog(dash) if p.get("type") == "table"]
    assert tablitsy, "исчезла таблица действующих тревог"

    podstanovki: set[str] = set()
    for panel in tablitsy:
        for override in panel["fieldConfig"].get("overrides", []):
            for prop in override.get("properties", []):
                if prop.get("id") != "mappings":
                    continue
                for mapping in prop.get("value", []):
                    if mapping.get("type") == "value":
                        podstanovki.update(mapping.get("options", {}))

    poteryalis = sorted(imena - podstanovki)
    assert not poteryalis, (
        f"у правил {poteryalis} нет человеческого текста в панели «Действующие тревоги» "
        f"(docker/monitoring/grafana/dashboards/opencrm-site.json). Владелец увидит "
        f"английское имя правила и пойдёт искать, что оно значит"
    )

    lishnie = sorted(podstanovki - imena - {"critical", "warning"})
    assert not lishnie, (
        f"в таблице остались подстановки для правил, которых больше нет: {lishnie}"
    )


def test_skolko_visit_beryotsya_iz_ryada_a_ne_ugadyvaetsya():
    """«Час назад» и «только что» требуют разных действий."""
    dash = _dashboards()["opencrm-site.json"]
    tablitsy = [p for p in _paneli_trevog(dash) if p.get("type") == "table"]
    for panel in tablitsy:
        vyrazheniya = " ".join(t.get("expr", "") for t in _tseli(panel))
        assert "ALERTS_FOR_STATE" in vyrazheniya, (
            f"панель «{panel.get('title')}» больше не показывает, сколько тревога висит"
        )
        assert "ignoring(alertstate)" in vyrazheniya, (
            "ALERTS_FOR_STATE не несёт метки alertstate, и без `ignoring(alertstate)` "
            "пересечение с ALERTS не даст ни одной строки — таблица окажется всегда пустой"
        )


# --- новые службы сбора -------------------------------------------------------


def test_eksportyory_vyklyuchayutsya_vmeste_s_monitoringom():
    """Выключенный блок — это контейнеры, которых в стеке нет вовсе.

    Профиль и ротация логов приезжают общим якорем `x-monitoring`, поэтому
    проверяется именно он: служба, подключённая мимо якоря, поднимется на
    машине, где мониторинг выключен.
    """
    text = _chitat(COMPOSE)
    compose = _compose()

    for name in EKSPORTYORY:
        assert name in compose["services"], f"службы {name} нет в docker-compose.yml"
        blok = re.search(rf"\n  {re.escape(name)}:\n(.*?)(?=\n  \w|\n\n  |\Z)", text, re.S)
        assert blok, f"не разобрался блок службы {name}"
        assert "<<: *monitoring_defaults" in blok.group(1), (
            f"{name} подключён мимо якоря x-monitoring: без профиля он поднимется там, "
            f"где мониторинг выключен, и без ротации логов"
        )


def test_u_eksportyorov_est_potolok_pamyati():
    """Без потолка OOM-killer уносит не наблюдателя, а сайт.

    Замерено на стенде: 7.8 МиБ в покое у каждого. Потолок 64 МБ — запас на
    всплеск при опросе, как у node-exporter и blackbox.
    """
    compose = _compose()
    for name in EKSPORTYORY:
        limit = compose["services"][name].get("mem_limit")
        assert limit, f"у {name} нет mem_limit"
        assert limit.endswith("m"), f"потолок {name} задан не в мегабайтах: {limit}"
        assert int(limit[:-1]) <= 128, (
            f"потолок {name} — {limit}: экспортёр ест меньше десяти мегабайт, "
            f"такой запас забирает память у сайта"
        )


def test_eksportyory_ne_publikuyut_portov_naruzhu():
    """Метрики базы — это карта нагрузки; наружу их не выставляют.

    Разговор идёт внутри сети compose: `expose`, а не `ports`.
    """
    compose = _compose()
    for name, port in EKSPORTYORY.items():
        sluzhba = compose["services"][name]
        assert "ports" not in sluzhba, (
            f"{name} публикует порт на хост — метрики станут видны всякому, кто "
            f"просканировал порты"
        )
        assert str(port) in [str(p) for p in sluzhba.get("expose", [])], (
            f"{name} не объявляет порт {port}"
        )


def test_zadaniya_sbora_stuchatsya_po_tem_zhe_imenam_i_portam():
    """Опечатка в имени службы даёт вечно красную цель и пустые панели."""
    zadaniya = _zadaniya()
    compose = _compose()
    ozhidaem = {"mysql": "db-exporter", "redis": "redis-exporter"}

    for job, sluzhba in ozhidaem.items():
        assert job in zadaniya, f"нет задания сбора {job!r} в prometheus.yml.template"
        tseli = zadaniya[job]["static_configs"][0]["targets"]
        port = EKSPORTYORY[sluzhba]
        assert tseli == [f"{sluzhba}:{port}"], (
            f"задание {job} стучится в {tseli}, а служба зовётся {sluzhba} и слушает {port}"
        )
        assert sluzhba in compose["services"]


def test_ryady_eksportyorov_urezany_spiskom():
    """Один экспортёр не имеет права удвоить хранилище.

    Замерено на стенде: mysqld-exporter отдаёт 992 ряда, redis-exporter — 367,
    а всего в этом Prometheus их было 2144. Потолок хранения — 512 МБ, и без
    отбора в него упёрлись бы раньше, чем истекут пятнадцать дней.
    """
    for job in ("mysql", "redis"):
        keep = _keep_regex(job)
        assert keep, f"у задания {job} нет списка keep — экспортёр зальёт всё подряд"
        assert not re.fullmatch(keep, "mysql_info_schema_table_rows"), (
            "список keep пропускает лишнее"
        )
        assert re.fullmatch(keep, "mysql_up" if job == "mysql" else "redis_up"), (
            f"список keep у {job} отсекает даже метрику «служба отвечает»"
        )


def test_eksportyor_bazy_khodit_pod_svoim_polzovatelem():
    """Наблюдателю нужны три права и ни одной таблицы с данными клиентов.

    Пароль root'а или пользователя приложения в контейнере наблюдателя — это
    ещё одна копия ключа от всей базы, лежащая там, где она не нужна.
    """
    compose = _compose()
    sluzhba = compose["services"]["db-exporter"]
    command = " ".join(sluzhba["command"])
    okruzhenie = json.dumps(sluzhba.get("environment", {}), ensure_ascii=False)

    assert "OPENCRM_DB_EXPORTER_USER" in command, (
        "пользователь экспортёра больше не берётся из docker/.env"
    )
    assert "root" not in command.lower(), "экспортёр ходит в базу под root"
    for chuzhoe in ("OPENCRM_DB_ROOT_PASSWORD", "OPENCRM_DB_PASSWORD"):
        assert chuzhoe not in okruzhenie + command, (
            f"в контейнер экспортёра уехал {chuzhoe} — чужой ключ от всей базы"
        )
    assert "MYSQLD_EXPORTER_PASSWORD" in okruzhenie, (
        "пароль экспортёра передаётся не переменной окружения: аргументы командной "
        "строки видны в `ps` внутри контейнера и в `docker inspect`"
    )


def test_prava_eksportyora_nazvany_i_ne_shire_nuzhnogo():
    """Запрос на создание лежит там, где его найдут, — рядом с паролем."""
    text = _chitat(ENV_EXAMPLE)
    assert "OPENCRM_DB_EXPORTER_PASSWORD" in text
    grant = "\n".join(s for s in text.splitlines() if "GRANT" in s.upper())
    assert grant, "в docker/.env.example нет запроса на создание пользователя наблюдателя"
    assert "PROCESS" in grant and "REPLICATION CLIENT" in grant
    assert "performance_schema" in grant, "нет права читать performance_schema"
    assert "ALL PRIVILEGES" not in grant.upper(), "наблюдателю выдаются все права разом"
    assert not re.search(r"SELECT\s+ON\s+\*\.\*", grant, re.I), (
        "SELECT на всё сразу открывает наблюдателю данные клиентов"
    )


# --- секреты ------------------------------------------------------------------


def test_v_dashbordakh_i_shablonakh_net_ni_odnogo_sekreta():
    """Дашборды лежат в репозитории и уезжают на каждую установку.

    Пароли живут в docker/.env с правами 600 и попадают в контейнер только
    переменными окружения. Ни значения, ни даже подстановки `${...}` в этих
    файлах быть не должно — подстановку compose здесь делать некому.
    """
    podozritelnye = re.compile(
        r"(password|passwd|secret|token|api[_-]?key)\s*[:=]\s*['\"]?[A-Za-z0-9!@#$%^&*_+\-]{6,}",
        re.I,
    )
    for path in [*sorted(DASHBOARDS.glob("*.json")), PROMETHEUS, PROMTAIL]:
        text = _chitat(path)
        naydeno = podozritelnye.search(text)
        assert not naydeno, f"{path.name}: похоже на секрет — {naydeno.group(0)!r}"
        if path.suffix == ".json":
            assert "${" not in text, (
                f"{path.name}: подстановка окружения в дашборде не сработает — "
                f"Grafana читает файл как есть"
            )


def test_paroli_eksportyorov_prikhodyat_tolko_podstanovkoy_iz_okruzheniya():
    """В репозитории — имя переменной, значение — в docker/.env с правами 600."""
    compose = _compose()
    for name in EKSPORTYORY:
        okruzhenie = compose["services"][name].get("environment", {}) or {}
        for klyuch, znachenie in okruzhenie.items():
            if not re.search(r"password", klyuch, re.I):
                continue
            assert isinstance(znachenie, str) and znachenie.startswith("${"), (
                f"{name}: {klyuch} задан значением {znachenie!r}, а не подстановкой из "
                f"docker/.env — пароль уехал бы в репозиторий"
            )


@pytest.mark.parametrize("name", sorted(EKSPORTYORY))
def test_eksportyor_ne_zhdyot_togo_za_kem_nablyudaet(name):
    """Наблюдатель не имеет права стоять в очереди за наблюдаемым.

    `depends_on: db` означал бы, что при не поднявшейся базе не поднимается и
    тот, кто должен об этом рассказать. Правильный ответ на лежачую базу —
    `mysql_up 0`, а не отсутствие контейнера.
    """
    sluzhba = _compose()["services"][name]
    assert "depends_on" not in sluzhba, (
        f"{name} ждёт другую службу: при её падении молчать будет и наблюдатель"
    )
