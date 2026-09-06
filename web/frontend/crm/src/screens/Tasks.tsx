import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { Icon } from "../components/Icon";
import { KartochkaNapominaniya } from "../components/KartochkaNapominaniya";
import { EmptyState, ScreenLoading } from "../components/ui";
import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { useLiveTopic } from "../lib/live";
import { useFailure } from "../lib/failure";
import { useGuard } from "../lib/guard";
import { formatDateTime, parseDate } from "../lib/format";
import {
  VAZHNOSTI,
  VAZHNOST_LABEL,
  VAZHNOST_PO_UMOLCHANIYU,
  Vazhnost,
  srochno,
  vazhnost,
} from "../lib/vazhnost";

/** Списки, которыми пользуются каждый день. Порядок — от срочного к общему. */
const SCOPES = ["overdue", "today", "week", "open", "done"] as const;

const SCOPE_LABEL = {
  overdue: "tasksOverdue",
  today: "tasksToday",
  week: "tasksWeek",
  open: "tasksAll",
  done: "tasksDone",
} as const;

/**
 * Срок из поля ввода в абсолютный момент.
 *
 * `datetime-local` отдаёт «2026-08-10T18:00» без зоны, и `new Date()` читает
 * это как МЕСТНОЕ время — именно так человек его и имел в виду. `toISOString()`
 * переводит в UTC, в котором время и хранится. Отправь строку как есть — и
 * «сегодня до 18:00» станет 18:00 UTC, то есть вечером для одних и ночью для
 * других.
 */
function toInstant(local: string): string | null {
  if (!local) return null;
  const moment = new Date(local);
  return Number.isNaN(moment.getTime()) ? null : moment.toISOString();
}

/** Обратно: абсолютный момент в значение для поля ввода, в местной зоне. */
function toLocalInput(iso: string | null): string {
  const date = parseDate(iso);
  if (!date) return "";
  const shifted = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
  return shifted.toISOString().slice(0, 16);
}

/** Полосы списка по местному дню: в одной ленте «сегодня» терялось между
 *  вчерашним и следующей неделей (владелец, 06.09.2026). */
type Polosa = "srochno" | "overdue" | "today" | "tomorrow" | "later" | "nodue";
const POLOSY: Polosa[] = ["srochno", "overdue", "today", "tomorrow", "later", "nodue"];
const POLOSA_LABEL = {
  srochno: "vazhnostUrgent",
  overdue: "tasksOverdue",
  today: "tasksToday",
  tomorrow: "tasksTomorrow",
  later: "tasksLater",
  nodue: "tasksNoDue",
} as const;

function polosa(task: { due_at: string | null; vazhnost?: string }, now: number): Polosa {
  // Срочное собирается наверх мимо дней. Иначе «срочно, но без срока» падало в
  // самый низ, под «позже», — а сервер как раз ставит важность выше срока.
  if (srochno(task.vazhnost)) return "srochno";
  const at = parseDate(task.due_at);
  if (!at) return "nodue";
  const moment = at.getTime();
  if (moment < now) return "overdue";
  const konetsDnya = new Date(now);
  konetsDnya.setHours(23, 59, 59, 999);
  if (moment <= konetsDnya.getTime()) return "today";
  if (moment <= konetsDnya.getTime() + 86_400_000) return "tomorrow";
  return "later";
}

/** Перенос одним нажатием: «завтра» и «через неделю» — в 10:00 по местному. */
function sdvig(dney: number): string {
  const moment = new Date();
  moment.setDate(moment.getDate() + dney);
  moment.setHours(10, 0, 0, 0);
  return moment.toISOString();
}

export function Tasks() {
  const { t, locale, refreshTasks, toastError } = useApp();
  const [scope, setScope] = useState<(typeof SCOPES)[number]>("open");
  const [items, setItems] = useState<any[] | null>(null);
  const [counts, setCounts] = useState<Record<string, number>>({});
  const [title, setTitle] = useState("");
  const [due, setDue] = useState("");
  const [novaya, setNovaya] = useState<Vazhnost>(VAZHNOST_PO_UMOLCHANIYU);
  const [otkryto, setOtkryto] = useState<number | null>(null);
  const [attempt, setAttempt] = useState(0);
  useLiveTopic("tasks", () => setAttempt((a) => a + 1));
  const guard = useGuard();

  const { failure, fail, clear } = useFailure();

  // Повтор после отказа и обновление после правки идут одним путём: два разных
  // способа перезагрузить один список расходятся в поведении с первой правкой.
  const reload = useCallback(() => setAttempt((n) => n + 1), []);
  const zakryt = useCallback(() => setOtkryto(null), []);

  useEffect(() => {
    // Списки переключают быстрее, чем отвечает сервер: без счётчика ответ по
    // «просроченным» ложился поверх «на неделю», и человек видел не тот
    // список, на который нажал. Приём тот же, что в отчётах и палитре команд.
    let current = true;
    clear();
    Promise.all([api.get(`/tasks?scope=${scope}`), api.get("/tasks/summary")])
      .then(([list, summary]) => {
        if (!current) return;
        setItems(list.items);
        setCounts(summary);
        // Счётчик в меню читает то же число из общего состояния — обновляем
        // вместе со списком, иначе он отстаёт до следующего перехода.
        void refreshTasks();
      })
      .catch((e) => {
        if (current) fail(e);
      });
    return () => {
      current = false;
    };
  }, [scope, attempt, refreshTasks, fail, clear]);

  if (!items) return <ScreenLoading error={failure} onRetry={reload} />;

  const add = async () => {
    const text = title.trim();
    // Напоминание заводят с клавиатуры и Enter нажимают дважды — от нетерпения
    // и просто с руки. Без засова в списке появлялись два одинаковых, и
    // выяснялось это только когда оба напоминали.
    if (!text || !guard.take()) return;
    try {
      await api.post("/tasks", { title: text, due_at: toInstant(due), vazhnost: novaya });
      setTitle("");
      setDue("");
      setNovaya(VAZHNOST_PO_UMOLCHANIYU);
      reload();
    } catch (e) {
      toastError(e);
    } finally {
      guard.free();
    }
  };

  const toggle = async (task: any) => {
    try {
      await api.patch(`/tasks/${task.id}`, { is_done: !task.is_done });
      reload();
    } catch (e) {
      toastError(e);
    }
  };

  const remove = async (task: any) => {
    try {
      await api.del(`/tasks/${task.id}`);
      reload();
    } catch (e) {
      toastError(e);
    }
  };

  const perenesti = async (task: any, dney: number) => {
    try {
      await api.patch(`/tasks/${task.id}`, { due_at: sdvig(dney) });
      reload();
    } catch (e) {
      toastError(e);
    }
  };

  const now = Date.now();
  // Сделанное полосами не режется: там спрашивают «что сделали», а не «когда».
  const gruppy: Array<[Polosa | "", any[]]> =
    scope === "done"
      ? [["", items]]
      : POLOSY.map((p): [Polosa, any[]] => [p, items.filter((task) => polosa(task, now) === p)]).filter(
          ([, chast]) => chast.length > 0,
        );
  const sPolosami = gruppy.length > 1;

  // Список в одну колонку на 1800px читался как пустой: ширина обычной страницы.
  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1 className="page-title">{t("tasks")}</h1>
          <div className="page-sub">{t("tasksSub", { n: counts.overdue ?? 0 })}</div>
        </div>
      </div>

      {/* Ввод сверху и всегда на виду: напоминание заводят на ходу, между
          разговором и следующим клиентом. Прятать это за кнопкой «создать»
          значит не завести напоминание вовсе. */}
      <div className="task-new">
        <input
          className="input"
          placeholder={t("tasksPlaceholder")}
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") void add();
          }}
        />
        <input
          className="input"
          type="datetime-local"
          value={due}
          onChange={(e) => setDue(e.target.value)}
        />
        {/* Важность выбирается здесь же: заведённое «на потом» напоминание
            срочным уже не сделают — ради этого пришлось бы открывать карточку. */}
        <select
          className="input task-new-vazhnost"
          value={novaya}
          aria-label={t("vazhnost")}
          onChange={(e) => setNovaya(vazhnost(e.target.value))}
        >
          {VAZHNOSTI.map((slovo) => (
            <option key={slovo} value={slovo}>
              {t(VAZHNOST_LABEL[slovo])}
            </option>
          ))}
        </select>
        <button
          className="btn btn-primary"
          onClick={() => void add()}
          disabled={guard.busy || !title.trim()}
        >
          <Icon name="plus" stroke={2} />
          {t("add")}
        </button>
      </div>

      <div className="tabs">
        {SCOPES.map((name) => (
          <button
            key={name}
            className={"tab" + (scope === name ? " active" : "")}
            onClick={() => setScope(name)}
          >
            {t(SCOPE_LABEL[name])}
            {name === "overdue" && counts.overdue > 0 && (
              <span className="count danger">{counts.overdue}</span>
            )}
          </button>
        ))}
      </div>

      {items.length === 0 ? (
        <EmptyState icon="clock" title={t("tasksNone")} sub={t("tasksNoneHint")} />
      ) : (
        <div className="list-card">
          {gruppy.map(([imya, chast]) => (
            <div key={imya || "all"}>
              {sPolosami && imya && (
                <div
                  className={
                    "spisok-polosa" +
                    (imya === "overdue" ? " beda" : "") +
                    (imya === "srochno" ? " srochnaya" : "")
                  }
                >
                  {t(POLOSA_LABEL[imya])} · {chast.length}
                </div>
              )}
              {chast.map((task) => {
            const at = parseDate(task.due_at);
            const late = at && !task.is_done && at.getTime() < now;
            const vazhnoe = vazhnost(task.vazhnost);
            // Волна по краю — только у незакрытых: у сделанного срочность в
            // прошлом, а движущаяся рамка тянет взгляд на то, что уже неважно.
            const volna = srochno(vazhnoe) && !task.is_done;
            return (
              <div key={task.id} className={"task-row" + (volna ? " srochno" : "")}>
                {/* Отметка одним нажатием: если закрытие задачи требует зайти
                    в карточку, её не закрывают, и список перестаёт отражать
                    действительность. */}
                <button
                  className={"task-check" + (task.is_done ? " done" : "")}
                  onClick={() => void toggle(task)}
                  aria-label={t("tasksDone")}
                >
                  {task.is_done && <Icon name="check" size={12} stroke={2.5} />}
                </button>
                <div className="task-text">
                  {/* Заголовок — кнопка: карточку открывают с него, а не с
                      отдельного значка, который ещё надо заметить. */}
                  <button
                    type="button"
                    className={"task-title" + (task.is_done ? " done" : "")}
                    onClick={() => setOtkryto(task.id)}
                  >
                    {task.title}
                  </button>
                  <div className="task-meta">
                    {vazhnoe !== "normal" && (
                      <span className={"vazhnost-chip " + vazhnoe}>{t(VAZHNOST_LABEL[vazhnoe])}</span>
                    )}
                    {task.files_count > 0 && (
                      <span className="task-vlozheno" title={t("tasksFiles")}>
                        <Icon name="image" size={11} />
                        {task.files_count}
                      </span>
                    )}
                    {task.note_est && (
                      <span className="task-vlozheno" title={t("tasksNote")}>
                        <Icon name="note" size={11} />
                      </span>
                    )}
                    {at && (
                      <span className={late ? "task-late" : undefined}>
                        {formatDateTime(task.due_at, locale)}
                      </span>
                    )}
                    {!task.is_done && (
                      <>
                        <button type="button" className="task-shift" onClick={() => void perenesti(task, 1)}>
                          {t("tasksShiftTomorrow")}
                        </button>
                        <button type="button" className="task-shift" onClick={() => void perenesti(task, 7)}>
                          {t("tasksShiftWeek")}
                        </button>
                      </>
                    )}
                    {task.assignee_name && <span>{task.assignee_name}</span>}
                    {task.deal_id && (
                      <Link to={`/deals/${task.deal_id}`} className="text-link">
                        {task.deal_title || t("deal")}
                      </Link>
                    )}
                    {task.client_id && (
                      <Link to={`/clients/${task.client_id}`} className="text-link">
                        {task.client_name || t("client")}
                      </Link>
                    )}
                  </div>
                </div>
                <button className="btn-icon" onClick={() => void remove(task)} aria-label={t("delete")}>
                  <Icon name="trash" size={13} />
                </button>
              </div>
            );
          })}
            </div>
          ))}
        </div>
      )}

      {otkryto !== null && (
        <KartochkaNapominaniya taskId={otkryto} onClose={zakryt} onChanged={reload} />
      )}
    </div>
  );
}

/** Быстрое создание из карточки клиента или заявки. */
export function QuickTask({
  clientId,
  dealId,
  onCreated,
}: {
  clientId?: number;
  dealId?: number;
  onCreated?: () => void;
}) {
  const { t, toastError } = useApp();
  const [title, setTitle] = useState("");
  const [due, setDue] = useState("");
  const guard = useGuard();

  const add = async () => {
    const text = title.trim();
    // Тот же засов, что в списке напоминаний: Enter здесь нажимают так же и с
    // тем же результатом — два одинаковых напоминания по одной заявке.
    if (!text || !guard.take()) return;
    try {
      await api.post("/tasks", {
        title: text,
        due_at: toInstant(due),
        client_id: clientId ?? null,
        deal_id: dealId ?? null,
      });
      setTitle("");
      setDue("");
      onCreated?.();
    } catch (e) {
      toastError(e);
    } finally {
      guard.free();
    }
  };

  return (
    <div className="task-new">
      <input
        className="input"
        placeholder={t("tasksPlaceholder")}
        value={title}
        onChange={(e) => setTitle(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") void add();
        }}
      />
      <input
        className="input"
        type="datetime-local"
        value={due}
        onChange={(e) => setDue(e.target.value)}
      />
      <button
        className="btn btn-secondary"
        onClick={() => void add()}
        disabled={guard.busy || !title.trim()}
      >
        {t("add")}
      </button>
    </div>
  );
}
