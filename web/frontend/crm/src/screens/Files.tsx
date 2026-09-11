import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";

import { Icon } from "../components/Icon";
import { OknoSsylki } from "../components/OknoSsylki";
import { ConfirmModal, Dochitat, Modal, ScreenLoading } from "../components/ui";
import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { kindLabel } from "../lib/documents";
import { useFailure } from "../lib/failure";
import { formatBytes, formatDate } from "../lib/format";
import type { TranslationKey } from "../lib/i18n";
import { can } from "../lib/permissions";
import { useGuard } from "../lib/guard";

/** Узел дерева. Имя приходит с сервера только там, где оно данные (доска,
 *  своя папка); у корней приходит `kind`, и слово подбирает экран — иначе
 *  «Доски» уехали бы на сервер и перестали переводиться. */
interface Uzel {
  id: string;
  kind?: string;
  name?: string;
  n: number;
  kids?: Uzel[];
}

interface Fayl {
  id: string;
  name: string;
  kind: "image" | "video" | "doc" | "other";
  mime: string;
  size_bytes: number;
  created_at: string | null;
  where: string;
  open: string | null;
  thumb: string | null;
}

/** Что сейчас у файла со ссылкой. Приходит тем же ответом, что и список:
 *  значок нужен в каждой строке, а по запросу на строку это обращение к базе
 *  на каждую. */
interface SostoyanieSsylki {
  rezhim: "view" | "download";
  is_active: boolean;
}

interface Hranilishche {
  storage_bytes: number;
  total_bytes: number;
  used_bytes: number;
  percent_used: number;
  level: string;
}

const IMENA: Record<string, TranslationKey> = {
  all: "filesAll",
  boards: "boards",
  clients: "clients",
  tasks: "tasks",
  docs: "documents",
  goods: "warehouse",
  own: "filesOwn",
  images: "filesPhotos",
  videos: "filesVideos",
};

/** Слово для вида файла. Картой, а не сборкой ключа из значения: ключ,
 *  собранный шаблоном, не видит ни проверка мёртвых переводов, ни типы. */
const VIDY: Record<string, TranslationKey> = {
  image: "filesKind_image",
  video: "filesKind_video",
  doc: "filesKind_doc",
  other: "filesKind_other",
};

const ZNACHKI: Record<string, string> = {
  all: "folder",
  boards: "boards",
  clients: "clients",
  tasks: "clock",
  docs: "receipt",
  goods: "warehouse",
  own: "upload",
  images: "image",
  videos: "play",
};

/** Сколько файлов на странице содержимого. */
const NA_STRANITSE = 60;

/** Полупрозрачная папка: ушко, задняя стенка, два листа под обрезкой и
 *  передняя створка. Обрезающий контейнер — ключ: без него листы просвечивают
 *  сквозь створку, и папка читается сплошным прямоугольником. */
function Steklyannaya({ imya, skolko, onClick }: { imya: string; skolko: number; onClick: () => void }) {
  return (
    <button type="button" className="papka-plita" onClick={onClick}>
      <span className="papka-risunok">
        <span className="papka-ushko" />
        <span className="papka-stenka" />
        <span className="papka-obrez">
          <span className="papka-list papka-list-a" />
          <span className="papka-list papka-list-b" />
        </span>
        {/* На створке счётчик, а не имя: имя стоит под карточкой, и второй
            раз оно ничего не добавляет. */}
        <span className="papka-stvorka">
          <span className="papka-metka">{skolko}</span>
        </span>
      </span>
      <span className="truncate papka-imya">{imya}</span>
    </button>
  );
}

export function Files() {
  const { t, locale, user, toastError, toast, refreshStorage } = useApp();
  const mozhnoNesti = can(user, "files.create");
  const mozhnoUbrat = can(user, "files.delete");
  // Работа доски сносится С ДОСКИ, а не из папки: право на это осталось
  // настройками, как было у прежнего менеджера файлов.
  const mozhnoUbratRabotu = can(user, "settings.manage");
  const mozhnoDelitsya = can(user, "files.share");

  const [derevo, setDerevo] = useState<Uzel | null>(null);
  const [hranilishche, setHranilishche] = useState<Hranilishche | null>(null);
  const [uzel, setUzel] = useState<string>("all");
  const [stroki, setStroki] = useState<Fayl[] | null>(null);
  const [vsego, setVsego] = useState(0);
  const [stranitsa, setStranitsa] = useState(1);
  const [dochityvaem, setDochityvaem] = useState(false);
  const [raskryty, setRaskryty] = useState<Set<string>>(() => new Set(["all", "boards", "own"]));
  const [poisk, setPoisk] = useState("");
  const [novaya, setNovaya] = useState<{ parent_id: number | null } | null>(null);
  const [imyaPapki, setImyaPapki] = useState("");
  const [snyat, setSnyat] = useState<{ vid: "file" | "folder" | "work"; id: number; name: string } | null>(null);
  const [ochered, setOchered] = useState<{ name: string; dolya: number; beda?: string }[]>([]);
  const [ssylki, setSsylki] = useState<Record<string, SostoyanieSsylki>>({});
  const [delimsya, setDelimsya] = useState<{ nomer: string; imya: string } | null>(null);
  const vybor = useRef<HTMLInputElement | null>(null);
  /** Поколение списка. Ветку переключили, пока ехала вторая страница прошлой —
   *  та приедет и допишется к чужим строкам, а «всего» останется от прошлой.
   *  Заметить это нечем: список выглядит обычным. */
  const pokolenie = useRef(0);

  // Засов на заведение папки: два нажатия подряд заводят двух «Договоров»
  // рядом, отличить которых человеку нечем.
  const papkaGuard = useGuard();
  const { failure, fail, clear } = useFailure();

  const vetki = useCallback(() => {
    api
      .get<{ tree: Uzel; storage: Hranilishche }>("/files/tree")
      .then((d) => {
        setDerevo(d.tree);
        setHranilishche(d.storage);
      })
      .catch(fail);
  }, [fail]);

  /** Страница содержимого. `dobavit` — «показать ещё»: строки дописываются к
   *  уже показанным, а не заменяют их. */
  const soderzhimoe = useCallback(
    (kuda: string, page = 1, dobavit = false) => {
      clear();
      if (dobavit) setDochityvaem(true);
      else pokolenie.current += 1;
      const moyo = pokolenie.current;
      api
        .get<{ items: Fayl[]; total: number; links?: Record<string, SostoyanieSsylki> }>(
          `/files?node=${encodeURIComponent(kuda)}&page=${page}&per_page=${NA_STRANITSE}`,
        )
        .then((d) => {
          if (pokolenie.current !== moyo) return;
          setStroki((bylo) => (dobavit && bylo ? [...bylo, ...d.items] : d.items));
          setSsylki((bylo) => (dobavit ? { ...bylo, ...(d.links ?? {}) } : d.links ?? {}));
          setVsego(d.total);
          setStranitsa(page);
        })
        .catch(fail)
        .finally(() => setDochityvaem(false));
    },
    [fail, clear],
  );

  useEffect(() => vetki(), [vetki]);
  useEffect(() => soderzhimoe(uzel), [uzel, soderzhimoe]);

  /** Имя узла: у корней слово подбирает экран, у досок и папок оно данные. */
  const imya = useCallback(
    (u: Uzel): string => {
      if (u.name) return u.name;
      if (u.kind && u.kind.startsWith("doc-")) return kindLabel(t, u.kind.slice(4));
      return u.kind && IMENA[u.kind] ? t(IMENA[u.kind]) : u.id;
    },
    [t],
  );

  /** Путь до узла: крошки в шапке содержимого. */
  const doroga = useMemo(() => {
    if (!derevo) return [] as Uzel[];
    const idti = (u: Uzel, put: Uzel[]): Uzel[] | null => {
      const svoy = [...put, u];
      if (u.id === uzel) return svoy;
      for (const rebyonok of u.kids ?? []) {
        const nayden = idti(rebyonok, svoy);
        if (nayden) return nayden;
      }
      return null;
    };
    return idti(derevo, []) ?? [derevo];
  }, [derevo, uzel]);

  /** Папки выбранной ветки — карточками в содержимом. */
  const podpapki = doroga.length > 0 ? doroga[doroga.length - 1].kids ?? [] : [];

  const otobrannye = useMemo(() => {
    const slovo = poisk.trim().toLowerCase();
    if (!slovo || !stroki) return stroki ?? [];
    return stroki.filter((f) => f.name.toLowerCase().includes(slovo));
  }, [stroki, poisk]);

  /** Дерево при наборе сужается тем же словом: ветка остаётся, если подошла
   *  сама или подошёл кто-то внутри неё. Показывать полное дерево рядом с
   *  сузившимся списком значило бы отвечать на один вопрос двумя ответами. */
  const suzhennoe = useMemo(() => {
    const slovo = poisk.trim().toLowerCase();
    if (!slovo || !derevo) return derevo;
    const proyti = (u: Uzel): Uzel | null => {
      const deti = (u.kids ?? []).map(proyti).filter((x): x is Uzel => x !== null);
      const svoyo = imya(u).toLowerCase().includes(slovo);
      if (!svoyo && deti.length === 0) return null;
      return { ...u, kids: svoyo ? u.kids ?? [] : deti };
    };
    return proyti(derevo) ?? { ...derevo, kids: [] };
  }, [derevo, poisk, imya]);

  if (!derevo || !stroki) return <ScreenLoading error={failure} onRetry={vetki} />;

  const perekluchit = (id: string) =>
    setRaskryty((bylo) => {
      const novoe = new Set(bylo);
      if (novoe.has(id)) novoe.delete(id);
      else novoe.add(id);
      return novoe;
    });

  const svezho = () => {
    vetki();
    soderzhimoe(uzel);
    void refreshStorage();
  };

  /** В какую папку лягут принесённые файлы. Корень «Загрузки» — без папки. */
  const kudaNesti = (): number | null => {
    const chasti = uzel.split(":");
    return chasti[0] === "folder" ? Number(chasti[1]) : null;
  };
  const svoyaVetka = uzel === "own" || uzel.startsWith("folder:");

  /** Файлы идут ПО ОДНОМУ, а не все сразу.
   *
   *  Пять параллельных заливок делят ту же полосу на пять, каждая идёт впятеро
   *  дольше, и очередь показывает пять полосок, ни одна из которых не движется
   *  заметно. По одному — первый файл готов через свои секунды, а не через
   *  общие. Доля — настоящая, из `upload.progress`: `fetch` о ходе молчит.
   */
  const nesti = async (spisok: File[]) => {
    if (spisok.length === 0) return;
    setOchered(spisok.map((f) => ({ name: f.name, dolya: 0 })));
    const folder = kudaNesti();
    const put = `/files${folder === null ? "" : `?folder_id=${folder}`}`;
    for (let i = 0; i < spisok.length; i += 1) {
      try {
        await api.zagruzka(put, spisok[i], ({ ushlo, vsego: skolko }) =>
          setOchered((bylo) =>
            bylo.map((x, j) => (j === i ? { ...x, dolya: Math.round((ushlo / skolko) * 100) } : x)),
          ),
        ).gotovo;
        setOchered((bylo) => bylo.map((x, j) => (j === i ? { ...x, dolya: 100 } : x)));
      } catch (e: any) {
        setOchered((bylo) =>
          bylo.map((x, j) => (j === i ? { ...x, dolya: 0, beda: e?.message ?? "" } : x)),
        );
        toastError(e);
      }
    }
    svezho();
    // Панель очереди уходит сама: висеть после того, как всё легло, ей незачем.
    window.setTimeout(() => setOchered([]), 2500);
  };

  const zavesti = async () => {
    if (!papkaGuard.take()) return;
    try {
      await api.post("/files/folders", { name: imyaPapki, parent_id: novaya?.parent_id ?? null });
      setNovaya(null);
      setImyaPapki("");
      vetki();
      toast(t("filesFolderCreated"));
    } catch (e) {
      toastError(e);
    } finally {
      papkaGuard.free();
    }
  };

  const ubrat = async () => {
    if (!snyat) return;
    try {
      if (snyat.vid === "folder") {
        await api.del(`/files/folders/${snyat.id}`);
        if (uzel === `folder:${snyat.id}`) setUzel("own");
      } else if (snyat.vid === "work") {
        await api.del(`/system/files/${snyat.id}`);
      } else {
        await api.del(`/files/${snyat.id}`);
      }
      setSnyat(null);
      svezho();
    } catch (e) {
      toastError(e);
    }
  };

  const Vetka = ({ u, glubina }: { u: Uzel; glubina: number }) => {
    const deti = u.kids ?? [];
    // При поиске раскрыто всё: находка в свёрнутой ветке не находка.
    const otkryt = poisk.trim() !== "" || raskryty.has(u.id);
    return (
      <>
        <div
          className={
            "fayl-vetka" + (glubina === 0 ? " fayl-vetka-koren" : "") + (uzel === u.id ? " fayl-vetka-on" : "")
          }
          // Связка к родителю рисуется от отступа: на плоском списке строк это
          // единственный способ показать, что ветка чья-то, а не сама по себе.
          style={{ paddingLeft: 8 + glubina * 15, "--otstup": `${glubina * 15}px` } as React.CSSProperties}
        >
          {deti.length > 0 ? (
            <button
              type="button"
              className="fayl-strelka"
              aria-label={otkryt ? t("collapse") : t("expand")}
              onClick={() => perekluchit(u.id)}
            >
              <Icon name="chevronRight" size={12} className={otkryt ? "fayl-strelka-vniz" : undefined} />
            </button>
          ) : (
            <span className="fayl-strelka" />
          )}
          <button type="button" className="fayl-vetka-imya" onClick={() => setUzel(u.id)}>
            <Icon name={ZNACHKI[u.kind ?? ""] ?? "folder"} size={14} />
            <span className="truncate">{imya(u)}</span>
            <span className="fayl-schyot">{u.n}</span>
          </button>
        </div>
        {otkryt && deti.map((rebyonok) => <Vetka key={rebyonok.id} u={rebyonok} glubina={glubina + 1} />)}
      </>
    );
  };

  return (
    <div className="page page-wide">
      <div className="staff-head">
        <div>
          <h1 className="page-title">{t("files")}</h1>
          <div className="page-sub">{t("filesSub")}</div>
        </div>
        <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
          {mozhnoNesti && (
            <button
              type="button"
              className="btn btn-secondary"
              onClick={() => setNovaya({ parent_id: kudaNesti() })}
            >
              <Icon name="plus" size={13} />
              {t("filesNewFolder")}
            </button>
          )}
          {mozhnoNesti && (
            <button
              type="button"
              className="btn btn-primary"
              disabled={!svoyaVetka}
              title={svoyaVetka ? undefined : t("filesUploadWhere")}
              onClick={() => vybor.current?.click()}
            >
              <Icon name="upload" size={13} />
              {t("filesUpload")}
            </button>
          )}
          <input
            ref={vybor}
            type="file"
            multiple
            hidden
            onChange={(e) => {
              void nesti([...(e.target.files ?? [])]);
              e.target.value = "";
            }}
          />
        </div>
      </div>

      <div className="fayly">
        <aside className="fayly-panel">
          <label className="klienty-poisk fayly-poisk">
            <Icon name="search" size={13} />
            <input
              className="input input-sm"
              value={poisk}
              placeholder={t("filesFind")}
              aria-label={t("filesFind")}
              onChange={(e) => setPoisk(e.target.value)}
            />
          </label>
          <div className="fayly-derevo">
            {suzhennoe && (suzhennoe.kids?.length || imya(suzhennoe).toLowerCase().includes(poisk.trim().toLowerCase())) ? (
              <Vetka u={suzhennoe} glubina={0} />
            ) : (
              <div className="klienty-pusto">{t("clientsNothingFound")}</div>
            )}
          </div>
          {hranilishche && (
            <div className="fayly-disk">
              <div className="fayly-disk-podpis">
                <span>{t("filesDisk")}</span>
                <span>{formatBytes(hranilishche.storage_bytes)}</span>
              </div>
              <div className="fayly-disk-polosa">
                <span style={{ width: `${Math.min(100, hranilishche.percent_used)}%` }} />
              </div>
              <div className="fayly-disk-tishe">
                {t("filesDiskFree", { size: formatBytes(hranilishche.total_bytes - hranilishche.used_bytes) })}
              </div>
            </div>
          )}
        </aside>

        <section
          className="fayly-soderzhimoe"
          onDragOver={(e) => {
            if (mozhnoNesti && svoyaVetka) e.preventDefault();
          }}
          onDrop={(e) => {
            if (!mozhnoNesti || !svoyaVetka) return;
            e.preventDefault();
            void nesti([...e.dataTransfer.files]);
          }}
        >
          <div className="fayly-kroshki">
            {doroga.map((u, i) => (
              <span key={u.id}>
                {i > 0 && <Icon name="chevronRight" size={12} />}
                <button type="button" className="fayly-kroshka" onClick={() => setUzel(u.id)}>
                  {imya(u)}
                </button>
              </span>
            ))}
          </div>

          {podpapki.length > 0 && (
            <div className="fayly-papki">
              {podpapki.map((p) => (
                <Steklyannaya key={p.id} imya={imya(p)} skolko={p.n} onClick={() => setUzel(p.id)} />
              ))}
            </div>
          )}

          {(otobrannye.length > 0 || podpapki.length === 0) && (
            <div className="fayly-zagolovok">
              <h2 className="section-title">{t("filesInFolder")}</h2>
              <span className="staff-shown">{t("filesShown", { n: otobrannye.length, total: vsego })}</span>
            </div>
          )}

          {otobrannye.length === 0 ? (
            podpapki.length === 0 && (
              <div className="klienty-pusto">{poisk ? t("clientsNothingFound") : t("filesEmpty")}</div>
            )
          ) : (
            <div className="fayly-svitok">
              <div className="fayly-setka">
                <div className="klienty-th">{t("filesFile")}</div>
                <div className="klienty-th">{t("filesWhere")}</div>
                <div className="klienty-th">{t("filesType")}</div>
                <div className="klienty-th klienty-th-num">{t("filesSize")}</div>
                <div className="klienty-th">{t("filesUploaded")}</div>
                <div className="klienty-th" />
                {otobrannye.map((f) => (
                  <div className="fayly-tr" key={f.id} role="row">
                    <span className="fayly-td fayly-kto">
                      {f.thumb ? (
                        <img className="fayly-mini" src={f.thumb} alt="" loading="lazy" />
                      ) : (
                        <span className="fayly-mini fayly-mini-bez">
                          <Icon name={f.kind === "video" ? "play" : f.kind === "image" ? "image" : "docs"} size={14} />
                        </span>
                      )}
                      <span className="truncate">{f.name}</span>
                    </span>
                    <span className="fayly-td truncate klienty-tishe">
                      {f.open ? (
                        <Link to={f.open} className="fayly-hod">
                          {f.where || t("filesOpenOwner")}
                        </Link>
                      ) : (
                        f.where || "—"
                      )}
                    </span>
                    <span className="fayly-td klienty-tishe">{t(VIDY[f.kind] ?? VIDY.other)}</span>
                    <span className="fayly-td klienty-tishe fayly-num">{formatBytes(f.size_bytes)}</span>
                    <span className="fayly-td klienty-tishe">
                      {f.created_at ? formatDate(f.created_at, locale) : "—"}
                    </span>
                    <span className="fayly-td fayly-deystviya">
                      {mozhnoDelitsya && (f.id.startsWith("stored:") || f.id.startsWith("work:")) && (
                        <button
                          type="button"
                          className={
                            "btn-icon staff-act" +
                            (ssylki[f.id] ? ` fayl-ssylka-${ssylki[f.id].rezhim}` : "")
                          }
                          title={
                            ssylki[f.id]
                              ? t(ssylki[f.id].rezhim === "download" ? "linkDownload" : "linkViewOnly")
                              : t("linkAccess")
                          }
                          aria-label={t("linkAccess")}
                          onClick={() => setDelimsya({ nomer: f.id, imya: f.name })}
                        >
                          <Icon name={ssylki[f.id]?.rezhim === "view" ? "lock" : "link"} size={14} />
                        </button>
                      )}
                      {f.id.startsWith("stored:") && (
                        <a
                          className="btn-icon staff-act"
                          href={`/api/v1/files/${f.id.split(":")[1]}/download`}
                          title={t("download")}
                          aria-label={t("download")}
                        >
                          <Icon name="download" size={14} />
                        </a>
                      )}
                      {((mozhnoUbrat && f.id.startsWith("stored:")) ||
                        (mozhnoUbratRabotu && f.id.startsWith("work:"))) && (
                        <button
                          type="button"
                          className="btn-icon staff-act staff-act-danger"
                          title={t("delete")}
                          aria-label={t("delete")}
                          onClick={() =>
                            setSnyat({
                              vid: f.id.startsWith("work:") ? "work" : "file",
                              id: Number(f.id.split(":")[1]),
                              name: f.name,
                            })
                          }
                        >
                          <Icon name="trash" size={14} />
                        </button>
                      )}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}

          <Dochitat
            pokazano={stroki.length}
            vsego={vsego}
            zanyat={dochityvaem}
            onClick={() => soderzhimoe(uzel, stranitsa + 1, true)}
          />

          {mozhnoUbrat && uzel.startsWith("folder:") && (
            <button
              type="button"
              className="text-link danger fayly-snyat-papku"
              onClick={() =>
                setSnyat({
                  vid: "folder",
                  id: Number(uzel.split(":")[1]),
                  name: imya(doroga[doroga.length - 1]),
                })
              }
            >
              {t("filesDeleteFolder")}
            </button>
          )}
        </section>
      </div>

      {ochered.length > 0 && (
        <div className="fayly-ochered">
          <div className="fayly-ochered-shapka">
            {t("filesQueue", { done: ochered.filter((x) => x.dolya === 100).length, n: ochered.length })}
          </div>
          {ochered.map((x) => (
            <div className="fayly-ochered-stroka" key={x.name}>
              <span className="truncate">{x.name}</span>
              <span className={"fayly-ochered-polosa" + (x.beda ? " fayly-ochered-beda" : "")}>
                <span style={{ width: `${x.dolya}%` }} />
              </span>
            </div>
          ))}
          <div className="fayly-ochered-kuda">
            {t("filesQueueTo", { name: imya(doroga[doroga.length - 1]) })}
          </div>
        </div>
      )}

      {delimsya && (
        <OknoSsylki
          nomer={delimsya.nomer}
          imya={delimsya.imya}
          onClose={() => {
            setDelimsya(null);
            soderzhimoe(uzel);
          }}
        />
      )}

      {novaya && (
        <Modal title={t("filesNewFolder")} onClose={() => setNovaya(null)}>
          <div className="field">
            <label className="label">{t("filesFolderName")}</label>
            <input
              className="input"
              autoFocus
              value={imyaPapki}
              maxLength={120}
              onChange={(e) => setImyaPapki(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && void zavesti()}
            />
            <div className="field-desc">
              {novaya.parent_id === null
                ? t("filesFolderAtRoot")
                : t("filesFolderInside", { name: imya(doroga[doroga.length - 1]) })}
            </div>
          </div>
          <div style={{ display: "flex", gap: 10, justifyContent: "flex-end" }}>
            <button type="button" className="btn btn-secondary btn-sm" onClick={() => setNovaya(null)}>
              {t("cancel")}
            </button>
            <button
              type="button"
              className="btn btn-primary btn-sm"
              disabled={!imyaPapki.trim() || papkaGuard.busy}
              onClick={() => void zavesti()}
            >
              {t("create")}
            </button>
          </div>
        </Modal>
      )}

      {snyat && (
        <ConfirmModal
          text={
            snyat.vid === "folder"
              ? t("filesDeleteFolderConfirm", { name: snyat.name })
              : snyat.vid === "work"
                ? t("filesDeleteWorkConfirm", { name: snyat.name })
                : t("filesDeleteConfirm", { name: snyat.name })
          }
          confirmLabel={t("delete")}
          danger
          onConfirm={() => void ubrat()}
          onClose={() => setSnyat(null)}
        />
      )}
    </div>
  );
}
