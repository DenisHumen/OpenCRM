import { useCallback, useEffect, useState } from "react";

import { Toggle } from "../components/ui";
import { api } from "../lib/api";
import { useApp } from "../lib/app";
import type { TranslationKey } from "../lib/i18n";
import { useSettings } from "./Settings";

/**
 * Настройки наклейки: размер рулона и состав полей.
 *
 * ПОЧЕМУ ЭКРАН ПОЯВИЛСЯ. Настройки наклейки существовали с самого начала —
 * `label_width_mm`, `label_show_price` и родня лежат в `SETTING_DEFAULTS`, — но
 * править их было нечем: ни одного экрана, ни одной формы. То есть подогнать
 * размер под свой рулон или включить цену человек не мог никак, кроме как
 * запросом в API руками. Половина «наклейка выглядит бедно» была именно в этом:
 * дело не только в составе полей, но и в том, что состав нечем задать.
 *
 * СПИСОК ПОЛЕЙ ПРИХОДИТ С СЕРВЕРА, а не написан здесь. Он задан реестром
 * (`core/services/barcode_service.POLYA_NAKLEYKI`), и в этом весь смысл: какие
 * поля есть, в каком порядке и в какой зоне — решает одно место.
 */
interface PoleNakleyki {
  key: string;
  /** Где поле встанет на наклейке: verh | stroka | niz | bok. */
  zone: string;
  on: boolean;
  /** Имя настройки — им же и сохраняем. Склеивать приставку здесь незачем. */
  setting: string;
}

interface LabelConfig {
  width_mm: number;
  height_mm: number;
  min_mm: number;
  max_mm: number;
  fields: PoleNakleyki[];
}

/**
 * Подписи полей: название и пояснение.
 *
 * ПЕРЕЧИСЛЕНЫ БУКВАМИ НАМЕРЕННО, хотя список полей и приходит с сервера.
 * Соблазн собрать ключ шаблоном из имени поля велик и стоил бы одной строки —
 * но такой ключ делает слепой проверку мёртвых переводов
 * (`tests/test_screens.py`): она ищет ключи по кавычкам и о собранных не знает.
 * Отключить её ради удобства значит однажды перевести на второй язык слова,
 * которых нигде нет, — двадцать таких она нашла при первом же запуске.
 *
 * Цена решения названа честно: новое поле стоит записи в реестре, двух строк
 * перевода и одной строки здесь. Забыть последнюю нельзя — за парой
 * «реестр ↔ подписи» следит `tests/test_labels.py`, и краснеет он в обе
 * стороны: и на поле без подписи, и на подписи без поля.
 */
const POLE_PODPIS: Record<string, readonly [TranslationKey, TranslationKey]> = {
  name: ["labelField_name", "labelFieldHint_name"],
  note: ["labelField_note", "labelFieldHint_note"],
  sku: ["labelField_sku", "labelFieldHint_sku"],
  unit: ["labelField_unit", "labelFieldHint_unit"],
  pack: ["labelField_pack", "labelFieldHint_pack"],
  min_stock: ["labelField_min_stock", "labelFieldHint_min_stock"],
  price: ["labelField_price", "labelFieldHint_price"],
  company: ["labelField_company", "labelFieldHint_company"],
  printed_at: ["labelField_printed_at", "labelFieldHint_printed_at"],
  qr: ["labelField_qr", "labelFieldHint_qr"],
};

/** Порядок зон на наклейке сверху вниз. Группы подписаны, чтобы человек видел,
 *  куда именно ляжет поле, а не гадал по названию. */
const ZONY: readonly (readonly [string, TranslationKey])[] = [
  ["verh", "labelZone_verh"],
  ["stroka", "labelZone_stroka"],
  ["niz", "labelZone_niz"],
  ["bok", "labelZone_bok"],
];

export function SettingsLabels() {
  const { t, toastError } = useApp();
  const { values, patch } = useSettings();
  const [config, setConfig] = useState<LabelConfig | null>(null);

  const load = useCallback(() => {
    api.get("/labels/settings").then(setConfig).catch(toastError);
  }, [toastError]);

  useEffect(load, [load]);

  // Булевы настройки хранятся строкой "1"/"0" — `SETTING_DEFAULTS` у сервера
  // строковый. Значения ещё нет в базе (поле добавлено реестром позже) —
  // берём то, что сказал сервер: он знает умолчание реестра, а мы нет.
  const vklyucheno = (pole: PoleNakleyki) =>
    values[pole.setting] === undefined ? pole.on : values[pole.setting] === "1";

  const pereklyuchit = (pole: PoleNakleyki) =>
    patch({ [pole.setting]: vklyucheno(pole) ? "0" : "1" });

  const razmer = (key: "label_width_mm" | "label_height_mm") => (
    <div>
      <label className="label">{t(key === "label_width_mm" ? "labelWidth" : "labelHeight")}</label>
      <input
        className="input"
        type="number"
        min={config?.min_mm}
        max={config?.max_mm}
        value={values[key] ?? ""}
        onChange={(e) => patch({ [key]: e.target.value })}
      />
    </div>
  );

  return (
    <div className="card" style={{ padding: "20px 22px" }}>
      <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 4 }}>{t("labelSettings")}</div>
      <div style={{ color: "var(--faint)", fontSize: 12.5, marginBottom: 18 }}>{t("labelSettingsSub")}</div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14, maxWidth: 320 }}>
        {razmer("label_width_mm")}
        {razmer("label_height_mm")}
      </div>
      <div className="field-desc" style={{ marginTop: 8 }}>{t("labelSizeDesc")}</div>

      <div style={{ borderTop: "1px solid var(--border)", margin: "20px 0 16px" }} />

      <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 4 }}>{t("labelFields")}</div>
      <div style={{ color: "var(--faint)", fontSize: 11.5, marginBottom: 16, lineHeight: 1.5 }}>
        {t("labelFieldsDesc")}
      </div>

      {config &&
        ZONY.map(([zona, podpis]) => {
          const polya = config.fields.filter((p) => p.zone === zona);
          if (!polya.length) return null;
          return (
            <div key={zona} style={{ marginBottom: 18 }}>
              <div style={{ fontSize: 11.5, color: "var(--faint)", marginBottom: 8 }}>{t(podpis)}</div>
              <div style={{ display: "grid", gap: 12 }}>
                {polya.map((pole) => {
                  // Поле у сервера есть, а подписи к нему нет — показываем сам
                  // ключ и не роняем экран. Такого быть не должно (за парой
                  // следит сторож), но настройки чинят с открытым экраном, и
                  // белая страница вместо формы была бы худшим из исходов.
                  const podpisi = POLE_PODPIS[pole.key];
                  const nazvanie = podpisi ? t(podpisi[0]) : pole.key;
                  return (
                    <div
                      key={pole.key}
                      style={{ display: "flex", alignItems: "flex-start", gap: 11, cursor: "pointer" }}
                      onClick={() => pereklyuchit(pole)}
                    >
                      {/* Обёртка расширяет область нажатия мышью на подпись; у
                          самого переключателя обработчик свой — иначе с
                          клавиатуры он мёртв. */}
                      <Toggle on={vklyucheno(pole)} label={nazvanie} onToggle={() => pereklyuchit(pole)} />
                      <div style={{ marginTop: -2 }}>
                        <div style={{ fontSize: 13, fontWeight: 500 }}>{nazvanie}</div>
                        <div style={{ color: "var(--faint)", fontSize: 11.5, marginTop: 2, lineHeight: 1.5 }}>
                          {podpisi ? t(podpisi[1]) : ""}
                        </div>
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
          );
        })}
    </div>
  );
}
