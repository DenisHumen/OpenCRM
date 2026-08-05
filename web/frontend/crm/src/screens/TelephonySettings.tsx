import { useEffect, useState } from "react";

import { Icon } from "../components/Icon";
import { ConfirmModal, ScreenLoading } from "../components/ui";
import { api } from "../lib/api";
import { useApp } from "../lib/app";
import type { TranslationKey } from "../lib/i18n";

interface TelephonyConfig {
  provider: string;
  api_url: string;
  has_api_token: boolean;
  has_webhook_secret: boolean;
  utc_offset: string;
  default_ext: string;
  default_country_code: string;
  webhook_path: string;
}

const PROVIDERS: { id: string; label: TranslationKey }[] = [
  { id: "", label: "providerNone" },
  { id: "fake", label: "providerFake" },
  { id: "http", label: "providerHttp" },
];

/**
 * Подключение к АТС.
 *
 * Отдельным маршрутом, а не разделом «Настроек сайта»: там одна кнопка
 * «Сохранить» на всю группу, а здесь свои поля и свой секрет, который
 * показывается один раз. По той же причине отдельно живут и блоки.
 *
 * Секреты сюда не приходят вовсе — сервер отдаёт только «задан или нет».
 */
export function SettingsTelephony() {
  const { t, toast, toastError } = useApp();
  const [config, setConfig] = useState<TelephonyConfig | null>(null);
  const [token, setToken] = useState("");
  const [freshSecret, setFreshSecret] = useState("");
  const [confirmSecret, setConfirmSecret] = useState(false);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    api.get<TelephonyConfig>("/telephony/settings").then(setConfig).catch(toastError);
  }, [toastError]);

  if (!config) return <ScreenLoading />;

  const set = (key: keyof TelephonyConfig) => (value: string) =>
    setConfig((prev) => ({ ...prev!, [key]: value }));

  const save = async () => {
    setBusy(true);
    try {
      const values: Record<string, string> = {
        telephony_provider: config.provider,
        telephony_api_url: config.api_url,
        telephony_utc_offset: config.utc_offset,
        telephony_default_ext: config.default_ext,
        default_country_code: config.default_country_code,
      };
      // Пустое поле токена означает «не менять», а не «стереть»: обратно он не
      // показывается, и пустая форма затирала бы рабочее подключение.
      if (token.trim()) values.telephony_api_token = token.trim();
      setConfig(await api.patch<TelephonyConfig>("/telephony/settings", { values }));
      setToken("");
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    } catch (e) {
      toastError(e);
    } finally {
      setBusy(false);
    }
  };

  const regenerate = async () => {
    try {
      const created = await api.post<{ secret: string }>("/telephony/settings/secret");
      setFreshSecret(created.secret);
      setConfig((prev) => ({ ...prev!, has_webhook_secret: true }));
    } catch (e) {
      toastError(e);
    }
  };

  const webhookUrl = window.location.origin + config.webhook_path;

  return (
    <div className="page page-narrow">
      <div className="page-head" style={{ alignItems: "flex-start", marginBottom: 22 }}>
        <div>
          <h1 className="page-title">{t("telephony")}</h1>
          <div className="page-sub">{t("telephonySub")}</div>
        </div>
        <button
          className={"btn " + (saved ? "btn-success" : "btn-primary")}
          disabled={busy}
          onClick={() => void save()}
        >
          {saved ? t("saved") : t("save")}
        </button>
      </div>

      <div className="card card-pad" style={{ marginBottom: 18 }}>
        <label className="label" style={{ marginBottom: 6 }}>{t("telephonyProvider")}</label>
        <div style={{ display: "flex", gap: 6 }}>
          {PROVIDERS.map((provider) => (
            <button
              key={provider.id}
              className={"option-chip" + (config.provider === provider.id ? " active" : "")}
              onClick={() => set("provider")(provider.id)}
            >
              {t(provider.label)}
            </button>
          ))}
        </div>
        <div className="field-desc">{t("telephonyProviderDesc")}</div>

        {config.provider === "http" && (
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "1fr 1fr",
              gap: "14px 16px",
              marginTop: 14,
            }}
          >
            <div>
              <label className="label">{t("telephonyApiUrl")}</label>
              <input
                className="input"
                value={config.api_url}
                placeholder="https://pbx.example.com/originate"
                onChange={(e) => set("api_url")(e.target.value)}
              />
              <div className="field-desc">{t("telephonyApiUrlDesc")}</div>
            </div>
            <div>
              <label className="label">{t("telephonyApiToken")}</label>
              <input
                className="input"
                type="password"
                value={token}
                placeholder={config.has_api_token ? "••••••••" : ""}
                onChange={(e) => setToken(e.target.value)}
              />
              <div className="field-desc">{t("telephonyApiTokenDesc")}</div>
            </div>
          </div>
        )}

        <div
          style={{
            display: "grid",
            gridTemplateColumns: "1fr 1fr 1fr",
            gap: "14px 16px",
            marginTop: 14,
          }}
        >
          <div>
            <label className="label">{t("telephonyExt")}</label>
            <input
              className="input"
              value={config.default_ext}
              placeholder="101"
              onChange={(e) => set("default_ext")(e.target.value)}
            />
            <div className="field-desc">{t("telephonyExtDesc")}</div>
          </div>
          <div>
            <label className="label">{t("telephonyCountry")}</label>
            <input
              className="input"
              value={config.default_country_code}
              placeholder="380"
              onChange={(e) => set("default_country_code")(e.target.value)}
            />
            <div className="field-desc">{t("telephonyCountryDesc")}</div>
          </div>
          <div>
            <label className="label">{t("telephonyOffset")}</label>
            <input
              className="input"
              value={config.utc_offset}
              placeholder="+03:00"
              onChange={(e) => set("utc_offset")(e.target.value)}
            />
            <div className="field-desc">{t("telephonyOffsetDesc")}</div>
          </div>
        </div>
      </div>

      <div className="card card-pad">
        <label className="label" style={{ marginBottom: 6 }}>{t("telephonyWebhook")}</label>
        <div
          className="input"
          style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer" }}
          onClick={() => {
            void navigator.clipboard?.writeText(webhookUrl);
            toast(t("copied"));
          }}
        >
          <Icon name="link" size={13} />
          <span
            style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
          >
            {webhookUrl}
          </span>
          <Icon name="copy" size={13} />
        </div>
        <div className="field-desc">{t("telephonyWebhookDesc")}</div>

        <div style={{ marginTop: 16, display: "flex", alignItems: "center", gap: 12 }}>
          <span style={{ fontSize: 13, fontWeight: 500 }}>{t("telephonySecret")}</span>
          <span
            style={{
              color: config.has_webhook_secret ? "var(--muted)" : "var(--danger)",
              fontSize: 12.5,
            }}
          >
            {config.has_webhook_secret ? t("telephonySecretSet") : t("telephonySecretMissing")}
          </span>
          <button className="text-link" style={{ marginLeft: "auto" }} onClick={() => setConfirmSecret(true)}>
            {t("regenerateSecret")}
          </button>
        </div>
        <div className="field-desc">{t("telephonySecretDesc")}</div>
        {freshSecret && (
          <div style={{ marginTop: 10 }}>
            <code className="input" style={{ display: "block", wordBreak: "break-all" }}>
              {freshSecret}
            </code>
            <div className="field-desc">{t("telephonySecretNew")}</div>
          </div>
        )}
      </div>

      {confirmSecret && (
        <ConfirmModal
          text={t("regenerateSecretConfirm")}
          confirmLabel={t("confirm")}
          onConfirm={() => void regenerate()}
          onClose={() => setConfirmSecret(false)}
        />
      )}
    </div>
  );
}
