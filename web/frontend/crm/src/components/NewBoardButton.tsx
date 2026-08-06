import { useState } from "react";
import { useNavigate } from "react-router-dom";

import { api } from "../lib/api";
import { useApp } from "../lib/app";
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
  const [busy, setBusy] = useState(false);

  const create = async () => {
    // Второе нажатие по неответившей кнопке заводило вторую доску: человек не
    // видел отклика и жал ещё раз, а получал два черновика.
    if (busy) return;
    setBusy(true);
    try {
      const board = await api.post("/boards", {
        title: t("newBoard"),
        ...(clientId ? { client_id: clientId } : {}),
      });
      navigate(`/boards/${board.id}`);
    } catch (e) {
      toastError(e);
      setBusy(false);
    }
  };

  return (
    <button className="btn btn-primary" onClick={() => void create()} disabled={busy}>
      <Icon name="plus" stroke={2} />
      {t("newBoard")}
    </button>
  );
}
