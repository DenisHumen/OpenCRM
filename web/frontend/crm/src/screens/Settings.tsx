import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { Outlet, useLocation, useOutletContext } from "react-router-dom";

import { StorageCard } from "../components/StorageCard";
import { ScreenLoading, Toggle } from "../components/ui";
import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { useFailure } from "../lib/failure";
import { formatDateTime } from "../lib/format";
import { moduleOn } from "../lib/modules";
import { DEAL_TERMS, term } from "../lib/terms";

const SWATCHES = ["#D97757", "#6C8EEF", "#4CAF6E", "#E8A23D"];

type Values = Record<string, string>;

/** Общее состояние секций: настройки грузятся и сохраняются один раз на всю группу. */
interface SettingsCtx {
  values: Values;
  patch: (next: Values) => void;
  set: (key: string) => (e: { target: { value: string } }) => void;
  input: (key: string, label: string, desc: string, placeholder?: string) => ReactNode;
  switcher: (key: string, label: string, hint: string) => ReactNode;
}

export function useSettings() {
  return useOutletContext<SettingsCtx>();
}

/**
 * Каркас «Настроек сайта»: заголовок, кнопка сохранения и общее состояние.
 *
 * Разделов уже пять, и их будет больше — одной страницей это давно не читается,
 * поэтому каждый живёт своим маршрутом, а в сайдбаре раскрывается списком.
 * Значения при этом общие: сохранение одно на всю группу, как и было.
 */
/** Разделы, у которых поля собираются в общую форму и уходят кнопкой «Сохранить».
 *  Остальные (копии, обслуживание) применяют каждое действие сразу, и кнопка
 *  над ними обещала бы сохранить то, что уже сохранено. */
const S_FORMOY = new Set(["brand", "labels", "contacts", "showcase", "return-button", "automation"]);

export function SettingsLayout() {
  const { t, refreshSettings, refreshWorkspace, toastError } = useApp();
  const { pathname } = useLocation();
  const [values, setValues] = useState<Values | null>(null);
  const [sohranyonnye, setSohranyonnye] = useState<Values | null>(null);
  const [saved, setSaved] = useState(false);
  const [busy, setBusy] = useState(false);

  const { failure, fail, clear } = useFailure();

  const load = useCallback(() => {
    clear();
    api.get("/settings").then((v: Values) => { setValues(v); setSohranyonnye(v); }).catch(fail);
  }, [fail, clear]);

  useEffect(load, [load]);

  if (!values) return <ScreenLoading error={failure} onRetry={load} />;

  const patch = (next: Values) => setValues((v) => ({ ...v!, ...next }));
  const set = (key: string) => (e: { target: { value: string } }) => patch({ [key]: e.target.value });

  const save = async () => {
    setBusy(true);
    try {
      const stalo: Values = await api.patch("/settings", { values });
      setValues(stalo);
      setSohranyonnye(stalo);
      await refreshSettings();
      // Название заявок и валюта живут не в настройках сайта, а в рабочем
      // пространстве: настройки читает только root, а подписи в меню и суммы
      // на карточках видят все. Обновляем именно ПОСЛЕ сохранения — прежняя
      // попытка стояла на изменении поля, то есть спрашивала сервер о том, что
      // ему ещё не отправили, и в меню оставалось старое слово до перезагрузки.
      await refreshWorkspace();
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    } catch (e) {
      toastError(e);
    } finally {
      setBusy(false);
    }
  };

  // у каждой настройки своя строка-пояснение: что это и где клиент это увидит —
  // экран открывают и те, кто систему не собирал
  const input = (key: string, label: string, desc: string, placeholder?: string) => (
    <div>
      <label className="label">{label}</label>
      <input className="input" value={values[key] ?? ""} onChange={set(key)} placeholder={placeholder} />
      <div className="field-desc">{desc}</div>
    </div>
  );

  // булевы настройки хранятся строкой "1"/"0" — SETTING_DEFAULTS у сервера строковый
  const switcher = (key: string, label: string, hint: string) => (
    <div
      style={{ display: "flex", alignItems: "flex-start", gap: 11, cursor: "pointer" }}
      onClick={() => patch({ [key]: values[key] === "1" ? "0" : "1" })}
    >
      {/* Обёртка расширяет область нажатия мышью на подпись; у самого
          переключателя обработчик свой — иначе с клавиатуры он мёртв. */}
      <Toggle
        on={values[key] === "1"}
        label={label}
        onToggle={() => patch({ [key]: values[key] === "1" ? "0" : "1" })}
      />
      <div style={{ marginTop: -2 }}>
        <div style={{ fontSize: 13, fontWeight: 500 }}>{label}</div>
        <div style={{ color: "var(--faint)", fontSize: 11.5, marginTop: 2, lineHeight: 1.5 }}>{hint}</div>
      </div>
    </div>
  );

  const razdel = pathname.split("/").pop() ?? "";
  const izmeneno = JSON.stringify(values) !== JSON.stringify(sohranyonnye);

  return (
    <div className="page page-narrow">
      <div className="page-head" style={{ alignItems: "flex-start", marginBottom: 26 }}>
        <div>
          <h1 className="page-title">{t("siteSettings")}</h1>
          <div className="page-sub">{t("settingsSub")}</div>
        </div>
        {S_FORMOY.has(razdel) && (
          <button
            className={"btn " + (saved ? "btn-success" : "btn-primary")}
            onClick={() => void save()}
            disabled={busy || (!izmeneno && !saved)}
          >
            {saved ? t("saved") : t("save")}
          </button>
        )}
      </div>
      <Outlet context={{ values, patch, set, input, switcher } satisfies SettingsCtx} />
    </div>
  );
}

export function SettingsBrand() {
  const { t, locale, refreshSettings, toastError } = useApp();
  const { values, patch, input } = useSettings();
  const logoInput = useRef<HTMLInputElement>(null);

  const uploadLogo = async (file: File | undefined) => {
    if (!file) return;
    try {
      const uploaded = await api.upload("/settings/logo", file);
      patch({ brand_logo_path: uploaded.brand_logo_path });
      await refreshSettings();
    } catch (e) {
      toastError(e);
    }
  };

  const removeLogo = async () => {
    try {
      await api.del("/settings/logo");
      patch({ brand_logo_path: "" });
      if (logoInput.current) logoInput.current.value = "";
      await refreshSettings();
    } catch (e) {
      toastError(e);
    }
  };

  return (
    <div className="card card-pad">
      <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 4 }}>{t("brand")}</div>
      <div style={{ color: "var(--faint)", fontSize: 12.5, marginBottom: 18 }}>{t("brandSub")}</div>
      <div style={{ display: "flex", gap: 20, alignItems: "flex-start" }}>
        <div>
          <label className="label" style={{ marginBottom: 6 }}>
            {t("logo")}
          </label>
          {/* На подложке витрины: логотип студии почти всегда светлый по
              прозрачному фону, и на светлой теме от него осталась бы пустая
              рамка. Заодно превью честнее — витрина тёмная у всех. */}
          <div
            className="dropzone media-plate"
            style={{ width: 96, height: 96, padding: 8, display: "grid", placeItems: "center", overflow: "hidden" }}
            onClick={() => logoInput.current?.click()}
          >
            {values.brand_logo_path ? (
              <img src={values.brand_logo_path} alt="" style={{ maxWidth: "100%", maxHeight: "100%" }} />
            ) : (
              <span style={{ fontSize: 11.5 }}>{t("dropLogo")}</span>
            )}
          </div>
          <input ref={logoInput} type="file" accept="image/*" hidden onChange={(e) => void uploadLogo(e.target.files?.[0])} />
          {values.brand_logo_path && (
            <button className="text-link danger" style={{ display: "block", marginTop: 8 }} onClick={() => void removeLogo()}>
              {t("removeImage")}
            </button>
          )}
          {/* шире самой дропзоны: в 96px текст рвался бы по два слова в строку */}
          <div className="field-desc" style={{ width: 150 }}>{t("logoDesc")}</div>
        </div>
        <div style={{ flex: 1, display: "flex", flexDirection: "column", gap: 14 }}>
          {input("brand_name", t("studioName"), t("studioNameDesc"))}
          {input("brand_tagline", t("tagline"), t("taglineDesc"))}
        </div>
      </div>

      {/* Как этот бизнес называет свои записи. Пресет, а не свободный текст:
          русский требует падежей («Новый заказ», «Заказов пока нет»), и
          произвольная строка дала бы «Новый запись». */}
      <div className="deal-fields" style={{ marginTop: 18 }}>
        <div>
          <label className="label">{t("dealTermTitle")}</label>
          <select
            className="input"
            value={values.deal_term ?? "deal"}
            onChange={(e) => patch({ deal_term: e.target.value })}
          >
            {DEAL_TERMS.map((name) => (
              <option key={name} value={name}>
                {term(name, locale, "many")}
              </option>
            ))}
          </select>
          <div className="field-desc">{t("dealTermDesc")}</div>
        </div>
        <div>
          <label className="label">{t("currencyTitle")}</label>
          <input
            className="input"
            value={values.currency ?? "USD"}
            maxLength={3}
            onChange={(e) => patch({ currency: e.target.value.toUpperCase() })}
          />
          <div className="field-desc">{t("currencyDesc")}</div>
        </div>
      </div>
      {/* Язык публичных страниц. Стоит здесь, а не в «Витрине», хотя ключ
          зовётся `showcase_locale`: он задаёт язык ВСЕХ страниц, которые видит
          посторонний, — витрины, страницы ввода PIN, страницы «ссылка закрыта»
          и заглушки режима обслуживания. Последняя показывается и тогда, когда
          блок досок выключен, а вместе с ним спрятан и раздел «Витрина». */}
      <div style={{ marginTop: 18 }}>
        <label className="label" style={{ marginBottom: 6 }}>
          {t("publicLanguage")}
        </label>
        <div style={{ display: "flex", gap: 6, marginBottom: 8 }}>
          {[
            { id: "en", label: "English" },
            { id: "ru", label: "Русский" },
          ].map((lang) => (
            <button
              key={lang.id}
              className={"option-chip lang-chip" + (values.showcase_locale === lang.id ? " active" : "")}
              onClick={() => patch({ showcase_locale: lang.id })}
            >
              {lang.label}
            </button>
          ))}
        </div>
        <div className="field-desc">{t("publicLanguageDesc")}</div>
      </div>
      <div style={{ marginTop: 18 }}>
        <label className="label" style={{ marginBottom: 8 }}>
          {t("accentColor")}
        </label>
        <div style={{ display: "flex", gap: 8 }}>
          {SWATCHES.map((color) => (
            <div
              key={color}
              className={"swatch" + (values.accent_color === color ? " active" : "")}
              style={{ background: color }}
              onClick={() => patch({ accent_color: color })}
            />
          ))}
        </div>
        <div className="field-desc">{t("accentColorDesc")}</div>
      </div>
    </div>
  );
}

export function SettingsContacts() {
  const { t } = useApp();
  const { input } = useSettings();

  return (
    <div className="card card-pad">
      <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 4 }}>{t("contacts")}</div>
      <div style={{ color: "var(--faint)", fontSize: 12.5, marginBottom: 6 }}>{t("contactsSub")}</div>
      <div style={{ color: "var(--faint)", fontSize: 12.5, marginBottom: 18, lineHeight: 1.5 }}>{t("contactsWhere")}</div>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "14px 16px" }}>
        {input("contact_email", t("email"), t("emailDesc"), "hello@studio.site")}
        {input("contact_phone", t("phone"), t("phoneDesc"))}
        {input("social_telegram", t("telegram"), t("telegramDesc"), "https://t.me/…")}
        {input("social_instagram", t("instagram"), t("instagramDesc"))}
        <div style={{ gridColumn: "1 / -1" }}>
          {input("social_website", t("website"), t("websiteDesc"), "https://…")}
        </div>
      </div>
    </div>
  );
}

export function SettingsShowcase() {
  const { t, toastError } = useApp();
  const { values, patch, switcher } = useSettings();
  const ogInput = useRef<HTMLInputElement>(null);

  const uploadOg = async (file: File | undefined) => {
    if (!file) return;
    try {
      const uploaded = await api.upload("/settings/og-image", file);
      patch({ og_default_image: uploaded.og_default_image });
    } catch (e) {
      toastError(e);
    }
  };

  const removeOg = async () => {
    try {
      await api.del("/settings/og-image");
      patch({ og_default_image: "" });
      if (ogInput.current) ogInput.current.value = "";
    } catch (e) {
      toastError(e);
    }
  };

  return (
    <div className="card card-pad">
      <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 4 }}>{t("showcase")}</div>
      <div style={{ color: "var(--faint)", fontSize: 12.5, marginBottom: 18 }}>{t("showcaseSub")}</div>
      {/* Языка публичных страниц здесь БОЛЬШЕ НЕТ — он переехал в «Бренд».
          Ключ `showcase_locale` называется витринным, но правит и заглушку
          режима обслуживания (`web/middleware.py`), а она показывается и с
          выключёнными досками — то есть когда этого раздела не существует.
          Оставить настройку здесь значило бы спрятать её вместе с блоком. */}
      <div style={{ display: "flex", gap: 20, alignItems: "flex-start" }}>
        <div>
          <label className="label" style={{ marginBottom: 6 }}>
            {t("ogImage")}
          </label>
          <div
            className="dropzone media-plate"
            style={{ width: 220, height: 116, padding: 8, display: "grid", placeItems: "center", overflow: "hidden", borderRadius: 10 }}
            onClick={() => ogInput.current?.click()}
          >
            {values.og_default_image ? (
              <img src={values.og_default_image} alt="" style={{ maxWidth: "100%", maxHeight: "100%", borderRadius: 6 }} />
            ) : (
              <span style={{ fontSize: 11.5 }}>{t("ogImageDrop")}</span>
            )}
          </div>
          <input ref={ogInput} type="file" accept="image/*" hidden onChange={(e) => void uploadOg(e.target.files?.[0])} />
          {values.og_default_image && (
            <button className="text-link danger" style={{ display: "block", marginTop: 8 }} onClick={() => void removeOg()}>
              {t("removeImage")}
            </button>
          )}
          <div className="field-desc" style={{ width: 220 }}>{t("ogImageDesc")}</div>
        </div>
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 14, marginTop: 20, paddingTop: 18, borderTop: "1px solid var(--surface-2)" }}>
        {switcher("showcase_show_meta", t("showcaseMeta"), t("showcaseMetaHint"))}
        {switcher("showcase_show_footer", t("showcaseFooter"), t("showcaseFooterHint"))}
      </div>
    </div>
  );
}

export function SettingsReturnButton() {
  const { t, refreshSettings, toastError } = useApp();
  const { values, patch, input } = useSettings();
  const siteLogoInput = useRef<HTMLInputElement>(null);
  const [fetchingLogo, setFetchingLogo] = useState(false);

  const uploadSiteLogo = async (file: File | undefined) => {
    if (!file) return;
    try {
      const uploaded = await api.upload("/settings/site-logo", file);
      patch({ studio_site_logo: uploaded.studio_site_logo });
      await refreshSettings();
    } catch (e) {
      toastError(e);
    }
  };

  const fetchSiteLogo = async () => {
    setFetchingLogo(true);
    try {
      // адрес берётся из настроек на сервере — сохраняем то, что сейчас в поле
      await api.patch("/settings", { values: { studio_site_url: values.studio_site_url ?? "" } });
      const fetched = await api.post("/settings/site-logo/fetch");
      patch({ studio_site_logo: fetched.studio_site_logo });
      await refreshSettings();
    } catch (e) {
      toastError(e);
    } finally {
      setFetchingLogo(false);
    }
  };

  const removeSiteLogo = async () => {
    try {
      await api.del("/settings/site-logo");
      patch({ studio_site_logo: "" });
      if (siteLogoInput.current) siteLogoInput.current.value = "";
      await refreshSettings();
    } catch (e) {
      toastError(e);
    }
  };

  return (
    <div className="card card-pad">
      <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 4 }}>{t("returnButton")}</div>
      <div style={{ color: "var(--faint)", fontSize: 12.5, marginBottom: 18 }}>{t("returnButtonSub")}</div>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 220px", gap: "14px 20px", alignItems: "start" }}>
        <div>
          {input("studio_site_url", t("studioSite"), t("studioSiteDesc"), "https://studio.site")}
          {input("studio_site_label", t("siteLabel"), t("siteLabelDesc"), "Return to the site")}
        </div>
        <div>
          <label className="label" style={{ marginBottom: 6 }}>
            {t("siteLogo")}
          </label>
          <div
            className="dropzone media-plate"
            style={{ height: 56, padding: 8, display: "grid", placeItems: "center", overflow: "hidden", borderRadius: 10 }}
            onClick={() => siteLogoInput.current?.click()}
          >
            {values.studio_site_logo ? (
              <img src={values.studio_site_logo} alt="" style={{ maxWidth: "100%", maxHeight: 28, objectFit: "contain" }} />
            ) : (
              <span style={{ fontSize: 11.5 }}>{t("siteLogoDrop")}</span>
            )}
          </div>
          <input ref={siteLogoInput} type="file" accept="image/*" hidden onChange={(e) => void uploadSiteLogo(e.target.files?.[0])} />
          <div style={{ display: "flex", gap: 12, marginTop: 8, alignItems: "center", flexWrap: "wrap" }}>
            <button className="text-link" disabled={fetchingLogo || !values.studio_site_url} onClick={() => void fetchSiteLogo()}>
              {fetchingLogo ? t("loading") : t("fetchLogo")}
            </button>
            {values.studio_site_logo && (
              <button className="text-link danger" onClick={() => void removeSiteLogo()}>
                {t("removeImage")}
              </button>
            )}
          </div>
          <div className="field-desc">{t("siteLogoDesc")}</div>
          <div className="field-desc">{t("fetchLogoHint")}</div>
        </div>
      </div>
      {/* Превью показывается ВСЕГДА и вместе с МЕСТОМ.
          Раньше оно появлялось только при заполненном адресе, и человек,
          зашедший впервые, видел три пустых поля и больше ничего: ни что это
          за кнопка, ни где она стоит. Владелец так и сказал — «даже перейдя в
          неё не понятно до конца». Поэтому рисуется кусок шапки доски: слева
          название студии, справа кнопка, ниже — заголовок доски. */}
      <div style={{ marginTop: 18 }}>
        <label className="label" style={{ marginBottom: 8 }}>
          {t("preview")}
        </label>
        <div className="preview-plate wide">
          <div className="showcase-head-preview">
            <span className="showcase-head-brand">{values.brand_name || "Studio"}</span>
            {/* Наведите — превью проигрывает ту же волну, что кнопка на витрине
                (см. .btn-site в web/public/templates/showcase.html). Ширина не
                задана: пилюля подгоняется под длину надписи, как и там. */}
            <div className={"site-btn-preview" + (values.studio_site_url ? "" : " off")}>
              {values.studio_site_logo && (
                <span className="seg">
                  <img src={values.studio_site_logo} alt="" style={{ height: 17, width: "auto", maxWidth: 92, objectFit: "contain", display: "block" }} />
                </span>
              )}
              <span style={{ padding: "0 18px" }}>{values.studio_site_label || "Return to the site"}</span>
            </div>
          </div>
          <div className="showcase-head-title">{t("previewBoardTitle")}</div>
        </div>
        {!values.studio_site_url && <div className="field-desc">{t("returnButtonEmpty")}</div>}
      </div>
    </div>
  );
}

/** Связь блоков: что заводится само (docs/bloki/21-svyaz-blokov.md). */
export function SettingsAutomation() {
  const { t, modules } = useApp();
  const { switcher } = useSettings();
  return (
    <div className="card card-pad">
      <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 4 }}>{t("automation")}</div>
      <div style={{ color: "var(--faint)", fontSize: 12.5, marginBottom: 18, lineHeight: 1.5 }}>{t("automationSub")}</div>
      <div style={{ display: "grid", gap: 16 }}>
        {/* Выключенный блок исчезает целиком — вместе со своим выключателем. */}
        {moduleOn(modules, "waybills") && switcher("auto_waybill", t("autoWaybill"), t("autoWaybillHint"))}
        {moduleOn(modules, "documents") && switcher("auto_act", t("autoAct"), t("autoActHint"))}
        {/* Блока у подсказок нет: они часть карточки клиента, а клиенты —
            основа системы и не выключаются. Выключателя не было вовсе, и
            настройка со значением «0» по умолчанию оставалась бы мёртвой. */}
        {switcher("address_hints", t("addressHints"), t("addressHintsHint"))}
      </div>
    </div>
  );
}

export function SettingsMaintenance() {
  const { t, locale, storage, refreshStorage, maintenance, setMaintenance, workspace, refreshWorkspace, toastError } = useApp();
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [liveBusy, setLiveBusy] = useState(false);

  // Выключатель живых обновлений — здесь, на видном месте: это первое, что
  // хочется погасить, когда система ведёт себя странно (docs/12 §14 п. 4).
  const toggleLive = async (enabled: boolean) => {
    setLiveBusy(true);
    try {
      await api.patch("/settings", { values: { realtime_enabled: enabled ? "1" : "0" } });
      await refreshWorkspace();
    } catch (e) {
      toastError(e);
    } finally {
      setLiveBusy(false);
    }
  };

  useEffect(() => {
    setNote(maintenance?.note || "");
  }, [maintenance?.note]);

  const toggle = async (enabled: boolean) => {
    setBusy(true);
    try {
      await setMaintenance(enabled, note);
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <div className="card card-pad" style={{ marginBottom: 20 }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 16, marginBottom: 4 }}>
          <div style={{ fontSize: 14, fontWeight: 600 }}>{t("liveUpdates")}</div>
          <Toggle
            on={workspace.realtime_enabled}
            label={t("liveUpdates")}
            onToggle={() => { if (!liveBusy) void toggleLive(!workspace.realtime_enabled); }}
          />
        </div>
        <div style={{ color: "var(--faint)", fontSize: 12.5, lineHeight: 1.5 }}>{t("liveUpdatesSub")}</div>
      </div>

      <div className="card card-pad" style={{ marginBottom: 20 }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 16, marginBottom: 4 }}>
          <div style={{ fontSize: 14, fontWeight: 600 }}>{t("closedMode")}</div>
          <Toggle
            on={!!maintenance?.enabled}
            label={t("closedMode")}
            onToggle={() => { if (!busy) void toggle(!maintenance?.enabled); }}
          />
        </div>
        <div style={{ color: "var(--faint)", fontSize: 12.5, marginBottom: 14, lineHeight: 1.5 }}>
          {t("closedModeSub")}
        </div>
        <label className="label" style={{ marginBottom: 6 }}>{t("closedNote")}</label>
        <input
          className="input"
          maxLength={200}
          placeholder={t("closedNotePlaceholder")}
          value={note}
          onChange={(e) => setNote(e.target.value)}
          onBlur={() => { if (maintenance?.enabled) void setMaintenance(true, note); }}
        />
        <div className="field-desc">{t("closedNoteDesc")}</div>
        {maintenance?.enabled && maintenance.since && (
          <div style={{ color: "var(--warning)", fontSize: 12.5, marginTop: 12 }}>
            {t("closedSince", { who: maintenance.by || "—", t: formatDateTime(maintenance.since, locale) })}
          </div>
        )}
      </div>

      <div className="card card-pad">
        <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 4 }}>{t("diskAndTrash")}</div>
        <div style={{ color: "var(--faint)", fontSize: 12.5, marginBottom: 6 }}>{t("maintenanceSub")}</div>
        <div style={{ color: "var(--faint)", fontSize: 12.5, marginBottom: 18, lineHeight: 1.5 }}>{t("maintenanceWhere")}</div>
        {storage ? (
          <StorageCard storage={storage} onPurged={() => void refreshStorage()} />
        ) : (
          <div style={{ color: "var(--faint)", fontSize: 12.5 }}>{t("loading")}</div>
        )}
      </div>
    </>
  );
}
