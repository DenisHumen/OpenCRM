import { useRef, useState, type FormEvent } from "react";

import { Avatar } from "../components/ui";
import { api, ApiError, type User } from "../lib/api";
import { useApp } from "../lib/app";
import { formatDate, initials } from "../lib/format";

export function Profile() {
  const { user, t, locale, setUser, toast, toastError } = useApp();
  const [name, setName] = useState(user?.name ?? "");
  const [saved, setSaved] = useState(false);
  const [oldPassword, setOldPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [repeat, setRepeat] = useState("");
  const [passwordError, setPasswordError] = useState<string | null>(null);
  const avatarInput = useRef<HTMLInputElement>(null);

  if (!user) return null;

  const uploadAvatar = async (file: File | undefined) => {
    if (!file) return;
    try {
      setUser(await api.upload<User>("/auth/me/avatar", file));
      toast(t("saved"));
    } catch (e) {
      toastError(e);
    }
  };

  const removeAvatar = async () => {
    try {
      setUser(await api.del<User>("/auth/me/avatar"));
      if (avatarInput.current) avatarInput.current.value = "";
    } catch (e) {
      toastError(e);
    }
  };

  const saveProfile = async () => {
    try {
      const updated = await api.patch<User>("/auth/me", { name });
      setUser(updated);
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    } catch (e) {
      toastError(e);
    }
  };

  const setLocale = async (loc: "en" | "ru") => {
    try {
      setUser(await api.patch<User>("/auth/me", { locale: loc }));
    } catch (e) {
      toastError(e);
    }
  };

  const changePassword = async (e: FormEvent) => {
    e.preventDefault();
    if (newPassword !== repeat) {
      setPasswordError(t("passwordsDontMatch"));
      return;
    }
    setPasswordError(null);
    try {
      await api.post("/auth/me/password", { old_password: oldPassword, new_password: newPassword });
      setOldPassword("");
      setNewPassword("");
      setRepeat("");
      toast(t("saved"));
    } catch (err) {
      setPasswordError(err instanceof ApiError ? err.message : t("error"));
    }
  };

  return (
    <div className="page page-tight">
      <div style={{ display: "flex", alignItems: "center", gap: 16, marginBottom: 26 }}>
        <div onClick={() => avatarInput.current?.click()} title={t("changePhoto")} style={{ cursor: "pointer" }}>
          <Avatar text={initials(user.name)} large src={user.avatar_url} online />
        </div>
        <input
          ref={avatarInput}
          type="file"
          accept="image/png,image/jpeg,image/webp,image/gif"
          hidden
          onChange={(e) => void uploadAvatar(e.target.files?.[0])}
        />
        <div>
          <h1 className="page-title" style={{ fontSize: 22 }}>
            {t("profile")}
          </h1>
          <div className="page-sub" style={{ marginTop: 4 }}>
            {user.email} ·{" "}
            <span style={{ color: user.role === "root" ? "var(--brand)" : "var(--muted)" }}>
              {user.role === "root" ? t("root") : user.role_name || t("noRole")}
            </span>{" "}
            · {t("joined")} {formatDate(user.created_at, locale)}
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 12, marginTop: 8 }}>
            <button className="text-link" onClick={() => avatarInput.current?.click()}>
              {user.avatar_url ? t("changePhoto") : t("uploadPhoto")}
            </button>
            {user.avatar_url && (
              <button className="text-link danger" onClick={() => void removeAvatar()}>
                {t("removeImage")}
              </button>
            )}
            <span style={{ color: "var(--faint)", fontSize: 11.5 }}>{t("photoHint")}</span>
          </div>
        </div>
      </div>

      <div className="card" style={{ padding: "20px 22px", marginBottom: 16 }}>
        <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 16 }}>{t("account")}</div>
        <div className="field">
          <label className="label">{t("displayName")}</label>
          <input className="input" value={name} onChange={(e) => setName(e.target.value)} />
        </div>
        <div className="field" style={{ marginBottom: 18 }}>
          <label className="label">
            {t("email")} <span className="label-hint">{t("emailHint")}</span>
          </label>
          <input className="input" value={user.email} disabled />
        </div>
        <button className={"btn btn-sm " + (saved ? "btn-success" : "btn-primary")} onClick={() => void saveProfile()}>
          {saved ? t("saved") : t("save")}
        </button>
      </div>

      <div className="card" style={{ padding: "20px 22px", marginBottom: 16 }}>
        <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 16 }}>{t("interfaceLanguage")}</div>
        <div style={{ display: "flex", gap: 6, marginBottom: 12 }}>
          {[
            { id: "en" as const, label: "English" },
            { id: "ru" as const, label: "Русский" },
          ].map((lang) => (
            <button
              key={lang.id}
              className={"option-chip lang-chip" + (user.locale === lang.id ? " active" : "")}
              onClick={() => void setLocale(lang.id)}
            >
              {lang.label}
            </button>
          ))}
        </div>
        <div style={{ color: "var(--faint)", fontSize: 11.5 }}>{t("langSaved")}</div>
      </div>

      <form className="card" style={{ padding: "20px 22px" }} onSubmit={changePassword}>
        <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 16 }}>{t("changePassword")}</div>
        <div className="field">
          <label className="label">{t("currentPassword")}</label>
          <input className="input" type="password" value={oldPassword} onChange={(e) => setOldPassword(e.target.value)} required />
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "14px 16px", marginBottom: 16 }}>
          <div>
            <label className="label">{t("newPassword")}</label>
            <input className="input" type="password" placeholder={t("minChars")} value={newPassword} onChange={(e) => setNewPassword(e.target.value)} minLength={10} required />
          </div>
          <div>
            <label className="label">{t("repeatPassword")}</label>
            <input className="input" type="password" value={repeat} onChange={(e) => setRepeat(e.target.value)} required />
          </div>
        </div>
        <button className="btn btn-secondary btn-sm" style={{ height: 32 }}>
          {t("updatePassword")}
        </button>
        {passwordError && <div className="form-error">{passwordError}</div>}
      </form>
    </div>
  );
}
