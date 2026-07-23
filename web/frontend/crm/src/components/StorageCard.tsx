import { useState } from "react";

import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { formatBytes } from "../lib/format";
import { Icon } from "./Icon";
import { Chip, ConfirmModal } from "./ui";

export interface StorageStatus {
  total_bytes: number;
  used_bytes: number;
  free_bytes: number;
  percent_used: number;
  level: "ok" | "warning" | "critical";
  warning_percent: number;
  critical_percent: number;
  min_free_mb: number;
  storage_bytes: number;
  uploads_blocked: boolean;
  reclaimable?: {
    bytes: number;
    boards: number;
    works: number;
    clients: number;
    client_files: number;
  };
}

/** Полоса заполнения диска с отметками порогов. */
export function DiskBar({ storage }: { storage: StorageStatus }) {
  const color =
    storage.level === "critical"
      ? "var(--danger)"
      : storage.level === "warning"
        ? "var(--warning)"
        : "var(--success)";
  return (
    <div className="disk-bar">
      <div className="disk-bar-fill" style={{ width: `${Math.min(100, storage.percent_used)}%`, background: color }} />
      <div className="disk-bar-tick" style={{ left: `${storage.warning_percent}%` }} />
      <div className="disk-bar-tick" style={{ left: `${storage.critical_percent}%` }} />
    </div>
  );
}

/** Карточка «Хранилище» на дашборде. */
export function StorageCard({ storage, onPurged }: { storage: StorageStatus; onPurged?: () => void }) {
  const { t, user, toast, toastError } = useApp();
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);

  const reclaimable = storage.reclaimable;
  const canPurge = user?.role === "root" && (reclaimable?.bytes ?? 0) > 0;

  const purge = async () => {
    setBusy(true);
    try {
      const result = await api.post("/system/storage/purge");
      toast(t("freedSpace", { size: formatBytes(result.freed_bytes) }));
      onPurged?.();
    } catch (e) {
      toastError(e);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="card card-pad">
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 14 }}>
        <div className="metric-title">
          <Icon name="database" size={14} />
          {t("storage")}
        </div>
        {storage.level !== "ok" && (
          <Chip variant={storage.level === "critical" ? undefined : "warning"}>
            {storage.level === "critical" ? t("diskCritical") : t("diskWarning")}
          </Chip>
        )}
      </div>
      <div className="metric-value">{formatBytes(storage.free_bytes)}</div>
      <div className="metric-sub">
        {t("freeOf", { total: formatBytes(storage.total_bytes), percent: storage.percent_used })}
      </div>
      <DiskBar storage={storage} />
      {storage.uploads_blocked && (
        <div className="disk-blocked">
          <Icon name="alert" size={13} />
          {t("uploadsBlocked")}
        </div>
      )}
      <div className="disk-legend">
        <span>
          {t("mediaTakes")} <strong>{formatBytes(storage.storage_bytes)}</strong>
        </span>
        {reclaimable && reclaimable.bytes > 0 && (
          <span>
            {t("inTrash")} <strong>{formatBytes(reclaimable.bytes)}</strong>
          </span>
        )}
      </div>
      {canPurge && (
        <button className="btn btn-secondary btn-sm" style={{ marginTop: 12 }} onClick={() => setConfirm(true)} disabled={busy}>
          <Icon name="trash" size={13} />
          {t("emptyTrash")}
        </button>
      )}
      {confirm && reclaimable && (
        <ConfirmModal
          text={t("emptyTrashConfirm", {
            boards: reclaimable.boards,
            clients: reclaimable.clients,
            size: formatBytes(reclaimable.bytes),
          })}
          confirmLabel={t("emptyTrash")}
          danger
          onConfirm={() => void purge()}
          onClose={() => setConfirm(false)}
        />
      )}
    </div>
  );
}
