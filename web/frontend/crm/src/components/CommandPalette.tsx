import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";

import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { initials, relativeDay } from "../lib/format";
import { Icon } from "./Icon";
import { Avatar, Chip } from "./ui";

interface Row {
  key: string;
  group: string;
  icon: React.ReactNode;
  title: string;
  subtitle?: string;
  meta?: React.ReactNode;
  run: () => void;
}

export function CommandPalette({ onClose }: { onClose: () => void }) {
  const { t, locale, toastError } = useApp();
  const navigate = useNavigate();
  const [query, setQuery] = useState("");
  const [data, setData] = useState<any>(null);
  const [active, setActive] = useState(0);
  const [loading, setLoading] = useState(true);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const debounce = useRef<number>();

  const go = useCallback(
    (path: string) => {
      onClose();
      navigate(path);
    },
    [navigate, onClose],
  );

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  useEffect(() => {
    window.clearTimeout(debounce.current);
    setLoading(true);
    debounce.current = window.setTimeout(async () => {
      try {
        setData(await api.get(`/search?q=${encodeURIComponent(query)}`));
      } catch (e) {
        toastError(e);
      } finally {
        setLoading(false);
      }
    }, 180);
    return () => window.clearTimeout(debounce.current);
  }, [query, toastError]);

  const rows = useMemo<Row[]>(() => {
    const result: Row[] = [];
    const term = query.trim().toLowerCase();

    for (const client of data?.clients?.items ?? []) {
      result.push({
        key: `client-${client.id}`,
        group: t("clients"),
        icon: <Avatar text={initials(client.name)} />,
        title: client.name,
        subtitle: client.company || client.email || client.phone,
        meta: <span className="cp-meta">{relativeDay(client.updated_at, locale)}</span>,
        run: () => go(`/clients/${client.id}`),
      });
    }

    for (const board of data?.boards?.items ?? []) {
      result.push({
        key: `board-${board.id}`,
        group: t("boards"),
        icon: (
          <div className="cp-cover">
            {board.cover ? (
              <img src={board.cover.thumb ?? board.cover.card} alt="" />
            ) : (
              <Icon name="image" size={14} />
            )}
          </div>
        ),
        title: board.title,
        subtitle: board.client_name ?? undefined,
        meta: (
          <span className="cp-meta">
            {!board.is_published && (
              <Chip>
                <span className="dot" />
                {t("draft")}
              </Chip>
            )}
            {board.has_pin && <Chip variant="accent">PIN</Chip>}
            <span>
              {board.works_count} {t("works")}
            </span>
          </span>
        ),
        run: () => go(`/boards/${board.id}`),
      });
    }

    // действия показываем, когда их видно по названию или когда искать ещё нечего
    const actions = [
      {
        key: "action-new-client",
        title: t("newClient"),
        icon: <Icon name="userPlus" size={15} />,
        match: [t("newClient"), "client", "клиент"],
        run: () => go("/clients?new=1"),
      },
      {
        key: "action-new-board",
        title: t("newBoard"),
        icon: <Icon name="plus" size={15} stroke={2} />,
        match: [t("newBoard"), "board", "доск"],
        run: async () => {
          try {
            const board = await api.post("/boards", { title: t("newBoard") });
            go(`/boards/${board.id}`);
          } catch (e) {
            toastError(e);
          }
        },
      },
    ];
    for (const action of actions) {
      const matches = !term || action.match.some((m) => m.toLowerCase().includes(term));
      if (matches) {
        result.push({
          key: action.key,
          group: t("actions"),
          icon: <div className="cp-action-icon">{action.icon}</div>,
          title: action.title,
          run: action.run,
        });
      }
    }
    return result;
  }, [data, query, t, locale, go, toastError]);

  useEffect(() => {
    setActive(0);
  }, [rows.length]);

  useEffect(() => {
    const node = listRef.current?.querySelector<HTMLElement>(`[data-index="${active}"]`);
    node?.scrollIntoView({ block: "nearest" });
  }, [active]);

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Escape") {
      e.preventDefault();
      onClose();
    } else if (e.key === "ArrowDown") {
      e.preventDefault();
      setActive((i) => (rows.length ? (i + 1) % rows.length : 0));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive((i) => (rows.length ? (i - 1 + rows.length) % rows.length : 0));
    } else if (e.key === "Enter") {
      e.preventDefault();
      rows[active]?.run();
    }
  };

  let lastGroup = "";

  return (
    <div
      className="cp-backdrop"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="cp-panel" onKeyDown={onKeyDown}>
        <div className="cp-input-row">
          <Icon name="search" size={16} />
          <input
            ref={inputRef}
            className="cp-input"
            placeholder={t("searchEverything")}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          <span className="kbd">Esc</span>
        </div>
        <div className="cp-list" ref={listRef}>
          {rows.length === 0 && !loading && (
            <div className="cp-empty">
              <div className="empty-title">{query ? t("nothingFound", { q: query }) : t("loading")}</div>
              {query && <div className="empty-sub">{t("tryDifferent")}</div>}
            </div>
          )}
          {rows.map((row, index) => {
            const header = row.group !== lastGroup ? row.group : null;
            lastGroup = row.group;
            return (
              <div key={row.key}>
                {header && <div className="cp-group">{header}</div>}
                <button
                  type="button"
                  data-index={index}
                  className={"cp-row" + (index === active ? " active" : "")}
                  onMouseMove={() => setActive(index)}
                  onClick={() => row.run()}
                >
                  {row.icon}
                  <span className="cp-text">
                    <span className="cp-title">{row.title}</span>
                    {row.subtitle && <span className="cp-sub">{row.subtitle}</span>}
                  </span>
                  {row.meta}
                </button>
              </div>
            );
          })}
        </div>
        <div className="cp-footer">
          <span>
            <span className="kbd">↑</span>
            <span className="kbd">↓</span> {t("hintNavigate")}
          </span>
          <span>
            <span className="kbd">↵</span> {t("hintOpen")}
          </span>
        </div>
      </div>
    </div>
  );
}
