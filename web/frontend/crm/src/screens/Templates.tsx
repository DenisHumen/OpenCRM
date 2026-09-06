import { useCallback, useEffect, useState, type FormEvent } from "react";

import { Icon } from "../components/Icon";
import { VyborKlienta } from "../components/VyborKlienta";
import {
  Chip,
  ConfirmModal,
  EmptyState,
  LoadFailed,
  Modal,
  ScreenLoading,
} from "../components/ui";
import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { useLiveTopic } from "../lib/live";
import { useFailure } from "../lib/failure";
import { formatDate } from "../lib/format";
import { useGuard } from "../lib/guard";
import type { TFunc, TranslationKey } from "../lib/i18n";
import { can } from "../lib/permissions";
import { useReference } from "../lib/reference";
import { term } from "../lib/terms";

export interface MessageTemplate {
  id: number;
  name: string;
  /** any | email | note — где шаблон применим. */
  channel: string;
  body: string;
  created_at: string | null;
  updated_at: string | null;
}

/** Поле закрытого набора: приходит с сервера (`core/services/template_service.py`). */
interface TemplateField {
  key: string;
  /** "" | "client" | "deal" — что нужно, чтобы у поля было значение. */
  needs: string;
}

interface Rendered {
  text: string;
  /** Поля, у которых значения законно нет: у заявки нет доски, у клиента фирмы. */
  missing: string[];
  /** Поля, которых нет в наборе: их видно в тексте как `[?имя]`. */
  unknown: string[];
}

const CHANNELS = ["any", "email", "note"] as const;

const CHANNEL_LABEL: Record<string, TranslationKey> = {
  any: "templateChannelAny",
  email: "templateChannelEmail",
  note: "templateChannelNote",
};

/** Подписи полей. Забытое поле показывается своим ключом — тем самым, что
 *  пишут в теле шаблона, — а не исчезает из подсказки. */
const FIELD_LABEL: Record<string, TranslationKey> = {
  client_name: "templateFieldClientName",
  client_company: "templateFieldClientCompany",
  board_url: "templateFieldBoardUrl",
  company_name: "templateFieldCompanyName",
};

/** Поля заявки подписываются словом самого бизнеса: у мастерской это «заявка»,
 *  у магазина «заказ». Через двоеточие, а не в родительном падеже: «Название
 *  заказа» и «Название запись» требуют словаря форм, а «Заказ: название»
 *  правильно при любом слове. */
const DEAL_PART: Record<string, TranslationKey> = {
  deal_title: "templateFieldTitle",
  deal_number: "templateFieldNumber",
};

const NEEDS_LABEL: Record<string, TranslationKey> = {
  client: "templateNeedsClient",
  deal: "templateNeedsDeal",
};

/** Как назвать поле человеку. Ключ, которого нет в картах, показывается как
 *  есть: это ровно то, что пишут в теле, — подсказка не врёт и не пустеет. */
function fieldLabel(key: string, t: TFunc, dealWord: string): string {
  if (DEAL_PART[key]) return `${dealWord}: ${t(DEAL_PART[key])}`;
  return FIELD_LABEL[key] ? t(FIELD_LABEL[key]) : key;
}

export function Templates() {
  const { t, user, locale, toastError } = useApp();
  const [items, setItems] = useState<MessageTemplate[] | null>(null);
  // Что сейчас правят. `null` — ничего, "new" — заводят новый: разделять эти
  // два состояния двумя флагами значит однажды открыть оба окна разом.
  const [editing, setEditing] = useState<MessageTemplate | "new" | null>(null);
  const [previewing, setPreviewing] = useState<MessageTemplate | null>(null);
  // Удаление спрашивается: шаблон пишут один раз и правят годами, а восстановить
  // его неоткуда — корзины у заготовок нет намеренно.
  const [asking, setAsking] = useState<MessageTemplate | null>(null);

  const mayCreate = can(user, "templates.create");
  const mayEdit = can(user, "templates.edit");

  const { failure, fail, clear } = useFailure();

  const load = useCallback(async () => {
    clear();
    try {
      const data = await api.get<{ items: MessageTemplate[] }>("/templates");
      setItems(data.items);
    } catch (e) {
      fail(e);
    }
  }, [fail, clear]);

  useEffect(() => {
    void load();
  }, [load]);

  useLiveTopic("templates", () => void load());

  if (!items) return <ScreenLoading error={failure} onRetry={() => void load()} />;

  const remove = async (template: MessageTemplate) => {
    try {
      await api.del(`/templates/${template.id}`);
      setEditing(null);
      void load();
    } catch (e) {
      toastError(e);
    }
  };

  return (
    <div className="page page-narrow">
      <div className="page-head">
        <div>
          <h1 className="page-title">{t("templates")}</h1>
          <div className="page-sub">{t("templatesSub", { total: items.length })}</div>
        </div>
        {/* Кнопки нет у того, кому сервер откажет: интерфейс прячет то, что
            всё равно получит отказ. */}
        {mayCreate && (
          <button className="btn btn-primary" onClick={() => setEditing("new")}>
            <Icon name="plus" stroke={2} />
            {t("newTemplate")}
          </button>
        )}
      </div>

      {items.length === 0 ? (
        <EmptyState title={t("noTemplates")} sub={t("noTemplatesHint")} />
      ) : (
        <div className="tpl-grid">
          {/* Карточки — перевод stale-yak-33 (docs/18): канал сверху, имя, две
              строки текста, «обновлено», под чертой действия. Удаление отсюда, а
              не только из окна правки: до этого шаблон удаляли в три нажатия. */}
          {items.map((template) => (
            <div key={template.id} className="card tpl-card">
              <div className="tpl-chan">
                <Chip>{t(CHANNEL_LABEL[template.channel] ?? "templateChannelAny")}</Chip>
              </div>
              <div className="tpl-name">{template.name}</div>
              <div className="tpl-body">{template.body}</div>
              {template.updated_at && (
                <div className="tpl-meta">
                  <span>{t("updated")}:</span>
                  <span>{formatDate(template.updated_at, locale)}</span>
                </div>
              )}
              <div className="tpl-actions">
                {/* Предпросмотр только у сохранённого: подстановку считает
                    сервер, и считать её не по чему, пока шаблона нет. */}
                <button type="button" className="tpl-act tpl-act-view" onClick={() => setPreviewing(template)}>
                  <Icon name="eye" size={15} />
                  {t("templatePreview")}
                </button>
                {mayEdit && (
                  <button type="button" className="tpl-act tpl-act-edit" onClick={() => setEditing(template)}>
                    <Icon name="note" size={15} />
                    {t("edit")}
                  </button>
                )}
                {mayEdit && (
                  <button type="button" className="tpl-act tpl-act-del" onClick={() => setAsking(template)}>
                    <Icon name="trash" size={15} />
                    {t("delete")}
                  </button>
                )}
              </div>
            </div>
          ))}
        </div>
      )}

      {editing && (
        <TemplateModal
          template={editing === "new" ? null : editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            void load();
          }}
          onDelete={editing === "new" ? undefined : () => setAsking(editing)}
        />
      )}

      {asking && (
        <ConfirmModal
          text={t("deleteTemplateConfirm", { name: asking.name })}
          confirmLabel={t("delete")}
          danger
          onConfirm={() => void remove(asking)}
          onClose={() => setAsking(null)}
        />
      )}

      {previewing && (
        <PreviewModal template={previewing} onClose={() => setPreviewing(null)} />
      )}
    </div>
  );
}

/** Заведение и правка. Набор полей приходит с сервера — второй его копии во
 *  фронтенде нет: список во втором экземпляре разошёлся бы с реестром на первом
 *  же новом поле, и заметить это было бы некому. */
function TemplateModal({
  template,
  onClose,
  onSaved,
  onDelete,
}: {
  template: MessageTemplate | null;
  onClose: () => void;
  onSaved: () => void;
  onDelete?: () => void;
}) {
  const { t, locale, workspace, toastError } = useApp();
  const [name, setName] = useState(template?.name ?? "");
  const [channel, setChannel] = useState(template?.channel ?? "any");
  const [body, setBody] = useState(template?.body ?? "");
  const fields = useReference<TemplateField>("/templates/fields");
  const dealWord = term(workspace.deal_term, locale, "one");
  // Засов, а не флаг состояния: форма отправляется и Enter'ом, и кнопкой, а два
  // обработчика в одном тике читают `busy` каждый из своего замыкания и оба
  // видят `false`. Второй шаблон с тем же названием сервер отвергнет
  // (`template_name_taken`), но показывать человеку конфликт там, где он просто
  // дважды нажал, незачем.
  const guard = useGuard();

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!guard.take()) return;
    const payload = { name: name.trim(), channel, body };
    try {
      if (template) {
        await api.patch(`/templates/${template.id}`, payload);
      } else {
        await api.post("/templates", payload);
      }
      onSaved();
    } catch (err) {
      // Отказ показывается словами: «Unknown placeholder(s): {clietn_name}» —
      // это ровно то, что человеку надо исправить, и общая фраза «не удалось»
      // отняла бы у него эту подсказку.
      toastError(err);
      guard.free();
    }
  };

  return (
    <Modal title={template ? template.name : t("newTemplate")} onClose={onClose}>
      <form onSubmit={submit}>
        <div className="field">
          <label className="label">{t("templateName")}</label>
          <input
            className="input"
            value={name}
            onChange={(e) => setName(e.target.value)}
            autoFocus
            required
          />
          <div className="field-desc">{t("templateNameHint")}</div>
        </div>

        <div className="field">
          <label className="label">{t("templateChannel")}</label>
          <select
            className="input"
            value={channel}
            onChange={(e) => setChannel(e.target.value)}
          >
            {CHANNELS.map((key) => (
              <option key={key} value={key}>
                {t(CHANNEL_LABEL[key])}
              </option>
            ))}
          </select>
        </div>

        <div className="field">
          <label className="label">{t("templateBody")}</label>
          <textarea
            className="textarea"
            style={{ minHeight: 150 }}
            value={body}
            onChange={(e) => setBody(e.target.value)}
            required
          />
          <div className="field-desc">{t("templateBodyHint")}</div>
        </div>

        {/* Набор закрыт: написать в теле можно только то, что перечислено
            здесь. Незнакомое имя сервер не сохранит и назовёт его — молча
            пустым местом посреди письма оно не станет. */}
        <div className="field">
          <label className="label">{t("templateFields")}</label>
          {fields.failure ? (
            <LoadFailed error={fields.failure} onRetry={fields.reload} />
          ) : (
            <div className="list-card">
              {(fields.items ?? []).map((field) => (
                <button
                  key={field.key}
                  type="button"
                  className="list-row hoverable"
                  style={{ width: "100%", textAlign: "left", background: "none" }}
                  title={t("templateInsert")}
                  onClick={() => setBody((current) => `${current}{${field.key}}`)}
                >
                  <code style={{ color: "var(--brand)", fontSize: 12 }}>{`{${field.key}}`}</code>
                  <span style={{ flex: 1, color: "var(--muted)", fontSize: 12 }}>
                    {fieldLabel(field.key, t, dealWord)}
                  </span>
                  {NEEDS_LABEL[field.needs] && (
                    <span style={{ color: "var(--faint)", fontSize: 11 }}>
                      {t(NEEDS_LABEL[field.needs])}
                    </span>
                  )}
                </button>
              ))}
            </div>
          )}
          <div className="field-desc">{t("templateFieldsHint")}</div>
        </div>

        <div style={{ display: "flex", gap: 8, marginTop: 20 }}>
          <button className="btn btn-primary" style={{ flex: 1 }} disabled={guard.busy}>
            {template ? t("save") : t("create")}
          </button>
          {onDelete && (
            <button type="button" className="btn btn-secondary" onClick={onDelete}>
              {t("delete")}
            </button>
          )}
        </div>
      </form>
    </Modal>
  );
}

/**
 * Предпросмотр: как шаблон выглядит для этого клиента и этой заявки.
 *
 * Считает подстановку сервер, а не экран. Второй экземпляр правила разошёлся бы
 * с первым молча, и разошёлся бы там, где ошибку видит клиент, а не автор.
 *
 * Без выбранного клиента предпросмотр всё равно показывается: все поля про
 * клиента станут прочерками, и это честный ответ на вопрос «как выглядит
 * шаблон», а не отказ отвечать.
 */
function PreviewModal({
  template,
  onClose,
}: {
  template: MessageTemplate;
  onClose: () => void;
}) {
  const { t, locale, user, workspace } = useApp();
  const [clientId, setClientId] = useState("");
  const [dealId, setDealId] = useState("");
  const [result, setResult] = useState<Rendered | null>(null);
  // Повтор после отказа идёт тем же путём, что и первый заход: два разных
  // способа перезагрузить одно и то же расходятся в поведении с первой правкой.
  const [attempt, setAttempt] = useState(0);

  // `null` — спрашивать нечего: права нет, и отказ сервера был бы не отказом, а
  // стуком в закрытую дверь на каждом открытии окна.
  const [imya_klienta, setImyaKlienta] = useState("");
  const deals = useReference<{ id: number; title: string }>(
    clientId && can(user, "deals.view") ? `/deals?client_id=${clientId}` : null,
  );

  const { failure, fail, clear } = useFailure();

  useEffect(() => {
    // Клиента переключают быстрее, чем отвечает сервер: без этого флажка ответ
    // по прошлому выбору лёг бы поверх нового, и человек увидел бы чужое имя
    // под именем выбранного клиента.
    let current = true;
    clear();
    setResult(null);
    const params = new URLSearchParams();
    if (clientId) params.set("client_id", clientId);
    if (dealId) params.set("deal_id", dealId);
    const query = params.toString();
    api
      .get<Rendered>(`/templates/${template.id}/render${query ? `?${query}` : ""}`)
      .then((data) => {
        if (current) setResult(data);
      })
      .catch((e) => {
        if (current) fail(e);
      });
    return () => {
      current = false;
    };
  }, [template.id, clientId, dealId, attempt, fail, clear]);

  const dealWord = term(workspace.deal_term, locale, "one");
  const names = (keys: string[]) =>
    keys.map((key) => fieldLabel(key, t, dealWord)).join(", ");

  return (
    <Modal title={t("templatePreview")} onClose={onClose}>
      <div className="field">
        <label className="label">{t("client")}</label>
        {/* Право на клиентов было условием загрузки справочника — остаётся
            условием поля: без него сервер откажет, а человеку показали бы
            поле, которое ничего не находит. */}
        {can(user, "clients.view") && (
        <VyborKlienta
          value={clientId ? Number(clientId) : null}
          imya={imya_klienta || null}
          pustoy
          onPick={(kto, imya) => {
            setImyaKlienta(imya ?? "");
            setClientId(kto ? String(kto) : "");
            // Заявка принадлежит клиенту: оставить её при смене клиента
            // значит попросить сервер о паре, которой не существует, и
            // получить `deal_other_client` на пустом месте.
            setDealId("");
          }}
          pustoyPodpis={t("templatePreviewNobody")}
        />
        )}
      </div>

      {clientId && (
        <div className="field">
          {/* Слово подставляет сам бизнес: у мастерской «Заявка», у магазина
              «Заказ». Зашитое «Сделка» здесь называло бы чужим словом ровно то,
              что человек выбирает. */}
          <label className="label">{dealWord}</label>
          {deals.failure ? (
            <LoadFailed error={deals.failure} onRetry={deals.reload} />
          ) : (
            <select
              className="input"
              value={dealId}
              onChange={(e) => setDealId(e.target.value)}
            >
              <option value="">{t("templatePreviewNobody")}</option>
              {(deals.items ?? []).map((deal) => (
                <option key={deal.id} value={String(deal.id)}>
                  {deal.title}
                </option>
              ))}
            </select>
          )}
        </div>
      )}

      {failure ? (
        <LoadFailed error={failure} onRetry={() => setAttempt((n) => n + 1)} />
      ) : (
        <div className="card card-pad" style={{ marginTop: 4 }}>
          <div style={{ whiteSpace: "pre-wrap", color: "var(--text)", fontSize: 13 }}>
            {result ? result.text : t("loading")}
          </div>
        </div>
      )}

      {/* Прочерк посреди предложения объясняется словами. Иначе человек ищет
          опечатку в шаблоне там, где на самом деле у клиента просто не
          заполнена фирма. */}
      {result && result.missing.length > 0 && (
        <div className="field-desc">
          {t("templateMissing", { list: names(result.missing) })} {t("templateMissingHint")}
        </div>
      )}
      {/* Поля, которого больше нет в наборе: в тексте оно стоит как `[?имя]`.
          Молча пустым местом оно не станет — но и починит его только правка
          шаблона, поэтому говорим об этом отдельно и громче. */}
      {result && result.unknown.length > 0 && (
        <div className="form-error">
          {t("templateUnknown", { list: result.unknown.join(", ") })}
        </div>
      )}
    </Modal>
  );
}
