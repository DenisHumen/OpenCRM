# Общая часть для HTTP- и HTTPS-варианта: раздача файлов и проксирование в uvicorn.
# Файл подключается через include, поэтому envsubst его не трогает — переменные
# здесь принадлежат nginx ($host, $uri), а не шаблонизатору.

# Встроенный DNS Docker. Без него имя `app` резолвится один раз, при разборе
# конфига, и запомненный адрес живёт до перезапуска nginx. Обновление же
# пересоздаёт контейнер приложения, и адрес в сети compose ему достаётся не тот
# же самый — если освободившийся успел занять кто-то другой. Проверено на
# стенде: приложение здорово и отвечает, а сайт отдаёт 502, потому что nginx
# всё ещё стучится по старому адресу. Само оно не чинится: nginx не падает, и
# `restart: unless-stopped` его не трогает.
#
# Резолвер работает только вместе с переменной в proxy_pass: имя, записанное
# литералом, nginx резолвит на этапе конфига и до резолвера дело не доходит.
resolver 127.0.0.11 valid=10s ipv6=off;

# медиа витрин — напрямую с диска, файлы неизменяемые
location /media/ {
    alias /srv/storage/media/;
    add_header Cache-Control "public, max-age=31536000, immutable";
    add_header X-Content-Type-Options nosniff always;

    # оригиналы наружу не отдаются — только производные (large/card/thumb/poster/video)
    location ~ /original\.[a-z0-9]+$ {
        return 404;
    }
}

location /branding/ {
    alias /srv/storage/branding/;
    add_header Cache-Control "public, max-age=3600";
}

location /avatars/ {
    alias /srv/storage/avatars/;
    add_header Cache-Control "public, max-age=86400";
    add_header X-Content-Type-Options "nosniff";
}

# Шрифты витрины лежат внутри образа, их отдаёт приложение — оно же ставит
# Cache-Control. Своего add_header тут нет намеренно: два одинаковых заголовка
# в ответе ничего не улучшают, а расходятся при первой же правке одного из них.

# внутренние файлы клиентов app отдаёт сам после проверки сессии

location / {
    set $opencrm_app app;
    proxy_pass http://$opencrm_app:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 120s;

    # Пока приложение не отвечает — обновление, миграции, перезапуск — вместо
    # nginx'ового «502 Bad Gateway» показываем страницу обслуживания.
    #
    # Именно 503, а не 502: это ответ «временно недоступен, зайдите позже», и
    # поисковики по нему держат страницы в индексе, а не выбрасывают их как
    # сломанные. Retry-After говорит, через сколько возвращаться.
    #
    # Ловятся только ошибки, которые породил сам nginx, не дозвонившись до
    # приложения. Ответы самого приложения (его 404, 500) проходят как есть:
    # proxy_intercept_errors по умолчанию выключен, и включать его нельзя —
    # иначе страница обслуживания подменяла бы настоящие ошибки сайта.
    error_page 502 503 504 =503 /__opencrm-maintenance;
}

location = /__opencrm-maintenance {
    internal;
    alias /opencrm/maintenance/maintenance.html;
    # Тип берётся из расширения в АДРЕСЕ, а не в имени файла на диске, и у этого
    # адреса расширения нет — без явного указания ответ уходил бы с
    # application/octet-stream, и браузер вместо страницы предлагал бы скачать
    # файл. Заодно charset: на странице есть кириллица.
    default_type text/html;
    charset utf-8;
    add_header Retry-After 30 always;
    add_header Cache-Control "no-store" always;
    add_header X-Content-Type-Options nosniff always;
}
