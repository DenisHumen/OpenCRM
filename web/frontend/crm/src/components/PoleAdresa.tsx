import { useEffect, useId, useRef, useState } from "react";

import { useApp } from "../lib/app";
import { MIN_DLINA_ADRESA, podskazki_adresa, type VariantAdresa } from "../lib/adres";
import { useDebounced } from "../lib/debounce";

/**
 * Улица с подсказкой: набрал начало — выбрал строку, и адрес разложен по полям.
 *
 * Правится на месте, как соседние поля карточки. Подсказок может не быть вовсе
 * (ручки нет, сеть не ответила) — тогда это обычное поле ввода, и никакого
 * разговора об этом с человеком не ведётся.
 */
export function PoleAdresa({
  label,
  value,
  clientId,
  onSave,
  onPick,
}: {
  label: string;
  value: string;
  /** Чей адрес: подсказки ищутся вокруг точки этого клиента. */
  clientId: number;
  /** Набранное руками — тем же путём, что у соседних полей. */
  onSave: (next: string) => void;
  /** Выбранный вариант: страна, город, индекс, улица и точка разом. */
  onPick: (variant: VariantAdresa) => void;
}) {
  const { t } = useApp();
  const [pravim, setPravim] = useState(false);
  const [chernovik, setChernovik] = useState(value);
  const [varianty, setVarianty] = useState<VariantAdresa[]>([]);
  const [podsvechen, setPodsvechen] = useState(-1);
  // Выбранное показываем сразу, не дожидаясь ответа на правку: иначе на
  // медленной сети нажатие выглядит как «ничего не произошло».
  const [vybrannoe, setVybrannoe] = useState<string | null>(null);
  const yacheyka = useRef<HTMLDivElement>(null);
  const opoznanie = useId();
  const spisok = `${opoznanie}-spisok`;
  const zapros = useDebounced(chernovik);

  const pokazat = vybrannoe ?? value;

  // Внешнее обновление не затирает набранное: пока поле правят, черновик
  // остаётся, новое значение придёт после сохранения.
  useEffect(() => {
    if (!pravim) setChernovik(value);
    setVybrannoe(null);
  }, [value, pravim]);

  useEffect(() => {
    // Уже сохранённый адрес переспрашивать незачем: список открылся бы сам,
    // стоило встать в поле, и закрыл бы собой соседние.
    if (!pravim || zapros.trim() === value.trim() || zapros.trim().length < MIN_DLINA_ADRESA) {
      setVarianty([]);
      setPodsvechen(-1);
      return;
    }
    let alive = true;
    void podskazki_adresa(zapros, clientId).then((otvet) => {
      // Ответа не было — придержали, отдых, отказ: прежний список вернее
      // пустого, из которого человек заключит «такого адреса нет».
      if (!alive || otvet.net_otveta) return;
      setVarianty(otvet.varianty);
      setPodsvechen(otvet.varianty.length ? 0 : -1);
    });
    return () => {
      alive = false;
    };
  }, [zapros, pravim, value, clientId]);

  // Подсвеченное стрелками не должно уезжать за край: список выше своего окна
  // уже на четвёртом варианте.
  useEffect(() => {
    if (podsvechen < 0) return;
    document.getElementById(`${spisok}-${podsvechen}`)?.scrollIntoView({ block: "nearest" });
  }, [podsvechen, spisok]);

  const zakryt = () => {
    setVarianty([]);
    setPodsvechen(-1);
  };

  // Фокус возвращается на ячейку: без этого он падает на страницу, и Tab
  // начинает обход с начала документа.
  const vernut_fokus = () => yacheyka.current?.focus();

  const zakonchit = () => {
    setPravim(false);
    zakryt();
    if (chernovik !== value) onSave(chernovik);
  };

  const vybrat = (variant: VariantAdresa) => {
    setPravim(false);
    zakryt();
    setVybrannoe(variant.street);
    onPick(variant);
    vernut_fokus();
  };

  return (
    <div
      ref={yacheyka}
      className="contact-cell adres-pole"
      tabIndex={pravim ? -1 : 0}
      role={pravim ? undefined : "button"}
      onClick={() => !pravim && setPravim(true)}
      onKeyDown={(e) => {
        if (!pravim && (e.key === "Enter" || e.key === " ")) {
          e.preventDefault();
          setPravim(true);
        }
      }}
      style={{ cursor: pravim ? "auto" : "text" }}
    >
      <div className="contact-label">{label}</div>
      {pravim ? (
        <>
          <input
            className="contact-input"
            value={chernovik}
            autoFocus
            placeholder={t("addressSuggestHint")}
            role="combobox"
            aria-expanded={varianty.length > 0}
            // Закрытый список — это отсутствующий узел, и ссылаться на него
            // нечем: читалка пошла бы искать его по опознанию и не нашла.
            aria-controls={varianty.length ? spisok : undefined}
            aria-autocomplete="list"
            aria-activedescendant={podsvechen >= 0 ? `${spisok}-${podsvechen}` : undefined}
            onChange={(e) => setChernovik(e.target.value)}
            onBlur={zakonchit}
            onKeyDown={(e) => {
              if (e.key === "Escape") {
                if (varianty.length) {
                  zakryt();
                  return;
                }
                setChernovik(value);
                setPravim(false);
                vernut_fokus();
                return;
              }
              if (!varianty.length) {
                if (e.key === "Enter") (e.target as HTMLInputElement).blur();
                return;
              }
              if (e.key === "ArrowDown") {
                e.preventDefault();
                setPodsvechen((bylo) => Math.min(bylo + 1, varianty.length - 1));
              } else if (e.key === "ArrowUp") {
                e.preventDefault();
                setPodsvechen((bylo) => Math.max(bylo - 1, 0));
              } else if (e.key === "Enter") {
                // Гасим отправку формы: человек выбирает адрес, а не сохраняет
                // карточку.
                e.preventDefault();
                if (podsvechen >= 0) vybrat(varianty[podsvechen]);
                else (e.target as HTMLInputElement).blur();
              }
            }}
          />
          {varianty.length > 0 && (
            <ul
              className="card vsplyvashka adres-spisok"
              id={spisok}
              role="listbox"
              aria-label={t("addressSuggestions")}
            >
              {varianty.map((variant, i) => (
                <li
                  key={`${variant.label}:${i}`}
                  id={`${spisok}-${i}`}
                  role="option"
                  aria-selected={i === podsvechen}
                  className={"adres-variant" + (i === podsvechen ? " active" : "")}
                  // Нажатие мышью не должно уводить фокус из поля: иначе сперва
                  // сработает уход из поля и сохранит набранное, а выбор
                  // придёт вторым и второй же правкой.
                  onMouseDown={(e) => e.preventDefault()}
                  onMouseEnter={() => setPodsvechen(i)}
                  onClick={() => vybrat(variant)}
                >
                  <span className="adres-variant-stroka">{variant.label}</span>
                  {(variant.postcode || variant.city || variant.country_code) && (
                    <span className="adres-variant-mesto">
                      {[variant.postcode, variant.city, variant.country_code]
                        .filter(Boolean)
                        .join(" · ")}
                    </span>
                  )}
                </li>
              ))}
            </ul>
          )}
        </>
      ) : (
        <div className="contact-value">
          {pokazat || <span className="contact-add">+ {t("contactAdd")}</span>}
        </div>
      )}
    </div>
  );
}
