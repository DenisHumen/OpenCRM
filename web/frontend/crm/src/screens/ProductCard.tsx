import { useCallback, useEffect, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { Icon } from "../components/Icon";
import { ConfirmModal, EmptyState, ScreenLoading } from "../components/ui";
import { api } from "../lib/api";
import { useApp } from "../lib/app";
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

export function ProductCard() {
  const { id } = useParams();
  const { t, locale, workspace, toast, toastError } = useApp();
  const navigate = useNavigate();
  const [product, setProduct] = useState<Product | null>(null);
  const [moves, setMoves] = useState<StockMove[]>([]);
  const [total, setTotal] = useState(0);
  const [currency, setCurrency] = useState(workspace.currency);
  const [confirmDelete, setConfirmDelete] = useState(false);

  const load = useCallback(async () => {
    try {
      const card = await api.get<Product & { currency: string }>(`/warehouse/products/${id}`);
      setProduct(card);
      setCurrency(card.currency || workspace.currency);
      const history = await api.get(`/warehouse/products/${id}/moves?per_page=200`);
      setMoves(history.items);
      setTotal(history.total);
    } catch (e) {
      toastError(e);
      navigate("/warehouse");
    }
  }, [id, workspace.currency, toastError, navigate]);

  useEffect(() => {
    void load();
  }, [load]);

  if (!product) return <ScreenLoading />;

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
        {/* Об этом стоит напоминать прямо на экране: увидев число, человек ищет
            поле, где его правят, — а правится оно только движением. */}
        <div className="field-desc" style={{ marginTop: 12 }}>{t("stockIsSumOfMoves")}</div>
        {negative && (
          <div className="field-desc" style={{ color: "var(--warning)" }}>
            {t("negativeStockHint")}
          </div>
        )}
      </div>

      {!product.is_service && <MoveForm product={product} onSaved={() => void load()} />}

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
      </div>

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
function MoveForm({ product, onSaved }: { product: Product; onSaved: () => void }) {
  const { t, toast, toastError } = useApp();
  const [kind, setKind] = useState<MoveKind>("in");
  const [quantity, setQuantity] = useState("");
  const [comment, setComment] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!quantity.trim()) return;
    setBusy(true);
    try {
      // Количество уходит строкой как набрали: разбирает его сервер, чтобы
      // лишние знаки после запятой получили отказ, а не тихое округление здесь.
      const result = await api.post("/warehouse/moves", {
        product_id: product.id,
        kind,
        quantity: quantity.trim(),
        comment: comment.trim() || null,
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
      setBusy(false);
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
      <div style={{ display: "grid", gridTemplateColumns: "160px 1fr auto", gap: 12, alignItems: "end" }}>
        <div className="field" style={{ marginBottom: 0 }}>
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
        <div className="field" style={{ marginBottom: 0 }}>
          <label className="label">{t("moveComment")}</label>
          <input className="input" value={comment} onChange={(e) => setComment(e.target.value)} />
        </div>
        <button className="btn btn-primary" disabled={busy}>
          {t("newMove")}
        </button>
      </div>
      <div className="field-desc">{t("quantityHint")}</div>
    </form>
  );
}
