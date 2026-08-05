# 03 — База данных

## Принципы

- **SQLite сейчас, MySQL потом.** Поэтому: никаких диалект-специфичных фич (JSON-функций SQLite, `AUTOINCREMENT`-трюков, частичных индексов). Только то, что SQLAlchemy переносит без правок.
- Все таблицы — через SQLAlchemy-модели в `database/models/`, изменения схемы — только через миграции Alembic.
- Первичные ключи — целочисленные автоинкременты; внешние публичные идентификаторы (токены ссылок, идентификаторы файлов) — отдельные случайные строки, чтобы не светить порядковые номера наружу.
- Время — всегда UTC, колонки `*_at` типа `DateTime`.
- Клиенты удаляются мягко (`deleted_at`) и восстанавливаются root'ом; их файлы чистятся отложенно. Доски восстановления не имеют, поэтому удаляются сразу вместе с файлами работ (мягкое удаление лишь занимало бы место).

## Схема

```mermaid
erDiagram
    users ||--o{ clients : "managed_by"
    users ||--o{ boards : "created_by"
    clients ||--o{ client_notes : ""
    clients ||--o{ client_files : ""
    clients ||--o{ boards : "optional"
    boards ||--o{ works : ""
    boards ||--o{ share_links : ""
    share_links ||--o{ share_views : ""
    users ||--o{ client_notes : "author"

    users {
        int id PK
        string email UK
        string password_hash
        string name
        string role "root | manager"
        string status "pending | active | disabled"
        string locale "en | ru"
        bool must_change_password
        string avatar_path "'' | /avatars/<uuid>.webp"
        datetime last_seen_at "присутствие, переживает logout"
        datetime created_at
        datetime approved_at
    }

    clients {
        int id PK
        string name
        string company
        string phone
        string email
        string messenger "telegram/whatsapp @..."
        string tags "comma-separated"
        int manager_id FK
        datetime created_at
        datetime updated_at
        datetime deleted_at
    }

    client_notes {
        int id PK
        int client_id FK
        int author_id FK
        string kind "note | call | meeting | email"
        text body
        datetime happened_at
        datetime created_at
    }

    client_files {
        int id PK
        int client_id FK
        int uploaded_by FK
        string file_uid UK "случайный id в имени на диске"
        string original_name
        string mime
        int size_bytes
        datetime created_at
    }

    boards {
        int id PK
        string title
        text description
        int cover_work_id FK "nullable"
        int client_id FK "nullable — доска может быть общей"
        int created_by FK
        bool is_published
        datetime created_at
        datetime updated_at
        datetime deleted_at
    }

    works {
        int id PK
        int board_id FK
        string work_uid UK "uuid для путей на диске"
        string kind "image | video"
        string title
        text description
        string project_url "'' | http(s)-ссылка на кейс клиента"
        int sort_order
        string status "processing | ready | failed"
        string original_name
        string mime
        int size_bytes
        int width
        int height
        float duration_sec "nullable, видео"
        string blurhash
        float preview_focus "nullable, видимый фрагмент: 0 — верх, 1 — низ"
        datetime created_at
    }

    share_links {
        int id PK
        int board_id FK
        string token UK "url-safe, 22+ символа"
        bool is_active
        datetime expires_at "nullable"
        string pin_hash "nullable"
        int created_by FK
        datetime created_at
        datetime revoked_at
    }

    share_views {
        int id PK
        int share_link_id FK
        datetime viewed_at
        string ip_hash
        string user_agent
    }

    site_settings {
        int id PK
        string key UK
        text value
        datetime updated_at
    }
```

## Пояснения к решениям

### users
- `role` — только `root` и `manager`. Первый root создаётся скриптом bootstrap при первом запуске (email/пароль из env, `must_change_password = true`). Дальше root может менять роль активных сотрудников (`manager ↔ root`), поэтому администраторов может быть несколько; система следит, чтобы хотя бы один root всегда оставался (см. [07-security.md](07-security.md)).
- `status = pending` после регистрации: вход запрещён до одобрения. Root меняет на `active` (одобрить) или аккаунт удаляется (отклонить). `disabled` — уволенный сотрудник: вход запрещён, данные и авторство сохраняются. Аккаунт можно и удалить окончательно (`DELETE /staff/{id}`): запись и сессии стираются, но авторство в клиентах/досках/заметках/файлах обнуляется (`ON DELETE SET NULL`), а не удаляется.
- `locale` — выбранный язык интерфейса; по умолчанию `en`. Применяется при каждом входе с любого устройства.

### clients
- `tags` — строка с разделителями для MVP (поиск через LIKE). При переезде на MySQL и росте — вынести в таблицы `tags`/`client_tags` (заложено в план, изменение локальное благодаря репозиторию).
- `source` — откуда пришёл клиент: стабильный строковый ключ (`referral`, `search`, `social`, `ads`, `repeat`, `other`) либо своё значение. Колонкой, а не ссылкой на справочник: у источника нет ни вида, ни порядка, ни архивации — платить за одно редактируемое название внешним ключом и join'ом в каждом отчёте не за что. Понадобятся названия и цвета — рядом появится `client_sources` с тем же ключом, и клиентов переносить не придётся. `NULL` («не спросили») и `other` («спросили, ответ — другое») — разные значения, отчёт показывает их отдельными строками.
- `manager_id` — «ответственный» менеджер; на права видимости в MVP не влияет (видят все сотрудники), только отображается.

### boards / works
- `works.sort_order` — целое с шагом 10 (10, 20, 30...): drag-and-drop меняет порядок без переписывания всех строк.
- `cover_work_id` — обложка доски: показывается в списках CRM и в OG-превью ссылки. По умолчанию — первая работа.
- `is_published` — черновик/опубликована. Неопубликованная доска по ссылке показывает «доступ закрыт» даже при активном токене — менеджер может спокойно готовить контент.
- `works.status = processing` — файл загружен, превью ещё генерируются; витрина такие работы не показывает, CRM показывает с индикатором.
- `works.project_url` — ссылка на кейс клиента: каждая работа на витрине может быть отдельным проектом. Пустая строка = кнопки перехода нет. Пускаются только `http(s)` (ссылка уходит в `href` на публичной странице), см. [10-showcase-cases.md](10-showcase-cases.md).
- `works.preview_focus` — какой фрагмент длинной работы попадает на витрину: 0 — верх картинки, 1 — низ. Форму места задаёт композиция, поэтому высота обрезки не хранится. `NULL` = от верха; заполняется только у длинных картинок, см. [06-showcase-design.md](06-showcase-design.md).

### share_links
- Токен — `secrets.token_urlsafe(16)` → 22 url-safe символа, неугадываемый. Уникален глобально.
- У доски может быть **несколько** ссылок одновременно (например, разным клиентам с разными PIN и сроками) — поэтому отдельная таблица, а не поля в `boards`.
- «Перегенерировать» = отозвать текущую (`is_active = false, revoked_at = now`) + создать новую. История сохраняется.
- `pin_hash` — bcrypt от PIN; сам PIN показывается менеджеру один раз при установке.
- Проверка доступности ссылки (все условия обязаны выполняться): `is_active = true` **и** (`expires_at` пуст **или** в будущем) **и** доска `is_published = true` **и** доска не удалена.

### share_views
- Пишется одна запись на просмотр (открытие страницы с успешным доступом; ввод PIN — после успешного ввода).
- `ip_hash` — хэш IP с солью, не сырой адрес: достаточно для отличия уникальных посетителей, без хранения персональных данных.
- Агрегаты для CRM (`count`, `last viewed`) считаются запросом; при росте — денормализовать счётчик в `share_links`.

### mail_accounts / mail_messages (блок «Почта»)

- `mail_accounts` — ящики фирмы, настройка уровня компании (правит только root). `password_encrypted` — обратимо зашифрованный пароль (см. [07-security.md](07-security.md)); `NULL` = пароль не задан, это не то же самое, что пустая строка. `last_sync_at = NULL` — ящик не читали ни разу: по нулю нельзя отличить новый ящик от сломанного. `last_error = NULL` — ошибок не было.
- `mail_messages` — зеркало переписки: заголовки, оба тела, адресаты. **Историю общения показывает не эта таблица, а `client_notes`**: письмо при появлении порождает запись общей ленты (`kind='email'`, `direction='in'|'out'`, `happened_at` = момент отправки письма, `deal_id` — если письмо про заявку). Так переписка стоит в одном ряду со звонками и встречами, а не рядом отдельным списком.
- `message_id` **уникален** — на нём держится идемпотентность синхронизации: то же письмо, приехавшее второй раз (сменился UIDVALIDITY, переехал ящик), не задваивает ни запись, ни строку ленты. Побочный эффект: письмо, пришедшее сразу на два наших ящика, сохранится один раз — для ленты клиента это верно, разговор был один.
- `uid = NULL` у исходящих: на сервере входящей почты их нет. Ноль — законный UID, поэтому именно `NULL`.
- `sent_at` — момент отправки письма, приведённый к UTC из зоны отправителя. Не момент синхронизации: иначе вся почта, забранная одним заходом, встала бы в ленте единым столбиком «сегодня».
- `body_text`/`body_html` объявлены `deferred`: выборка списка писем их не читает — письмо на сотни килобайт не редкость, а в списке нужны только заголовки.
- Внешние ключи: `account_id → mail_accounts` **CASCADE** (без доступа к серверу зеркало не обновить; история общения при этом остаётся в ленте и от ящика не зависит), `client_id → clients` и `deal_id → deals` — **SET NULL**: карточку вычистили из корзины, а переписка остаётся. Это документы фирмы, по ним разбираются и после ухода клиента.
- Входящее письмо привязывается к клиенту по адресу отправителя, но **не к заявке**: у клиента их бывает несколько сразу, и «взять последнюю открытую» — угадывание. Клиент — факт (адрес совпал), заявка — решение человека.

### site_settings
- Ключ-значение: `brand_name`, `brand_logo_path`, `contact_email`, `contact_phone`, `social_telegram`, `showcase_locale`, `og_default_image` и т.п.
- Тумблеры витрины хранятся строкой `"1"`/`"0"` (таблица строковая): `showcase_show_meta` — строка «7 works · updated …» под названием доски, `showcase_show_footer` — футер с контактами. Оба по умолчанию `"0"`.
- Редактирует только root. Кэшируется в памяти процесса с инвалидацией при записи.

## Жизненный цикл файлов и освобождение места

| Действие | Что с файлами |
|---|---|
| Удалить работу | файлы удаляются сразу (в т.ч. из менеджера файлов, root) |
| Удалить **ссылку** на доску | файлы **не трогаются** — они принадлежат доске, а у доски может быть несколько ссылок; удаление одной не должно ломать остальные |
| Удалить **доску** | доска, её работы, ссылки и просмотры удаляются сразу; файлы работ стираются с диска (восстановления досок нет — каскад `ondelete=CASCADE`) |
| Удалить **клиента** | мягкое удаление: запись скрыта, файлы лежат до очистки корзины (клиента можно восстановить) |
| Очистить корзину (root) или `purge_deleted.py` | мягко удалённые клиенты и их файлы удаляются безвозвратно, место освобождается |

Объём корзины (мягко удалённые клиенты) виден в карточке «Хранилище». Автоматика: cron с карантином 30 дней, см. [08-deployment.md](08-deployment.md). Обзор всех медиафайлов досок с размером/датами и точечным удалением — менеджер файлов (root), см. [05-crm-design.md](05-crm-design.md).

## Регистронезависимый поиск

Встроенные `lower()`/`upper()` в SQLite работают только с ASCII: `lower('Брусника')` возвращает строку без изменений, поэтому `ilike` (SQLAlchemy эмулирует его через `lower()`) не находил русские названия, набранные в другом регистре. В [database/session.py](../database/session.py) обе функции подменяются Python-реализациями через `create_function` — это чинит поиск во всех репозиториях сразу и не требует диалектных веток в запросах.

При переезде на MySQL подмена не нужна: сравнение в `utf8mb4_general_ci` регистронезависимо нативно.

## Индексы

| Таблица | Индекс | Зачем |
|---|---|---|
| users | `email` (unique) | вход |
| clients | `name`, `manager_id`, `deleted_at` | списки и поиск |
| clients | `source`, `created_at` | отчёт по источникам: группировка и отбор за период — иначе отчёт за год читает таблицу целиком |
| client_notes | `(client_id, happened_at)` | лента карточки |
| works | `(board_id, sort_order)` | вывод доски по порядку |
| share_links | `token` (unique) | открытие витрины — самый горячий запрос |
| share_views | `(share_link_id, viewed_at)` | статистика |
| mail_messages | `message_id` (unique) | идемпотентность синхронизации |
| mail_messages | `client_id`, `deal_id`, `sent_at` | письма в карточке клиента/заявки и порядок по дате |
| mail_messages | `(account_id, uid)` | «с какого места забирать дальше» |

## Миграция SQLite → MySQL

1. Все типы уже переносимые; Alembic-миграции написаны без диалектных веток.
2. Перенос данных: скрипт `scripts/migrate_to_mysql.py` — читает через SQLAlchemy из SQLite, пишет в MySQL (порядок: users → clients → boards → works → остальное).
3. Меняется `OPENCRM_DB_URL`, прогоняются миграции на пустой MySQL, затем перенос, затем переключение приложения.
4. Файлы в `storage/` не затрагиваются — пути в БД относительные.
5. После переезда: `utf8mb4`, движок InnoDB — задаётся в Alembic `env.py` через `mysql_*`-аргументы, SQLite их игнорирует.
