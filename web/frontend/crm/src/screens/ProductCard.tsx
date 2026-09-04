import { type FormEvent, useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { Icon } from "../components/Icon";
import { ProductBarcodes } from "../components/ProductBarcodes";
import { ProductPhotos } from "../components/ProductPhotos";
import {
  TransferLog,
  TransferModal,
  WarehousePicker,
  WarehouseSpread,
  useWarehouses,
} from "../components/Warehouses";
import { ConfirmModal, Dochitat, EmptyState, ScreenLoading } from "../components/ui";
import { api, ApiError } from "../lib/api";
import { ProductHolders } from "../components/ProductHolders";
import { useApp } from "../lib/app";
import { useLiveTopic } from "../lib/live";
import { useFailure } from "../lib/failure";
import { useGuard } from "../lib/guard";
import { formatDateTime, formatMoney, formatQuantity } from "../lib/format";
import { StockValue, unitKey, type Product } from "./Warehouse";

const MOVE_KINDS = ["in", "out", "writeoff", "adjust", "return"] as const;
export type MoveKind = (typeof MOVE_KINDS)[number];

type KindLabel = "moveIn" | "moveOut" | "moveWriteoff" | "moveAdjust" | "moveReturn";

export const KIND_LABEL: Record<MoveKind, KindLabel> = {
  in: "moveIn",
  out: "moveOut",
  writeoff: "moveWriteoff",
  adjust: "moveAdjust",
  return: "moveReturn",
};

export interface StockMove {
  id: number;
  product_id: number;
  product_name: string | null;
  unit: string | null;
  /** Знаковое: приход +, расход −. Знак приходит с сервера, а не выводится из вида. */
  quantity_milli: number;
  kind: MoveKind;
  deal_id: number | null;
  cost: number | null;
  comment: string;
  happened_at: string | null;
  created_at: string | null;
  author_id: number | null;
  author_name: string | null;
}

/** По скольку записей истории дочитывается карточка. */
const DVIZHENIY_NA_STRANITSE = 100;

export function ProductCard() {
  const { id } = useParams();
  const { t, locale, workspace, toast, toastError } = useApp();
  const navigate = useNavigate();
  const [product, setProduct] = useState<Product | null>(null);
  const [moves, setMoves] = useState<StockMove[]>([]);
  const [total, setTotal] = useState(0);
  const [stranitsaDvizheniy, setStranitsaDvizheniy] = useState(1);
  const [dochityvaem, setDochityvaem] = useState(false);
  // Чему принадлежит показанное. Ставит загрузка, сверяет дочитка: пока
  // страница едет, можно уйти на другую карточку — и опоздавший ответ
  // дописал бы чужие строки к чужому же списку, молча.
  const otbor_spiska = useRef("");
  const [currency, setCurrency] = useState(workspace.currency);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const places = useWarehouses();
  const [showTransfer, setShowTransfer] = useState(false);
  // Раскладка «где и сколько» приходит вместе со списком, а не с карточкой:
  // карточка спрашивает один товар, и отдельная ручка ради неё была бы третьим
  // способом посчитать один и тот же остаток.
  const [spread, setSpread] = useState<Record<string, number> | undefined>();

  const { failure, fail, clear } = useFailure();

  // Списали в другом окне — остаток пересчитался здесь. Намёк говорит
  // «перечитай», число считает сервер.
  useLiveTopic("warehouse", (s) => {
    if (s.resync || s.hints.some((h) => h.id === Number(id))) void load();
  });

  const load = useCallback(async () => {
    clear();
    otbor_spiska.current = String(id);
    try {
      const card = await api.get<Product & { currency: string }>(`/warehouse/products/${id}`);
      setProduct(card);
      setCurrency(card.currency || workspace.currency);
      setSpread(card.by_warehouse);
      // Первая страница истории. Прежде бралось двести записей и на этом всё:
      // рядом с заголовком честно писалось «всего 640», а показывались первые
      // двести, и добраться до остальных было нечем.
      const history = await api.get<{ items: StockMove[]; total: number }>(
        `/warehouse/products/${id}/moves?page=1&per_page=${DVIZHENIY_NA_STRANITSE}`,
      );
      setMoves(history.items);
      setTotal(history.total);
      setStranitsaDvizheniy(1);
    } catch (e) {
      // Записи нет или она не наша: показывать «попробуйте ещё раз» тут не о
      // чем — повтор вернёт тот же ответ. Возвращаемся в список, как и раньше.
      if (e instanceof ApiError && (e.status === 404 || e.status === 403)) {
        toastError(e);
        navigate("/warehouse");
        return;
      }
      // Всё остальное — беда связи или сервера. Карточку не бросаем: адрес в
      // строке верный, и повторить имеет смысл именно его, а не список.
      fail(e);
    }
  }, [id, workspace.currency, toastError, navigate, fail, clear]);

  /** Дочитать историю движений.
   *
   * Дописывает страницу к показанному, а не перезагружает карточку целиком:
   * перезагрузка стоила бы ещё и запроса самой карточки с раскладкой по
   * складам, а меняется от дочитки только список внизу.
   */
  const dochitat_dvizheniya = async () => {
    if (dochityvaem) return;
    const sprosheno = String(id);
    setDochityvaem(true);
    try {
      const dalshe = await api.get<{ items: StockMove[]; total: number }>(
        `/warehouse/products/${id}/moves` +
          `?page=${stranitsaDvizheniy + 1}&per_page=${DVIZHENIY_NA_STRANITSE}`,
      );
      // Отбор сменился, пока страница ехала, — ответ чужой.
      if (otbor_spiska.current !== sprosheno) return;
      setMoves((bylo) => [...bylo, ...dalshe.items]);
      setTotal(dalshe.total);
      setStranitsaDvizheniy((bylo) => bylo + 1);
    } catch (e) {
      toastError(e);
    } finally {
      setDochityvaem(false);
    }
  };

  useEffect(() => {
    void load();
  }, [load]);

  if (!product) return <ScreenLoading error={failure} onRetry={() => void load()} />;

  const negative = product.stock_milli !== null && product.stock_milli < 0;

  return (
    <div className="page">
      <Link
        to="/warehouse"
        style={{ display: "inline-flex", alignItems: "center", gap: 6, color: "var(--muted)", fontSize: 13, marginBottom: 20 }}
      >
        <Icon name="arrowLeft" size={14} />
        {t("warehouse")}
      </Link>

      <div className="page-head" style={{ alignItems: "flex-start", marginBottom: 24 }}>
        <div>
          <h1 className="page-title" style={{ fontSize: 22 }}>
            {product.name}
          </h1>
          <div className="page-sub" style={{ marginTop: 5 }}>
            {product.sku ? <>{t("sku")} {product.sku} · </> : null}
            {t(unitKey(product.unit))}
            {product.is_service && <> · {t("isService")}</>}
          </div>
        </div>
        <button className="btn-icon" onClick={() => setConfirmDelete(true)} title={t("deleteProduct")}>
          <Icon name="trash" />
        </button>
      </div>

      <div className="card card-pad" style={{ marginBottom: 20 }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 16 }}>
          <div>
            <div className="metric-title" style={{ marginBottom: 6 }}>{t("stock")}</div>
            <div style={{ fontSize: 20 }}>
              <StockValue product={product} />
            </div>
          </div>
          <div style={{ textAlign: "right", color: "var(--muted)", fontSize: 12.5, lineHeight: 1.6 }}>
            <div>
              {t("costPrice")}: {formatMoney(product.cost, currency, locale)}
            </div>
            <div>
              {t("sellPrice")}: {formatMoney(product.price, currency, locale)}
            </div>
          </div>
        </div>
        {/* Где лежит. Раскладка по местам появляется вместе со вторым складом:
            пока склад один, она повторяла бы общий остаток. */}
        <WarehouseSpread places={places} spread={spread} unit={t(unitKey(product.unit))} />
        {/* Об этом стоит напоминать прямо на экране: увидев число, человек ищет
            поле, где его правят, — а правится оно только движением. */}
        <div className="field-desc" style={{ marginTop: 12 }}>{t("stockIsSumOfMoves")}</div>
        {negative && (
          <div className="field-desc" style={{ color: "var(--warning)" }}>
            {t("negativeStockHint")}
          </div>
        )}
      </div>

      <SiteDescription product={product} onSaved={() => void load()} />
      <ProductHolders productId={product.id} />


      {!product.is_service && (
        <MoveForm product={product} places={places} onSaved={() => void load()} />
      )}

      {/* Переезд — отдельное действие, а не «расход тут, приход там»: товар не
          появился и не пропал, и в истории это одно событие. Кнопка появляется
          вместе со вторым складом: перевозить внутри одного места нечего. */}
      {!product.is_service && places?.many && (
        <>
          <div className="section-head" style={{ marginTop: 28 }}>
            <h2 className="section-title">{t("transfers")}</h2>
            <button
              className="btn btn-secondary btn-sm"
              style={{ marginLeft: "auto" }}
              onClick={() => setShowTransfer(true)}
            >
              <Icon name="arrowOut" size={13} />
              {t("transfer")}
            </button>
          </div>
          <TransferLog productId={product.id} productNames={{ [product.id]: product.name }} />
        </>
      )}

      {/* Раздел сам решает, показываться ли: выключен блок или нет права —
          возвращает null. Услуге штрихкод не нужен, её не сканируют с полки. */}
      {!product.is_service && <ProductBarcodes productId={product.id} />}

      {/* Снимки — и услуге тоже. «Выезд мастера» на полке не лежит, но
          фотография у услуги осмысленна: так выглядит результат работы, и
          показать её клиенту проще, чем описать. */}
      <ProductPhotos productId={product.id} />

      <div className="section-head" style={{ marginTop: 28 }}>
        <h2 className="section-title">{t("moves")}</h2>
        <span className="page-sub">{total}</span>
      </div>
      <div>
        {moves.map((move) => (
          <div className="feed-item" key={move.id}>
            <div className="feed-icon">
              <Icon name={move.quantity_milli > 0 ? "arrowIn" : "arrowOut"} size={14} />
            </div>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ display: "flex", alignItems: "baseline", gap: 8, marginBottom: 4 }}>
                <span style={{ color: "var(--muted)", fontSize: 12.5, fontWeight: 500 }}>
                  {t(KIND_LABEL[move.kind] ?? "moveAdjust")}
                </span>
                <span style={{ color: "var(--faint)", fontSize: 12 }}>
                  {formatDateTime(move.happened_at, locale)}
                </span>
                {move.deal_id !== null && (
                  <Link to={`/deals/${move.deal_id}`} style={{ color: "var(--accent)", fontSize: 12 }}>
                    #{move.deal_id}
                  </Link>
                )}
                <span
                  style={{
                    marginLeft: "auto",
                    color: move.quantity_milli > 0 ? "var(--success)" : "var(--text)",
                    fontSize: 13.5,
                    fontWeight: 500,
                  }}
                >
                  {move.quantity_milli > 0 ? "+" : ""}
                  {formatQuantity(move.quantity_milli)} {t(unitKey(product.unit))}
                </span>
              </div>
              <div style={{ color: "var(--faint)", fontSize: 12.5 }}>
                {move.cost !== null && <>{t("costPrice")}: {formatMoney(move.cost, currency, locale)}</>}
                {move.author_name && <> · {move.author_name}</>}
                {move.comment && <> · {move.comment}</>}
              </div>
            </div>
          </div>
        ))}
        {moves.length === 0 && <EmptyState title={t("noMovesYet")} />}
        <Dochitat
          pokazano={moves.length}
          vsego={total}
          zanyat={dochityvaem}
          onClick={() => void dochitat_dvizheniya()}
        />
      </div>

      {showTransfer && places && (
        <TransferModal
          productId={product.id}
          places={places}
          onClose={() => setShowTransfer(false)}
          onDone={() => void load()}
        />
      )}

      {confirmDelete && (
        <ConfirmModal
          text={t("deleteProductConfirm")}
          confirmLabel={t("delete")}
          danger
          onConfirm={async () => {
            try {
              await api.del(`/warehouse/products/${product.id}`);
              toast(t("deleteProduct") + " ✓");
              navigate("/warehouse");
            } catch (e) {
              toastError(e);
            }
          }}
          onClose={() => setConfirmDelete(false)}
        />
      )}
    </div>
  );
}

/** Форма прихода/расхода.
 *
 * Движение записывается, а не редактируется: ошибку исправляют обратным
 * движением, поэтому здесь нет ни правки, ни удаления. Иначе остаток на прошлую
 * пятницу зависел бы от того, когда его спросили. */
/** Описание для сайта — правится прямо на карточке: остальные поля товара
 *  задаются при заведении, а описание дописывают, когда карточка уже есть. */
function SiteDescription({ product, onSaved }: { product: Product; onSaved: () => void }) {
  const { t, toast, toastError } = useApp();
  const [text, setText] = useState(product.site_description ?? "");
  const guard = useGuard();

  useEffect(() => setText(product.site_description ?? ""), [product.site_description]);

  const save = async () => {
    if (!guard.take()) return;
    try {
      await api.patch(`/warehouse/products/${product.id}`, { site_description: text });
      toast(t("save") + " ✓");
      onSaved();
    } catch (e) {
      toastError(e);
    } finally {
      guard.free();
    }
  };

  return (
    <div className="card card-pad" style={{ marginBottom: 20 }}>
      <div className="metric-title" style={{ marginBottom: 8 }}>{t("productSiteDescription")}</div>
      <textarea className="textarea" value={text} onChange={(e) => setText(e.target.value)} rows={4} />
      <div className="field-desc" style={{ marginTop: 6 }}>{t("productSiteDescriptionHint")}</div>
      {text !== (product.site_description ?? "") && (
        <button className="btn btn-primary btn-sm" style={{ marginTop: 10 }} disabled={guard.busy} onClick={() => void save()}>
          {t("save")}
        </button>
      )}
    </div>
  );
}

function MoveForm({
  product,
  places,
  onSaved,
}: {
  product: Product;
  places: ReturnType<typeof useWarehouses>;
  onSaved: () => void;
}) {
  const { t, toast, toastError } = useApp();
  const [kind, setKind] = useState<MoveKind>("in");
  // Склад по умолчанию — основной, но выбирается явно. Молчаливое списание с
  // основного однажды спишет деталь не оттуда, где её взяли.
  const [place, setPlace] = useState<number | null>(null);
  const [quantity, setQuantity] = useState("");
  const [comment, setComment] = useState("");
  // Засов, а не флаг состояния, и это здесь дороже, чем где-либо ещё: остаток
  // склада не хранится, он равен сумме движений. Записанное дважды движение
  // не «показывает лишнее» — оно и ЕСТЬ остаток, и отличить его от настоящего
  // потом нельзя ничем, кроме памяти кладовщика. Два нажатия в одном тике оба
  // читают `busy` из своих замыканий и оба видят `false` — см. lib/guard.ts.
  const guard = useGuard();

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!quantity.trim() || !guard.take()) return;
    try {
      // Количество уходит строкой как набрали: разбирает его сервер, чтобы
      // лишние знаки после запятой получили отказ, а не тихое округление здесь.
      const result = await api.post("/warehouse/moves", {
        product_id: product.id,
        kind,
        quantity: quantity.trim(),
        comment: comment.trim() || null,
        warehouse_id: place,
      });
      // Уход в минус — не отказ: движение записано, а предупреждение видит
      // оператор, чтобы пойти искать неоприходованный приход.
      toast(result.went_negative ? t("negativeStock") : t("moveSaved"), result.went_negative);
      setQuantity("");
      setComment("");
      onSaved();
    } catch (err) {
      toastError(err);
    } finally {
      guard.free();
    }
  };

  return (
    <form className="card card-pad" onSubmit={submit}>
      <div style={{ display: "flex", gap: 6, marginBottom: 14, flexWrap: "wrap" }}>
        {MOVE_KINDS.map((option) => (
          <button
            key={option}
            type="button"
            className={"option-chip" + (kind === option ? " active" : "")}
            onClick={() => setKind(option)}
          >
            {t(KIND_LABEL[option])}
          </button>
        ))}
      </div>
      {/* Не сетка с колонками, а перенос: на телефоне четыре поля в один ряд
          не встают, и жёсткие колонки увозили страницу вбок. Каждое поле
          называет свою минимальную ширину и переносится, когда её не хватает. */}
      <div style={{ display: "flex", flexWrap: "wrap", gap: 12, alignItems: "end" }}>
        <div className="field" style={{ marginBottom: 0, flex: "0 1 140px" }}>
          <label className="label">
            {t("quantity")}, {t(unitKey(product.unit))}
          </label>
          <input
            className="input"
            value={quantity}
            onChange={(e) => setQuantity(e.target.value)}
            placeholder="0"
            required
          />
        </div>
        {places?.many && (
          <div className="field" style={{ marginBottom: 0, flex: "0 1 150px" }}>
            <label className="label">{t("warehousePick")}</label>
            <WarehousePicker places={places} value={place ?? places.items[0]?.id ?? null} onChange={setPlace} />
          </div>
        )}
        <div className="field" style={{ marginBottom: 0, flex: "1 1 170px", minWidth: 0 }}>
          <label className="label">{t("moveComment")}</label>
          <input className="input" value={comment} onChange={(e) => setComment(e.target.value)} />
        </div>
        <button className="btn btn-primary" disabled={guard.busy}>
          {t("newMove")}
        </button>
      </div>
      <div className="field-desc">{t("quantityHint")}</div>
    </form>
  );
}
