import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";

import { Icon } from "./Icon";
import { EmptyState, Modal, Spinner } from "./ui";
import { api, type Zalivka } from "../lib/api";
import { useApp } from "../lib/app";
import { dropTarget } from "../lib/dnd";
import { formatDateTime } from "../lib/format";
import { VAZHNOSTI, VAZHNOST_LABEL, srochno, vazhnost } from "../lib/vazhnost";

export interface Vlozhenie {
  id: number;
  original_name: string;
  mime: string;
  size_bytes: number;
  download_url: string;
}

/** Тот же потолок, что у сервера (`task_service.MAX_NOTE`): поле не должно
 *  принимать то, что будет отвергнуто. */
const MAX_NOTE = 20_000;

/** Срок из поля ввода в абсолютный момент — тот же перевод, что в списке. */
function toInstant(local: string): string | null {
  if (!local) return null;
  const moment = new Date(local);
  return Number.isNaN(moment.getTime()) ? null : moment.toISOString();
}

function toLocalInput(iso: string | null): string {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
}

/**
 * Углублённая карточка напоминания: подробности, важность и вложения.
 *
 * Заведена по просьбе владельца: строки списка хватает на «перезвонить», но не
 * на «перезвонить, вот фото шильдика, вот видео, как гудит». Всё это лежало по
 * заявкам и клиентам, а напоминание оставалось голым заголовком.
 */
export function KartochkaNapominaniya({
  taskId,
  onClose,
  onChanged,
}: {
  taskId: number;
  onClose: () => void;
  onChanged: () => void;
}) {
  const { t, locale, toastError } = useApp();
  const [task, setTask] = useState<any | null>(null);
  const [title, setTitle] = useState("");
  const [note, setNote] = useState("");
  const [srok, setSrok] = useState("");
  const [hod, setHod] = useState<{ imya: string; dolya: number } | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const zalivka = useRef<Zalivka<unknown> | null>(null);

  // Обработчики окна приходят новыми на каждой перерисовке списка, а список
  // перерисовывается от каждого живого события. Держи их в зависимостях — и
  // карточка перечитывалась бы поверх недописанной заметки.
  const svyazi = useRef({ onClose, onChanged, toastError });
  svyazi.current = { onClose, onChanged, toastError };

  // Что человек набрал и что лежит на сервере. Нужно при закрытии: Escape и
  // клик мимо снимают окно синхронно, а `blur` браузер удалённому элементу не
  // шлёт — без этого пять строк подробностей исчезали молча.
  const chernovik = useRef({ title: "", note: "", srok: "" });
  const sohranyonnoe = useRef({ title: "", note: "", srok: "" });
  chernovik.current = { title, note, srok };

  // Номер правки: на медленной сети ответы приходят не в том порядке, в каком
  // ушли, и старый откатывал бы карточку к состоянию до правки.
  const nomer = useRef(0);
  const zhivo = useRef(true);

  const perechitat = useCallback(async () => {
    try {
      const data = await api.get<any>(`/tasks/${taskId}`);
      if (!zhivo.current) return;
      setTask(data);
      setTitle(data.title);
      // Недописанное не затираем: перечитка идёт и после заливки файла, а файл
      // роняют перетаскиванием — фокус при этом никуда не уходит.
      if (chernovik.current.note === sohranyonnoe.current.note) setNote(data.note || "");
      if (chernovik.current.srok === sohranyonnoe.current.srok) setSrok(toLocalInput(data.due_at));
      sohranyonnoe.current = {
        title: data.title,
        note: data.note || "",
        srok: toLocalInput(data.due_at),
      };
    } catch (e) {
      svyazi.current.toastError(e);
      svyazi.current.onClose();
    }
  }, [taskId]);

  useEffect(() => {
    zhivo.current = true;
    void perechitat();
    return () => {
      zhivo.current = false;
    };
  }, [perechitat]);

  const pravit = useCallback(
    async (data: Record<string, unknown>) => {
      const moy = ++nomer.current;
      try {
        const svezhee = await api.patch<any>(`/tasks/${taskId}`, data);
        // Ответ обогнавшей правки не откатываем: последним словом остаётся
        // последняя правка, а не последний ответ.
        if (moy !== nomer.current || !zhivo.current) return;
        setTask((prev: any) => ({ ...prev, ...svezhee }));
        sohranyonnoe.current = {
          title: svezhee.title,
          note: "note" in data ? String(data.note ?? "") : sohranyonnoe.current.note,
          srok: toLocalInput(svezhee.due_at),
        };
        svyazi.current.onChanged();
      } catch (e) {
        svyazi.current.toastError(e);
        if (zhivo.current) void perechitat();
      }
    },
    [taskId, perechitat],
  );

  // Закрыли окно, не убрав фокуса из поля, — дописываем сами. Не из состояния:
  // к моменту уборки его уже нет, есть только ссылки.
  useEffect(
    () => () => {
      const bylo = sohranyonnoe.current;
      const stalo = chernovik.current;
      const pravka: Record<string, unknown> = {};
      if (stalo.title.trim() && stalo.title.trim() !== bylo.title) pravka.title = stalo.title.trim();
      if (stalo.note !== bylo.note) pravka.note = stalo.note;
      if (stalo.srok !== bylo.srok) pravka.due_at = toInstant(stalo.srok);
      if (!Object.keys(pravka).length) return;
      api
        .patch(`/tasks/${taskId}`, pravka)
        .then(() => svyazi.current.onChanged())
        .catch((e) => svyazi.current.toastError(e));
    },
    [taskId],
  );

  // Окно закрыли посреди заливки — бросаем её: полоса, которой уже никто не
  // видит, только держит соединение.
  useEffect(() => () => zalivka.current?.otmenit(), []);

  const zalit = async (spisok: FileList | null) => {
    if (!spisok || spisok.length === 0) return;
    for (const file of Array.from(spisok)) {
      const rabota = api.zagruzka(`/tasks/${taskId}/files`, file, (k) =>
        setHod({ imya: file.name, dolya: k.vsego ? k.ushlo / k.vsego : 0 }),
      );
      zalivka.current = rabota;
      try {
        await rabota.gotovo;
      } catch (e) {
        if (zhivo.current) svyazi.current.toastError(e);
      } finally {
        zalivka.current = null;
      }
      if (!zhivo.current) return;
    }
    setHod(null);
    await perechitat();
    svyazi.current.onChanged();
  };

  const snyat = async (fileId: number) => {
    try {
      await api.del(`/tasks/${taskId}/files/${fileId}`);
      await perechitat();
      svyazi.current.onChanged();
    } catch (e) {
      svyazi.current.toastError(e);
    }
  };

  if (!task) {
    return (
      <Modal title={t("tasksCard")} onClose={onClose} wide>
        <div style={{ display: "flex", justifyContent: "center", padding: 40 }}>
          <Spinner />
        </div>
      </Modal>
    );
  }

  const files: Vlozhenie[] = task.files ?? [];
  const tekushchaya = vazhnost(task.vazhnost);

  return (
    <Modal title={t("tasksCard")} onClose={onClose} wide>
      {/* Рябь по краю окна — та же, что вокруг строки списка: если срочное
          напоминание открыли, оно и здесь обязано выглядеть срочным. */}
      <div className={"napominanie-karta" + (srochno(tekushchaya) ? " srochno" : "")}>
        <div className="napominanie-karta-telo">
          <input
            className="input napominanie-zagolovok"
            value={title}
            aria-label={t("tasksTitleLabel")}
            onChange={(e) => setTitle(e.target.value)}
            onBlur={() => {
              const text = title.trim();
              if (!text) {
                setTitle(sohranyonnoe.current.title);
                return;
              }
              if (text !== sohranyonnoe.current.title) void pravit({ title: text });
            }}
          />

          <div className="napominanie-ryad">
            <div className="vazhnost-vybor" role="radiogroup" aria-label={t("vazhnost")}>
              {VAZHNOSTI.map((slovo) => (
                <button
                  key={slovo}
                  type="button"
                  role="radio"
                  className={"vazhnost-knopka " + slovo + (tekushchaya === slovo ? " active" : "")}
                  aria-checked={tekushchaya === slovo}
                  onClick={() => {
                    if (tekushchaya !== slovo) void pravit({ vazhnost: slovo });
                  }}
                >
                  {t(VAZHNOST_LABEL[slovo])}
                </button>
              ))}
            </div>
            {/* Срок сохраняется по уходу из поля, а не на каждое изменение:
                стирая месяц у заполненной даты, человек на мгновение оставляет
                поле пустым — и правка «по изменению» стёрла бы срок целиком. */}
            <input
              className="input"
              type="datetime-local"
              style={{ width: 200, flex: "none" }}
              aria-label={t("tasksDueAt")}
              value={srok}
              onChange={(e) => setSrok(e.target.value)}
              onBlur={() => {
                if (srok !== sohranyonnoe.current.srok) void pravit({ due_at: toInstant(srok) });
              }}
            />
          </div>

          <div className="field">
            <label className="label" htmlFor="napominanie-note">
              {t("tasksNote")}
            </label>
            <textarea
              id="napominanie-note"
              className="input"
              rows={5}
              maxLength={MAX_NOTE}
              placeholder={t("tasksNoteHint")}
              value={note}
              onChange={(e) => setNote(e.target.value)}
              onBlur={() => {
                if (note !== sohranyonnoe.current.note) void pravit({ note });
              }}
            />
          </div>

          <div className="napominanie-svyazi">
            {task.assignee_name && (
              <span>
                <Icon name="user" size={12} /> {task.assignee_name}
              </span>
            )}
            {task.client_id && (
              <Link to={`/clients/${task.client_id}`} className="text-link" onClick={onClose}>
                {task.client_name || t("client")}
              </Link>
            )}
            {task.deal_id && (
              <Link to={`/deals/${task.deal_id}`} className="text-link" onClick={onClose}>
                {task.deal_title || t("deal")}
              </Link>
            )}
            <span>{formatDateTime(task.created_at, locale)}</span>
          </div>

          <div className="napominanie-vlozheniya">
            <div className="metric-title" style={{ marginBottom: 10 }}>
              <Icon name="image" size={13} />
              {t("tasksFiles")}
            </div>
            {/* Кнопка, а не просто область под курсором: с клавиатуры сюда
                иначе не попасть — скрытое поле фокус не принимает. */}
            <div
              className="dropzone"
              role="button"
              tabIndex={0}
              style={{ marginBottom: files.length ? 12 : 0 }}
              onClick={() => fileInput.current?.click()}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  fileInput.current?.click();
                }
              }}
              {...dropTarget((e) => void zalit(e.dataTransfer.files))}
            >
              {hod ? (
                <>
                  <div className="truncate">{hod.imya}</div>
                  <div className="karta-hod">
                    <span
                      className="karta-hod-polosa"
                      style={{ width: `${Math.round(hod.dolya * 100)}%` }}
                    />
                  </div>
                </>
              ) : (
                <>
                  {t("dropFiles")} <span className="dropzone-vybor">{t("browse")}</span>{" "}
                  {t("tasksFilesHint")}
                </>
              )}
              {/* Мимо обхода табом: поле скрыто и фокус не принимает, а ловушка
                  окна считала его последней остановкой и упускала фокус. */}
              <input
                ref={fileInput}
                type="file"
                multiple
                tabIndex={-1}
                accept="image/*,video/*"
                hidden
                onChange={(e) => void zalit(e.target.files)}
              />
            </div>
            {files.length === 0 ? (
              <EmptyState icon="image" title={t("tasksFilesNone")} />
            ) : (
              <div className="vlozheniya">
                {files.map((file) => (
                  <figure key={file.id} className="vlozhenie">
                    {file.mime.startsWith("video/") ? (
                      <video
                        className="vlozhenie-media"
                        src={file.download_url}
                        controls
                        preload="metadata"
                      />
                    ) : (
                      <a href={file.download_url} target="_blank" rel="noreferrer">
                        <img
                          className="vlozhenie-media"
                          src={file.download_url}
                          alt={file.original_name}
                          loading="lazy"
                        />
                      </a>
                    )}
                    <figcaption className="vlozhenie-podpis">
                      <span className="truncate" title={file.original_name}>
                        {file.original_name}
                      </span>
                      <button
                        type="button"
                        className="btn-icon"
                        title={t("delete")}
                        onClick={() => void snyat(file.id)}
                      >
                        <Icon name="trash" size={13} />
                      </button>
                    </figcaption>
                  </figure>
                ))}
              </div>
            )}
          </div>
        </div>
      </div>
    </Modal>
  );
}
