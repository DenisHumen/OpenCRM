import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { BoardCard } from "../components/BoardCard";
import { Icon } from "../components/Icon";
import { StorageCard } from "../components/StorageCard";
import { Avatar, Chip, EmptyState, ScreenLoading } from "../components/ui";
import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { formatDateTime, formatMoney, initials, relativeDay } from "../lib/format";

export function Dashboard() {
  const { user, t, locale, storage, refreshStorage, toastError } = useApp();
  const navigate = useNavigate();
  const [data, setData] = useState<any>(null);

  useEffect(() => {
    api.get("/dashboard").then(setData).catch(toastError);
  }, [toastError]);

  if (!data) return <ScreenLoading />;

  const hour = new Date().getHours();
  const greeting = hour < 12 ? t("goodMorning") : hour < 18 ? t("goodAfternoon") : t("goodEvening");
  const growth =
    data.views_prev_7d > 0
      ? Math.round(((data.views_7d - data.views_prev_7d) / data.views_prev_7d) * 100)
      : null;
  const maxDay = Math.max(1, ...data.views_by_day.map((d: any) => d.count));
  const dayLabels = data.views_by_day.map((d: any) =>
    new Date(d.date + "T00:00:00").toLocaleDateString(locale === "ru" ? "ru-RU" : "en-US", { weekday: "short" }),
  );

  return (
    <div className="page">
      <div className="page-head" style={{ marginBottom: 26 }}>
        <h1 className="page-title">
          {greeting}, {user?.name}
        </h1>
        <div style={{ display: "flex", gap: 10 }}>
          <Link to="/clients?new=1" className="btn btn-secondary">
            <Icon name="userPlus" />
            {t("newClient")}
          </Link>
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
      </div>

      <div className="metric-grid">
        {/* Деньги первыми: владелец открывает сводку ради них, а не ради
            количества карточек. «Закрыто» считаем с начала месяца, а не за
            последние 30 дней — иначе число не сходится с месячной отчётностью. */}
        <div className="card card-pad">
          <div className="metric-title" style={{ marginBottom: 14 }}>
            {t("moneyInWork")}
          </div>
          <div className="metric-value money-value">
            {formatMoney(data.money_in_work, data.currency, locale)}
          </div>
          <div className="metric-sub">
            {t("moneyWonThisMonth")}:{" "}
            {formatMoney(data.money_won_this_month, data.currency, locale)}
          </div>
        </div>
        <div className="card card-pad">
          <div className="metric-title" style={{ marginBottom: 14 }}>
            {t("metricClients")}
          </div>
          <div className="metric-value">{data.clients_total}</div>
          <div className="metric-sub">{t("addedThisMonth", { n: data.clients_this_month })}</div>
        </div>
        <div className="card card-pad">
          <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 14 }}>
            <div className="metric-title">{t("metricBoards")}</div>
            {data.boards_total - data.boards_published > 0 && (
              <Chip>
                <span className="dot" />
                {data.boards_total - data.boards_published} {t("drafts")}
              </Chip>
            )}
          </div>
          <div className="metric-value">{data.boards_published}</div>
          <div className="metric-sub">{t("ofTotal", { n: data.boards_total })}</div>
        </div>
        <div className="card card-pad">
          <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 14 }}>
            <div className="metric-title">{t("metricViews")}</div>
            {growth !== null && (
              <Chip variant={growth >= 0 ? "success" : "warning"}>
                {growth >= 0 ? "+" : ""}
                {growth}%
              </Chip>
            )}
          </div>
          <div className="metric-value">{data.views_7d}</div>
          <div className="metric-sub">
            {data.last_view_at ? t("lastView", { t: formatDateTime(data.last_view_at, locale) }) : t("noViewsYet")}
          </div>
        </div>
      </div>

      <div className="card" style={{ padding: "18px 20px", display: "flex", alignItems: "flex-end", justifyContent: "space-between", gap: 28, marginBottom: 28 }}>
        {/* Два числа рядом: сколько раз открывали и сколько людей открывало.
            Подпись у второго объясняет разницу — иначе «просмотров 108, а
            посетителей 3» читается как ошибка. */}
        <div style={{ display: "flex", gap: 40, flexWrap: "wrap" }}>
          <div>
            <div className="metric-title" style={{ marginBottom: 14 }}>
              {t("showcaseViews")}
            </div>
            <div className="metric-value">{data.views_7d}</div>
            <div className="metric-sub">
              {t("last7days")}
              {growth !== null && (
                <>
                  {" · "}
                  <span style={{ color: growth >= 0 ? "var(--success)" : "var(--warning)" }}>
                    {growth >= 0 ? "+" : ""}
                    {growth}%
                  </span>{" "}
                  {t("vsPrevWeek")}
                </>
              )}
            </div>
          </div>
          <div>
            <div className="metric-title" style={{ marginBottom: 14 }}>
              {t("uniqueViewersTitle")}
            </div>
            <div className="metric-value">{data.unique_viewers_7d ?? 0}</div>
            <div className="metric-sub">{t("uniqueViewersHint")}</div>
          </div>
        </div>
        <div className="bars">
          {data.views_by_day.map((d: any, i: number) => (
            <div className="bar-col" key={d.date}>
              <div
                className={"bar" + (d.count === maxDay && d.count > 0 ? " top" : "")}
                style={{ height: Math.max(4, Math.round((d.count / maxDay) * 52)) }}
                title={`${d.count}`}
              />
              <span className="bar-label">{dayLabels[i]}</span>
            </div>
          ))}
        </div>
      </div>

      {storage && (
        <div style={{ marginBottom: 28 }}>
          <StorageCard storage={storage} onPurged={() => void refreshStorage()} />
        </div>
      )}

      <div className="section-head">
        <h2 className="section-title">{t("recentBoards")}</h2>
        <Link to="/boards" className="section-link">
          {t("viewAll")}
        </Link>
      </div>
      {data.recent_boards.length === 0 ? (
        <div className="card" style={{ marginBottom: 28 }}>
          <EmptyState title={t("noBoardsYet")} />
        </div>
      ) : (
        <div className="board-grid board-grid-4" style={{ marginBottom: 28 }}>
          {data.recent_boards.map((board: any) => (
            <BoardCard key={board.id} board={board} compact />
          ))}
        </div>
      )}

      <div className="section-head" style={{ marginBottom: 12 }}>
        <h2 className="section-title">{t("recentClients")}</h2>
        <Link to="/clients" className="section-link">
          {t("allClients")}
        </Link>
      </div>
      <div className="list-card">
        {data.recent_clients.length === 0 && <EmptyState title={t("noClientsYet")} />}
        {data.recent_clients.map((client: any) => (
          <Link to={`/clients/${client.id}`} key={client.id} className="list-row hoverable">
            <Avatar text={initials(client.name)} />
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ color: "var(--text)", fontSize: 13.5, fontWeight: 500 }}>{client.name}</div>
              <div style={{ color: "var(--faint)", fontSize: 12 }}>{client.company}</div>
            </div>
            <div style={{ display: "flex", gap: 6 }}>
              {client.tags.slice(0, 2).map((tag: string) => (
                <Chip key={tag}>{tag}</Chip>
              ))}
            </div>
            <div style={{ color: "var(--faint)", fontSize: 12, width: 150, textAlign: "right", flexShrink: 0 }}>
              {relativeDay(client.updated_at, locale)}
            </div>
          </Link>
        ))}
      </div>
    </div>
  );
}
