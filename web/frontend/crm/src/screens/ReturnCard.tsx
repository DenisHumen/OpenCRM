import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { History } from "../components/History";
import { Icon } from "../components/Icon";
import { Chip, ConfirmModal, EmptyState, LoadFailed, ScreenLoading } from "../components/ui";
import { VyborKlienta } from "../components/VyborKlienta";
import { WarehousePicker, useWarehouses } from "../components/Warehouses";
import { api, ApiError } from "../lib/api";
import { useApp } from "../lib/app";
import { dropTarget } from "../lib/dnd";
import { statusLabel, statusVariant } from "../lib/documents";
import { useFailure } from "../lib/failure";
import { formatDate, formatMoney, formatQuantity, toMinorUnits } from "../lib/format";
import { useGuard } from "../lib/guard";
import { useLiveTopic } from "../lib/live";
import { moduleOn } from "../lib/modules";
import { can } from "../lib/permissions";
import { useReference } from "../lib/reference";
import type { FinanceCategory } from "./Finance";
import type { Return, ReturnFile } from "./Returns";

/** Карточка возврата: что вернули, почему, сколько денег назад, фото.
 *
 * Экран построен вокруг одного различия — черновик или уже нет. До проведения
 * правится всё, после не правится ничего: товар на складе, деньги у клиента, и
 * бумага стала документом о свершившемся. Признак `pravitsya` приходит с
 * сервера, как у накладной.
 */
export function ReturnCard() {
  const { id } = useParams();
  const { t, locale, user, modules, workspace, toast, toastError } = useApp();
  const navigate = useNavigate();
  const [vozvrat, setVozvrat] = useState<Return | null>(null);
  // Проведение трогает склад и деньги. Засов, а не флаг состояния: двойное
  // нажатие вернуло бы товар дважды.
  const guard = useGuard();
  const [confirm, setConfirm] = useState<"cancel" | "delete" | null>(null);
  const places = useWarehouses();
  const [place, setPlace] = useState<number | null>(null);
  const { failure, fail, clear } = useFailure();

  const load = useCallback(async () => {
    clear();
    try {
      setVozvrat(await api.get<Return>(`/returns/${id}`));
    } catch (e) {
      if (e instanceof ApiError && (e.status === 404 || e.status === 403)) {
        toastError(e);
        navigate("/returns");
        return;
      }
      fail(e);
    }
  }, [id, toastError, navigate, fail, clear]);

  useEffect(() => {
    void load();
  }, [load]);

  useLiveTopic("orders", (s) => {
    if (s.resync || s.hints.some((h) => h.id === Number(id))) void load();
  });

  if (!vozvrat) return <ScreenLoading error={failure} onRetry={() => void load()} />;

  const draft = vozvrat.pravitsya;
  const canEdit = can(user, "orders.edit") && draft;
  const canIssue = can(user, "orders.issue");
  const dengi = moduleOn(modules, "finance") && can(user, "finance.view");

  const post = async () => {
    if (!guard.take()) return;
    try {
      setVozvrat(await api.post<Return>(`/returns/${vozvrat.id}/post`, { warehouse_id: place }));
      toast(t("returnPosted"));
      await load();
    } catch (e) {
      toastError(e);
    } finally {
      guard.free();
    }
  };

  const cancel = async () => {
    if (!guard.take()) return;
    try {
      setVozvrat(await api.post<Return>(`/returns/${vozvrat.id}/cancel`, { note: "" }));
    } catch (e) {
      toastError(e);
    } finally {
      guard.free();
      setConfirm(null);
    }
  };

  const udalit = async () => {
    if (!guard.take()) return;
    try {
      await api.del(`/returns/${vozvrat.id}`);
      toast(t("paperDeleted"));
      navigate("/returns");
    } catch (e) {
      toastError(e);
    } finally {
      guard.free();
    }
  };

  const patch = async (telo: Record<string, unknown>) => {
    try {
      setVozvrat((bylo) => bylo && { ...bylo, ...telo });
      const otvet = await api.patch<Return>(`/returns/${vozvrat.id}`, telo);
      setVozvrat((bylo) => bylo && { ...bylo, ...otvet });
    } catch (e) {
      toastError(e);
      void load();
    }
  };

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <Link to="/returns" className="btn btn-secondary btn-sm">
            <Icon name="arrowLeft" size={15} />
            {t("returns")}
          </Link>
          <h1 className="page-title" style={{ marginTop: 8 }}>
            {vozvrat.number}
          </h1>
          <div className="page-sub" style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
            <Chip>{t("kindReturn")}</Chip>
            <Chip variant={statusVariant(vozvrat.status, "return")}>{statusLabel(t, vozvrat.status, "return")}</Chip>
            {vozvrat.order_id && (
              <Link to={`/orders/${vozvrat.order_id}`} style={{ color: "var(--muted)", textDecoration: "underline", textUnderlineOffset: 2 }}>
                {t("returnOrder")} {vozvrat.order_number}
              </Link>
            )}
            {vozvrat.client_id ? (
              <Link to={`/clients/${vozvrat.client_id}`} style={{ color: "var(--muted)", textDecoration: "underline", textUnderlineOffset: 2 }}>
                {vozvrat.client_name ?? t("client")}
              </Link>
            ) : (
              <span style={{ color: "var(--faint)" }}>{t("noClient")}</span>
            )}
            {vozvrat.created_at && <span>{formatDate(vozvrat.created_at, locale)}</span>}
          </div>
        </div>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          {draft && canIssue && (
            <button
              className="btn btn-primary"
              disabled={guard.busy || vozvrat.lines.length === 0}
              onClick={() => void post()}
            >
              {t("returnPost")}
            </button>
          )}
          {canEdit && (
            <button className="btn btn-secondary" disabled={guard.busy} onClick={() => setConfirm("cancel")}>
              {t("returnCancel")}
            </button>
          )}
          {(draft || vozvrat.status === "cancelled") && can(user, "orders.edit") && (
            <button className="text-link danger" disabled={guard.busy} onClick={() => setConfirm("delete")}>
              {t("paperDelete")}
            </button>
          )}
        </div>
      </div>

      {!draft && (
        <div className="field-desc" style={{ margin: "0 0 12px" }}>
          {t("returnFinal")}
        </div>
      )}

      {vozvrat.waybills && vozvrat.waybills.length > 0 && (
        <div className="card" style={{ padding: "10px 14px", marginBottom: 16 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
            <Icon name="arrowIn" size={14} />
            <span className="page-sub" style={{ marginTop: 0 }}>{t("orderWaybills")}</span>
            {vozvrat.waybills.map((w) => (
              <Link key={w.id} className="chip" to={`/waybills/${w.id}`}>
                {w.number} · {statusLabel(t, w.status, w.kind)}
              </Link>
            ))}
          </div>
        </div>
      )}

      <ReturnLines vozvrat={vozvrat} canEdit={canEdit} onChanged={() => void load()} />

      <div className="card card-pad" style={{ marginTop: 16 }}>
        <div className="metric-title" style={{ marginBottom: 12 }}>{t("returnDetails")}</div>
        <div className="field">
          <label className="label">{t("returnNote")}</label>
          {draft ? (
            <NoteField value={vozvrat.note} canEdit={canEdit} onSave={(note) => void patch({ note })} />
          ) : (
            <div style={{ color: vozvrat.note ? "var(--text)" : "var(--faint)", fontSize: 13.5, whiteSpace: "pre-wrap" }}>
              {vozvrat.note || "—"}
            </div>
          )}
          {draft && <div className="field-desc">{t("returnNoteHint")}</div>}
        </div>
        <div style={{ display: "flex", gap: 12, flexWrap: "wrap", alignItems: "end" }}>
          <div className="field" style={{ marginBottom: 0, flex: "0 1 200px" }}>
            <label className="label">{t("returnRefund")}</label>
            {draft ? (
              <RefundField value={vozvrat.refund} canEdit={canEdit} onSave={(refund) => void patch({ refund })} />
            ) : (
              <div className="money-value" style={{ fontSize: 18 }}>
                {formatMoney(vozvrat.refund, workspace.currency, locale)}
              </div>
            )}
          </div>
          {dengi && (
            <div className="field" style={{ marginBottom: 0, flex: "1 1 220px" }}>
              <label className="label">{t("finCategory")}</label>
              <CategoryField
                value={vozvrat.category_id}
                canEdit={canEdit}
                onChange={(category_id) => void patch({ category_id })}
              />
            </div>
          )}
          {draft && (
            <div className="field" style={{ marginBottom: 0, flex: "1 1 220px" }}>
              <label className="label">{t("client")}</label>
              <VyborKlienta
                value={vozvrat.client_id}
                imya={vozvrat.client_name}
                onPick={(client_id, imya) => {
                  setVozvrat((bylo) => bylo && { ...bylo, client_name: imya });
                  void patch({ client_id });
                }}
                pustoy
                pustoyPodpis={t("noClient")}
              />
            </div>
          )}
          {draft && places?.many && (
            <div className="field" style={{ marginBottom: 0, flex: "0 1 200px" }}>
              <label className="label">{t("warehousePick")}</label>
              <WarehousePicker places={places} value={place ?? places.items[0]?.id ?? null} onChange={setPlace} />
            </div>
          )}
        </div>
        <div className="field-desc">
          {t("returnRefundHint")}
          {dengi && ` ${t("returnCategoryHint")}`}
        </div>
      </div>

      <ReturnMedia vozvrat={vozvrat} canEdit={canEdit} onChanged={() => void load()} />

      <div style={{ marginTop: 20 }}>
        <History events={vozvrat.events} label={(status) => statusLabel(t, status, "return")} />
      </div>

      {confirm && (
        <ConfirmModal
          text={confirm === "cancel" ? t("returnCancelAsk", { number: vozvrat.number }) : t("paperDeleteConfirm", { number: vozvrat.number })}
          confirmLabel={confirm === "cancel" ? t("returnCancel") : t("paperDelete")}
          danger={confirm === "delete"}
          onConfirm={() => {
            if (confirm === "delete") void udalit();
            else void cancel();
          }}
          onClose={() => setConfirm(null)}
        />
      )}
    </div>
  );
}

/** Описание сохраняется по потере фокуса, а не на каждую букву. */
function NoteField({ value, canEdit, onSave }: { value: string; canEdit: boolean; onSave: (note: string) => void }) {
  const { t } = useApp();
  const [typed, setTyped] = useState(value);
  useEffect(() => setTyped(value), [value]);
  return (
    <textarea
      className="textarea"
      value={typed}
      disabled={!canEdit}
      placeholder={t("returnNoteHint")}
      onChange={(e) => setTyped(e.target.value)}
      onBlur={() => {
        if (typed !== value) onSave(typed);
      }}
    />
  );
}

function RefundField({ value, canEdit, onSave }: { value: number | null; canEdit: boolean; onSave: (refund: number) => void }) {
  const { t } = useApp();
  const [typed, setTyped] = useState(value === null ? "" : String(value / 100));
  useEffect(() => setTyped(value === null ? "" : String(value / 100)), [value]);
  return (
    <input
      className="input"
      value={typed}
      disabled={!canEdit}
      aria-label={t("returnRefund")}
      onChange={(e) => setTyped(e.target.value)}
      onBlur={() => {
        const next = toMinorUnits(typed);
        if (typed.trim() !== "" && next !== value) onSave(next);
      }}
    />
  );
}

/** Доходная статья, через которую деньги возвращаются клиенту. */
function CategoryField({ value, canEdit, onChange }: { value: number | null; canEdit: boolean; onChange: (id: number | null) => void }) {
  const { t } = useApp();
  const categories = useReference<FinanceCategory>("/finance/categories");
  const income = (categories.items ?? []).filter((row) => !row.closed && row.direction === "income");
  return (
    <>
      <select
        className="select"
        value={value === null ? "" : String(value)}
        disabled={!canEdit}
        aria-label={t("finCategory")}
        onChange={(e) => onChange(e.target.value ? Number(e.target.value) : null)}
      >
        <option value="">—</option>
        {income.map((row) => (
          <option key={row.id} value={row.id}>{row.name}</option>
        ))}
      </select>
      {categories.failure !== null ? (
        <LoadFailed error={categories.failure} onRetry={categories.reload} />
      ) : (
        categories.items !== null && income.length === 0 && <div className="field-desc">{t("finNoCategories")}</div>
      )}
    </>
  );
}

/** Позиции: только товары заказа, не больше отгруженного. Правятся в черновике. */
function ReturnLines({ vozvrat, canEdit, onChanged }: { vozvrat: Return; canEdit: boolean; onChanged: () => void }) {
  const { t, locale, workspace, toastError } = useApp();
  const guard = useGuard();
  const [product, setProduct] = useState("");
  const [quantity, setQuantity] = useState("1");
  const vNalichii = (vozvrat.order_lines ?? []).filter(
    (row) => row.max_milli > 0 && !vozvrat.lines.some((line) => line.product_id === row.product_id),
  );
  const maxOf = (productId: number | null) =>
    vozvrat.order_lines?.find((row) => row.product_id === productId)?.max_milli ?? null;

  const add = async () => {
    if (!product || !guard.take()) return;
    try {
      await api.post(`/returns/${vozvrat.id}/lines`, { product_id: Number(product), quantity });
      setProduct("");
      setQuantity("1");
      onChanged();
    } catch (e) {
      toastError(e);
    } finally {
      guard.free();
    }
  };

  const remove = async (lineId: number) => {
    if (!guard.take()) return;
    try {
      await api.del(`/returns/${vozvrat.id}/lines/${lineId}`);
      onChanged();
    } catch (e) {
      toastError(e);
    } finally {
      guard.free();
    }
  };

  return (
    <div className="card" style={{ overflow: "hidden" }}>
      <div className="list-header">
        <span style={{ flex: 1 }}>{t("returnLines")}</span>
        <span style={{ width: 160, textAlign: "right" }}>{t("quantity")}</span>
        <span style={{ width: 110, textAlign: "right" }}>{t("sellPrice")}</span>
        {canEdit && <span style={{ width: 34 }} />}
      </div>
      {vozvrat.lines.map((line) => (
        <div className="list-row" key={line.id}>
          <span style={{ flex: 1, minWidth: 0, color: "var(--text)", fontSize: 13.5 }}>{line.name}</span>
          <span style={{ width: 160, textAlign: "right", fontSize: 13, display: "flex", justifyContent: "flex-end", alignItems: "center", gap: 6 }}>
            {canEdit ? (
              <QuantityField
                value={line.quantity_milli}
                onSave={async (q) => {
                  try {
                    await api.patch(`/returns/${vozvrat.id}/lines/${line.id}`, { quantity: q });
                    onChanged();
                  } catch (e) {
                    toastError(e);
                  }
                }}
              />
            ) : (
              formatQuantity(line.quantity_milli)
            )}
            {canEdit && maxOf(line.product_id) !== null && (
              <span style={{ color: "var(--faint)", fontSize: 12 }}>
                {t("returnMax", { n: formatQuantity(maxOf(line.product_id)) })}
              </span>
            )}
          </span>
          <span style={{ width: 110, textAlign: "right", color: "var(--muted)", fontSize: 12.5 }}>
            {formatMoney(line.price, workspace.currency, locale)}
          </span>
          {canEdit && (
            <button className="btn-icon" title={t("delete")} disabled={guard.busy} onClick={() => void remove(line.id)}>
              <Icon name="trash" size={14} />
            </button>
          )}
        </div>
      ))}
      {vozvrat.lines.length === 0 && (
        <div className="list-row" style={{ color: "var(--faint)" }}>{t("orderLines")}</div>
      )}
      <div className="list-row" style={{ background: "var(--surface-2)" }}>
        <span style={{ flex: 1, color: "var(--muted)", fontSize: 12.5 }}>{t("orderTotal")}</span>
        <span style={{ color: "var(--text)", fontSize: 14, fontWeight: 600 }}>
          {formatMoney(vozvrat.total, workspace.currency, locale)}
        </span>
      </div>
      {canEdit && vNalichii.length > 0 && (
        <div className="list-row" style={{ gap: 8 }}>
          <select
            className="select select-inline"
            style={{ flex: 1, maxWidth: "none" }}
            value={product}
            aria-label={t("returnAddLine")}
            onChange={(e) => setProduct(e.target.value)}
          >
            <option value="">{t("returnAddLine")}</option>
            {vNalichii.map((row) => (
              <option key={row.product_id} value={row.product_id}>
                {row.name} · {t("returnMax", { n: formatQuantity(row.max_milli) })}
              </option>
            ))}
          </select>
          <input className="input" style={{ width: 90 }} value={quantity} aria-label={t("quantity")} onChange={(e) => setQuantity(e.target.value)} />
          <button className="btn btn-secondary btn-sm" disabled={guard.busy || !product} aria-label={t("add")} title={t("add")} onClick={() => void add()}>
            <Icon name="plus" size={15} />
          </button>
        </div>
      )}
    </div>
  );
}

function QuantityField({ value, onSave }: { value: number; onSave: (q: string) => void }) {
  const { t } = useApp();
  const [typed, setTyped] = useState(formatQuantity(value));
  useEffect(() => setTyped(formatQuantity(value)), [value]);
  return (
    <input
      className="input input-sm"
      style={{ width: 70, textAlign: "right" }}
      value={typed}
      aria-label={t("quantity")}
      onChange={(e) => setTyped(e.target.value)}
      onBlur={() => {
        if (typed.trim() && typed !== formatQuantity(value)) onSave(typed);
      }}
    />
  );
}

/** Фото и видео к возврату: как выглядела вещь. Снимки показываются на месте. */
function ReturnMedia({ vozvrat, canEdit, onChanged }: { vozvrat: Return; canEdit: boolean; onChanged: () => void }) {
  const { t, toastError } = useApp();
  const fileInput = useRef<HTMLInputElement>(null);
  const files: ReturnFile[] = vozvrat.files ?? [];

  const upload = async (list: FileList | null) => {
    if (!list) return;
    for (const file of Array.from(list)) {
      try {
        await api.upload(`/returns/${vozvrat.id}/files`, file);
      } catch (e) {
        toastError(e);
      }
    }
    onChanged();
  };

  return (
    <div className="card card-pad" style={{ marginTop: 16 }}>
      <div className="metric-title" style={{ marginBottom: 12 }}>{t("returnMedia")}</div>
      {canEdit && (
        <div
          className="dropzone"
          style={{ marginBottom: 14 }}
          onClick={() => fileInput.current?.click()}
          {...dropTarget((e) => void upload(e.dataTransfer.files))}
        >
          {t("dropFiles")} <span style={{ color: "var(--accent)", textDecoration: "underline", textUnderlineOffset: 2 }}>{t("browse")}</span>{" "}
          {t("returnMediaHint")}
          <input ref={fileInput} type="file" multiple accept="image/*,video/*" hidden onChange={(e) => void upload(e.target.files)} />
        </div>
      )}
      {files.length === 0 ? (
        <EmptyState icon="image" title={t("returnMediaEmpty")} />
      ) : (
        <div className="vlozheniya">
          {files.map((file) => (
            <figure key={file.id} className="vlozhenie">
              {file.mime.startsWith("video/") ? (
                <video className="vlozhenie-media" src={file.download_url} controls preload="metadata" />
              ) : (
                <a href={file.download_url} target="_blank" rel="noreferrer">
                  <img className="vlozhenie-media" src={file.download_url} alt={file.original_name} loading="lazy" />
                </a>
              )}
              <figcaption className="vlozhenie-podpis">
                <span className="truncate" title={file.original_name}>{file.original_name}</span>
                {canEdit && (
                  <button
                    type="button"
                    className="btn-icon"
                    title={t("delete")}
                    onClick={async () => {
                      try {
                        await api.del(`/returns/${vozvrat.id}/files/${file.id}`);
                        onChanged();
                      } catch (e) {
                        toastError(e);
                      }
                    }}
                  >
                    <Icon name="trash" size={13} />
                  </button>
                )}
              </figcaption>
            </figure>
          ))}
        </div>
      )}
    </div>
  );
}
