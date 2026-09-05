import { useCallback, useEffect, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { History } from "../components/History";
import { Icon } from "../components/Icon";
import { useLabelsOn } from "../components/ProductBarcodes";
import { Chip, ConfirmModal, LoadFailed, Modal, ScreenLoading } from "../components/ui";
import { VyborKlienta } from "../components/VyborKlienta";
import { WarehousePicker, useWarehouses } from "../components/Warehouses";
import { api, ApiError } from "../lib/api";
import { useApp } from "../lib/app";
import { useLiveTopic } from "../lib/live";
import { useDebounced } from "../lib/debounce";
import { useFailure } from "../lib/failure";
import { useGuard } from "../lib/guard";
import { formatDate, formatDateTime, formatMoney, formatQuantity, formatRate, toMinorUnits } from "../lib/format";
import { moduleOn } from "../lib/modules";
import { orderStatusLabel, statusLabel } from "../lib/documents";
import { can } from "../lib/permissions";
import { useReference } from "../lib/reference";
import type { FinanceCategory } from "./Finance";
import { type Order } from "./Orders";
import type { Product } from "./Warehouse";

/** Карточка заказа: позиции, сборка сканером, проведение.
 *
 * Проведение — единственное место, где заказ трогает склад. До него он только
 * держит обещание (резерв), после — оно снято, а товар списан. Поэтому кнопка
 * называется действием («Отгрузить», «Принять»), а не состоянием.
 */
export function OrderCard() {
  const { id } = useParams();
  const { t, locale, user, modules, workspace, toast, toastError } = useApp();
  const navigate = useNavigate();
  const [order, setOrder] = useState<Order | null>(null);
  // Проведение трогает склад: отгрузка списывает, приёмка приходует. Засов, а
  // не флаг состояния — двойное нажатие записало бы движения дважды, а остаток
  // равен их сумме и отличить лишнее от настоящего потом нечем.
  const guard = useGuard();
  const [shortage, setShortage] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<"cancel" | "delete" | null>(null);
  const places = useWarehouses();
  const [place, setPlace] = useState<number | null>(null);
  // Окно «кому отгружаем»: открыто только у открытого заказа — проведённый
  // записан, и для кого он был, не меняется (владелец, 05.09.2026).
  const [klientOkno, setKlientOkno] = useState(false);
  const [vybor, setVybor] = useState<{ id: number | null; imya: string | null }>({ id: null, imya: null });
  const { failure, fail, clear } = useFailure();

  useLiveTopic("orders", (s) => {
    if (s.resync || s.hints.some((h) => h.id === Number(id))) void load();
  });

  const load = useCallback(async () => {
    clear();
    try {
      setOrder(await api.get<Order>(`/orders/${id}`));
    } catch (e) {
      if (e instanceof ApiError && (e.status === 404 || e.status === 403)) {
        toastError(e);
        navigate("/orders");
        return;
      }
      fail(e);
    }
  }, [id, toastError, navigate, fail, clear]);

  useEffect(() => {
    void load();
  }, [load]);

  if (!order) return <ScreenLoading error={failure} onRetry={() => void load()} />;

  const outgoing = order.kind === "sales_order";
  const open = order.status === "issued" || order.status === "ready";

  const close = async (force: boolean) => {
    if (!guard.take()) return;
    try {
      await api.post(`/orders/${order.id}/close`, {
        warehouse_id: place,
        confirm_negative: force,
      });
      setShortage(null);
      toast(outgoing ? t("orderShip") : t("orderReceive"));
      await load();
    } catch (err) {
      if (err instanceof ApiError && err.code === "not_enough_stock") {
        // Не отказ насовсем: показываем, чего именно не хватает, и даём
        // подтвердить. Список позиций приходит с сервера — иначе человеку
        // пришлось бы сверять заказ со складом построчно руками.
        setShortage(err.message);
      } else {
        toastError(err);
      }
    } finally {
      guard.free();
    }
  };

  return (
    <div className="page">
      <Link
        to="/orders"
        style={{ display: "inline-flex", alignItems: "center", gap: 6, color: "var(--muted)", fontSize: 13, marginBottom: 20 }}
      >
        <Icon name="arrowLeft" size={14} />
        {t("orders")}
      </Link>

      <div className="page-head" style={{ alignItems: "flex-start", marginBottom: 20 }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <h1 className="page-title" style={{ fontSize: 22 }}>
            {order.number}
          </h1>
          <div className="page-sub" style={{ marginTop: 5, display: "flex", gap: 8, alignItems: "center" }}>
            <Chip>{outgoing ? t("orderKindSales") : t("orderKindPurchase")}</Chip>
            <Chip variant={order.status === "closed" ? "success" : undefined}>
              {orderStatusLabel(t, order.status, order.kind)}
            </Chip>
            {order.client_id ? (
              <Link
                to={`/clients/${order.client_id}`}
                title={open ? undefined : t("orderClientLocked")}
                style={{ color: "var(--muted)", fontSize: 12.5, textDecoration: "underline", textUnderlineOffset: 2 }}
              >
                {order.client_name ?? t("client")}
              </Link>
            ) : (
              <span style={{ color: "var(--faint)", fontSize: 12.5 }}>{t("noClient")}</span>
            )}
            {open && can(user, "orders.edit") && (
              <button
                type="button"
                className="text-link"
                onClick={() => {
                  setVybor({ id: order.client_id, imya: order.client_name });
                  setKlientOkno(true);
                }}
              >
                {order.client_id ? t("orderChangeClient") : t("orderAttachClient")}
              </button>
            )}
            {order.site_ref && <span style={{ color: "var(--faint)", fontSize: 12.5 }}>{t("orderFromSite", { ref: order.site_ref })}</span>}
            {order.reserved_until && (
              order.reserve_expired ? (
                <Chip variant="warning">{t("orderReserveExpired")}</Chip>
              ) : (
                <span style={{ color: "var(--faint)", fontSize: 12.5 }}>{t("orderReserveUntil", { t: formatDateTime(order.reserved_until, locale) })}</span>
              )
            )}
          </div>
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          {order.status === "issued" && order.lines.length > 0 && (
            <button
              className="btn btn-secondary btn-sm"
              onClick={async () => {
                try {
                  await api.post(`/orders/${order.id}/ready`);
                  await load();
                } catch (err) {
                  toastError(err);
                }
              }}
            >
              {t("orderReady")}
            </button>
          )}
          {/* Печать — обычная ссылка в новую вкладку: это window.print() на
              настоящей странице со своим @page, и выборкой её не получить. */}
          <a
            className="btn btn-secondary btn-sm"
            href={`/api/v1/orders/${order.id}/print`}
            target="_blank"
            rel="noreferrer"
          >
            <Icon name="printer" size={14} />
            {t("orderPrint")}
          </a>
        </div>
      </div>

      {/* Бумаги, выписанные по этому заказу. Закрытие выписывает накладную, и
          не показать КАКУЮ значит оставить человека искать её глазами по
          всему списку накладных.

          Ключа нет вовсе, когда блок накладных выключен, — тогда и строки
          нет: выключенный блок исчезает целиком, включая упоминания о себе.
          Пустой массив у заказа, закрытого до переезда, — тоже молчание:
          обещать бумагу, которой нет, хуже, чем не обещать. */}
      {order.waybills && order.waybills.length > 0 && (
        <div className="card" style={{ padding: "10px 14px", marginBottom: 16 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
            <Icon name="receipt" size={14} />
            <span className="page-sub" style={{ marginTop: 0 }}>{t("orderWaybills")}</span>
            {order.waybills.map((w) => (
              <Link key={w.id} className="chip" to={`/waybills/${w.id}`}>
                {w.number} · {statusLabel(t, w.status, w.kind)}
              </Link>
            ))}
          </div>
        </div>
      )}

      <div className="card" style={{ overflow: "hidden", marginBottom: 16 }}>
        <div className="list-header">
          <span style={{ flex: 1 }}>{t("orderLineName")}</span>
          <span style={{ width: 130, textAlign: "right" }}>{t("quantity")}</span>
          <span style={{ width: 110, textAlign: "right" }}>{t("sellPrice")}</span>
          {open && <span style={{ width: 34 }} />}
        </div>
        {order.lines.map((line) => (
          <div className="list-row" key={line.id}>
            <span style={{ flex: 1, minWidth: 0, color: "var(--text)", fontSize: 13.5 }}>
              {line.name}
            </span>
            <span style={{ width: 130, textAlign: "right", fontSize: 13 }}>
              {formatQuantity(line.quantity_milli)}
              {/* Собранное показываем только когда оно есть и не сходится:
                  «собрано 3 из 3» на каждой строке — шум, прячущий те строки,
                  где расхождение настоящее. */}
              {line.picked_milli > 0 &&
                (line.picked_milli === line.quantity_milli ? (
                  // Сошлось — короткая пометка вместо «собрано 3 из 3»: на
                  // каждой строке она была бы шумом, прячущим те строки, где
                  // расхождение настоящее.
                  <span style={{ color: "var(--success)", fontSize: 12, marginLeft: 6 }}>
                    ✓ {t("orderPickedAll")}
                  </span>
                ) : (
                  <span style={{ color: "var(--warning)", fontSize: 12, marginLeft: 6 }}>
                    {t("orderPicked", {
                      done: formatQuantity(line.picked_milli),
                      all: formatQuantity(line.quantity_milli),
                    })}
                  </span>
                ))}
            </span>
            <span style={{ width: 110, textAlign: "right", color: "var(--muted)", fontSize: 12.5 }}>
              {formatMoney(line.price, workspace.currency, locale)}
            </span>
            {open && (
              <button
                className="btn-icon"
                title={t("delete")}
                onClick={async () => {
                  try {
                    await api.del(`/orders/${order.id}/lines/${line.id}`);
                    await load();
                  } catch (err) {
                    toastError(err);
                  }
                }}
              >
                <Icon name="trash" size={14} />
              </button>
            )}
          </div>
        ))}
        <div className="list-row" style={{ background: "var(--surface-2)" }}>
          <span style={{ flex: 1, color: "var(--muted)", fontSize: 12.5 }}>{t("orderTotal")}</span>
          <span style={{ color: "var(--text)", fontSize: 14, fontWeight: 600 }}>
            {formatMoney(order.total, workspace.currency, locale)}
          </span>
        </div>
      </div>

      {open && <AddLine orderId={order.id} onAdded={load} />}
      {open && <PickScanner orderId={order.id} onPicked={load} />}

      {open && (
        <div className="card card-pad" style={{ marginTop: 16, display: "flex", gap: 10, flexWrap: "wrap", alignItems: "center" }}>
          {/* Склад выбирается явно: молчаливое списание с основного однажды
              снимет деталь не оттуда, где её взяли. */}
          <WarehousePicker places={places} value={place ?? places?.items[0]?.id ?? null} onChange={setPlace} inline />
          <button
            className="btn btn-primary"
            disabled={guard.busy || order.lines.length === 0}
            onClick={() => void close(false)}
          >
            {outgoing ? t("orderShip") : t("orderReceive")}
          </button>
          <button className="btn btn-secondary" disabled={guard.busy} onClick={() => setConfirm("cancel")}>
            {t("orderCancel")}
          </button>
          {can(user, "orders.edit") && (
            <button className="text-link danger" disabled={guard.busy} onClick={() => setConfirm("delete")}>
              {t("paperDelete")}
            </button>
          )}
          {shortage && (
            <div style={{ flexBasis: "100%" }}>
              <div className="field-desc" style={{ color: "var(--warning)" }}>{shortage}</div>
              <button className="btn btn-secondary btn-sm" disabled={guard.busy} onClick={() => void close(true)}>
                {t("orderShipForce")}
              </button>
            </div>
          )}
        </div>
      )}

      {/* Деньги по заказу — врезка блока `finance`, а не часть заказа.
          Выключен блок или нет права смотреть деньги — заказ работает целиком,
          и это не половина экрана, а правда о системе без финансов. */}
      {klientOkno && (
        <Modal title={order.client_id ? t("orderChangeClient") : t("orderAttachClient")} onClose={() => setKlientOkno(false)}>
          <div className="field">
            <label className="label">{t("client")}</label>
            <VyborKlienta
              value={vybor.id}
              imya={vybor.imya}
              onPick={(id, imya) => setVybor({ id, imya })}
              pustoy
              pustoyPodpis={t("noClient")}
            />
            <div className="field-desc">{t("orderClientHint")}</div>
          </div>
          <div style={{ display: "flex", gap: 8, justifyContent: "flex-end", marginTop: 14 }}>
            <button type="button" className="btn btn-secondary" onClick={() => setKlientOkno(false)}>
              {t("cancel")}
            </button>
            <button
              type="button"
              className="btn btn-primary"
              disabled={guard.busy}
              onClick={async () => {
                if (!guard.take()) return;
                try {
                  // Ответ ручки — заказ без истории и бумаг; карточка перечитывается
                  // целиком, как после «готов» и «отгрузить».
                  await api.post<Order>(`/orders/${order.id}/client`, { client_id: vybor.id });
                  setKlientOkno(false);
                  await load();
                } catch (err) {
                  toastError(err);
                } finally {
                  guard.free();
                }
              }}
            >
              {t("save")}
            </button>
          </div>
        </Modal>
      )}
      {moduleOn(modules, "finance") && can(user, "finance.view") && <OrderMoney order={order} />}

      {/* Назад по проведённому заказу дорога одна — возврат (владелец,
          05.09.2026): отмены проведения нет, бумага о свершившемся не
          переписывается. Заводится черновиком и открывается сразу. */}
      {order.returns && (
        <div className="card" style={{ padding: "10px 14px", marginBottom: 16 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
            <Icon name="arrowIn" size={14} />
            <span className="page-sub" style={{ marginTop: 0 }}>{t("returnsOfOrder")}</span>
            {order.returns.map((v) => (
              <Link key={v.id} className="chip" to={`/returns/${v.id}`}>
                {v.number} · {statusLabel(t, v.status, "return")}
                {v.refund !== null && v.status === "closed" && ` · ${formatMoney(v.refund, workspace.currency, locale)}`}
              </Link>
            ))}
            {order.status === "closed" && can(user, "orders.create") && (
              <button
                type="button"
                className="btn btn-secondary btn-sm"
                disabled={guard.busy}
                onClick={async () => {
                  if (!guard.take()) return;
                  try {
                    const v = await api.post<{ id: number }>(`/orders/${order.id}/returns`);
                    navigate(`/returns/${v.id}`);
                  } catch (err) {
                    toastError(err);
                    guard.free();
                  }
                }}
              >
                {t("returnNew")}
              </button>
            )}
          </div>
        </div>
      )}

      {/* История заказа. Заведена по беде: закрытие при выключенном складе
          пишет в примечание «движений нет», и показать это было негде —
          человек не отличал «списали» от «не списали». */}
      <div style={{ marginTop: 20 }}>
        <History
          events={order.events}
          label={(status) => orderStatusLabel(t, status, order.kind)}
        />
      </div>

      {confirm && (
        <ConfirmModal
          text={confirm === "cancel" ? t("orderCancelConfirm") : t("paperDeleteConfirm", { number: order.number })}
          confirmLabel={confirm === "cancel" ? t("orderCancel") : t("paperDelete")}
          danger
          onConfirm={async () => {
            try {
              if (confirm === "delete") {
                await api.del(`/orders/${order.id}`);
                toast(t("paperDeleted"));
                navigate("/orders");
                return;
              }
              await api.post(`/orders/${order.id}/cancel`);
              await load();
            } catch (err) {
              toastError(err);
            }
          }}
          onClose={() => setConfirm(null)}
        />
      )}
    </div>
  );
}

/** Начисление по заказу: голова цепочки поправок, уже сложенная сервером. */
interface Accrual {
  id: number;
  rule_id: number | null;
  category_id: number;
  category_name: string;
  direction: "income" | "expense" | null;
  purpose: string;
  comment: string;
  /** Сумма В ТЕРМИНАХ СТАТЬИ и с учётом всех поправок: было 80, стало 140. */
  amount: number;
  /** Снимок ставки на момент начисления. Пусто — начислено фиксированной суммой. */
  rate_bp: number | null;
  base_amount: number | null;
  happened_at: string | null;
  reverted: boolean;
}

interface OrderMoneyData {
  document_id: number;
  /** Пусто — блок бланков выключен, и сравнивать полученное не с чем. */
  total: number | null;
  /** Состояние бланка. Пусто — бланки выключены. Различает два «остатка нет». */
  status: string | null;
  received: number;
  /** Пусто у отменённого и при выключенных бланках: вопрос «сколько взять» не задан. */
  due: number | null;
  paid: boolean | null;
  accruals: Accrual[];
  currency: string;
}

/**
 * Разбор суммы заказа: из чего сложилось то, что осталось.
 *
 * **Ни одно число здесь не считается заново.** Получено, остаток, «оплачен» и
 * сумма каждого начисления приходят с сервера посчитанными запросом; браузеру
 * остаётся вычесть одно из другого ради строки «Остаётся». Это не то же самое,
 * что запрещено на экране денег: там журнал показывает первую сотню операций из
 * трёх тысяч, и сложение по экрану дало бы правдоподобно неверную прибыль.
 * Здесь список начислений по заказу полон — сервер отдаёт его целиком, без
 * листалки, — и вычитание не порождает второго способа получить число.
 *
 * Считается всё ПО ОПЛАТЕ, а не по отгрузке: приход — это то, что получено, а
 * не то, что выписано. Сказано это на экране словами, потому что вывод
 * «отгрузили на 12 000, а тут 5 000» человек сделает не в ту сторону.
 */
function OrderMoney({ order }: { order: Order }) {
  const { t, locale, user, toast } = useApp();
  const [money, setMoney] = useState<OrderMoneyData | null>(null);
  const [failure, setFailure] = useState<unknown>(null);
  const [paying, setPaying] = useState(false);
  const [attempt, setAttempt] = useState(0);
  // «Было 80» рядом с поправленной цифрой. Держится до перезагрузки страницы и
  // нарочно: сервер отдаёт ИТОГ цепочки, а не первую записанную сумму, и взять
  // «было» из ответа неоткуда. Постоянный ответ на этот вопрос живёт в журнале
  // действий — о нём говорит подсказка под списком.
  const [was, setWas] = useState<Record<number, number>>({});

  useEffect(() => {
    let current = true;
    setFailure(null);
    api
      .get<OrderMoneyData>(`/finance/documents/${order.id}/money`)
      .then((data) => {
        if (current) setMoney(data);
      })
      .catch((e) => {
        // Отказ — не «денег нет»: врезка говорит словами сервера и даёт
        // повторить, а не показывает пустой разбор.
        if (current) setFailure(e);
      });
    return () => {
      current = false;
    };
    // Статус в зависимостях не для красоты: начисления заводятся ровно в момент
    // закрытия заказа, и без перечитывания разбор остался бы вчерашним.
  }, [order.id, order.status, attempt]);

  if (failure !== null) {
    return (
      <div className="card card-pad" style={{ marginTop: 16 }}>
        <LoadFailed error={failure} onRetry={() => setAttempt((n) => n + 1)} />
      </div>
    );
  }
  if (!money) return null;

  const sum = (value: number | null) => formatMoney(value, money.currency, locale);
  const total = money.total ?? order.total;
  const canPay = can(user, "finance.create");
  // Отменённые начисления в вычитание не идут: сторно уже сложено сервером в
  // ноль, а показываются они затем, чтобы вопрос «куда делась упаковка» имел
  // ответ на экране, а не в журнале.
  const charged = money.accruals.reduce(
    (acc, row) => acc + (row.direction === "income" ? -row.amount : row.amount),
    0,
  );

  return (
    <div className="card card-pad" style={{ marginTop: 16, marginBottom: 16 }}>
      <div className="section-head" style={{ marginBottom: 12 }}>
        <div className="metric-title">{t("orderMoney")}</div>
        {/* Оплату принимаем только по заказу покупателя: у заказа поставщику
            деньги идут в другую сторону, и «принять оплату» на нём означало бы
            записать приход там, где был расход. */}
        {canPay && order.kind === "sales_order" && (
          <button className="btn btn-secondary btn-sm" onClick={() => setPaying(true)}>
            {t("finTakePayment")}
          </button>
        )}
      </div>

      <div className="report-grid">
        <div className="report-figure">
          <div className="metric-title" style={{ marginBottom: 10 }}>
            {t("orderTotal")}
          </div>
          <div className="metric-value money-value" style={{ fontSize: 22 }}>
            {sum(total)}
          </div>
        </div>
        <div className="report-figure">
          <div className="metric-title" style={{ marginBottom: 10 }}>
            {t("finReceived")}
          </div>
          <div className="metric-value money-value" style={{ fontSize: 22 }}>
            {sum(money.received)}
          </div>
        </div>
        {/*
          Отменённый бланк разводится на два случая, и это главное здесь.

          «Остаток к оплате» отвечает на вопрос «сколько ещё с человека взять».
          У отменённого такого числа не существует — не ноль, а вопрос не задан;
          сервер поэтому и отдаёт `due: null`, отличая это от выключенных
          бланков состоянием (`status`).

          Но пустой остаток ещё не значит «все в расчёте». Отмена бумаги и
          возврат денег — РАЗНЫЕ действия, и сделать можно только одно: заказ
          отменён, а оплата не возвращена — обычное состояние. Единый чип
          «платить нечего» на нём успокаивал бы ровно там, где мы держим чужие
          деньги. Поэтому при непустом `received` показывается «К возврату».
        */}
        {money.status === "cancelled" ? (
          <div className="report-figure">
            <div className="metric-title" style={{ marginBottom: 10 }}>
              {money.received > 0 ? t("orderToRefund") : t("finDueLeft")}
            </div>
            {money.received > 0 ? (
              <div className="metric-value money-value" style={{ fontSize: 22 }}>
                {sum(money.received)}
              </div>
            ) : (
              <Chip>{t("orderNothingToPay")}</Chip>
            )}
          </div>
        ) : (
          money.due !== null && (
            <div className="report-figure">
              <div className="metric-title" style={{ marginBottom: 10 }}>
                {t("finDueLeft")}
              </div>
              {money.paid ? (
                <Chip variant="success">{t("dealPaidInFull")}</Chip>
              ) : (
                <div className="metric-value money-value" style={{ fontSize: 22 }}>
                  {sum(money.due)}
                </div>
              )}
            </div>
          )
        )}
      </div>

      <div className="field-desc" style={{ marginTop: 0, marginBottom: 14 }}>
        {t("finMoneyByPayment")}
      </div>

      <div className="section-head" style={{ marginBottom: 6 }}>
        <div className="metric-title">{t("finAccruals")}</div>
      </div>

      {money.accruals.length === 0 ? (
        <div className="field-desc" style={{ marginTop: 0 }}>
          {t("finNoAccruals")}
        </div>
      ) : (
        <>
          <div className="calc">
            <div className="calc-row">
              <div className="calc-name">{t("finReceived")}</div>
              <span className="calc-sum in">{sum(money.received)}</span>
            </div>
            {money.accruals.map((row) => (
              <AccrualRow
                key={row.id}
                row={row}
                currency={money.currency}
                canEdit={canPay}
                was={was[row.id]}
                onAdjusted={(id, before) => {
                  setWas((prev) => ({ ...prev, [id]: prev[id] ?? before }));
                  setAttempt((n) => n + 1);
                }}
              />
            ))}
            <div className="calc-row calc-total">
              <div className="calc-name">{t("finLeftAfter")}</div>
              <span className="calc-sum">{sum(money.received - charged)}</span>
            </div>
          </div>
          {canPay && (
            <div className="field-desc">{t("finAdjustHint")}</div>
          )}
        </>
      )}

      {paying && (
        <PaymentModal
          order={order}
          due={money.due}
          currency={money.currency}
          onClose={() => setPaying(false)}
          onSaved={() => {
            setPaying(false);
            toast(t("finPaymentSaved"));
            setAttempt((n) => n + 1);
          }}
        />
      )}
    </div>
  );
}

/**
 * Строка начисления с правкой суммы НА МЕСТЕ.
 *
 * Меняют 80 на 140 — и в строке сразу стоит 140, потому что сервер отдаёт
 * ИТОГ цепочки поправок, а не последнее записанное число. Исходная операция при
 * этом не правится и не удаляется: рядом заводится операция на разницу, и в
 * журнале видно обе.
 *
 * Разницу считает сервер: в теле запроса едет НОВАЯ ИТОГОВАЯ сумма. Вводить
 * разницу руками — верный способ ошибиться на знак.
 */
function AccrualRow({
  row,
  currency,
  canEdit,
  was,
  onAdjusted,
}: {
  row: Accrual;
  currency: string;
  canEdit: boolean;
  /** Сумма до правки, сделанной в этот заход. */
  was: number | undefined;
  onAdjusted: (id: number, before: number) => void;
}) {
  const { t, locale, toastError } = useApp();
  const [editing, setEditing] = useState(false);
  const [typed, setTyped] = useState("");
  // Засов: правка заводит операцию, и второе нажатие завело бы вторую поправку
  // на ту же разницу — то есть 200 вместо 140, молча и правдоподобно.
  const guard = useGuard();

  const sum = (value: number) => formatMoney(value, currency, locale);

  const save = async () => {
    const next = toMinorUnits(typed);
    // Та же сумма — просто закрываем поле. Сервер на это отвечает отказом
    // `nothing_to_adjust`, и он прав, но человеку показывать отказ за то, что
    // он передумал, не за что.
    if (next === row.amount) {
      setEditing(false);
      return;
    }
    if (!guard.take()) return;
    try {
      await api.patch(`/finance/accruals/${row.id}`, { amount: next });
      setEditing(false);
      onAdjusted(row.id, row.amount);
    } catch (err) {
      toastError(err);
    } finally {
      guard.free();
    }
  };

  return (
    // Комментарий начисления («Упаковка по заказу 2026-000001») в строку не
    // выводится: на карточке самого заказа он повторяет и статью, и номер,
    // который стоит в заголовке. Под курсором он остаётся — на случай двух
    // правил, кладущих деньги в одну статью.
    <div className={"calc-row" + (row.reverted ? " calc-off" : "")} title={row.comment || undefined}>
      <div className="calc-name">
        {row.category_name}
        <div className="calc-why">
          {/* Ставка и база — снимок на момент начисления: правило с тех пор
              могли поправить, и «5% от 12 000» отвечает на вопрос «почему тут
              600, а в соседнем заказе 840». */}
          {row.rate_bp !== null && row.base_amount !== null
            ? t("finOfSum", {
                rate: formatRate(row.rate_bp, locale),
                sum: sum(row.base_amount),
              })
            : formatDate(row.happened_at, locale)}
          {was !== undefined && ` · ${t("finAdjustWas", { sum: sum(was) })}`}
        </div>
      </div>
      {row.reverted && <Chip>{t("finAccrualReverted")}</Chip>}
      {editing ? (
        <span className="calc-edit">
          <input
            className="input input-sm"
            value={typed}
            autoFocus
            aria-label={t("finAdjust")}
            onChange={(e) => setTyped(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                void save();
              }
              if (e.key === "Escape") setEditing(false);
            }}
          />
          <button
            className="btn-icon"
            title={t("save")}
            disabled={guard.busy}
            onClick={() => void save()}
          >
            <Icon name="check" size={14} />
          </button>
          <button className="btn-icon" title={t("cancel")} onClick={() => setEditing(false)}>
            <Icon name="x" size={14} />
          </button>
        </span>
      ) : (
        <>
          <span className="calc-sum">
            {(row.direction === "income" ? "+ " : "− ") + sum(row.amount)}
          </span>
          {canEdit && !row.reverted && (
            <button
              className="btn-icon"
              title={t("finAdjust")}
              onClick={() => {
                setTyped(String(row.amount / 100));
                setEditing(true);
              }}
            >
              <Icon name="note" size={13} />
            </button>
          )}
        </>
      )}
    </div>
  );
}

/** Приём оплаты и возврат — одной формой: решает знак суммы.
 *
 * Отдельной кнопки «вернуть» нет намеренно. Возврат — это доходная операция с
 * отрицательной суммой по той же статье, и налог по ней сторнируется сам;
 * вторая форма означала бы второе место, где заводится приход.
 */
function PaymentModal({
  order,
  due,
  currency,
  onClose,
  onSaved,
}: {
  order: Order;
  due: number | null;
  currency: string;
  onClose: () => void;
  onSaved: () => void;
}) {
  const { t, locale, toastError } = useApp();
  const categories = useReference<FinanceCategory>("/finance/categories");
  const [form, setForm] = useState({
    // Умолчание — остаток к оплате: в большинстве случаев платят именно его, а
    // предоплату человек поправит сам.
    amount: due !== null && due > 0 ? String(due / 100) : "",
    happened_at: "",
    category_id: "",
    comment: "",
  });
  // Засов: платёж — это движение денег, и вторая такая же строка завышает
  // выручку ровно на свою сумму, а вместе с ней и отложенный налог.
  const guard = useGuard();

  const set = (key: string) => (e: any) => setForm((f) => ({ ...f, [key]: e.target.value }));
  const income = (categories.items ?? []).filter(
    (row) => !row.closed && row.direction === "income",
  );

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!guard.take()) return;
    try {
      await api.post("/finance/payments", {
        amount: toMinorUnits(form.amount),
        category_id: Number(form.category_id),
        document_id: order.id,
        // Полдень, а не полночь: дату человек выбирает по своему календарю, а
        // хранится момент в UTC. Полночь при смещении уезжает на соседние
        // сутки, и платёж попадает в чужой месяц вместе со своим налогом.
        happened_at: form.happened_at ? `${form.happened_at}T12:00:00` : null,
        comment: form.comment,
      });
      onSaved();
    } catch (err) {
      toastError(err);
      guard.free();
    }
  };

  return (
    <Modal title={t("finTakePayment")} onClose={onClose}>
      <form onSubmit={submit}>
        <div className="field">
          <label className="label">{t("finAmount")}</label>
          <input className="input" value={form.amount} onChange={set("amount")} autoFocus required />
          <div className="field-desc">
            {t("finPaymentHint")}
            {due !== null && due > 0 && (
              <>
                {" "}
                {t("finDueLeft")}: {formatMoney(due, currency, locale)}
              </>
            )}
          </div>
        </div>
        <div className="field">
          <label className="label">{t("finCategory")}</label>
          <select className="input" value={form.category_id} onChange={set("category_id")} required>
            <option value="">—</option>
            {income.map((row) => (
              <option key={row.id} value={row.id}>
                {row.name}
              </option>
            ))}
          </select>
          <div className="field-desc">{t("finPaymentIncomeOnly")}</div>
          {/* Список не приехал — платёж не записать вовсе, и молчать об этом
              нельзя: пустой выбор читается как «доходных статей не завели». */}
          {categories.failure !== null ? (
            <LoadFailed error={categories.failure} onRetry={categories.reload} />
          ) : (
            categories.items !== null && income.length === 0 && (
              <div className="field-desc">{t("finNoCategories")}</div>
            )
          )}
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
        <div className="field" style={{ marginBottom: 20 }}>
          <label className="label">{t("finComment")}</label>
          <textarea className="textarea" value={form.comment} onChange={set("comment")} />
        </div>
        <button className="btn btn-primary" style={{ width: "100%" }} disabled={guard.busy}>
          {t("finTakePayment")}
        </button>
      </form>
    </Modal>
  );
}

/** Добавление позиции: из справочника или разовой строкой.
 *
 * Два способа рядом, а не два экрана. **Из справочника** — для товара: только
 * такая строка попадает в резерв и списывается при отгрузке. **Разовая** — для
 * «доставки» и «упаковки», которых в справочнике нет и заводить их туда значит
 * замусорить его одноразовыми записями.
 */
function AddLine({ orderId, onAdded }: { orderId: number; onAdded: () => Promise<void> }) {
  const { t, locale, workspace, toastError } = useApp();
  const [fromCatalogue, setFromCatalogue] = useState(true);
  const [name, setName] = useState("");
  const [picked, setPicked] = useState<Product | null>(null);
  const [quantity, setQuantity] = useState("1");
  // Засов, а не флаг: строку добавляют Enter'ом из поля количества, и вторая
  // такая же строка в заказе — это вдвое больший резерв и вдвое большее
  // списание при отгрузке.
  const guard = useGuard();
  const [found, setFound] = useState<Product[]>([]);

  const search = useDebounced(name);
  useEffect(() => {
    if (!fromCatalogue || !search.trim() || picked) {
      setFound([]);
      return;
    }
    let alive = true;
    api
      .get<{ items: Product[] }>(
        `/warehouse/products?search=${encodeURIComponent(search)}&per_page=8`,
      )
      .then((data) => {
        if (alive) setFound(data.items);
      })
      // Склад может быть выключен — тогда справочника просто нет, и остаётся
      // разовая позиция. Это не ошибка человека, и говорить о ней нечего.
      .catch(() => {
        if (alive) setFound([]);
      });
    return () => {
      alive = false;
    };
  }, [search, fromCatalogue, picked]);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (fromCatalogue ? !picked : !name.trim()) return;
    if (!guard.take()) return;
    try {
      await api.post(`/orders/${orderId}/lines`, {
        product_id: picked?.id ?? null,
        name: picked ? null : name.trim(),
        quantity: quantity.trim(),
      });
      setName("");
      setPicked(null);
      setQuantity("1");
      await onAdded();
    } catch (err) {
      toastError(err);
    } finally {
      guard.free();
    }
  };

  return (
    <form className="card card-pad" onSubmit={submit} style={{ marginTop: 12 }}>
      <div style={{ display: "flex", gap: 6, marginBottom: 12 }}>
        {([[true, t("orderLinePick")], [false, t("orderLineOnce")]] as const).map(
          ([value, label]) => (
            <button
              key={String(value)}
              type="button"
              className={"option-chip" + (fromCatalogue === value ? " active" : "")}
              onClick={() => {
                setFromCatalogue(value);
                setPicked(null);
                setName("");
              }}
            >
              {label}
            </button>
          ),
        )}
      </div>
      <div style={{ display: "flex", gap: 12, flexWrap: "wrap", alignItems: "end" }}>
        <div
          className="field"
          style={{ marginBottom: 0, flex: "1 1 180px", minWidth: 0, position: "relative" }}
        >
          <label className="label">{fromCatalogue ? t("orderLinePick") : t("orderLineName")}</label>
          <input
            className="input"
            value={name}
            onChange={(e) => {
              setName(e.target.value);
              // Начали править — выбор сброшен: иначе в заказ уехал бы товар,
              // которого человек уже не видит в поле.
              setPicked(null);
            }}
          />
          {found.length > 0 && (
            <div
              className="card vsplyvashka"
              style={{ position: "absolute", top: "100%", left: 0, right: 0, zIndex: 20, marginTop: 4, overflow: "hidden" }}
            >
              {found.map((item) => (
                <button
                  key={item.id}
                  type="button"
                  className="list-row hoverable"
                  style={{ width: "100%", textAlign: "left", background: "none", border: "none", cursor: "pointer" }}
                  onClick={() => {
                    setPicked(item);
                    setName(item.name);
                    setFound([]);
                  }}
                >
                  <span style={{ flex: 1, color: "var(--text)", fontSize: 13 }}>{item.name}</span>
                  <span style={{ color: "var(--faint)", fontSize: 12 }}>{item.sku ?? ""}</span>
                  <span style={{ color: "var(--muted)", fontSize: 12.5, minWidth: 70, textAlign: "right" }}>
                    {formatMoney(item.price, workspace.currency, locale)}
                  </span>
                </button>
              ))}
            </div>
          )}
        </div>
        <div className="field" style={{ marginBottom: 0, flex: "0 1 120px" }}>
          <label className="label">{t("quantity")}</label>
          <input className="input" value={quantity} onChange={(e) => setQuantity(e.target.value)} />
        </div>
        <button className="btn btn-primary" disabled={guard.busy}>
          {t("orderAddLine")}
        </button>
      </div>
    </form>
  );
}

/** Поле сборки сканером.
 *
 * Появляется вместе с блоком наклеек: без штрихкодов сканировать нечего, и
 * пустое поле означало бы обещание, которого система не выполнит. Собранное
 * растёт по строке заказа, а не по остатку: склад двигается только отгрузкой.
 */
function PickScanner({ orderId, onPicked }: { orderId: number; onPicked: () => Promise<void> }) {
  const { t, toast, toastError } = useApp();
  const [code, setCode] = useState("");
  // Засов, а не флаг состояния, и здесь это не перестраховка: ввод идёт со
  // сканера, а сканер шлёт Enter сам и повторяет его, когда наклейка читается
  // с трудом. Каждое нажатие прибавляет собранное по строке заказа — второе
  // прибавило бы ещё раз, и заказ считался бы собранным вдвое.
  const guard = useGuard();
  const scanning = useLabelsOn();

  const lookup = async () => {
    const scanned = code.trim();
    if (!scanned || !guard.take()) return;
    try {
      const line = await api.post<{ name: string }>(`/orders/${orderId}/pick`, { code: scanned });
      setCode("");
      toast(line.name);
      await onPicked();
    } catch (err) {
      // Отказ называет товар: «этого нет в заказе» с именем читается, а пустой
      // ответ после писка сканера — как «сканер сломался».
      toastError(err);
    } finally {
      guard.free();
    }
  };

  if (!scanning) return null;

  return (
    <form
      className="card card-pad"
      style={{ marginTop: 12, display: "flex", alignItems: "center", gap: 10 }}
      title={t("orderScanHint")}
      onSubmit={(e) => {
        e.preventDefault();
        void lookup();
      }}
    >
      <Icon name="scan" size={16} className="" />
      <input
        className="input"
        style={{ flex: 1, minWidth: 0, fontFamily: "ui-monospace, monospace" }}
        placeholder={t("orderScan")}
        value={code}
        onChange={(e) => setCode(e.target.value)}
        // Enter ловим сами: форма без кнопки отправки не порождает submit —
        // проверено на живой странице, — а сканер заканчивает ввод именно им.
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            void lookup();
          }
        }}
        autoComplete="off"
      />
    </form>
  );
}
