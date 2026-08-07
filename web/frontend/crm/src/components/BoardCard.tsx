import { Link } from "react-router-dom";

import { useApp } from "../lib/app";
import { formatDate } from "../lib/format";
import { Icon } from "./Icon";
import { Chip } from "./ui";

/** Карточка доски в сетках (дашборд, список досок, доски клиента). */
export function BoardCard({ board, compact }: { board: any; compact?: boolean }) {
  const { t, locale } = useApp();
  const revoked = board.has_links && !board.has_active_link;
  return (
    <Link to={`/boards/${board.id}`} className="card card-hover" style={{ display: "block", overflow: "hidden", color: "inherit" }}>
      <div className="board-cover" style={{ height: compact ? 110 : 150 }}>
        {board.cover ? (
          <img src={board.cover.card ?? board.cover.thumb ?? board.cover.poster} alt="" loading="lazy" />
        ) : (
          <Icon name="image" size={22} />
        )}
        {/* Затемнение лежит на обложке, а не на карточке: от темы CRM не зависит. */}
        {revoked && <div style={{ position: "absolute", inset: 0, background: "var(--media-scrim)" }} />}
      </div>
      <div style={{ padding: compact ? "12px 14px 14px" : "14px 16px 16px" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 8, flexWrap: "wrap" }}>
          <span style={{ color: "var(--text)", fontSize: compact ? 13.5 : 14, fontWeight: 500, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
            {board.title}
          </span>
          {!board.is_published && (
            <Chip>
              <span className="dot" />
              {t("draft")}
            </Chip>
          )}
          {board.is_published && revoked && <Chip>{t("linkRevoked")}</Chip>}
          {board.has_pin && <Chip variant="accent">PIN</Chip>}
        </div>
        {!compact && (
          <div style={{ color: "var(--faint)", fontSize: 12.5, marginBottom: 10, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
            {board.client_name ?? t("noClient")}
          </div>
        )}
        <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
          <Chip>
            <Icon name="image" size={11} />
            {board.works_count}
          </Chip>
          <Chip>
            <Icon name="eye" size={11} />
            {board.has_links ? board.views_count : "—"}
          </Chip>
          <span style={{ marginLeft: "auto", color: "var(--faint)", fontSize: 11.5 }}>
            {formatDate(board.updated_at, locale)}
          </span>
        </div>
      </div>
    </Link>
  );
}
