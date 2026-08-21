import { useCallback, useEffect, useState } from "react";

import { ConfirmModal, ScreenLoading } from "../components/ui";
import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { useFailure } from "../lib/failure";
import { formatBytes, formatDate } from "../lib/format";
import { useGuard } from "../lib/guard";

/** Чем платит владелец за вечное хранение — числами, а не словом «растёт». */
interface TelegramRost {
  messages: number;
  files: number;
  /** Вес забранных вложений по строкам базы. */
  file_bytes: number;
  /** Что каталог канала занимает на диске: там же лежат аватары. */
  disk_bytes: number;
  oldest_at: string | null;
  /** Цена каждого предлагаемого срока: ключ — месяцы. */
  po_srokam: Record<string, { messages: number; bytes: number }>;
}

/** След последнего прогона уборки. Пусто — уборка ни разу не ходила. */
interface TelegramProgon {
  at: string;
  cutoff: string;
  months: number;
  messages: number;
  files: number;
  bytes: number;
  /** Ложь — прогон уперся в предел времени и продолжит в следующий раз. */
  done: boolean;
}

interface TelegramConfig {
  configured: boolean;
  token_tail: string;
  digest_chat: string;
  /** Куда уходят тревоги о сервере. Пусто — туда же, куда сводка. */
  alerts_chat: string;
  bot_username: string;
  webhook_secret_set: boolean;
  /** Ноль — хранить вечно. Это и есть значение по умолчанию. */
  retention_months: number;
  rost: TelegramRost;
  last_cleanup: TelegramProgon | null;
}

/**
 * Подключение бота фирмы.
 *
 * Отдельным маршрутом, а не разделом общих настроек: здесь свой секрет и своё
 * действие «подключить», а там одна кнопка «Сохранить» на всю группу.
 *
 * **Бот тут ДРУГОЙ, не тот, что уведомляет об обновлениях.** В этого пишут
 * клиенты, и цена его утечки другая: не «сообщение в служебный чат», а чтение и
 * письмо клиентам от имени фирмы. Поэтому токен сюда не приходит вовсе —
 * сервер отдаёт только хвост из четырёх знаков, чтобы владелец узнал свой.
 */
export function SettingsTelegram() {
  const { t, locale, toast, toastError } = useApp();
  const [config, setConfig] = useState<TelegramConfig | null>(null);
  const [token, setToken] = useState("");
  const [chat, setChat] = useState("");
  const [alerts, setAlerts] = useState("");
  const [botName, setBotName] = useState("");
  // Срок хранения переписки. Ноль — вечно, и с него всё начинается: пока
  // владелец не назвал срок, не удаляется ничего.
  const [srok, setSrok] = useState(0);
  const [confirmSrok, setConfirmSrok] = useState(false);
  const [busy, setBusy] = useState(false);
  // Засов на подключение: второе нажатие послало бы телеграму тот же
  // `setWebhook` ещё раз, пока первый в пути.
  const guard = useGuard();
  // Свой засов у отправки сводки, а не общий с подключением: это разные
  // разговоры с телеграмом, и запирать их одним значило бы гасить кнопку
  // «Подключить» на время, пока уходит сводка.
  const svodkaGuard = useGuard();
  // Приглашение подгружается отдельно и только когда есть чем: без имени бота
  // ссылка выглядела бы настоящей и не работала.
  const [invite, setInvite] = useState<{ url: string; qr_svg: string } | null>(null);

  const { failure, fail, clear } = useFailure();

  const load = useCallback(() => {
    clear();
    api
      .get<TelegramConfig>("/telegram/settings")
      .then((svezhee) => {
        setConfig(svezhee);
        setChat(svezhee.digest_chat);
        setAlerts(svezhee.alerts_chat);
        setBotName(svezhee.bot_username);
        setSrok(svezhee.retention_months);
      })
      .catch(fail);
  }, [fail, clear]);

  useEffect(load, [load]);

  useEffect(() => {
    if (!config?.bot_username) {
      setInvite(null);
      return;
    }
    api
      .get<{ url: string; qr_svg: string }>("/telegram/invite?label=site")
      .then(setInvite)
      // Молча: приглашение — вспомогательное, и его отказ не должен закрывать
      // экран настроек, на котором чинят как раз причину отказа.
      .catch(() => setInvite(null));
  }, [config?.bot_username]);

  if (!config) return <ScreenLoading error={failure} onRetry={load} />;

  const save = async () => {
    setBusy(true);
    try {
      // Токен отправляем ТОЛЬКО когда его вводили. Пустое поле означает «не
      // меняй»: экран настоящего токена не знает и вернуть его не может.
      const telo: Record<string, string> = {
        digest_chat: chat,
        alerts_chat: alerts,
        bot_username: botName,
      };
      if (token.trim()) telo.token = token.trim();
      const svezhee = await api.put<TelegramConfig>("/telegram/settings", telo);
      setConfig(svezhee);
      setToken("");
      toast(t("saved"));
    } catch (beda) {
      toastError(beda);
    } finally {
      setBusy(false);
    }
  };

  const connect = async () => {
    if (!guard.take()) return;
    setBusy(true);
    try {
      await api.post("/telegram/connect");
      toast(t("tgConnected"));
    } catch (beda) {
      toastError(beda);
    } finally {
      guard.free();
      setBusy(false);
    }
  };

  /**
   * Отправить сводку прямо сейчас.
   *
   * Ручка была, кнопки не было: посмотреть, как сводка выглядит, можно было
   * только дождавшись утра — а оформление правят итерациями.
   *
   * Ответ приходит с состоянием, а не отказом: ненастроенный канал — не ошибка
   * ручки, человек мог открыть экран раньше, чем ввёл токен. Поэтому три исхода
   * разбираются здесь, и «не ушла» показывается вместе с причиной от телеграма:
   * без неё «не ушла» отправляет искать поломку наугад.
   */
  const poslatSvodku = async () => {
    if (!svodkaGuard.take()) return;
    try {
      const otvet = await api.post<{ status: string; reason?: string; error?: string }>(
        "/telegram/digest/send",
      );
      if (otvet.status === "sent") toast(t("tgDigestSent"));
      else if (otvet.status === "failed") toast(t("tgDigestFailed", { error: otvet.error ?? "" }), true);
      else toast(t("tgDigestSkipped"), true);
    } catch (beda) {
      toastError(beda);
    } finally {
      svodkaGuard.free();
    }
  };

  const disconnect = async () => {
    setBusy(true);
    try {
      const svezhee = await api.del<TelegramConfig>("/telegram/settings");
      setConfig(svezhee);
      toast(t("tgDisconnected"));
    } catch (beda) {
      toastError(beda);
    } finally {
      setBusy(false);
    }
  };

  /**
   * Записать срок хранения — отдельно от прочих настроек и отдельной кнопкой.
   *
   * Отдельно потому, что это единственное решение на экране, которое СТИРАЕТ
   * данные. Стоя в одном ряду с токеном и чатом сводки, оно уезжало бы в базу
   * заодно с ними — то есть человек, поправивший имя бота, нечаянно включал бы
   * удаление переписки.
   */
  const saveSrok = async (mesyatsev: number) => {
    setBusy(true);
    try {
      const svezhee = await api.put<TelegramConfig>("/telegram/settings", {
        retention_months: mesyatsev,
      });
      setConfig(svezhee);
      setSrok(svezhee.retention_months);
      toast(t("saved"));
    } catch (beda) {
      toastError(beda);
    } finally {
      setBusy(false);
    }
  };

  const rost = config.rost;
  const podSrok = srok > 0 ? rost.po_srokam[String(srok)] : undefined;
  // Что уйдёт при выбранном, но ещё не сохранённом сроке. Показывается ДО
  // нажатия: цифры после удаления — это уже не предупреждение, а отчёт.
  const izmenilsya = srok !== config.retention_months;

  return (
    <div className="page page-narrow">
      <div className="page-head" style={{ alignItems: "flex-start", marginBottom: 22 }}>
        <div>
          <h1 className="page-title">{t("modTelegram")}</h1>
          <div className="page-sub">{t("tgSettingsAbout")}</div>
        </div>
      </div>

      <div className="card card-pad" style={{ marginBottom: 18 }}>
        <div style={{ display: "grid", gap: 14 }}>
          <div>
            <label className="label">{t("tgToken")}</label>
            <input
              className="input"
              type="password"
              value={token}
              onChange={(e) => setToken(e.target.value)}
              placeholder={
                config.configured ? `\u2022\u2022\u2022\u2022${config.token_tail}` : "123456789:AA..."
              }
              autoComplete="off"
            />
            <div className="field-desc">{t("tgTokenHint")}</div>
          </div>

          <div>
            <label className="label">{t("tgBotName")}</label>
            <input
              className="input"
              value={botName}
              onChange={(e) => setBotName(e.target.value)}
              placeholder="my_company_bot"
            />
            <div className="field-desc">{t("tgBotNameHint")}</div>
          </div>

          <div>
            <label className="label">{t("tgDigestChat")}</label>
            <input
              className="input"
              value={chat}
              onChange={(e) => setChat(e.target.value)}
              placeholder="123456789"
              inputMode="numeric"
            />
            <div className="field-desc">{t("tgDigestChatHint")}</div>
            {/* «Отправить сейчас» стоит у самого поля, а не в общем ряду кнопок:
                проверяют этим не бота вообще, а именно этот чат — дошло ли до
                него и как выглядит. Кнопка не сохраняет: сводка уйдёт по тому
                чату, что записан, и разойдись она с полем — человек решил бы,
                что проверил новое значение. */}
            <button
              type="button"
              className="text-link"
              style={{ marginTop: 8 }}
              disabled={svodkaGuard.busy || !config.configured || !config.digest_chat}
              onClick={() => void poslatSvodku()}
            >
              {t("tgDigestSendNow")}
            </button>
          </div>

          <div>
            <label className="label">{t("tgAlertsChat")}</label>
            <input
              className="input"
              value={alerts}
              onChange={(e) => setAlerts(e.target.value)}
              placeholder={chat || "123456789"}
              inputMode="numeric"
            />
            <div className="field-desc">{t("tgAlertsChatHint")}</div>
          </div>
        </div>

        <div style={{ display: "flex", gap: 10, marginTop: 18, flexWrap: "wrap" }}>
          <button
            type="button"
            className="btn btn-primary"
            onClick={() => void save()}
            disabled={busy}
          >
            {t("save")}
          </button>
          {/*
            «Подключить» отдельной кнопкой, и это не лишний шаг. Адрес приёма
            зависит от того, как сайт виден снаружи, и меняется при переезде или
            смене домена, — а токен при этом остаётся прежним.
          */}
          <button
            type="button"
            className="btn"
            onClick={() => void connect()}
            disabled={busy || !config.configured}
          >
            {t("tgConnect")}
          </button>
          {config.configured && (
            <button
              type="button"
              className="btn btn-danger-link"
              onClick={() => void disconnect()}
              disabled={busy}
            >
              {t("tgDisconnect")}
            </button>
          )}
        </div>
      </div>

      {invite && (
        <div className="card card-pad">
          <label className="label">{t("tgInviteHint")}</label>
          <div className="field-desc" style={{ marginBottom: 10 }}>
            <code>{invite.url}</code>
          </div>
          {/*
            QR рядом со ссылкой, а не вместо неё. Ссылку кладут на сайт и в
            письмо, код — на квитанцию и наклейку: это разные места, и выбирать
            за владельца незачем. Код приходит с сервера готовым SVG — рисовать
            его в браузере значило бы тащить ещё одну библиотеку.
          */}
          <div
            className="tg-qr"
            aria-label={t("tgInviteHint")}
            dangerouslySetInnerHTML={{ __html: invite.qr_svg }}
          />
        </div>
      )}

      {/*
        Рост переписки и уборка старого.

        Карточка начинается с ЦИФР, а не с выбора срока, и это порядок, а не
        оформление: пока не видно, сколько переписка весит и с какого числа она
        идёт, выбирать срок не из чего. «Растёт быстро» — не довод ни за, ни
        против.

        Сроки приезжают с сервера вместе с ценой каждого (`po_srokam`), поэтому
        список здесь не повторяется: одно место, где он назван, — служба уборки.
      */}
      <div className="card card-pad" style={{ marginTop: 18 }}>
        <label className="label">{t("tgStorage")}</label>
        <div className="field-desc" style={{ marginBottom: 14 }}>{t("tgStorageAbout")}</div>

        <div style={{ display: "flex", gap: 26, flexWrap: "wrap", marginBottom: 16 }}>
          <div>
            <div className="page-sub">{t("tgStoredMessages")}</div>
            <div style={{ fontSize: 20, fontWeight: 600 }}>
              {rost.messages.toLocaleString(locale)}
            </div>
          </div>
          <div>
            <div className="page-sub">{t("tgStoredFiles")}</div>
            <div style={{ fontSize: 20, fontWeight: 600 }}>
              {rost.files.toLocaleString(locale)}
            </div>
          </div>
          <div>
            <div className="page-sub">{t("tgStoredDisk")}</div>
            <div style={{ fontSize: 20, fontWeight: 600 }}>{formatBytes(rost.disk_bytes)}</div>
          </div>
          {rost.oldest_at && (
            <div>
              <div className="page-sub">{t("tgOldest")}</div>
              <div style={{ fontSize: 20, fontWeight: 600 }}>
                {formatDate(rost.oldest_at, locale)}
              </div>
            </div>
          )}
        </div>

        <label className="label">{t("tgRetention")}</label>
        <select
          className="input"
          value={String(srok)}
          onChange={(e) => setSrok(Number(e.target.value))}
          disabled={busy}
        >
          {/*
            «Хранить вечно» стоит первым и выбрано по умолчанию. Умолчание здесь
            и есть решение: переписка с клиентом бывает доказательством, и молча
            стереть её нельзя.
          */}
          <option value="0">{t("tgRetentionForever")}</option>
          {Object.keys(rost.po_srokam)
            .map(Number)
            .sort((a, b) => a - b)
            .map((mesyatsev) => (
              <option key={mesyatsev} value={String(mesyatsev)}>
                {t("tgRetentionOption", {
                  months: mesyatsev,
                  messages: rost.po_srokam[String(mesyatsev)].messages.toLocaleString(locale),
                  size: formatBytes(rost.po_srokam[String(mesyatsev)].bytes),
                })}
              </option>
            ))}
        </select>
        <div className="field-desc">{t("tgRetentionHint")}</div>

        {/*
          Предупреждение показывается ДО сохранения и называет числа. Оно и есть
          то, чем «уборка по решению владельца» отличается от «уборки молча»:
          решение принимают, зная цену, а не узнав её постфактум.
        */}
        {izmenilsya && srok > 0 && podSrok && (
          <div className="field-desc" style={{ marginTop: 12, color: "var(--danger)" }}>
            {t("tgRetentionWarn", {
              messages: podSrok.messages.toLocaleString(locale),
              size: formatBytes(podSrok.bytes),
            })}
          </div>
        )}
        <div className="field-desc" style={{ marginTop: 8 }}>{t("tgRetentionKeeps")}</div>

        <div style={{ display: "flex", gap: 10, marginTop: 16, flexWrap: "wrap" }}>
          <button
            type="button"
            className="btn btn-primary"
            disabled={busy || !izmenilsya}
            onClick={() => {
              // Подтверждение спрашивается только у включения уборки. Возврат к
              // вечному хранению ничего не стирает и спрашивать не о чем.
              if (srok > 0) setConfirmSrok(true);
              else void saveSrok(0);
            }}
          >
            {t("save")}
          </button>
        </div>

        <div className="field-desc" style={{ marginTop: 16 }}>
          {config.last_cleanup
            ? t("tgCleanupLine", {
                date: formatDate(config.last_cleanup.at, locale),
                messages: config.last_cleanup.messages.toLocaleString(locale),
                files: config.last_cleanup.files.toLocaleString(locale),
                size: formatBytes(config.last_cleanup.bytes),
                cutoff: formatDate(config.last_cleanup.cutoff, locale),
              }) + (config.last_cleanup.done ? "" : " " + t("tgCleanupPartial"))
            : t("tgCleanupNever")}
        </div>
      </div>

      {confirmSrok && podSrok && (
        <ConfirmModal
          text={t("tgRetentionConfirm", {
            months: srok,
            messages: podSrok.messages.toLocaleString(locale),
            size: formatBytes(podSrok.bytes),
          })}
          confirmLabel={t("save")}
          danger
          onConfirm={() => void saveSrok(srok)}
          onClose={() => setConfirmSrok(false)}
        />
      )}
    </div>
  );
}
