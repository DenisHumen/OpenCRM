import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { Icon } from "../components/Icon";
import { EmptyState, ScreenLoading } from "../components/ui";
import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { useLiveTopic } from "../lib/live";
import { useFailure } from "../lib/failure";
import { kindLabel, paperLink } from "../lib/documents";
import { formatMoney } from "../lib/format";
import { moduleOn } from "../lib/modules";
import { can } from "../lib/permissions";
import { sourceLabel } from "../lib/sources";
import { nazvanieEtapa } from "../lib/etapy";

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
  const { t, locale, user, modules } = useApp();
  // Возвраты — при включённых заказах и праве на них: статистика живёт в
  // разделе возвратов, здесь — три числа за тот же период и ход к ней.
  const vozvratyOn = moduleOn(modules, "orders") && can(user, "orders.view");
  // Долги — деньги: при включённых финансах и бланках, тем же правом, что суммы.
  const dolgiOn = moduleOn(modules, "finance") && moduleOn(modules, "documents") && can(user, "reports.view_amounts");
  const today = useMemo(() => new Date(), []);
  const quick = useMemo(() => presets(today), [today]);
  const [from, setFrom] = useState(quick[0].from);
  const [to, setTo] = useState(quick[0].to);
  const [data, setData] = useState<{ funnel: any; revenue: any; sources: any; vozvraty: any | null; dolgi: any | null } | null>(null);

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
  // Отчёт на экране устаревал, пока сосед закрывал заявки: перечитываем по
  // намёкам, как списки (план Ж-07).
  useLiveTopic(["deals", "finance", "orders", "clients"], () => setAttempt((n) => n + 1));

  useEffect(() => {
    let current = true;
    clear();
    Promise.all([
      api.get(`/reports/funnel?${query}`),
      api.get(`/reports/revenue?${query}`),
      api.get(`/reports/sources?${query}`),
      vozvratyOn ? api.get(`/returns/stats?${query}`) : Promise.resolve(null),
      dolgiOn ? api.get(`/reports/debts`) : Promise.resolve(null),
    ])
      .then(([funnel, revenue, sources, vozvraty, dolgi]) => {
        if (current) setData({ funnel, revenue, sources, vozvraty, dolgi });
      })
      .catch((e) => {
        if (current) fail(e);
      });
    return () => {
      current = false;
    };
  }, [query, attempt, vozvratyOn, dolgiOn, fail, clear]);

  if (!data) {
    return <ScreenLoading error={failure} onRetry={() => setAttempt((n) => n + 1)} />;
  }

  const { funnel, revenue, sources, vozvraty, dolgi } = data;
  const currency = revenue.currency ?? "USD";
  const money = (value: number | null) => formatMoney(value, currency, locale);

  // Ширина полосы — доля от самого населённого этапа, а не от общего числа: при
  // пяти этапах доли от целого дают пять одинаково коротких полос.
  const maxEntered = Math.max(1, ...funnel.stages.map((s: any) => s.entered));
  // `?? 0` не украшение: месяц, в котором ни у одной сделки не названа цена,
  // приходит с прочерком (null), а `Math.max(null, null)` дал бы 0 молча, зато
  // `null / maxMonth` ниже — высоту столбика NaN.
  // Касса берётся ПО МОДУЛЮ: месяц с возвратами уходит в минус (возврат клиенту
  // — доходная операция с отрицательной суммой), а суммы заявок отрицательными
  // не бывают. Без модуля такой месяц дал бы столбик в два пикселя вверх, то
  // есть «почти ноль» вместо «убыли», и убыль стала бы невидимой.
  const maxMonth = Math.max(
    1,
    ...revenue.months.map((m: any) =>
      Math.max(m.won_amount ?? 0, m.lost_amount ?? 0, Math.abs(m.received_amount ?? 0)),
    ),
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
          <EmptyState icon="analytics" title={t("repNothing")} />
        ) : (
          <>
          <div className="field-desc" style={{ marginBottom: 8 }}>{t("repStageEntered")}</div>
          <div className="funnel">
            {funnel.stages.map((stage: any) => (
              <Link
                key={stage.key}
                to={`/deals?stage=${encodeURIComponent(stage.key)}`}
                className={"funnel-step kind-" + stage.kind}
              >
                <span className="funnel-count">{stage.entered}</span>
                <span className="funnel-name">{nazvanieEtapa(t, stage.name)}</span>
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
          </>
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
          {/*
            Две плитки вместо одной, и это главное на экране.

            «Получено» — деньги, пришедшие в кассу; по решению владельца («банк
            один») это и есть выручка, и только так виден заказ, оплаченный мимо
            заявки. «Выиграно на сумму» — сумма выигранных заявок: величина
            полезная, но деньгами не являющаяся, заявку можно выиграть и не
            получить по ней ни копейки.

            Пока обе стояли под словом «Выручка», система показывала пустой
            месяц при полной кассе. Ось берётся из ответа (`basis`), а не из
            своей карты блоков: два ответа на один вопрос снова развели бы числа.
          */}
          {revenue.basis === "cash" ? (
            <Figure
              title={t("repMoneyIn")}
              value={money(revenue.received_amount)}
              sub={t("repMoneyInHint")}
              money
            />
          ) : (
            <Figure
              title={t("repWonValue")}
              value={money(revenue.won_amount)}
              sub={t("repRevenueByDeals")}
              money
            />
          )}
          {revenue.basis === "cash" && (
            <Figure
              title={t("repWonValue")}
              value={money(revenue.won_amount)}
              sub={t("repWonValueHint", { n: revenue.won_priced })}
              money
            />
          )}
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
            /* Не «конверсия»: у воронки под этим словом доля от ВСЕГО, что
               пришло, а здесь — доля выигранных среди ЗАКРЫТЫХ за период.
               Знаменатели разные, и одна подпись над двумя числами читалась
               как расхождение в отчёте. */
            title={t("repWinRate")}
            value={percent(revenue.conversion, locale)}
            sub={`${t("repWinRateHint")} · ${t("repWon")}: ${revenue.won_count}`}
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
                {/* Третья полоса — деньги. Рисуется по модулю и своим цветом:
                    месяц, в котором вернули больше, чем получили, обязан быть
                    виден как убыль, а не как «почти ноль». */}
                {month.received_amount !== null && month.received_amount !== undefined && (
                  <div
                    className={
                      "month-bar " + (month.received_amount < 0 ? "money-back" : "money-in")
                    }
                    style={{
                      height: Math.max(
                        2,
                        Math.round((Math.abs(month.received_amount) / maxMonth) * 96),
                      ),
                    }}
                    title={money(month.received_amount)}
                  />
                )}
              </div>
              <span className="month-label">{month.month.slice(5)}.{month.month.slice(2, 4)}</span>
            </div>
          ))}
        </div>
      </div>

      {/* --- возвраты --- */}
      {vozvraty && (
        <div className="card card-pad report-card">
          <div className="section-head" style={{ marginBottom: 14 }}>
            <div className="metric-title">{t("returnStats")}</div>
            <Link to="/returns" className="section-link">
              {t("viewAll")}
            </Link>
          </div>
          {vozvraty.count === 0 ? (
            <EmptyState icon="receipt" title={t("returnStatsEmpty")} />
          ) : (
            <div className="report-grid">
              <Figure title={t("returnStatsCount")} value={String(vozvraty.count)} sub={t("returnStatsShipped", { n: vozvraty.shipped_count })} />
              <Figure title={t("returnStatsRefund")} value={formatMoney(vozvraty.refund_amount, vozvraty.currency ?? currency, locale)} />
              <Figure title={t("returnStatsShare")} value={vozvraty.share === null ? "—" : `${Math.round(vozvraty.share)}%`} />
            </div>
          )}
        </div>
      )}

      {/* --- долги клиентов --- */}
      {dolgi && (
        <div className="card card-pad report-card">
          <div className="section-head" style={{ marginBottom: 6 }}>
            <div className="metric-title">{t("repDebts")}</div>
            <ExportLink name="debts" query={query} label={t("exportCsv")} />
          </div>
          <div className="field-desc" style={{ marginBottom: 12 }}>{t("repDebtsHint")}</div>
          {dolgi.items.length === 0 ? (
            <EmptyState icon="analytics" title={t("repNoDebts")} />
          ) : (
            <div className="list-card">
              {dolgi.items.map((row: any) => (
                <Link key={row.document_id} to={paperLink({ id: row.document_id, kind: row.kind })} className="list-row hoverable">
                  <span style={{ width: 110, color: "var(--faint)", fontSize: 12.5, fontFamily: "ui-monospace, monospace" }}>{row.number}</span>
                  <span style={{ width: 120, color: "var(--faint)", fontSize: 12.5 }}>{kindLabel(t, row.kind)}</span>
                  <span className="truncate" style={{ flex: 1, minWidth: 0, color: "var(--text)", fontSize: 13 }}>
                    {row.client_name ?? t("noClient")}
                  </span>
                  <span style={{ width: 120, textAlign: "right", color: "var(--faint)", fontSize: 12.5 }}>
                    {formatMoney(row.received, dolgi.currency ?? currency, locale)} / {formatMoney(row.total, dolgi.currency ?? currency, locale)}
                  </span>
                  <span style={{ width: 110, textAlign: "right", color: "var(--danger)", fontSize: 13, fontVariantNumeric: "tabular-nums" }}>
                    {formatMoney(row.due, dolgi.currency ?? currency, locale)}
                  </span>
                </Link>
              ))}
              <div className="itog-spiska">{t("repDebtsTotal", { sum: formatMoney(dolgi.total_due, dolgi.currency ?? currency, locale), n: dolgi.count })}</div>
            </div>
          )}
        </div>
      )}

      {/* --- источники --- */}
      <div className="card card-pad report-card">
        <div className="section-head" style={{ marginBottom: 6 }}>
          <div className="metric-title">{t("repSources")}</div>
          <ExportLink name="sources" query={query} label={t("exportCsv")} />
        </div>
        <div className="field-desc" style={{ marginBottom: 12 }}>{t("repSourcesHint")}</div>

        {sources.clients_total === 0 && sources.revenue_total === 0 ? (
          <EmptyState icon="analytics" title={t("repNoSources")} />
        ) : (
          <div className="src-table">
            <div className="src-row src-head">
              <span className="src-name">{t("repSources")}</span>
              <span className="src-num">{t("clients")}</span>
              <span className="src-num">{t("repWon")}</span>
              <span className="src-num">{t("repLost")}</span>
              {/* «Выиграно на сумму», а не «Выручка»: колонка считается по
                  заявкам, и слово «выручка» на экране означает деньги. */}
              <span className="src-money">{t("repWonValue")}</span>
              <span className="src-num">{t("repWinRate")}</span>
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
