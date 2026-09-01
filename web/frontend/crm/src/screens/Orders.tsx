import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { ContextMenu, punktyDlyaZapisi, useContextMenu } from "../components/ContextMenu";
import { Icon } from "../components/Icon";
import { Chip, Dochitat, EmptyState, ScreenLoading } from "../components/ui";
import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { useDebounced } from "../lib/debounce";
import { useFailure } from "../lib/failure";
import { useGuard } from "../lib/guard";
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
  assembled: boolean;
  client_id: number | null;
  deal_id: number | null;
  lines: OrderLine[];
  total: number | null;
  created_at: string | null;
  /** Накладные, выписанные по этому заказу.
   *
   * Ключа НЕТ вовсе, когда блок накладных выключен, — не пустой массив, а
   * отсутствие: выключенный блок исчезает целиком, включая упоминания о
   * себе в чужих ответах. Пустой массив значит другое — «бумаг не
   * выписывалось», и так выглядят заказы, закрытые до переезда. */
  waybills?: { id: number; number: string; kind: string; status: string }[];
}

/** Подписи состояний — те же, что у бланка: состояния общие, и заводить им
 *  второй набор слов значит однажды назвать одно и то же по-разному. */
export const ORDER_STATUS_LABEL = {
  issued: "docIssued",
  ready: "docReady",
  closed: "docClosed",
  cancelled: "docCancelled",
} as const;

/** По скольку заказов дочитывается список. */
const NA_STRANITSE = 100;

export function Orders() {
  const { t, locale, workspace, toastError } = useApp();
  const navigate = useNavigate();
  const kontekst = useContextMenu();
  const [query, setQuery] = useState("");
  const [kind, setKind] = useState<string>("");
  const [data, setData] = useState<{ items: Order[]; total: number } | null>(null);
  // До какой страницы дочитан список. Прежде экран просил сотню заказов и на
  // этом заканчивался — а в подзаголовке честно писал «всего N». Сам сообщал,
  // что показывает часть, и ничего с этим сделать не давал.
  const [stranitsa, setStranitsa] = useState(1);
  const [dochityvaem, setDochityvaem] = useState(false);
  // Отбор, которому принадлежит показанный список. Ставится загрузкой,
  // сверяется дочиткой: пока вторая страница по «иван» едет, человек успевает
  // набрать «п», первая страница «п» заменяет список — и опоздавшая страница
  // дописывается к чужим находкам. На экране два отбора вперемешку, а «всего»
  // от прошлого.
  const otbor_spiska = useRef("");
  const [attempt, setAttempt] = useState(0);
  const guard = useGuard();
  const { failure, fail, clear } = useFailure();

  const search = useDebounced(query);

  // Отбор без номера страницы: положи страницу сюда — и смена отбора станет
  // неотличима от перехода на следующую. Загрузка зависит только от отбора и
  // всегда просит первую страницу, дочитка приписывает номер сама.
  const otbor = useMemo(() => {
    const params = new URLSearchParams({ per_page: String(NA_STRANITSE) });
    if (search) params.set("search", search);
    if (kind) params.set("kind", kind);
    return `/orders?${params}`;
  }, [search, kind]);

  useEffect(() => {
    // Вид заказа переключают быстрее, чем отвечает сервер: без счётчика ответ
    // по прошлому виду ложился поверх текущего, и на экране оказывался список
    // позапрошлого отбора. Приём тот же, что в отчётах и палитре команд.
    let current = true;
    otbor_spiska.current = otbor;
    clear();
    api
      .get<{ items: Order[]; total: number }>(`${otbor}&page=1`)
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

  /** Дочитать список.
   *
   * Отдельным действием, а не номером страницы в пути загрузки, и номер растёт
   * ПОСЛЕ удачного ответа. Иначе отказ на второй странице оставлял бы счётчик
   * на двойке, а следующее нажатие просило бы третью — вторая сотня заказов
   * пропадала бы из списка навсегда и молча.
   *
   * Отказ говорит о себе всплывающей жалобой, а не через `fail`: `fail` рисует
   * экран «не удалось загрузить», а он виден только пока показывать нечего.
   * После первой удачной загрузки отказ дочитки не показал бы ничего вовсе —
   * кнопка просто переставала бы отвечать.
   */
  const dochitat = async () => {
    if (dochityvaem) return;
    setDochityvaem(true);
    const sprosheno = otbor;
    try {
      const dalshe = await api.get<{ items: Order[]; total: number }>(
        `${otbor}&page=${stranitsa + 1}`,
      );
      // Отбор сменился, пока страница ехала, — ответ чужой.
      if (otbor_spiska.current !== sprosheno) return;
      setData((bylo) =>
        bylo ? { ...dalshe, items: [...bylo.items, ...dalshe.items] } : dalshe,
      );
      setStranitsa((bylo) => bylo + 1);
    } catch (e) {
      toastError(e);
    } finally {
      setDochityvaem(false);
    }
  };

  const create = async (which: string) => {
    // Заказ заводится пустым и сразу открывается: пока сервер отдаёт номер,
    // кнопка выглядит неотвеченной, и второе нажатие заводило второй заказ —
    // человек уходил в один, а второй оставался висеть без строк.
    if (!guard.take()) return;
    try {
      const order = await api.post<Order>("/orders", { kind: which });
      navigate(`/orders/${order.id}`);
    } catch (e) {
      // Отказ здесь был не пойман вовсе: нажатие просто ничего не делало.
      toastError(e);
      guard.free();
    }
  };

  if (!data) {
    return <ScreenLoading error={failure} onRetry={() => setAttempt((n) => n + 1)} />;
  }

  return (
    <div className="page">
      <ContextMenu menu={kontekst.menu} zakryt={kontekst.zakryt} />
      <div className="page-head">
        <div>
          <h1 className="page-title">{t("orders")}</h1>
          <div className="page-sub">{t("ordersSub", { total: data.total })}</div>
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          <button
            className="btn btn-primary"
            disabled={guard.busy}
            onClick={() => void create("sales_order")}
          >
            <Icon name="plus" stroke={2} />
            {t("newSalesOrder")}
          </button>
          <button
            className="btn btn-secondary"
            disabled={guard.busy}
            onClick={() => void create("purchase_order")}
          >
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
          <Link
            to={`/orders/${order.id}`}
            key={order.id}
            className="list-row hoverable"
            onContextMenu={(e) => kontekst.otkryt(e, punktyDlyaZapisi(`/orders/${order.id}`, t, navigate))}
          >
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
        {/* `data.total` — сколько заказов всего; денежный итог заказа лежит в
            `order.total`, и это разные числа с одинаковым именем. */}
        <Dochitat
          pokazano={data.items.length}
          vsego={data.total}
          zanyat={dochityvaem}
          onClick={() => void dochitat()}
        />
        {data.items.length === 0 && <EmptyState title={t("ordersEmpty")} />}
      </div>
    </div>
  );
}
