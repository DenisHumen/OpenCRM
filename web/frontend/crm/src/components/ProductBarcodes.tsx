import { useCallback, useEffect, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";

import { Icon } from "./Icon";
import { Chip } from "./ui";
import { api, ApiError } from "../lib/api";
import { useApp } from "../lib/app";
import { moduleOn } from "../lib/modules";
import { can } from "../lib/permissions";

/** Штрихкоды одного товара — весь интерфейс блока «наклейки» в карточке.
 *
 * Отдельным файлом, а не куском ProductCard, намеренно. Правило блоков в этом
 * проекте — «выключается без вреда для остальных», и держится оно тем, что у
 * блока есть граница. Пока разметка кодов размазана по карточке товара,
 * выключить блок значит пройти по ней с ножницами; собранная в один файл, она
 * выключается одним условием на вызове.
 *
 * Возвращаем null, когда блок выключен или права нет: раздела просто не
 * существует, а не «есть, но пустой».
 */

/** Виден ли интерфейс наклеек этому человеку.
 *
 * Правило одно — «блок включён и право есть», — а спрашивают его три места:
 * раздел кодов в карточке, поле сканера и экран склада, который из-за сканера
 * не забирает фокус себе. Три копии одного условия расходятся молча, и первым
 * расходится то, о котором забыли. */
export function useLabelsOn(): boolean {
  const { user, modules } = useApp();
  return moduleOn(modules, "labels") && can(user, "labels.view");
}

interface Barcode {
  id: number;
  code: string;
  kind: string;
  /** Единиц в упаковке, в тысячных: штука — 1000, блок из десяти — 10000. */
  pack_size_milli: number;
  is_primary: boolean;
}

export function ProductBarcodes({ productId }: { productId: number }) {
  const { t, user, toast, toastError } = useApp();
  const [items, setItems] = useState<Barcode[] | null>(null);
  const [copies, setCopies] = useState("1");

  const enabled = useLabelsOn();

  const load = useCallback(async () => {
    try {
      const data = await api.get<{ items: Barcode[] }>(`/labels/products/${productId}/barcodes`);
      setItems(data.items);
    } catch (e) {
      // 403 — блок выключили, пока карточка была открыта. Не ошибка человека:
      // раздел просто исчезает, ровно как исчез бы при перезагрузке страницы.
      if (e instanceof ApiError && (e.status === 403 || e.status === 404)) {
        setItems(null);
        return;
      }
      toastError(e);
    }
  }, [productId, toastError]);

  useEffect(() => {
    if (enabled) void load();
  }, [enabled, load]);

  if (!enabled || items === null) return null;

  const change = can(user, "labels.create");
  const remove = can(user, "labels.delete");
  const printable = items.length > 0;

  return (
    <>
      <div className="section-head" style={{ marginTop: 28 }}>
        <h2 className="section-title">{t("barcodes")}</h2>
        {printable && (
          <div style={{ display: "flex", alignItems: "center", gap: 8, marginLeft: "auto" }}>
            <label style={{ color: "var(--faint)", fontSize: 12 }}>{t("barcodeCopies")}</label>
            <input
              className="input"
              style={{ width: 62, height: 30, textAlign: "center" }}
              value={copies}
              onChange={(e) => setCopies(e.target.value.replace(/\D/g, "").slice(0, 3))}
              inputMode="numeric"
            />
            {/* Обычная ссылка в новую вкладку, а не запрос из скрипта: печать —
                это window.print() на настоящей странице со своим @page, и
                выборкой такую страницу не открыть. */}
            <a
              className="btn btn-secondary btn-sm"
              href={`/api/v1/labels/print?product_id=${productId}&copies=${
                Math.min(Math.max(Number(copies) || 1, 1), 200)
              }`}
              target="_blank"
              rel="noreferrer"
            >
              <Icon name="printer" size={14} />
              {t("barcodePrint")}
            </a>
          </div>
        )}
      </div>
      <div className="field-desc" style={{ marginTop: -6, marginBottom: 12 }}>
        {t("barcodesAbout")}
      </div>

      <div className="card" style={{ overflow: "hidden" }}>
        {items.map((item) => (
          <div className="list-row" key={item.id} style={{ gap: 10 }}>
            <Icon name="scan" size={15} className="" />
            <span style={{ fontFamily: "ui-monospace, monospace", fontSize: 13.5, letterSpacing: "0.04em" }}>
              {item.code}
            </span>
            {item.kind === "internal" && <Chip>{t("barcodeKindInternal")}</Chip>}
            {/* Упаковку показываем только когда она не штука: «1 шт.» рядом с
                каждым кодом — это шум, который прячет те строки, где число
                действительно другое. */}
            {item.pack_size_milli !== 1000 && (
              <span style={{ color: "var(--muted)", fontSize: 12.5 }}>
                × {item.pack_size_milli / 1000}
              </span>
            )}
            <span style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 8 }}>
              {item.is_primary ? (
                <Chip variant="brand">{t("barcodePrimary")}</Chip>
              ) : (
                change && (
                  <button
                    className="btn btn-secondary btn-sm"
                    onClick={async () => {
                      try {
                        await api.post(
                          `/labels/products/${productId}/barcodes/${item.id}/primary`,
                        );
                        await load();
                      } catch (e) {
                        toastError(e);
                      }
                    }}
                  >
                    {t("barcodeMakePrimary")}
                  </button>
                )
              )}
              {remove && (
                <button
                  className="btn-icon"
                  title={t("delete")}
                  onClick={async () => {
                    try {
                      await api.del(`/labels/products/${productId}/barcodes/${item.id}`);
                      await load();
                    } catch (e) {
                      toastError(e);
                    }
                  }}
                >
                  <Icon name="trash" size={14} />
                </button>
              )}
            </span>
          </div>
        ))}
        {items.length === 0 && (
          <div className="list-row" style={{ color: "var(--faint)", fontSize: 12.5 }}>
            {t("barcodeNone")}
          </div>
        )}
      </div>

      {change && <AddBarcode productId={productId} onSaved={load} toast={toast} />}
    </>
  );
}

function AddBarcode({
  productId,
  onSaved,
  toast,
}: {
  productId: number;
  onSaved: () => Promise<void>;
  toast: (text: string) => void;
}) {
  const { t, toastError } = useApp();
  const [code, setCode] = useState("");
  const [pack, setPack] = useState("1");
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!code.trim() || busy) return;
    setBusy(true);
    try {
      await api.post(`/labels/products/${productId}/barcodes`, {
        code: code.trim(),
        // Упаковку человек вводит в штуках, база хранит в тысячных — как и
        // всякое количество на складе.
        pack_size_milli: Math.round((Number(pack.replace(",", ".")) || 1) * 1000),
      });
      setCode("");
      setPack("1");
      await onSaved();
    } catch (err) {
      toastError(err);
    } finally {
      setBusy(false);
    }
  };

  const issueOwn = async () => {
    setBusy(true);
    try {
      const created = await api.post<Barcode>(`/labels/products/${productId}/barcodes/internal`);
      toast(created.code);
      await onSaved();
    } catch (err) {
      toastError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <form
      className="card card-pad"
      style={{ marginTop: 12, display: "grid", gridTemplateColumns: "1fr 140px auto auto", gap: 12, alignItems: "end" }}
      onSubmit={submit}
    >
      <div className="field" style={{ marginBottom: 0 }}>
        <label className="label">{t("barcodeCode")}</label>
        {/* Поле принимает и сканер, и клавиатуру: сканер работает как
            клавиатура — «печатает» цифры и жмёт Enter, отчего форма и
            отправляется сама. Драйвера для этого не нужно никакого. */}
        <input
          className="input"
          value={code}
          onChange={(e) => setCode(e.target.value)}
          placeholder="4600000000000"
          autoComplete="off"
        />
      </div>
      <div className="field" style={{ marginBottom: 0 }}>
        <label className="label">{t("barcodePack")}</label>
        <input className="input" value={pack} onChange={(e) => setPack(e.target.value)} />
      </div>
      <button className="btn btn-primary" disabled={busy}>
        {t("barcodeAdd")}
      </button>
      <button
        type="button"
        className="btn btn-secondary"
        onClick={issueOwn}
        disabled={busy}
        title={t("barcodeIssueInternalHint")}
      >
        {t("barcodeIssueInternal")}
      </button>
      <div className="field-desc" style={{ gridColumn: "1 / -1", marginBottom: 0 }}>
        {t("barcodePackHint")}
      </div>
    </form>
  );
}

/** Поле сканера над списком склада.
 *
 * Сканер — это клавиатура: он «печатает» цифры и жмёт Enter, поэтому всё
 * устройство со стороны интерфейса сводится к одному полю ввода. Ни драйвера,
 * ни разрешения браузера, ни камеры.
 *
 * Фокус забираем себе, а не оставляем поиску, и это осознанный размен: блок
 * наклеек выключен по умолчанию, включает его тот, у кого сканер есть, — и на
 * экране склада он чаще сканирует, чем набирает название руками.
 */
export function BarcodeScanner() {
  const { t, toast, toastError } = useApp();
  const navigate = useNavigate();
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);

  const enabled = useLabelsOn();

  const lookup = async () => {
    const scanned = code.trim();
    if (!scanned || busy) return;
    setBusy(true);
    try {
      const product = await api.get<{ id: number }>(
        `/labels/scan/${encodeURIComponent(scanned)}`,
      );
      setCode("");
      navigate(`/warehouse/${product.id}`);
    } catch (err) {
      // Код в отказе называем сами: сервер отвечает «barcode_unknown», а
      // человеку после писка сканера нужно видеть, какой именно код не нашёлся.
      // Пустой ответ здесь читается как «сканер сломался».
      if (err instanceof ApiError && err.status === 404) {
        toast(t("barcodeUnknown", { code: scanned }), true);
      } else {
        toastError(err);
      }
      // Поле не чистим: код остался на глазах, его можно поправить руками —
      // затёртую наклейку сканер берёт через раз, и набирают её вручную.
      setBusy(false);
      return;
    }
    setBusy(false);
  };

  if (!enabled) return null;

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        void lookup();
      }}
      title={t("barcodeScanHint")}
      style={{
        display: "flex",
        alignItems: "center",
        gap: 8,
        padding: "0 12px",
        height: 36,
        // Не жёсткая ширина: на узком экране жёсткая выталкивала строку
        // инструментов за край, и страница начинала ездить вбок. Растём и
        // ужимаемся вместе с поиском, но не уже, чем помещается код.
        flex: "1 1 170px",
        minWidth: 150,
        maxWidth: 260,
        border: "1px solid var(--border)",
        borderRadius: 8,
        background: "var(--surface)",
      }}
    >
      <Icon name="scan" size={15} className="" />
      <input
        style={{
          flex: 1,
          minWidth: 0,
          background: "none",
          border: "none",
          outline: "none",
          color: "var(--text)",
          fontSize: 13.5,
          fontFamily: "ui-monospace, monospace",
        }}
        placeholder={t("barcodeScan")}
        value={code}
        onChange={(e) => setCode(e.target.value)}
        // Enter ловим сами, а не полагаемся на отправку формы «по умолчанию».
        // Проверено на живой странице: доверенный Enter долетает до поля, а
        // событие submit у формы без кнопки отправки НЕ возникает — и сканер,
        // который весь свой ввод заканчивает именно Enter, не сработал бы
        // вовсе. Молчаливый отказ ровно там, ради чего блок и заводился.
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            void lookup();
          }
        }}
        autoComplete="off"
        autoFocus
      />
    </form>
  );
}
