#!/bin/sh
# OpenCRM — установка и управление сайтом одной командой.
#
#   ./opencrm.sh                 первый запуск: мастер установки; дальше — меню
#   ./opencrm.sh <команда>       то же без меню (см. `./opencrm.sh help`)
#
# Мастер сам ставит Docker, генерирует секреты, создаёт каталоги, поднимает стек,
# выпускает сертификат и включает автообновление. От человека нужен только домен
# (или его отсутствие — тогда сайт поднимется по IP в локальной сети).
#
# Написано на POSIX sh, а не bash: на голой Ubuntu `/bin/sh` — это dash, и скрипт
# установки не имеет права требовать того, что сам ещё не поставил. По той же
# причине здесь нет ни jq, ни dig, ни openssl — только то, что есть в базовой
# системе (coreutils, getent, curl ставится первым делом).

set -eu

VERSION="1.0"
REPO_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
COMPOSE_FILE="$REPO_DIR/docker/docker-compose.yml"
APP_ENV="$REPO_DIR/config/.env"
DOCKER_ENV="$REPO_DIR/docker/.env"

ASSUME_YES=${OPENCRM_ASSUME_YES:-0}
ARG_DOMAIN=""
# Пустая строка — законное значение домена («работаем по IP»), поэтому «флаг не
# задан» и «задан пустым» приходится различать отдельным признаком: без него
# `--domain ""` не сбрасывал бы уже настроенный домен, а молча оставлял его.
ARG_DOMAIN_SET=0
ARG_EMAIL=""

# --------------------------------------------------------------------------
# Вывод
# --------------------------------------------------------------------------

if [ -t 1 ] && [ "${TERM:-dumb}" != "dumb" ]; then
    B=$(printf '\033[1m'); D=$(printf '\033[2m'); R=$(printf '\033[0m')
    GREEN=$(printf '\033[32m'); YELLOW=$(printf '\033[33m'); RED=$(printf '\033[31m')
else
    B=""; D=""; R=""; GREEN=""; YELLOW=""; RED=""
fi

say()  { printf '%s\n' "$*"; }
step() { printf '\n%s==>%s %s\n' "$B" "$R" "$*"; }
ok()   { printf '    %s+%s %s\n' "$GREEN" "$R" "$*"; }
info() { printf '    %s.%s %s\n' "$D" "$R" "$*"; }
warn() { printf '    %s!%s %s\n' "$YELLOW" "$R" "$*"; }
die()  { printf '\n%s[!] %s%s\n' "$RED" "$*" "$R" >&2; exit 1; }

# Вопросы читаются из /dev/tty, а не со stdin: скрипт должен работать и когда
# его скормили через пайп (`curl ... | sh`), где stdin занят самим скриптом и
# `read` съел бы его собственный текст.
#
# Обратная сторона — меню нельзя проскроллить пайпом, поэтому есть явная отдушина
# OPENCRM_INPUT=stdin: она нужна тестам и автоматизации, которые прогоняют меню
# заранее заготовленными ответами.
ask() {
    _prompt=$1
    _default=${2:-}
    if [ "$ASSUME_YES" = "1" ]; then
        printf '%s' "$_default"
        return 0
    fi

    if [ -n "$_default" ]; then
        _line="$_prompt [$_default]: "
    else
        _line="$_prompt: "
    fi

    if [ "${OPENCRM_INPUT:-tty}" = "stdin" ] || [ ! -r /dev/tty ]; then
        # `>&2`, а НЕ `> /dev/stderr`: второе заново открывает тот же файл с
        # нулевого смещения и затирает уже написанное. При обычном
        # `./opencrm.sh install > install.log 2>&1` лог превращался бы в кашу.
        printf '%s' "$_line" >&2
        IFS= read -r _answer || _answer=""
    else
        printf '%s' "$_line" > /dev/tty
        IFS= read -r _answer < /dev/tty || _answer=""
    fi

    [ -n "$_answer" ] || _answer=$_default
    printf '%s' "$_answer"
}

confirm() {
    _reply=$(ask "$1 (y/n)" "${2:-y}")
    case "$_reply" in
        [yYдД]*) return 0 ;;
        *) return 1 ;;
    esac
}

# --------------------------------------------------------------------------
# Окружение
# --------------------------------------------------------------------------

OS_ID=""; OS_VERSION=""; OS_NAME=""; OS_CODENAME=""
detect_os() {
    [ -r /etc/os-release ] || die "не вижу /etc/os-release — не знаю, что за система"
    # shellcheck disable=SC1091
    . /etc/os-release
    OS_ID=${ID:-}
    OS_VERSION=${VERSION_ID:-}
    OS_NAME=${PRETTY_NAME:-$OS_ID}
    OS_CODENAME=${UBUNTU_CODENAME:-${VERSION_CODENAME:-}}
}

SUDO=""
detect_sudo() {
    if [ "$(id -u)" -eq 0 ]; then
        SUDO=""
    elif command -v sudo >/dev/null 2>&1; then
        SUDO="sudo"
    else
        die "нужны права root: запустите от root или поставьте sudo"
    fi
}

has() { command -v "$1" >/dev/null 2>&1; }
has_systemd() { [ -d /run/systemd/system ]; }
docker_ready() { docker info >/dev/null 2>&1; }

# Git в каталоге проекта — всегда через это, а не голым `git -C`.
#
#   safe.directory   — каталог клонировал человек, а скрипт зовут и от него, и
#                      от root через sudo; без исключения git отвечает `detected
#                      dubious ownership` и не делает вообще ничего.
#   core.fileMode    — бит исполнения не содержит изменений и не считается
#                      правкой: иначе `chmod +x opencrm.sh` из инструкции по
#                      установке навсегда делает дерево «грязным», а обновления
#                      останавливаются с `M opencrm.sh`.
git_repo() {
    git -c "safe.directory=$REPO_DIR" -c core.fileMode=false -C "$REPO_DIR" "$@"
}

# Наличия файла `docker` в PATH мало: Docker Desktop оставляет в дистрибутивах
# WSL заглушку, которая только советует включить интеграцию. Спрашиваем версию и
# смотрим, что ответ вообще похож на версию.
docker_version() {
    _v=$(docker --version 2>/dev/null | head -n 1)
    case "$_v" in
        "Docker version "*) printf '%s' "$_v" | cut -d' ' -f3 | tr -d ',' ;;
        *) return 1 ;;
    esac
}

compose_version() {
    docker compose version --short 2>/dev/null | head -n 1
}

has_docker() { docker_version >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; }

# Членство в группе docker появляется только в новой сессии. Перезаходить руками
# посреди установки — плохая идея, поэтому перезапускаем себя внутри группы.
reenter_docker_group() {
    has sg || return 1
    id -nG 2>/dev/null | tr ' ' '\n' | grep -qx docker && return 1
    getent group docker 2>/dev/null | grep -q "[:,]$(id -un)\(,\|$\)" || return 1
    info "членство в группе docker ещё не подхвачено — перезапускаюсь в ней"
    exec sg docker -c "$(quote_argv "$0" "$@")"
}

quote_argv() {
    _out=""
    for _arg in "$@"; do
        _out="$_out'$(printf '%s' "$_arg" | sed "s/'/'\\\\''/g")' "
    done
    printf '%s' "$_out"
}

# --------------------------------------------------------------------------
# Работа с .env
# --------------------------------------------------------------------------

env_get() {
    [ -f "$1" ] || return 1
    sed -n "s/^$2=//p" "$1" | head -n 1
}

# Правка идёт через временный файл и awk (а не sed -i): значения содержат слэши,
# амперсанды и base64-мусор, на которых sed-подстановка молча ломается.
#
# Права выставляются на каждой записи, и это не перестраховка. Временный файл
# рождается с обычным umask (644), а `mv` переносит права вместе с содержимым —
# и config/.env, созданный с chmod 600, после первой же правки становился
# доступен на чтение всем в системе. Вместе с ключом подписи сессий, солью
# хэширования IP и паролем администратора.
env_set() {
    _file=$1; _key=$2; _value=$3
    if [ ! -f "$_file" ]; then
        touch "$_file"
    fi
    if grep -q "^$_key=" "$_file"; then
        awk -v k="$_key" -v v="$_value" '
            index($0, k "=") == 1 { print k "=" v; next }
            { print }
        ' "$_file" > "$_file.new"
        chmod 600 "$_file.new"
        mv "$_file.new" "$_file"
    else
        printf '%s=%s\n' "$_key" "$_value" >> "$_file"
        chmod 600 "$_file"
    fi
}

# 48 случайных байт в алфавите, безопасном для .env и командной строки.
gen_secret() {
    LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom 2>/dev/null | head -c "${1:-64}" || true
    printf '\n'
}

lan_ip() {
    ip route get 1.1.1.1 2>/dev/null | awk '{for (i=1;i<NF;i++) if ($i=="src") {print $(i+1); exit}}'
}

public_ip() {
    curl -fsS --max-time 6 https://api.ipify.org 2>/dev/null \
        || curl -fsS --max-time 6 https://ifconfig.me/ip 2>/dev/null \
        || true
}

domain_ip() {
    getent ahostsv4 "$1" 2>/dev/null | awk 'NR==1 {print $1}'
}

# --------------------------------------------------------------------------
# Установка зависимостей
# --------------------------------------------------------------------------

apt_install() {
    $SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$@" >/dev/null
}

install_base_packages() {
    step "Базовые пакеты"
    _missing=""
    for _pkg in git curl ca-certificates; do
        has "$_pkg" || _missing="$_missing $_pkg"
    done
    if [ -z "$_missing" ]; then
        ok "git, curl, ca-certificates уже есть"
        return 0
    fi
    case "$OS_ID" in
        ubuntu|debian)
            info "ставлю:$_missing"
            $SUDO apt-get update -qq >/dev/null
            # shellcheck disable=SC2086
            apt_install $_missing
            ok "поставлено:$_missing"
            ;;
        *)
            die "не знаю, как ставить пакеты в «$OS_NAME» — поставьте вручную:$_missing"
            ;;
    esac
}

install_docker() {
    step "Docker"
    if has_docker; then
        ok "Docker $(docker_version) и плагин compose $(compose_version) уже есть"
    else
        case "$OS_ID" in
            ubuntu|debian) ;;
            *) die "автоустановка Docker есть только для Ubuntu/Debian; поставьте Docker и плагин compose вручную" ;;
        esac
        # Именно из репозитория Docker, а не `apt install docker.io`: в системном
        # пакете нет плагина `docker compose` v2, на который завязан весь проект.
        info "подключаю репозиторий Docker"
        $SUDO install -m 0755 -d /etc/apt/keyrings
        $SUDO curl -fsSL "https://download.docker.com/linux/$OS_ID/gpg" \
            -o /etc/apt/keyrings/docker.asc
        $SUDO chmod a+r /etc/apt/keyrings/docker.asc
        printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/%s %s stable\n' \
            "$(dpkg --print-architecture)" "$OS_ID" "$OS_CODENAME" \
            | $SUDO tee /etc/apt/sources.list.d/docker.list >/dev/null
        $SUDO apt-get update -qq >/dev/null
        info "ставлю docker-ce и плагины (это займёт минуту-другую)"
        apt_install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
        ok "Docker поставлен"
    fi

    if has_systemd; then
        $SUDO systemctl enable --now docker >/dev/null 2>&1 || true
    elif ! docker_ready; then
        # WSL и контейнеры без systemd: демон поднимается sysv-скриптом.
        info "systemd не найден — поднимаю демон через service"
        $SUDO service docker start >/dev/null 2>&1 || true
        _wait=15
        while [ "$_wait" -gt 0 ] && ! docker_ready; do sleep 1; _wait=$((_wait - 1)); done
    fi

    if [ "$(id -u)" -ne 0 ] && ! docker_ready; then
        info "добавляю $(id -un) в группу docker"
        $SUDO usermod -aG docker "$(id -un)"
        warn "членство появится в новой сессии — если после установки docker не отвечает, перезайдите"
    fi

    docker_ready || die "Docker не отвечает. Проверьте: $SUDO service docker start (или systemctl start docker)"
    ok "демон Docker отвечает"
}

install_python() {
    step "Python для автообновления"
    if has python3; then
        ok "python3 есть ($(python3 --version 2>&1 | cut -d' ' -f2))"
        return 0
    fi
    case "$OS_ID" in
        ubuntu|debian)
            $SUDO apt-get update -qq >/dev/null
            apt_install python3
            ok "python3 поставлен"
            ;;
        *) warn "python3 не найден — автообновление работать не будет" ;;
    esac
}

# --------------------------------------------------------------------------
# Ресурсы машины
# --------------------------------------------------------------------------

mem_mb()  { awk '/^MemTotal:/  {print int($2/1024)}' /proc/meminfo 2>/dev/null || printf '0'; }
swap_mb() { awk '/^SwapTotal:/ {print int($2/1024)}' /proc/meminfo 2>/dev/null || printf '0'; }
free_mb() { df -Pm "$1" 2>/dev/null | awk 'NR==2 {print $4}'; }

add_swap() {
    if [ -e /swapfile ]; then
        warn "/swapfile уже существует — не трогаю"
        return 0
    fi
    info "создаю файл подкачки на 2 ГБ"
    if ! $SUDO fallocate -l 2G /swapfile 2>/dev/null; then
        # fallocate не работает на некоторых файловых системах — тогда честный dd
        $SUDO dd if=/dev/zero of=/swapfile bs=1M count=2048 status=none 2>/dev/null || {
            warn "не удалось создать файл подкачки — пропускаю"
            $SUDO rm -f /swapfile
            return 0
        }
    fi
    $SUDO chmod 600 /swapfile
    if ! $SUDO mkswap /swapfile >/dev/null 2>&1 || ! $SUDO swapon /swapfile 2>/dev/null; then
        warn "ядро не приняло файл подкачки (бывает в OpenVZ-контейнерах) — пропускаю"
        $SUDO rm -f /swapfile
        return 0
    fi
    # Без записи в fstab подкачка исчезнет при первой же перезагрузке, и сборка
    # обновления через месяц упадёт по той же причине, что и сегодня.
    if ! grep -q '^/swapfile ' /etc/fstab 2>/dev/null; then
        printf '/swapfile none swap sw 0 0\n' | $SUDO tee -a /etc/fstab >/dev/null
    fi
    ok "подкачка включена (2 ГБ), переживёт перезагрузку"
}

check_resources() {
    step "Место и память"
    _mem=$(mem_mb); _swap=$(swap_mb)
    _disk=$(free_mb /var/lib/docker)
    [ -n "$_disk" ] || _disk=$(free_mb /)
    [ -n "$_disk" ] || _disk=0
    info "память ${_mem} МБ, подкачка ${_swap} МБ, свободно на диске ${_disk} МБ"

    # Образ с ffmpeg, слоями python и node_modules — это несколько гигабайт, и
    # кончившееся посреди сборки место оставляет докер в состоянии, из которого
    # он выбирается только `docker system prune`.
    if [ "$_disk" -lt 5000 ]; then
        warn "меньше 5 ГБ свободно — сборке образа может не хватить места"
    fi

    # Самое прожорливое место установки — сборка фронтенда: vite держит дерево
    # модулей в памяти, и на машине с 1 ГБ без подкачки её убивает OOM-killer.
    # Симптом обманчив: npm обрывается без внятной ошибки, будто «просто не
    # собралось». Дешёвая подкачка снимает вопрос совсем.
    if [ $((_mem + _swap)) -ge 1800 ]; then
        ok "памяти достаточно"
        return 0
    fi
    warn "меньше 2 ГБ памяти вместе с подкачкой — сборка фронтенда может оборваться по OOM"
    if [ "$_swap" -gt 0 ]; then
        say "        Подкачка уже есть; если сборка всё-таки упадёт — увеличьте её."
        return 0
    fi
    if confirm "    Добавить файл подкачки на 2 ГБ?" y; then
        add_swap
    else
        info "пропускаю — при обрыве сборки вернитесь к этому"
    fi
}

# --------------------------------------------------------------------------
# Фаервол
# --------------------------------------------------------------------------

# Порты, по которым в машину заходят по SSH. Ошибиться здесь — значит запереть
# хозяина снаружи собственного сервера, поэтому спрашиваем всех, кто может знать,
# и берём объединение.
ssh_ports() {
    {
        # Самый надёжный источник: порт, по которому мы прямо сейчас подключены.
        # Что бы ни было написано в конфигах, этот точно работает.
        # $SSH_CONNECTION = «адрес-клиента порт адрес-сервера порт».
        if [ -n "${SSH_CONNECTION:-}" ]; then
            printf '%s\n' "$SSH_CONNECTION" | awk '{print $4}'
        fi
        # Кто реально слушает сейчас.
        ss -tlnp 2>/dev/null | awk '/sshd|"ssh/ {n=split($4, a, ":"); print a[n]}' || true
        # Ubuntu 24.04 поднимает ssh через сокет systemd, и порт бывает задан
        # только там — в sshd_config его тогда нет вовсе.
        systemctl show ssh.socket sshd.socket -p Listen 2>/dev/null \
            | sed -n 's/.*:\([0-9]\{1,5\}\) (Stream).*/\1/p' || true
        # Классический конфиг, включая drop-in-каталог.
        grep -rhsiE '^[[:space:]]*Port[[:space:]]+[0-9]+' \
            /etc/ssh/sshd_config /etc/ssh/sshd_config.d/ 2>/dev/null | awk '{print $2}' || true
    } 2>/dev/null | grep -E '^[0-9]+$' | sort -un || true
}

# Первая строка `ufw status` — «Status: active» либо «Status: inactive».
# Проверять её через `grep active` нельзя: это слово целиком содержится в
# «inactive», и на выключенном фаерволе проверка отвечала бы «включён». Скрипт
# при этом рапортовал об успехе, а сервер оставался открытым — поймано на стенде.
# Отсюда якорь: «active» должно идти сразу после двоеточия.
#
# $1 — чем запускать ufw: пусто (мы root), `sudo` или `sudo -n` там, где нельзя
# зависнуть на запросе пароля. Без кавычек намеренно: «sudo -n» должно
# разделиться на два слова.
ufw_is_active() {
    LC_ALL=C ${1:-} ufw status 2>/dev/null | head -n 1 | grep -qiE '^status:[[:space:]]*active'
}

setup_firewall() {
    step "Фаервол"
    case "$OS_ID" in
        ubuntu|debian) ;;
        *) warn "автонастройка ufw есть только для Ubuntu/Debian — закройте порты сами"; return 0 ;;
    esac

    if ! has ufw; then
        if ! confirm "    Поставить ufw и закрыть всё, кроме сайта и SSH?" y; then
            info "пропускаю; включить позже — ./opencrm.sh firewall"
            return 0
        fi
        $SUDO apt-get update -qq >/dev/null
        apt_install ufw
        ok "ufw поставлен"
    fi

    _ssh=$(ssh_ports)
    [ -n "$_ssh" ] || _ssh=22
    _was_active=0
    if ufw_is_active "$SUDO"; then _was_active=1; fi

    say "    Останутся открытыми: SSH ($(printf '%s' "$_ssh" | tr '\n' ' ')), 80/tcp, 443/tcp"
    if [ "$_was_active" = "0" ]; then
        say "    ${D}Порт SSH определён по активному подключению, слушающим сокетам и конфигу.${R}"
        if ! confirm "    Включить фаервол с этими правилами?" y; then
            info "пропускаю; включить позже — ./opencrm.sh firewall"
            return 0
        fi
    fi

    for _port in $_ssh; do
        $SUDO ufw allow "$_port/tcp" >/dev/null 2>&1 || warn "не удалось открыть $_port/tcp"
    done
    $SUDO ufw allow 80/tcp  >/dev/null 2>&1 || true
    $SUDO ufw allow 443/tcp >/dev/null 2>&1 || true

    if [ "$_was_active" = "0" ]; then
        # Политику по умолчанию меняем только на выключенном фаерволе: если он
        # уже работал, у хозяина машины свои правила, и ломать их мы не вправе.
        $SUDO ufw default deny incoming >/dev/null 2>&1 || true
        $SUDO ufw default allow outgoing >/dev/null 2>&1 || true
        $SUDO ufw --force enable >/dev/null 2>&1 || true
        # Спрашиваем состояние заново, а не верим коду возврата: «команда не
        # упала» и «фаервол работает» — разные утверждения, и рапортовать о
        # защите, которой нет, хуже, чем честно сказать, что не вышло.
        if ufw_is_active "$SUDO"; then
            ok "фаервол включён, входящие закрыты кроме SSH и сайта"
        else
            warn "ufw не включился — проверьте: $SUDO ufw status verbose"
            return 0
        fi
    else
        ok "фаервол уже работал — правила для сайта добавлены, политику не трогал"
    fi

    verify_docker_network

    # Это не недоработка скрипта, а устройство Docker: опубликованный порт он
    # заворачивает в PREROUTING/DOCKER, минуя цепочки, куда пишет ufw. Для 80 и
    # 443 это ровно то, что нужно. Важно другое — не считать ufw защитой от
    # случайно опубликованного порта соседнего контейнера: он не поможет.
    warn "Docker публикует порты мимо ufw: 80 и 443 будут открыты, даже если их запретить."
    say "        Для сайта это и нужно. Но и любой другой контейнер с \`ports:\` окажется"
    say "        снаружи вопреки правилам — публикуйте только на 127.0.0.1 (docs/07-security.md)."
}

# Резолвится ли имя из НОВОГО контейнера — то есть по тому же пути, каким ходит
# сборка образа. У уже работающих контейнеров правила свои, и их благополучие
# ничего не доказывает.
docker_resolves() {
    docker run --rm --entrypoint getent "$1" hosts github.com >/dev/null 2>&1
}

# Разрешить контейнерам спрашивать DNS у хоста. Узко: только порт 53 и только из
# частных подсетей Docker — наружу резолвер остаётся закрыт, открытый миру он
# годится разве что усилителям DNS-атак. Идемпотентно: ufw дублей не заводит.
ensure_docker_dns() {
    has ufw || return 0
    ufw_is_active "$SUDO" || return 0
    $SUDO ufw allow from 172.16.0.0/12 to any port 53 proto udp >/dev/null 2>&1 || true
    $SUDO ufw allow from 172.16.0.0/12 to any port 53 proto tcp >/dev/null 2>&1 || true
}

app_image() {
    compose images -q app 2>/dev/null | head -n 1
}

# Включённый фаервол умеет незаметно отрезать контейнерам выход наружу. DNS у
# них нередко смотрит на адрес самого хоста, а такой пакет уходит не в FORWARD,
# а в INPUT — и гибнет о «deny incoming». Коварство в том, что сайт при этом
# работает как ни в чём не бывало: ломается только следующая сборка, то есть
# очередное автообновление, недели спустя и без всякой связи с причиной.
#
# Поэтому проверяем сразу, а лечим узко: только DNS и только из частных подсетей
# Docker (172.16/12). Наружу порт 53 при этом закрыт — открывать резолвер миру
# значит подарить его усилителям DNS-атак.
verify_docker_network() {
    _image=$(app_image)
    if [ -z "$_image" ]; then
        info "образ ещё не собран — выход контейнеров наружу проверю после сборки"
        return 0
    fi
    if docker_resolves "$_image"; then
        ok "контейнеры видят сеть — сборка обновлений не пострадает"
        return 0
    fi
    info "контейнеры потеряли DNS — открываю его из подсетей Docker"
    ensure_docker_dns
    if docker_resolves "$_image"; then
        ok "починено: DNS разрешён контейнерам, наружу порт 53 закрыт"
    else
        warn "контейнеры не видят сеть — следующее обновление не соберётся"
        say "        Проверить руками: docker run --rm --entrypoint getent \\"
        say "            \$(docker compose -f docker/docker-compose.yml images -q app) hosts github.com"
        say "        Если не чинится — снимите фаервол: $SUDO ufw disable"
    fi
}

# --------------------------------------------------------------------------
# Мастер первого запуска
# --------------------------------------------------------------------------

installed() { [ -f "$APP_ENV" ] && [ -f "$DOCKER_ENV" ]; }

home_dir() {
    _home=$(env_get "$DOCKER_ENV" OPENCRM_HOME 2>/dev/null || true)
    [ -n "$_home" ] || _home="$HOME/opencrm"
    printf '%s' "$_home"
}

configure_domain() {
    step "Домен"
    _current=$(env_get "$DOCKER_ENV" OPENCRM_DOMAIN 2>/dev/null || true)
    if [ "$ARG_DOMAIN_SET" = "1" ]; then
        _domain=$ARG_DOMAIN
    else
        say "    Укажите домен, который уже смотрит A-записью на этот сервер."
        say "    ${D}Пустое значение — работать по IP в локальной сети, без HTTPS.${R}"
        _domain=$(ask "    Домен" "$_current")
    fi
    _domain=$(printf '%s' "$_domain" | tr -d ' ' | sed 's#^https\?://##; s#/.*##')
    env_set "$DOCKER_ENV" OPENCRM_DOMAIN "$_domain"

    if [ -z "$_domain" ]; then
        _ip=$(lan_ip)
        [ -n "$_ip" ] || _ip="127.0.0.1"
        env_set "$APP_ENV" OPENCRM_BASE_URL "http://$_ip"
        ok "без домена: сайт будет доступен по http://$_ip"
        return 0
    fi

    # Схема в BASE_URL — не украшение: по ней приложение решает, ставить ли
    # cookie флаг Secure. Написать https:// до выпуска сертификата значит выдать
    # Secure-cookie по обычному HTTP, а её браузер молча выбросит — вход стал бы
    # «залогинился и тут же вылетел». Поэтому https появляется только вместе с
    # сертификатом (см. issue_certificate).
    if [ -d "$(home_dir)/letsencrypt/live/$_domain" ]; then
        env_set "$APP_ENV" OPENCRM_BASE_URL "https://$_domain"
        ok "домен: $_domain (сертификат на месте)"
    else
        env_set "$APP_ENV" OPENCRM_BASE_URL "http://$_domain"
        ok "домен: $_domain (пока по HTTP, до выпуска сертификата)"
    fi
}

# Секреты генерируются один раз. Перегенерация на повторном запуске разлогинила
# бы всех и обесценила бы выданные PIN-ссылки, поэтому непустые значения не
# трогаем никогда.
seed_secret() {
    _key=$1
    _existing=$(env_get "$APP_ENV" "$_key" 2>/dev/null || true)
    if [ -n "$_existing" ]; then
        info "$_key уже задан — не трогаю"
    else
        env_set "$APP_ENV" "$_key" "$(gen_secret 64)"
        ok "$_key сгенерирован"
    fi
}

ROOT_PASSWORD_SHOWN=""
configure_app_env() {
    step "Настройки приложения"
    if [ ! -f "$APP_ENV" ]; then
        cp "$REPO_DIR/config/.env.example" "$APP_ENV"
        chmod 600 "$APP_ENV"
        ok "создан config/.env из шаблона"
    else
        ok "config/.env уже есть — дополняю недостающее"
    fi

    env_set "$APP_ENV" OPENCRM_ENV production
    env_set "$APP_ENV" OPENCRM_TRUSTED_PROXY_HOPS 1
    seed_secret OPENCRM_SECRET_KEY
    seed_secret OPENCRM_IP_HASH_SALT

    _root_email=$(env_get "$APP_ENV" OPENCRM_ROOT_EMAIL 2>/dev/null || true)
    if [ -z "$_root_email" ]; then
        # --email один на двоих: и логин администратора, и контакт для Let's
        # Encrypt. Спрашивать одно и то же дважды у одного человека незачем.
        _root_email=$(ask "    Email администратора (логин в CRM)" \
            "${ARG_EMAIL:-admin@${ARG_DOMAIN:-opencrm.local}}")
        env_set "$APP_ENV" OPENCRM_ROOT_EMAIL "$_root_email"
    fi
    ok "администратор: $_root_email"

    _root_password=$(env_get "$APP_ENV" OPENCRM_ROOT_PASSWORD 2>/dev/null || true)
    if [ -z "$_root_password" ]; then
        ROOT_PASSWORD_SHOWN=$(gen_secret 20)
        env_set "$APP_ENV" OPENCRM_ROOT_PASSWORD "$ROOT_PASSWORD_SHOWN"
        ok "пароль администратора сгенерирован (покажу в конце)"
    fi
}

configure_docker_env() {
    step "Настройки compose"
    [ -f "$DOCKER_ENV" ] || cp "$REPO_DIR/docker/.env.example" "$DOCKER_ENV"
    # Контейнер пишет в примонтированные каталоги под этим UID — не совпадёт
    # с владельцем, и первая же миграция упрётся в permission denied.
    env_set "$DOCKER_ENV" OPENCRM_UID "$(id -u)"
    env_set "$DOCKER_ENV" OPENCRM_GID "$(id -g)"
    # Путь задаём явно: у systemd-службы автообновления $HOME не обязан
    # совпадать с вашим, а разойдись он — compose примонтирует другие каталоги.
    _home=$(env_get "$DOCKER_ENV" OPENCRM_HOME 2>/dev/null || true)
    [ -n "$_home" ] || _home="$HOME/opencrm"
    env_set "$DOCKER_ENV" OPENCRM_HOME "$_home"
    ok "UID:GID $(id -u):$(id -g), состояние в $_home"
}

create_dirs() {
    step "Каталоги состояния"
    _home=$(home_dir)
    for _sub in data storage letsencrypt acme updates; do
        mkdir -p "$_home/$_sub"
    done
    ok "$_home/{data,storage,letsencrypt,acme,updates}"
}

compose() {
    docker compose -f "$COMPOSE_FILE" "$@"
}

# Настройки читаются один раз при старте процесса, а `compose up -d` при
# изменении одного лишь env_file контейнер НЕ пересоздаёт — приложение продолжает
# жить со старыми значениями. Молча и потому опасно: после выпуска сертификата
# сайт уже за TLS, а cookie так и остались бы без флага Secure.
apply_env_change() {
    compose up -d --force-recreate app
    # nginx проксирует в app и до его готовности отдаёт 502 — ждём здесь, иначе
    # каждый вызывающий получал бы «сайт лежит» сразу после успешной настройки.
    if wait_health 90; then
        ok "приложение перезапущено с новыми настройками"
    else
        warn "приложение не ответило за 3 минуты — смотрите ./opencrm.sh logs app"
    fi
}

build_and_start() {
    step "Сборка и запуск"
    # Фаервол мог стоять на сервере и до нас. Тогда контейнеру сборки нечем
    # резолвить имена, и установка обрывается на `pip install` невнятной ошибкой,
    # в которой про фаервол ни слова. Разрешение узкое, лишним не будет.
    ensure_docker_dns
    info "первая сборка занимает 3-10 минут: собирается фронтенд и ставится ffmpeg"
    compose up -d --build
    ok "контейнеры подняты"
}

wait_health() {
    _tries=${1:-90}
    _url="http://127.0.0.1/healthz"
    while [ "$_tries" -gt 0 ]; do
        if curl -fsS --max-time 3 "$_url" 2>/dev/null | grep -q '"ok"'; then
            return 0
        fi
        _tries=$((_tries - 1))
        sleep 2
    done
    return 1
}

check_health() {
    step "Проверка, что сайт живой"
    if wait_health 90; then
        ok "/healthz отвечает ok"
    else
        warn "сайт не ответил за 3 минуты"
        say ""
        compose logs --tail 40 app || true
        die "не удалось дождаться /healthz — логи выше"
    fi
}

issue_certificate() {
    _domain=$(env_get "$DOCKER_ENV" OPENCRM_DOMAIN 2>/dev/null || true)
    [ -n "$_domain" ] || return 0

    step "HTTPS для $_domain"
    _home=$(home_dir)
    if [ -d "$_home/letsencrypt/live/$_domain" ]; then
        ok "сертификат уже выпущен"
        return 0
    fi

    # Let's Encrypt проверяет домен по HTTP, и если A-запись смотрит не сюда,
    # запрос всё равно провалится — только потратит попытку из недельного лимита.
    _server_ip=$(public_ip)
    _dns_ip=$(domain_ip "$_domain")
    if [ -z "$_dns_ip" ]; then
        warn "домен $_domain не резолвится — сертификат не выпускаю"
        say "        Настройте A-запись и повторите: ./opencrm.sh https"
        return 0
    fi
    if [ -n "$_server_ip" ] && [ "$_dns_ip" != "$_server_ip" ]; then
        warn "A-запись $_domain ведёт на $_dns_ip, а сервер — $_server_ip"
        say "        Let's Encrypt проверку не пройдёт. Повторите позже: ./opencrm.sh https"
        return 0
    fi
    ok "A-запись совпадает с адресом сервера ($_dns_ip)"

    _email=$ARG_EMAIL
    [ -n "$_email" ] || _email=$(ask "    Email для Let's Encrypt (уведомления об истечении)" \
        "$(env_get "$APP_ENV" OPENCRM_ROOT_EMAIL 2>/dev/null || true)")
    [ -n "$_email" ] || { warn "без email сертификат не выпустить"; return 0; }

    info "запрашиваю сертификат"
    if compose run --rm certbot certonly --webroot -w /var/www/certbot \
        -d "$_domain" --email "$_email" --agree-tos --no-eff-email --non-interactive; then
        # Теперь сайт правда за TLS — можно и нужно переводить BASE_URL на https,
        # чтобы cookie получили флаг Secure. Настройки читаются при старте
        # процесса, поэтому контейнер приложения пересоздаётся, а не просто ждёт.
        env_set "$APP_ENV" OPENCRM_BASE_URL "https://$_domain"
        apply_env_change
        compose restart nginx
        ok "HTTPS включён, cookie получили флаг Secure, продление идёт само"
    else
        warn "certbot не справился — сайт остаётся на HTTP"
        say "        Повторить: ./opencrm.sh https"
    fi
}

setup_autoupdate() {
    step "Автообновление из GitHub"
    _home=$(home_dir)
    _env_file="$_home/autoupdate.env"

    # Обновление умеет только `git fetch` — из распакованного архива обновляться
    # неоткуда. Сказать об этом здесь честнее, чем ошибкой раз в пять минут.
    if [ ! -d "$REPO_DIR/.git" ]; then
        warn "$REPO_DIR — не git-репозиторий, автообновлению неоткуда брать версии"
        say "        Разверните через: git clone https://github.com/DenisHumen/OpenCRM.git"
        return 0
    fi

    if [ ! -f "$_env_file" ]; then
        cp "$REPO_DIR/deploy/autoupdate.env.example" "$_env_file"
        env_set "$_env_file" OPENCRM_HOME "$_home"
        env_set "$_env_file" OPENCRM_UPDATE_PROJECT_DIR "$REPO_DIR"
        chmod 600 "$_env_file"
        ok "создан $_env_file"
    else
        ok "$_env_file уже есть"
    fi

    if ! confirm "    Включить автообновление (сайт сам подтянет новые коммиты)?" y; then
        info "пропускаю; включить позже — пункт меню «Автообновление»"
        return 0
    fi

    _token=$(ask "    Telegram-токен бота для уведомлений (Enter — без уведомлений)" "")
    if [ -n "$_token" ]; then
        _chat=$(ask "    Telegram chat_id" "")
        env_set "$_env_file" OPENCRM_UPDATE_TELEGRAM_TOKEN "$_token"
        env_set "$_env_file" OPENCRM_UPDATE_TELEGRAM_CHAT "$_chat"
        ok "уведомления в Telegram настроены"
    fi

    if has_systemd; then
        _unit=/etc/systemd/system/opencrm-autoupdate.service
        sed -e "s#^User=.*#User=$(id -un)#" \
            -e "s#^WorkingDirectory=.*#WorkingDirectory=$REPO_DIR#" \
            -e "s#^EnvironmentFile=.*#EnvironmentFile=$_env_file#" \
            "$REPO_DIR/deploy/systemd/opencrm-autoupdate.service" \
            | $SUDO tee "$_unit" >/dev/null
        $SUDO systemctl daemon-reload
        $SUDO systemctl enable --now opencrm-autoupdate
        ok "служба opencrm-autoupdate запущена (логи: journalctl -u opencrm-autoupdate -f)"
    else
        # WSL и минимальные образы: systemd нет, но cron делает то же самое.
        _line="*/5 * * * * cd $REPO_DIR && /usr/bin/python3 scripts/autoupdate.py check >/dev/null 2>&1"
        if crontab -l 2>/dev/null | grep -q "autoupdate.py"; then
            ok "задание cron уже стоит"
        elif has crontab; then
            (crontab -l 2>/dev/null || true; printf '%s\n' "$_line") | crontab -
            ok "systemd нет — поставил задание cron раз в 5 минут"
        else
            warn "нет ни systemd, ни cron — запускайте обновление вручную (пункт меню)"
        fi
    fi
}

# Копии не спрашивают разрешения: скрипты бэкапа лежали в репозитории с самого
# начала, но запускать их было некому — «резервное копирование настроено» на
# бумаге и ни одной копии на диске. Расписание ставится молча, снять его —
# отдельная сознательная команда.
setup_backups() {
    step "Ежедневные копии"
    if has_systemd; then
        _dst=/etc/systemd/system/opencrm-backup
        sed -e "s#^User=.*#User=$(id -un)#" \
            -e "s#^WorkingDirectory=.*#WorkingDirectory=$REPO_DIR#" \
            -e "s#^ExecStart=.*#ExecStart=$REPO_DIR/opencrm.sh backup#" \
            "$REPO_DIR/deploy/systemd/opencrm-backup.service" | $SUDO tee "$_dst.service" >/dev/null
        $SUDO cp "$REPO_DIR/deploy/systemd/opencrm-backup.timer" "$_dst.timer"
        $SUDO systemctl daemon-reload
        if $SUDO systemctl enable --now opencrm-backup.timer >/dev/null 2>&1; then
            ok "копия снимается каждую ночь (systemctl list-timers opencrm-backup)"
        else
            warn "таймер не запустился — снимайте копии вручную (пункт меню)"
        fi
    elif has crontab; then
        _line="30 3 * * * cd $REPO_DIR && ./opencrm.sh backup >/dev/null 2>&1"
        if crontab -l 2>/dev/null | grep -q "opencrm.sh backup"; then
            ok "задание cron уже стоит"
        else
            (crontab -l 2>/dev/null || true; printf '%s\n' "$_line") | crontab -
            ok "systemd нет — поставил задание cron на 3:30"
        fi
    else
        warn "нет ни systemd, ни cron — снимайте копии вручную (пункт меню)"
        return 0
    fi
    # Копия на том же диске спасает от испорченной базы и от собственной ошибки,
    # но не от смерти диска и не от потери сервера. Сказать это честно дешевле,
    # чем однажды обнаружить, что копии были ровно там же, где оригинал.
    info "копии лежат в $(home_dir)/data/backups — на том же диске, что и база"
    say "        Выгрузка наружу настраивается в scripts/backup.sh (там же пример)."
}

show_summary() {
    _domain=$(env_get "$DOCKER_ENV" OPENCRM_DOMAIN 2>/dev/null || true)
    _url=$(env_get "$APP_ENV" OPENCRM_BASE_URL 2>/dev/null || true)
    _email=$(env_get "$APP_ENV" OPENCRM_ROOT_EMAIL 2>/dev/null || true)

    printf '\n%s========================================================%s\n' "$B" "$R"
    printf '%s  OpenCRM развёрнут%s\n' "$B" "$R"
    printf '%s========================================================%s\n\n' "$B" "$R"
    say "  Адрес:  $_url"
    say "  Логин:  $_email"
    if [ -n "$ROOT_PASSWORD_SHOWN" ]; then
        printf '  Пароль: %s%s%s\n' "$B" "$ROOT_PASSWORD_SHOWN" "$R"
        say ""
        warn "Пароль показан один раз. При первом входе система попросит его сменить."
        say "        Забыли — ./opencrm.sh password"
    else
        say "  Пароль: задан ранее (сброс — ./opencrm.sh password)"
    fi
    case "$_url" in
        https://*) ;;
        *)
            say ""
            warn "Сайт работает по HTTP — пароли и cookie идут по сети открытым текстом."
            if [ -n "$_domain" ]; then
                say "        Домен задан, но сертификата нет. Когда A-запись заработает: ./opencrm.sh https"
            else
                say "        Годится для локальной сети. Для публичного сайта задайте домен: ./opencrm.sh domain"
            fi
            ;;
    esac
    say ""
    say "  Дальше всё делается через меню: ${B}./opencrm.sh${R}"
    say ""
}

cmd_install() {
    printf '\n%s  OpenCRM — установка (v%s)%s\n' "$B" "$VERSION" "$R"
    detect_os
    say "    Система: $OS_NAME"
    case "$OS_ID" in
        ubuntu|debian) ;;
        *) warn "проверялось на Ubuntu 24.04; «$OS_NAME» может потребовать ручных шагов" ;;
    esac

    detect_sudo
    check_resources
    install_base_packages
    install_docker
    install_python
    configure_docker_env
    configure_app_env
    configure_domain
    create_dirs
    build_and_start
    check_health
    issue_certificate
    setup_firewall
    setup_backups
    setup_autoupdate
    show_summary
}

# --------------------------------------------------------------------------
# Операции
# --------------------------------------------------------------------------

need_install() {
    installed || die "сайт ещё не установлен — запустите ./opencrm.sh install"
}

autoupdate() {
    _home=$(home_dir)
    _env_file="$_home/autoupdate.env"
    if [ -f "$_env_file" ]; then
        # shellcheck disable=SC1090
        set -a; . "$_env_file"; set +a
    fi
    OPENCRM_UPDATE_PROJECT_DIR="$REPO_DIR" python3 "$REPO_DIR/scripts/autoupdate.py" "$@"
}

cmd_status() {
    need_install
    step "Контейнеры"
    compose ps
    step "Сайт"
    if curl -fsS --max-time 3 http://127.0.0.1/healthz 2>/dev/null | grep -q '"ok"'; then
        ok "/healthz отвечает ok  ($(env_get "$APP_ENV" OPENCRM_BASE_URL))"
    else
        warn "/healthz не отвечает"
    fi
    step "Версия и обновления"
    autoupdate status || warn "автообновление недоступно (нет python3?)"
    step "Диск"
    df -h "$(home_dir)" | tail -n 2
}

cmd_start()   { need_install; step "Запуск"; compose up -d; wait_health 60 && ok "сайт отвечает" || warn "сайт ещё поднимается"; }
cmd_stop()    { need_install; step "Остановка"; compose down; ok "остановлено"; }
cmd_restart() { need_install; step "Перезапуск"; compose restart; wait_health 60 && ok "сайт отвечает" || warn "сайт ещё поднимается"; }

cmd_logs() {
    need_install
    _service=${1:-}
    if [ -n "$_service" ]; then
        compose logs -f --tail 100 "$_service"
    else
        compose logs -f --tail 100
    fi
}

cmd_update() {
    need_install
    step "Обновление до последней версии"
    autoupdate force-update
}

cmd_autoupdate() {
    need_install
    case "${1:-}" in
        on)  autoupdate enable ;;
        off) autoupdate disable ;;
        *)
            if autoupdate status | grep -q "автообновление: включено"; then
                confirm "    Сейчас включено. Выключить?" n && autoupdate disable || info "оставляю как есть"
            else
                confirm "    Сейчас выключено. Включить?" y && autoupdate enable || info "оставляю как есть"
            fi
            ;;
    esac
}

cmd_history() { need_install; autoupdate history -n "${1:-15}"; }

cmd_backup() {
    need_install
    step "Резервная копия"
    compose exec -T app sh scripts/backup.sh
    ok "готово: $(home_dir)/data/backups"
}

cmd_restore() {
    need_install
    step "Восстановление из копии"
    _dir="$(home_dir)/data/backups/daily"
    [ -d "$_dir" ] || die "копий ещё нет ($_dir)"
    say ""
    ls -1t "$_dir"/db-*.db 2>/dev/null | head -n 10 | nl -w4 -s') '
    say ""
    _n=$(ask "    Номер копии (Enter — отмена)" "")
    [ -n "$_n" ] || { info "отменено"; return 0; }
    _db=$(ls -1t "$_dir"/db-*.db | sed -n "${_n}p")
    [ -n "$_db" ] || die "нет такого номера"
    _stamp=$(basename "$_db" | sed 's/^db-//; s/\.db$//')
    _storage="$_dir/storage-$_stamp.tar.gz"
    [ -f "$_storage" ] || die "нет пары к базе: $_storage"
    warn "текущие данные будут заменены копией от $_stamp"
    confirm "    Продолжить?" n || { info "отменено"; return 0; }
    compose stop app
    # --entrypoint sh обязателен: у образа ENTRYPOINT — это entrypoint.sh, и
    # `compose run app <команда>` передаёт команду ему аргументами, а не вместо
    # него. Без переопределения вместо восстановления поднимался бы uvicorn.
    compose run --rm -T --entrypoint sh app scripts/restore.sh \
        "/app/data/backups/daily/$(basename "$_db")" \
        "/app/data/backups/daily/$(basename "$_storage")"
    compose up -d
    wait_health 60 && ok "восстановлено, сайт отвечает" || warn "сайт не поднялся — смотрите логи"
}

cmd_https() { need_install; detect_sudo; issue_certificate; }

cmd_firewall() {
    detect_os
    detect_sudo
    if has ufw; then
        step "Что открыто сейчас"
        LC_ALL=C $SUDO ufw status verbose 2>&1 | sed 's/^/    /'
    fi
    setup_firewall
}

cmd_domain() {
    need_install
    detect_sudo
    configure_domain
    apply_env_change
    issue_certificate
    ok "готово"
}

cmd_password() {
    need_install
    step "Сброс пароля администратора"
    _email=$(ask "    Email" "$(env_get "$APP_ENV" OPENCRM_ROOT_EMAIL)")
    _password=$(ask "    Новый пароль (Enter — сгенерировать)" "")
    if [ -z "$_password" ]; then
        _password=$(gen_secret 20)
        printf '    Сгенерирован: %s%s%s\n' "$B" "$_password" "$R"
    fi
    compose exec -T app python scripts/reset_root.py --email "$_email" --password "$_password"
    ok "пароль изменён"
}

# Дополнить строку пробелами до нужной ширины.
# `%-14s` в printf считает БАЙТЫ, а кириллица в UTF-8 двухбайтная — колонка в
# таблице разъезжается ровно на длину русского слова. `wc -m` считает символы.
pad() {
    _text=$1; _width=$2
    _len=$(printf '%s' "$_text" | LC_ALL=C.UTF-8 wc -m 2>/dev/null || printf '%s' "${#_text}")
    printf '%s' "$_text"
    while [ "$_len" -lt "$_width" ]; do
        printf ' '
        _len=$((_len + 1))
    done
}

# Строка диагностики: «имя  значение», с плюсом или восклицанием.
probe() {
    _label=$1; _good=$2; _text=$3
    if [ "$_good" = "1" ]; then _mark="$GREEN+$R"; else _mark="$YELLOW!$R"; fi
    printf '    %s %s %s\n' "$_mark" "$(pad "$_label" 13)" "$_text"
}

cmd_doctor() {
    step "Диагностика"
    detect_os
    probe "система" 1 "$OS_NAME"

    if _v=$(docker_version); then probe "docker" 1 "$_v"; else probe "docker" 0 "не установлен или не отвечает"; fi
    if _c=$(compose_version) && [ -n "$_c" ]; then probe "compose" 1 "$_c"; else probe "compose" 0 "плагин compose v2 не найден"; fi
    if docker_ready; then probe "демон" 1 "отвечает"; else probe "демон" 0 "не отвечает"; fi
    if has python3; then probe "python3" 1 "$(python3 --version 2>&1 | cut -d' ' -f2)"; else probe "python3" 0 "нет — автообновление не заработает"; fi
    if has_systemd; then probe "systemd" 1 "есть"; else probe "systemd" 1 "нет (автообновление пойдёт через cron)"; fi

    if ! installed; then
        probe "конфиг" 0 "не установлено — запустите ./opencrm.sh install"
        return 0
    fi
    probe "конфиг" 1 "config/.env и docker/.env на месте"

    _home=$(home_dir)
    if [ -w "$_home" ]; then probe "состояние" 1 "$_home"; else probe "состояние" 0 "нет доступа на запись: $_home"; fi

    _uid=$(env_get "$DOCKER_ENV" OPENCRM_UID 2>/dev/null || true)
    if [ "$_uid" = "$(id -u)" ]; then
        probe "UID" 1 "$_uid — совпадает"
    else
        probe "UID" 0 "в docker/.env $_uid, у вас $(id -u) — будут ошибки доступа"
    fi

    for _key in OPENCRM_SECRET_KEY OPENCRM_IP_HASH_SALT; do
        if [ -n "$(env_get "$APP_ENV" "$_key" 2>/dev/null || true)" ]; then
            probe "$_key" 1 "задан"
        else
            probe "$_key" 0 "пуст — приложение не стартует в production"
        fi
    done

    # Секреты в .env читаемы всем — их видит любой пользователь машины.
    _mode=$(stat -c '%a' "$APP_ENV" 2>/dev/null || printf '')
    case "$_mode" in
        600|400) probe "права .env" 1 "$_mode — только владелец" ;;
        "")      probe "права .env" 1 "не проверить (нет stat)" ;;
        *)       probe "права .env" 0 "$_mode — секреты видны всем; чинится ./opencrm.sh install" ;;
    esac

    if [ -f "$REPO_DIR/docker/nginx/maintenance/maintenance.html" ]; then
        probe "заглушка" 1 "есть — при обновлении вместо 502 будет страница"
    else
        probe "заглушка" 0 "нет файла docker/nginx/maintenance/maintenance.html"
    fi

    # `ufw status` умеет только root. Диагностика не имеет права ни упасть без
    # sudo, ни зависнуть на запросе пароля, поэтому строго `sudo -n`.
    _as_root=""
    if [ "$(id -u)" -ne 0 ]; then
        if has sudo; then _as_root="sudo -n"; else _as_root="нельзя"; fi
    fi
    if ! has ufw; then
        probe "фаервол" 0 "ufw не установлен — ./opencrm.sh firewall"
    elif [ "$_as_root" = "нельзя" ]; then
        probe "фаервол" 1 "не проверить без root"
    elif ufw_is_active "$_as_root"; then
        probe "фаервол" 1 "ufw включён"
    elif LC_ALL=C ${_as_root} ufw status >/dev/null 2>&1; then
        probe "фаервол" 0 "ufw стоит, но выключен — ./opencrm.sh firewall"
    else
        probe "фаервол" 1 "не проверить без пароля — sudo ufw status"
    fi

    # Стоит пары секунд, но ловит поломку, которая иначе всплывёт через месяц
    # неудавшимся обновлением: фаервол отрезал контейнерам DNS.
    _image=$(app_image)
    if [ -z "$_image" ]; then
        probe "сеть сборки" 1 "образ не собран — нечего проверять"
    elif docker_resolves "$_image"; then
        probe "сеть сборки" 1 "контейнеры видят интернет"
    else
        probe "сеть сборки" 0 "контейнеры без DNS — обновление не соберётся; ./opencrm.sh firewall"
    fi

    _last=$(ls -1t "$(home_dir)"/data/backups/daily/db-*.db 2>/dev/null | head -n 1)
    if [ -n "$_last" ]; then
        probe "копии" 1 "последняя: $(basename "$_last" | sed 's/^db-//; s/\.db$//')"
    else
        probe "копии" 0 "ни одной копии — ./opencrm.sh backup"
    fi
    if systemctl is-enabled opencrm-backup.timer >/dev/null 2>&1 \
        || crontab -l 2>/dev/null | grep -q "opencrm.sh backup"; then
        probe "расписание" 1 "ежедневная копия запланирована"
    else
        probe "расписание" 0 "копии по расписанию не снимаются"
    fi

    _mem=$(mem_mb); _swap=$(swap_mb)
    if [ $((_mem + _swap)) -ge 1800 ]; then
        probe "память" 1 "${_mem} МБ + ${_swap} МБ подкачки"
    else
        probe "память" 0 "${_mem} МБ + ${_swap} МБ — сборке может не хватить"
    fi
    _disk=$(free_mb "$(home_dir)")
    if [ -n "$_disk" ] && [ "$_disk" -ge 5000 ]; then
        probe "диск" 1 "${_disk} МБ свободно"
    else
        probe "диск" 0 "${_disk:-?} МБ свободно — мало для сборки образа"
    fi

    if [ -d "$REPO_DIR/.git" ] && has git; then
        # Те же два флага, что у обновлятора (deploy/updater.py), иначе
        # диагностика врала бы в обе стороны: `2>/dev/null` глотал отказ git
        # работать с чужим каталогом и показывал «чисто» там, где обновление
        # падало, а бит исполнения показывал «грязно» на нетронутом дереве.
        _dirty=$(git_repo status --porcelain 2>/dev/null) || _dirty=""
        if [ -z "$_dirty" ]; then
            probe "репозиторий" 1 "чистый"
        else
            probe "репозиторий" 0 "есть несохранённые правки — автообновление остановится"
        fi
    fi
}

# --------------------------------------------------------------------------
# Меню
# --------------------------------------------------------------------------

menu_header() {
    _url=$(env_get "$APP_ENV" OPENCRM_BASE_URL 2>/dev/null || echo "—")
    if curl -fsS --max-time 2 http://127.0.0.1/healthz 2>/dev/null | grep -q '"ok"'; then
        _state="${GREEN}работает${R}"
    else
        _state="${RED}не отвечает${R}"
    fi
    printf '\n%s========================================================%s\n' "$B" "$R"
    printf '  %sOpenCRM%s  —  %s\n' "$B" "$R" "$_state"
    printf '  %s%s%s\n' "$D" "$_url" "$R"
    printf '%s========================================================%s\n' "$B" "$R"
}

menu() {
    while :; do
        menu_header
        cat <<EOF

   1) Статус и здоровье
   2) Запустить
   3) Перезапустить
   4) Остановить
   5) Обновить сейчас
   6) Автообновление: включить / выключить
   7) Журнал обновлений
   8) Логи (Ctrl+C — выйти)
   9) Резервная копия
  10) Восстановить из копии
  11) Домен и HTTPS
  12) Фаервол
  13) Сбросить пароль администратора
  14) Диагностика

   0) Выход

EOF
        _choice=$(ask "  Выбор" "0")
        case "$_choice" in
            1)  cmd_status ;;
            2)  cmd_start ;;
            3)  cmd_restart ;;
            4)  confirm "    Остановить сайт?" n && cmd_stop || info "отменено" ;;
            5)  cmd_update ;;
            6)  cmd_autoupdate ;;
            7)  cmd_history ;;
            8)  cmd_logs ;;
            9)  cmd_backup ;;
            10) cmd_restore ;;
            11) cmd_domain ;;
            12) cmd_firewall ;;
            13) cmd_password ;;
            14) cmd_doctor ;;
            0|q|Q|"") say ""; exit 0 ;;
            *)  warn "нет такого пункта" ;;
        esac
        # Явный if, а не `[ ] && exit`: под `set -e` невыполнившийся тест в конце
        # списка сам по себе завершает скрипт с ненулевым кодом.
        if [ "$ASSUME_YES" = "1" ]; then exit 0; fi
        printf '\n%s' "$D"
        ask "  Enter — вернуться в меню" "" >/dev/null
        printf '%s' "$R"
    done
}

usage() {
    cat <<EOF
OpenCRM v$VERSION — установка и управление

  ./opencrm.sh                    меню (или мастер установки при первом запуске)
  ./opencrm.sh install            установить/донастроить
  ./opencrm.sh start|stop|restart управление стеком
  ./opencrm.sh status             что развёрнуто, живо ли, есть ли обновление
  ./opencrm.sh update             обновить сейчас
  ./opencrm.sh autoupdate on|off  автообновление
  ./opencrm.sh history [N]        журнал обновлений
  ./opencrm.sh logs [сервис]      логи
  ./opencrm.sh backup|restore     резервные копии
  ./opencrm.sh domain|https       домен и сертификат
  ./opencrm.sh firewall           открыть только SSH и сайт (ufw)
  ./opencrm.sh password           сбросить пароль администратора
  ./opencrm.sh doctor             диагностика

Флаги установки (для неинтерактивного запуска):
  --domain example.com   домен сайта; --domain "" — работать по IP без HTTPS
  --email you@example.com  логин администратора и контакт для Let's Encrypt
  --yes                    не задавать вопросов, брать значения по умолчанию
EOF
}

# --------------------------------------------------------------------------

main() {
    _command=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --domain) ARG_DOMAIN=${2:-}; ARG_DOMAIN_SET=1; shift 2 ;;
            --email)  ARG_EMAIL=${2:-}; shift 2 ;;
            --yes|-y) ASSUME_YES=1; shift ;;
            -h|--help|help) usage; exit 0 ;;
            *) if [ -z "$_command" ]; then _command=$1; shift; else break; fi ;;
        esac
    done

    case "$_command" in
        install)    cmd_install ;;
        start)      cmd_start ;;
        stop)       cmd_stop ;;
        restart)    cmd_restart ;;
        status)     cmd_status ;;
        update)     cmd_update ;;
        autoupdate) cmd_autoupdate "${1:-}" ;;
        history)    cmd_history "${1:-15}" ;;
        logs)       cmd_logs "${1:-}" ;;
        backup)     cmd_backup ;;
        restore)    cmd_restore ;;
        https)      cmd_https ;;
        domain)     cmd_domain ;;
        firewall)   cmd_firewall ;;
        password)   cmd_password ;;
        doctor)     cmd_doctor ;;
        "")
            if installed; then menu; else cmd_install; fi
            ;;
        *) die "неизвестная команда: $_command (см. ./opencrm.sh help)" ;;
    esac
}

main "$@"
