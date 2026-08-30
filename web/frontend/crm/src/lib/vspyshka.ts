import { useCallback, useEffect, useRef, useState } from "react";

/** Сколько держится короткая отметка «получилось». */
export const VSPYSHKA_MS = 1600;

/** Отметка, которая сама гаснет: галочка на кнопке, «скопировано» и подобное.
 *
 * Пауза живёт здесь по той же причине, что и пауза ввода в `debounce.ts`: копия
 * числа у каждого экрана однажды разойдётся с остальными.
 */
export function useVspyshka(ms: number = VSPYSHKA_MS) {
  const [gorit, setGorit] = useState(false);
  const chasy = useRef<number | null>(null);

  useEffect(() => () => window.clearTimeout(chasy.current ?? undefined), []);

  const zazhech = useCallback(() => {
    setGorit(true);
    window.clearTimeout(chasy.current ?? undefined);
    chasy.current = window.setTimeout(() => setGorit(false), ms);
  }, [ms]);

  return [gorit, zazhech] as const;
}
