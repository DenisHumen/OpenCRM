import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { formatQuantity } from "../lib/format";
import { moduleOn } from "../lib/modules";

type Derzhatel = {
  kind: "deal" | "order";
  /** Пусто — заявка чужая: имени и ссылки у неё нет, только количество. */
  id: number | null;
  title: string | null;
  quantity_milli: number;
};

type Nalichie = {
  stock_milli: number;
  reserved_milli: number;
  expected_milli: number;
  available_milli: number;
  holders: Derzhatel[];
};

/**
 * «Доступно» и кто держит остальное.
 *
 * Ради этого списка бронь и делается видимой: число «доступно 2 из 5» без
 * ответа «а где остальные три» отправляет человека искать их по всем заявкам
 * руками — и он идёт спрашивать у соседа, а не смотреть на экран.
 *
 * Врезка не рисуется, когда держать нечего: пустая таблица «в брони: никто»
 * занимает место и ничего не сообщает.
 */
export function ProductHolders({ productId }: { productId: number }) {
  const { t, modules, toastError } = useApp();
  const [data, setData] = useState<Nalichie | null>(null);
  const vklyuchen = moduleOn(modules, "warehouse");

  useEffect(() => {
    if (!vklyuchen) return;
    let alive = true;
    api
      .get<Nalichie>(`/warehouse/products/${productId}/availability`)
      .then((otvet) => alive && setData(otvet))
      .catch((beda) => {
        if (alive) toastError(beda);
      });
    return () => {
      alive = false;
    };
  }, [productId, vklyuchen, toastError]);

  if (!vklyuchen || !data || data.reserved_milli === 0) return null;

  return (
    <div className="card card-pad" style={{ marginBottom: 20 }}>
      <div className="page-head" style={{ marginBottom: 12 }}>
        <div className="metric-title">{t("reservedBy")}</div>
        <div style={{ color: "var(--muted)", fontSize: 12.5 }}>
          {t("availableOf", {
            available: formatQuantity(data.available_milli),
            stock: formatQuantity(data.stock_milli),
          })}
        </div>
      </div>
      <div className="doc-mini-list">
        {data.holders.map((d) => {
          const soderzhimoe = (
            <>
              <span className="truncate" style={{ flex: 1, minWidth: 0 }}>
                {d.title ?? t("heldByOthers")}
              </span>
              <span style={{ color: "var(--faint)", fontSize: 12 }}>
                {t(d.kind === "deal" ? "deal" : "order")}
              </span>
              <span style={{ color: "var(--muted)", width: 80, textAlign: "right" }}>
                {formatQuantity(d.quantity_milli)}
              </span>
            </>
          );
          // Чужая заявка приходит без имени и без номера: ссылке вести некуда,
          // и строка остаётся строкой. Количество при этом на месте — иначе
          // «в брони 5» стояло бы рядом с пустым списком держателей.
          return d.id === null ? (
            <div key={`${d.kind}-chuzhie`} className="doc-mini">
              {soderzhimoe}
            </div>
          ) : (
            <Link
              key={`${d.kind}-${d.id}`}
              to={d.kind === "deal" ? `/deals/${d.id}` : `/orders/${d.id}`}
              className="doc-mini"
            >
              {soderzhimoe}
            </Link>
          );
        })}
      </div>
    </div>
  );
}
