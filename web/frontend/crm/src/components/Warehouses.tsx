import { useCallback, useEffect, useState, type FormEvent } from "react";

import { Icon } from "./Icon";
import { Chip, Modal } from "./ui";
import { api, ApiError } from "../lib/api";
import { useApp } from "../lib/app";
import { useGuard } from "../lib/guard";
import { formatQuantity } from "../lib/format";

/** Склады как места: выбор в формах, раскладка остатка, переезд.
 *
 * Собрано в один файл по той же причине, что и штрихкоды: у части интерфейса,
 * которая появляется и исчезает по условию, должна быть граница. Здесь условие
 * не «блок включён», а **«складов больше одного»** — и оно выводится из данных,
 * а не из настройки. Мастерской с одной подсобкой выбор склада в каждой форме
 * помеха, а не возможность; завёл второй склад — выбор появился везде сам,
 * закрыл — исчез.
 *
 * Считает это правило сервер (`GET /warehouses` отдаёт `many`), а не экран:
 * посчитанное здесь стало бы вторым экземпляром того же правила, а два
 * экземпляра расходятся молча.
 */

export interface Warehouse {
  id: number;
  name: string;
  code: string | null;
  address: string;
  is_default: boolean;
  /** Что это за место: `stock` / `shop` / `transit` / `defect`. Сайту виден
   *  остаток складов `shop` и только их. */
  kind: string;
  note: string;
}

export interface Transfer {
  id: number;
  from_warehouse_id: number;
  from_warehouse_name: string | null;
  to_warehouse_id: number;
  to_warehouse_name: string | null;
  comment: string;
  reverses_id: number | null;
  /** Уже отменён: кнопки «отменить» на нём быть не должно. */
  reverted: boolean;
  happened_at: string | null;
  author_id: number | null;
  items: { product_id: number; quantity_milli: number }[];
}

interface Places {
  items: Warehouse[];
  /** Показывать ли выбор склада. Приходит с сервера, здесь не выводится. */
  many: boolean;
}

/** Склады и «надо ли вообще про них спрашивать».
 *
 * `null` — ещё не загрузились или блок закрыт: в этом случае формы ведут себя
 * ровно так, как вели до складов, и ничего не ломается.
 */
export function useWarehouses(): Places | null {
  const [places, setPlaces] = useState<Places | null>(null);
  useEffect(() => {
    let alive = true;
    api
      .get<Places>("/warehouses")
      .then((data) => {
        if (alive) setPlaces(data);
      })
      .catch(() => {
        // Блок выключили или права нет — это не ошибка человека: выбор склада
        // просто не появляется, а склад работает как одиночный.
        if (alive) setPlaces(null);
      });
    return () => {
      alive = false;
    };
  }, []);
  return places;
}

/** Выпадающий список складов. Пока склад один — не показывается вовсе. */
export function WarehousePicker({
  places,
  value,
  onChange,
  allowAll = false,
  label,
}: {
  places: Places | null;
  value: number | null;
  onChange: (id: number | null) => void;
  /** Пункт «все склады» — для фильтров, но не для форм записи движения. */
  allowAll?: boolean;
  label?: string;
}) {
  const { t } = useApp();
  if (!places?.many) return null;
  return (
    <select
      className="select"
      aria-label={label ?? t("warehousePick")}
      value={value === null ? "" : String(value)}
      onChange={(e) => onChange(e.target.value ? Number(e.target.value) : null)}
    >
      {allowAll && <option value="">{t("warehouseAll")}</option>}
      {places.items.map((w) => (
        <option key={w.id} value={w.id}>
          {w.name}
        </option>
      ))}
    </select>
  );
}

/** Где и сколько лежит — раскладка под остатком в карточке товара.
 *
 * Склады с нулём показываем тоже, и это осознанно: «на точке ноль» — самый
 * важный ответ из всех, а строки без нулей читаются как «про эту точку данных
 * нет». Сервер нули не присылает (их там нет), дорисовываем здесь — у экрана
 * есть список складов, а у запроса его нет.
 */
export function WarehouseSpread({
  places,
  spread,
  unit,
}: {
  places: Places | null;
  spread: Record<string, number> | undefined;
  unit: string;
}) {
  if (!places?.many || !spread) return null;
  return (
    <div style={{ marginTop: 12, display: "flex", flexDirection: "column", gap: 4 }}>
      {places.items.map((w) => {
        const amount = spread[String(w.id)] ?? 0;
        return (
          <div
            key={w.id}
            style={{ display: "flex", justifyContent: "space-between", fontSize: 12.5 }}
          >
            <span style={{ color: amount ? "var(--muted)" : "var(--faint)" }}>{w.name}</span>
            <span
              style={{
                color: amount < 0 ? "var(--danger)" : amount ? "var(--text)" : "var(--faint)",
                fontWeight: amount ? 500 : 400,
              }}
            >
              {formatQuantity(amount)} {unit}
            </span>
          </div>
        );
      })}
    </div>
  );
}

/** Переезд товара между складами.
 *
 * Отдельным действием, а не «расход тут, приход там»: товар не появился и не
 * пропал, он переехал, и в истории это одно событие. Не хватает на источнике —
 * форма останавливается и спрашивает ещё раз: увезти с пустого склада нечего
 * физически, и молчаливый минус здесь означал бы коробку из воздуха.
 */
export function TransferModal({
  productId,
  places,
  onClose,
  onDone,
}: {
  productId: number;
  places: Places;
  onClose: () => void;
  onDone: () => void;
}) {
  const { t, toast, toastError } = useApp();
  const [from, setFrom] = useState(places.items[0]?.id ?? 0);
  const [to, setTo] = useState(places.items[1]?.id ?? 0);
  const [quantity, setQuantity] = useState("");
  const [comment, setComment] = useState("");
  // Засов, а не состояние. Переезд — это запись в истории склада, а остаток
  // равен её сумме: два одинаковых переезда от двойного нажатия увезут вдвое
  // больше, чем увезли на самом деле, и разобраться потом будет не по чему.
  const guard = useGuard();
  const [shortage, setShortage] = useState<string | null>(null);

  const send = async (force: boolean) => {
    if (!quantity.trim() || !guard.take()) return;
    try {
      await api.post("/warehouse/transfers", {
        product_id: productId,
        from_warehouse_id: from,
        to_warehouse_id: to,
        // Количество уходит строкой как набрали: разбирает его сервер, чтобы
        // лишние знаки после запятой получили отказ, а не тихое округление.
        quantity: quantity.trim(),
        comment: comment.trim() || null,
        confirm_negative: force,
      });
      toast(t("transferSaved"));
      onDone();
      onClose();
    } catch (err) {
      if (err instanceof ApiError && err.code === "not_enough_on_source") {
        // Не отказ насовсем: показываем, сколько лежит, и даём подтвердить.
        setShortage(err.message);
      } else {
        toastError(err);
      }
    } finally {
      guard.free();
    }
  };

  const submit = (e: FormEvent) => {
    e.preventDefault();
    void send(false);
  };

  return (
    <Modal title={t("transfer")} onClose={onClose}>
      <form onSubmit={submit}>
        <div style={{ display: "grid", gridTemplateColumns: "1fr auto 1fr", gap: 10, alignItems: "end" }}>
          <div className="field" style={{ marginBottom: 0 }}>
            <label className="label">{t("transferFrom")}</label>
            <select className="select" value={from} onChange={(e) => setFrom(Number(e.target.value))}>
              {places.items.map((w) => (
                <option key={w.id} value={w.id}>
                  {w.name}
                </option>
              ))}
            </select>
          </div>
          <Icon name="arrowIn" size={16} />
          <div className="field" style={{ marginBottom: 0 }}>
            <label className="label">{t("transferTo")}</label>
            <select className="select" value={to} onChange={(e) => setTo(Number(e.target.value))}>
              {places.items.map((w) => (
                <option key={w.id} value={w.id}>
                  {w.name}
                </option>
              ))}
            </select>
          </div>
        </div>
        <div className="field" style={{ marginTop: 14 }}>
          <label className="label">{t("quantity")}</label>
          <input
            className="input"
            value={quantity}
            onChange={(e) => {
              setQuantity(e.target.value);
              // Изменили количество — прежнее предупреждение о нехватке
              // относилось к другому числу и вводило бы в заблуждение.
              setShortage(null);
            }}
            autoFocus
            required
          />
        </div>
        <div className="field">
          <label className="label">{t("moveComment")}</label>
          <input className="input" value={comment} onChange={(e) => setComment(e.target.value)} />
        </div>

        {shortage && (
          <div className="field-desc" style={{ color: "var(--warning)", marginBottom: 12 }}>
            {shortage}
          </div>
        )}
        {shortage ? (
          <button
            type="button"
            className="btn btn-primary"
            style={{ width: "100%" }}
            disabled={guard.busy}
            onClick={() => void send(true)}
          >
            {t("transferForce")}
          </button>
        ) : (
          <button className="btn btn-primary" style={{ width: "100%" }} disabled={guard.busy || from === to}>
            {t("transferDo")}
          </button>
        )}
      </form>
    </Modal>
  );
}

/** Журнал переездов: что, сколько, откуда, куда и кто.
 *
 * Отдельным списком, а не строками в общей истории движений: там переезд —
 * две строки подряд с разными знаками, и понять, одно это событие или два
 * разных, можно только по времени и по памяти.
 */
export function TransferLog({
  warehouseId,
  productId,
  productNames,
}: {
  warehouseId?: number | null;
  /** Журнал в карточке товара обязан быть про эту позицию, а не про все сразу. */
  productId?: number;
  productNames?: Record<number, string>;
}) {
  const { t, locale, toastError } = useApp();
  const [items, setItems] = useState<Transfer[] | null>(null);
  // Отмена переезда — не удаление, а встречный переезд, то есть ещё одна
  // запись в истории. Двойное нажатие завело бы две, и товар уехал бы обратно
  // дважды. Засов один на весь журнал: отменяют по одному.
  const guard = useGuard();

  const load = useCallback(() => {
    const query = new URLSearchParams();
    if (warehouseId) query.set("warehouse_id", String(warehouseId));
    if (productId) query.set("product_id", String(productId));
    const tail = query.toString();
    api
      .get<{ items: Transfer[] }>(`/warehouse/transfers${tail ? `?${tail}` : ""}`)
      .then((data) => setItems(data.items))
      .catch(toastError);
  }, [warehouseId, productId, toastError]);

  useEffect(load, [load]);

  if (!items) return null;
  if (items.length === 0) {
    return <div className="field-desc">{t("transfersEmpty")}</div>;
  }

  return (
    <div>
      {items.map((row) => (
        <div className="feed-item" key={row.id}>
          <div className="feed-icon">
            <Icon name="arrowOut" size={14} />
          </div>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ display: "flex", alignItems: "baseline", gap: 8, flexWrap: "wrap" }}>
              <span style={{ color: "var(--text)", fontSize: 13 }}>
                {row.from_warehouse_name} → {row.to_warehouse_name}
              </span>
              {row.reverses_id !== null && <Chip>{t("transferIsUndo")}</Chip>}
              <span style={{ color: "var(--faint)", fontSize: 12, marginLeft: "auto" }}>
                {formatWhen(row.happened_at, locale)}
              </span>
            </div>
            <div style={{ color: "var(--faint)", fontSize: 12.5, marginTop: 3 }}>
              {row.items
                .map(
                  (line) =>
                    `${productNames?.[line.product_id] ?? "#" + line.product_id}: ` +
                    formatQuantity(line.quantity_milli),
                )
                .join(" · ")}
              {row.comment && <> · {row.comment}</>}
            </div>
          </div>
          {/* Кнопки нет ни у отменённого переезда, ни у самой отмены: сервер
              откажет и там, и там, и предлагать это значит звать на отказ. */}
          {!row.reverted && row.reverses_id === null && (
            <button
              className="btn btn-secondary btn-sm"
              disabled={guard.busy}
              onClick={async () => {
                if (!guard.take()) return;
                try {
                  await api.post(`/warehouse/transfers/${row.id}/revert`);
                  load();
                } catch (err) {
                  toastError(err);
                } finally {
                  guard.free();
                }
              }}
            >
              {t("transferRevert")}
            </button>
          )}
        </div>
      ))}
    </div>
  );
}

function formatWhen(value: string | null, locale: string): string {
  if (!value) return "";
  return new Date(value).toLocaleString(locale === "ru" ? "ru-RU" : "en-GB", {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}
