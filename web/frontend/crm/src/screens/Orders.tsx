import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { Icon } from "../components/Icon";
import { Chip, EmptyState, ScreenLoading } from "../components/ui";
import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { useDebounced } from "../lib/debounce";
import { useFailure } from "../lib/failure";
import { formatMoney } from "../lib/format";

/** Список заказов: покупателей и поставщикам.
 *
 * Экран отдельный от бланков, хотя таблица у них одна: список квитанций, куда
 * затесались заказы, отвечает не на тот вопрос, с которым туда пришли. Отбор
 * по виду делает сервер.
 */

export interface OrderLine {
  id: number;
  product_id: number | null;
  /** Снимок на момент добавления: товар переименуют, заказ не поедет. */
  name: string;
  quantity_milli: number;
  picked_milli: number;
  price: number | null;
  cost: number | null;
}

export interface Order {
  id: number;
  number: string;
  kind: "sales_order" | "purchase_order";
  status: string;
  client_id: number | null;
  deal_id: number | null;
  lines: OrderLine[];
  total: number | null;
  created_at: string | null;
}

/** Подписи состояний — те же, что у бланка: состояния общие, и заводить им
 *  второй набор слов значит однажды назвать одно и то же по-разному. */
export const ORDER_STATUS_LABEL = {
  issued: "docIssued",
  ready: "docReady",
  closed: "docClosed",
  cancelled: "docCancelled",
} as const;

export function Orders() {
  const { t, locale, workspace } = useApp();
  const navigate = useNavigate();
  const [query, setQuery] = useState("");
  const [kind, setKind] = useState<string>("");
  const [data, setData] = useState<{ items: Order[]; total: number } | null>(null);
  const { failure, fail, clear } = useFailure();

  const load = useCallback(
    (q: string, which: string) => {
      clear();
      const params = new URLSearchParams({ per_page: "100" });
      if (q) params.set("search", q);
      if (which) params.set("kind", which);
      api
        .get(`/orders?${params}`)
        .then(setData)
        .catch(fail);
    },
    [fail, clear],
  );

  const search = useDebounced(query);
  useEffect(() => {
    load(search, kind);
  }, [search, kind, load]);

  const create = async (which: string) => {
    const order = await api.post<Order>("/orders", { kind: which });
    navigate(`/orders/${order.id}`);
  };

  if (!data) return <ScreenLoading error={failure} onRetry={() => load(query, kind)} />;

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1 className="page-title">{t("orders")}</h1>
          <div className="page-sub">{t("ordersSub", { total: data.total })}</div>
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          <button className="btn btn-primary" onClick={() => void create("sales_order")}>
            <Icon name="plus" stroke={2} />
            {t("newSalesOrder")}
          </button>
          <button className="btn btn-secondary" onClick={() => void create("purchase_order")}>
            {t("newPurchaseOrder")}
          </button>
        </div>
      </div>

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
        {/* Два вида заказа не смешиваются: у них разные вопросы — «когда
            отдадим» против «когда привезут». */}
        {[
          ["", t("viewAll")],
          ["sales_order", t("orderKindSales")],
          ["purchase_order", t("orderKindPurchase")],
        ].map(([value, label]) => (
          <button
            key={value || "all"}
            className={"filter-chip" + (kind === value ? " active" : "")}
            onClick={() => setKind(value)}
          >
            {label}
          </button>
        ))}
      </div>

      <div className="list-card">
        {data.items.map((order) => (
          <Link to={`/orders/${order.id}`} key={order.id} className="list-row hoverable">
            <span style={{ width: 110, color: "var(--faint)", fontSize: 12.5, fontFamily: "ui-monospace, monospace" }}>
              {order.number}
            </span>
            <span style={{ width: 110 }}>
              <Chip>{order.kind === "sales_order" ? t("orderKindSales") : t("orderKindPurchase")}</Chip>
            </span>
            <span style={{ flex: 1, minWidth: 0, color: "var(--muted)", fontSize: 12.5 }}>
              {order.lines.length
                ? order.lines.map((line) => line.name).join(" · ")
                : t("orderLines")}
            </span>
            <span style={{ width: 120, textAlign: "right", color: "var(--text)", fontSize: 13 }}>
              {formatMoney(order.total, workspace.currency, locale)}
            </span>
            <span style={{ width: 110, textAlign: "right" }}>
              <Chip variant={order.status === "closed" ? "success" : undefined}>
                {t(ORDER_STATUS_LABEL[order.status as keyof typeof ORDER_STATUS_LABEL] ?? "docIssued")}
              </Chip>
            </span>
          </Link>
        ))}
        {data.items.length === 0 && <EmptyState title={t("ordersEmpty")} />}
      </div>
    </div>
  );
}
