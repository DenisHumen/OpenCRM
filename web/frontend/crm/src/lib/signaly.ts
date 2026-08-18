/**
 * Сигнал о письме клиента: всплывающее окно и звук.
 *
 * Четыре правила, и каждое взято из того, как такие уведомления перестают
 * работать в первый же день:
 *
 * 1. **Разрешение спрашивается по нажатию, а не при загрузке.** Спрошенное
 *    при загрузке разрешение люди отклоняют не глядя, а отклонённое браузер
 *    запоминает НАВСЕГДА: второго раза не будет, и починить это из приложения
 *    нельзя.
 * 2. **Звук — только после взаимодействия.** Автовоспроизведение запрещено;
 *    звук, заведённый без нажатия, просто не прозвучит, и разбираться в этом
 *    будет некому.
 * 3. **Только на входящие и только когда чат не на виду.** Сигнал на
 *    собственный ответ и на открытый диалог — самый быстрый способ добиться,
 *    чтобы уведомления выключили совсем.
 * 4. **Одна вкладка на человека.** Три открытые вкладки CRM — три сигнала на
 *    одно сообщение.
 */

/** Хочет ли человек сигналов. Хранится в браузере — как и само разрешение. */
const KLYUCH = "opencrm.tg.signaly";

/**
 * Имя замка для выбора «сигналящей» вкладки.
 *
 * Замки браузера (`navigator.locks`), а не отметка в `localStorage` со сроком
 * годности. Отметка гоняется: две вкладки читают её одновременно, обе видят
 * свободно, обе пишут себя и обе сигналят. Замок выдаётся ровно одной вкладке
 * и освобождается сам, когда она закрывается, — то есть выбор не «почти
 * всегда один», а один.
 */
const ZAMOK = "opencrm-tg-signal";

let vedushchaya = false;
let vybory_nachaty = false;
let zvuk: AudioContext | null = null;
let otkrytyy_chat: number | null = null;

/**
 * Ключ, которым вкладки сообщают друг другу, на какой диалог сейчас смотрят.
 *
 * **Зачем это общее знание.** Сигналит ОДНА вкладка — та, что взяла замок. А
 * смотрят на переписку в другой: список заявок открыт в первой вкладке,
 * мессенджер во второй, и это обычный способ работы. Ведущая вкладка про
 * вторую не знает ничего: у неё свой `document.visibilityState` и свой
 * «открытый чат». Получалось, что звук и всплывающее окно приходят на
 * сообщение, которое человек читает прямо сейчас, — то самое, от чего
 * уведомления выключают в первый же день.
 *
 * `localStorage` виден всем вкладкам одного сайта и читается синхронно, то есть
 * ровно то, что нужно для «а смотрит ли кто-нибудь».
 */
const VIDNO = "opencrm.tg.vidno";

/** Сколько отметка «смотрю» считается свежей. Подтверждается каждые 5 секунд. */
const SVEZHEST_VIDNO = 12000;

/** Своё имя вкладки: чтобы снимать СВОЮ отметку, а не чужую. */
const MOYA_VKLADKA = Math.random().toString(36).slice(2);

/**
 * Экран сообщает, какой диалог сейчас открыт (или `null`, если ушли).
 *
 * Зовётся и по смене диалога, и биением каждые несколько секунд: отметка
 * протухает нарочно. Вкладку могут закрыть на полуслове, и вечная отметка
 * означала бы диалог, о котором навсегда перестали сигналить.
 */
export function otmetit_otkrytyy_chat(chat_id: number | null): void {
  otkrytyy_chat = chat_id;
  try {
    if (chat_id == null || document.visibilityState !== "visible") {
      const syroe = localStorage.getItem(VIDNO);
      // Снимаем ТОЛЬКО свою отметку: чужая вкладка может смотреть на тот же
      // диалог, и глушить её нам нечем.
      if (syroe && JSON.parse(syroe)?.vkladka === MOYA_VKLADKA) {
        localStorage.removeItem(VIDNO);
      }
      return;
    }
    localStorage.setItem(
      VIDNO,
      JSON.stringify({ chat_id, ts: Date.now(), vkladka: MOYA_VKLADKA }),
    );
  } catch {
    /* приватный режим: останется знание своей вкладки, и то хлеб */
  }
}

/** Смотрит ли человек прямо сейчас на этот диалог — в ЛЮБОЙ из своих вкладок. */
export function chat_na_vidu(chat_id: number): boolean {
  if (document.visibilityState === "visible" && otkrytyy_chat === chat_id) return true;
  try {
    const syroe = localStorage.getItem(VIDNO);
    if (!syroe) return false;
    const zapis = JSON.parse(syroe);
    return zapis?.chat_id === chat_id && Date.now() - (zapis?.ts ?? 0) < SVEZHEST_VIDNO;
  } catch {
    return false;
  }
}

/** Включены ли сигналы в этом браузере. */
export function signaly_vklyucheny(): boolean {
  try {
    return localStorage.getItem(KLYUCH) === "1";
  } catch {
    // Приватный режим запрещает хранилище. Молча считаем выключенными: сигнал
    // без возможности его отключить хуже отсутствия сигнала.
    return false;
  }
}

/**
 * Включить сигналы. Зовётся ТОЛЬКО из обработчика нажатия.
 *
 * Возвращает, чем кончилось: `granted` — будет и окно, и звук; `denied` —
 * человек отказал, останется только звук; `unsupported` — браузер не умеет
 * всплывающих окон.
 */
export async function vklyuchit_signaly(): Promise<"granted" | "denied" | "unsupported"> {
  try {
    localStorage.setItem(KLYUCH, "1");
  } catch {
    /* хранилища нет — сигналы проживут до перезагрузки */
  }
  // Заводим звук здесь же, внутри нажатия: в другом месте браузер оставит
  // контекст в состоянии `suspended`, и первый же сигнал промолчит.
  razbudit_zvuk();
  nachat_vybory();

  if (typeof Notification === "undefined") return "unsupported";
  if (Notification.permission === "granted") return "granted";
  if (Notification.permission === "denied") return "denied";
  const otvet = await Notification.requestPermission();
  return otvet === "granted" ? "granted" : "denied";
}

export function vyklyuchit_signaly(): void {
  try {
    localStorage.removeItem(KLYUCH);
  } catch {
    /* нечего убирать */
  }
}

/** Выбрать вкладку, которая сигналит. Безопасно звать сколько угодно раз. */
export function nachat_vybory(): void {
  if (vybory_nachaty) return;
  vybory_nachaty = true;
  const zamki = (navigator as any).locks;
  if (!zamki?.request) {
    // Замков нет — сигналит всякая вкладка. Лучше три сигнала, чем ни одного:
    // пропущенное сообщение клиента дороже лишнего звука.
    vedushchaya = true;
    return;
  }
  void zamki.request(ZAMOK, () => {
    vedushchaya = true;
    // Держим замок, пока жива вкладка. Закроется — браузер отпустит сам, и
    // замок достанется следующей из очереди.
    return new Promise<void>(() => {});
  });
}

function razbudit_zvuk(): void {
  try {
    const Kontekst = (window as any).AudioContext || (window as any).webkitAudioContext;
    if (!Kontekst) return;
    zvuk ||= new Kontekst();
    void zvuk?.resume();
  } catch {
    /* звука не будет, окно останется */
  }
}

/**
 * Короткий сигнал.
 *
 * Синтезом, а не звуковым файлом. Файл — это ещё один путь, который надо
 * положить в сборку, отдать с правильным заголовком и не потерять при
 * переезде; всё это ради двух десятых секунды. Две ноты подряд слышны и не
 * похожи ни на системный звук, ни на музыку из соседней вкладки.
 */
function propikat(): void {
  // Разбудить перед каждым сигналом, а не однажды при включении.
  //
  // Настройка переживает перезагрузку страницы, а разрешение браузера на звук —
  // нет: после `F5` контекст создаётся «приостановленным», и пока его не
  // разбудили, не звучит ничего. Проверяй мы состояние один раз, сигнал молчал
  // бы до следующего захода в настройки — то есть всегда.
  razbudit_zvuk();
  if (!zvuk) return;
  if (zvuk.state !== "running") {
    // `resume` — обещание: расписание нот составляем ПОСЛЕ его исполнения,
    // иначе они назначены на время, которое уже прошло.
    void zvuk.resume().then(zagudet, () => {});
    return;
  }
  zagudet();
}

function zagudet(): void {
  try {
    if (!zvuk || zvuk.state !== "running") return;
    const teper = zvuk.currentTime;
    for (const [kogda, chastota] of [
      [0, 880],
      [0.12, 1174.7],
    ] as const) {
      const golos = zvuk.createOscillator();
      const gromkost = zvuk.createGain();
      golos.frequency.value = chastota;
      golos.type = "sine";
      // Плавное затухание: резкий обрыв синусоиды слышен щелчком.
      gromkost.gain.setValueAtTime(0.0001, teper + kogda);
      gromkost.gain.exponentialRampToValueAtTime(0.2, teper + kogda + 0.01);
      gromkost.gain.exponentialRampToValueAtTime(0.0001, teper + kogda + 0.11);
      golos.connect(gromkost).connect(zvuk.destination);
      golos.start(teper + kogda);
      golos.stop(teper + kogda + 0.12);
    }
  } catch {
    /* звук не обязателен, окно важнее */
  }
}

/**
 * Подать сигнал о новом сообщении.
 *
 * `na_vidu` — смотрит ли человек прямо сейчас на этот самый диалог. Знает об
 * этом только экран, поэтому решение приходит снаружи, а не угадывается здесь.
 */
export function signal_o_soobshchenii(opts: {
  zagolovok: string;
  telo: string;
  na_vidu: boolean;
}): void {
  if (!signaly_vklyucheny() || !vedushchaya || opts.na_vidu) return;

  propikat();
  try {
    if (typeof Notification !== "undefined" && Notification.permission === "granted") {
      const okno = new Notification(opts.zagolovok, {
        body: opts.telo,
        // Метка одна на все сообщения: десять писем подряд заменяют друг друга
        // в одном окне, а не выстраиваются башней на весь экран.
        tag: "opencrm-telegram",
        icon: "/static/favicon.svg",
      });
      okno.onclick = () => {
        window.focus();
        okno.close();
      };
    }
  } catch {
    /* окно не показалось — звук уже прозвучал */
  }
}
