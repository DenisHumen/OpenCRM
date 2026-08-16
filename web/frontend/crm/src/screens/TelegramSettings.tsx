import { useCallback, useEffect, useState } from "react";

import { ScreenLoading } from "../components/ui";
import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { useFailure } from "../lib/failure";
import { useGuard } from "../lib/guard";

interface TelegramConfig {
  configured: boolean;
  token_tail: string;
  digest_chat: string;
  bot_username: string;
  webhook_secret_set: boolean;
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
  const { t, toast, toastError } = useApp();
  const [config, setConfig] = useState<TelegramConfig | null>(null);
  const [token, setToken] = useState("");
  const [chat, setChat] = useState("");
  const [botName, setBotName] = useState("");
  const [busy, setBusy] = useState(false);
  // Засов на подключение: второе нажатие послало бы телеграму тот же
  // `setWebhook` ещё раз, пока первый в пути.
  const guard = useGuard();
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
        setBotName(svezhee.bot_username);
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
    </div>
  );
}
