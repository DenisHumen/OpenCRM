import { useCallback, useEffect, useState } from "react";

import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { formatDateTime } from "../lib/format";
import { moduleOn } from "../lib/modules";
import { can } from "../lib/permissions";
import { Icon } from "./Icon";

/**
 * Лента общения: письма, звонки, встречи и заметки одним потоком.
 *
 * Решение принято до почты и телефонии намеренно. Заведи каждый канал свой
 * журнал — и свести их потом придётся вручную: разные поля, разное время,
 * разные привязки. Здесь у всех видов одна форма записи с самого начала.
 */
const KINDS = ["note", "call", "meeting", "email"] as const;

// Виды, которые появляются сами — от подписчика на событие, а не из формы.
// В выпадающем списке «добавить» их нет: набрать смену этапа, которой не было,
// нельзя, иначе лента перестаёт быть свидетельством. Отфильтровать — можно.
//
// Списаний и бланков в этом списке не убавляется, когда склад или бланки
// выключены: это записи ленты, а не склада, ровно как письма и звонки.
// Выключенный блок перестаёт их порождать — уже написанные остаются.
const SYSTEM_KINDS = ["stage", "document", "stock"] as const;

const KIND_ICON: Record<string, string> = {
  note: "note",
  call: "call",
  meeting: "meeting",
  email: "email",
  // Значки те же, что у разделов, откуда события приходят: строку узнаёшь, не
  // читая её. Воронка — этап, квитанция — бланк, склад — списание.
  stage: "deals",
  document: "receipt",
  stock: "warehouse",
};

const KIND_LABEL = {
  note: "feedNote",
  call: "feedCall",
  meeting: "feedMeeting",
  email: "feedEmail",
  stage: "feedStage",
  document: "feedDocument",
  stock: "feedStock",
} as const;

export function Feed({ dealId, clientId }: { dealId: number; clientId: number }) {
  const { t, locale, user, modules, toast, toastError, refreshTasks } = useApp();
  const [items, setItems] = useState<any[]>([]);
  const [filter, setFilter] = useState<string>("");
  const [kind, setKind] = useState<(typeof KINDS)[number]>("note");
  const [direction, setDirection] = useState("");
  const [body, setBody] = useState("");

  const load = useCallback(() => {
    const query = filter ? `?kind=${filter}` : "";
    api
      .get(`/deals/${dealId}/feed${query}`)
      .then((d) => setItems(d.items))
      .catch(() => undefined);
  }, [dealId, filter]);

  useEffect(load, [load]);

  // Направление есть у звонка и письма; у заметки и встречи его нет, и
  // подставлять «входящее» по умолчанию нельзя — это разные вещи.
  const needsDirection = kind === "call" || kind === "email";

  const add = async () => {
    const text = body.trim();
    if (!text) return;
    try {
      await api.post(`/deals/${dealId}/feed`, {
        kind,
        body: text,
        direction: needsDirection ? direction || "in" : "",
      });
      setBody("");
      load();
    } catch (e) {
      toastError(e);
    }
  };

  // Задача из записи в один клик: «клиент просил перезвонить в четверг»
  // записывают в ленту, а вспоминают о нём — по напоминанию.
  const toTask = async (entry: any) => {
    try {
      await api.post("/tasks", {
        title: entry.body.slice(0, 120),
        client_id: clientId,
        deal_id: dealId,
      });
      toast(t("feedTaskCreated"));
      void refreshTasks();
    } catch (e) {
      toastError(e);
    }
  };

  return (
    <div className="card card-pad" style={{ marginBottom: 20 }}>
      <div className="page-head" style={{ marginBottom: 12 }}>
        <div className="metric-title">{t("feed")}</div>
        <select className="input" style={{ width: 150 }} value={filter} onChange={(e) => setFilter(e.target.value)}>
          <option value="">{t("feedAll")}</option>
          {[...KINDS, ...SYSTEM_KINDS].map((name) => (
            <option key={name} value={name}>{t(KIND_LABEL[name])}</option>
          ))}
        </select>
      </div>

      <div className="feed-new">
        <select className="input" style={{ width: 130 }} value={kind} onChange={(e) => setKind(e.target.value as never)}>
          {KINDS.map((name) => (
            <option key={name} value={name}>{t(KIND_LABEL[name])}</option>
          ))}
        </select>
        {needsDirection && (
          <select className="input" style={{ width: 120 }} value={direction} onChange={(e) => setDirection(e.target.value)}>
            <option value="in">{t("feedIn")}</option>
            <option value="out">{t("feedOut")}</option>
          </select>
        )}
        <input
          className="input"
          placeholder={t("feedPlaceholder")}
          value={body}
          onChange={(e) => setBody(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") void add();
          }}
        />
        <button className="btn btn-secondary" onClick={() => void add()} disabled={!body.trim()}>
          {t("add")}
        </button>
      </div>

      {items.length === 0 ? (
        <div className="field-desc" style={{ marginTop: 12 }}>{t("feedEmpty")}</div>
      ) : (
        <div className="feed-list">
          {items.map((entry) => (
            <div key={entry.id} className="feed-row">
              <span className="feed-icon">
                <Icon name={KIND_ICON[entry.kind] ?? "note"} size={14} />
              </span>
              <div className="feed-text">
                <div>{entry.body}</div>
                <div className="feed-meta">
                  <span>{formatDateTime(entry.happened_at, locale)}</span>
                  {entry.direction && (
                    <span>{entry.direction === "in" ? t("feedIn") : t("feedOut")}</span>
                  )}
                </div>
              </div>
              {/* Из записи, набранной человеком, задача осмысленна («перезвонить
                  в четверг»); из строки «этап: Новая → В работе» получилось бы
                  напоминание с машинным заголовком. */}
              {moduleOn(modules, "tasks") && can(user, "tasks.create")
                && !SYSTEM_KINDS.includes(entry.kind) && (
                <button
                  className="btn-icon"
                  title={t("feedToTask")}
                  onClick={() => void toTask(entry)}
                >
                  <Icon name="clock" size={13} />
                </button>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
