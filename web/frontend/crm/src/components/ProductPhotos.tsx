import { useCallback, useEffect, useRef, useState } from "react";

import { Icon } from "./Icon";
import { ConfirmModal } from "./ui";
import { api, ApiError, type Zalivka } from "../lib/api";
import { useApp } from "../lib/app";
import { fileSize } from "../lib/format";
import { useGuard } from "../lib/guard";
import { can } from "../lib/permissions";

/**
 * Снимки одного товара.
 *
 * **Зачем.** Название опознаёт вещь плохо: «шлейф 40-pin» и «шлейф 40-pin
 * (узкий)» — две строки, отличить которые на полке можно только глазами.
 * Снимок отвечает на вопрос «это она?» за секунду, а описание — никогда.
 *
 * Отдельным файлом, как и коды: раздел, собранный в одном месте, снимается и
 * прячется одним условием, а размазанный по карточке — ножницами.
 *
 * Первый снимок — тот, что показывают везде, где место есть только под один.
 * Отдельного признака «главная» нет: он завёл бы инвариант «ровно одна», а
 * порядок и так задаёт человек — и «первый» разъехаться сам не может.
 */

interface Photo {
  id: number;
  original_name: string;
  size_bytes: number;
  sort_order: number;
  created_at: string | null;
}

const dolya = (hod: { ushlo: number; vsego: number }) =>
  hod.vsego > 0 ? Math.min(1, hod.ushlo / hod.vsego) : 0;

export function ProductPhotos({ productId }: { productId: number }) {
  const { t, user, toastError } = useApp();
  const [items, setItems] = useState<Photo[] | null>(null);
  const [open, setOpen] = useState<Photo | null>(null);
  const [confirm, setConfirm] = useState<Photo | null>(null);
  // Ход заливки: снимок с телефона — это мегабайты, и на медленной связи без
  // полосы не видно, идёт ли что-нибудь вообще (владелец 23.09.2026).
  const [hod, setHod] = useState<{ ushlo: number; vsego: number } | null>(null);
  const zalivka = useRef<Zalivka<unknown> | null>(null);
  const guard = useGuard();
  const vybor = useRef<HTMLInputElement>(null);

  // Ушли с карточки посреди заливки — бросаем её: полоса, которой никто не видит,
  // только держит соединение.
  useEffect(() => () => zalivka.current?.otmenit(), []);

  const mozhno_pravit = can(user, "warehouse.edit");

  const load = useCallback(async () => {
    try {
      const data = await api.get<{ items: Photo[] }>(`/warehouse/products/${productId}/photos`);
      setItems(data.items);
    } catch (e) {
      // 403 — блок выключили, пока карточка была открыта; 404 — товара уже нет.
      // И то и другое означает «раздела просто нет», а не ошибку человека.
      if (e instanceof ApiError && (e.status === 403 || e.status === 404)) {
        setItems(null);
        return;
      }
      toastError(e);
    }
  }, [productId, toastError]);

  useEffect(() => {
    void load();
  }, [load]);

  if (items === null) return null;

  const prilozhit = async (file: File) => {
    if (!guard.take()) return;
    setHod({ ushlo: 0, vsego: file.size });
    const rabota = api.zagruzka(`/warehouse/products/${productId}/photos`, file, (k) =>
      setHod({ ushlo: k.ushlo, vsego: k.vsego }),
    );
    zalivka.current = rabota;
    try {
      await rabota.gotovo;
      await load();
    } catch (e) {
      if (!(e instanceof ApiError && e.code === "canceled")) toastError(e);
    } finally {
      zalivka.current = null;
      setHod(null);
      guard.free();
    }
  };

  const udalit = async (photo: Photo) => {
    try {
      await api.del(`/warehouse/products/${productId}/photos/${photo.id}`);
      if (open?.id === photo.id) setOpen(null);
      await load();
    } catch (e) {
      toastError(e);
    }
  };

  return (
    <>
      <div className="section-head" style={{ marginTop: 28 }}>
        <h2 className="section-title">{t("prodPhotos")}</h2>
        {mozhno_pravit && (
          <button
            className="btn btn-secondary btn-sm"
            style={{ marginLeft: "auto" }}
            disabled={guard.busy}
            onClick={() => vybor.current?.click()}
          >
            <Icon name="upload" size={13} />
            {t("prodPhotoAdd")}
          </button>
        )}
        <input
          ref={vybor}
          type="file"
          accept="image/*"
          hidden
          onChange={(e) => {
            const file = e.target.files?.[0];
            if (file) void prilozhit(file);
            // Сбрасываем, иначе повторный выбор того же файла не вызовет
            // `change` и человек решит, что кнопка сломалась.
            e.target.value = "";
          }}
        />
      </div>

      {items.length === 0 && !hod ? (
        <div className="field-desc">{t("prodNoPhotos")}</div>
      ) : (
        <div className="photo-grid">
          {items.map((photo, mesto) => (
            <div className="photo-cell" key={photo.id}>
              <button
                type="button"
                className="photo-open"
                title={photo.original_name}
                onClick={() => setOpen(photo)}
              >
                <img
                  src={`/api/v1/warehouse/products/${productId}/photos/${photo.id}?size=thumb`}
                  alt={photo.original_name}
                  loading="lazy"
                />
              </button>
              {/* Подпись только у первого: она объясняет, почему именно он
                  стоит в списке товаров, а на остальных была бы шумом. */}
              {mesto === 0 && <span className="photo-first">{t("prodPhotoFirst")}</span>}
              {mozhno_pravit && (
                <button
                  type="button"
                  className="photo-drop"
                  aria-label={t("delete")}
                  title={t("delete")}
                  onClick={() => setConfirm(photo)}
                >
                  <Icon name="x" size={12} />
                </button>
              )}
            </div>
          ))}
          {/* Заглушка стоит там, куда ляжет снимок, — в конце: новый идёт последним.
              После 100 % полоса ждёт, пока сервер ужмёт снимок, — это секунда-две. */}
          {hod && (
            <div className="photo-cell">
              <div
                className="photo-open photo-upload"
                role="progressbar"
                aria-label={t("uploading")}
                aria-valuemin={0}
                aria-valuemax={100}
                aria-valuenow={Math.round(dolya(hod) * 100)}
              >
                <div className="upload-bar">
                  <div className="upload-progress" style={{ width: `${dolya(hod) * 100}%` }} />
                </div>
                <div className="upload-digits">
                  <span>{Math.round(dolya(hod) * 100)}%</span>
                  <span>{fileSize(hod.ushlo)} / {fileSize(hod.vsego)}</span>
                </div>
              </div>
              <button
                type="button"
                className="photo-drop photo-drop-shown"
                aria-label={t("uploadCancel")}
                title={t("uploadCancel")}
                onClick={() => zalivka.current?.otmenit()}
              >
                <Icon name="x" size={12} />
              </button>
            </div>
          )}
        </div>
      )}

      {open && (
        <div
          className="photo-viewer"
          role="dialog"
          aria-label={open.original_name}
          onClick={() => setOpen(null)}
        >
          <img
            src={`/api/v1/warehouse/products/${productId}/photos/${open.id}`}
            alt={open.original_name}
          />
        </div>
      )}

      {confirm && (
        <ConfirmModal
          text={t("prodPhotoDropConfirm", { name: confirm.original_name })}
          confirmLabel={t("delete")}
          danger
          onConfirm={() => void udalit(confirm)}
          onClose={() => setConfirm(null)}
        />
      )}
    </>
  );
}
