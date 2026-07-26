import { useEffect, useState } from "react";
import { NavLink, useNavigate } from "react-router-dom";

import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { formatBytes, initials } from "../lib/format";
import { Icon } from "./Icon";
import { Avatar } from "./ui";

export function Sidebar({ onOpenSearch }: { onOpenSearch: () => void }) {
  const { user, t, settings, storage, setUser, logout, toastError } = useApp();
  const navigate = useNavigate();
  const [menuOpen, setMenuOpen] = useState(false);
  const [pendingCount, setPendingCount] = useState(0);

  const isRoot = user?.role === "root";

  useEffect(() => {
    if (!isRoot) return;
    api
      .get("/staff?status=pending")
      .then((data) => setPendingCount(data.items.length))
      .catch(() => undefined);
  }, [isRoot]);

  const switchLocale = async () => {
    if (!user) return;
    const next = user.locale === "en" ? "ru" : "en";
    try {
      const updated = await api.patch("/auth/me", { locale: next });
      setUser(updated);
    } catch (e) {
      toastError(e);
    }
  };

  const brandName = settings.brand_name || "OpenCRM";

  return (
    <aside className="sidebar">
      <div className="side-top">
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
          <NavLink to="/" className="side-brand">
            <div className="side-logo">O</div>
            <span style={{ color: "var(--text)", fontSize: 14, fontWeight: 600 }}>OpenCRM</span>
          </NavLink>
          <Icon name="sidebar" size={16} className="" />
        </div>
        <div className="side-workspace">
          <div
            style={{
              width: 22,
              height: 22,
              borderRadius: 5,
              background: "rgba(217,119,87,0.18)",
              color: "var(--brand)",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              fontSize: 11,
              fontWeight: 700,
            }}
          >
            {brandName[0]?.toUpperCase() ?? "S"}
          </div>
          <div style={{ flex: 1, minWidth: 0, color: "var(--text)", fontSize: 13, fontWeight: 500, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
            {brandName}
          </div>
          <Icon name="chevronsUpDown" size={14} />
        </div>
        <button type="button" className="side-search" onClick={onOpenSearch}>
          <Icon name="search" size={14} />
          <span style={{ flex: 1, color: "var(--faint)", fontSize: 13, textAlign: "left" }}>
            {t("search")}
          </span>
          <span style={{ display: "flex", gap: 3 }}>
            <span className="kbd">{navigator.platform.includes("Mac") ? "⌘" : "Ctrl"}</span>
            <span className="kbd">K</span>
          </span>
        </button>
      </div>
      <nav className="side-nav">
        <NavLink to="/" end className={({ isActive }) => "nav-item" + (isActive ? " active" : "")}>
          <Icon name="dashboard" size={16} />
          <span style={{ flex: 1 }}>{t("dashboard")}</span>
        </NavLink>
        <NavLink to="/clients" className={({ isActive }) => "nav-item" + (isActive ? " active" : "")}>
          <Icon name="clients" size={16} />
          <span style={{ flex: 1 }}>{t("clients")}</span>
        </NavLink>
        <NavLink to="/boards" className={({ isActive }) => "nav-item" + (isActive ? " active" : "")}>
          <Icon name="boards" size={16} />
          <span style={{ flex: 1 }}>{t("boards")}</span>
        </NavLink>
        {isRoot && (
          <>
            <div className="nav-section">{t("admin")}</div>
            <NavLink to="/staff" className={({ isActive }) => "nav-item" + (isActive ? " active" : "")}>
              <Icon name="staff" size={16} />
              <span style={{ flex: 1 }}>{t("staff")}</span>
            </NavLink>
            <NavLink to="/files" className={({ isActive }) => "nav-item" + (isActive ? " active" : "")}>
              <Icon name="folder" size={16} />
              <span style={{ flex: 1 }}>{t("files")}</span>
            </NavLink>
            <NavLink to="/settings" className={({ isActive }) => "nav-item" + (isActive ? " active" : "")}>
              <Icon name="settings" size={16} />
              <span style={{ flex: 1 }}>{t("siteSettings")}</span>
            </NavLink>
          </>
        )}
      </nav>
      <div className="side-bottom">
        {storage && storage.level !== "ok" && (
          <NavLink
            to={isRoot ? "/settings" : "/"}
            className={"side-banner" + (storage.level === "critical" ? " critical" : "")}
          >
            <span style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 2 }}>
              <Icon name="alert" size={13} />
              <strong style={{ fontWeight: 600 }}>{t("storage")}</strong>
            </span>
            {storage.level === "critical"
              ? t("diskBannerCritical", { free: formatBytes(storage.free_bytes) })
              : t("diskBannerWarning", {
                  percent: storage.percent_used,
                  free: formatBytes(storage.free_bytes),
                })}
            {storage.uploads_blocked && <>. {t("uploadsBlocked")}</>}{" "}
            <span style={{ textDecoration: "underline", textUnderlineOffset: 2 }}>
              {t("diskBannerAction")}
            </span>
          </NavLink>
        )}
        {isRoot && pendingCount > 0 && (
          <NavLink to="/staff" className="side-banner">
            <strong style={{ fontWeight: 600 }}>
              {pendingCount} {t("signupRequests").toLowerCase()}
            </strong>{" "}
            <span style={{ textDecoration: "underline", textUnderlineOffset: 2 }}>{t("approve")}</span>
          </NavLink>
        )}
        <a
          className="nav-item"
          href="https://github.com/DenisHumen/OpenCRM"
          target="_blank"
          rel="noreferrer"
          style={{ color: "var(--muted)", fontSize: 13 }}
        >
          <Icon name="docs" size={15} />
          {t("documentation")}
        </a>
        <div style={{ position: "relative" }}>
          {menuOpen && (
            <div className="user-menu">
              <NavLink to="/profile" className="user-menu-item" onClick={() => setMenuOpen(false)}>
                <Icon name="user" size={15} className="" />
                {t("profile")}
              </NavLink>
              <button className="user-menu-item" onClick={switchLocale}>
                <Icon name="globe" size={15} />
                <span style={{ flex: 1 }}>{t("language")}</span>
                <span style={{ color: "var(--faint)", fontSize: 12 }}>
                  {user?.locale === "ru" ? "Русский" : "English"}
                </span>
              </button>
              <div className="user-menu-sep" />
              <button
                className="user-menu-item"
                style={{ color: "var(--danger)" }}
                onClick={() => {
                  void logout().then(() => navigate("/login"));
                }}
              >
                <Icon name="logout" size={15} />
                {t("signOut")}
              </button>
            </div>
          )}
          <div className="side-user" onClick={() => setMenuOpen((open) => !open)}>
            <Avatar text={initials(user?.name ?? "?")} src={user?.avatar_url} online />
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ color: "var(--text)", fontSize: 13, fontWeight: 500 }}>{user?.name}</div>
              <div style={{ color: "var(--faint)", fontSize: 11 }}>
                {user?.role === "root" ? t("root") : t("managerRole")}
              </div>
            </div>
            <Icon name="chevronsUpDown" size={14} />
          </div>
        </div>
      </div>
    </aside>
  );
}
