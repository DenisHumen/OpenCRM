import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";

import { BoardCard } from "../components/BoardCard";
import { Icon } from "../components/Icon";
import { NewBoardButton } from "../components/NewBoardButton";
import { Dochitat, EmptyState, ScreenLoading } from "../components/ui";
import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { useLiveTopic } from "../lib/live";
import { useFailure } from "../lib/failure";

type Filter = "all" | "published" | "drafts" | "revoked";

/** По скольку досок дочитывается список. */
const NA_STRANITSE = 100;

export function Boards() {
  const { t, toastError } = useApp();
  const navigate = useNavigate();
  const [data, setData] = useState<any>(null);
  const [filter, setFilter] = useState<Filter>("all");

  const { failure, fail, clear } = useFailure();

  // До какой страницы дочитан список. Прежде бралась сотня досок и на этом
  // всё; отбор «опубликованные» при этом искал ТОЛЬКО среди них, то есть
  // сто первая опубликованная доска не находилась ни фильтром, ни глазами.
  const [stranitsa, setStranitsa] = useState(1);
  const [dochityvaem, setDochityvaem] = useState(false);
  // Поколение показанного списка. Отбора у этого экрана нет, зато список
  // перечитывается целиком — повтором после отказа, а на доске заявок ещё
  // и после переноса карточки. Дочитка, ушедшая до перечитывания, вернулась
  // бы со страницей прошлого списка и дописала бы её к новому.
  const pokolenie = useRef(0);

  const load = useCallback(() => {
    clear();
    pokolenie.current += 1;
    api
      .get<{ items: unknown[]; total: number }>(`/boards?page=1&per_page=${NA_STRANITSE}`)
      .then((otvet) => {
        setData(otvet);
        setStranitsa(1);
      })
      .catch((beda) => {
        fail(beda);
      });
  }, [fail, clear]);

  useEffect(load, [load]);
  useLiveTopic("boards", load);

  /** Дочитать список.
   *
   * Отдельным действием, а не номером страницы в пути загрузки, и номер
   * растёт ПОСЛЕ удачного ответа. Иначе отказ на второй странице оставлял бы
   * счётчик на двойке, а следующее нажатие просило бы третью — вторая сотня
   * досок пропадала бы из списка навсегда и молча, а отбор ищет только по
   * загруженному.
   *
   * Отказ говорит о себе всплывающей жалобой, а не через `fail`: `fail`
   * рисует экран «не удалось загрузить», а он виден только пока показывать
   * нечего. После первой удачной загрузки отказ дочитки не показал бы ничего
   * вовсе — кнопка просто переставала бы отвечать.
   */
  const dochitat = async () => {
    if (dochityvaem) return;
    setDochityvaem(true);
    const bylo_pokolenie = pokolenie.current;
    try {
      const dalshe = await api.get<{ items: unknown[]; total: number }>(
        `/boards?page=${stranitsa + 1}&per_page=${NA_STRANITSE}`,
      );
      // Список перечитали, пока страница ехала, — ответ от прошлого.
      if (pokolenie.current !== bylo_pokolenie) return;
      setData((bylo: any) =>
        bylo ? { ...dalshe, items: [...bylo.items, ...dalshe.items] } : dalshe,
      );
      setStranitsa((bylo) => bylo + 1);
    } catch (beda) {
      toastError(beda);
    } finally {
      setDochityvaem(false);
    }
  };

  if (!data) return <ScreenLoading error={failure} onRetry={load} />;

  const published = data.items.filter((b: any) => b.is_published);
  const filtered = data.items.filter((board: any) => {
    if (filter === "published") return board.is_published;
    if (filter === "drafts") return !board.is_published;
    if (filter === "revoked") return board.has_links && !board.has_active_link;
    return true;
  });

  const filters: { id: Filter; label: string }[] = [
    { id: "all", label: t("all") },
    { id: "published", label: t("published") },
    { id: "drafts", label: t("draft") },
    { id: "revoked", label: t("revoked") },
  ];

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1 className="page-title">{t("boards")}</h1>
          <div className="page-sub">{t("boardsSub", { total: data.total, published: published.length })}</div>
        </div>
        <NewBoardButton />
      </div>

      <div style={{ display: "flex", gap: 6, marginBottom: 20 }}>
        {filters.map((item) => (
          <button
            key={item.id}
            className={"filter-chip" + (filter === item.id ? " active" : "")}
            onClick={() => setFilter(item.id)}
          >
            {item.label}
          </button>
        ))}
      </div>

      {filtered.length === 0 ? (
        <div className="card">
          <EmptyState icon="boards" title={t("noBoardsYet")} />
        </div>
      ) : (
        <div className="board-grid">
          {filtered.map((board: any) => (
            <BoardCard key={board.id} board={board} />
          ))}
        </div>
      )}

      {/* Счётчик показывает дочитанное ко всему, а не отобранное: отбор
          работает по загруженному, и «3 из 240» рядом с тремя досками
          сбивало бы с толку. */}
      <Dochitat
        pokazano={data.items.length}
        vsego={data.total}
        zanyat={dochityvaem}
        onClick={() => void dochitat()}
      />
    </div>
  );
}
