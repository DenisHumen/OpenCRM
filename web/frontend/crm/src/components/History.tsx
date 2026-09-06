import { useApp } from "../lib/app";
import { podpisSistemnoy } from "../lib/sistemnye_zapisi";
import { formatDateTime } from "../lib/format";

export interface HistoryEvent {
  id: number;
  from_status: string;
  to_status: string;
  note: string;
  author_name: string | null;
  created_at: string;
}

/** История переходов бумаги: когда, из чего во что и кто.
 *
 * Общая у квитанции, акта и заказа: спор о сроках («когда вы сказали, что
 * готово») разрешается записью с временем, а не текущим состоянием. Подписи
 * состояний у них разные, поэтому приходят функцией, а не берутся отсюда.
 */
export function History({
  events,
  label,
}: {
  events: HistoryEvent[] | undefined;
  label: (status: string) => string;
}) {
  const { t, locale } = useApp();
  return (
    <div className="card card-pad">
      <div className="metric-title" style={{ marginBottom: 12 }}>{t("history")}</div>
      <ol className="stage-log">
        {(events ?? []).map((event) => (
          <li key={event.id}>
            <span className="stage-log-when">{formatDateTime(event.created_at, locale)}</span>
            <span className="stage-log-what">
              {event.from_status
                ? `${label(event.from_status)} → ${label(event.to_status)}`
                : label(event.to_status)}
              {event.note && <span style={{ color: "var(--faint)" }}> · {podpisSistemnoy(event.note, t)}</span>}
            </span>
            <span className="stage-log-who">{event.author_name || "—"}</span>
          </li>
        ))}
      </ol>
    </div>
  );
}
