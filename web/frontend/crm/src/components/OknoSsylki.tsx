import { useCallback, useEffect, useState } from "react";

import { Icon } from "./Icon";
import { ConfirmModal, Modal, Spinner } from "./ui";
import { api } from "../lib/api";
import { copyText } from "../lib/clipboard";
import { useApp } from "../lib/app";
import { useGuard } from "../lib/guard";

/**
 * Окно «Доступ по ссылке»: четыре решения, каждое отдельным блоком.
 *
 * Кому открыт, что можно делать, срок и сам адрес. Механика взята у витрин
 * досок — код, срок, счётчик открытий, отзыв, — и она уже проверена людьми;
 * писать вторую значило бы получить два набора условий на один вопрос.
 *
 * **«Только приглашённым» здесь нет.** Список почт и журнал входов по нему —
 * отдельная работа; вариант в окне без него был бы обещанием, а не выбором.
 */

interface Ssylka {
  id: number;
  url: string;
  rezhim: "view" | "download";
  krug: "link" | "code";
  is_active: boolean;
  expires_at: string | null;
  has_code: boolean;
  views_count: number;
  unique_views_count: number;
  last_viewed_at: string | null;
}

/** Сколько дней живёт ссылка. «Без срока» первым: чаще всего им и пользуются,
 *  а срок ставят осознанно. */
const SROKI = [0, 7, 30, 90] as const;

function cherez(dney: number): string | null {
  if (dney === 0) return null;
  const kogda = new Date();
  kogda.setDate(kogda.getDate() + dney);
  return kogda.toISOString().slice(0, 19);
}

function ostalos(iso: string | null): number {
  if (!iso) return 0;
  const dney = Math.round((new Date(iso).getTime() - Date.now()) / 86_400_000);
  return SROKI.find((d) => d >= dney) ?? 90;
}

export function OknoSsylki({ nomer, imya, onClose }: { nomer: string; imya: string; onClose: () => void }) {
  const { t, toast, toastError } = useApp();
  const [ssylka, setSsylka] = useState<Ssylka | null | undefined>(undefined);
  const [kod, setKod] = useState("");
  const [otzyv, setOtzyv] = useState(false);
  const guard = useGuard();

  const chitat = useCallback(() => {
    api
      .get<{ link: Ssylka | null }>(`/files/${encodeURIComponent(nomer)}/link`)
      .then((d) => setSsylka(d.link))
      .catch((e) => {
        toastError(e);
        setSsylka(null);
      });
  }, [nomer, toastError]);

  useEffect(() => chitat(), [chitat]);

  /** Одна дорога на выпуск и на правку: сервер сам решает, завести ссылку или
   *  поправить существующую. Две дороги разошлись бы на первой же проверке. */
  const zapisat = async (chto: Partial<Ssylka> & { pin?: string | null }) => {
    if (!guard.take()) return;
    const bylo = ssylka;
    const telo = {
      rezhim: chto.rezhim ?? bylo?.rezhim ?? "view",
      krug: chto.krug ?? bylo?.krug ?? "link",
      pin: chto.pin !== undefined ? chto.pin : undefined,
      expires_at: chto.expires_at !== undefined ? chto.expires_at : (bylo?.expires_at ?? null),
    };
    try {
      const otvet = bylo
        ? await api.patch<{ link: Ssylka }>(`/files/links/${bylo.id}`, telo)
        : await api.post<{ link: Ssylka }>(`/files/${encodeURIComponent(nomer)}/link`, telo);
      setSsylka(otvet.link);
      setKod("");
    } catch (e) {
      toastError(e);
    } finally {
      guard.free();
    }
  };

  const otozvat = async () => {
    if (!ssylka) return;
    try {
      await api.del(`/files/links/${ssylka.id}`);
      setSsylka(null);
      setOtzyv(false);
      toast(t("linkRevoked"));
    } catch (e) {
      toastError(e);
    }
  };

  const skopirovat = async () => {
    if (!ssylka) return;
    // Через `copyText`: по HTTP `navigator.clipboard` отсутствует, и прямой
    // вызов был бы молчаливым отказом — человек жмёт «Скопировать», ничего не
    // происходит, и он не знает, скопировалось или нет.
    if (await copyText(ssylka.url)) toast(t("copied"));
    else toastError(new Error(t("copyFailed")));
  };

  return (
    <Modal title={t("linkAccess")} onClose={onClose}>
      <div className="ssylka-fayl truncate">{imya}</div>

      {ssylka === undefined ? (
        <div className="potok-zhdyom">
          <Spinner />
        </div>
      ) : (
        <>
          <div className="ssylka-blok">
            <div className="ssylka-podpis">{t("linkAudience")}</div>
            <div className="ssylka-vybor">
              <button
                type="button"
                className={"ssylka-knopka" + (ssylka?.krug !== "code" ? " ssylka-knopka-on" : "")}
                disabled={guard.busy}
                onClick={() => void zapisat({ krug: "link", pin: null })}
              >
                <span className="ssylka-imya">{t("linkAnyone")}</span>
                <span className="ssylka-tishe">{t("linkAnyoneHint")}</span>
              </button>
              <button
                type="button"
                className={"ssylka-knopka" + (ssylka?.krug === "code" ? " ssylka-knopka-on" : "")}
                disabled={guard.busy || (!ssylka?.has_code && kod.trim().length < 4)}
                title={!ssylka?.has_code && kod.trim().length < 4 ? t("linkCodeFirst") : undefined}
                onClick={() => void zapisat({ krug: "code", pin: kod.trim() || undefined })}
              >
                <span className="ssylka-imya">{t("linkByCode")}</span>
                <span className="ssylka-tishe">{t("linkByCodeHint")}</span>
              </button>
            </div>
            {/* Поле кода стоит рядом с выбором, а не в отдельном блоке ниже:
                «по коду» без кода — открытая ссылка, которая называется
                закрытой, и сервер такое отвергает. */}
            <label className="ssylka-kod">
              <span className="label">{t("linkCode")}</span>
              <input
                className="input input-sm"
                inputMode="numeric"
                maxLength={8}
                placeholder={ssylka?.has_code ? "••••" : "4079"}
                value={kod}
                onChange={(e) => setKod(e.target.value.replace(/\D/g, ""))}
              />
              {ssylka?.has_code && (
                <button
                  type="button"
                  className="text-link"
                  disabled={guard.busy}
                  onClick={() => void zapisat({ krug: "link", pin: null })}
                >
                  {t("linkCodeOff")}
                </button>
              )}
            </label>
          </div>

          <div className="ssylka-blok">
            <div className="ssylka-podpis">{t("linkWhat")}</div>
            <div className="ssylka-vybor">
              <button
                type="button"
                className={"ssylka-knopka" + (ssylka?.rezhim !== "download" ? " ssylka-knopka-on" : "")}
                disabled={guard.busy}
                onClick={() => void zapisat({ rezhim: "view" })}
              >
                <span className="ssylka-imya">{t("linkViewOnly")}</span>
                <span className="ssylka-tishe">{t("linkViewOnlyHint")}</span>
              </button>
              <button
                type="button"
                className={"ssylka-knopka" + (ssylka?.rezhim === "download" ? " ssylka-knopka-on" : "")}
                disabled={guard.busy}
                onClick={() => void zapisat({ rezhim: "download" })}
              >
                <span className="ssylka-imya">{t("linkDownload")}</span>
                <span className="ssylka-tishe">{t("linkDownloadHint")}</span>
              </button>
            </div>
          </div>

          {/* Блок защиты появляется только у «только смотреть»: с открытым
              скачиванием защищать нечего, и показывать его там значило бы
              обещать то, чего в этом режиме нет. */}
          {ssylka?.rezhim !== "download" && (
            <div className="ssylka-blok ssylka-zashchita">
              <div className="ssylka-podpis">{t("linkDrm")}</div>
              <div className="ssylka-tishe">{t("linkDrmWhat")}</div>
              <div className="ssylka-chestno">{t("linkDrmHonest")}</div>
            </div>
          )}

          <div className="ssylka-blok">
            <div className="ssylka-podpis">{t("linkExpiry")}</div>
            <div className="potok-okna ssylka-sroki">
              {SROKI.map((d) => (
                <button
                  key={d}
                  type="button"
                  className={"potok-okno" + (ostalos(ssylka?.expires_at ?? null) === d ? " potok-okno-on" : "")}
                  disabled={guard.busy}
                  onClick={() => void zapisat({ expires_at: cherez(d) })}
                >
                  {d === 0 ? t("linkForever") : t("linkDays", { n: d })}
                </button>
              ))}
            </div>
          </div>

          {ssylka ? (
            <div className="ssylka-blok">
              <div className="ssylka-podpis">{t("linkItself")}</div>
              <div className="ssylka-adres">
                <input className="input input-sm" readOnly value={ssylka.url} onFocus={(e) => e.target.select()} />
                <button type="button" className="btn btn-secondary btn-sm" onClick={() => void skopirovat()}>
                  <Icon name="copy" size={13} />
                  {t("copy")}
                </button>
              </div>
              <div className="ssylka-schyot">
                {t("linkOpened", { n: ssylka.views_count, people: ssylka.unique_views_count })}
              </div>
              <button type="button" className="text-link danger" onClick={() => setOtzyv(true)}>
                {t("linkRevoke")}
              </button>
            </div>
          ) : (
            <div className="field-desc">{t("linkNotYet")}</div>
          )}
        </>
      )}

      {otzyv && (
        <ConfirmModal
          text={t("linkRevokeConfirm")}
          confirmLabel={t("linkRevoke")}
          danger
          onConfirm={() => void otozvat()}
          onClose={() => setOtzyv(false)}
        />
      )}
    </Modal>
  );
}
