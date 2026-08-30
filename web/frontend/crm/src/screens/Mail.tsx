import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";

import { Icon } from "../components/Icon";
import { Dochitat, EmptyState, Modal, ScreenLoading } from "../components/ui";
import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { useDebounced } from "../lib/debounce";
import { useFailure } from "../lib/failure";
import { useGuard } from "../lib/guard";
import { formatDateTime } from "../lib/format";
import { can } from "../lib/permissions";
import { useReference } from "../lib/reference";

export interface MailMessage {
  id: number;
  account_id: number;
  direction: "in" | "out";
  subject: string;
  from_addr: string;
  to_addrs: string[];
  sent_at: string | null;
  has_attachments: boolean;
  client_id: number | null;
  is_read: boolean;
  body_text?: string;
  body_html?: string;
}

/**
 * Ящик, ИЗ КОТОРОГО можно писать. Три поля и только они.
 *
 * Не путать с настройкой ящика (`Mailboxes.tsx`): там хосты, порты и логины,
 * и закрыты они правом `settings.manage`. Чтобы отправить письмо, всего этого
 * знать не надо — нужен выбор из двух-трёх адресов, и его отдаёт `/mail/senders`
 * тому, у кого есть право писать.
 */
export interface MailSender {
  id: number;
  title: string;
  address: string;
}

type Filter = "all" | "in" | "out" | "unread";

/** По скольку писем дочитывается список. */
const NA_STRANITSE = 100;

export function Mail() {
  const { t, locale, toastError, user } = useApp();
  const [messages, setMessages] = useState<MailMessage[] | null>(null);
  const [total, setTotal] = useState(0);
  const [filter, setFilter] = useState<Filter>("all");
  const [search, setSearch] = useState("");
  const [open, setOpen] = useState<MailMessage | null>(null);
  /** На какое письмо отвечаем. Не булево: форме нужны и адрес, и тема, и id. */
  const [otvechaem, setOtvechaem] = useState<MailMessage | null>(null);
  const [composing, setComposing] = useState(false);

  // До какой страницы дочитан список. Прежде экран просил сотню писем и на
  // этом заканчивался — а в подзаголовке честно писал «всего N». Сам сообщал,
  // что показывает часть почты, и ничего с этим сделать не давал.
  const [stranitsa, setStranitsa] = useState(1);
  const [dochityvaem, setDochityvaem] = useState(false);
  // Отбор, которому принадлежит показанный список. Ставится загрузкой,
  // сверяется дочиткой: пока вторая страница по «иван» едет, человек успевает
  // набрать «п», первая страница «п» заменяет список — и опоздавшая страница
  // дописывается к чужим находкам. На экране два отбора вперемешку, а «всего»
  // от прошлого.
  const otbor_spiska = useRef("");

  const [attempt, setAttempt] = useState(0);

  const { failure, fail, clear } = useFailure();

  const typed = useDebounced(search);

  // Отбор без номера страницы: положи страницу сюда — и смена отбора станет
  // неотличима от перехода на следующую. Загрузка зависит только от отбора и
  // всегда просит первую страницу, дочитка приписывает номер сама.
  const otbor = useMemo(() => {
    const params = new URLSearchParams({ per_page: String(NA_STRANITSE) });
    if (filter === "in" || filter === "out") params.set("direction", filter);
    if (filter === "unread") params.set("unread", "true");
    if (typed.trim()) params.set("search", typed.trim());
    return `/mail/messages?${params}`;
  }, [filter, typed]);

  // Перечитывание после отправки письма начинается с первой страницы — как и
  // всякая загрузка здесь, отдельно счётчик сбрасывать больше не надо. Плата
  // за это — дочитанный хвост схлопывается до первой сотни, и так правильно:
  // перечитанная третья страница легла бы вторым слоем поверх уже показанной,
  // и письма задвоились бы.
  const reload = useCallback(() => {
    setAttempt((n) => n + 1);
  }, []);

  useEffect(() => {
    // Фильтры переключают быстрее, чем отвечает сервер: без счётчика ответ по
    // прошлому отбору ложился поверх текущего, и во «входящих» оказывались
    // исходящие. Приём тот же, что в отчётах и палитре команд.
    let current = true;
    otbor_spiska.current = otbor;
    clear();
    api
      .get<{ items: MailMessage[]; total: number }>(`${otbor}&page=1`)
      .then((data) => {
        if (!current) return;
        setMessages(data.items);
        setTotal(data.total);
        setStranitsa(1);
      })
      // Раньше здесь стоял пустой список — и экран показывал «писем нет» там,
      // где на деле не ответил сервер. Пустая почта и недоступная почта для
      // человека решения принимают разные: первую он закроет, вторую повторит.
      .catch((e) => {
        if (current) fail(e);
      });
    return () => {
      current = false;
    };
  }, [otbor, attempt, fail, clear]);

  /** Дочитать список.
   *
   * Отдельным действием, а не номером страницы в пути загрузки, и номер растёт
   * ПОСЛЕ удачного ответа. Иначе отказ на второй странице оставлял бы счётчик
   * на двойке, а следующее нажатие просило бы третью — вторая сотня писем
   * пропадала бы из списка навсегда и молча.
   *
   * Отказ говорит о себе всплывающей жалобой, а не через `fail`: `fail` рисует
   * экран «не удалось загрузить», а он виден только пока показывать нечего.
   * После первой удачной загрузки отказ дочитки не показал бы ничего вовсе —
   * кнопка просто переставала бы отвечать.
   */
  const dochitat = async () => {
    if (dochityvaem) return;
    setDochityvaem(true);
    const sprosheno = otbor;
    try {
      const dalshe = await api.get<{ items: MailMessage[]; total: number }>(
        `${otbor}&page=${stranitsa + 1}`,
      );
      // Отбор сменился, пока страница ехала, — ответ чужой.
      if (otbor_spiska.current !== sprosheno) return;
      setMessages((bylo) => (bylo ? [...bylo, ...dalshe.items] : dalshe.items));
      setTotal(dalshe.total);
      setStranitsa((bylo) => bylo + 1);
    } catch (e) {
      toastError(e);
    } finally {
      setDochityvaem(false);
    }
  };

  // Список ящиков нужен форме отправки: с какого адреса уходит письмо.
  // Менеджеру он недоступен (это настройка root), поэтому отказ здесь не беда:
  // сервер возьмёт первый активный ящик сам. Важно только не выдать «ящиков
  // нет» за ответ — на это и `null` в крючке справочника.
  // Ящики для ОТПРАВКИ, а не список настроек: общий список закрыт правом
  // `settings.manage`, и менеджеру с правом писать он отвечал отказом — ящиков
  // ноль, выбирать не из чего, кнопка ответа спрятана. Писать письма мог
  // только владелец системы.
  const accounts = useReference<MailSender>("/mail/senders");

  const openMessage = async (message: MailMessage) => {
    try {
      const full = await api.get<MailMessage>(`/mail/messages/${message.id}`);
      setOpen(full);
      if (!message.is_read) {
        await api.post(`/mail/messages/${message.id}/read`, { is_read: true });
        setMessages((prev) =>
          (prev ?? []).map((m) => (m.id === message.id ? { ...m, is_read: true } : m)),
        );
      }
    } catch (e) {
      toastError(e);
    }
  };

  if (!messages) return <ScreenLoading error={failure} onRetry={reload} />;

  const filters: { id: Filter; label: string }[] = [
    { id: "all", label: t("all") },
    { id: "in", label: t("incoming") },
    { id: "out", label: t("outgoing") },
    { id: "unread", label: t("unreadOnly") },
  ];

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1 className="page-title">{t("mail")}</h1>
          <div className="page-sub">{t("mailSub", { total })}</div>
        </div>
        <button className="btn btn-primary" onClick={() => setComposing(true)}>
          <Icon name="send" stroke={2} />
          {t("compose")}
        </button>
      </div>

      {/* Переносится по строкам: на узком экране поиск и четыре фильтра в одну
          строку не помещаются и утаскивают вбок всю страницу. */}
      <div style={{ display: "flex", gap: 10, alignItems: "center", margin: "18px 0", flexWrap: "wrap" }}>
        <input
          className="input"
          style={{ flex: 1, minWidth: 160 }}
          placeholder={t("searchMail")}
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
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
      </div>

      <div className="list-card">
        {messages.map((message) => (
          <div
            key={message.id}
            className="list-row hoverable"
            style={{ height: 56, cursor: "pointer" }}
            onClick={() => void openMessage(message)}
          >
            <div className="feed-icon">
              <Icon name={message.direction === "in" ? "inbox" : "send"} size={14} />
            </div>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div
                className="truncate"
                style={{
                  color: "var(--text)",
                  fontSize: 13.5,
                  fontWeight: message.is_read ? 500 : 600,
                }}
              >
                {message.subject || t("noSubject")}
              </div>
              <div className="truncate" style={{ color: "var(--faint)", fontSize: 12 }}>
                {message.direction === "in" ? message.from_addr : message.to_addrs.join(", ")}
              </div>
            </div>
            {message.has_attachments && <Icon name="folder" size={13} />}
            {message.client_id ? (
              <Link
                to={`/clients/${message.client_id}`}
                className="text-link"
                style={{ fontSize: 11.5 }}
                onClick={(e) => e.stopPropagation()}
              >
                {t("openClient")}
              </Link>
            ) : (
              <span style={{ color: "var(--faint)", fontSize: 11.5 }}>{t("unlinkedLetter")}</span>
            )}
            <div style={{ color: "var(--faint)", fontSize: 12, whiteSpace: "nowrap" }}>
              {formatDateTime(message.sent_at, locale)}
            </div>
          </div>
        ))}
        {messages.length === 0 && <EmptyState title={t("mailEmpty")} />}
        <Dochitat
          pokazano={messages.length}
          vsego={total}
          zanyat={dochityvaem}
          onClick={() => void dochitat()}
        />
      </div>

      {open && (
        <Modal title={open.subject || t("noSubject")} onClose={() => setOpen(null)} wide>
          <div style={{ color: "var(--faint)", fontSize: 12, marginBottom: 14 }}>
            {open.from_addr} → {open.to_addrs.join(", ")} · {formatDateTime(open.sent_at, locale)}
          </div>
          {/* html-тело письма приходит из внешнего мира и здесь не рендерится:
              вставить чужую разметку в интерфейс CRM — это XSS. Показываем текст;
              html лежит в базе и ждёт нормального санитайзера. */}
          <div style={{ whiteSpace: "pre-wrap", fontSize: 13.5, lineHeight: 1.6 }}>
            {open.body_text || "—"}
          </div>
          {/* Ответ — прямо отсюда, а не «напишите новое письмо».
              Пока кнопки не было, менеджер набирал адрес и тему заново, и
              собеседник получал ответ ОТДЕЛЬНЫМ письмом: заголовков цепочки в
              таком письме нет, и почтовый клиент не ставит его под вопросом.
              Проверено на двух живых серверах. */}
          {can(user, "mail.create") && (accounts.items ?? []).length > 0 && (
            <div style={{ marginTop: 18, display: "flex", justifyContent: "flex-end" }}>
              <button
                className="btn btn-primary"
                onClick={() => {
                  setOtvechaem(open);
                  setOpen(null);
                }}
              >
                {t("reply")}
              </button>
            </div>
          )}
        </Modal>
      )}

      {otvechaem && (
        <MailCompose
          accounts={accounts.items ?? []}
          replyTo={otvechaem}
          onClose={() => setOtvechaem(null)}
          onSent={reload}
        />
      )}

      {composing && (
        <MailCompose
          accounts={accounts.items ?? []}
          onClose={() => setComposing(false)}
          onSent={reload}
        />
      )}
    </div>
  );
}

/** Форма отправки. Живёт отдельно: её открывают и из карточки клиента. */
export function MailCompose({
  accounts,
  // Без умолчания "" намеренно: ниже стоит `to ?? …`, а пустая строка не
  // нулевая — с умолчанием ветка ответа не срабатывала НИКОГДА, и поле адреса
  // оставалось пустым. Поймано живым осмотром экрана.
  to,
  clientId,
  dealId,
  replyTo,
  onClose,
  onSent,
}: {
  accounts: MailSender[];
  to?: string;
  clientId?: number;
  dealId?: number;
  /** Письмо, на которое отвечаем. Оно задаёт адрес, тему и цепочку. */
  replyTo?: MailMessage;
  onClose: () => void;
  onSent?: () => void;
}) {
  const { t, toast, toastError } = useApp();
  // Отвечают ОТПРАВИТЕЛЮ входящего и ПОЛУЧАТЕЛЮ исходящего: во втором случае
  // человек дописывает собеседнику, а не самому себе.
  const komu =
    to ??
    (replyTo ? (replyTo.direction === "in" ? replyTo.from_addr : replyTo.to_addrs.join(", ")) : "");
  // «Re:» не удваивается: на «Re: Заявка» ответ остаётся «Re: Заявка».
  const tema = replyTo
    ? /^\s*re\s*:/i.test(replyTo.subject)
      ? replyTo.subject
      : `Re: ${replyTo.subject}`
    : "";
  const [form, setForm] = useState({ to: komu, subject: tema, body: "" });
  const [accountId, setAccountId] = useState<number | "">(
    // Отвечаем ИЗ ТОГО ЯЩИКА, куда письмо пришло. Ответ с другого адреса
    // приходит собеседнику от незнакомца и в переписку не встаёт.
    replyTo?.account_id ?? accounts[0]?.id ?? "",
  );
  // Засов, а не флаг состояния: отправка идёт через SMTP и отвечает не сразу.
  // Второе нажатие по «неответившей» кнопке слало клиенту второе такое же
  // письмо — и отозвать его уже нельзя ничем.
  const guard = useGuard();

  const send = async () => {
    if (!guard.take()) return;
    try {
      await api.post("/mail/send", {
        to: form.to.split(",").map((a) => a.trim()).filter(Boolean),
        subject: form.subject,
        body: form.body,
        account_id: accountId || null,
        client_id: clientId ?? null,
        deal_id: dealId ?? null,
        reply_to_id: replyTo?.id ?? null,
      });
      toast(t("letterSent"));
      onSent?.();
      onClose();
    } catch (e) {
      toastError(e);
    } finally {
      guard.free();
    }
  };

  return (
    <Modal title={replyTo ? t("reply") : t("compose")} onClose={onClose} wide>
      {accounts.length > 1 && (
        <div className="field">
          <label className="label">{t("fromMailbox")}</label>
          <select
            className="input"
            value={accountId}
            onChange={(e) => setAccountId(Number(e.target.value))}
          >
            {accounts.map((account) => (
              <option key={account.id} value={account.id}>
                {account.title || account.address}
              </option>
            ))}
          </select>
        </div>
      )}
      <div className="field">
        <label className="label">{t("toField")}</label>
        <input
          className="input"
          value={form.to}
          onChange={(e) => setForm({ ...form, to: e.target.value })}
          placeholder="client@example.com"
        />
      </div>
      <div className="field">
        <label className="label">{t("subjectField")}</label>
        <input
          className="input"
          value={form.subject}
          onChange={(e) => setForm({ ...form, subject: e.target.value })}
        />
      </div>
      <div className="field">
        <label className="label">{t("bodyField")}</label>
        <textarea
          className="textarea"
          rows={8}
          value={form.body}
          onChange={(e) => setForm({ ...form, body: e.target.value })}
        />
      </div>
      <div style={{ display: "flex", gap: 10, justifyContent: "flex-end", marginTop: 16 }}>
        <button className="btn btn-secondary btn-sm" onClick={onClose}>
          {t("cancel")}
        </button>
        <button className="btn btn-primary btn-sm" disabled={guard.busy} onClick={() => void send()}>
          {t("sendLetter")}
        </button>
      </div>
    </Modal>
  );
}
