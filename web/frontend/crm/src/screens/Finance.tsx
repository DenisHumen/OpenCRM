import { useEffect, useMemo, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";

import { Icon } from "../components/Icon";
import { VyborKlienta } from "../components/VyborKlienta";
import { Chip, Dochitat, EmptyState, LoadFailed, Modal, ScreenLoading } from "../components/ui";
import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { useFailure } from "../lib/failure";
import { useGuard } from "../lib/guard";
import { formatDate, formatMoney } from "../lib/format";
import { moduleOn } from "../lib/modules";
import { can } from "../lib/permissions";
import { useReference } from "../lib/reference";

/** Финансы: приход и расход, прибыль за период, разбивка по статьям.
 *
 * **Экран не считает прибыль.** Все числа приходят с сервера
 * (`GET /finance/profit`), где их считает база одним запросом. Сложить операции
 * здесь было бы соблазнительно и неверно: журнал показывает первую сотню, а
 * операций за квартал три тысячи — и прибыль тихо оказалась бы неполной,
 * выглядя при этом правдоподобно.
 *
 * Суммы приходят В ТЕРМИНАХ СТАТЬИ: у расходной «40 000» значит «потратили».
 * Знак из базы (расход лежит минусом) наружу не выходит нарочно — иначе про это
 * соглашение пришлось бы помнить каждому экрану. Минус в ответе означает ровно
 * одно: возврат по этой статье.
 */

export interface FinanceCategory {
  id: number;
  name: string;
  direction: "income" | "expense";
  purpose: "general" | "tax" | "salary";
  note: string;
  closed: boolean;
}

interface Operation {
  id: number;
  category_id: number;
  category_name: string;
  direction: "income" | "expense" | null;
  amount: number;
  comment: string;
  happened_at: string | null;
  client_id: number | null;
  deal_id: number | null;
  author_name: string | null;
}

interface Profit {
  from: string;
  to: string;
  currency: string;
  income: number;
  expense: number;
  profit: number;
  taxes: number;
  salaries: number;
  items: {
    category_id: number;
    name: string;
    direction: "income" | "expense" | null;
    purpose: string;
    amount: number;
    count: number;
  }[];
}

//: Сколько строк журнала показываем. Столько же берут списки клиентов и
//: заметок: страница длиннее не читается, а листалка на первом заходе в раздел
//: нужна реже, чем сужение периода.
const PER_PAGE = 100;

export function Finance() {
  const { t, locale, user, modules, toast, toastError } = useApp();
  // Период НЕ задаётся экраном. Умолчание («с первого числа по сегодня») знает
  // `report_service.parse_period`, и второй экземпляр этого правила во
  // фронтенде разошёлся бы с первым: «прибыль за август» в финансах перестала
  // бы совпадать с выручкой за август в отчётах при одинаковых датах на экране.
  // Пустая строка означает «период выбирает сервер»; в полях при этом стоит то,
  // что он выбрал, — и никакого лишнего запроса это не стоит.
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [data, setData] = useState<{ profit: Profit; operations: Operation[]; total: number } | null>(
    null,
  );
  // Журнал дочитывается страницами. Прежде он честно писал «показано 100 из
  // 4300» — и на этом всё: сказать, что список обрезан, и не дать его
  // раскрыть, немногим лучше, чем обрезать молча.
  const [stranitsa, setStranitsa] = useState(1);
  const [dochityvaem, setDochityvaem] = useState(false);
  const [adding, setAdding] = useState(false);
  const { failure, fail, clear } = useFailure();
  // Периоды переключают быстрее, чем отвечает сервер: без этого счётчика ответ
  // на прошлый период мог бы лечь поверх текущего и показать чужие числа. Он
  // же — кнопка «ещё раз» на экране отказа и перезагрузка после новой операции.
  const [attempt, setAttempt] = useState(0);

  // Смещение зоны браузера едет вместе с датами: «за август» человек понимает
  // по своему календарю, а в базе время в UTC. Без этого 31 августа терял бы
  // вечер, а 1 сентября приносило бы чужие деньги.
  const query = useMemo(() => {
    const params = new URLSearchParams({ tz_offset: String(new Date().getTimezoneOffset()) });
    if (from) params.set("from", from);
    if (to) params.set("to", to);
    return params.toString();
  }, [from, to]);

  // Сменили период — журнал начинается заново: дописывать операции июня к
  // операциям мая значило бы показать вперемешку два разных отбора.
  useEffect(() => {
    setStranitsa(1);
  }, [query]);

  /** Дочитать журнал. Спрашивается ТОЛЬКО журнал: итог прибыли наверху
   * посчитан сервером по всему периоду и от дочитки не меняется, а лишний
   * запрос за ним — лишний круг на каждое нажатие.
   */
  const dochitat_zhurnal = async () => {
    if (dochityvaem) return;
    setDochityvaem(true);
    try {
      const dalshe = await api.get<{ items: Operation[]; total: number }>(
        `/finance/operations?${query}&page=${stranitsa + 1}&per_page=${PER_PAGE}`,
      );
      setData((bylo) =>
        bylo
          ? { ...bylo, operations: [...bylo.operations, ...dalshe.items], total: dalshe.total }
          : bylo,
      );
      setStranitsa((bylo) => bylo + 1);
    } catch (e) {
      toastError(e);
    } finally {
      setDochityvaem(false);
    }
  };

  useEffect(() => {
    let current = true;
    clear();
    Promise.all([
      api.get<Profit>(`/finance/profit?${query}`),
      api.get<{ items: Operation[]; total: number }>(
        `/finance/operations?${query}&page=1&per_page=${PER_PAGE}`,
      ),
    ])
      .then(([profit, journal]) => {
        if (current) setData({ profit, operations: journal.items, total: journal.total });
      })
      .catch((e) => {
        // Отказ — не пустой список: «операций нет» и «сервер не ответил» это
        // разные ответы, и подменять второй первым значит соврать.
        if (current) fail(e);
      });
    return () => {
      current = false;
    };
  }, [query, attempt, fail, clear]);

  if (!data) return <ScreenLoading error={failure} onRetry={() => setAttempt((n) => n + 1)} />;

  const { profit, operations, total } = data;
  const money = (value: number | null) => formatMoney(value, profit.currency, locale);
  // Самая крупная статья задаёт длину полосы: доли от общего итога дали бы
  // десять одинаково коротких полос, по которым ничего не видно.
  const biggest = Math.max(1, ...profit.items.map((row) => Math.abs(row.amount)));

  return (
    <div className="page page-wide">
      <div className="page-head" style={{ alignItems: "flex-start" }}>
        <div>
          <h1 className="page-title">{t("modFinance")}</h1>
          <div className="page-sub">{t("financeSub")}</div>
        </div>
        {can(user, "finance.create") && (
          <button className="btn btn-primary" onClick={() => setAdding(true)}>
            <Icon name="plus" stroke={2} />
            {t("finNewOperation")}
          </button>
        )}
      </div>

      <div className="report-period">
        <span style={{ color: "var(--faint)", fontSize: 13 }}>{t("finPeriod")}</span>
        <input
          className="input report-date"
          type="date"
          value={from || profit.from}
          max={to || profit.to}
          onChange={(e) => e.target.value && setFrom(e.target.value)}
        />
        <span style={{ color: "var(--faint)" }}>—</span>
        <input
          className="input report-date"
          type="date"
          value={to || profit.to}
          min={from || profit.from}
          onChange={(e) => e.target.value && setTo(e.target.value)}
        />
      </div>

      <div className="card card-pad report-card">
        <div className="report-grid">
          <Figure title={t("finIncome")} value={money(profit.income)} />
          <Figure title={t("finExpense")} value={money(profit.expense)} />
          <Figure
            title={t("finProfit")}
            value={money(profit.profit)}
            sub={t("finProfitHint")}
            tone={profit.profit < 0 ? "bad" : "good"}
          />
          {/* Налоги и зарплата — первое, что спрашивают у расходов. Ради этого
              вопроса признак и живёт в справочнике статей.

              Подозрительный ноль объясняется прямо здесь. В строку «Налоги»
              попадает только то, что легло в статью с признаком «налог»;
              правило начисления про признак не спрашивает, а итог прибыли верен
              в обоих случаях. Настроив налог в обычную статью, человек видел
              «налогов 0» — и верил, потому что всё остальное сходилось.

              Условие узкое намеренно. `income > 0` — чтобы подпись не висела
              вечно у того, кто налоги через CRM честно не проводит: у него ноль
              настоящий. Проверка «в периоде не было ни одной налоговой статьи»
              отличает «не настроено» от «настроено, и за период не начислялось»:
              во втором случае ноль — ответ, а не пробел. */}
          <Figure
            title={t("finTaxes")}
            value={money(profit.taxes)}
            sub={
              profit.income > 0 &&
              profit.taxes === 0 &&
              !profit.items.some((row) => row.purpose === "tax")
                ? t("finNoTaxCategory")
                : undefined
            }
          />
          <Figure title={t("finSalaries")} value={money(profit.salaries)} />
        </div>
      </div>

      <div className="card card-pad report-card">
        <div className="section-head" style={{ marginBottom: 14 }}>
          <div className="metric-title">{t("finByCategory")}</div>
        </div>
        {profit.items.length === 0 ? (
          <EmptyState title={t("finNothing")} />
        ) : (
          <div className="funnel">
            {profit.items.map((row) => (
              <div
                key={row.category_id}
                className={"funnel-step kind-" + (row.direction === "income" ? "won" : "lost")}
              >
                <span className="funnel-count">{money(row.amount)}</span>
                <span className="funnel-name">{row.name}</span>
                <span className="funnel-step-share">{row.count}</span>
                <span
                  className="funnel-bar"
                  style={{ width: `${Math.round((Math.abs(row.amount) / biggest) * 100)}%` }}
                />
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="card card-pad report-card">
        <div className="section-head" style={{ marginBottom: 14 }}>
          <div className="metric-title">{t("finOperations")}</div>
          <span className="field-desc">{t("finNoEdit")}</span>
        </div>

        {operations.length === 0 ? (
          <EmptyState title={t("finNothing")} />
        ) : (
          <div className="list-card">
            {operations.map((row) => (
              <div className="list-row" key={row.id} style={{ gap: 10 }}>
                <Icon name={row.direction === "income" ? "arrowIn" : "arrowOut"} size={15} />
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ color: "var(--text)", fontSize: 13.5, fontWeight: 500 }}>
                    {row.category_name}
                    {row.comment && (
                      <span style={{ color: "var(--faint)", fontWeight: 400 }}>
                        {" · "}
                        {row.comment}
                      </span>
                    )}
                  </div>
                  <div style={{ color: "var(--faint)", fontSize: 12 }}>
                    {formatDate(row.happened_at, locale)}
                    {row.author_name ? ` · ${row.author_name}` : ""}
                  </div>
                </div>
                {row.client_id && (
                  <Link to={`/clients/${row.client_id}`} className="btn-icon" title={t("client")}>
                    <Icon name="user" size={14} />
                  </Link>
                )}
                {/* Минус у операции — это возврат, и подпись обязана быть:
                    иначе он читается как «ошиблись знаком». */}
                {row.amount < 0 && <Chip variant="warning">{t("finRefund")}</Chip>}
                <span
                  style={{
                    color: row.direction === "income" ? "var(--success)" : "var(--muted)",
                    fontSize: 13.5,
                    whiteSpace: "nowrap",
                  }}
                >
                  {money(row.amount)}
                </span>
              </div>
            ))}
          </div>
        )}

        {/* Прежде здесь стояла надпись «показано 100 из 4300». Сказать, что
            список обрезан, — половина дела: человек считает по экрану и
            получает не ту сумму, а свериться с итогом наверху ему нечем, тот
            полон. Теперь обрезку не объявляют, а снимают. */}
        <Dochitat
          pokazano={operations.length}
          vsego={total}
          zanyat={dochityvaem}
          onClick={() => void dochitat_zhurnal()}
        />
      </div>

      {adding && (
        <OperationModal
          companiesOn={moduleOn(modules, "companies")}
          onClose={() => setAdding(false)}
          onSaved={() => {
            setAdding(false);
            toast(t("finOperationSaved"));
            setAttempt((n) => n + 1);
          }}
          onFailed={toastError}
        />
      )}
    </div>
  );
}

function Figure({
  title,
  value,
  sub,
  tone,
}: {
  title: string;
  value: string;
  sub?: string;
  tone?: "good" | "bad";
}) {
  return (
    <div className="report-figure">
      <div className="metric-title" style={{ marginBottom: 10 }}>
        {title}
      </div>
      <div
        className="metric-value money-value"
        style={{
          fontSize: 22,
          color: tone ? (tone === "bad" ? "var(--danger)" : "var(--success)") : undefined,
        }}
      >
        {value}
      </div>
      {sub && <div className="metric-sub">{sub}</div>}
    </div>
  );
}

function OperationModal({
  companiesOn,
  onClose,
  onSaved,
  onFailed,
}: {
  companiesOn: boolean;
  onClose: () => void;
  onSaved: () => void;
  onFailed: (error: unknown) => void;
}) {
  const { t } = useApp();
  const categories = useReference<FinanceCategory>("/finance/categories");
  const [imya_klienta, setImyaKlienta] = useState("");
  // `null` — спрашивать нечего: блок фирм выключен. Это не отказ, и строки об
  // отказе быть не должно (см. `useReference`).
  const companies = useReference<{ id: number; name: string }>(companiesOn ? "/companies" : null);
  const [form, setForm] = useState({
    category_id: "",
    amount: "",
    happened_at: "",
    comment: "",
    client_id: "",
    deal_id: "",
    company_id: "",
  });
  // Засов, а не флаг состояния: операция — это движение денег, и вторая
  // такая же строка тихо занижает прибыль ровно на свою сумму. Отпускаем
  // только на отказе: при успехе окно закрывается.
  const guard = useGuard();

  // Заявки — только выбранного клиента. Список всех заявок фирмы в выпадающем
  // это выбор из тысячи, а расход почти всегда относится к работе того клиента,
  // которого уже назвали.
  const deals = useReference<{ id: number; title: string }>(
    form.client_id ? `/deals?client_id=${form.client_id}&per_page=200` : null,
  );

  const set = (key: string) => (e: any) => setForm((f) => ({ ...f, [key]: e.target.value }));

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!guard.take()) return;
    try {
      await api.post("/finance/operations", {
        category_id: Number(form.category_id),
        // Копейки считает браузер и только здесь, на самом краю: дальше число
        // едет целым и целым же лежит в базе. Округление до целой копейки —
        // не вольность: дробных копеек не бывает, а `12.345` иначе доехало бы
        // до сервера и получило законный, но непонятный человеку отказ.
        //
        // Запятая как разделитель — обычный способ набора в русской раскладке.
        amount: Math.round(Number(form.amount.replace(",", ".")) * 100),
        // Полдень, а не полночь: дату человек выбирает по своему календарю, а
        // хранится момент в UTC. Полночь при смещении в любую сторону уезжает
        // на соседние сутки, и операция попадает в чужой месяц.
        happened_at: form.happened_at ? `${form.happened_at}T12:00:00` : null,
        comment: form.comment,
        client_id: form.client_id ? Number(form.client_id) : null,
        deal_id: form.deal_id ? Number(form.deal_id) : null,
        company_id: form.company_id ? Number(form.company_id) : null,
      });
      onSaved();
    } catch (err) {
      onFailed(err);
      guard.free();
    }
  };

  const open = (categories.items ?? []).filter((row) => !row.closed);

  return (
    <Modal title={t("finNewOperation")} onClose={onClose}>
      <form onSubmit={submit}>
        <div className="field">
          <label className="label">{t("finCategory")}</label>
          <select className="input" value={form.category_id} onChange={set("category_id")} required>
            <option value="">—</option>
            {open.map((row) => (
              <option key={row.id} value={row.id}>
                {row.name} · {t(row.direction === "income" ? "finIncome" : "finExpense")}
              </option>
            ))}
          </select>
          {/* «Статей нет» — только когда их действительно нет. Отказ говорит о
              себе сам: без статей форма не отправляется вовсе, и человек перед
              пустым списком решал бы, что финансы в этой системе не настроены. */}
          {categories.failure !== null ? (
            <LoadFailed error={categories.failure} onRetry={categories.reload} />
          ) : (
            categories.items !== null && open.length === 0 && (
              <div className="field-desc">{t("finNoCategories")}</div>
            )
          )}
        </div>
        <div className="field">
          <label className="label">{t("finAmount")}</label>
          <input className="input" value={form.amount} onChange={set("amount")} autoFocus required />
          <div className="field-desc">{t("finAmountHint")}</div>
        </div>
        <div className="field">
          <label className="label">{t("finDate")}</label>
          <input
            className="input"
            type="date"
            value={form.happened_at}
            onChange={set("happened_at")}
          />
        </div>
        <div className="field">
          <label className="label">{t("client")}</label>
          <VyborKlienta
            value={form.client_id ? Number(form.client_id) : null}
            imya={imya_klienta || null}
            pustoy
            onPick={(kto, imya) => {
              setImyaKlienta(imya ?? "");
              setForm((f) => ({ ...f, client_id: kto ? String(kto) : "" }));
            }}
          />
        </div>
        {/* Заявок у клиента может не быть — тогда выбора нет по делу. Отказ —
            другое дело: он прячет выбор, который на самом деле есть, и расход
            остаётся не привязанным к работе, ради которой его понесли. */}
        {form.client_id && deals.failure !== null && (
          <LoadFailed error={deals.failure} onRetry={deals.reload} />
        )}
        {form.client_id && (deals.items ?? []).length > 0 && (
          <div className="field">
            <label className="label">{t("deal")}</label>
            <select className="input" value={form.deal_id} onChange={set("deal_id")}>
              <option value="">—</option>
              {(deals.items ?? []).map((row) => (
                <option key={row.id} value={row.id}>
                  {row.title}
                </option>
              ))}
            </select>
          </div>
        )}
        {companiesOn && (
          <div className="field">
            <label className="label">{t("company")}</label>
            <select className="input" value={form.company_id} onChange={set("company_id")}>
              <option value="">—</option>
              {(companies.items ?? []).map((row) => (
                <option key={row.id} value={row.id}>
                  {row.name}
                </option>
              ))}
            </select>
            {companies.failure !== null && (
              <LoadFailed error={companies.failure} onRetry={companies.reload} />
            )}
          </div>
        )}
        <div className="field" style={{ marginBottom: 20 }}>
          <label className="label">{t("finComment")}</label>
          <textarea className="textarea" value={form.comment} onChange={set("comment")} />
        </div>
        <button className="btn btn-primary" style={{ width: "100%" }} disabled={guard.busy}>
          {t("create")}
        </button>
      </form>
    </Modal>
  );
}
