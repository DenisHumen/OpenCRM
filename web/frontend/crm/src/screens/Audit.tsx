import { useCallback, useEffect, useState } from "react";

import { EmptyState, ScreenLoading } from "../components/ui";
import { api, type AuditEvent } from "../lib/api";
import { useApp } from "../lib/app";
import { useDebounced } from "../lib/debounce";
import { useFailure } from "../lib/failure";
import { formatDateTime, formatMoney } from "../lib/format";
import type { TranslationKey } from "../lib/i18n";
import { term } from "../lib/terms";

// Ширины общие для шапки и строк, иначе колонки разъезжаются.
const COL = {
  when: { width: 150 } as const,
  who: { width: 150 } as const,
  what: { width: 170 } as const,
  source: { width: 150 } as const,
};

/** Подписи действий. Ключ приходит с сервера (core/services/audit_service.py).
 *
 * Действия, которого здесь нет, экран не скрывает и не роняет — показывает его
 * ключ как есть. Иначе новое действие в сервисе требовало бы правки фронта тем
 * же коммитом, и его просто не стали бы писать в журнал. */
const ACTION: Record<string, TranslationKey> = {
  "deal.stage_changed": "auditActStageChanged",
  "deal.amount_changed": "auditActAmountChanged",
  "deal.prepaid_changed": "auditActPrepaidChanged",
  "stock.move_added": "auditActStockMoveAdded",
  "module.switched": "auditActModuleSwitched",
  "role.created": "auditActRoleCreated",
  "role.permissions_changed": "auditActRolePermissions",
  "role.assigned": "auditActRoleAssigned",
  "staff.role_changed": "auditActRoleChanged",
  "staff.approved": "auditActApproved",
  "staff.rejected": "auditActRejected",
  "staff.disabled": "auditActDisabled",
  "staff.enabled": "auditActEnabled",
  "staff.password_reset": "auditActPasswordReset",
};

const ENTITY: Record<string, TranslationKey> = {
  role: "auditEntRole",
  company: "auditEntCompany",
  board: "auditEntBoard",
  share: "auditEntShare",
  mailbox: "auditEntMailbox",
  client: "auditEntClient",
  product: "auditEntProduct",
  user: "auditEntUser",
  module: "auditEntModule",
  note: "auditEntNote",
  file: "auditEntFile",
};

const SOURCE: Record<string, TranslationKey> = {
  manual: "auditSrcManual",
  telephony_webhook: "auditSrcTelephonyWebhook",
  mail_sync: "auditSrcMailSync",
};

/** Действия, у которых величина — деньги в минорных единицах.
 *
 * В базе они лежат целыми и без валюты (форматирование зависит от локали
 * читающего и от настройки, а обе могут смениться), но на экране «было 500000»
 * заставляет делить в уме — а делят как раз тогда, когда торопятся. */
const MONEY_ACTIONS = new Set(["deal.amount_changed", "deal.prepaid_changed"]);

export function Audit() {
  const { t, locale, workspace } = useApp();
  const [items, setItems] = useState<AuditEvent[] | null>(null);
  const [total, setTotal] = useState(0);
  const [search, setSearch] = useState("");
  const [source, setSource] = useState("");

  const { failure, fail, clear } = useFailure();

  const typed = useDebounced(search);

  const load = useCallback(async () => {
    const params = new URLSearchParams({ per_page: "100" });
    if (typed.trim()) params.set("search", typed.trim());
    if (source) params.set("source", source);
    clear();
    try {
      const data = await api.get<{ items: AuditEvent[]; total: number }>(`/audit?${params}`);
      setItems(data.items);
      setTotal(data.total);
    } catch (e) {
      fail(e);
    }
  }, [typed, source, fail, clear]);

  useEffect(() => {
    void load();
  }, [load]);

  if (!items) return <ScreenLoading error={failure} onRetry={() => void load()} />;

  // Заявку каждый бизнес называет по-своему, и журнал не исключение: «Удалил
  // deal» в мастерской, где всё зовут заказами, читается как чужая запись.
  const entityLabel = (type: string) =>
    type === "deal"
      ? term(workspace.deal_term, locale, "one")
      : ENTITY[type]
        ? t(ENTITY[type])
        : type;

  const actionLabel = (entry: AuditEvent) => {
    if (ACTION[entry.action]) return t(ACTION[entry.action]);
    // Удаление приходит как «объект.deleted» для любого объекта: складывать
    // отдельную подпись под каждый вид значило бы дописывать её при появлении
    // каждого следующего.
    if (entry.action.endsWith(".deleted")) return t("auditActDeleted");
    if (entry.action.endsWith(".restored")) return t("auditActRestored");
    return entry.action;
  };

  // Прочерк, а не пустое место: «было пусто» и «колонка не заполнилась» на
  // экране должны выглядеть по-разному.
  //
  // Этапы показываются ключами (`in_progress`), а не названиями воронки, и это
  // не недоделка: этап могли с тех пор переименовать или заархивировать, и
  // подстановка сегодняшнего названия показала бы не то, что было в тот день.
  const value = (entry: AuditEvent, raw: string | null) => {
    if (raw === null) return "—";
    if (!MONEY_ACTIONS.has(entry.action)) return raw || "—";
    return formatMoney(Number(raw), workspace.currency || "USD", locale);
  };

  return (
    <div className="page">
      <div className="page-head" style={{ alignItems: "flex-start", marginBottom: 20 }}>
        <div>
          <h1 className="page-title">{t("auditLog")}</h1>
          <div className="page-sub">{t("auditSub", { total })}</div>
        </div>
      </div>

      <div className="card card-pad" style={{ marginBottom: 18 }}>
        <div className="field-desc" style={{ marginTop: 0 }}>{t("auditHint")}</div>
      </div>

      <div style={{ display: "flex", gap: 10, alignItems: "center", marginBottom: 16 }}>
        <input
          className="input"
          style={{ maxWidth: 320 }}
          placeholder={t("auditSearch")}
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <select
          className="input"
          style={{ maxWidth: 200 }}
          value={source}
          onChange={(e) => setSource(e.target.value)}
        >
          <option value="">{t("auditAnySource")}</option>
          {Object.entries(SOURCE).map(([key, label]) => (
            <option key={key} value={key}>
              {t(label)}
            </option>
          ))}
        </select>
      </div>

      {items.length === 0 ? (
        <EmptyState title={t("auditEmpty")} />
      ) : (
        <div className="list-card">
          <div className="list-header">
            <span style={COL.when}>{t("colWhen")}</span>
            <span style={COL.who}>{t("auditColWho")}</span>
            <span style={COL.what}>{t("auditColWhat")}</span>
            <span style={{ flex: 1 }}>{t("auditColObject")}</span>
            <span style={{ flex: 1 }}>{t("auditColChange")}</span>
            <span style={COL.source}>{t("auditColSource")}</span>
          </div>
          {items.map((entry) => (
            <div key={entry.id} className="list-row">
              <span style={{ ...COL.when, color: "var(--muted)", fontSize: 12.5 }}>
                {formatDateTime(entry.created_at, locale)}
              </span>
              <span style={{ ...COL.who, fontSize: 12.5 }}>
                {/* Пустой исполнитель бывает только у вебхука и почты, и в
                    журнале он должен быть виден именно как пустой, а не как
                    «система»: «система» отвечала бы на вопрос «кто» неправдой. */}
                {entry.actor_name || (
                  <span style={{ color: "var(--faint)" }}>{t("auditNobody")}</span>
                )}
              </span>
              <span style={{ ...COL.what, color: "var(--text)", fontSize: 12.5 }}>
                {actionLabel(entry)}
              </span>
              <span style={{ flex: 1, minWidth: 0, fontSize: 12.5 }}>
                <span style={{ color: "var(--faint)" }}>{entityLabel(entry.entity_type)}</span>{" "}
                <span style={{ color: "var(--text)" }}>{entry.entity_label}</span>
              </span>
              <span style={{ flex: 1, minWidth: 0, fontSize: 12.5, color: "var(--text)" }}>
                {/* Старое значение рядом с новым: «сумма изменена» без «было
                    5 000, стало 500» не отвечает ни на один вопрос, ради
                    которого в журнал заходят. У удаления величины нет вовсе —
                    там исчез сам объект, и он назван в соседней колонке. */}
                {(entry.value_before !== null || entry.value_after !== null) && (
                  <>
                    <span style={{ color: "var(--muted)" }}>
                      {value(entry, entry.value_before)} →{" "}
                    </span>
                    {value(entry, entry.value_after)}
                  </>
                )}
              </span>
              <span style={{ ...COL.source, color: "var(--muted)", fontSize: 12.5 }}>
                {SOURCE[entry.source] ? t(SOURCE[entry.source]) : entry.source}
                {entry.source_ref && (
                  <span style={{ color: "var(--faint)" }}> · {entry.source_ref}</span>
                )}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
