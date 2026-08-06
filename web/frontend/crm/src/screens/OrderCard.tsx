import { useCallback, useEffect, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { Icon } from "../components/Icon";
import { Chip, ConfirmModal, ScreenLoading } from "../components/ui";
import { WarehousePicker, useWarehouses } from "../components/Warehouses";
import { api, ApiError } from "../lib/api";
import { useApp } from "../lib/app";
import { useFailure } from "../lib/failure";
import { formatMoney, formatQuantity } from "../lib/format";
import { ORDER_STATUS_LABEL, type Order } from "./Orders";

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
        <div>
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
              {line.picked_milli > 0 && line.picked_milli !== line.quantity_milli && (
                <span style={{ color: "var(--warning)", fontSize: 12, marginLeft: 6 }}>
                  {t("orderPicked", {
                    done: formatQuantity(line.picked_milli),
                    all: formatQuantity(line.quantity_milli),
                  })}
                </span>
              )}
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

function AddLine({ orderId, onAdded }: { orderId: number; onAdded: () => Promise<void> }) {
  const { t, toastError } = useApp();
  const [name, setName] = useState("");
  const [quantity, setQuantity] = useState("1");
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!name.trim() || busy) return;
    setBusy(true);
    try {
      // Разовая позиция без карточки товара: «доставка», «упаковка». Выбор из
      // справочника добавится отдельно — сначала должно работать то, без чего
      // заказ вообще нельзя набрать.
      await api.post(`/orders/${orderId}/lines`, { name: name.trim(), quantity: quantity.trim() });
      setName("");
      setQuantity("1");
      await onAdded();
    } catch (err) {
      toastError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="card card-pad" onSubmit={submit} style={{ display: "flex", gap: 12, flexWrap: "wrap", alignItems: "end" }}>
      <div className="field" style={{ marginBottom: 0, flex: "1 1 180px", minWidth: 0 }}>
        <label className="label">{t("orderLineName")}</label>
        <input className="input" value={name} onChange={(e) => setName(e.target.value)} />
      </div>
      <div className="field" style={{ marginBottom: 0, flex: "0 1 120px" }}>
        <label className="label">{t("quantity")}</label>
        <input className="input" value={quantity} onChange={(e) => setQuantity(e.target.value)} />
      </div>
      <button className="btn btn-primary" disabled={busy}>
        {t("orderAddLine")}
      </button>
    </form>
  );
}
