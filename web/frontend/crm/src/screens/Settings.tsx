import { useEffect, useRef, useState, type ReactNode } from "react";
import { Outlet, useOutletContext } from "react-router-dom";

import { StorageCard } from "../components/StorageCard";
import { ScreenLoading, Toggle } from "../components/ui";
import { api } from "../lib/api";
import { useApp } from "../lib/app";

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
export function SettingsLayout() {
  const { t, refreshSettings, toastError } = useApp();
  const [values, setValues] = useState<Values | null>(null);
  const [saved, setSaved] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.get("/settings").then(setValues).catch(toastError);
  }, [toastError]);

  if (!values) return <ScreenLoading />;

  const patch = (next: Values) => setValues((v) => ({ ...v!, ...next }));
  const set = (key: string) => (e: { target: { value: string } }) => patch({ [key]: e.target.value });

  const save = async () => {
    setBusy(true);
    try {
      setValues(await api.patch("/settings", { values }));
      await refreshSettings();
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
      <Toggle on={values[key] === "1"} onToggle={() => undefined} />
      <div style={{ marginTop: -2 }}>
        <div style={{ fontSize: 13, fontWeight: 500 }}>{label}</div>
        <div style={{ color: "var(--faint)", fontSize: 11.5, marginTop: 2, lineHeight: 1.5 }}>{hint}</div>
      </div>
    </div>
  );

  return (
    <div className="page page-narrow">
      <div className="page-head" style={{ alignItems: "flex-start", marginBottom: 26 }}>
        <div>
          <h1 className="page-title">{t("siteSettings")}</h1>
          <div className="page-sub">{t("settingsSub")}</div>
        </div>
        <button className={"btn " + (saved ? "btn-success" : "btn-primary")} onClick={() => void save()} disabled={busy}>
          {saved ? t("saved") : t("save")}
        </button>
      </div>
      <Outlet context={{ values, patch, set, input, switcher } satisfies SettingsCtx} />
    </div>
  );
}

export function SettingsBrand() {
  const { t, refreshSettings, toastError } = useApp();
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
    <div className="card" style={{ padding: "20px 22px" }}>
      <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 4 }}>{t("brand")}</div>
      <div style={{ color: "var(--faint)", fontSize: 12.5, marginBottom: 18 }}>{t("brandSub")}</div>
      <div style={{ display: "flex", gap: 20, alignItems: "flex-start" }}>
        <div>
          <label className="label" style={{ marginBottom: 6 }}>
            {t("logo")}
          </label>
          <div
            className="dropzone"
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
    <div className="card" style={{ padding: "20px 22px" }}>
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
    <div className="card" style={{ padding: "20px 22px" }}>
      <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 4 }}>{t("showcase")}</div>
      <div style={{ color: "var(--faint)", fontSize: 12.5, marginBottom: 18 }}>{t("showcaseSub")}</div>
      <div style={{ display: "flex", gap: 20, alignItems: "flex-start" }}>
        <div style={{ flex: 1 }}>
          <label className="label" style={{ marginBottom: 6 }}>
            {t("showcaseLanguage")}
          </label>
          <div style={{ display: "flex", gap: 6, marginBottom: 16 }}>
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
          <div style={{ color: "var(--faint)", fontSize: 11.5, lineHeight: 1.5 }}>{t("showcaseLangHint")}</div>
        </div>
        <div>
          <label className="label" style={{ marginBottom: 6 }}>
            {t("ogImage")}
          </label>
          <div
            className="dropzone"
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
    <div className="card" style={{ padding: "20px 22px" }}>
      <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 4 }}>{t("returnButton")}</div>
      <div style={{ color: "var(--faint)", fontSize: 12.5, marginBottom: 18 }}>{t("returnButtonSub")}</div>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 220px", gap: "14px 20px", alignItems: "start" }}>
        {input("studio_site_url", t("studioSite"), t("studioSiteDesc"), "https://studio.site")}
        <div>
          <label className="label" style={{ marginBottom: 6 }}>
            {t("siteLogo")}
          </label>
          <div
            className="dropzone"
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
      {values.studio_site_url && (
        <div style={{ marginTop: 18 }}>
          <label className="label" style={{ marginBottom: 8 }}>
            {t("preview")}
          </label>
          {/* покой кнопки на витрине — белая пилюля с тёмной надписью; наведение
              её гасит (см. .btn-site в web/public/templates/showcase.html) */}
          <div
            style={{
              display: "inline-flex", alignItems: "center", height: 36, padding: 0,
              background: "var(--text)", border: "1px solid var(--text)", borderRadius: 999, overflow: "hidden",
              fontSize: 12.5, fontWeight: 500, color: "var(--bg)", whiteSpace: "nowrap",
            }}
          >
            {values.studio_site_logo && (
              <span
                style={{
                  display: "flex", alignItems: "center", height: "100%", padding: "0 12px",
                  borderRight: "1px solid rgba(26, 26, 25, 0.18)",
                }}
              >
                <img src={values.studio_site_logo} alt="" style={{ height: 17, width: "auto", maxWidth: 92, objectFit: "contain", display: "block" }} />
              </span>
            )}
            <span style={{ padding: "0 18px" }}>Return to the site</span>
          </div>
        </div>
      )}
    </div>
  );
}

export function SettingsMaintenance() {
  const { t, storage, refreshStorage } = useApp();

  return (
    <div className="card" style={{ padding: "20px 22px" }}>
      <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 4 }}>{t("maintenance")}</div>
      <div style={{ color: "var(--faint)", fontSize: 12.5, marginBottom: 6 }}>{t("maintenanceSub")}</div>
      <div style={{ color: "var(--faint)", fontSize: 12.5, marginBottom: 18, lineHeight: 1.5 }}>{t("maintenanceWhere")}</div>
      {storage ? (
        <StorageCard storage={storage} onPurged={() => void refreshStorage()} />
      ) : (
        <div style={{ color: "var(--faint)", fontSize: 12.5 }}>{t("loading")}</div>
      )}
    </div>
  );
}
