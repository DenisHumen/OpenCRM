#!/bin/sh
# Точка входа Alertmanager: подставляет токен бота и чат, а без них — уходит на
# конфиг без единого канала.
#
# **Токена нет в репозитории и не будет.** Он приходит переменными из
# `docker/.env` (файл с правами 600, пишет `./opencrm.sh` — ровно так же, как
# пароль MySQL). Alertmanager переменные окружения в конфиге не раскрывает,
# поэтому подстановка делается здесь, до его запуска; готовый конфиг рождается в
# `/tmp` внутри контейнера и наружу не выходит.
#
# Отдельный конфиг «без канала» нужен потому, что Alertmanager с пустым
# `bot_token` не стартует вовсе и уходит в цикл перезапуска. Молчащий
# мониторинг — плохо, а мониторинг, который не поднимается и тянет за собой
# правило про циклический перезапуск, — хуже: он врёт про сам себя.
set -e

TOKEN="${OPENCRM_MONITORING_TELEGRAM_TOKEN:-}"
CHAT="${OPENCRM_MONITORING_TELEGRAM_CHAT:-}"
# Настоящий Telegram по умолчанию; переопределяется своим прокси там, где он
# заблокирован, и заглушкой — на стенде.
API="${OPENCRM_MONITORING_TELEGRAM_API:-https://api.telegram.org}"
TARGET=/tmp/alertmanager.yml

if [ -n "$TOKEN" ] && [ -n "$CHAT" ]; then
    TEMPLATE=/etc/alertmanager/alertmanager.yml.template
    echo "[opencrm-alertmanager] канал: Telegram, чат $CHAT"
else
    TEMPLATE=/etc/alertmanager/alertmanager-silent.yml
    echo "[opencrm-alertmanager] ВНИМАНИЕ: Telegram не настроен."
    echo "[opencrm-alertmanager] Тревоги будут считаться и показываться на дашборде,"
    echo "[opencrm-alertmanager] но никуда не уйдут — то есть о поломке всё равно"
    echo "[opencrm-alertmanager] узнают глазами. Настроить: ./opencrm.sh monitoring"
fi

render() {
    sed -e "s|__TELEGRAM_TOKEN__|$TOKEN|g" \
        -e "s|__TELEGRAM_CHAT__|$CHAT|g" \
        -e "s|__TELEGRAM_API__|$API|g" \
        "$TEMPLATE" > "$TARGET"
}
render

# Перечитывание раз в пять минут — как у Prometheus и у nginx, и по той же
# причине: шаблон примонтирован из чекаута, `up -d --build` эту службу не
# пересоздаёт, и правка маршрутизации тревог молча не применялась бы до
# ближайшего перезапуска.
(
    while :; do
        sleep 300
        render
        kill -HUP 1 2>/dev/null || true
    done
) &

exec /bin/alertmanager --config.file="$TARGET" "$@"
