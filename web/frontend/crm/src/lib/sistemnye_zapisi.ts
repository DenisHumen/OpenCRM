import { UMOLCHANIYA } from "./documents";
import { nazvanieEtapa } from "./etapy";
import type { TranslationKey } from "./i18n";

type T = (key: TranslationKey, params?: Record<string, string | number>) => string;

/**
 * Системные записи ленты и истории хранятся по-английски (`test_zasev_yazyk`:
 * язык интерфейса у каждого свой, а строка в базе одна). На экране их
 * подписываем словами интерфейса — по шаблонам, которыми их пишет сервер
 * (`core/subscriptions.py`, `order_service`, `return_service`, заметки
 * движений склада и операций денег). Незнакомая строка возвращается как
 * есть: перевод — подпись, а не пересказ.
 */
export function podpisSistemnoy(text: string | null | undefined, t: T): string {
  if (!text) return "";
  let m: RegExpMatchArray | null;

  if ((m = text.match(/^Order (\S+) (shipped|received) \((.*)\)$/))) {
    return `${t(m[2] === "shipped" ? "sysOrderShipped" : "sysOrderReceived", { n: m[1] })} (${prichina(m[3], t)})`;
  }
  if ((m = text.match(/^Return (\S+) for order (\S+): (\d+) line\(s\), refund (.*?) \((.*)\)$/))) {
    return `${t("sysReturn", { n: m[1], m: m[2], k: m[3], sum: m[4] })} (${prichina(m[5], t)})`;
  }
  if ((m = text.match(/^Waybill (\S+) posted: (\d+) line\(s\) \((.*)\)$/))) {
    return `${t("sysWaybillPosted", { n: m[1], k: m[2] })} (${prichina(m[3], t)})`;
  }
  if ((m = text.match(/^Stage: (.*?) → (.*?) \((.*)\)$/))) {
    return `${t("sysStage", { a: nazvanieEtapa(t, m[1]), b: nazvanieEtapa(t, m[2]) })} (${prichina(m[3], t)})`;
  }
  if ((m = text.match(/^Stage: (.*?) \((.*)\)$/))) {
    return `${t("sysStageOne", { b: nazvanieEtapa(t, m[1]) })} (${prichina(m[2], t)})`;
  }
  if ((m = text.match(/^Document (\S+) (issued|closed|cancelled|ready|in progress)(?:: (.*?))? \((.*)\)$/))) {
    const chto = {
      issued: "sysDocIssued",
      closed: "sysDocClosed",
      cancelled: "sysDocCancelled",
      ready: "sysDocReady",
      "in progress": "sysDocInProgress",
    }[m[2]] as TranslationKey;
    // Предмет-умолчание («Sales order») — не предмет, а вид: он уже в подписи.
    const predmet = m[3] && !UMOLCHANIYA.has(m[3]) ? `: ${m[3]}` : "";
    return `${t(chto, { n: m[1] })}${predmet} (${prichina(m[4], t)})`;
  }
  if ((m = text.match(/^Act (\S+) carried out: (\d+) line\(s\)$/))) {
    return t("sysAct", { n: m[1], k: m[2] });
  }
  return prichina(text, t);
}

/** Причина в скобках, заметки истории, движений склада и операций денег —
 *  те же слова, что у бумаг. Частные образцы раньше общего «… for order N»:
 *  общий берёт имя правила начисления как есть — это данные, не шаблон. */
export function prichina(text: string, t: T): string {
  let m: RegExpMatchArray | null;
  if ((m = text.match(/^refund by return (\S+) for order (\S+)$/))) return t("sysRefundByReturn", { n: m[1], m: m[2] });
  if ((m = text.match(/^return (\S+) for order (\S+)$/))) return t("sysReturnForOrder", { n: m[1], m: m[2] });
  if ((m = text.match(/^returned by (\S+)$/))) return t("sysReturnedBy", { n: m[1] });
  if ((m = text.match(/^(shipped|received) for order (\S+)$/))) {
    return t(m[1] === "shipped" ? "sysShippedForOrder" : "sysReceivedForOrder", { n: m[2] });
  }
  if ((m = text.match(/^for order (\S+)$/))) return t("sysForOrder", { n: m[1] });
  if ((m = text.match(/^(.+) for order (\S+)$/))) return `${m[1]} · ${t("sysForOrder", { n: m[2] })}`;
  if ((m = text.match(/^for certificate (\S+)$/))) return t("sysForCertificate", { n: m[1] });
  if ((m = text.match(/^written off on closing deal (\d+)$/))) return t("sysWrittenOffOnClosing", { n: m[1] });
  if ((m = text.match(/^transfer (\d+) reversed$/))) return t("sysTransferReversed", { n: m[1] });
  if ((m = text.match(/^reversed by (\S+)$/))) return t("sysReversedBy", { n: m[1] });
  if ((m = text.match(/^adjustment: (.*)$/))) return `${t("sysAdjustment")}: ${m[1]}`;
  if ((m = text.match(/^(?:(.*) · )?moved beyond the balance \((.*)\)$/))) {
    return `${m[1] ? `${m[1]} · ` : ""}${t("sysOverdraft", { n: m[2] })}`;
  }
  if (text === "the item was handed over") return t("sysItemHandedOver");
  if ((m = text.match(/^order (\S+) closed by waybill (\S+)$/))) return t("sysByWaybill", { n: m[2] });
  if ((m = text.match(/^shipped by waybill (\S+)$/))) return t("sysShippedByWaybill", { n: m[1] });
  if ((m = text.match(/^received by waybill (\S+)$/))) return t("sysReceivedByWaybill", { n: m[1] });
  if ((m = text.match(/^reversal of waybill (\S+)$/))) return t("sysReversalOf", { n: m[1] });
  if ((m = text.match(/^reservation extended to (.+)$/))) return t("sysReserveExtended", { t: m[1] });
  if ((m = text.match(/^due (.+)$/))) return t("sysDue", { t: m[1] });
  if (/^order \S+ closed$/.test(text)) return t("sysClosed");
  if (/^order \S+ cancelled$/.test(text)) return t("sysCancelled");
  if (/^order \S+ lines changed$/.test(text)) return t("sysLinesChanged");
  if (text === "due date cleared") return t("sysDueCleared");
  if (text === "handed to the client") return t("sysHanded");
  if (text === "moved on the board") return t("sysMovedOnBoard");
  if (text === "deal lost on the board") return t("sysDealLost");
  if (text === "warehouse module off, no stock moves") return t("sysWarehouseOff");
  return text;
}
