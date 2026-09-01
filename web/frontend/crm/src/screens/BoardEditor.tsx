import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { Icon } from "../components/Icon";
import { SkachatFayl } from "../components/SkachatFayl";
import { VyborKlienta } from "../components/VyborKlienta";
import { Chip, ConfirmModal, LoadFailed, Modal, ScreenLoading, Toggle } from "../components/ui";
import { api, ApiError } from "../lib/api";
import { dropTarget } from "../lib/dnd";
import { useApp } from "../lib/app";
import { useFailure } from "../lib/failure";
import { useGuard } from "../lib/guard";
import { copyText } from "../lib/clipboard";
import { fileSize, formatDateTime, formatDuration } from "../lib/format";
import { useReference } from "../lib/reference";

/**
 * Остаток времени коротко: «12 с», «3 мин», «1 ч 20 мин».
 *
 * Округляем ВВЕРХ и грубо. Точность здесь не нужна и даже вредна: «осталось
 * 47 секунд», сменяющееся на «осталось 52 секунды», читается как поломка, а не
 * как уточнение. Человеку нужен порядок величины — ждать ли рядом или уйти за
 * чаем.
 */
function srok(sekund: number): string {
  if (sekund < 60) return `${Math.ceil(sekund)} s`;
  if (sekund < 3600) return `${Math.ceil(sekund / 60)} min`;
  const chasov = Math.floor(sekund / 3600);
  const minut = Math.round((sekund % 3600) / 60);
  return minut ? `${chasov} h ${minut} min` : `${chasov} h`;
}

/** Файл на пути к серверу: сколько ушло, как быстро и сколько ещё ждать. */
interface Zaliv {
  klyuch: string;
  imya: string;
  vsego: number;
  ushlo: number;
  /** Байт в секунду по последнему отрезку. Ноль — считать ещё не по чему. */
  skorost: number;
  /** Секунд до конца. `null` — скорости пока нет, и врать числом не будем. */
  ostalos: number | null;
  /** Пошли байты или файл ещё ждёт очереди. */
  idyot: boolean;
  otmenit: (() => void) | null;
}

export function BoardEditor() {
  const { id } = useParams();
  const { t, locale, toast, toastError } = useApp();
  const navigate = useNavigate();
  const [board, setBoard] = useState<any>(null);
  const [confirm, setConfirm] = useState<null | "regenerate" | "deleteBoard" | "deleteShare" | number>(null);
  const [copied, setCopied] = useState(false);
  const guard = useGuard();
  const [expiryOpen, setExpiryOpen] = useState(false);
  const [pinOpen, setPinOpen] = useState(false);
  const [pinDraft, setPinDraft] = useState("");
  const [dragId, setDragId] = useState<number | null>(null);
  //: Файлы, которые сейчас едут на сервер. Живут только на экране: сервер о них
  //: ещё не знает, и после ответа строка исчезает, уступая настоящей карточке.
  const [zalivki, setZalivki] = useState<Zaliv[]>([]);
  const [linkWork, setLinkWork] = useState<any>(null);
  const [cropWork, setCropWork] = useState<any>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const pollTimer = useRef<number>();

  const { failure, fail, clear } = useFailure();

  const load = useCallback(async () => {
    clear();
    try {
      setBoard(await api.get(`/boards/${id}`));
    } catch (e) {
      // Записи нет или она не наша: показывать «попробуйте ещё раз» тут не о
      // чем — повтор вернёт тот же ответ. Возвращаемся в список, как и раньше.
      if (e instanceof ApiError && (e.status === 404 || e.status === 403)) {
        toastError(e);
        navigate("/boards");
        return;
      }
      // Всё остальное — беда связи или сервера. Карточку не бросаем: адрес в
      // строке верный, и повторить имеет смысл именно его, а не список.
      fail(e);
    }
  }, [id, toastError, navigate, fail, clear]);

  useEffect(() => {
    void load();
  }, [load]);

  // Справочники правой колонки — через общий крючок. Отказ здесь не должен
  // сводиться к пустому списку: выбор клиента без клиентов выглядит как
  // «клиентов нет», а подпись «для клиента» над доской просто исчезает.
  // Заявки выбранного клиента. Показывать чужие в этом окне незачем: доска
  // делается по конкретному заказу конкретного человека. Список
  // перезапрашивается при смене клиента — иначе доску привяжут к чужому заказу.
  const clientDeals = useReference<any>(
    board?.client_id ? `/deals?client_id=${board.client_id}&per_page=100` : null,
  );

  // поллинг обрабатывающихся работ
  useEffect(() => {
    const processing = board?.works?.filter((w: any) => w.status === "processing") ?? [];
    if (processing.length === 0) return;
    pollTimer.current = window.setTimeout(async () => {
      let settled = false;
      for (const work of processing) {
        try {
          const fresh = await api.get(`/boards/${id}/works/${work.id}`);
          if (fresh.status !== "processing") settled = true;
        } catch {
          /* повторим на следующем тике */
        }
      }
      // готовая работа занимает место в композиции, а значит форма мест у всех
      // соседей меняется — их считает выдача доски, поэтому перезапрашиваем её
      if (settled) await load();
      else setBoard((prev: any) => ({ ...prev })); // перезапуск эффекта
    }, 1500);
    return () => window.clearTimeout(pollTimer.current);
  }, [board, id, load]);

  if (!board) return <ScreenLoading error={failure} onRetry={() => void load()} />;

  const share = board.shares.find((s: any) => s.is_active) ?? board.shares[0] ?? null;

  const patchBoard = async (patch: any) => {
    try {
      const updated = await api.patch(`/boards/${id}`, patch);
      setBoard((prev: any) => ({ ...prev, ...updated }));
    } catch (e) {
      toastError(e);
    }
  };

  const patchShare = async (patch: any) => {
    if (!share) return;
    try {
      const updated = await api.patch(`/shares/${share.id}`, patch);
      setBoard((prev: any) => ({
        ...prev,
        shares: prev.shares.map((s: any) => (s.id === updated.id ? updated : s)),
      }));
    } catch (e) {
      toastError(e);
    }
  };

  const createShare = async () => {
    // Второе нажатие по неответившей кнопке заводило вторую ссылку на ту же
    // доску: клиенту уходила одна, а отзывали потом другую — и доска
    // оставалась открытой по забытому адресу.
    if (!guard.take()) return;
    try {
      const link = await api.post(`/boards/${id}/shares`, {});
      setBoard((prev: any) => ({ ...prev, shares: [link, ...prev.shares] }));
    } catch (e) {
      toastError(e);
    } finally {
      guard.free();
    }
  };

  const regenerate = async () => {
    if (!share) return;
    try {
      const link = await api.post(`/shares/${share.id}/regenerate`);
      await load();
      // не получилось скопировать — показываем адрес, иначе новая ссылка
      // потерялась бы: старая уже недействительна
      toast((await copyText(link.url)) ? t("copied") : link.url);
    } catch (e) {
      toastError(e);
    }
  };

  const copyLink = async () => {
    if (!share) return;
    if (await copyText(share.url)) {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } else {
      toast(share.url);
    }
  };

  const uploadFiles = async (list: FileList | null) => {
    if (!list) return;
    const fayly = Array.from(list);
    if (fayly.length === 0) return;

    // Заглушки появляются СРАЗУ и на все файлы, ещё до первого байта.
    //
    // Это и есть главная правка. Прежде между «выбрал файл» и «карточка
    // появилась» не происходило ничего видимого: на большом файле или медленном
    // канале человек не знал, идёт ли загрузка вообще, и жал ещё раз. Пустое
    // место — худший из возможных ответов, потому что читается как «ничего не
    // случилось».
    const novye: Zaliv[] = fayly.map((file, nomer) => ({
      klyuch: `${Date.now()}-${nomer}-${file.name}`,
      imya: file.name,
      vsego: file.size,
      ushlo: 0,
      skorost: 0,
      ostalos: null,
      idyot: false,
      otmenit: null,
    }));
    setZalivki((bylo) => [...bylo, ...novye]);

    // По одному, а не разом: параллельная заливка десяти файлов делит канал на
    // десять и каждый ползёт вдесятеро дольше. Ждущие показаны «в очереди» —
    // это честный ответ, а не застывшая на нуле полоса.
    for (let nomer = 0; nomer < fayly.length; nomer++) {
      const file = fayly[nomer];
      const klyuch = novye[nomer].klyuch;
      // Скорость считаем по ПОСЛЕДНЕМУ отрезку, а не по среднему с начала:
      // среднее помнит медленный разгон и до конца врёт про остаток.
      let bylo_vremya = performance.now();
      let bylo_ushlo = 0;

      try {
        const zalivka = api.zagruzka<any>(`/boards/${id}/works`, file, ({ ushlo, vsego }) => {
          const teper = performance.now();
          const proshlo = (teper - bylo_vremya) / 1000;
          setZalivki((spisok) =>
            spisok.map((z) => {
              if (z.klyuch !== klyuch) return z;
              // Отрезки короче четверти секунды пропускаем: на них скорость
              // скачет от сотни килобайт до десятков мегабайт, и число под
              // полосой мельтешит так, что читать его нельзя.
              if (proshlo < 0.25) return { ...z, ushlo, vsego, idyot: true };
              const skorost = (ushlo - bylo_ushlo) / proshlo;
              const ostatok = vsego - ushlo;
              return {
                ...z,
                ushlo,
                vsego,
                idyot: true,
                skorost,
                ostalos: skorost > 0 ? ostatok / skorost : null,
              };
            }),
          );
          if (proshlo >= 0.25) {
            bylo_vremya = teper;
            bylo_ushlo = ushlo;
          }
        });

        setZalivki((spisok) =>
          spisok.map((z) => (z.klyuch === klyuch ? { ...z, otmenit: zalivka.otmenit } : z)),
        );

        const work = await zalivka.gotovo;
        setBoard((prev: any) => ({
          ...prev,
          works: [...prev.works, work],
          works_count: prev.works_count + 1,
        }));
      } catch (e) {
        // Отменил сам — не беда, и ругаться незачем.
        if ((e as any)?.code !== "canceled") toastError(e);
      } finally {
        setZalivki((spisok) => spisok.filter((z) => z.klyuch !== klyuch));
      }
    }
  };

  const deleteWork = async (workId: number) => {
    try {
      await api.del(`/boards/${id}/works/${workId}`);
      // мест в композиции стало меньше — их форму пересчитывает сервер
      await load();
    } catch (e) {
      toastError(e);
    }
  };

  const reorder = async (sourceId: number, targetId: number) => {
    if (sourceId === targetId) return;
    const ids = board.works.map((w: any) => w.id);
    const from = ids.indexOf(sourceId);
    const to = ids.indexOf(targetId);
    ids.splice(from, 1);
    ids.splice(to, 0, sourceId);
    const reordered = ids.map((wid: number) => board.works.find((w: any) => w.id === wid));
    setBoard((prev: any) => ({ ...prev, works: reordered }));
    try {
      await api.put(`/boards/${id}/works/order`, { work_ids: ids });
      // порядок решает, какое место занимает работа, — форма мест поменялась
      await load();
    } catch (e) {
      toastError(e);
      void load();
    }
  };

  const expiryLabel = (s: any) => {
    if (!s?.expires_at) return t("off");
    const days = Math.ceil((new Date(s.expires_at + "Z").getTime() - Date.now()) / 86400000);
    return days > 0 ? `${days}d` : t("off");
  };

  const setExpiry = (days: number | null) => {
    void patchShare({ expires_at: days ? new Date(Date.now() + days * 86400000).toISOString() : null });
  };

  return (
    <div className="page page-wide" style={{ paddingTop: 36 }}>
      <Link to="/boards" style={{ display: "inline-flex", alignItems: "center", gap: 6, color: "var(--muted)", fontSize: 13, marginBottom: 20 }}>
        <Icon name="arrowLeft" size={14} />
        {t("boards")}
      </Link>
      <div className="page-head" style={{ marginBottom: 28 }}>
        <div>
          <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
            <h1 className="page-title" style={{ fontSize: 22 }}>
              {board.title}
            </h1>
            {board.is_published ? (
              <Chip variant="success">{t("published")}</Chip>
            ) : (
              <Chip>
                <span className="dot" />
                {t("draft")}
              </Chip>
            )}
          </div>
          <div className="page-sub">
            {board.client_id && board.client_name && (
              <>
                {t("forClient")}{" "}
                <Link to={`/clients/${board.client_id}`} style={{ color: "var(--muted)", textDecoration: "underline", textUnderlineOffset: 2 }}>
                  {board.client_name}
                </Link>
                {" · "}
              </>
            )}
            {board.works.length} {t("works")} · {t("updated")} {formatDateTime(board.updated_at, locale)}
          </div>
        </div>
        {/* Обёртка расширяет область нажатия мышью на подпись рядом; сам
            переключатель — настоящая кнопка и работает с клавиатуры. */}
        <div
          style={{ display: "flex", alignItems: "center", gap: 9, cursor: "pointer", padding: "8px 12px", border: "1px solid var(--border)", borderRadius: 8 }}
          onClick={() => void patchBoard({ is_published: !board.is_published })}
        >
          <Toggle
            on={board.is_published}
            label={t("published")}
            onToggle={() => void patchBoard({ is_published: !board.is_published })}
          />
          <span style={{ fontSize: 13, fontWeight: 500 }}>{t("published")}</span>
        </div>
      </div>

      <div className="editor-layout">
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="works-grid">
            {/* Заглушки идут ПЕРВЫМИ, а не в хвосте списка.
                Человек только что нажал «загрузить» и смотрит туда, куда
                смотрел, — в начало сетки. Дописанная в конец полоса на доске из
                сорока работ оказывается за краем экрана, и вопрос «грузится ли
                вообще» остаётся без ответа ровно так же, как без неё. */}
            {zalivki.map((z) => {
              const dolya = z.vsego > 0 ? Math.min(1, z.ushlo / z.vsego) : 0;
              return (
                <div key={z.klyuch} className="work-card work-card--zaliv">
                  <div className="work-media work-media--zaliv">
                    <div className="zaliv-polosa">
                      <div className="zaliv-hod" style={{ width: `${dolya * 100}%` }} />
                    </div>
                    <div className="zaliv-cifry">
                      <span>{z.idyot ? t("uploading") : t("uploadQueued")}</span>
                      {z.idyot && (
                        <span>
                          {fileSize(z.ushlo)} / {fileSize(z.vsego)}
                        </span>
                      )}
                      {/* Скорость и остаток показываем ТОЛЬКО когда они
                          посчитаны. Ноль под полосой читается как «встало», а
                          «осталось 0 с» на пятисотмегабайтном файле — прямая
                          ложь. Молчание честнее. */}
                      {z.idyot && z.skorost > 0 && (
                        <span>{fileSize(z.skorost)}/s</span>
                      )}
                      {z.idyot && z.ostalos !== null && z.ostalos > 1 && (
                        <span>{t("uploadLeft", { time: srok(z.ostalos) })}</span>
                      )}
                    </div>
                    {z.otmenit && (
                      <button
                        type="button"
                        className="zaliv-otmena"
                        title={t("uploadCancel")}
                        onClick={() => z.otmenit?.()}
                      >
                        <Icon name="x" size={14} />
                      </button>
                    )}
                  </div>
                  <div className="work-foot">
                    <span className="zaliv-imya">{z.imya}</span>
                  </div>
                </div>
              );
            })}
            {board.works.map((work: any) => (
              <div
                key={work.id}
                className={"work-card" + (dragId !== null && dragId !== work.id ? "" : "")}
                draggable
                onDragStart={() => setDragId(work.id)}
                onDragEnd={() => setDragId(null)}
                {...dropTarget(() => {
                  if (dragId !== null) void reorder(dragId, work.id);
                })}
              >
                <div className="work-media">
                  {work.media?.card || work.media?.poster ? (
                    // работу, которую срезало её место, карточка показывает так
                    // же, как плитка витрины: та же форма места и тот же
                    // выбранный фрагмент — иначе менеджер правит вслепую
                    <img
                      src={work.media.card ?? work.media.poster}
                      alt=""
                      className={isCroppedWork(work) ? "is-cropped" : ""}
                      style={
                        isCroppedWork(work)
                          ? {
                              aspectRatio: String(placeRatio(work)),
                              objectPosition: `50% ${(work.preview_focus ?? 0) * 100}%`,
                            }
                          : undefined
                      }
                    />
                  ) : (
                    <div style={{ aspectRatio: "16/10", display: "grid", placeItems: "center", color: "var(--faint)" }}>
                      <Icon name={work.kind === "video" ? "play" : "image"} size={22} />
                    </div>
                  )}
                  {board.cover_work_id === work.id && (
                    <div className="badge-floating">
                      <Icon name="star" size={11} fill="currentColor" />
                      {t("cover")}
                    </div>
                  )}
                  {work.status === "processing" && (
                    <div className="work-overlay">
                      <div className="spinner" />
                      <span style={{ color: "var(--on-media-dim)", fontSize: 11.5 }}>{t("processing")}</span>
                    </div>
                  )}
                  {work.status === "ready" && board.cover_work_id !== work.id && work.kind === "image" && (
                    <button className="cover-btn" title={t("cover")} onClick={() => void patchBoard({ cover_work_id: work.id })}>
                      <Icon name="star" size={13} />
                    </button>
                  )}
                  {/* Кнопка — ровно у тех работ, которые своё место не заняли
                      целиком: по одному правилу с плашкой «открыть целиком» на
                      витрине. Прежде здесь стоял порог «высота ≥ ширины во
                      столько-то раз», и он утверждал, что короткая картинка
                      помещается в место целиком. Это неправда — форму места
                      задаёт композиция, мест несколько и они разной формы, —
                      отсюда и жалоба: у одной работы срезано название, у другой
                      кончики предметов, а подвинуть окно было нечем.

                      Первым заходом кнопку показали любой готовой картинке: без
                      правила иначе было не угадать. Теперь правило есть, и
                      кнопка не открывает окно, в котором нечего двигать. */}
                  {work.status === "ready" && isCroppedWork(work) && (
                    <button className="crop-btn" onClick={() => setCropWork(work)}>
                      <Icon name="crop" size={13} />
                      {t("cropPreview")}
                    </button>
                  )}
                  {work.duration_sec && (
                    <div className="badge-floating" style={{ top: "auto", left: "auto", bottom: 8, right: 8, color: "var(--text)", fontWeight: 500, fontSize: 10.5 }}>
                      <Icon name="play" size={9} fill="currentColor" />
                      {formatDuration(work.duration_sec)}
                    </div>
                  )}
                </div>
                <div className="work-foot">
                  <Icon name="grip" size={13} />
                  <WorkTitle work={work} boardId={board.id} onSaved={(updated) =>
                    setBoard((prev: any) => ({ ...prev, works: prev.works.map((w: any) => (w.id === updated.id ? { ...w, ...updated } : w)) }))
                  } />
                  {/* Скачивание — обычной ссылкой, а не запросом через fetch:
                      имя файла, прогресс и папку «Загрузки» браузер делает сам
                      и лучше (тот же довод, что у выгрузки отчётов). Ссылка
                      ведёт в API CRM, то есть работает под сессией сотрудника;
                      клиент витрины по ней не проходит. */}
                  <SkachatFayl
                    href={work.download_url}
                    bytes={work.size_bytes ?? 0}
                    label={t("download")}
                  />
                  <button
                    className="text-link"
                    style={{ display: "flex", color: work.project_url ? "var(--accent)" : "var(--faint)" }}
                    title={work.project_url || t("projectLink")}
                    onClick={() => setLinkWork(work)}
                  >
                    <Icon name="link" size={13} />
                  </button>
                  <button
                    className="text-link"
                    style={{ display: "flex", color: "var(--faint)" }}
                    aria-label={t("delete")}
                    onClick={() => setConfirm(work.id)}
                  >
                    <Icon name="trash" size={13} />
                  </button>
                </div>
              </div>
            ))}
            <div
              className="dropzone"
              style={{ minHeight: 200, display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", gap: 8 }}
              onClick={() => fileInput.current?.click()}
              {...dropTarget((e) => void uploadFiles(e.dataTransfer.files))}
            >
              <Icon name="upload" size={20} />
              <span style={{ fontSize: 12.5, textAlign: "center", lineHeight: 1.5, whiteSpace: "pre-line" }}>{t("dropWorks")}</span>
              <input
                ref={fileInput}
                type="file"
                accept="image/jpeg,image/png,image/gif,image/webp,image/svg+xml,video/mp4,video/webm"
                multiple
                hidden
                onChange={(e) => {
                  void uploadFiles(e.target.files);
                  e.target.value = "";
                }}
              />
            </div>
          </div>
        </div>

        <div className="editor-rail">
          <div className="rail-card">
            <div className="rail-title">{t("boardSettings")}</div>
            <label className="label">{t("boardTitle")}</label>
            <BlurInput value={board.title} onSave={(v) => void patchBoard({ title: v })} style={{ marginBottom: 12, height: 32 }} />
            <label className="label">{t("description")}</label>
            <BlurInput value={board.description} onSave={(v) => void patchBoard({ description: v })} textarea style={{ marginBottom: 12 }} />
            <label className="label">{t("client")}</label>
            <VyborKlienta
              value={board.client_id ?? null}
              imya={board.client_name ?? null}
              pustoy
              pustoyPodpis={t("noClient")}
              onPick={(kto) => void patchBoard({ client_id: kto })}
            />
            {/* Заявка, ради которой доска сделана. Список — только заявки
                выбранного клиента: чужие в этом окне лишь мешают. */}
            <label className="label" style={{ marginTop: 12 }}>{t("deal")}</label>
            <select
              className="select"
              style={{ height: 32 }}
              value={board.deal_id ?? ""}
              onChange={(e) => void patchBoard({ deal_id: e.target.value ? Number(e.target.value) : null })}
            >
              <option value="">{t("noDealLink")}</option>
              {(clientDeals.items ?? []).map((deal) => (
                <option key={deal.id} value={deal.id}>
                  {deal.title}
                </option>
              ))}
            </select>
            {clientDeals.failure !== null && (
              <LoadFailed error={clientDeals.failure} onRetry={clientDeals.reload} />
            )}
            <div style={{ color: "var(--faint)", fontSize: 11.5, marginTop: 10, lineHeight: 1.5 }}>{t("coverHint")}</div>
          </div>

          <div className="rail-card">
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 14 }}>
              <div style={{ fontSize: 14, fontWeight: 600 }}>{t("share")}</div>
              {share && (
                <div style={{ display: "flex", alignItems: "center", gap: 7 }}>
                  <span style={{ color: "var(--muted)", fontSize: 12 }}>
                    {share.is_active ? t("linkActive") : t("linkOff")}
                  </span>
                  <Toggle
                    on={share.is_active}
                    label={t("share")}
                    onToggle={() => void patchShare({ is_active: !share.is_active })}
                  />
                </div>
              )}
            </div>
            {!share ? (
              <button className="copy-btn" disabled={guard.busy} onClick={() => void createShare()}>
                <Icon name="link" size={15} />
                {t("createLink")}
              </button>
            ) : (
              <>
                <button className={"copy-btn" + (copied ? " copied" : "")} onClick={() => void copyLink()}>
                  <Icon name={copied ? "check" : "copy"} size={15} stroke={copied ? 2 : 1.5} />
                  {copied ? t("copied") : t("copyLink")}
                </button>
                <div style={{ display: "flex", alignItems: "center", gap: 6, margin: "10px 2px 14px", color: "var(--faint)", fontSize: 12 }}>
                  <Icon name="link" size={12} />
                  <span style={{ whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
                    {share.url.replace(/^https?:\/\//, "")}
                  </span>
                </div>
                <div>
                  <div className="share-row" onClick={() => setExpiryOpen((o) => !o)}>
                    <div className="share-row-label">
                      <Icon name="clock" size={14} />
                      {t("expiry")}
                    </div>
                    <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                      <span style={{ fontSize: 12.5 }}>{expiryLabel(share)}</span>
                      <Icon name="chevronDown" size={13} className="" />
                    </div>
                  </div>
                  {expiryOpen && (
                    <div style={{ display: "flex", gap: 6, padding: "2px 2px 12px" }}>
                      {[
                        { label: t("off"), days: null },
                        { label: t("days7"), days: 7 },
                        { label: t("days30"), days: 30 },
                        { label: t("days90"), days: 90 },
                      ].map((option) => (
                        <button
                          key={option.label}
                          className={"option-chip" + ((option.days === null) === !share.expires_at && option.days === null ? " active" : "")}
                          onClick={() => setExpiry(option.days)}
                        >
                          {option.label}
                        </button>
                      ))}
                    </div>
                  )}
                  <div className="share-row" onClick={() => setPinOpen((o) => !o)}>
                    <div className="share-row-label">
                      <Icon name="lock" size={14} />
                      {t("pinCode")}
                    </div>
                    <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                      <span style={{ fontSize: 12.5, letterSpacing: "0.1em" }}>{share.has_pin ? "••••" : t("off")}</span>
                      <Icon name="chevronDown" size={13} />
                    </div>
                  </div>
                  {pinOpen && (
                    <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "2px 2px 12px" }}>
                      {share.has_pin ? (
                        <button className="option-chip" onClick={() => void patchShare({ pin: null })}>
                          {t("pinRemove")}
                        </button>
                      ) : (
                        <>
                          <input
                            className="input"
                            style={{ width: 90, height: 30, letterSpacing: "0.2em" }}
                            maxLength={8}
                            inputMode="numeric"
                            placeholder="4821"
                            value={pinDraft}
                            onChange={(e) => setPinDraft(e.target.value.replace(/\D/g, ""))}
                          />
                          <button
                            className="option-chip"
                            onClick={() => {
                              if (pinDraft.length >= 4) {
                                void patchShare({ pin: pinDraft });
                                setPinDraft("");
                              }
                            }}
                          >
                            {t("pinSet")}
                          </button>
                        </>
                      )}
                      <span style={{ color: "var(--faint)", fontSize: 11.5, lineHeight: 1.4 }}>{t("pinHint")}</span>
                    </div>
                  )}
                  {/* gap и flexShrink — без них подсказка вплотную упирается в кнопку,
                      переносится на вторую строку и заезжает под неё: у «Перегенерировать»
                      пара строк длиннее, чем у «Удалить ссылку», и ломалась только она */}
                  <div className="share-action-row" style={{ paddingTop: 12, borderTop: "1px solid var(--surface-2)" }}>
                    <button className="btn-danger-link" onClick={() => setConfirm("regenerate")}>
                      {t("regenerate")}
                    </button>
                    <span className="share-action-hint">{t("regenerateHint")}</span>
                  </div>
                  <div className="share-action-row" style={{ paddingTop: 10 }}>
                    <button className="btn-danger-link" onClick={() => setConfirm("deleteShare")}>
                      {t("deleteLink")}
                    </button>
                    <span className="share-action-hint">{t("deleteLinkHint")}</span>
                  </div>
                </div>
              </>
            )}
          </div>

          {share && (
            <div className="rail-card" style={{ padding: "16px 18px" }}>
              <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                <div>
                  <div style={{ fontSize: 13.5, fontWeight: 500 }}>{t("viewsCount", { n: share.views_count })}</div>
                  <div style={{ color: "var(--faint)", fontSize: 12, marginTop: 3 }}>
                    {share.last_viewed_at
                      ? t("lastView", { t: formatDateTime(share.last_viewed_at, locale) })
                      : t("noViewsYet")}
                  </div>
                </div>
                <span style={{ color: "var(--muted)", fontSize: 12.5 }}>
                  {t("uniqueVisitors", { n: share.unique_views_count })}
                </span>
              </div>
            </div>
          )}

          {share && (
            <a className="btn btn-secondary" href={`/b/${share.token}`} target="_blank" rel="noreferrer">
              <Icon name="eye" size={15} />
              {t("previewAsClient")}
            </a>
          )}

          {/* Архивом — потому что доска это НАБОР. Сдать клиенту три десятка
              работ по одному щелчку на каждую значит тридцать щелчков и
              тридцать строк в «Загрузках»; ровно ради набора доска и заведена.
              Кнопки нет у пустой доски: она отвечала бы отказом. */}
          {board.works.length > 0 && (
            <a
              className="btn btn-secondary skachat-vsyo"
              href={`/api/v1/boards/${board.id}/download`}
            >
              <Icon name="download" size={15} />
              {t("downloadAll")}
              {/* Сумму складываем здесь, и это законно: работы приходят СПИСКОМ
                  целиком, без страниц. Будь он подрезан — итог пришлось бы
                  просить у сервера, как у себестоимости заявки. */}
              <span style={{ color: "var(--faint)" }}>
                {fileSize(
                  board.works.reduce((s: number, w: any) => s + (w.size_bytes ?? 0), 0),
                )}
              </span>
            </a>
          )}

          <button className="btn-danger-link" style={{ alignSelf: "flex-start" }} onClick={() => setConfirm("deleteBoard")}>
            {t("deleteBoard")}
          </button>
        </div>
      </div>

      {confirm === "regenerate" && (
        <ConfirmModal text={t("regenerateConfirm")} confirmLabel={t("confirm")} danger onConfirm={() => void regenerate()} onClose={() => setConfirm(null)} />
      )}
      {confirm === "deleteShare" && share && (
        <ConfirmModal
          text={t("deleteLinkConfirm")}
          confirmLabel={t("delete")}
          danger
          onConfirm={async () => {
            try {
              await api.del(`/shares/${share.id}`);
              await load();
            } catch (e) {
              toastError(e);
            }
          }}
          onClose={() => setConfirm(null)}
        />
      )}
      {confirm === "deleteBoard" && (
        <ConfirmModal
          text={t("deleteBoardConfirm")}
          confirmLabel={t("delete")}
          danger
          onConfirm={async () => {
            try {
              await api.del(`/boards/${id}`);
              navigate("/boards");
            } catch (e) {
              toastError(e);
            }
          }}
          onClose={() => setConfirm(null)}
        />
      )}
      {typeof confirm === "number" && (
        <ConfirmModal
          text={t("deleteWorkConfirm")}
          confirmLabel={t("delete")}
          danger
          onConfirm={() => void deleteWork(confirm)}
          onClose={() => setConfirm(null)}
        />
      )}
      {linkWork && (
        <WorkLinkModal
          work={linkWork}
          boardId={board.id}
          onClose={() => setLinkWork(null)}
          onSaved={(updated) => {
            setBoard((prev: any) => ({
              ...prev,
              works: prev.works.map((w: any) => (w.id === updated.id ? { ...w, ...updated } : w)),
            }));
            setLinkWork(null);
          }}
        />
      )}
      {cropWork && (
        <WorkCropModal
          work={cropWork}
          boardId={board.id}
          onClose={() => setCropWork(null)}
          onSaved={(updated) => {
            setBoard((prev: any) => ({
              ...prev,
              works: prev.works.map((w: any) => (w.id === updated.id ? { ...w, ...updated } : w)),
            }));
            setCropWork(null);
          }}
        />
      )}
    </div>
  );
}

const FALLBACK_PLACE = 1.34; // форма самого заметного места — пока работа не встала в композицию
const STAGE = 400; // высота обеих колонок редактора, px

/** Обрезана ли работа своим местом на витрине.
 *
 * Правило одно на обе стороны и живёт на сервере (`web/public/layout.py`,
 * `is_cropped`): обрезка зависит не от того, какая картинка, а от того, в какое
 * место композиции она попала. Мест семь, и формы у них разные — знает об этом
 * только сервер, поэтому фронтенд условие не повторяет, а читает готовый ответ.
 * Своя копия правила здесь неминуемо разошлась бы с витриной, и карточка в CRM
 * показывала бы не то, что видит клиент.
 *
 * Ответ приходит вместе с формой места (`place_ratio`) в выдаче доски и
 * пересчитывается сервером при каждой перестановке — порядок работ решает, кому
 * какое место досталось.
 */
export function isCroppedWork(work: any): boolean {
  return work.is_cropped === true;
}

/** Форма места работы на витрине (ширина / высота) — её присылает сервер. */
export function placeRatio(work: any): number {
  return work.place_ratio || FALLBACK_PLACE;
}

/** Редактор превью: какой фрагмент длинной работы попадёт на витрину. */
function WorkCropModal({
  work,
  boardId,
  onSaved,
  onClose,
}: {
  work: any;
  boardId: number;
  onSaved: (w: any) => void;
  onClose: () => void;
}) {
  const { t, toastError } = useApp();
  const natural = work.height / work.width; // во сколько ширин вытянута работа
  const place = placeRatio(work);
  const [focus, setFocus] = useState<number>(work.preview_focus ?? 0);
  const [saving, setSaving] = useState(false);

  const source = work.media?.large ?? work.media?.card;
  // карта всей работы: высота фиксирована, ширина — сколько остаётся от пропорций
  const mapWidth = Math.max(26, Math.round(STAGE / natural));
  // окно обрезки той же формы, что место на витрине: во всю ширину карты
  const windowHeight = Math.min(STAGE, mapWidth / place);
  const travel = STAGE - windowHeight; // сколько окну есть куда ехать

  // Считаем от точки, где нажали, поэтому окно не прыгает под курсор.
  const startDrag = (event: React.PointerEvent) => {
    event.preventDefault();
    const originY = event.clientY;
    const startTop = focus * travel;

    const onMove = (e: PointerEvent) => {
      if (travel <= 0) return;
      setFocus(Math.max(0, Math.min(1, (startTop + e.clientY - originY) / travel)));
    };
    const onUp = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  };

  const save = async (focusValue: number | null) => {
    setSaving(true);
    try {
      onSaved(
        await api.patch(`/boards/${boardId}/works/${work.id}`, { preview_focus: focusValue })
      );
    } catch (e) {
      toastError(e);
      setSaving(false);
    }
  };

  return (
    <Modal title={t("cropPreview")} onClose={onClose} wide>
      <div style={{ color: "var(--faint)", fontSize: 12.5, marginBottom: 18, lineHeight: 1.5 }}>
        {t("cropPreviewHint")}
      </div>

      <div className="crop-stage">
        <div>
          <div className="crop-label">{t("cropWindow")}</div>
          <div className="crop-map" style={{ width: mapWidth, height: STAGE }}>
            <img src={source} alt="" draggable={false} />
            <div className="crop-map-veil" style={{ height: focus * travel }} />
            <div
              className="crop-map-veil"
              style={{ top: focus * travel + windowHeight, bottom: 0, height: "auto" }}
            />
            <div
              className="crop-window"
              style={{ top: focus * travel, height: windowHeight }}
              onPointerDown={startDrag}
            />
          </div>
        </div>

        <div className="crop-side">
          <div className="crop-label">{t("cropResult")}</div>
          {/* ровно то же, что делает витрина: место своей формы, картинка во
              всю ширину, сдвинутая по вертикали на focus */}
          <div className="crop-preview" style={{ width: STAGE * place, height: STAGE }}>
            <img src={source} alt="" style={{ objectPosition: `50% ${focus * 100}%` }} />
          </div>
        </div>

        <div className="crop-controls">
          <div style={{ color: "var(--faint)", fontSize: 11.5, lineHeight: 1.5 }}>
            {t("cropWindowHint")}
          </div>
        </div>
      </div>

      <div style={{ display: "flex", gap: 10, justifyContent: "flex-end", marginTop: 20 }}>
        {work.preview_focus ? (
          <button
            className="btn btn-secondary btn-sm"
            style={{ marginRight: "auto" }}
            disabled={saving}
            onClick={() => void save(null)}
          >
            {t("cropReset")}
          </button>
        ) : null}
        <button className="btn btn-secondary btn-sm" onClick={onClose}>
          {t("cancel")}
        </button>
        <button className="btn btn-primary btn-sm" disabled={saving} onClick={() => void save(focus)}>
          {t("save")}
        </button>
      </div>
    </Modal>
  );
}

function WorkLinkModal({
  work,
  boardId,
  onSaved,
  onClose,
}: {
  work: any;
  boardId: number;
  onSaved: (w: any) => void;
  onClose: () => void;
}) {
  const { t, toastError } = useApp();
  const [draft, setDraft] = useState(work.project_url || "");
  const [saving, setSaving] = useState(false);

  const save = async (value: string) => {
    setSaving(true);
    try {
      onSaved(await api.patch(`/boards/${boardId}/works/${work.id}`, { project_url: value }));
    } catch (e) {
      toastError(e);
      setSaving(false);
    }
  };

  return (
    <Modal title={t("projectLink")} onClose={onClose}>
      <div style={{ color: "var(--faint)", fontSize: 12.5, marginBottom: 12, lineHeight: 1.5 }}>
        {t("projectLinkHint")}
      </div>
      <input
        className="input"
        value={draft}
        autoFocus
        placeholder="https://client.example/case"
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => e.key === "Enter" && void save(draft.trim())}
        style={{ marginBottom: 16 }}
      />
      <div style={{ display: "flex", gap: 10, justifyContent: "flex-end" }}>
        {work.project_url && (
          <button
            className="btn btn-secondary btn-sm"
            style={{ marginRight: "auto", color: "var(--danger)" }}
            disabled={saving}
            onClick={() => void save("")}
          >
            {t("removeImage")}
          </button>
        )}
        <button className="btn btn-secondary btn-sm" onClick={onClose}>
          {t("cancel")}
        </button>
        <button className="btn btn-primary btn-sm" disabled={saving} onClick={() => void save(draft.trim())}>
          {t("save")}
        </button>
      </div>
    </Modal>
  );
}

function BlurInput({
  value,
  onSave,
  textarea,
  style,
}: {
  value: string;
  onSave: (value: string) => void;
  textarea?: boolean;
  style?: React.CSSProperties;
}) {
  const [draft, setDraft] = useState(value);
  useEffect(() => setDraft(value), [value]);
  const commit = () => {
    if (draft !== value) onSave(draft);
  };
  return textarea ? (
    <textarea className="textarea" style={style} value={draft} onChange={(e) => setDraft(e.target.value)} onBlur={commit} />
  ) : (
    <input
      className="input"
      style={style}
      value={draft}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
    />
  );
}

function WorkTitle({ work, boardId, onSaved }: { work: any; boardId: number; onSaved: (w: any) => void }) {
  const { toastError } = useApp();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(work.title || work.original_name);

  useEffect(() => setDraft(work.title || work.original_name), [work.title, work.original_name]);

  if (!editing) {
    return (
      <button className="work-name" onClick={() => setEditing(true)} title={draft}>
        {draft}
      </button>
    );
  }
  return (
    <input
      className="work-name"
      style={{ color: "var(--text)" }}
      value={draft}
      autoFocus
      onChange={(e) => setDraft(e.target.value)}
      onBlur={async () => {
        setEditing(false);
        if (draft !== (work.title || work.original_name)) {
          try {
            onSaved(await api.patch(`/boards/${boardId}/works/${work.id}`, { title: draft }));
          } catch (e) {
            toastError(e);
          }
        }
      }}
      onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
    />
  );
}
