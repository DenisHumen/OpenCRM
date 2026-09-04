// Порождено скриптом scripts/spravochnik_api.py из docs/04-api.md.
// Руками не править: правится справочник, потом запускается скрипт.

export type VidDostupa = "otkryto" | "sotrudnik" | "pravo" | "klyuch" | "inoe";

export type Ruchka = {
  metod: string;
  put: string;
  vid: VidDostupa;
  dostup: string;
  opisanie: string;
  podrazdel: string;
  vne_api: boolean;
};

export type RazdelApi = { nazvanie: string; ruchki: Ruchka[] };

export const SPRAVOCHNIK_API: RazdelApi[] = [
  {
    "nazvanie": "Аутентификация и аккаунт",
    "ruchki": [
      {
        "metod": "POST",
        "put": "/auth/register",
        "vid": "otkryto",
        "dostup": "",
        "opisanie": "Заявка на аккаунт менеджера: `name, email, password`. Ответ — «ожидайте одобрения»",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/auth/login",
        "vid": "otkryto",
        "dostup": "",
        "opisanie": "Вход. `403 account_pending` — не одобрен, `403 account_disabled` — деактивирован",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/auth/logout",
        "vid": "sotrudnik",
        "dostup": "",
        "opisanie": "Выход",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/auth/me",
        "vid": "sotrudnik",
        "dostup": "",
        "opisanie": "Текущий пользователь: имя, `role` (root или нет), `role_id`/`role_name` (должность), `permissions` — текущий набор прав, `locale`, `must_change_password`, `avatar_url`, `is_online`, `last_seen_at`. Права читаются на каждый запрос, а не запоминаются при входе",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "PATCH",
        "put": "/auth/me",
        "vid": "sotrudnik",
        "dostup": "",
        "opisanie": "Смена имени, `locale` (en/ru — сохраняется в БД)",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/auth/me/password",
        "vid": "sotrudnik",
        "dostup": "",
        "opisanie": "Смена пароля (старый + новый); сбрасывает `must_change_password`",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/auth/heartbeat",
        "vid": "sotrudnik",
        "dostup": "",
        "opisanie": "Пинг присутствия: обновляет `last_seen` (фронт шлёт раз в ~45 c, пока вкладка активна)",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/auth/me/avatar",
        "vid": "sotrudnik",
        "dostup": "",
        "opisanie": "Загрузить свой аватар (растр по сигнатуре, не SVG; обрезается в квадрат 256, webp)",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "DELETE",
        "put": "/auth/me/avatar",
        "vid": "sotrudnik",
        "dostup": "",
        "opisanie": "Убрать аватар (файл стирается с диска)",
        "podrazdel": "",
        "vne_api": false
      }
    ]
  },
  {
    "nazvanie": "Сотрудники",
    "ruchki": [
      {
        "metod": "GET",
        "put": "/staff",
        "vid": "pravo",
        "dostup": "staff.view",
        "opisanie": "Список сотрудников с фильтром `?status=pending`; у каждого `role_id` и `role_name`",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/staff/{id}/approve",
        "vid": "pravo",
        "dostup": "staff.manage",
        "opisanie": "Одобрить заявку",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/staff/{id}/reject",
        "vid": "pravo",
        "dostup": "staff.manage",
        "opisanie": "Отклонить заявку (запись удаляется)",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/staff/{id}/disable",
        "vid": "pravo",
        "dostup": "staff.manage",
        "opisanie": "Деактивировать: сессии гаснут, войти нельзя",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/staff/{id}/enable",
        "vid": "pravo",
        "dostup": "staff.manage",
        "opisanie": "Вернуть деактивированного в строй",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/staff/{id}/reset-password",
        "vid": "pravo",
        "dostup": "staff.manage",
        "opisanie": "Выдать временный пароль с принудительной сменой",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/staff/{id}/role",
        "vid": "pravo",
        "dostup": "roles.manage",
        "opisanie": "Сделать root'ом или вернуть обратно (`{\"role\": \"root\"\\|\"manager\"}`) — это признак владельца системы, а не должность. Только активным; свою нельзя (`403 cannot_change_own_role`), последнего root не снять (`403 last_root`), `409 not_active`",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "DELETE",
        "put": "/staff/{id}",
        "vid": "pravo",
        "dostup": "staff.manage",
        "opisanie": "Удалить аккаунт безвозвратно. Себя нельзя (`403 cannot_delete_self`), последнего root нельзя (`403 last_root`). Авторство сохраняется, но обнуляется",
        "podrazdel": "",
        "vne_api": false
      }
    ]
  },
  {
    "nazvanie": "Роли и доступы",
    "ruchki": [
      {
        "metod": "GET",
        "put": "/roles/matrix",
        "vid": "sotrudnik",
        "dostup": "",
        "opisanie": "Из чего собирается роль: строки по реестру блоков плюс системные области, столбцы-действия, пресеты. Открыт всем — интерфейс по нему решает, что показывать",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/roles",
        "vid": "pravo",
        "dostup": "roles.view",
        "opisanie": "Роли с их правами и числом сотрудников",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/roles/{id}",
        "vid": "pravo",
        "dostup": "roles.view",
        "opisanie": "Одна роль",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/roles",
        "vid": "pravo",
        "dostup": "roles.manage",
        "opisanie": "Создать: `{name, permissions: [\"deals.view\", …]}`. Несуществующее право — `422 unknown_permission`, занятое имя — `409 role_name_taken`",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/roles/from-preset",
        "vid": "pravo",
        "dostup": "roles.manage",
        "opisanie": "Создать из готового набора: `{preset, name?}`. `422 unknown_preset`",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "PATCH",
        "put": "/roles/{id}",
        "vid": "pravo",
        "dostup": "roles.manage",
        "opisanie": "Переименовать и/или заменить набор прав. Снять `roles.manage` с последней такой роли нельзя — `403 last_roles_manager`",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/roles/{id}/default",
        "vid": "pravo",
        "dostup": "roles.manage",
        "opisanie": "Какую роль получает новый сотрудник при регистрации",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "DELETE",
        "put": "/roles/{id}",
        "vid": "pravo",
        "dostup": "roles.manage",
        "opisanie": "Удалить. Занятую нельзя (`409 role_in_use`), роль по умолчанию нельзя (`422 role_is_default`)",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/roles/assign/{user_id}",
        "vid": "pravo",
        "dostup": "roles.manage",
        "opisanie": "Назначить должность (`{\"role_id\": 3}`; `null` — снять). Себе нельзя (`403 cannot_change_own_role`), root'у нельзя (`403 cannot_assign_role_to_root`), последнего управляющего правами не снять (`403 last_roles_manager`)",
        "podrazdel": "",
        "vne_api": false
      }
    ]
  },
  {
    "nazvanie": "Коллеги и рабочее пространство",
    "ruchki": [
      {
        "metod": "GET",
        "put": "/people",
        "vid": "sotrudnik",
        "dostup": "",
        "opisanie": "Коллеги для выпадающего списка «ответственный»: `id`, имя, аватар активных сотрудников. Отдельно от `/staff`: назначать заявку на коллегу должен всякий, а знать при этом чужие адреса, статусы и даты одобрения — незачем",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/workspace",
        "vid": "sotrudnik",
        "dostup": "",
        "opisanie": "`brand_name`, `currency`, `deal_term`. Полные настройки читает не каждый, а валюта и слово для заявки нужны на каждом экране: без них суммы показываются без обозначения, а разделы называются чужими словами",
        "podrazdel": "",
        "vne_api": false
      }
    ]
  },
  {
    "nazvanie": "Блоки системы",
    "ruchki": [
      {
        "metod": "GET",
        "put": "/modules",
        "vid": "sotrudnik",
        "dostup": "",
        "opisanie": "Что включено, кем и когда. Права нет намеренно: интерфейсу надо знать, что рисовать в меню, — а это ровно каждый сотрудник",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/modules/presets",
        "vid": "pravo",
        "dostup": "settings.manage",
        "opisanie": "Наборы блоков под тип дела: состав, `will_enable` (чего ещё нет), воронка и слово для заявки. Состав считает сервер — список ключей во фронтенде разошёлся бы с реестром на первом же новом блоке",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/modules/presets",
        "vid": "pravo",
        "dostup": "settings.manage",
        "opisanie": "Применить набор: `{key, apply_pipeline?, apply_deal_term?}`. Набор **только включает** — блоки вне него остаются как есть",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/modules/{key}",
        "vid": "pravo",
        "dostup": "settings.manage",
        "opisanie": "Переключить один блок: `{\"enabled\": true\\|false}`",
        "podrazdel": "",
        "vne_api": false
      }
    ]
  },
  {
    "nazvanie": "Поиск и дашборд",
    "ruchki": [
      {
        "metod": "GET",
        "put": "/search?q=",
        "vid": "sotrudnik",
        "dostup": "",
        "opisanie": "Общий поиск для командной палитры (Ctrl+K). Три группы в порядке левой колонки — `clients`, `deals`, `boards`, — по первой странице (6 записей) в каждой. Пустой `q` — недавние записи",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/search/{area}?q=&page=",
        "vid": "sotrudnik",
        "dostup": "",
        "opisanie": "Продолжение ОДНОЙ группы — «показать ещё» в палитре. `area` — ключ группы из общего поиска, неизвестный даёт 404. Отвечает той же формой, что группа: `items`, `total`, `has_more`",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/dashboard",
        "vid": "sotrudnik",
        "dostup": "",
        "opisanie": "Сводка: деньги с начала месяца, воронка со счётчиками, мои задачи на сегодня, просмотры за 7 дней, недавние доски и клиенты",
        "podrazdel": "",
        "vne_api": false
      }
    ]
  },
  {
    "nazvanie": "Хранилище и состояние системы",
    "ruchki": [
      {
        "metod": "GET",
        "put": "/system/storage",
        "vid": "sotrudnik",
        "dostup": "",
        "opisanie": "Место на диске: занято/свободно, размер `storage/`, объём корзины, уровень тревоги, блокирована ли загрузка",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/system/storage/purge",
        "vid": "pravo",
        "dostup": "settings.manage",
        "opisanie": "Окончательно удалить мягко удалённое: доски, работы, клиентов, заявки и файлы клиентов. Отвечает счётчиками по каждому виду и свежим состоянием хранилища",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/system/files",
        "vid": "pravo",
        "dostup": "settings.manage",
        "opisanie": "Менеджер файлов: все работы досок — размер на диске, дата загрузки, дата последнего просмотра доски, превью",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "DELETE",
        "put": "/system/files/{work_id}",
        "vid": "pravo",
        "dostup": "settings.manage",
        "opisanie": "Удалить одну работу вместе с файлами; отвечает свежим статусом хранилища",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/system/schema",
        "vid": "pravo",
        "dostup": "settings.manage",
        "opisanie": "Сходится ли живая база с моделями — подробно, для разбора",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/system/github",
        "vid": "sotrudnik",
        "dostup": "",
        "opisanie": "Звёзды проекта на GitHub из суточного кэша. `null` — «не спрашивали или не дозвонились»; ноль был бы утверждением, которого мы не делали",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/system/monitoring",
        "vid": "pravo",
        "dostup": "monitoring.view",
        "opisanie": "Состояние стека наблюдения и путь к панели",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/system/backups",
        "vid": "pravo",
        "dostup": "settings.manage",
        "opisanie": "Копии с экрана: есть ли ключ, последние работы, итог последней проверки. Разбор — [15-backup-encryption.md](15-backup-encryption.md) §10",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/system/backups/key",
        "vid": "pravo",
        "dostup": "settings.manage",
        "opisanie": "Породить ключ копий; показывается один раз и до подтверждения не действует. `409 backup_key_exists` — ключ уже есть, менять через `{\"replace\": true}`",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/system/backups/key/confirm",
        "vid": "pravo",
        "dostup": "settings.manage",
        "opisanie": "Подтвердить ключ последними восемью знаками. `404 backup_key_not_pending`, `422 backup_key_fragment_mismatch`",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/system/backups/db",
        "vid": "pravo",
        "dostup": "settings.manage",
        "opisanie": "Снять копию базы в потоке; отвечает работой `{id, status}`. `409 backup_key_missing` — ключа нет, `409 backup_busy` — другая работа идёт",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/system/backups/storage",
        "vid": "pravo",
        "dostup": "settings.manage",
        "opisanie": "То же для архива файлов (`storage`): фотографии, вложения, оформление. Отдельным файлом — решение владельца, docs/15 §0",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/system/backups/jobs/{id}",
        "vid": "pravo",
        "dostup": "settings.manage",
        "opisanie": "Состояние работы: `running / done / failed`, имя и размер файла, таблицы и строки, итог проверки ключом",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/system/backups/jobs/{id}/file",
        "vid": "pravo",
        "dostup": "settings.manage",
        "opisanie": "Готовая копия файлом; пишется в журнал и ставит отметку увоза. `404 backup_not_ready`, `404 backup_gone` (копия старше суток убрана)",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/system/backups/jobs/{id}/check",
        "vid": "pravo",
        "dostup": "settings.manage",
        "opisanie": "Ещё раз открыть копию нынешним ключом — так обнаруживается потерянный или заменённый ключ",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "DELETE",
        "put": "/system/backups/jobs/{id}",
        "vid": "pravo",
        "dostup": "settings.manage",
        "opisanie": "Убрать копию с сервера раньше суток; идущую нельзя (`409 backup_busy`). Пишется в журнал `backup.deleted`",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/system/backups/restore",
        "vid": "pravo",
        "dostup": "backups.manage",
        "opisanie": "Заменить базу (или дополнить файлы) из зашифрованной копии: multipart `kind=db|storage`, `file`. Отказы до того, как тронута база: `422 backup_not_encrypted`, `backup_bad_key`, `backup_truncated`, `backup_unknown_revision`; `409 backup_busy`. Дальше — работа, за которой следят по `/jobs/{id}`",
        "podrazdel": "",
        "vne_api": false
      }
    ]
  },
  {
    "nazvanie": "Клиенты",
    "ruchki": [
      {
        "metod": "GET",
        "put": "/clients",
        "vid": "pravo",
        "dostup": "clients.view",
        "opisanie": "Список: `?search=`, `?tag=`, `?manager_id=`, пагинация, сортировка по обновлению",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/clients/export.csv",
        "vid": "pravo",
        "dostup": "clients.view",
        "opisanie": "Тот же отбор файлом, целиком и без страниц. Больше 10 000 строк — отказ `export_too_large`, а не молчаливое обрезание. Право то же, что на просмотр: выгрузка отдаёт ровно то, что человек и так видит",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/clients",
        "vid": "pravo",
        "dostup": "clients.create",
        "opisanie": "Создать карточку",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/clients/{id}",
        "vid": "pravo",
        "dostup": "clients.view",
        "opisanie": "Карточка целиком: контакты, последние заметки, файлы, связанные доски",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "PATCH",
        "put": "/clients/{id}",
        "vid": "pravo",
        "dostup": "clients.edit",
        "opisanie": "Обновить поля",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "DELETE",
        "put": "/clients/{id}",
        "vid": "pravo",
        "dostup": "clients.delete",
        "opisanie": "Мягкое удаление",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/clients/{id}/restore",
        "vid": "pravo",
        "dostup": "clients.restore",
        "opisanie": "Вернуть из корзины",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/clients/{id}/notes",
        "vid": "pravo",
        "dostup": "clients.view",
        "opisanie": "Лента истории (пагинация). Фильтры `kind` и `deal_id`",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/clients/{id}/notes",
        "vid": "pravo",
        "dostup": "clients.edit",
        "opisanie": "Добавить запись: `kind` (note/call/meeting/email), `body`, `happened_at`, `direction`, `deal_id`",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "DELETE",
        "put": "/clients/{id}/notes/{note_id}",
        "vid": "pravo",
        "dostup": "clients.edit",
        "opisanie": "Удалить запись",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/clients/{id}/files",
        "vid": "pravo",
        "dostup": "clients.view",
        "opisanie": "Список файлов (одним `items`, без страниц)",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/clients/{id}/files",
        "vid": "pravo",
        "dostup": "clients.edit",
        "opisanie": "Загрузить файл (multipart)",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/clients/{id}/files/{file_id}/download",
        "vid": "pravo",
        "dostup": "clients.view",
        "opisanie": "Скачать (через приложение, с проверкой сессии)",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "DELETE",
        "put": "/clients/{id}/files/{file_id}",
        "vid": "pravo",
        "dostup": "clients.edit",
        "opisanie": "Удалить файл",
        "podrazdel": "",
        "vne_api": false
      }
    ]
  },
  {
    "nazvanie": "Заявки (блок `deals`, несущий)",
    "ruchki": [
      {
        "metod": "GET",
        "put": "/deals/board",
        "vid": "pravo",
        "dostup": "deals.view",
        "opisanie": "Канбан: колонки по этапам воронки целиком (этап отдаётся не ключом, а записью — фронт рисует названия этой фирмы), `amount_total` над колонкой, `currency`",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/deals",
        "vid": "pravo",
        "dostup": "deals.view",
        "opisanie": "Список: `search`, `stage`, `client_id`, `manager_id`, `include_closed`, пагинация",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/deals",
        "vid": "pravo",
        "dostup": "deals.create",
        "opisanie": "Завести заявку",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/deals/{id}",
        "vid": "pravo",
        "dostup": "deals.view",
        "opisanie": "Карточка: поля, история этапов, валюта, телефон клиента, доски заявки (если блок досок включён)",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "PATCH",
        "put": "/deals/{id}",
        "vid": "pravo",
        "dostup": "deals.edit",
        "opisanie": "Изменить поля",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/deals/{id}/move",
        "vid": "pravo",
        "dostup": "deals.move_stage",
        "opisanie": "Перетаскивание в канбане: `{stage, sort_order?, lost_reason?}`",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "DELETE",
        "put": "/deals/{id}",
        "vid": "pravo",
        "dostup": "deals.delete",
        "opisanie": "Удалить",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/deals/{id}/feed",
        "vid": "pravo",
        "dostup": "deals.view",
        "opisanie": "Лента заявки: звонки, письма, встречи и заметки одним потоком, фильтр `kind`",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/deals/{id}/feed",
        "vid": "pravo",
        "dostup": "deals.edit",
        "opisanie": "Дописать в ленту (то же тело, что у заметки клиента)",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/deals/{id}/lines",
        "vid": "pravo",
        "dostup": "deals.view",
        "opisanie": "Состав заявки: товары и свои траты, итог, себестоимость и ожидаемая прибыль. Блок `warehouse`",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/deals/{id}/lines",
        "vid": "pravo",
        "dostup": "deals.edit",
        "opisanie": "Добавить строку: `product_id`, `sku` или `code` (скан); ничего из них — своя трата",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "PATCH",
        "put": "/deals/{id}/lines/{line_id}",
        "vid": "pravo",
        "dostup": "deals.edit",
        "opisanie": "Количество, цена, склад; название — только у своей траты",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "DELETE",
        "put": "/deals/{id}/lines/{line_id}",
        "vid": "pravo",
        "dostup": "deals.edit",
        "opisanie": "Убрать строку",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/deals/{id}/order",
        "vid": "pravo",
        "dostup": "orders.create",
        "opisanie": "Завести заказ по заявке, перенеся её товары. Блок `orders`",
        "podrazdel": "",
        "vne_api": false
      }
    ]
  },
  {
    "nazvanie": "Воронка",
    "ruchki": [
      {
        "metod": "GET",
        "put": "/pipeline/stages",
        "vid": "pravo",
        "dostup": "deals.view",
        "opisanie": "Этапы по порядку. Читать должен всякий, кто видит заявки, — без этого не нарисовать доску",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/pipeline/presets",
        "vid": "pravo",
        "dostup": "settings.manage",
        "opisanie": "Готовые воронки под отрасль: ключ, название, подсказка, состав",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/pipeline/preset",
        "vid": "pravo",
        "dostup": "settings.manage",
        "opisanie": "Применить готовую воронку целиком",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/pipeline/stages",
        "vid": "pravo",
        "dostup": "settings.manage",
        "opisanie": "Добавить этап: `{name, kind, after?}`",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "PATCH",
        "put": "/pipeline/stages/{key}",
        "vid": "pravo",
        "dostup": "settings.manage",
        "opisanie": "Переименовать, сменить вид, цвет, порядок",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "DELETE",
        "put": "/pipeline/stages/{key}",
        "vid": "pravo",
        "dostup": "settings.manage",
        "opisanie": "Убрать этап с доски",
        "podrazdel": "",
        "vne_api": false
      }
    ]
  },
  {
    "nazvanie": "Напоминания (блок `tasks`)",
    "ruchki": [
      {
        "metod": "GET",
        "put": "/tasks",
        "vid": "pravo",
        "dostup": "tasks.view",
        "opisanie": "Список одним `items`; у записи рядом с номерами — `assignee_name`, `client_name`, `deal_title`. Фильтры `scope` (по умолчанию `open`), `assignee_id`, `client_id`, `deal_id`",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/tasks/summary",
        "vid": "pravo",
        "dostup": "tasks.view",
        "opisanie": "Счётчики для навигации",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/tasks",
        "vid": "pravo",
        "dostup": "tasks.create",
        "opisanie": "Завести: `title`, `due_at`, `assignee_id`, `client_id`, `deal_id`",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "PATCH",
        "put": "/tasks/{id}",
        "vid": "pravo",
        "dostup": "tasks.edit",
        "opisanie": "Изменить любое поле, включая `is_done`",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "DELETE",
        "put": "/tasks/{id}",
        "vid": "pravo",
        "dostup": "tasks.delete",
        "opisanie": "Удалить",
        "podrazdel": "",
        "vne_api": false
      }
    ]
  },
  {
    "nazvanie": "Доски и работы (блок `boards`)",
    "ruchki": [
      {
        "metod": "GET",
        "put": "/boards",
        "vid": "pravo",
        "dostup": "boards.view",
        "opisanie": "Список: `?search=`, `?client_id=`, счётчики работ и просмотров",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/boards",
        "vid": "pravo",
        "dostup": "boards.create",
        "opisanie": "Создать: `title`, `description?`, `client_id?`, `deal_id?`",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/boards/{id}",
        "vid": "pravo",
        "dostup": "boards.view",
        "opisanie": "Доска + работы по порядку + **все** ссылки доски (ключ `shares`)",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "PATCH",
        "put": "/boards/{id}",
        "vid": "pravo",
        "dostup": "boards.edit",
        "opisanie": "Название, описание, `client_id`, `deal_id`, `cover_work_id`, `is_published`",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "DELETE",
        "put": "/boards/{id}",
        "vid": "pravo",
        "dostup": "boards.delete",
        "opisanie": "Мягкое удаление; все ссылки доски перестают открываться",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/boards/{id}/works",
        "vid": "pravo",
        "dostup": "boards.create",
        "opisanie": "Загрузка файла работы (multipart). Ответ `202` + `work` со `status=processing`",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/boards/{id}/works/{work_id}",
        "vid": "pravo",
        "dostup": "boards.view",
        "opisanie": "Одна работа (поллинг статуса обработки)",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "PATCH",
        "put": "/boards/{id}/works/{work_id}",
        "vid": "pravo",
        "dostup": "boards.edit",
        "opisanie": "`title`, `description`, `project_url` (только `http(s)`, иначе `422 bad_project_url`), `preview_focus` — видимый фрагмент работы (0…1): только у картинок с известными сторонами (иначе `422 not_a_croppable_work`), значение подрезается до диапазона, `null` возвращает показ от верха",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "PUT",
        "put": "/boards/{id}/works/order",
        "vid": "pravo",
        "dostup": "boards.edit",
        "opisanie": "Новый порядок: `{\"work_ids\": [5, 2, 9, ...]}` (drag-and-drop)",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "DELETE",
        "put": "/boards/{id}/works/{work_id}",
        "vid": "pravo",
        "dostup": "boards.delete",
        "opisanie": "Удалить работу и её файлы",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/boards/{id}/works/{work_id}/download",
        "vid": "pravo",
        "dostup": "boards.view",
        "opisanie": "Исходник работы файлом (`attachment`). Имя — то, под которым файл загрузили, расширение — настоящее (вид определяется по сигнатуре). Работа обязана принадлежать НАЗВАННОЙ доске: чужой номер получает `404`, а не чужой файл",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/boards/{id}/download",
        "vid": "pravo",
        "dostup": "boards.view",
        "opisanie": "Все исходники доски одним архивом, потоком (в памяти не собирается). Имена внутри пронумерованы порядком доски; пустая доска — `422 board_has_no_files`",
        "podrazdel": "",
        "vne_api": false
      }
    ]
  },
  {
    "nazvanie": "Публичные ссылки",
    "ruchki": [
      {
        "metod": "GET",
        "put": "/boards/{id}/shares",
        "vid": "pravo",
        "dostup": "boards.view",
        "opisanie": "Все ссылки доски: токен, статус, срок, есть ли PIN, просмотры",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/boards/{id}/shares",
        "vid": "pravo",
        "dostup": "boards.edit",
        "opisanie": "Создать: `expires_at?`, `pin?` (4–8 цифр). Ответ содержит полный URL",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "PATCH",
        "put": "/shares/{id}",
        "vid": "pravo",
        "dostup": "boards.edit",
        "opisanie": "Изменить: `is_active` (отзыв/включение), `expires_at`, `pin` (null — убрать)",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "DELETE",
        "put": "/shares/{id}",
        "vid": "pravo",
        "dostup": "boards.delete",
        "opisanie": "Удалить ссылку и журнал её просмотров. Файлы работ не трогаются: они принадлежат доске, а у доски может быть несколько ссылок",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/shares/{id}/regenerate",
        "vid": "pravo",
        "dostup": "boards.edit",
        "opisanie": "Отозвать текущую + создать новую с теми же настройками",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/shares/{id}/views",
        "vid": "pravo",
        "dostup": "boards.view",
        "opisanie": "Журнал просмотров: когда, сколько уникальных",
        "podrazdel": "",
        "vne_api": false
      }
    ]
  },
  {
    "nazvanie": "Фирмы (блок `companies`)",
    "ruchki": [
      {
        "metod": "GET",
        "put": "/companies",
        "vid": "pravo",
        "dostup": "companies.view",
        "opisanie": "Список одним `items`",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/companies/{id}",
        "vid": "pravo",
        "dostup": "companies.view",
        "opisanie": "Одна фирма",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/companies",
        "vid": "pravo",
        "dostup": "companies.create",
        "opisanie": "Завести",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "PATCH",
        "put": "/companies/{id}",
        "vid": "pravo",
        "dostup": "companies.edit",
        "opisanie": "Изменить. Присланное пустым значит «сотри реквизит», а не «оставь как было»",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/companies/{id}/default",
        "vid": "pravo",
        "dostup": "companies.edit",
        "opisanie": "Сделать основной; отвечает всем списком",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "DELETE",
        "put": "/companies/{id}",
        "vid": "pravo",
        "dostup": "companies.delete",
        "opisanie": "Удалить",
        "podrazdel": "",
        "vne_api": false
      }
    ]
  },
  {
    "nazvanie": "Настройки сайта",
    "ruchki": [
      {
        "metod": "GET",
        "put": "/settings",
        "vid": "pravo",
        "dostup": "settings.view",
        "opisanie": "Все настройки: бренд, контакты, соцсети, язык витрины, валюта",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "PATCH",
        "put": "/settings",
        "vid": "pravo",
        "dostup": "settings.manage",
        "opisanie": "Обновить значения. `studio_site_url` — только `http(s)`, иначе `422 bad_site_url`",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/settings/maintenance",
        "vid": "pravo",
        "dostup": "settings.view",
        "opisanie": "Закрыт ли сайт на работы, с какой запиской и кем",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/settings/maintenance",
        "vid": "pravo",
        "dostup": "settings.manage",
        "opisanie": "Закрыть сайт на работы или открыть обратно: `{enabled, note}`",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/settings/logo",
        "vid": "pravo",
        "dostup": "settings.manage",
        "opisanie": "Загрузить логотип (multipart). Путь возвращается с меткой версии `?v=`",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "DELETE",
        "put": "/settings/logo",
        "vid": "pravo",
        "dostup": "settings.manage",
        "opisanie": "Убрать логотип",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/settings/site-logo",
        "vid": "pravo",
        "dostup": "settings.manage",
        "opisanie": "Логотип сайта для кнопки «Return to the site»",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/settings/site-logo/fetch",
        "vid": "pravo",
        "dostup": "settings.manage",
        "opisanie": "Попробовать достать логотип с сайта из настроек. Не вышло — `422 logo_fetch_failed`, и логотип грузят руками",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "DELETE",
        "put": "/settings/site-logo",
        "vid": "pravo",
        "dostup": "settings.manage",
        "opisanie": "Убрать логотип сайта",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/settings/og-image",
        "vid": "pravo",
        "dostup": "settings.manage",
        "opisanie": "Картинка для соцсетей по умолчанию",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "DELETE",
        "put": "/settings/og-image",
        "vid": "pravo",
        "dostup": "settings.manage",
        "opisanie": "Убрать её",
        "podrazdel": "",
        "vne_api": false
      }
    ]
  },
  {
    "nazvanie": "Журнал действий",
    "ruchki": [
      {
        "metod": "GET",
        "put": "/audit",
        "vid": "pravo",
        "dostup": "audit.view",
        "opisanie": "Журнал. Фильтры: `actor_id`, `entity_type`, `entity_id`, `source`, `action`, `search` (по имени человека и названию объекта), `since`, `until`; `page`/`per_page` (до 100)",
        "podrazdel": "",
        "vne_api": false
      }
    ]
  },
  {
    "nazvanie": "Почта (блок `mail`)",
    "ruchki": [
      {
        "metod": "GET",
        "put": "/mail/accounts",
        "vid": "pravo",
        "dostup": "settings.manage",
        "opisanie": "Ящики фирмы. **Пароля в ответе нет никогда** — только `has_password`",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/mail/accounts",
        "vid": "pravo",
        "dostup": "settings.manage",
        "opisanie": "Завести ящик: адрес, IMAP/SMTP, логин, `password` (уходит только внутрь)",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "PATCH",
        "put": "/mail/accounts/{id}",
        "vid": "pravo",
        "dostup": "settings.manage",
        "opisanie": "Изменить. Пустой `password` = «не менять», а не «стереть»",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "DELETE",
        "put": "/mail/accounts/{id}",
        "vid": "pravo",
        "dostup": "settings.manage",
        "opisanie": "Удалить ящик вместе с зеркалом его писем. Лента клиента остаётся",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/mail/accounts/{id}/check",
        "vid": "pravo",
        "dostup": "settings.manage",
        "opisanie": "Проверка входа по IMAP и SMTP. Ответ `{ok, error}` — ошибка текстом, без пароля",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/mail/senders",
        "vid": "pravo",
        "dostup": "mail.create",
        "opisanie": "Ящики, из которых можно писать: только `id`, `title`, `address`, только включённые",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/mail/accounts/{id}/sync",
        "vid": "pravo",
        "dostup": "mail.view",
        "opisanie": "Ручная синхронизация. Своего права не требует: это чтение почты фирмы, а не правка настроек — ждать управляющего ради свежих писем незачем",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/mail/messages",
        "vid": "pravo",
        "dostup": "mail.view",
        "opisanie": "Список: `account_id`, `client_id`, `deal_id`, `direction=in\\|out`, `unread`, `search`. Тела писем в списке не отдаются",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/mail/messages/{id}",
        "vid": "pravo",
        "dostup": "mail.view",
        "opisanie": "Письмо целиком, включая `body_text`/`body_html`",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/mail/messages/{id}/read",
        "vid": "pravo",
        "dostup": "mail.view",
        "opisanie": "`{\"is_read\": true\\|false}`",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/mail/send",
        "vid": "pravo",
        "dostup": "mail.create",
        "opisanie": "Отправить: `to[]`, `subject`, `body`, `account_id?`, `client_id?`, `deal_id?`, `reply_to_id?`",
        "podrazdel": "",
        "vne_api": false
      }
    ]
  },
  {
    "nazvanie": "Шаблоны сообщений (блок `templates`)",
    "ruchki": [
      {
        "metod": "GET",
        "put": "/templates/fields",
        "vid": "pravo",
        "dostup": "templates.view",
        "opisanie": "**Закрытый набор полей**: `{key, needs}`, где `needs` — `\"\"` / `client` / `deal`. Отдаётся с сервера, чтобы список не существовал во втором экземпляре во фронтенде",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/templates",
        "vid": "pravo",
        "dostup": "templates.view",
        "opisanie": "Список по алфавиту. `?channel=email` — годные для письма (включая `any`); незнакомый канал — `422 unknown_channel`, а не пустая выдача, неотличимая от «шаблонов нет»",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/templates/{id}",
        "vid": "pravo",
        "dostup": "templates.view",
        "opisanie": "Один шаблон",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/templates/{id}/render",
        "vid": "pravo",
        "dostup": "templates.view",
        "opisanie": "Готовый текст: `?client_id=`, `?deal_id=` (оба необязательны). Ответ — `{template_id, name, channel, text, missing[], unknown[]}`",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/templates",
        "vid": "pravo",
        "dostup": "templates.create",
        "opisanie": "Завести: `{name, body, channel?}`. Занятое название — `409 template_name_taken`",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "PATCH",
        "put": "/templates/{id}",
        "vid": "pravo",
        "dostup": "templates.edit",
        "opisanie": "Изменить любое из трёх полей",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "DELETE",
        "put": "/templates/{id}",
        "vid": "pravo",
        "dostup": "templates.delete",
        "opisanie": "Удалить. Уже отправленные сообщения не меняются: применённый шаблон давно стал самостоятельным письмом или записью ленты и связи с шаблоном не хранит",
        "podrazdel": "",
        "vne_api": false
      }
    ]
  },
  {
    "nazvanie": "Бланки и акты (блок `documents`)",
    "ruchki": [
      {
        "metod": "GET",
        "put": "/documents",
        "vid": "pravo",
        "dostup": "documents.view",
        "opisanie": "Список: `search`, `status`, `client_id`, `deal_id`, `kind` (повторяемый), `sort`, пагинация. В ответе сверх обычного — `counts`: сколько бумаг каждого вида",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/documents",
        "vid": "pravo",
        "dostup": "documents.create",
        "opisanie": "Завести бланк",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/documents/by-number/{number}",
        "vid": "pravo",
        "dostup": "documents.view",
        "opisanie": "Поиск сканом: сюда приходит то, что прочитал сканер штрихкода",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/documents/{id}",
        "vid": "pravo",
        "dostup": "documents.view",
        "opisanie": "Бланк + история состояний с именами авторов",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/documents/{id}/status",
        "vid": "pravo",
        "dostup": "documents.issue",
        "opisanie": "Сменить состояние: `{status, note}`",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/documents/{id}/print",
        "vid": "pravo",
        "dostup": "documents.view",
        "opisanie": "**HTML на печать**, а не JSON: две одинаковые половины с линией отреза, штрихкод и QR. `?locale=` переопределяет язык бумаги. Заказ и накладная — отказ (`document_is_an_order`, `document_is_a_waybill`): у них свои формы",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/documents/acts",
        "vid": "pravo",
        "dostup": "documents.create",
        "opisanie": "Завести акт выполненных работ",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/documents/acts/{id}",
        "vid": "pravo",
        "dostup": "documents.view",
        "opisanie": "Акт: позиции и история",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/documents/acts/{id}/lines",
        "vid": "pravo",
        "dostup": "documents.edit",
        "opisanie": "Добавить строку работ",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "DELETE",
        "put": "/documents/acts/{id}/lines/{line_id}",
        "vid": "pravo",
        "dostup": "documents.edit",
        "opisanie": "Убрать строку",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/documents/acts/{id}/complete",
        "vid": "pravo",
        "dostup": "documents.issue",
        "opisanie": "Провести акт",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/documents/acts/{id}/cancel",
        "vid": "pravo",
        "dostup": "documents.edit",
        "opisanie": "Отменить непроведённый. Склада и воронки не касается — их не трогали",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/documents/acts/{id}/print",
        "vid": "pravo",
        "dostup": "documents.view",
        "opisanie": "**HTML на печать**: перечень работ, итог и две подписи на одном листе",
        "podrazdel": "",
        "vne_api": false
      }
    ]
  },
  {
    "nazvanie": "Заказы (блок `orders`, по умолчанию выключен, зависит от `documents`)",
    "ruchki": [
      {
        "metod": "GET",
        "put": "/orders",
        "vid": "pravo",
        "dostup": "orders.view",
        "opisanie": "Список заказов: `search`, `kind`, `status`, `sort`, `client_id`, `deal_id`, пагинация. В ответе `counts` — сколько заказов в каждом состоянии",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/orders",
        "vid": "pravo",
        "dostup": "orders.create",
        "opisanie": "Завести",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/orders/{id}",
        "vid": "pravo",
        "dostup": "orders.view",
        "opisanie": "Заказ с позициями, выписанными по нему накладными (`waybills`, ключа нет вовсе при выключенном блоке) и историей переходов (`events`)",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/orders/{id}/lines",
        "vid": "pravo",
        "dostup": "orders.edit",
        "opisanie": "Добавить позицию",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "PATCH",
        "put": "/orders/{id}/lines/{line_id}",
        "vid": "pravo",
        "dostup": "orders.edit",
        "opisanie": "Изменить позицию",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "DELETE",
        "put": "/orders/{id}/lines/{line_id}",
        "vid": "pravo",
        "dostup": "orders.edit",
        "opisanie": "Убрать позицию",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/orders/{id}/pick",
        "vid": "pravo",
        "dostup": "orders.edit",
        "opisanie": "Отметить собранным по отсканированному коду: `{code, quantity_milli}`. Код, не найденный среди штрихкодов, пробуется как артикул (с 04.09.2026)",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/orders/{id}/ready",
        "vid": "pravo",
        "dostup": "orders.edit",
        "opisanie": "Заказ собран",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/orders/{id}/close",
        "vid": "pravo",
        "dostup": "orders.issue",
        "opisanie": "Провести: отгрузить покупателю или принять от поставщика",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/orders/{id}/revert",
        "vid": "pravo",
        "dostup": "orders.issue",
        "opisanie": "Отменить проведение обратными движениями. Прежние остаются на месте",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/orders/{id}/cancel",
        "vid": "pravo",
        "dostup": "orders.edit",
        "opisanie": "Отменить непроведённый. Резерв снимется сам — он не хранится",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/orders/{id}/deal",
        "vid": "pravo",
        "dostup": "orders.edit",
        "opisanie": "Прицепить заказ к заявке или отцепить (`deal_id: null`)",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/orders/{id}/print",
        "vid": "pravo",
        "dostup": "orders.view",
        "opisanie": "**HTML на печать**: таблица позиций и итог",
        "podrazdel": "",
        "vne_api": false
      }
    ]
  },
  {
    "nazvanie": "Накладные (блок `waybills`, по умолчанию выключен, зависит от `documents`)",
    "ruchki": [
      {
        "metod": "GET",
        "put": "/waybills",
        "vid": "pravo",
        "dostup": "waybills.view",
        "opisanie": "Список: `?search=`, `?kind=`, `?status=`, `?client_id=`, `?deal_id=`, `?basis_id=`, пагинация",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/waybills",
        "vid": "pravo",
        "dostup": "waybills.create",
        "opisanie": "Черновик: `kind` обязателен, дальше `client_id`, `deal_id`, `basis_id`, `warehouse_id`, `locale`, `note`",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/waybills/from-order/{order_id}",
        "vid": "pravo",
        "dostup": "waybills.create",
        "opisanie": "Черновик, заполненный позициями заказа",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/waybills/{id}",
        "vid": "pravo",
        "dostup": "waybills.view",
        "opisanie": "Накладная с позициями",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/waybills/{id}/reversals",
        "vid": "pravo",
        "dostup": "waybills.view",
        "opisanie": "Что выписано на основании этой: сторно",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/waybills/{id}/lines",
        "vid": "pravo",
        "dostup": "waybills.edit",
        "opisanie": "Добавить позицию: `product_id` либо `name`, `quantity`, `price`",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "PATCH",
        "put": "/waybills/{id}/lines/{line_id}",
        "vid": "pravo",
        "dostup": "waybills.edit",
        "opisanie": "Количество, цена; название — только у строки без товара",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "DELETE",
        "put": "/waybills/{id}/lines/{line_id}",
        "vid": "pravo",
        "dostup": "waybills.edit",
        "opisanie": "Убрать позицию",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/waybills/{id}/post",
        "vid": "pravo",
        "dostup": "waybills.issue",
        "opisanie": "Провести: товар уехал, остаток падает. `{confirm_negative: true}` — согласие отгрузить больше, чем лежит",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/waybills/{id}/confirm",
        "vid": "pravo",
        "dostup": "waybills.edit",
        "opisanie": "Получатель подтвердил приёмку. Склад не двигает",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/waybills/{id}/cancel",
        "vid": "pravo",
        "dostup": "waybills.edit",
        "opisanie": "Отменить черновик. Проведённую — нельзя, для неё сторно",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/waybills/{id}/reverse",
        "vid": "pravo",
        "dostup": "waybills.issue",
        "opisanie": "Сторнирующая накладная — черновиком, на основании этой",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/waybills/{id}/print",
        "vid": "pravo",
        "dostup": "waybills.view",
        "opisanie": "Печатная форма (HTML). `?locale=ru|en|uk` — язык бумаги",
        "podrazdel": "",
        "vne_api": false
      }
    ]
  },
  {
    "nazvanie": "Склад (блок `warehouse`, по умолчанию выключен)",
    "ruchki": [
      {
        "metod": "GET",
        "put": "/warehouse/products",
        "vid": "pravo",
        "dostup": "warehouse.view",
        "opisanie": "Список с остатками. `search`, `low_only`, `include_services`, `warehouse_id`, пагинация",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/warehouse/products",
        "vid": "pravo",
        "dostup": "warehouse.create",
        "opisanie": "Создать позицию. `sku` уникален, но необязателен (пусто → `NULL`); `409 sku_taken`",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/warehouse/products/{id}",
        "vid": "pravo",
        "dostup": "warehouse.view",
        "opisanie": "Карточка с остатком",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/warehouse/products/{id}/availability",
        "vid": "pravo",
        "dostup": "warehouse.view",
        "opisanie": "Остаток, бронь, ожидается, доступно и `holders` — кто держит",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "PATCH",
        "put": "/warehouse/products/{id}",
        "vid": "pravo",
        "dostup": "warehouse.edit",
        "opisanie": "Изменить. Товар с остатком нельзя сделать услугой (`422 product_has_stock`)",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "DELETE",
        "put": "/warehouse/products/{id}",
        "vid": "pravo",
        "dostup": "warehouse.delete",
        "opisanie": "Мягкое удаление. Движения остаются",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/warehouse/products/{id}/restore",
        "vid": "pravo",
        "dostup": "warehouse.restore",
        "opisanie": "Вернуть из корзины",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/warehouse/products/{id}/photos",
        "vid": "pravo",
        "dostup": "warehouse.view",
        "opisanie": "Снимки позиции в заданном порядке",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/warehouse/products/{id}/photos",
        "vid": "pravo",
        "dostup": "warehouse.edit",
        "opisanie": "Приложить снимок (форма, поле `file`). Тип по подписи файла, не по расширению; SVG не принимается",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/warehouse/products/{id}/photos/{photo_id}",
        "vid": "pravo",
        "dostup": "warehouse.view",
        "opisanie": "Отдать снимок. `?size=view` (по умолчанию) или `thumb`",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "DELETE",
        "put": "/warehouse/products/{id}/photos/{photo_id}",
        "vid": "pravo",
        "dostup": "warehouse.edit",
        "opisanie": "Убрать снимок вместе с файлами",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "PUT",
        "put": "/warehouse/products/{id}/photos/order",
        "vid": "pravo",
        "dostup": "warehouse.edit",
        "opisanie": "Задать порядок: `{\"order\": [id, …]}` целиком",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/warehouse/products/{id}/moves",
        "vid": "pravo",
        "dostup": "warehouse.view",
        "opisanie": "История по товару + `stock_milli` (агрегат, а не сумма страницы)",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/warehouse/moves",
        "vid": "pravo",
        "dostup": "warehouse.view",
        "opisanie": "Движения. Фильтры `product_id`, `deal_id`, `warehouse_id`; при `deal_id` в ответе `cost` — себестоимость списанного под заявку",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/warehouse/moves",
        "vid": "pravo",
        "dostup": "warehouse.create",
        "opisanie": "Записать движение. Виды: `in`, `out`, `writeoff`, `adjust`, `return`",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/warehouses",
        "vid": "pravo",
        "dostup": "warehouse.view",
        "opisanie": "Склады как места, одним `items` + признак `many`",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/warehouses",
        "vid": "pravo",
        "dostup": "warehouse.manage",
        "opisanie": "Завести склад",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "PATCH",
        "put": "/warehouses/{id}",
        "vid": "pravo",
        "dostup": "warehouse.manage",
        "opisanie": "Переименовать, сменить адрес, пометку",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "DELETE",
        "put": "/warehouses/{id}",
        "vid": "pravo",
        "dostup": "warehouse.manage",
        "opisanie": "Закрыть склад. Последний и непустой закрыть нельзя",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/warehouse/transfers",
        "vid": "pravo",
        "dostup": "warehouse.view",
        "opisanie": "Журнал переездов. Фильтры `warehouse_id`, `product_id`, пагинация",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/warehouse/transfers",
        "vid": "pravo",
        "dostup": "warehouse.create",
        "opisanie": "Перевезти товар с одного склада на другой",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/warehouse/transfers/{id}/revert",
        "vid": "pravo",
        "dostup": "warehouse.create",
        "opisanie": "Отменить переезд обратным переездом. Дважды один и тот же — отказ",
        "podrazdel": "",
        "vne_api": false
      }
    ]
  },
  {
    "nazvanie": "Наклейки (блок `labels`, по умолчанию выключен, зависит от `warehouse`)",
    "ruchki": [
      {
        "metod": "GET",
        "put": "/labels/products/{id}/barcodes",
        "vid": "pravo",
        "dostup": "labels.view",
        "opisanie": "Коды товара",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/labels/products/{id}/barcodes",
        "vid": "pravo",
        "dostup": "labels.create",
        "opisanie": "Привязать код: `{code, kind, pack_size_milli, is_primary}`",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/labels/products/{id}/barcodes/internal",
        "vid": "pravo",
        "dostup": "labels.create",
        "opisanie": "Выдать собственный код — тому, у чего заводского нет",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/labels/products/{id}/barcodes/{barcode_id}/primary",
        "vid": "pravo",
        "dostup": "labels.create",
        "opisanie": "Какой код печатать на наклейке",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "DELETE",
        "put": "/labels/products/{id}/barcodes/{barcode_id}",
        "vid": "pravo",
        "dostup": "labels.delete",
        "opisanie": "Отвязать код",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/labels/scan/{code}",
        "vid": "pravo",
        "dostup": "labels.view",
        "opisanie": "Товар по отсканированному коду. Не нашли — `404` с самим кодом внутри, чтобы экран сказал «код 20000127 не найден» и предложил завести товар прямо отсюда",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/labels/print",
        "vid": "pravo",
        "dostup": "labels.view",
        "opisanie": "**HTML на печать**: `?product_id=` списком, `copies`, `preview`, `locale`. Пустая пачка — `422 nothing_to_print`",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/labels/settings",
        "vid": "pravo",
        "dostup": "labels.view",
        "opisanie": "Размер наклейки и что на ней печатать",
        "podrazdel": "",
        "vne_api": false
      }
    ]
  },
  {
    "nazvanie": "Финансы (блок `finance`, по умолчанию выключен)",
    "ruchki": [
      {
        "metod": "GET",
        "put": "/finance/categories",
        "vid": "pravo",
        "dostup": "finance.view",
        "opisanie": "Справочник статей",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/finance/categories",
        "vid": "pravo",
        "dostup": "finance.manage",
        "opisanie": "Завести статью",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "PATCH",
        "put": "/finance/categories/{id}",
        "vid": "pravo",
        "dostup": "finance.manage",
        "opisanie": "Изменить (направление при правке не принимается)",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "DELETE",
        "put": "/finance/categories/{id}",
        "vid": "pravo",
        "dostup": "finance.manage",
        "opisanie": "Убрать статью",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/finance/operations",
        "vid": "pravo",
        "dostup": "finance.view",
        "opisanie": "Операции за период",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/finance/operations",
        "vid": "pravo",
        "dostup": "finance.create",
        "opisanie": "Записать операцию",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/finance/rules",
        "vid": "pravo",
        "dostup": "finance.view",
        "opisanie": "Правила разнесения",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/finance/rules",
        "vid": "pravo",
        "dostup": "finance.manage",
        "opisanie": "Завести правило",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "PATCH",
        "put": "/finance/rules/{id}",
        "vid": "pravo",
        "dostup": "finance.manage",
        "opisanie": "Изменить правило",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "DELETE",
        "put": "/finance/rules/{id}",
        "vid": "pravo",
        "dostup": "finance.manage",
        "opisanie": "Убрать правило",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/finance/payments",
        "vid": "pravo",
        "dostup": "finance.create",
        "opisanie": "Принять оплату или вернуть её — решает знак суммы",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "PATCH",
        "put": "/finance/accruals/{operation_id}",
        "vid": "pravo",
        "dostup": "finance.create",
        "opisanie": "Поправить сумму начисления: было 80, стало 140",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/finance/documents/{id}/money",
        "vid": "pravo",
        "dostup": "finance.view",
        "opisanie": "Деньги по бланку: получено, остаток, состояние, начисления",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/finance/deals/{id}/money",
        "vid": "pravo",
        "dostup": "finance.view",
        "opisanie": "Сколько получено по заявке",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/finance/profit",
        "vid": "pravo",
        "dostup": "finance.view",
        "opisanie": "Доход минус расход за период, с разбивкой по статьям",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/finance/budgets",
        "vid": "pravo",
        "dostup": "finance.view",
        "opisanie": "Планы и факт по ним",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/finance/budgets",
        "vid": "pravo",
        "dostup": "finance.manage",
        "opisanie": "Завести план",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "PATCH",
        "put": "/finance/budgets/{id}",
        "vid": "pravo",
        "dostup": "finance.manage",
        "opisanie": "Изменить план",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "DELETE",
        "put": "/finance/budgets/{id}",
        "vid": "pravo",
        "dostup": "finance.manage",
        "opisanie": "Убрать план",
        "podrazdel": "",
        "vne_api": false
      }
    ]
  },
  {
    "nazvanie": "Отчёты (блок `reports`)",
    "ruchki": [
      {
        "metod": "GET",
        "put": "/reports/funnel",
        "vid": "pravo",
        "dostup": "reports.view",
        "opisanie": "Воронка за период",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/reports/funnel.csv",
        "vid": "pravo",
        "dostup": "reports.view",
        "opisanie": "Она же выгрузкой",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/reports/revenue",
        "vid": "pravo",
        "dostup": "reports.view_amounts",
        "opisanie": "Деньги за период: `received_*` (пришло в кассу) и `won_*` (сумма выигранных заявок), плюс `basis` — чем меряем",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/reports/revenue.csv",
        "vid": "pravo",
        "dostup": "reports.view_amounts",
        "opisanie": "Она же выгрузкой",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/reports/sources",
        "vid": "pravo",
        "dostup": "reports.view",
        "opisanie": "Откуда пришли клиенты. Деньги в нём прячет `reports.view_amounts`",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/reports/sources.csv",
        "vid": "pravo",
        "dostup": "reports.view",
        "opisanie": "Он же выгрузкой",
        "podrazdel": "",
        "vne_api": false
      }
    ]
  },
  {
    "nazvanie": "Телефония (блок `telephony`)",
    "ruchki": [
      {
        "metod": "GET",
        "put": "/telephony/calls",
        "vid": "pravo",
        "dostup": "telephony.view",
        "opisanie": "Журнал: `?direction=in\\|out`, `?outcome=`, `?client_id=`, `?deal_id=`, `?user_id=`, `?number=` (приводится к нормализованному виду), `?since=`, `?until=`, пагинация. Незнакомое значение `direction`/`outcome` — `422`, а не пустая выдача, неотличимая от «звонков нет»",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/telephony/calls/{id}",
        "vid": "pravo",
        "dostup": "telephony.view",
        "opisanie": "Карточка звонка",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "PATCH",
        "put": "/telephony/calls/{id}",
        "vid": "pravo",
        "dostup": "telephony.edit",
        "opisanie": "Привязать разговор к заявке или отвязать: `{\"deal_id\": 7 \\| null}`. `422 deal_other_client` — заявка чужого клиента",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/telephony/calls/{id}/callback-task",
        "vid": "pravo",
        "dostup": "telephony.create",
        "opisanie": "Напоминание перезвонить по пропущенному. `422 call_not_missed`, `409 module_disabled` — блок напоминаний выключен",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/telephony/click-to-call",
        "vid": "pravo",
        "dostup": "telephony.create",
        "opisanie": "Просит АТС набрать: `{\"number\": \"…\", \"from_ext\": \"…\", \"deal_id\": 7}`. `422 telephony_not_configured`, `400 pbx_unavailable`, `409 pbx_call_id_taken` — станция выдала ключ, который уже занят прошлым разговором",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/telephony/settings",
        "vid": "pravo",
        "dostup": "settings.manage",
        "opisanie": "Настройки подключения. Секреты не отдаются — только `has_api_token` / `has_webhook_secret`",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "PATCH",
        "put": "/telephony/settings",
        "vid": "pravo",
        "dostup": "settings.manage",
        "opisanie": "Провайдер, адрес команды набора, токен, внутренний номер, смещение зоны АТС, код страны. `422 bad_telephony_url` — адрес станции не http(s) или слишком длинный",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/telephony/settings/secret",
        "vid": "pravo",
        "dostup": "settings.manage",
        "opisanie": "Новый секрет подписи вебхука; возвращается **один раз**",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/telephony/webhook",
        "vid": "otkryto",
        "dostup": "",
        "opisanie": "События звонка от АТС. Подпись обязательна (см. [07-security.md](07-security.md)). Отказы, в порядке проверок: `429 webhook_rate_limited` (600 в минуту с адреса, ДО сверки подписи), `401 webhook_not_configured` (секрет не задан — приём выключен), `401 bad_signature`, `422 bad_payload` (не JSON или не объект). Выключенный блок отвечает `200 {\"status\": \"ignored\", \"reason\": \"module_disabled\"}` — станции незачем повторять то, что мы не примем никогда",
        "podrazdel": "",
        "vne_api": false
      }
    ]
  },
  {
    "nazvanie": "Телеграм: переписка с клиентами (блок `telegram`)",
    "ruchki": [
      {
        "metod": "GET",
        "put": "/telegram/settings",
        "vid": "pravo",
        "dostup": "telegram.manage",
        "opisanie": "Состояние подключения. Токен НЕ отдаётся — только `token_tail` из четырёх знаков и признаки `configured` / `webhook_secret_set`. Чаты отдаются как есть: секрета в идентификаторе нет",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "PUT",
        "put": "/telegram/settings",
        "vid": "pravo",
        "dostup": "telegram.manage",
        "opisanie": "Токен, имя бота, чат утренней сводки (`digest_chat`), чат тревог о сервере (`alerts_chat`), `retention_months` — сколько месяцев хранить переписку (`0` — вечно, и это умолчание; числом, а не строкой). Пустой токен означает «не меняй»: экран настоящего не знает и вернуть не может. `422 bad_bot_token`, `422 bad_chat_id`",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "DELETE",
        "put": "/telegram/settings",
        "vid": "pravo",
        "dostup": "telegram.manage",
        "opisanie": "Отключить бота: снимает вебхук у телеграма и стирает токен с секретом. **Переписка остаётся** — отключение про связь, а не про данные",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/telegram/connect",
        "vid": "pravo",
        "dostup": "telegram.manage",
        "opisanie": "Сказать телеграму, куда доставлять (`setWebhook`). Отдельно от сохранения токена: адрес зависит от того, как сайт виден снаружи, и меняется при переезде. `422 telegram_needs_https` — телеграм принимает вебхук только по HTTPS",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/telegram/invite",
        "vid": "pravo",
        "dostup": "telegram.view",
        "opisanie": "Ссылка `t.me/имя?start=метка` и QR-код к ней. `?label=` — только буквы, цифры, дефис и подчёркивание (требование телеграма). `422 telegram_username_missing`",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/telegram/chats",
        "vid": "pravo",
        "dostup": "telegram.view",
        "opisanie": "Диалоги, свежие сверху: `?q=`, `?source=` (метка из ссылки, точным совпадением, а не подстрокой), пагинация. У каждого `unread` — **личный** счётчик непрочитанного, `has_avatar` (стоит ли просить картинку) и `is_premium`",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/telegram/sources",
        "vid": "pravo",
        "dostup": "telegram.view",
        "opisanie": "Какие метки источника встречаются и по сколько диалогов на каждой. Без него отбор по метке пришлось бы предлагать полем ввода, то есть предлагать угадывать",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "PATCH",
        "put": "/telegram/chats/{id}",
        "vid": "pravo",
        "dostup": "telegram.create",
        "opisanie": "Привязать диалог к карточке клиента или отвязать: `{\"client_id\": 7 \\| null}`. Только руками: автоматически связывается лишь точное совпадение номера",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/telegram/chats/{id}/deal",
        "vid": "pravo",
        "dostup": "deals.create",
        "opisanie": "Завести заявку по переписке. Диалог обязан быть привязан к карточке — `422 telegram_chat_not_linked`, заявка без клиента бессмысленна. Пустое название берётся из последнего входящего",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/telegram/chats/{id}/task",
        "vid": "pravo",
        "dostup": "tasks.create",
        "opisanie": "Завести напоминание по переписке. Привязки к карточке НЕ требует: «перезвонить этому человеку» осмысленно и до того, как выяснили, кто он. Привязан диалог — клиент подставится",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/telegram/chats/{id}/client",
        "vid": "pravo",
        "dostup": "clients.create",
        "opisanie": "Завести карточку клиента по переписке и сразу её привязать. Тела у запроса нет: всё нужное уже знает диалог, а форма посреди разговора означала бы, что кнопкой перестанут пользоваться. Ответ несёт `created` — карточку могли не завести, а привязать к найденной по точному совпадению номера",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/telegram/chats/{id}/client/refresh",
        "vid": "pravo",
        "dostup": "clients.edit",
        "opisanie": "Перенести в карточку то, что телеграм узнал о человеке позже. Заполняется **только пустое**. Ответ несёт `updated` — список перенесённых полей; пустой список законен и отказом не является",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/telegram/chats/{id}/messages",
        "vid": "pravo",
        "dostup": "telegram.view",
        "opisanie": "Лента. `?before=` — листание вглубь по идентификатору (не по смещению: на живой переписке оно врёт), `?after=` — дочитать пропущенное после обрыва. Показ ленты сдвигает границу «прочитано»",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/telegram/chats/{id}/messages",
        "vid": "pravo",
        "dostup": "telegram.create",
        "opisanie": "Ответить текстом: `{text, reply_to_id?}` — ответ на конкретное сообщение нашим номером строки, телеграмовы наружу не отдаются вовсе. Строка заводится ДО обращения к телеграму, поэтому двойное нажатие не отправляет дважды. Отказ телеграма не откатывает запись: `send_state=failed` и причина в `send_error`",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/telegram/chats/{id}/files",
        "vid": "pravo",
        "dostup": "telegram.create",
        "opisanie": "Ответить файлом: картинка, видео или документ. Форма, а не JSON: `file`, `caption`, `reply_to_id` (пустая строка — «без привязки»: `null` в форму не положить)",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/telegram/chats/{id}/messages/{msg}/file",
        "vid": "pravo",
        "dostup": "telegram.view",
        "opisanie": "Отдать вложение. Через приложение, а не статикой: переписка с клиентом не публичная картинка, и ссылка не должна работать у всякого, кто её узнал",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/telegram/chats/{id}/messages/{msg}/fetch",
        "vid": "pravo",
        "dostup": "telegram.view",
        "opisanie": "Забрать видео, которое сразу не тянули. Идемпотентно",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/telegram/chats/{id}/read",
        "vid": "pravo",
        "dostup": "telegram.view",
        "opisanie": "«Я это увидел»: сдвигает личную границу прочитанного. Нужна потому, что дочитывание (`?after=`) границу НЕ двигает — оно приносит сообщения и в свёрнутую вкладку. Различает эти два случая только браузер",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/telegram/chats/{id}/avatar",
        "vid": "pravo",
        "dostup": "telegram.view",
        "opisanie": "Фотография профиля собеседника. Освежается по требованию, но не чаще раза в сутки: аватар не приходит с сообщением, за ним надо ходить отдельно. `404 telegram_no_avatar` — обычное дело, а не отказ: экран рисует инициалы",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/telegram/chats/{id}/emoji",
        "vid": "pravo",
        "dostup": "telegram.view",
        "opisanie": "Именной эмодзи собеседника — статичной картинкой: сам эмодзи Lottie, и браузер его не рисует. Забирается при приёме сообщения, не чаще раза в сутки и только у премиума. `404 telegram_no_emoji` — как и у аватара, обычное дело, а не отказ",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/telegram/chats/{id}/presence",
        "vid": "pravo",
        "dostup": "telegram.view",
        "opisanie": "«Я в этом чате» / «ушёл». Отвечает списком смотрящих. Ключ со сроком годности в Redis — уборки не требует",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/telegram/stream",
        "vid": "pravo",
        "dostup": "telegram.view",
        "opisanie": "Поток событий (SSE): новые сообщения и смена присутствия. Соединение живёт пять минут, дальше браузер переподключается сам. Первым сообщением — `{\"type\": \"ready\", \"bus\": …}`; ложь означает недоступный Redis, то есть событий не будет ни одного при живом соединении, и экран переходит на запасное перечитывание",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/telegram/digest/send",
        "vid": "pravo",
        "dostup": "telegram.manage",
        "opisanie": "Отправить утреннюю сводку прямо сейчас — посмотреть оформление, не дожидаясь утра. Отвечает состоянием, а не отказом: `{\"status\": \"sent\"}`, `{\"status\": \"skipped\", \"reason\": …}` (бот не настроен или блок выключен), `{\"status\": \"failed\", \"error\": …}` — причина от телеграма дословно. Ненастроенный канал не отказ ручки: экран могли открыть раньше, чем ввели токен",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/telegram/webhook",
        "vid": "otkryto",
        "dostup": "",
        "opisanie": "Обновление от телеграма. Секрет заголовком `X-Telegram-Bot-Api-Secret-Token`; сверка постоянным временем, ограничитель ДО сверки. Отказы: `422 telegram_not_configured` (бот не подключён), `429 webhook_flooded` (600 в минуту с адреса), `401 bad_webhook_secret`. `503 limiter_unavailable` — Redis не отвечает; телеграм повторит доставку, и это правильно",
        "podrazdel": "",
        "vne_api": false
      }
    ]
  },
  {
    "nazvanie": "Заявки с сайта",
    "ruchki": [
      {
        "metod": "POST",
        "put": "/public/leads",
        "vid": "otkryto",
        "dostup": "",
        "opisanie": "Заявка с формы на сайте. Ключ приёма — в заголовке `X-OpenCRM-Intake-Key`. Ответ на принятую заявку всегда один — `202 {\"status\": \"accepted\"}`: ни «завели», ни «узнали своего», ни ловушка не отличимы снаружи. Отказы, в порядке проверок: `429 lead_rate_limited` (30 обращений за 10 минут с адреса — считается ДО ключа, иначе подбирающий ключ не считался бы), `401 bad_intake_key` (ключа нет или он не тот — один ответ на оба случая), `422 contact_required` (ни почты, ни телефона), `422 bad_email` / `email_too_long` / `phone_too_long` (адрес и номер не обрезаются, а отвергаются: обрезанный адрес — другой адрес), `429 lead_intake_flooded` (потолок новых карточек в час), `409 no_responsible` (заявку некому отдать: ответственный не назван, а root удалён). `503 limiter_unavailable` — Redis не отвечает, и ограничитель отказывает, а не пропускает",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/leads/settings",
        "vid": "pravo",
        "dostup": "settings.manage",
        "opisanie": "Состояние приёма. Ключ **не отдаётся**: только `has_intake_key`",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/leads/settings/key",
        "vid": "pravo",
        "dostup": "settings.manage",
        "opisanie": "Новый ключ. Возвращается **один раз**: `201 {\"key\": \"…\"}`. Прежний перестаёт работать в ту же секунду",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "DELETE",
        "put": "/leads/settings/key",
        "vid": "pravo",
        "dostup": "settings.manage",
        "opisanie": "Закрыть приём: без ключа публичная ручка отвечает как несуществующая",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "PATCH",
        "put": "/leads/settings/manager",
        "vid": "pravo",
        "dostup": "settings.manage",
        "opisanie": "Кому достаются заявки. `{\"manager_id\": null}` — владельцу системы. `404 manager_not_found` — такого сотрудника нет",
        "podrazdel": "",
        "vne_api": false
      }
    ]
  },
  {
    "nazvanie": "API сайта магазина (`/site/*`, по ключу)",
    "ruchki": [
      {
        "metod": "GET",
        "put": "/site/catalog",
        "vid": "klyuch",
        "dostup": "catalog.read",
        "opisanie": "Страница карточек, `?page=&per_page=` (до 200). Виден товар, по складу ключа было хоть одно движение, и все услуги; ключ без склада — одни услуги",
        "podrazdel": "Чтение",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/site/catalog/{product_id}",
        "vid": "klyuch",
        "dostup": "catalog.read",
        "opisanie": "Одна карточка; `404 product_not_found` — не опубликован или удалён",
        "podrazdel": "Чтение",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/site/changes",
        "vid": "klyuch",
        "dostup": "catalog.read",
        "opisanie": "Лента изменений: `?since=<курсор>&limit=<до 200>`. Без `since` — полная выгрузка",
        "podrazdel": "Чтение",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/site/stock",
        "vid": "klyuch",
        "dostup": "stock.read",
        "opisanie": "Наличие: `?id=17,42` либо `?sku=A,B`, до 200 позиций (`422 too_many_ids`)",
        "podrazdel": "Чтение",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/site/orders",
        "vid": "klyuch",
        "dostup": "orders.write",
        "opisanie": "Заказ со сроком брони. `201` — новый, `200` — повтор с тем же `site_ref` (тот же заказ, второй не заводится)",
        "podrazdel": "Запись",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/site/orders/{site_ref}",
        "vid": "klyuch",
        "dostup": "orders.read",
        "opisanie": "Свой заказ по чужому номеру; чужой или несуществующий — `404 order_not_found`",
        "podrazdel": "Запись",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/site/orders/{site_ref}/cancel",
        "vid": "klyuch",
        "dostup": "orders.write",
        "opisanie": "Снять бронь: заказ уходит в `cancelled`, товар свободен. После накладной — `409 order_already_fulfilled`",
        "podrazdel": "Запись",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/site/customers",
        "vid": "klyuch",
        "dostup": "customers.write",
        "opisanie": "Завести карточку клиента или узнать свою; ответ всегда одной формы, `202`",
        "podrazdel": "Запись",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/site/leads",
        "vid": "klyuch",
        "dostup": "leads.write",
        "opisanie": "Та же заявка, что `/public/leads`, только по ключу сайта; `202 {\"status\": \"accepted\"}`",
        "podrazdel": "Запись",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/media/product/{filename}",
        "vid": "inoe",
        "dostup": "uid 128 бит; блок `warehouse` включён; товар опубликован",
        "opisanie": "`<uid>.webp` и `<uid>-thumb.webp` из карточки; `Cache-Control: public, max-age=31536000, immutable`, `X-Content-Type-Options: nosniff`. Неопубликованный товар — `404`, а не «есть, но не покажем»",
        "podrazdel": "Снимок товара",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/settings/api-keys",
        "vid": "pravo",
        "dostup": "settings.manage",
        "opisanie": "Все ключи, включая отозванные и истёкшие; `alive` — сколько живых («наружу открыто: N ключей» на экране), словари `scopes`, `stock_modes`, имя заголовка",
        "podrazdel": "Ключи API сайта (настройки)",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/settings/api-keys",
        "vid": "pravo",
        "dostup": "settings.manage",
        "opisanie": "Выдать: `name`, `scopes[]`, `warehouse_id` (обязателен при `stock.read`, склад типа `shop`), `days` (365; 0 — бессрочный), `stock_mode`, `few_threshold_milli`, `rate_per_min`, `max_reserve_minutes`, `ttl_sec`. Ответ `201` содержит `key` — **один раз**. `422 unknown_scope` / `scope_required` / `warehouse_required` / `warehouse_not_shop` / `unknown_stock_mode`",
        "podrazdel": "Ключи API сайта (настройки)",
        "vne_api": false
      },
      {
        "metod": "PATCH",
        "put": "/settings/api-keys/{key_id}",
        "vid": "pravo",
        "dostup": "settings.manage",
        "opisanie": "Имя, режим наличия, порог и потолки. Области и склад не правятся — на них выпускают новый ключ",
        "podrazdel": "Ключи API сайта (настройки)",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/settings/api-keys/{key_id}/revoke",
        "vid": "pravo",
        "dostup": "settings.manage",
        "opisanie": "Отзыв отметкой; строка остаётся",
        "podrazdel": "Ключи API сайта (настройки)",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/settings/api-keys/{key_id}/rotate",
        "vid": "pravo",
        "dostup": "settings.manage",
        "opisanie": "Новый ключ с теми же полями (`201`, `key` один раз); старый живёт ещё `grace_hours` (24). `409 api_key_revoked` — отозванный не ротируется",
        "podrazdel": "Ключи API сайта (настройки)",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/warehouses/{warehouse_id}/site",
        "vid": "pravo",
        "dostup": "warehouse.view",
        "opisanie": "Сколько карточек этого склада на сайте и сколько без цены: `{published, without_price}`. Отвечает и на «что случится, если сменить тип» ДО нажатия, и на строку экрана «На сайте: 132 позиции, 4 без цены»",
        "podrazdel": "Что изменилось в соседних ручках",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/live",
        "vid": "sotrudnik",
        "dostup": "",
        "opisanie": "Поток намёков «перечитай» (SSE) для вкладки сотрудника. Первое сообщение — `resync` (и когда догнать нечем), дальше `change` с `id:` номера потока (он же `Last-Event-ID` при переподключении), `mode: off` при выключенной настройке `realtime_enabled` (`reason: disabled`) и лежащем Redis (`bus_unavailable`). Права и живость сессии — на каждое сообщение; соединение живёт не дольше пяти минут. Разбор — [12-realtime.md](12-realtime.md)",
        "podrazdel": "Живые обновления",
        "vne_api": false
      }
    ]
  },
  {
    "nazvanie": "Служебные ручки без сессии",
    "ruchki": [
      {
        "metod": "GET",
        "put": "/metrics",
        "vid": "inoe",
        "dostup": "границей сети",
        "opisanie": "Метрики приложения текстом в формате Prometheus. Блоком `monitoring` закрыт тоже — но снаружи его не пускает nginx: `location = /api/v1/metrics { deny all; }` и он же префиксом",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/alerts/webhook",
        "vid": "inoe",
        "dostup": "проверкой, что запрос не шёл через nginx",
        "opisanie": "Доставка от Alertmanager: тревоги, зажёгшиеся или погасшие. Отказы: `403 alerts_external` (пришёл снаружи, через nginx), `403 alerts_bad_key` (задан `OPENCRM_ALERTS_SECRET`, а заголовок `X-OpenCRM-Alerts-Key` не сошёлся), `429 alerts_flooded` (100 в минуту). Неразбор тела — `200`, преходящая беда — `503`: разбор ниже",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/alerts/ready",
        "vid": "inoe",
        "dostup": "тем же",
        "opisanie": "Готова ли CRM принимать тревоги. Ответ «нет» — рабочее состояние, а не отказ",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/arcade/leaderboard",
        "vid": "inoe",
        "dostup": "ничем — данных фирмы не касается",
        "opisanie": "Таблица результатов змейки со страницы обслуживания",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/arcade/scores",
        "vid": "inoe",
        "dostup": "тем же",
        "opisanie": "Записать счёт",
        "podrazdel": "",
        "vne_api": false
      }
    ]
  },
  {
    "nazvanie": "Публичные маршруты (витрина, вне /api)",
    "ruchki": [
      {
        "metod": "GET",
        "put": "/b/{token}",
        "vid": "otkryto",
        "dostup": "",
        "opisanie": "Страница витрины. SSR: OG-теги, первые работы, blurhash. Если PIN — форма PIN. Если недоступна — страница «доступ закрыт» (единый вид для отозванной/истёкшей/неопубликованной/несуществующей — не раскрываем причину)",
        "podrazdel": "",
        "vne_api": true
      },
      {
        "metod": "POST",
        "put": "/b/{token}/pin",
        "vid": "otkryto",
        "dostup": "",
        "opisanie": "Проверка PIN. Успех → подписанная cookie доступа на этот токен (живёт сессию браузера). Перебор ограничен (см. [07-security.md](07-security.md))",
        "podrazdel": "",
        "vne_api": true
      },
      {
        "metod": "GET",
        "put": "/b/{token}/data",
        "vid": "otkryto",
        "dostup": "",
        "opisanie": "JSON с работами для гидрации/дозагрузки (те же проверки доступа)",
        "podrazdel": "",
        "vne_api": true
      },
      {
        "metod": "GET",
        "put": "/media/{work_uid}/{filename}",
        "vid": "otkryto",
        "dostup": "",
        "opisanie": "Файлы работ. **Отдаёт приложение**, nginx только проксирует",
        "podrazdel": "",
        "vne_api": true
      },
      {
        "metod": "GET",
        "put": "/branding/{filename}",
        "vid": "otkryto",
        "dostup": "",
        "opisanie": "Логотипы и картинка для соцсетей. Их nginx раздаёт с диска (`/srv/storage/branding/`)",
        "podrazdel": "",
        "vne_api": true
      },
      {
        "metod": "GET",
        "put": "/avatars/{filename}",
        "vid": "otkryto",
        "dostup": "",
        "opisanie": "Аватары сотрудников, так же с диска",
        "podrazdel": "",
        "vne_api": true
      },
      {
        "metod": "GET",
        "put": "/d/{number}",
        "vid": "otkryto",
        "dostup": "",
        "opisanie": "Состояние заказа по QR с квитанции, без входа в систему",
        "podrazdel": "",
        "vne_api": true
      }
    ]
  }
];
