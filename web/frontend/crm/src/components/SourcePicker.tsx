import { useEffect, useState } from "react";

import { useApp } from "../lib/app";
import { CLIENT_SOURCES, sourceLabel } from "../lib/sources";

/** Значение пункта «свой источник». В базу не попадает — только в разметку. */
const CUSTOM = "__custom__";
/** Совпадает с MAX_SOURCE на сервере: обрезать молча на бэкенде — хуже. */
const MAX_LENGTH = 32;

function isPreset(value: string): boolean {
  return (CLIENT_SOURCES as readonly string[]).includes(value);
}

/**
 * Откуда пришёл клиент: список плюс возможность вписать своё.
 *
 * Список, а не свободное поле: «сарафан», «по рекомендации» и «от знакомых» —
 * это один источник, и написанные по-разному они разваливают отчёт на три
 * строки по одной штуке. Но и только список нельзя: справочник из шести слов не
 * покроет ни одного дела целиком, а невозможность записать правду учит
 * оставлять поле пустым.
 *
 * Пусто — «не спросили», и это отдельное значение, а не синоним «другое».
 */
export function SourcePicker({
  value,
  onChange,
  onCommit,
}: {
  value: string;
  /** Каждое изменение — для формы, которая держит своё состояние. */
  onChange?: (next: string) => void;
  /** Готовое значение — для карточки, которая сохраняет сразу. Своё значение
   *  отдаётся по уходу из поля, а не на каждую букву: иначе PATCH уходил бы
   *  после каждого нажатия. */
  onCommit?: (next: string) => void;
}) {
  const { t } = useApp();
  const [custom, setCustom] = useState(!!value && !isPreset(value));
  const [draft, setDraft] = useState(value);

  useEffect(() => {
    setDraft(value);
    // Только включаем режим своего значения, но не выключаем: выключение здесь
    // спорило бы с переходом в него — тот начинается с пустой строки, и
    // эффект тут же вернул бы список.
    if (value && !isPreset(value)) setCustom(true);
  }, [value]);

  const commit = (next: string) => {
    onChange?.(next);
    onCommit?.(next);
  };

  if (custom) {
    return (
      <div style={{ display: "flex", gap: 8 }}>
        <input
          className="input"
          value={draft}
          maxLength={MAX_LENGTH}
          autoFocus
          placeholder={t("srcCustom")}
          onChange={(e) => {
            setDraft(e.target.value);
            onChange?.(e.target.value);
          }}
          onBlur={() => commit(draft.trim())}
          onKeyDown={(e) => {
            if (e.key === "Enter") (e.target as HTMLInputElement).blur();
          }}
        />
        <button
          type="button"
          className="btn btn-secondary"
          onClick={() => {
            setCustom(false);
            setDraft("");
            commit("");
          }}
        >
          {t("cancel")}
        </button>
      </div>
    );
  }

  return (
    <select
      className="select"
      value={isPreset(value) ? value : ""}
      onChange={(e) => {
        if (e.target.value === CUSTOM) {
          setCustom(true);
          setDraft("");
          onChange?.("");
          return;
        }
        commit(e.target.value);
      }}
    >
      <option value="">{t("srcNone")}</option>
      {CLIENT_SOURCES.map((key) => (
        <option key={key} value={key}>
          {sourceLabel(key, t)}
        </option>
      ))}
      <option value={CUSTOM}>{t("srcCustom")}</option>
    </select>
  );
}
