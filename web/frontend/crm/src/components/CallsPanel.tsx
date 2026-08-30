import { useCallback, useEffect, useRef, useState } from "react";

import { api, type PhoneCall } from "../lib/api";
import { useApp } from "../lib/app";
import { useGuard } from "../lib/guard";
import { formatCallDuration, formatDateTime } from "../lib/format";
import { moduleOn } from "../lib/modules";
import { Icon } from "./Icon";
import { Chip, Dochitat, EmptyState } from "./ui";

/** Иконка звонка: направление, а у пропущенного — то, что он пропущен. */
export function callIcon(call: PhoneCall): string {
  if (call.outcome === "missed") return "callMissed";
  return call.direction === "in" ? "callIn" : "callOut";
}

/** Подпись итога — одна и та же в журнале и во врезке. */
export function useOutcomeLabel() {
  const { t } = useApp();
  return (call: PhoneCall) => {
    switch (call.outcome) {
      case "answered":
        return t("outcomeAnswered");
      case "missed":
        return t("outcomeMissed");
      case "busy":
        return t("outcomeBusy");
      case "failed":
        return t("outcomeFailed");
      case "canceled":
        return t("outcomeCanceled");
      default:
        // пусто — звонок ещё идёт, а не «итог неизвестен навсегда»
        return t("outcomeRinging");
    }
  };
}

/** Кнопка «позвонить»: показывается, только когда блок телефонии включён. */
export function CallButton({
  number,
  dealId,
  onCalled,
}: {
  number: string;
  dealId?: number;
  onCalled?: () => void;
}) {
  const { t, modules, toast, toastError } = useApp();
  // Засов, а не флаг состояния: станция поднимает трубку не мгновенно, кнопка
  // выглядит неотвеченной — и второе нажатие ставило второй вызов на тот же
  // номер. У клиента это два звонка подряд, у нас — две строки в журнале.
  const guard = useGuard();
  if (!moduleOn(modules, "telephony") || !number.trim()) return null;
  return (
    <button
      className="btn btn-secondary"
      disabled={guard.busy}
      onClick={async () => {
        if (!guard.take()) return;
        try {
          await api.post("/telephony/click-to-call", { number, deal_id: dealId ?? null });
          toast(t("clickToCall") + " ✓");
          onCalled?.();
        } catch (e) {
          toastError(e);
        } finally {
          guard.free();
        }
      }}
    >
      <Icon name="callOut" size={13} />
      {t("clickToCall")}
    </button>
  );
}

/**
 * Врезка со звонками в карточке клиента или заявки.
 *
 * Не дубль ленты: сам разговор в ленту уже попал записью (см.
 * telephony_service), а здесь то, что в строку ленты не влезает — длительность,
 * итог и наличие записи разговора.
 *
 * Блок выключен — врезки нет вовсе: карточка не должна стучаться в закрытое API.
 */
export function CallsPanel({
  clientId,
  dealId,
  deals,
  limit = 20,
}: {
  clientId?: number;
  dealId?: number;
  /** Заявки клиента: при них у непривязанного звонка появляется выбор заявки. */
  deals?: { id: number; title: string }[];
  limit?: number;
}) {
  const { t, locale, modules, toastError } = useApp();
  const [items, setItems] = useState<PhoneCall[] | null>(null);
  // Врезка брала двадцать звонков и молчала об остальных. У клиента, с
  // которым разговаривают годами, двадцать первый звонок переставал
  // существовать — а в карточку и заходят, чтобы вспомнить, о чём говорили.
  const [vsego, setVsego] = useState(0);
  const [stranitsa, setStranitsa] = useState(1);
  const [dochityvaem, setDochityvaem] = useState(false);
  // Карточка, которой принадлежит показанный список. Ставится загрузкой,
  // сверяется дочиткой: пока вторая страница звонков одной карточки едет,
  // человек уходит в другую, первая страница новой заменяет список — и
  // опоздавшая страница дописывает к нему чужие звонки. Во врезке две
  // карточки вперемешку, а «всего» от прошлой.
  const otbor_spiska = useRef("");
  const enabled = moduleOn(modules, "telephony");
  const outcomeLabel = useOutcomeLabel();

  /** Отбор врезки без номера страницы. Одна сборка на загрузку и на дочитку:
   *  разойдись они — дочитка приписала бы звонки чужой карточки. */
  const otbor = useCallback(
    (nomer: number) => {
      const params = new URLSearchParams({
        page: String(nomer),
        per_page: String(limit),
      });
      if (clientId) params.set("client_id", String(clientId));
      if (dealId) params.set("deal_id", String(dealId));
      return `/telephony/calls?${params}`;
    },
    [clientId, dealId, limit],
  );

  const load = useCallback(async () => {
    if (!enabled) return;
    otbor_spiska.current = otbor(1);
    try {
      const otvet = await api.get<{ items: PhoneCall[]; total: number }>(otbor(1));
      setItems(otvet.items);
      setVsego(otvet.total);
      setStranitsa(1);
    } catch (e) {
      toastError(e);
    }
  }, [enabled, otbor, toastError]);

  /** Дочитать звонки. Номер растёт ПОСЛЕ ответа: увеличь его до, и отказ на
   *  второй странице оставил бы счётчик на двойке — следующее нажатие
   *  просило бы третью, а вторая пропала бы из врезки навсегда. */
  const dochitat = async () => {
    if (dochityvaem) return;
    setDochityvaem(true);
    const sprosheno = otbor(1);
    try {
      const dalshe = await api.get<{ items: PhoneCall[]; total: number }>(
        otbor(stranitsa + 1),
      );
      // Отбор сменился, пока страница ехала, — ответ чужой.
      if (otbor_spiska.current !== sprosheno) return;
      setItems((bylo) => [...(bylo ?? []), ...dalshe.items]);
      setVsego(dalshe.total);
      setStranitsa((bylo) => bylo + 1);
    } catch (e) {
      toastError(e);
    } finally {
      setDochityvaem(false);
    }
  };

  useEffect(() => {
    void load();
  }, [load]);

  if (!enabled || items === null) return null;

  // Звонок к заявке привязывает человек: станция про заявки ничего не знает, а
  // угадывать по клиенту нельзя — заказов у него может быть пять.
  const attach = async (call: PhoneCall, value: string) => {
    try {
      await api.patch(`/telephony/calls/${call.id}`, {
        deal_id: value ? Number(value) : null,
      });
      await load();
    } catch (e) {
      toastError(e);
    }
  };

  return (
    <div className="list-card">
      {items.map((call) => (
        <div key={call.id} className="list-row" style={{ height: 52 }}>
          <span
            className="feed-icon"
            style={{ color: call.outcome === "missed" ? "var(--danger)" : undefined }}
          >
            <Icon name={callIcon(call)} size={14} />
          </span>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ color: "var(--text)", fontSize: 13.5, fontWeight: 500 }}>
              {call.counterparty || t("unknownNumber")}
            </div>
            <div style={{ color: "var(--faint)", fontSize: 12 }}>
              {formatDateTime(call.started_at, locale)} · {formatCallDuration(call.duration_sec)}
              {call.has_recording && <> · {t("callRecording")}</>}
            </div>
          </div>
          {deals && deals.length > 0 && (
            <select
              className="input"
              style={{ width: 150, height: 28, fontSize: 12.5 }}
              value={call.deal_id ?? ""}
              onChange={(e) => void attach(call, e.target.value)}
            >
              <option value="">{t("noDealLink")}</option>
              {deals.map((deal) => (
                <option key={deal.id} value={deal.id}>
                  {deal.title}
                </option>
              ))}
            </select>
          )}
          {call.outcome === "missed" ? (
            <Chip variant="warning">{outcomeLabel(call)}</Chip>
          ) : (
            <span style={{ color: "var(--muted)", fontSize: 12.5 }}>{outcomeLabel(call)}</span>
          )}
        </div>
      ))}
      <Dochitat
        pokazano={items.length}
        vsego={vsego}
        zanyat={dochityvaem}
        onClick={() => void dochitat()}
      />
      {items.length === 0 && <EmptyState title={t("noCallsYet")} />}
    </div>
  );
}
