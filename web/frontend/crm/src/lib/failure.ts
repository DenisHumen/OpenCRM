import { useCallback, useState } from "react";

import { useApp } from "./app";

/**
 * Чем кончилась загрузка экрана.
 *
 * `null` — идёт или удалась; иначе — то, чем сервер отказал. Экран передаёт это
 * в `ScreenLoading`, и тот вместо вечной вертушки показывает, что случилось, и
 * кнопку «ещё раз».
 *
 * Отдельный крючок, а не флаг в каждом экране, по одной причине: сообщить
 * человеку и запомнить отказ — это одно действие, и разъезжаться им нельзя.
 * Пока `toastError` вызывали руками, половина экранов сообщала и забывала:
 * всплывающая подсказка уходила через четыре секунды, а экран оставался
 * крутиться. Здесь забыть нечего — `fail` делает и то и другое.
 *
 * `clear` вызывается в начале загрузки, а не по нажатию «ещё раз»: повтор идёт
 * тем же путём, что и первый заход, и вертушка возвращается сама.
 */
export function useFailure() {
  const { toastError } = useApp();
  const [failure, setFailure] = useState<unknown>(null);

  const fail = useCallback(
    (e: unknown) => {
      toastError(e);
      setFailure(e);
    },
    [toastError],
  );

  const clear = useCallback(() => setFailure(null), []);

  return { failure, fail, clear };
}
