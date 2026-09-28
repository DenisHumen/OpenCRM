import { useCallback, useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";

import { Icon } from "../components/Icon";
import { KalendarNapominaniy } from "../components/KalendarNapominaniy";
import { KartochkaNapominaniya } from "../components/KartochkaNapominaniya";
import { StrokaNapominaniya } from "../components/StrokaNapominaniya";
import { EmptyState, ScreenLoading } from "../components/ui";
import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { useLiveTopic } from "../lib/live";
import { useFailure } from "../lib/failure";
import { useGuard } from "../lib/guard";
import { parseDate } from "../lib/format";
import { poyasBrauzera } from "../lib/povtor";
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

/** Чьи: все видимые, звонящие мне, поставленные мной другим, общая полка. */
const KTO = ["vse", "moi", "postavil", "polka"] as const;
const KTO_LABEL = {
  vse: "napomKtoVse",
  moi: "napomKtoMoi",
  postavil: "napomKtoPostavil",
  polka: "napomKtoPolka",
} as const;

const VID_KLYUCH = "opencrm.napom.vid";

/**
 * Срок из поля ввода в абсолютный момент.
 *
 * `datetime-local` отдаёт «2026-08-10T18:00» без зоны, и `new Date()` читает
 * это как МЕСТНОЕ время — именно так человек его и имел в виду. `toISOString()`
 * переводит в UTC, в котором время и хранится.
 */
function toInstant(local: string): string | null {
  if (!local) return null;
  const moment = new Date(local);
  return Number.isNaN(moment.getTime()) ? null : moment.toISOString();
}

/** Полосы списка по местному дню: в одной ленте «сегодня» терялось между
 *  вчерашним и следующей неделей (владелец, 06.09.2026). */
type Polosa = "urgent" | "overdue" | "today" | "tomorrow" | "later" | "nodue";
const POLOSY: Polosa[] = ["urgent", "overdue", "today", "tomorrow", "later", "nodue"];
const POLOSA_LABEL = {
  urgent: "vazhnostUrgent",
  overdue: "tasksOverdue",
  today: "tasksToday",
  tomorrow: "tasksTomorrow",
  later: "tasksLater",
  nodue: "tasksNoDue",
} as const;

function polosa(task: { due_at: string | null; vazhnost?: string }, now: number): Polosa {
  // Срочное собирается наверх мимо дней. Иначе «срочно, но без срока» падало в
  // самый низ, под «позже», — а сервер как раз ставит важность выше срока.
  if (srochno(task.vazhnost)) return "urgent";
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

function zapomnennyyVid(): "kalendar" | "spisok" {
  try {
    return localStorage.getItem(VID_KLYUCH) === "spisok" ? "spisok" : "kalendar";
  } catch {
    return "kalendar";
  }
}

export function Tasks() {
  const { t, refreshTasks, toastError } = useApp();
  const [params, setParams] = useSearchParams();
  const [vid, setVid] = useState<"kalendar" | "spisok">(zapomnennyyVid);
  const [kto, setKto] = useState<(typeof KTO)[number]>("vse");
  const [scope, setScope] = useState<(typeof SCOPES)[number]>("open");
  const [items, setItems] = useState<any[] | null>(null);
  const [counts, setCounts] = useState<Record<string, number>>({});
  const [title, setTitle] = useState("");
  const [due, setDue] = useState("");
  const [novaya, setNovaya] = useState<Vazhnost>(VAZHNOST_PO_UMOLCHANIYU);
  const [otkryto, setOtkryto] = useState<number | null>(() => Number(params.get("open")) || null);
  const [attempt, setAttempt] = useState(0);
  useLiveTopic("tasks", () => setAttempt((a) => a + 1));
  const guard = useGuard();

  const { failure, fail, clear } = useFailure();

  // Повтор после отказа и обновление после правки идут одним путём: два разных
  // способа перезагрузить один список расходятся в поведении с первой правкой.
  const reload = useCallback(() => setAttempt((n) => n + 1), []);
  const zakryt = useCallback(() => {
    setOtkryto(null);
    if (params.has("open")) {
      params.delete("open");
      setParams(params, { replace: true });
    }
  }, [params, setParams]);

  // Ссылка из колокольчика и системного окна ведёт сюда с `?open=`.
  useEffect(() => {
    const nomer = Number(params.get("open"));
    if (nomer) setOtkryto(nomer);
  }, [params]);

  const vybratVid = (novyy: "kalendar" | "spisok") => {
    setVid(novyy);
    try {
      localStorage.setItem(VID_KLYUCH, novyy);
    } catch {
      /* без хранилища вид проживёт до перезагрузки */
    }
  };

  useEffect(() => {
    // Списки переключают быстрее, чем отвечает сервер: без счётчика ответ по
    // «просроченным» ложился поверх «на неделю», и человек видел не тот
    // список, на который нажал.
    let current = true;
    clear();
    Promise.all([api.get(`/tasks?scope=${scope}&kto=${kto}`), api.get("/tasks/summary")])
      .then(([list, summary]) => {
        if (!current) return;
        setItems(list.items);
        setCounts(summary);
        void refreshTasks();
      })
      .catch((e) => {
        if (current) fail(e);
      });
    return () => {
      current = false;
    };
  }, [scope, kto, attempt, refreshTasks, fail, clear]);

  if (!items) return <ScreenLoading error={failure} onRetry={reload} />;

  const add = async () => {
    const text = title.trim();
    // Напоминание заводят с клавиатуры и Enter нажимают дважды — от нетерпения
    // и просто с руки. Без засова в списке появлялись два одинаковых.
    if (!text || !guard.take()) return;
    try {
      await api.post("/tasks", {
        title: text,
        due_at: toInstant(due),
        vazhnost: novaya,
        poyas: poyasBrauzera(),
      });
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

  const now = Date.now();
  // Сделанное полосами не режется: там спрашивают «что сделали», а не «когда».
  const gruppy: Array<[Polosa | "", any[]]> =
    scope === "done"
      ? [["", items]]
      : POLOSY.map((p): [Polosa, any[]] => [p, items.filter((task) => polosa(task, now) === p)]).filter(
          ([, chast]) => chast.length > 0,
        );
  const sPolosami = gruppy.length > 1;

  return (
    <div className={vid === "kalendar" ? "page page-napom" : "page"}>
      <div className="page-head">
        <div>
          <h1 className="page-title">{t("tasks")}</h1>
          <div className="page-sub">{t("tasksSub", { n: counts.overdue ?? 0 })}</div>
        </div>
        <div className="napom-vid" role="tablist" aria-label={t("tasks")}>
          {(["kalendar", "spisok"] as const).map((v) => (
            <button
              key={v}
              type="button"
              role="tab"
              aria-selected={vid === v}
              className={"napom-vid-btn" + (vid === v ? " active" : "")}
              onClick={() => vybratVid(v)}
            >
              <Icon name={v === "kalendar" ? "calendar" : "list"} size={14} />
              {t(v === "kalendar" ? "napomVidKalendar" : "napomVidSpisok")}
            </button>
          ))}
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
        <select
          className="input task-new-importance"
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

      <div className="napom-kto" role="tablist" aria-label={t("napomLyudi")}>
        {KTO.map((k) => (
          <button
            key={k}
            type="button"
            role="tab"
            aria-selected={kto === k}
            className={"filter-chip" + (kto === k ? " active" : "")}
            onClick={() => setKto(k)}
          >
            {t(KTO_LABEL[k])}
            {k === "moi" && counts.mine > 0 && <span className="chip-count">{counts.mine}</span>}
          </button>
        ))}
      </div>

      {vid === "kalendar" ? (
        <KalendarNapominaniy kto={kto} onOpen={setOtkryto} versiya={attempt} />
      ) : (
        <>
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
                        "list-bar" +
                        (imya === "overdue" ? " beda" : "") +
                        (imya === "urgent" ? " urgent" : "")
                      }
                    >
                      {t(POLOSA_LABEL[imya])} · {chast.length}
                    </div>
                  )}
                  {chast.map((task) => (
                    <StrokaNapominaniya
                      key={task.id}
                      task={task}
                      sUdaleniem
                      onOpen={setOtkryto}
                      onChanged={reload}
                    />
                  ))}
                </div>
              ))}
            </div>
          )}
        </>
      )}

      {otkryto !== null && (
        <KartochkaNapominaniya taskId={otkryto} onClose={zakryt} onChanged={reload} />
      )}
    </div>
  );
}

/** Быстрое создание из карточки другого блока: клиента, заявки, заказа, товара, доски. */
export function QuickTask({
  clientId,
  dealId,
  documentId,
  productId,
  boardId,
  onCreated,
}: {
  clientId?: number;
  dealId?: number;
  documentId?: number;
  productId?: number;
  boardId?: number;
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
        poyas: poyasBrauzera(),
        client_id: clientId ?? null,
        deal_id: dealId ?? null,
        document_id: documentId ?? null,
        product_id: productId ?? null,
        board_id: boardId ?? null,
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
