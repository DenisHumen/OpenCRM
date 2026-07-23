# 08 — Деплой

Целевая среда: **Linux VPS + Docker Compose**. Один сервер, на нём же хранятся файлы работ.

## Состав (файлы в репозитории)

```
docker/
├── Dockerfile            # multi-stage: node собирает фронтенд → python:3.12-slim + ffmpeg
├── entrypoint.sh         # страховочная копия SQLite → alembic upgrade head → uvicorn
├── docker-compose.yml    # app (healthcheck на /healthz) + nginx, тома data/storage
└── nginx/
    └── opencrm.conf      # /media и /branding напрямую с тома, блок оригиналов, лимит тела 220m
scripts/
├── backup.sh             # ежедневный бэкап: SQLite .backup + tar storage, ротация 7д/4нед
├── restore.sh            # восстановление (текущая БД откладывается в сторону, не затирается)
├── purge_deleted.py      # окончательная очистка мягко удалённого (карантин 30 дней, --dry-run)
├── reset_root.py         # смена email/пароля root, восстановление доступа
└── migrate_to_mysql.py   # перенос данных при переезде на MySQL
```

Перед первым запуском заполните `config/.env` по шаблону `config/.env.example`: в `production` приложение остановится, если `OPENCRM_SECRET_KEY` или `OPENCRM_IP_HASH_SALT` пусты (подробности — [07-security.md](07-security.md)).

Когда переедем на MySQL — добавится сервис `mysql` с собственным томом; для приложения изменится только `OPENCRM_DB_URL` (данные переносит `scripts/migrate_to_mysql.py`, порядок описан в [03-database.md](03-database.md)).

## Разделение ответственности nginx / app

| Путь | Кто отвечает |
|---|---|
| `/media/*` | nginx напрямую с тома (кэш-заголовки `immutable`, файлы никогда не меняются — только создаются/удаляются) |
| `/assets/*` (JS/CSS фронтенда) | nginx, из собранного бандла |
| `/api/*`, `/b/*`, всё остальное | proxy_pass → uvicorn |
| Внутренние файлы клиентов | app проверяет сессию → `X-Accel-Redirect` → nginx отдаёт файл |

Лимит тела запроса в nginx = `OPENCRM_MAX_UPLOAD_MB` + запас.

### Определение IP клиента за прокси

Реальный IP клиента используется для rate-limit подбора PIN и хэша IP в журнале просмотров.
`X-Forwarded-For` клиент может подделать, поэтому:

- **`OPENCRM_TRUSTED_PROXY_HOPS=1`** (в `docker-compose.yml`): за приложением один nginx,
  и реальный адрес — последний элемент `X-Forwarded-For`, дописанный им (`$proxy_add_x_forwarded_for`).
  Левые элементы шлёт клиент — им не верим. Прямой запуск без nginx — `0` (заголовок игнорируется).
- **uvicorn с `--no-proxy-headers`** (в `entrypoint.sh`): иначе uvicorn сам перепишет
  `request.client` по `X-Forwarded-For` ещё до приложения, и подделка снова пройдёт.
  Определение IP целиком за приложением (`core client_ip`).
- **`OPENCRM_WORKERS=1`** (по умолчанию): rate-limit хранится в памяти процесса. При нескольких
  воркерах порог фактически умножается на их число — до выноса лимитера в БД/Redis держите один воркер.

Без этой связки ротацией `X-Forwarded-For` можно было бы обойти лимит и перебрать PIN доски.

## Развёртывание с нуля

```bash
git clone <repo> && cd OpenCRM
cp config/.env.example config/.env   # заполнить: секреты, домен, root-креды
docker compose -f docker/docker-compose.yml up -d --build
# entrypoint сам прогоняет alembic upgrade head; root создаётся на первом старте
```

Обновление: `git pull && docker compose -f docker/docker-compose.yml up -d --build` — миграции применяются на старте автоматически (перед ними entrypoint снимает копию файла SQLite `*.pre-migrate`).

HTTPS: выпустить сертификат certbot'ом, добавить в `nginx/opencrm.conf` server-блок на 443 с теми же location + редирект 80 → 443 + HSTS, раскомментировать порт 443 и том сертификатов в compose.

## Бэкапы и обслуживание

Cron на хосте (пример):

```
0 3 * * *  docker compose -f /opt/OpenCRM/docker/docker-compose.yml exec -T app sh scripts/backup.sh
30 3 * * * docker compose -f /opt/OpenCRM/docker/docker-compose.yml exec -T app python scripts/purge_deleted.py
```

1. `backup.sh`: консистентная копия SQLite (`sqlite3 .backup`) + tar всего `storage/`; ротация 7 ежедневных + 4 еженедельных.
2. Копия на внешнее хранилище — заготовка в скрипте (age + rclone), включить при наличии стораджа.
3. `restore.sh <db> <storage.tar.gz>`: восстанавливает, откладывая текущую БД в `*.before-restore-*`. Отрепетировать на тестовой копии после первого деплоя.
4. `purge_deleted.py`: спустя 30 дней карантина безвозвратно удаляет мягко удалённые доски/клиентов вместе с файлами и чистит истёкшие сессии; `--dry-run` показывает план.

## Мониторинг (минимум)

- `GET /healthz` — проверка приложения и доступности БД; внешний uptime-мониторинг (UptimeRobot или аналог) на него.
- Логи — в stdout контейнеров (`docker logs`), ротация средствами Docker (`max-size`).

### Свободное место

Приложение следит за диском само — отдельный cron-скрипт не нужен:

- `GET /api/v1/system/storage` — занято/свободно, размер `storage/`, объём корзины, уровень (`ok` / `warning` / `critical`).
- Данные берутся из `shutil.disk_usage` (POSIX `statvfs`), **прав root не требуется**: поле `free` — это `f_bavail`, то есть место, доступное непривилегированному процессу вроде `www-data`. Работает одинаково на Ubuntu и Fedora, внешних утилит не нужно.
- Сотрудники видят баннер в сайдбаре и карточку «Хранилище» на дашборде; root дополнительно получает раздел «Обслуживание» в настройках сайта с кнопкой очистки корзины.
- При нехватке места загрузка файлов блокируется с ошибкой `disk_full` — это защищает от ситуации, когда диск забивается полностью и повреждается база.

Пороги (в `config/.env`):

| Переменная | По умолчанию | Смысл |
|---|---|---|
| `OPENCRM_DISK_WARNING_PERCENT` | 80 | янтарный баннер «место кончается» |
| `OPENCRM_DISK_CRITICAL_PERCENT` | 90 | красный баннер |
| `OPENCRM_DISK_MIN_FREE_MB` | 1024 | аварийный запас: ниже него загрузка блокируется |

Место освобождается двумя путями: `scripts/purge_deleted.py` по cron (карантин 30 дней) и кнопкой «Очистить корзину» в настройках — обе используют один код (`core/services/maintenance_service.py`).

## Окружения

| Окружение | Где | БД | Особенности |
|---|---|---|---|
| dev | локально, `uvicorn --reload` + `vite dev` | SQLite-файл в репо-каталоге `data/` (gitignore) | без nginx, медиа отдаёт FastAPI |
| production | VPS, Docker Compose | SQLite на томе → MySQL | nginx, HTTPS, бэкапы, мониторинг |
