import { useCallback, useEffect, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { Icon } from "../components/Icon";
import { useLabelsOn } from "../components/ProductBarcodes";
import { Chip, ConfirmModal, ScreenLoading } from "../components/ui";
import { WarehousePicker, useWarehouses } from "../components/Warehouses";
import { api, ApiError } from "../lib/api";
import { useApp } from "../lib/app";
import { useDebounced } from "../lib/debounce";
import { useFailure } from "../lib/failure";
import { formatMoney, formatQuantity } from "../lib/format";
import { ORDER_STATUS_LABEL, type Order } from "./Orders";
import type { Product } from "./Warehouse";

/** Карточка заказа: позиции, сборка сканером, проведение.
 *
 * Проведение — единственное место, где заказ трогает склад. До него он только
 * держит обещание (резерв), после — оно снято, а товар списан. Поэтому кнопка
 * называется действием («Отгрузить», «Принять»), а не состоянием.
 */
export function OrderCard() {
  const { id } = useParams();
  const { t, locale, workspace, toast, toastError } = useApp();
  const navigate = useNavigate();
  const [order, setOrder] = useState<Order | null>(null);
  const [busy, setBusy] = useState(false);
  const [shortage, setShortage] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<"cancel" | "revert" | null>(null);
  const places = useWarehouses();
  const [place, setPlace] = useState<number | null>(null);
  const { failure, fail, clear } = useFailure();

  const load = useCallback(async () => {
    clear();
    try {
      setOrder(await api.get<Order>(`/orders/${id}`));
    } catch (e) {
      if (e instanceof ApiError && (e.status === 404 || e.status === 403)) {
        toastError(e);
        navigate("/orders");
        return;
      }
      fail(e);
    }
  }, [id, toastError, navigate, fail, clear]);

  useEffect(() => {
    void load();
  }, [load]);

  if (!order) return <ScreenLoading error={failure} onRetry={() => void load()} />;

  const outgoing = order.kind === "sales_order";
  const open = order.status === "issued" || order.status === "ready";

  const close = async (force: boolean) => {
    setBusy(true);
    try {
      await api.post(`/orders/${order.id}/close`, {
        warehouse_id: place,
        confirm_negative: force,
      });
      setShortage(null);
      toast(outgoing ? t("orderShip") : t("orderReceive"));
      await load();
    } catch (err) {
      if (err instanceof ApiError && err.code === "not_enough_stock") {
        // Не отказ насовсем: показываем, чего именно не хватает, и даём
        // подтвердить. Список позиций приходит с сервера — иначе человеку
        // пришлось бы сверять заказ со складом построчно руками.
        setShortage(err.message);
      } else {
        toastError(err);
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="page">
      <Link
        to="/orders"
        style={{ display: "inline-flex", alignItems: "center", gap: 6, color: "var(--muted)", fontSize: 13, marginBottom: 20 }}
      >
        <Icon name="arrowLeft" size={14} />
        {t("orders")}
      </Link>

      <div className="page-head" style={{ alignItems: "flex-start", marginBottom: 20 }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <h1 className="page-title" style={{ fontSize: 22 }}>
            {order.number}
          </h1>
          <div className="page-sub" style={{ marginTop: 5, display: "flex", gap: 8, alignItems: "center" }}>
            <Chip>{outgoing ? t("orderKindSales") : t("orderKindPurchase")}</Chip>
            <Chip variant={order.status === "closed" ? "success" : undefined}>
              {t(ORDER_STATUS_LABEL[order.status as keyof typeof ORDER_STATUS_LABEL] ?? "docIssued")}
            </Chip>
          </div>
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          {order.status === "issued" && order.lines.length > 0 && (
            <button
              className="btn btn-secondary btn-sm"
              onClick={async () => {
                try {
                  await api.post(`/orders/${order.id}/ready`);
                  await load();
                } catch (err) {
                  toastError(err);
                }
              }}
            >
              {t("orderReady")}
            </button>
          )}
          {/* Печать — обычная ссылка в новую вкладку: это window.print() на
              настоящей странице со своим @page, и выборкой её не получить. */}
          <a
            className="btn btn-secondary btn-sm"
            href={`/api/v1/orders/${order.id}/print`}
            target="_blank"
            rel="noreferrer"
          >
            <Icon name="printer" size={14} />
            {t("orderPrint")}
          </a>
        </div>
      </div>

      <div className="card" style={{ overflow: "hidden", marginBottom: 16 }}>
        <div className="list-header">
          <span style={{ flex: 1 }}>{t("orderLineName")}</span>
          <span style={{ width: 130, textAlign: "right" }}>{t("quantity")}</span>
          <span style={{ width: 110, textAlign: "right" }}>{t("sellPrice")}</span>
          {open && <span style={{ width: 34 }} />}
        </div>
        {order.lines.map((line) => (
          <div className="list-row" key={line.id}>
            <span style={{ flex: 1, minWidth: 0, color: "var(--text)", fontSize: 13.5 }}>
              {line.name}
            </span>
            <span style={{ width: 130, textAlign: "right", fontSize: 13 }}>
              {formatQuantity(line.quantity_milli)}
              {/* Собранное показываем только когда оно есть и не сходится:
                  «собрано 3 из 3» на каждой строке — шум, прячущий те строки,
                  где расхождение настоящее. */}
              {line.picked_milli > 0 &&
                (line.picked_milli === line.quantity_milli ? (
                  // Сошлось — короткая пометка вместо «собрано 3 из 3»: на
                  // каждой строке она была бы шумом, прячущим те строки, где
                  // расхождение настоящее.
                  <span style={{ color: "var(--success)", fontSize: 12, marginLeft: 6 }}>
                    ✓ {t("orderPickedAll")}
                  </span>
                ) : (
                  <span style={{ color: "var(--warning)", fontSize: 12, marginLeft: 6 }}>
                    {t("orderPicked", {
                      done: formatQuantity(line.picked_milli),
                      all: formatQuantity(line.quantity_milli),
                    })}
                  </span>
                ))}
            </span>
            <span style={{ width: 110, textAlign: "right", color: "var(--muted)", fontSize: 12.5 }}>
              {formatMoney(line.price, workspace.currency, locale)}
            </span>
            {open && (
              <button
                className="btn-icon"
                title={t("delete")}
                onClick={async () => {
                  try {
                    await api.del(`/orders/${order.id}/lines/${line.id}`);
                    await load();
                  } catch (err) {
                    toastError(err);
                  }
                }}
              >
                <Icon name="trash" size={14} />
              </button>
            )}
          </div>
        ))}
        <div className="list-row" style={{ background: "var(--surface-2)" }}>
          <span style={{ flex: 1, color: "var(--muted)", fontSize: 12.5 }}>{t("orderTotal")}</span>
          <span style={{ color: "var(--text)", fontSize: 14, fontWeight: 600 }}>
            {formatMoney(order.total, workspace.currency, locale)}
          </span>
        </div>
      </div>

      {open && <AddLine orderId={order.id} onAdded={load} />}
      {open && <PickScanner orderId={order.id} onPicked={load} />}

      {open && (
        <div className="card card-pad" style={{ marginTop: 16, display: "flex", gap: 10, flexWrap: "wrap", alignItems: "center" }}>
          {/* Склад выбирается явно: молчаливое списание с основного однажды
              снимет деталь не оттуда, где её взяли. */}
          <WarehousePicker places={places} value={place ?? places?.items[0]?.id ?? null} onChange={setPlace} />
          <button
            className="btn btn-primary"
            disabled={busy || order.lines.length === 0}
            onClick={() => void close(false)}
          >
            {outgoing ? t("orderShip") : t("orderReceive")}
          </button>
          <button className="btn btn-secondary" disabled={busy} onClick={() => setConfirm("cancel")}>
            {t("orderCancel")}
          </button>
          {shortage && (
            <div style={{ flexBasis: "100%" }}>
              <div className="field-desc" style={{ color: "var(--warning)" }}>{shortage}</div>
              <button className="btn btn-secondary btn-sm" disabled={busy} onClick={() => void close(true)}>
                {t("orderShipForce")}
              </button>
            </div>
          )}
        </div>
      )}

      {order.status === "closed" && (
        <button className="btn btn-secondary" onClick={() => setConfirm("revert")}>
          {t("orderRevert")}
        </button>
      )}

      {confirm && (
        <ConfirmModal
          text={confirm === "cancel" ? t("orderCancelConfirm") : t("orderRevertConfirm")}
          confirmLabel={confirm === "cancel" ? t("orderCancel") : t("orderRevert")}
          danger
          onConfirm={async () => {
            try {
              await api.post(`/orders/${order.id}/${confirm === "cancel" ? "cancel" : "revert"}`);
              await load();
            } catch (err) {
              toastError(err);
            }
          }}
          onClose={() => setConfirm(null)}
        />
      )}
    </div>
  );
}

/** Добавление позиции: из справочника или разовой строкой.
 *
 * Два способа рядом, а не два экрана. **Из справочника** — для товара: только
 * такая строка попадает в резерв и списывается при отгрузке. **Разовая** — для
 * «доставки» и «упаковки», которых в справочнике нет и заводить их туда значит
 * замусорить его одноразовыми записями.
 */
function AddLine({ orderId, onAdded }: { orderId: number; onAdded: () => Promise<void> }) {
  const { t, toastError } = useApp();
  const [fromCatalogue, setFromCatalogue] = useState(true);
  const [name, setName] = useState("");
  const [picked, setPicked] = useState<Product | null>(null);
  const [quantity, setQuantity] = useState("1");
  const [busy, setBusy] = useState(false);
  const [found, setFound] = useState<Product[]>([]);

  const search = useDebounced(name);
  useEffect(() => {
    if (!fromCatalogue || !search.trim() || picked) {
      setFound([]);
      return;
    }
    let alive = true;
    api
      .get<{ items: Product[] }>(
        `/warehouse/products?search=${encodeURIComponent(search)}&per_page=8`,
      )
      .then((data) => {
        if (alive) setFound(data.items);
      })
      // Склад может быть выключен — тогда справочника просто нет, и остаётся
      // разовая позиция. Это не ошибка человека, и говорить о ней нечего.
      .catch(() => {
        if (alive) setFound([]);
      });
    return () => {
      alive = false;
    };
  }, [search, fromCatalogue, picked]);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (busy) return;
    if (fromCatalogue ? !picked : !name.trim()) return;
    setBusy(true);
    try {
      await api.post(`/orders/${orderId}/lines`, {
        product_id: picked?.id ?? null,
        name: picked ? null : name.trim(),
        quantity: quantity.trim(),
      });
      setName("");
      setPicked(null);
      setQuantity("1");
      await onAdded();
    } catch (err) {
      toastError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="card card-pad" onSubmit={submit} style={{ marginTop: 12 }}>
      <div style={{ display: "flex", gap: 6, marginBottom: 12 }}>
        {([[true, t("orderLinePick")], [false, t("orderLineOnce")]] as const).map(
          ([value, label]) => (
            <button
              key={String(value)}
              type="button"
              className={"option-chip" + (fromCatalogue === value ? " active" : "")}
              onClick={() => {
                setFromCatalogue(value);
                setPicked(null);
                setName("");
              }}
            >
              {label}
            </button>
          ),
        )}
      </div>
      <div style={{ display: "flex", gap: 12, flexWrap: "wrap", alignItems: "end" }}>
        <div
          className="field"
          style={{ marginBottom: 0, flex: "1 1 180px", minWidth: 0, position: "relative" }}
        >
          <label className="label">{fromCatalogue ? t("orderLinePick") : t("orderLineName")}</label>
          <input
            className="input"
            value={name}
            onChange={(e) => {
              setName(e.target.value);
              // Начали править — выбор сброшен: иначе в заказ уехал бы товар,
              // которого человек уже не видит в поле.
              setPicked(null);
            }}
          />
          {found.length > 0 && (
            <div
              className="card"
              style={{ position: "absolute", top: "100%", left: 0, right: 0, zIndex: 20, marginTop: 4, overflow: "hidden" }}
            >
              {found.map((item) => (
                <button
                  key={item.id}
                  type="button"
                  className="list-row hoverable"
                  style={{ width: "100%", textAlign: "left", background: "none", border: "none", cursor: "pointer" }}
                  onClick={() => {
                    setPicked(item);
                    setName(item.name);
                    setFound([]);
                  }}
                >
                  <span style={{ flex: 1, color: "var(--text)", fontSize: 13 }}>{item.name}</span>
                  <span style={{ color: "var(--faint)", fontSize: 12 }}>{item.sku ?? ""}</span>
                </button>
              ))}
            </div>
          )}
        </div>
        <div className="field" style={{ marginBottom: 0, flex: "0 1 120px" }}>
          <label className="label">{t("quantity")}</label>
          <input className="input" value={quantity} onChange={(e) => setQuantity(e.target.value)} />
        </div>
        <button className="btn btn-primary" disabled={busy}>
          {t("orderAddLine")}
        </button>
      </div>
    </form>
  );
}

/** Поле сборки сканером.
 *
 * Появляется вместе с блоком наклеек: без штрихкодов сканировать нечего, и
 * пустое поле означало бы обещание, которого система не выполнит. Собранное
 * растёт по строке заказа, а не по остатку: склад двигается только отгрузкой.
 */
function PickScanner({ orderId, onPicked }: { orderId: number; onPicked: () => Promise<void> }) {
  const { t, toast, toastError } = useApp();
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const scanning = useLabelsOn();

  const lookup = async () => {
    const scanned = code.trim();
    if (!scanned || busy) return;
    setBusy(true);
    try {
      const line = await api.post<{ name: string }>(`/orders/${orderId}/pick`, { code: scanned });
      setCode("");
      toast(line.name);
      await onPicked();
    } catch (err) {
      // Отказ называет товар: «этого нет в заказе» с именем читается, а пустой
      // ответ после писка сканера — как «сканер сломался».
      toastError(err);
    } finally {
      setBusy(false);
    }
  };

  if (!scanning) return null;

  return (
    <form
      className="card card-pad"
      style={{ marginTop: 12, display: "flex", alignItems: "center", gap: 10 }}
      title={t("orderScanHint")}
      onSubmit={(e) => {
        e.preventDefault();
        void lookup();
      }}
    >
      <Icon name="scan" size={16} className="" />
      <input
        className="input"
        style={{ flex: 1, minWidth: 0, fontFamily: "ui-monospace, monospace" }}
        placeholder={t("orderScan")}
        value={code}
        onChange={(e) => setCode(e.target.value)}
        // Enter ловим сами: форма без кнопки отправки не порождает submit —
        // проверено на живой странице, — а сканер заканчивает ввод именно им.
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            void lookup();
          }
        }}
        autoComplete="off"
      />
    </form>
  );
}
