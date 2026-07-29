# Общая часть для HTTP- и HTTPS-варианта: раздача файлов и проксирование в uvicorn.
# Файл подключается через include, поэтому envsubst его не трогает — переменные
# здесь принадлежат nginx ($host, $uri), а не шаблонизатору.

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
    proxy_pass http://app:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 120s;
}
