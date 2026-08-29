import { useCallback, useEffect, useLayoutEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";

import { Icon } from "../components/Icon";
import { VyborKlienta } from "../components/VyborKlienta";
import { Avatar, EmptyState, LoadFailed, Modal, ScreenLoading } from "../components/ui";
import { api, ApiError } from "../lib/api";
import { useApp } from "../lib/app";
import { useFailure } from "../lib/failure";
import { useGuard } from "../lib/guard";
import { formatDate, formatMoney, initials } from "../lib/format";
import { useReference } from "../lib/reference";
import { term } from "../lib/terms";

/** Ширина, ниже которой доска перестаёт быть доской.
 *
 *  Две колонки минимальной ширины плюс промежуток — это 2×232 + 12 = 476px;
 *  520 — то же самое с запасом на полосу прокрутки под доской. Меньше двух
 *  колонок сразу видеть нельзя, а значит нельзя и перетащить карточку из одной
 *  в другую: пришлось бы тащить «вслепую», с автопрокруткой, которой нет.
 *  Поэтому ниже этого порога доска заменяется списком с выбором этапа.
 */
const BOARD_MIN_WIDTH = 520;

/** Запас на возврат в режим доски.
 *
 *  Без него на пограничной ширине режимы мигают: список и доска дают разную
 *  высоту содержимого, из-за неё появляется или исчезает вертикальная полоса
 *  прокрутки, ширина контейнера меняется на её толщину — и переключение
 *  зацикливается.
 */
const MODE_HYSTERESIS = 32;

/** Режим доски по ширине КОНТЕЙНЕРА, а не окна.
 *
 *  Ширина окна тут ничего не решает: сайдбар забирает 240px, окно можно
 *  раскрыть на половину монитора — и на «настольных» 1280px под доску остаётся
 *  столько же места, сколько на планшете. Медиазапрос этого не увидит, потому
 *  что смотрит на окно.
 *
 *  Почему ResizeObserver, а не контейнерные запросы (@container): CSS умеет
 *  только переоформить то, что уже отрисовано, а здесь меняется само
 *  взаимодействие — вместо перетаскивания выбор этапа списком. Это другая
 *  разметка и другие обработчики событий, то есть решение принимает React.
 *  Оформление, которому хватает CSS (растягивание колонок по свободной
 *  ширине), в JS не поднимаем — оно осталось во flex-раскладке `.kanban-col`.
 */
function useNarrowBoard(node: HTMLElement | null) {
  const [narrow, setNarrow] = useState(false);
  useLayoutEffect(() => {
    if (!node) return;
    const decide = (width: number) =>
      setNarrow((was) => (was ? width < BOARD_MIN_WIDTH + MODE_HYSTERESIS : width < BOARD_MIN_WIDTH));
    // Первое измерение — сразу, до первой отрисовки. ResizeObserver сообщает о
    // начальном размере тоже, но не раньше следующего кадра, и на телефоне
    // доска успевала мигнуть перед тем, как смениться списком.
    decide(node.getBoundingClientRect().width);
    const observer = new ResizeObserver((entries) => decide(entries[0]?.contentRect.width ?? 0));
    observer.observe(node);
    return () => observer.disconnect();
  }, [node]);
  return narrow;
}


/** Названия этапов приходят с сервера: у ремонта техники «диагностика», у
 *  салона «клиент пришёл», у магазина «отправлен» — общего списка не бывает.
 *  Здесь их не переводим и не подменяем: это слова конкретного бизнеса. */
type Deal = {
  id: number;
  title: string;
  client_id: number;
  client_name: string | null;
  manager_id: number | null;
  manager_name: string | null;
  manager_avatar: string | null;
  stage: string;
  sort_order: number;
  due_at: string | null;
  lost_reason: string;
  /** Деньги в минимальных единицах. null — сумму ещё не называли; это не ноль:
   *  ноль означает «работа бесплатная», и в отчёте они считаются по-разному. */
  amount: number | null;
  prepaid: number;
  remainder: number | null;
  is_paid: boolean;
};

type Column = {
  key: string;
  name: string;
  kind: "open" | "won" | "lost";
  color: string;
  /** Сумма по всем сделкам этапа — считает сервер: колонка отдаётся с
   *  пределом, и сложение показанных карточек занижало бы итог. */
  /** null — у смотрящего нет права `deals.view_amounts`. */
  amount_total: number | null;
  deals: Deal[];
};

export function Deals() {
  const { t, locale, workspace, toastError } = useApp();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const [columns, setColumns] = useState<Column[] | null>(null);
  // Незакрытые заявки — числом с сервера, а не сложением карточек. Подробности
  // у запроса ниже.
  const [openTotal, setOpenTotal] = useState(0);
  const [dragId, setDragId] = useState<number | null>(null);
  const [overStage, setOverStage] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const guard = useGuard();
  // Элемент, по ширине которого выбирается режим. Ref через состояние, а не
  // useRef: наблюдатель должен подключиться в тот момент, когда узел появился.
  const [frame, setFrame] = useState<HTMLDivElement | null>(null);
  const narrow = useNarrowBoard(frame);
  // Валюта одна на систему и приходит вместе с доской: настройки читает
  // только root, а суммы видят все.
  const [currency, setCurrency] = useState("USD");
  const [draft, setDraft] = useState({
    title: "",
    client_id: "",
    manager_id: "",
    due_at: "",
    description: "",
  });

  // Справочники окна «новая заявка». Через общий крючок, а не своим `catch`:
  // пустой список клиентов означал бы «клиентов не завели» и подсовывал бы
  // подсказку «сначала заведите клиента» ровно там, где их двести, а не
  // ответил сервер.
  // Имя выбранного держим рядом с черновиком: поле показывает его, а на
  // сервер уходит только номер.
  const [imya_klienta, setImyaKlienta] = useState("");
  const people = useReference<any>("/people");

  const { failure, fail, clear } = useFailure();

  const load = useCallback(async () => {
    clear();
    try {
      const [board, open] = await Promise.all([
        api.get("/deals/board"),
        // «В работе» считает сервер тем же отбором, что и воронка: закрытые
        // этапы в счёт не идут (`include_closed=false`). Складывать карточки
        // на экране нельзя дважды: колонка приходит с пределом в 200, а
        // выигранные и проигранные к работе не относятся вовсе. Ошибка была
        // тихая — число есть, оно правдоподобное, и разойтись с ним можно
        // только вручную пересчитав.
        api.get("/deals?include_closed=false&per_page=1"),
      ]);
      setColumns(board.columns);
      setCurrency(board.currency);
      setOpenTotal(open.total);
    } catch (e) {
      fail(e);
    }
  }, [fail, clear]);

  useEffect(() => {
    void load();
  }, [load]);

  if (!columns) return <ScreenLoading error={failure} onRetry={() => void load()} />;

  // Этап из адреса: по ссылке с дашборда и из отчёта приходят в конкретный
  // столбец воронки, а не «куда-то на доску». Экран этих параметров не читал
  // вовсе, и обе ссылки открывали доску целиком — то есть молча делали не то,
  // что обещали.
  //
  // Этап мог с тех пор исчезнуть из воронки: тогда показываем всё, а не пустую
  // доску — пустая читается как «заявок нет», а их отобрали не тем ключом.
  const wanted = params.get("stage") ?? "all";
  const stageFilter = columns.some((c) => c.key === wanted) ? wanted : "all";
  const setStageFilter = (key: string) => {
    if (key === "all") params.delete("stage");
    else params.set("stage", key);
    setParams(params, { replace: true });
  };

  const anyDeals = columns.some((c) => c.deals.length > 0);

  // Смена этапа — одна на оба режима: на широком экране её вызывает
  // перетаскивание, на узком — выбор из списка. Сервер и откат при ошибке
  // общие, иначе два способа переезда сделки расходились бы в поведении.
  const move = async (id: number, stage: string) => {
    const from = columns.find((c) => c.deals.some((d) => d.id === id));
    if (!from || from.key === stage) return;

    // Двигаем карточку сразу, не дожидаясь сервера: перетаскивание, которое
    // «думает» полсекунды, ощущается сломанным. При ошибке вернём как было.
    const before = columns;
    setColumns((prev) =>
      (prev ?? []).map((c) => {
        if (c.key === from.key) return { ...c, deals: c.deals.filter((d) => d.id !== id) };
        if (c.key === stage) {
          const moved = from.deals.find((d) => d.id === id);
          return moved ? { ...c, deals: [...c.deals, { ...moved, stage }] } : c;
        }
        return c;
      }),
    );
    try {
      await api.post(`/deals/${id}/move`, { stage });
      void load();
    } catch (e) {
      toastError(e);
      // Заявку передвинул кто-то другой, пока карточка была у нас на экране.
      // Возвращать её на прежнее место нельзя: это позапрошлое положение, и
      // человек снова потянет её из колонки, где её уже нет. Спрашиваем доску.
      if (e instanceof ApiError && e.code === "stage_moved_meanwhile") void load();
      else setColumns(before);
    }
  };

  const drop = (stage: string) => {
    const id = dragId;
    setDragId(null);
    setOverStage(null);
    if (id) void move(id, stage);
  };

  const create = async () => {
    // Кнопка не отвечает мгновенно — сервер заводит запись и отдаёт номер. Без
    // засова второе нажатие по «неответившей» кнопке заводило вторую заявку, и
    // человек попадал в одну из двух, не зная о существовании другой.
    if (!draft.title.trim() || !draft.client_id || !guard.take()) return;
    try {
      const deal = await api.post("/deals", {
        title: draft.title.trim(),
        client_id: Number(draft.client_id),
        manager_id: draft.manager_id ? Number(draft.manager_id) : null,
        due_at: draft.due_at ? `${draft.due_at}T12:00:00` : null,
        description: draft.description.trim(),
      });
      setCreating(false);
      setDraft({ title: "", client_id: "", manager_id: "", due_at: "", description: "" });
      navigate(`/deals/${deal.id}`);
    } catch (e) {
      toastError(e);
      // Засов снимаем только на отказе: при успехе экран уже уезжает в
      // карточку, и трогать состояние закрывающегося окна незачем.
      guard.free();
    }
  };

  /** Содержимое карточки — одно на оба режима.
   *
   *  На доске карточка сама себе кнопка и перетаскивается, в списке она стоит
   *  рядом с выбором этапа. Разной начинку делать нельзя: сделка, у которой на
   *  телефоне пропала сумма или ответственный, — это уже другой экран, а не тот
   *  же в другой раскладке. */
  const cardBody = (deal: Deal, kind: Column["kind"]) => {
    const overdue = deal.due_at && kind === "open" && new Date(deal.due_at) < new Date();
    return (
      <>
        <span className="deal-title">{deal.title}</span>
        {deal.amount !== null && (
          <span className={"deal-money" + (deal.is_paid ? " paid" : "")}>
            {formatMoney(deal.amount, currency, locale)}
            {deal.prepaid > 0 && !deal.is_paid && (
              <span className="deal-owed">
                {" · "}
                {formatMoney(deal.remainder, currency, locale)}
              </span>
            )}
          </span>
        )}
        {deal.client_name && <span className="deal-client">{deal.client_name}</span>}
        <span className="deal-foot">
          {/* Кто ведёт — первое, что спрашивают у доски. Без ответственного
              показываем это явно, а не пустотой: ничейная сделка и есть
              проблема. */}
          {deal.manager_id ? (
            <span className="deal-who" title={deal.manager_name ?? ""}>
              <Avatar
                small
                text={initials(deal.manager_name ?? "?")}
                src={deal.manager_avatar ?? undefined}
              />
              {deal.manager_name}
            </span>
          ) : (
            <span className="deal-nobody">{t("nobody")}</span>
          )}
          {deal.due_at && (
            <span className={"deal-due" + (overdue ? " overdue" : "")}>
              <Icon name="clock" size={11} />
              {formatDate(deal.due_at, locale)}
            </span>
          )}
        </span>
        {deal.lost_reason && <span className="deal-lost">{deal.lost_reason}</span>}
      </>
    );
  };

  // В списке пустые этапы не показываем: на телефоне заголовок «0 сделок»
  // отодвигает вниз то, ради чего экран открыли. Добраться до пустого этапа
  // всё равно можно — он есть в фильтре, вместе со своим счётчиком.
  const listed =
    stageFilter === "all"
      ? columns.filter((c) => c.deals.length > 0)
      : columns.filter((c) => c.key === stageFilter);

  return (
    <div className="page page-board">
      <div className="page-head">
        <div>
          <h1 className="page-title">{term(workspace.deal_term, locale, "many")}</h1>
          <div className="page-sub">{t("dealsSub", { total: openTotal })}</div>
        </div>
        <button className="btn btn-primary" onClick={() => setCreating(true)}>
          <Icon name="plus" stroke={2} />
          {term(workspace.deal_term, locale, "new")}
        </button>
      </div>

      {!anyDeals ? (
        <EmptyState
          title={term(workspace.deal_term, locale, "none")}
          sub={term(workspace.deal_term, locale, "noneHint")}
        />
      ) : (
        // Обёртка нужна и как измеряемый элемент: её ширина не зависит от
        // выбранного режима (доска прокручивается внутри себя), поэтому
        // переключение не может сдвинуть то, по чему оно принимается.
        <div className="deals-frame" ref={setFrame}>
          {narrow ? (
            <>
              <div className="stage-filter">
                <select
                  className="input"
                  aria-label={t("stagePick")}
                  value={stageFilter}
                  onChange={(e) => setStageFilter(e.target.value)}
                >
                  <option value="all">{t("allStages")}</option>
                  {columns.map((c) => (
                    <option key={c.key} value={c.key}>
                      {c.name} · {c.deals.length}
                    </option>
                  ))}
                </select>
              </div>
              <div className="deal-list">
                {listed.map((column) => (
                  <div key={column.key} className={"deal-group kanban-" + column.kind}>
                    <div className="kanban-head deal-group-head">
                      {/* Название пришло из воронки этого бизнеса — как есть */}
                      <span style={column.color ? { color: column.color } : undefined}>
                        {column.name}
                      </span>
                      <span className="kanban-count">{column.deals.length}</span>
                      {/* Сумма по этапу остаётся на виду и в списке: на
                          телефоне смотрят те же деньги, что и на мониторе. */}
                      {!!column.amount_total && column.amount_total > 0 && (
                        <span className="deal-group-money">
                          {formatMoney(column.amount_total, currency, locale)}
                        </span>
                      )}
                    </div>
                    {column.deals.length === 0 && (
                      <div className="kanban-empty">{t("noDealsInStage")}</div>
                    )}
                    {column.deals.map((deal) => (
                      <div key={deal.id} className="deal-row">
                        <button className="deal-card" onClick={() => navigate(`/deals/${deal.id}`)}>
                          {cardBody(deal, column.kind)}
                        </button>
                        {/* Этап меняется выбором, а не перетаскиванием: на
                            узком экране горизонтальный жест спорит с прокруткой
                            страницы, и перетаскивание там просто не работает. */}
                        <select
                          className="deal-row-stage"
                          aria-label={t("stagePick")}
                          value={deal.stage}
                          onChange={(e) => void move(deal.id, e.target.value)}
                        >
                          {columns.map((c) => (
                            <option key={c.key} value={c.key}>
                              {c.name}
                            </option>
                          ))}
                        </select>
                      </div>
                    ))}
                  </div>
                ))}
              </div>
            </>
          ) : (
            <>
              {/* На широком экране фильтра нет — доску смотрят целиком. Но
                  пришедшего по ссылке с дашборда надо и вернуть обратно ко
                  всем этапам, иначе отобранная доска выглядит как поломка. */}
              {stageFilter !== "all" && (
                <div className="stage-filter">
                  <button className="filter-chip" onClick={() => setStageFilter("all")}>
                    {t("allStages")}
                  </button>
                  <span className="filter-chip active">
                    {columns.find((c) => c.key === stageFilter)?.name}
                  </span>
                </div>
              )}
              <div className="kanban">
              {(stageFilter === "all"
                ? columns
                : columns.filter((c) => c.key === stageFilter)
              ).map((column) => (
                <div
                  key={column.key}
                  className={
                    "kanban-col kanban-" + column.kind + (overStage === column.key ? " over" : "")
                  }
                  onDragOver={(e) => {
                    e.preventDefault();
                    setOverStage(column.key);
                  }}
                  onDragLeave={() => setOverStage((s) => (s === column.key ? null : s))}
                  onDrop={() => drop(column.key)}
                >
                  <div className="kanban-head">
                    {/* Название пришло из воронки этого бизнеса — как есть */}
                    <span style={column.color ? { color: column.color } : undefined}>
                      {column.name}
                    </span>
                    <span className="kanban-count">{column.deals.length}</span>
                  </div>
                  {/* Сумма по колонке: малый бизнес смотрит на деньги, а не на
                      количество карточек. Ноль не показываем — пустая строка
                      честнее нуля, которого никто не называл. */}
                  {!!column.amount_total && column.amount_total > 0 && (
                    <div className="kanban-money">
                      {formatMoney(column.amount_total, currency, locale)}
                    </div>
                  )}
                  <div className="kanban-body">
                    {column.deals.length === 0 && (
                      <div className="kanban-empty">{t("dragHere")}</div>
                    )}
                    {column.deals.map((deal) => (
                      <button
                        key={deal.id}
                        className={"deal-card" + (dragId === deal.id ? " dragging" : "")}
                        draggable
                        onDragStart={() => setDragId(deal.id)}
                        onDragEnd={() => setDragId(null)}
                        onClick={() => navigate(`/deals/${deal.id}`)}
                      >
                        {cardBody(deal, column.kind)}
                      </button>
                    ))}
                  </div>
                </div>
              ))}
              </div>
            </>
          )}
        </div>
      )}

      {creating && (
        <Modal title={t("newDeal")} onClose={() => setCreating(false)}>
          <div className="field">
            <label className="label">{t("dealTitle")}</label>
            <input
              className="input"
              autoFocus
              value={draft.title}
              onChange={(e) => setDraft({ ...draft, title: e.target.value })}
            />
          </div>
          <div className="deal-fields">
            <div className="field">
              <label className="label">{t("client")}</label>
              <VyborKlienta
                value={draft.client_id ? Number(draft.client_id) : null}
                imya={imya_klienta || null}
                pustoy
                netVovse={t("noClientsForDeal")}
                onPick={(kto, imya) => {
                  setImyaKlienta(imya ?? "");
                  setDraft({ ...draft, client_id: kto ? String(kto) : "" });
                }}
              />
            </div>
            {/* Ответственный и срок — прямо при заведении. Проставлять их потом
                по одной сделке никто не будет, и доска зарастает ничейными. */}
            <div className="field">
              <label className="label">{t("responsible")}</label>
              <select
                className="input"
                value={draft.manager_id}
                onChange={(e) => setDraft({ ...draft, manager_id: e.target.value })}
              >
                <option value="">{t("nobody")}</option>
                {(people.items ?? []).map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                  </option>
                ))}
              </select>
              {/* Список сотрудников не приехал — заявка заведётся ничейной и
                  никто об этом не узнает. Говорим и даём повторить. */}
              {people.failure !== null && (
                <LoadFailed error={people.failure} onRetry={people.reload} />
              )}
            </div>
            <div className="field">
              <label className="label">{t("dueDate")}</label>
              <input
                className="input"
                type="date"
                value={draft.due_at}
                onChange={(e) => setDraft({ ...draft, due_at: e.target.value })}
              />
            </div>
          </div>
          <div className="field">
            <label className="label">{t("dealDetails")}</label>
            <textarea
              className="input"
              rows={3}
              placeholder={t("dealDetailsHint")}
              value={draft.description}
              onChange={(e) => setDraft({ ...draft, description: e.target.value })}
            />
          </div>
          <button
            className="btn btn-primary"
            style={{ width: "100%" }}
            disabled={guard.busy || !draft.title.trim() || !draft.client_id}
            onClick={() => void create()}
          >
            {t("create")}
          </button>
        </Modal>
      )}
    </div>
  );
}
