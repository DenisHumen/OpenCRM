import { useEffect, useState } from "react";

import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { useDebounced } from "../lib/debounce";
import { formatMoney, formatQuantity } from "../lib/format";
import { useGuard } from "../lib/guard";
import { moduleOn } from "../lib/modules";
import { can } from "../lib/permissions";
import { Icon } from "./Icon";
import { WarehousePicker, useWarehouses } from "./Warehouses";

type Stroka = {
  id: number;
  product_id: number | null;
  name: string;
  quantity_milli: number;
  price_minor: number | null;
  total_minor: number | null;
  shortage_milli: number;
  kind: "product" | "extra";
};

type Tovar = {
  id: number;
  name: string;
  sku: string;
  price: number | null;
  // Услуге склад не положен, и без этого поля отличить её было нечем:
  // выбрал склад — и услуги перестали добавляться в заявку вовсе.
  is_service: boolean;
};

/**
 * Состав заявки: что продаём и во сколько это встаёт клиенту.
 *
 * Одно поле на товар и на свою трату. Набрали название товара — выбрали из
 * подсказки, и строка встала с ценой из прайса; набрали «упаковка» и никого не
 * выбрали — та же строка стала своей тратой. Два отдельных поля заставляли бы
 * решать, каким пользоваться, ещё до того, как человек начал печатать.
 *
 * Итог считает сервер и отдаёт вместе со списком: складывать видимые строки на
 * фронте нельзя — их может быть больше, чем показано.
 */
export function DealLines({
  dealId,
  closed,
  onOrder,
  onSostav,
}: {
  dealId: number;
  closed: boolean;
  /** Заказ заведён — карточке надо перечитать свои врезки. */
  onOrder?: () => void;
  /** Сколько строк набрано и на какую сумму. Карточке нужно и то, и другое:
   *  строки запирают поле суммы (со строками у неё единственный писатель, §Р5),
   *  а итог говорит, что показанная сумма устарела. Счёт идёт отсюда, а не
   *  отдельным запросом: список уже загружен. */
  onSostav?: (skolko: number, itog: number | null) => void;
}) {
  const { t, locale, modules, user, workspace, toastError } = useApp();
  const guard = useGuard();
  const [stroki, setStroki] = useState<Stroka[] | null>(null);
  const [itog, setItog] = useState<number | null>(null);
  const [pribyl, setPribyl] = useState<number | null>(null);

  const [poisk, setPoisk] = useState("");
  const [vybran, setVybran] = useState<Tovar | null>(null);
  const [kolichestvo, setKolichestvo] = useState("1");
  const [tsena, setTsena] = useState("");
  const [podskazki, setPodskazki] = useState<Tovar[]>([]);
  const [skan, setSkan] = useState("");
  // Выбор склада появляется САМ, когда складов больше одного:
  // `WarehousePicker` отдаёт null при одном, и правило живёт в одном месте.
  const places = useWarehouses();
  const [sklad, setSklad] = useState<number | null>(null);
  const zapros = useDebounced(poisk);

  const vklyuchen = moduleOn(modules, "warehouse");

  const zagruzit = async () => {
    try {
      const otvet = await api.get<{
        items: Stroka[];
        total_minor: number | null;
        profit_minor: number | null;
      }>(`/deals/${dealId}/lines`);
      setStroki(otvet.items);
      setItog(otvet.total_minor);
      setPribyl(otvet.profit_minor);
      onSostav?.(otvet.items.length, otvet.total_minor);
    } catch (beda) {
      toastError(beda);
    }
  };

  useEffect(() => {
    if (!vklyuchen) return;
    void zagruzit();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dealId, vklyuchen]);

  // Подсказка ищет ТОЛЬКО пока никого не выбрали: после выбора строка поиска
  // показывает выбранный товар, и продолжать искать по ней значит мигать
  // списком под уже принятым решением.
  useEffect(() => {
    if (!vklyuchen || vybran || zapros.trim().length < 2) {
      setPodskazki([]);
      return;
    }
    let alive = true;
    api
      .get<{ items: Tovar[] }>(`/warehouse/products?search=${encodeURIComponent(zapros)}&per_page=8`)
      .then((otvet) => alive && setPodskazki(otvet.items))
      .catch(() => alive && setPodskazki([]));
    return () => {
      alive = false;
    };
  }, [zapros, vybran, vklyuchen]);

  if (!vklyuchen || stroki === null) return null;

  const vzyat = (tovar: Tovar) => {
    setVybran(tovar);
    setPoisk(tovar.name);
    setPodskazki([]);
    if (tovar.price !== null) setTsena(String(tovar.price / 100));
  };

  const sbrosit = () => {
    setPoisk("");
    setVybran(null);
    setKolichestvo("1");
    setTsena("");
    setPodskazki([]);
  };

  const dobavit = async () => {
    if (!guard.take()) return;
    try {
      // Цена приходит с сервера и уходит на сервер В МИНОРНЫХ единицах, а в поле
      // человек пишет рубли. Умножение здесь безопасно: у денег два знака, в
      // отличие от количества, где `Math.round(0.3335 * 1000)` даёт 334.
      const kopeyki = tsena.trim() ? Math.round(Number(tsena.replace(",", ".")) * 100) : null;
      await api.post(`/deals/${dealId}/lines`, {
        ...(vybran ? { product_id: vybran.id } : { name: poisk.trim() }),
        quantity: kolichestvo,
        ...(kopeyki === null || Number.isNaN(kopeyki) ? {} : { price: kopeyki }),
        // Склад — только у ТОВАРНОЙ строки: упаковку не берут с полки, а у
        // услуги остатка нет и сервер отвечает `line_has_no_warehouse`. Тот же
        // случай разобран у сканера (`test_skan_uslugi_ne_upiraetsya_v_sklad`).
        ...(vybran && !vybran.is_service && sklad !== null
          ? { warehouse_id: sklad }
          : {}),
      });
      sbrosit();
      await zagruzit();
    } catch (beda) {
      toastError(beda);
    } finally {
      guard.free();
    }
  };

  const ubrat = async (lineId: number) => {
    if (!guard.take()) return;
    try {
      await api.del(`/deals/${dealId}/lines/${lineId}`);
      await zagruzit();
    } catch (beda) {
      toastError(beda);
    } finally {
      guard.free();
    }
  };

  // Скан отдельной строкой, а не тем же полем, что и поиск: у стойки коробка
  // уже в руках, и разбирать «это код или название» по виду набранного значит
  // однажды завести своей тратой строку «4600000000109».
  const skanirovat = async () => {
    const kod = skan.trim();
    if (!kod || !guard.take()) return;
    try {
      await api.post(`/deals/${dealId}/lines`, {
        code: kod,
        quantity: "1",
        ...(sklad !== null ? { warehouse_id: sklad } : {}),
      });
      setSkan("");
      await zagruzit();
    } catch (beda) {
      toastError(beda);
    } finally {
      guard.free();
    }
  };

  const sobrat = async () => {
    if (!guard.take()) return;
    try {
      await api.post(`/deals/${dealId}/order`, {});
      onOrder?.();
    } catch (beda) {
      toastError(beda);
    } finally {
      guard.free();
    }
  };

  // Кнопка появляется только когда есть что заказывать и есть чем: блок заказов
  // выключается, а право на заведение заказа спрашивается ЗАКАЗОВ, а не заявок.
  const mozhnoZakaz =
    !closed &&
    moduleOn(modules, "orders") &&
    can(user, "orders.create") &&
    stroki.some((s) => s.kind === "product");

  const currency = workspace.currency;

  return (
    <div className="card card-pad" style={{ marginBottom: 20 }}>
      <div className="page-head" style={{ marginBottom: 12 }}>
        <div className="metric-title">{t("dealLines")}</div>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          {itog !== null && (
            <div style={{ color: "var(--muted)", fontSize: 12.5 }}>
              {formatMoney(itog, currency, locale)}
            </div>
          )}
          {/* Прибыль приходит только когда себестоимость известна у ВСЕХ строк:
              неполная завысила бы её там, где решают о скидке. */}
          {pribyl !== null && (
            <div style={{ color: "var(--faint)", fontSize: 12 }}>
              {t("expectedProfit", { sum: formatMoney(pribyl, currency, locale) })}
            </div>
          )}
          {mozhnoZakaz && (
            <button className="btn btn-secondary btn-sm" disabled={guard.busy} onClick={() => void sobrat()}>
              {t("makeOrder")}
            </button>
          )}
        </div>
      </div>

      <div className="doc-mini-list">
        {stroki.map((s) => (
          <div key={s.id} className="doc-mini">
            <span className="truncate" style={{ flex: 1, minWidth: 0 }}>
              {s.name}
            </span>
            {/* Своя трата подписана: иначе «упаковка» в перечне выглядит как
                товар, которого на складе нет, и его начинают там искать. */}
            {s.kind === "extra" && (
              <span style={{ color: "var(--faint)", fontSize: 12 }}>{t("extraCost")}</span>
            )}
            <span style={{ color: "var(--muted)" }}>{formatQuantity(s.quantity_milli)}</span>
            {/* Нехватка КРАСИТСЯ, а не запрещается: продавать то, что ещё едет,
                обычное дело, и отказ сломал бы работу вместо помощи. */}
            {s.shortage_milli > 0 && (
              <span style={{ color: "var(--warning)", fontSize: 12 }}>
                {t("shortBy", { n: formatQuantity(s.shortage_milli) })}
              </span>
            )}
            <span style={{ color: "var(--faint)", width: 100, textAlign: "right" }}>
              {s.total_minor === null ? "—" : formatMoney(s.total_minor, currency, locale)}
            </span>
            {!closed && (
              <button
                className="btn btn-secondary btn-sm"
                aria-label={t("delete")}
                disabled={guard.busy}
                onClick={() => void ubrat(s.id)}
              >
                <Icon name="trash" size={14} />
              </button>
            )}
          </div>
        ))}
      </div>

      {!closed && (
        <div className="scan-box" style={{ marginTop: 12 }}>
          <Icon name="scan" size={18} className="scan-icon" />
          <input
            className="scan-input"
            placeholder={t("scanPlaceholder")}
            value={skan}
            disabled={guard.busy}
            onChange={(e) => setSkan(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") void skanirovat();
            }}
          />
          <span className="scan-hint">{t("scanHint")}</span>
        </div>
      )}

      {!closed && (
        <div style={{ display: "flex", gap: 8, marginTop: 12, position: "relative" }}>
          <div style={{ flex: 1, minWidth: 0, position: "relative" }}>
            <input
              className="input"
              value={poisk}
              placeholder={t("dealLineHint")}
              onChange={(e) => {
                setPoisk(e.target.value);
                setVybran(null);
              }}
            />
          </div>
          {/* Список во всю строку, а не под одним полем: с тремя колонками (имя, артикул,
              цена) в ширину поля названия он не помещался; у прежнего к тому же не было
              ни фона, ни рамки — строки палитры Ctrl+K ложились прямо на текст. */}
          {podskazki.length > 0 && (
            <div
              className="card"
              style={{ position: "absolute", top: "100%", left: 0, right: 0, zIndex: 20, marginTop: 4, overflow: "hidden" }}
            >
              {podskazki.map((tovar) => (
                <button
                  key={tovar.id}
                  type="button"
                  className="list-row hoverable"
                  style={{ width: "100%", textAlign: "left", background: "none", border: "none", cursor: "pointer" }}
                  onClick={() => vzyat(tovar)}
                >
                  <span style={{ flex: 1, color: "var(--text)", fontSize: 13 }}>{tovar.name}</span>
                  <span style={{ color: "var(--faint)", fontSize: 12 }}>{tovar.sku ?? ""}</span>
                  <span style={{ color: "var(--muted)", fontSize: 12.5, minWidth: 70, textAlign: "right" }}>
                    {formatMoney(tovar.price, workspace.currency, locale)}
                  </span>
                </button>
              ))}
            </div>
          )}
          <input
            className="input"
            style={{ width: 90 }}
            value={kolichestvo}
            placeholder={t("quantity")}
            onChange={(e) => setKolichestvo(e.target.value)}
          />
          <input
            className="input"
            style={{ width: 110 }}
            value={tsena}
            placeholder={t("price")}
            onChange={(e) => setTsena(e.target.value)}
          />
          <WarehousePicker
            places={places}
            value={sklad ?? places?.items[0]?.id ?? null}
            onChange={setSklad}
            inline
          />
          <button
            className="btn btn-primary"
            disabled={guard.busy || !poisk.trim()}
            onClick={() => void dobavit()}
          >
            <Icon name="plus" stroke={2} />
            {t("add")}
          </button>
        </div>
      )}
    </div>
  );
}
