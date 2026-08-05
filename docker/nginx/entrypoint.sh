#!/bin/sh
# Точка входа nginx: выбирает конфиг по наличию сертификата и держит его свежим.
#
# Курица и яйцо: server-блок на 443 не поднимется без файлов сертификата, а
# получить их можно только когда nginx уже отвечает на 80 (проверка http-01).
# Поэтому конфиг не статичный: нет сертификата — поднимаемся на одном 80 и
# отдаём каталог проверки; появился — включаем 443 и редирект с 80.
set -e

DOMAIN="${OPENCRM_DOMAIN:-_}"
LIVE_DIR="/etc/letsencrypt/live/${DOMAIN}"
TARGET="/etc/nginx/conf.d/default.conf"

mkdir -p /var/www/certbot

if [ "$DOMAIN" != "_" ] && [ -f "${LIVE_DIR}/fullchain.pem" ]; then
    TEMPLATE="/opencrm/templates/https.conf.template"
    echo "[opencrm-nginx] сертификат для ${DOMAIN} найден — включаю HTTPS"
else
    TEMPLATE="/opencrm/templates/http.conf.template"
    if [ "$DOMAIN" = "_" ]; then
        echo "[opencrm-nginx] OPENCRM_DOMAIN не задан — работаю по HTTP (годится для сети и по IP)"
    else
        echo "[opencrm-nginx] сертификата для ${DOMAIN} ещё нет — работаю по HTTP."
        # --entrypoint обязателен: entrypoint сервиса certbot — цикл продления,
        # а `run` подменяет команду, а не его. Без флага выпуск просто виснет.
        echo "[opencrm-nginx] выпустить: docker compose run --rm --entrypoint certbot certbot certonly \\"
        echo "[opencrm-nginx]     --webroot -w /var/www/certbot -d ${DOMAIN} --agree-tos --no-eff-email --email ВАША@ПОЧТА"
        echo "[opencrm-nginx] после выпуска: docker compose restart nginx"
    fi
fi

# Одинарные кавычки здесь обязательны и не опечатка: envsubst принимает СПИСОК
# переменных, которые ему разрешено подставлять. Раскрой его оболочка — список
# стал бы пустым, и envsubst заменил бы вообще всё, включая $host и $uri из
# конфига nginx.
# shellcheck disable=SC2016
envsubst '${OPENCRM_DOMAIN}' < "$TEMPLATE" > "$TARGET"
nginx -t

# Продлённый сертификат — это новый файл на диске, но уже запущенный nginx
# продолжит отдавать старый, пока его не попросят перечитать конфиг. Сертификат
# живёт 90 дней, обновляется за 30 — перечитывать раз в 6 часов с запасом хватает.
while :; do
    sleep 6h
    nginx -s reload 2>/dev/null || true
done &

exec nginx -g 'daemon off;'
