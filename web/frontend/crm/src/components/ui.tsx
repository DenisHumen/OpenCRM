import { useEffect, useRef, type ReactNode } from "react";

import { ApiError } from "../lib/api";
import { useApp } from "../lib/app";
import { Icon } from "./Icon";

export function Chip({
  variant,
  title,
  children,
}: {
  variant?: "success" | "warning" | "accent" | "brand";
  /** Подсказка при наведении: почему метка именно такая. */
  title?: string;
  children: ReactNode;
}) {
  return (
    <span className={"chip" + (variant ? ` chip-${variant}` : "")} title={title}>
      {children}
    </span>
  );
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
  small,
  src,
  online,
}: {
  text: string;
  large?: boolean;
  /** Для плотных мест — карточка канбана, строка списка. */
  small?: boolean;
  src?: string | null;
  /** undefined — не показывать индикатор; true/false — точка «в сети»/«не в сети». */
  online?: boolean;
}) {
  const size = large ? " avatar-lg" : small ? " avatar-sm" : "";
  return (
    <div className={"avatar-wrap" + (large ? " avatar-wrap-lg" : "")}>
      <div className={"avatar" + size}>
        {src ? <img className="avatar-img" src={src} alt="" /> : text}
      </div>
      {online !== undefined && <span className={"avatar-dot" + (online ? " on" : "")} />}
    </div>
  );
}

export function Spinner() {
  return <div className="spinner" />;
}

/**
 * Место экрана, пока данных нет.
 *
 * Вертушка честна ровно до первого отказа сервера. Дальше она врёт: загрузка
 * кончилась, а экран продолжает обещать, что вот-вот покажет. Сообщение об
 * ошибке живёт четыре секунды и уходит — и человек остаётся один на один с
 * бесконечным кружком, из которого не следует ни что случилось, ни что делать.
 *
 * Поэтому решение «вертушка или отказ» принимается здесь, в единственном месте,
 * где экран и так уже выбирает, что показать вместо данных. Экрану остаётся
 * передать, чем кончилась загрузка, и чем её повторить.
 *
 * Сообщение берём от сервера, если он его прислал: «Раздел выключен» и «нет
 * связи» — разные беды, и на вторую есть смысл нажать «ещё раз», а на первую
 * нет. Своё общее объяснение — только когда сервер промолчал.
 */
export function ScreenLoading({ error, onRetry }: { error?: unknown; onRetry?: () => void }) {
  const { t } = useApp();

  if (error === undefined || error === null) {
    return (
      <div className="screen-loading">
        <Spinner />
      </div>
    );
  }

  return (
    <div className="screen-loading">
      <div className="screen-failed">
        <span className="screen-failed-icon">
          <Icon name="alert" size={20} />
        </span>
        <div className="empty-title">{t("loadFailed")}</div>
        <div className="empty-sub">
          {error instanceof ApiError ? error.message : t("loadFailedHint")}
        </div>
        {onRetry && (
          <button className="btn btn-secondary" onClick={onRetry} style={{ marginTop: 14 }}>
            {t("retry")}
          </button>
        )}
      </div>
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
  wide,
}: {
  title?: string;
  children: ReactNode;
  onClose: () => void;
  /** редактору обрезки нужна ширина: рядом стоят карта работы и превью */
  wide?: boolean;
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
      <div className={"modal" + (wide ? " modal-wide" : "")} ref={ref}>
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
