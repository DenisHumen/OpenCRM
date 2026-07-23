import { useCallback, useEffect, useState } from "react";

import { Icon } from "../components/Icon";
import { Avatar, Chip, ConfirmModal, EmptyState, Modal, ScreenLoading } from "../components/ui";
import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { formatDate, formatDateTime, initials } from "../lib/format";

export function Staff() {
  const { t, locale, user, toastError } = useApp();
  const [items, setItems] = useState<any[] | null>(null);
  const [tempPassword, setTempPassword] = useState<{ name: string; password: string } | null>(null);
  const [confirmDisable, setConfirmDisable] = useState<number | null>(null);

  const load = useCallback(() => {
    api.get("/staff").then((d) => setItems(d.items)).catch(toastError);
  }, [toastError]);

  useEffect(() => {
    load();
  }, [load]);

  if (!items) return <ScreenLoading />;

  const pending = items.filter((u) => u.status === "pending");
  const active = items.filter((u) => u.status === "active");
  const disabled = items.filter((u) => u.status === "disabled");

  const action = async (path: string) => {
    try {
      await api.post(path);
      load();
    } catch (e) {
      toastError(e);
    }
  };

  return (
    <div className="page">
      <div style={{ marginBottom: 32 }}>
        <h1 className="page-title">{t("staff")}</h1>
        <div className="page-sub">{t("staffSub")}</div>
      </div>

      {pending.length > 0 && (
        <>
          <div style={{ display: "flex", alignItems: "baseline", gap: 8, marginBottom: 14 }}>
            <h2 className="section-title">{t("signupRequests")}</h2>
            <Chip variant="warning">{pending.length}</Chip>
          </div>
          <div className="list-card" style={{ marginBottom: 40 }}>
            {pending.map((person) => (
              <div key={person.id} className="list-row" style={{ height: 60 }}>
                <Avatar text={initials(person.name)} />
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ color: "var(--text)", fontSize: 13.5, fontWeight: 500 }}>{person.name}</div>
                  <div style={{ color: "var(--faint)", fontSize: 12 }}>
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
      <div className="list-card" style={{ marginBottom: 40 }}>
        {active.map((person) => (
          <div key={person.id} className="list-row hoverable">
            <Avatar text={initials(person.name)} />
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ color: "var(--text)", fontSize: 13.5, fontWeight: 500 }}>{person.name}</div>
              <div style={{ color: "var(--faint)", fontSize: 12 }}>{person.email}</div>
            </div>
            <Chip variant={person.role === "root" ? "brand" : undefined}>
              {person.role === "root" ? t("root") : t("managerRole")}
            </Chip>
            {person.role !== "root" && (
              <div style={{ display: "flex", gap: 12, marginLeft: 8 }}>
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
              </div>
            )}
          </div>
        ))}
      </div>

      {disabled.length > 0 && (
        <>
          <h2 className="section-title" style={{ color: "var(--faint)", marginBottom: 14 }}>
            {t("deactivated")}
          </h2>
          <div className="list-card" style={{ opacity: 0.7 }}>
            {disabled.map((person) => (
              <div key={person.id} className="list-row">
                <Avatar text={initials(person.name)} />
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ color: "var(--muted)", fontSize: 13.5, fontWeight: 500 }}>{person.name}</div>
                  <div style={{ color: "var(--faint)", fontSize: 12 }}>{person.email}</div>
                </div>
                <button className="text-link" onClick={() => void action(`/staff/${person.id}/enable`)}>
                  {t("restore")}
                </button>
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
    </div>
  );
}
