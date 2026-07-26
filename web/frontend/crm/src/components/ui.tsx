import { useEffect, useRef, type ReactNode } from "react";

import { useApp } from "../lib/app";
import { Icon } from "./Icon";

export function Chip({
  variant,
  children,
}: {
  variant?: "success" | "warning" | "accent" | "brand";
  children: ReactNode;
}) {
  return <span className={"chip" + (variant ? ` chip-${variant}` : "")}>{children}</span>;
}

export function Toggle({ on, onToggle }: { on: boolean; onToggle: () => void }) {
  return (
    <div
      className={"toggle-track" + (on ? " on" : "")}
      role="switch"
      aria-checked={on}
      onClick={onToggle}
    >
      <div className="toggle-knob" />
    </div>
  );
}

export function Avatar({
  text,
  large,
  src,
  online,
}: {
  text: string;
  large?: boolean;
  src?: string | null;
  /** undefined — не показывать индикатор; true/false — точка «в сети»/«не в сети». */
  online?: boolean;
}) {
  return (
    <div className={"avatar-wrap" + (large ? " avatar-wrap-lg" : "")}>
      <div className={"avatar" + (large ? " avatar-lg" : "")}>
        {src ? <img className="avatar-img" src={src} alt="" /> : text}
      </div>
      {online !== undefined && <span className={"avatar-dot" + (online ? " on" : "")} />}
    </div>
  );
}

export function Spinner() {
  return <div className="spinner" />;
}

export function ScreenLoading() {
  return (
    <div className="screen-loading">
      <Spinner />
    </div>
  );
}

export function EmptyState({ title, sub }: { title: string; sub?: string }) {
  return (
    <div className="empty-state">
      <div className="empty-title">{title}</div>
      {sub && <div className="empty-sub">{sub}</div>}
    </div>
  );
}

export function Modal({
  title,
  children,
  onClose,
}: {
  title?: string;
  children: ReactNode;
  onClose: () => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div
      className="modal-backdrop"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="modal" ref={ref}>
        {title && (
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
            <div style={{ fontSize: 15, fontWeight: 600 }}>{title}</div>
            <button className="btn-icon" style={{ width: 28, height: 28, border: "none" }} onClick={onClose}>
              <Icon name="x" size={14} />
            </button>
          </div>
        )}
        {children}
      </div>
    </div>
  );
}

export function ConfirmModal({
  text,
  confirmLabel,
  danger,
  onConfirm,
  onClose,
}: {
  text: string;
  confirmLabel: string;
  danger?: boolean;
  onConfirm: () => void;
  onClose: () => void;
}) {
  const { t } = useApp();
  return (
    <Modal onClose={onClose}>
      <div style={{ fontSize: 13.5, lineHeight: 1.6, marginBottom: 20 }}>{text}</div>
      <div style={{ display: "flex", gap: 10, justifyContent: "flex-end" }}>
        <button className="btn btn-secondary btn-sm" onClick={onClose}>
          {t("cancel")}
        </button>
        <button
          className="btn btn-primary btn-sm"
          style={danger ? { background: "var(--danger)", color: "#fff" } : undefined}
          onClick={() => {
            onConfirm();
            onClose();
          }}
        >
          {confirmLabel}
        </button>
      </div>
    </Modal>
  );
}

export function Toasts() {
  const { toasts } = useApp();
  if (toasts.length === 0) return null;
  return (
    <div className="toast-wrap">
      {toasts.map((toast) => (
        <div key={toast.id} className={"toast" + (toast.error ? " error" : "")}>
          <Icon name={toast.error ? "x" : "check"} size={14} />
          {toast.text}
        </div>
      ))}
    </div>
  );
}
