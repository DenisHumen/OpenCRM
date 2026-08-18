import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";

import { Icon } from "../components/Icon";
import { Avatar, EmptyState, ScreenLoading } from "../components/ui";
import { api } from "../lib/api";
import { copyText } from "../lib/clipboard";
import { useApp } from "../lib/app";
import { useDebounced } from "../lib/debounce";
import { useFailure } from "../lib/failure";
import { useGuard } from "../lib/guard";
import { moduleOn } from "../lib/modules";
import { can } from "../lib/permissions";
import { useReference } from "../lib/reference";
import { podpisatsya } from "../lib/tgpotok";
import { otmetit_otkrytyy_chat } from "../lib/signaly";
import { formatDateTime, formatMoney, initials } from "../lib/format";

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
  unread: number;
  last_preview: string;
  last_direction: string;
  last_kind: string;
  has_avatar: boolean;
  is_premium: boolean;
  has_emoji: boolean;
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
  reply_to_id: number | null;
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

/**
 * Предел телеграма на файл от бота — пятьдесят мегабайт.
 *
 * Названо и здесь, и в `core/services/telegram_service.py`: браузер бережёт
 * человека от впустую потраченной заливки, сервер — от обхода браузера. Число
 * одно, потому что предел не наш, а телеграма, и подвинуть его нельзя.
 */
const TG_PREDEL_FAYLA = 50 * 1024 * 1024;

/** Байты словами: «14.2 MB». Три знака до запятой хватает, дробь — лишний шум. */
function razmer(baytov: number): string {
  if (baytov < 1024) return `${baytov} B`;
  if (baytov < 1024 * 1024) return `${Math.round(baytov / 1024)} KB`;
  return `${(baytov / 1024 / 1024).toFixed(1)} MB`;
}

/**
 * Свести сообщения в одну ленту без повторов и в порядке возрастания `id`.
 *
 * **Почему множество, а не список, в который дописывают.** Одно и то же
 * сообщение приезжает разными путями: ответом ручки при отправке, событием
 * шины, дочитыванием после обрыва. Пути независимы и обгоняют друг друга —
 * значит рано или поздно любое из них привезёт то, что уже показано.
 *
 * Так и вышло: отправленный файл появлялся в переписке дважды. В телеграм он
 * уходил ОДИН раз, то есть задваивался показ. Со стороны это неотличимо от
 * повторной отправки клиенту, и менеджер не может понять, что произошло.
 *
 * Чинить порядок присваиваний бессмысленно: он чинится ровно до следующего
 * пути доставки. Идемпотентное слияние снимает вопрос целиком — и обрыв связи,
 * и двойная подписка, и опережающее событие становятся безвредны.
 *
 * Свежая запись побеждает: у отправленного сообщения состояние меняется
 * (`pending` → `sent` или `failed`), и показывать надо последнее известное.
 */
function slit(bylo: TgMessage[], novye: TgMessage[]): TgMessage[] {
  const po_id = new Map<number, TgMessage>();
  for (const stroka of bylo) po_id.set(stroka.id, stroka);
  for (const stroka of novye) po_id.set(stroka.id, stroka);
  return [...po_id.values()].sort((a, b) => a.id - b.id);
}

/**
 * Есть ли что переносить из телеграма в уже заведённую карточку.
 *
 * Условие ровно то же, по которому переносит сервер: непустое в телеграме
 * против пустого в карточке. Разойдись они — кнопка либо не появлялась бы там,
 * где перенос сработал бы, либо появлялась там, где ей нечего делать, и нажатие
 * отвечало бы бодрым «готово», ничего не изменив.
 *
 * Имени здесь нет намеренно, и это не пропуск. Имя карточки обязательно и
 * пустым не бывает — значит правило «не затирать непустое» не пропустит его
 * никогда. Свежее имя из телеграма при этом никуда не девается: оно стоит в
 * шапке того же разговора, рядом с именем карточки.
 */
function est_chto_perenesti(chat: TgChat, kartochka: any): boolean {
  if (!kartochka) return false;
  return (
    (!!chat.phone && !kartochka.phone) ||
    (!!chat.username && !kartochka.messenger) ||
    (!!chat.source && !kartochka.source)
  );
}

export function Telegram() {
  const { t, locale, toast, toastError, user, modules } = useApp();
  const [chats, setChats] = useState<TgChat[] | null>(null);
  const [vybran, setVybran] = useState<number | null>(null);
  const [messages, setMessages] = useState<TgMessage[]>([]);
  const [watchers, setWatchers] = useState<Watcher[]>([]);
  const [tekst, setTekst] = useState("");
  /** На какое сообщение отвечаем. Само сообщение, а не номер: цитату надо
   *  показать над полем ввода, а лезть за ней в ленту при каждом наборе
   *  буквы значило бы искать по всему списку на каждое нажатие. */
  const [otvechaem, setOtvechaem] = useState<TgMessage | null>(null);
  /** Открыто ли меню действий в шапке разговора. */
  const [menyu, setMenyu] = useState(false);
  /** Показан ли выбор карточки клиента. Зовут его изредка — потому и прячем. */
  const [privyazka, setPrivyazka] = useState(false);
  const [otpravka, setOtpravka] = useState(false);
  /** Идущая заливка: сколько ушло, сколько всего и чем её бросить. */
  const [zaliv, setZaliv] = useState<{
    imya: string;
    ushlo: number;
    vsego: number;
    otmenit: () => void;
  } | null>(null);
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
  /**
   * Дело клиента рядом с его словами.
   *
   * Это и есть ответ на вопрос «зачем переписка в CRM, если она есть в
   * телефоне»: отвечая, менеджер видит, что человек заказывал, сколько должен и
   * какая заявка в работе. В телефоне этого нет и быть не может.
   *
   * Берём СУЩЕСТВУЮЩУЮ ручку карточки, а не заводим свою сводную. Она уже
   * подчиняется правилам раздела заявок: чужие заявки в ответ не попадают, а
   * суммы приходят пустыми у того, кому их видеть не положено. Своя ручка
   * означала бы второе место, где эти правила надо помнить, — и первое, где их
   * забудут.
   */
  const [delo, setDelo] = useState<any>(null);
  const [deloIdyot, setDeloIdyot] = useState(false);
  /** Счётчик «перечитай дело»: растёт, когда мы сами что-то в нём завели. */
  const [deloOsvezhit, setDeloOsvezhit] = useState(0);
  // Свой засов у панели дела, отдельно от засова отправки. Разделены не ради
  // порядка: общий засов означал бы, что заведение карточки на секунду гасит
  // кнопку «отправить», то есть действие с карточкой мешает отвечать клиенту.
  // Имя со словом `Guard` — сторож `test_screens.py` ищет засов по имени.
  const kartochkaGuard = useGuard();
  const stages = useReference<any>("/pipeline/stages");
  // Справочник клиентов для ручной привязки. Через общий крючок, как в
  // редакторе доски: отказ здесь не должен выглядеть как «клиентов нет».
  const klienty = useReference<any>("/clients?per_page=200");
  // Шаблоны сообщений уже сделаны для других каналов — здесь их просто
  // подключаем. Заводить свои значило бы завести ВТОРОЕ место, где живут
  // типовые ответы, и разойтись с первым на первой же правке.
  //
  // Спрашиваем, только если есть у кого: блок включён и право на шаблоны есть.
  // Иначе `null` — «спрашивать нечего». Разница не в одном запросе: справочник,
  // спрошенный при выключенном блоке, отвечает `module_disabled`, а экран
  // показывает «не удалось загрузить» — то есть врёт. Ничего не ломалось,
  // повторять нечего, и выключенный блок обязан исчезнуть целиком, а не
  // остаться жалобой на сервер.
  const shablony_dostupny = moduleOn(modules, "templates") && can(user, "templates.view");
  const shablony = useReference<any>(
    shablony_dostupny ? "/templates?per_page=100" : null,
  );

  // Последнее известное сообщение: по нему дочитываем пропущенное после
  // обрыва. Обрыв неизбежен, а начинать с чистого листа в переписке нельзя.
  const posledneye = useRef(0);
  // Какой диалог открыт ПРЯМО СЕЙЧАС. Именно ref, а не состояние: ответ сервера
  // разбирается в замыкании, которое помнит `vybran` НА МОМЕНТ ЗАПРОСА, а нужно
  // знать, что открыто на момент ОТВЕТА. Человек щёлкает по списку быстрее, чем
  // отвечает сервер, и без этой опоры переписка одного клиента показывается в
  // окне другого.
  const vybranRef = useRef<number | null>(null);
  useEffect(() => {
    vybranRef.current = vybran;
  }, [vybran]);
  const lentaRef = useRef<HTMLDivElement | null>(null);
  /**
   * Граница новизны: оживляем пузырь, только если его номер БОЛЬШЕ этого.
   *
   * Иначе анимация превращается в дискотеку. Лента приезжает пачкой по
   * полсотни — при каждом открытии диалога и при каждом «показать более
   * ранние». Повесь появление на сам класс пузыря, и полсотни чужих реплик
   * недельной давности будут выпрыгивать разом, как будто их только что
   * прислали. Это ровно обратное тому, что делает анимация: она обязана
   * сказать «вот это пришло СЕЙЧАС», а сказала бы «пришло всё».
   *
   * Отличать новинку по времени прихода в браузер нельзя: пачка и новость
   * приходят одинаково — ответом сети. Отличает их только номер. Пачка
   * задаёт границу собой (её последний номер), и всё, что появится после
   * неё, по определению новее.
   *
   * Начальное значение — предел, а не ноль. Пока пачка не приехала, новизну
   * определять НЕ ПО ЧЕМУ: `posledneye` в этот миг ещё от прошлого диалога, и
   * дочитывание, обогнавшее загрузку, привезло бы целый чат «свежим». При
   * пределе не оживает никто — молчание здесь безопаснее пляски.
   */
  const granica = useRef(Number.MAX_SAFE_INTEGER);
  /**
   * Номер самого нижнего показанного сообщения. По нему видно, ПРИРОСЛА ли
   * лента снизу: дочитанная вверх история его не меняет, и прокручивать вниз
   * из-за неё нельзя — человек нажал «более ранние» именно чтобы их прочесть.
   */
  const nizhniy = useRef(0);
  /**
   * Стоит ли лента у самого низа. Нужно догружающейся картинке: она меняет
   * высоту пузыря уже ПОСЛЕ прокрутки, и без поправки последнее сообщение
   * уезжает под край. Поправлять вслепую нельзя — отлистанного вверх это
   * дёргало бы вниз на каждое доехавшее фото.
   */
  const vnizu = useRef(true);

  /**
   * Прокрутить ленту к последнему сообщению.
   *
   * Плавно — только на приход: это и есть та часть повадки iMessage, где лента
   * СДВИГАЕТСЯ вверх, освобождая место пузырю, а не подпрыгивает. Открытие
   * диалога прокручивается мгновенно: плавно там означало бы проезд через всю
   * историю на глазах у человека, пришедшего читать последнее.
   */
  const k_nizu = useCallback((plavno: boolean) => {
    const uzel = lentaRef.current;
    if (!uzel) return;
    const spokoyno = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    uzel.scrollTo({
      top: uzel.scrollHeight,
      behavior: plavno && !spokoyno ? "smooth" : "auto",
    });
  }, []);

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
        // Ответ мог приехать, когда открыт уже ДРУГОЙ диалог: человек щёлкает
        // по списку быстрее, чем отвечает сервер. Без этой проверки переписка
        // одного клиента показывается в окне другого — и менеджер отвечает не
        // тому.
        if (vybranRef.current !== chatId) return;
        // Граница новизны двигается ДО показа: пачка — это история, и оживать
        // ей нечего. Ставим её последним номером пачки, и всё, что приедет
        // после (свой ответ, событие шины, дочитывание), окажется новее.
        granica.current = otvet.items.length ? otvet.items[otvet.items.length - 1].id : 0;
        setMessages(slit([], otvet.items));
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
    // Привязку снимаем вместе со сменой диалога: цитата из прошлого
    // разговора над полем ввода нового — прямой путь отправить не то.
    setOtvechaem(null);
    // Меню и выбор карточки — туда же: открытые, они относились бы к диалогу,
    // которого на экране уже нет.
    setMenyu(false);
    setPrivyazka(false);
    // Новизна и прокрутка считаются заново на каждый диалог. Номера сквозные,
    // и у соседнего чата они запросто крупнее: не сбрось границу — его пачка
    // приедет «свежей» целиком и оживёт вся разом.
    granica.current = Number.MAX_SAFE_INTEGER;
    nizhniy.current = 0;
    vnizu.current = true;
    if (vybran == null) return;
    void zagruzit_lentu(vybran);
  }, [vybran, zagruzit_lentu]);

  // Лента прокручивается вниз на каждое новое сообщение: в переписке смотрят
  // последнее, а не первое.
  //
  // Но именно на НОВОЕ. Прежде прокрутка висела на всяком изменении ленты, а
  // меняет её и «показать более ранние»: человек просил историю, а его тут же
  // уносило обратно вниз, к тому, что он и так видел. Признак прироста снизу —
  // сменившийся последний номер; дочитанная вверх история его не трогает.
  //
  // Со спором прокрутки и анимации разобрались раньше — тем, ЧЕМ анимируем.
  // Появление сделано `transform`, а он не участвует в раскладке: высота
  // пузыря с первого кадра конечная, `scrollHeight` тоже, и прокрутке нечего
  // догонять. Анимируй мы высоту — лента ехала бы под растущим пузырём все
  // четыреста миллисекунд, и низ уползал бы из виду.
  useEffect(() => {
    const posledniy = messages.length ? messages[messages.length - 1].id : 0;
    if (posledniy === nizhniy.current) return;
    const pervyy_pokaz = nizhniy.current === 0;
    nizhniy.current = posledniy;
    k_nizu(!pervyy_pokaz);
  }, [messages, k_nizu]);

  /** Дочитать то, что появилось, пока нас не было или пока шло событие. */
  const dochitat = useCallback(async () => {
    if (vybran == null) return;
    const chat = vybran;
    try {
      const otvet = await api.get<{ items: TgMessage[] }>(
        `/telegram/chats/${vybran}/messages?after=${posledneye.current}`,
      );
      if (!otvet.items.length || vybranRef.current !== chat) return;
      setMessages((bylo) => slit(bylo, otvet.items));
      const posledniy = otvet.items[otvet.items.length - 1].id;
      posledneye.current = posledniy;
      // Видна вкладка — значит человек смотрит прямо на пришедшее, и держать
      // его непрочитанным незачем. Свёрнута — граница не двигается: счётчик
      // затем и нужен, чтобы вернувшийся увидел, что пропустил.
      //
      // Знает об этом только браузер: на сервере «вкладка открыта» и «на неё
      // смотрят» неразличимы.
      if (document.visibilityState === "visible") {
        try {
          await api.post(`/telegram/chats/${vybran}/read`, { up_to: posledniy });
        } catch {
          /* не отметилось — счётчик просто снимется при следующем открытии */
        }
      }
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
    const chat = vybran;
    try {
      const otvet = await api.get<{ items: TgMessage[] }>(
        `/telegram/chats/${vybran}/messages?before=${messages[0].id}`,
      );
      if (vybranRef.current !== chat) return;
      setEstGlubzhe(otvet.items.length >= 50);
      if (!otvet.items.length) return;
      setMessages((bylo) => slit(bylo, otvet.items));
    } catch (beda) {
      toastError(beda);
    }
  };

  // Поток событий. Одно соединение на экран, а не на диалог: список слева
  // обязан слышать про ВСЕ диалоги, иначе новый не появится, пока не
  // перезагрузишь страницу.
  useEffect(() => {
    // Подписка на ОБЩИЙ источник, а не свой `EventSource`. Оболочка приложения
    // слушает тот же поток ради сигналов о письмах, и второе соединение с той
    // же вкладки означало бы два переподключения и два живых генератора на
    // сервере — на каждого, кто открыл мессенджер.
    return podpisatsya((dannye) => {
      if (dannye.type === "message") {
        if (dannye.chat_id === vybran) {
          // Список — ПОСЛЕ дочитывания, а не вместе с ним. Вместе получалась
          // гонка: список успевал сосчитать непрочитанное раньше, чем открытый
          // диалог отмечал прочтение, и у чата, на который человек смотрит,
          // загоралась единица — до следующего события ничем не снимаемая.
          void dochitat().then(() => zagruzit_chats());
        } else {
          void zagruzit_chats();
        }
      } else if (dannye.type === "presence" && dannye.chat_id === vybran) {
        setWatchers(dannye.watchers || []);
      }
    });
  }, [vybran, zagruzit_chats, dochitat]);

  // Какой диалог открыт — знать обязана и оболочка: сигнал о сообщении в
  // ОТКРЫТОМ и видимом чате не подаётся. Убираем отметку, уходя с экрана,
  // иначе после ухода сигналы молчали бы про последний просмотренный диалог.
  useEffect(() => {
    const na_klavishu = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      // Порядок как в телеграме: Escape сперва снимает «отвечаем на», и только
      // следующий — закрывает диалог. Слушатель на документе, а не на поле:
      // выход должен работать откуда угодно.
      if (otvechaem) {
        setOtvechaem(null);
        return;
      }
      if (vybranRef.current != null) setVybran(null);
    };
    document.addEventListener("keydown", na_klavishu);
    return () => document.removeEventListener("keydown", na_klavishu);
  }, [otvechaem]);

  useEffect(() => {
    otmetit_otkrytyy_chat(vybran);
    // Отметка живёт двенадцать секунд и подтверждается каждые пять: закрытая на
    // полуслове вкладка иначе навсегда заглушила бы сигналы по своему диалогу.
    // Тот же срок и то же биение, что у присутствия, — и по той же причине.
    const bienie = window.setInterval(() => otmetit_otkrytyy_chat(vybran), PULS_PRISUTSTVIYA);
    // Свернули вкладку — отметка снимается сразу, не дожидаясь протухания:
    // двенадцать секунд тишины по открытому диалогу это уже пропущенный клиент.
    const na_vidimost = () => otmetit_otkrytyy_chat(vybran);
    document.addEventListener("visibilitychange", na_vidimost);
    return () => {
      window.clearInterval(bienie);
      document.removeEventListener("visibilitychange", na_vidimost);
      otmetit_otkrytyy_chat(null);
    };
  }, [vybran]);

  // Дело клиента: грузится по привязке диалога, а не по открытию чата.
  // Непривязанный диалог — обычное состояние, и запрос на него ушёл бы впустую.
  // Ищем прямо в списке, а не в `otkrytyy`: тот считается ниже, после
  // стражей загрузки, а крючки обязаны стоять до них — иначе их число
  // менялось бы от захода к заходу, и React потерял бы состояние.
  const klient_id: number | null =
    (chats ?? []).find((c) => c.id === vybran)?.client_id ?? null;
  const vidno_klientov = can(user, "clients.view");
  useEffect(() => {
    if (klient_id == null || !vidno_klientov) {
      setDelo(null);
      return;
    }
    let nuzhno = true;
    setDeloIdyot(true);
    void (async () => {
      try {
        const otvet = await api.get<any>(`/clients/${klient_id}`);
        // Тот же довод, что у ленты: пока ответ ехал, могли открыть другой
        // диалог, и чужое дело рядом с чужой перепиской — худшее, что тут
        // может показаться.
        if (nuzhno) setDelo(otvet);
      } catch {
        // Молча: карточка — довесок к переписке. Всплывающая жалоба на каждый
        // моргнувший запрос мешала бы отвечать клиенту.
        if (nuzhno) setDelo(null);
      } finally {
        if (nuzhno) setDeloIdyot(false);
      }
    })();
    return () => {
      nuzhno = false;
    };
  }, [klient_id, vidno_klientov, deloOsvezhit]);

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

  /** Завести карточку клиента прямо отсюда — и сразу привязать к ней диалог.
   *
   * Ради одного нажатия. До этой кнопки менеджер, которому написал незнакомый
   * человек, уходил в раздел клиентов, заводил карточку руками, возвращался и
   * выбирал её в списке — три перехода посреди разговора. Столько не делают: в
   * лучшем случае откладывают на «потом», которое не наступает.
   *
   * Ничего не спрашиваем перед отправкой: всё, что нужно, уже знает диалог.
   * Форма посреди разговора вернула бы нас к переносу руками.
   */
  const zavesti_kartochku = async () => {
    if (vybran == null) return;
    if (!kartochkaGuard.take()) return;
    try {
      const itog = await api.post<any>(`/telegram/chats/${vybran}/client`, {});
      // Заведение и привязка к уже существующей — разные вести, и молчать про
      // разницу нельзя: во втором случае человек обязан знать, что карточка не
      // новая и в ней уже есть история.
      toast(`${t(itog.created ? "tgClientMade" : "tgClientLinked")}: ${itog.name}`);
      // Перечитываем список, а не правим состояние руками. Привязка живёт в
      // строке диалога, из неё берётся `klient_id`, из него — само дело.
      // Одного запроса хватает на всю цепочку.
      await zagruzit_chats();
    } catch (beda) {
      toastError(beda);
    } finally {
      kartochkaGuard.free();
    }
  };

  /** Перенести в карточку то, что телеграм узнал о человеке позже.
   *
   * Заполняется только пустое: непустое поле карточки могли внести руками, и
   * оно точнее самоназвания в телеграме. Разбор — в `core/services/telegram_service.py`.
   */
  const obnovit_kartochku = async () => {
    if (vybran == null) return;
    if (!kartochkaGuard.take()) return;
    try {
      const svezhaya = await api.post<any>(`/telegram/chats/${vybran}/client/refresh`, {});
      // Сливаем поверх, а не подменяем целиком: ручка отвечает карточкой, а в
      // `delo` рядом с ней лежат заявки, остатки и валюта, которых у неё нет.
      // Подмена стёрла бы врезку заявок до следующего открытия диалога.
      setDelo((bylo: any) => ({ ...(bylo || {}), ...svezhaya }));
      toast(t("tgClientRefreshed"));
    } catch (beda) {
      toastError(beda);
    } finally {
      kartochkaGuard.free();
    }
  };

  /** Забрать видео, которое сразу не тянули. */
  const zabrat = async (stroka: TgMessage) => {
    try {
      const svezhee = await api.post<TgMessage>(
        `/telegram/chats/${stroka.chat_id}/messages/${stroka.id}/fetch`,
      );
      if (vybranRef.current !== svezhee.chat_id) return;
      setMessages((bylo) => slit(bylo, [svezhee]));
    } catch (beda) {
      toastError(beda);
    }
  };

  /** Завести заявку или напоминание по этой переписке.
   *
   * Ради этого канал и внутри CRM: иначе менеджер читает разговор здесь, а
   * заявку заводит отдельно, перенося сведения руками.
   */
  const zavesti = async (chto: "deal" | "task") => {
    if (vybran == null) return;
    // Заявка заводится НА КАРТОЧКУ, и сервер отказывает непривязанному диалогу
    // (`telegram_chat_not_linked`). Говорим об этом сами и на языке интерфейса:
    // ответ сервера обращён к разработчику и приходит по-английски, а человеку
    // у экрана нужна не причина отказа, а следующее движение — выбрать клиента
    // в списке рядом. Кнопку при этом не гасим: погашенная кнопка молчит о том,
    // чего ей не хватает, и нажать её, чтобы узнать, уже нельзя.
    //
    // Напоминания это не касается: «перезвонить этому человеку» осмысленно и
    // до того, как выяснили, кто он, — так же решено и на сервере.
    if (chto === "deal" && klient_id == null) {
      toast(t("tgDealNeedsClient"), true);
      return;
    }
    if (!guard.take()) return;
    try {
      const sozdano = await api.post<{ id: number; title: string }>(
        `/telegram/chats/${vybran}/${chto}`,
        {},
      );
      toast(`${t(chto === "deal" ? "tgDealMade" : "tgTaskMade")}: ${sozdano.title}`);
      // Дело клиента рядом обязано показать заведённое. Без этого заявка
      // есть, а в панели её нет до переоткрытия диалога — и человек
      // заводит вторую такую же.
      setDeloOsvezhit((bylo) => bylo + 1);
    } catch (beda) {
      toastError(beda);
    } finally {
      guard.free();
    }
  };

  /** Подставить шаблон в поле ввода.
   *
   * Именно ПОДСТАВИТЬ, а не отправить. Шаблон — заготовка: в него дописывают
   * подробности этого разговора. Отправляй он сразу, им пользовались бы только
   * там, где ответ дословно типовой, то есть почти нигде.
   */
  const podstavit_shablon = async (id: string) => {
    if (!id) return;
    try {
      const gotovo = await api.get<{ text: string; missing?: string[] }>(
        // Клиента берём из `klient_id` — того же значения, по которому рядом
        // грузится дело. Нет привязки — параметра нет вовсе, и это законный
        // путь: подстановка отвечает и без клиента (это предпросмотр), а
        // отказывать в шаблоне из-за того, что собеседник ещё не опознан,
        // значило бы отключить шаблоны в половине разговоров.
        `/templates/${id}/render${klient_id != null ? `?client_id=${klient_id}` : ""}`,
      );
      // Дописываем к тому, что уже набрано, а не затираем: человек мог начать
      // печатать и вспомнить про шаблон.
      setTekst((bylo) => (bylo ? `${bylo}\n${gotovo.text}` : gotovo.text));
      // Поля, которых нечем заполнить, сервер называет поимённо — и молчать о
      // них нельзя. В непривязанном диалоге шаблон подставляется как
      // «Здравствуйте, —!», прочерк посреди фразы глазами не ловится, и
      // уезжает такое клиенту. Отправку не запрещаем: прочерк бывает и
      // уместен, решает человек, — но узнать он должен ДО нажатия «отправить».
      const propusk = gotovo.missing ?? [];
      if (propusk.length > 0) {
        toast(t("tgTemplateGaps", { list: propusk.join(", ") }), true);
      }
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
        { text: tekst, reply_to_id: otvechaem?.id ?? null },
      );
      // Ушло в тот диалог, что был открыт при нажатии, — и показать это надо
      // там же. Успел человек переключиться — сообщение отправлено верно, а вот
      // показывать его в чужой переписке нельзя.
      if (vybranRef.current === stroka.chat_id) {
        setMessages((bylo) => slit(bylo, [stroka]));
        posledneye.current = stroka.id;
      }
      setTekst("");
      setOtvechaem(null);
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
    // Отказ ДО заливки, а не после. Своим пределом телеграм отвечает уже приняв
    // файл целиком: на показе видео в сто мегабайт лилось до конца, и только
    // тогда приходило «Request Entity Too Large» — чужими словами и без намёка
    // на то, что делать. Проверка здесь стоит миллисекунду и говорит по делу.
    if (fayl.size > TG_PREDEL_FAYLA) {
      toastError(
        new Error(
          t("tgTooBig")
            .replace("{max}", String(TG_PREDEL_FAYLA / 1024 / 1024))
            .replace("{size}", razmer(fayl.size)),
        ),
      );
      return;
    }
    if (!guard.take()) return;
    setOtpravka(true);
    setZaliv({ imya: fayl.name, ushlo: 0, vsego: fayl.size, otmenit: () => {} });
    try {
      const zalivka = api.zagruzka<TgMessage>(
        `/telegram/chats/${vybran}/files`,
        fayl,
        ({ ushlo, vsego }) =>
          setZaliv((bylo) => (bylo ? { ...bylo, ushlo, vsego } : bylo)),
      );
      setZaliv((bylo) => (bylo ? { ...bylo, otmenit: zalivka.otmenit } : bylo));
      const stroka = await zalivka.gotovo;
      // Заливка идёт долго — переключиться за это время успевают наверняка.
      if (vybranRef.current === stroka.chat_id) {
        setMessages((bylo) => slit(bylo, [stroka]));
        posledneye.current = stroka.id;
      }
      void zagruzit_chats();
    } catch (beda) {
      // Отменил сам — значит не беда, и ругаться незачем.
      if ((beda as any)?.code !== "canceled") toastError(beda);
    } finally {
      guard.free();
      setZaliv(null);
      setOtpravka(false);
    }
  };

  if (chats === null) return <ScreenLoading error={failure} onRetry={() => void zagruzit_chats()} />;

  const otkrytyy = chats.find((c) => c.id === vybran) || null;

  return (
    <div className={vybran != null ? "tg-screen is-talking" : "tg-screen"}>
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
                  className={[
                    "tg-chat",
                    chat.id === vybran ? "is-active" : "",
                    // Непрочитанное видно и не доводя глаз до правого края:
                    // счётчик говорит «сколько», насыщенность строки — «есть».
                    chat.unread > 0 ? "is-unread" : "",
                  ]
                    .filter(Boolean)
                    .join(" ")}
                  onClick={() => setVybran(chat.id)}
                >
                  <span className="tg-face">
                    <Avatar
                      small
                      text={initials(chat.title || chat.username || "?")}
                      src={chat.has_avatar ? `/api/v1/telegram/chats/${chat.id}/avatar` : null}
                    />
                  </span>
                  <span className="tg-chat-title">
                    {chat.title || chat.username}
                    {chat.has_emoji ? (
                      /*
                        Именной эмодзи вместо звёздочки: человек выбрал его сам,
                        и узнают его по нему. Звёздочка остаётся тем, у кого
                        премиум есть, а значка нет.
                      */
                      <img
                        className="tg-emoji"
                        src={`/api/v1/telegram/chats/${chat.id}/emoji`}
                        alt={t("tgPremium")}
                        title={t("tgPremium")}
                        loading="lazy"
                      />
                    ) : (
                      chat.is_premium && (
                        <span className="tg-premium" title={t("tgPremium")}>
                          ★
                        </span>
                      )
                    )}
                  </span>
                  <span className="tg-chat-time">
                    {chat.last_message_at ? formatDateTime(chat.last_message_at, locale) : ""}
                  </span>
                  <span className="tg-chat-prev">
                    {chat.last_direction === "out" && <em>{t("tgYou")} </em>}
                    {chat.last_preview ||
                      (chat.last_kind === "photo"
                        ? t("tgKindPhoto")
                        : chat.last_kind === "video"
                          ? t("tgKindVideo")
                          : chat.last_kind === "voice"
                            ? t("tgKindVoice")
                            : chat.last_kind === "document"
                              ? t("tgKindDocument")
                              : "")}
                  </span>
                  {chat.unread > 0 && (
                    <span className="tg-unread" aria-label={t("tgUnread")}>
                      {chat.unread}
                    </span>
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
              <button
                type="button"
                className="tg-back"
                onClick={() => setVybran(null)}
                aria-label={t("back")}
                title={t("back")}
              >
                <Icon name="arrowLeft" />
              </button>
              <div className="tg-who">
                <Avatar
                  text={initials(otkrytyy.title || otkrytyy.username || "?")}
                  src={
                    otkrytyy.has_avatar
                      ? `/api/v1/telegram/chats/${otkrytyy.id}/avatar`
                      : null
                  }
                />
                <strong>
                  {otkrytyy.title || otkrytyy.username}
                  {otkrytyy.has_emoji ? (
                    <img
                      className="tg-emoji"
                      src={`/api/v1/telegram/chats/${otkrytyy.id}/emoji`}
                      alt={t("tgPremium")}
                      title={t("tgPremium")}
                    />
                  ) : (
                    otkrytyy.is_premium && (
                      <span className="tg-premium" title={t("tgPremium")}>
                        ★
                      </span>
                    )
                  )}
                </strong>
                {/*
                  Логин есть — ссылка в профиль. Логина нет — не ссылка, а
                  копирование идентификатора: `tg://user?id=` открывается только
                  в установленном приложении и в браузере молча не делает
                  ничего, а мёртвая ссылка хуже её отсутствия. По
                  скопированному числу человека находят в самом телеграме.
                */}
                {otkrytyy.username ? (
                  <a
                    className="tg-profile"
                    href={`https://t.me/${otkrytyy.username}`}
                    target="_blank"
                    rel="noreferrer"
                    title={t("tgOpenProfile")}
                  >
                    @{otkrytyy.username}
                  </a>
                ) : (
                  <button
                    type="button"
                    className="tg-profile"
                    onClick={() =>
                      void copyText(String(otkrytyy.chat_id)).then((vyshlo) =>
                        vyshlo ? toast(t("copied")) : toast(String(otkrytyy.chat_id)),
                      )
                    }
                    title={t("tgCopyChatId")}
                  >
                    ID {otkrytyy.chat_id}
                  </button>
                )}
              </div>
              <div className="tg-head-right">
                {/*
                  К какой карточке привязан диалог — на виду, потому что это
                  первое, что спрашивают, отвечая клиенту. Привязан — ссылка
                  прямо в карточку; не привязан — слово «нет карточки», по
                  которому открывается тот же выбор, что и был.
                */}
                {otkrytyy.client_id != null ? (
                  <Link className="tg-chip" to={`/clients/${otkrytyy.client_id}`}>
                    <Icon name="user" size={13} />
                    {delo?.name || t("tgClient")}
                  </Link>
                ) : (
                  <button
                    type="button"
                    className="tg-chip tg-chip-empty"
                    onClick={() => setPrivyazka((bylo) => !bylo)}
                    aria-expanded={privyazka}
                  >
                    <Icon name="user" size={13} />
                    {t("tgUnlinked")}
                  </button>
                )}

                <div className="tg-actions">
                  <button
                    type="button"
                    className="btn-icon"
                    aria-label={t("actions")}
                    aria-expanded={menyu}
                    onClick={() => setMenyu((bylo) => !bylo)}
                  >
                    <Icon name="dots" />
                  </button>
                  {menyu && (
                    <div className="user-menu tg-menu">
                      {/*
                        Пункт стоит за тем же правом, что спросит сервер:
                        заводится ЗАЯВКА, и решает раздел заявок. Без права его
                        нет вовсе — иначе он отвечал бы отказом на каждое
                        нажатие, то есть выглядел бы сломанным.

                        Непривязанный диалог — другое дело: право есть, не
                        хватает одного движения. Поэтому пункт на месте и живой,
                        а подсказка называет это движение и указывает на выбор
                        карточки рядом.
                      */}
                      {can(user, "deals.create") && (
                        <button
                          type="button"
                          className="user-menu-item"
                          disabled={guard.busy}
                          title={
                            otkrytyy.client_id == null
                              ? t("tgDealNeedsClient")
                              : t("tgMakeDeal")
                          }
                          onClick={() => {
                            setMenyu(false);
                            void zavesti("deal");
                          }}
                        >
                          <Icon name="deals" size={14} />
                          {t("tgMakeDeal")}
                        </button>
                      )}
                      {can(user, "tasks.create") && (
                        <button
                          type="button"
                          className="user-menu-item"
                          disabled={guard.busy}
                          onClick={() => {
                            setMenyu(false);
                            void zavesti("task");
                          }}
                        >
                          <Icon name="clock" size={14} />
                          {t("tgMakeTask")}
                        </button>
                      )}
                      <div className="user-menu-sep" />
                      <button
                        type="button"
                        className="user-menu-item"
                        onClick={() => {
                          setMenyu(false);
                          setPrivyazka(true);
                        }}
                      >
                        <Icon name="link" size={14} />
                        {otkrytyy.client_id != null ? t("tgRelink") : t("tgLink")}
                      </button>
                      {otkrytyy.source && (
                        <>
                          <div className="user-menu-sep" />
                          <span className="tg-menu-note">
                            {t("tgCameFrom")}: {otkrytyy.source}
                          </span>
                        </>
                      )}
                    </div>
                  )}
                </div>
              </div>

              {/*
                Выбор карточки — отдельной строкой и только когда его позвали.
                Прежде он стоял в шапке всегда: место занимал постоянно, а
                трогают его один раз за всю жизнь диалога.

                Справочник не приехал — говорим об этом, а не рисуем пустой
                выбор. Пустой выбор читается как «клиентов нет», и человек
                заводит второго того же клиента.
              */}
              {privyazka && (
                <label className="tg-link">
                  <span>{t("tgClient")}</span>
                  <select
                    value={otkrytyy.client_id ?? ""}
                    onChange={(e) => {
                      void privyazat(e.target.value ? Number(e.target.value) : null);
                      setPrivyazka(false);
                    }}
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
              )}
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

            <div
              className="tg-feed"
              ref={lentaRef}
              onScroll={(e) => {
                // Запас в 24 пикселя, а не ноль: плавная прокрутка и дробные
                // высоты строк оставляют полпикселя недоезда, и «у самого низа»
                // с точностью до нуля не наступает почти никогда.
                const uzel = e.currentTarget;
                vnizu.current = uzel.scrollHeight - uzel.scrollTop - uzel.clientHeight < 24;
              }}
            >
              {estGlubzhe && (
                <button type="button" className="tg-more" onClick={() => void pokazat_eshchyo()}>
                  {t("tgOlder")}
                </button>
              )}
              {messages.map((stroka) => (
                <div
                  key={stroka.id}
                  id={`tg-msg-${stroka.id}`}
                  className={[
                    "tg-msg",
                    `tg-${stroka.direction}`,
                    // Метка новизны считается номером и НЕ снимается: узел у
                    // React один и тот же (ключ — номер сообщения), а анимация
                    // без повтора проигрывается однажды на узел. Отсюда и ответ
                    // на отправку в два шага: подтверждение (`pending` → `sent`)
                    // меняет только состояние, узел остаётся прежним, и второго
                    // прыжка не будет. Само подтверждение показано цветом —
                    // пузырь дозревает до своего, как синева в iMessage.
                    stroka.id > granica.current ? "is-fresh" : "",
                    stroka.send_state === "pending" ? "is-pending" : "",
                    stroka.send_state === "failed" ? "is-failed" : "",
                  ]
                    .filter(Boolean)
                    .join(" ")}
                >
                  <button
                    type="button"
                    className="tg-reply"
                    onClick={() => setOtvechaem(stroka)}
                    aria-label={t("tgReply")}
                    title={t("tgReply")}
                  >
                    <Icon name="send" size={12} />
                  </button>
                  {stroka.reply_to_id != null && (
                    /*
                      Цитата отвечает на вопрос «а на что это ответ». Берём из
                      уже загруженной ленты: сообщение, которому отвечали, почти
                      всегда рядом. Нет его (ушло глубже, чем открыто) —
                      показываем, что ответ есть, но цитаты не видно; врать
                      «ответ на последнее» нельзя, ровно из-за этого пункт и
                      заведён.
                    */
                    <button
                      type="button"
                      className="tg-quote"
                      onClick={() => {
                        const uzel = document.getElementById(`tg-msg-${stroka.reply_to_id}`);
                        uzel?.scrollIntoView({ block: "center", behavior: "smooth" });
                      }}
                    >
                      {messages.find((s) => s.id === stroka.reply_to_id)?.body?.slice(0, 90) ||
                        t("tgQuoteGone")}
                    </button>
                  )}
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
                  {stroka.has_file &&
                    (stroka.kind === "photo" ? (
                      /*
                       * Фотография — картинкой, и это не украшение. Прежде она
                       * была ссылкой из опасения натянуть на экран всё подряд
                       * при открытии диалога. Довод снимается `loading="lazy"`:
                       * браузер тянет только то, что доехало до глаз. А цена
                       * ошибки в другую сторону оказалась куда выше — на показе
                       * присланного фото попросту не было видно, и мессенджер
                       * переставал быть мессенджером.
                       *
                       * Ссылка вокруг остаётся: щелчок открывает целиком, потому
                       * что в переписке картинка ужата до ширины пузыря.
                       */
                      <a
                        className="tg-photo"
                        href={`/api/v1/telegram/chats/${stroka.chat_id}/messages/${stroka.id}/file`}
                        target="_blank"
                        rel="noreferrer"
                      >
                        <img
                          src={`/api/v1/telegram/chats/${stroka.chat_id}/messages/${stroka.id}/file`}
                          alt={stroka.file_name || t("tgFile")}
                          loading="lazy"
                          // Размеров у картинки заранее нет, и до загрузки она
                          // занимает нисколько. Доехав, она вырастает на
                          // три сотни пикселей — уже ПОСЛЕ того, как ленту
                          // прокрутили вниз, — и своё же сообщение уезжает под
                          // край. Поправляем, но только если стояли внизу:
                          // отлистанному вверх этот рывок был бы вмешательством.
                          onLoad={() => {
                            if (vnizu.current) k_nizu(false);
                          }}
                        />
                      </a>
                    ) : stroka.kind === "video" ? (
                      /*
                       * Забранное видео — проигрывателем, но `preload`
                       * «metadata»: браузер берёт заголовок ради длительности
                       * и первого кадра, а сами десятки мегабайт тянет только
                       * по нажатию «играть». Без этой оговорки открытие
                       * диалога качало бы всё видео подряд у каждого, кто
                       * пролистал мимо.
                       */
                      <video
                        className="tg-video"
                        controls
                        preload="metadata"
                        src={`/api/v1/telegram/chats/${stroka.chat_id}/messages/${stroka.id}/file`}
                      />
                    ) : (
                      /*
                       * Всё прочее — ссылкой: документ показывать нечем, а
                       * скачивание браузер делает сам. Файл отдаётся через
                       * приложение и только тому, у кого есть право на раздел.
                       */
                      <a
                        className="tg-file"
                        href={`/api/v1/telegram/chats/${stroka.chat_id}/messages/${stroka.id}/file`}
                        target="_blank"
                        rel="noreferrer"
                      >
                        <Icon name="download" /> {stroka.file_name || t("tgFile")}
                        {stroka.file_size ? (
                          <span className="tg-file-size">{razmer(stroka.file_size)}</span>
                        ) : null}
                      </a>
                    ))}
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

            {zaliv && (
              <div className="tg-upload" role="status" aria-live="polite">
                <div className="tg-upload-head">
                  <span className="tg-upload-name">{zaliv.imya}</span>
                  <span className="tg-upload-num">
                    {razmer(zaliv.ushlo)} / {razmer(zaliv.vsego)}
                  </span>
                  <button
                    type="button"
                    className="tg-upload-stop"
                    onClick={() => zaliv.otmenit()}
                  >
                    {t("cancel")}
                  </button>
                </div>
                <div className="tg-upload-bar">
                  <div
                    className="tg-upload-fill"
                    style={{
                      width: `${zaliv.vsego ? (zaliv.ushlo / zaliv.vsego) * 100 : 0}%`,
                    }}
                  />
                </div>
              </div>
            )}
            {otvechaem && (
              <div className="tg-reply-bar">
                <span>{t("tgReplyTo")}:</span>
                <span className="tg-reply-text">
                  {otvechaem.body?.slice(0, 90) || otvechaem.file_name || t("tgFile")}
                </span>
                <button type="button" onClick={() => setOtvechaem(null)}>
                  {t("cancel")}
                </button>
              </div>
            )}
            <footer className="tg-compose">
              {/*
                Выбор стоит на месте всегда, пока блок включён и право есть, —
                и когда шаблонов ещё ни одного тоже. Прежде он появлялся только
                при непустом списке, и на свежей системе в подписи не было
                ВООБЩЕ ничего: человек искал шаблоны там, где их обещали, не
                находил даже места, где они должны быть, и делал единственный
                возможный вывод — «не работают».

                Поэтому три состояния названы врозь прямо в закрытом виде
                выбора, не требуя его открывать: «Шаблон» (есть из чего
                выбирать), «шаблонов пока нет» (справочник ответил пустым) и
                «не удалось загрузить» (не ответил вовсе). Свести их к одному
                пустому списку значит ответить на вопрос, которого не задавали.
                Пока ответа нет, подпись остаётся нейтральной: «шаблонов нет»
                на полсекунды загрузки — то же самое враньё, только короткое.
              */}
              {shablony_dostupny && (
                <select
                  className="tg-templates"
                  value=""
                  onChange={(e) => {
                    void podstavit_shablon(e.target.value);
                    e.target.value = "";
                  }}
                  aria-label={t("tgTemplate")}
                  title={t("tgTemplate")}
                >
                  <option value="">
                    {shablony.failure != null
                      ? t("loadFailed")
                      : shablony.items != null && shablony.items.length === 0
                        ? t("tgNoTemplates")
                        : t("tgTemplate")}
                  </option>
                  {(shablony.items ?? []).map((shablon: any) => (
                    <option key={shablon.id} value={shablon.id}>
                      {shablon.name}
                    </option>
                  ))}
                </select>
              )}
              <label
                className="tg-attach"
                title={t("tgAttach").replace("{max}", String(TG_PREDEL_FAYLA / 1024 / 1024))}
              >
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

      {/*
        Третья колонка появляется у того, кому клиенты видны, — и у
        непривязанного диалога тоже. Прежде она ждала привязки, и довод был
        честный: пустая колонка «здесь могло быть дело» отнимает место у
        переписки. Но именно у непривязанного диалога и есть что предложить —
        завести карточку одним нажатием, — а место для этого предложения нужно
        ровно там, где потом появится сама карточка.
      */}
      {otkrytyy !== null && vidno_klientov && (
        <aside className="tg-delo">
          {klient_id == null ? (
            <>
              <div className="tg-delo-pusto">{t("tgNoCard")}</div>
              {/*
                Прячем от того, кому сервер откажет: правило `lib/permissions.ts`.
                Кнопка, отвечающая отказом, хуже её отсутствия — она обещает.
              */}
              {can(user, "clients.create") && (
                <button
                  type="button"
                  className="tg-delo-knopka"
                  onClick={() => void zavesti_kartochku()}
                  disabled={kartochkaGuard.busy}
                >
                  {t("tgMakeClient")}
                </button>
              )}
            </>
          ) : delo === null ? (
            <div className="tg-delo-pusto">{deloIdyot ? t("loading") : t("loadFailed")}</div>
          ) : (
            <>
              <div className="tg-delo-head">
                <Link className="tg-delo-name" to={`/clients/${delo.id}`}>
                  {delo.name}
                </Link>
                {delo.company && <div className="tg-delo-sub">{delo.company}</div>}
              </div>

              <div className="tg-delo-fields">
                {delo.phone && (
                  <div>
                    <span>{t("phone")}</span>
                    <a href={`tel:${delo.phone}`}>{delo.phone}</a>
                  </div>
                )}
                {delo.email && (
                  <div>
                    <span>{t("email")}</span>
                    <a href={`mailto:${delo.email}`}>{delo.email}</a>
                  </div>
                )}
              </div>

              {/*
                Кнопка появляется только тогда, когда переносить есть что:
                клиент поделился номером, а в карточке его нет; завёл логин, а в
                карточке пусто. Висеть постоянно ей нельзя — нажатие, которое
                девять раз из десяти ничего не меняет, перестают замечать, и
                десятый раз тоже.
              */}
              {can(user, "clients.edit") && est_chto_perenesti(otkrytyy, delo) && (
                <button
                  type="button"
                  className="tg-delo-knopka"
                  onClick={() => void obnovit_kartochku()}
                  disabled={kartochkaGuard.busy}
                >
                  {t("tgClientRefresh")}
                </button>
              )}

              <div className="tg-delo-title">{t("deals")}</div>
              {(delo.deals ?? []).length === 0 ? (
                <div className="tg-delo-pusto">{t("tgNoDeals")}</div>
              ) : (
                <ul className="tg-delo-deals">
                  {(delo.deals ?? []).map((zayavka: any) => (
                    <li key={zayavka.id}>
                      <Link to={`/deals/${zayavka.id}`}>{zayavka.title}</Link>
                      <div className="tg-delo-line">
                        <span>
                          {(stages.items ?? []).find((s: any) => s.key === zayavka.stage)?.name ||
                            zayavka.stage}
                        </span>
                        {/*
                          Показываем ОСТАТОК, а не сумму: отвечая клиенту, важно
                          «сколько он должен», а не «сколько стоило». Пусто —
                          значит сумм этому сотруднику не показывают, и это не
                          то же самое, что ноль.
                        */}
                        {zayavka.remainder != null && (
                          <strong>
                            {formatMoney(zayavka.remainder, delo.currency || "USD", locale)}
                          </strong>
                        )}
                      </div>
                    </li>
                  ))}
                </ul>
              )}
            </>
          )}
        </aside>
      )}
    </div>
  );
}
