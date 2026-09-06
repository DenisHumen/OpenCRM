import {
  useCallback,
  useEffect,
  useMemo,
  useState,
  type CSSProperties,
  type FormEvent,
} from "react";

import { Icon } from "../components/Icon";
import { Chip, ConfirmModal, EmptyState, Modal, ScreenLoading, Toggle } from "../components/ui";
import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { useLiveTopic } from "../lib/live";
import { useFailure } from "../lib/failure";
import { useGuard } from "../lib/guard";
import { formatMoney, formatRate, toMinorUnits } from "../lib/format";
import type { FinanceCategory } from "./Finance";

/** Справочник статей, правила начисления и планы — экран настроек, на праве
 * `finance.manage`.
 *
 * Стоит рядом со складами и по той же причине: завести статью, ставку налога
 * или назначить план — решение структурное, вроде «завести юрлицо». Расходы
 * заносят каждый день, а ставку налога заводят раз в год, и это разные
 * полномочия (`finance.create` против `finance.manage`).
 *
 * **Факт по бюджету считает сервер и не хранит.** Вторая колонка «сколько
 * вышло» рядом с планом разошлась бы с операциями при первой же записи задним
 * числом — тот же довод, по которому не хранится остаток склада.
 */

/** Правило начисления. Величина едет ДВУМЯ полями, а не одним универсальным:
 *  процент и сумма — разные единицы, и склеенные в одно поле они врут при
 *  первом же чтении без оглядки на `base`. */
interface FinanceRule {
  id: number;
  name: string;
  /** `income_percent` — процент с прихода, `per_order` — сумма на заказ. */
  base: string;
  /** Куда ложится начисленное. */
  category_id: number;
  /** С какого вида дохода считаем. `null` — со всякого прихода. */
  source_category_id: number | null;
  /** Ставка в базисных пунктах: 5% = 500, 6,5% = 650. */
  rate_bp: number | null;
  amount: number | null;
  is_active: boolean;
  sort_order: number;
  note: string;
  closed: boolean;
}

const BASE_PERCENT = "income_percent";
const BASE_PER_ORDER = "per_order";

/** Заказ, на котором показывается пример: 12 000 в минорных единицах.
 *
 * Число из разговора с заказчиком («заказ 12 000 → налог 600, упаковка 80»):
 * на нём 5% дают ровно 600, и пример узнаётся с первого взгляда. */
const SAMPLE_MINOR = 1_200_000;

/**
 * Начисление по ставке в базисных пунктах — ровно тем же правилом, что на
 * сервере (`finance_service.accrue_minor`): к ближайшей минорной единице,
 * ровная половина ВВЕРХ ПО МОДУЛЮ, симметрично для обоих знаков.
 *
 * Это единственное место во фронтенде, где считается начисление, и считается
 * оно НЕ ради денег: ни одно из этих чисел никуда не отправляется и нигде не
 * показывается как факт. Это пример — «что произойдёт, если завести такое
 * правило», — и спросить его у сервера негде: правила ещё нет, а у уже
 * заведённого ручки «посчитай на 12 000» не существует и заводить её значило бы
 * второй способ получить то же число.
 *
 * Настоящие суммы по заказу приходят с сервера готовыми и здесь не считаются
 * никогда. Расхождение с сервером ловится глазами в первую же минуту: пример
 * обещает 600, а в карточке заказа стоит 601.
 *
 * Целые точны: 2 000 000 000 × 10 000 = 2·10¹³, а двойная точность держит целые
 * до 9·10¹⁵. `Math.floor` вместо `//` — в JS целочисленного деления нет.
 */
function accrueMinor(baseMinor: number, rateBp: number): number {
  const scaled = baseMinor * rateBp;
  const half = 5000;
  return scaled >= 0
    ? Math.floor((scaled + half) / 10000)
    : -Math.floor((-scaled + half) / 10000);
}

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
  const [rules, setRules] = useState<FinanceRule[] | null>(null);
  const [currency, setCurrency] = useState("USD");
  const [showClosed, setShowClosed] = useState(false);
  const [showClosedRules, setShowClosedRules] = useState(false);
  const [editing, setEditing] = useState<FinanceCategory | null>(null);
  const [adding, setAdding] = useState(false);
  const [closing, setClosing] = useState<FinanceCategory | null>(null);
  const [planning, setPlanning] = useState(false);
  const [dropping, setDropping] = useState<Budget | null>(null);
  const [rule, setRule] = useState<FinanceRule | null>(null);
  const [addingRule, setAddingRule] = useState(false);
  const [closingRule, setClosingRule] = useState<FinanceRule | null>(null);
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
      const [list, plans, ruleset] = await Promise.all([
        api.get<{ items: FinanceCategory[] }>("/finance/categories?include_closed=true"),
        api.get<{ items: Budget[]; currency: string }>(`/finance/budgets?${query}`),
        api.get<{ items: FinanceRule[] }>("/finance/rules?include_closed=true"),
      ]);
      setCategories(list.items);
      setBudgets(plans.items);
      setRules(ruleset.items);
      setCurrency(plans.currency);
    } catch (e) {
      fail(e);
    }
  }, [query, fail, clear]);

  useEffect(() => {
    void load();
  }, [load]);

  useLiveTopic("finance", () => void load());

  if (!categories || !budgets || !rules) {
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
        {shown.length === 0 && <EmptyState icon="analytics" title={t("finCategories")} />}
      </div>

      {/* --- правила начисления ---------------------------------------------
          Стоят между статьями и планами не по алфавиту: правило ссылается на
          статью и без неё не заводится, а план — намерение, к начислению
          отношения не имеющее. Порядок на экране повторяет порядок в голове:
          сначала «куда относить», потом «что начислять», потом «сколько
          собирались потратить». */}
      <div className="page-head" style={{ marginTop: 32 }}>
        <div>
          <h1 className="page-title">{t("finRules")}</h1>
          <div className="page-sub">{t("finRulesSub")}</div>
        </div>
        <button className="btn btn-secondary" onClick={() => setAddingRule(true)}>
          <Icon name="plus" stroke={2} />
          {t("finNewRule")}
        </button>
      </div>

      <RulesExample
        rules={rules}
        categories={categories}
        currency={currency}
        style={{ marginBottom: 16 }}
      />

      <div className="field-desc" style={{ marginBottom: 16 }}>
        <label style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
          <input
            type="checkbox"
            checked={showClosedRules}
            onChange={(e) => setShowClosedRules(e.target.checked)}
          />
          {t("finShowClosed")}
        </label>
      </div>

      <div className="list-card">
        {rules
          .filter((row) => showClosedRules || !row.closed)
          .map((row) => {
            const target = categories.find((c) => c.id === row.category_id);
            const source = categories.find((c) => c.id === row.source_category_id);
            return (
              <div className="list-row" key={row.id} style={{ gap: 10 }}>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ color: "var(--text)", fontSize: 13.5, fontWeight: 500 }}>
                    {row.name}
                  </div>
                  {/* Строка под названием — не примечание, а само правило
                      словами: с чего считает, куда кладёт, с какого дохода.
                      Поэтому `--muted`, а не `--faint`: последний в тёмной
                      теме даёт 3.1:1, и определение правила читалось бы на
                      просвет. */}
                  <div style={{ color: "var(--muted)", fontSize: 12 }}>
                    {row.base === BASE_PERCENT
                      ? t("finRuleIncomePercent")
                      : t("finRulePerOrder")}
                    {" · "}
                    {t("finRuleTarget")}: {target ? target.name : "—"}
                    {/* Признак статьи прямо в строке правила. Подсказка в
                        модалке лечит будущие ошибки, а уже сделанные остаются
                        невидимыми: чтобы увидеть их сегодня, надо открыть
                        каждое правило, посмотреть статью, потом открыть статью
                        и посмотреть признак. Здесь расхождение видно с одного
                        взгляда на список. */}
                    {target?.purpose === "tax" && ` · ${t("finTaxes")}`}
                    {target?.purpose === "salary" && ` · ${t("finSalaries")}`}
                    {row.base === BASE_PERCENT &&
                      ` · ${
                        source ? t("finRuleOnIncome", { name: source.name }) : t("finAppliesToAny")
                      }`}
                  </div>
                </div>
                <span
                  style={{
                    color: "var(--text)",
                    fontSize: 13.5,
                    fontWeight: 600,
                    whiteSpace: "nowrap",
                  }}
                  className="money-value"
                >
                  {row.base === BASE_PERCENT ? formatRate(row.rate_bp, locale) : money(row.amount)}
                </span>
                {row.closed ? (
                  <Chip>{t("finRuleClosed")}</Chip>
                ) : (
                  <>
                    {/* Выключатель и закрытие — разные вещи, и путать их нельзя:
                        «сейчас не считаем» возвращается одним нажатием, а
                        закрытое правило из списка уходит. */}
                    <Toggle
                      on={row.is_active}
                      label={row.is_active ? t("finRuleOn") : t("finRuleOff")}
                      onToggle={async () => {
                        try {
                          await api.patch(`/finance/rules/${row.id}`, {
                            is_active: !row.is_active,
                          });
                          void load();
                        } catch (err) {
                          toastError(err);
                        }
                      }}
                    />
                    <button className="btn-icon" title={t("edit")} onClick={() => setRule(row)}>
                      <Icon name="note" size={14} />
                    </button>
                    <button
                      className="btn-icon"
                      title={t("finCloseRule")}
                      onClick={() => setClosingRule(row)}
                    >
                      <Icon name="trash" size={14} />
                    </button>
                  </>
                )}
              </div>
            );
          })}
        {rules.filter((row) => showClosedRules || !row.closed).length === 0 && (
          <EmptyState icon="analytics" title={t("finNoRules")} />
        )}
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
              {/* Полоса выполнения: «сколько от плана уже потрачено» читается
                  раньше двух чисел рядом; перерасход — красным. */}
              <div className="byudzhet-polosa" aria-hidden="true">
                <div
                  className={row.left < 0 ? "beda" : undefined}
                  style={{ width: `${Math.min(100, Math.round((Math.abs(row.fact) * 100) / Math.max(1, row.planned)))}%` }}
                />
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
        {budgets.length === 0 && <EmptyState icon="analytics" title={t("finNoBudgets")} />}
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

      {(addingRule || rule) && (
        <RuleModal
          rule={rule}
          categories={categories.filter((row) => !row.closed)}
          currency={currency}
          onClose={() => {
            setAddingRule(false);
            setRule(null);
          }}
          onSaved={() => {
            setAddingRule(false);
            setRule(null);
            void load();
          }}
          onFailed={toastError}
        />
      )}

      {closingRule && (
        <ConfirmModal
          text={t("finCloseRuleConfirm", { name: closingRule.name })}
          confirmLabel={t("finCloseRule")}
          danger
          onConfirm={async () => {
            try {
              await api.del(`/finance/rules/${closingRule.id}`);
              void load();
            } catch (err) {
              toastError(err);
            }
          }}
          onClose={() => setClosingRule(null)}
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
          text={t("finBudgetDeleteConfirm", { name: dropping.category_name })}
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

/**
 * Пример расчёта прямо на экране настроек.
 *
 * Нужен затем, что правило само по себе нечитаемо: «income_percent, 500, статья
 * 7» не отвечает на единственный вопрос, с которым сюда приходят, — «сколько у
 * меня останется». Пример отвечает на него числом и заодно объясняет две вещи,
 * которых иначе не видно вовсе: что налог считается с ПРИХОДА (а не с прибыли и
 * не в конце месяца) и что стандартные расходы снимаются с КАЖДОГО закрытого
 * заказа.
 *
 * Сумма правится на месте: 12 000 — образец из разговора, но проверять человек
 * будет на своём среднем чеке.
 */
function RulesExample({
  rules,
  categories,
  currency,
  style,
}: {
  rules: FinanceRule[];
  categories: FinanceCategory[];
  currency: string;
  style?: CSSProperties;
}) {
  const { t, locale } = useApp();
  const [typed, setTyped] = useState(String(SAMPLE_MINOR / 100));
  const [source, setSource] = useState("");

  const money = (value: number) => formatMoney(value, currency, locale);
  const base = toMinorUnits(typed);
  const income = categories.filter((row) => row.direction === "income" && !row.closed);
  const live = rules.filter((row) => !row.closed && row.is_active);

  // Вид дохода спрашиваем, только когда он на что-то влияет: пока все ставки
  // считаются со всякого прихода, второй выпадающий список — шум, из которого
  // человек делает ложный вывод, что выбор что-то меняет.
  const picky = live.some((row) => row.base === BASE_PERCENT && row.source_category_id !== null);
  const sourceId = Number(source || income[0]?.id || 0);

  // «Пусто или совпало» — то же условие, по которому отбирает правила сервер
  // (`finance_repo.rules_for_income`). Фиксированные суммы снимаются с любого
  // закрытого заказа и от вида дохода не зависят вовсе.
  const firing = live.filter(
    (row) =>
      row.base === BASE_PER_ORDER ||
      (row.base === BASE_PERCENT &&
        (row.source_category_id === null || row.source_category_id === sourceId)),
  );

  const chargeOf = (row: FinanceRule) =>
    row.base === BASE_PERCENT ? accrueMinor(base, row.rate_bp ?? 0) : row.amount ?? 0;
  // Знак берём у статьи, а не у вида начисления: правило, кладущее деньги в
  // доходную статью, встречается редко, но в примере оно обязано прибавлять, а
  // не отнимать — иначе пример врёт ровно про то, ради чего его читают.
  const signOf = (row: FinanceRule) =>
    categories.find((c) => c.id === row.category_id)?.direction === "income" ? 1 : -1;

  const left = firing.reduce((sum, row) => sum + signOf(row) * chargeOf(row), base);

  return (
    <div className="card card-pad" style={style}>
      <div className="section-head" style={{ marginBottom: 12 }}>
        <div className="metric-title">{t("finExample")}</div>
      </div>

      <div
        style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap", marginBottom: 4 }}
      >
        <span style={{ color: "var(--muted)", fontSize: 13 }}>{t("finExampleOrder")}</span>
        <input
          className="input input-sm"
          style={{ width: 120 }}
          value={typed}
          aria-label={t("finExampleOrder")}
          onChange={(e) => setTyped(e.target.value)}
        />
        {picky && (
          <>
            <span style={{ color: "var(--muted)", fontSize: 13 }}>{t("finExamplePaidAs")}</span>
            <select
              className="input input-sm"
              style={{ width: 190 }}
              value={String(sourceId)}
              aria-label={t("finExamplePaidAs")}
              onChange={(e) => setSource(e.target.value)}
            >
              {income.map((row) => (
                <option key={row.id} value={row.id}>
                  {row.name}
                </option>
              ))}
            </select>
          </>
        )}
      </div>
      <div className="field-desc" style={{ marginBottom: 14 }}>
        {t("finExampleHint")}
      </div>

      {live.length === 0 ? (
        <div className="field-desc" style={{ marginTop: 0 }}>
          {t("finExampleEmpty")}
        </div>
      ) : (
        <div className="calc">
          <div className="calc-row">
            <div className="calc-name">{t("orderTotal")}</div>
            <span className="calc-sum in">{money(base)}</span>
          </div>
          {firing.map((row) => (
            <div className="calc-row" key={row.id}>
              <div className="calc-name">
                {row.name}
                <div className="calc-why">
                  {row.base === BASE_PERCENT
                    ? t("finOfSum", {
                        rate: formatRate(row.rate_bp, locale),
                        sum: money(base),
                      })
                    : t("finRulePerOrder")}
                </div>
              </div>
              <span className="calc-sum">
                {(signOf(row) < 0 ? "− " : "+ ") + money(chargeOf(row))}
              </span>
            </div>
          ))}
          <div className="calc-row calc-total">
            <div className="calc-name">{t("finLeftAfter")}</div>
            <span className="calc-sum">{money(left)}</span>
          </div>
        </div>
      )}
    </div>
  );
}

/** Заведение и правка правила.
 *
 * Ставка вводится процентами, а уезжает в базисных пунктах: перевод делает
 * браузер, целочисленно и на самом краю — как копейки в модалке операции.
 *
 * Под полем величины стоит та же строка примера, что и на экране: человек
 * набирает «6,5» и сразу видит, сколько это в деньгах на заказе из образца.
 * Отдельного «предпросмотра» с кнопкой нет намеренно — предпросмотр, который
 * надо запросить, никто не запрашивает.
 */
function RuleModal({
  rule,
  categories,
  currency,
  onClose,
  onSaved,
  onFailed,
}: {
  rule: FinanceRule | null;
  categories: FinanceCategory[];
  currency: string;
  onClose: () => void;
  onSaved: () => void;
  onFailed: (error: unknown) => void;
}) {
  const { t, locale } = useApp();
  const [form, setForm] = useState({
    name: rule?.name ?? "",
    base: rule?.base ?? BASE_PERCENT,
    category_id: rule ? String(rule.category_id) : "",
    source_category_id: rule?.source_category_id ? String(rule.source_category_id) : "",
    // Проценты и деньги показываем человеку в его единицах; в пункты и копейки
    // переводим на отправке.
    rate: rule?.rate_bp != null ? String(rule.rate_bp / 100) : "",
    amount: rule?.amount != null ? String(rule.amount / 100) : "",
    note: rule?.note ?? "",
    is_active: rule ? rule.is_active : true,
  });
  // Засов, а не флаг состояния: второе правило с тем же смыслом начисляет
  // дважды, и налог тихо удваивается на каждом платеже.
  const guard = useGuard();

  const set = (key: string) => (e: any) => setForm((f) => ({ ...f, [key]: e.target.value }));
  const percent = form.base === BASE_PERCENT;
  const rateBp = Math.round(Number(form.rate.replace(",", ".") || "0") * 100);
  const money = (value: number) => formatMoney(value, currency, locale);
  const income = categories.filter((row) => row.direction === "income");

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!guard.take()) return;
    try {
      const body: Record<string, unknown> = {
        name: form.name,
        base: form.base,
        category_id: Number(form.category_id),
        // Вид дохода осмыслен только у процента. Не обнули мы его при переходе
        // на фиксированную сумму — в базе осталась бы ссылка, которой правило
        // не пользуется, и следующий читатель списка решил бы, что упаковка
        // почему-то привязана к «Выручке».
        source_category_id:
          percent && form.source_category_id ? Number(form.source_category_id) : null,
        is_active: form.is_active,
        note: form.note,
      };
      if (percent) body.rate_bp = rateBp;
      else body.amount = toMinorUnits(form.amount);

      if (rule) await api.patch(`/finance/rules/${rule.id}`, body);
      else await api.post("/finance/rules", body);
      onSaved();
    } catch (err) {
      onFailed(err);
      guard.free();
    }
  };

  return (
    <Modal title={rule ? rule.name : t("finNewRule")} onClose={onClose}>
      <form onSubmit={submit}>
        <div className="field">
          <label className="label">{t("finRuleName")}</label>
          <input className="input" value={form.name} onChange={set("name")} autoFocus required />
        </div>

        <div className="field">
          <label className="label">{t("finRuleBase")}</label>
          <select className="input" value={form.base} onChange={set("base")}>
            <option value={BASE_PERCENT}>{t("finRuleIncomePercent")}</option>
            <option value={BASE_PER_ORDER}>{t("finRulePerOrder")}</option>
          </select>
        </div>

        {percent ? (
          <div className="field">
            <label className="label">{t("finRatePercent")}</label>
            <input className="input" value={form.rate} onChange={set("rate")} required />
            <div className="field-desc">{t("finRateHint")}</div>
            {rateBp > 0 && (
              <div className="field-desc">
                {t("finOfSum", {
                  rate: formatRate(rateBp, locale),
                  sum: money(SAMPLE_MINOR),
                })}
                {" → "}
                {money(accrueMinor(SAMPLE_MINOR, rateBp))}
              </div>
            )}
          </div>
        ) : (
          <div className="field">
            <label className="label">{t("finFixedAmount")}</label>
            <input className="input" value={form.amount} onChange={set("amount")} required />
            <div className="field-desc">{t("finFixedAmountHint")}</div>
          </div>
        )}

        <div className="field">
          <label className="label">{t("finRuleTarget")}</label>
          <select
            className="input"
            value={form.category_id}
            onChange={set("category_id")}
            required
          >
            <option value="">—</option>
            {categories.map((row) => (
              <option key={row.id} value={row.id}>
                {row.name} · {t(row.direction === "income" ? "finIncome" : "finExpense")}
              </option>
            ))}
          </select>
          {/*
            Подсказка говорит про ВЫБРАННУЮ статью, а не вообще, и это не
            придирка. Отчёт о прибыли выносит отдельной строкой то, что легло в
            статью с признаком «налог» или «зарплата»; правило про признак не
            спрашивает вовсе, а итог прибыли верен в обоих случаях. Получалось,
            что человек настраивал налог в обычную статью, видел в отчёте
            «налогов 0» — и верил.

            Общая фраза «налог — в налоговую» рядом со списком из десяти статей
            на вопрос «а ЭТА — налоговая?» не отвечает. Эта отвечает.
          */}
          <div className="field-desc">
            {(() => {
              const vybrana = categories.find((row) => String(row.id) === String(form.category_id));
              if (!vybrana) return t("finRuleTargetHint");
              if (vybrana.purpose === "tax") return t("finRuleTargetTax");
              if (vybrana.purpose === "salary") return t("finRuleTargetSalary");
              return t("finRuleTargetGeneral");
            })()}
          </div>
          {categories.length === 0 && <div className="field-desc">{t("finNoCategories")}</div>}
        </div>

        {percent && (
          <div className="field">
            <label className="label">{t("finAppliesTo")}</label>
            <select
              className="input"
              value={form.source_category_id}
              onChange={set("source_category_id")}
            >
              {/* Пусто — не «не заполнили», а осмысленное значение: налог с
                  оборота обычно один на все виды дохода. */}
              <option value="">{t("finAppliesToAny")}</option>
              {income.map((row) => (
                <option key={row.id} value={row.id}>
                  {row.name}
                </option>
              ))}
            </select>
          </div>
        )}

        <div
          className="field"
          style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 10 }}
        >
          <span style={{ color: "var(--muted)", fontSize: 13 }}>
            {form.is_active ? t("finRuleOn") : t("finRuleOff")}
          </span>
          <Toggle
            on={form.is_active}
            label={t("finRuleOn")}
            onToggle={() => setForm((f) => ({ ...f, is_active: !f.is_active }))}
          />
        </div>

        <div className="field" style={{ marginBottom: 20 }}>
          <label className="label">{t("note")}</label>
          <textarea className="textarea" value={form.note} onChange={set("note")} />
        </div>

        <button className="btn btn-primary" style={{ width: "100%" }} disabled={guard.busy}>
          {rule ? t("save") : t("create")}
        </button>
      </form>
    </Modal>
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
  // Засов, а не флаг состояния: вторая статья с тем же названием разводит
  // расходы по двум строкам отчёта, и половина трат исчезает из той, на
  // которую смотрят. Отпускаем только на отказе: при успехе окно закрывается.
  const guard = useGuard();

  const set = (key: string) => (e: any) => setForm((f) => ({ ...f, [key]: e.target.value }));

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!guard.take()) return;
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
      guard.free();
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
        <button className="btn btn-primary" style={{ width: "100%" }} disabled={guard.busy}>
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
  // Тот же засов, что у статьи: два одинаковых плана на один период — это
  // две строки «план/факт» по одной и той же статье.
  const guard = useGuard();

  const set = (key: string) => (e: any) => setForm((f) => ({ ...f, [key]: e.target.value }));

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!guard.take()) return;
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
      guard.free();
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
        <button className="btn btn-primary" style={{ width: "100%" }} disabled={guard.busy}>
          {t("create")}
        </button>
      </form>
    </Modal>
  );
}
