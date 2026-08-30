import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";

import { CallButton, callIcon, useOutcomeLabel } from "../components/CallsPanel";
import { Icon } from "../components/Icon";
import { Chip, Dochitat, EmptyState, ScreenLoading } from "../components/ui";
import { api, type PhoneCall } from "../lib/api";
import { useApp } from "../lib/app";
import { useDebounced } from "../lib/debounce";
import { useFailure } from "../lib/failure";
import { useGuard } from "../lib/guard";
import { formatCallDuration, formatDateTime } from "../lib/format";
import { moduleOn } from "../lib/modules";

// Ширины колонок общие для шапки и строк, чтобы всё было выровнено.
const COL = {
  when: { width: 150 } as const,
  duration: { width: 90, textAlign: "right" } as const,
  outcome: { width: 110 } as const,
  actions: { width: 190, textAlign: "right" } as const,
};

/** По скольку звонков дочитывается журнал. */
const NA_STRANITSE = 100;

export function Calls() {
  const { t, locale, modules, toast, toastError, refreshTasks } = useApp();
  const [items, setItems] = useState<PhoneCall[] | null>(null);
  const [total, setTotal] = useState(0);
  const [search, setSearch] = useState("");
  const [missedOnly, setMissedOnly] = useState(false);
  const outcomeLabel = useOutcomeLabel();
  const tasksOn = moduleOn(modules, "tasks");
  // Засов на «перезвонить»: кнопка стоит в каждой строке пропущенных, и второе
  // нажатие заводило второе такое же напоминание по тому же звонку. Один засов
  // на весь журнал — напоминания ставят по одному.
  const guard = useGuard();

  // До какой страницы дочитан журнал. Прежде экран просил сотню звонков и на
  // этом заканчивался — а рядом честно писал «всего 2000». Сам сообщал, что
  // показывает двадцатую часть, и ничего с этим сделать не давал.
  const [stranitsa, setStranitsa] = useState(1);
  const [dochityvaem, setDochityvaem] = useState(false);
  // Отбор, которому принадлежит показанный журнал. Ставится загрузкой,
  // сверяется дочиткой: пока вторая страница по «380» едет, человек успевает
  // набрать «44», первая страница «44» заменяет журнал — и опоздавшая страница
  // дописывается к чужим находкам. На экране два отбора вперемешку, а «всего»
  // от прошлого.
  const otbor_spiska = useRef("");

  const [attempt, setAttempt] = useState(0);

  const { failure, fail, clear } = useFailure();

  const typed = useDebounced(search);

  // Отбор без номера страницы: по нему видно, что спрашивают уже про другое.
  const otbor = useMemo(() => {
    const params = new URLSearchParams({ per_page: String(NA_STRANITSE) });
    if (typed.trim()) params.set("number", typed.trim());
    if (missedOnly) params.set("outcome", "missed");
    return `/telephony/calls?${params}`;
  }, [typed, missedOnly]);

  // Повтор и обновление после звонка идут одним путём — счётчиком попыток:
  // так «нажали ещё раз» и «позвонили и вернулись» не расходятся в поведении.
  // Журнал при этом начинается заново, с первой страницы: перезагрузка просит
  // её у сервера и заменяет ею показанное, потому что свежий звонок ложится
  // именно в её начало, а дочитанные дальние страницы после него сдвинулись бы
  // и повторили бы уже показанные строки.
  const reload = useCallback(() => {
    setAttempt((n) => n + 1);
  }, []);

  useEffect(() => {
    // «Только пропущенные» переключают быстрее, чем отвечает сервер: без
    // счётчика ответ по прошлому отбору ложился поверх текущего. Приём тот же,
    // что в отчётах и палитре команд.
    let current = true;
    otbor_spiska.current = otbor;
    clear();
    api
      .get<{ items: PhoneCall[]; total: number }>(`${otbor}&page=1`)
      .then((data) => {
        if (!current) return;
        setItems(data.items);
        setTotal(data.total);
        setStranitsa(1);
      })
      .catch((e) => {
        if (current) fail(e);
      });
    return () => {
      current = false;
    };
  }, [otbor, attempt, fail, clear]);

  /** Дочитать журнал.
   *
   * Отдельным действием, а не номером страницы в пути загрузки, и номер растёт
   * ПОСЛЕ удачного ответа. Иначе отказ на второй странице оставлял бы счётчик
   * на двойке, а следующее нажатие просило бы третью — вторая пропускалась бы
   * навсегда, и журнал молча недосчитывался бы сотни звонков.
   *
   * Отказ говорит о себе всплывающей жалобой, а не через `fail`: `fail` рисует
   * экран «не удалось загрузить», а он виден только пока показывать нечего.
   * После первой удачной загрузки отказ дочитки не показал бы ничего вовсе —
   * кнопка просто переставала бы отвечать.
   */
  const dochitat = async () => {
    if (dochityvaem) return;
    setDochityvaem(true);
    const sprosheno = otbor;
    try {
      const dalshe = await api.get<{ items: PhoneCall[]; total: number }>(
        `${otbor}&page=${stranitsa + 1}`,
      );
      // Отбор сменился, пока страница ехала, — ответ чужой.
      if (otbor_spiska.current !== sprosheno) return;
      setItems((bylo) => (bylo ? [...bylo, ...dalshe.items] : dalshe.items));
      setTotal(dalshe.total);
      setStranitsa((bylo) => bylo + 1);
    } catch (e) {
      toastError(e);
    } finally {
      setDochityvaem(false);
    }
  };

  if (!items) return <ScreenLoading error={failure} onRetry={reload} />;

  // Напоминание перезвонить живёт в блоке напоминаний: он выключен — кнопки
  // просто нет, а журнал работает как работал.
  const remind = async (call: PhoneCall) => {
    if (!guard.take()) return;
    try {
      await api.post(`/telephony/calls/${call.id}/callback-task`);
      toast(t("callbackTaskCreated"));
      void refreshTasks();
    } catch (e) {
      toastError(e);
    } finally {
      guard.free();
    }
  };

  return (
    <div className="page">
      <div className="page-head" style={{ alignItems: "flex-start", marginBottom: 20 }}>
        <div>
          <h1 className="page-title">{t("calls")}</h1>
          <div className="page-sub">{t("callsSub", { total })}</div>
        </div>
      </div>

      <div style={{ display: "flex", gap: 10, alignItems: "center", marginBottom: 16 }}>
        <input
          className="input"
          style={{ maxWidth: 320 }}
          placeholder={t("callsSearch")}
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        {/* Пропущенные — то, ради чего в журнал заходят чаще всего: это
            несостоявшийся разговор, и он ждёт действия. */}
        <button
          className={"option-chip" + (missedOnly ? " active" : "")}
          onClick={() => setMissedOnly((on) => !on)}
        >
          <Icon name="callMissed" size={13} />
          {t("onlyMissed")}
        </button>
      </div>

      {items.length === 0 ? (
        <EmptyState title={t("callsEmpty")} />
      ) : (
        <div className="list-card">
          <div className="list-header">
            <span style={{ width: 34 }} />
            <span style={{ flex: 1 }}>{t("colNumber")}</span>
            <span style={COL.when}>{t("colWhen")}</span>
            <span style={COL.duration}>{t("colDuration")}</span>
            <span style={COL.outcome}>{t("colOutcome")}</span>
            <span style={COL.actions} />
          </div>
          {items.map((call) => (
            <div key={call.id} className="list-row hoverable">
              <span
                className="feed-icon"
                style={{ color: call.outcome === "missed" ? "var(--danger)" : undefined }}
                title={call.direction === "in" ? t("feedIn") : t("feedOut")}
              >
                <Icon name={callIcon(call)} size={14} />
              </span>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ color: "var(--text)", fontSize: 13.5, fontWeight: 500 }}>
                  {call.counterparty || t("unknownNumber")}
                </div>
                <div style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 2 }}>
                  <span style={{ color: "var(--faint)", fontSize: 12 }}>
                    {call.direction === "in" ? t("feedIn") : t("feedOut")}
                  </span>
                  {call.client_id ? (
                    <Link
                      to={`/clients/${call.client_id}`}
                      style={{ color: "var(--muted)", fontSize: 12 }}
                    >
                      {t("client")}
                    </Link>
                  ) : (
                    // Звонок с незнакомого номера карточку не заводит: кто из
                    // звонивших клиент, решает человек.
                    <span style={{ color: "var(--faint)", fontSize: 12 }}>
                      {t("unknownNumber")}
                    </span>
                  )}
                  {call.has_recording && (
                    <span style={{ color: "var(--faint)", fontSize: 12 }}>{t("callRecording")}</span>
                  )}
                </div>
              </div>
              <span style={{ ...COL.when, color: "var(--muted)", fontSize: 12.5 }}>
                {formatDateTime(call.started_at, locale)}
              </span>
              <span style={{ ...COL.duration, color: "var(--text)", fontSize: 12.5 }}>
                {formatCallDuration(call.duration_sec)}
              </span>
              <span style={COL.outcome}>
                {call.outcome === "missed" ? (
                  <Chip variant="warning">{outcomeLabel(call)}</Chip>
                ) : (
                  <span style={{ color: "var(--muted)", fontSize: 12.5 }}>
                    {outcomeLabel(call)}
                  </span>
                )}
              </span>
              <span
                style={{ ...COL.actions, display: "flex", gap: 8, justifyContent: "flex-end" }}
              >
                {call.outcome === "missed" && tasksOn && (
                  <button className="text-link" disabled={guard.busy} onClick={() => void remind(call)}>
                    {t("callbackTask")}
                  </button>
                )}
                <CallButton number={call.counterparty} onCalled={reload} />
              </span>
            </div>
          ))}
          <Dochitat
            pokazano={items.length}
            vsego={total}
            zanyat={dochityvaem}
            onClick={() => void dochitat()}
          />
        </div>
      )}
    </div>
  );
}
