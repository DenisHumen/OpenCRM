import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { CallButton, callIcon, useOutcomeLabel } from "../components/CallsPanel";
import { Icon } from "../components/Icon";
import { Chip, EmptyState, ScreenLoading } from "../components/ui";
import { api, type PhoneCall } from "../lib/api";
import { useApp } from "../lib/app";
import { useDebounced } from "../lib/debounce";
import { useFailure } from "../lib/failure";
import { formatCallDuration, formatDateTime } from "../lib/format";
import { moduleOn } from "../lib/modules";

// Ширины колонок общие для шапки и строк, чтобы всё было выровнено.
const COL = {
  when: { width: 150 } as const,
  duration: { width: 90, textAlign: "right" } as const,
  outcome: { width: 110 } as const,
  actions: { width: 190, textAlign: "right" } as const,
};

export function Calls() {
  const { t, locale, modules, toast, toastError, refreshTasks } = useApp();
  const [items, setItems] = useState<PhoneCall[] | null>(null);
  const [total, setTotal] = useState(0);
  const [search, setSearch] = useState("");
  const [missedOnly, setMissedOnly] = useState(false);
  const outcomeLabel = useOutcomeLabel();
  const tasksOn = moduleOn(modules, "tasks");

  const [attempt, setAttempt] = useState(0);

  const { failure, fail, clear } = useFailure();

  const typed = useDebounced(search);

  const path = useMemo(() => {
    const params = new URLSearchParams({ per_page: "100" });
    if (typed.trim()) params.set("number", typed.trim());
    if (missedOnly) params.set("outcome", "missed");
    return `/telephony/calls?${params}`;
  }, [typed, missedOnly]);

  // Повтор и обновление после звонка идут одним путём — счётчиком попыток:
  // так «нажали ещё раз» и «позвонили и вернулись» не расходятся в поведении.
  const reload = useCallback(() => setAttempt((n) => n + 1), []);

  useEffect(() => {
    // «Только пропущенные» переключают быстрее, чем отвечает сервер: без
    // счётчика ответ по прошлому отбору ложился поверх текущего. Приём тот же,
    // что в отчётах и палитре команд.
    let current = true;
    clear();
    api
      .get<{ items: PhoneCall[]; total: number }>(path)
      .then((data) => {
        if (!current) return;
        setItems(data.items);
        setTotal(data.total);
      })
      .catch((e) => {
        if (current) fail(e);
      });
    return () => {
      current = false;
    };
  }, [path, attempt, fail, clear]);

  if (!items) return <ScreenLoading error={failure} onRetry={reload} />;

  // Напоминание перезвонить живёт в блоке напоминаний: он выключен — кнопки
  // просто нет, а журнал работает как работал.
  const remind = async (call: PhoneCall) => {
    try {
      await api.post(`/telephony/calls/${call.id}/callback-task`);
      toast(t("callbackTaskCreated"));
      void refreshTasks();
    } catch (e) {
      toastError(e);
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
                  <button className="text-link" onClick={() => void remind(call)}>
                    {t("callbackTask")}
                  </button>
                )}
                <CallButton number={call.counterparty} onCalled={reload} />
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
