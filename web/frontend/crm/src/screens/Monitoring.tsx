import { useCallback, useEffect, useState } from "react";

import { Icon } from "../components/Icon";
import { Chip, ScreenLoading } from "../components/ui";
import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { useFailure } from "../lib/failure";
import { formatDateTime } from "../lib/format";
import type { TranslationKey } from "../lib/i18n";

interface MonitoringStatus {
  checked_at: string;
  panel: { path: string; state: string };
  grafana: { reachable: boolean; version: string | null };
  targets: { reachable: boolean; up: number; total: number; down: string[] };
  site: { url: string | null; up: boolean | null };
  alerts: { reachable: boolean; firing: { name: string; severity: string }[] };
  channel: { reachable: boolean; configured: boolean | null };
  logs: { on: boolean | null };
}

/**
 * Что означает исход пробы пути к панели.
 *
 * Четыре состояния, и различить их человек сам не может ничем: снаружи «стек не
 * поднят» и «nginx работает по старому конфигу» выглядят совершенно одинаково —
 * ровно это и продержалось на боевом пять суток. Поэтому у каждого исхода своя
 * фраза и своя команда-лекарство.
 *
 * Картой, а не ключом, собранным из состояния шаблонной строкой: такой ключ
 * невидим для проверки мёртвых переводов (`tests/test_screens.py`), и её
 * пришлось бы учить его видеть — а до тех пор переводы молча умирали бы вместе
 * с экранами, которые их показывали.
 */
const PATH_STATE: Record<string, TranslationKey> = {
  open: "monPathOpen",
  off: "monPathOff",
  stale: "monPathStale",
  no_answer: "monPathSilent",
};

/** Строка состояния: подпись слева, ответ справа. Ничего не открывает. */
function StateRow({
  name,
  chip,
  good,
  note,
}: {
  name: string;
  chip: string;
  /** Зелёный чип или янтарный. Красного здесь нет намеренно: сломанное
   *  наблюдение — это повод пойти на сервер, а не авария сайта. */
  good?: boolean;
  note?: string;
}) {
  return (
    <div className="state-row">
      <span className="state-name">{name}</span>
      <span className="state-value">
        <span>
          <Chip variant={good ? "success" : "warning"}>{chip}</Chip>
        </span>
        {note && <span className="state-note">{note}</span>}
      </span>
    </div>
  );
}

/**
 * Мониторинг: путь к панели и её состояние.
 *
 * Экран, а не голая ссылка в меню, по одной причине: ссылка молчит ровно тогда,
 * когда всё сломалось. На боевом 12 августа сломался именно путь — Grafana была
 * жива, а `/monitoring/` отвечал так, будто мониторинг выключен, — и узнать об
 * этом человеку было неоткуда. Кнопка «Открыть панель» здесь главная, но рядом с
 * ней стоит то, чего кнопка сказать не умеет.
 *
 * Адрес экрана `/server`, а НЕ `/monitoring`: последний перехватывает nginx
 * (`location = /monitoring { return 301 /monitoring/; }`), и переход внутри SPA
 * работал бы, а F5 и закладка уводили бы в Grafana. Поломка тихая и вылезла бы
 * уже на боевом.
 *
 * Настраивать здесь нечего и незачем: стек поднимается командой на сервере, у
 * приложения нет ни docker.sock, ни права его иметь. Кнопка «включить
 * мониторинг» была бы обещанием, а не функцией, поэтому экран называет команду.
 */
export function Monitoring() {
  const { t, locale } = useApp();
  const [status, setStatus] = useState<MonitoringStatus | null>(null);
  const { failure, fail, clear } = useFailure();

  const load = useCallback(async () => {
    clear();
    try {
      setStatus(await api.get<MonitoringStatus>("/system/monitoring"));
    } catch (e) {
      fail(e);
    }
  }, [fail, clear]);

  useEffect(() => {
    void load();
  }, [load]);

  if (!status) return <ScreenLoading error={failure} onRetry={() => void load()} />;

  const { panel, grafana, targets, site, alerts, channel, logs } = status;

  return (
    <div className="page page-narrow">
      <div className="page-head" style={{ alignItems: "flex-start" }}>
        <div>
          <h1 className="page-title">{t("modMonitoring")}</h1>
          <div className="page-sub">{t("modMonitoringAbout")}</div>
        </div>
        {/* Обычная внешняя ссылка, а не переход внутри SPA: Grafana живёт вне
            приложения, на том же домене отдельным путём. Новая вкладка — чтобы
            не терять место в CRM: в панель уходят посмотреть, а не работать. */}
        <a
          className="btn btn-primary"
          href={panel.path}
          target="_blank"
          rel="noreferrer"
          style={{ flex: "none", display: "inline-flex", alignItems: "center", gap: 7 }}
        >
          <Icon name="link" size={15} />
          {t("monOpenPanel")}
        </a>
      </div>

      {/* Что именно откроется и что там спросят. Панель — чужая программа со
          своим входом, и человек, впервые нажавший кнопку, упирается в форму
          пароля, которого у него нет на руках. */}
      <div className="card card-pad" style={{ marginBottom: 18 }}>
        <div style={{ fontSize: 13, lineHeight: 1.6 }}>{t("monOpensWhat")}</div>
        <div className="field-desc">{t("monSignIn")}</div>
      </div>

      <div className="section-head">
        <div style={{ fontSize: 14, fontWeight: 600 }}>{t("monState")}</div>
        <button type="button" className="text-link" onClick={() => void load()}>
          {t("monRefresh")}
        </button>
      </div>

      <div className="list-card">
        <StateRow
          name={t("monPath")}
          chip={t(PATH_STATE[panel.state] ?? "monPathSilent")}
          good={panel.state === "open"}
          note={panel.path}
        />
        <StateRow
          name={t("monService")}
          chip={
            grafana.reachable
              ? t("monVersion", { version: grafana.version ?? "?" })
              : t("monSilent")
          }
          good={grafana.reachable}
        />
        <StateRow
          name={t("monTargets")}
          chip={
            targets.reachable
              ? t("monTargetsUp", { up: targets.up, total: targets.total })
              : t("monSilent")
          }
          good={targets.reachable && targets.down.length === 0}
          note={
            targets.down.length > 0
              ? t("monTargetsDown", { list: targets.down.join(", ") })
              : undefined
          }
        />
        {/* Куда идёт внешняя проверка — и, главное, чего она не видит.
            Строка стоит сразу за целями: адрес берётся из их же метки.

            Оговорка про роутер здесь, а не в конце главы документации, и это
            не педантизм. Сервер за NAT не дозванивается до собственного
            публичного адреса, поэтому проверку переводят на локальный — и она
            перестаёт видеть роутер. Отвалится проброс портов, сайт ляжет для
            всего мира, а этот экран останется зелёным. Человек обязан знать
            границу того, что ему обещают. */}
        <StateRow
          name={t("monSite")}
          chip={site.url ?? t("monSiteNone")}
          good={site.up === true}
          note={site.url ? t("monSiteBlind") : undefined}
        />
        <StateRow
          name={t("monAlerts")}
          chip={
            !alerts.reachable
              ? t("monSilent")
              : alerts.firing.length === 0
                ? t("monAlertsQuiet")
                : alerts.firing.map((alert) => alert.name).join(", ")
          }
          good={alerts.reachable && alerts.firing.length === 0}
        />
        <StateRow
          name={t("monChannel")}
          chip={
            !channel.reachable
              ? t("monSilent")
              : channel.configured
                ? t("monChannelOn")
                : t("monChannelOff")
          }
          good={channel.reachable && channel.configured === true}
          note={channel.reachable && !channel.configured ? t("monChannelOffWhy") : undefined}
        />
        {/* Три состояния, а не два: `null` — это «Prometheus не ответил», и
            выдать его за «логи выключены» значило бы соврать о причине. */}
        <StateRow
          name={t("monLogs")}
          chip={logs.on === null ? t("monSilent") : logs.on ? t("monLogsOn") : t("monLogsOff")}
          good={logs.on === true}
          note={logs.on === false ? t("monLogsOffWhy") : undefined}
        />
      </div>

      <div className="page-sub" style={{ marginTop: 14 }}>
        {t("monChecked", { when: formatDateTime(status.checked_at, locale) })} · {t("monDoctor")}
      </div>
    </div>
  );
}
