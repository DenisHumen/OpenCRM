import { useCallback, useEffect, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { Icon } from "../components/Icon";
import { Chip, ConfirmModal, LoadFailed, ScreenLoading } from "../components/ui";
import { WarehousePicker, useWarehouses } from "../components/Warehouses";
import { api, ApiError } from "../lib/api";
import { useApp } from "../lib/app";
import { useDebounced } from "../lib/debounce";
import { useFailure } from "../lib/failure";
import { useGuard } from "../lib/guard";
import { copyText } from "../lib/clipboard";
import { isFinished, nextStatuses, statusLabel, statusVariant } from "../lib/documents";
import { formatDateTime, formatMoney, formatQuantity } from "../lib/format";
import { useReference } from "../lib/reference";
import type { Product } from "./Warehouse";

/** Языки печати. Бумагу печатают под клиента, а не под сотрудника: приехал
 *  турист — печатаем по-английски, ничего в базе не меняя. */
const PRINT_LANGS = [
  { id: "ru", label: "Рус" },
  { id: "en", label: "Eng" },
  { id: "uk", label: "Укр" },
];

const ROWS = [
  ["item", "docItem"],
  ["serial", "docSerial"],
  ["condition", "docCondition"],
  ["accessories", "docAccessories"],
  ["problem", "docProblem"],
  ["estimate", "docEstimate"],
  ["terms", "docTerms"],
] as const;

type Stage = { key: string; name: string; kind: "open" | "won" | "lost" };

export function DocumentCard() {
  const { id } = useParams();
  const { t, locale, toast, toastError } = useApp();
  const navigate = useNavigate();
  const [doc, setDoc] = useState<any>(null);
  const [confirmCancel, setConfirmCancel] = useState(false);

  const { failure, fail, clear } = useFailure();

  const load = useCallback(async () => {
    clear();
    try {
      const found = await api.get(`/documents/${id}`);
      // Акт живёт по тому же адресу — он **вид бланка**, — но отдаётся своей
      // ручкой: у него есть перечень работ, итог и себестоимость, которых у
      // квитанции нет. Вид узнаём первым запросом: в адресе его нет и быть не
      // может, а класть строки в ответ каждой квитанции значит возить пустой
      // массив по всем экранам, где бланки показывают списком.
      setDoc(found.kind === "act" ? await api.get(`/documents/acts/${id}`) : found);
    } catch (e) {
      // Записи нет или она не наша: показывать «попробуйте ещё раз» тут не о
      // чем — повтор вернёт тот же ответ. Возвращаемся в список, как и раньше.
      if (e instanceof ApiError && (e.status === 404 || e.status === 403)) {
        toastError(e);
        navigate("/documents");
        return;
      }
      // Всё остальное — беда связи или сервера. Карточку не бросаем: адрес в
      // строке верный, и повторить имеет смысл именно его, а не список.
      fail(e);
    }
  }, [id, toastError, navigate, fail, clear]);

  useEffect(() => {
    void load();
  }, [load]);

  if (!doc) return <ScreenLoading error={failure} onRetry={() => void load()} />;

  if (doc.kind === "act") return <ActCard act={doc} reload={load} />;

  const fields = doc.payload?.fields ?? {};
  const client = doc.payload?.client ?? {};
  const publicUrl = `${window.location.origin}/d/${doc.number}`;
  const finished = isFinished(doc.status);

  const move = async (status: string) => {
    try {
      // Смена состояния возвращает бланк без истории — перечитываем целиком,
      // иначе журнал внизу застынет на том, что было при открытии.
      await api.post(`/documents/${id}/status`, { status });
      await load();
    } catch (e) {
      toastError(e);
    }
  };

  return (
    <div className="page page-narrow">
      <Link to="/documents" className="back-link">
        <Icon name="arrowLeft" size={14} />
        {t("documents")}
      </Link>

      <div className="page-head" style={{ alignItems: "flex-start" }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <h1 className="page-title doc-number-title">{doc.number}</h1>
          <div className="page-sub" style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
            <span>{fields.item || "—"}</span>
            <Chip variant={statusVariant(doc.status)}>{statusLabel(t, doc.status)}</Chip>
          </div>
        </div>
        {/* Печать — то, ради чего сюда заходят чаще всего: бумагу теряют, мнут
            и заливают кофе, поэтому кнопка на виду, а не в меню. */}
        <PrintLangs base={`/api/v1/documents/${doc.id}/print`} current={doc.locale} />
      </div>

      <div className="card card-pad" style={{ marginBottom: 20 }}>
        <div className="metric-title" style={{ marginBottom: 12 }}>{t("docWhatNext")}</div>
        {finished ? (
          <div className="field-desc" style={{ marginTop: 0 }}>{t("docFinished")}</div>
        ) : (
          <div className="stage-actions">
            {nextStatuses(doc.status).map((status) =>
              status === "cancelled" ? (
                <button key={status} className="stage-btn stage-lost" onClick={() => setConfirmCancel(true)}>
                  {statusLabel(t, status)}
                </button>
              ) : (
                <button
                  key={status}
                  className={"stage-btn" + (status === "closed" ? " stage-won" : "")}
                  onClick={() => void move(status)}
                >
                  {status === "closed" && <Icon name="check" size={13} stroke={2} />}
                  {statusLabel(t, status)}
                </button>
              ),
            )}
          </div>
        )}
      </div>

      <div className="card card-pad" style={{ marginBottom: 20 }}>
        <table className="doc-table">
          <tbody>
            <tr>
              <th>{t("client")}</th>
              <td>
                {/* Клиента могли удалить — тогда остаётся имя со снимка, без ссылки. */}
                {doc.client_id ? (
                  <Link to={`/clients/${doc.client_id}`} className="text-link">
                    {client.name || "—"}
                  </Link>
                ) : (
                  client.name || "—"
                )}
                {client.phone && <span style={{ color: "var(--faint)" }}> · {client.phone}</span>}
              </td>
            </tr>
            {doc.deal_id && (
              <tr>
                <th>{t("deal")}</th>
                <td>
                  <Link to={`/deals/${doc.deal_id}`} className="text-link">
                    {doc.payload?.deal?.title || `#${doc.deal_id}`}
                  </Link>
                </td>
              </tr>
            )}
            {ROWS.map(([key, label]) => (
              <tr key={key}>
                <th>{t(label)}</th>
                <td>{fields[key] || <span style={{ color: "var(--faint)" }}>—</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {/* Поля не редактируются намеренно: у клиента на руках половина этой же
            бумаги. Правка задним числом означала бы, что база и бумага
            разошлись, а спор «что вы у меня приняли» решать станет нечем. */}
        <div className="field-desc" style={{ marginTop: 12 }}>
          {t("docSnapshotHint", { date: formatDateTime(doc.created_at, locale) })}
        </div>
      </div>

      <div className="card card-pad" style={{ marginBottom: 20 }}>
        <div className="metric-title" style={{ marginBottom: 10 }}>{t("docClientLink")}</div>
        <div className="share-action-row">
          <input className="input" readOnly value={publicUrl} onFocus={(e) => e.target.select()} />
          <button
            className="btn btn-secondary btn-sm"
            onClick={() => {
              // Не получилось скопировать — показываем адрес, а не молчим.
              void copyText(publicUrl).then((ok) => toast(ok ? t("copied") : publicUrl));
            }}
          >
            <Icon name="copy" size={13} />
            {t("copyLink")}
          </button>
        </div>
        <div className="field-desc">{t("docClientLinkHint")}</div>
      </div>

      <History events={doc.events} />

      {confirmCancel && (
        <ConfirmModal
          text={t("docCancelConfirm")}
          confirmLabel={t("confirm")}
          danger
          onConfirm={() => void move("cancelled")}
          onClose={() => setConfirmCancel(false)}
        />
      )}
    </div>
  );
}

/** Кнопки печати на трёх языках. Общие у квитанции и у акта: язык бумаги
 *  выбирают под клиента, и правило это одно на все бланки. */
function PrintLangs({ base, current }: { base: string; current: string }) {
  const { t } = useApp();
  return (
    <div className="print-actions">
      <span className="print-label">
        <Icon name="printer" size={14} />
        {t("docPrint")}
      </span>
      {PRINT_LANGS.map((lang) => (
        <a
          key={lang.id}
          className={"btn btn-secondary btn-sm" + (lang.id === current ? " btn-current" : "")}
          href={`${base}?locale=${lang.id}`}
          target="_blank"
          rel="noreferrer"
        >
          {lang.label}
        </a>
      ))}
    </div>
  );
}

/** История переходов бумаги. Общая: спор о сроках разрешается записью с
 *  временем, а не текущим состоянием, — и у квитанции, и у акта. */
function History({ events }: { events: any[] | undefined }) {
  const { t, locale } = useApp();
  return (
    <div className="card card-pad">
      <div className="metric-title" style={{ marginBottom: 12 }}>{t("history")}</div>
      <ol className="stage-log">
        {(events ?? []).map((event: any) => (
          <li key={event.id}>
            <span className="stage-log-when">{formatDateTime(event.created_at, locale)}</span>
            <span className="stage-log-what">
              {event.from_status
                ? `${statusLabel(t, event.from_status)} → ${statusLabel(t, event.to_status)}`
                : statusLabel(t, event.to_status)}
              {event.note && <span style={{ color: "var(--faint)" }}> · {event.note}</span>}
            </span>
            <span className="stage-log-who">{event.author_name || "—"}</span>
          </li>
        ))}
      </ol>
    </div>
  );
}

/** Карточка акта выполненных работ.
 *
 * Отличается от квитанции по существу, а не оформлением. Квитанция описывает
 * принятую вещь и не редактируется вовсе; акт **набирают по ходу работы** и
 * один раз проводят — и это проведение делает сразу три вещи: списывает
 * материалы, фиксирует работы и переводит заявку на следующий этап.
 *
 * Поэтому кнопка одна и называется действием, а не состоянием. Три кнопки
 * («списать», «закрыть», «перевести») означали бы, что состояние «списано, но
 * заявка не переведена» достижимо обычным нажатием, а не только сбоем.
 */
function ActCard({ act, reload }: { act: any; reload: () => Promise<void> }) {
  const { t, locale, workspace, toast, toastError } = useApp();
  // Проведение делает сразу три вещи: списывает материалы, закрывает акт и
  // переводит заявку. Засов, а не флаг состояния: второе нажатие в том же тике
  // списало бы материалы дважды, а остаток склада равен сумме движений.
  const guard = useGuard();
  const [shortage, setShortage] = useState<string | null>(null);
  const [confirm, setConfirm] = useState(false);
  const [stage, setStage] = useState<string>(act.next_stage || "");
  const places = useWarehouses();
  const [place, setPlace] = useState<number | null>(null);
  // Список этапов нужен выбору «куда переведём». Не приехал — выбор пуст, и
  // сервер возьмёт следующий по воронке сам: одно действие остаётся одним.
  const stages = useReference<Stage>("/pipeline/stages");

  const open = act.status === "issued";
  const movedTo = (stages.items ?? []).find((s) => s.key === act.next_stage);

  const complete = async (force: boolean) => {
    if (!guard.take()) return;
    try {
      await api.post(`/documents/acts/${act.id}/complete`, {
        warehouse_id: place,
        stage: stage || null,
        confirm_negative: force,
      });
      setShortage(null);
      toast(t("actDone"));
      await reload();
    } catch (err) {
      if (err instanceof ApiError && err.code === "not_enough_stock") {
        // Не отказ насовсем: показываем, чего именно не хватает, и даём
        // подтвердить. Список позиций приходит с сервера — иначе человеку
        // пришлось бы сверять акт со складом построчно руками.
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
      <Link to="/documents" className="back-link">
        <Icon name="arrowLeft" size={14} />
        {t("documents")}
      </Link>

      <div className="page-head" style={{ alignItems: "flex-start" }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <h1 className="page-title doc-number-title">{act.number}</h1>
          <div className="page-sub" style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
            <span>{act.payload?.fields?.item || t("actTitle")}</span>
            <Chip variant={statusVariant(act.status)}>{statusLabel(t, act.status)}</Chip>
            {act.deal_id && (
              <Link to={`/deals/${act.deal_id}`} className="text-link">
                {act.payload?.deal?.title || `#${act.deal_id}`}
              </Link>
            )}
          </div>
        </div>
        <PrintLangs base={`/api/v1/documents/acts/${act.id}/print`} current={act.locale} />
      </div>

      <div className="card" style={{ overflow: "hidden", marginBottom: 16 }}>
        <div className="list-header">
          <span style={{ flex: 1 }}>{t("actLineName")}</span>
          <span style={{ width: 120, textAlign: "right" }}>{t("quantity")}</span>
          <span style={{ width: 110, textAlign: "right" }}>{t("sellPrice")}</span>
          {open && <span style={{ width: 34 }} />}
        </div>
        {act.lines.length === 0 && (
          <div className="list-row">
            <span className="field-desc" style={{ marginTop: 0 }}>{t("actEmpty")}</span>
          </div>
        )}
        {act.lines.map((line: any) => (
          <div className="list-row" key={line.id}>
            <span style={{ flex: 1, minWidth: 0, color: "var(--text)", fontSize: 13.5 }}>
              {line.name}
            </span>
            <span style={{ width: 120, textAlign: "right", fontSize: 13 }}>
              {formatQuantity(line.quantity_milli)}
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
                    await api.del(`/documents/acts/${act.id}/lines/${line.id}`);
                    await reload();
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
            {formatMoney(act.total, workspace.currency, locale)}
          </span>
        </div>
        {/* Себестоимость считается по строкам, а не хранится числом. До
            проведения её неоткуда взять — и «не знаем» показывается словами, а
            не нулём: ноль означал бы «работа не стоила нам ничего». */}
        <div className="list-row">
          <span style={{ flex: 1, color: "var(--muted)", fontSize: 12.5 }}>{t("actCost")}</span>
          <span style={{ color: "var(--faint)", fontSize: 13 }}>
            {act.cost === null ? t("actCostUnknown") : formatMoney(act.cost, workspace.currency, locale)}
          </span>
        </div>
      </div>

      {open && <ActLineForm actId={act.id} onAdded={reload} />}

      {open ? (
        <div className="card card-pad" style={{ marginTop: 16 }}>
          <div className="field-desc" style={{ marginTop: 0, marginBottom: 12 }}>
            {t("actCompleteHint")}
          </div>
          <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "center" }}>
            {/* Склад выбирается явно: молчаливое списание с основного однажды
                снимет деталь не оттуда, где её взяли. */}
            <WarehousePicker places={places} value={place ?? places?.items[0]?.id ?? null} onChange={setPlace} />
            <label className="label" style={{ marginBottom: 0 }}>{t("actNextStage")}</label>
            <select className="select" value={stage} onChange={(e) => setStage(e.target.value)}>
              <option value="">{t("actNextStageAuto")}</option>
              {(stages.items ?? []).map((s) => (
                <option key={s.key} value={s.key}>{s.name}</option>
              ))}
            </select>
            <button
              className="btn btn-primary"
              disabled={guard.busy || act.lines.length === 0}
              onClick={() => void complete(false)}
            >
              <Icon name="check" size={14} stroke={2} />
              {t("actComplete")}
            </button>
            <button className="btn btn-secondary" disabled={guard.busy} onClick={() => setConfirm(true)}>
              {t("actCancel")}
            </button>
          </div>
          {/* Без воронки выбор этапа пуст, и об этом надо сказать: молчаливо
              пустой список читается как «этапов нет», а это не так. */}
          {stages.failure !== null && (
            <LoadFailed error={stages.failure} onRetry={stages.reload} />
          )}
          {shortage && (
            <div style={{ marginTop: 12 }}>
              <div className="field-desc" style={{ color: "var(--warning)" }}>{shortage}</div>
              <button className="btn btn-secondary btn-sm" disabled={guard.busy} onClick={() => void complete(true)}>
                {t("actCompleteForce")}
              </button>
            </div>
          )}
        </div>
      ) : (
        act.next_stage && (
          <div className="card card-pad" style={{ marginTop: 16 }}>
            <div className="field-desc" style={{ marginTop: 0 }}>
              {t("actMovedTo", { stage: movedTo?.name ?? act.next_stage })}
            </div>
          </div>
        )
      )}

      <div style={{ marginTop: 16 }}>
        <History events={act.events} />
      </div>

      {confirm && (
        <ConfirmModal
          text={t("actCancelConfirm")}
          confirmLabel={t("actCancel")}
          danger
          onConfirm={async () => {
            try {
              await api.post(`/documents/acts/${act.id}/cancel`, {});
              await reload();
            } catch (err) {
              toastError(err);
            }
          }}
          onClose={() => setConfirm(false)}
        />
      )}
    </div>
  );
}

/** Добавление строки акта: выполненная работа или израсходованный материал.
 *
 * Два способа рядом, а не два экрана, — как у заказа. **Из справочника** нужен
 * материалу: только такая строка потом спишется со склада. **Разовая** — для
 * работ, которых в справочнике нет и заводить их туда незачем: «выезд
 * мастера», «диагностика».
 *
 * Цена здесь есть, в отличие от заказа: у разовой работы её взять больше
 * неоткуда, а без неё в подписываемом акте окажется пустой столбец.
 */
function ActLineForm({ actId, onAdded }: { actId: number; onAdded: () => Promise<void> }) {
  const { t, toastError } = useApp();
  const [fromCatalogue, setFromCatalogue] = useState(false);
  const [name, setName] = useState("");
  const [picked, setPicked] = useState<Product | null>(null);
  const [quantity, setQuantity] = useState("1");
  const [price, setPrice] = useState("");
  // Засов, а не флаг: строку добавляют Enter'ом, и вторая такая же строка в
  // акте — это вдвое больший счёт клиенту и вдвое большее списание материала
  // при проведении.
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
      // разовая строка. Это не ошибка человека, и говорить о ней нечего.
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
      await api.post(`/documents/acts/${actId}/lines`, {
        product_id: picked?.id ?? null,
        name: picked ? null : name.trim(),
        // Количество уходит строкой как набрали: разбирает его сервер, чтобы
        // лишние знаки после запятой получили отказ, а не тихое округление.
        quantity: quantity.trim(),
        // Деньги переводим в минимальные единицы здесь, на краю: два знака и
        // умножение на сто в браузере безобидны, в отличие от количества.
        price: price.trim() ? Math.round(Number(price) * 100) : null,
      });
      setName("");
      setPicked(null);
      setQuantity("1");
      setPrice("");
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
        {([[false, t("orderLineOnce")], [true, t("orderLinePick")]] as const).map(
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
          <label className="label">{fromCatalogue ? t("orderLinePick") : t("actLineName")}</label>
          <input
            className="input"
            value={name}
            onChange={(e) => {
              setName(e.target.value);
              // Начали править — выбор сброшен: иначе в акт уехал бы товар,
              // которого человек уже не видит в поле.
              setPicked(null);
            }}
          />
          {found.length > 0 && (
            <div
              className="card"
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
                </button>
              ))}
            </div>
          )}
        </div>
        <div className="field" style={{ marginBottom: 0, flex: "0 1 110px" }}>
          <label className="label">{t("quantity")}</label>
          <input className="input" value={quantity} onChange={(e) => setQuantity(e.target.value)} />
        </div>
        <div className="field" style={{ marginBottom: 0, flex: "0 1 120px" }}>
          <label className="label">{t("sellPrice")}</label>
          <input
            className="input"
            type="number"
            min={0}
            step="0.01"
            value={price}
            onChange={(e) => setPrice(e.target.value)}
          />
        </div>
        <button className="btn btn-primary" disabled={guard.busy}>
          {t("orderAddLine")}
        </button>
      </div>
    </form>
  );
}
