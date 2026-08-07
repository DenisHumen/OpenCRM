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
# CDPATH= перед cd — не опечатка: у пользователя в CDPATH может лежать
# каталог, из-за которого `cd` уйдёт не туда и напечатает свой путь в stdout,
# испортив подстановку. Пустое значение выключает это на одну команду.
# shellcheck disable=SC1007
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

# Раскраска чужого вывода.
#
# Сообщения самого скрипта различаются взглядом, а docker, git и python сыплют
# ровной простынёй, в которой ни ошибку, ни успех глазом не выхватить — и
# именно в ней тонули сегодняшние поломки. Подсвечиваем только значимые строки:
# красить всё подряд значит снова получить кашу, просто цветную.
#
# Разбор идёт по словам, а не по коду возврата: у docker compose одна команда
# печатает и «Started», и «Error» в одном потоке.
paint() {
    if [ -z "$RED" ]; then cat; return 0; fi
    # Без \b и прочих расширений GNU: в Ubuntu awk — это mawk, и `ok\b` там
    # молча не сработает. Границу слова выражаем явно через [^a-z].
    #
    # `fail` подстрокой покрывает и failed, и failure, и FAIL из отчёта деплоя —
    # перечисление форм по одной как раз и пропускало главное слово.
    #
    # Проверка «нездоров» идёт раньше «здоров»: строка docker про контейнер
    # содержит оба слова, и выиграть должно тревожное.
    awk -v red="$RED" -v green="$GREEN" -v yellow="$YELLOW" -v reset="$R" '
        {
            low = tolower($0)
            if (low ~ /error|fail|fatal|denied|refused|cannot|unable|traceback|exception|not found|no such|unhealthy|broken|✘|✗/)
                printf "%s%s%s\n", red, $0, reset
            else if (low ~ /warn|deprecat|skipped|pending/)
                printf "%s%s%s\n", yellow, $0, reset
            else if (low ~ /done|success|started|running|healthy|created|built|up-to-date|✔|✓/ || low ~ /(^|[^a-z])ok([^a-z]|$)/)
                printf "%s%s%s\n", green, $0, reset
            else
                print
            # Без сброса буфера awk копит вывод блоками, и `logs -f` шёл бы
            # рывками по 4 КБ вместо живой ленты.
            fflush()
        }
    '
}

# Запуск команды с раскраской и СОХРАНЁННЫМ кодом возврата.
#
# Просто `cmd | paint` использовать нельзя: в пайпе `$?` — это код последней
# команды, то есть раскраски, и она успешна всегда. Проверки вида
# `if compose up; then` начали бы считать успехом любой исход. POSIX sh не знает
# PIPESTATUS, поэтому код переносим через файл.
run_painted() {
    _rc_file=$(mktemp 2>/dev/null) || { "$@" 2>&1; return $?; }
    { "$@" 2>&1; printf '%s' "$?" > "$_rc_file"; } | paint
    _rc=$(cat "$_rc_file" 2>/dev/null || printf '1')
    rm -f "$_rc_file"
    return "${_rc:-1}"
}

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

OS_ID=""; OS_NAME=""; OS_CODENAME=""
detect_os() {
    [ -r /etc/os-release ] || die "$(tr_ "не вижу /etc/os-release — не знаю, что за система" "no /etc/os-release — cannot tell what system this is")"
    # shellcheck disable=SC1091
    . /etc/os-release
    OS_ID=${ID:-}
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

# --------------------------------------------------------------------------
# Запуск от root
# --------------------------------------------------------------------------
# Ломает не сразу, а потом, и потому особенно неприятно. Скрипт записывает в
# docker/.env владельца каталогов с данными:
#
#     OPENCRM_UID=$(id -u)   OPENCRM_GID=$(id -g)   OPENCRM_HOME=$HOME/opencrm
#
# Под `sudo` это 0, 0 и /root/opencrm. Контейнер начинает работать от root и в
# другом каталоге: прежние данные остаются в домашней папке хозяина машины и
# выглядят пропавшими, а всё новое рождается root-овским. Следующий запуск уже
# без sudo упирается в эти файлы — «attempt to write a readonly database».
#
# Сам по себе root не зло: на многих VPS другого пользователя просто нет, и
# установка целиком под root непротиворечива. Беда от смешивания. Поэтому:
# честный root-логин пропускаем, `sudo` от обычного пользователя — нет.
#
# OPENCRM_ALLOW_ROOT=1 снимает запрет, если вы понимаете, что делаете.
guard_root() {
    # Явные if, а не цепочки `[ ] && return`: под `set -e` такие цепочки уже
    # однажды роняли этот скрипт на ровном месте.
    if [ "$(id -u)" -ne 0 ]; then return 0; fi
    if [ "${OPENCRM_ALLOW_ROOT:-0}" = "1" ]; then return 0; fi
    if [ -z "${SUDO_USER:-}" ]; then return 0; fi
    if [ "${SUDO_USER:-}" = "root" ]; then return 0; fi

    printf '\n%s[!] %s%s\n' "$RED" "$(tr_ \
        "Не запускайте скрипт через sudo." \
        "Do not run this script with sudo.")" "$R"
    say ""
    say "$(tr_ \
        "    Скрипт сам просит пароль там, где он нужен (Docker, ufw, сертификат)." \
        "    The script asks for a password itself where it is needed (Docker, ufw, certificates).")"
    say "$(tr_ \
        "    Под sudo он записал бы владельцем данных root (UID 0) и перенёс бы" \
        "    Under sudo it would record root (UID 0) as the owner of your data and move")"
    say "$(tr_ \
        "    состояние в /root/opencrm. Прежние данные остались бы в вашей домашней" \
        "    the state to /root/opencrm. Your existing data would stay in your home")"
    say "$(tr_ \
        "    папке и выглядели бы пропавшими, а сайт при следующем запуске без sudo" \
        "    directory and look lost, and the next run without sudo would fail with")"
    say "$(tr_ \
        "    упал бы с «readonly database»." \
        "    \"readonly database\".")"
    say ""
    say "$(tr_ "    Запустите так:" "    Run it like this:")"
    say "        ${B}./opencrm.sh${R}"
    say ""
    say "$(tr_ \
        "    Уже запускали под sudo и сайт сломался — это чинится:" \
        "    Already ran it under sudo and the site broke — this is repairable:")"
    say "        ${B}./opencrm.sh repair${R}"
    say ""
    exit 1
}

# Владелец установки: от чьего имени должен работать контейнер.
#
# Под sudo это тот, кто вызвал sudo, а не root. При честном root-логине берём
# владельца каталога с репозиторием: установка делалась им, ему и владеть.
install_owner() {
    if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != "root" ]; then
        printf '%s' "$SUDO_USER"
    elif [ "$(id -u)" -ne 0 ]; then
        id -un
    else
        stat -c '%U' "$REPO_DIR" 2>/dev/null || printf 'root'
    fi
}

# Расхождение между тем, кто владеет данными, и тем, от кого работает контейнер.
# Молчать о нём нельзя: сайт при этом выглядит живым ровно до первой записи.
warn_owner_mismatch() {
    installed || return 0
    _env_uid=$(env_get "$DOCKER_ENV" OPENCRM_UID 2>/dev/null || true)
    [ -n "$_env_uid" ] || return 0
    [ "$_env_uid" = "$(id -u)" ] && return 0
    warn "$(tr_ \
        "docker/.env говорит UID $_env_uid, а вы $(id -u) — будут ошибки доступа" \
        "docker/.env says UID $_env_uid, you are $(id -u) — expect permission errors")"
    info "$(tr_ "починить: ./opencrm.sh repair" "fix it: ./opencrm.sh repair")"
}

home_dir() {
    _home=$(env_get "$DOCKER_ENV" OPENCRM_HOME 2>/dev/null || true)
    [ -n "$_home" ] || _home="$HOME/opencrm"
    printf '%s' "$_home"
}

# На чём работает установка: mysql или sqlite.
#
# Единственный признак — сам URL в config/.env. Отдельного флага «мы на MySQL»
# нарочно нет: два источника правды о том, где лежат данные, рано или поздно
# разъезжаются, и разъехавшись отправляют копию не в ту базу.
db_engine() {
    case "$(env_get "$APP_ENV" OPENCRM_DB_URL 2>/dev/null || true)" in
        mysql*) printf 'mysql' ;;
        *)      printf 'sqlite' ;;
    esac
}

# --------------------------------------------------------------------------
# Профили compose
# --------------------------------------------------------------------------
#
# COMPOSE_PROFILES — общий список, в который пишут независимые решения: база
# (`mysql`) и мониторинг (`monitoring`, `monitoring-logs`). Поэтому значение
# правится по одному имени, а не перезаписывается целиком: строка
# `env_set COMPOSE_PROFILES monitoring` молча вынесла бы из стека службу базы —
# и сайт после ближайшего `up` поднялся бы на пустом файле рядом.

compose_profile_enabled() {
    case ",$(env_get "$DOCKER_ENV" COMPOSE_PROFILES 2>/dev/null || true)," in
        *",$1,"*) return 0 ;;
        *)        return 1 ;;
    esac
}

# compose_profile <имя> on|off
compose_profile() {
    _pkey=$1; _pwant=$2
    _plist=""
    for _pitem in $(env_get "$DOCKER_ENV" COMPOSE_PROFILES 2>/dev/null | tr ',' ' ' || true); do
        # Явный if, а не `[ ] && continue`: под `set -e` невыполнившийся тест в
        # конце списка сам по себе завершает скрипт (тот же разбор — у меню).
        if [ "$_pitem" != "$_pkey" ]; then
            _plist="${_plist:+$_plist,}$_pitem"
        fi
    done
    if [ "$_pwant" = "on" ]; then
        _plist="${_plist:+$_plist,}$_pkey"
    fi
    env_set "$DOCKER_ENV" COMPOSE_PROFILES "$_plist"
}

# Адрес, по которому мониторинг проверяет сайт СНАРУЖИ.
#
# Значение то же, что у OPENCRM_BASE_URL, но лежать обязано в docker/.env:
# compose подставляет переменные в описание служб только оттуда, а config/.env
# он лишь передаёт внутрь контейнера приложения. Записанный в config/.env адрес
# Prometheus не увидел бы вовсе.
#
# Зовётся из всех мест, где адрес может поменяться (запуск, перезапуск, выпуск
# сертификата), а не один раз при установке: после перехода на HTTPS проверка
# обязана пойти на https://, иначе она вечно ходит по редиректу.
sync_monitor_url() {
    _murl=$(env_get "$APP_ENV" OPENCRM_BASE_URL 2>/dev/null || true)
    [ -n "$_murl" ] || return 0
    [ -f "$DOCKER_ENV" ] || return 0
    env_set "$DOCKER_ENV" OPENCRM_MONITOR_URL "$_murl"
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

# Установка на MySQL с уже населённой SQLite: переносить ли данные. Ответ берут
# при выборе базы, а сам перенос идёт много позже — после того, как стек
# поднялся и миграции построили схему в новой базе.
MIGRATE_FROM_SQLITE=0

# Выбор базы.
#
# По умолчанию SQLite: она не требует ничего, лежит одним файлом и на нагрузке
# одной студии не уступает никому. MySQL нужна там, где рабочих процессов
# больше одного — SQLite допускает ровно одного писателя, и вторая копия
# приложения не поднимется вовсе (см. docker/entrypoint.sh).
#
# Спрашивается один раз. Повторный запуск установки на уже настроенной MySQL не
# переспрашивает и ничего не перегенерирует: ответ «SQLite» увёл бы работающий
# сайт на пустой файл, а новый пароль разошёлся бы с пользователем, который в
# базе уже создан, — и приложение перестало бы к ней подключаться.
choose_database() {
    step "$(tr_ "База данных" "Database")"

    if [ "$(db_engine)" = "mysql" ]; then
        ok "$(tr_ "уже настроена MySQL — не трогаю" "already running on MySQL — leaving it alone")"
        return 0
    fi

    say "$(tr_ \
        "    ${D}SQLite — один файл рядом с данными, ставить и настраивать нечего.${R}" \
        "    ${D}SQLite — a single file next to your data, nothing to install or tune.${R}")"
    say "$(tr_ \
        "    ${D}MySQL — отдельный контейнер рядом: нужна, когда рабочих процессов${R}" \
        "    ${D}MySQL — a container alongside: needed once you run more than one${R}")"
    say "$(tr_ \
        "    ${D}больше одного или базу обслуживают отдельно.${R}" \
        "    ${D}worker process, or the database is administered separately.${R}")"
    say ""
    menu_item 1 "$(tr_ "SQLite (по умолчанию)" "SQLite (default)")"
    menu_item 2 "MySQL"
    _pick=$(ask "$(tr_ "    Выбор" "    Choice")" "1")

    case "$_pick" in
        2|mysql|MySQL|MYSQL|m) ;;
        *)
            # Абсолютный путь, а не относительный из шаблона: приложение,
            # alembic и entrypoint.sh запускаются из разных рабочих каталогов,
            # и относительный URL однажды укажет на пустой файл рядом.
            env_set "$APP_ENV" OPENCRM_DB_URL "sqlite:////app/data/opencrm.db"
            # Снятый профиль — это «службы базы в стеке нет». Снимаем явно, а не
            # оставляем строку отсутствовать: установку запускают и поверх
            # прежней, и там COMPOSE_PROFILES=mysql могло остаться с прошлого
            # раза — тогда рядом молча поднялся бы никому не нужный сервер.
            #
            # Снимается ровно `mysql`, а не весь список: рядом в нём живёт
            # решение про мониторинг, и затирать его выбором базы нельзя.
            compose_profile mysql off
            ok "$(tr_ "SQLite: $(home_dir)/data/opencrm.db" "SQLite: $(home_dir)/data/opencrm.db")"
            return 0
            ;;
    esac

    # Пароль генерируется, а не спрашивается, и это не мелочь: спрошенный пароль
    # базы человек придумывает за минуту и повторяет от сервера к серверу. Руками
    # его всё равно никто не вводит — он нужен ровно двум контейнерам, и оба
    # берут его из файла.
    #
    # Алфавит gen_secret (A-Za-z0-9) здесь ещё и обязателен: пароль уезжает
    # внутрь URL, а `@`, `:` и `/` разобрали бы его на части.
    _db_pass=$(gen_secret 32)
    _db_root=$(gen_secret 32)

    env_set "$DOCKER_ENV" OPENCRM_DB_NAME opencrm
    env_set "$DOCKER_ENV" OPENCRM_DB_USER opencrm
    env_set "$DOCKER_ENV" OPENCRM_DB_PASSWORD "$_db_pass"
    env_set "$DOCKER_ENV" OPENCRM_DB_ROOT_PASSWORD "$_db_root"
    compose_profile mysql on
    # charset=utf8mb4 в URL — вторая половина той же защиты, что и настройка
    # сервера: без неё соединение договаривается о трёхбайтном utf8, и эмодзи
    # в заметке клиента обрывают вставку на полуслове.
    env_set "$APP_ENV" OPENCRM_DB_URL \
        "mysql+pymysql://opencrm:$_db_pass@db:3306/opencrm?charset=utf8mb4"
    ok "$(tr_ "MySQL: контейнер db, пароль сгенерирован и записан" "MySQL: container db, password generated and stored")"
    info "$(tr_ "данные базы: $(home_dir)/mysql" "database files: $(home_dir)/mysql")"

    # Уже есть SQLite с данными — молчать об этом нельзя: сайт поднимется на
    # пустой MySQL и будет выглядеть так, будто всё пропало.
    if [ -f "$(home_dir)/data/opencrm.db" ]; then
        say ""
        warn "$(tr_ "рядом лежит база SQLite с данными" "there is an SQLite database with data next to it")"
        say "$(tr_ \
            "        Перенести её в MySQL можно тем же скриптом, что и всегда" \
            "        It can be moved into MySQL by the usual script")"
        say "        ${D}scripts/migrate_to_mysql.py${R}"
        say "$(tr_ \
            "        Файл SQLite при переносе открывается только на чтение и не меняется." \
            "        The SQLite file is opened read-only during the move and is not changed.")"
        say "$(tr_ \
            "        Пока он цел, возврат стоит одной строки: OPENCRM_DB_URL в config/.env" \
            "        While it is intact, going back costs one line: OPENCRM_DB_URL in config/.env")"
        say "$(tr_ \
            "        обратно на sqlite:////app/data/opencrm.db и ./opencrm.sh restart" \
            "        back to sqlite:////app/data/opencrm.db and ./opencrm.sh restart")"
        if confirm "$(tr_ "    Перенести данные после запуска?" "    Move the data once the stack is up?")" y; then
            MIGRATE_FROM_SQLITE=1
        else
            info "$(tr_ "перенос пропущен — сайт поднимется на пустой базе" "move skipped — the site will come up on an empty database")"
        fi
    fi
}

# Перенос данных из SQLite в MySQL — уже после того, как стек поднялся.
#
# Раньше нельзя: схему в новой базе строят миграции из entrypoint.sh, а до
# первого старта её там нет вовсе. Скрипт переноса на это и рассчитан — он
# сверяет ревизии обеих баз и сам чистит цель от того, что насеяли миграции.
migrate_sqlite_to_mysql() {
    [ "$MIGRATE_FROM_SQLITE" = "1" ] || return 0
    step "$(tr_ "Перенос данных SQLite → MySQL" "Moving the data SQLite → MySQL")"

    # URL раскрывается ВНУТРИ контейнера, а не подставляется здесь: иначе пароль
    # базы попал бы в командную строку docker и стал виден в `ps` любому
    # пользователю сервера.
    if run_painted compose exec -T app sh -c 'exec python scripts/migrate_to_mysql.py --source "sqlite:////app/data/opencrm.db" --target "$OPENCRM_DB_URL"'; then
        run_painted compose restart app
        if wait_health 90; then
            ok "$(tr_ "данные перенесены, сайт отвечает" "data moved, the site is answering")"
        else
            warn "$(tr_ "данные перенесены, но сайт не ответил — ./opencrm.sh logs app" "data moved, but the site did not answer — ./opencrm.sh logs app")"
        fi
        say "$(tr_ \
            "        Файл SQLite не тронут. Что-то не так — верните OPENCRM_DB_URL" \
            "        The SQLite file is untouched. If anything is off, put OPENCRM_DB_URL")"
        say "$(tr_ \
            "        в config/.env на sqlite:////app/data/opencrm.db и ./opencrm.sh restart" \
            "        in config/.env back to sqlite:////app/data/opencrm.db and ./opencrm.sh restart")"
    else
        warn "$(tr_ "перенос не удался — сайт остаётся на пустой MySQL" "the move failed — the site stays on an empty MySQL")"
        say "$(tr_ \
            "        Исходная база не тронута: верните OPENCRM_DB_URL в config/.env" \
            "        The source database is untouched: put OPENCRM_DB_URL in config/.env")"
        say "$(tr_ \
            "        на sqlite:////app/data/opencrm.db и ./opencrm.sh restart — всё вернётся." \
            "        back to sqlite:////app/data/opencrm.db and ./opencrm.sh restart — everything returns.")"
    fi
}

# --------------------------------------------------------------------------
# Мониторинг
# --------------------------------------------------------------------------
#
# Заводится как всё остальное необязательное: службы стоят под профилями
# compose, и выключенный мониторинг — это не остановленные контейнеры, а
# контейнеры, которых в стеке нет.
#
# Спрашивается один раз, и ответ по умолчанию зависит от машины. На VPS с двумя
# гигабайтами полный набор наблюдателей съедает заметную часть памяти, а сайт с
# неё же и живёт; на машине попросторнее отказываться незачем. Поэтому умолчание
# считается от того, сколько памяти есть на самом деле, и решение проговаривается
# вслух — угадывать за человека можно, молчать об этом нельзя.

#: Сколько памяти (вместе с подкачкой) считаем достаточным для полного набора:
#: метрики плюс логи. Ниже этого порога предлагаем только метрики.
MONITORING_LOGS_MIN_MB=3000
#: Ниже этого мониторинг по умолчанию не предлагаем вовсе.
MONITORING_MIN_MB=1800

MONITORING_PASSWORD_SHOWN=""

# Пароль Grafana. Генерируется, а не спрашивается, и в репозиторий не попадает —
# ровно как пароль MySQL: лежит в docker/.env с правами 600.
#
# Существующий не трогаем никогда. Перегенерация означала бы, что человек,
# записавший пароль себе, в следующий заход установщика теряет доступ к панели.
seed_grafana_password() {
    _gp=$(env_get "$DOCKER_ENV" OPENCRM_GRAFANA_PASSWORD 2>/dev/null || true)
    if [ -n "$_gp" ]; then
        return 0
    fi
    MONITORING_PASSWORD_SHOWN=$(gen_secret 24)
    env_set "$DOCKER_ENV" OPENCRM_GRAFANA_PASSWORD "$MONITORING_PASSWORD_SHOWN"
}

# Канал оповещений. Бот берётся тот же, что у автообновления: он уже настроен, и
# этот чат уже читают. Заводить второй значит завести второй, который читать
# перестанут.
#
# Значения переносятся в docker/.env, а не читаются из autoupdate.env напрямую.
# Причина не в удобстве: в autoupdate.env рядом лежит токен GitHub, и подключать
# весь файл к контейнеру Alertmanager значило бы отдать ему заодно и его.
sync_alert_channel() {
    _auto="$(home_dir)/autoupdate.env"
    _tok=$(env_get "$DOCKER_ENV" OPENCRM_MONITORING_TELEGRAM_TOKEN 2>/dev/null || true)
    _cha=$(env_get "$DOCKER_ENV" OPENCRM_MONITORING_TELEGRAM_CHAT 2>/dev/null || true)
    if [ -z "$_tok" ] && [ -f "$_auto" ]; then
        _tok=$(env_get "$_auto" OPENCRM_UPDATE_TELEGRAM_TOKEN 2>/dev/null || true)
        _cha=$(env_get "$_auto" OPENCRM_UPDATE_TELEGRAM_CHAT 2>/dev/null || true)
    fi
    if [ -z "$_tok" ] || [ -z "$_cha" ]; then
        return 1
    fi
    # Alertmanager принимает chat_id только числом. Имя канала (@name) он
    # отвергнет при разборе конфига и не поднимется вовсе — а узнать об этом
    # хотелось бы сейчас, а не в день первой аварии.
    case "$_cha" in
        ''|*[!0-9-]*)
            warn "$(tr_ "chat_id «$_cha» не число — Alertmanager такой не примет" "chat_id \"$_cha\" is not a number — Alertmanager will not take it")"
            say "$(tr_ "        Нужен числовой id чата (у групп он отрицательный), а не @имя." "        A numeric chat id is required (negative for groups), not @name.")"
            return 1
            ;;
    esac
    env_set "$DOCKER_ENV" OPENCRM_MONITORING_TELEGRAM_TOKEN "$_tok"
    env_set "$DOCKER_ENV" OPENCRM_MONITORING_TELEGRAM_CHAT "$_cha"
    return 0
}

configure_monitoring() {
    step "$(tr_ "Мониторинг" "Monitoring")"

    if compose_profile_enabled monitoring; then
        seed_grafana_password
        sync_alert_channel || true
        ok "$(tr_ "уже включён — не трогаю" "already on — leaving it alone")"
        return 0
    fi

    _mem=$(mem_mb); _swap=$(swap_mb); _total=$((_mem + _swap))
    say "$(tr_ \
        "    ${D}Оповещения в Telegram: сайт не отвечает, кончается место, истекает${R}" \
        "    ${D}Telegram alerts: the site is down, disk is filling up, the certificate${R}")"
    say "$(tr_ \
        "    ${D}сертификат, откатился деплой, не снялась копия. Плюс графики и поиск${R}" \
        "    ${D}is expiring, a deploy rolled back, a backup was missed. Plus dashboards${R}")"
    say "$(tr_ \
        "    ${D}по логам в Grafana на /monitoring/ этого же сайта.${R}" \
        "    ${D}and log search in Grafana at /monitoring/ on this same site.${R}")"
    say ""
    info "$(tr_ "памяти с подкачкой: ${_total} МБ; метрики занимают ~250 МБ, логи ещё ~200 МБ" "memory with swap: ${_total} MB; metrics take ~250 MB, logs another ~200 MB")"

    if [ "$_total" -ge "$MONITORING_MIN_MB" ]; then
        _default=y
    else
        _default=n
        warn "$(tr_ "на этой машине памяти мало — по умолчанию предлагаю обойтись без него" "this machine is short on memory — the default is to skip it")"
    fi

    if ! confirm "$(tr_ "    Включить мониторинг?" "    Enable monitoring?")" "$_default"; then
        compose_profile monitoring off
        compose_profile monitoring-logs off
        info "$(tr_ "пропускаю; включить позже — ./opencrm.sh monitoring" "skipping; enable later — ./opencrm.sh monitoring")"
        return 0
    fi

    compose_profile monitoring on
    if [ "$_total" -ge "$MONITORING_LOGS_MIN_MB" ]; then
        compose_profile monitoring-logs on
        ok "$(tr_ "включены метрики и логи" "metrics and logs are on")"
    else
        compose_profile monitoring-logs off
        ok "$(tr_ "включены метрики" "metrics are on")"
        info "$(tr_ "логи (Loki) не включаю: памяти мало. Без них не будет поиска по логам и правила про долю 5xx" "logs (Loki) left off: not enough memory. Without them there is no log search and no 5xx-share alert")"
        say "$(tr_ "        Передумаете — ./opencrm.sh monitoring logs" "        Changed your mind — ./opencrm.sh monitoring logs")"
    fi

    seed_grafana_password
    sync_monitor_url

    if sync_alert_channel; then
        ok "$(tr_ "тревоги пойдут в тот же Telegram, что и сообщения об обновлениях" "alerts will go to the same Telegram as the update messages")"
        return 0
    fi

    say ""
    say "$(tr_ \
        "    ${D}Оповещения важнее графиков: график смотрят, когда уже заподозрили,${R}" \
        "    ${D}Alerts matter more than dashboards: a dashboard is opened once you${R}")"
    say "$(tr_ \
        "    ${D}а сообщение приходит само.${R}" \
        "    ${D}already suspect something; a message arrives on its own.${R}")"
    _tok=$(ask "$(tr_ "    Telegram-токен бота (Enter — без оповещений)" "    Telegram bot token (Enter — no alerts)")" "")
    if [ -z "$_tok" ]; then
        warn "$(tr_ "канал не настроен — тревоги будут копиться в Grafana, но никуда не уйдут" "no channel — alerts will pile up in Grafana but go nowhere")"
        return 0
    fi
    _cha=$(ask "$(tr_ "    chat_id (число; у групп отрицательное)" "    chat_id (a number; negative for groups)")" "")
    env_set "$DOCKER_ENV" OPENCRM_MONITORING_TELEGRAM_TOKEN "$_tok"
    env_set "$DOCKER_ENV" OPENCRM_MONITORING_TELEGRAM_CHAT "$_cha"
    if sync_alert_channel; then
        ok "$(tr_ "оповещения настроены" "alerts configured")"
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
    # Права сужаем сразу, а не полагаемся на первую правку: с выбором MySQL сюда
    # ложатся пароли базы, а `cp` переносит права шаблона (644) — файл с паролем
    # оказался бы читаем всем в системе ровно до первого env_set.
    chmod 600 "$DOCKER_ENV"
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
    # mysql создаётся всегда, даже на SQLite: пустой каталог не стоит ничего, а
    # созданный докером на лету принадлежал бы root — и служба базы, включённая
    # позже, упёрлась бы в права на своём же каталоге данных.
    # Каталоги мониторинга создаются всегда, даже когда он выключен, — по той же
    # причине, что и mysql: созданный докером на лету принадлежал бы root, и
    # включённый позже Prometheus упёрся бы в права на своём же хранилище.
    for _sub in data storage letsencrypt acme updates mysql \
                monitoring/prometheus monitoring/grafana monitoring/alertmanager monitoring/loki; do
        mkdir -p "$_home/$_sub"
    done
    ok "$_home/{data,storage,letsencrypt,acme,updates,mysql,monitoring}"
}

compose() {
    $DOCKER_PREFIX docker compose -f "$COMPOSE_FILE" "$@"
}

# Настройки читаются один раз при старте процесса, а `compose up -d` при
# изменении одного лишь env_file контейнер НЕ пересоздаёт — приложение продолжает
# жить со старыми значениями. Молча и потому опасно: после выпуска сертификата
# сайт уже за TLS, а cookie так и остались бы без флага Secure.
apply_env_change() {
    # Адрес сайта мог только что смениться (выпуск сертификата переводит его на
    # https) — проверка снаружи обязана пойти туда же, иначе она вечно ходит по
    # редиректу.
    sync_monitor_url
    run_painted compose up -d --force-recreate app
    # nginx проксирует в app и до его готовности отдаёт 502 — ждём здесь, иначе
    # каждый вызывающий получал бы «сайт лежит» сразу после успешной настройки.
    if wait_health 90; then
        ok "$(tr_ "приложение перезапущено с новыми настройками" "application restarted with the new settings")"
    else
        warn "$(tr_ "приложение не ответило за 3 минуты — смотрите ./opencrm.sh logs app" "no answer from the application in 3 minutes — see ./opencrm.sh logs app")"
    fi
}

# Попросить nginx перечитать конфиг.
#
# Файлы nginx примонтированы из чекаута, а не лежат в образе: `git pull` меняет
# их на диске мгновенно. Но `compose up -d --build` пересоздаёт только те службы,
# у которых изменилось описание или образ, — у nginx не меняется ни то, ни
# другое, и он продолжает работать с конфигом, прочитанным при своём запуске.
# Сам он за файлами не следит.
#
# Проверено репетицией обновления: compose тронул только `app`, а nginx ещё
# долго раздавал старые правила. Само оно чинится циклом перезагрузки раз в
# шесть часов (продление сертификата), но полагаться на «когда-нибудь за шесть
# часов» при обновлении нельзя.
#
# Перезагрузка мягкая: начатые запросы дорабатываются, порты не переоткрываются,
# простоя нет. Молчаливая: nginx может быть не поднят вовсе (у кого-то свой
# снаружи), и ронять из-за этого удавшееся обновление незачем.
reload_nginx() {
    compose exec -T nginx nginx -s reload >/dev/null 2>&1 || true
}

build_and_start() {
    step "$(tr_ "Сборка и запуск" "Build and start")"
    sync_monitor_url
    # Фаервол мог стоять на сервере и до нас. Тогда контейнеру сборки нечем
    # резолвить имена, и установка обрывается на `pip install` невнятной ошибкой,
    # в которой про фаервол ни слова. Разрешение узкое, лишним не будет.
    ensure_docker_dns
    info "$(tr_ "первая сборка занимает 3-10 минут: собирается фронтенд и ставится ffmpeg" "the first build takes 3-10 minutes: frontend compile plus ffmpeg install")"
    run_painted compose up -d --build
    reload_nginx
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
        run_painted compose logs --tail 40 app || true
        die "$(tr_ "не удалось дождаться /healthz — логи выше" "gave up waiting for /healthz — logs above")"
    fi
}

issue_certificate() {
    _domain=$(env_get "$DOCKER_ENV" OPENCRM_DOMAIN 2>/dev/null || true)
    [ -n "$_domain" ] || return 0

    step "$(tr_ "HTTPS для $_domain" "HTTPS for $_domain")"
    _home=$(home_dir)
    # Проверяем сам файл, а не каталог, и ровно тот же, что смотрит nginx
    # (docker/nginx/entrypoint.sh). Каталог live/<домен>/ остаётся после
    # оборванного выпуска и от переезда со старого сервера, а сертификата в нём
    # нет. По каталогу выходил тупик: скрипт отвечал «уже выпущен» и не делал
    # ничего, nginx не находил файл и не поднимал 443, а человек оставался без
    # HTTPS и без объяснения.
    if $SUDO test -f "$_home/letsencrypt/live/$_domain/fullchain.pem"; then
        ok "$(tr_ "сертификат уже выпущен" "certificate already issued")"
        return 0
    fi
    if $SUDO test -d "$_home/letsencrypt/live/$_domain"; then
        warn "$(tr_ \
            "каталог сертификата есть, а самого сертификата нет — выпускаю заново" \
            "the certificate directory exists but the certificate does not — issuing again")"
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
    # --entrypoint обязателен. Сервис certbot объявляет своим entrypoint цикл
    # продления (`sh -c 'while :; do certbot renew; sleep 12h; done'`), а
    # `compose run` подменяет команду, а не entrypoint. Без этого флага аргументы
    # `certonly ...` уезжают в позиционные параметры `sh -c` и не выполняются
    # вовсе: контейнер запускает бесконечное продление, `run` ждёт его вечно, и
    # выпуск висит на «Created» до Ctrl+C. Сертификат при этом не выпускается
    # никогда, а nginx без файла сертификата не поднимает 443 — сайт остаётся
    # доступен только по HTTP, и снаружи это выглядит как отказ соединения.
    if run_painted compose run --rm --entrypoint certbot certbot certonly --webroot -w /var/www/certbot \
        -d "$_domain" --email "$_email" --agree-tos --no-eff-email --non-interactive; then
        # Теперь сайт правда за TLS — можно и нужно переводить BASE_URL на https,
        # чтобы cookie получили флаг Secure. Настройки читаются при старте
        # процесса, поэтому контейнер приложения пересоздаётся, а не просто ждёт.
        env_set "$APP_ENV" OPENCRM_BASE_URL "https://$_domain"
        apply_env_change
        run_painted compose restart nginx
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
    # Где лежат данные — то, что спрашивают первым, когда приходит время
    # копий, переезда или разбора аварии. Сказать один раз в конце установки
    # дешевле, чем потом выяснять по конфигам.
    if [ "$(db_engine)" = "mysql" ]; then
        say "$(tr_ "  База:   MySQL в контейнере db, данные в $(home_dir)/mysql" "  Database: MySQL in container db, files in $(home_dir)/mysql")"
    else
        say "$(tr_ "  База:   SQLite, $(home_dir)/data/opencrm.db" "  Database: SQLite, $(home_dir)/data/opencrm.db")"
    fi
    if compose_profile_enabled monitoring; then
        say "$(tr_ "  Мониторинг: ${_url}/monitoring/  (логин admin)" "  Monitoring: ${_url}/monitoring/  (login admin)")"
        if [ -n "$MONITORING_PASSWORD_SHOWN" ]; then
            printf '  %s%s%s\n' "$B" "$MONITORING_PASSWORD_SHOWN" "$R"
        fi
    fi
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
    # Строго после configure_app_env: config/.env к этому моменту уже создан, и
    # выбор базы дописывается в него, а не создаёт файл мимо шаблона.
    choose_database
    configure_domain
    create_dirs
    build_and_start
    check_health
    # После check_health: перенос идёт в живую базу, схему в которой построили
    # миграции при первом старте приложения.
    migrate_sqlite_to_mysql
    issue_certificate
    setup_firewall
    setup_backups
    # Строго после setup_autoupdate: канал оповещений берётся из настроенного
    # там же бота, и спрашивать токен второй раз незачем.
    setup_autoupdate
    configure_monitoring
    if compose_profile_enabled monitoring; then
        step "$(tr_ "Запуск мониторинга" "Starting monitoring")"
        monitoring_apply
    fi
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
        # Директива обязана стоять вплотную к `.`, поэтому он на отдельной
        # строке: в связке `set -a; . файл; set +a` она относилась бы к `set`.
        set -a
        # shellcheck disable=SC1090  # путь известен только в рантайме
        . "$_env_file"
        set +a
    fi
    OPENCRM_UPDATE_PROJECT_DIR="$REPO_DIR" python3 "$REPO_DIR/scripts/autoupdate.py" "$@"
}

cmd_status() {
    need_install
    step "$(tr_ "Контейнеры" "Containers")"
    run_painted compose ps
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

cmd_start()   { need_install; step "$(tr_ "Запуск" "Start")"; sync_monitor_url; run_painted compose up -d; if wait_health 60; then ok "$(tr_ "сайт отвечает" "the site is answering")"; else warn "$(tr_ "сайт ещё поднимается" "the site is still coming up")"; fi; }
cmd_stop()    { need_install; step "$(tr_ "Остановка" "Stop")"; run_painted compose down; ok "$(tr_ "остановлено" "stopped")"; }
cmd_restart() { need_install; step "$(tr_ "Перезапуск" "Restart")"; sync_monitor_url; run_painted compose restart; if wait_health 60; then ok "$(tr_ "сайт отвечает" "the site is answering")"; else warn "$(tr_ "сайт ещё поднимается" "the site is still coming up")"; fi; }

cmd_logs() {
    need_install
    _service=${1:-}
    if [ -n "$_service" ]; then
        run_painted compose logs -f --tail 100 "$_service"
    else
        run_painted compose logs -f --tail 100
    fi
}

cmd_update() {
    need_install
    step "$(tr_ "Обновление до последней версии" "Updating to the latest version")"
    run_painted autoupdate force-update
}

cmd_autoupdate() {
    need_install
    case "${1:-}" in
        on)  autoupdate enable ;;
        off) autoupdate disable ;;
        *)
            if autoupdate status | grep -q "$(tr_ "автообновление: включено" "auto-update: enabled")"; then
                if confirm "$(tr_ "    Сейчас включено. Выключить?" "    Currently enabled. Disable it?")" n; then
                    autoupdate disable
                else
                    info "$(tr_ "оставляю как есть" "leaving it as is")"
                fi
            else
                if confirm "$(tr_ "    Сейчас выключено. Включить?" "    Currently disabled. Enable it?")" y; then
                    autoupdate enable
                else
                    info "$(tr_ "оставляю как есть" "leaving it as is")"
                fi
            fi
            ;;
    esac
}

cmd_history() { need_install; autoupdate history -n "${1:-15}"; }

# Службы мониторинга поимённо. Список нужен для выключения: снятый профиль
# убирает службу из ОПИСАНИЯ стека, но уже поднятый контейнер от этого сам не
# исчезает — а compose не считает его «лишним» (orphan), потому что службу он
# знает, просто не выбрал. Проверено на стенде: после снятия профиля и
# `up -d --remove-orphans` все контейнеры мониторинга продолжали работать и есть
# память, а человек считал, что выключил их.
#
# Названная поимённо служба поднимает свой профиль сама, поэтому `rm` до них
# дотягивается и при снятом профиле.
MONITORING_SERVICES="prometheus alertmanager node-exporter containers blackbox grafana loki promtail"

monitoring_apply() {
    run_painted compose up -d --remove-orphans
}

monitoring_remove() {
    # shellcheck disable=SC2086  # список имён служб должен разбиться на слова
    run_painted compose rm -s -f $MONITORING_SERVICES || true
}

monitoring_state() {
    if compose_profile_enabled monitoring; then
        _mstate="$(tr_ "включён" "on")"
        if compose_profile_enabled monitoring-logs; then
            _mstate="$_mstate + $(tr_ "логи" "logs")"
        else
            _mstate="$_mstate, $(tr_ "без логов" "no logs")"
        fi
    else
        _mstate="$(tr_ "выключен" "off")"
    fi
    printf '%s' "$_mstate"
}

cmd_monitoring() {
    need_install
    step "$(tr_ "Мониторинг" "Monitoring")"

    case "${1:-}" in
        on)
            compose_profile monitoring on
            # Логи — по тому же правилу, что при установке: на тесной машине их
            # не поднимаем. Иначе выключение и включение обратно тихо меняло бы
            # состав: человек выключил полный набор, включил — а поиска по
            # логам больше нет, и связать это не с чем.
            _total=$(( $(mem_mb) + $(swap_mb) ))
            if [ "$_total" -ge "$MONITORING_LOGS_MIN_MB" ]; then
                compose_profile monitoring-logs on
            else
                info "$(tr_ "памяти ${_total} МБ — логи не поднимаю (./opencrm.sh monitoring logs)" "memory is ${_total} MB — leaving logs off (./opencrm.sh monitoring logs)")"
            fi
            seed_grafana_password
            sync_monitor_url
            sync_alert_channel || warn "$(tr_ "канал Telegram не настроен — тревоги никуда не пойдут" "no Telegram channel — alerts will go nowhere")"
            monitoring_apply
            ok "$(tr_ "включён" "on")"
            return 0
            ;;
        off)
            monitoring_remove
            compose_profile monitoring off
            compose_profile monitoring-logs off
            monitoring_apply
            ok "$(tr_ "выключен, контейнеры сняты" "off, containers removed")"
            return 0
            ;;
        reload)
            # Правила и конфиги примонтированы из чекаута, и службы перечитывают
            # их сами раз в пять минут (см. entrypoint.sh каждой). Эта команда —
            # для тех случаев, когда ждать не хочется.
            run_painted compose restart prometheus alertmanager
            ok "$(tr_ "конфиги и правила перечитаны" "configs and rules re-read")"
            return 0
            ;;
        logs)
            if compose_profile_enabled monitoring-logs; then
                # shellcheck disable=SC2086
                run_painted compose rm -s -f loki promtail || true
                compose_profile monitoring-logs off
                ok "$(tr_ "логи выключены: минус ~200 МБ памяти, минус поиск по логам и правило про долю 5xx" "logs off: ~200 MB less memory, no log search and no 5xx-share alert")"
            else
                compose_profile monitoring on
                compose_profile monitoring-logs on
                ok "$(tr_ "логи включены" "logs on")"
            fi
            monitoring_apply
            return 0
            ;;
        password)
            _np=$(gen_secret 24)
            env_set "$DOCKER_ENV" OPENCRM_GRAFANA_PASSWORD "$_np"
            printf '    %s%s%s\n' "$B" "$_np" "$R"
            # Пароль читается при СОЗДАНИИ контейнера, поэтому не `restart`:
            # перезапущенная Grafana осталась бы со старым.
            run_painted compose up -d --force-recreate grafana
            ok "$(tr_ "пароль сменён" "password changed")"
            return 0
            ;;
    esac

    say "$(tr_ "    Состояние: $(monitoring_state)" "    State: $(monitoring_state)")"
    if compose_profile_enabled monitoring; then
        _murl=$(env_get "$DOCKER_ENV" OPENCRM_MONITOR_URL 2>/dev/null || true)
        say "$(tr_ "    Панель:    ${_murl}/monitoring/  (логин admin)" "    Dashboard: ${_murl}/monitoring/  (login admin)")"
        if [ -n "$(env_get "$DOCKER_ENV" OPENCRM_MONITORING_TELEGRAM_TOKEN 2>/dev/null || true)" ]; then
            say "$(tr_ "    Тревоги:   Telegram, чат $(env_get "$DOCKER_ENV" OPENCRM_MONITORING_TELEGRAM_CHAT)" "    Alerts:    Telegram, chat $(env_get "$DOCKER_ENV" OPENCRM_MONITORING_TELEGRAM_CHAT)")"
        else
            warn "$(tr_ "тревоги никуда не уходят — Telegram не настроен" "alerts go nowhere — Telegram is not configured")"
        fi
        say ""
        run_painted compose ps prometheus alertmanager grafana || true
        say ""
        menu_item 1 "$(tr_ "Выключить мониторинг" "Turn monitoring off")"
        menu_item 2 "$(tr_ "Логи (Loki): включить / выключить" "Logs (Loki): on / off")"
        menu_item 3 "$(tr_ "Сменить пароль панели" "Change the dashboard password")"
        menu_item 4 "$(tr_ "Перечитать правила тревог сейчас" "Re-read the alert rules now")"
        menu_item 0 "$(tr_ "Ничего не менять" "Leave as is")"
        case "$(ask "$(tr_ "    Выбор" "    Choice")" "0")" in
            1) cmd_monitoring off ;;
            2) cmd_monitoring logs ;;
            3) cmd_monitoring password ;;
            4) cmd_monitoring reload ;;
            *) info "$(tr_ "оставляю как есть" "leaving it as is")" ;;
        esac
    else
        say "$(tr_ \
            "    ${D}Оповещения в Telegram о том, что сайт лёг, кончается место или${R}" \
            "    ${D}Telegram alerts when the site is down, the disk is filling up or${R}")"
        say "$(tr_ \
            "    ${D}истекает сертификат. Плюс графики и логи на /monitoring/.${R}" \
            "    ${D}the certificate is expiring. Plus dashboards and logs at /monitoring/.${R}")"
        info "$(tr_ "цена: ~250 МБ памяти, с логами ~450 МБ" "cost: ~250 MB of memory, ~450 MB with logs")"
        if confirm "$(tr_ "    Включить?" "    Enable it?")" y; then
            cmd_monitoring on
        else
            info "$(tr_ "оставляю как есть" "leaving it as is")"
        fi
    fi
}

# Снять дамп MySQL в указанный файл.
#
# Заходом в контейнер БАЗЫ, а не приложения: клиент `mysqldump` лежит в образе
# MySQL, а в образе приложения его нет и взяться ему там неоткуда.
#
# Пароль разворачивается уже ВНУТРИ контейнера, из окружения самой службы.
# Поэтому его нет ни в командной строке docker на хосте, ни в `ps` — а был бы,
# подставь мы значение здесь. MYSQL_PWD вместо `-p`: пароль не попадает даже в
# список аргументов внутри контейнера, и mysqldump не сыплет предупреждением
# про небезопасный пароль в командной строке.
#
# --single-transaction — то, ради чего всё это: дамп снимается с одного
# согласованного снимка данных и НЕ блокирует работу. Без него mysqldump берёт
# блокировку чтения на все таблицы, и сайт на время копии встаёт.
dump_mysql() {
    compose exec -T db sh -c 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysqldump --single-transaction --routines --triggers --no-tablespaces --default-character-set=utf8mb4 -u root "$MYSQL_DATABASE"' > "$1"
}

cmd_backup() {
    need_install
    step "$(tr_ "Резервная копия" "Backup")"
    if [ "$(db_engine)" = "mysql" ]; then
        _incoming="$(home_dir)/data/backups/incoming.sql"
        mkdir -p "$(home_dir)/data/backups"
        info "$(tr_ "снимаю дамп MySQL" "taking the MySQL dump")"
        if ! dump_mysql "$_incoming"; then
            # Недоснятый дамп убираем сразу: файл, оставшийся от оборванного
            # снятия, в следующий раз выглядел бы как готовая копия.
            rm -f "$_incoming"
            die "$(tr_ "не удалось снять дамп MySQL — ./opencrm.sh logs db" "could not take the MySQL dump — ./opencrm.sh logs db")"
        fi
        # Дальше всё как всегда: имя по дате, архив storage, ключ шифрования,
        # ротация и проверка годности — это одно и то же для обеих баз и живёт
        # в одном месте, в scripts/backup.sh.
        #
        # Путь передаётся такой, каким его видит контейнер приложения:
        # $OPENCRM_HOME/data смонтирован в нём как /app/data (docker-compose.yml),
        # то есть это тот же самый файл, что мы только что записали.
        run_painted compose exec -T -e OPENCRM_DB_DUMP=/app/data/backups/incoming.sql \
            app sh scripts/backup.sh
    else
        run_painted compose exec -T app sh scripts/backup.sh
    fi
    ok "$(tr_ "готово: $(home_dir)/data/backups" "done: $(home_dir)/data/backups")"
}

cmd_restore() {
    need_install
    step "$(tr_ "Восстановление из копии" "Restore from backup")"
    _dir="$(home_dir)/data/backups/daily"
    [ -d "$_dir" ] || die "$(tr_ "копий ещё нет ($_dir)" "no backups yet ($_dir)")"
    say ""
    # Оба вида разом: db-ГГГГ-ММ-ДД.db — SQLite, db-ГГГГ-ММ-ДД.sql — дамп MySQL.
    # Список общий нарочно: базу меняли, а копии от прежней остались лежать
    # рядом, и прятать их значило бы объявить их несуществующими.
    # shellcheck disable=SC2012  # имена копий делает сам скрипт
    ls -1t "$_dir"/db-*.db "$_dir"/db-*.sql 2>/dev/null | head -n 10 | nl -w4 -s') '
    say ""
    _n=$(ask "$(tr_ "    Номер копии (Enter — отмена)" "    Backup number (Enter — cancel)")" "")
    [ -n "$_n" ] || { info "$(tr_ "отменено" "cancelled")"; return 0; }
    # shellcheck disable=SC2012  # имена копий делает сам скрипт
    _db=$(ls -1t "$_dir"/db-*.db "$_dir"/db-*.sql 2>/dev/null | sed -n "${_n}p")
    [ -n "$_db" ] || die "$(tr_ "нет такого номера" "no such number")"
    _stamp=$(basename "$_db" | sed 's/^db-//; s/\.db$//; s/\.sql$//')
    _storage="$_dir/storage-$_stamp.tar.gz"
    [ -f "$_storage" ] || die "$(tr_ "нет пары к базе: $_storage" "no storage archive to match the database: $_storage")"

    # Копию от другой базы восстановить нельзя: дамп MySQL не заливается в
    # SQLite, а файл SQLite не заливается в MySQL. Сказать об этом до
    # остановки сайта дешевле, чем после.
    case "$_db:$(db_engine)" in
        *.sql:sqlite) die "$(tr_ "это дамп MySQL, а установка работает на SQLite" "this is a MySQL dump, but the installation runs on SQLite")" ;;
        *.db:mysql)   die "$(tr_ "это файл SQLite, а установка работает на MySQL — перенос делает scripts/migrate_to_mysql.py" "this is an SQLite file, but the installation runs on MySQL — use scripts/migrate_to_mysql.py")" ;;
    esac

    warn "$(tr_ "текущие данные будут заменены копией от $_stamp" "current data will be replaced by the backup from $_stamp")"
    confirm "$(tr_ "    Продолжить?" "    Continue?")" n || { info "$(tr_ "отменено" "cancelled")"; return 0; }
    run_painted compose stop app

    if [ "$(db_engine)" = "mysql" ]; then
        # Текущее состояние — в сторону, а не в /dev/null: то же правило, что и
        # у SQLite в scripts/restore.sh, только вместо переименования файла
        # приходится снимать дамп. Без него откат неудачного восстановления
        # некуда делать.
        _before="$(home_dir)/data/backups/db-before-restore-$(date +%Y%m%d-%H%M%S).sql"
        info "$(tr_ "снимаю дамп текущей базы: $_before" "dumping the current database to $_before")"
        if ! dump_mysql "$_before"; then
            rm -f "$_before"
            run_painted compose up -d
            die "$(tr_ "не удалось снять дамп текущей базы — ничего не менял" "could not dump the current database — nothing was changed")"
        fi
        # Заливаем клиентом из образа базы, по той же причине, что и дамп.
        # Пароль опять разворачивается внутри контейнера.
        info "$(tr_ "заливаю дамп" "loading the dump")"
        if ! compose exec -T db sh -c 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql --default-character-set=utf8mb4 -u root "$MYSQL_DATABASE"' < "$_db"; then
            run_painted compose up -d
            die "$(tr_ "дамп не залился — прежняя база осталась как была, её копия в $_before" "the dump did not load — the previous database is unchanged, its copy is at $_before")"
        fi
        # storage восстанавливаем прежним путём, а базу приложению трогать
        # нечем — она уже на месте.
        run_painted compose run --rm -T --entrypoint sh -e OPENCRM_SKIP_DB=1 app scripts/restore.sh \
            "/app/data/backups/daily/$(basename "$_db")" \
            "/app/data/backups/daily/$(basename "$_storage")"
    else
        # --entrypoint sh обязателен: у образа ENTRYPOINT — это entrypoint.sh, и
        # `compose run app <команда>` передаёт команду ему аргументами, а не вместо
        # него. Без переопределения вместо восстановления поднимался бы uvicorn.
        run_painted compose run --rm -T --entrypoint sh app scripts/restore.sh \
            "/app/data/backups/daily/$(basename "$_db")" \
            "/app/data/backups/daily/$(basename "$_storage")"
    fi
    run_painted compose up -d
    if wait_health 60; then
        ok "$(tr_ "восстановлено, сайт отвечает" "restored, the site is answering")"
    else
        warn "$(tr_ "сайт не поднялся — смотрите логи" "the site did not come up — check the logs")"
    fi
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
    run_painted compose exec -T app python scripts/reset_root.py --email "$_email" --password "$_password"
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

# Починка после запуска под sudo: вернуть владельца данных и адрес состояния.
#
# Работает и от обычного пользователя (сама попросит пароль), и из-под sudo —
# во втором случае владельцем считается тот, кто вызвал sudo, а не root.
#
# Ничего не удаляет. Данные только переносит, и только когда переносить есть
# куда: если непустые каталоги нашлись с обеих сторон, выбор остаётся за
# человеком — молча слить две базы хуже, чем не сделать ничего.
cmd_repair() {
    step "$(tr_ "Починка прав и владельца" "Repairing ownership and paths")"
    detect_sudo
    installed || die "$(tr_ "сайт ещё не установлен — чинить нечего" "the site is not installed yet — nothing to repair")"

    _owner=$(install_owner)
    if ! _want_uid=$(id -u "$_owner" 2>/dev/null); then
        die "$(tr_ "не удалось определить владельца установки" "could not determine the installation owner")"
    fi
    _want_gid=$(id -g "$_owner")
    _owner_home=$(getent passwd "$_owner" | cut -d: -f6)
    [ -n "$_owner_home" ] || _owner_home="/home/$_owner"

    _env_uid=$(env_get "$DOCKER_ENV" OPENCRM_UID 2>/dev/null || true)
    _env_gid=$(env_get "$DOCKER_ENV" OPENCRM_GID 2>/dev/null || true)
    _env_home=$(env_get "$DOCKER_ENV" OPENCRM_HOME 2>/dev/null || true)
    [ -n "$_env_home" ] || _env_home="$_owner_home/opencrm"
    _want_home="$_owner_home/opencrm"

    say ""
    probe "$(tr_ "владелец" "owner")" 1 "$_owner ($_want_uid:$_want_gid)"
    if [ "$_env_uid" = "$_want_uid" ] && [ "$_env_gid" = "$_want_gid" ]; then
        probe "docker/.env" 1 "UID $_env_uid:$_env_gid — $(tr_ "верно" "correct")"
    else
        probe "docker/.env" 0 "UID $_env_uid:$_env_gid → $_want_uid:$_want_gid"
    fi

    # Состояние в /root — след запуска под sudo: данные хозяина машины остались
    # в его домашней папке, а сайт с тех пор писал в другое место.
    #
    # Проверять существование обязательно через $SUDO. Каталог /root закрыт
    # (0700), и обычный `[ -d /root/opencrm/data ]` от имени пользователя
    # отвечает «нет» не потому, что каталога нет, а потому, что туда не
    # заглянуть. Первая версия этой починки так и сделала: не увидела боевую
    # базу в /root/opencrm, отрапортовала «путь исправим» и оставила сайт с
    # пустым каталогом. Данные были целы, но выглядело это как их потеря.
    _dir_has_data() { $SUDO test -d "$1" && [ -n "$($SUDO ls -A "$1" 2>/dev/null || true)" ]; }

    # Ищем брошенное состояние не только по записи в .env: если починку уже
    # запускали, путь там исправлен, а данные так и остались в /root.
    _root_home=$(getent passwd root | cut -d: -f6 2>/dev/null || true)
    [ -n "$_root_home" ] || _root_home="/root"

    # Брошенным считается место, где остались ЛЮБЫЕ данные — база или медиа.
    # Проверять один лишь data мало: база может уже переехать, а картинки
    # остаться, и тогда сайт выглядит рабочим, но все изображения битые.
    _has_state() { _dir_has_data "$1/data" || _dir_has_data "$1/storage"; }

    _source=""
    if [ "$_env_home" != "$_want_home" ] && _has_state "$_env_home"; then
        _source="$_env_home"
    elif [ "$_root_home/opencrm" != "$_want_home" ] && _has_state "$_root_home/opencrm"; then
        _source="$_root_home/opencrm"
    fi

    _move_state=0
    if [ -z "$_source" ]; then
        if [ "$_env_home" = "$_want_home" ]; then
            probe "$(tr_ "состояние" "state")" 1 "$_env_home"
        else
            probe "$(tr_ "состояние" "state")" 0 "$(tr_ "путь исправим на" "path will become") $_want_home"
        fi
    elif _dir_has_data "$_want_home/data" && _dir_has_data "$_source/data"; then
        probe "$(tr_ "состояние" "state")" 0 "$(tr_ "данные и там, и там" "data in both places")"
        warn "$(tr_ "$_source/data и $_want_home/data — оба непустые" "$_source/data and $_want_home/data are both non-empty")"
        say "$(tr_ \
            "    Какая из баз рабочая, знаете только вы. Перенесите нужную вручную" \
            "    Only you know which database is the live one. Move the right one by hand")"
        say "$(tr_ \
            "    и запустите починку снова." \
            "    and run the repair again.")"
        die "$(tr_ "останавливаюсь, чтобы не потерять данные" "stopping so that no data is lost")"
    else
        probe "$(tr_ "состояние" "state")" 0 "$_source → $_want_home"
        _move_state=1
    fi

    say ""
    say "$(tr_ "    Что будет сделано:" "    What will be done:")"
    say "$(tr_ "      1. остановка сайта" "      1. stop the site")"
    if [ "$_move_state" = "1" ]; then
        say "$(tr_ "      2. перенос $_source → $_want_home" "      2. move $_source → $_want_home")"
    fi
    say "$(tr_ "      3. владелец данных и репозитория → $_owner" "      3. owner of data and repository → $_owner")"
    say "$(tr_ "      4. правка docker/.env (UID, GID, путь)" "      4. update docker/.env (UID, GID, path)")"
    say "$(tr_ "      5. запуск сайта" "      5. start the site")"
    say "$(tr_ \
        "    ${D}Ключи Let's Encrypt не трогаем: их читает nginx от root.${R}" \
        "    ${D}Let's Encrypt keys are left alone: nginx reads them as root.${R}")"
    say ""
    confirm "$(tr_ "    Чиним?" "    Repair?")" y || { info "$(tr_ "отменено" "cancelled")"; return 0; }

    step "$(tr_ "Остановка" "Stopping")"
    compose down >/dev/null 2>&1 || warn "$(tr_ "стек не остановился штатно — продолжаю" "the stack did not stop cleanly — continuing")"

    if [ "$_move_state" = "1" ]; then
        step "$(tr_ "Перенос состояния" "Moving the state")"
        $SUDO mkdir -p "$_want_home"
        # Переносим содержимое, а не каталог: цель может уже существовать
        # (пустая), и `mv` вложил бы источник внутрь неё.
        #
        # `test` снова через $SUDO — источник лежит в /root, куда обычному
        # пользователю не заглянуть.
        for _item in data storage letsencrypt acme autoupdate.env; do
            if ! $SUDO test -e "$_source/$_item"; then continue; fi
            # Пустой каталог в цели — след прошлого запуска починки, а не данные,
            # и он не должен отменять перенос. Именно на этом переехала база, а
            # медиа осталось: сайт выглядел рабочим, но все картинки были битые.
            #
            # Снимаем через rmdir, а не rm: непустой каталог он удалить
            # откажется — ровно та страховка, которая тут и нужна.
            $SUDO rmdir "$_want_home/$_item" 2>/dev/null || true
            if $SUDO test -e "$_want_home/$_item"; then
                warn "$(tr_ "$_item уже есть в цели и не пуст — оставляю как есть" "$_item already exists in the target and is not empty — leaving it alone")"
                continue
            fi
            $SUDO mv "$_source/$_item" "$_want_home/$_item"
            ok "$_item"
        done
    fi

    step "$(tr_ "Права" "Ownership")"
    $SUDO mkdir -p "$_want_home/data" "$_want_home/storage"
    $SUDO chown "$_want_uid:$_want_gid" "$_want_home"
    $SUDO chown -R "$_want_uid:$_want_gid" "$_want_home/data" "$_want_home/storage"
    # Настройки автообновления читает и пишет скрипт от имени человека. Остались
    # root-овскими — и меню спотыкается о «Permission denied» на ровном месте.
    if $SUDO test -e "$_want_home/autoupdate.env"; then
        $SUDO chown "$_want_uid:$_want_gid" "$_want_home/autoupdate.env"
    fi
    if $SUDO test -d "$_want_home/updates"; then
        $SUDO chown -R "$_want_uid:$_want_gid" "$_want_home/updates"
    fi
    ok "$(tr_ "данные: $_want_home" "data: $_want_home")"
    # Репозиторий: под sudo git и правки конфигов оставляли root-овские файлы,
    # после чего обновление вставало на «dubious ownership».
    $SUDO chown -R "$_want_uid:$_want_gid" "$REPO_DIR"
    ok "$(tr_ "репозиторий: $REPO_DIR" "repository: $REPO_DIR")"

    step "$(tr_ "Настройки" "Settings")"
    env_set "$DOCKER_ENV" OPENCRM_UID "$_want_uid"
    env_set "$DOCKER_ENV" OPENCRM_GID "$_want_gid"
    env_set "$DOCKER_ENV" OPENCRM_HOME "$_want_home"
    # env_set пишет от текущего пользователя: под sudo файл снова стал бы
    # root-овским ровно после того, как мы его починили.
    $SUDO chown "$_want_uid:$_want_gid" "$DOCKER_ENV"
    ok "UID $_want_uid:$_want_gid, $_want_home"

    # Автообновление помнит пути отдельно от docker/.env, и там остался /root.
    #
    # Мало переставить владельца файла: внутри лежит OPENCRM_HOME, записанный
    # при установке под sudo. Обновлятор берёт каталог состояния именно оттуда и
    # упирается в «Permission denied: /root/opencrm/updates» — при том, что всё
    # остальное уже починено.
    _auto_env="$_want_home/autoupdate.env"
    if $SUDO test -f "$_auto_env"; then
        env_set "$_auto_env" OPENCRM_HOME "$_want_home"
        env_set "$_auto_env" OPENCRM_UPDATE_PROJECT_DIR "$REPO_DIR"
        chmod 600 "$_auto_env"
        ok "$(tr_ "автообновление: пути исправлены" "auto-update: paths fixed")"
    fi

    # И сам юнит. Под sudo в нём прописался User=root — демон продолжил бы
    # работать от root и заново создавать root-овские файлы, отменяя починку на
    # первом же тике. Это единственное место, где не поправить значит не
    # починить вовсе.
    _unit=/etc/systemd/system/opencrm-autoupdate.service
    if has_systemd && $SUDO test -f "$_unit"; then
        $SUDO sed -i \
            -e "s#^User=.*#User=$_owner#" \
            -e "s#^WorkingDirectory=.*#WorkingDirectory=$REPO_DIR#" \
            -e "s#^EnvironmentFile=.*#EnvironmentFile=$_auto_env#" \
            "$_unit"
        $SUDO systemctl daemon-reload
        $SUDO systemctl restart opencrm-autoupdate >/dev/null 2>&1 || true
        ok "$(tr_ "служба автообновления: работает от $_owner" "auto-update service: runs as $_owner")"
    fi

    step "$(tr_ "Запуск" "Starting")"
    run_painted compose up -d
    if wait_health 90; then
        ok "$(tr_ "сайт отвечает" "the site is answering")"
    else
        warn "$(tr_ "сайт не ответил за 3 минуты — смотрите ./opencrm.sh logs app" "no answer in 3 minutes — see ./opencrm.sh logs app")"
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

    # На чём работает база и поднята ли она. Строка нужна не ради любопытства:
    # почти всё остальное — от копий до восстановления — идёт разными путями
    # для файла и для сервера, и первым делом надо знать, какой из них ваш.
    if [ "$(db_engine)" = "mysql" ]; then
        if compose ps db 2>/dev/null | grep -q "healthy"; then
            probe "$(tr_ "база" "database")" 1 "MySQL ($(tr_ "контейнер db здоров" "container db is healthy"))"
        elif compose ps db 2>/dev/null | grep -q "db"; then
            probe "$(tr_ "база" "database")" 0 "MySQL ($(tr_ "контейнер db не здоров — ./opencrm.sh logs db" "container db is not healthy — ./opencrm.sh logs db"))"
        else
            probe "$(tr_ "база" "database")" 0 "$(tr_ "URL ведёт на MySQL, а службы db в стеке нет — проверьте COMPOSE_PROFILES в docker/.env" "the URL points at MySQL but there is no db service in the stack — check COMPOSE_PROFILES in docker/.env")"
        fi
    else
        probe "$(tr_ "база" "database")" 1 "SQLite ($(home_dir)/data/opencrm.db)"
    fi

    # Схема базы — тот самый вопрос «переживёт ли прод обновление». Спрашиваем
    # само приложение: оно снимает сверку на старте и без неё не поднимается,
    # поэтому ответ здесь не пересчитывается и стоит один запрос.
    _health=$(curl -fsS --max-time 3 http://127.0.0.1/healthz 2>/dev/null || true)
    case "$_health" in
        *'"schema":"ok"'*)
            probe "$(tr_ "схема базы" "database schema")" 1 "$(tr_ "сходится с моделями" "matches the models")" ;;
        *schema*)
            probe "$(tr_ "схема базы" "database schema")" 0 "$(tr_ "не сходится — ./opencrm.sh logs, затем alembic upgrade head" "mismatch — ./opencrm.sh logs, then alembic upgrade head")" ;;
        *'"ok"'*)
            probe "$(tr_ "схема базы" "database schema")" 1 "$(tr_ "приложение старой версии, сверки ещё нет" "older build, no schema check yet")" ;;
        *)
            probe "$(tr_ "схема базы" "database schema")" 0 "$(tr_ "приложение не отвечает — ./opencrm.sh logs" "the application is not answering — ./opencrm.sh logs")" ;;
    esac

    # Резервная копия: когда снята и прошла ли проверку. Отчёт кладёт
    # `scripts/verify_backup.py` рядом с копиями — вопрос «есть ли у нас
    # рабочая копия» должен иметь ответ на диске, а не в чьей-то памяти.
    _check="$(home_dir)/data/backups/last-check.json"
    if [ ! -f "$_check" ]; then
        probe "$(tr_ "резервная копия" "backup")" 0 "$(tr_ "ни одной проверенной копии — ./opencrm.sh backup" "no verified backup yet — ./opencrm.sh backup")"
    elif grep -q '"ok": true' "$_check" 2>/dev/null; then
        _when=$(sed -n 's/.*"checked_at": "\([^"]*\)".*/\1/p' "$_check" | head -n1)
        probe "$(tr_ "резервная копия" "backup")" 1 "$(tr_ "проверена $_when" "verified $_when")"
    else
        probe "$(tr_ "резервная копия" "backup")" 0 "$(tr_ "последняя копия НЕГОДНА — смотрите $_check" "the last backup is BROKEN — see $_check")"
    fi

    # Мониторинг. Три вопроса, и третий важнее первых двух: включён ли он,
    # закрыта ли панель паролем и **уйдёт ли тревога хоть куда-нибудь**.
    # Мониторинг, который всё видит и молчит, отличается от выключенного только
    # съеденной памятью.
    if ! compose_profile_enabled monitoring; then
        probe "$(tr_ "мониторинг" "monitoring")" 1 "$(tr_ "выключен (./opencrm.sh monitoring)" "off (./opencrm.sh monitoring)")"
    else
        if compose ps prometheus 2>/dev/null | grep -q "prometheus"; then
            probe "$(tr_ "мониторинг" "monitoring")" 1 "$(monitoring_state)"
        else
            probe "$(tr_ "мониторинг" "monitoring")" 0 "$(tr_ "профиль включён, а контейнеров нет — ./opencrm.sh monitoring on" "profile is on but there are no containers — ./opencrm.sh monitoring on")"
        fi

        if [ -n "$(env_get "$DOCKER_ENV" OPENCRM_GRAFANA_PASSWORD 2>/dev/null || true)" ]; then
            probe "$(tr_ "панель" "dashboard")" 1 "$(tr_ "закрыта паролем" "password protected")"
        else
            probe "$(tr_ "панель" "dashboard")" 0 "$(tr_ "пароль пуст — Grafana пустит по admin/admin; ./opencrm.sh monitoring password" "password is empty — Grafana will accept admin/admin; ./opencrm.sh monitoring password")"
        fi

        if [ -n "$(env_get "$DOCKER_ENV" OPENCRM_MONITORING_TELEGRAM_TOKEN 2>/dev/null || true)" ]; then
            probe "$(tr_ "тревоги" "alerts")" 1 "Telegram"
        else
            probe "$(tr_ "тревоги" "alerts")" 0 "$(tr_ "канал не настроен — о поломке узнают глазами; ./opencrm.sh monitoring" "no channel — breakage will be spotted by eye; ./opencrm.sh monitoring")"
        fi

        # Проверка сайта обязана идти по внешнему адресу. По внутреннему она
        # зелёная и тогда, когда nginx не поднялся, а 443 никто не слушает, —
        # то есть ровно в том случае, ради которого всё затевалось.
        _murl=$(env_get "$DOCKER_ENV" OPENCRM_MONITOR_URL 2>/dev/null || true)
        if [ -n "$_murl" ]; then
            probe "$(tr_ "проверка сайта" "site probe")" 1 "$_murl"
        else
            probe "$(tr_ "проверка сайта" "site probe")" 0 "$(tr_ "адрес не задан — проверка пойдёт изнутри сети и не увидит 443; ./opencrm.sh monitoring" "no address — the probe will run from inside the network and will not see 443; ./opencrm.sh monitoring")"
        fi
    fi

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

    # Оба вида: .db — файл SQLite, .sql — дамп MySQL. Смотреть только на .db
    # значило бы на установке с MySQL всегда докладывать «ни одной копии» —
    # ровно то сообщение, после которого перестают верить всей строке.
    # shellcheck disable=SC2012  # имена копий делает сам скрипт
    _last=$(ls -1t "$(home_dir)"/data/backups/daily/db-*.db "$(home_dir)"/data/backups/daily/db-*.sql 2>/dev/null | head -n 1)
    if [ -n "$_last" ]; then
        _stamp_last=$(basename "$_last" | sed 's/^db-//; s/\.db$//; s/\.sql$//')
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

    why_down
}

# Почему сайт не отвечает.
#
# Появилось из живого разбора: на сервере сайт лежал, а выяснение причины
# растянулось на несколько заходов «пришлите логи» — каждый ответ порождал
# следующий вопрос. Здесь собрано всё, что для этого нужно, разом: состояние
# контейнеров, слушает ли кто-то 80 и 443, и хвост лога того контейнера,
# который не поднялся.
#
# Раздел молчит, когда сайт отвечает: диагностика не должна тонуть в выводе,
# который в норме никому не нужен.
why_down() {
    if curl -fsS --max-time 3 http://127.0.0.1/healthz 2>/dev/null | grep -q '"ok"'; then
        return 0
    fi

    step "$(tr_ "Почему сайт не отвечает" "Why the site is down")"

    # Обычная таблица, без --format с Go-шаблоном: шаблоны поддерживают не все
    # версии compose, а диагностика обязана работать везде, где работает сайт.
    say "$(tr_ "    Контейнеры:" "    Containers:")"
    compose ps --all 2>&1 | sed 's/^/      /' \
        || say "$(tr_ "      не удалось спросить compose" "      could not ask compose")"

    # Кто слушает 80 и 443. Пусто — снаружи это «отказ соединения», а не
    # страница с ошибкой, и по браузеру причину не отличить.
    say ""
    say "$(tr_ "    Порты:" "    Ports:")"
    _ports=""
    if has ss; then
        _ports=$(${_as_root:-} ss -lntH 2>/dev/null | awk '$4 ~ /:(80|443)$/ {print "      " $4}' || true)
    fi
    if [ -n "$_ports" ]; then
        printf '%s\n' "$_ports"
    else
        say "$(tr_ "      80 и 443 никто не слушает" "      nobody is listening on 80 or 443")"
        say "$(tr_ \
            "      ${D}nginx поднимает 443 только при наличии сертификата, а сам${R}" \
            "      ${D}nginx only opens 443 once a certificate exists, and it${R}")"
        say "$(tr_ \
            "      ${D}не стартует, пока приложение не станет healthy.${R}" \
            "      ${D}will not start until the application is healthy.${R}")"
    fi

    # Логи показываем всегда: раздел и так печатается только когда сайт лежит,
    # а именно этих строк и не хватало каждый раз, чтобы понять причину.
    for _svc in app nginx; do
        say ""
        say "$(tr_ "    Лог $_svc (последние 25 строк):" "    Log for $_svc (last 25 lines):")"
        compose logs --tail 25 "$_svc" 2>&1 | sed 's/^/      /' \
            || say "$(tr_ "      лога нет" "      no log")"
    done

    say ""
    say "$(tr_ \
        "    ${D}Этот вывод можно целиком отдать тому, кто помогает: секретов в нём нет.${R}" \
        "    ${D}This output is safe to share as is: it contains no secrets.${R}")"
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
        menu_item 15 "$(tr_ "Починка прав (после запуска под sudo)" "Repair ownership (after running under sudo)")"
        menu_item 16 "$(tr_ "Мониторинг и оповещения" "Monitoring and alerts")"
        say ""
        menu_item 0  "$(tr_ "Выход" "Exit")"
        say ""
        _choice=$(ask "$(tr_ "  Выбор" "  Choice")" "0")
        case "$_choice" in
            1)  cmd_status ;;
            2)  cmd_start ;;
            3)  cmd_restart ;;
            4)  if confirm "$(tr_ "    Остановить сайт?" "    Stop the site?")" n; then cmd_stop; else info "$(tr_ "отменено" "cancelled")"; fi ;;
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
            15) cmd_repair ;;
            16) cmd_monitoring ;;
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
  ./opencrm.sh repair             починка прав после запуска под sudo
  ./opencrm.sh monitoring [on|off|logs|password|reload]
                                  мониторинг, оповещения и панель /monitoring/

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

    # Заслон от sudo — после разбора аргументов: `repair` и `help` обязаны
    # работать и из-под sudo, иначе чинить последствия было бы нечем.
    case "$_command" in
        repair|help) : ;;
        *) guard_root ;;
    esac
    warn_owner_mismatch

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
        repair)     cmd_repair ;;
        monitoring) cmd_monitoring "${1:-}" ;;
        "")
            if installed; then menu; else cmd_install; fi
            ;;
        *) die "$(tr_ "неизвестная команда: $_command (см. ./opencrm.sh help)" "unknown command: $_command (see ./opencrm.sh help)")" ;;
    esac
}

main "$@"
