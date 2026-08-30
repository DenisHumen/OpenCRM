import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

import { copyText } from "../lib/clipboard";
import type { TFunc } from "../lib/i18n";
import { Icon } from "./Icon";

export type PunktMenyu = {
  key: string;
  label: string;
  icon?: string;
  /** Красный пункт: удаление и прочее необратимое. */
  opasno?: boolean;
  /** Черта над пунктом — отделить необратимое от обычного. */
  razdel?: boolean;
  run: () => void;
};

/** Пункты, годные любой записи с собственным адресом. Списки добавляют к ним своё. */
export function punktyDlyaZapisi(
  put: string,
  t: TFunc,
  navigate: (p: string) => void,
): PunktMenyu[] {
  return [
    { key: "open", label: t("open"), icon: "eye", run: () => navigate(put) },
    {
      key: "tab",
      label: t("openInNewTab"),
      icon: "external",
      run: () => window.open(put, "_blank", "noopener,noreferrer"),
    },
    {
      key: "link",
      label: t("copyLink"),
      icon: "link",
      run: () => void copyText(new URL(put, window.location.origin).href),
    },
  ];
}

type Sostoyanie = { x: number; y: number; punkty: PunktMenyu[] } | null;

export function useContextMenu() {
  const [menu, setMenu] = useState<Sostoyanie>(null);
  const zakryt = useCallback(() => setMenu(null), []);
  const otkryt = useCallback((e: React.MouseEvent, punkty: PunktMenyu[]) => {
    // Пустое меню не открываем: пользователю с урезанными правами системное меню
    // браузера полезнее нашей пустой карточки.
    if (!punkty.length) return;
    e.preventDefault();
    e.stopPropagation();
    setMenu({ x: e.clientX, y: e.clientY, punkty });
  }, []);
  return { menu, otkryt, zakryt };
}

export function ContextMenu({ menu, zakryt }: { menu: Sostoyanie; zakryt: () => void }) {
  const ref = useRef<HTMLDivElement>(null);
  const [gde, setGde] = useState({ x: 0, y: 0 });
  const [aktiv, setAktiv] = useState(0);

  useLayoutEffect(() => {
    if (!menu || !ref.current) return;
    const { width, height } = ref.current.getBoundingClientRect();
    // У края экрана меню прижимается, а не переворачивается вверх: переворот
    // уводит первый пункт из-под курсора, и щелчок попадает не туда.
    setGde({
      x: Math.max(8, Math.min(menu.x, window.innerWidth - width - 8)),
      y: Math.max(8, Math.min(menu.y, window.innerHeight - height - 8)),
    });
    setAktiv(0);
  }, [menu]);

  useEffect(() => {
    if (!menu) return;
    const klavisha = (e: KeyboardEvent) => {
      const n = menu.punkty.length;
      if (e.key === "Escape") zakryt();
      else if (e.key === "ArrowDown") setAktiv((i) => (i + 1) % n);
      else if (e.key === "ArrowUp") setAktiv((i) => (i - 1 + n) % n);
      else if (e.key === "Enter") {
        menu.punkty[aktiv]?.run();
        zakryt();
      } else return;
      e.preventDefault();
    };
    // Прокрутка и смена размера окна оставляют меню висеть на старом месте, уже не
    // над своей строкой: закрываем, а не пересчитываем.
    window.addEventListener("keydown", klavisha);
    window.addEventListener("scroll", zakryt, true);
    window.addEventListener("resize", zakryt);
    window.addEventListener("blur", zakryt);
    return () => {
      window.removeEventListener("keydown", klavisha);
      window.removeEventListener("scroll", zakryt, true);
      window.removeEventListener("resize", zakryt);
      window.removeEventListener("blur", zakryt);
    };
  }, [menu, aktiv, zakryt]);

  if (!menu) return null;

  // Через портал: колонки доски и таблицы режут по `overflow`, и меню внутри них
  // обрезалось бы по границе колонки.
  return createPortal(
    <div
      className="ctx-fon"
      onMouseDown={zakryt}
      onContextMenu={(e) => {
        e.preventDefault();
        zakryt();
      }}
    >
      <div
        ref={ref}
        className="ctx-menu"
        style={{ left: gde.x, top: gde.y }}
        onMouseDown={(e) => e.stopPropagation()}
      >
        {menu.punkty.map((p, i) => (
          <div key={p.key} className={p.razdel ? "ctx-group razdel" : "ctx-group"}>
            <button
              type="button"
              className={
                "ctx-item" + (p.opasno ? " opasno" : "") + (i === aktiv ? " active" : "")
              }
              onMouseMove={() => setAktiv(i)}
              onClick={() => {
                p.run();
                zakryt();
              }}
            >
              {p.icon && <Icon name={p.icon} size={15} />}
              <span>{p.label}</span>
            </button>
          </div>
        ))}
      </div>
    </div>,
    document.body,
  );
}
