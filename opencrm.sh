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

# Аргументы запуска в закавыченном виде — чтобы перезапустить себя тем же
# вызовом, когда после установки Docker приходится войти в новую группу.
SCRIPT_ARGS=""
# Префикс для обращений к docker. Пустой, когда демон доступен напрямую; после
# установки Docker в текущей сессии членства в группе ещё нет, и до перезахода
# единственный путь к демону — через sudo.
DOCKER_PREFIX=""

# --------------------------------------------------------------------------
# Вывод
# --------------------------------------------------------------------------

# Цвет включаем, только когда вывод действительно смотрит человек в терминале.
# NO_COLOR — общепринятый способ его выключить (no-color.org); OPENCRM_COLOR=0
# делает то же для тех, кто не хочет трогать общее окружение. При перенаправлении
# в файл или пайп управляющие последовательности только мешают читать лог.
if [ -t 1 ] && [ "${TERM:-dumb}" != "dumb" ] \
   && [ -z "${NO_COLOR:-}" ] && [ "${OPENCRM_COLOR:-1}" != "0" ]; then
    B=$(printf '\033[1m'); D=$(printf '\033[2m'); R=$(printf '\033[0m')
    GREEN=$(printf '\033[32m'); YELLOW=$(printf '\033[33m'); RED=$(printf '\033[31m')
    CYAN=$(printf '\033[36m')
else
    B=""; D=""; R=""; GREEN=""; YELLOW=""; RED=""; CYAN=""
fi

# Цвет несёт смысл, а не украшает: заголовок шага, успех, шум прогресса,
# предупреждение и отказ должны различаться взглядом, не чтением. Раскрашен весь
# текст сообщения, а не только значок слева, — иначе на длинной установке всё
# сливается в одну белую простыню.
# Язык вывода: ru | en. Спрашивается один раз при установке и запоминается в
# docker/.env, чтобы повторные запуски, cron и systemd говорили так же.
# OPENCRM_LANG из окружения перебивает сохранённое — это отдушина для тестов и
# для тех, кто зовёт скрипт из чужой автоматизации.
UI_LANG=${OPENCRM_LANG:-}

# Две редакции сообщения стоят рядом, а не в отдельном словаре с ключами: так
# перевод не разъезжается с кодом, а правка текста не требует помнить, где
# лежит его пара. Комментарии остаются русскими — их читает тот, кто правит
# скрипт, а не тот, кто его запускает.
tr_() {
    if [ "$UI_LANG" = "en" ]; then printf '%s' "$2"; else printf '%s' "$1"; fi
}

say()  { printf '%s\n' "$*"; }
step() { printf '\n%s==>%s %s%s%s\n' "$CYAN" "$R" "$B" "$*" "$R"; }
ok()   { printf '    %s+%s %s%s%s\n' "$GREEN" "$R" "$GREEN" "$*" "$R"; }
info() { printf '    %s.%s %s%s%s\n' "$D" "$R" "$D" "$*" "$R"; }
warn() { printf '    %s!%s %s%s%s\n' "$YELLOW" "$R" "$YELLOW" "$*" "$R"; }
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
    [ -r /etc/os-release ] || die "$(tr_ "не вижу /etc/os-release — не знаю, что за система" "no /etc/os-release — cannot tell what system this is")"
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
        die "$(tr_ "нужны права root: запустите от root или поставьте sudo" "root rights required: run as root or install sudo")"
    fi
}

has() { command -v "$1" >/dev/null 2>&1; }
has_systemd() { [ -d /run/systemd/system ]; }
docker_ready() { $DOCKER_PREFIX docker info >/dev/null 2>&1; }

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
    _v=$($DOCKER_PREFIX docker --version 2>/dev/null | head -n 1)
    case "$_v" in
        "Docker version "*) printf '%s' "$_v" | cut -d' ' -f3 | tr -d ',' ;;
        *) return 1 ;;
    esac
}

compose_version() {
    $DOCKER_PREFIX docker compose version --short 2>/dev/null | head -n 1
}

has_docker() {
    docker_version >/dev/null 2>&1 && $DOCKER_PREFIX docker compose version >/dev/null 2>&1
}

# Членство в группе docker появляется только в новой сессии, и установка на этом
# спотыкалась: `usermod -aG docker` уже отработал, а текущая оболочка о группе не
# знает — демон «не отвечает», хотя всё поставлено. Перезаходить руками посреди
# установки нельзя, поэтому перезапускаем себя внутри новой группы: все шаги
# мастера идемпотентны, повторный проход ничего не портит.
#
# Условия проверяются явными `if`, а не `&&`/`||`: под `set -e` строка вида
# `cmd && return 1` роняет функцию целиком, когда cmd не сработал.
#
# OPENCRM_REENTERED — страховка от петли: если `sg` группу почему-то не дал,
# второй заход уже не перезапускается, а доходит до понятной ошибки.
reenter_docker_group() {
    has sg || return 1
    if [ -n "${OPENCRM_REENTERED:-}" ]; then
        return 1
    fi
    if id -nG 2>/dev/null | tr ' ' '\n' | grep -qx docker; then
        return 1
    fi
    if ! getent group docker 2>/dev/null | grep -q "[:,]$(id -un)\(,\|$\)"; then
        return 1
    fi
    info "$(tr_ "членство в группе docker ещё не подхвачено — перезапускаюсь в ней" "docker group membership not picked up yet — restarting inside it")"
    OPENCRM_REENTERED=1
    export OPENCRM_REENTERED
    exec sg docker -c "$(quote_argv "$0")$SCRIPT_ARGS"
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
    step "$(tr_ "Базовые пакеты" "Base packages")"
    _missing=""
    for _pkg in git curl ca-certificates; do
        has "$_pkg" || _missing="$_missing $_pkg"
    done
    if [ -z "$_missing" ]; then
        ok "$(tr_ "git, curl, ca-certificates уже есть" "git, curl, ca-certificates already present")"
        return 0
    fi
    case "$OS_ID" in
        ubuntu|debian)
            info "$(tr_ "ставлю:$_missing" "installing:$_missing")"
            $SUDO apt-get update -qq >/dev/null
            # shellcheck disable=SC2086
            apt_install $_missing
            ok "$(tr_ "поставлено:$_missing" "installed:$_missing")"
            ;;
        *)
            die "$(tr_ "не знаю, как ставить пакеты в «$OS_NAME» — поставьте вручную:$_missing" "do not know how to install packages on \"$OS_NAME\" — install by hand:$_missing")"
            ;;
    esac
}

install_docker() {
    step "Docker"
    if has_docker; then
        ok "$(tr_ "Docker $(docker_version) и плагин compose $(compose_version) уже есть" "Docker $(docker_version) and the compose plugin $(compose_version) are already here")"
    else
        case "$OS_ID" in
            ubuntu|debian) ;;
            *) die "$(tr_ "автоустановка Docker есть только для Ubuntu/Debian; поставьте Docker и плагин compose вручную" "automatic Docker install is Ubuntu/Debian only; install Docker and the compose plugin by hand")" ;;
        esac
        # Именно из репозитория Docker, а не `apt install docker.io`: в системном
        # пакете нет плагина `docker compose` v2, на который завязан весь проект.
        info "$(tr_ "подключаю репозиторий Docker" "adding the Docker repository")"
        $SUDO install -m 0755 -d /etc/apt/keyrings
        $SUDO curl -fsSL "https://download.docker.com/linux/$OS_ID/gpg" \
            -o /etc/apt/keyrings/docker.asc
        $SUDO chmod a+r /etc/apt/keyrings/docker.asc
        printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/%s %s stable\n' \
            "$(dpkg --print-architecture)" "$OS_ID" "$OS_CODENAME" \
            | $SUDO tee /etc/apt/sources.list.d/docker.list >/dev/null
        $SUDO apt-get update -qq >/dev/null
        info "$(tr_ "ставлю docker-ce и плагины (это займёт минуту-другую)" "installing docker-ce and plugins (this takes a minute or two)")"
        apt_install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
        ok "$(tr_ "Docker поставлен" "Docker installed")"
    fi

    if has_systemd; then
        $SUDO systemctl enable --now docker >/dev/null 2>&1 || true
    elif ! docker_ready; then
        # WSL и контейнеры без systemd: демон поднимается sysv-скриптом.
        info "$(tr_ "systemd не найден — поднимаю демон через service" "no systemd — starting the daemon via service")"
        $SUDO service docker start >/dev/null 2>&1 || true
        _wait=15
        while [ "$_wait" -gt 0 ] && ! docker_ready; do sleep 1; _wait=$((_wait - 1)); done
    fi

    if [ "$(id -u)" -ne 0 ] && ! docker_ready; then
        info "$(tr_ "добавляю $(id -un) в группу docker" "adding $(id -un) to the docker group")"
        $SUDO usermod -aG docker "$(id -un)"
        # Не отправляем человека перезаходить посреди установки: сначала пробуем
        # перезапуститься в новой группе сами (эта ветка не возвращается).
        reenter_docker_group || true
    fi

    # Перезапуск не удался (нет `sg`, или он не помог) — но root у нас есть, и
    # бросать установку на полпути незачем: доводим её, обращаясь к демону через
    # sudo. После перезахода членство подхватится и префикс больше не понадобится.
    if ! docker_ready && [ -n "$SUDO" ] && $SUDO docker info >/dev/null 2>&1; then
        DOCKER_PREFIX="$SUDO"
        warn "$(tr_ "группа docker подхватится только в новой сессии — пока работаю через sudo" "the docker group applies only to a new session — using sudo for now")"
    fi

    docker_ready || die "$(tr_ "Docker не отвечает. Проверьте: $SUDO service docker start (или systemctl start docker)" "Docker is not responding. Try: $SUDO service docker start (or systemctl start docker)")"
    ok "$(tr_ "демон Docker отвечает" "Docker daemon is responding")"
}

install_python() {
    step "$(tr_ "Python для автообновления" "Python for auto-update")"
    if has python3; then
        ok "$(tr_ "python3 есть ($(python3 --version 2>&1 | cut -d' ' -f2))" "python3 present ($(python3 --version 2>&1 | cut -d' ' -f2))")"
        return 0
    fi
    case "$OS_ID" in
        ubuntu|debian)
            $SUDO apt-get update -qq >/dev/null
            apt_install python3
            ok "$(tr_ "python3 поставлен" "python3 installed")"
            ;;
        *) warn "$(tr_ "python3 не найден — автообновление работать не будет" "python3 not found — auto-update will not work")" ;;
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
        warn "$(tr_ "/swapfile уже существует — не трогаю" "/swapfile already exists — leaving it alone")"
        return 0
    fi
    info "$(tr_ "создаю файл подкачки на 2 ГБ" "creating a 2 GB swap file")"
    if ! $SUDO fallocate -l 2G /swapfile 2>/dev/null; then
        # fallocate не работает на некоторых файловых системах — тогда честный dd
        $SUDO dd if=/dev/zero of=/swapfile bs=1M count=2048 status=none 2>/dev/null || {
            warn "$(tr_ "не удалось создать файл подкачки — пропускаю" "could not create the swap file — skipping")"
            $SUDO rm -f /swapfile
            return 0
        }
    fi
    $SUDO chmod 600 /swapfile
    if ! $SUDO mkswap /swapfile >/dev/null 2>&1 || ! $SUDO swapon /swapfile 2>/dev/null; then
        warn "$(tr_ "ядро не приняло файл подкачки (бывает в OpenVZ-контейнерах) — пропускаю" "the kernel refused the swap file (happens in OpenVZ containers) — skipping")"
        $SUDO rm -f /swapfile
        return 0
    fi
    # Без записи в fstab подкачка исчезнет при первой же перезагрузке, и сборка
    # обновления через месяц упадёт по той же причине, что и сегодня.
    if ! grep -q '^/swapfile ' /etc/fstab 2>/dev/null; then
        printf '/swapfile none swap sw 0 0\n' | $SUDO tee -a /etc/fstab >/dev/null
    fi
    ok "$(tr_ "подкачка включена (2 ГБ), переживёт перезагрузку" "swap enabled (2 GB), and it survives a reboot")"
}

check_resources() {
    step "$(tr_ "Место и память" "Disk and memory")"
    _mem=$(mem_mb); _swap=$(swap_mb)
    _disk=$(free_mb /var/lib/docker)
    [ -n "$_disk" ] || _disk=$(free_mb /)
    [ -n "$_disk" ] || _disk=0
    info "$(tr_ "память ${_mem} МБ, подкачка ${_swap} МБ, свободно на диске ${_disk} МБ" "memory ${_mem} MB, swap ${_swap} MB, free disk ${_disk} MB")"

    # Образ с ffmpeg, слоями python и node_modules — это несколько гигабайт, и
    # кончившееся посреди сборки место оставляет докер в состоянии, из которого
    # он выбирается только `docker system prune`.
    if [ "$_disk" -lt 5000 ]; then
        warn "$(tr_ "меньше 5 ГБ свободно — сборке образа может не хватить места" "less than 5 GB free — the image build may run out of space")"
    fi

    # Самое прожорливое место установки — сборка фронтенда: vite держит дерево
    # модулей в памяти, и на машине с 1 ГБ без подкачки её убивает OOM-killer.
    # Симптом обманчив: npm обрывается без внятной ошибки, будто «просто не
    # собралось». Дешёвая подкачка снимает вопрос совсем.
    if [ $((_mem + _swap)) -ge 1800 ]; then
        ok "$(tr_ "памяти достаточно" "memory is sufficient")"
        return 0
    fi
    warn "$(tr_ "меньше 2 ГБ памяти вместе с подкачкой — сборка фронтенда может оборваться по OOM" "under 2 GB of memory including swap — the frontend build may be killed by OOM")"
    if [ "$_swap" -gt 0 ]; then
        say "$(tr_ "        Подкачка уже есть; если сборка всё-таки упадёт — увеличьте её." "        Swap already exists; if the build still fails, make it bigger.")"
        return 0
    fi
    if confirm "$(tr_ "    Добавить файл подкачки на 2 ГБ?" "    Add a 2 GB swap file?")" y; then
        add_swap
    else
        info "$(tr_ "пропускаю — при обрыве сборки вернитесь к этому" "skipping — come back to this if the build dies")"
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
    step "$(tr_ "Фаервол" "Firewall")"
    case "$OS_ID" in
        ubuntu|debian) ;;
        *) warn "$(tr_ "автонастройка ufw есть только для Ubuntu/Debian — закройте порты сами" "automatic ufw setup is Ubuntu/Debian only — close the ports yourself")"; return 0 ;;
    esac

    if ! has ufw; then
        if ! confirm "$(tr_ "    Поставить ufw и закрыть всё, кроме сайта и SSH?" "    Install ufw and close everything except the site and SSH?")" y; then
            info "$(tr_ "пропускаю; включить позже — ./opencrm.sh firewall" "skipping; enable later with ./opencrm.sh firewall")"
            return 0
        fi
        $SUDO apt-get update -qq >/dev/null
        apt_install ufw
        ok "$(tr_ "ufw поставлен" "ufw installed")"
    fi

    _ssh=$(ssh_ports)
    [ -n "$_ssh" ] || _ssh=22
    _was_active=0
    if ufw_is_active "$SUDO"; then _was_active=1; fi

    _ports=$(printf '%s' "$_ssh" | tr '\n' ' ')
    say "    $(tr_ "Останутся открытыми: SSH" "Will stay open: SSH") ($_ports), 80/tcp, 443/tcp"
    if [ "$_was_active" = "0" ]; then
        say "$(tr_ "    ${D}Порт SSH определён по активному подключению, слушающим сокетам и конфигу.${R}" "    ${D}The SSH port is taken from the live connection, listening sockets and config.${R}")"
        if ! confirm "$(tr_ "    Включить фаервол с этими правилами?" "    Enable the firewall with these rules?")" y; then
            info "$(tr_ "пропускаю; включить позже — ./opencrm.sh firewall" "skipping; enable later with ./opencrm.sh firewall")"
            return 0
        fi
    fi

    for _port in $_ssh; do
        $SUDO ufw allow "$_port/tcp" >/dev/null 2>&1 || warn "$(tr_ "не удалось открыть $_port/tcp" "could not open $_port/tcp")"
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
            ok "$(tr_ "фаервол включён, входящие закрыты кроме SSH и сайта" "firewall is on; incoming is closed except SSH and the site")"
        else
            warn "$(tr_ "ufw не включился — проверьте: $SUDO ufw status verbose" "ufw did not come up — check: $SUDO ufw status verbose")"
            return 0
        fi
    else
        ok "$(tr_ "фаервол уже работал — правила для сайта добавлены, политику не трогал" "firewall was already running — site rules added, policy left alone")"
    fi

    verify_docker_network

    # Это не недоработка скрипта, а устройство Docker: опубликованный порт он
    # заворачивает в PREROUTING/DOCKER, минуя цепочки, куда пишет ufw. Для 80 и
    # 443 это ровно то, что нужно. Важно другое — не считать ufw защитой от
    # случайно опубликованного порта соседнего контейнера: он не поможет.
    warn "$(tr_ "Docker публикует порты мимо ufw: 80 и 443 будут открыты, даже если их запретить." "Docker publishes ports around ufw: 80 and 443 will be open even if a rule forbids them.")"
    say "$(tr_ "        Для сайта это и нужно. Но и любой другой контейнер с \`ports:\` окажется" "        For the site that is what you want. But any other container with \`ports:\` ends up")"
    say "$(tr_ "        снаружи вопреки правилам — публикуйте только на 127.0.0.1 (docs/07-security.md)." "        exposed against the rules too — publish on 127.0.0.1 only (docs/07-security.md).")"
}

# Резолвится ли имя из НОВОГО контейнера — то есть по тому же пути, каким ходит
# сборка образа. У уже работающих контейнеров правила свои, и их благополучие
# ничего не доказывает.
docker_resolves() {
    $DOCKER_PREFIX docker run --rm --entrypoint getent "$1" hosts github.com >/dev/null 2>&1
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
        info "$(tr_ "образ ещё не собран — выход контейнеров наружу проверю после сборки" "image not built yet — container network access will be checked after the build")"
        return 0
    fi
    if docker_resolves "$_image"; then
        ok "$(tr_ "контейнеры видят сеть — сборка обновлений не пострадает" "containers can reach the network — update builds are safe")"
        return 0
    fi
    info "$(tr_ "контейнеры потеряли DNS — открываю его из подсетей Docker" "containers lost DNS — opening it for Docker subnets")"
    ensure_docker_dns
    if docker_resolves "$_image"; then
        ok "$(tr_ "починено: DNS разрешён контейнерам, наружу порт 53 закрыт" "fixed: DNS allowed for containers, port 53 stays closed to the world")"
    else
        warn "$(tr_ "контейнеры не видят сеть — следующее обновление не соберётся" "containers cannot reach the network — the next update will not build")"
        say "$(tr_ "        Проверить руками: docker run --rm --entrypoint getent \\" "        Check by hand: docker run --rm --entrypoint getent \\")"
        say "            \$(docker compose -f docker/docker-compose.yml images -q app) hosts github.com"
        say "$(tr_ "        Если не чинится — снимите фаервол: $SUDO ufw disable" "        If it stays broken, drop the firewall: $SUDO ufw disable")"
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
    step "$(tr_ "Домен" "Domain")"
    _current=$(env_get "$DOCKER_ENV" OPENCRM_DOMAIN 2>/dev/null || true)
    if [ "$ARG_DOMAIN_SET" = "1" ]; then
        _domain=$ARG_DOMAIN
    else
        say "$(tr_ "    Укажите домен, который уже смотрит A-записью на этот сервер." "    Enter a domain whose A record already points at this server.")"
        say "$(tr_ "    ${D}Пустое значение — работать по IP в локальной сети, без HTTPS.${R}" "    ${D}Leave empty to run by IP on the local network, without HTTPS.${R}")"
        _domain=$(ask "$(tr_ "    Домен" "    Domain")" "$_current")
    fi
    _domain=$(printf '%s' "$_domain" | tr -d ' ' | sed 's#^https\?://##; s#/.*##')
    env_set "$DOCKER_ENV" OPENCRM_DOMAIN "$_domain"

    if [ -z "$_domain" ]; then
        _ip=$(lan_ip)
        [ -n "$_ip" ] || _ip="127.0.0.1"
        env_set "$APP_ENV" OPENCRM_BASE_URL "http://$_ip"
        ok "$(tr_ "без домена: сайт будет доступен по http://$_ip" "no domain: the site will be reachable at http://$_ip")"
        return 0
    fi

    # Схема в BASE_URL — не украшение: по ней приложение решает, ставить ли
    # cookie флаг Secure. Написать https:// до выпуска сертификата значит выдать
    # Secure-cookie по обычному HTTP, а её браузер молча выбросит — вход стал бы
    # «залогинился и тут же вылетел». Поэтому https появляется только вместе с
    # сертификатом (см. issue_certificate).
    if [ -d "$(home_dir)/letsencrypt/live/$_domain" ]; then
        env_set "$APP_ENV" OPENCRM_BASE_URL "https://$_domain"
        ok "$(tr_ "домен: $_domain (сертификат на месте)" "domain: $_domain (certificate in place)")"
    else
        env_set "$APP_ENV" OPENCRM_BASE_URL "http://$_domain"
        ok "$(tr_ "домен: $_domain (пока по HTTP, до выпуска сертификата)" "domain: $_domain (HTTP for now, until the certificate is issued)")"
    fi
}

# Секреты генерируются один раз. Перегенерация на повторном запуске разлогинила
# бы всех и обесценила бы выданные PIN-ссылки, поэтому непустые значения не
# трогаем никогда.
seed_secret() {
    _key=$1
    _existing=$(env_get "$APP_ENV" "$_key" 2>/dev/null || true)
    if [ -n "$_existing" ]; then
        info "$(tr_ "$_key уже задан — не трогаю" "$_key already set — leaving it alone")"
    else
        env_set "$APP_ENV" "$_key" "$(gen_secret 64)"
        ok "$(tr_ "$_key сгенерирован" "$_key generated")"
    fi
}

ROOT_PASSWORD_SHOWN=""
configure_app_env() {
    step "$(tr_ "Настройки приложения" "Application settings")"
    if [ ! -f "$APP_ENV" ]; then
        cp "$REPO_DIR/config/.env.example" "$APP_ENV"
        chmod 600 "$APP_ENV"
        ok "$(tr_ "создан config/.env из шаблона" "config/.env created from the template")"
    else
        ok "$(tr_ "config/.env уже есть — дополняю недостающее" "config/.env already exists — filling in what is missing")"
    fi

    env_set "$APP_ENV" OPENCRM_ENV production
    env_set "$APP_ENV" OPENCRM_TRUSTED_PROXY_HOPS 1
    seed_secret OPENCRM_SECRET_KEY
    seed_secret OPENCRM_IP_HASH_SALT

    _root_email=$(env_get "$APP_ENV" OPENCRM_ROOT_EMAIL 2>/dev/null || true)
    if [ -z "$_root_email" ]; then
        # --email один на двоих: и логин администратора, и контакт для Let's
        # Encrypt. Спрашивать одно и то же дважды у одного человека незачем.
        _root_email=$(ask "$(tr_ "    Email администратора (логин в CRM)" "    Admin email (the CRM login)")" \
            "${ARG_EMAIL:-admin@${ARG_DOMAIN:-opencrm.local}}")
        env_set "$APP_ENV" OPENCRM_ROOT_EMAIL "$_root_email"
    fi
    ok "$(tr_ "администратор: $_root_email" "admin: $_root_email")"

    _root_password=$(env_get "$APP_ENV" OPENCRM_ROOT_PASSWORD 2>/dev/null || true)
    if [ -z "$_root_password" ]; then
        ROOT_PASSWORD_SHOWN=$(gen_secret 20)
        env_set "$APP_ENV" OPENCRM_ROOT_PASSWORD "$ROOT_PASSWORD_SHOWN"
        ok "$(tr_ "пароль администратора сгенерирован (покажу в конце)" "admin password generated (shown at the end)")"
    fi
}

# Язык запоминается в docker/.env, а не в отдельном файле: он и так есть на
# каждой установке, лежит рядом с остальными настройками стека и переживает
# обновления. Спрашиваем один раз — при повторных запусках берём сохранённое.
load_language() {
    if [ -n "$UI_LANG" ]; then
        return 0
    fi
    if [ -f "$DOCKER_ENV" ]; then
        UI_LANG=$(env_get "$DOCKER_ENV" OPENCRM_LANG 2>/dev/null || true)
    fi
    if [ -z "$UI_LANG" ]; then
        UI_LANG=ru
    fi
}

choose_language() {
    # Задан снаружи или уже сохранён — не переспрашиваем
    if [ -n "${OPENCRM_LANG:-}" ]; then
        return 0
    fi
    if [ -f "$DOCKER_ENV" ] && [ -n "$(env_get "$DOCKER_ENV" OPENCRM_LANG 2>/dev/null || true)" ]; then
        load_language
        return 0
    fi
    say ""
    menu_item 1 "Русский"
    menu_item 2 "English"
    _pick=$(ask "  Язык / Language" "1")
    case "$_pick" in
        2|e|en|EN|eng|english|English) UI_LANG=en ;;
        *) UI_LANG=ru ;;
    esac
}

configure_docker_env() {
    step "$(tr_ "Настройки compose" "Compose settings")"
    [ -f "$DOCKER_ENV" ] || cp "$REPO_DIR/docker/.env.example" "$DOCKER_ENV"
    env_set "$DOCKER_ENV" OPENCRM_LANG "$UI_LANG"
    # Контейнер пишет в примонтированные каталоги под этим UID — не совпадёт
    # с владельцем, и первая же миграция упрётся в permission denied.
    env_set "$DOCKER_ENV" OPENCRM_UID "$(id -u)"
    env_set "$DOCKER_ENV" OPENCRM_GID "$(id -g)"
    # Путь задаём явно: у systemd-службы автообновления $HOME не обязан
    # совпадать с вашим, а разойдись он — compose примонтирует другие каталоги.
    _home=$(env_get "$DOCKER_ENV" OPENCRM_HOME 2>/dev/null || true)
    [ -n "$_home" ] || _home="$HOME/opencrm"
    env_set "$DOCKER_ENV" OPENCRM_HOME "$_home"
    ok "$(tr_ "UID:GID $(id -u):$(id -g), состояние в $_home" "UID:GID $(id -u):$(id -g), state in $_home")"
}

create_dirs() {
    step "$(tr_ "Каталоги состояния" "State directories")"
    _home=$(home_dir)
    for _sub in data storage letsencrypt acme updates; do
        mkdir -p "$_home/$_sub"
    done
    ok "$_home/{data,storage,letsencrypt,acme,updates}"
}

compose() {
    $DOCKER_PREFIX docker compose -f "$COMPOSE_FILE" "$@"
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
        ok "$(tr_ "приложение перезапущено с новыми настройками" "application restarted with the new settings")"
    else
        warn "$(tr_ "приложение не ответило за 3 минуты — смотрите ./opencrm.sh logs app" "no answer from the application in 3 minutes — see ./opencrm.sh logs app")"
    fi
}

build_and_start() {
    step "$(tr_ "Сборка и запуск" "Build and start")"
    # Фаервол мог стоять на сервере и до нас. Тогда контейнеру сборки нечем
    # резолвить имена, и установка обрывается на `pip install` невнятной ошибкой,
    # в которой про фаервол ни слова. Разрешение узкое, лишним не будет.
    ensure_docker_dns
    info "$(tr_ "первая сборка занимает 3-10 минут: собирается фронтенд и ставится ffmpeg" "the first build takes 3-10 minutes: frontend compile plus ffmpeg install")"
    compose up -d --build
    ok "$(tr_ "контейнеры подняты" "containers are up")"
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
    step "$(tr_ "Проверка, что сайт живой" "Checking that the site is alive")"
    if wait_health 90; then
        ok "$(tr_ "/healthz отвечает ok" "/healthz answers ok")"
    else
        warn "$(tr_ "сайт не ответил за 3 минуты" "no answer from the site in 3 minutes")"
        say ""
        compose logs --tail 40 app || true
        die "$(tr_ "не удалось дождаться /healthz — логи выше" "gave up waiting for /healthz — logs above")"
    fi
}

issue_certificate() {
    _domain=$(env_get "$DOCKER_ENV" OPENCRM_DOMAIN 2>/dev/null || true)
    [ -n "$_domain" ] || return 0

    step "$(tr_ "HTTPS для $_domain" "HTTPS for $_domain")"
    _home=$(home_dir)
    if [ -d "$_home/letsencrypt/live/$_domain" ]; then
        ok "$(tr_ "сертификат уже выпущен" "certificate already issued")"
        return 0
    fi

    # Let's Encrypt проверяет домен по HTTP, и если A-запись смотрит не сюда,
    # запрос всё равно провалится — только потратит попытку из недельного лимита.
    _server_ip=$(public_ip)
    _dns_ip=$(domain_ip "$_domain")
    if [ -z "$_dns_ip" ]; then
        warn "$(tr_ "домен $_domain не резолвится — сертификат не выпускаю" "$_domain does not resolve — not requesting a certificate")"
        say "$(tr_ "        Настройте A-запись и повторите: ./opencrm.sh https" "        Set the A record and retry: ./opencrm.sh https")"
        return 0
    fi
    if [ -n "$_server_ip" ] && [ "$_dns_ip" != "$_server_ip" ]; then
        warn "$(tr_ "A-запись $_domain ведёт на $_dns_ip, а сервер — $_server_ip" "the A record of $_domain points at $_dns_ip, but this server is $_server_ip")"
        say "$(tr_ "        Let's Encrypt проверку не пройдёт. Повторите позже: ./opencrm.sh https" "        Let's Encrypt will fail the challenge. Retry later: ./opencrm.sh https")"
        return 0
    fi
    ok "$(tr_ "A-запись совпадает с адресом сервера ($_dns_ip)" "the A record matches this server ($_dns_ip)")"

    _email=$ARG_EMAIL
    [ -n "$_email" ] || _email=$(ask "$(tr_ "    Email для Let's Encrypt (уведомления об истечении)" "    Email for Let's Encrypt (expiry notifications)")" \
        "$(env_get "$APP_ENV" OPENCRM_ROOT_EMAIL 2>/dev/null || true)")
    [ -n "$_email" ] || { warn "$(tr_ "без email сертификат не выпустить" "no certificate without an email")"; return 0; }

    info "$(tr_ "запрашиваю сертификат" "requesting the certificate")"
    if compose run --rm certbot certonly --webroot -w /var/www/certbot \
        -d "$_domain" --email "$_email" --agree-tos --no-eff-email --non-interactive; then
        # Теперь сайт правда за TLS — можно и нужно переводить BASE_URL на https,
        # чтобы cookie получили флаг Secure. Настройки читаются при старте
        # процесса, поэтому контейнер приложения пересоздаётся, а не просто ждёт.
        env_set "$APP_ENV" OPENCRM_BASE_URL "https://$_domain"
        apply_env_change
        compose restart nginx
        ok "$(tr_ "HTTPS включён, cookie получили флаг Secure, продление идёт само" "HTTPS is on, cookies got the Secure flag, renewal runs by itself")"
    else
        warn "$(tr_ "certbot не справился — сайт остаётся на HTTP" "certbot failed — the site stays on HTTP")"
        say "$(tr_ "        Повторить: ./opencrm.sh https" "        Retry: ./opencrm.sh https")"
    fi
}

setup_autoupdate() {
    step "$(tr_ "Автообновление из GitHub" "Auto-update from GitHub")"
    _home=$(home_dir)
    _env_file="$_home/autoupdate.env"

    # Обновление умеет только `git fetch` — из распакованного архива обновляться
    # неоткуда. Сказать об этом здесь честнее, чем ошибкой раз в пять минут.
    if [ ! -d "$REPO_DIR/.git" ]; then
        warn "$(tr_ "$REPO_DIR — не git-репозиторий, автообновлению неоткуда брать версии" "$REPO_DIR is not a git repository — auto-update has nowhere to take versions from")"
        say "$(tr_ "        Разверните через: git clone https://github.com/DenisHumen/OpenCRM.git" "        Deploy with: git clone https://github.com/DenisHumen/OpenCRM.git")"
        return 0
    fi

    if [ ! -f "$_env_file" ]; then
        cp "$REPO_DIR/deploy/autoupdate.env.example" "$_env_file"
        env_set "$_env_file" OPENCRM_HOME "$_home"
        env_set "$_env_file" OPENCRM_UPDATE_PROJECT_DIR "$REPO_DIR"
        chmod 600 "$_env_file"
        ok "$(tr_ "создан $_env_file" "$_env_file created")"
    else
        ok "$(tr_ "$_env_file уже есть" "$_env_file already exists")"
    fi

    if ! confirm "$(tr_ "    Включить автообновление (сайт сам подтянет новые коммиты)?" "    Enable auto-update (the site pulls new commits by itself)?")" y; then
        info "$(tr_ "пропускаю; включить позже — пункт меню «Автообновление»" "skipping; enable later from the \"Auto-update\" menu item")"
        return 0
    fi

    _token=$(ask "$(tr_ "    Telegram-токен бота для уведомлений (Enter — без уведомлений)" "    Telegram bot token for notifications (Enter — no notifications)")" "")
    if [ -n "$_token" ]; then
        _chat=$(ask "    Telegram chat_id" "")
        env_set "$_env_file" OPENCRM_UPDATE_TELEGRAM_TOKEN "$_token"
        env_set "$_env_file" OPENCRM_UPDATE_TELEGRAM_CHAT "$_chat"
        ok "$(tr_ "уведомления в Telegram настроены" "Telegram notifications configured")"
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
        ok "$(tr_ "служба opencrm-autoupdate запущена (логи: journalctl -u opencrm-autoupdate -f)" "opencrm-autoupdate service started (logs: journalctl -u opencrm-autoupdate -f)")"
    else
        # WSL и минимальные образы: systemd нет, но cron делает то же самое.
        _line="*/5 * * * * cd $REPO_DIR && /usr/bin/python3 scripts/autoupdate.py check >/dev/null 2>&1"
        if crontab -l 2>/dev/null | grep -q "autoupdate.py"; then
            ok "$(tr_ "задание cron уже стоит" "the cron job is already in place")"
        elif has crontab; then
            (crontab -l 2>/dev/null || true; printf '%s\n' "$_line") | crontab -
            ok "$(tr_ "systemd нет — поставил задание cron раз в 5 минут" "no systemd — added a cron job every 5 minutes")"
        else
            warn "$(tr_ "нет ни systemd, ни cron — запускайте обновление вручную (пункт меню)" "neither systemd nor cron — run updates by hand (menu item)")"
        fi
    fi
}

# Копии не спрашивают разрешения: скрипты бэкапа лежали в репозитории с самого
# начала, но запускать их было некому — «резервное копирование настроено» на
# бумаге и ни одной копии на диске. Расписание ставится молча, снять его —
# отдельная сознательная команда.
setup_backups() {
    step "$(tr_ "Ежедневные копии" "Daily backups")"
    if has_systemd; then
        _dst=/etc/systemd/system/opencrm-backup
        sed -e "s#^User=.*#User=$(id -un)#" \
            -e "s#^WorkingDirectory=.*#WorkingDirectory=$REPO_DIR#" \
            -e "s#^ExecStart=.*#ExecStart=$REPO_DIR/opencrm.sh backup#" \
            "$REPO_DIR/deploy/systemd/opencrm-backup.service" | $SUDO tee "$_dst.service" >/dev/null
        $SUDO cp "$REPO_DIR/deploy/systemd/opencrm-backup.timer" "$_dst.timer"
        $SUDO systemctl daemon-reload
        if $SUDO systemctl enable --now opencrm-backup.timer >/dev/null 2>&1; then
            ok "$(tr_ "копия снимается каждую ночь (systemctl list-timers opencrm-backup)" "a backup is taken every night (systemctl list-timers opencrm-backup)")"
        else
            warn "$(tr_ "таймер не запустился — снимайте копии вручную (пункт меню)" "the timer did not start — take backups by hand (menu item)")"
        fi
    elif has crontab; then
        _line="30 3 * * * cd $REPO_DIR && ./opencrm.sh backup >/dev/null 2>&1"
        if crontab -l 2>/dev/null | grep -q "opencrm.sh backup"; then
            ok "$(tr_ "задание cron уже стоит" "the cron job is already in place")"
        else
            (crontab -l 2>/dev/null || true; printf '%s\n' "$_line") | crontab -
            ok "$(tr_ "systemd нет — поставил задание cron на 3:30" "no systemd — added a cron job at 03:30")"
        fi
    else
        warn "$(tr_ "нет ни systemd, ни cron — снимайте копии вручную (пункт меню)" "neither systemd nor cron — take backups by hand (menu item)")"
        return 0
    fi
    # Копия на том же диске спасает от испорченной базы и от собственной ошибки,
    # но не от смерти диска и не от потери сервера. Сказать это честно дешевле,
    # чем однажды обнаружить, что копии были ровно там же, где оригинал.
    info "$(tr_ "копии лежат в $(home_dir)/data/backups — на том же диске, что и база" "backups live in $(home_dir)/data/backups — the same disk as the database")"
    say "$(tr_ "        Выгрузка наружу настраивается в scripts/backup.sh (там же пример)." "        Off-site upload is configured in scripts/backup.sh (example included).")"
}

show_summary() {
    _domain=$(env_get "$DOCKER_ENV" OPENCRM_DOMAIN 2>/dev/null || true)
    _url=$(env_get "$APP_ENV" OPENCRM_BASE_URL 2>/dev/null || true)
    _email=$(env_get "$APP_ENV" OPENCRM_ROOT_EMAIL 2>/dev/null || true)

    printf '\n%s========================================================%s\n' "$B" "$R"
    printf '%s  OpenCRM развёрнут%s\n' "$B" "$R"
    printf '%s========================================================%s\n\n' "$B" "$R"
    say "$(tr_ "  Адрес:  $_url" "  Address:  $_url")"
    say "$(tr_ "  Логин:  $_email" "  Login:    $_email")"
    if [ -n "$ROOT_PASSWORD_SHOWN" ]; then
        printf '  Пароль: %s%s%s\n' "$B" "$ROOT_PASSWORD_SHOWN" "$R"
        say ""
        warn "$(tr_ "Пароль показан один раз. При первом входе система попросит его сменить." "The password is shown once. The first sign-in will ask you to change it.")"
        say "$(tr_ "        Забыли — ./opencrm.sh password" "        Lost it — ./opencrm.sh password")"
    else
        say "$(tr_ "  Пароль: задан ранее (сброс — ./opencrm.sh password)" "  Password: set earlier (reset — ./opencrm.sh password)")"
    fi
    case "$_url" in
        https://*) ;;
        *)
            say ""
            warn "$(tr_ "Сайт работает по HTTP — пароли и cookie идут по сети открытым текстом." "The site runs over HTTP — passwords and cookies travel in clear text.")"
            if [ -n "$_domain" ]; then
                say "$(tr_ "        Домен задан, но сертификата нет. Когда A-запись заработает: ./opencrm.sh https" "        A domain is set but there is no certificate. Once the A record works: ./opencrm.sh https")"
            else
                say "$(tr_ "        Годится для локальной сети. Для публичного сайта задайте домен: ./opencrm.sh domain" "        Fine for a local network. For a public site set a domain: ./opencrm.sh domain")"
            fi
            ;;
    esac
    say ""
    say "$(tr_ "  Дальше всё делается через меню: ${B}./opencrm.sh${R}" "  Everything else is done from the menu: ${B}./opencrm.sh${R}")"
    say ""
}

cmd_install() {
    # Язык — самый первый вопрос: всё, что скрипт скажет дальше, зависит от него
    choose_language
    printf '\n%s  %s (v%s)%s\n' "$B" "$(tr_ "OpenCRM — установка" "OpenCRM — install")" "$VERSION" "$R"
    detect_os
    say "$(tr_ "    Система: $OS_NAME" "    System: $OS_NAME")"
    case "$OS_ID" in
        ubuntu|debian) ;;
        *) warn "$(tr_ "проверялось на Ubuntu 24.04; «$OS_NAME» может потребовать ручных шагов" "tested on Ubuntu 24.04; \"$OS_NAME\" may need manual steps")" ;;
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
    installed || die "$(tr_ "сайт ещё не установлен — запустите ./opencrm.sh install" "the site is not installed yet — run ./opencrm.sh install")"
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
    step "$(tr_ "Контейнеры" "Containers")"
    compose ps
    step "$(tr_ "Сайт" "Site")"
    if curl -fsS --max-time 3 http://127.0.0.1/healthz 2>/dev/null | grep -q '"ok"'; then
        ok "$(tr_ "/healthz отвечает ok" "/healthz answers ok")  ($(env_get "$APP_ENV" OPENCRM_BASE_URL))"
    else
        warn "$(tr_ "/healthz не отвечает" "/healthz is not answering")"
    fi
    step "$(tr_ "Версия и обновления" "Version and updates")"
    autoupdate status || warn "$(tr_ "автообновление недоступно (нет python3?)" "auto-update unavailable (no python3?)")"
    step "$(tr_ "Диск" "Disk")"
    df -h "$(home_dir)" | tail -n 2
}

cmd_start()   { need_install; step "$(tr_ "Запуск" "Start")"; compose up -d; wait_health 60 && ok "$(tr_ "сайт отвечает" "the site is answering")" || warn "$(tr_ "сайт ещё поднимается" "the site is still coming up")"; }
cmd_stop()    { need_install; step "$(tr_ "Остановка" "Stop")"; compose down; ok "$(tr_ "остановлено" "stopped")"; }
cmd_restart() { need_install; step "$(tr_ "Перезапуск" "Restart")"; compose restart; wait_health 60 && ok "$(tr_ "сайт отвечает" "the site is answering")" || warn "$(tr_ "сайт ещё поднимается" "the site is still coming up")"; }

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
    step "$(tr_ "Обновление до последней версии" "Updating to the latest version")"
    autoupdate force-update
}

cmd_autoupdate() {
    need_install
    case "${1:-}" in
        on)  autoupdate enable ;;
        off) autoupdate disable ;;
        *)
            if autoupdate status | grep -q "$(tr_ "автообновление: включено" "auto-update: enabled")"; then
                confirm "$(tr_ "    Сейчас включено. Выключить?" "    Currently enabled. Disable it?")" n && autoupdate disable || info "$(tr_ "оставляю как есть" "leaving it as is")"
            else
                confirm "$(tr_ "    Сейчас выключено. Включить?" "    Currently disabled. Enable it?")" y && autoupdate enable || info "$(tr_ "оставляю как есть" "leaving it as is")"
            fi
            ;;
    esac
}

cmd_history() { need_install; autoupdate history -n "${1:-15}"; }

cmd_backup() {
    need_install
    step "$(tr_ "Резервная копия" "Backup")"
    compose exec -T app sh scripts/backup.sh
    ok "$(tr_ "готово: $(home_dir)/data/backups" "done: $(home_dir)/data/backups")"
}

cmd_restore() {
    need_install
    step "$(tr_ "Восстановление из копии" "Restore from backup")"
    _dir="$(home_dir)/data/backups/daily"
    [ -d "$_dir" ] || die "$(tr_ "копий ещё нет ($_dir)" "no backups yet ($_dir)")"
    say ""
    ls -1t "$_dir"/db-*.db 2>/dev/null | head -n 10 | nl -w4 -s') '
    say ""
    _n=$(ask "$(tr_ "    Номер копии (Enter — отмена)" "    Backup number (Enter — cancel)")" "")
    [ -n "$_n" ] || { info "$(tr_ "отменено" "cancelled")"; return 0; }
    _db=$(ls -1t "$_dir"/db-*.db | sed -n "${_n}p")
    [ -n "$_db" ] || die "$(tr_ "нет такого номера" "no such number")"
    _stamp=$(basename "$_db" | sed 's/^db-//; s/\.db$//')
    _storage="$_dir/storage-$_stamp.tar.gz"
    [ -f "$_storage" ] || die "$(tr_ "нет пары к базе: $_storage" "no storage archive to match the database: $_storage")"
    warn "$(tr_ "текущие данные будут заменены копией от $_stamp" "current data will be replaced by the backup from $_stamp")"
    confirm "$(tr_ "    Продолжить?" "    Continue?")" n || { info "$(tr_ "отменено" "cancelled")"; return 0; }
    compose stop app
    # --entrypoint sh обязателен: у образа ENTRYPOINT — это entrypoint.sh, и
    # `compose run app <команда>` передаёт команду ему аргументами, а не вместо
    # него. Без переопределения вместо восстановления поднимался бы uvicorn.
    compose run --rm -T --entrypoint sh app scripts/restore.sh \
        "/app/data/backups/daily/$(basename "$_db")" \
        "/app/data/backups/daily/$(basename "$_storage")"
    compose up -d
    wait_health 60 && ok "$(tr_ "восстановлено, сайт отвечает" "restored, the site is answering")" || warn "$(tr_ "сайт не поднялся — смотрите логи" "the site did not come up — check the logs")"
}

cmd_https() { need_install; detect_sudo; issue_certificate; }

cmd_firewall() {
    detect_os
    detect_sudo
    if has ufw; then
        step "$(tr_ "Что открыто сейчас" "What is open right now")"
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
    ok "$(tr_ "готово" "done")"
}

cmd_password() {
    need_install
    step "$(tr_ "Сброс пароля администратора" "Resetting the admin password")"
    _email=$(ask "    Email" "$(env_get "$APP_ENV" OPENCRM_ROOT_EMAIL)")
    _password=$(ask "$(tr_ "    Новый пароль (Enter — сгенерировать)" "    New password (Enter — generate one)")" "")
    if [ -z "$_password" ]; then
        _password=$(gen_secret 20)
        printf '    Сгенерирован: %s%s%s\n' "$B" "$_password" "$R"
    fi
    compose exec -T app python scripts/reset_root.py --email "$_email" --password "$_password"
    ok "$(tr_ "пароль изменён" "password changed")"
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
#
# Целиком красим только проблемные строки. Если раскрасить и хорошие, экран
# станет ровно зелёным, и то единственное, ради чего диагностику открывают,
# опять потеряется среди остального. Названия приглушены — это разметка, а не
# новость; глаз идёт по значениям.
probe() {
    _label=$1; _good=$2; _text=$3
    if [ "$_good" = "1" ]; then
        printf '    %s+%s %s%s%s %s\n' "$GREEN" "$R" "$D" "$(pad "$_label" 13)" "$R" "$_text"
    else
        printf '    %s!%s %s%s %s%s\n' "$YELLOW" "$R" "$YELLOW" "$(pad "$_label" 13)" "$_text" "$R"
    fi
}

cmd_doctor() {
    step "$(tr_ "Диагностика" "Diagnostics")"
    detect_os
    probe "$(tr_ "система" "system")" 1 "$OS_NAME"

    if _v=$(docker_version); then probe "docker" 1 "$_v"; else probe "docker" 0 "$(tr_ "не установлен или не отвечает" "not installed or not responding")"; fi
    if _c=$(compose_version) && [ -n "$_c" ]; then probe "compose" 1 "$_c"; else probe "compose" 0 "$(tr_ "плагин compose v2 не найден" "compose v2 plugin not found")"; fi
    if docker_ready; then probe "$(tr_ "демон" "daemon")" 1 "$(tr_ "отвечает" "responding")"; else probe "$(tr_ "демон" "daemon")" 0 "$(tr_ "не отвечает" "not responding")"; fi
    if has python3; then probe "python3" 1 "$(python3 --version 2>&1 | cut -d' ' -f2)"; else probe "python3" 0 "$(tr_ "нет — автообновление не заработает" "missing — auto-update will not run")"; fi
    if has_systemd; then probe "systemd" 1 "$(tr_ "есть" "present")"; else probe "systemd" 1 "$(tr_ "нет (автообновление пойдёт через cron)" "absent (auto-update will use cron)")"; fi

    if ! installed; then
        probe "$(tr_ "конфиг" "config")" 0 "$(tr_ "не установлено — запустите ./opencrm.sh install" "not installed — run ./opencrm.sh install")"
        return 0
    fi
    probe "$(tr_ "конфиг" "config")" 1 "$(tr_ "config/.env и docker/.env на месте" "config/.env and docker/.env are in place")"

    _home=$(home_dir)
    if [ -w "$_home" ]; then probe "$(tr_ "состояние" "state")" 1 "$_home"; else probe "$(tr_ "состояние" "state")" 0 "$(tr_ "нет доступа на запись: $_home" "not writable: $_home")"; fi

    _uid=$(env_get "$DOCKER_ENV" OPENCRM_UID 2>/dev/null || true)
    if [ "$_uid" = "$(id -u)" ]; then
        probe "UID" 1 "$(tr_ "$_uid — совпадает" "$_uid — matches")"
    else
        probe "UID" 0 "$(tr_ "в docker/.env $_uid, у вас $(id -u) — будут ошибки доступа" "docker/.env says $_uid, you are $(id -u) — expect permission errors")"
    fi

    for _key in OPENCRM_SECRET_KEY OPENCRM_IP_HASH_SALT; do
        if [ -n "$(env_get "$APP_ENV" "$_key" 2>/dev/null || true)" ]; then
            probe "$_key" 1 "$(tr_ "задан" "set")"
        else
            probe "$_key" 0 "$(tr_ "пуст — приложение не стартует в production" "empty — the application will not start in production")"
        fi
    done

    # Секреты в .env читаемы всем — их видит любой пользователь машины.
    _mode=$(stat -c '%a' "$APP_ENV" 2>/dev/null || printf '')
    case "$_mode" in
        600|400) probe "$(tr_ "права .env" ".env mode")" 1 "$(tr_ "$_mode — только владелец" "$_mode — owner only")" ;;
        "")      probe "$(tr_ "права .env" ".env mode")" 1 "$(tr_ "не проверить (нет stat)" "cannot check (no stat)")" ;;
        *)       probe "$(tr_ "права .env" ".env mode")" 0 "$(tr_ "$_mode — секреты видны всем; чинится ./opencrm.sh install" "$_mode — secrets readable by everyone; fixed by ./opencrm.sh install")" ;;
    esac

    if [ -f "$REPO_DIR/docker/nginx/maintenance/maintenance.html" ]; then
        probe "$(tr_ "заглушка" "fallback page")" 1 "$(tr_ "есть — при обновлении вместо 502 будет страница" "present — an update shows a page instead of 502")"
    else
        probe "$(tr_ "заглушка" "fallback page")" 0 "$(tr_ "нет файла docker/nginx/maintenance/maintenance.html" "docker/nginx/maintenance/maintenance.html is missing")"
    fi

    # `ufw status` умеет только root. Диагностика не имеет права ни упасть без
    # sudo, ни зависнуть на запросе пароля, поэтому строго `sudo -n`.
    _as_root=""
    if [ "$(id -u)" -ne 0 ]; then
        if has sudo; then _as_root="sudo -n"; else _as_root="$(tr_ "нельзя" "no")"; fi
    fi
    if ! has ufw; then
        probe "$(tr_ "фаервол" "firewall")" 0 "$(tr_ "ufw не установлен — ./opencrm.sh firewall" "ufw not installed — ./opencrm.sh firewall")"
    elif [ "$_as_root" = "$(tr_ "нельзя" "no")" ]; then
        probe "$(tr_ "фаервол" "firewall")" 1 "$(tr_ "не проверить без root" "cannot check without root")"
    elif ufw_is_active "$_as_root"; then
        probe "$(tr_ "фаервол" "firewall")" 1 "$(tr_ "ufw включён" "ufw is on")"
    elif LC_ALL=C ${_as_root} ufw status >/dev/null 2>&1; then
        probe "$(tr_ "фаервол" "firewall")" 0 "$(tr_ "ufw стоит, но выключен — ./opencrm.sh firewall" "ufw is installed but off — ./opencrm.sh firewall")"
    else
        probe "$(tr_ "фаервол" "firewall")" 1 "$(tr_ "не проверить без пароля — sudo ufw status" "cannot check without a password — sudo ufw status")"
    fi

    # Стоит пары секунд, но ловит поломку, которая иначе всплывёт через месяц
    # неудавшимся обновлением: фаервол отрезал контейнерам DNS.
    _image=$(app_image)
    if [ -z "$_image" ]; then
        probe "$(tr_ "сеть сборки" "build network")" 1 "$(tr_ "образ не собран — нечего проверять" "image not built — nothing to check")"
    elif docker_resolves "$_image"; then
        probe "$(tr_ "сеть сборки" "build network")" 1 "$(tr_ "контейнеры видят интернет" "containers can reach the internet")"
    else
        probe "$(tr_ "сеть сборки" "build network")" 0 "$(tr_ "контейнеры без DNS — обновление не соберётся; ./opencrm.sh firewall" "containers have no DNS — updates will not build; ./opencrm.sh firewall")"
    fi

    _last=$(ls -1t "$(home_dir)"/data/backups/daily/db-*.db 2>/dev/null | head -n 1)
    if [ -n "$_last" ]; then
        _stamp_last=$(basename "$_last" | sed 's/^db-//; s/\.db$//')
        probe "$(tr_ "копии" "backups")" 1 "$(tr_ "последняя" "latest"): $_stamp_last"
    else
        probe "$(tr_ "копии" "backups")" 0 "$(tr_ "ни одной копии — ./opencrm.sh backup" "none yet — ./opencrm.sh backup")"
    fi
    if systemctl is-enabled opencrm-backup.timer >/dev/null 2>&1 \
        || crontab -l 2>/dev/null | grep -q "opencrm.sh backup"; then
        probe "$(tr_ "расписание" "schedule")" 1 "$(tr_ "ежедневная копия запланирована" "a daily backup is scheduled")"
    else
        probe "$(tr_ "расписание" "schedule")" 0 "$(tr_ "копии по расписанию не снимаются" "no scheduled backups")"
    fi

    _mem=$(mem_mb); _swap=$(swap_mb)
    if [ $((_mem + _swap)) -ge 1800 ]; then
        probe "$(tr_ "память" "memory")" 1 "$(tr_ "${_mem} МБ + ${_swap} МБ подкачки" "${_mem} MB + ${_swap} MB swap")"
    else
        probe "$(tr_ "память" "memory")" 0 "$(tr_ "${_mem} МБ + ${_swap} МБ — сборке может не хватить" "${_mem} MB + ${_swap} MB — the build may run short")"
    fi
    _disk=$(free_mb "$(home_dir)")
    if [ -n "$_disk" ] && [ "$_disk" -ge 5000 ]; then
        probe "$(tr_ "диск" "disk")" 1 "$(tr_ "${_disk} МБ свободно" "${_disk} MB free")"
    else
        probe "$(tr_ "диск" "disk")" 0 "$(tr_ "${_disk:-?} МБ свободно — мало для сборки образа" "${_disk:-?} MB free — too little for an image build")"
    fi

    if [ -d "$REPO_DIR/.git" ] && has git; then
        # Те же два флага, что у обновлятора (deploy/updater.py), иначе
        # диагностика врала бы в обе стороны: `2>/dev/null` глотал отказ git
        # работать с чужим каталогом и показывал «чисто» там, где обновление
        # падало, а бит исполнения показывал «грязно» на нетронутом дереве.
        _dirty=$(git_repo status --porcelain 2>/dev/null) || _dirty=""
        if [ -z "$_dirty" ]; then
            probe "$(tr_ "репозиторий" "repository")" 1 "$(tr_ "чистый" "clean")"
        else
            probe "$(tr_ "репозиторий" "repository")" 0 "$(tr_ "есть несохранённые правки — автообновление остановится" "uncommitted changes — auto-update will stop")"
        fi
    fi
}

# --------------------------------------------------------------------------
# Меню
# --------------------------------------------------------------------------

menu_header() {
    _url=$(env_get "$APP_ENV" OPENCRM_BASE_URL 2>/dev/null || echo "—")
    if curl -fsS --max-time 2 http://127.0.0.1/healthz 2>/dev/null | grep -q '"ok"'; then
        _state="$(tr_ "${GREEN}работает${R}" "${GREEN}running${R}")"
    else
        _state="$(tr_ "${RED}не отвечает${R}" "${RED}not responding${R}")"
    fi
    printf '\n%s========================================================%s\n' "$B" "$R"
    printf '  %sOpenCRM%s  —  %s\n' "$B" "$R" "$_state"
    printf '  %s%s%s\n' "$D" "$_url" "$R"
    printf '%s========================================================%s\n' "$B" "$R"
}

# Номер выделен цветом, подпись — обычная: выбирают по номеру, и глаз должен
# находить колонку цифр сразу, не вычитывая строки целиком.
menu_item() { printf '  %s%2s)%s %s\n' "$CYAN" "$1" "$R" "$2"; }

menu() {
    while :; do
        menu_header
        say ""
        menu_item 1  "$(tr_ "Статус и здоровье" "Status and health")"
        menu_item 2  "$(tr_ "Запустить" "Start")"
        menu_item 3  "$(tr_ "Перезапустить" "Restart")"
        menu_item 4  "$(tr_ "Остановить" "Stop")"
        menu_item 5  "$(tr_ "Обновить сейчас" "Update now")"
        menu_item 6  "$(tr_ "Автообновление: включить / выключить" "Auto-update: on / off")"
        menu_item 7  "$(tr_ "Журнал обновлений" "Update journal")"
        menu_item 8  "$(tr_ "Логи (Ctrl+C — выйти)" "Logs (Ctrl+C to exit)")"
        menu_item 9  "$(tr_ "Резервная копия" "Backup")"
        menu_item 10 "$(tr_ "Восстановить из копии" "Restore from backup")"
        menu_item 11 "$(tr_ "Домен и HTTPS" "Domain and HTTPS")"
        menu_item 12 "$(tr_ "Фаервол" "Firewall")"
        menu_item 13 "$(tr_ "Сбросить пароль администратора" "Reset admin password")"
        menu_item 14 "$(tr_ "Диагностика" "Diagnostics")"
        say ""
        menu_item 0  "$(tr_ "Выход" "Exit")"
        say ""
        _choice=$(ask "$(tr_ "  Выбор" "  Choice")" "0")
        case "$_choice" in
            1)  cmd_status ;;
            2)  cmd_start ;;
            3)  cmd_restart ;;
            4)  confirm "$(tr_ "    Остановить сайт?" "    Stop the site?")" n && cmd_stop || info "$(tr_ "отменено" "cancelled")" ;;
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
            *)  warn "$(tr_ "нет такого пункта" "no such item")" ;;
        esac
        # Явный if, а не `[ ] && exit`: под `set -e` невыполнившийся тест в конце
        # списка сам по себе завершает скрипт с ненулевым кодом.
        if [ "$ASSUME_YES" = "1" ]; then exit 0; fi
        printf '\n%s' "$D"
        ask "$(tr_ "  Enter — вернуться в меню" "  Enter — back to the menu")" "" >/dev/null
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
    # До разбора: понадобятся, если придётся перезапуститься в группе docker
    SCRIPT_ARGS=$(quote_argv "$@")
    # Язык — до первого вывода, иначе половина сообщений уйдёт не на том языке
    load_language
    # Docker поставили в прошлый заход, а сессию с тех пор не перезапускали:
    # чиним это для любой команды, а не только для установки, — иначе `status`
    # и `update` до перезахода упираются в «демон не отвечает» на ровном месте.
    reenter_docker_group || true
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
        *) die "$(tr_ "неизвестная команда: $_command (см. ./opencrm.sh help)" "unknown command: $_command (see ./opencrm.sh help)")" ;;
    esac
}

main "$@"
