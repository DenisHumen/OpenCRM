import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";

import { Icon } from "../components/Icon";
import { Chip, ConfirmModal, EmptyState, Modal, ScreenLoading } from "../components/ui";
import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { useFailure } from "../lib/failure";
import { formatMoney } from "../lib/format";
import type { FinanceCategory } from "./Finance";

/** Справочник статей и планы по ним — экран настроек, на праве `finance.manage`.
 *
 * Стоит рядом со складами и по той же причине: завести статью или назначить план
 * — решение структурное, вроде «завести юрлицо». Расходы заносят каждый день, а
 * статьи заводят раз в год, и это разные полномочия (`finance.create` против
 * `finance.manage`).
 *
 * **Факт по бюджету считает сервер и не хранит.** Вторая колонка «сколько
 * вышло» рядом с планом разошлась бы с операциями при первой же записи задним
 * числом — тот же довод, по которому не хранится остаток склада.
 */

interface Budget {
  id: number;
  category_id: number;
  category_name: string;
  direction: "income" | "expense";
  period_start: string;
  period_end: string;
  planned: number;
  fact: number;
  left: number;
  note: string;
}

export function FinanceSettings() {
  const { t, locale, toast, toastError } = useApp();
  const [categories, setCategories] = useState<FinanceCategory[] | null>(null);
  const [budgets, setBudgets] = useState<Budget[] | null>(null);
  const [currency, setCurrency] = useState("USD");
  const [showClosed, setShowClosed] = useState(false);
  const [editing, setEditing] = useState<FinanceCategory | null>(null);
  const [adding, setAdding] = useState(false);
  const [closing, setClosing] = useState<FinanceCategory | null>(null);
  const [planning, setPlanning] = useState(false);
  const [dropping, setDropping] = useState<Budget | null>(null);
  const { failure, fail, clear } = useFailure();

  // Период планов — тот же, что у экрана денег: смещение зоны едет с датами,
  // иначе факт последнего вечера месяца уехал бы в следующий.
  const query = useMemo(
    () => new URLSearchParams({ tz_offset: String(new Date().getTimezoneOffset()) }).toString(),
    [],
  );

  const load = useCallback(async () => {
    clear();
    try {
      const [list, plans] = await Promise.all([
        api.get<{ items: FinanceCategory[] }>("/finance/categories?include_closed=true"),
        api.get<{ items: Budget[]; currency: string }>(`/finance/budgets?${query}`),
      ]);
      setCategories(list.items);
      setBudgets(plans.items);
      setCurrency(plans.currency);
    } catch (e) {
      fail(e);
    }
  }, [query, fail, clear]);

  useEffect(() => {
    void load();
  }, [load]);

  if (!categories || !budgets) {
    return <ScreenLoading error={failure} onRetry={() => void load()} />;
  }

  const money = (value: number | null) => formatMoney(value, currency, locale);
  const shown = categories.filter((row) => showClosed || !row.closed);

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1 className="page-title">{t("finCategories")}</h1>
          <div className="page-sub">{t("finCategoriesSub")}</div>
        </div>
        <button className="btn btn-primary" onClick={() => setAdding(true)}>
          <Icon name="plus" stroke={2} />
          {t("finNewCategory")}
        </button>
      </div>

      <div className="field-desc" style={{ marginBottom: 16 }}>
        <label style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
          <input
            type="checkbox"
            checked={showClosed}
            onChange={(e) => setShowClosed(e.target.checked)}
          />
          {t("finShowClosed")}
        </label>
      </div>

      <div className="list-card">
        {shown.map((row) => (
          <div className="list-row" key={row.id} style={{ gap: 10 }}>
            <Icon name={row.direction === "income" ? "arrowIn" : "arrowOut"} size={15} />
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ color: "var(--text)", fontSize: 13.5, fontWeight: 500 }}>
                {row.name}
              </div>
              {row.note && (
                <div style={{ color: "var(--faint)", fontSize: 12 }}>{row.note}</div>
              )}
            </div>
            {row.purpose === "tax" && <Chip variant="accent">{t("finTaxes")}</Chip>}
            {row.purpose === "salary" && <Chip variant="accent">{t("finSalaries")}</Chip>}
            {row.closed ? (
              <Chip>{t("finClosed")}</Chip>
            ) : (
              <>
                <button
                  className="btn-icon"
                  title={t("edit")}
                  onClick={() => setEditing(row)}
                >
                  <Icon name="note" size={14} />
                </button>
                <button
                  className="btn-icon"
                  title={t("finCloseCategory")}
                  onClick={() => setClosing(row)}
                >
                  <Icon name="trash" size={14} />
                </button>
              </>
            )}
          </div>
        ))}
        {shown.length === 0 && <EmptyState title={t("finCategories")} />}
      </div>

      <div className="page-head" style={{ marginTop: 32 }}>
        <div>
          <h1 className="page-title">{t("finBudgets")}</h1>
          <div className="page-sub">{t("finBudgetsSub")}</div>
        </div>
        <button className="btn btn-secondary" onClick={() => setPlanning(true)}>
          <Icon name="plus" stroke={2} />
          {t("finNewBudget")}
        </button>
      </div>

      <div className="list-card">
        {budgets.map((row) => (
          <div className="list-row" key={row.id} style={{ gap: 10 }}>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ color: "var(--text)", fontSize: 13.5, fontWeight: 500 }}>
                {row.category_name}
              </div>
              <div style={{ color: "var(--faint)", fontSize: 12 }}>
                {row.period_start} — {row.period_end}
              </div>
            </div>
            <span style={{ color: "var(--muted)", fontSize: 13, whiteSpace: "nowrap" }}>
              {t("finPlanned")}: {money(row.planned)}
            </span>
            <span style={{ color: "var(--text)", fontSize: 13, whiteSpace: "nowrap" }}>
              {t("finFact")}: {money(row.fact)}
            </span>
            {/* Перерасход называем словом, а не отрицательным «осталось»:
                «осталось −12 000» человек читает дважды, прежде чем понять. */}
            {row.left < 0 ? (
              <Chip variant="warning">{t("finOver", { sum: money(-row.left) })}</Chip>
            ) : (
              <span style={{ color: "var(--faint)", fontSize: 12, whiteSpace: "nowrap" }}>
                {t("finLeft")}: {money(row.left)}
              </span>
            )}
            <button
              className="btn-icon"
              title={t("delete")}
              onClick={() => setDropping(row)}
            >
              <Icon name="trash" size={14} />
            </button>
          </div>
        ))}
        {budgets.length === 0 && <EmptyState title={t("finNoBudgets")} />}
      </div>

      {(adding || editing) && (
        <CategoryModal
          category={editing}
          onClose={() => {
            setAdding(false);
            setEditing(null);
          }}
          onSaved={() => {
            setAdding(false);
            setEditing(null);
            void load();
          }}
          onFailed={toastError}
        />
      )}

      {planning && (
        <BudgetModal
          categories={categories.filter((row) => !row.closed)}
          onClose={() => setPlanning(false)}
          onSaved={() => {
            setPlanning(false);
            void load();
          }}
          onFailed={toastError}
        />
      )}

      {closing && (
        <ConfirmModal
          text={t("finCloseCategoryConfirm", { name: closing.name })}
          confirmLabel={t("finCloseCategory")}
          danger
          onConfirm={async () => {
            try {
              await api.del(`/finance/categories/${closing.id}`);
              toast(t("finCloseCategory") + " ✓");
              void load();
            } catch (err) {
              toastError(err);
            }
          }}
          onClose={() => setClosing(null)}
        />
      )}

      {dropping && (
        <ConfirmModal
          text={t("finBudgetDeleteConfirm")}
          confirmLabel={t("delete")}
          danger
          onConfirm={async () => {
            try {
              await api.del(`/finance/budgets/${dropping.id}`);
              void load();
            } catch (err) {
              toastError(err);
            }
          }}
          onClose={() => setDropping(null)}
        />
      )}
    </div>
  );
}

function CategoryModal({
  category,
  onClose,
  onSaved,
  onFailed,
}: {
  category: FinanceCategory | null;
  onClose: () => void;
  onSaved: () => void;
  onFailed: (error: unknown) => void;
}) {
  const { t } = useApp();
  const [form, setForm] = useState({
    name: category?.name ?? "",
    direction: category?.direction ?? "expense",
    purpose: category?.purpose ?? "general",
    note: category?.note ?? "",
  });
  const [busy, setBusy] = useState(false);

  const set = (key: string) => (e: any) => setForm((f) => ({ ...f, [key]: e.target.value }));

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    try {
      if (category) {
        // Направление не отправляем вовсе: сервер откажет, а поле на экране
        // заперто. Послать его «как есть» значило бы получить отказ на
        // сохранении, где человек ничего не менял.
        await api.patch(`/finance/categories/${category.id}`, {
          name: form.name,
          purpose: form.purpose,
          note: form.note,
        });
      } else {
        await api.post("/finance/categories", form);
      }
      onSaved();
    } catch (err) {
      onFailed(err);
      setBusy(false);
    }
  };

  return (
    <Modal title={category ? category.name : t("finNewCategory")} onClose={onClose}>
      <form onSubmit={submit}>
        <div className="field">
          <label className="label">{t("finCategory")}</label>
          <input className="input" value={form.name} onChange={set("name")} autoFocus required />
        </div>
        <div className="field">
          <label className="label">{t("finDirection")}</label>
          <select
            className="input"
            value={form.direction}
            onChange={set("direction")}
            disabled={!!category}
          >
            <option value="income">{t("finIncome")}</option>
            <option value="expense">{t("finExpense")}</option>
          </select>
          {/* Заперто не из вредности: знак операции ставится по направлению
              статьи при записи, и перевернуть его задним числом значит объявить
              весь прошлый расход доходом. */}
          {category && <div className="field-desc">{t("finDirectionFixed")}</div>}
        </div>
        <div className="field">
          <label className="label">{t("finPurpose")}</label>
          <select className="input" value={form.purpose} onChange={set("purpose")}>
            <option value="general">{t("finPurposeGeneral")}</option>
            <option value="tax">{t("finTaxes")}</option>
            <option value="salary">{t("finSalaries")}</option>
          </select>
          <div className="field-desc">{t("finPurposeHint")}</div>
        </div>
        <div className="field" style={{ marginBottom: 20 }}>
          <label className="label">{t("note")}</label>
          <textarea className="textarea" value={form.note} onChange={set("note")} />
        </div>
        <button className="btn btn-primary" style={{ width: "100%" }} disabled={busy}>
          {category ? t("save") : t("create")}
        </button>
      </form>
    </Modal>
  );
}

function BudgetModal({
  categories,
  onClose,
  onSaved,
  onFailed,
}: {
  categories: FinanceCategory[];
  onClose: () => void;
  onSaved: () => void;
  onFailed: (error: unknown) => void;
}) {
  const { t } = useApp();
  const [form, setForm] = useState({
    category_id: "",
    period_start: "",
    period_end: "",
    planned: "",
    note: "",
  });
  const [busy, setBusy] = useState(false);

  const set = (key: string) => (e: any) => setForm((f) => ({ ...f, [key]: e.target.value }));

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    try {
      await api.post("/finance/budgets", {
        category_id: Number(form.category_id),
        period_start: form.period_start,
        period_end: form.period_end,
        // Копейки считает браузер и только здесь, на краю: дальше число едет
        // целым и целым же лежит в базе.
        planned: Math.round(Number(form.planned.replace(",", ".")) * 100),
        note: form.note,
      });
      onSaved();
    } catch (err) {
      onFailed(err);
      setBusy(false);
    }
  };

  return (
    <Modal title={t("finNewBudget")} onClose={onClose}>
      <form onSubmit={submit}>
        <div className="field">
          <label className="label">{t("finCategory")}</label>
          <select className="input" value={form.category_id} onChange={set("category_id")} required>
            <option value="">—</option>
            {categories.map((row) => (
              <option key={row.id} value={row.id}>
                {row.name} · {t(row.direction === "income" ? "finIncome" : "finExpense")}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label className="label">{t("finFrom")}</label>
          <input
            className="input"
            type="date"
            value={form.period_start}
            max={form.period_end || undefined}
            onChange={set("period_start")}
            required
          />
        </div>
        <div className="field">
          <label className="label">{t("finTo")}</label>
          <input
            className="input"
            type="date"
            value={form.period_end}
            min={form.period_start || undefined}
            onChange={set("period_end")}
            required
          />
        </div>
        <div className="field">
          <label className="label">{t("finPlanned")}</label>
          <input className="input" value={form.planned} onChange={set("planned")} required />
          <div className="field-desc">{t("finPlannedHint")}</div>
        </div>
        <div className="field" style={{ marginBottom: 20 }}>
          <label className="label">{t("note")}</label>
          <textarea className="textarea" value={form.note} onChange={set("note")} />
        </div>
        <button className="btn btn-primary" style={{ width: "100%" }} disabled={busy}>
          {t("create")}
        </button>
      </form>
    </Modal>
  );
}
