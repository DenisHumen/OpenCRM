import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { ContextMenu, punktyDlyaZapisi, useContextMenu } from "../components/ContextMenu";
import { Icon } from "../components/Icon";
import { SpisokPoKategoriyam } from "../components/SpisokPoKategoriyam";
import { Chip, Dochitat, ItogSpiska, LoadFailed, ScreenLoading } from "../components/ui";
import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { DOC_SORTS, sortLabel, statusLabel, statusVariant } from "../lib/documents";
import { useLiveTopic } from "../lib/live";
import { useDebounced } from "../lib/debounce";
import { useFailure } from "../lib/failure";
import { formatMoney, formatQuantity } from "../lib/format";
import type { HistoryEvent } from "../components/History";
import type { OrderLine } from "./Orders";

/** Возвраты покупателей: список и своя статистика.
 *
 * Экран отдельный от заказов, хотя бумага — вид бланка и живёт в их блоке:
 * у заказа спрашивают «когда отдадим», у возврата — «что и почему вернули».
 * Статистика возвратов — сверху, а не в отчётах: её смотрят вместе со
 * списком, когда разбирают, что пошло не так.
 */

export interface Return {
  id: number;
  number: string;
  kind: "return";
  status: string;
  client_id: number | null;
  client_name: string | null;
  deal_id: number | null;
  order_id: number | null;
  order_number: string | null;
  warehouse_id: number | null;
  note: string;
  category_id: number | null;
  /** Сколько вернули клиенту. Пусто — нет права на суммы. */
  refund: number | null;
  /** Считает сервер: «черновик правится, проведённый нет» живёт в службе. */
  pravitsya: boolean;
  lines: OrderLine[];
  total: number | null;
  created_at: string | null;
  files?: ReturnFile[];
  order_lines?: { product_id: number; name: string; price: number | null; max_milli: number }[];
  waybills?: { id: number; number: string; kind: string; status: string }[];
  events?: HistoryEvent[];
}

export interface ReturnFile {
  id: number;
  document_id: number;
  original_name: string;
  mime: string;
  size_bytes: number;
  created_at: string | null;
  download_url: string;
}

export const RETURN_STATUSES = ["draft", "closed", "cancelled"] as const;

/** По скольку возвратов дочитывается список. */
const NA_STRANITSE = 100;

export function Returns() {
  const { t, locale, workspace, toastError } = useApp();
  const navigate = useNavigate();
  const kontekst = useContextMenu();
  const [query, setQuery] = useState("");
  const [data, setData] = useState<{ items: Return[]; total: number; counts?: Record<string, number> } | null>(null);
  const [stranitsa, setStranitsa] = useState(1);
  const [dochityvaem, setDochityvaem] = useState(false);
  const otbor_spiska = useRef("");
  const { failure, fail, clear } = useFailure();
  const [attempt, setAttempt] = useState(0);
  const [poryadok, setPoryadok] = useState("new");
  // Возврат живёт в теме заказов: проведённый соседом — перечитать.
  useLiveTopic("orders", () => setAttempt((a) => a + 1));

  const search = useDebounced(query);

  const otbor = useMemo(() => {
    const params = new URLSearchParams({ per_page: String(NA_STRANITSE) });
    if (search) params.set("search", search);
    if (poryadok !== "new") params.set("sort", poryadok);
    return `/returns?${params}`;
  }, [search, poryadok]);

  useEffect(() => {
    let current = true;
    otbor_spiska.current = otbor;
    clear();
    api
      .get<{ items: Return[]; total: number; counts?: Record<string, number> }>(`${otbor}&page=1`)
      .then((found) => {
        if (!current) return;
        setData(found);
        setStranitsa(1);
      })
      .catch((e) => {
        if (current) fail(e);
      });
    return () => {
      current = false;
    };
  }, [otbor, attempt, fail, clear]);

  const dochitat = async () => {
    if (dochityvaem) return;
    setDochityvaem(true);
    const sprosheno = otbor;
    try {
      const dalshe = await api.get<{ items: Return[]; total: number }>(`${otbor}&page=${stranitsa + 1}`);
      if (otbor_spiska.current !== sprosheno) return;
      setData((bylo) => (bylo ? { ...bylo, ...dalshe, items: [...bylo.items, ...dalshe.items] } : dalshe));
      setStranitsa((bylo) => bylo + 1);
    } catch (e) {
      toastError(e);
    } finally {
      setDochityvaem(false);
    }
  };

  if (!data) {
    return <ScreenLoading error={failure} onRetry={() => setAttempt((n) => n + 1)} />;
  }

  return (
    <div className="page page-wide">
      <ContextMenu menu={kontekst.menu} zakryt={kontekst.zakryt} />
      <div className="page-head">
        <div>
          <h1 className="page-title">{t("returns")}</h1>
          <div className="page-sub">{t("returnsSub", { total: data.total })}</div>
        </div>
      </div>

      <ReturnStats attempt={attempt} />

      <div style={{ display: "flex", gap: 10, marginBottom: 16, flexWrap: "wrap" }}>
        <div
          style={{
            flex: "2 1 180px", minWidth: 0, display: "flex", alignItems: "center", gap: 8,
            padding: "0 12px", height: 36, border: "1px solid var(--border)",
            borderRadius: 8, background: "var(--surface)",
          }}
        >
          <Icon name="search" size={15} className="" />
          <input
            style={{ flex: 1, minWidth: 0, background: "none", border: "none", outline: "none", color: "var(--text)", fontSize: 13.5, fontFamily: "var(--sans)" }}
            placeholder={t("search")}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            autoFocus
          />
        </div>
        <select
          className="input sort-select"
          value={poryadok}
          onChange={(e) => setPoryadok(e.target.value)}
          aria-label={t("sortLabel")}
        >
          {DOC_SORTS.map((s) => (
            <option key={s} value={s}>
              {sortLabel(t, s)}
            </option>
          ))}
        </select>
      </div>

      <div className="list-card">
        <SpisokPoKategoriyam
          pamyat="returns:status"
          kategorii={RETURN_STATUSES.map((key) => ({ key, label: statusLabel(t, key, "return") }))}
          stroki={data.items}
          kategoriyaStroki={(v) => v.status}
          vsego={data.counts}
          klyuchStroki={(v) => v.id}
          render={(v) => (
            <Link
              to={`/returns/${v.id}`}
              className="list-row hoverable"
              onContextMenu={(e) => kontekst.otkryt(e, punktyDlyaZapisi(`/returns/${v.id}`, t, navigate))}
            >
              <span style={{ width: 110, color: "var(--faint)", fontSize: 12.5, fontFamily: "ui-monospace, monospace" }}>
                {v.number}
              </span>
              <span style={{ width: 120, color: "var(--muted)", fontSize: 12.5 }}>
                {v.order_number ? `${t("returnOrder")} ${v.order_number}` : ""}
              </span>
              <span className="truncate" style={{ width: 150, color: "var(--muted)", fontSize: 12.5 }}>
                {v.client_name ?? ""}
              </span>
              <span className="truncate" style={{ flex: 1, minWidth: 0, color: "var(--muted)", fontSize: 12.5 }}>
                {v.lines.length
                  ? v.lines.map((line) => `${line.name} × ${formatQuantity(line.quantity_milli)}`).join(" · ")
                  : t("orderLines")}
              </span>
              <span style={{ width: 120, textAlign: "right", color: "var(--text)", fontSize: 13 }}>
                {formatMoney(v.refund, workspace.currency, locale)}
              </span>
              <span style={{ width: 130, textAlign: "right" }}>
                <Chip variant={statusVariant(v.status, "return")}>{statusLabel(t, v.status, "return")}</Chip>
              </span>
            </Link>
          )}
        />
        {data.items.length === 0 && (
          <div className="list-row" style={{ color: "var(--faint)" }}>{t("noReturns")}</div>
        )}
        <ItogSpiska pokazano={data.items.length} vsego={data.total} summa={data.items.reduce((s, v) => s + (v.refund ?? 0), 0)} currency={workspace.currency} />
        <Dochitat pokazano={data.items.length} vsego={data.total} zanyat={dochityvaem} onClick={() => void dochitat()} />
      </div>
    </div>
  );
}

interface Svodka {
  from: string;
  to: string;
  currency: string;
  count: number;
  refund_amount: number | null;
  avg_refund: number | null;
  shipped_count: number;
  share: number | null;
  months: { month: string; count: number; refund_amount: number | null; shipped_count: number }[];
  products: { product_id: number; name: string; quantity_milli: number; returns: number }[];
}

const PERIODY = [30, 90, 365] as const;

function periodOt(dney: number): { from: string; to: string } {
  const to = new Date();
  const from = new Date(to.getTime() - dney * 86_400_000);
  return { from: from.toISOString().slice(0, 10), to: to.toISOString().slice(0, 10) };
}

/** Статистика возвратов: числа, по месяцам и что возвращают чаще. Своя, а не
 *  в отчётах: возврат — новый вид действия, и его считают отдельно от выручки. */
function ReturnStats({ attempt }: { attempt: number }) {
  const { t, locale } = useApp();
  const [dney, setDney] = useState<number>(30);
  const [svodka, setSvodka] = useState<Svodka | null>(null);
  const { failure, fail, clear } = useFailure();

  useEffect(() => {
    let alive = true;
    clear();
    const { from, to } = periodOt(dney);
    const params = new URLSearchParams({ from, to, tz_offset: String(new Date().getTimezoneOffset()) });
    api
      .get<Svodka>(`/returns/stats?${params}`)
      .then((s) => {
        if (alive) setSvodka(s);
      })
      .catch((e) => {
        if (alive) fail(e);
      });
    return () => {
      alive = false;
    };
  }, [dney, attempt, fail, clear]);

  const sum = (value: number | null) => formatMoney(value, svodka?.currency ?? "USD", locale);
  const maxMes = Math.max(1, ...(svodka?.months ?? []).map((m) => m.count));
  const maxTovar = Math.max(1, ...(svodka?.products ?? []).map((p) => p.quantity_milli));
  const podpisPerioda = (n: number) =>
    n === 30 ? t("returnsPeriod30") : n === 90 ? t("returnsPeriod90") : t("returnsPeriod365");

  return (
    <div className="card" style={{ marginBottom: 16 }}>
      <div className="section-head" style={{ padding: "14px 16px 0", marginBottom: 0 }}>
        <div className="metric-title">{t("returnStats")}</div>
        <div style={{ display: "flex", gap: 6 }}>
          {PERIODY.map((n) => (
            <button
              key={n}
              type="button"
              className={"option-chip" + (dney === n ? " active" : "")}
              onClick={() => setDney(n)}
            >
              {podpisPerioda(n)}
            </button>
          ))}
        </div>
      </div>
      {failure !== null ? (
        <div className="stat-blok">
          <LoadFailed error={failure} onRetry={() => setDney((n) => n)} />
        </div>
      ) : svodka === null ? (
        <div className="stat-blok stat-tikho">{t("loading")}</div>
      ) : (
        <div className="stat-blok" style={{ borderBottom: "none" }}>
          <div className="metric-grid stat-plitki">
            <Plitka title={t("returnStatsCount")} value={String(svodka.count)} sub={t("returnStatsShipped", { n: svodka.shipped_count })} />
            <Plitka title={t("returnStatsRefund")} value={sum(svodka.refund_amount)} />
            <Plitka title={t("returnStatsShare")} value={svodka.share === null ? "—" : `${Math.round(svodka.share)}%`} />
            <Plitka title={t("returnStatsAvg")} value={sum(svodka.avg_refund)} />
          </div>
          {svodka.count === 0 ? (
            <div className="stat-tikho">{t("returnStatsEmpty")}</div>
          ) : (
            <>
              <div className="stat-ryad">
                <div className="metric-title">{t("returnStatsByMonth")}</div>
                <div className="bars stat-bars">
                  {svodka.months.map((m) => (
                    <div className="bar-col" key={m.month}>
                      <div
                        className={"bar stat-bar" + (m.count === maxMes && m.count > 0 ? " top" : "")}
                        style={{ height: Math.max(3, Math.round((m.count / maxMes) * 52)) }}
                        title={`${m.month}: ${m.count} · ${sum(m.refund_amount)}`}
                      />
                      <span className="bar-label">{m.month.slice(5)}</span>
                    </div>
                  ))}
                </div>
              </div>
              <div className="stat-ryad">
                <div className="metric-title">{t("returnStatsProducts")}</div>
                <div className="src-table">
                  {svodka.products.map((p) => (
                    <div key={p.product_id} className="src-row">
                      <div className="src-bar" style={{ width: `${Math.round((p.quantity_milli / maxTovar) * 100)}%` }} />
                      <span className="src-name">
                        <Link to={`/warehouse/${p.product_id}`}>{p.name}</Link>
                      </span>
                      <span className="src-num">{formatQuantity(p.quantity_milli)} · {p.returns}</span>
                    </div>
                  ))}
                </div>
              </div>
            </>
          )}
        </div>
      )}
    </div>
  );
}

function Plitka({ title, value, sub }: { title: string; value: string; sub?: string }) {
  return (
    <div className="card card-pad">
      <div className="metric-title">{title}</div>
      <div className="metric-value">{value}</div>
      {sub && <div className="metric-sub">{sub}</div>}
    </div>
  );
}
