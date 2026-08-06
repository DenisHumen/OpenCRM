import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";

import { BoardCard } from "../components/BoardCard";
import { Icon } from "../components/Icon";
import { EmptyState, ScreenLoading } from "../components/ui";
import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { useFailure } from "../lib/failure";

type Filter = "all" | "published" | "drafts" | "revoked";

export function Boards() {
  const { t, toastError } = useApp();
  const navigate = useNavigate();
  const [data, setData] = useState<any>(null);
  const [filter, setFilter] = useState<Filter>("all");

  const { failure, fail, clear } = useFailure();

  const load = useCallback(() => {
    clear();
    api.get("/boards?per_page=100").then(setData).catch(fail);
  }, [fail, clear]);

  useEffect(load, [load]);

  if (!data) return <ScreenLoading error={failure} onRetry={load} />;

  const published = data.items.filter((b: any) => b.is_published);
  const filtered = data.items.filter((board: any) => {
    if (filter === "published") return board.is_published;
    if (filter === "drafts") return !board.is_published;
    if (filter === "revoked") return board.has_links && !board.has_active_link;
    return true;
  });

  const filters: { id: Filter; label: string }[] = [
    { id: "all", label: t("all") },
    { id: "published", label: t("published") },
    { id: "drafts", label: t("draft") },
    { id: "revoked", label: t("revoked") },
  ];

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1 className="page-title">{t("boards")}</h1>
          <div className="page-sub">{t("boardsSub", { total: data.total, published: published.length })}</div>
        </div>
        <button
          className="btn btn-primary"
          onClick={async () => {
            try {
              const board = await api.post("/boards", { title: t("newBoard") });
              navigate(`/boards/${board.id}`);
            } catch (e) {
              toastError(e);
            }
          }}
        >
          <Icon name="plus" stroke={2} />
          {t("newBoard")}
        </button>
      </div>

      <div style={{ display: "flex", gap: 6, marginBottom: 20 }}>
        {filters.map((item) => (
          <button
            key={item.id}
            className={"filter-chip" + (filter === item.id ? " active" : "")}
            onClick={() => setFilter(item.id)}
          >
            {item.label}
          </button>
        ))}
      </div>

      {filtered.length === 0 ? (
        <div className="card">
          <EmptyState title={t("noBoardsYet")} />
        </div>
      ) : (
        <div className="board-grid">
          {filtered.map((board: any) => (
            <BoardCard key={board.id} board={board} />
          ))}
        </div>
      )}
    </div>
  );
}
