import { useEffect, useRef, useState, type ReactNode } from "react";

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

/**
 * Переключатель «включено/выключено».
 *
 * Настоящая кнопка, а не `div` с обработчиком: `div` не попадает под Tab и не
 * отвечает на пробел, и из-за этого ни один блок системы нельзя было включить
 * без мыши — экран блоков состоит из одних таких переключателей.
 *
 * Всплытие останавливаем: переключатель почти всегда стоит внутри строки,
 * которая ловит нажатие сама (строка блока, строка настройки). Без остановки
 * одно нажатие срабатывало бы дважды — туда и обратно.
 *
 * `label` обязателен там, где рядом нет текста, привязанного к кнопке: без
 * подписи с клавиатуры слышно «переключатель, включён» и ни слова о том, чего
 * именно он касается.
 */
export function Toggle({
  on,
  onToggle,
  label,
  disabled,
}: {
  on: boolean;
  onToggle: () => void;
  label?: string;
  /** Переключатель, который не поддаётся: несущий блок, ещё не готовый блок. */
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      className={"toggle-track" + (on ? " on" : "")}
      role="switch"
      aria-checked={on}
      aria-label={label}
      disabled={disabled}
      onClick={(e) => {
        e.stopPropagation();
        onToggle();
      }}
    >
      <span className="toggle-knob" />
    </button>
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
  // Картинка может не приехать: файл потеряли при переезде, у собеседника в
  // телеграме аватар закрыт настройками приватности, ссылка ответила 404.
  // Раньше на этом месте оставался пустой кружок (или значок битой картинки), а
  // инициалы — единственное, по чему человека узнают в списке.
  const [upalo, setUpalo] = useState(false);
  useEffect(() => setUpalo(false), [src]);
  return (
    <div className={"avatar-wrap" + (large ? " avatar-wrap-lg" : "")}>
      <div className={"avatar" + size}>
        {src && !upalo ? (
          <img className="avatar-img" src={src} alt="" onError={() => setUpalo(true)} />
        ) : (
          text
        )}
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

/**
 * Врезка не загрузилась — строкой, а не вместо экрана.
 *
 * `ScreenLoading` занимает страницу целиком и годится там, где без данных
 * показывать нечего. Врезка живёт внутри карточки рядом с другими: лента
 * заявки не приехала — это не повод прятать саму заявку.
 *
 * Нужна строка ровно затем же, зачем `ScreenLoading`: отличить «пусто» от «не
 * смогли», назвать причину словами сервера и дать повторить. Пока разницы не
 * было, отказ показывался как «Пока пусто» — то есть как ответ сервера, хотя
 * ответа не было вовсе.
 */
export function LoadFailed({ error, onRetry }: { error: unknown; onRetry: () => void }) {
  const { t } = useApp();
  return (
    <div className="load-failed">
      <Icon name="alert" size={13} />
      <span className="load-failed-text">
        {error instanceof ApiError ? error.message : t("loadFailedHint")}
      </span>
      <button type="button" className="text-link" onClick={onRetry}>
        {t("retry")}
      </button>
    </div>
  );
}

/** «Показать ещё» с числом показанного и всего.
 *
 * Число в подписи — не украшение. Без него человек не знает, дочитал он до
 * конца или список просто закончился на круглом месте; а именно этим и была
 * прежняя беда — сотня записей выглядела как весь список.
 */
export function Dochitat({
  pokazano,
  vsego,
  zanyat,
  onClick,
}: {
  pokazano: number;
  vsego: number;
  zanyat?: boolean;
  onClick: () => void;
}) {
  const { t } = useApp();
  if (pokazano >= vsego) return null;
  return (
    <button type="button" className="dochitat" disabled={zanyat} onClick={onClick}>
      {t("showMore")} ({pokazano} / {vsego})
    </button>
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

/** Что в окне можно взять фокусом. Отключённые кнопки отсеиваем: Tab их
 *  пропускает, а в списке остановок они сдвигали бы край на несуществующий. */
const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]),' +
  ' textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

export function Modal({
  title,
  label,
  children,
  onClose,
  wide,
}: {
  title?: string;
  /** Имя окна, когда видимого заголовка нет (подтверждения). */
  label?: string;
  children: ReactNode;
  onClose: () => void;
  /** редактору обрезки нужна ширина: рядом стоят карта работы и превью */
  wide?: boolean;
}) {
  const { t } = useApp();
  const panel = useRef<HTMLDivElement>(null);
  // Кто открыл окно. Запоминаем прямо на первом рендере, до того как autoFocus
  // внутри окна перетянет фокус на себя: иначе «вернуть, где был» вернуло бы
  // в поле того же окна, которого через мгновение не станет.
  const opener = useRef<HTMLElement | null>(document.activeElement as HTMLElement | null);

  useEffect(() => {
    const box = panel.current;
    // Фокус внутрь окна. Пока он оставался снаружи, Tab уводил по ссылкам
    // страницы ПОД окном — по тем самым, что закрыты затемнением и по которым
    // мышью не попасть. Поле с autoFocus не трогаем: оно уже там, где надо.
    if (box && !box.contains(document.activeElement)) {
      (box.querySelector<HTMLElement>(FOCUSABLE) ?? box).focus();
    }
    // Закрыли — курсор возвращается туда, откуда открывали. Без этого он
    // оказывается в начале страницы, и путь до кнопки приходится проходить
    // заново.
    return () => opener.current?.focus?.();
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        onClose();
        return;
      }
      const box = panel.current;
      if (e.key !== "Tab" || !box) return;
      const stops = box.querySelectorAll<HTMLElement>(FOCUSABLE);
      if (stops.length === 0) return;
      const first = stops[0];
      const last = stops[stops.length - 1];
      const active = document.activeElement;
      // На краю списка Tab ушёл бы за пределы окна — заворачиваем по кругу.
      if (!box.contains(active)) {
        e.preventDefault();
        (e.shiftKey ? last : first).focus();
      } else if (e.shiftKey && active === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && active === last) {
        e.preventDefault();
        first.focus();
      }
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
      <div
        className={"modal" + (wide ? " modal-wide" : "")}
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-label={title ?? label}
        tabIndex={-1}
      >
        {title && (
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
            <div style={{ fontSize: 15, fontWeight: 600 }}>{title}</div>
            <button
              className="btn-icon"
              style={{ width: 28, height: 28, border: "none" }}
              aria-label={t("close")}
              onClick={onClose}
            >
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
    // Видимого заголовка у подтверждения нет намеренно — весь его смысл в одной
    // фразе ниже. Но имя окну всё равно нужно: без него читалка объявляет
    // «диалог» и ничего больше.
    <Modal label={t("confirm")} onClose={onClose}>
      <div style={{ fontSize: 13.5, lineHeight: 1.6, marginBottom: 20 }}>{text}</div>
      <div style={{ display: "flex", gap: 10, justifyContent: "flex-end" }}>
        <button className="btn btn-secondary btn-sm" onClick={onClose}>
          {t("cancel")}
        </button>
        <button
          className="btn btn-primary btn-sm"
          style={
            danger
              ? // Заливкой — `--danger-solid`, а не текстовый `--danger`: под
                // белым он в тёмной теме даёт 3.2:1. Разбор — у токена.
                { background: "var(--danger-solid)", color: "var(--on-danger)" }
              : undefined
          }
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
