import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { Icon } from "../components/Icon";
import { EmptyState, ScreenLoading } from "../components/ui";
import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { useFailure } from "../lib/failure";
import { formatMoney } from "../lib/format";
import { sourceLabel } from "../lib/sources";

/**
 * Дата в вид, который понимает `<input type="date">` и сервер.
 *
 * `toISOString().slice(0, 10)` здесь неверен: он переводит момент в UTC, и в
 * Киеве в час ночи отдаёт вчерашнее число. Собираем строку из местных полей —
 * пользователь выбирает день своего календаря, а не UTC.
 */
function isoDay(date: Date): string {
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${date.getFullYear()}-${month}-${day}`;
}

/** Готовые периоды: за отчётом приходят с одним из трёх вопросов. */
function presets(now: Date) {
  const monthStart = new Date(now.getFullYear(), now.getMonth(), 1);
  const prevStart = new Date(now.getFullYear(), now.getMonth() - 1, 1);
  const prevEnd = new Date(now.getFullYear(), now.getMonth(), 0);
  return [
    { key: "periodThisMonth" as const, from: isoDay(monthStart), to: isoDay(now) },
    { key: "periodLastMonth" as const, from: isoDay(prevStart), to: isoDay(prevEnd) },
    {
      key: "periodThisYear" as const,
      from: isoDay(new Date(now.getFullYear(), 0, 1)),
      to: isoDay(now),
    },
  ];
}

/** Прочерк вместо нуля: «делить было не на что» и «ноль процентов» — разное. */
function percent(value: number | null, locale: string): string {
  if (value === null || value === undefined) return "—";
  return `${value.toFixed(1).replace(".", locale === "ru" ? "," : ".")}%`;
}

export function Reports() {
  const { t, locale } = useApp();
  const today = useMemo(() => new Date(), []);
  const quick = useMemo(() => presets(today), [today]);
  const [from, setFrom] = useState(quick[0].from);
  const [to, setTo] = useState(quick[0].to);
  const [data, setData] = useState<{ funnel: any; revenue: any; sources: any } | null>(null);

  // Смещение зоны браузера едет вместе с датами: «за июль» человек понимает по
  // своему календарю, а в базе время в UTC. Без этого 31 июля терял бы вечер, а
  // 1 августа приносил бы чужие сделки.
  const query = useMemo(
    () =>
      new URLSearchParams({
        from,
        to,
        tz_offset: String(new Date().getTimezoneOffset()),
      }).toString(),
    [from, to],
  );

  const { failure, fail, clear } = useFailure();
  // Периоды переключают быстрее, чем отвечает сервер: без этого счётчика ответ
  // на прошлый период мог бы лечь поверх текущего и показать чужие числа.
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let current = true;
    clear();
    Promise.all([
      api.get(`/reports/funnel?${query}`),
      api.get(`/reports/revenue?${query}`),
      api.get(`/reports/sources?${query}`),
    ])
      .then(([funnel, revenue, sources]) => {
        if (current) setData({ funnel, revenue, sources });
      })
      .catch((e) => {
        if (current) fail(e);
      });
    return () => {
      current = false;
    };
  }, [query, attempt, fail, clear]);

  if (!data) {
    return <ScreenLoading error={failure} onRetry={() => setAttempt((n) => n + 1)} />;
  }

  const { funnel, revenue, sources } = data;
  const currency = revenue.currency ?? "USD";
  const money = (value: number | null) => formatMoney(value, currency, locale);

  // Ширина полосы — доля от самого населённого этапа, а не от общего числа: при
  // пяти этапах доли от целого дают пять одинаково коротких полос.
  const maxEntered = Math.max(1, ...funnel.stages.map((s: any) => s.entered));
  // `?? 0` не украшение: месяц, в котором ни у одной сделки не названа цена,
  // приходит с прочерком (null), а `Math.max(null, null)` дал бы 0 молча, зато
  // `null / maxMonth` ниже — высоту столбика NaN.
  const maxMonth = Math.max(
    1,
    ...revenue.months.map((m: any) => Math.max(m.won_amount ?? 0, m.lost_amount ?? 0)),
  );
  const maxSource = Math.max(1, ...sources.items.map((row: any) => row.revenue ?? 0));

  return (
    <div className="page page-wide">
      <div className="page-head" style={{ alignItems: "flex-start" }}>
        <div>
          <h1 className="page-title">{t("reports")}</h1>
          <div className="page-sub">{t("reportsSub")}</div>
        </div>
      </div>

      <div className="report-period">
        {quick.map((preset) => (
          <button
            key={preset.key}
            className={
              "filter-chip" + (from === preset.from && to === preset.to ? " active" : "")
            }
            onClick={() => {
              setFrom(preset.from);
              setTo(preset.to);
            }}
          >
            {t(preset.key)}
          </button>
        ))}
        <span className="report-period-sep" />
        <input
          className="input report-date"
          type="date"
          value={from}
          max={to}
          onChange={(e) => e.target.value && setFrom(e.target.value)}
        />
        <span style={{ color: "var(--faint)" }}>—</span>
        <input
          className="input report-date"
          type="date"
          value={to}
          min={from}
          onChange={(e) => e.target.value && setTo(e.target.value)}
        />
      </div>

      {/* --- воронка --- */}
      <div className="card card-pad report-card">
        <div className="section-head" style={{ marginBottom: 14 }}>
          <div className="metric-title">{t("funnel")}</div>
          <ExportLink name="funnel" query={query} label={t("exportCsv")} />
        </div>

        <div className="report-grid">
          <Figure title={t("repEntered")} value={String(funnel.entered)} sub={t("repEnteredHint")} />
          <Figure title={t("repWon")} value={String(funnel.won)} />
          <Figure title={t("repLost")} value={String(funnel.lost)} />
          <Figure
            title={t("repConversion")}
            value={percent(funnel.conversion, locale)}
            sub={t("repConversionHint")}
          />
        </div>

        {funnel.entered === 0 && funnel.won === 0 && funnel.lost === 0 ? (
          <EmptyState title={t("repNothing")} />
        ) : (
          <div className="funnel">
            {funnel.stages.map((stage: any) => (
              <Link
                key={stage.key}
                to={`/deals?stage=${encodeURIComponent(stage.key)}`}
                className={"funnel-step kind-" + stage.kind}
              >
                <span className="funnel-count">{stage.entered}</span>
                <span className="funnel-name">{stage.name}</span>
                {/* Конверсия к предыдущему шагу — то самое число, которое
                    показывает, ГДЕ теряют, а не сколько потеряли всего. */}
                <span className="funnel-step-share">
                  {stage.from_previous === null ? "" : percent(stage.from_previous, locale)}
                </span>
                <span
                  className="funnel-bar"
                  style={{ width: `${Math.round((stage.entered / maxEntered) * 100)}%` }}
                />
              </Link>
            ))}
          </div>
        )}

        {funnel.lost_reasons.length > 0 && (
          <div className="report-reasons">
            <div className="field-desc" style={{ marginBottom: 6 }}>{t("repLostReasons")}</div>
            {funnel.lost_reasons.map((reason: any) => (
              <div key={reason.reason} className="report-reason">
                <span className="truncate">{reason.reason}</span>
                <span className="report-reason-count">{reason.count}</span>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* --- выручка --- */}
      <div className="card card-pad report-card">
        <div className="section-head" style={{ marginBottom: 14 }}>
          <div className="metric-title">{t("repRevenue")}</div>
          <ExportLink name="revenue" query={query} label={t("exportCsv")} />
        </div>

        <div className="report-grid">
          <Figure
            title={t("repRevenue")}
            value={money(revenue.won_amount)}
            sub={t("repPriced", { n: revenue.won_priced })}
            money
          />
          {/* Средний чек без единой сделки с ценой — прочерк, а не ноль: ноль
              прочитают как «работаем даром». */}
          <Figure
            title={t("avgCheck")}
            value={revenue.avg_check === null ? "—" : money(revenue.avg_check)}
            sub={revenue.avg_check === null ? t("avgCheckNone") : t("avgCheckHint")}
            money
          />
          <Figure
            title={t("repLossValue")}
            value={money(revenue.lost_amount)}
            sub={`${t("repLost")}: ${revenue.lost_count}`}
            money
          />
          <Figure
            title={t("repConversion")}
            value={percent(revenue.conversion, locale)}
            sub={`${t("repWon")}: ${revenue.won_count}`}
          />
        </div>

        <div className="field-desc" style={{ marginBottom: 8 }}>{t("repMonths")}</div>
        <div className="month-bars">
          {revenue.months.map((month: any) => (
            <div className="month-col" key={month.month}>
              <div className="month-stack">
                {/* Две полосы рядом, а не одна поверх другой: выручка и потери
                    складываются в голове читателя, а не в графике. */}
                <div
                  className="month-bar won"
                  style={{ height: Math.max(2, Math.round(((month.won_amount ?? 0) / maxMonth) * 96)) }}
                  title={money(month.won_amount)}
                />
                <div
                  className="month-bar lost"
                  style={{ height: Math.max(2, Math.round(((month.lost_amount ?? 0) / maxMonth) * 96)) }}
                  title={money(month.lost_amount)}
                />
              </div>
              <span className="month-label">{month.month.slice(5)}.{month.month.slice(2, 4)}</span>
            </div>
          ))}
        </div>
      </div>

      {/* --- источники --- */}
      <div className="card card-pad report-card">
        <div className="section-head" style={{ marginBottom: 6 }}>
          <div className="metric-title">{t("repSources")}</div>
          <ExportLink name="sources" query={query} label={t("exportCsv")} />
        </div>
        <div className="field-desc" style={{ marginBottom: 12 }}>{t("repSourcesHint")}</div>

        {sources.clients_total === 0 && sources.revenue_total === 0 ? (
          <EmptyState title={t("repNoSources")} />
        ) : (
          <div className="src-table">
            <div className="src-row src-head">
              <span className="src-name">{t("repSources")}</span>
              <span className="src-num">{t("clients")}</span>
              <span className="src-num">{t("repWon")}</span>
              <span className="src-num">{t("repLost")}</span>
              <span className="src-money">{t("repRevenue")}</span>
              <span className="src-num">{t("repConversion")}</span>
            </div>
            {sources.items.map((row: any) => (
              <div className="src-row" key={row.source ?? "__none__"}>
                <span
                  className="src-bar"
                  style={{ width: `${Math.round(((row.revenue ?? 0) / maxSource) * 100)}%` }}
                />
                <span className="src-name truncate">{sourceLabel(row.source, t)}</span>
                <span className="src-num">{row.clients}</span>
                <span className="src-num">{row.won_count}</span>
                <span className="src-num">{row.lost_count}</span>
                {/* Прочерк, а не «0 $»: у выигранной сделки могли не назвать
                    цену, и ноль прочитался бы как «с этого источника не
                    заработали». Ноль остаётся там, где он настоящий. */}
                <span className="src-money">
                  {row.revenue === null ? "—" : money(row.revenue)}
                </span>
                <span className="src-num">{percent(row.conversion, locale)}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function Figure({
  title,
  value,
  sub,
  money,
}: {
  title: string;
  value: string;
  sub?: string;
  money?: boolean;
}) {
  return (
    <div className="report-figure">
      <div className="metric-title" style={{ marginBottom: 10 }}>{title}</div>
      <div className={"metric-value" + (money ? " money-value" : "")} style={{ fontSize: 22 }}>
        {value}
      </div>
      {sub && <div className="metric-sub">{sub}</div>}
    </div>
  );
}

/**
 * Выгрузка обычной ссылкой, а не запросом через fetch.
 *
 * Скачивание — работа браузера: он сам покажет прогресс, положит файл в
 * «Загрузки» и возьмёт имя из Content-Disposition. Тянуть файл в память и
 * собирать Blob значило бы делать всё это руками и хуже.
 */
function ExportLink({ name, query, label }: { name: string; query: string; label: string }) {
  return (
    <a className="section-link report-export" href={`/api/v1/reports/${name}.csv?${query}`}>
      <Icon name="download" size={13} />
      {label}
    </a>
  );
}
