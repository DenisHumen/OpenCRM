import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";

import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { PALETTE_DELAY, useDebounced } from "../lib/debounce";
import { useGuard } from "../lib/guard";
import { initials, relativeDay } from "../lib/format";
import { type Gated, shown } from "../lib/modules";
import { term } from "../lib/terms";
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

/**
 * Дозагруженные страницы одной группы — то, что дописала «показать ещё».
 *
 * Заведено по беде: палитра показывала шесть находок и на этом кончалась.
 * Досок под «be» было больше шести, и седьмую нельзя было увидеть ничем — ни
 * прокруткой, ни кнопкой. Сервер честно присылал `has_more: true`, а палитра
 * этот ключ не читала вовсе.
 */
interface Dozagruzka {
  /** Строки СВЕРХ первой страницы: она приходит в общем ответе и живёт в `data`. */
  items: any[];
  hasMore: boolean;
  /**
   * Какую страницу просить дальше.
   *
   * Считаем страницы, а не строки: размер страницы — дело сервера, и знай его
   * палитра вторым экземпляром, первое же расхождение дало бы пропущенные или
   * задвоенные находки, причём молча.
   */
  next: number;
  loading: boolean;
}

export function CommandPalette({ onClose }: { onClose: () => void }) {
  const { t, locale, modules, workspace, toastError } = useApp();
  const navigate = useNavigate();
  const [query, setQuery] = useState("");
  const [data, setData] = useState<any>(null);
  const [more, setMore] = useState<Record<string, Dozagruzka>>({});
  const [active, setActive] = useState(0);
  const [loading, setLoading] = useState(true);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  // Засов на дозагрузку — по одному на группу. Довод тот же, что у «новой
  // доски» ниже: «показать ещё» это обычная строка палитры, и Enter по ней
  // нажимают дважды. Состояния React для этого мало — оно меняется только к
  // следующему рендеру, а оба нажатия читают своё замыкание (см. `useGuard`).
  const paging = useRef(new Set<string>());
  // Поколение запроса. Набор идёт быстрее сети, и ответ «показать ещё» по
  // прежнему слову обязан быть выброшен, а не дописан к находкам по новому.
  const pokolenie = useRef(0);
  // Засов «Новой доски» — общий крючок, а не своя копия того же ref. Ref здесь
  // обязателен вдвойне: строки палитры пересобираются на каждую набранную
  // букву, и всё, что живёт в замыкании строки, к следующему нажатию уже
  // другое. Без засова Enter, нажатый дважды (а его нажимают дважды), заводил
  // два черновика — человек уходил в один, второй оставался.
  const boardGuard = useGuard();

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
    // Новое слово — новая выдача: дозагруженное по прежнему слову выбрасываем
    // целиком, иначе к находкам по «be» дописались бы находки по «bel».
    pokolenie.current += 1;
    paging.current.clear();
    setMore({});
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

  /**
   * Дописать к группе следующую страницу находок.
   *
   * Номер страницы приходит снаружи — его считает сама группа по тому, что уже
   * показано. Палитра при этом не знает, сколько строк на странице: она просит
   * СЛЕДУЮЩИЙ номер, а размер страницы остаётся делом сервера.
   */
  const loadMore = useCallback(
    async (area: string, page: number) => {
      if (paging.current.has(area)) return;
      paging.current.add(area);
      const moyo = pokolenie.current;
      const bylo = (prev: Record<string, Dozagruzka>): Dozagruzka =>
        prev[area] ?? { items: [], hasMore: true, next: page, loading: false };
      setMore((prev) => ({ ...prev, [area]: { ...bylo(prev), loading: true } }));
      try {
        const chunk: any = await api.get(
          `/search/${area}?q=${encodeURIComponent(typed)}&page=${page}`,
        );
        if (pokolenie.current !== moyo) return;
        setMore((prev) => ({
          ...prev,
          [area]: {
            items: [...(prev[area]?.items ?? []), ...(chunk?.items ?? [])],
            hasMore: Boolean(chunk?.has_more),
            next: page + 1,
            loading: false,
          },
        }));
      } catch (e) {
        if (pokolenie.current !== moyo) return;
        toastError(e);
        // «Показать ещё» остаётся на месте: отказ сети — повод повторить, а не
        // повод молча оборвать выдачу на том же месте, где она обрывалась
        // раньше.
        setMore((prev) => ({
          ...prev,
          [area]: { ...bylo(prev), hasMore: true, loading: false },
        }));
      } finally {
        paging.current.delete(area);
      }
    },
    [typed, toastError],
  );

  const rows = useMemo<Row[]>(() => {
    // `needle`, а не `term`: словом `term` называется общий подбор названия для
    // основной записи (`lib/terms.ts`), и заголовок группы заявок берётся
    // оттуда же — двум разным вещам одно имя здесь уже стоило сборки.
    const needle = query.trim().toLowerCase();
    const dealLabel = term(workspace.deal_term, locale, "many");

    /**
     * Что группа показывает сейчас: первая страница из общего ответа плюс всё,
     * что дописала «показать ещё».
     *
     * `hasMore` берётся у дозагрузки, если она была, и у первой страницы, если
     * не было: последнее слово всегда за последним ответом сервера. Пока этот
     * ключ не читал никто, палитра обрывала выдачу на шести находках и об
     * остальных не сообщала ничем.
     */
    const chast = (area: string) => {
      const base = data?.[area];
      const dop = more[area];
      const pervaya: any[] = base?.items ?? [];
      return {
        items: dop ? [...pervaya, ...dop.items] : pervaya,
        hasMore: dop ? dop.hasMore : Boolean(base?.has_more),
        next: dop?.next ?? 2,
        loading: Boolean(dop?.loading),
      };
    };

    // Группы выдачи — списком, как пункты меню: у группы есть необязательный
    // `module`, и выключенный блок уходит из поиска тем же правилом, каким
    // уходит из левой колонки. Раньше доски искались всегда, и выключенный
    // блок продолжал предлагать переходы в раздел, которого в меню уже нет.
    //
    // Сервер выключенный блок в выдачу тоже не кладёт; условие здесь не дубль,
    // а защита от разъезда: ответ мог прийти до того, как блок выключили.
    //
    // `area` — ключ группы в ответе сервера и он же кусок адреса продолжения
    // (`/search/<area>`). Одно поле на оба употребления намеренно: разъедься
    // они — и «показать ещё» дозагружало бы не ту группу, к которой пристроено.
    const groups = shown<Gated & { area: string; label: string; rows: Row[] }>(modules, [
      {
        area: "clients",
        label: t("clients"),
        rows: chast("clients").items.map((client: any) => ({
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
        // Заявка — стержень системы, и в палитре её ищут чаще прочего: группа
        // стоит сразу за клиентами, как и пункт в левой колонке.
        module: "deals",
        area: "deals",
        // Слово берём то же, каким подписан раздел: у мастерской это «Заявки»,
        // у магазина «Заказы», и заголовок группы обязан совпадать с меню,
        // иначе человек ищет заказы и находит заголовок «Сделки».
        label: dealLabel,
        rows: chast("deals").items.map((deal: any) => ({
          key: `deal-${deal.id}`,
          group: dealLabel,
          icon: (
            <div className="cp-action-icon">
              <Icon name="deals" size={15} />
            </div>
          ),
          title: deal.title,
          subtitle: deal.client_name ?? undefined,
          // Сумму не показываем: без права её всё равно нет в ответе, а с
          // правом она ничего не добавляет к выбору строки — от палитры ждут
          // «открой вот эту», а не сводку.
          meta: <span className="cp-meta">{relativeDay(deal.updated_at, locale)}</span>,
          run: () => go(`/deals/${deal.id}`),
        })),
      },
      {
        module: "boards",
        area: "boards",
        label: t("boards"),
        rows: chast("boards").items.map((board: any) => ({
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
          if (!boardGuard.take()) return;
          try {
            const board = await api.post("/boards", { title: t("newBoard") });
            go(`/boards/${board.id}`);
          } catch (e) {
            toastError(e);
            // Засов снимаем только на отказе: при успехе палитра уже закрыта.
            boardGuard.free();
          }
        },
      },
    ]);

    return [
      ...groups.flatMap((group) => {
        const state = chast(group.area);
        if (!state.hasMore) return group.rows;
        // «Показать ещё» — такая же строка списка, как находка: её видно
        // стрелками и открывает Enter. Отдельной кнопкой внизу окна она была бы
        // недостижима с клавиатуры, а палитрой пользуются именно с неё.
        //
        // Строка стоит ВНУТРИ группы, под её находками, потому что кончаются
        // группы порознь: доски могут листаться дальше, когда клиенты уже
        // показаны все.
        return [
          ...group.rows,
          {
            key: `more-${group.area}`,
            group: group.label,
            icon: (
              <div className="cp-action-icon">
                <Icon name="chevronDown" size={15} />
              </div>
            ),
            title: state.loading ? t("loading") : t("showMore"),
            run: () => loadMore(group.area, state.next),
          },
        ];
      }),
      ...actions
        .filter((action) => !needle || action.match.some((m) => m.toLowerCase().includes(needle)))
        .map((action) => ({
          key: action.key,
          group: t("actions"),
          icon: <div className="cp-action-icon">{action.icon}</div>,
          title: action.title,
          run: action.run,
        })),
    ];
  }, [data, more, query, t, locale, modules, workspace, go, loadMore, toastError]);

  // Новый запрос — выделение с начала списка.
  useEffect(() => {
    setActive(0);
  }, [typed]);

  // Список стал короче — выделение не должно указывать за край.
  //
  // Раньше здесь стоял безусловный сброс на нуль по изменению длины, и с
  // «показать ещё» он оборачивался бы бедой: список РАСТЁТ, и человека
  // отбрасывало бы в начало ровно в тот миг, когда он долистал до конца.
  useEffect(() => {
    setActive((i) => (i < rows.length ? i : 0));
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
