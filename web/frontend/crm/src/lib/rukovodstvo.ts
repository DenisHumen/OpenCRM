/** Содержимое руководства. Данные, а не разметка.
 *
 * Дополнять руководство правкой одного файла, а не вёрстки: разделов будет
 * много, а экран у них один. Двуязычно, потому что интерфейс продукта
 * английский по умолчанию, а владелец читает по-русски.
 */

export type Yazyk = "ru" | "en";
export type Dvuyazychno = Record<Yazyk, string>;

/** Кусок статьи. Виды намеренно наперечёт: чем их меньше, тем ровнее вид. */
export type Kusok =
  | { vid: "abzats"; tekst: Dvuyazychno }
  | { vid: "spisok"; punkty: Dvuyazychno[] }
  | { vid: "shagi"; punkty: Dvuyazychno[] }
  | { vid: "vazhno"; tekst: Dvuyazychno }
  | { vid: "kod"; yazyk: string; tekst: string }
  | {
      vid: "ruchka";
      metod: string;
      put: string;
      opisanie: Dvuyazychno;
      polya?: { imya: string; tip: string; obyazatelno: boolean; opisanie: Dvuyazychno }[];
      zapros?: string;
      otvet?: string;
    };

export type Statya = {
  id: string;
  nazvanie: Dvuyazychno;
  kratko: Dvuyazychno;
  kuski: Kusok[];
};

export type Razdel = {
  id: string;
  nazvanie: Dvuyazychno;
  znachok: string;
  statyi: Statya[];
};

export const RUKOVODSTVO: Razdel[] = [
  {
    id: "nachalo",
    znachok: "dashboard",
    nazvanie: { ru: "Начало", en: "Getting started" },
    statyi: [
      {
        id: "chto-eto",
        nazvanie: { ru: "Что это такое", en: "What this is" },
        kratko: {
          ru: "CRM для малого дела: клиенты, заявки, склад, деньги и каналы связи в одном месте.",
          en: "A CRM for small business: clients, deals, stock, money and channels in one place.",
        },
        kuski: [
          {
            vid: "abzats",
            tekst: {
              ru: "Система собрана из блоков. Включён только тот, который вам нужен: мастерской не нужны накладные, магазину — доски работ. Выключенный блок исчезает целиком — из меню, из настроек, из отчётов, — а данные его остаются на месте.",
              en: "The system is made of modules. Only the ones you need are on: a workshop has no use for waybills, a shop has none for work boards. A disabled module disappears entirely — from the menu, settings and reports — while its data stays untouched.",
            },
          },
          {
            vid: "vazhno",
            tekst: {
              ru: "Тип дела выбирается один раз при первом запуске и включает нужный набор блоков. Позже любой блок включается и выключается по отдельности.",
              en: "You pick your line of business once at first start, and it switches on a matching set of modules. Afterwards every module can be toggled on its own.",
            },
          },
        ],
      },
      {
        id: "pervyy-zapusk",
        nazvanie: { ru: "Первый запуск", en: "First run" },
        kratko: {
          ru: "Четыре шага до рабочей системы.",
          en: "Four steps to a working system.",
        },
        kuski: [
          {
            vid: "shagi",
            punkty: [
              {
                ru: "Выберите тип дела — он включит подходящие блоки.",
                en: "Pick your line of business — it switches on the matching modules.",
              },
              {
                ru: "Смените пароль владельца: система потребует это сама при первом входе.",
                en: "Change the owner password: the system asks for it at first sign-in.",
              },
              {
                ru: "Назовите свою контору в настройках бренда — название встанет в панель и в печатные бланки.",
                en: "Name your company in brand settings — it goes into the sidebar and printed forms.",
              },
              {
                ru: "Заведите сотрудников и раздайте им должности: права выдаются должности, а не человеку.",
                en: "Add your staff and give them roles: permissions belong to a role, not to a person.",
              },
            ],
          },
        ],
      },
    ],
  },
  {
    id: "rabota",
    znachok: "clients",
    nazvanie: { ru: "Ежедневная работа", en: "Daily work" },
    statyi: [
      {
        id: "klienty",
        nazvanie: { ru: "Клиенты", en: "Clients" },
        kratko: {
          ru: "Карточка клиента — стержень: к ней сходятся заявки, бланки, звонки и переписка.",
          en: "The client card is the hub: deals, forms, calls and chats all lead back to it.",
        },
        kuski: [
          {
            vid: "abzats",
            tekst: {
              ru: "Поиск ищет по имени, фирме, телефону, почте и метке — одной строкой, без выбора поля. Список дочитывается кнопкой «Показать ещё»: рядом с ней всегда написано, сколько показано из скольких.",
              en: "Search covers name, company, phone, email and tag in one box — no field picker. The list loads more on demand, and the button always says how many of how many are shown.",
            },
          },
          {
            vid: "spisok",
            punkty: [
              {
                ru: "Метка источника показывает, откуда клиент пришёл: наклейка, сайт, квитанция.",
                en: "The source tag shows where the client came from: a sticker, the website, a receipt.",
              },
              {
                ru: "Выгрузка в CSV отдаёт ровно тот отбор, который сейчас на экране.",
                en: "CSV export returns exactly the selection currently on screen.",
              },
              {
                ru: "Правая кнопка на строке списка открывает меню: открыть, открыть в новой вкладке, скопировать ссылку, почту или телефон.",
                en: "Right-clicking a list row opens a menu: open, open in a new tab, copy the link, email or phone.",
              },
            ],
          },
        ],
      },
      {
        id: "zayavki",
        nazvanie: { ru: "Заявки и заказы", en: "Deals and orders" },
        kratko: {
          ru: "Заявка — работа, заказ — перечень товаров. У каждого свой путь.",
          en: "A deal is the work; an order is the list of goods. Each has its own path.",
        },
        kuski: [
          {
            vid: "abzats",
            tekst: {
              ru: "Заявки живут на доске по этапам воронки. Этапы свои у каждого дела: у мастерской «принято — в работе — готово», у студии «бриф — макет — сдача». Колонка дочитывается по одной, а число в её шапке считает весь этап, а не показанное.",
              en: "Deals live on a board of pipeline stages. Stages differ per business: a workshop uses “taken in — in progress — ready”, a studio uses “brief — draft — handover”. Each column loads more on its own, and the count in its header covers the whole stage, not just what is shown.",
            },
          },
          {
            vid: "vazhno",
            tekst: {
              ru: "Закрытие заказа списывает товар со склада. Если включён блок накладных, списание идёт бумагой — накладной, которую можно найти и отменить.",
              en: "Closing an order writes goods off stock. With the waybills module on, the write-off goes through a document you can find and reverse.",
            },
          },
        ],
      },
      {
        id: "sklad",
        nazvanie: { ru: "Склад", en: "Stock" },
        kratko: {
          ru: "Остаток не хранится числом — он равен сумме движений.",
          en: "The balance is not stored as a number — it is the sum of movements.",
        },
        kuski: [
          {
            vid: "abzats",
            tekst: {
              ru: "Любое изменение остатка — это движение с причиной: приход, отгрузка, списание, возврат, переучёт. Поэтому на вопрос «куда делось» всегда есть ответ, а хранимое число однажды разошлось бы с историей.",
              en: "Every change of stock is a movement with a reason: intake, shipment, write-off, return, recount. That way “where did it go” always has an answer, whereas a stored number would eventually drift from the history.",
            },
          },
          {
            vid: "spisok",
            punkty: [
              {
                ru: "«Доступно» — это остаток минус обещанное по незакрытым заказам.",
                en: "“Available” is the balance minus what open orders have promised.",
              },
              {
                ru: "Отгрузили часть заказа — обещание уменьшается ровно на отгруженное.",
                en: "Ship part of an order and the promise drops by exactly what shipped.",
              },
            ],
          },
        ],
      },
    ],
  },
  {
    id: "nastroyka",
    znachok: "settings",
    nazvanie: { ru: "Настройка", en: "Setup" },
    statyi: [
      {
        id: "prava",
        nazvanie: { ru: "Должности и права", en: "Roles and permissions" },
        kratko: {
          ru: "Право выдаётся должности. Интерфейс прячет то, на что права нет.",
          en: "Permissions belong to a role. The interface hides what you have no right to.",
        },
        kuski: [
          {
            vid: "abzats",
            tekst: {
              ru: "Право состоит из области и действия: «клиенты — смотреть», «заказы — видеть суммы». Кнопка, которой человек воспользоваться не может, ему не показывается вовсе: иначе он нажимает и получает отказ.",
              en: "A permission is an area plus an action: “clients — view”, “orders — see amounts”. A button a person cannot use is not shown at all: otherwise they press it and get refused.",
            },
          },
        ],
      },
      {
        id: "bloki",
        nazvanie: { ru: "Блоки системы", en: "Modules" },
        kratko: {
          ru: "Выключенный блок исчезает целиком, но данные его остаются.",
          en: "A disabled module disappears entirely, but its data stays.",
        },
        kuski: [
          {
            vid: "abzats",
            tekst: {
              ru: "Перед выключением система говорит, что именно исчезнет и сколько записей это затронет. Включите обратно — всё вернётся на место, ничего не теряется.",
              en: "Before switching one off the system tells you what will disappear and how many records it touches. Switch it back on and everything returns; nothing is lost.",
            },
          },
        ],
      },
    ],
  },
  {
    id: "api",
    znachok: "docs",
    nazvanie: { ru: "API", en: "API" },
    statyi: [
      {
        id: "obshchee",
        nazvanie: { ru: "Соглашения", en: "Conventions" },
        kratko: {
          ru: "Одна форма ответа, одна форма ошибки, одна форма страницы.",
          en: "One response shape, one error shape, one page shape.",
        },
        kuski: [
          {
            vid: "abzats",
            tekst: {
              ru: "Все ручки живут под /api/v1. Списки отдаются страницами и всегда сообщают, сколько записей всего: по этому числу видно, есть ли ещё.",
              en: "Every endpoint lives under /api/v1. Lists come in pages and always report the total, so you can tell whether there is more.",
            },
          },
          {
            vid: "kod",
            yazyk: "json",
            tekst: `{
  "items": [ /* … */ ],
  "total": 137,
  "page": 1,
  "per_page": 100
}`,
          },
          {
            vid: "abzats",
            tekst: {
              ru: "Отказ приходит одной и той же формой, где `code` — для программы, а `message` — для человека.",
              en: "A refusal always comes in the same shape, where `code` is for the program and `message` is for a human.",
            },
          },
          {
            vid: "kod",
            yazyk: "json",
            tekst: `{
  "error": {
    "code": "file_content_mismatch",
    "message": "The file does not look like a .pdf",
    "details": { }
  }
}`,
          },
          {
            vid: "vazhno",
            tekst: {
              ru: "Раздел будет дополняться: публичное API для сайта магазина ещё в работе. Форма ответов и ошибок при этом не изменится — она общая для всего продукта.",
              en: "This section will grow: the public API for a shop website is still in progress. The response and error shapes will not change — they are shared across the product.",
            },
          },
        ],
      },
      {
        id: "primer",
        nazvanie: { ru: "Пример: список клиентов", en: "Example: list clients" },
        kratko: {
          ru: "Как выглядит обычная читающая ручка.",
          en: "What an ordinary read endpoint looks like.",
        },
        kuski: [
          {
            vid: "ruchka",
            metod: "GET",
            put: "/api/v1/clients",
            opisanie: {
              ru: "Страница списка клиентов с тем же отбором, что и на экране.",
              en: "A page of the client list with the same selection as on screen.",
            },
            polya: [
              {
                imya: "search",
                tip: "string",
                obyazatelno: false,
                opisanie: {
                  ru: "Подстрока: имя, фирма, телефон, почта или метка.",
                  en: "Substring: name, company, phone, email or tag.",
                },
              },
              {
                imya: "page",
                tip: "integer ≥ 1",
                obyazatelno: false,
                opisanie: { ru: "Номер страницы, по умолчанию 1.", en: "Page number, defaults to 1." },
              },
              {
                imya: "per_page",
                tip: "integer 1…200",
                obyazatelno: false,
                opisanie: { ru: "Размер страницы, по умолчанию 50.", en: "Page size, defaults to 50." },
              },
            ],
            zapros: `curl -s "https://ваш-адрес/api/v1/clients?search=иван&per_page=2" \\
  -H "Cookie: session=…"`,
            otvet: `{
  "items": [
    { "id": 17, "name": "Иванов Пётр", "phone": "+380 50 111 2233" },
    { "id": 42, "name": "Иванова Мария", "phone": "+380 67 444 5566" }
  ],
  "total": 2,
  "page": 1,
  "per_page": 2
}`,
          },
        ],
      },
    ],
  },
  {
    id: "obsluzhivanie",
    znachok: "settings",
    nazvanie: { ru: "Обслуживание", en: "Maintenance" },
    statyi: [
      {
        id: "obnovlenie",
        nazvanie: { ru: "Обновление", en: "Updates" },
        kratko: {
          ru: "Сервер обновляется сам и откатывается, если что-то пошло не так.",
          en: "The server updates itself and rolls back if something goes wrong.",
        },
        kuski: [
          {
            vid: "abzats",
            tekst: {
              ru: "Перед сменой версии снимается копия базы, затем накатываются миграции. Если новая версия не отвечает, возвращается и код, и база. Состояния «работает, но данные не те» не бывает.",
              en: "Before a version change a database copy is taken, then migrations run. If the new version does not answer, both the code and the database are rolled back. There is no “running but with the wrong data” state.",
            },
          },
        ],
      },
      {
        id: "kopii",
        nazvanie: { ru: "Копии базы", en: "Backups" },
        kratko: {
          ru: "Копия снимается по расписанию и проверяется на годность.",
          en: "A copy is taken on schedule and checked for soundness.",
        },
        kuski: [
          {
            vid: "abzats",
            tekst: {
              ru: "Копия считается годной, только если в ней есть метка конца: оборванный дамп выглядит как обычный файл и подводит ровно тогда, когда из него надо восстановиться.",
              en: "A copy counts as sound only if it carries an end marker: a truncated dump looks like an ordinary file and fails exactly when you need to restore from it.",
            },
          },
        ],
      },
    ],
  },
];
