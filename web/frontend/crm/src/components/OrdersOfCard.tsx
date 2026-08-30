import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";

import { Chip, Dochitat } from "./ui";
import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { formatMoney } from "../lib/format";
import { moduleOn } from "../lib/modules";
import { can } from "../lib/permissions";
import { ORDER_STATUS_LABEL, type Order } from "../screens/Orders";

/** Заказы клиента или заявки — врезкой в карточке.
 *
 * Врезка одна на два места намеренно. Заказ может принадлежать заявке, но не
 * заменяет её: у мастерской заявка — это работа, а заказ — перечень; у магазина
 * заявки может не быть вовсе. Значит один и тот же список нужен и там, и там, и
 * второй его экземпляр разошёлся бы с первым при первой же правке.
 *
 * Возвращает null, когда блок выключен или права нет: раздела просто не
 * существует, а не «есть, но пустой».
 */
/** По скольку заказов дочитывается врезка. */
const NA_STRANITSE = 20;

/** Путь за одной страницей врезки.
 *
 * Отбор карточки собирается в одном месте на обе загрузки — первую и
 * дочитку: разойдись они хоть в одном условии, дочитка приписала бы к списку
 * чужие заказы, и заметить это было бы неоткуда. */
function putStranitsy(page: number, clientId?: number, dealId?: number): string {
  const params = new URLSearchParams({
    page: String(page),
    per_page: String(NA_STRANITSE),
  });
  if (dealId) params.set("deal_id", String(dealId));
  else if (clientId) params.set("client_id", String(clientId));
  return `/orders?${params}`;
}

export function OrdersOfCard({ clientId, dealId }: { clientId?: number; dealId?: number }) {
  const { t, locale, workspace, user, modules, toastError } = useApp();
  const [items, setItems] = useState<Order[] | null>(null);
  // Врезка брала двадцать заказов и молчала об остальных. У постоянного
  // покупателя двадцать первый заказ переставал существовать — а именно в
  // его карточку и заходят, чтобы посмотреть историю покупок.
  const [vsego, setVsego] = useState(0);
  const [stranitsa, setStranitsa] = useState(1);
  const [dochityvaem, setDochityvaem] = useState(false);
  // Чему принадлежит показанное. Ставит загрузка, сверяет дочитка: врезка
  // висит в карточке, карточку меняют переходом — и опоздавший ответ
  // дописал бы строки чужой заявки к нынешней, молча.
  const otbor_spiska = useRef("");

  const visible = moduleOn(modules, "orders") && can(user, "orders.view");

  useEffect(() => {
    if (!visible || (!clientId && !dealId)) return;
    let alive = true;
    otbor_spiska.current = putStranitsy(1, clientId, dealId);
    api
      .get<{ items: Order[]; total: number }>(putStranitsy(1, clientId, dealId))
      .then((data) => {
        if (!alive) return;
        setItems(data.items);
        setVsego(data.total);
        setStranitsa(1);
      })
      // Блок выключили, пока карточка была открыта, — врезка просто исчезает,
      // ровно как исчезла бы при перезагрузке. Это не ошибка человека.
      .catch(() => {
        if (!alive) return;
        setItems(null);
      });
    return () => {
      alive = false;
    };
  }, [visible, clientId, dealId]);

  /** Дочитать врезку.
   *
   * Отдельным действием, а не номером страницы в пути загрузки, и номер
   * растёт ПОСЛЕ удачного ответа. Иначе отказ на второй странице оставлял бы
   * счётчик на двойке, а следующее нажатие просило бы третью — два десятка
   * заказов пропали бы из истории покупок навсегда и молча.
   *
   * Отказ дочитки — всплывающая жалоба, в отличие от отказа первой загрузки
   * выше: там пустота объяснима выключенным блоком, а здесь список уже стоит
   * на экране, и молчание выглядело бы как переставшая работать кнопка.
   */
  const dochitat = async () => {
    if (dochityvaem) return;
    const sprosheno = putStranitsy(1, clientId, dealId);
    setDochityvaem(true);
    try {
      const dalshe = await api.get<{ items: Order[]; total: number }>(
        putStranitsy(stranitsa + 1, clientId, dealId),
      );
      // Отбор сменился, пока страница ехала, — ответ чужой.
      if (otbor_spiska.current !== sprosheno) return;
      setItems((bylo) => (bylo ? [...bylo, ...dalshe.items] : dalshe.items));
      setVsego(dalshe.total);
      setStranitsa((bylo) => bylo + 1);
    } catch (beda) {
      toastError(beda);
    } finally {
      setDochityvaem(false);
    }
  };

  // Пустой список не показываем: врезка «заказов нет» в каждой карточке
  // клиента — это строка, которую перестают читать на третьей карточке.
  if (!visible || !items || items.length === 0) return null;

  return (
    <div className="card card-pad" style={{ marginBottom: 20 }}>
      <div className="page-head" style={{ marginBottom: 12 }}>
        <div className="metric-title">{t("orders")}</div>
      </div>
      <div className="doc-mini-list">
        {items.map((order) => (
          <Link key={order.id} to={`/orders/${order.id}`} className="doc-mini">
            <span className="doc-number">{order.number}</span>
            <span className="truncate" style={{ flex: 1, minWidth: 0 }}>
              {order.lines.map((line) => line.name).join(" · ") || "—"}
            </span>
            <span style={{ color: "var(--muted)", fontSize: 12.5 }}>
              {formatMoney(order.total, workspace.currency, locale)}
            </span>
            <Chip variant={order.status === "closed" ? "success" : undefined}>
              {t(ORDER_STATUS_LABEL[order.status as keyof typeof ORDER_STATUS_LABEL] ?? "docIssued")}
            </Chip>
          </Link>
        ))}
        <Dochitat
          pokazano={items.length}
          vsego={vsego}
          zanyat={dochityvaem}
          onClick={() => void dochitat()}
        />
      </div>
    </div>
  );
}
