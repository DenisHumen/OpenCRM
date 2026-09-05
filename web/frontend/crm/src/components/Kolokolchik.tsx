import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";

import { Icon } from "./Icon";
import { LoadFailed, ottenok } from "./ui";
import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { useFailure } from "../lib/failure";
import { formatDateTime } from "../lib/format";
import { useLiveTopic } from "../lib/live";
import { signal_o_sobytii } from "../lib/signaly";

/** Одно уведомление, как отдаёт сервер: текста нет, подпись собирает экран. */
export interface Uvedomlenie {
  id: number;
  kind: string;
  params: Record<string, string>;
  link: string;
  created_at: string | null;
  read: boolean;
}

type Perevod = ReturnType<typeof useApp>["t"];

/** Значок вида в углу кружка: о чём речь, видно раньше, чем прочитан текст. */
const ZNACHOK: Record<string, string> = {
  order_closed: "receipt",
  order_reverted: "receipt",
  order_cancelled: "receipt",
  waybill_posted: "clipboard",
  auto_waybill: "clipboard",
  act_completed: "note",
  auto_act: "note",
  deal_stage: "deals",
  lead_received: "inbox",
  task_assigned: "clock",
};

/** Предмет уведомления — чьей буквой подписан кружок. Имя ищется раньше
 *  номера: у заявки и клиента буква говорящая, у «2026-000009» — нет. */
function predmet(n: Uvedomlenie): string {
  const p = n.params;
  return p.title || p.client || p.deal || p.order || p.number || n.kind;
}

/** Первая буква предмета, если она буква: цифра номера в кружке ничего не
 *  говорит, тогда кружок несёт значок вида, а уголок пустует. */
function bukva(n: Uvedomlenie): string | null {
  const b = predmet(n).slice(0, 1).toUpperCase();
  return /\p{L}/u.test(b) ? b : null;
}

/** Подпись по виду. Неизвестный вид не роняет колокольчик — показывается ключом. */
export function podpis(t: Perevod, n: Uvedomlenie): string {
  const tr = t as unknown as (k: string, p?: Record<string, string | number>) => string;
  const klyuchi: Record<string, string> = {
    order_closed: "ntfOrderClosed",
    order_reverted: "ntfOrderReverted",
    order_cancelled: "ntfOrderCancelled",
    waybill_posted: "ntfWaybillPosted",
    act_completed: "ntfActCompleted",
    deal_stage: "ntfDealStage",
    lead_received: "ntfLeadReceived",
    task_assigned: "ntfTaskAssigned",
    auto_waybill: "ntfAutoWaybill",
    auto_act: "ntfAutoAct",
  };
  const klyuch = klyuchi[n.kind];
  return klyuch ? tr(klyuch, n.params) : n.kind;
}

/** Колокольчик в панели: число непрочитанных и список последних.
 *
 * Список читается по нажатию, а не держится в памяти: уведомлений мало, а
 * держать их значило бы перечитывать на каждый намёк. Намёк живого потока
 * обновляет только счётчик — и подаёт сигнал браузера, если счётчик вырос.
 */
export function Kolokolchik() {
  const { t, locale, user } = useApp();
  const navigate = useNavigate();
  const [neprochitano, setNeprochitano] = useState(0);
  const [otkryt, setOtkryt] = useState(false);
  const [spisok, setSpisok] = useState<Uvedomlenie[] | null>(null);
  const { failure, fail, clear } = useFailure();
  const bylo = useRef<number | null>(null);

  const schitat = useCallback(async () => {
    try {
      const r = await api.get<{ unread: number }>("/notifications/summary");
      setNeprochitano(r.unread);
      // Выросло — значит пришло новое, и о нём стоит сказать вслух. Первый
      // счёт при входе — не событие: о старом непрочитанном не сигналим.
      if (bylo.current !== null && r.unread > bylo.current) {
        const svezhie = await api.get<{ items: Uvedomlenie[] }>("/notifications");
        const pervoe = svezhie.items.find((n) => !n.read);
        if (pervoe) signal_o_sobytii({ zagolovok: t("notifications"), telo: podpis(t, pervoe) });
        if (otkryt) setSpisok(svezhie.items);
      }
      bylo.current = r.unread;
    } catch {
      /* панель без счётчика лучше упавшей панели */
    }
  }, [t, otkryt]);

  useEffect(() => {
    if (user) void schitat();
  }, [user, schitat]);

  useLiveTopic("notifications", () => void schitat());

  // Живые обновления могут быть выключены — тогда счётчик пересчитывается,
  // когда человек возвращается во вкладку: дешевле опроса и достаточно.
  useEffect(() => {
    const vernulis = () => {
      if (document.visibilityState === "visible") void schitat();
    };
    document.addEventListener("visibilitychange", vernulis);
    return () => document.removeEventListener("visibilitychange", vernulis);
  }, [schitat]);

  // Отказ не выдаётся за пустоту: «пока пусто» — это ответ, а «не загрузилось»
  // — его отсутствие, и человек решает по ним разное.
  const otkryt_panel = async () => {
    setOtkryt(true);
    clear();
    try {
      const r = await api.get<{ items: Uvedomlenie[] }>("/notifications");
      setSpisok(r.items);
    } catch (beda) {
      setSpisok(null);
      fail(beda);
    }
  };

  const perejti = async (n: Uvedomlenie) => {
    setOtkryt(false);
    if (!n.read) {
      try {
        await api.post("/notifications/read", { ids: [n.id] });
        setNeprochitano((x) => Math.max(0, x - 1));
        bylo.current = Math.max(0, (bylo.current ?? 1) - 1);
      } catch {
        /* переход важнее отметки */
      }
    }
    if (n.link) navigate(n.link);
  };

  const prochitat_vse = async () => {
    try {
      await api.post("/notifications/read", {});
      setNeprochitano(0);
      bylo.current = 0;
      setSpisok((s) => (s ? s.map((n) => ({ ...n, read: true })) : s));
    } catch {
      /* останется как было */
    }
  };

  return (
    <div className="bell-wrap">
      <button
        type="button"
        className={"nav-item bell-btn" + (otkryt ? " active" : "")}
        aria-expanded={otkryt}
        onClick={() => (otkryt ? setOtkryt(false) : void otkryt_panel())}
      >
        <Icon name="inbox" size={15} />
        <span style={{ flex: 1 }}>{t("notifications")}</span>
        {neprochitano > 0 && <span className="nav-badge">{neprochitano}</span>}
      </button>
      {otkryt && (
        <div className="bell-panel" role="dialog" aria-label={t("notifications")}>
          <div className="bell-head">
            <span>{t("notifications")}</span>
            {neprochitano > 0 && (
              <button type="button" className="text-link" onClick={() => void prochitat_vse()}>
                {t("markAllRead")}
              </button>
            )}
            <button type="button" className="btn-icon" aria-label={t("close")} onClick={() => setOtkryt(false)}>
              <Icon name="x" size={13} />
            </button>
          </div>
          {failure !== null && <LoadFailed error={failure} onRetry={() => void otkryt_panel()} />}
          {failure === null && spisok === null && <div className="bell-empty">{t("loading")}</div>}
          {spisok !== null && spisok.length === 0 && <div className="bell-empty">{t("notificationsEmpty")}</div>}
          {spisok !== null && spisok.length > 0 && (
            <div className="bell-list">
              {spisok.map((n) => (
                <button
                  key={n.id}
                  type="button"
                  className={"bell-row" + (n.read ? "" : " unread")}
                  onClick={() => void perejti(n)}
                >
                  {/* Строка — перевод weak-vampirebat-44 (docs/18): кружок с буквой
                      предмета, значок вида в углу, текст, время. */}
                  <span className="bell-ava" aria-hidden="true">
                    <span className={"avatar avatar-t" + ottenok(predmet(n))}>
                      {bukva(n) ?? <Icon name={ZNACHOK[n.kind] ?? "inbox"} size={16} />}
                    </span>
                    {bukva(n) !== null && (
                      <span className="bell-badge">
                        <Icon name={ZNACHOK[n.kind] ?? "inbox"} size={10} stroke={2} />
                      </span>
                    )}
                  </span>
                  <span className="bell-body">
                    <span className="bell-text">{podpis(t, n)}</span>
                    <span className="bell-when">{n.created_at ? formatDateTime(n.created_at, locale) : ""}</span>
                  </span>
                </button>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
