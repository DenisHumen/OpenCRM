import { useState } from "react";

import { Icon } from "./Icon";
import { KartochkaNapominaniya } from "./KartochkaNapominaniya";
import { LoadFailed } from "./ui";
import { useApp } from "../lib/app";
import { formatDateTime, parseDate } from "../lib/format";
import { moduleOn } from "../lib/modules";
import { can } from "../lib/permissions";
import { opisat } from "../lib/povtor";
import { useReference } from "../lib/reference";
import { srochno, vazhnost } from "../lib/vazhnost";
import { QuickTask } from "../screens/Tasks";

export interface Privyazka {
  client_id?: number;
  deal_id?: number;
  document_id?: number;
  product_id?: number;
  board_id?: number;
}

/**
 * Напоминания при карточке другого блока: заявки, клиента, заказа, бумаги,
 * товара. «Перезвонить в четверг» придумывается во время разговора о заявке, а
 * не потом на отдельном экране (docs/bloki/29 §7). Список — только видимые
 * смотрящему: личное чужое сюда не просачивается.
 */
export function NapominaniyaKartochki({
  privyazka,
  klientId,
  order,
}: {
  privyazka: Privyazka;
  /** Клиент бумаги или заявки — чтобы напоминание само привязалось и к нему. */
  klientId?: number | null;
  order?: number;
}) {
  const { t, locale, modules, user } = useApp();
  const [otkryto, setOtkryto] = useState<number | null>(null);
  const vklyucheno = moduleOn(modules, "tasks") && can(user, "tasks.view");
  const zapros = new URLSearchParams(
    Object.entries(privyazka).filter(([, v]) => v) as [string, string][],
  ).toString();
  const napominaniya = useReference<any>(vklyucheno ? `/tasks?scope=open&${zapros}` : null);
  if (!vklyucheno) return null;

  return (
    <div className="card card-pad" style={{ marginBottom: 20, order }}>
      <div className="metric-title" style={{ marginBottom: 12 }}>
        <Icon name="bell" size={13} />
        {t("tasks")}
      </div>
      {(napominaniya.items ?? []).map((task: any) => {
        const at = parseDate(task.due_at);
        const late = at && at.getTime() < Date.now();
        return (
          <button
            key={task.id}
            type="button"
            className={"doc-mini napom-mini" + (srochno(task.vazhnost) ? " urgent" : "")}
            onClick={() => setOtkryto(task.id)}
          >
            <i className={"kalendar-tochka " + vazhnost(task.vazhnost)} />
            <span className="truncate" style={{ flex: 1, minWidth: 0 }}>{task.title}</span>
            {task.povtor && (
              <span title={opisat(task.povtor, task.povtor_posle, t)}>
                <Icon name="repeat" size={11} />
              </span>
            )}
            {task.due_at && (
              <span className={late ? "task-late" : undefined} style={{ fontSize: 12, color: late ? undefined : "var(--faint)" }}>
                {formatDateTime(task.due_at, locale)}
              </span>
            )}
          </button>
        );
      })}
      {napominaniya.failure !== null && <LoadFailed error={napominaniya.failure} onRetry={napominaniya.reload} />}
      {can(user, "tasks.create") && (
        <QuickTask
          clientId={privyazka.client_id ?? klientId ?? undefined}
          dealId={privyazka.deal_id}
          documentId={privyazka.document_id}
          productId={privyazka.product_id}
          boardId={privyazka.board_id}
          onCreated={napominaniya.reload}
        />
      )}
      {otkryto !== null && (
        <KartochkaNapominaniya taskId={otkryto} onClose={() => setOtkryto(null)} onChanged={napominaniya.reload} />
      )}
    </div>
  );
}
