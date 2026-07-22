# 08 — Деплой

Целевая среда: **Linux VPS + Docker Compose**. Один сервер, на нём же хранятся файлы работ.

## Состав

```
docker/
├── Dockerfile            # multi-stage: сборка фронтенда (node) → python-образ
├── docker-compose.yml
└── nginx/
    └── opencrm.conf
```

```yaml
# docker-compose.yml (схема)
services:
  app:
    build: {context: .., dockerfile: docker/Dockerfile}
    env_file: ../config/.env
    volumes:
      - opencrm_data:/app/data        # SQLite-база
      - opencrm_storage:/app/storage  # файлы работ и клиентов
    expose: ["8000"]
    restart: unless-stopped

  nginx:
    image: nginx:alpine
    ports: ["80:80", "443:443"]
    volumes:
      - ./nginx/opencrm.conf:/etc/nginx/conf.d/default.conf:ro
      - opencrm_storage:/srv/storage:ro   # прямая отдача /media
      - certbot_certs:/etc/letsencrypt:ro
    depends_on: [app]
    restart: unless-stopped

volumes:
  opencrm_data:
  opencrm_storage:
  certbot_certs:
```

Когда переедем на MySQL — добавится сервис `mysql` с собственным томом; для приложения изменится только `OPENCRM_DB_URL`.

## Разделение ответственности nginx / app

| Путь | Кто отвечает |
|---|---|
| `/media/*` | nginx напрямую с тома (кэш-заголовки `immutable`, файлы никогда не меняются — только создаются/удаляются) |
| `/assets/*` (JS/CSS фронтенда) | nginx, из собранного бандла |
| `/api/*`, `/b/*`, всё остальное | proxy_pass → uvicorn |
| Внутренние файлы клиентов | app проверяет сессию → `X-Accel-Redirect` → nginx отдаёт файл |

Лимит тела запроса в nginx = `OPENCRM_MAX_UPLOAD_MB` + запас.

## Развёртывание с нуля

```bash
git clone <repo> && cd OpenCRM
cp config/.env.example config/.env   # заполнить: секреты, домен, root-креды
docker compose -f docker/docker-compose.yml up -d --build
# первый запуск сам прогоняет alembic upgrade head и bootstrap root-аккаунта
```

Обновление: `git pull && docker compose up -d --build` — миграции применяются на старте автоматически (для SQLite перед этим снимается автоматическая копия файла БД).

## Бэкапы

Скрипт `scripts/backup.sh` по cron (ежедневно, ночью):

1. SQLite: `sqlite3 opencrm.db ".backup backup-YYYY-MM-DD.db"` — консистентная копия на горячую.
2. `storage/` — инкрементально (rsync/tar с датой).
3. Хранение: 7 ежедневных + 4 еженедельных, старые удаляются.
4. Копия на внешнее хранилище (S3-совместимое / другой сервер) — настраивается при наличии; при выгрузке наружу архив шифруется (age/gpg).
5. Восстановление отрепетировано и описано в самом скрипте (`restore.sh`).

## Мониторинг (минимум)

- `GET /healthz` — проверка приложения и доступности БД; внешний uptime-мониторинг (UptimeRobot или аналог) на него.
- Логи — в stdout контейнеров (`docker logs`), ротация средствами Docker (`max-size`).
- Диск: файлы работ растут — алерт при заполнении > 80% (простой cron-скрипт).

## Окружения

| Окружение | Где | БД | Особенности |
|---|---|---|---|
| dev | локально, `uvicorn --reload` + `vite dev` | SQLite-файл в репо-каталоге `data/` (gitignore) | без nginx, медиа отдаёт FastAPI |
| production | VPS, Docker Compose | SQLite на томе → MySQL | nginx, HTTPS, бэкапы, мониторинг |
