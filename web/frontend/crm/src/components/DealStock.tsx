import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { Dochitat } from "./ui";
import { formatMoney, formatQuantity } from "../lib/format";
import { moduleOn } from "../lib/modules";
import { can } from "../lib/permissions";
import { KIND_LABEL, type StockMove } from "../screens/ProductCard";
import { unitKey } from "../screens/Warehouse";

/**
 * Врезка в карточке заявки: что списали со склада и во сколько это обошлось.
 *
 * Отдельным компонентом, а не куском карточки, потому что блоки выключаемые: у
 * студии без склада карточка заявки не должна ничего знать про товары. Пустая
 * врезка не рисуется вовсе — заявка без списаний должна выглядеть как заявка, а
 * не как заявка с пустой таблицей.
 *
 * Себестоимость считает сервер запросом, а не фронт сложением видимых строк:
 * строк может быть больше, чем помещается на страницу, и итог тихо занизился бы
 * ровно там, где на него и смотрят.
 */
/** По скольку списаний дочитывается врезка. */
const NA_STRANITSE = 100;

export function DealStock({ dealId }: { dealId: number }) {
  const { t, locale, user, modules, workspace, toastError } = useApp();
  const [data, setData] = useState<{
    items: StockMove[];
    total: number;
    cost: number;
    currency: string;
  } | null>(null);
  const [stranitsa, setStranitsa] = useState(1);
  const [dochityvaem, setDochityvaem] = useState(false);

  // Право на склад — вместе с блоком: без него врезка всё равно
  // получит отказ, а пустая рамка «Списано со склада» выглядит
  // как поломка, а не как запрет.
  const enabled = moduleOn(modules, "warehouse") && can(user, "warehouse.view");

  useEffect(() => {
    if (!enabled) return;
    let alive = true;
    api
      .get<{ items: StockMove[]; total: number; cost: number; currency: string }>(
        `/warehouse/moves?deal_id=${dealId}&page=1&per_page=${NA_STRANITSE}`,
      )
      .then((moves) => {
        if (!alive) return;
        setData(moves);
        setStranitsa(1);
      })
      .catch((beda) => {
        if (!alive) return;
        toastError(beda);
      });
    return () => {
      alive = false;
    };
  }, [dealId, enabled, toastError]);

  /** Дочитать врезку.
   *
   * Страницы дописываются: заявку с сотнями списаний рвать на страницы
   * незачем, а обрезать молча — тем более. Итог себестоимости считает сервер
   * по ВСЕМ строкам, поэтому от дочитки он не меняется.
   *
   * Отдельным действием, а не номером страницы в пути загрузки, и номер
   * растёт ПОСЛЕ удачного ответа: иначе отказ на второй странице оставил бы
   * счётчик на двойке, следующее нажатие попросило бы третью, и сотня
   * списаний пропала бы из перечня навсегда — при верном итоге сверху, а
   * значит незаметно. Отказ дочитки виден всплывающей жалобой, потому что
   * список уже на экране и молчание выглядело бы как сломанная кнопка.
   */
  const dochitat = async () => {
    if (dochityvaem) return;
    setDochityvaem(true);
    try {
      const dalshe = await api.get<{
        items: StockMove[];
        total: number;
        cost: number;
        currency: string;
      }>(`/warehouse/moves?deal_id=${dealId}&page=${stranitsa + 1}&per_page=${NA_STRANITSE}`);
      setData((bylo) =>
        bylo ? { ...dalshe, items: [...bylo.items, ...dalshe.items] } : dalshe,
      );
      setStranitsa((bylo) => bylo + 1);
    } catch (beda) {
      toastError(beda);
    } finally {
      setDochityvaem(false);
    }
  };

  if (!enabled || !data || data.items.length === 0) return null;

  const currency = data.currency || workspace.currency;

  return (
    <div className="card card-pad" style={{ marginBottom: 20 }}>
      <div className="page-head" style={{ marginBottom: 12 }}>
        <div className="metric-title">{t("writtenOff")}</div>
        <div style={{ color: "var(--muted)", fontSize: 12.5 }}>
          {t("writtenOffCost", { sum: formatMoney(data.cost, currency, locale) })}
        </div>
      </div>
      <div className="doc-mini-list">
        {data.items.map((move) => (
          <Link key={move.id} to={`/warehouse/${move.product_id}`} className="doc-mini">
            <span className="truncate" style={{ flex: 1, minWidth: 0 }}>
              {move.product_name ?? `#${move.product_id}`}
            </span>
            <span style={{ color: "var(--faint)", fontSize: 12 }}>
              {t(KIND_LABEL[move.kind] ?? "moveOut")}
            </span>
            <span style={{ color: "var(--muted)" }}>
              {/* Модуль количества: в карточке заявки «списали 4 м», а знак
                  минус тут ничего не добавляет — направление и так известно. */}
              {formatQuantity(Math.abs(move.quantity_milli))}
              {move.unit ? " " + t(unitKey(move.unit)) : ""}
            </span>
            <span style={{ color: "var(--faint)", width: 90, textAlign: "right" }}>
              {formatMoney(move.cost, currency, locale)}
            </span>
          </Link>
        ))}
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
