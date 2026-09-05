import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { Chip, ConfirmModal, EmptyState, ScreenLoading } from "../components/ui";
import { Icon } from "../components/Icon";
import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { useFailure } from "../lib/failure";
import { formatBytes, formatDate, formatDateTime } from "../lib/format";

interface MediaFile {
  id: number;
  board_id: number;
  board_title: string;
  kind: string;
  status: string;
  original_name: string;
  mime: string;
  size_bytes: number;
  created_at: string | null;
  last_viewed_at: string | null;
  thumb: string | null;
}

// Ширины колонок — общие для шапки и строк, чтобы всё было выровнено.
const COL = {
  board: { width: 180 } as const,
  size: { width: 90, textAlign: "right" } as const,
  uploaded: { width: 120 } as const,
  viewed: { width: 150 } as const,
  actions: { width: 40, textAlign: "right" } as const,
};

export function Files() {
  const { t, locale, toastError, refreshStorage } = useApp();
  const [items, setItems] = useState<MediaFile[] | null>(null);
  const [confirmId, setConfirmId] = useState<number | null>(null);

  const { failure, fail, clear } = useFailure();

  const load = useCallback(() => {
    clear();
    api
      .get<{ items: MediaFile[] }>("/system/files")
      .then((d) => setItems(d.items))
      .catch(fail);
  }, [fail, clear]);

  useEffect(() => {
    load();
  }, [load]);

  const totalBytes = useMemo(
    () => (items ?? []).reduce((sum, f) => sum + (f.size_bytes || 0), 0),
    [items],
  );

  if (!items) return <ScreenLoading error={failure} onRetry={load} />;

  const remove = async (id: number) => {
    try {
      await api.del(`/system/files/${id}`);
      setItems((prev) => (prev ?? []).filter((f) => f.id !== id));
      await refreshStorage();
    } catch (e) {
      toastError(e);
    }
  };

  return (
    <div className="page page-wide">
      <div style={{ marginBottom: 24 }}>
        <h1 className="page-title">{t("files")}</h1>
        <div className="page-sub">{t("filesSub")}</div>
      </div>

      {items.length === 0 ? (
        <EmptyState icon="folder" title={t("filesEmpty")} />
      ) : (
        <>
          <div style={{ color: "var(--faint)", fontSize: 12.5, marginBottom: 14 }}>
            {t("filesSummary", { count: items.length, size: formatBytes(totalBytes) })}
          </div>
          <div className="list-card">
            <div className="list-header">
              <span style={{ width: 40 }} />
              <span style={{ flex: 1 }}>{t("colFile")}</span>
              <span style={COL.board}>{t("colBoard")}</span>
              <span style={COL.size}>{t("colSize")}</span>
              <span style={COL.uploaded}>{t("colUploaded")}</span>
              <span style={COL.viewed}>{t("colLastViewed")}</span>
              <span style={COL.actions} />
            </div>
            {items.map((file) => (
              <div key={file.id} className="list-row hoverable">
                <div
                  style={{
                    width: 40,
                    height: 40,
                    borderRadius: 7,
                    overflow: "hidden",
                    background: "var(--surface-2)",
                    display: "grid",
                    placeItems: "center",
                    flexShrink: 0,
                  }}
                >
                  {file.thumb ? (
                    <img src={file.thumb} alt="" style={{ width: "100%", height: "100%", objectFit: "cover" }} />
                  ) : (
                    <Icon name={file.kind === "video" ? "play" : "image"} size={15} className="" />
                  )}
                </div>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div
                    style={{
                      color: "var(--text)",
                      fontSize: 13,
                      fontWeight: 500,
                      whiteSpace: "nowrap",
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                    }}
                    title={file.original_name}
                  >
                    {file.original_name}
                  </div>
                  <div style={{ display: "flex", alignItems: "center", gap: 6, marginTop: 2 }}>
                    <Chip variant={file.kind === "video" ? "accent" : undefined}>{file.kind}</Chip>
                    {file.status !== "ready" && (
                      <span style={{ color: "var(--faint)", fontSize: 11 }}>{file.status}</span>
                    )}
                  </div>
                </div>
                <Link
                  to={`/boards/${file.board_id}`}
                  style={{
                    ...COL.board,
                    color: "var(--muted)",
                    fontSize: 12.5,
                    textDecoration: "none",
                    whiteSpace: "nowrap",
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                  }}
                  title={file.board_title}
                >
                  {file.board_title}
                </Link>
                <span style={{ ...COL.size, color: "var(--text)", fontSize: 12.5 }}>
                  {formatBytes(file.size_bytes)}
                </span>
                <span style={{ ...COL.uploaded, color: "var(--muted)", fontSize: 12.5 }}>
                  {formatDate(file.created_at, locale)}
                </span>
                <span style={{ ...COL.viewed, color: "var(--muted)", fontSize: 12.5 }}>
                  {file.last_viewed_at ? formatDateTime(file.last_viewed_at, locale) : t("neverViewed")}
                </span>
                <span style={COL.actions}>
                  <button
                    className="btn-icon"
                    style={{ width: 30, height: 30, border: "none", color: "var(--danger)" }}
                    title={t("delete")}
                    onClick={() => setConfirmId(file.id)}
                  >
                    <Icon name="trash" size={14} />
                  </button>
                </span>
              </div>
            ))}
          </div>
        </>
      )}

      {confirmId !== null && (
        <ConfirmModal
          text={t("deleteFileConfirm")}
          confirmLabel={t("delete")}
          danger
          onConfirm={() => void remove(confirmId)}
          onClose={() => setConfirmId(null)}
        />
      )}
    </div>
  );
}
