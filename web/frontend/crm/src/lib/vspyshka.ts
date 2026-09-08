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

/** То же, но с меткой: какой именно строке в списке зажгли отметку.
 *
 * Список кнопок иначе держал бы по крючку на строку, а строк столько, сколько
 * ключей на экране. Собственная пара «отметка + пауза ввода» здесь уже была и
 * ошиблась: отставшее значение гасило свежее нажатие, и подтверждения не было
 * вовсе, если нажать ту же кнопку второй раз сразу после того, как оно погасло.
 */
export function useVspyshkaNa<T>(ms: number = VSPYSHKA_MS) {
  const [gorit, setGorit] = useState<T | null>(null);
  const chasy = useRef<number | null>(null);

  useEffect(() => () => window.clearTimeout(chasy.current ?? undefined), []);

  const zazhech = useCallback(
    (chto: T) => {
      setGorit(chto);
      window.clearTimeout(chasy.current ?? undefined);
      chasy.current = window.setTimeout(() => setGorit(null), ms);
    },
    [ms],
  );

  return [gorit, zazhech] as const;
}
