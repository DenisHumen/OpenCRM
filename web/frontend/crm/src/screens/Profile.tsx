import { useRef, useState, type FormEvent } from "react";

import { Icon } from "../components/Icon";
import { Avatar } from "../components/ui";
import { api, ApiError, type User } from "../lib/api";
import { useApp } from "../lib/app";
import {
  signaly_vklyucheny,
  vklyuchit_signaly,
  vyklyuchit_signaly,
} from "../lib/signaly";
import { formatDate, initials } from "../lib/format";
import { THEMES, type Theme } from "../lib/theme";

/** Подпись под каждым положением переключателя тем. */
const THEME_LABEL: Record<Theme, "themeLight" | "themeDark" | "themeSystem"> = {
  light: "themeLight",
  dark: "themeDark",
  system: "themeSystem",
};

const ZONY = [1, 2, 3, 4, 5, 6, 7, 8, 9];

/** Пропуск сотрудника — карточка, что наклоняется за курсором. Перевод
 *  uiverse.io/Cobp/silent-bullfrog-72 (docs/18): девять пустых зон ловят
 *  курсор, карточка идёт после них. Читалке скрыт: всё то же есть в шапке. */
function Propusk({ user, brand }: { user: User; brand: string }) {
  const { t, locale } = useApp();
  return (
    <div className="propusk-wrap" aria-hidden="true">
      {ZONY.map((z) => (
        <div key={z} className="propusk-zona" />
      ))}
      <div className="propusk">
        <div className="propusk-logo">OpenCRM</div>
        <div className="propusk-rol">
          <span>{user.role === "root" ? t("root") : user.role_name || t("noRole")}</span>
          <Icon name="check" size={18} stroke={2} />
        </div>
        <div className="propusk-info">
          <div>
            <div className="propusk-l">{t("company")}</div>
            <div className="propusk-v">{brand || "OpenCRM"}</div>
          </div>
          <div>
            <div className="propusk-l">{t("badgeJoined")}</div>
            <div className="propusk-v">{user.created_at ? formatDate(user.created_at, locale) : ""}</div>
          </div>
        </div>
        <div className="propusk-line" />
        <div className="propusk-user">
          <span className="propusk-alias">{user.email}</span>
          <span className="propusk-name">{user.name}</span>
        </div>
        <div className="propusk-niz">
          <div className="propusk-qr">
            {user.avatar_url ? <img src={user.avatar_url} alt="" /> : initials(user.name)}
          </div>
          <div className="propusk-pos">
            <div>
              <div className="propusk-l">{t("badgeId")}</div>
              <div className="propusk-v">#{user.id}</div>
            </div>
            <div>
              <div className="propusk-l">{t("badgeSince")}</div>
              <div className="propusk-v">{user.created_at ? user.created_at.slice(0, 4) : ""}</div>
            </div>
          </div>
        </div>
        <div className="propusk-svet" />
      </div>
    </div>
  );
}

export function Profile() {
  const { user, t, locale, theme, setTheme, setUser, toast, toastError, workspace } = useApp();
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

  // Сигналы о письмах клиентов: настройка браузера, не учётной записи.
  const [signaly, setSignaly] = useState(signaly_vklyucheny());

  const perekluchit_signaly = async () => {
    const itog = await vklyuchit_signaly();
    setSignaly(true);
    if (itog === "denied") toast(t("tgSignalsDenied"));
    else if (itog === "unsupported") toast(t("tgSignalsSoundOnly"));
  };

  return (
    <div className="page page-tight">
      <div style={{ display: "flex", alignItems: "center", flexWrap: "wrap", gap: 16, marginBottom: 26 }}>
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
        <Propusk user={user} brand={workspace.brand_name} />
      </div>

      <div className="card card-pad" style={{ marginBottom: 16 }}>
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

      <div className="card card-pad" style={{ marginBottom: 16 }}>
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

      {/* Тема. Три положения, а не переключатель «светлая/тёмная»: «как в
          системе» — не среднее между ними, а отказ решать за человека, у
          которого ноутбук сам темнеет к вечеру.

          Кнопки, а не `div`: по ним ходит Tab и на них отвечает пробел — тем же
          нажатием, что мышью. `aria-pressed` вместо `role="radio"` намеренно:
          настоящая радиогруппа обязана водить стрелками и держать один Tab-стоп
          на всю группу, и объявить её, не сделав этого, хуже, чем не объявлять.
          Проверка — tests/test_screens.py. */}
      <div className="card card-pad" style={{ marginBottom: 16 }}>
        <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 16 }}>{t("interfaceTheme")}</div>
        <div className="theme-pick" role="group" aria-label={t("interfaceTheme")} style={{ display: "flex", gap: 6, marginBottom: 12, flexWrap: "wrap" }}>
          {THEMES.map((id) => (
            <button
              key={id}
              type="button"
              className={"option-chip pick-chip" + (theme === id ? " active" : "")}
              aria-pressed={theme === id}
              onClick={() => setTheme(id)}
            >
              {t(THEME_LABEL[id])}
            </button>
          ))}
        </div>
        <div style={{ color: "var(--faint)", fontSize: 11.5 }}>{t("themeHint")}</div>
      </div>

      {/* Сигналы о письмах клиентов. Настройка ЭТОГО БРАУЗЕРА, а не учётной
          записи, и написано об этом прямо: разрешение на всплывающие окна даёт
          браузер, и оно не переезжает на телефон вместе с логином. Храни мы
          её на сервере — человек включил бы сигналы на рабочем компьютере и
          недоумевал, почему дома тихо.

          Разрешение спрашивается ПО НАЖАТИЮ, и это не придирка к порядку:
          спрошенное при загрузке страницы люди отклоняют не глядя, а
          отклонённое браузер помнит навсегда — второго раза не будет. */}
      <div className="card card-pad" style={{ marginBottom: 16 }}>
        <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 16 }}>{t("tgSignals")}</div>
        <div style={{ display: "flex", gap: 6, marginBottom: 12, flexWrap: "wrap" }}>
          <button
            type="button"
            className={"option-chip pick-chip" + (signaly ? " active" : "")}
            aria-pressed={signaly}
            onClick={() => void perekluchit_signaly()}
          >
            {t("tgSignalsOn")}
          </button>
          <button
            type="button"
            className={"option-chip pick-chip" + (!signaly ? " active" : "")}
            aria-pressed={!signaly}
            onClick={() => {
              vyklyuchit_signaly();
              setSignaly(false);
            }}
          >
            {t("tgSignalsOff")}
          </button>
        </div>
        <div style={{ color: "var(--faint)", fontSize: 11.5 }}>{t("tgSignalsHint")}</div>
      </div>

      <form className="card card-pad" onSubmit={changePassword}>
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
