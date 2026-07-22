# 04 — API

## Общее

- Базовый префикс внутреннего API: `/api/v1`. Публичные страницы витрины — вне API (см. раздел «Публичные маршруты»).
- Формат — JSON; загрузка файлов — `multipart/form-data`.
- Аутентификация — сессионная cookie (`HttpOnly`, `Secure`, `SameSite=Lax`). SPA и API живут на одном домене, поэтому cookie проще и безопаснее JWT в localStorage.
- Ошибки — единый формат:

```json
{ "error": { "code": "board_not_found", "message": "Board not found" } }
```

- Пагинация списков: `?page=1&per_page=50`, ответ содержит `items`, `total`, `page`, `per_page`.
- Все mutation-запросы защищены CSRF-токеном (double-submit cookie).

## Права

| Обозначение | Кто |
|---|---|
| 🔓 | без аутентификации |
| 👤 | любой активный сотрудник (root или менеджер) |
| 👑 | только root |

## Аутентификация и аккаунт

| Метод | Путь | Права | Описание |
|---|---|---|---|
| POST | `/auth/register` | 🔓 | Заявка на аккаунт менеджера: `name, email, password`. Ответ — «ожидайте одобрения» |
| POST | `/auth/login` | 🔓 | Вход. `403 account_pending` — не одобрен, `403 account_disabled` — деактивирован |
| POST | `/auth/logout` | 👤 | Выход |
| GET | `/auth/me` | 👤 | Текущий пользователь: имя, роль, `locale`, `must_change_password` |
| PATCH | `/auth/me` | 👤 | Смена имени, `locale` (en/ru — сохраняется в БД) |
| POST | `/auth/me/password` | 👤 | Смена пароля (старый + новый); сбрасывает `must_change_password` |

## Сотрудники (root)

| Метод | Путь | Права | Описание |
|---|---|---|---|
| GET | `/staff` | 👑 | Список сотрудников с фильтром `?status=pending` |
| POST | `/staff/{id}/approve` | 👑 | Одобрить заявку |
| POST | `/staff/{id}/reject` | 👑 | Отклонить заявку (запись удаляется) |
| POST | `/staff/{id}/disable` | 👑 | Деактивировать; `/enable` — вернуть |
| POST | `/staff/{id}/reset-password` | 👑 | Выдать временный пароль с принудительной сменой |

## Клиенты

| Метод | Путь | Права | Описание |
|---|---|---|---|
| GET | `/clients` | 👤 | Список: `?search=`, `?tag=`, `?manager_id=`, сортировка по обновлению |
| POST | `/clients` | 👤 | Создать карточку |
| GET | `/clients/{id}` | 👤 | Карточка целиком: контакты, последние заметки, файлы, связанные доски |
| PATCH | `/clients/{id}` | 👤 | Обновить поля |
| DELETE | `/clients/{id}` | 👤 | Мягкое удаление (root может восстановить) |
| GET | `/clients/{id}/notes` | 👤 | Лента истории (пагинация) |
| POST | `/clients/{id}/notes` | 👤 | Добавить запись: `kind (note/call/meeting/email), body, happened_at` |
| DELETE | `/clients/{id}/notes/{note_id}` | 👤 | Удалить запись (автор или root) |
| GET | `/clients/{id}/files` | 👤 | Список файлов |
| POST | `/clients/{id}/files` | 👤 | Загрузить файл (multipart) |
| GET | `/clients/{id}/files/{file_id}/download` | 👤 | Скачать (через приложение, с проверкой сессии) |
| DELETE | `/clients/{id}/files/{file_id}` | 👤 | Удалить файл |

## Доски и работы

| Метод | Путь | Права | Описание |
|---|---|---|---|
| GET | `/boards` | 👤 | Список: `?search=`, `?client_id=`, счётчики работ и просмотров |
| POST | `/boards` | 👤 | Создать: `title, description?, client_id?` |
| GET | `/boards/{id}` | 👤 | Доска + работы по порядку + активные ссылки |
| PATCH | `/boards/{id}` | 👤 | Название, описание, `client_id`, `cover_work_id`, `is_published` |
| DELETE | `/boards/{id}` | 👤 | Мягкое удаление; все ссылки доски перестают открываться |
| POST | `/boards/{id}/works` | 👤 | Загрузка файла работы (multipart). Ответ `202` + `work` со `status=processing` |
| GET | `/boards/{id}/works/{work_id}` | 👤 | Одна работа (поллинг статуса обработки) |
| PATCH | `/boards/{id}/works/{work_id}` | 👤 | `title`, `description` |
| PUT | `/boards/{id}/works/order` | 👤 | Новый порядок: `{"work_ids": [5, 2, 9, ...]}` (drag-and-drop) |
| DELETE | `/boards/{id}/works/{work_id}` | 👤 | Удалить работу и её файлы |

## Публичные ссылки

| Метод | Путь | Права | Описание |
|---|---|---|---|
| GET | `/boards/{id}/shares` | 👤 | Все ссылки доски: токен, статус, срок, есть ли PIN, просмотры |
| POST | `/boards/{id}/shares` | 👤 | Создать: `expires_at?`, `pin?` (4–8 цифр). Ответ содержит полный URL |
| PATCH | `/shares/{id}` | 👤 | Изменить: `is_active` (отзыв/включение), `expires_at`, `pin` (null — убрать) |
| POST | `/shares/{id}/regenerate` | 👤 | Отозвать текущую + создать новую с теми же настройками |
| GET | `/shares/{id}/views` | 👤 | Журнал просмотров: когда, сколько уникальных |

## Настройки сайта (root)

| Метод | Путь | Права | Описание |
|---|---|---|---|
| GET | `/settings` | 👑 | Все настройки: бренд, контакты, соцсети, язык витрины |
| PATCH | `/settings` | 👑 | Обновить значения |
| POST | `/settings/logo` | 👑 | Загрузить логотип (multipart) |

## Публичные маршруты (витрина, вне /api)

| Метод | Путь | Описание |
|---|---|---|
| GET | `/b/{token}` | Страница витрины. SSR: OG-теги, первые работы, blurhash. Если PIN — форма PIN. Если недоступна — страница «доступ закрыт» (единый вид для отозванной/истёкшей/неопубликованной/несуществующей — не раскрываем причину) |
| POST | `/b/{token}/pin` | Проверка PIN. Успех → подписанная cookie доступа на этот токен (живёт сессию браузера). Перебор ограничен (см. [07-security.md](07-security.md)) |
| GET | `/b/{token}/data` | JSON с работами для гидрации/дозагрузки (те же проверки доступа) |
| GET | `/media/{board_uid}/{work_uid}/{size}.webp` | Файлы медиа — отдаёт nginx напрямую |

## Пример: создание ссылки

```http
POST /api/v1/boards/12/shares
Content-Type: application/json

{ "expires_at": "2026-08-22T00:00:00Z", "pin": "4821" }
```

```json
{
  "id": 7,
  "url": "https://studio.example.com/b/xK9fQ2mNpL7vTzR3aW8bYc",
  "token": "xK9fQ2mNpL7vTzR3aW8bYc",
  "is_active": true,
  "expires_at": "2026-08-22T00:00:00Z",
  "has_pin": true,
  "views_count": 0,
  "created_at": "2026-07-22T12:00:00Z"
}
```
