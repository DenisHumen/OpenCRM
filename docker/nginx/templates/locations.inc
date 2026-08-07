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

# Журнал доступа в формате, который умеет разбирать promtail (сам формат — в
# logging.inc, он подключён уровнем выше). Отсюда берутся доли ответов 4xx/5xx,
# RPS и время ответа: **только nginx видит 502**. Приложение о них не знает по
# определению — в этот момент его нет.
access_log /var/log/nginx/access.log opencrm_json;

# Медиа витрин отдаёт ПРИЛОЖЕНИЕ, а не nginx.
#
# Раньше nginx раздавал их прямо с диска, и файл не закрывался ничем. Проверено
# прогоном: клиент один раз открыл витрину и запомнил адреса картинок; после
# смены PIN, отзыва ссылки, истечения срока, снятия доски с публикации и даже
# удаления ссылки страница отвечала 401/404, а файлы — 200, причём анониму и
# вовсе без PIN. Витрина закрыта, работы открыты — то есть отзыв ссылки не
# отзывал ничего. Оригиналы при этом закрывал этот же файл, отдельным правилом;
# теперь их (и всё остальное) закрывает приложение — одним списком разрешённых
# имён, который лежит рядом с кодом, а не в двух местах сразу.
#
# Быстрее было бы `X-Accel-Redirect`: приложение проверяет доступ, байты отдаёт
# nginx. Не сделано нарочно. Такой ответ живёт ровно до тех пор, пока внутренний
# адрес в этом файле совпадает с заголовком в коде, а файлы nginx примонтированы
# из чекаута и меняются в момент `git checkout` — то есть раньше, чем
# пересоберётся контейнер приложения. В это окно новое приложение встречает
# старый конфиг, `X-Accel-Redirect` уходит в никуда, и на витринах у всех
# клиентов пропадают картинки. Приём стоит взять, когда нагрузка этого
# потребует, но тогда — вместе с проверкой совпадения, а не вместо неё.
#
# Заголовки безопасности здесь не ставятся: их ставит приложение, и ставит один
# раз. Два одинаковых заголовка в ответе ничего не улучшают, а расходятся при
# первой же правке одного из них.
location /media/ {
    set $opencrm_media app;
    proxy_pass http://$opencrm_media:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 120s;
    error_page 502 503 504 =503 /__opencrm-maintenance;
}

# Логотип и картинка для соцсетей. Тот же разговор про SVG, что и у медиа, плюс
# `nosniff`, которого здесь не было вовсе: без него браузер вправе решить, что
# «logo.png» на самом деле документ, и обойтись с ним соответственно.
location /branding/ {
    alias /srv/storage/branding/;
    add_header Cache-Control "public, max-age=3600";
    add_header X-Content-Type-Options nosniff always;
    add_header Content-Security-Policy "default-src 'none'; img-src 'self'; media-src 'self'; style-src 'unsafe-inline'; sandbox" always;
}

location /avatars/ {
    alias /srv/storage/avatars/;
    add_header Cache-Control "public, max-age=86400";
    add_header X-Content-Type-Options nosniff always;
    add_header Content-Security-Policy "default-src 'none'; img-src 'self'; media-src 'self'; style-src 'unsafe-inline'; sandbox" always;
}

# Шрифты витрины лежат внутри образа, их отдаёт приложение — оно же ставит
# Cache-Control. Своего add_header тут нет намеренно: два одинаковых заголовка
# в ответе ничего не улучшают, а расходятся при первой же правке одного из них.

# внутренние файлы клиентов app отдаёт сам после проверки сессии

# Метрики приложения наружу не отдаются вовсе.
#
# Сессии у Prometheus нет и быть не может, поэтому маршрут открыт без входа — и
# закрывается он здесь, границей сети, а не паролем. Prometheus ходит к
# приложению напрямую по сети compose (`app:8000`), минуя nginx, так что запрет
# ему не мешает; постороннему же незачем знать ни размер базы, ни возраст
# последней резервной копии, ни то, что вчерашнее обновление откатилось.
#
# `= /api/v1/metrics` — точное совпадение: точнее любого префикса и не задевает
# остальной API.
location = /api/v1/metrics {
    deny all;
}

# Grafana — на том же домене, отдельным путём и под своим паролем.
#
# Своего порта наружу у неё нет намеренно (docker-compose.yml, `expose` вместо
# `ports`): открытая панель мониторинга — это карта всей системы, выданная
# любому, кто угадал порт. Единственный вход — через этот nginx, то есть через
# тот же сертификат и тот же фаервол, что и сам сайт.
location = /monitoring {
    return 301 /monitoring/;
}

location /monitoring/ {
    # Переменная и резолвер — по той же причине, что у приложения: контейнер
    # мониторинга пересоздаётся при обновлении, и записанное литералом имя
    # осталось бы указывать на адрес, которого больше нет.
    set $opencrm_grafana grafana;
    proxy_pass http://$opencrm_grafana:3000;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 120s;

    # Мониторинг выключен — служб нет в стеке вовсе (профиль compose), и имя
    # `grafana` не резолвится. Отдаём внятное «выключено» вместо страницы
    # обслуживания: заглушка «скоро вернёмся» на этом адресе означала бы, что
    # лежит сайт, тогда как сайт в полном порядке.
    error_page 502 503 504 = @opencrm_monitoring_off;
}

location @opencrm_monitoring_off {
    default_type text/plain;
    charset utf-8;
    add_header Cache-Control "no-store" always;
    add_header X-Content-Type-Options nosniff always;
    return 503 "Monitoring is switched off (./opencrm.sh monitoring)\n";
}

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
