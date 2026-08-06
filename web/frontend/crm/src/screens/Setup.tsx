import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";

import { Icon } from "../components/Icon";
import { ScreenLoading } from "../components/ui";
import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { useFailure } from "../lib/failure";
// Подписи блоков берём у экрана настроек: вторая копия той же карты разъехалась
// бы с первой молча.
import { LABEL as MODULE_LABEL } from "./SettingsModules";

/** Один вопрос при первом входе: чем вы занимаетесь?
 *
 * Человек, только что поставивший CRM, не знает, что ему нужно: он видит
 * десяток переключателей с незнакомыми словами и закрывает вкладку. Ответ
 * включает набор разделов, ставит воронку и подбирает слово для основной
 * записи — всё три сразу.
 *
 * **Набор ничего не выключает.** Разделы вне набора остаются как есть: забрать
 * раздел с данными одним нажатием — самый быстрый способ потерять доверие.
 *
 * Состав приходит с сервера. Список ключей, живущий в этом файле, разошёлся бы
 * с реестром блоков на первом же новом разделе, и заметить это было бы некому.
 */

interface Preset {
  key: string;
  modules: string[];
  /** Чего ещё нет — то и появится. Считает сервер: он знает зависимости. */
  will_enable: string[];
  pipeline: string;
  deal_term: string;
}

/** Подписи наборов и разделов. Ключи приходят с сервера, слова живут здесь —
 *  как у блоков в настройках. Незнакомый ключ показываем как есть: экран, не
 *  знающий нового набора, не должен падать. */
const PRESET_LABEL: Record<string, string> = {
  services: "presetServices",
  retail: "presetRetail",
  wholesale: "presetWholesale",
  production: "presetProduction",
  agency: "presetAgency",
};

const PRESET_ABOUT: Record<string, string> = {
  services: "presetServicesAbout",
  retail: "presetRetailAbout",
  wholesale: "presetWholesaleAbout",
  production: "presetProductionAbout",
  agency: "presetAgencyAbout",
};

export function Setup() {
  const { t, refreshModules, toastError } = useApp();
  const navigate = useNavigate();
  const [items, setItems] = useState<Preset[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const { failure, fail, clear } = useFailure();

  useEffect(() => {
    clear();
    api
      .get<{ items: Preset[] }>("/modules/presets")
      .then((data) => setItems(data.items))
      .catch(fail);
  }, [fail, clear]);

  if (!items) return <ScreenLoading error={failure} onRetry={() => window.location.reload()} />;

  const apply = async (key: string) => {
    setBusy(key);
    try {
      await api.post("/modules/presets", { key });
      // Меню строится по составу блоков: не обновив его, человек увидит старый
      // список разделов и решит, что ничего не произошло.
      await refreshModules();
      navigate("/");
    } catch (err) {
      toastError(err);
      setBusy(null);
    }
  };

  return (
    <div className="page" style={{ maxWidth: 900, margin: "0 auto" }}>
      <div style={{ textAlign: "center", marginBottom: 8 }}>
        <h1 className="page-title" style={{ fontSize: 26 }}>{t("setupTitle")}</h1>
      </div>
      <div className="field-desc" style={{ textAlign: "center", marginBottom: 28 }}>
        {t("setupHint")}
      </div>

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(240px, 1fr))",
          gap: 14,
        }}
      >
        {items.map((preset) => (
          <button
            key={preset.key}
            className="card card-pad"
            style={{
              textAlign: "left", cursor: "pointer", border: "1px solid var(--border)",
              display: "flex", flexDirection: "column", gap: 8, minHeight: 150,
            }}
            disabled={busy !== null}
            onClick={() => void apply(preset.key)}
          >
            <div style={{ color: "var(--text)", fontSize: 15, fontWeight: 600 }}>
              {t((PRESET_LABEL[preset.key] ?? "setupTitle") as never)}
            </div>
            <div style={{ color: "var(--muted)", fontSize: 12.5, lineHeight: 1.5 }}>
              {t((PRESET_ABOUT[preset.key] ?? "setupHint") as never)}
            </div>
            {/* Что появится — списком и человеческими словами. Без него выбор
                выглядит как «нажму и что-то произойдёт». */}
            <div style={{ marginTop: "auto", color: "var(--faint)", fontSize: 12 }}>
              {t("setupWillEnable")}:{" "}
              {(preset.will_enable.length ? preset.will_enable : preset.modules)
                .map((key) => t((MODULE_LABEL[key] ?? key) as never))
                .join(", ")}
            </div>
            {busy === preset.key && (
              <div style={{ color: "var(--accent)", fontSize: 12 }}>{t("loading")}</div>
            )}
          </button>
        ))}
      </div>

      <div className="field-desc" style={{ textAlign: "center", marginTop: 24 }}>
        {t("setupNothingOff")}
      </div>

      <div style={{ textAlign: "center", marginTop: 16 }}>
        <button className="btn btn-secondary" onClick={() => navigate("/")}>
          {t("setupSkip")}
        </button>
      </div>
    </div>
  );
}
