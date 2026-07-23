import { useEffect, useRef, useState } from "react";

import { StorageCard } from "../components/StorageCard";
import { ScreenLoading } from "../components/ui";
import { api } from "../lib/api";
import { useApp } from "../lib/app";

const SWATCHES = ["#D97757", "#6C8EEF", "#4CAF6E", "#E8A23D"];

export function Settings() {
  const { t, storage, refreshSettings, refreshStorage, toastError } = useApp();
  const [values, setValues] = useState<Record<string, string> | null>(null);
  const [saved, setSaved] = useState(false);
  const [busy, setBusy] = useState(false);
  const logoInput = useRef<HTMLInputElement>(null);
  const ogInput = useRef<HTMLInputElement>(null);

  useEffect(() => {
    api.get("/settings").then(setValues).catch(toastError);
  }, [toastError]);

  if (!values) return <ScreenLoading />;

  const set = (key: string) => (e: any) => setValues((v) => ({ ...v!, [key]: e.target.value }));

  const save = async () => {
    setBusy(true);
    try {
      const updated = await api.patch("/settings", { values });
      setValues(updated);
      await refreshSettings();
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    } catch (e) {
      toastError(e);
    } finally {
      setBusy(false);
    }
  };

  const uploadLogo = async (file: File | undefined) => {
    if (!file) return;
    try {
      const uploaded = await api.upload("/settings/logo", file);
      setValues((v) => ({ ...v!, brand_logo_path: uploaded.brand_logo_path }));
      await refreshSettings();
    } catch (e) {
      toastError(e);
    }
  };

  const removeLogo = async () => {
    try {
      await api.del("/settings/logo");
      setValues((v) => ({ ...v!, brand_logo_path: "" }));
      if (logoInput.current) logoInput.current.value = "";
      await refreshSettings();
    } catch (e) {
      toastError(e);
    }
  };

  const uploadOg = async (file: File | undefined) => {
    if (!file) return;
    try {
      const uploaded = await api.upload("/settings/og-image", file);
      setValues((v) => ({ ...v!, og_default_image: uploaded.og_default_image }));
    } catch (e) {
      toastError(e);
    }
  };

  const removeOg = async () => {
    try {
      await api.del("/settings/og-image");
      setValues((v) => ({ ...v!, og_default_image: "" }));
      if (ogInput.current) ogInput.current.value = "";
    } catch (e) {
      toastError(e);
    }
  };

  const input = (key: string, label: string, hint?: string, placeholder?: string) => (
    <div>
      <label className="label">
        {label} {hint && <span className="label-hint">{hint}</span>}
      </label>
      <input className="input" value={values[key] ?? ""} onChange={set(key)} placeholder={placeholder} />
    </div>
  );

  return (
    <div className="page page-narrow">
      <div className="page-head" style={{ alignItems: "flex-start", marginBottom: 32 }}>
        <div>
          <h1 className="page-title">{t("siteSettings")}</h1>
          <div className="page-sub">{t("settingsSub")}</div>
        </div>
        <button className={"btn " + (saved ? "btn-success" : "btn-primary")} onClick={() => void save()} disabled={busy}>
          {saved ? t("saved") : t("save")}
        </button>
      </div>

      <div className="card" style={{ padding: "22px 24px", marginBottom: 16 }}>
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
          </div>
          <div style={{ flex: 1, display: "flex", flexDirection: "column", gap: 14 }}>
            {input("brand_name", t("studioName"))}
            {input("brand_tagline", t("tagline"), t("taglineHint"))}
          </div>
        </div>
        <div style={{ marginTop: 18 }}>
          <label className="label" style={{ marginBottom: 8 }}>
            {t("accentColor")} <span className="label-hint">{t("accentColorHint")}</span>
          </label>
          <div style={{ display: "flex", gap: 8 }}>
            {SWATCHES.map((color) => (
              <div
                key={color}
                className={"swatch" + (values.accent_color === color ? " active" : "")}
                style={{ background: color }}
                onClick={() => setValues((v) => ({ ...v!, accent_color: color }))}
              />
            ))}
          </div>
        </div>
      </div>

      <div className="card" style={{ padding: "22px 24px", marginBottom: 16 }}>
        <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 4 }}>{t("contacts")}</div>
        <div style={{ color: "var(--faint)", fontSize: 12.5, marginBottom: 18 }}>{t("contactsSub")}</div>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "14px 16px" }}>
          {input("contact_email", t("email"), undefined, "hello@studio.site")}
          {input("contact_phone", t("phone"))}
          {input("social_telegram", t("telegram"), undefined, "https://t.me/…")}
          {input("social_instagram", t("instagram"))}
          <div style={{ gridColumn: "1 / -1" }}>{input("social_website", t("website"), undefined, "https://…")}</div>
        </div>
      </div>

      <div className="card" style={{ padding: "22px 24px" }}>
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
                  onClick={() => setValues((v) => ({ ...v!, showcase_locale: lang.id }))}
                >
                  {lang.label}
                </button>
              ))}
            </div>
                <div style={{ color: "var(--faint)", fontSize: 11.5, lineHeight: 1.5 }}>{t("showcaseLangHint")}</div>
          </div>
          <div>
            <label className="label" style={{ marginBottom: 6 }}>
              {t("ogImage")} <span className="label-hint">{t("ogImageHint")}</span>
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
          </div>
        </div>
      </div>

      <div className="card" style={{ padding: "22px 24px", marginTop: 16 }}>
        <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 4 }}>{t("maintenance")}</div>
        <div style={{ color: "var(--faint)", fontSize: 12.5, marginBottom: 18 }}>{t("maintenanceSub")}</div>
        {storage ? (
          <StorageCard storage={storage} onPurged={() => void refreshStorage()} />
        ) : (
          <div style={{ color: "var(--faint)", fontSize: 12.5 }}>{t("loading")}</div>
        )}
      </div>
    </div>
  );
}
