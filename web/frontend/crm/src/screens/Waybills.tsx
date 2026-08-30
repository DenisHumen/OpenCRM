import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { ContextMenu, punktyDlyaZapisi, useContextMenu } from "../components/ContextMenu";
import { Icon } from "../components/Icon";
import { Chip, Dochitat, EmptyState, ScreenLoading } from "../components/ui";
import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { useDebounced } from "../lib/debounce";
import { useFailure } from "../lib/failure";
import { useGuard } from "../lib/guard";
import { formatMoney } from "../lib/format";

/** Список накладных: расходных и приходных.
 *
 * Экран отдельный от заказов, хотя таблица у них одна и строки общие. Вопросы
 * разные: у заказа спрашивают «когда отдадим», у накладной — «что уже уехало».
 * Отбор по виду делает сервер.
 */

export interface WaybillLine {
  id: number;
  product_id: number | null;
  /** Снимок на момент добавления: товар переименуют, накладная не поедет. */
  name: string;
  quantity_milli: number;
  picked_milli: number;
  price: number | null;
  cost: number | null;
}

export interface Waybill {
  id: number;
  number: string;
  kind: "waybill_out" | "waybill_in";
  status: string;
  client_id: number | null;
  deal_id: number | null;
  basis_id: number | null;
  warehouse_id: number | null;
  /** Считает сервер, а не экран: правило «черновик правится» живёт в службе. */
  pravitsya: boolean;
  lines: WaybillLine[];
  total: number | null;
  created_at: string | null;
}

/** Подписи состояний.
 *
 * Свой набор, а не общий с бланком, и это тот редкий случай, когда второй набор
 * слов оправдан. У накладной те же четыре ключа означают другое: `issued` — не
 * «выдана клиенту», а «проведена, товар уехал»; `closed` — не «вещь отдали», а
 * «получатель расписался». Показывать здесь слова бланка значит называть
 * отгрузку выдачей квитанции.
 */
export const WAYBILL_STATUS_LABEL = {
  draft: "wbDraft",
  issued: "wbPosted",
  closed: "wbConfirmed",
  cancelled: "wbCancelled",
} as const;

/** По скольку накладных дочитывается список. */
const NA_STRANITSE = 100;

export function Waybills() {
  const { t, locale, workspace, toastError } = useApp();
  const navigate = useNavigate();
  const kontekst = useContextMenu();
  const [query, setQuery] = useState("");
  const [kind, setKind] = useState<string>("");
  const [data, setData] = useState<{ items: Waybill[]; total: number } | null>(null);
  // До какой страницы дочитан список. Прежде экран просил сотню накладных и на
  // этом заканчивался — а в подзаголовке честно писал «всего N». Сам сообщал,
  // что показывает часть, и ничего с этим сделать не давал.
  const [stranitsa, setStranitsa] = useState(1);
  const [dochityvaem, setDochityvaem] = useState(false);
  // Отбор, которому принадлежит показанный список. Ставится загрузкой,
  // сверяется дочиткой: пока вторая страница по «иван» едет, человек успевает
  // набрать «п», первая страница «п» заменяет список — и опоздавшая страница
  // дописывается к чужим находкам. На экране два отбора вперемешку, а «всего»
  // от прошлого.
  const otbor_spiska = useRef("");
  const [attempt, setAttempt] = useState(0);
  const guard = useGuard();
  const { failure, fail, clear } = useFailure();

  const search = useDebounced(query);

  // Отбор без номера страницы: положи страницу сюда — и смена отбора станет
  // неотличима от перехода на следующую. Загрузка зависит только от отбора и
  // всегда просит первую страницу, дочитка приписывает номер сама.
  const otbor = useMemo(() => {
    const params = new URLSearchParams({ per_page: String(NA_STRANITSE) });
    if (search) params.set("search", search);
    if (kind) params.set("kind", kind);
    return `/waybills?${params}`;
  }, [search, kind]);

  useEffect(() => {
    // Вид переключают быстрее, чем отвечает сервер: без счётчика ответ по
    // прошлому виду ложится поверх текущего. Приём тот же, что у заказов.
    let current = true;
    otbor_spiska.current = otbor;
    clear();
    api
      .get<{ items: Waybill[]; total: number }>(`${otbor}&page=1`)
      .then((found) => {
        if (!current) return;
        setData(found);
        setStranitsa(1);
      })
      .catch((e) => {
        if (current) fail(e);
      });
    return () => {
      current = false;
    };
  }, [otbor, attempt, fail, clear]);

  /** Дочитать список.
   *
   * Отдельным действием, а не номером страницы в пути загрузки, и номер растёт
   * ПОСЛЕ удачного ответа. Иначе отказ на второй странице оставлял бы счётчик
   * на двойке, а следующее нажатие просило бы третью — вторая сотня накладных
   * пропадала бы из списка навсегда и молча.
   *
   * Отказ говорит о себе всплывающей жалобой, а не через `fail`: `fail` рисует
   * экран «не удалось загрузить», а он виден только пока показывать нечего.
   * После первой удачной загрузки отказ дочитки не показал бы ничего вовсе —
   * кнопка просто переставала бы отвечать.
   */
  const dochitat = async () => {
    if (dochityvaem) return;
    setDochityvaem(true);
    const sprosheno = otbor;
    try {
      // `dalshe.total` — сколько накладных всего; не путать с `waybill.total`,
      // который сумма денег по одной накладной.
      const dalshe = await api.get<{ items: Waybill[]; total: number }>(
        `${otbor}&page=${stranitsa + 1}`,
      );
      // Отбор сменился, пока страница ехала, — ответ чужой.
      if (otbor_spiska.current !== sprosheno) return;
      setData((bylo) =>
        bylo ? { ...dalshe, items: [...bylo.items, ...dalshe.items] } : dalshe,
      );
      setStranitsa((bylo) => bylo + 1);
    } catch (e) {
      toastError(e);
    } finally {
      setDochityvaem(false);
    }
  };

  const create = async (which: string) => {
    // Заводится пустой черновик и сразу открывается: пока сервер отдаёт номер,
    // кнопка выглядит неотвеченной, и второе нажатие завело бы вторую бумагу.
    if (!guard.take()) return;
    try {
      const waybill = await api.post<Waybill>("/waybills", { kind: which });
      navigate(`/waybills/${waybill.id}`);
    } catch (e) {
      toastError(e);
      guard.free();
    }
  };

  if (!data) {
    return <ScreenLoading error={failure} onRetry={() => setAttempt((n) => n + 1)} />;
  }

  return (
    <div className="page">
      <ContextMenu menu={kontekst.menu} zakryt={kontekst.zakryt} />
      <div className="page-head">
        <div>
          <h1 className="page-title">{t("waybills")}</h1>
          <div className="page-sub">{t("waybillsSub", { total: data.total })}</div>
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          <button
            className="btn btn-primary"
            disabled={guard.busy}
            onClick={() => void create("waybill_out")}
          >
            <Icon name="plus" stroke={2} />
            {t("newWaybillOut")}
          </button>
          <button
            className="btn btn-secondary"
            disabled={guard.busy}
            onClick={() => void create("waybill_in")}
          >
            {t("newWaybillIn")}
          </button>
        </div>
      </div>

      <div style={{ display: "flex", gap: 10, marginBottom: 16, flexWrap: "wrap" }}>
        <div
          style={{
            flex: "2 1 180px", minWidth: 0, display: "flex", alignItems: "center", gap: 8,
            padding: "0 12px", height: 36, border: "1px solid var(--border)",
            borderRadius: 8, background: "var(--surface)",
          }}
        >
          <Icon name="search" size={15} className="" />
          <input
            style={{ flex: 1, minWidth: 0, background: "none", border: "none", outline: "none", color: "var(--text)", fontSize: 13.5, fontFamily: "var(--sans)" }}
            placeholder={t("search")}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            autoFocus
          />
        </div>
        {[
          ["", t("viewAll")],
          ["waybill_out", t("waybillKindOut")],
          ["waybill_in", t("waybillKindIn")],
        ].map(([value, label]) => (
          <button
            key={value || "all"}
            className={"filter-chip" + (kind === value ? " active" : "")}
            onClick={() => setKind(value)}
          >
            {label}
          </button>
        ))}
      </div>

      <div className="list-card">
        {data.items.map((waybill) => (
          <Link
            to={`/waybills/${waybill.id}`}
            key={waybill.id}
            className="list-row hoverable"
            onContextMenu={(e) => kontekst.otkryt(e, punktyDlyaZapisi(`/waybills/${waybill.id}`, t, navigate))}
          >
            <span style={{ width: 110, color: "var(--faint)", fontSize: 12.5, fontFamily: "ui-monospace, monospace" }}>
              {waybill.number}
            </span>
            <span style={{ width: 110 }}>
              <Chip>
                {waybill.kind === "waybill_out" ? t("waybillKindOut") : t("waybillKindIn")}
              </Chip>
            </span>
            <span style={{ flex: 1, minWidth: 0, color: "var(--muted)", fontSize: 12.5 }}>
              {waybill.lines.length
                ? waybill.lines.map((line) => line.name).join(" · ")
                : t("orderLines")}
            </span>
            <span style={{ width: 120, textAlign: "right", color: "var(--text)", fontSize: 13 }}>
              {formatMoney(waybill.total, workspace.currency, locale)}
            </span>
            <span style={{ width: 130, textAlign: "right" }}>
              <Chip
                variant={
                  waybill.status === "closed" || waybill.status === "issued"
                    ? "success"
                    : undefined
                }
              >
                {t(WAYBILL_STATUS_LABEL[waybill.status as keyof typeof WAYBILL_STATUS_LABEL] ?? "wbDraft")}
              </Chip>
            </span>
          </Link>
        ))}
        {data.items.length === 0 && <EmptyState title={t("waybillsEmpty")} />}
        <Dochitat
          pokazano={data.items.length}
          vsego={data.total}
          zanyat={dochityvaem}
          onClick={() => void dochitat()}
        />
      </div>
    </div>
  );
}
