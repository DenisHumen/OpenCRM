import { useNavigate } from "react-router-dom";

import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { useGuard } from "../lib/guard";
import { Icon } from "./Icon";

/**
 * «Новая доска» — кнопка, которая заводит доску и сразу открывает её.
 *
 * Три экрана держали эту кнопку своей копией, и копии успели разойтись: с
 * карточки клиента доска заводилась привязанной к нему, с дашборда и со списка
 * — ничьей. Разница законная, но она одна; всё остальное было повторено слово в
 * слово, включая обработку отказа.
 *
 * Пустой доски пугаться не надо: доска без работ — это черновик, а не мусор,
 * и заводят её именно затем, чтобы тут же перетащить в неё файлы. Поэтому
 * никакой формы перед созданием нет — название правится уже внутри.
 */
export function NewBoardButton({ clientId }: { clientId?: number }) {
  const { t, toastError } = useApp();
  const navigate = useNavigate();
  const guard = useGuard();

  const create = async () => {
    // Второе нажатие по неответившей кнопке заводило вторую доску: человек не
    // видел отклика и жал ещё раз, а получал два черновика.
    //
    // Засов, а не состояние: `setBusy(true)` виден только со следующего
    // рендера, а два нажатия в одном тике читают `busy` из своих замыканий и
    // оба видят `false` (подробности — в lib/guard.ts). Отпускать нечего:
    // при успехе экран уезжает в созданную доску.
    if (!guard.take()) return;
    try {
      const board = await api.post("/boards", {
        title: t("newBoard"),
        ...(clientId ? { client_id: clientId } : {}),
      });
      navigate(`/boards/${board.id}`);
    } catch (e) {
      toastError(e);
      guard.free();
    }
  };

  return (
    <button className="btn btn-primary" onClick={() => void create()} disabled={guard.busy}>
      <Icon name="plus" stroke={2} />
      {t("newBoard")}
    </button>
  );
}
