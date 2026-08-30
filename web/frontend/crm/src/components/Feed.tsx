import { useCallback, useEffect, useRef, useState } from "react";

import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { useGuard } from "../lib/guard";
import { formatDateTime } from "../lib/format";
import { moduleOn } from "../lib/modules";
import { can } from "../lib/permissions";
import { useFailure } from "../lib/failure";
import { Icon } from "./Icon";
import { Dochitat, LoadFailed } from "./ui";

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

/** По скольку событий дочитывается лента. */
const NA_STRANITSE = 100;

export function Feed({ dealId, clientId }: { dealId: number; clientId: number }) {
  const { t, locale, user, modules, toast, toastError, refreshTasks } = useApp();
  const [filter, setFilter] = useState<string>("");
  const [kind, setKind] = useState<(typeof KINDS)[number]>("note");
  const [direction, setDirection] = useState("");
  const [body, setBody] = useState("");
  const guard = useGuard();
  // Свой засов у «сделать напоминанием»: с засовом ввода он не общий намеренно
  // — добавление записи и превращение записи в напоминание идут независимо, и
  // одно не должно запирать другое. Один засов на все строки ленты — это и
  // есть нужное поведение: двойное нажатие по строке заводило два одинаковых
  // напоминания, и выяснялось это только когда оба напоминали.
  const taskGuard = useGuard();

  // Лента — поток, а не справочник, и потому дочитывается, а не собирается
  // целиком. Общий крючок справочников её тянул постранично до конца: у
  // заявки, которую ведут год, это два десятка запросов подряд и две тысячи
  // строк в разметке — ради событий, которых никто не листает дальше первого
  // экрана.
  //
  // Отказ при этом по-прежнему не превращается в «Пока ничего не записано»:
  // пустая лента — это ответ («по заявке ещё не разговаривали»), а отказ — его
  // отсутствие, и решения человек принимает разные: первое закроет, второе
  // повторит.
  const otbor = `/deals/${dealId}/feed?per_page=${NA_STRANITSE}` +
    (filter ? `&kind=${filter}` : "");
  const [items, setItems] = useState<any[] | null>(null);
  const [vsego, setVsego] = useState(0);
  const [stranitsa, setStranitsa] = useState(1);
  const [dochityvaem, setDochityvaem] = useState(false);
  const [popytka, setPopytka] = useState(0);
  const { failure, fail, clear } = useFailure();
  // Чему принадлежит показанное. Ставит загрузка, сверяет дочитка: отбор по
  // виду записи переключают мышью, и опоздавшая страница дописала бы звонки
  // к письмам.
  const otbor_spiska = useRef("");

  const reload = useCallback(() => setPopytka((bylo) => bylo + 1), []);

  useEffect(() => {
    let current = true;
    otbor_spiska.current = otbor;
    clear();
    api
      .get<{ items: any[]; total: number }>(`${otbor}&page=1`)
      .then((otvet) => {
        if (!current) return;
        setItems(otvet.items);
        setVsego(otvet.total);
        setStranitsa(1);
      })
      .catch((beda) => {
        if (current) fail(beda);
      });
    return () => {
      current = false;
    };
  }, [otbor, popytka, fail, clear]);

  /** Дочитать ленту. Номер растёт ПОСЛЕ ответа: увеличь его до — и отказ на
   *  второй странице пропустил бы её навсегда. Отказ говорит всплывающей
   *  жалобой, а не через `fail`: тот рисует «не удалось загрузить» вместо
   *  всей ленты, а лента уже показана. */
  const dochitat = async () => {
    if (dochityvaem) return;
    const sprosheno = otbor;
    setDochityvaem(true);
    try {
      const dalshe = await api.get<{ items: any[]; total: number }>(
        `${otbor}&page=${stranitsa + 1}`,
      );
      // Отбор сменился, пока страница ехала, — ответ чужой.
      if (otbor_spiska.current !== sprosheno) return;
      setItems((bylo) => [...(bylo ?? []), ...dalshe.items]);
      setVsego(dalshe.total);
      setStranitsa((bylo) => bylo + 1);
    } catch (beda) {
      toastError(beda);
    } finally {
      setDochityvaem(false);
    }
  };

  // Направление есть у звонка и письма; у заметки и встречи его нет, и
  // подставлять «входящее» по умолчанию нельзя — это разные вещи.
  const needsDirection = kind === "call" || kind === "email";

  const add = async () => {
    const text = body.trim();
    // Enter в поле ленты нажимают дважды — от нетерпения и просто с руки. Пока
    // отправка не завершилась, вторая запись была бы копией первой, и удалять
    // её пришлось бы вручную.
    if (!text || !guard.take()) return;
    try {
      await api.post(`/deals/${dealId}/feed`, {
        kind,
        body: text,
        direction: needsDirection ? direction || "in" : "",
      });
      setBody("");
      reload();
    } catch (e) {
      toastError(e);
    } finally {
      guard.free();
    }
  };

  // Задача из записи в один клик: «клиент просил перезвонить в четверг»
  // записывают в ленту, а вспоминают о нём — по напоминанию.
  const toTask = async (entry: any) => {
    if (!taskGuard.take()) return;
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
    } finally {
      taskGuard.free();
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
        <button
          className="btn btn-secondary"
          onClick={() => void add()}
          disabled={guard.busy || !body.trim()}
        >
          {t("add")}
        </button>
      </div>

      {failure !== null ? (
        <LoadFailed error={failure} onRetry={reload} />
      ) : items === null ? null : items.length === 0 ? (
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
                  {/* Кто — половина смысла ленты. У письма и звонка, пришедших
                      извне, живого автора нет, и подписывать их «системой»
                      значило бы соврать: там его действительно не было. */}
                  {entry.author_name && <span>{entry.author_name}</span>}
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
                  disabled={taskGuard.busy}
                  onClick={() => void toTask(entry)}
                >
                  <Icon name="clock" size={13} />
                </button>
              )}
            </div>
          ))}
          <Dochitat
            pokazano={items.length}
            vsego={vsego}
            zanyat={dochityvaem}
            onClick={() => void dochitat()}
          />
        </div>
      )}
    </div>
  );
}
