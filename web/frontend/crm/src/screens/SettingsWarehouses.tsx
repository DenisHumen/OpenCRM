import { useCallback, useEffect, useState, type FormEvent } from "react";

import { Icon } from "../components/Icon";
import { Chip, ConfirmModal, EmptyState, Modal, ScreenLoading } from "../components/ui";
import type { Warehouse } from "../components/Warehouses";
import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { useLiveTopic } from "../lib/live";
import { useFailure } from "../lib/failure";
import { useGuard } from "../lib/guard";
import type { TranslationKey } from "../lib/i18n";

/** Склады как места: завести, переименовать, закрыть.
 *
 * Экран в настройках, а не в «Работе», и на своём праве `warehouse.manage`.
 * Завести склад — решение структурное, вроде «завести юрлицо»: кладовщик
 * двигает товар каждый день, а склады заводят раз в год, и это разные
 * полномочия. Приход, расход и перемещение остаются на `warehouse.create`.
 */
export function SettingsWarehouses() {
  const { t, toast, toastError } = useApp();
  const [items, setItems] = useState<Warehouse[] | null>(null);
  const [editing, setEditing] = useState<Warehouse | null>(null);
  const [adding, setAdding] = useState(false);
  const [closing, setClosing] = useState<Warehouse | null>(null);
  const { failure, fail, clear } = useFailure();

  const load = useCallback(() => {
    clear();
    api
      .get<{ items: Warehouse[] }>("/warehouses")
      .then((data) => setItems(data.items))
      .catch(fail);
  }, [fail, clear]);

  useEffect(load, [load]);
  useLiveTopic("warehouses", load);

  if (!items) return <ScreenLoading error={failure} onRetry={load} />;

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1 className="page-title">{t("warehouses")}</h1>
          <div className="page-sub">{t("warehousesSub")}</div>
        </div>
        <button className="btn btn-primary" onClick={() => setAdding(true)}>
          <Icon name="plus" stroke={2} />
          {t("newWarehouse")}
        </button>
      </div>

      {/* Правило «выбор склада появляется, когда складов больше одного» стоит
          объяснить здесь же: иначе заведение второго склада выглядит как
          изменение, которое ничего не поменяло. */}
      <div className="field-desc" style={{ marginBottom: 16 }}>{t("warehousesAbout")}</div>

      <div className="list-card">
        {items.map((warehouse) => (
          <div className="list-row" key={warehouse.id} style={{ gap: 10 }}>
            <Icon name="warehouse" size={15} className="" />
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ color: "var(--text)", fontSize: 13.5, fontWeight: 500 }}>
                {warehouse.name}
                {warehouse.code && (
                  <span style={{ color: "var(--faint)", fontWeight: 400 }}> · {warehouse.code}</span>
                )}
              </div>
              {warehouse.address && (
                <div style={{ color: "var(--faint)", fontSize: 12 }}>{warehouse.address}</div>
              )}
              {warehouse.kind === "shop" && <SiteLine warehouseId={warehouse.id} />}
            </div>
            {warehouse.kind !== "stock" && <Chip>{t(KIND_LABEL[warehouse.kind] ?? "warehouseKindStock")}</Chip>}
            {warehouse.is_default ? (
              <Chip variant="brand">{t("warehouseDefault")}</Chip>
            ) : (
              // Кнопка названа действием, а не состоянием: подпись
              // «Основной» на всех строках сразу читается так, будто основных
              // три, и какая из них метка, а какая кнопка — не разобрать.
              <button
                className="btn btn-secondary btn-sm"
                onClick={async () => {
                  try {
                    await api.patch(`/warehouses/${warehouse.id}`, { is_default: true });
                    load();
                  } catch (err) {
                    toastError(err);
                  }
                }}
              >
                {t("warehouseMakeDefault")}
              </button>
            )}
            <button className="btn-icon" title={t("warehouseName")} onClick={() => setEditing(warehouse)}>
              <Icon name="note" size={14} />
            </button>
            <button
              className="btn-icon"
              title={t("warehouseClose")}
              onClick={() => setClosing(warehouse)}
            >
              <Icon name="trash" size={14} />
            </button>
          </div>
        ))}
        {items.length === 0 && <EmptyState icon="warehouse" title={t("warehouses")} />}
      </div>

      {(adding || editing) && (
        <WarehouseModal
          warehouse={editing}
          onClose={() => {
            setAdding(false);
            setEditing(null);
          }}
          onSaved={() => {
            setAdding(false);
            setEditing(null);
            load();
          }}
        />
      )}

      {closing && (
        <ConfirmModal
          text={t("warehouseCloseConfirm", { name: closing.name })}
          confirmLabel={t("warehouseClose")}
          danger
          onConfirm={async () => {
            try {
              await api.del(`/warehouses/${closing.id}`);
              toast(t("warehouseClose") + " ✓");
              load();
            } catch (err) {
              // Последний склад и склад с остатком закрыть нельзя — сервер
              // называет причину, и показать её надо целиком: «нельзя» без
              // причины отправляет человека гадать.
              toastError(err);
            }
          }}
          onClose={() => setClosing(null)}
        />
      )}
    </div>
  );
}

export const KIND_LABEL: Record<string, TranslationKey> = {
  stock: "warehouseKindStock",
  shop: "warehouseKindShop",
  transit: "warehouseKindTransit",
  defect: "warehouseKindDefect",
};

/** «На сайте: 132 позиции, 4 без цены» — у магазинного склада. Молчать об
 *  этом нельзя: товар без цены на сайте есть, а купить его нельзя. */
function SiteLine({ warehouseId }: { warehouseId: number }) {
  const { t } = useApp();
  const [itog, setItog] = useState<{ published: number; without_price: number } | null>(null);

  useEffect(() => {
    let zhiv = true;
    api
      .get<{ published: number; without_price: number }>(`/warehouses/${warehouseId}/site`)
      .then((r) => { if (zhiv) setItog(r); })
      .catch(() => undefined);
    return () => { zhiv = false; };
  }, [warehouseId]);

  if (!itog) return null;
  return (
    <div style={{ color: itog.without_price ? "var(--warning)" : "var(--faint)", fontSize: 12 }}>
      {t("warehouseOnSite", { published: itog.published, without_price: itog.without_price })}
    </div>
  );
}

function WarehouseModal({
  warehouse,
  onClose,
  onSaved,
}: {
  warehouse: Warehouse | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const { t, toastError } = useApp();
  const [form, setForm] = useState({
    name: warehouse?.name ?? "",
    code: warehouse?.code ?? "",
    address: warehouse?.address ?? "",
    note: warehouse?.note ?? "",
    kind: warehouse?.kind ?? "stock",
  });
  // Засов, а не флаг состояния: второй склад с тем же названием — это
  // второе место, по которому потом разъедется остаток одного и того же
  // товара. Отпускаем только на отказе: при успехе окно закрывается.
  const guard = useGuard();
  // Уход из «магазина» убирает с сайта весь каталог склада разом — экран
  // спрашивает подтверждение и называет число ДО нажатия.
  const [leavingShop, setLeavingShop] = useState<number | null>(null);

  const set = (key: string) => (e: any) => setForm((f) => ({ ...f, [key]: e.target.value }));

  const save = async () => {
    if (!guard.take()) return;
    try {
      const body = {
        name: form.name,
        // Пустой код — null, а не пустая строка: два склада без кода это норма,
        // а две пустые строки нарушили бы уникальность.
        code: form.code.trim() || null,
        address: form.address,
        note: form.note,
        kind: form.kind,
      };
      if (warehouse) await api.patch(`/warehouses/${warehouse.id}`, body);
      else await api.post("/warehouses", body);
      onSaved();
    } catch (err) {
      toastError(err);
      guard.free();
    }
  };

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (warehouse && warehouse.kind === "shop" && form.kind !== "shop") {
      try {
        const itog = await api.get<{ published: number }>(`/warehouses/${warehouse.id}/site`);
        setLeavingShop(itog.published);
      } catch (err) {
        toastError(err);
      }
      return;
    }
    await save();
  };

  return (
    <Modal title={warehouse ? warehouse.name : t("newWarehouse")} onClose={onClose}>
      {leavingShop !== null && (
        <ConfirmModal
          text={t("warehouseKindLeaveShop", { count: leavingShop })}
          confirmLabel={t("save")}
          danger
          onConfirm={() => { setLeavingShop(null); void save(); }}
          onClose={() => setLeavingShop(null)}
        />
      )}
      <form onSubmit={submit}>
        <div className="field">
          <label className="label">{t("warehouseName")}</label>
          <input className="input" value={form.name} onChange={set("name")} autoFocus required />
        </div>
        <div className="field">
          <label className="label">{t("warehouseKind")}</label>
          <select className="input" value={form.kind} onChange={set("kind")}>
            <option value="stock">{t("warehouseKindStock")}</option>
            <option value="shop">{t("warehouseKindShop")}</option>
            <option value="transit">{t("warehouseKindTransit")}</option>
            <option value="defect">{t("warehouseKindDefect")}</option>
          </select>
          <div className="field-desc">{t("warehouseKindHint")}</div>
        </div>
        <div className="field">
          <label className="label">{t("warehouseCode")}</label>
          <input className="input" value={form.code} onChange={set("code")} />
          <div className="field-desc">{t("warehouseCodeHint")}</div>
        </div>
        <div className="field">
          <label className="label">{t("warehouseAddress")}</label>
          <input className="input" value={form.address} onChange={set("address")} />
        </div>
        <div className="field" style={{ marginBottom: 20 }}>
          <label className="label">{t("productNote")}</label>
          <textarea className="textarea" value={form.note} onChange={set("note")} />
        </div>
        <button className="btn btn-primary" style={{ width: "100%" }} disabled={guard.busy}>
          {warehouse ? t("save") : t("create")}
        </button>
      </form>
    </Modal>
  );
}
