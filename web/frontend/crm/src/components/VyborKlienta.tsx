import { useEffect, useRef, useState } from "react";

import { useApp } from "../lib/app";
import { api } from "../lib/api";
import { useDebounced } from "../lib/debounce";

type Najden = { id: number; name: string };

/** Выбор клиента поиском, а не списком.
 *
 * Прежде здесь стоял обычный `<select>`, набитый первыми двумя сотнями
 * карточек. С двести первым клиентом это ломалось дважды, и вторая поломка
 * хуже первой: выбрать его было нельзя — и, если сделка УЖЕ была на него
 * заведена, `<select>` не находил среди своих вариантов текущего значения и
 * показывал первый попавшийся. То есть поле уверенно называло чужое имя.
 *
 * Поэтому имя показанного клиента берётся не из списка, а приходит снаружи
 * (`imya`): карточка знает его и без справочника. Список же нужен ровно на
 * время выбора и приезжает по мере набора.
 *
 * Пустая строка запроса тоже спрашивает сервер — первую страницу. У кого
 * клиентов дюжина, тем печатать незачем: щёлкнул и выбрал, как в прежнем
 * списке.
 */
export function VyborKlienta({
  value,
  imya,
  onPick,
  pustoy,
  pustoyPodpis,
  netVovse,
}: {
  value: number | null;
  imya: string | null;
  onPick: (id: number | null, imya: string | null) => void;
  /** Можно ли «без клиента». Бланк без клиента бывает, сделка — нет. */
  pustoy?: boolean;
  /** Как назвать «без клиента». По умолчанию прочерк. */
  pustoyPodpis?: string;
  /** Что сказать, когда клиентов нет вовсе. Пустой поиск и пустая база —
   * разные вещи: первое просят уточнить, во втором уточнять нечего, и
   * человеку нужно не «ничего не найдено», а «заведите клиента». */
  netVovse?: string;
}) {
  const { t, toastError } = useApp();
  const [otkryt, setOtkryt] = useState(false);
  const [stroka, setStroka] = useState("");
  const [najdeno, setNajdeno] = useState<Najden[]>([]);
  const [podsvechen, setPodsvechen] = useState(0);
  const gnezdo = useRef<HTMLDivElement | null>(null);

  const zapros = useDebounced(stroka);

  useEffect(() => {
    if (!otkryt) return;
    let alive = true;
    api
      .get<{ items: Najden[] }>(
        `/clients?search=${encodeURIComponent(zapros)}&per_page=8`,
      )
      .then((otvet) => {
        if (!alive) return;
        setNajdeno(otvet.items);
        setPodsvechen(0);
      })
      .catch((beda) => {
        if (alive) toastError(beda);
      });
    return () => {
      alive = false;
    };
  }, [zapros, otkryt, toastError]);

  // Щелчок мимо закрывает выпадашку. Без этого она оставалась висеть поверх
  // соседних полей и перехватывала нажатия по ним.
  useEffect(() => {
    if (!otkryt) return;
    const mimo = (e: MouseEvent) => {
      if (gnezdo.current && !gnezdo.current.contains(e.target as Node)) {
        setOtkryt(false);
      }
    };
    document.addEventListener("mousedown", mimo);
    return () => document.removeEventListener("mousedown", mimo);
  }, [otkryt]);

  const vybrat = (kto: Najden | null) => {
    onPick(kto ? kto.id : null, kto ? kto.name : null);
    setOtkryt(false);
    setStroka("");
  };

  const varianty: (Najden | null)[] = pustoy ? [null, ...najdeno] : najdeno;

  return (
    <div ref={gnezdo} style={{ position: "relative" }}>
      <input
        className="input"
        value={otkryt ? stroka : (imya ?? "")}
        placeholder={t("search")}
        onFocus={() => {
          setStroka("");
          setOtkryt(true);
        }}
        onChange={(e) => setStroka(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Escape") {
            setOtkryt(false);
            return;
          }
          if (!otkryt) return;
          if (e.key === "ArrowDown") {
            e.preventDefault();
            setPodsvechen((b) => Math.min(b + 1, varianty.length - 1));
          } else if (e.key === "ArrowUp") {
            e.preventDefault();
            setPodsvechen((b) => Math.max(b - 1, 0));
          } else if (e.key === "Enter") {
            // Гасим отправку формы: человек выбирает клиента, а не сохраняет
            // бланк. Без этого Enter по подсказке отправлял пустой бланк.
            e.preventDefault();
            if (varianty.length) vybrat(varianty[podsvechen] ?? null);
          }
        }}
      />
      {otkryt && (
        <div
          className="card"
          style={{
            position: "absolute",
            top: "100%",
            left: 0,
            right: 0,
            zIndex: 20,
            marginTop: 4,
            overflow: "hidden",
          }}
        >
          {varianty.length === 0 ? (
            <div className="page-sub" style={{ padding: "8px 12px" }}>
              {!zapros && netVovse ? netVovse : t("nothingFound", { q: zapros })}
            </div>
          ) : (
            varianty.map((kto, i) => (
              <button
                key={kto ? kto.id : "pusto"}
                type="button"
                className={"list-row hoverable" + (i === podsvechen ? " active" : "")}
                style={{
                  width: "100%",
                  textAlign: "left",
                  background: "none",
                  border: "none",
                  cursor: "pointer",
                }}
                onMouseEnter={() => setPodsvechen(i)}
                onClick={() => vybrat(kto)}
              >
                <span style={{ flex: 1, color: "var(--text)", fontSize: 13 }}>
                  {kto ? kto.name : (pustoyPodpis ?? "—")}
                </span>
                {kto && kto.id === value && (
                  <span style={{ color: "var(--faint)", fontSize: 12 }}>✓</span>
                )}
              </button>
            ))
          )}
        </div>
      )}
    </div>
  );
}
