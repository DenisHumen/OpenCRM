import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { Chip } from "./ui";
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
export function OrdersOfCard({ clientId, dealId }: { clientId?: number; dealId?: number }) {
  const { t, locale, workspace, user, modules } = useApp();
  const [items, setItems] = useState<Order[] | null>(null);

  const visible = moduleOn(modules, "orders") && can(user, "orders.view");

  useEffect(() => {
    if (!visible || (!clientId && !dealId)) return;
    const params = new URLSearchParams({ per_page: "20" });
    if (dealId) params.set("deal_id", String(dealId));
    else if (clientId) params.set("client_id", String(clientId));
    let alive = true;
    api
      .get<{ items: Order[] }>(`/orders?${params}`)
      .then((data) => {
        if (alive) setItems(data.items);
      })
      // Блок выключили, пока карточка была открыта, — врезка просто исчезает,
      // ровно как исчезла бы при перезагрузке. Это не ошибка человека.
      .catch(() => {
        if (alive) setItems(null);
      });
    return () => {
      alive = false;
    };
  }, [visible, clientId, dealId]);

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
      </div>
    </div>
  );
}
