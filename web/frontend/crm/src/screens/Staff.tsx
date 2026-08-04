import { useCallback, useEffect, useState } from "react";

import { Icon } from "../components/Icon";
import { Avatar, Chip, ConfirmModal, EmptyState, Modal, ScreenLoading } from "../components/ui";
import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { formatDateTime, initials } from "../lib/format";

export function Staff() {
  const { t, locale, user, toastError } = useApp();
  const [items, setItems] = useState<any[] | null>(null);
  const [tempPassword, setTempPassword] = useState<{ name: string; password: string } | null>(null);
  const [confirmDisable, setConfirmDisable] = useState<number | null>(null);
  const [confirmRole, setConfirmRole] = useState<{ id: number; name: string; role: "root" | "manager" } | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<{ id: number; name: string } | null>(null);

  const load = useCallback(() => {
    api.get("/staff").then((d) => setItems(d.items)).catch(toastError);
  }, [toastError]);

  useEffect(() => {
    load();
    // присутствие меняется со временем — периодически освежаем список
    const timer = window.setInterval(load, 60_000);
    return () => window.clearInterval(timer);
  }, [load]);

  if (!items) return <ScreenLoading />;

  const presence = (person: any) =>
    person.is_online
      ? t("online")
      : person.last_seen_at
        ? t("lastSeenAt", { t: formatDateTime(person.last_seen_at, locale) })
        : t("neverOnline");

  const pending = items.filter((u) => u.status === "pending");
  const active = items.filter((u) => u.status === "active");
  const disabled = items.filter((u) => u.status === "disabled");
  const rootCount = active.filter((u) => u.role === "root").length;

  const action = async (path: string) => {
    try {
      await api.post(path);
      load();
    } catch (e) {
      toastError(e);
    }
  };

  const changeRole = async (id: number, role: "root" | "manager") => {
    try {
      await api.post(`/staff/${id}/role`, { role });
      load();
    } catch (e) {
      toastError(e);
    }
  };

  const removeUser = async (id: number) => {
    try {
      await api.del(`/staff/${id}`);
      load();
    } catch (e) {
      toastError(e);
    }
  };

  return (
    <div className="page">
      <div style={{ marginBottom: 26 }}>
        <h1 className="page-title">{t("staff")}</h1>
        <div className="page-sub">{t("staffSub")}</div>
      </div>

      {pending.length > 0 && (
        <>
          <div style={{ display: "flex", alignItems: "baseline", gap: 8, marginBottom: 14 }}>
            <h2 className="section-title">{t("signupRequests")}</h2>
            <Chip variant="warning">{pending.length}</Chip>
          </div>
          <div className="list-card" style={{ marginBottom: 32 }}>
            {pending.map((person) => (
              <div key={person.id} className="list-row" style={{ height: 56 }}>
                <Avatar text={initials(person.name)} src={person.avatar_url} />
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div className="truncate" style={{ color: "var(--text)", fontSize: 13.5, fontWeight: 500 }}>{person.name}</div>
                  <div className="truncate" style={{ color: "var(--faint)", fontSize: 12 }}>
                    {person.email} · {t("requested")} {formatDateTime(person.created_at, locale)}
                  </div>
                </div>
                <button className="btn btn-primary btn-sm" onClick={() => void action(`/staff/${person.id}/approve`)}>
                  <Icon name="check" size={13} stroke={2} />
                  {t("approve")}
                </button>
                <button
                  className="btn btn-secondary btn-sm"
                  style={{ color: "var(--danger)" }}
                  onClick={() => void action(`/staff/${person.id}/reject`)}
                >
                  {t("reject")}
                </button>
              </div>
            ))}
          </div>
        </>
      )}

      <h2 className="section-title" style={{ marginBottom: 14 }}>
        {t("active")}
      </h2>
      <div className="list-card" style={{ marginBottom: 32 }}>
        {active.map((person) => {
          const isSelf = person.id === user?.id;
          const isRoot = person.role === "root";
          // последнего root снять/удалить нельзя — иначе некому управлять студией
          const isLastRoot = isRoot && rootCount <= 1;
          return (
            <div key={person.id} className="list-row hoverable">
              <Avatar text={initials(person.name)} src={person.avatar_url} online={person.is_online} />
              <div className="list-row-text">
                <div className="truncate" style={{ color: "var(--text)", fontSize: 13.5, fontWeight: 500 }}>{person.name}</div>
                <div className="truncate" style={{ color: "var(--faint)", fontSize: 12 }}>{person.email}</div>
              </div>
              <div
                style={{
                  fontSize: 12,
                  whiteSpace: "nowrap",
                  flexShrink: 0,
                  marginRight: 4,
                  color: person.is_online ? "var(--success)" : "var(--faint)",
                }}
              >
                {presence(person)}
              </div>
              <Chip variant={isRoot ? "brand" : undefined}>{isRoot ? t("root") : t("managerRole")}</Chip>
              {isSelf ? (
                <span style={{ fontSize: 12, color: "var(--faint)", marginLeft: 8 }}>{t("you")}</span>
              ) : (
                <div style={{ display: "flex", gap: 12, marginLeft: 8 }}>
                  {isRoot
                    ? !isLastRoot && (
                        <button
                          className="text-link"
                          onClick={() => setConfirmRole({ id: person.id, name: person.name, role: "manager" })}
                        >
                          {t("makeManager")}
                        </button>
                      )
                    : (
                        <button
                          className="text-link"
                          onClick={() => setConfirmRole({ id: person.id, name: person.name, role: "root" })}
                        >
                          {t("makeRoot")}
                        </button>
                      )}
                  {!isRoot && (
                    <>
                      <button
                        className="text-link"
                        onClick={async () => {
                          try {
                            const result = await api.post(`/staff/${person.id}/reset-password`);
                            setTempPassword({ name: person.name, password: result.temp_password });
                          } catch (e) {
                            toastError(e);
                          }
                        }}
                      >
                        {t("resetPassword")}
                      </button>
                      <button className="text-link danger" onClick={() => setConfirmDisable(person.id)}>
                        {t("deactivate")}
                      </button>
                    </>
                  )}
                  {!isLastRoot && (
                    <button
                      className="text-link danger"
                      onClick={() => setConfirmDelete({ id: person.id, name: person.name })}
                    >
                      {t("deletePermanently")}
                    </button>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>

      {disabled.length > 0 && (
        <>
          <h2 className="section-title" style={{ color: "var(--faint)", marginBottom: 14 }}>
            {t("deactivated")}
          </h2>
          <div className="list-card" style={{ opacity: 0.7 }}>
            {disabled.map((person) => (
              <div key={person.id} className="list-row">
                <Avatar text={initials(person.name)} src={person.avatar_url} />
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div className="truncate" style={{ color: "var(--muted)", fontSize: 13.5, fontWeight: 500 }}>{person.name}</div>
                  <div className="truncate" style={{ color: "var(--faint)", fontSize: 12 }}>{person.email}</div>
                </div>
                <div style={{ display: "flex", gap: 12 }}>
                  <button className="text-link" onClick={() => void action(`/staff/${person.id}/enable`)}>
                    {t("restore")}
                  </button>
                  <button
                    className="text-link danger"
                    onClick={() => setConfirmDelete({ id: person.id, name: person.name })}
                  >
                    {t("deletePermanently")}
                  </button>
                </div>
              </div>
            ))}
          </div>
        </>
      )}

      {items.length === 0 && <EmptyState title="—" />}

      {tempPassword && (
        <Modal title={t("resetPassword")} onClose={() => setTempPassword(null)}>
          <div style={{ fontSize: 13.5, marginBottom: 12 }}>{t("tempPasswordIs", { name: tempPassword.name })}</div>
          <div
            className="card"
            style={{ padding: "12px 16px", fontFamily: "monospace", fontSize: 16, letterSpacing: "0.05em", textAlign: "center", marginBottom: 12, userSelect: "all" }}
          >
            {tempPassword.password}
          </div>
          <div style={{ color: "var(--faint)", fontSize: 12.5 }}>{t("tempPasswordHint")}</div>
        </Modal>
      )}
      {confirmDisable !== null && (
        <ConfirmModal
          text={t("deactivateConfirm")}
          confirmLabel={t("deactivate")}
          danger
          onConfirm={() => void action(`/staff/${confirmDisable}/disable`)}
          onClose={() => setConfirmDisable(null)}
        />
      )}
      {confirmRole && (
        <ConfirmModal
          text={
            confirmRole.role === "root"
              ? t("makeRootConfirm", { name: confirmRole.name })
              : t("makeManagerConfirm", { name: confirmRole.name })
          }
          confirmLabel={confirmRole.role === "root" ? t("makeRoot") : t("makeManager")}
          danger={confirmRole.role === "root"}
          onConfirm={() => void changeRole(confirmRole.id, confirmRole.role)}
          onClose={() => setConfirmRole(null)}
        />
      )}
      {confirmDelete && (
        <ConfirmModal
          text={t("deleteUserConfirm", { name: confirmDelete.name })}
          confirmLabel={t("deletePermanently")}
          danger
          onConfirm={() => void removeUser(confirmDelete.id)}
          onClose={() => setConfirmDelete(null)}
        />
      )}
    </div>
  );
}
