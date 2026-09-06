import { useCallback, useEffect, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { Icon } from "../components/Icon";
import { PrintLangs } from "../components/PrintLangs";
import { Chip, ConfirmModal, ScreenLoading } from "../components/ui";
import { api, ApiError } from "../lib/api";
import { useApp } from "../lib/app";
import { useFailure } from "../lib/failure";
import { useGuard } from "../lib/guard";
import { formatDate, formatMoney, formatQuantity, toMinorUnits } from "../lib/format";
import { paperLink, statusLabel, statusVariant } from "../lib/documents";
import { can } from "../lib/permissions";
import { useLiveTopic } from "../lib/live";
import { osnovanieKey, type Waybill } from "./Waybills";

/** Карточка накладной: позиции, проведение, сторнирование.
 *
 * **Экран построен вокруг одного различия — черновик или уже нет.** До
 * проведения правится всё, после не правится ничего: товар уехал, и бумага
 * стала документом о свершившемся.
 *
 * Признак `pravitsya` приходит С СЕРВЕРА, а не вычисляется здесь по статусу.
 * Правило живёт в службе (`waybill_service._tolko_chernovik`), и экран,
 * вычисляющий его заново, завёл бы второй ответ на тот же вопрос — разошлись
 * бы они в тот день, когда в службе появится ещё одно условие.
 *
 * Спрятанная кнопка при этом ничего не запрещает: неизменяемость держится
 * сервером. Экран лишь не предлагает того, на что всё равно получит отказ.
 */
export function WaybillCard() {
  const { id } = useParams();
  const { t, locale, user, workspace, toast, toastError } = useApp();
  const navigate = useNavigate();
  const [waybill, setWaybill] = useState<Waybill | null>(null);
  // Проведение трогает склад. Засов, а не флаг состояния: двойное нажатие
  // записало бы движения дважды, а остаток равен их сумме — отличить лишнее от
  // настоящего потом нечем.
  const guard = useGuard();
  const [shortage, setShortage] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<"cancel" | "reverse" | "delete" | null>(null);
  const { failure, fail, clear } = useFailure();

  const load = useCallback(async () => {
    clear();
    try {
      setWaybill(await api.get<Waybill>(`/waybills/${id}`));
    } catch (e) {
      if (e instanceof ApiError && (e.status === 404 || e.status === 403)) {
        toastError(e);
        navigate("/waybills");
        return;
      }
      fail(e);
    }
  }, [id, toastError, navigate, fail, clear]);

  useEffect(() => {
    void load();
  }, [load]);

  // Проведённая соседом накладная на открытой карточке оставалась черновиком
  // с кнопкой «Провести» до перезагрузки.
  useLiveTopic("waybills", (sob) => {
    if (sob.resync || sob.hints.some((h) => h.id === Number(id))) void load();
  });

  if (!waybill) return <ScreenLoading error={failure} onRetry={() => void load()} />;

  const draft = waybill.pravitsya;
  const canEdit = can(user, "waybills.edit") && draft;
  const canIssue = can(user, "waybills.issue");
  const outgoing = waybill.kind === "waybill_out";

  const post = async (confirmNegative: boolean) => {
    if (!guard.take()) return;
    setShortage(null);
    try {
      setWaybill(
        await api.post<Waybill>(`/waybills/${waybill.id}/post`, {
          confirm_negative: confirmNegative,
        }),
      );
      toast(t("wbPosted"));
    } catch (e) {
      // Нехватка — не ошибка ввода, а вопрос человеку: отгружать ли в минус.
      // Показываем ЧТО именно не сходится и переспрашиваем; общий тост увёл бы
      // этот выбор в угол экрана, где его закрывают не читая.
      if (e instanceof ApiError && e.code === "not_enough_stock") {
        setShortage(e.message);
      } else {
        toastError(e);
      }
    } finally {
      guard.free();
    }
  };

  const udalit = async () => {
    if (!guard.take()) return;
    try {
      await api.del(`/waybills/${waybill.id}`);
      toast(t("paperDeleted"));
      navigate("/waybills");
    } catch (err) {
      toastError(err);
    } finally {
      guard.free();
    }
  };

  const act = async (what: "confirm" | "cancel") => {
    if (!guard.take()) return;
    try {
      setWaybill(await api.post<Waybill>(`/waybills/${waybill.id}/${what}`, { note: "" }));
    } catch (e) {
      toastError(e);
    } finally {
      guard.free();
      setConfirm(null);
    }
  };

  const reverse = async () => {
    if (!guard.take()) return;
    try {
      const storno = await api.post<Waybill>(`/waybills/${waybill.id}/reverse`);
      // Уходим в сторно: оно рождается черновиком, и человеку почти всегда
      // надо поправить в нём количества («вернули четыре из шести»).
      navigate(`/waybills/${storno.id}`);
    } catch (e) {
      toastError(e);
      guard.free();
    } finally {
      setConfirm(null);
    }
  };

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <Link to="/waybills" className="btn btn-secondary btn-sm">
            <Icon name="arrowLeft" size={15} />
            {t("waybills")}
          </Link>
          <h1 className="page-title" style={{ marginTop: 8 }}>
            {waybill.number}
          </h1>
          <div className="page-sub" style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <Chip>{outgoing ? t("waybillKindOut") : t("waybillKindIn")}</Chip>
            <Chip variant={statusVariant(waybill.status, waybill.kind)}>{statusLabel(t, waybill.status, waybill.kind)}</Chip>
            {waybill.created_at && <span>{formatDate(waybill.created_at, locale)}</span>}
            {waybill.basis_id !== null && waybill.basis_kind && (
              <Link to={paperLink({ id: waybill.basis_id, kind: waybill.basis_kind })} style={{ color: "var(--brand)" }}>
                {t(osnovanieKey(waybill.basis_kind), { n: waybill.basis_number ?? "" })}
              </Link>
            )}
          </div>
        </div>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          {draft && canIssue && (
            <button
              className="btn btn-primary"
              disabled={guard.busy || waybill.lines.length === 0}
              onClick={() => void post(false)}
            >
              {outgoing ? t("wbPost") : t("wbReceive")}
            </button>
          )}
          {waybill.status === "issued" && can(user, "waybills.edit") && (
            <button className="btn btn-secondary" disabled={guard.busy} onClick={() => void act("confirm")}>
              {outgoing ? t("wbConfirm") : t("wbCloseIn")}
            </button>
          )}
          {!draft && waybill.status !== "cancelled" && canIssue && (
            <button className="btn btn-secondary" disabled={guard.busy} onClick={() => setConfirm("reverse")}>
              {t("wbReverse")}
            </button>
          )}
          {draft && canEdit && (
            <button className="btn btn-secondary" disabled={guard.busy} onClick={() => setConfirm("cancel")}>
              {t("wbCancel")}
            </button>
          )}
          {(draft || waybill.status === "cancelled") && can(user, "waybills.edit") && (
            <button className="text-link danger" disabled={guard.busy} onClick={() => setConfirm("delete")}>
              {t("paperDelete")}
            </button>
          )}
        </div>
      </div>

      {/* Печать — только у проведённой, и это правило сервера, а не вкус экрана.
          Напечатанный черновик дал бы лист с подписью получателя под перечнем,
          который назавтра станет другим. */}
      {(waybill.status === "issued" || waybill.status === "closed") && (
        <div style={{ marginBottom: 16 }}>
          <PrintLangs base={`/api/v1/waybills/${waybill.id}/print`} current={waybill.locale} />
        </div>
      )}

      {/* Почему кнопок правки нет — сказано словами, а не показано пустотой.
          Исчезнувшая без объяснения кнопка читается как поломка. */}
      {!draft && (
        <div className="field-desc" style={{ margin: "0 0 12px" }}>
          {t("wbFinal")}
        </div>
      )}

      {shortage && (
        <div
          className="field-desc"
          style={{
            display: "flex", gap: 12, alignItems: "center",
            color: "var(--warning)", margin: "0 0 12px",
          }}
        >
          <span style={{ flex: 1 }}>{shortage}</span>
          <button className="btn btn-sm btn-secondary" disabled={guard.busy} onClick={() => void post(true)}>
            {t("wbPostAnyway")}
          </button>
          <button className="btn btn-secondary btn-sm" onClick={() => setShortage(null)}>
            {t("cancel")}
          </button>
        </div>
      )}

      <WaybillLines waybill={waybill} canEdit={canEdit} onChanged={() => void load()} />

      <div className="list-card" style={{ marginTop: 12 }}>
        <div className="list-row" style={{ justifyContent: "space-between" }}>
          <span style={{ color: "var(--muted)" }}>{t("orderTotal")}</span>
          <span style={{ fontSize: 15 }}>
            {formatMoney(waybill.total, workspace.currency, locale)}
          </span>
        </div>
      </div>

      {confirm && (
        <ConfirmModal
          text={confirm === "cancel" ? t("wbCancelAsk") : confirm === "delete" ? t("paperDeleteConfirm", { number: waybill.number }) : t("wbReverseAsk")}
          confirmLabel={confirm === "cancel" ? t("wbCancel") : confirm === "delete" ? t("paperDelete") : t("wbReverse")}
          onConfirm={() => {
            if (confirm === "delete") void udalit();
            else if (confirm === "cancel") void act("cancel");
            else void reverse();
          }}
          onClose={() => setConfirm(null)}
        />
      )}
    </div>
  );
}

/** Позиции. Правятся только в черновике — довод в шапке экрана. */
function WaybillLines({
  waybill,
  canEdit,
  onChanged,
}: {
  waybill: Waybill;
  canEdit: boolean;
  onChanged: () => void;
}) {
  const { t, locale, workspace, toastError } = useApp();
  const [name, setName] = useState("");
  const [quantity, setQuantity] = useState("1");
  const [price, setPrice] = useState("");
  const guard = useGuard();

  const add = async (event: FormEvent) => {
    event.preventDefault();
    if (!name.trim() || !guard.take()) return;
    try {
      await api.post(`/waybills/${waybill.id}/lines`, {
        name: name.trim(),
        quantity,
        price: price ? toMinorUnits(price) : null,
      });
      setName("");
      setQuantity("1");
      setPrice("");
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
      await api.del(`/waybills/${waybill.id}/lines/${lineId}`);
      onChanged();
    } catch (e) {
      toastError(e);
    } finally {
      guard.free();
    }
  };

  return (
    <div className="list-card">
      {waybill.lines.map((line) => (
        <div className="list-row" key={line.id}>
          <span style={{ flex: 1, minWidth: 0 }}>{line.name}</span>
          <span style={{ width: 100, textAlign: "right", color: "var(--muted)" }}>
            {formatQuantity(line.quantity_milli)}
          </span>
          <span style={{ width: 110, textAlign: "right" }}>
            {formatMoney(line.price, workspace.currency, locale)}
          </span>
          {canEdit && (
            <button
              className="btn btn-secondary btn-sm"
              disabled={guard.busy}
              onClick={() => void remove(line.id)}
              title={t("delete")}
            >
              <Icon name="trash" size={15} />
            </button>
          )}
        </div>
      ))}
      {waybill.lines.length === 0 && (
        <div className="list-row" style={{ color: "var(--faint)" }}>
          {t("orderLines")}
        </div>
      )}

      {canEdit && (
        <form className="list-row" onSubmit={(e) => void add(e)} style={{ gap: 8 }}>
          <input
            className="input"
            style={{ flex: 1 }}
            placeholder={t("name")}
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
          <input
            className="input"
            style={{ width: 90 }}
            value={quantity}
            onChange={(e) => setQuantity(e.target.value)}
          />
          <input
            className="input"
            style={{ width: 110 }}
            placeholder={t("sellPrice")}
            value={price}
            onChange={(e) => setPrice(e.target.value)}
          />
          {/* Значок без подписи — пустая кнопка для читалки экрана: она
              объявляет «кнопка» и замолкает. Имя даём явно. */}
          <button
            className="btn btn-secondary btn-sm"
            disabled={guard.busy}
            aria-label={t("add")}
            title={t("add")}
          >
            <Icon name="plus" size={15} />
          </button>
        </form>
      )}
    </div>
  );
}
