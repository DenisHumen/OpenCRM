/** Содержимое руководства. Данные, а не разметка.
 *
 * Дополнять руководство правкой одного файла, а не вёрстки: разделов будет
 * много, а экран у них один. Двуязычно, потому что интерфейс продукта
 * английский по умолчанию, а владелец читает по-русски.
 */

export type Yazyk = "ru" | "en";
export type Dvuyazychno = Record<Yazyk, string>;

/** Кусок статьи. Виды намеренно наперечёт: чем их меньше, тем ровнее вид.
 *
 * Каждый новый вид — это новый способ, которым сотня статей может выглядеть
 * по-разному, поэтому у каждого свой довод:
 *
 * - `svyortka` — длинный разбор под заголовком. Без неё «максимально детально»
 *   и «читаемо» противоречат друг другу: статья на блок отвечает «зачем и как»,
 *   а перечень всех полей прячется под заголовок и открывается тем, кому нужен;
 * - `tablitsa` — там, где у строки две стороны: право и что оно даёт, состояние
 *   и что с ним делать. Списком это не показать, а разбивать на два списка —
 *   значит заставить сличать их глазами;
 * - `vnimanie` — отдельно от `vazhno`. «Важно» — это совет, «внимание» — это
 *   «данные не вернуть». Одним видом их путают, и тогда перестают читать оба;
 * - `ekran` — ссылка на место в системе. Читатель уже внутри, и отправлять его
 *   искать раздел глазами после того, как о нём рассказали, — потеря половины
 *   пользы.
 */
export type Kusok =
  | { vid: "abzats"; tekst: Dvuyazychno }
  | { vid: "spisok"; punkty: Dvuyazychno[] }
  | { vid: "shagi"; punkty: Dvuyazychno[] }
  | { vid: "vazhno"; tekst: Dvuyazychno }
  | { vid: "vnimanie"; tekst: Dvuyazychno }
  | { vid: "kod"; yazyk: string; tekst: string }
  | { vid: "svyortka"; zagolovok: Dvuyazychno; kuski: Kusok[] }
  | { vid: "tablitsa"; shapka: Dvuyazychno[]; ryady: Dvuyazychno[][] }
  | { vid: "ekran"; put: string; podpis: Dvuyazychno }
  // Все ручки одним списком; сами данные порождены из docs/04-api.md.
  | { vid: "spravochnik" }
  | {
      vid: "ruchka";
      metod: string;
      put: string;
      opisanie: Dvuyazychno;
      polya?: { imya: string; tip: string; obyazatelno: boolean; opisanie: Dvuyazychno }[];
      zapros?: string;
      otvet?: string;
    };

/** Кому статья видна.
 *
 * Те же два признака, что у пункта меню (`lib/permissions.ts`), и по тому же
 * правилу: у кого нет блока — у того нет и статьи про него. Иначе руководство
 * описывает чужую систему, и читатель идёт искать раздел, которого у него нет.
 *
 * Без признаков — видно всем: так у общих статей про начало работы и поиск.
 */
export type Vidimost = {
  /** Блок, вместе с которым статья исчезает. */
  module?: string;
  /** Право, без которого статью показывать незачем. */
  perm?: string;
};

export type Statya = Vidimost & {
  id: string;
  nazvanie: Dvuyazychno;
  kratko: Dvuyazychno;
  kuski: Kusok[];
};

export type Razdel = Vidimost & {
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
        module: "clients",
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
        module: "deals",
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
        module: "warehouse",
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
              {
                ru: "У склада есть тип: хранение, магазин, в пути, брак. Сайту магазина виден остаток складов типа «магазин» и только их — подробности в статье «API для сайта магазина».",
                en: "A warehouse has a kind: storage, shop, transit, defect. The shop site sees the balance of «shop» warehouses and only them — details in the «Shop-site API» article.",
              },
              {
                ru: "У товара два текста: заметка кладовщика (наружу не уходит) и описание для сайта.",
                en: "A product has two texts: the storekeeper's note (never leaves the CRM) and the description for the site.",
              },
            ],
          },
        ],
      },
      {
        id: "nakleyki",
        perm: "settings.manage",
        module: "labels",
        nazvanie: { ru: "Наклейки и сканер", en: "Labels and the scanner" },
        kratko: {
          ru: "Свой штрихкод на коробке, печать по размеру рулона, поиск сканом.",
          en: "Your own barcode on the box, printing to your roll size, scan lookup.",
        },
        kuski: [
          {
            vid: "abzats",
            tekst: {
              ru: "У позиции бывает несколько кодов сразу: штука, блок, коробка. Отсканировали код коробки — в строку встало столько штук, сколько в ней лежит, а не одна.",
              en: "One item can carry several codes at once: a piece, a pack, a box. Scan the box code and the line takes the number of pieces inside it, not one.",
            },
          },
          {
            vid: "spisok",
            punkty: [
              {
                ru: "Штрихкоды рисует само приложение — сторонний сервис для этого не нужен и не подключается.",
                en: "Barcodes are drawn by the application itself — no third-party service is needed or used.",
              },
              {
                ru: "Что печатать на наклейке, выбираете вы: десять полей от единицы измерения до QR на карточку товара.",
                en: "You choose what goes on the label: ten fields from the unit of measure to a QR code pointing at the product card.",
              },
              {
                ru: "Размер задаётся под ваш рулон, а не под лист А4.",
                en: "The size is set for your roll, not for an A4 sheet.",
              },
              {
                ru: "Чужой код за своим не закрепится: сканер иначе молча подставлял бы не тот товар, и заметили бы это на инвентаризации.",
                en: "A foreign code will not attach to your product: otherwise the scanner would silently substitute the wrong item, and you would find out at stocktaking.",
              },
            ],
          },
          { vid: "ekran", put: "/settings/labels", podpis: { ru: "Настроить наклейку", en: "Set up the label" } },
        ],
      },
      {
        id: "napominaniya",
        perm: "tasks.view",
        module: "tasks",
        nazvanie: { ru: "Напоминания", en: "Reminders" },
        kratko: {
          ru: "Срок, исполнитель и счётчик просрочки в меню.",
          en: "A due date, an assignee and an overdue counter in the menu.",
        },
        kuski: [
          {
            vid: "abzats",
            tekst: {
              ru: "Напоминание — это то, о чём нельзя забыть: перезвонить, заказать деталь, отдать вещь. У него есть срок и исполнитель; клиент и заявка необязательны, потому что часть дел ни к кому не привязана.",
              en: "A reminder is something you must not forget: call back, order a part, hand the item over. It has a due date and an assignee; a client and a deal are optional, because some things belong to nobody in particular.",
            },
          },
          {
            vid: "abzats",
            tekst: {
              ru: "Просроченные считаются и показываются числом рядом с пунктом меню. Число видно с любого экрана — в этом и смысл: напоминание, которое надо пойти и посмотреть, работает хуже того, которое само попадается на глаза.",
              en: "Overdue ones are counted and shown as a number next to the menu item. You see it from any screen — that is the point: a reminder you have to go and look up works worse than one that catches your eye.",
            },
          },
          {
            vid: "vazhno",
            tekst: {
              ru: "Напоминание заводится прямо из карточки клиента, из заявки и из переписки в телеграме — не выходя туда, где оно понадобилось.",
              en: "A reminder can be created straight from a client card, a deal and a Telegram chat — without leaving the place where you needed it.",
            },
          },
          { vid: "ekran", put: "/tasks", podpis: { ru: "Открыть напоминания", en: "Open reminders" } },
        ],
      },
      {
        id: "shablony",
        perm: "templates.view",
        module: "templates",
        nazvanie: { ru: "Шаблоны сообщений", en: "Message templates" },
        kratko: {
          ru: "Готовые тексты с подстановками: имя клиента, название заявки, ссылка на доску.",
          en: "Ready-made texts with placeholders: client name, deal title, board link.",
        },
        kuski: [
          {
            vid: "abzats",
            tekst: {
              ru: "Шаблон — это текст, который пишут один раз и потом подставляют. Внутри него места для подстановки: имя клиента, название заявки, номер бланка, ссылка на доску работ, название вашей фирмы.",
              en: "A template is a text you write once and reuse. Inside it there are placeholders: client name, deal title, form number, work board link, your company name.",
            },
          },
          {
            vid: "abzats",
            tekst: {
              ru: "Незаполненные подстановки видно ДО отправки. Это не придирка: письмо с обращением по пустому имени уходит к клиенту один раз, а помнят о нём долго.",
              en: "Unfilled placeholders are visible BEFORE you send. That is not fussiness: a letter greeting an empty name goes out once and is remembered for a long time.",
            },
          },
          {
            vid: "svyortka",
            zagolovok: { ru: "Откуда берутся значения подстановок", en: "Where placeholder values come from" },
            kuski: [
              {
                vid: "spisok",
                punkty: [
                  {
                    ru: "Ссылка на доску — ЖИВАЯ: отозванная и просроченная не подставляются, потому что ведут на страницу с отказом. Из нескольких берётся самая ранняя, чтобы текст не менялся между отправками.",
                    en: "The board link is a LIVE one: revoked and expired links are not used, because they lead to a refusal page. Of several, the earliest is taken so the text does not change between sends.",
                  },
                  {
                    ru: "Название фирмы: фирма заявки, иначе основная фирма, иначе название бизнеса из настроек.",
                    en: "Company name: the deal company, else the default company, else the business name from settings.",
                  },
                  {
                    ru: "Выключенные блоки в текст не подтекают: без досок поле станет прочерком, а не ссылкой в никуда.",
                    en: "Disabled modules do not leak into the text: with boards off the field becomes a dash, not a link to nowhere.",
                  },
                ],
              },
            ],
          },
          { vid: "ekran", put: "/templates", podpis: { ru: "Открыть шаблоны", en: "Open templates" } },
        ],
      },
    ],
  },
  {
    id: "bumagi",
    znachok: "receipt",
    nazvanie: { ru: "Бумаги", en: "Paperwork" },
    statyi: [
      {
        id: "blanki",
        perm: "documents.view",
        module: "documents",
        nazvanie: { ru: "Бланки", en: "Forms" },
        kratko: {
          ru: "Квитанция приёмки и акт работ: номер, состояния, печать, штрихкод.",
          en: "Intake receipt and act of works: number, statuses, printing, barcode.",
        },
        kuski: [
          {
            vid: "abzats",
            tekst: {
              ru: "Бланк — единственное, что уходит из системы на руки человеку. Поэтому у него сквозной номер внутри года, история состояний с именами и печатная форма на русском, английском или украинском.",
              en: "A form is the only thing that leaves the system into someone else's hands. Hence a sequential number within the year, a status history with names, and a printable form in Russian, English or Ukrainian.",
            },
          },
          {
            vid: "abzats",
            tekst: {
              ru: "Акту можно дать своё название — «Наряд-заказ», «Акт сдачи-приёмки», — и оно печатается в шапке листа так, как вписано. Не вписали своего — в шапке стоит общее название на языке бумаги.",
              en: "An act can be given a title of your own — «Work order», «Handover certificate» — and it is printed in the sheet heading exactly as typed. Leave it empty and the heading carries the general title in the language of the paper.",
            },
          },
          {
            vid: "tablitsa",
            shapka: [
              { ru: "Состояние", en: "Status" },
              { ru: "Что оно значит", en: "What it means" },
            ],
            ryady: [
              [
                { ru: "Выдан", en: "Issued" },
                { ru: "Бумага напечатана и отдана.", en: "The paper is printed and handed over." },
              ],
              [
                { ru: "В работе", en: "In progress" },
                { ru: "Взяли в работу.", en: "Work has started." },
              ],
              [
                { ru: "Готово", en: "Ready" },
                { ru: "Сделано, ждём выдачи. Можно вернуть в работу: вещь забрали проверить и нашли ещё поломку.", en: "Done, waiting to be handed over. You can send it back to work: the item was checked and another fault turned up." },
              ],
              [
                { ru: "Закрыт", en: "Closed" },
                { ru: "Отдано. Оживить закрытый бланк нельзя.", en: "Handed over. A closed form cannot be revived." },
              ],
              [
                { ru: "Отменён", en: "Cancelled" },
                { ru: "Бумага отменена. Именно так бланк и убирают: он остаётся в истории.", en: "The paper is cancelled. This is how a form is put aside: it stays in the history." },
              ],
            ],
          },
          {
            vid: "vnimanie",
            tekst: {
              ru: "Бланк не удаляется ничем и никогда — ни кнопкой, ни правом. Выданная бумага не исчезает, это правило учёта: у клиента на руках копия, и расхождение с ней дороже любой уборки.",
              en: "A form is never deleted — not by a button, not by a permission. An issued paper does not vanish; it is an accounting rule: the client holds a copy, and disagreeing with it costs more than any tidiness.",
            },
          },
          {
            vid: "abzats",
            tekst: {
              ru: "На бумаге печатаются штрихкод и QR. Штрихкод читает сканер за стойкой — поле скана стоит вверху списка бланков и ловит ввод само. QR ведёт на публичную страницу состояния: клиент открывает её телефоном и видит, где его вещь, не звоня.",
              en: "The paper carries a barcode and a QR code. The barcode is read by a counter scanner: the scan box sits at the top of the forms list and catches input on its own. The QR leads to a public status page, so the client opens it on a phone and sees where the item is without calling.",
            },
          },
          {
            vid: "abzats",
            tekst: {
              ru: "Список бланков разложен по видам, и каждую категорию можно свернуть. Числа рядом с видами — серверные и не меняются от того, что вы сняли вид с показа: иначе, спрятав квитанции, вы потеряли бы и способ их вернуть.",
              en: "The forms list is grouped by kind, and every category can be collapsed. The numbers next to kinds come from the server and do not change when you hide a kind: otherwise, hiding receipts would also hide the way to bring them back.",
            },
          },
          { vid: "ekran", put: "/documents", podpis: { ru: "Открыть бланки", en: "Open forms" } },
        ],
      },
      {
        id: "zakazy",
        module: "orders",
        nazvanie: { ru: "Заказы", en: "Orders" },
        kratko: {
          ru: "Заказ покупателю и заказ поставщику: сборка сканом, отгрузка, приёмка.",
          en: "Customer and supplier orders: scan picking, shipping, receiving.",
        },
        kuski: [
          {
            vid: "abzats",
            tekst: {
              ru: "Заказ покупателя отвечает на вопрос «когда отдадим», заказ поставщику — «когда привезут». Виды не смешиваются, потому что вопросы у них разные.",
              en: "A customer order answers the question «when do we hand it over», a supplier order answers «when does it arrive». The two kinds are not mixed, because the questions differ.",
            },
          },
          {
            vid: "abzats",
            tekst: {
              ru: "Заказ заводится из заявки одним нажатием: товарные строки переезжают в него сами. Свои траты и услуги при этом НЕ переносятся — по заказу собирают коробки, и строка про упаковку показывала бы «собрано 0 из 1», пока её не отметят руками.",
              en: "An order is created from a deal in one click: the goods lines move into it by themselves. Your own costs and services are NOT carried over — an order is a picking list, and a packaging line would show «0 of 1 picked» until someone ticked it by hand.",
            },
          },
          {
            vid: "vazhno",
            tekst: {
              ru: "Бронь при этом не удваивается. Заказ ПЕРЕНИМАЕТ её у заявки: обещанное считается вычитанием, а не сложением двух списков.",
              en: "The reservation is not doubled. The order TAKES IT OVER from the deal: what is promised is computed by subtraction, not by adding two lists together.",
            },
          },
          {
            vid: "svyortka",
            zagolovok: { ru: "Сборка и проведение", en: "Picking and processing" },
            kuski: [
              {
                vid: "spisok",
                punkty: [
                  {
                    ru: "Собранное живёт отдельно от заказанного: расхождение «заказано пять, собрано четыре» видно построчно ДО отгрузки, а не на выдаче.",
                    en: "Picked is stored separately from ordered: a mismatch «five ordered, four picked» is visible line by line BEFORE shipping, not at handover.",
                  },
                  {
                    ru: "Собирают сканером: отсканировали код — строка отметилась. Код чужого товара даёт отказ, а не молчаливую отметку не той строки.",
                    en: "Picking is done with a scanner: scan a code and the line is ticked. A code from another product is refused, rather than silently ticking the wrong line.",
                  },
                  {
                    ru: "Набирать позиции и двигать склад — разные полномочия: сборщик набирает, отгружает старший.",
                    en: "Picking items and moving stock are different powers: a picker picks, a senior ships.",
                  },
                  {
                    ru: "Проведение откатывается обратными движениями. Прежние движения остаются на месте: склад — это история, а не текущее число.",
                    en: "Processing is rolled back with counter-movements. The original movements stay where they were: stock is a history, not a current number.",
                  },
                ],
              },
            ],
          },
          { vid: "ekran", put: "/orders", podpis: { ru: "Открыть заказы", en: "Open orders" } },
        ],
      },
      {
        id: "nakladnye",
        module: "waybills",
        nazvanie: { ru: "Накладные", en: "Waybills" },
        kratko: {
          ru: "Отгрузка и приёмка отдельной бумагой, а не движением заодно с заказом.",
          en: "Shipping and receiving as a separate paper, not as a side effect of an order.",
        },
        kuski: [
          {
            vid: "abzats",
            tekst: {
              ru: "Накладная нужна там, где товар передают из рук в руки и это надо подтвердить. Расходная — отгрузка со склада, приходная — приёмка на склад.",
              en: "A waybill is needed where goods change hands and that must be confirmed. An outgoing one ships from the warehouse, an incoming one receives into it.",
            },
          },
          {
            vid: "spisok",
            punkty: [
              {
                ru: "Накладная по заказу заводится одним нажатием: позиции переносятся, повторно набирать их не надо.",
                en: "A waybill for an order is created in one click: the items are carried over, no need to key them in again.",
              },
              {
                ru: "Проведение двигает остаток. До проведения это черновик, и правится только он.",
                en: "Processing moves the stock. Until then it is a draft, and only a draft can be edited.",
              },
              {
                ru: "Получатель подтверждает приёмку. Ошиблись — сторнируется черновиком, а не правкой проведённой бумаги.",
                en: "The recipient confirms receipt. If something is wrong it is reversed with a draft, not by editing a processed paper.",
              },
            ],
          },
          {
            vid: "vnimanie",
            tekst: {
              ru: "Товар, уже ушедший накладной, второй раз не отгрузится: проведение заказа откажет и назовёт номера накладных, которыми он ушёл.",
              en: "Goods already shipped by a waybill will not ship twice: processing the order refuses and names the waybills that carried them.",
            },
          },
          {
            vid: "svyortka",
            zagolovok: { ru: "Печать накладной", en: "Printing a waybill" },
            kuski: [
              {
                vid: "abzats",
                tekst: {
                  ru: "Накладную печатают на трёх языках — по получателю, а не по сотруднику. На листе перечень с единицами, столбец сумм, складывающийся в «Итого», реквизиты вашей фирмы и две подписи: отпустил и получил.",
                  en: "A waybill prints in three languages — chosen for the recipient, not the employee. The sheet carries the item list with units, an amount column that adds up to the total, your company details and two signatures: released and received.",
                },
              },
              {
                vid: "abzats",
                tekst: {
                  ru: "Длинная накладная печатается на нескольких листах: шапка таблицы повторяется на каждом, позиция не разрывается пополам, а номер стоит внизу каждого листа — он и связывает их между собой.",
                  en: "A long waybill prints across several sheets: the table header repeats on each, an item is never split in half, and the number sits at the bottom of every sheet — that is what ties them together.",
                },
              },
              {
                vid: "vnimanie",
                tekst: {
                  ru: "Печатается только проведённая. У черновика кнопки печати нет: перечень в нём ещё изменится, а подпись получателя под изменившимся листом не значит ничего.",
                  en: "Only a processed waybill prints. A draft has no print button: its list can still change, and a recipient signature under a changed sheet means nothing.",
                },
              },
              {
                vid: "abzats",
                tekst: {
                  ru: "Имя отпустившего берётся на момент проведения и остаётся на бумаге навсегда — даже если сотрудник потом уволился и удалён. Цены на листе видит только тот, кому они видны на экране; без этого права столбцы сумм не пустеют, а исчезают.",
                  en: "The name of the person who released the goods is captured at processing and stays on the paper forever — even after that employee leaves and is deleted. Prices print only for those allowed to see them on screen; without that permission the amount columns disappear rather than going blank.",
                },
              },
            ],
          },
          { vid: "ekran", put: "/waybills", podpis: { ru: "Открыть накладные", en: "Open waybills" } },
        ],
      },
      {
        id: "firmy",
        perm: "companies.view",
        module: "companies",
        nazvanie: { ru: "Свои юрлица", en: "Your companies" },
        kratko: {
          ru: "От чьего имени печатается бумага: реквизиты, счёт, подписант.",
          en: "Whose name is on the paper: details, bank account, signatory.",
        },
        kuski: [
          {
            vid: "abzats",
            tekst: {
              ru: "Блок нужен тем, у кого юрлиц больше одного или у кого в бумагах есть реквизиты. Бланк печатается с реквизитами выбранной фирмы; заявка помнит свою.",
              en: "The module is for those with more than one legal entity, or with details on their papers. A form is printed with the chosen company details; a deal remembers its own.",
            },
          },
          {
            vid: "vazhno",
            tekst: {
              ru: "Основная фирма ровно одна. Сделали основной другую — признак у прежней снимается сам, и промежуточного состояния «основных две» не возникает.",
              en: "There is exactly one default company. Make another one default and the flag is cleared from the previous one, with no intermediate state of «two defaults».",
            },
          },
          { vid: "ekran", put: "/companies", podpis: { ru: "Открыть фирмы", en: "Open companies" } },
        ],
      },
    ],
  },
  {
    id: "naruzhu",
    znachok: "boards",
    nazvanie: { ru: "Клиенту наружу", en: "Facing the client" },
    statyi: [
      {
        id: "doski",
        perm: "boards.view",
        module: "boards",
        nazvanie: { ru: "Доски работ и витрина", en: "Work boards and the showcase" },
        kratko: {
          ru: "Подборка работ по ссылке без регистрации: PIN, срок действия, счётчик просмотров.",
          en: "A set of works behind a link with no sign-up: PIN, expiry, view counter.",
        },
        kuski: [
          {
            vid: "abzats",
            tekst: {
              ru: "Доска — это подборка работ для одного клиента. Витрина — публичная страница, на которой он их видит: без входа в систему, по ссылке, которую вы ему дали.",
              en: "A board is a set of works for one client. The showcase is the public page where they see it: no sign-in, just the link you sent.",
            },
          },
          {
            vid: "spisok",
            punkty: [
              {
                ru: "У доски бывает НЕСКОЛЬКО ссылок сразу — разным людям, с разными PIN и сроками. Отозвали одну, остальные работают.",
                en: "A board can have SEVERAL links at once — for different people, with different PINs and expiry dates. Revoke one and the rest keep working.",
              },
              {
                ru: "PIN — четыре цифры, и подбор их закрыт ограничителем по адресу. Сам PIN показывается вам один раз, при установке.",
                en: "The PIN is four digits, and guessing is blocked by a per-address rate limit. The PIN itself is shown to you once, when you set it.",
              },
              {
                ru: "Просмотры считаются, а сырые адреса посетителей не хранятся: достаточно отличать уникальных.",
                en: "Views are counted, and raw visitor addresses are not stored: telling unique visitors apart is enough.",
              },
              {
                ru: "Исходники клиенту не отдаются вовсе. Сотрудник забирает их сам: одну работу файлом или всю доску архивом.",
                en: "Source files are never handed to the client. Staff take them out themselves: one work as a file or the whole board as an archive.",
              },
            ],
          },
          {
            vid: "vnimanie",
            tekst: {
              ru: "Выключение блока досок закрывает и уже разосланные ссылки. Это нарочно: выключенный блок исчезает целиком, а не только из меню.",
              en: "Disabling the boards module also closes links you have already sent. That is deliberate: a disabled module disappears entirely, not just from the menu.",
            },
          },
          {
            vid: "svyortka",
            zagolovok: { ru: "Оформление витрины", en: "How the showcase looks" },
            kuski: [
              {
                vid: "spisok",
                punkty: [
                  {
                    ru: "Логотип, название и цвет акцента берутся из настроек бренда — те же, что стоят в панели.",
                    en: "The logo, name and accent colour come from brand settings — the same ones you see in the sidebar.",
                  },
                  {
                    ru: "Кнопка в шапке уводит клиента на ваш сайт. Не указан адрес — кнопки нет вовсе.",
                    en: "A button in the header takes the client to your website. With no address there is no button at all.",
                  },
                  {
                    ru: "Язык публичных страниц задаётся отдельно от языка вашего интерфейса: клиент и сотрудник читают разное.",
                    en: "The language of public pages is set separately from your own interface language: the client and the employee read different things.",
                  },
                  {
                    ru: "Картинка для мессенджеров нужна, чтобы ссылка в чате разворачивалась превью, а не голым адресом.",
                    en: "A preview image makes the link unfold as a card in chats instead of showing a bare address.",
                  },
                ],
              },
            ],
          },
          { vid: "ekran", put: "/boards", podpis: { ru: "Открыть доски", en: "Open boards" } },
        ],
      },
      {
        id: "zayavki-s-sayta",
        perm: "settings.manage",
        nazvanie: { ru: "Заявки с сайта", en: "Website requests" },
        kratko: {
          ru: "Форма на вашем сайте заводит клиента и работу сама.",
          en: "A form on your website creates the client and the work by itself.",
        },
        kuski: [
          {
            vid: "abzats",
            tekst: {
              ru: "Приём заявок работает по ключу. Ключ создаётся в настройках и показывается ОДИН раз: дальше он живёт у того, кто пишет форму на сайте.",
              en: "Request intake works with a key. The key is created in settings and shown ONCE: afterwards it lives with whoever writes the form on your site.",
            },
          },
          {
            vid: "spisok",
            punkty: [
              {
                ru: "Пустой ключ — это и есть выключатель: приёма не существует, пока ключа нет.",
                en: "An empty key is the switch: intake does not exist while there is no key.",
              },
              {
                ru: "Там же выбирается ответственный — тот, на кого лягут пришедшие заявки.",
                en: "The same screen picks the owner — the person the incoming requests land on.",
              },
              {
                ru: "Карточка получает источник «сайт». Он лежит в общем справочнике источников, поэтому его же можно поставить руками: человек, посмотревший сайт и позвонивший, пришёл оттуда же, а форму не заполнял.",
                en: "The card gets the source «site». It sits in the common source list, so you can set it by hand too: someone who looked at the site and then called came from there, without filling the form.",
              },
              {
                ru: "Ловушка для ботов и ограничитель по адресу стоят на приёме: форма в интернете открыта всем.",
                en: "A bot trap and a per-address rate limit guard the intake: a form on the internet is open to everyone.",
              },
            ],
          },
          { vid: "ekran", put: "/settings/leads", podpis: { ru: "Настроить приём заявок", en: "Set up request intake" } },
        ],
      },
      {
        id: "api-sayta",
        perm: "settings.manage",
        nazvanie: { ru: "API для сайта магазина", en: "Shop-site API" },
        kratko: {
          ru: "Сайт, витрина или маркетплейс читают каталог и наличие, заводят заказы и клиентов — по ключу.",
          en: "A site, storefront or marketplace reads the catalogue and stock, creates orders and customers — with a key.",
        },
        kuski: [
          {
            vid: "abzats",
            tekst: {
              ru: "Ключ — пропуск для чужой программы, а не сотрудник и не роль. У него есть имя, области действия, срок, потолок запросов в минуту, потолок срока брони и режим наличия. Строка ключа показывается ОДИН раз при выдаче: в базе лежит только отпечаток, «показать ещё раз» не существует — потеряли, выпустите новый.",
              en: "A key is a pass for another program, not an employee or a role. It has a name, scopes, an expiry, a per-minute request limit, a reservation limit and a stock precision mode. The key string is shown ONCE when issued: only a fingerprint is stored, there is no «show again» — lose it, issue a new one.",
            },
          },
          {
            vid: "tablitsa",
            shapka: [
              { ru: "Область", en: "Scope" },
              { ru: "Что открывает", en: "What it opens" },
            ],
            ryady: [
              [{ ru: "Читать каталог", en: "Read the catalogue" }, { ru: "карточки: артикул, имя, единица, описание для сайта, цены, снимки", en: "cards: SKU, name, unit, site description, prices, photos" }],
              [{ ru: "Читать наличие", en: "Read stock" }, { ru: "остаток ОДНОГО склада типа «магазин», названного в ключе; точность — число, много/мало/нет или есть/нет", en: "the balance of ONE warehouse of kind «shop» named in the key; precision — a number, many/few/none, or in/out of stock" }],
              [{ ru: "Заводить заказы", en: "Create orders" }, { ru: "заказ со сроком брони: товар отложен на время, срок истекает сам; отмена — пока нет накладной", en: "an order with a reservation: stock is held for a while, the term expires by itself; cancel until a waybill exists" }],
              [{ ru: "Читать свои заказы", en: "Read own orders" }, { ru: "только заказы, заведённые этим же ключом", en: "only orders created by this very key" }],
              [{ ru: "Регистрировать клиентов", en: "Register customers" }, { ru: "завести карточку или узнать свою; известная карточка не переписывается", en: "create a card or recognise an existing one; a known card is never overwritten" }],
              [{ ru: "Присылать заявки", en: "Submit requests" }, { ru: "та же заявка, что с формы, только по ключу сайта", en: "the same request as from the form, only with the site key" }],
            ],
          },
          {
            vid: "spisok",
            punkty: [
              {
                ru: "Сайту виден остаток складов типа «магазин» и только их: подсобка, товар в пути и брак наружу не отдаются ни при каком ключе. Тип задаётся у склада в настройках; уход склада из «магазина» убирает с сайта весь его каталог, и экран спрашивает подтверждение с числом.",
                en: "The site sees the balance of «shop» warehouses and only them: the back room, goods in transit and defects are never exposed, whatever the key. The kind is set on the warehouse in settings; taking a warehouse out of «shop» removes its whole catalogue from the site, and the screen asks for confirmation with a count.",
              },
              {
                ru: "Карточка публикуется, когда по складу магазина было хоть одно движение. Распроданный товар остаётся на сайте с «нет в наличии»; товар без цены остаётся без кнопки «купить». Описание для сайта — отдельное поле карточки товара, заметка кладовщика наружу не уходит.",
                en: "A card is published once there has been at least one movement on the shop warehouse. A sold-out product stays on the site as «out of stock»; a product without a price stays without a «buy» button. The site description is a separate field on the product card; the storekeeper's note never leaves the CRM.",
              },
              {
                ru: "Сайт узнаёт об изменениях лентой: «что поменялось с такого-то момента» — сам догоняет после любого простоя, а система ничего не помнит и не повторяет.",
                en: "The site learns about changes from a feed: «what changed since a moment» — it catches up by itself after any downtime while the system remembers and repeats nothing.",
              },
              {
                ru: "Заказ с сайта — обычный заказ покупателя с пометкой «бронь до». Истёкшая бронь товар не держит, а заказ остаётся открытым: в списке заказов есть отбор «бронь истекла» — это очередь на разбор.",
                en: "An order from the site is an ordinary customer order marked «reserved until». An expired reservation no longer holds the stock while the order stays open: the order list has a filter «reservation expired» — that is the queue to sort out.",
              },
              {
                ru: "Ключи выдаёт и отзывает тот же экран или консоль сервера (`./opencrm.sh apikey`). Отзыв действует сразу; перевыпуск оставляет старый ключ живым на сутки, чтобы сайт успели переключить.",
                en: "Keys are issued and revoked on the same screen or from the server console (`./opencrm.sh apikey`). A revoke takes effect at once; a rotation keeps the old key alive for a day so the site can be switched over.",
              },
            ],
          },
          {
            vid: "ruchka",
            metod: "GET",
            put: "/api/v1/site/stock",
            opisanie: {
              ru: "Наличие по складу ключа в режиме ключа. Заголовок X-OpenCRM-Api-Key обязателен; запрос шлёт сервер сайта, а не страница в браузере.",
              en: "Stock of the key's warehouse in the key's precision. The X-OpenCRM-Api-Key header is required; the request is sent by the site's server, not by a page in the browser.",
            },
            polya: [
              { imya: "id", tip: "string", obyazatelno: false, opisanie: { ru: "Номера товаров через запятую, до 200.", en: "Product ids, comma-separated, up to 200." } },
              { imya: "sku", tip: "string", obyazatelno: false, opisanie: { ru: "Артикулы через запятую — вместо номеров или вместе с ними.", en: "SKUs, comma-separated — instead of ids or together with them." } },
            ],
            zapros: `curl -s "https://ваш-адрес/api/v1/site/stock?sku=ABC-1,ABC-2" \\
  -H "X-OpenCRM-Api-Key: …"`,
            otvet: `{
  "as_of": "2026-09-04T14:32:11Z",
  "ttl_sec": 60,
  "recheck_after": "2026-09-04T14:47:00Z",
  "items": [
    { "id": 17, "sku": "ABC-1", "unit": "pcs", "state": "many", "available_milli": 12000 },
    { "id": 42, "sku": "ABC-2", "unit": "pcs", "state": "none", "available_milli": 0 }
  ]
}`,
          },
          {
            vid: "vazhno",
            tekst: {
              ru: "Полный справочник ручек, кодов отказов и порядок первичной настройки — docs/04-api.md, раздел «API сайта магазина»; доводы устройства — docs/16-api-sayta.md.",
              en: "The full reference of endpoints, error codes and the first-time setup order is in docs/04-api.md, section «Shop-site API»; the reasoning behind the design is in docs/16-api-sayta.md.",
            },
          },
          { vid: "ekran", put: "/settings/api-keys", podpis: { ru: "Открыть ключи API сайта", en: "Open site API keys" } },
        ],
      },
    ],
  },
  {
    id: "kanaly",
    znachok: "mail",
    nazvanie: { ru: "Каналы связи", en: "Channels" },
    statyi: [
      {
        id: "pochta",
        perm: "mail.view",
        module: "mail",
        nazvanie: { ru: "Почта", en: "Mail" },
        kratko: {
          ru: "Ящик фирмы: письма привязываются к клиенту и ложатся в общую ленту.",
          en: "The company mailbox: letters attach to a client and land in the shared feed.",
        },
        kuski: [
          {
            vid: "abzats",
            tekst: {
              ru: "Ящик подключается по IMAP и SMTP. Входящее письмо привязывается к клиенту по адресу отправителя — и попадает в ленту карточки рядом со звонками, встречами и заметками.",
              en: "The mailbox connects over IMAP and SMTP. An incoming letter is matched to a client by the sender address and lands in the card feed next to calls, meetings and notes.",
            },
          },
          {
            vid: "vazhno",
            tekst: {
              ru: "К ЗАЯВКЕ письмо само не привязывается: у клиента их бывает несколько сразу, и «взять последнюю открытую» — это угадывание. Клиент — факт, заявка — решение человека.",
              en: "A letter does not attach itself to a DEAL: a client often has several at once, and «take the latest open one» is guesswork. The client is a fact, the deal is a human decision.",
            },
          },
          {
            vid: "spisok",
            punkty: [
              {
                ru: "Письмо, доставленное сразу на два ящика фирмы, сохраняется дважды — по разу на ящик, и это нарочно.",
                en: "A letter delivered to two company mailboxes is stored twice — once per mailbox, and that is deliberate.",
              },
              {
                ru: "Повторная синхронизация не задваивает ни письмо, ни строку ленты.",
                en: "Re-syncing duplicates neither the letter nor the feed entry.",
              },
              {
                ru: "Письма не правятся и не удаляются: почта — зеркало сервера, а правка задним числом была бы способом подделать переписку.",
                en: "Letters are neither edited nor deleted: mail mirrors the server, and editing after the fact would be a way to forge correspondence.",
              },
            ],
          },
          { vid: "ekran", put: "/mail", podpis: { ru: "Открыть почту", en: "Open mail" } },
        ],
      },
      {
        id: "telefoniya",
        perm: "settings.manage",
        module: "telephony",
        nazvanie: { ru: "Звонки", en: "Calls" },
        kratko: {
          ru: "Журнал звонков от АТС, звонок из карточки и задача «перезвонить».",
          en: "A call log from your PBX, calling from a card and a «call back» task.",
        },
        kuski: [
          {
            vid: "abzats",
            tekst: {
              ru: "АТС присылает события о звонках вебхуком. Подпись проверяется, повтор того же события не задваивает запись — линия связи с чужой системой обязана быть устойчивой к обоим.",
              en: "The PBX sends call events over a webhook. The signature is verified and a repeated event does not duplicate the record — a link to someone else's system must survive both.",
            },
          },
          {
            vid: "spisok",
            punkty: [
              {
                ru: "Звонок ищет карточку по нормализованному номеру: записанный как +7 (900) 000-00-00 и как 89000000000 — один и тот же человек.",
                en: "A call finds the card by a normalised number: written as +7 (900) 000-00-00 or as 89000000000, it is the same person.",
              },
              {
                ru: "Из карточки звонят одним нажатием, а после разговора заводят напоминание «перезвонить».",
                en: "You call from a card in one click, and after the conversation you create a «call back» reminder.",
              },
              {
                ru: "Журнал только дописывается: звонок был, и убрать его из истории значит убрать факт.",
                en: "The log is append-only: the call happened, and removing it from the history would remove the fact.",
              },
            ],
          },
          { vid: "ekran", put: "/settings/telephony", podpis: { ru: "Подключить АТС", en: "Connect a PBX" } },
        ],
      },
      {
        id: "telegram",
        perm: "telegram.view",
        module: "telegram",
        nazvanie: { ru: "Телеграм", en: "Telegram" },
        kratko: {
          ru: "Переписка с клиентами через бота фирмы, прямо в системе.",
          en: "Chatting with clients through the company bot, right inside the system.",
        },
        kuski: [
          {
            vid: "abzats",
            tekst: {
              ru: "Слева список диалогов, справа лента переписки с вложениями. Отвечают отсюда же; шаблоны подставляются прямо в поле ввода.",
              en: "Chats on the left, the conversation with attachments on the right. You answer from here, and templates drop straight into the input box.",
            },
          },
          {
            vid: "spisok",
            punkty: [
              {
                ru: "Диалог привязывается к карточке руками или по точному номеру. Из переписки заводятся карточка, заявка и напоминание.",
                en: "A chat is attached to a card by hand or by an exact phone number. From the conversation you can create a card, a deal and a reminder.",
              },
              {
                ru: "Когда в диалог заходит второй сотрудник, оба видят баннер с именами — чтобы не отвечать клиенту вдвоём.",
                en: "When a second employee opens the chat, both see a banner with names — so the client does not get two answers.",
              },
              {
                ru: "Диалоги отбираются по метке источника: наклейка, визитка и письмо дают разные метки, и видно, откуда пришли.",
                en: "Chats can be filtered by source label: a sticker, a business card and a letter carry different labels, so you see where people came from.",
              },
              {
                ru: "Срок хранения переписки называете вы. Старое убирает таймер, а не человек.",
                en: "You set how long conversations are kept. Old ones are removed by a timer, not by a person.",
              },
            ],
          },
          {
            vid: "svyortka",
            zagolovok: { ru: "Приглашение и QR", en: "Invitation and QR" },
            kuski: [
              {
                vid: "abzats",
                tekst: {
                  ru: "В настройках бота собирается ссылка-приглашение и QR к ней. Рядом задаётся метка: наберите «наклейка» — и QR, распечатанный на коробке, будет отличим от того, что стоит на квитанции.",
                  en: "Bot settings build an invitation link and its QR code. Next to it you type a label: enter «sticker» and the QR printed on a box becomes distinguishable from the one on a receipt.",
                },
              },
              {
                vid: "abzats",
                tekst: {
                  ru: "Тем же ботом каждое утро уходит сводка по системе, и её НЕПРИХОД сам по себе тревога: тишину нельзя отличить от «всё хорошо», пока о ней не сообщают вслух.",
                  en: "The same bot sends a morning digest, and its ABSENCE is an alert in itself: silence is indistinguishable from «all good» until someone says so out loud.",
                },
              },
            ],
          },
          { vid: "ekran", put: "/telegram", podpis: { ru: "Открыть переписку", en: "Open chats" } },
        ],
      },
    ],
  },
  {
    id: "dengi",
    znachok: "analytics",
    nazvanie: { ru: "Деньги и отчёты", en: "Money and reports" },
    statyi: [
      {
        id: "finansy",
        perm: "finance.view",
        module: "finance",
        nazvanie: { ru: "Деньги", en: "Money" },
        kratko: {
          ru: "Статьи прихода и расхода, планы на период, прибыль за отрезок.",
          en: "Income and expense categories, budgets for a period, profit over a range.",
        },
        kuski: [
          {
            vid: "abzats",
            tekst: {
              ru: "Операция — это факт: пришли или ушли деньги. У неё есть статья, сумма и дата события, а не дата занесения.",
              en: "An operation is a fact: money came in or went out. It has a category, an amount and the date it happened — not the date it was typed in.",
            },
          },
          {
            vid: "vnimanie",
            tekst: {
              ru: "Операция НЕИЗМЕНЯЕМА. Ошиблись — заводится обратная, а не правится прежняя. Иначе прошлый отчёт менялся бы от сегодняшней правки, и сверить его было бы не с чем.",
              en: "An operation is IMMUTABLE. If you got it wrong, add a reversing one instead of editing the old one. Otherwise last month's report would change from today's edit, and there would be nothing to reconcile against.",
            },
          },
          {
            vid: "spisok",
            punkty: [
              {
                ru: "Правила начисления записывают то, что повторяется: с каждого заказа — столько-то в такую-то статью.",
                en: "Accrual rules record what repeats: so much from every order into a given category.",
              },
              {
                ru: "План на период показывает, сколько собирались потратить и сколько потратили на самом деле.",
                en: "A budget for a period shows what you meant to spend and what you actually spent.",
              },
              {
                ru: "Оплаты привязываются к бланку: видно, сколько получено и сколько осталось.",
                en: "Payments attach to a form: you see how much came in and how much is left.",
              },
            ],
          },
          { vid: "ekran", put: "/finance", podpis: { ru: "Открыть деньги", en: "Open money" } },
        ],
      },
      {
        id: "otchyoty",
        perm: "reports.view",
        module: "reports",
        nazvanie: { ru: "Отчёты", en: "Reports" },
        kratko: {
          ru: "Воронка с конверсией, выручка по месяцам, источники клиентов.",
          en: "A funnel with conversion, revenue by month, client sources.",
        },
        kuski: [
          {
            vid: "abzats",
            tekst: {
              ru: "Три отчёта отвечают на три разных вопроса: где встают заявки, сколько денег пришло и откуда приходят клиенты. Каждый выгружается в CSV тем же отбором, что виден на экране.",
              en: "Three reports answer three different questions: where deals get stuck, how much money came in, and where clients come from. Each exports to CSV with the same filter you see on screen.",
            },
          },
          {
            vid: "vazhno",
            tekst: {
              ru: "Выручка — это ПОЛУЧЕННЫЕ деньги. Сумма выигранных заявок стоит рядом отдельным числом и так и называется: это разные вещи, и путать их дорого.",
              en: "Revenue means money RECEIVED. The total of won deals stands next to it as a separate number under its own name: these are different things, and confusing them is expensive.",
            },
          },
          {
            vid: "abzats",
            tekst: {
              ru: "Воронка считается по журналу перемещений, а не по текущему этапу: текущий отвечает «где всё лежит сейчас», а воронка — «сколько прошло через каждый шаг».",
              en: "The funnel is computed from the stage-change log, not from the current stage: the current one answers «where everything sits now», while the funnel answers «how many went through each step».",
            },
          },
          { vid: "ekran", put: "/reports", podpis: { ru: "Открыть отчёты", en: "Open reports" } },
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
        perm: "roles.view",
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
        perm: "settings.manage",
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
      {
        id: "sotrudniki",
        perm: "staff.view",
        nazvanie: { ru: "Сотрудники", en: "Staff" },
        kratko: {
          ru: "Кого пускаем внутрь, кого отключаем и почему уволить последнего управляющего нельзя.",
          en: "Who gets in, who gets switched off, and why the last permissions manager cannot be removed.",
        },
        kuski: [
          {
            vid: "abzats",
            tekst: {
              ru: "Сотрудник заводится с почтой и должностью. Права выдаются ДОЛЖНОСТИ, а не человеку: иначе через год никто не ответит, почему у Петрова есть то, чего нет у Иванова на той же работе.",
              en: "An employee is created with an email and a role. Permissions belong to the ROLE, not to the person: otherwise, a year later nobody can say why Peter has something Ivan on the same job does not.",
            },
          },
          {
            vid: "spisok",
            punkty: [
              {
                ru: "Отключённый сотрудник остаётся в системе и в истории, но войти не может: его сессии снимаются сразу.",
                en: "A disabled employee stays in the system and in the history but cannot sign in: their sessions are dropped at once.",
              },
              {
                ru: "Удаление безвозвратно. Авторство при этом сохраняется: заметки, файлы и движения склада остаются, а имя в них — снимком.",
                en: "Deletion is permanent. Authorship survives: notes, files and stock movements stay, with the name kept as a snapshot.",
              },
              {
                ru: "Себе роль не меняют и себя не удаляют. Это не вежливость: иначе тот, кто раздаёт права, снял бы с себя любое ограничение.",
                en: "You cannot change your own role or delete yourself. That is not politeness: otherwise whoever grants permissions could lift any limit from themselves.",
              },
            ],
          },
          {
            vid: "vnimanie",
            tekst: {
              ru: "Последнего, кто умеет раздавать права, снять нельзя ничем: ни правкой должности, ни переводом, ни отключением, ни увольнением. Сначала дайте право второму человеку. Владелец (root) — исключение: он действует, значит доступ у него есть прямо сейчас.",
              en: "The last person able to grant permissions cannot be removed by any means: not by editing the role, not by reassigning, disabling or deleting. Give the permission to a second person first. The owner (root) is the exception: they are acting, so their access exists right now.",
            },
          },
          { vid: "ekran", put: "/staff", podpis: { ru: "Открыть сотрудников", en: "Open staff" } },
        ],
      },
      {
        id: "nastroyki-sayta",
        perm: "settings.manage",
        nazvanie: { ru: "Настройки сайта", en: "Site settings" },
        kratko: {
          ru: "Разложены по категориям: фирма, страницы для клиента, склад, каналы, деньги, система.",
          en: "Grouped into categories: company, client-facing pages, stock, channels, money, system.",
        },
        kuski: [
          {
            vid: "abzats",
            tekst: {
              ru: "Разделов настроек четырнадцать, и они разложены по категориям. Категория из двух и более разделов сворачивается, из одного — показывается самим разделом: заголовок, за которым лежит ровно один пункт, обещает выбор, которого нет.",
              en: "There are fourteen settings sections, grouped into categories. A category with two or more sections collapses; one with a single section shows that section directly: a heading hiding exactly one item promises a choice that is not there.",
            },
          },
          {
            vid: "abzats",
            tekst: {
              ru: "Раскладка меняется сама вслед за блоками. Выключили склад — категория «Склад и товар» исчезает целиком, вместе с адресами: спрятанный пункт при живом адресе это не защита, а её видимость.",
              en: "The layout follows the modules by itself. Switch stock off and the «Stock and goods» category disappears entirely, addresses included: a hidden item with a live address is not protection, only the look of it.",
            },
          },
          {
            vid: "svyortka",
            zagolovok: { ru: "Что в какой категории", en: "What lives where" },
            kuski: [
              {
                vid: "tablitsa",
                shapka: [
                  { ru: "Категория", en: "Category" },
                  { ru: "Что внутри", en: "What is inside" },
                ],
                ryady: [
                  [
                    { ru: "Блоки системы", en: "Modules" },
                    { ru: "Стоят первыми и вне категорий: они решают, какие категории вообще существуют. Единственные применяются сразу, а не по кнопке «Сохранить».", en: "They come first and outside the categories: they decide which categories exist at all. The only ones applied instantly rather than by a Save button." },
                  ],
                  [
                    { ru: "Доступ", en: "Access" },
                    { ru: "Должности и права.", en: "Roles and permissions." },
                  ],
                  [
                    { ru: "Фирма", en: "Company" },
                    { ru: "Бренд (логотип, название, цвет, язык публичных страниц) и контакты.", en: "Brand (logo, name, colour, language of public pages) and contacts." },
                  ],
                  [
                    { ru: "Страницы для клиента", en: "Client-facing pages" },
                    { ru: "Оформление витрины и ссылка на ваш сайт.", en: "Showcase appearance and the link to your website." },
                  ],
                  [
                    { ru: "Склад и товар", en: "Stock and goods" },
                    { ru: "Склады как места и настройки наклейки.", en: "Warehouses as places and label settings." },
                  ],
                  [
                    { ru: "Как с вами связываются", en: "Ways to reach you" },
                    { ru: "Почтовые ящики, АТС, бот телеграма, приём заявок с сайта.", en: "Mailboxes, the PBX, the Telegram bot, website request intake." },
                  ],
                  [
                    { ru: "Деньги", en: "Money" },
                    { ru: "Статьи прихода и расхода, планы.", en: "Income and expense categories, budgets." },
                  ],
                  [
                    { ru: "Система", en: "System" },
                    { ru: "Режим обслуживания.", en: "Maintenance mode." },
                  ],
                ],
              },
            ],
          },
          { vid: "ekran", put: "/settings/brand", podpis: { ru: "Открыть настройки", en: "Open settings" } },
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
            vid: "vazhno",
            tekst: {
              ru: "Здесь — соглашения и один разобранный пример. Все ручки с адресами и правами собраны в статье «Все ручки» ниже: она порождена из справочника docs/04-api.md, полноту которого стережёт проверка, поэтому расходиться этим двум спискам не с чем.",
              en: "This is conventions plus one worked example. Every endpoint with its address and permission is collected in the «All endpoints» article below: it is generated from the docs/04-api.md reference, whose completeness a test guards, so the two lists have nothing to diverge over.",
            },
          },
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
              ru: "Публичное API для сайта магазина живёт под тем же адресом, но входит по ключу, а не по сессии: как его завести и что оно умеет — в статье «API для сайта магазина» раздела «Наружу». Форма ответов и ошибок у него та же.",
              en: "The public API for a shop website lives under the same address but signs in with a key, not a session: how to set it up and what it can do is in the «Shop-site API» article of the «Facing the client» section. Its response and error shapes are the same.",
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
      {
        id: "vse-ruchki",
        nazvanie: { ru: "Все ручки", en: "All endpoints" },
        kratko: {
          ru: "Каждый адрес системы одним списком: по разделам, с правом и поиском.",
          en: "Every address in the system in one list: by section, with its permission and a search box.",
        },
        kuski: [
          {
            vid: "abzats",
            tekst: {
              ru: "Список порождён из справочника docs/04-api.md и обновляется вместе с ним; ручка, не названная в справочнике, не пройдёт проверку, поэтому здесь есть всё. Описания идут на языке справочника. Поиск смотрит в адрес, описание, право и название раздела.",
              en: "The list is generated from the docs/04-api.md reference and updates together with it; an endpoint missing from the reference fails a test, so everything is here. Descriptions are in the language of the reference. The search looks at the address, the description, the permission and the section name.",
            },
          },
          {
            vid: "vazhno",
            tekst: {
              ru: "Право у ручки — то же, что в матрице доступов: «открыто» отвечает без входа, «любой сотрудник» — по сессии, код вида «раздел.действие» — по праву из должности, «ключ сайта» — по ключу с областью.",
              en: "The permission next to an endpoint is the same as in the access matrix: «public» answers without signing in, «any staff» needs a session, a «section.action» code needs the permission from a job role, «site key» needs a key with that scope.",
            },
          },
          { vid: "spravochnik" },
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
        perm: "settings.manage",
        nazvanie: { ru: "Копии", en: "Backups" },
        kratko: {
          ru: "По расписанию — на сервере; с экрана — зашифрованный файл себе.",
          en: "On schedule — on the server; from the screen — an encrypted file for yourself.",
        },
        kuski: [
          {
            vid: "abzats",
            tekst: {
              ru: "Ежедневная копия снимается на сервере по расписанию и проверяется на годность: годной считается только копия с меткой конца — оборванный дамп выглядит как обычный файл и подводит ровно тогда, когда из него надо восстановиться. Лежит она на том же диске, что и база, поэтому её надо забирать себе.",
              en: "A daily copy is taken on the server on schedule and checked for soundness: only a copy with an end marker counts — a truncated dump looks like an ordinary file and fails exactly when you need to restore from it. It sits on the same disk as the database, so take it away with you.",
            },
          },
          {
            vid: "abzats",
            tekst: {
              ru: "Для этого есть экран «Копии»: база и файлы снимаются отдельными зашифрованными файлами, которые вы скачиваете руками. База — минуты, берётся часто; файлы (фотографии, вложения, оформление) — тяжелее и берутся реже. База без файлов — это карточки товаров без фотографий и переписка без вложений.",
              en: "That is what the Copies screen is for: the database and the files are taken as separate encrypted files that you download by hand. The database takes minutes and is worth taking often; the files (photos, attachments, branding) are heavier and taken less often. A database without the files is product cards without photos and conversations without attachments.",
            },
          },
          {
            vid: "shagi",
            punkty: [
              {
                ru: "Заведите ключ копий. Он показывается ОДИН раз и подтверждается вводом последних знаков — так система убеждается, что вы его сохранили.",
                en: "Create the copy key. It is shown ONCE and confirmed by typing its last characters — that is how the system makes sure you saved it.",
              },
              {
                ru: "Снимите копию базы, затем файлов. Каждая сразу открывается нынешним ключом — итог проверки виден в списке.",
                en: "Take a copy of the database, then of the files. Each is opened with the current key right away — the check result shows in the list.",
              },
              {
                ru: "Скачайте и уберите файлы в надёжное место. Готовая копия лежит на сервере сутки, потом убирается.",
                en: "Download the files and keep them somewhere safe. A finished copy stays on the server for a day, then it is removed.",
              },
            ],
          },
          {
            vid: "vnimanie",
            tekst: {
              ru: "Потерянный ключ — потерянная копия: подбирать нечего и восстановить неоткуда. Храните ключ не на этом сервере. Кнопка «Проверить ключ» у готовой копии повторяет сверку — заменённый или потерянный ключ обнаруживается на экране, а не в день аварии.",
              en: "A lost key is a lost copy: there is nothing to guess and nowhere to recover it from. Keep the key off this server. The «Check the key» button on a finished copy repeats the check — a replaced or lost key shows up on screen, not on the day of the disaster.",
            },
          },
          {
            vid: "svyortka",
            zagolovok: { ru: "Восстановление из копии", en: "Restoring from a copy" },
            kuski: [
              {
                vid: "abzats",
                tekst: {
                  ru: "Самая опасная кнопка в системе, и потому под отдельным правом «Восстановление из копии», а не под общими настройками. Она закрывает случай «испортили данные», а не «сгорела машина»: кнопкой на упавшем сервере упавший сервер не поднимают.",
                  en: "The most dangerous button in the system, hence its own right «Restore from a copy» rather than the general settings right. It covers «we corrupted the data», not «the machine burned down»: a button on a dead server does not bring the server back.",
                },
              },
              {
                vid: "shagi",
                punkty: [
                  { ru: "Копия расшифровывается нынешним ключом; чужой ключ, не копия или обрывок отвергаются до того, как тронута база.", en: "The copy is decrypted with the current key; a foreign key, a non-copy or a truncated file are refused before the database is touched." },
                  { ru: "Копия от кода, которого здесь нет, отвергается — догнать её нечем.", en: "A copy taken by code this server does not have is refused — there is nothing to bring it up to date with." },
                  { ru: "Сайт закрывается на обслуживание, снимок живой базы остаётся рядом с копиями на неделю.", en: "The site closes for maintenance; a snapshot of the live database stays next to the copies for a week." },
                  { ru: "Копия заливается, старая версия догоняется миграциями, схема сверяется, обслуживание снимается, событие ложится в журнал.", en: "The copy is loaded, an older version is brought up to date by migrations, the schema is checked, maintenance is lifted and the event is logged." },
                  { ru: "Файлы из копии ложатся поверх нынешних, ничего не стирая. После восстановления базы придётся войти заново.", en: "Files from a copy are added on top of the current ones; nothing is deleted. After a database restore you will need to sign in again." },
                ],
              },
            ],
          },
          { vid: "ekran", put: "/settings/backups", podpis: { ru: "Открыть копии", en: "Open copies" } },
        ],
      },
      {
        id: "zhivye-obnovleniya",
        perm: "settings.manage",
        nazvanie: { ru: "Живые обновления", en: "Live updates" },
        kratko: {
          ru: "Правка коллеги появляется на открытом экране сама.",
          en: "A colleague's change shows up on an open screen by itself.",
        },
        kuski: [
          {
            vid: "abzats",
            tekst: {
              ru: "Перенесли карточку на доске — она поехала у всех; списали товар — остаток пересчитался у всех; завели клиента — он появился в списке. Без кнопки «обновить». Вкладка держит одно соединение с сервером, сервер шлёт намёк «перечитай вот это», и экран перечитывает данные обычным запросом — значит увидеть больше, чем разрешают права, нельзя по устройству.",
              en: "Move a card on the board and it moves for everyone; write stock off and the balance updates for everyone; create a client and they appear in the list. No refresh button. A tab keeps one connection to the server, the server sends a hint «re-read this», and the screen re-reads the data with an ordinary request — so seeing more than your rights allow is impossible by construction.",
            },
          },
          {
            vid: "spisok",
            punkty: [
              {
                ru: "Начатую правку живое обновление не затирает: если вы печатаете в карточке, а коллега её изменил, появится полоса «данные изменились — показать».",
                en: "A live update never overwrites an edit in progress: if you are typing in a card and a colleague changed it, a bar «this record was changed — show» appears.",
              },
              {
                ru: "Свёрнутая вкладка молчит и перечитывает один раз при возвращении.",
                en: "A background tab stays quiet and re-reads once when you come back.",
              },
              {
                ru: "Пропала связь с шиной — наверху появляется полоса «обновления приостановлены», экраны перечитываются сами, но реже; поднялась — полоса уходит сама.",
                en: "If the bus goes down, a bar «live updates are paused» appears at the top; screens refresh on their own, less often; when it is back, the bar leaves by itself.",
              },
              {
                ru: "Выключатель — в настройках обслуживания. Выключенные обновления — это выбор, а не авария: полосы нет, CRM работает как раньше.",
                en: "The switch is in the maintenance settings. Updates switched off are a choice, not a failure: no bar, the CRM works as before.",
              },
            ],
          },
          { vid: "ekran", put: "/settings/maintenance", podpis: { ru: "Открыть обслуживание", en: "Open maintenance" } },
        ],
      },
      {
        id: "nablyudenie",
        module: "monitoring",
        perm: "monitoring.view",
        nazvanie: { ru: "Наблюдение за сервером", en: "Server monitoring" },
        kratko: {
          ru: "Состояние стека, горящие тревоги и кнопки «принято» и «заглушить» прямо в телеграме.",
          en: "Stack health, firing alerts and «ack» and «silence» buttons right in Telegram.",
        },
        kuski: [
          {
            vid: "abzats",
            tekst: {
              ru: "Раздел показывает, что происходит с самим сервером: живы ли сборщики метрик, отвечает ли база, что горит прямо сейчас. Он только показывает — заводить и править там нечего, всё приходит снаружи.",
              en: "The section shows what is happening to the server itself: are the metric collectors alive, does the database answer, what is firing right now. It only shows — there is nothing to create or edit, everything arrives from outside.",
            },
          },
          {
            vid: "abzats",
            tekst: {
              ru: "Тревога приходит в телеграм с кнопками «принято» и «заглушить на час». Кнопки здесь не украшение: без них ночная тревога либо будит всех подряд, либо глушится навсегда и забывается.",
              en: "An alert arrives in Telegram with «ack» and «silence for an hour» buttons. They are not decoration: without them a night alert either wakes everyone or gets silenced forever and forgotten.",
            },
          },
          {
            vid: "vnimanie",
            tekst: {
              ru: "Это наблюдение ИЗНУТРИ. Упавшая машина о себе не сообщит — закрывает это только наблюдатель со стороны, с чужого адреса. Заведите его отдельно.",
              en: "This is monitoring from the INSIDE. A machine that is down will not report itself — only an outside watcher on someone else's address covers that. Set one up separately.",
            },
          },
          { vid: "ekran", put: "/server", podpis: { ru: "Открыть наблюдение", en: "Open monitoring" } },
        ],
      },
      {
        id: "zhurnal",
        perm: "audit.view",
        nazvanie: { ru: "Журнал действий", en: "Activity log" },
        kratko: {
          ru: "Кто что менял: деньги, этапы, удаления, доступы и блоки.",
          en: "Who changed what: money, stages, deletions, access and modules.",
        },
        kuski: [
          {
            vid: "abzats",
            tekst: {
              ru: "Журнал отвечает на вопрос «кто это сделал» и больше ни на какой. В нём записаны действия, из-за которых потом спорят: правка денег, смена этапа, удаление, выдача и снятие доступа, переключение блоков.",
              en: "The log answers the question «who did this» and nothing else. It records the actions people later argue about: money edits, stage changes, deletions, granting and revoking access, module toggles.",
            },
          },
          {
            vid: "vazhno",
            tekst: {
              ru: "Журнал только дописывается. Переписать его нельзя ни правом, ни кнопкой — это единственный раздел, где ничего нельзя изменить, и в этом всё его значение.",
              en: "The log is append-only. It cannot be rewritten by any permission or button — it is the only section where nothing can be changed, and that is the whole point of it.",
            },
          },
          {
            vid: "abzats",
            tekst: {
              ru: "Имя исполнителя хранится СНИМКОМ. Сотрудника уволили и удалили — запись остаётся с его именем, а не с прочерком: иначе журнал терял бы ответ ровно в тот момент, когда за ним приходят.",
              en: "The actor name is stored as a SNAPSHOT. If the employee is deleted, the entry keeps their name instead of a dash: otherwise the log would lose the answer exactly when someone comes looking for it.",
            },
          },
          { vid: "ekran", put: "/audit", podpis: { ru: "Открыть журнал", en: "Open the log" } },
        ],
      },
      {
        id: "obsluzhivanie-rezhim",
        perm: "settings.manage",
        nazvanie: { ru: "Режим обслуживания", en: "Maintenance mode" },
        kratko: {
          ru: "Заглушка для посетителей, пока вы работаете внутри.",
          en: "A holding page for visitors while you work inside.",
        },
        kuski: [
          {
            vid: "abzats",
            tekst: {
              ru: "Включённый режим показывает посетителям заглушку с вашей запиской, а владелец продолжает работать. Нужен он там, где правят данные и не хотят, чтобы в этот момент кто-то оформлял заказ.",
              en: "With the mode on, visitors see a holding page with your note while the owner keeps working. It is for the times you are fixing data and do not want someone placing an order right then.",
            },
          },
          {
            vid: "abzats",
            tekst: {
              ru: "Та же заглушка показывается сама во время обновления. На ней живёт змейка с таблицей рекордов — не шутки ради: страница без единого признака жизни читается как «сайт умер», и звонить начинают через минуту.",
              en: "The same page appears on its own during an update. It carries a snake game with a leaderboard — not for fun: a page with no sign of life reads as «the site is dead», and the calls start within a minute.",
            },
          },
          { vid: "ekran", put: "/settings/maintenance", podpis: { ru: "Открыть обслуживание", en: "Open maintenance" } },
        ],
      },
    ],
  },
];
