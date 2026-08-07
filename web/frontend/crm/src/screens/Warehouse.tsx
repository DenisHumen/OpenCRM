import { useEffect, useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";

import { Icon } from "../components/Icon";
import { BarcodeScanner, useLabelsOn } from "../components/ProductBarcodes";
import { WarehousePicker, useWarehouses } from "../components/Warehouses";
import { Chip, EmptyState, Modal, ScreenLoading } from "../components/ui";
import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { useDebounced } from "../lib/debounce";
import { useFailure } from "../lib/failure";
import { useGuard } from "../lib/guard";
import { formatMoney, formatQuantity } from "../lib/format";

export const UNITS = ["pcs", "kg", "g", "l", "ml", "m", "m2", "pack", "hour"] as const;

type UnitKey =
  | "unitPcs" | "unitKg" | "unitG" | "unitL" | "unitMl"
  | "unitM" | "unitM2" | "unitPack" | "unitHour";

/** Ключ единицы с сервера → ключ перевода: pcs → unitPcs, m2 → unitM2.
 *
 * Единицы приходят стабильными строками и переводятся на фронте, поэтому ключ
 * собирается, а не хранится вторым списком, который разъедется с первым. */
export function unitKey(unit: string): UnitKey {
  return ("unit" + unit.charAt(0).toUpperCase() + unit.slice(1)) as UnitKey;
}

export interface Product {
  id: number;
  sku: string | null;
  name: string;
  unit: string;
  /** Минимальные единицы, как везде в проекте. null — «цену не назвали». */
  price: number | null;
  cost: number | null;
  is_service: boolean;
  min_stock_milli: number | null;
  note: string;
  /** null у услуги: остатка не бывает — это не то же самое, что ноль. */
  stock_milli: number | null;
  low_stock: boolean;
  /** Где и сколько. Приходит только когда складов больше одного. */
  by_warehouse?: Record<string, number>;
}

export function Warehouse() {
  const { t, locale, workspace, toastError } = useApp();
  const navigate = useNavigate();
  const [query, setQuery] = useState("");
  const [lowOnly, setLowOnly] = useState(false);
  const [data, setData] = useState<{ items: Product[]; total: number; currency: string } | null>(
    null,
  );
  const [showNew, setShowNew] = useState(false);
  // Есть ли на экране поле сканера — от этого зависит, кто забирает фокус.
  const scanning = useLabelsOn();
  const places = useWarehouses();
  // Остаток одного склада вместо суммы по всем — «а на точке-то оно есть?».
  const [place, setPlace] = useState<number | null>(null);

  const [attempt, setAttempt] = useState(0);

  const { failure, fail, clear } = useFailure();

  const search = useDebounced(query);

  const path =
    `/warehouse/products?search=${encodeURIComponent(search)}&low_only=${lowOnly}&per_page=200` +
    (place ? `&warehouse_id=${place}` : "");

  useEffect(() => {
    // Склад и «только мало» переключают быстрее, чем отвечает сервер: без
    // счётчика остаток по прошлому складу ложился поверх текущего — и это
    // худший из возможных обманов, потому что число правдоподобное. Приём тот
    // же, что в отчётах и палитре команд.
    let current = true;
    clear();
    api
      .get(path)
      .then((found) => {
        if (current) setData(found);
      })
      .catch((e) => {
        if (current) fail(e);
      });
    return () => {
      current = false;
    };
  }, [path, attempt, fail, clear]);

  if (!data) {
    return <ScreenLoading error={failure} onRetry={() => setAttempt((n) => n + 1)} />;
  }

  const currency = data.currency || workspace.currency;

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1 className="page-title">{t("warehouse")}</h1>
          <div className="page-sub">{t("warehouseSub", { total: data.total })}</div>
        </div>
        <button className="btn btn-primary" onClick={() => setShowNew(true)}>
          <Icon name="plus" stroke={2} />
          {t("newProduct")}
        </button>
      </div>

      {/* Переносим на новую строку, а не сжимаем до нечитаемого: на телефоне
          поиск, сканер и фильтр в один ряд не встают. */}
      <div style={{ display: "flex", gap: 10, marginBottom: 16, alignItems: "center", flexWrap: "wrap" }}>
        <div
          style={{
            flex: "2 1 180px",
            minWidth: 0,
            display: "flex",
            alignItems: "center",
            gap: 8,
            padding: "0 12px",
            height: 36,
            border: "1px solid var(--border)",
            borderRadius: 8,
            background: "var(--surface)",
          }}
        >
          <Icon name="search" size={15} className="" />
          <input
            style={{ flex: 1, background: "none", border: "none", outline: "none", color: "var(--text)", fontSize: 13.5, fontFamily: "var(--sans)" }}
            placeholder={t("searchProducts")}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            // Фокус отдаём сканеру, когда он есть: два поля с autoFocus дерутся
            // за курсор, и выигрывает то, которое смонтировалось последним, —
            // то есть случайное.
            autoFocus={!scanning}
          />
        </div>
        <BarcodeScanner />
        {/* Выбор склада появляется сам, когда складов больше одного, — правило
            считает сервер, не этот экран. */}
        <WarehousePicker places={places} value={place} onChange={setPlace} allowAll />
        <button
          className={"filter-chip" + (lowOnly ? " active" : "")}
          onClick={() => setLowOnly((on) => !on)}
        >
          <Icon name="alert" size={13} />
          {t("onlyLow")}
        </button>
      </div>

      <div className="list-card">
        <div className="list-header">
          <span style={{ width: 90 }}>{t("sku")}</span>
          <span style={{ flex: 1 }}>{t("product")}</span>
          <span style={{ width: 110, textAlign: "right" }}>{t("costPrice")}</span>
          <span style={{ width: 110, textAlign: "right" }}>{t("sellPrice")}</span>
          <span style={{ width: 150, textAlign: "right" }}>{t("stock")}</span>
        </div>
        {data.items.map((product) => (
          <Link to={`/warehouse/${product.id}`} key={product.id} className="list-row hoverable">
            <span style={{ width: 90, color: "var(--faint)", fontSize: 12 }} className="truncate">
              {product.sku ?? "—"}
            </span>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ color: "var(--text)", fontSize: 13.5, fontWeight: 500 }}>
                {product.name}
              </div>
              {product.is_service && (
                <div style={{ color: "var(--faint)", fontSize: 12 }}>{t("isService")}</div>
              )}
            </div>
            <span style={{ width: 110, textAlign: "right", color: "var(--muted)", fontSize: 12.5 }}>
              {formatMoney(product.cost, currency, locale)}
            </span>
            <span style={{ width: 110, textAlign: "right", color: "var(--muted)", fontSize: 12.5 }}>
              {formatMoney(product.price, currency, locale)}
            </span>
            <span style={{ width: 150, textAlign: "right", flexShrink: 0 }}>
              <StockValue product={product} />
            </span>
          </Link>
        ))}
        {data.items.length === 0 && (
          <EmptyState
            title={query || lowOnly ? t("nothingFound", { q: query }) : t("noProductsYet")}
          />
        )}
      </div>

      {showNew && (
        <NewProductModal
          onClose={() => setShowNew(false)}
          onCreated={(product) => navigate(`/warehouse/${product.id}`)}
        />
      )}
    </div>
  );
}

/** Остаток одной строкой.
 *
 * Три состояния, которые нельзя путать: у услуги остатка не бывает, минус —
 * товар отдали без прихода, «мало» — пора закупать. Ноль и «нет остатка»
 * выглядят по-разному намеренно. */
export function StockValue({ product }: { product: Product }) {
  const { t } = useApp();
  if (product.stock_milli === null) {
    return <span style={{ color: "var(--faint)", fontSize: 12.5 }}>{t("noStock")}</span>;
  }
  const negative = product.stock_milli < 0;
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 6, justifyContent: "flex-end" }}>
      {negative ? (
        <Chip variant="brand">{t("negativeStock")}</Chip>
      ) : (
        product.low_stock && <Chip variant="warning">{t("lowStock")}</Chip>
      )}
      <span
        style={{
          color: negative ? "var(--danger)" : "var(--text)",
          fontSize: 13.5,
          fontWeight: 500,
        }}
      >
        {formatQuantity(product.stock_milli)} {t(unitKey(product.unit))}
      </span>
    </span>
  );
}

/** Поле ввода денег → минимальные единицы, как в карточке заявки. */
function toMinor(value: string): number | null {
  const trimmed = value.trim().replace(",", ".");
  if (!trimmed) return null;
  const parsed = Number(trimmed);
  return Number.isFinite(parsed) ? Math.round(parsed * 100) : null;
}

function NewProductModal({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: (product: Product) => void;
}) {
  const { t, toastError } = useApp();
  const [form, setForm] = useState({
    name: "",
    sku: "",
    unit: "pcs",
    cost: "",
    price: "",
    min_stock: "",
    note: "",
    is_service: false,
  });
  // Засов, а не флаг: два одинаковых товара в справочнике — это два остатка,
  // и приход дальше ляжет на тот, который откроют, а не на тот, который
  // считают. Отпускаем только на отказе: при успехе уходим в карточку.
  const guard = useGuard();

  const set = (key: string) => (e: any) => setForm((f) => ({ ...f, [key]: e.target.value }));

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!guard.take()) return;
    try {
      onCreated(
        await api.post("/warehouse/products", {
          name: form.name,
          unit: form.unit,
          is_service: form.is_service,
          note: form.note,
          // Пустое поле — null, а не 0: «цену не назвали» и «отдаём бесплатно»
          // разные состояния, и подменять одно другим нельзя.
          sku: form.sku.trim() || null,
          cost: toMinor(form.cost),
          price: toMinor(form.price),
          // Количество уходит строкой: его разбирает сервер, чтобы лишние знаки
          // после запятой получили отказ, а не тихое округление в браузере.
          min_stock: form.is_service ? null : form.min_stock.trim() || null,
        }),
      );
    } catch (err) {
      toastError(err);
      guard.free();
    }
  };

  return (
    <Modal title={t("newProduct")} onClose={onClose}>
      <form onSubmit={submit}>
        <div className="field">
          <label className="label">{t("name")}</label>
          <input className="input" value={form.name} onChange={set("name")} autoFocus required />
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14 }}>
          <div className="field">
            <label className="label">{t("skuOptional")}</label>
            <input className="input" value={form.sku} onChange={set("sku")} />
          </div>
          <div className="field">
            <label className="label">{t("unit")}</label>
            <select className="select" value={form.unit} onChange={set("unit")}>
              {UNITS.map((unit) => (
                <option key={unit} value={unit}>
                  {t(unitKey(unit))}
                </option>
              ))}
            </select>
          </div>
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14 }}>
          <div className="field">
            <label className="label">{t("costPrice")}</label>
            <input className="input" value={form.cost} onChange={set("cost")} placeholder="0" />
          </div>
          <div className="field">
            <label className="label">{t("sellPrice")}</label>
            <input className="input" value={form.price} onChange={set("price")} placeholder="0" />
          </div>
        </div>
        <div className="field">
          <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer" }}>
            <input
              type="checkbox"
              checked={form.is_service}
              onChange={(e) => setForm((f) => ({ ...f, is_service: e.target.checked }))}
            />
            <span style={{ fontSize: 13 }}>{t("isService")}</span>
          </label>
          <div className="field-desc">{t("isServiceHint")}</div>
        </div>
        {!form.is_service && (
          <div className="field">
            <label className="label">{t("minStock")}</label>
            <input className="input" value={form.min_stock} onChange={set("min_stock")} />
            <div className="field-desc">{t("minStockHint")}</div>
          </div>
        )}
        <div className="field" style={{ marginBottom: 20 }}>
          <label className="label">{t("productNote")}</label>
          <textarea className="textarea" value={form.note} onChange={set("note")} />
        </div>
        <button className="btn btn-primary" style={{ width: "100%" }} disabled={guard.busy}>
          {t("create")}
        </button>
      </form>
    </Modal>
  );
}
