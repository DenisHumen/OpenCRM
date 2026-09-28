import { useState } from "react";
import { Link } from "react-router-dom";

import { Icon } from "./Icon";
import { KnopkaKorziny } from "./ui";
import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { useGuard } from "../lib/guard";
import { kindLabel } from "../lib/documents";
import { formatDateTime, parseDate } from "../lib/format";
import { moduleOn } from "../lib/modules";
import { opisat } from "../lib/povtor";
import { VAZHNOST_LABEL, srochno, vazhnost } from "../lib/vazhnost";

/** Куда ведёт бумага: у заказа, накладной и возврата — свои экраны. */
export function putBumagi(kind: string | null | undefined, id: number): string {
  if (kind === "sales_order" || kind === "purchase_order") return `/orders/${id}`;
  if (kind === "waybill_out" || kind === "waybill_in") return `/waybills/${id}`;
  if (kind === "return") return `/returns/${id}`;
  return `/documents/${id}`;
}

/** «Отложить» одним нажатием: 10 минут, час, завтра в 9:00 по местному. */
export function otlozhitDo(variant: "10" | "60" | "zavtra"): string {
  const moment = new Date();
  if (variant === "zavtra") {
    moment.setDate(moment.getDate() + 1);
    moment.setHours(9, 0, 0, 0);
  } else {
    moment.setMinutes(moment.getMinutes() + Number(variant));
  }
  return moment.toISOString();
}

/** Перенос срока одним нажатием: «завтра» и «через неделю» — в 10:00 по местному. */
function sdvig(dney: number): string {
  const moment = new Date();
  moment.setDate(moment.getDate() + dney);
  moment.setHours(10, 0, 0, 0);
  return moment.toISOString();
}

/**
 * Строка напоминания — одна на список, панель дня и карточки других блоков.
 *
 * Вид прежний (владелец 28.09.2026: «фиолетовая обводка у срочного нравится,
 * сохранить»): волна по краю у незакрытого срочного, отметка одним нажатием,
 * заголовок-кнопка открывает карточку.
 */
export function StrokaNapominaniya({
  task,
  vremya,
  sdelan,
  budushchiy,
  sSrokom = true,
  sUdaleniem = false,
  onOpen,
  onChanged,
}: {
  task: any;
  /** Время в колонке слева — в панели дня. */
  vremya?: string;
  /** Раз закрыт (закрытый раз повторяющегося — при открытом напоминании). */
  sdelan?: boolean;
  /** Будущий раз повторяющегося: отметить его нельзя, он ещё не наступил. */
  budushchiy?: boolean;
  sSrokom?: boolean;
  sUdaleniem?: boolean;
  onOpen: (id: number) => void;
  onChanged: () => void;
}) {
  const { t, locale, modules, user, toastError } = useApp();
  const [menyu, setMenyu] = useState(false);
  const guard = useGuard();
  const zakryt = sdelan ?? task.is_done;
  const at = parseDate(task.due_at);
  const late = at && !zakryt && !budushchiy && at.getTime() < Date.now();
  const vazhnoe = vazhnost(task.vazhnost);
  // Волна — только у незакрытых: у сделанного срочность в прошлом, а движущаяся
  // рамка тянет взгляд на то, что уже неважно.
  const volna = srochno(vazhnoe) && !zakryt && !budushchiy;
  // Кому ещё звонит — кроме смотрящего: «себе» в своей же строке ничего не говорит.
  const drugie = (task.lyudi ?? []).filter((c: any) => c.poluchaet && c.user_id !== user?.id);

  // Засов: двойное «Готово» у повторяющегося сдвинуло бы срок на два раза вперёд.
  const deystvie = async (put: string, telo?: unknown) => {
    if (!guard.take()) return;
    try {
      await api.post(`/tasks/${task.id}/${put}`, telo);
      onChanged();
    } catch (e) {
      toastError(e);
    } finally {
      guard.free();
    }
  };
  const pravit = async (telo: Record<string, unknown>) => {
    try {
      await api.patch(`/tasks/${task.id}`, telo);
      onChanged();
    } catch (e) {
      toastError(e);
    }
  };

  return (
    <div className={"task-row" + (volna ? " urgent" : "") + (budushchiy ? " napom-budushchiy" : "")}>
      {vremya !== undefined && <div className="napom-vremya">{vremya}</div>}
      <button
        className={"task-check" + (zakryt ? " done" : "")}
        onClick={() => void (zakryt ? pravit({ is_done: false }) : deystvie("done"))}
        disabled={budushchiy || (sdelan && !task.is_done)}
        aria-label={t("napomGotovo")}
      >
        {zakryt && <Icon name="check" size={12} stroke={2.5} />}
      </button>
      <div className="task-text">
        <button
          type="button"
          className={"task-title" + (zakryt ? " done" : "")}
          onClick={() => onOpen(task.id)}
        >
          {task.title}
        </button>
        <div className="task-meta">
          {vazhnoe !== "normal" && (
            <span className={"importance-chip " + vazhnoe}>{t(VAZHNOST_LABEL[vazhnoe])}</span>
          )}
          {task.povtor && (
            <span className="task-attached" title={opisat(task.povtor, task.povtor_posle, t)}>
              <Icon name="repeat" size={11} />
              {budushchiy ? t("napomBudushchiy") : opisat(task.povtor, task.povtor_posle, t)}
            </span>
          )}
          {task.obshchee && <span className="napom-polka">{t("napomNaPolke")}</span>}
          {task.files_count > 0 && (
            <span className="task-attached" title={t("tasksFiles")}>
              <Icon name="image" size={11} />
              {task.files_count}
            </span>
          )}
          {task.note_est && (
            <span className="task-attached" title={t("tasksNote")}>
              <Icon name="note" size={11} />
            </span>
          )}
          {task.shagi?.[1] > 0 && (
            <span className="task-attached" title={t("napomShagi")}>
              <Icon name="check" size={11} />
              {task.shagi[0]}/{task.shagi[1]}
            </span>
          )}
          {sSrokom && at && (
            <span className={late ? "task-late" : undefined}>
              {task.ves_den ? `${formatDateTime(task.due_at, locale).split(",")[0]} · ${t("napomVesDen")}` : formatDateTime(task.due_at, locale)}
            </span>
          )}
          {!zakryt && !budushchiy && sSrokom && (
            <>
              <button type="button" className="task-shift" onClick={() => void pravit({ due_at: sdvig(1) })}>
                {t("tasksShiftTomorrow")}
              </button>
              <button type="button" className="task-shift" onClick={() => void pravit({ due_at: sdvig(7) })}>
                {t("tasksShiftWeek")}
              </button>
            </>
          )}
          {drugie.length > 0 && (
            <span className="task-attached" title={t("napomPoluchateli")}>
              <Icon name="user" size={11} />
              {drugie.map((c: any) => c.name).join(", ")}
            </span>
          )}
          {task.deal_id && (
            <Link to={`/deals/${task.deal_id}`} className="text-link">
              {task.deal_title || t("deal")}
            </Link>
          )}
          {task.client_id && (
            <Link to={`/clients/${task.client_id}`} className="text-link">
              {task.client_name || t("client")}
            </Link>
          )}
          {task.document_id && task.document_title && (
            <Link to={putBumagi(task.document_kind, task.document_id)} className="text-link">
              {kindLabel(t, task.document_kind)} {task.document_title}
            </Link>
          )}
          {task.product_id && task.product_name && moduleOn(modules, "warehouse") && (
            <Link to={`/warehouse/${task.product_id}`} className="text-link">
              {task.product_name}
            </Link>
          )}
          {task.board_id && task.board_title && moduleOn(modules, "boards") && (
            <Link to={`/boards/${task.board_id}`} className="text-link">
              {task.board_title}
            </Link>
          )}
        </div>
      </div>
      {!zakryt && !budushchiy && (
        <div className="napom-deystviya">
          <button
            type="button"
            className="btn-icon"
            aria-haspopup="menu"
            aria-expanded={menyu}
            title={t("napomOtlozhit")}
            onClick={() => setMenyu((m) => !m)}
          >
            <Icon name="clock" size={14} />
          </button>
          {menyu && (
            <div className="napom-menyu" role="menu" onMouseLeave={() => setMenyu(false)}>
              {(["10", "60", "zavtra"] as const).map((v) => (
                <button
                  key={v}
                  type="button"
                  role="menuitem"
                  onClick={() => {
                    setMenyu(false);
                    void deystvie("snooze", { do: otlozhitDo(v) });
                  }}
                >
                  {t(v === "10" ? "otlozhit10" : v === "60" ? "otlozhit60" : "otlozhitZavtra")}
                </button>
              ))}
              {task.povtor && (
                <button
                  type="button"
                  role="menuitem"
                  onClick={() => {
                    setMenyu(false);
                    void deystvie("skip");
                  }}
                >
                  {t("napomPropustit")}
                </button>
              )}
            </div>
          )}
        </div>
      )}
      {sUdaleniem && (
        <KnopkaKorziny
          onClick={() =>
            void api.del(`/tasks/${task.id}`).then(onChanged, (e) => toastError(e))
          }
        />
      )}
    </div>
  );
}
