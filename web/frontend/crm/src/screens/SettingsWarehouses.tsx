import { useCallback, useEffect, useState, type FormEvent } from "react";

import { Icon } from "../components/Icon";
import { Chip, ConfirmModal, EmptyState, Modal, ScreenLoading } from "../components/ui";
import type { Warehouse } from "../components/Warehouses";
import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { useFailure } from "../lib/failure";

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
            </div>
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
        {items.length === 0 && <EmptyState title={t("warehouses")} />}
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
          text={t("warehouseCloseConfirm")}
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
  });
  const [busy, setBusy] = useState(false);

  const set = (key: string) => (e: any) => setForm((f) => ({ ...f, [key]: e.target.value }));

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    try {
      const body = {
        name: form.name,
        // Пустой код — null, а не пустая строка: два склада без кода это норма,
        // а две пустые строки нарушили бы уникальность.
        code: form.code.trim() || null,
        address: form.address,
        note: form.note,
      };
      if (warehouse) await api.patch(`/warehouses/${warehouse.id}`, body);
      else await api.post("/warehouses", body);
      onSaved();
    } catch (err) {
      toastError(err);
      setBusy(false);
    }
  };

  return (
    <Modal title={warehouse ? warehouse.name : t("newWarehouse")} onClose={onClose}>
      <form onSubmit={submit}>
        <div className="field">
          <label className="label">{t("warehouseName")}</label>
          <input className="input" value={form.name} onChange={set("name")} autoFocus required />
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
        <button className="btn btn-primary" style={{ width: "100%" }} disabled={busy}>
          {warehouse ? t("save") : t("create")}
        </button>
      </form>
    </Modal>
  );
}
