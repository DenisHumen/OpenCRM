import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";

import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { PALETTE_DELAY, useDebounced } from "../lib/debounce";
import { initials, relativeDay } from "../lib/format";
import { type Gated, shown } from "../lib/modules";
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

/** Действие палитры. `module` — блок, без которого действие бессмысленно. */
type Action = Gated & {
  key: string;
  title: string;
  icon: React.ReactNode;
  /** Слова, по которым действие находится; пустой запрос показывает все. */
  match: string[];
  run: () => void;
};

export function CommandPalette({ onClose }: { onClose: () => void }) {
  const { t, locale, modules, toastError } = useApp();
  const navigate = useNavigate();
  const [query, setQuery] = useState("");
  const [data, setData] = useState<any>(null);
  const [active, setActive] = useState(0);
  const [loading, setLoading] = useState(true);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  // Засов «Новой доски». Ref, а не состояние: строки палитры пересобираются на
  // каждую набранную букву, и всё, что живёт в замыкании строки, к следующему
  // нажатию уже другое. Без засова Enter, нажатый дважды (а его нажимают
  // дважды), заводил два черновика — человек уходил в один, второй оставался.
  const creatingBoard = useRef(false);

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

  const typed = useDebounced(query, PALETTE_DELAY);
  // Набор ещё идёт: запроса с этими буквами пока не было. Раньше это состояние
  // приходилось выставлять руками рядом с таймером — теперь оно просто видно.
  const typing = typed !== query;

  useEffect(() => {
    let current = true;
    setLoading(true);
    api
      .get(`/search?q=${encodeURIComponent(typed)}`)
      .then((found) => {
        if (current) setData(found);
      })
      .catch((e) => {
        if (current) toastError(e);
      })
      .finally(() => {
        if (current) setLoading(false);
      });
    return () => {
      current = false;
    };
  }, [typed, toastError]);

  const rows = useMemo<Row[]>(() => {
    const term = query.trim().toLowerCase();

    // Группы выдачи — списком, как пункты меню: у группы есть необязательный
    // `module`, и выключенный блок уходит из поиска тем же правилом, каким
    // уходит из левой колонки. Раньше доски искались всегда, и выключенный
    // блок продолжал предлагать переходы в раздел, которого в меню уже нет.
    //
    // Сервер выключенный блок в выдачу тоже не кладёт; условие здесь не дубль,
    // а защита от разъезда: ответ мог прийти до того, как блок выключили.
    const groups = shown<Gated & { rows: Row[] }>(modules, [
      {
        rows: (data?.clients?.items ?? []).map((client: any) => ({
          key: `client-${client.id}`,
          group: t("clients"),
          icon: <Avatar text={initials(client.name)} />,
          title: client.name,
          subtitle: client.company || client.email || client.phone,
          meta: <span className="cp-meta">{relativeDay(client.updated_at, locale)}</span>,
          run: () => go(`/clients/${client.id}`),
        })),
      },
      {
        module: "boards",
        rows: (data?.boards?.items ?? []).map((board: any) => ({
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
        })),
      },
    ]);

    // действия показываем, когда их видно по названию или когда искать ещё нечего
    const actions = shown<Action>(modules, [
      {
        key: "action-new-client",
        title: t("newClient"),
        icon: <Icon name="userPlus" size={15} />,
        match: [t("newClient"), "client", "клиент"],
        run: () => go("/clients?new=1"),
      },
      {
        module: "boards",
        key: "action-new-board",
        title: t("newBoard"),
        icon: <Icon name="plus" size={15} stroke={2} />,
        match: [t("newBoard"), "board", "доск"],
        run: async () => {
          if (creatingBoard.current) return;
          creatingBoard.current = true;
          try {
            const board = await api.post("/boards", { title: t("newBoard") });
            go(`/boards/${board.id}`);
          } catch (e) {
            toastError(e);
            // Засов снимаем только на отказе: при успехе палитра уже закрыта.
            creatingBoard.current = false;
          }
        },
      },
    ]);

    return [
      ...groups.flatMap((group) => group.rows),
      ...actions
        .filter((action) => !term || action.match.some((m) => m.toLowerCase().includes(term)))
        .map((action) => ({
          key: action.key,
          group: t("actions"),
          icon: <div className="cp-action-icon">{action.icon}</div>,
          title: action.title,
          run: action.run,
        })),
    ];
  }, [data, query, t, locale, modules, go, toastError]);

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
          {rows.length === 0 && !loading && !typing && (
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
