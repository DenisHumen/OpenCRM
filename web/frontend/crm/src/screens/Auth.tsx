import { useState, type FormEvent } from "react";
import { useLocation, useNavigate } from "react-router-dom";

import { api, ApiError, type User } from "../lib/api";
import { useApp } from "../lib/app";

type View = "login" | "register" | "pending";

function BrandMark() {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 9, marginBottom: 28 }}>
      <div
        style={{
          width: 26,
          height: 26,
          borderRadius: 7,
          background: "var(--brand)",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          color: "var(--bg-sidebar)",
          fontSize: 13,
          fontWeight: 700,
        }}
      >
        O
      </div>
      <span style={{ color: "var(--text)", fontSize: 16, fontWeight: 600 }}>OpenCRM</span>
    </div>
  );
}

export function AuthScreen() {
  const { user, ready, t, setUser } = useApp();
  const navigate = useNavigate();
  const location = useLocation();
  const [view, setView] = useState<View>("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [name, setName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  if (ready && user && !user.must_change_password) {
    const from = (location.state as any)?.from?.pathname ?? "/";
    navigate(from, { replace: true });
  }

  const submitLogin = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const me = await api.post<User>("/auth/login", { email, password });
      setUser(me);
      navigate(me.must_change_password ? "/" : ((location.state as any)?.from?.pathname ?? "/"), { replace: true });
    } catch (err) {
      setError(err instanceof ApiError ? err.message : t("error"));
    } finally {
      setBusy(false);
    }
  };

  const submitRegister = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.post("/auth/register", { name, email, password });
      setView("pending");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : t("error"));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="center-screen">
      <BrandMark />
      {view === "login" && (
        <form className="auth-card" onSubmit={submitLogin}>
          <h1 className="auth-title">{t("welcomeBack")}</h1>
          <div className="auth-sub">{t("signInSub")}</div>
          <div className="field">
            <label className="label">{t("email")}</label>
            {/* inputMode — клавиатура с @ и точкой сразу; autoCapitalize/autoCorrect —
                иначе iOS пишет адрес с заглавной и «исправляет» домен. autoComplete —
                чтобы менеджер паролей подставлял пару, а не только логин. */}
            <input
              className="input"
              type="email"
              inputMode="email"
              autoCapitalize="none"
              autoCorrect="off"
              spellCheck={false}
              autoComplete="email"
              placeholder="you@studio.site"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              autoFocus
              required
            />
          </div>
          <div className="field" style={{ marginBottom: 20 }}>
            <label className="label">{t("password")}</label>
            <input
              className="input"
              type="password"
              autoComplete="current-password"
              placeholder="••••••••"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
            />
          </div>
          <button className="btn btn-primary" style={{ width: "100%", marginBottom: 16 }} disabled={busy}>
            {t("signIn")}
          </button>
          {error && <div className="form-error" style={{ marginBottom: 12 }}>{error}</div>}
          <div style={{ textAlign: "center", color: "var(--faint)", fontSize: 13 }}>
            {t("noAccount")}{" "}
            <span
              style={{ color: "var(--accent)", cursor: "pointer", textDecoration: "underline", textUnderlineOffset: 2 }}
              onClick={() => {
                setView("register");
                setError(null);
              }}
            >
              {t("requestOne")}
            </span>
          </div>
        </form>
      )}
      {view === "register" && (
        <form className="auth-card" onSubmit={submitRegister}>
          <h1 className="auth-title">{t("joinStudio")}</h1>
          <div className="auth-sub">{t("joinSub")}</div>
          <div className="field">
            <label className="label">{t("fullName")}</label>
            <input className="input" autoComplete="name" value={name} onChange={(e) => setName(e.target.value)} autoFocus required />
          </div>
          <div className="field">
            <label className="label">{t("email")}</label>
            <input
              className="input"
              type="email"
              inputMode="email"
              autoCapitalize="none"
              autoCorrect="off"
              spellCheck={false}
              autoComplete="email"
              placeholder="you@studio.site"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
            />
          </div>
          <div className="field" style={{ marginBottom: 20 }}>
            <label className="label">{t("password")}</label>
            <input
              className="input"
              type="password"
              autoComplete="new-password"
              placeholder={t("minChars")}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              minLength={10}
              required
            />
          </div>
          <button className="btn btn-primary" style={{ width: "100%", marginBottom: 16 }} disabled={busy}>
            {t("requestAccount")}
          </button>
          {error && <div className="form-error" style={{ marginBottom: 12 }}>{error}</div>}
          <div style={{ textAlign: "center", color: "var(--faint)", fontSize: 13 }}>
            {t("alreadyApproved")}{" "}
            <span
              style={{ color: "var(--accent)", cursor: "pointer", textDecoration: "underline", textUnderlineOffset: 2 }}
              onClick={() => {
                setView("login");
                setError(null);
              }}
            >
              {t("signIn")}
            </span>
          </div>
        </form>
      )}
      {view === "pending" && (
        <div className="auth-card" style={{ textAlign: "center", padding: "36px 28px 30px" }}>
          <div
            style={{
              width: 44,
              height: 44,
              borderRadius: "50%",
              background: "rgba(232,162,61,0.15)",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              margin: "0 auto 18px",
              color: "var(--warning)",
            }}
          >
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
              <circle cx="12" cy="12" r="10" />
              <polyline points="12 6 12 12 16 14" />
            </svg>
          </div>
          <h1 className="auth-title">{t("requestSent")}</h1>
          <div style={{ color: "var(--muted)", fontSize: 13.5, lineHeight: 1.6, marginBottom: 22 }}>
            {t("requestSentSub")}
          </div>
          <button className="btn btn-secondary btn-sm" onClick={() => setView("login")}>
            {t("backToSignIn")}
          </button>
        </div>
      )}
    </div>
  );
}

export function ForcePasswordChange() {
  const { user, t, setUser, toastError } = useApp();
  const [oldPassword, setOldPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [repeat, setRepeat] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (newPassword !== repeat) {
      setError(t("passwordsDontMatch"));
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await api.post("/auth/me/password", { old_password: oldPassword, new_password: newPassword });
      const me = await api.get<User>("/auth/me");
      setUser(me);
    } catch (err) {
      if (err instanceof ApiError) setError(err.message);
      else toastError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="center-screen">
      <BrandMark />
      <form className="auth-card" onSubmit={submit}>
        <h1 className="auth-title">{t("changePassword")}</h1>
        <div className="auth-sub">{t("changePasswordForced")}</div>
        <div className="field">
          <label className="label">{t("currentPassword")}</label>
          <input className="input" type="password" value={oldPassword} onChange={(e) => setOldPassword(e.target.value)} autoFocus required />
        </div>
        <div className="field">
          <label className="label">{t("newPassword")}</label>
          <input className="input" type="password" placeholder={t("minChars")} value={newPassword} onChange={(e) => setNewPassword(e.target.value)} minLength={10} required />
        </div>
        <div className="field" style={{ marginBottom: 20 }}>
          <label className="label">{t("repeatPassword")}</label>
          <input className="input" type="password" value={repeat} onChange={(e) => setRepeat(e.target.value)} required />
        </div>
        <button className="btn btn-primary" style={{ width: "100%" }} disabled={busy}>
          {t("updatePassword")}
        </button>
        {error && <div className="form-error">{error}</div>}
        <div style={{ marginTop: 16, fontSize: 12, color: "var(--faint)", textAlign: "center" }}>{user?.email}</div>
      </form>
    </div>
  );
}
