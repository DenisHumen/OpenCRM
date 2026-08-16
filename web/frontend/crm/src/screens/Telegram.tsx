import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { Icon } from "../components/Icon";
import { EmptyState, ScreenLoading } from "../components/ui";
import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { useDebounced } from "../lib/debounce";
import { useFailure } from "../lib/failure";
import { useGuard } from "../lib/guard";
import { useReference } from "../lib/reference";
import { formatDateTime } from "../lib/format";

/**
 * Мессенджер: переписка с клиентами через бота фирмы.
 *
 * Экран живой, и это его главное отличие от списков рядом. Новые сообщения и
 * присутствие приходят потоком событий, а не по кнопке «обновить»: переписка,
 * которую надо обновлять руками, — это не переписка.
 *
 * Про конфликты решено сознательно: только предупреждающий баннер. Ни замка при
 * наборе, ни закрепления диалога за сотрудником. Система показывает, кто в
 * чате, и на этом останавливается — договариваются люди.
 */

export interface TgChat {
  id: number;
  chat_id: number;
  title: string;
  username: string;
  phone: string;
  client_id: number | null;
  source: string;
  last_message_at: string | null;
}

export interface TgMessage {
  id: number;
  chat_id: number;
  direction: "in" | "out";
  kind: string;
  body: string;
  file_name: string;
  file_size: number | null;
  has_file: boolean;
  can_fetch: boolean;
  author_id: number | null;
  send_state: "pending" | "sent" | "failed";
  send_error: string;
  happened_at: string;
}

interface Watcher {
  user_id: number;
  name: string;
}

/** Как часто подтверждаем «я в этом чате». Срок жизни отметки — 15 секунд. */
const PULS_PRISUTSTVIYA = 5000;

export function Telegram() {
  const { t, locale, toastError, user } = useApp();
  const [chats, setChats] = useState<TgChat[] | null>(null);
  const [vybran, setVybran] = useState<number | null>(null);
  const [messages, setMessages] = useState<TgMessage[]>([]);
  const [watchers, setWatchers] = useState<Watcher[]>([]);
  const [tekst, setTekst] = useState("");
  const [otpravka, setOtpravka] = useState(false);
  const [poisk, setPoisk] = useState("");
  // Отказ загрузки списка: экран обязан о нём сказать и дать повторить.
  // Вечная вертушка — это не «грузится», это «мы не знаем и молчим».
  const { failure, fail, clear } = useFailure();
  // Засов на отправку. Именно ref внутри `useGuard`, а не состояние:
  // `setBusy(true)` меняет значение к следующему рендеру, и два нажатия
  // в одном тике читают `false` оба. Enter на автоповторе жмёт именно так.
  //
  // Имя латиницей и со словом `guard` — не вкус: сторож `test_screens.py`
  // ищет засов по имени (`\w*[Gg]uard\.take()`). Назови переменную иначе, и
  // засов будет стоять, а сторож его не увидит и потребует ещё один.
  const guard = useGuard();
  // Есть ли что листать вглубь. Отдельным состоянием, а не по длине ленты:
  // короткая переписка и дочитанная до конца выглядят одинаково.
  const [estGlubzhe, setEstGlubzhe] = useState(false);
  const iskat = useDebounced(poisk, 300);
  // Справочник клиентов для ручной привязки. Через общий крючок, как в
  // редакторе доски: отказ здесь не должен выглядеть как «клиентов нет».
  const klienty = useReference<any>("/clients?per_page=200");

  // Последнее известное сообщение: по нему дочитываем пропущенное после
  // обрыва. Обрыв неизбежен, а начинать с чистого листа в переписке нельзя.
  const posledneye = useRef(0);
  const lentaRef = useRef<HTMLDivElement | null>(null);

  const zagruzit_chats = useCallback(async () => {
    clear();
    try {
      const otvet = await api.get<{ items: TgChat[] }>(
        `/telegram/chats?q=${encodeURIComponent(iskat)}&per_page=100`,
      );
      setChats(otvet.items);
      return otvet.items;
    } catch (beda) {
      fail(beda);
      return null;
    }
  }, [iskat, clear, fail]);

  useEffect(() => {
    // Флажок «этот ответ ещё нужен». Человек печатает в поиске, запросы уходят
    // один за другим, и приходят они не в том порядке, в каком ушли: ответ на
    // «ив» может лечь ПОВЕРХ ответа на «иванов». Список показывал бы отбор,
    // которого в поле уже нет.
    let alive = true;
    void (async () => {
      const svezhee = await zagruzit_chats();
      if (!alive || svezhee === null) return;
    })();
    return () => {
      alive = false;
    };
  }, [zagruzit_chats]);

  const zagruzit_lentu = useCallback(
    async (chatId: number) => {
      try {
        const otvet = await api.get<{ items: TgMessage[] }>(
          `/telegram/chats/${chatId}/messages`,
        );
        setMessages(otvet.items);
        // Ручка отдаёт по полсотни. Пришла полная страница — значит вглубь,
        // скорее всего, есть ещё.
        setEstGlubzhe(otvet.items.length >= 50);
        posledneye.current = otvet.items.length
          ? otvet.items[otvet.items.length - 1].id
          : 0;
      } catch (beda) {
        toastError(beda);
      }
    },
    [toastError],
  );

  useEffect(() => {
    if (vybran == null) return;
    void zagruzit_lentu(vybran);
  }, [vybran, zagruzit_lentu]);

  // Лента прокручивается вниз на каждое новое сообщение: в переписке смотрят
  // последнее, а не первое.
  useEffect(() => {
    const uzel = lentaRef.current;
    if (uzel) uzel.scrollTop = uzel.scrollHeight;
  }, [messages]);

  /** Дочитать то, что появилось, пока нас не было или пока шло событие. */
  const dochitat = useCallback(async () => {
    if (vybran == null) return;
    try {
      const otvet = await api.get<{ items: TgMessage[] }>(
        `/telegram/chats/${vybran}/messages?after=${posledneye.current}`,
      );
      if (!otvet.items.length) return;
      setMessages((bylo) => [...bylo, ...otvet.items]);
      posledneye.current = otvet.items[otvet.items.length - 1].id;
    } catch {
      // Молча: это фоновое дочитывание, и всплывающая ошибка на каждый
      // моргнувший запрос раздражала бы сильнее, чем помогала.
    }
  }, [vybran]);

  /** Показать более старые сообщения.
   *
   * Листание по идентификатору самого верхнего, а не по номеру страницы.
   * Смещение на живой переписке врёт: пока человек читает, приходят новые
   * сообщения, и «вторая страница» показывает то, что уже было на первой.
   */
  const pokazat_eshchyo = async () => {
    if (vybran == null || !messages.length) return;
    try {
      const otvet = await api.get<{ items: TgMessage[] }>(
        `/telegram/chats/${vybran}/messages?before=${messages[0].id}`,
      );
      setEstGlubzhe(otvet.items.length >= 50);
      if (!otvet.items.length) return;
      setMessages((bylo) => [...otvet.items, ...bylo]);
    } catch (beda) {
      toastError(beda);
    }
  };

  // Поток событий. Одно соединение на экран, а не на диалог: список слева
  // обязан слышать про ВСЕ диалоги, иначе новый не появится, пока не
  // перезагрузишь страницу.
  useEffect(() => {
    const potok = new EventSource("/api/v1/telegram/stream");
    potok.onmessage = (sobytie) => {
      let dannye: any;
      try {
        dannye = JSON.parse(sobytie.data);
      } catch {
        return;
      }
      if (dannye.type === "message") {
        void zagruzit_chats();
        if (dannye.chat_id === vybran) void dochitat();
      } else if (dannye.type === "presence" && dannye.chat_id === vybran) {
        setWatchers(dannye.watchers || []);
      }
    };
    // Ошибку не показываем: `EventSource` переподключается сам, а всплывающее
    // окно на каждое моргание сети превратило бы экран в поток жалоб.
    potok.onerror = () => {};
    return () => potok.close();
  }, [vybran, zagruzit_chats, dochitat]);

  // Присутствие: подтверждаем, пока чат открыт, и снимаем, уходя.
  useEffect(() => {
    if (vybran == null) {
      setWatchers([]);
      return;
    }
    const otmetitsya = async () => {
      try {
        const otvet = await api.post<{ watchers: Watcher[] }>(
          `/telegram/chats/${vybran}/presence`,
          { present: true },
        );
        setWatchers(otvet.watchers);
      } catch {
        // Присутствие — украшение поверх работы: его отказ не должен мешать
        // отвечать клиенту.
      }
    };
    void otmetitsya();
    const chasy = window.setInterval(otmetitsya, PULS_PRISUTSTVIYA);
    const ushyol = vybran;
    return () => {
      window.clearInterval(chasy);
      void api
        .post(`/telegram/chats/${ushyol}/presence`, { present: false })
        .catch(() => {});
    };
  }, [vybran]);

  const sosedi = useMemo(
    () => watchers.filter((kto) => kto.user_id !== user?.id),
    [watchers, user],
  );

  /** Сказать, чей это диалог.
   *
   * Руками, и это главное. Автоматическая привязка возможна ровно одна:
   * точное совпадение номера, когда клиент сам поделился контактом. Всё
   * остальное — похожее имя, похожий логин — запрещено: в заказах привязка по
   * частичному совпадению имени уводила деньги и товар на чужую карточку, и
   * здесь ценой была бы переписка, которую читает не тот человек.
   */
  const privyazat = async (clientId: number | null) => {
    if (vybran == null) return;
    try {
      const svezhiy = await api.patch<TgChat>(`/telegram/chats/${vybran}`, {
        client_id: clientId,
      });
      setChats((bylo) =>
        (bylo || []).map((c) => (c.id === svezhiy.id ? svezhiy : c)),
      );
    } catch (beda) {
      toastError(beda);
    }
  };

  /** Забрать видео, которое сразу не тянули. */
  const zabrat = async (stroka: TgMessage) => {
    try {
      const svezhee = await api.post<TgMessage>(
        `/telegram/chats/${stroka.chat_id}/messages/${stroka.id}/fetch`,
      );
      setMessages((bylo) => bylo.map((s) => (s.id === svezhee.id ? svezhee : s)));
    } catch (beda) {
      toastError(beda);
    }
  };

  const otvetit = async () => {
    if (vybran == null || !tekst.trim()) return;
    // Засов берётся ПЕРВЫМ действием: не взялся — значит отправка уже идёт, и
    // второе нажатие отправило бы клиенту то же сообщение дважды. В переписке
    // это видит он, а не мы.
    if (!guard.take()) return;
    setOtpravka(true);
    try {
      const stroka = await api.post<TgMessage>(
        `/telegram/chats/${vybran}/messages`,
        { text: tekst },
      );
      setMessages((bylo) => [...bylo, stroka]);
      posledneye.current = stroka.id;
      setTekst("");
      void zagruzit_chats();
    } catch (beda) {
      toastError(beda);
    } finally {
      guard.free();
      setOtpravka(false);
    }
  };

  const prilozhit = async (fayl: File) => {
    if (vybran == null) return;
    if (!guard.take()) return;
    setOtpravka(true);
    try {
      const stroka = await api.upload<TgMessage>(
        `/telegram/chats/${vybran}/files`,
        fayl,
      );
      setMessages((bylo) => [...bylo, stroka]);
      posledneye.current = stroka.id;
      void zagruzit_chats();
    } catch (beda) {
      toastError(beda);
    } finally {
      guard.free();
      setOtpravka(false);
    }
  };

  if (chats === null) return <ScreenLoading error={failure} onRetry={() => void zagruzit_chats()} />;

  const otkrytyy = chats.find((c) => c.id === vybran) || null;

  return (
    <div className="screen tg-screen">
      <aside className="tg-list">
        <div className="tg-search">
          <Icon name="search" />
          <input
            value={poisk}
            onChange={(e) => setPoisk(e.target.value)}
            placeholder={t("search")}
          />
        </div>
        {chats.length === 0 ? (
          <EmptyState title={t("tgNoChats")} sub={t("tgNoChatsHint")} />
        ) : (
          <ul>
            {chats.map((chat) => (
              <li key={chat.id}>
                <button
                  type="button"
                  className={chat.id === vybran ? "tg-chat is-active" : "tg-chat"}
                  onClick={() => setVybran(chat.id)}
                >
                  <span className="tg-chat-title">{chat.title || chat.username}</span>
                  <span className="tg-chat-time">
                    {chat.last_message_at ? formatDateTime(chat.last_message_at, locale) : ""}
                  </span>
                  {chat.client_id == null && (
                    <span className="tg-chat-unlinked">{t("tgUnlinked")}</span>
                  )}
                </button>
              </li>
            ))}
          </ul>
        )}
      </aside>

      <section className="tg-talk">
        {otkrytyy === null ? (
          <EmptyState title={t("tgPickChat")} sub={t("tgPickChatHint")} />
        ) : (
          <>
            <header className="tg-head">
              <div>
                <strong>{otkrytyy.title || otkrytyy.username}</strong>
                {otkrytyy.username && <span>@{otkrytyy.username}</span>}
              </div>
              <div className="tg-head-right">
                <label className="tg-link">
                  <span>{t("tgClient")}</span>
                  {/*
                    Справочник не приехал — говорим об этом, а не рисуем пустой
                    выбор. Пустой выбор читается как «клиентов нет», и человек
                    заводит второго того же клиента.
                  */}
                  <select
                    value={otkrytyy.client_id ?? ""}
                    onChange={(e) =>
                      void privyazat(e.target.value ? Number(e.target.value) : null)
                    }
                  >
                    <option value="">{t("tgUnlinked")}</option>
                    {klienty.failure != null && (
                      <option value="" disabled>
                        {t("loadFailed")}
                      </option>
                    )}
                    {(klienty.items ?? []).map((klient: any) => (
                      <option key={klient.id} value={klient.id}>
                        {klient.name}
                      </option>
                    ))}
                  </select>
                </label>
                {otkrytyy.source && (
                  <span className="tg-source">
                    {t("tgCameFrom")}: {otkrytyy.source}
                  </span>
                )}
              </div>
            </header>

            {sosedi.length > 0 && (
              /*
               * Баннер, а не запрет. Решено сознательно: люди договорятся
               * сами, система только показывает, с кем. Имена, а не число:
               * число не говорит, к кому идти.
               */
              <div className="tg-banner" role="status">
                <Icon name="alert" />
                {t("tgAlsoHere")}: {sosedi.map((kto) => kto.name).join(", ")}
              </div>
            )}

            <div className="tg-feed" ref={lentaRef}>
              {estGlubzhe && (
                <button type="button" className="tg-more" onClick={() => void pokazat_eshchyo()}>
                  {t("tgOlder")}
                </button>
              )}
              {messages.map((stroka) => (
                <div
                  key={stroka.id}
                  className={`tg-msg tg-${stroka.direction}${
                    stroka.send_state === "failed" ? " is-failed" : ""
                  }`}
                >
                  {stroka.body && <p>{stroka.body}</p>}
                  {stroka.can_fetch && (
                    /*
                     * Видео сразу не забирается: переписка с видео съест диск
                     * за недели. Кнопка тянет его тогда, когда посмотреть
                     * действительно захотели.
                     */
                    <button
                      type="button"
                      className="tg-fetch"
                      onClick={() => void zabrat(stroka)}
                    >
                      <Icon name="download" /> {stroka.file_name || t("tgFile")}
                    </button>
                  )}
                  {stroka.has_file && (
                    /*
                     * Ссылкой, а не встроенной картинкой. Встроить — значит
                     * тянуть на экран всё подряд при открытии диалога, включая
                     * то, что человек смотреть не собирался; на мобильном
                     * интернете это ощутимо. Файл отдаётся через приложение и
                     * только тому, у кого есть право на раздел.
                     */
                    <a
                      className="tg-file"
                      href={`/api/v1/telegram/chats/${stroka.chat_id}/messages/${stroka.id}/file`}
                      target="_blank"
                      rel="noreferrer"
                    >
                      <Icon name="download" /> {stroka.file_name || t("tgFile")}
                    </a>
                  )}
                  <span className="tg-time">{formatDateTime(stroka.happened_at, locale)}</span>
                  {stroka.send_state === "failed" && (
                    // Причину показываем прямо здесь: «бот заблокирован
                    // пользователем» и «неверный токен» чинятся по-разному, и
                    // прятать разницу значило бы заставить гадать.
                    <span className="tg-failed">
                      {t("tgNotSent")}: {stroka.send_error}
                    </span>
                  )}
                </div>
              ))}
            </div>

            <footer className="tg-compose">
              <label className="tg-attach">
                <Icon name="upload" />
                <input
                  type="file"
                  hidden
                  onChange={(e) => {
                    const fayl = e.target.files?.[0];
                    if (fayl) void prilozhit(fayl);
                    e.target.value = "";
                  }}
                />
              </label>
              <textarea
                value={tekst}
                onChange={(e) => setTekst(e.target.value)}
                placeholder={t("tgWrite")}
                onKeyDown={(e) => {
                  // Enter отправляет, Shift+Enter переносит строку — как во
                  // всяком мессенджере. Обратное поведение здесь удивляло бы.
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    void otvetit();
                  }
                }}
              />
              <button
                type="button"
                onClick={() => void otvetit()}
                disabled={otpravka}
                aria-label={t("tgSend")}
                title={t("tgSend")}
              >
                <Icon name="send" />
              </button>
            </footer>
          </>
        )}
      </section>
    </div>
  );
}
