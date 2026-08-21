import { useCallback, useEffect, useRef, useState } from "react";

import { Icon } from "./Icon";
import { ConfirmModal } from "./ui";
import { api, ApiError } from "../lib/api";
import { useApp } from "../lib/app";
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

export function ProductPhotos({ productId }: { productId: number }) {
  const { t, user, toastError } = useApp();
  const [items, setItems] = useState<Photo[] | null>(null);
  const [open, setOpen] = useState<Photo | null>(null);
  const [confirm, setConfirm] = useState<Photo | null>(null);
  const guard = useGuard();
  const vybor = useRef<HTMLInputElement>(null);

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
    try {
      await api.upload(`/warehouse/products/${productId}/photos`, file);
      await load();
    } catch (e) {
      toastError(e);
    } finally {
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

      {items.length === 0 ? (
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
