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

# Спросить СЕКРЕТ: то же самое, но без эха.
#
# Пароль владельца и токен бота набирались на виду и оставались в прокрутке
# терминала — то есть на общей машине их читал следующий, кто сядет, а в
# записанной сессии они лежат вечно. Пароль в `ps` из этого файла убрали
# дважды с объяснением, а про экран не подумали ни разу.
#
# `stty` возвращается на место ЛОВУШКОЙ, а не строкой следом: человек жмёт
# Ctrl+C в этом месте чаще, чем где-либо ещё («не тот пароль набрал»), и
# терминал остался бы немым до `stty sane`.
ask_secret() {
    _as_prompt=$1
    if [ "$ASSUME_YES" = "1" ]; then
        return 0
    fi
    # Нет управляющего терминала — глушить нечего: ввод пришёл трубой, и
    # эха там не бывает. Тогда обычный путь, иначе `stty` отказом свалит
    # установку под `set -e`.
    if [ "${OPENCRM_INPUT:-tty}" = "stdin" ] || [ ! -r /dev/tty ]; then
        ask "$_as_prompt" ""
        return 0
    fi

    _as_tty=$(stty -g < /dev/tty 2>/dev/null || printf "")
    if [ -n "$_as_tty" ]; then
        trap 'stty "$_as_tty" < /dev/tty 2>/dev/null || true' INT TERM EXIT
        stty -echo < /dev/tty 2>/dev/null || true
    fi
    printf '%s: ' "$_as_prompt" > /dev/tty
    IFS= read -r _as_secret < /dev/tty || _as_secret=""
    if [ -n "$_as_tty" ]; then
        stty "$_as_tty" < /dev/tty 2>/dev/null || true
        trap - INT TERM EXIT
    fi
    # Перевод строки за человека: его Enter съеден вместе с эхом, и без
    # этого следующая строка вывода начиналась бы в конце приглашения.
    printf '\n' > /dev/tty
    printf '%s' "$_as_secret"
}

confirm() {
    _reply=$(ask "$1 (y/n)" "${2:-y}")
    # Кириллица вынесена ИЗ скобочного набора, и это не косметика записи.
    #
    # dash сопоставляет образцы ПОБАЙТНО и многобайтный знак внутри `[...]` не
    # собирает. `[yYдД]` разворачивался во множество байтов {y, Y, D0, B4, 94},
    # и любой ответ, начинающийся с байта D0 — то есть с любой кириллической
    # буквы от «А» до «п», — совпадал. **«нет» означало согласие.**
    #
    # Задеты были все опасные вопросы разом: «Продолжить?» перед заливкой дампа
    # поверх живой базы (`cmd_restore`), «Остановить сайт?» (`tui_stop` и пункт
    # меню), установка ufw с `default deny incoming`. Ответ «нет» делал ровно
    # то, от чего человек отказывался.
    #
    # На разработческой машине беды не видно вовсе: bash собирает многобайтные
    # знаки в наборе и отвечает верно. Видно её только там, где `sh` — это dash,
    # то есть на боевом сервере. Поэтому охранник в `tests/test_install_script.py`
    # гоняет эту функцию настоящим `sh` и пропускается на Windows.
    case "$_reply" in
        [yY]*|д*|Д*) return 0 ;;
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
    for _pkg in git curl; do
        has "$_pkg" || _missing="$_missing $_pkg"
    done
    # ca-certificates — ПАКЕТ, а не программа: исполняемого файла с таким
    # именем нет, и `has` для него ложен всегда. Из-за этого ранний выход был
    # недостижим, и каждый заход обязательно шёл в apt. На только что
    # загруженном VPS, где ещё работает apt-daily, `apt-get update` под
    # `set -e` клал весь мастер установки на первом же шаге — на ровном месте.
    if has dpkg-query; then
        if ! dpkg-query -W -f='${Status}' ca-certificates 2>/dev/null | grep -q "install ok installed"; then
            _missing="$_missing ca-certificates"
        fi
    fi
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

# Ноль печатается и тогда, когда файл ПРОЧИТАЛСЯ, а нужной строки в нём нет.
# Прежняя страховка `|| printf '0'` ловила только отказ awk; при живом
# /proc/meminfo без `SwapTotal:` (контейнер, ядро без подкачки) помощник отдавал
# ПУСТОТУ. В переменной пустота законно считается нулём, а прямо в арифметике
# `$(( $(mem_mb) + $(swap_mb) ))` она превращает выражение в «1024 + » —
# синтаксическую ошибку, которая под `set -e` кладёт скрипт целиком.
mem_mb()  { awk '/^MemTotal:/  {print int($2/1024); f=1} END {if (!f) print 0}' /proc/meminfo 2>/dev/null || printf '0'; }
swap_mb() { awk '/^SwapTotal:/ {print int($2/1024); f=1} END {if (!f) print 0}' /proc/meminfo 2>/dev/null || printf '0'; }
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

# Имя сайта из адреса проверки: без схемы, без порта, без пути.
monitor_host() {
    printf '%s' "${1:-}" | sed 's#^https\?://##; s#/.*##; s#:[0-9]*$##'
}

# Оговорка, без которой правка ниже опаснее болезни. Печатается на экране, а не
# лежит в конце главы документации: человек, ставящий сервер, до документации
# ещё не дошёл, а поверит он именно зелёной строчке.
monitor_local_warning() {
    warn "$(tr_ "проверка пойдёт по локальному адресу и роутер видеть НЕ будет" \
                "the probe will go to the local address and will NOT see the router")"
    say "$(tr_ "        Она видит nginx, TLS и приложение. Отвалится проброс портов — сайт" \
             "        It sees nginx, TLS and the application. Lose the port forwarding and the site")"
    say "$(tr_ "        ляжет для всего мира, а мониторинг останется зелёным." \
             "        goes down for the whole world while monitoring stays green.")"
    say "$(tr_ "        Закрывает это только наблюдатель со стороны: GET /healthz в UptimeRobot" \
             "        Only an outside watcher closes that: GET /healthz in UptimeRobot")"
    say "$(tr_ "        или любом аналоге. Пять минут настройки." \
             "        or any equivalent. Five minutes of setup.")"
}

# Постучаться по адресу проверки С ЭТОГО СЕРВЕРА — и починить, если не вышло.
#
# ПОЧЕМУ ЭТО НАДО СПРАШИВАТЬ ДЕЛОМ, А НЕ ВЕРИТЬ НА СЛОВО. Адрес проверки выводится
# из домена, а домен взят со слов человека. Единственная проверка, которая была
# рядом (issue_certificate), сравнивает A-запись с внешним IP — и при NAT она как
# раз ПРОХОДИТ: A-запись ведёт на роутер, роутер и есть наш публичный адрес.
#
# А сервер за NAT до собственного публичного имени не дозванивается: пакет уходит
# на роутер, а развернуть его обратно внутрь (hairpin NAT) умеет не всякий.
# Проверка тогда красная ВСЕГДА, правило SiteDown шлёт тревогу при полностью
# живом сайте — и её перестают читать вместе с настоящими. Ложная тревога хуже
# отсутствия тревог.
#
# Живой случай, 12 августа 2026: `curl https://sharebranding.xyz/healthz` с самого
# сервера — отказ за 55 мс, `probe_success{job=site}` ноль у обеих целей, сайт при
# этом работает и владелец это уже проверил глазами.
#
# Лечение — пара OPENCRM_MONITOR_HOST/OPENCRM_MONITOR_IP: она уезжает в
# `extra_hosts` контейнера blackbox, то есть в его /etc/hosts. Имя остаётся тем
# же, поэтому запрос идёт по имени, попадает в свой же nginx и получает ТОТ САМЫЙ
# сертификат — проверять его можно по-честному, и `insecure_skip_verify` остаётся
# выключенным. Подстановка серого адреса в сам URL так не умеет: сертификат
# выписан на имя.
#
# Пара пишется ЦЕЛИКОМ или не пишется вовсе: имя без адреса молча уводит проверку
# на 127.0.0.1 — это хуже, чем ничего, потому что выглядит настроенным.
#
# Зовётся из мест, где адрес мог поменяться И стек уже поднят (configure_monitoring,
# `monitoring on`). В sync_monitor_url класть нельзя: её зовут из build_and_start
# (стека ещё нет) и из start/restart — каждый запуск платил бы таймаутом.
probe_monitor_url() {
    _pmurl=$(env_get "$DOCKER_ENV" OPENCRM_MONITOR_URL 2>/dev/null || true)
    [ -n "$_pmurl" ] || return 0
    _pmhost=$(monitor_host "$_pmurl")
    [ -n "$_pmhost" ] || return 0

    # По голому IP проверять нечего: hairpin — беда имени, а не адреса.
    case "$_pmhost" in
        *[!0-9.]*) ;;
        *) return 0 ;;
    esac

    _pmname=$(env_get "$DOCKER_ENV" OPENCRM_MONITOR_HOST 2>/dev/null || true)
    _pmip=$(env_get "$DOCKER_ENV" OPENCRM_MONITOR_IP 2>/dev/null || true)

    # Пара уже стоит и стоит для ЭТОГО имени — значит вопрос задавали, ответ
    # получили. Проверять с хоста бессмысленно: подмена живёт в контейнере, а не
    # здесь, и curl отсюда снова упрётся в тот же hairpin.
    if [ -n "$_pmname" ] && [ "$_pmname" = "$_pmhost" ] && [ -n "$_pmip" ]; then
        ok "$(tr_ "проверка сайта: $_pmhost по локальному адресу $_pmip" \
                  "site probe: $_pmhost via the local address $_pmip")"
        monitor_local_warning
        return 0
    fi

    # `-k`: сертификат тут ни при чём, спрашиваем только «доезжает ли запрос».
    # Его настоящую проверку делает blackbox, и она остаётся строгой.
    if curl -fsS -k --max-time 8 -o /dev/null "$_pmurl/healthz" 2>/dev/null; then
        # Достучались. Осталась одна опасность — прошлая пара от прошлого домена:
        # она бы увела проверку чужого имени неизвестно куда.
        if [ -n "$_pmname" ] && [ "$_pmname" != "$_pmhost" ]; then
            env_set "$DOCKER_ENV" OPENCRM_MONITOR_HOST ""
            env_set "$DOCKER_ENV" OPENCRM_MONITOR_IP ""
            info "$(tr_ "снял подмену адреса от прежнего домена $_pmname" "dropped the address override left from the previous domain $_pmname")"
        fi
        ok "$(tr_ "адрес проверки отвечает отсюда: $_pmurl" "the probe address answers from here: $_pmurl")"
        return 0
    fi

    _pmdns=$(domain_ip "$_pmhost")
    if [ -z "$_pmdns" ]; then
        warn "$(tr_ "адрес $_pmurl отсюда недоступен, и имя $_pmhost не резолвится вовсе" \
                    "$_pmurl is unreachable from here, and the name $_pmhost does not resolve at all")"
        say "$(tr_ "        Проверка будет краснеть всегда, а тревога «сайт лёг» — приходить при живом сайте." \
                 "        The probe will stay red and the \"site is down\" alert will arrive on a healthy site.")"
        say "$(tr_ "        Поправьте домен: ./opencrm.sh domain" "        Fix the domain: ./opencrm.sh domain")"
        return 0
    fi

    _pmpub=$(public_ip)
    if [ -n "$_pmpub" ] && [ "$_pmdns" = "$_pmpub" ]; then
        warn "$(tr_ "отсюда не дозвониться до собственного публичного адреса — это hairpin NAT" \
                    "this server cannot reach its own public address — that is hairpin NAT")"
        say "$(tr_ "        $_pmhost ведёт на $_pmdns, это и есть адрес наружу у этого сервера," \
                 "        $_pmhost points at $_pmdns, which is this server's own address to the world,")"
        say "$(tr_ "        но роутер не разворачивает пакет обратно внутрь. Сайт при этом жив." \
                 "        but the router does not turn the packet back inside. The site itself is fine.")"
    else
        warn "$(tr_ "адрес $_pmurl отсюда не отвечает (имя ведёт на $_pmdns)" \
                    "$_pmurl does not answer from here (the name points at $_pmdns)")"
    fi
    say "$(tr_ "        Оставить как есть — значит получать ложную тревогу «сайт лёг» вечно." \
             "        Leaving it as is means a false \"site is down\" alert forever.")"

    _pmlan=$(lan_ip)
    _pmanswer=$(ask "$(tr_ "    Адрес этого сервера в локальной сети (Enter — оставить как есть)" \
                           "    This server's address on the local network (Enter — leave as is)")" "$_pmlan")
    if [ -z "$_pmanswer" ]; then
        warn "$(tr_ "проверка сайта останется красной; поправить — ./opencrm.sh monitoring" \
                    "the site probe will stay red; fix it with ./opencrm.sh monitoring")"
        return 0
    fi

    # Пара пишется обеими строками сразу — половина хуже, чем ничего.
    env_set "$DOCKER_ENV" OPENCRM_MONITOR_HOST "$_pmhost"
    env_set "$DOCKER_ENV" OPENCRM_MONITOR_IP "$_pmanswer"
    ok "$(tr_ "проверка пойдёт на $_pmhost по адресу $_pmanswer — по имени, значит с настоящим сертификатом" \
              "the probe will go to $_pmhost at $_pmanswer — by name, so with the real certificate")"
    monitor_local_warning
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
    # Сам ФАЙЛ, а не каталог, и ровно тот, что смотрит nginx. Тот же разбор
    # записан у `issue_certificate` двумя сотнями строк ниже: каталог
    # `live/<домен>/` остаётся после оборванного выпуска (Ctrl+C, отказ по
    # лимиту Let's Encrypt, убитый `compose run`) и от переезда со старого
    # сервера, а сертификата в нём нет.
    #
    # Цена расхождения здесь выше, чем там. По пустому каталогу сюда писался
    # `https://домен` при отсутствующем сертификате — и приложение начинало
    # ставить cookie с флагом Secure, а nginx по тому же fullchain.pem держал
    # только HTTP. Браузер Secure-cookie по HTTP молча выбрасывает: вход
    # превращался в «залогинился и тут же вылетел», то есть ровно в ту беду, от
    # которой этот блок и написан.
    if $SUDO test -f "$(home_dir)/letsencrypt/live/$_domain/fullchain.pem"; then
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

# Общий счётчик попыток: адрес и пароль.
#
# Спрашивать нечего — служба поднимается всегда и наружу не смотрит, поэтому
# пароль генерируется, как у базы, и кладётся в те же два места по тому же
# правилу: контейнеру — в docker/.env, приложению — внутрь URL в config/.env.
#
# Почему всегда, а не «если выбрали MySQL»: без общего счётчика защита от
# подбора живёт в памяти процесса, и на любой установке, где однажды поставят
# OPENCRM_WORKERS больше единицы, порог молча умножится на число процессов.
# Поднимать Redis только вместе с MySQL значило бы завести два разных поведения
# защиты и проверять из них одно.
#
# Уже настроенное не трогаем: смена пароля Redis сама по себе безвредна (в нём
# лежат счётчики за последние 15 минут), но разъехавшиеся половины пары дают
# приложение, которое отвечает 503 на вход, — а это уже лежащий сайт.
configure_redis() {
    step "$(tr_ "Общий счётчик попыток (Redis)" "Shared attempt counter (Redis)")"

    _redis_pass=$(env_get "$DOCKER_ENV" OPENCRM_REDIS_PASSWORD 2>/dev/null || true)
    if [ -z "$_redis_pass" ]; then
        # Алфавит A-Za-z0-9 обязателен по той же причине, что и у базы: пароль
        # уезжает внутрь URL, а `@`, `:` и `/` разобрали бы его на части.
        _redis_pass=$(gen_secret 32)
        env_set "$DOCKER_ENV" OPENCRM_REDIS_PASSWORD "$_redis_pass"
    fi
    env_set "$APP_ENV" OPENCRM_REDIS_URL "redis://:$_redis_pass@redis:6379/0"
    ok "$(tr_ "контейнер redis, пароль записан в оба файла" "container redis, password stored in both files")"
    info "$(tr_ "без него защита от подбора пароля и PIN работает только в одном процессе" "without it brute-force protection only works within a single process")"
}

# База — только MySQL. Выбора больше нет, и это решение, а не упрощение.
#
# SQLite допускала ровно одного писателя, поэтому на ней был невозможен второй
# рабочий процесс, а значит невозможно и всё, что за ним стоит. Установка,
# начатая «попроще», рано или поздно упиралась в переезд — то есть в закрытый
# сайт и самую опасную операцию, какая в проекте была. Теперь её нет вовсе:
# ставим сразу то, на чём система работает.
#
# Redis обязателен рядом (configure_redis): в нём общий на все процессы счётчик
# попыток входа и PIN.
nastroit_mysql() {
    step "$(tr_ "База данных" "Database")"

    # Повторный запуск установки пароль НЕ перегенерирует: у поднятой базы
    # пользователь уже создан с прежним, и новый дал бы «access denied» на
    # первом же соединении.
    #
    # Алфавит gen_secret (A-Za-z0-9) обязателен: пароль уезжает внутрь URL, а
    # `@`, `:` и `/` разобрали бы его на части.
    _db_pass=$(env_get "$DOCKER_ENV" OPENCRM_DB_PASSWORD 2>/dev/null || true)
    if [ -z "$_db_pass" ]; then
        _db_pass=$(gen_secret 32)
        env_set "$DOCKER_ENV" OPENCRM_DB_PASSWORD "$_db_pass"
        env_set "$DOCKER_ENV" OPENCRM_DB_ROOT_PASSWORD "$(gen_secret 32)"
    fi
    env_set "$DOCKER_ENV" OPENCRM_DB_NAME opencrm
    env_set "$DOCKER_ENV" OPENCRM_DB_USER opencrm
    # charset=utf8mb4 в URL — вторая половина той же защиты, что и настройка
    # сервера: без неё соединение договаривается о трёхбайтном utf8, и эмодзи
    # в заметке клиента обрывают вставку на полуслове.
    env_set "$APP_ENV" OPENCRM_DB_URL "mysql+pymysql://opencrm:$_db_pass@db:3306/opencrm?charset=utf8mb4"
    ok "$(tr_ "MySQL: контейнер db, пароль записан" "MySQL: container db, password stored")"
    info "$(tr_ "данные базы: $(home_dir)/mysql" "database files: $(home_dir)/mysql")"
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

# --- Наблюдатель за базой -----------------------------------------------------
#
# У `db-exporter` свой пользователь в MySQL, и заводит его установщик. Иначе
# получается худший из видов поломки: контейнер поднят, здоров, в цикл
# перезапусков не уходит, тревог не шлёт — а метрик базы нет вовсе. Увидеть это
# можно только открыв дашборд, то есть в тот единственный день, когда метрики
# базы понадобились.
#
# Выключение мониторинга пользователя из базы НЕ убирает: включат обратно —
# метрики пойдут сразу, а прав на данные у него нет и лежачий он ничего не
# открывает.

# Имя наблюдателя. Читается из docker/.env с тем же умолчанием, что стоит в
# описании службы: разойдись эти двое — установщик завёл бы одного
# пользователя, а экспортёр ходил бы в базу другим.
db_exporter_user() {
    _dxu=$(env_get "$DOCKER_ENV" OPENCRM_DB_EXPORTER_USER 2>/dev/null || true)
    [ -n "$_dxu" ] || _dxu="opencrm_exporter"
    printf '%s' "$_dxu"
}

# Пароль наблюдателя — ровно тем же способом, что пароль Grafana и пароль
# MySQL: генерируется, кладётся в docker/.env с правами 600, в репозиторий не
# попадает. Алфавит gen_secret (A-Za-z0-9) обязателен и здесь: пароль уезжает
# внутрь SQL-литерала в одинарных кавычках.
#
# Существующий не трогаем никогда, и это строже, чем у Grafana: тот же пароль
# записан ВНУТРИ БАЗЫ, у пользователя наблюдателя. Перегенерация на повторном
# запуске развела бы половины пары — экспортёр получил бы «access denied», а
# метрики базы пропали бы молча.
#
# Имя пользователя пишется в файл явно, а не остаётся на умолчании compose: так
# видно, кого искать в `mysql.user`, и так его можно сменить, не правя описание
# стека. Уже записанное имя при этом сохраняется — иначе установщик завёл бы
# второго пользователя мимо того, которым ходит экспортёр.
seed_db_exporter_password() {
    env_set "$DOCKER_ENV" OPENCRM_DB_EXPORTER_USER "$(db_exporter_user)"
    if [ -n "$(env_get "$DOCKER_ENV" OPENCRM_DB_EXPORTER_PASSWORD 2>/dev/null || true)" ]; then
        return 0
    fi
    env_set "$DOCKER_ENV" OPENCRM_DB_EXPORTER_PASSWORD "$(gen_secret 32)"
}

# Завести наблюдателя в базе — или довести уже заведённого до записанного
# пароля.
#
# Четыре запроса, и все повторяются без вреда, поэтому звать можно сколько
# угодно раз:
#
#   CREATE USER IF NOT EXISTS — завести, когда его ещё нет;
#   ALTER USER … IDENTIFIED BY — довести пароль до УЖЕ существующего. Без него
#     пароль из docker/.env разошёлся бы с базой навсегда, и выглядело бы это
#     как «мониторинг включён, а метрик базы нет»;
#   два GRANT — права. Их ровно три: PROCESS, REPLICATION CLIENT и SELECT на
#     performance_schema. Ни одной таблицы с данными клиентов наблюдателю не
#     видно, и утёкший из логов пароль базу не открывает.
#
# MAX_USER_CONNECTIONS 3 — чтобы наблюдатель не съел последние соединения ровно
# тогда, когда их не хватает и он нужнее всего.
#
# НИ ОДИН пароль не уезжает в командную строку хоста: рутовый разворачивается
# внутри контейнера из его собственного окружения (как в dump_mysql), а пароль
# наблюдателя уходит туда же СТАНДАРТНЫМ ВВОДОМ вместе с запросом — тем же
# путём, каким заливается дамп в cmd_restore. В `ps` не видно ни того, ни
# другого.
grant_db_exporter() {
    _dxp=$(env_get "$DOCKER_ENV" OPENCRM_DB_EXPORTER_PASSWORD 2>/dev/null || true)
    [ -n "$_dxp" ] || return 1
    _dxu=$(db_exporter_user)
    # shellcheck disable=SC2016  # рутовый пароль обязан раскрыться ВНУТРИ контейнера
    printf '%s\n' \
        "CREATE USER IF NOT EXISTS '$_dxu'@'%' IDENTIFIED BY '$_dxp' WITH MAX_USER_CONNECTIONS 3;" \
        "ALTER USER '$_dxu'@'%' IDENTIFIED BY '$_dxp' WITH MAX_USER_CONNECTIONS 3;" \
        "GRANT PROCESS, REPLICATION CLIENT ON *.* TO '$_dxu'@'%';" \
        "GRANT SELECT ON performance_schema.* TO '$_dxu'@'%';" \
        | compose exec -T db sh -c 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql -u root'
}

# Заведён ли наблюдатель В САМОЙ БАЗЕ: «1», «0» или пусто, когда спросить не у
# кого. Спрашиваем базу, а не docker/.env: пароль в файле и пользователь в базе
# — разные вещи, и расходятся они ровно в том случае, ради которого проверка и
# нужна (мониторинг включали, пока база лежала).
db_exporter_granted() {
    # shellcheck disable=SC2016  # рутовый пароль обязан раскрыться ВНУТРИ контейнера
    _dxq=$(printf "SELECT COUNT(*) FROM mysql.user WHERE user = '%s';\n" "$(db_exporter_user)" \
        | compose exec -T db sh -c 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql -N -B --connect-timeout=5 -u root' 2>/dev/null || true)
    printf '%s' "$_dxq"
}

# Метрики базы при включении мониторинга.
#
# База в этот момент может быть ещё не поднята: при установке мониторинг
# настраивается до сборки, а `monitoring on` поднимает стек сам. Поэтому
# сначала ждём её готовности — тем же вопросом, каким её проверяет compose.
#
# Не дождались или запрос не прошёл — это НЕ повод завалить включение
# мониторинга: тревоги, панель, метрики машины и проверка сайта работают и без
# метрик базы. Поэтому предупреждение и ровно одна строка о том, чем доделать.
setup_db_exporter() {
    if wait_db 15 && grant_db_exporter; then
        ok "$(tr_ "метрики базы: пользователь $(db_exporter_user) заведён" \
                  "database metrics: user $(db_exporter_user) is in place")"
        return 0
    fi
    warn "$(tr_ "метрики базы не заведены — база не ответила; остальной мониторинг работает" \
                "database metrics are not set up — the database did not answer; the rest of monitoring works")"
    say "$(tr_ "        Поднимется база — повторите ./opencrm.sh monitoring on; проверить — ./opencrm.sh doctor, строка «метрики базы»" \
             "        Once the database is up run ./opencrm.sh monitoring on again; check it with ./opencrm.sh doctor, the \"database metrics\" line")"
}

# Канал оповещений. Бот берётся тот же, что у автообновления: он уже настроен, и
# этот чат уже читают. Заводить второй значит завести второй, который читать
# перестанут.
#
# Значения переносятся в docker/.env, а не читаются из autoupdate.env напрямую.
# Причина не в удобстве: в autoupdate.env рядом лежит токен GitHub, и подключать
# весь файл к контейнеру Alertmanager значило бы отдать ему заодно и его.
#: Свои имена (`_sac_*`) — не украшение. В POSIX sh переменные общие, а эту
#: функцию зовут прямо из `configure_monitoring`, где `_tok` и `_cha` тоже
#: заняты. Сегодня беды нет: вызывающая перечитывает оба значения из `ask`
#: заново. Но именно так и выглядела самая дорогая ошибка живого меню —
#: `_vsego` в шапке против `_vsego` в цикле, — и там она тоже была
#: безвредной ровно до тех пор, пока порядок строк не поменяли.
sync_alert_channel() {
    _sac_auto="$(home_dir)/autoupdate.env"
    _sac_tok=$(env_get "$DOCKER_ENV" OPENCRM_MONITORING_TELEGRAM_TOKEN 2>/dev/null || true)
    _sac_cha=$(env_get "$DOCKER_ENV" OPENCRM_MONITORING_TELEGRAM_CHAT 2>/dev/null || true)
    if [ -z "$_sac_tok" ] && [ -f "$_sac_auto" ]; then
        _sac_tok=$(env_get "$_sac_auto" OPENCRM_UPDATE_TELEGRAM_TOKEN 2>/dev/null || true)
        _sac_cha=$(env_get "$_sac_auto" OPENCRM_UPDATE_TELEGRAM_CHAT 2>/dev/null || true)
    fi
    if [ -z "$_sac_tok" ] || [ -z "$_sac_cha" ]; then
        return 1
    fi
    # Alertmanager принимает chat_id только числом. Имя канала (@name) он
    # отвергнет при разборе конфига и не поднимется вовсе — а узнать об этом
    # хотелось бы сейчас, а не в день первой аварии.
    case "$_sac_cha" in
        ''|*[!0-9-]*)
            warn "$(tr_ "chat_id «$_sac_cha» не число — Alertmanager такой не примет" "chat_id \"$_sac_cha\" is not a number — Alertmanager will not take it")"
            say "$(tr_ "        Нужен числовой id чата (у групп он отрицательный), а не @имя." "        A numeric chat id is required (negative for groups), not @name.")"
            return 1
            ;;
    esac
    env_set "$DOCKER_ENV" OPENCRM_MONITORING_TELEGRAM_TOKEN "$_sac_tok"
    env_set "$DOCKER_ENV" OPENCRM_MONITORING_TELEGRAM_CHAT "$_sac_cha"
    return 0
}

configure_monitoring() {
    step "$(tr_ "Мониторинг" "Monitoring")"

    if compose_profile_enabled monitoring; then
        seed_grafana_password
        seed_db_exporter_password
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
    # Пароль наблюдателя за базой — здесь же, ДО подъёма служб: экспортёр
    # читает его при создании контейнера, и записанный позже подхватился бы
    # только следующим `up`, то есть неизвестно когда. Самого пользователя
    # заводим уже после сборки (cmd_install): базы к этой минуте ещё нет.
    seed_db_exporter_password
    sync_monitor_url
    # Адрес проверки — единственная настройка мониторинга, которую не видно
    # глазами: неверная выглядит точно так же, как верная, а узнают о ней по
    # тревоге при живом сайте. Поэтому спрашиваем делом, а не словом.
    probe_monitor_url

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
    _tok=$(ask_secret "$(tr_ "    Telegram-токен бота (Enter — без оповещений)" "    Telegram bot token (Enter — no alerts)")")
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
    # mysql создаётся всегда: пустой каталог не стоит ничего, а
    # созданный докером на лету принадлежал бы root — и служба базы, включённая
    # позже, упёрлась бы в права на своём же каталоге данных.
    for _sub in data storage letsencrypt acme updates mysql; do
        mkdir -p "$_home/$_sub"
    done
    # Каталоги мониторинга создаются всегда, даже когда он выключен, — по той же
    # причине, что и mysql: созданный докером на лету принадлежал бы root, и
    # включённый позже Prometheus упёрся бы в права на своём же хранилище.
    #
    # Отдельным вызовом, а не строкой в списке выше: этим службам мало
    # существования каталога, им нужен ВЛАДЕЛЕЦ — compose запускает их от
    # OPENCRM_UID, а `mkdir` здесь отработал бы от того, кто ставит, то есть под
    # `sudo` от root. Подробности — у `own_monitoring_dirs`.
    own_monitoring_dirs
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
    # nginx — тоже. Домен доезжает до него ОКРУЖЕНИЕМ (`OPENCRM_DOMAIN` в
    # описании службы), а окружение контейнера замораживается в момент создания.
    # `reload.sh` берёт домен из своего окружения и им же выбирает шаблон по
    # наличию `live/$DOMAIN/fullchain.pem` — то есть после смены домена он
    # перерисовывал конфиг со СТАРЫМ доменом, не находил сертификата и оставлял
    # только 80. Скрипт при этом рапортовал «HTTPS включён»: сертификат вправду
    # выпущен, просто nginx о нём не знает.
    #
    # Без `--force-recreate`: compose сам сверит описание службы с запущенным
    # контейнером и пересоздаст его, только если оно изменилось. Домен тот же —
    # ничего не происходит, и штатный простой в 1-3 секунды, ради устранения
    # которого перезапуск отсюда когда-то и убрали, не возвращается.
    run_painted compose up -d nginx
    # nginx проксирует в app и до его готовности отдаёт 502 — ждём здесь, иначе
    # каждый вызывающий получал бы «сайт лежит» сразу после успешной настройки.
    if wait_health 90; then
        ok "$(tr_ "приложение перезапущено с новыми настройками" "application restarted with the new settings")"
    else
        warn "$(tr_ "приложение не ответило за 3 минуты — смотрите ./opencrm.sh logs app" "no answer from the application in 3 minutes — see ./opencrm.sh logs app")"
    fi
}

# Попросить nginx перечитать конфиг — с повторным рендером шаблона.
#
# Файлы nginx примонтированы из чекаута, а не лежат в образе: `git pull` меняет
# их на диске мгновенно. Но `compose up -d --build` пересоздаёт только те службы,
# у которых изменилось описание или образ, — у nginx не меняется ни то, ни
# другое, и он продолжает работать с конфигом, прочитанным при своём запуске.
# Сам он за файлами не следит.
#
# ПОЧЕМУ ЗДЕСЬ СКРИПТ, А НЕ `nginx -s reload`. Голый reload перечитывает уже
# отрендеренный `default.conf` и его include-ы, но заново подставить домен в
# шаблон он не умеет. Значит правки `http.conf.template`/`https.conf.template`
# им не применяются ВООБЩЕ НИКОГДА. На боевом это кончилось так: include
# дописали в шаблон, а ссылку на него — в locations.inc; include не приехал,
# ссылка приехала, и nginx стал отвергать конфиг целиком на каждом reload. Пять
# суток — потому что ошибка глушилась `>/dev/null 2>&1 || true`. Разбор и выбор
# «перечитывание против перезапуска» — в шапке docker/nginx/reload.sh.
#
# Перезагрузка мягкая: начатые запросы дорабатываются, порты не переоткрываются,
# простоя нет; неудачный конфиг не применяется, и работает прежний.
#
# Не смертельная, но и не молчаливая. Не смертельная — nginx может быть не
# поднят вовсе (у кого-то свой снаружи), и ронять из-за этого удавшуюся
# установку незачем. Не молчаливая — ровно молчание и стоило проекту пяти
# суток с открытыми наружу метриками и адресами клиентов в логах.
# Три попытки, потому что зовут эту функцию сразу после `compose up -d`: контейнер
# уже создан, а мастер nginx мог ещё не подняться, и `compose exec` в это окно
# отвечает отказом. Без повторов установка ругалась бы на ровном месте — а
# предупреждение, которое врёт, читать перестают ровно так же, как молчание.
reload_nginx() {
    _rl_try=3
    while [ "$_rl_try" -gt 0 ]; do
        if _rl_out=$(compose exec -T nginx sh /opencrm/reload.sh 2>&1); then
            return 0
        fi
        _rl_try=$((_rl_try - 1))
        [ "$_rl_try" -eq 0 ] || sleep 2
    done
    warn "$(tr_ "nginx не перечитал конфиг — правки шаблонов и путь /monitoring/ не применились" \
                "nginx did not re-read its config — template changes and /monitoring/ are not applied")"
    printf '%s\n' "$_rl_out" | sed 's/^/        /'
    return 0
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

# Готовность базы — тем же вопросом, каким проверяет её сам контейнер
# (healthcheck в docker-compose.yml): подключением ПО TCP, а не по сокету.
# Сокет отвечает раньше, чем сервер начинает слушать порт, и по нему база
# объявляется готовой за секунды до того, как в неё можно зайти.
#
# Разбирать вывод `compose ps` было бы дешевле и неверно: в строке состояния
# слово «healthy» лежит внутри «unhealthy», и больная база сошла бы за
# здоровую — ровно в том случае, ради которого ожидание и написано.
wait_db() {
    _dbtries=${1:-15}
    while [ "$_dbtries" -gt 0 ]; do
        # shellcheck disable=SC2016  # пароль обязан раскрыться ВНУТРИ контейнера
        if compose exec -T db sh -c 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysqladmin ping --protocol=TCP -u root' >/dev/null 2>&1; then
            return 0
        fi
        _dbtries=$((_dbtries - 1))
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
    # `-T` — по той же причине, что и в migrate_run: `compose run` выделяет
    # псевдотерминал и переводит наш терминал в сырой режим, а вывод здесь идёт
    # в пайп раскраски, откуда возврат срабатывает не всегда. Выпуск идёт с
    # `--non-interactive`, спрашивать нечего.
    if run_painted compose run --rm -T --entrypoint certbot certbot certonly --webroot -w /var/www/certbot \
        -d "$_domain" --email "$_email" --agree-tos --no-eff-email --non-interactive; then
        # Теперь сайт правда за TLS — можно и нужно переводить BASE_URL на https,
        # чтобы cookie получили флаг Secure. Настройки читаются при старте
        # процесса, поэтому контейнер приложения пересоздаётся, а не просто ждёт.
        env_set "$APP_ENV" OPENCRM_BASE_URL "https://$_domain"
        apply_env_change
        # Раньше здесь был `compose restart nginx`, и он был обязателен: выбор
        # шаблона (443 или только 80) делался единственный раз, при старте
        # контейнера, а reload перечитал бы тот же отрендеренный файл. Ценой был
        # единственный штатный простой сайта во всём сценарии установки —
        # 1-3 секунды отказа соединения, без заглушки: её отдаёт сам nginx.
        # Теперь шаблон выбирает reload.sh, и по тому же признаку — файлу
        # fullchain.pem. Перезапуск стал не нужен, простой ушёл.
        reload_nginx
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

    _token=$(ask_secret "$(tr_ "    Telegram-токен бота для уведомлений (Enter — без уведомлений)" "    Telegram bot token for notifications (Enter — no notifications)")")
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
        # Таймер утренней сводки — рядом с копией и тем же приёмом. Отдельным
        # блоком, а не строкой в предыдущем: у них разное время и разный смысл
        # неудачи. Копия не снялась — данные под угрозой; сводка не ушла —
        # владелец не получил письмо, что заметно и само.
        _svd=/etc/systemd/system/opencrm-svodka
        sed -e "s#^User=.*#User=$(id -un)#" \
            -e "s#^WorkingDirectory=.*#WorkingDirectory=$REPO_DIR#" \
            -e "s#^ExecStart=.*#ExecStart=$REPO_DIR/opencrm.sh svodka#" \
            "$REPO_DIR/deploy/systemd/opencrm-svodka.service" | $SUDO tee "$_svd.service" >/dev/null
        $SUDO cp "$REPO_DIR/deploy/systemd/opencrm-svodka.timer" "$_svd.timer"
        $SUDO systemctl daemon-reload
        if $SUDO systemctl enable --now opencrm-svodka.timer >/dev/null 2>&1; then
            ok "$(tr_ "сводка уходит по утрам (systemctl list-timers opencrm-svodka)" "the summary goes out every morning (systemctl list-timers opencrm-svodka)")"
        else
            warn "$(tr_ "таймер сводки не запустился" "the summary timer did not start")"
        fi

        # Уборка старой переписки телеграма. Таймер ставится всегда, а удаляет
        # он что-либо только тогда, когда владелец сам назвал срок хранения:
        # умолчание — хранить вечно. Пустой прогон дешевле, чем расписание,
        # которое надо не забыть поставить в тот день, когда решение примут.
        _tgu=/etc/systemd/system/opencrm-telegram-uborka
        sed -e "s#^User=.*#User=$(id -un)#" \
            -e "s#^WorkingDirectory=.*#WorkingDirectory=$REPO_DIR#" \
            -e "s#^ExecStart=.*#ExecStart=$REPO_DIR/opencrm.sh tg-uborka#" \
            "$REPO_DIR/deploy/systemd/opencrm-telegram-uborka.service" | $SUDO tee "$_tgu.service" >/dev/null
        $SUDO cp "$REPO_DIR/deploy/systemd/opencrm-telegram-uborka.timer" "$_tgu.timer"
        $SUDO systemctl daemon-reload
        if $SUDO systemctl enable --now opencrm-telegram-uborka.timer >/dev/null 2>&1; then
            ok "$(tr_ "уборка переписки ходит по ночам (systemctl list-timers opencrm-telegram-uborka)" "the conversation cleanup runs at night (systemctl list-timers opencrm-telegram-uborka)")"
        else
            warn "$(tr_ "таймер уборки переписки не запустился" "the cleanup timer did not start")"
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
    say "$(tr_ "  База:   MySQL в контейнере db, данные в $(home_dir)/mysql" "  Database: MySQL in container db, files in $(home_dir)/mysql")"
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
    # Строго после configure_app_env и ДО выбора базы: адрес общего счётчика
    # нужен любой установке, а не только той, что переезжает на MySQL.
    configure_redis
    # Строго после configure_app_env: config/.env к этому моменту уже создан, и
    # выбор базы дописывается в него, а не создаёт файл мимо шаблона.
    nastroit_mysql
    configure_domain
    create_dirs
    build_and_start
    check_health
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
        # Пользователь наблюдателя заводится ЗДЕСЬ, а не в configure_monitoring:
        # там базы ещё нет вовсе (сборка идёт выше по списку), а здесь стек уже
        # поднят и check_health дождался живого сайта — значит и базы.
        setup_db_exporter
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
    _au_home=$(home_dir)
    _au_env="$_au_home/autoupdate.env"
    if [ -f "$_au_env" ]; then
        # ПОДОБОЛОЧКА обязательна, и это не аккуратность ради аккуратности.
        #
        # `set -a` экспортирует всё, что есть в файле, а `.` в текущей оболочке
        # оставляет это до конца работы скрипта. Среди прочего там лежит
        # `OPENCRM_HOME` — и у `docker compose` переменная окружения СИЛЬНЕЕ
        # файла `docker/.env`. К `${OPENCRM_HOME}` привязаны все тома стека,
        # включая каталог данных MySQL.
        #
        # Значит стоило двум файлам разойтись (а разойтись им есть отчего —
        # починка прав после sudo правит их по отдельности), и любой `compose
        # up -d` ПОСЛЕ вызова автообновления в том же запуске поднимал бы стек
        # на других каталогах: пустая база, сайт с нуля, а настоящие данные
        # целыми лежат по прежнему пути и выглядят пропавшими.
        #
        # Путей до этого хватало: `cmd_status`, `cmd_history`, `cmd_autoupdate`
        # зовут `autoupdate` напрямую, а меню зовёт их подряд в одном запуске.
        (
            # Директива обязана стоять вплотную к `.`, поэтому он на отдельной
            # строке: в связке `set -a; . файл; set +a` она относилась бы к `set`.
            set -a
            # shellcheck disable=SC1090  # путь известен только в рантайме
            . "$_au_env"
            set +a
            OPENCRM_UPDATE_PROJECT_DIR="$REPO_DIR" python3 "$REPO_DIR/scripts/autoupdate.py" "$@"
        )
    else
        OPENCRM_UPDATE_PROJECT_DIR="$REPO_DIR" python3 "$REPO_DIR/scripts/autoupdate.py" "$@"
    fi
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
MONITORING_SERVICES="prometheus alertmanager node-exporter containers blackbox db-exporter redis-exporter grafana loki promtail"

# Каталоги состояния мониторинга: не только существование, но и ВЛАДЕЛЕЦ.
#
# Prometheus, Alertmanager, Grafana и Loki compose запускает от OPENCRM_UID:GID
# (`user:` в docker-compose.yml), а каталоги под их данные создаёт `mkdir` от
# того, кто запустил установку. Ставили под `sudo` — каталоги достались root, и
# все четверо падают на первой же записи в своё же хранилище:
#
#   prometheus   open /prometheus/queries.active: permission denied → panic
#   grafana      GF_PATHS_DATA='/var/lib/grafana' is not writable
#   loki         mkdir /loki/rules: permission denied
#
# Снаружи это выглядит как «мониторинг не поднялся» и цикл перезапусков, без
# единого слова про права. Данные приложения от этого не страдали только
# потому, что `data` и `storage` чинит установщик отдельной строкой, а каталоги
# мониторинга в тот список не попали.
#
# Владелец выставляется ПЕРЕД КАЖДЫМ подъёмом, а не один раз при установке:
# так чинится и уже сломанная установка — той же командой, которой мониторинг
# включали, без похода в консоль с `chown`.
own_monitoring_dirs() {
    _mhome=$(home_dir)
    _muid=$(env_get "$DOCKER_ENV" OPENCRM_UID 2>/dev/null || true)
    _mgid=$(env_get "$DOCKER_ENV" OPENCRM_GID 2>/dev/null || true)
    [ -n "$_muid" ] || _muid=$(id -u)
    [ -n "$_mgid" ] || _mgid=$(id -g)
    _mne_smog=""
    for _msub in prometheus grafana alertmanager loki promtail; do
        _mdir="$_mhome/monitoring/$_msub"
        if [ ! -d "$_mdir" ]; then
            # Создали сами — владелец уже верный, трогать нечего.
            if ! mkdir -p "$_mdir" 2>/dev/null; then
                _mne_smog="$_mne_smog $_msub"
            fi
            continue
        fi
        # Владелец сошёлся — не трогаем ВОВСЕ. Это обычный случай, и он обязан
        # оставаться бесплатным: `chown` на каталоге Prometheus с полугодом
        # метрик обходит десятки тысяч файлов на каждом включении мониторинга.
        _mvlad=$(stat -c '%u:%g' "$_mdir" 2>/dev/null || printf '%s' "$_muid:$_mgid")
        if [ "$_mvlad" = "$_muid:$_mgid" ]; then
            continue
        fi
        if chown -R "$_muid:$_mgid" "$_mdir" 2>/dev/null; then
            continue
        fi
        if [ -n "$SUDO" ] && $SUDO chown -R "$_muid:$_mgid" "$_mdir" 2>/dev/null; then
            continue
        fi
        _mne_smog="$_mne_smog $_msub"
    done
    # Предупреждение, а не отказ, и это главное здесь.
    #
    # Прежде тут стоял безусловный `$SUDO chown`. Но `cmd_monitoring` не зовёт
    # `detect_sudo` — и правильно не зовёт: спрашивать пароль на `monitoring
    # logs` незачем. Значит `$SUDO` пуст, `chown` на доставшемся от докера
    # root-овском каталоге отвечает «Operation not permitted», а под `set -eu`
    # это не предупреждение, а КОНЕЦ КОМАНДЫ — без единого слова про мониторинг
    # и уже после того, как профиль включён в файлах. Оставалось состояние
    # «включено в файлах, выключено на деле», и связать его с чем-либо было
    # нечем.
    if [ -n "$_mne_smog" ]; then
        warn "$(tr_ "каталоги мониторинга принадлежат другому пользователю:$_mne_smog" "monitoring directories belong to another user:$_mne_smog")"
        say "        sudo chown -R $_muid:$_mgid $_mhome/monitoring"
    fi
    return 0
}

# Поднять (или снять) службы мониторинга И ОБЯЗАТЕЛЬНО дать об этом знать nginx.
#
# Без последней строки включение мониторинга не делало ровно ничего видимого.
# Панель у Grafana своего порта не имеет намеренно (`expose`, а не `ports`):
# единственный вход — через nginx, путь /monitoring/. А nginx про этот путь
# узнаёт из конфига, который он прочитал при своём запуске, — то есть, возможно,
# полгода назад. `compose up -d` его не пересоздаёт: у службы не меняются ни
# образ, ни описание. Проверено на боевом: контейнеры поднялись, все восемь
# здоровы, а /monitoring/ отвечал так, будто мониторинг выключен.
#
# ПЕРЕЧИТЫВАНИЕ, А НЕ ПЕРЕЗАПУСК — и вот цена обоих, вслух.
#
# `compose restart nginx` — это SIGTERM процессу nginx, то есть FAST SHUTDOWN:
# рабочие процессы гибнут немедленно, начатые запросы не доигрываются. Снаружи
# 1-3 секунды НЕ страницы обслуживания, а отказа соединения — заглушку отдаёт
# сам nginx, и пока его нет, отдавать её некому. Рвутся загрузки файлов (лимит
# тела 220 МБ), теряется кэш TLS-сессий. И главная беда: `nginx -t` в entrypoint
# стоит под `set -e` при `restart: unless-stopped` — неудачный конфиг здесь
# означает не «не применилось», а лежащий сайт в цикле перезапусков.
#
# `nginx -s reload` — SIGHUP: старые рабочие доигрывают начатые запросы, сокеты
# не переоткрываются, простоя нет вовсе. Неудачный конфиг просто не применяется.
# Раньше единственным доводом за перезапуск было то, что reload не рендерит
# шаблон заново; теперь это делает reload.sh, и довод исчез.
#
# Платить простоем за включение мониторинга — тем более не то: мониторинг
# заводят, чтобы сайт лежал реже, а не чтобы уронить его при включении.
monitoring_apply() {
    own_monitoring_dirs
    run_painted compose up -d --remove-orphans
    reload_nginx
    warn_module_off
}

# Второй выключатель, о котором никто не помнит.
#
# Этот скрипт поднимает ПРОФИЛЬ ДОКЕРА. Блок «Мониторинг» внутри CRM —
# отдельный переключатель, в интерфейсе, и выключенный он закрывает
# `/api/v1/metrics`: Prometheus получает 403 на каждом опросе, а рядов
# `opencrm_*` не появляется вовсе.
#
# Заметить это самому почти невозможно. Панели машины, базы и Redis полны —
# их наполняют наблюдатели, а не приложение; пустыми остаются только ряды
# приложения, и пустота читается как «пока ничего не происходило». Молчат при
# этом тревоги о старой копии базы, разошедшейся схеме, ограничителе без Redis
# и подборе паролей: все они считаются по `opencrm_*`.
#
# Живой случай (26.08.2026): `GET /api/v1/metrics 403` шло в журнале сервера
# непрерывно — до обновления и после, — и ни одна проверка об этом не говорила.
#
# Сами включить блок отсюда мы не можем: он меняется через API под сессией
# владельца, а у скрипта её нет. Поэтому — сказать вслух в тот момент, когда
# человек как раз занят мониторингом и помнит, зачем пришёл.
warn_module_off() {
    _mcode=$(compose exec -T app python -c "$(printf '%s' '
import urllib.request, urllib.error, sys
try:
    with urllib.request.urlopen("http://127.0.0.1:8000/api/v1/metrics", timeout=5) as o:
        sys.stdout.write(str(o.status))
except urllib.error.HTTPError as e:
    sys.stdout.write(str(e.code))
except Exception:
    sys.stdout.write("")
')" 2>/dev/null || true)
    [ "$_mcode" = "403" ] || return 0
    warn "$(tr_ "стек поднят, но блок «Мониторинг» в CRM выключен — метрики приложения не собираются" \
                "the stack is up, but the Monitoring module is switched off in the CRM — application metrics are not collected")"
    say "$(tr_ "        Включите его в Настройках → Модули, иначе рядов opencrm_* не будет, а тревоги о копии базы, схеме и подборе не сработают" \
             "        Switch it on in Settings → Modules, otherwise there will be no opencrm_* series and alerts about backups, schema and brute force will never fire")"
    say "$(tr_ "        Проверить — ./opencrm.sh doctor, строка «метрики приложения»" \
             "        Check it with ./opencrm.sh doctor, the \"application metrics\" line")"
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

# Куда идти смотреть панель — печатается и после включения, и на экране
# состояния, одним текстом.
#
# Включённый блок обязан ЗАЖИГАТЬ что-то видимое. Раньше `monitoring on`
# заканчивалось словом «включён», и человек оставался без адреса: панель у
# Grafana своего порта не имеет намеренно, найти её угадыванием нельзя.
#
# Пароль здесь НЕ печатается. Он и так лежит в docker/.env с правами 600, а
# вывод команды уходит в историю оболочки, в скроллбек и в чужой лог, если
# команду запускали из автоматизации. Печатать его один раз при генерации
# (`show_summary`) — это осознанное исключение; повторять при каждом включении
# значило бы разложить пароль от карты всей системы по всем логам сразу.
monitoring_panel_hint() {
    _phurl=$(env_get "$DOCKER_ENV" OPENCRM_MONITOR_URL 2>/dev/null || true)
    [ -n "$_phurl" ] || _phurl=$(env_get "$APP_ENV" OPENCRM_BASE_URL 2>/dev/null || true)
    if [ -z "$_phurl" ]; then
        # Домена нет — запуск по IP в локальной сети. `lan_ip` возвращает пустую
        # строку с нулевым кодом, поэтому проверяется значение, а не `||`.
        _phip=$(lan_ip)
        [ -n "$_phip" ] || _phip="127.0.0.1"
        _phurl="http://$_phip"
    fi
    say "$(tr_ "    Панель:   ${_phurl}/monitoring/   (логин admin)" \
             "    Dashboard: ${_phurl}/monitoring/   (login admin)")"
    say "$(tr_ "    Пароль:   строка OPENCRM_GRAFANA_PASSWORD в docker/.env; сменить — ./opencrm.sh monitoring password" \
             "    Password:  the OPENCRM_GRAFANA_PASSWORD line in docker/.env; change it with ./opencrm.sh monitoring password")"
    # Второй вход — мимо nginx, портом 9080. Он публикуется всегда, но привязан к
    # адресу: по умолчанию 127.0.0.1, то есть снаружи не виден никому. Печатаем
    # оба ответа на «хочу в панель напрямую», чтобы вместо них не завели третий —
    # `9080:3000` на все интерфейсы.
    _phbind=$(env_get "$DOCKER_ENV" OPENCRM_GRAFANA_BIND 2>/dev/null || true)
    [ -n "$_phbind" ] || _phbind="127.0.0.1"
    # Номер порта берётся оттуда же, где он и задан: печатать зашитый 9080 при
    # изменённом OPENCRM_GRAFANA_PORT значило бы диктовать человеку неверный
    # адрес — и он пошёл бы искать беду в панели, а не в подсказке.
    _phport=$(env_get "$DOCKER_ENV" OPENCRM_GRAFANA_PORT 2>/dev/null || true)
    [ -n "$_phport" ] || _phport="9080"
    if [ "$_phbind" = "127.0.0.1" ] || [ "$_phbind" = "localhost" ]; then
        # `lan_ip` возвращает пустую строку с НУЛЕВЫМ кодом, поэтому проверяется
        # значение, а не `||`: иначе в примере вышло бы «user@» без адреса.
        _phlan=$(lan_ip)
        [ -n "$_phlan" ] || _phlan="server"
        say "$(tr_ "    Напрямую: только с самого сервера, порт $_phport. С чужой машины — туннелем:" \
                 "    Direct:   from this server only, port $_phport. From another machine — a tunnel:")"
        say "$(tr_ "              ssh -L $_phport:127.0.0.1:$_phport $(id -un)@${_phlan}   затем http://localhost:$_phport/" \
                 "              ssh -L $_phport:127.0.0.1:$_phport $(id -un)@${_phlan}   then http://localhost:$_phport/")"
        say "$(tr_ "              Вход из локальной сети — строка OPENCRM_GRAFANA_BIND в docker/.env" \
                 "              For local-network access set OPENCRM_GRAFANA_BIND in docker/.env")"
    else
        say "$(tr_ "    Напрямую: http://${_phbind}:$_phport/   (без TLS — только для локальной сети или VPN)" \
                 "    Direct:   http://${_phbind}:$_phport/   (no TLS — local network or VPN only)")"
    fi
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
            # Тоже до monitoring_apply, и по той же причине: пароль наблюдателя
            # уезжает в описание контейнера db-exporter.
            seed_db_exporter_password
            sync_monitor_url
            # До monitoring_apply: пара «имя-адрес» уезжает в описание контейнера
            # blackbox, и записанная после него подхватилась бы только следующим
            # `up`, то есть неизвестно когда.
            probe_monitor_url
            sync_alert_channel || warn "$(tr_ "канал Telegram не настроен — тревоги никуда не пойдут" "no Telegram channel — alerts will go nowhere")"
            monitoring_apply
            # А пользователь в базе — ПОСЛЕ подъёма: `compose up -d` не
            # возвращается, пока база не станет здоровой (у приложения стоит
            # `condition: service_healthy`), поэтому здесь она уже отвечает.
            # Команда идемпотентна, поэтому это же и способ починки: забыли
            # завести пользователя, сменили пароль, переехали базой — повторный
            # `monitoring on` доводит всё до записанного в docker/.env.
            setup_db_exporter
            ok "$(tr_ "включён" "on")"
            monitoring_panel_hint
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
            # nginx — в том же списке, и это не для симметрии: путь /monitoring/
            # описан в ЕГО конфиге, и пока он не перечитан, панель недостижима
            # при полностью здоровой Grafana. Мягко, без простоя (см.
            # reload_nginx).
            reload_nginx
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
            # `GF_SECURITY_ADMIN_PASSWORD` действует только при СОЗДАНИИ учётной
            # записи — то есть на первом в жизни старте с пустым каталогом
            # состояния. Дальше пароль живёт в собственной базе Grafana, и
            # переменная на него не влияет никак.
            #
            # Здесь стояло `up -d --force-recreate grafana` с комментарием
            # «пароль читается при создании контейнера». Комментарий отвечал не
            # на тот вопрос, и команда врала человеку: печатала новый пароль,
            # писала его в docker/.env — а пускала панель по-прежнему по
            # старому. Замерено на живой Grafana: после пересоздания вход новым
            # паролем отвергается, старым — проходит.
            #
            # Меняет пароль по-настоящему только `grafana cli`. Со стороны
            # человека это выглядит так же, поэтому проверить разницу можно было
            # ровно одним способом — попробовать войти.
            _np=$(gen_secret 24)
            env_set "$DOCKER_ENV" OPENCRM_GRAFANA_PASSWORD "$_np"

            if [ -z "$(compose ps -q grafana 2>/dev/null || true)" ]; then
                # Панели нет — учётной записи ещё не существует, и переменная
                # сработает сама при первом старте. Это единственный случай,
                # когда прежнее поведение было верным.
                printf '    %s%s%s\n' "$B" "$_np" "$R"
                ok "$(tr_ "пароль записан — подействует при включении мониторинга" \
                         "password saved — it will apply when monitoring is switched on")"
                return 0
            fi

            # Пароль уходит на stdin, а не аргументом: аргументы видны в `ps` и
            # в `docker inspect` — тем же правилом заведён пароль наблюдателя за
            # базой. Флаг проверен на живой Grafana.
            if printf '%s' "$_np" | compose exec -T grafana \
                grafana cli --homepath /usr/share/grafana \
                admin reset-admin-password --password-from-stdin >/dev/null 2>&1; then
                printf '    %s%s%s\n' "$B" "$_np" "$R"
                ok "$(tr_ "пароль сменён" "password changed")"
            else
                # Не молчим и не выдаём успех: человек уходит уверенный, что
                # сменил пароль, и обнаруживает обратное в худший момент.
                die "$(tr_ "Grafana не приняла новый пароль — старый остался в силе. Панель поднята? ./opencrm.sh monitoring" \
                         "Grafana refused the new password — the old one is still in effect. Is the dashboard up? ./opencrm.sh monitoring")"
            fi
            return 0
            ;;
    esac

    say "$(tr_ "    Состояние: $(monitoring_state)" "    State: $(monitoring_state)")"
    if compose_profile_enabled monitoring; then
        monitoring_panel_hint
        if [ -n "$(env_get "$DOCKER_ENV" OPENCRM_MONITORING_TELEGRAM_TOKEN 2>/dev/null || true)" ]; then
            say "$(tr_ "    Тревоги:   Telegram, чат $(env_get "$DOCKER_ENV" OPENCRM_MONITORING_TELEGRAM_CHAT)" "    Alerts:    Telegram, chat $(env_get "$DOCKER_ENV" OPENCRM_MONITORING_TELEGRAM_CHAT)")"
        else
            warn "$(tr_ "тревоги никуда не уходят — Telegram не настроен" "alerts go nowhere — Telegram is not configured")"
        fi
        say ""
        # Экспортёры базы и Redis стоят в списке наравне с остальными: без них
        # «мониторинг включён» означает половину дашборда, а пропажу самого
        # контейнера иначе не видно ниоткуда. Строка здесь отвечает на вопрос
        # «есть ли служба», а не «доходит ли она до базы» — второе спрашивает
        # `./opencrm.sh doctor` строкой «метрики базы».
        run_painted compose ps prometheus alertmanager db-exporter redis-exporter grafana || true
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
    # shellcheck disable=SC2016  # пароль обязан раскрыться ВНУТРИ контейнера:
    # подставленный здесь, он попал бы в командную строку docker и стал виден
    # в `ps` любому пользователю сервера.
    compose exec -T db sh -c 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysqldump --single-transaction --routines --triggers --no-tablespaces --default-character-set=utf8mb4 -u root "$MYSQL_DATABASE"' > "$1"
}

cmd_svodka() {
    need_install
    step "$(tr_ "Утренняя сводка" "Morning summary")"
    # Изнутри контейнера приложения: цифры считают те же репозитории, что
    # отвечают экранам. Второй счёт «снаружи» означал бы два места, где
    # считается одно и то же, и разошлись бы они молча.
    #
    # `-T` обязателен: под systemd и cron терминала нет, и без него docker
    # отказывается выделять псевдотерминал — команда падает не сделав ничего.
    if compose exec -T app python -m scripts.svodka; then
        ok "$(tr_ "сводка обработана" "summary processed")"
    else
        warn "$(tr_ "сводка не ушла — подробности выше" "the summary did not go out — details above")"
        return 1
    fi
}

cmd_tg_uborka() {
    need_install
    step "$(tr_ "Уборка старой переписки" "Old conversation cleanup")"
    # Изнутри контейнера приложения, тем же приёмом, что и сводка: удаляют
    # репозитории канала, а не сочинённый рядом SQL. Второе место, знающее
    # устройство таблицы, разошлось бы с первым на первой же правке схемы.
    #
    # `-T` обязателен: под systemd терминала нет, и без него docker отказывается
    # выделять псевдотерминал — задание падает, не сделав ничего.
    #
    # Задание молчит и выходит с нулём, пока владелец не назвал срок хранения:
    # умолчание — хранить вечно, и расписание не должно краснеть у того, кто
    # уборку не включал.
    if compose exec -T app python -m scripts.telegram_uborka "$@"; then
        ok "$(tr_ "уборка отработала" "cleanup finished")"
    else
        warn "$(tr_ "уборка не отработала — подробности выше" "the cleanup did not finish — details above")"
        return 1
    fi
}

cmd_backup() {
    need_install
    step "$(tr_ "Резервная копия" "Backup")"
    _incoming="$(home_dir)/data/backups/incoming.sql"
    mkdir -p "$(home_dir)/data/backups"
    info "$(tr_ "снимаю дамп базы" "taking the database dump")"
    # 600 до первой записи: в файле вся база целиком, а перенаправление создало
    # бы его с 0644 — читаемым любым пользователем машины. То же правило, что и
    # у копий в `scripts/backup.sh`.
    : > "$_incoming" && chmod 600 "$_incoming"
    if ! dump_mysql "$_incoming"; then
        # Недоснятый дамп убираем сразу: файл, оставшийся от оборванного
        # снятия, в следующий раз выглядел бы как готовая копия.
        rm -f "$_incoming"
        die "$(tr_ "не удалось снять дамп — ./opencrm.sh logs db" "could not take the dump — ./opencrm.sh logs db")"
    fi
    # Дальше всё как всегда: имя по дате, архив storage, ключ шифрования,
    # ротация и проверка годности — это живёт в одном месте, scripts/backup.sh.
    #
    # Путь передаётся такой, каким его видит контейнер приложения:
    # $OPENCRM_HOME/data смонтирован в нём как /app/data (docker-compose.yml),
    # то есть это тот же самый файл, что мы только что записали.
    run_painted compose exec -T -e OPENCRM_DB_DUMP=/app/data/backups/incoming.sql \
        app sh scripts/backup.sh
    ok "$(tr_ "готово: $(home_dir)/data/backups" "done: $(home_dir)/data/backups")"
}

cmd_restore() {
    need_install
    step "$(tr_ "Восстановление из копии" "Restore from backup")"
    _dir="$(home_dir)/data/backups/daily"
    # Недельные — тоже в списке, и это не мелочь удобства. Ежедневных хранится
    # семь; всё, что старше недели, живёт ТОЛЬКО в weekly. Показывая один
    # `daily`, меню объявляло четыре недельные копии несуществующими — ровно
    # тогда, когда они и нужны: беду, замеченную через десять дней, из daily уже
    # не откатить.
    _dirw="$(home_dir)/data/backups/weekly"
    [ -d "$_dir" ] || [ -d "$_dirw" ] || die "$(tr_ "копий ещё нет ($_dir)" "no backups yet ($_dir)")"
    say ""
    # Копии называются db-ГГГГ-ММ-ДД.sql — это дамп.
    # Список общий нарочно: базу меняли, а копии от прежней остались лежать
    # рядом, и прятать их значило бы объявить их несуществующими.
    # shellcheck disable=SC2012  # имена копий делает сам скрипт
    ls -1t "$_dir"/db-*.db "$_dir"/db-*.sql "$_dirw"/db-*.sql 2>/dev/null | head -n 10 | nl -w4 -s') '
    say ""
    _n=$(ask "$(tr_ "    Номер копии (Enter — отмена)" "    Backup number (Enter — cancel)")" "")
    [ -n "$_n" ] || { info "$(tr_ "отменено" "cancelled")"; return 0; }
    # Номер проверяется ДО подстановки в sed. Всё, что человек набрал, уезжает
    # в выражение `sed -n "${_n}p"`, и на «y», «-1», «2:» sed отвечает своей
    # ошибкой и кодом 1. Он последний в конвейере, значит подстановка возвращает
    # 1, значит присваивание возвращает 1, и под `set -e` скрипт кончается ТУТ
    # ЖЕ. Заготовленное строкой ниже понятное «нет такого номера» не
    # срабатывало никогда, а человек получал невнятную ругань sed на опечатку.
    case "$_n" in
        ''|*[!0-9]*) die "$(tr_ "нет такого номера" "no such number")" ;;
    esac
    # shellcheck disable=SC2012  # имена копий делает сам скрипт
    _db=$(ls -1t "$_dir"/db-*.db "$_dir"/db-*.sql "$_dirw"/db-*.sql 2>/dev/null | sed -n "${_n}p")
    [ -n "$_db" ] || die "$(tr_ "нет такого номера" "no such number")"
    _stamp=$(basename "$_db" | sed 's/^db-//; s/\.db$//; s/\.sql$//')
    # Пара ищется РЯДОМ с выбранной базой: недельная копия лежит в weekly, и
    # жёсткий `$_dir` не нашёл бы её архив.
    _storage="$(dirname "$_db")/storage-$_stamp.tar.gz"
    [ -f "$_storage" ] || die "$(tr_ "нет пары к базе: $_storage" "no storage archive to match the database: $_storage")"

    # Копию от другой базы восстановить нельзя: дамп MySQL не заливается в
    # Файл SQLite среди копий может остаться от прежних времён. Заливать его
    # некуда, и сказать об этом надо ДО остановки сайта.
    case "$_db" in
        *.db) die "$(tr_ "это файл SQLite от прежней установки — заливать его некуда" "this is an SQLite file from an older installation — there is nowhere to load it")" ;;
    esac

    # Копия дочитана до метки конца? Спрашивается ЗДЕСЬ, до остановки сайта и
    # до заливки, потому что здесь это ещё бесплатно.
    #
    # Оборванный дамп (кончилось место, убили контейнер) — обычный текстовый
    # файл, и негодность у него не видна ничем, кроме отсутствующего хвоста.
    # `mysql` заливает такой файл БЕЗ ЖАЛОБ и выходит с нулём. Проверено: копия,
    # оборванная ровно на границе оператора, восстанавливалась «успешно», а
    # таблиц `users`, `warehouses`, `works`, `stock_moves` в базе после этого не
    # было вовсе. Человек при этом видел «восстановлено, сайт отвечает».
    #
    # Та же проверка стоит и в `scripts/restore.sh`, но досюда она не
    # дотягивается: этот путь заливает дамп САМ и зовёт скрипт уже с
    # OPENCRM_SKIP_DB=1 — то есть проверка сработала бы после порчи.
    #
    # Меток две, потому что дамперов двое: `-- Dump completed` пишет mysqldump,
    # `-- opencrm snapshot complete` — scripts/snapshot_db.py.
    if ! tail -c 4096 "$_db" 2>/dev/null | grep -q -e "-- Dump completed" -e "-- opencrm snapshot complete"; then
        die "$(tr_ "копия оборвана — метки конца в ней нет, заливать такую нельзя: $_db"                  "the backup is truncated — it has no end marker, loading it is not safe: $_db")"
    fi

    warn "$(tr_ "текущие данные будут заменены копией от $_stamp" "current data will be replaced by the backup from $_stamp")"
    confirm "$(tr_ "    Продолжить?" "    Continue?")" n || { info "$(tr_ "отменено" "cancelled")"; return 0; }
    run_painted compose stop app

    # Текущее состояние — в сторону, а не в /dev/null: без него откат
    # неудачного восстановления делать некуда.
    _before="$(home_dir)/data/backups/db-before-restore-$(date +%Y%m%d-%H%M%S).sql"
    info "$(tr_ "снимаю дамп текущей базы: $_before" "dumping the current database to $_before")"
    # 600 сразу: в этом файле лежит вся система целиком, а создаёт его
    # перенаправление с правами по умолчанию (0644 при обычной умаске).
    : > "$_before" && chmod 600 "$_before"
    if ! dump_mysql "$_before"; then
        rm -f "$_before"
        run_painted compose up -d
        die "$(tr_ "не удалось снять дамп текущей базы — ничего не менял" "could not dump the current database — nothing was changed")"
    fi
    # Заливаем клиентом из образа базы, по той же причине, что и дамп.
    # Пароль опять разворачивается внутри контейнера.
    info "$(tr_ "заливаю дамп" "loading the dump")"
    # shellcheck disable=SC2016  # пароль раскрывается внутри контейнера, см. dump_mysql
    if ! compose exec -T db sh -c 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql --default-character-set=utf8mb4 -u root "$MYSQL_DATABASE"' < "$_db"; then
        run_painted compose up -d
        # Не «осталась как была». Дамп начинается с `DROP TABLE`, и клиент
        # выполняет его по мере чтения: отказ на середине означает базу,
        # подменённую НАПОЛОВИНУ. Прежнее сообщение успокаивало ровно там, где
        # надо было немедленно вернуться из $_before, — и человек, поверив ему,
        # шёл искать причину, пока сайт работал на половине таблиц.
        die "$(tr_ "дамп долился не до конца — база подменена частично; прежняя целиком лежит в $_before, вернуть её: ./opencrm.sh restore" "the dump did not load fully — the database is partially replaced; the previous one is whole at $_before, bring it back with ./opencrm.sh restore")"
    fi
    # storage восстанавливаем прежним путём, а базу приложению трогать нечем —
    # она уже на месте.
    #
    # --entrypoint sh обязателен: у образа ENTRYPOINT — это entrypoint.sh, и
    # `compose run app <команда>` передаёт команду ему аргументами, а не вместо
    # него. Без переопределения вместо восстановления поднимался бы uvicorn.
    # Отказ обязан ПОДНЯТЬ САЙТ и объяснить, куда откатываться. Обе соседние
    # ветки так и сделаны, а эта единственная выпала — и стоила бы дорого:
    # приложение уже остановлено, база уже подменена, и `set -e` на голой
    # команде завершал скрипт молча. Человек оставался с погашенным сайтом,
    # подменённой базой и без единого слова о том, что произошло.
    if ! run_painted compose run --rm -T --entrypoint sh -e OPENCRM_SKIP_DB=1 app \
        scripts/restore.sh \
        "/app/data/backups/$(basename "$(dirname "$_db")")/$(basename "$_db")" \
        "/app/data/backups/$(basename "$(dirname "$_storage")")/$(basename "$_storage")"; then
        run_painted compose up -d
        die "$(tr_ "файлы не восстановились — база УЖЕ подменена копией от $_stamp, прежняя лежит в $_before" "the files were not restored — the database is ALREADY replaced by the backup from $_stamp, the previous one is at $_before")"
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
    _password=$(ask_secret "$(tr_ "    Новый пароль (Enter — сгенерировать)" "    New password (Enter — generate one)")")
    if [ -z "$_password" ]; then
        _password=$(gen_secret 20)
        printf '    Сгенерирован: %s%s%s\n' "$B" "$_password" "$R"
    fi
    # Пароль уходит СТАНДАРТНЫМ ВВОДОМ, а не аргументом. Аргументы видны в
    # `ps` любому пользователю машины (/proc/<pid>/cmdline читается всеми) и
    # оседают в `docker inspect`. Пароль владельца системы — последнее, что
    # стоит там оставлять.
    #
    # Правило в этом файле записано уже дважды — у пароля наблюдателя базы
    # (`grant_db_exporter`) и у пароля панели (`monitoring password`), — и оба
    # раза с объяснением. Сюда оно просто не дошло.
    #
    # `run_painted` стандартный ввод не трогает, конвейер до него доходит.
    printf '%s\n' "$_password" | run_painted compose exec -T app python scripts/reset_root.py --email "$_email" --password-stdin
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

# Режим обслуживания из командной строки.
#
# Существует ради одного случая: сайт закрыл НЕ человек, а оборванный переезд
# базы, — и открыть его через настройки может только root, который в закрытый
# сайт как раз проходит и потому беды не видит. Команда даёт способ открыть
# сайт с сервера, не заходя в CRM и не поднимая приложение заново.
#
# Работает тем же сервисом, что и переключатель в настройках
# (`scripts/maintenance.py` → `core/services/maintenance_mode.py`), а не второй
# его разновидностью: два способа закрыть сайт однажды разошлись бы.
# Тот же режим из меню. Сначала показываем, как сейчас, и только потом
# предлагаем переключить: человек приходит сюда, НЕ ЗНАЯ, закрыт ли сайт, —
# именно этого знания ему и не хватало, раз он видит сайт рабочим.
menu_maintenance() {
    need_install
    step "$(tr_ "Режим обслуживания" "Maintenance mode")"
    if ! compose exec -T app python -m scripts.maintenance status; then
        die "$(tr_ "приложение не отвечает — ./opencrm.sh logs app" "the application is not answering — ./opencrm.sh logs app")"
    fi
    say ""
    menu_item 1 "$(tr_ "Открыть сайт" "Open the site")"
    menu_item 2 "$(tr_ "Закрыть сайт на обслуживание" "Close the site for maintenance")"
    say ""
    _mm=$(ask "$(tr_ "  Выбор" "  Choice")" "1")
    case "$_mm" in
        2) cmd_maintenance on ;;
        *) cmd_maintenance off ;;
    esac
}

cmd_maintenance() {
    need_install
    case "$1" in
        on|off|status) ;;
        *) die "$(tr_ "нужно on, off или status" "expected on, off or status")" ;;
    esac
    step "$(tr_ "Режим обслуживания" "Maintenance mode")"
    if ! compose exec -T app python -m scripts.maintenance "$1" \
        --note "$(tr_ "Технические работы" "Maintenance")"; then
        die "$(tr_ "приложение не отвечает — ./opencrm.sh logs app" "the application is not answering — ./opencrm.sh logs app")"
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
        # Тот же `chown` ПОСЛЕ `env_set`, что и у docker/.env десятью строками
        # выше, и по той же причине: `env_set` правит файл не на месте, а через
        # временный и `mv`, то есть создаёт НОВЫЙ файл от имени текущего
        # пользователя. Под sudo это root — и файл, только что отданный
        # владельцу, снова становился root-овским ровно в починке, которая
        # затевалась ради него.
        #
        # Дальше владелец его не прочитает, а `autoupdate` втягивает файл через
        # `.` — dash на неоткрываемом файле обрывает скрипт целиком. То есть
        # «починка» ломала автообновление насмерть, отрапортовав об успехе.
        #
        # `chmod` тоже через `$SUDO`: после `chown` файл уже не наш.
        $SUDO chmod 600 "$_auto_env"
        $SUDO chown "$_want_uid:$_want_gid" "$_auto_env"
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

    # Поднята ли база. Строка нужна не ради любопытства: почти всё остальное —
    # от копий до восстановления — идёт через неё, и первым делом надо знать,
    # жива ли она вообще.
    # `(healthy)` со скобками, а не голое слово. В строке состояния докера
    # «healthy» лежит внутри «unhealthy» — `Up 5 minutes (unhealthy)`, — и
    # больная база сходила за здоровую ровно в том случае, ради которого сюда и
    # смотрят. Ветка «не здоров» ниже была при этом недостижима вовсе.
    #
    # Тот же разбор записан в шапке `wait_db`: там урок был учтён, здесь — нет.
    if compose ps db 2>/dev/null | grep -q "(healthy)"; then
        probe "$(tr_ "база" "database")" 1 "MySQL ($(tr_ "контейнер db здоров" "container db is healthy"))"
    elif compose ps db 2>/dev/null | grep -q "db"; then
        probe "$(tr_ "база" "database")" 0 "MySQL ($(tr_ "контейнер db не здоров — ./opencrm.sh logs db" "container db is not healthy — ./opencrm.sh logs db"))"
    else
        probe "$(tr_ "база" "database")" 0 "$(tr_ "службы db в стеке нет — ./opencrm.sh logs db" "there is no db service in the stack — ./opencrm.sh logs db")"
    fi

    # Общий счётчик попыток. Строка стоит рядом с базой не случайно: это
    # вторая половина переезда на MySQL. Без неё несколько рабочих процессов
    # означают порог защиты от подбора, умноженный на их число, — без единой
    # ошибки, без следа в логах и с виду работающей защитой.
    case "$(curl -fsS --max-time 3 http://127.0.0.1/healthz 2>/dev/null || true)" in
        *'"redis":"ok"'*)
            probe "$(tr_ "счётчик попыток" "attempt counter")" 1 "$(tr_ "общий на все процессы (redis)" "shared across processes (redis)")" ;;
        *'"redis":"down"'*)
            probe "$(tr_ "счётчик попыток" "attempt counter")" 0 "$(tr_ "redis не отвечает — вход и PIN сейчас отдают 503; ./opencrm.sh logs redis" "redis does not answer — sign-in and PIN return 503 right now; ./opencrm.sh logs redis")" ;;
        *'"redis":"off"'*)
            probe "$(tr_ "счётчик попыток" "attempt counter")" 0 "$(tr_ "OPENCRM_REDIS_URL пуст — защита от подбора работает только в одном процессе" "OPENCRM_REDIS_URL is empty — brute-force protection only works within one process")" ;;
        *) ;;
    esac

    # Схема базы — тот самый вопрос «переживёт ли прод обновление». Спрашиваем
    # само приложение: оно снимает сверку на старте и без неё не поднимается,
    # поэтому ответ здесь не пересчитывается и стоит один запрос.
    _health=$(curl -fsS --max-time 3 http://127.0.0.1/healthz 2>/dev/null || true)

    # Закрытый сайт. Строка стоит здесь, а не среди мелочей, потому что это
    # единственное состояние, в котором ВСЁ ОСТАЛЬНОЕ зелёное, а сайта нет.
    #
    # Режим включает не только человек: его закрывает и открывает переезд базы
    # (`migrate_maintenance`). Оборванный переезд оставлял сайт закрытым, и не
    # видел этого никто — ни докер, ни автообновление, ни мониторинг. Хуже
    # того, владелец в закрытый сайт проходит по устройству режима и видит его
    # работающим: тот, кто может открыть, беды и не замечает.
    case "$_health" in
        *'"maintenance":"on"'*)
            probe "$(tr_ "обслуживание" "maintenance")" 0 "$(tr_ "сайт ЗАКРЫТ: сотрудники и посетители видят 503; снять — ./opencrm.sh maintenance off или в настройках сайта" "the site is CLOSED: staff and visitors get 503; lift it with ./opencrm.sh maintenance off or in site settings")" ;;
        *'"maintenance":"off"'*)
            probe "$(tr_ "обслуживание" "maintenance")" 1 "$(tr_ "сайт открыт" "the site is open")" ;;
        *) ;;
    esac
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

        # «Панель открывается?» — ВОПРОСОМ К ПАНЕЛИ, а не по наличию пароля.
        #
        # Прежде здесь стояла проверка непустого OPENCRM_GRAFANA_PASSWORD, и она
        # рапортовала «закрыта паролем» ровно в том случае, ради которого
        # диагностику и запускают: контейнеры здоровы, пароль на месте, а панель
        # недостижима, потому что nginx работает с конфигом, отрендеренным до
        # появления пути /monitoring/. На боевом это продержалось пять суток.
        #
        # Идём тем же путём, что и человек, — через nginx на 127.0.0.1. Редирект
        # проходится (`-L`): при включённом HTTPS порт 80 отвечает 301 на
        # https://, а сертификат выписан на домен, не на адрес, поэтому `-k`.
        # Проверка не про TLS, она про то, доезжает ли запрос до Grafana.
        _panel=$(curl -sk -L --max-redirs 5 --max-time 8 http://127.0.0.1/monitoring/ 2>/dev/null || true)
        case "$_panel" in
            *[Gg]rafana*)
                probe "$(tr_ "панель" "dashboard")" 1 "$(tr_ "открывается: /monitoring/, логин admin" "opens at /monitoring/, login admin")" ;;
            *"Monitoring is switched off"*)
                probe "$(tr_ "панель" "dashboard")" 0 "$(tr_ "nginx отвечает «выключено», а профиль включён — ./opencrm.sh monitoring on" "nginx answers \"switched off\" while the profile is on — ./opencrm.sh monitoring on")" ;;
            "")
                probe "$(tr_ "панель" "dashboard")" 0 "$(tr_ "/monitoring/ не отвечает вовсе — поднят ли nginx: ./opencrm.sh logs nginx" "/monitoring/ does not answer at all — is nginx up: ./opencrm.sh logs nginx")" ;;
            *)
                probe "$(tr_ "панель" "dashboard")" 0 "$(tr_ "по /monitoring/ отвечает не панель: nginx работает со старым конфигом — ./opencrm.sh monitoring reload" "/monitoring/ is answered by something other than the dashboard: nginx runs an old config — ./opencrm.sh monitoring reload")" ;;
        esac

        # Пароль — отдельной строкой: панель может открываться и при пустом
        # пароле, и это худший из исходов, а не лучший.
        if [ -n "$(env_get "$DOCKER_ENV" OPENCRM_GRAFANA_PASSWORD 2>/dev/null || true)" ]; then
            probe "$(tr_ "пароль панели" "dashboard password")" 1 "$(tr_ "задан (docker/.env)" "set (docker/.env)")"
        else
            probe "$(tr_ "пароль панели" "dashboard password")" 0 "$(tr_ "пуст — Grafana пустит по admin/admin; ./opencrm.sh monitoring password" "empty — Grafana will accept admin/admin; ./opencrm.sh monitoring password")"
        fi

        # По ПАРЕ, а не по одному токену. Точка входа Alertmanager решает ровно
        # так же: нет токена ИЛИ нет chat_id — уходит на молчащий конфиг. Значит
        # полупара «токен есть, чата нет» — это мониторинг, который всё видит и
        # молчит, а диагностика ставила ему зелёный плюс. Породить полупару умеет
        # штатный путь: `configure_monitoring` пишет оба значения безусловно, и
        # пустой ответ на вопрос про chat_id её и даёт.
        _dtok=$(env_get "$DOCKER_ENV" OPENCRM_MONITORING_TELEGRAM_TOKEN 2>/dev/null || true)
        _dcha=$(env_get "$DOCKER_ENV" OPENCRM_MONITORING_TELEGRAM_CHAT 2>/dev/null || true)
        if [ -n "$_dtok" ] && [ -n "$_dcha" ]; then
            probe "$(tr_ "тревоги" "alerts")" 1 "Telegram"
        elif [ -n "$_dtok" ]; then
            probe "$(tr_ "тревоги" "alerts")" 0 "$(tr_ "токен есть, chat_id пуст — Alertmanager поднят с молчащим конфигом; ./opencrm.sh monitoring" "the token is set but chat_id is empty — Alertmanager runs a silent config; ./opencrm.sh monitoring")"
        else
            probe "$(tr_ "тревоги" "alerts")" 0 "$(tr_ "канал не настроен — о поломке узнают глазами; ./opencrm.sh monitoring" "no channel — breakage will be spotted by eye; ./opencrm.sh monitoring")"
        fi

        # Метрики базы. Без пользователя в MySQL наблюдатель отдаёт `mysql_up 0`
        # и молчит: контейнер здоров, в цикл перезапусков не уходит, тревоги не
        # шлёт. Снаружи это неотличимо от работающего мониторинга, а на деле нет
        # ни соединений, ни ожиданий замков, ни буферного пула — то есть всего
        # того, по чему деградацию базы замечают до падения сайта.
        #
        # Спрашиваем базу, а не только файл: пароль в docker/.env и
        # пользователь в MySQL расходятся именно в том случае, ради которого
        # строка нужна (мониторинг включали, пока база лежала).
        _dxuser=$(db_exporter_user)
        if [ -z "$(env_get "$DOCKER_ENV" OPENCRM_DB_EXPORTER_PASSWORD 2>/dev/null || true)" ]; then
            probe "$(tr_ "метрики базы" "database metrics")" 0 "$(tr_ "пароль наблюдателя не задан — метрик базы нет; ./opencrm.sh monitoring on" "no watcher password — there are no database metrics; ./opencrm.sh monitoring on")"
        else
            case "$(db_exporter_granted)" in
                "")
                    probe "$(tr_ "метрики базы" "database metrics")" 1 "$(tr_ "не проверить — база не отвечает" "cannot check — the database is not answering")" ;;
                0)
                    probe "$(tr_ "метрики базы" "database metrics")" 0 "$(tr_ "пользователь $_dxuser в базе не заведён — на дашборде «нет доступа к базе»; ./opencrm.sh monitoring on" "user $_dxuser does not exist in the database — the dashboard shows \"no access to the database\"; ./opencrm.sh monitoring on")" ;;
                *)
                    probe "$(tr_ "метрики базы" "database metrics")" 1 "$(tr_ "собираются под пользователем $_dxuser" "collected as user $_dxuser")" ;;
            esac
        fi

        # Метрики САМОГО ПРИЛОЖЕНИЯ. Два выключателя, один результат — и об
        # этом не говорило ничто.
        #
        # `./opencrm.sh monitoring on` поднимает профиль докера: Prometheus,
        # Grafana, наблюдатели за машиной, базой и Redis. Блок «Мониторинг»
        # ВНУТРИ CRM — отдельный переключатель, в интерфейсе. Выключенный, он
        # закрывает `/api/v1/metrics` вместе с остальным своим API, и Prometheus
        # получает 403 на каждом опросе.
        #
        # Снаружи это выглядит работающим мониторингом: панели машины, базы и
        # Redis полны — их данные приходят от наблюдателей, а не от приложения.
        # Пустыми остаются только ряды `opencrm_*`, и пустота читается как «пока
        # ничего не происходило». А не работает при этом целый класс тревог:
        # старая копия базы, разошедшаяся схема, ограничитель без Redis,
        # заблокированная загрузка, подбор паролей — все они считаются по
        # `opencrm_*` и не сработают никогда.
        #
        # Живой случай (26.08.2026): в журнале сервера `GET /api/v1/metrics 403`
        # шло непрерывно, до обновления и после, а `doctor` был полностью
        # зелёным.
        #
        # Правила «цель не отвечает» на приложение нет намеренно — выключенный
        # блок это законное состояние, и звонить о нём ночью незачем (разбор — в
        # шапке `docker/monitoring/prometheus/rules/opencrm.yml`). Поэтому
        # состояние говорится ЗДЕСЬ, где его смотрят руками.
        #
        # Спрашиваем само приложение изнутри его контейнера: это ровно тот путь,
        # которым ходит Prometheus, и он отвечает на вопрос «собираются ли»,
        # а не «что записано в настройках».
        _mcode=$(compose exec -T app python -c "$(printf '%s' '
import urllib.request, urllib.error, sys
try:
    with urllib.request.urlopen("http://127.0.0.1:8000/api/v1/metrics", timeout=5) as o:
        sys.stdout.write(str(o.status))
except urllib.error.HTTPError as e:
    sys.stdout.write(str(e.code))
except Exception:
    sys.stdout.write("")
')" 2>/dev/null || true)
        case "$_mcode" in
            200)
                probe "$(tr_ "метрики приложения" "application metrics")" 1 "$(tr_ "собираются" "collected")" ;;
            403)
                probe "$(tr_ "метрики приложения" "application metrics")" 0 "$(tr_ "блок «Мониторинг» в CRM выключен — рядов opencrm_* нет, и тревоги о копии, схеме и подборе не сработают; включите его в Настройках → Модули" "the Monitoring module is switched off in the CRM — there are no opencrm_* series, and alerts about backups, schema and brute force will never fire; switch it on in Settings → Modules")" ;;
            "")
                probe "$(tr_ "метрики приложения" "application metrics")" 1 "$(tr_ "не проверить — приложение не ответило" "cannot check — the application did not answer")" ;;
            *)
                probe "$(tr_ "метрики приложения" "application metrics")" 0 "$(tr_ "приложение ответило $_mcode вместо 200 — Prometheus получает то же самое" "the application answered $_mcode instead of 200 — Prometheus gets the same")" ;;
        esac

        # Проверка сайта обязана идти по ИМЕНИ САЙТА. По внутреннему адресу она
        # зелёная и тогда, когда nginx не поднялся, а 443 никто не слушает, —
        # то есть ровно в том случае, ради которого всё затевалось.
        #
        # Строка соседствовала с образцовой пробой панели и при этом проверяла
        # НЕПУСТОТУ СТРОКИ, а не достижимость. За NAT непустая строка означала
        # вечно красную проверку и вечную ложную тревогу. Теперь спрашиваем.
        _murl=$(env_get "$DOCKER_ENV" OPENCRM_MONITOR_URL 2>/dev/null || true)
        _mhost=$(env_get "$DOCKER_ENV" OPENCRM_MONITOR_HOST 2>/dev/null || true)
        _mip=$(env_get "$DOCKER_ENV" OPENCRM_MONITOR_IP 2>/dev/null || true)
        if [ -z "$_murl" ]; then
            probe "$(tr_ "проверка сайта" "site probe")" 0 "$(tr_ "адрес не задан — проверка пойдёт изнутри сети и не увидит 443; ./opencrm.sh monitoring" "no address — the probe will run from inside the network and will not see 443; ./opencrm.sh monitoring")"
        elif [ -n "$_mhost" ] && [ -z "$_mip" ]; then
            # Полупустая пара — самая тихая из поломок: compose разбирается, стек
            # поднимается, а проверка уходит на 127.0.0.1 внутри своего же
            # контейнера, где не отвечает никто.
            probe "$(tr_ "проверка сайта" "site probe")" 0 "$(tr_ "имя $_mhost подменено без адреса — проверка уходит в никуда; ./opencrm.sh monitoring" "the name $_mhost is overridden without an address — the probe goes nowhere; ./opencrm.sh monitoring")"
        elif [ -z "$_mhost" ] && [ -n "$_mip" ]; then
            probe "$(tr_ "проверка сайта" "site probe")" 0 "$(tr_ "адрес $_mip задан без имени — подмена не действует; ./opencrm.sh monitoring" "the address $_mip is set without a name — the override does nothing; ./opencrm.sh monitoring")"
        elif [ -n "$_mhost" ] && [ "$_mhost" != "$(monitor_host "$_murl")" ]; then
            probe "$(tr_ "проверка сайта" "site probe")" 0 "$(tr_ "подменено имя $_mhost, а проверяется $_murl — подмена не действует; ./opencrm.sh monitoring" "the override is for $_mhost while the probe goes to $_murl — it does nothing; ./opencrm.sh monitoring")"
        elif [ -n "$_mhost" ]; then
            probe "$(tr_ "проверка сайта" "site probe")" 1 "$_murl → $_mip $(tr_ "(локально: nginx, TLS и приложение видны, роутер — нет)" "(locally: nginx, TLS and the app are seen, the router is not)")"
            say "$(tr_ "      ${D}отвалится проброс портов — сайт ляжет для всех, а здесь останется зелено;${R}" \
                     "      ${D}lose the port forwarding and the site goes down for everyone while this stays green;${R}")"
            say "$(tr_ "      ${D}закрывает это только наблюдатель со стороны: GET /healthz в UptimeRobot${R}" \
                     "      ${D}only an outside watcher closes that: GET /healthz in UptimeRobot${R}")"
        elif curl -fsS -k --max-time 8 -o /dev/null "$_murl/healthz" 2>/dev/null; then
            probe "$(tr_ "проверка сайта" "site probe")" 1 "$_murl"
        else
            probe "$(tr_ "проверка сайта" "site probe")" 0 "$(tr_ "$_murl отсюда не отвечает — за NAT так и бывает (hairpin), и тревога придёт при живом сайте; ./opencrm.sh monitoring" "$_murl does not answer from here — usual behind NAT (hairpin), and the alert will fire on a healthy site; ./opencrm.sh monitoring")"
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

    # shellcheck disable=SC2012  # имена копий делает сам скрипт
    _last=$(ls -1t "$(home_dir)"/data/backups/daily/db-*.sql 2>/dev/null | head -n 1)
    if [ -n "$_last" ]; then
        _stamp_last=$(basename "$_last" | sed 's/^db-//; s/\.sql$//')
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
            # Лекарство названо прямо здесь. Отказ, который сообщает только о
            # беде, заставляет искать команду на стороне — а ищут её в тот час,
            # когда сайт не обновляется и разбираться некогда. `git clean` не
            # предлагаем: он снёс бы и то, чего в репозитории нет.
            probe "$(tr_ "репозиторий" "repository")" 0 "$(tr_ "есть несохранённые правки — автообновление остановится; стереть их: git -C $REPO_DIR checkout -- ." "uncommitted changes — auto-update will stop; discard them: git -C $REPO_DIR checkout -- .")"
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
    # Без префикса `$_as_root`, и это не упрощение записи. Та переменная
    # заполняется в `cmd_doctor` и принимает три значения: пусто, `sudo -n` и —
    # когда sudo не установлен — САМО ПЕРЕВЕДЁННОЕ СЛОВО «нельзя»/«no». В позиции
    # команды последнее означает запуск программы с именем «нельзя», а `sudo -n`
    # молча падает всюду, где sudo просит пароль. Оба отказа немы (stderr
    # погашен), `_ports` остаётся пустым — и раздел печатал «80 и 443 никто не
    # слушает» как установленный факт на сайте, у которого с портами всё в
    # порядке.
    #
    # Root тут не нужен вовсе: `ss -lntH` перечисляет слушающие сокеты любому,
    # права нужны только для `-p` (кто именно слушает), а его мы не просим.
    if has ss; then
        _ports=$(ss -lntH 2>/dev/null | awk '$4 ~ /:(80|443)$/ {print "      " $4}')
    fi
    if [ -n "$_ports" ]; then
        printf '%s\n' "$_ports"
    elif ! has ss; then
        # «Не смогли спросить» и «никто не слушает» — разные ответы, и путать их
        # нельзя: второй уводит разбираться с nginx там, где просто нет `ss`.
        say "$(tr_ "      нечем посмотреть порты (нет ss)" "      no way to check the ports (ss is missing)")"
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

# Меню возвращает терминал в то состояние, в котором его застало.
#
# Причину — `compose run` без `-T` — починили выше, и это главное. Но меню тут
# пострадавшая сторона, а не виновник: любая будущая команда, которая тронет
# терминал и не приберёт за собой, снова оставит человека в сыром режиме, где
# `read` не дожидается перевода строки, а Ctrl+C не доходит как сигнал. Со
# стороны это неотличимо от зависшего скрипта, и выход остаётся один —
# переподключиться по ssh. Такой исход не должен зависеть от аккуратности
# каждой отдельной команды.
#
# Восстанавливаем ИМЕННО ТО, что было (`stty -g` → `stty "$state"`), а не
# `stty sane`: последний навязал бы свои настройки человеку, у которого они
# намеренно другие.
_TTY_STATE=""
tty_zapomnit() {
    [ -r /dev/tty ] || return 0
    # Гасим stderr ВСЕЙ группы, а не одного `stty`.
    #
    # `/dev/tty` бывает на месте и при этом не открывается: так устроен запуск
    # без управляющего терминала — cron, systemd, `docker run` без `-t`. Отказ
    # печатает сама оболочка, ДО того как `stty` запустится, и его собственный
    # `2>/dev/null` этого сообщения не касается. В выводе появлялась строка
    # «can't open /dev/tty», пугающая ровно там, где всё в порядке.
    { _TTY_STATE=$(stty -g < /dev/tty) || _TTY_STATE=""; } 2>/dev/null
}
tty_vernut() {
    [ -n "$_TTY_STATE" ] || return 0
    { stty "$_TTY_STATE" < /dev/tty || true; } 2>/dev/null
}

# --------------------------------------------------------------------------
# Живое меню (TUI)
# --------------------------------------------------------------------------
#
# **Зачем.** Прежнее меню печатало семнадцать строк и ждало номер. Работало это
# безотказно, но отвечало ровно на один вопрос — «какой пункт», — а человек
# приходит сюда с другим: «что сейчас с сайтом». Чтобы узнать это, приходилось
# выбрать пункт, дождаться вывода, прочитать, нажать Enter и вернуться. Живая
# шапка отвечает на него до того, как что-либо нажато.
#
# **Чем это НЕ является.** Не заменой прежнего меню, а надстройкой над ним.
# Номерное меню остаётся на месте и включается всюду, где живому нельзя:
#
#   - вывод не в терминал (`curl … | sh`, cron, systemd, перенаправление в файл);
#   - `OPENCRM_INPUT=stdin` — отдушина для тестов и чужой автоматизации, которые
#     скармливают меню заранее заготовленные ответы;
#   - `-y` (ASSUME_YES): молчаливый прогон вопросов не задаёт вовсе;
#   - терминал без цвета, `TERM=dumb`, узкое окно, отсутствующий `stty`.
#
# Список этих условий — не перестраховка. Пульт боевого сервера обязан работать
# в самом бедном окружении, какое бывает: аварийная консоль хостера, `ssh` из
# телефона, вывод, уехавший в файл. Живое меню там либо не нужно, либо вредно.
#
# **Главная опасность — оставленный сырой режим.** В нём `read` не ждёт перевода
# строки, а Ctrl+C не доходит как сигнал; со стороны это неотличимо от зависшего
# скрипта, и выход остаётся один — переподключиться по ssh. В скрипте уже есть
# `tty_zapomnit`/`tty_vernut` именно потому, что это однажды случилось. Здесь
# сырой режим включается НАМЕРЕННО, поэтому возврат повешен на `trap` для выхода
# и сигналов: как бы цикл ни кончился — обрывом, ошибкой, Ctrl+C, — терминал
# вернётся в то состояние, в котором его застали.

#: Живое меню разрешено. Проверки идут от дешёвых к дорогим.
tui_dostupen() {
    [ "${OPENCRM_TUI:-1}" != "0" ] || return 1
    [ "$ASSUME_YES" != "1" ] || return 1
    [ "${OPENCRM_INPUT:-tty}" != "stdin" ] || return 1
    [ -t 1 ] || return 1
    [ -r /dev/tty ] && [ -w /dev/tty ] || return 1
    [ "${TERM:-dumb}" != "dumb" ] || return 1
    # Без цвета живое меню превращается в мигающий текст без выделения выбора:
    # тот же список, только хуже прежнего. Цвет здесь — не украшение, а
    # единственный способ показать, на чём стоит курсор.
    [ -n "$B" ] || return 1
    command -v stty >/dev/null 2>&1 || return 1
    command -v od >/dev/null 2>&1 || return 1
    tui_razmer
    # Ниже двадцати строк шапка со списком не помещаются вместе, и меню начнёт
    # само себя прокручивать. Прежнее в таком окне читается лучше.
    [ "$TUI_STROK" -ge 20 ] && [ "$TUI_STOLBCOV" -ge 60 ] || return 1
    return 0
}

TUI_STROK=24
TUI_STOLBCOV=80

tui_razmer() {
    # `stty size` есть не везде, `tput` — не всегда настроен. Спрашиваем оба и
    # оставляем разумное умолчание: ошибиться в размере не страшно, а вот
    # свалиться на его определении — страшно.
    _tui_razmer=$(stty size < /dev/tty 2>/dev/null || true)
    case "$_tui_razmer" in
        *' '*)
            TUI_STROK=${_tui_razmer%% *}
            TUI_STOLBCOV=${_tui_razmer##* }
            ;;
        *)
            TUI_STROK=$(tput lines 2>/dev/null || echo 24)
            TUI_STOLBCOV=$(tput cols 2>/dev/null || echo 80)
            ;;
    esac
    case "$TUI_STROK" in ''|*[!0-9]*) TUI_STROK=24 ;; esac
    case "$TUI_STOLBCOV" in ''|*[!0-9]*) TUI_STOLBCOV=80 ;; esac
}

#: ESC отдельной переменной, а не `\033` внутри `sed`.
#:
#: POSIX sed не разбирает `\033` как escape — для него это четыре обычных знака,
#: и выражение снятия цвета не совпадало НИ РАЗУ. Управляющие последовательности
#: оставались в строке, счётчик знаков считал их за текст, и правая рамка
#: уезжала ровно на длину раскраски. Видно только глазами на живом терминале: ни
#: `sh -n`, ни набор тестов такого не ловят.
TUI_ESC=$(printf '\033')

#: Стереть строку до конца — ПЕРЕД переводом строки.
#:
#: Кадры бывают разной высоты: строка «последнее обновление» появляется, только
#: когда сводка собралась, и список пунктов съезжает на строку вниз. Стирание
#: одного хвоста экрана (`ESC[J` в конце кадра) убирает то, что НИЖЕ, а строка,
#: оставшаяся от прошлого кадра выше, продолжает висеть. На боевом терминале это
#: выглядело так: «Состояние» показано дважды, «Копии» наехали на «Доступ и
#: сеть». Поэтому стирается каждая строка в момент отрисовки.
TUI_KE=$(printf '\033[K')

#: Видимая ширина строки в ЗНАКАХ, без управляющих последовательностей.
#:
#: `wc -m` считает знаки НЕ САМ ПО СЕБЕ, а по локали: под `LANG=C` он считает
#: байты, кириллица двухбайтная, и рамка разъезжается ровно на длину русской
#: подписи. Локаль задаётся на одну команду — тем же приёмом, что в `pad`.
tui_shirina() {
    _tui_sh_golyy=$(printf '%s' "$1" | sed "s/${TUI_ESC}\[[0-9;?]*[A-Za-z]//g")
    _tui_sh_dlina=$(printf '%s' "$_tui_sh_golyy" | LC_ALL=C.UTF-8 wc -m 2>/dev/null | tr -d ' ')
    # Локали C.UTF-8 на машине нет — считаем как умеем. Кривая ширина лучше
    # пустоты: на ней встанет вся арифметика рамки.
    case "$_tui_sh_dlina" in ''|*[!0-9]*) _tui_sh_dlina=${#_tui_sh_golyy} ;; esac
    printf '%s' "$_tui_sh_dlina"
}

#: Кусок строки по ЗНАКАМ: с какого и по какой.
#:
#: Через `sed`, а не `cut -c` или `awk substr`. Замерено на тех же двух
#: системах: оба режут БАЙТЫ и оставляют половину буквы — «последнее \xd0»
#: вместо «последнее обновление».
#:
#: Локаль задана на команду по той же причине: под `LANG=C` точка в `sed` —
#: это тоже БАЙТ, и та же половина буквы возвращается через чёрный ход.
#:
#: Перевод строки в замене — обратной косой с настоящим переносом, а не `\n`:
#: последнее — расширение GNU, а в Ubuntu под именем awk живёт mawk, и такие
#: расширения молча не работают. Тот же урок записан у раскраски.
tui_srez() {
    printf '%s' "$1" | LC_ALL=C.UTF-8 sed 's/./&\
/g' | sed -n "$2,$3p" | tr -d '\n'
}

#: Обрезать строку до N ЗНАКОВ, не считая управляющих последовательностей.
#:
#: Строка длиннее рамки не «вылезает вправо», а ПЕРЕНОСИТСЯ терминалом: кадр
#: рисуется от `ESC[H` без очистки, и весь остаток съезжает на строку вниз.
#: Разъехавшаяся рамка остаётся такой до выхода из меню.
tui_obrezat() {
    _tui_ob_tekst=$1; _tui_ob_predel=$2
    [ "$_tui_ob_predel" -ge 1 ] || return 0
    if [ "$(tui_shirina "$_tui_ob_tekst")" -le "$_tui_ob_predel" ]; then
        printf '%s' "$_tui_ob_tekst"
        return 0
    fi
    if tui_ramki; then _tui_ob_znak='…'; else _tui_ob_znak='>'; fi
    _tui_ob_mesto=$(( _tui_ob_predel - 1 ))
    _tui_ob_vyhod=""
    _tui_ob_hvost=$_tui_ob_tekst
    while [ -n "$_tui_ob_hvost" ]; do
        # Управляющая последовательность идёт целиком и ширины не занимает:
        # разрезанная посередине, она вылезет на экран своими же буквами.
        case "$_tui_ob_hvost" in
            "$TUI_ESC"*)
                _tui_ob_dalshe=${_tui_ob_hvost#*[A-Za-z]}
                _tui_ob_kusok=${_tui_ob_hvost%"$_tui_ob_dalshe"}
                [ -n "$_tui_ob_kusok" ] || break
                _tui_ob_vyhod="$_tui_ob_vyhod$_tui_ob_kusok"
                _tui_ob_hvost=$_tui_ob_dalshe
                continue ;;
        esac
        _tui_ob_kusok=${_tui_ob_hvost%%"$TUI_ESC"*}
        _tui_ob_hvost=${_tui_ob_hvost#"$_tui_ob_kusok"}
        _tui_ob_dlina=$(tui_shirina "$_tui_ob_kusok")
        if [ "$_tui_ob_dlina" -le "$_tui_ob_mesto" ]; then
            _tui_ob_vyhod="$_tui_ob_vyhod$_tui_ob_kusok"
            _tui_ob_mesto=$(( _tui_ob_mesto - _tui_ob_dlina ))
            continue
        fi
        # Ноль места — и резать нечего: `sed -n "1,0p"` отдаёт ПЕРВУЮ строку,
        # то есть лишний знак сверх предела.
        if [ "$_tui_ob_mesto" -gt 0 ]; then
            _tui_ob_vyhod="$_tui_ob_vyhod$(tui_srez "$_tui_ob_kusok" 1 "$_tui_ob_mesto")"
        fi
        break
    done
    # Сброс цвета обязателен: закрывающая последовательность осталась в
    # отрезанном хвосте, и без него в цвет уйдёт весь остаток экрана.
    printf '%s%s%s' "$_tui_ob_vyhod" "$_tui_ob_znak" "$R"
}

#: Рисовать рамками или палочками. UTF-8 есть не в каждой консоли хостера, а
#: рамка, распавшаяся на вопросительные знаки, хуже честного минуса.
tui_ramki() {
    case "${LC_ALL:-${LC_CTYPE:-${LANG:-}}}" in
        *[Uu][Tt][Ff]*) return 0 ;;
        *) return 1 ;;
    esac
}

TUI_SYROY=0

tui_vklyuchit() {
    # `-icanon -echo`, а НЕ `raw`, и это не мелочь настройки.
    #
    # `raw` выключает разом всё, включая обработку ВЫВОДА (`opost`/`onlcr`).
    # Без неё `\n` опускает строку, но НЕ возвращает каретку в нулевую колонку:
    # каждая следующая строка начинается там, где кончилась прошлая, и меню
    # рассыпается лесенкой вправо через весь экран. Снято с боевого терминала —
    # 99 переводов строки, ни одного с возвратом каретки.
    #
    # Нужен здесь не сырой режим, а посимвольный ввод: `-icanon` отдаёт байты, не
    # дожидаясь Enter, и этого достаточно. Обработка вывода остаётся на месте.
    #
    # Побочная выгода: `isig` тоже остаётся, то есть Ctrl+C приходит НАСТОЯЩИМ
    # сигналом и попадает в `trap`, а не байтом, который надо узнавать вручную.
    #
    # `min 0 time 10` — ключ ко всему живому: чтение возвращается пустым через
    # секунду, даже если никто ничего не нажал. Без этого цикл висел бы на
    # клавише, и шапка обновлялась бы только по нажатию — то есть не была бы
    # живой.
    stty -icanon -echo min 0 time 10 < /dev/tty 2>/dev/null || return 1
    TUI_SYROY=1
    printf '\033[?25l' > /dev/tty   # спрятать курсор: он мигает посреди списка
    return 0
}

tui_vyklyuchit() {
    [ "$TUI_SYROY" = "1" ] || return 0
    TUI_SYROY=0
    printf '\033[?25h\033[0m' > /dev/tty   # вернуть курсор и снять цвет
    tty_vernut
}

#: Одна клавиша словом: up, down, left, right, enter, quit, цифра или пусто.
#:
#: Через `od`, а не подстановкой команды: `$(…)` срезает переводы строк, и
#: Enter в сыром режиме (одиночный CR) от него не отличить. Числовой код
#: однозначен и не зависит от локали.
#: Один байт с клавиатуры кодом или пустота, если за секунду не нажали.
tui_bayt() {
    dd bs=1 count=1 2>/dev/null < /dev/tty | od -An -tu1 2>/dev/null | tr -d ' \n'
}

tui_klavisha() {
    _tui_kod=$(tui_bayt)
    case "$_tui_kod" in
        "")   printf 'timeout'; return 0 ;;
        27)
            # Escape-последовательность стрелки: ESC [ A|B|C|D, а в режиме
            # приложения — ESC O A|B|C|D. Одиночный ESC (человек нажал его сам)
            # отличается тем, что продолжения нет — `min 0` вернёт пустоту, и мы
            # поймём это как «назад».
            _tui_k2=$(tui_bayt)
            case "$_tui_k2" in
                91|79) : ;;
                *) printf 'left'; return 0 ;;
            esac
            _tui_k3=$(tui_bayt)
            case "$_tui_k3" in
                65) printf 'up'; return 0 ;;
                66) printf 'down'; return 0 ;;
                67) printf 'right'; return 0 ;;
                68) printf 'left'; return 0 ;;
            esac
            # Хвост НЕЗНАКОМОЙ последовательности дочитываем до конца, и это не
            # педантизм. Брошенный хвост остаётся в буфере терминала и читается
            # дальше как отдельные нажатия: у Shift+стрелки (`ESC [ 1 ; 2 B`) и
            # у F5..F12 (`ESC [ 1 5 ~`) в остатке лежит ЦИФРА, а цифра в меню не
            # просто переводит подсветку — она ставит `enter` и ТУТ ЖЕ исполняет
            # пункт. Shift+Down в разделе «Копии» открывал восстановление из
            # копии; человек при этом ничего похожего не нажимал.
            #
            # Строение последовательности: параметры 0x30–0x3F, промежуточные
            # 0x20–0x2F, и конечный байт 0x40–0x7E. Читаем, пока байт меньше 64;
            # пустота (тайм-аут) цикл тоже обрывает, поэтому зависнуть на
            # оборванной последовательности нельзя.
            while [ -n "$_tui_k3" ] && [ "$_tui_k3" -lt 64 ]; do
                _tui_k3=$(tui_bayt)
            done
            printf 'timeout'
            return 0 ;;
        10|13) printf 'enter'; return 0 ;;
        113|81) printf 'quit'; return 0 ;;
        3)  printf 'quit'; return 0 ;;          # Ctrl+C в сыром режиме приходит байтом
        107) printf 'up'; return 0 ;;           # k — как в less и vim
        106) printf 'down'; return 0 ;;         # j
        104) printf 'left'; return 0 ;;         # h
        108) printf 'right'; return 0 ;;        # l
        48|49|50|51|52|53|54|55|56|57)
            # Вычитанием, а не двойным `printf` через восьмеричный код.
            #
            # Прежняя запись собирала формат из переменной, и shellcheck
            # справедливо на неё ругался (SC2059): формат, пришедший снаружи, —
            # это дыра, через которую в него попадает что угодно. Здесь коды
            # заведомо от 48 до 57, и разность с сорока восемью даёт ту же
            # цифру короче и без построения формата на лету.
            printf 'cifra:%s' "$(( _tui_kod - 48 ))"
            return 0 ;;
        *)  printf 'timeout'; return 0 ;;
    esac
}

# --- живая сводка ---------------------------------------------------------
#
# Собирается В ФОНЕ и кладётся в файл, а рисуется из файла. Иначе не выйдет:
# `compose ps`, `curl` и `autoupdate status` (это python) вместе стоят пару
# секунд, и собирай мы их в цикле отрисовки — меню замирало бы на эти секунды
# при каждом обновлении. Замирающий пульт хуже неподвижного: непонятно, он
# думает или сломался.
#
# Формат — строки `ключ=значение`. Не JSON: разбирать его в POSIX sh нечем, а
# заводить ради шапки зависимость от python значило бы, что шапка исчезнет
# ровно там, где python и сломался.

TUI_SVODKA=""
TUI_SBOR_PID=""
TUI_SVODKA_KOGDA=0

tui_svodka_sobrat() {
    _tui_u=$(env_get "$APP_ENV" OPENCRM_BASE_URL 2>/dev/null || true)
    printf 'url=%s\n' "${_tui_u:-—}"

    if curl -fsS --max-time 3 http://127.0.0.1/healthz 2>/dev/null | grep -q '"ok"'; then
        printf 'zdorov=1\n'
    else
        printf 'zdorov=0\n'
    fi
    # Режим обслуживания — отдельное состояние, а не разновидность «не
    # отвечает»: сайт закрыт НАМЕРЕННО, и путать это с аварией нельзя.
    if [ -f "$(home_dir)/data/maintenance.on" ] 2>/dev/null; then
        printf 'obsluzhivanie=1\n'
    else
        printf 'obsluzhivanie=0\n'
    fi

    _tui_vsego=$(compose ps --services 2>/dev/null | grep -c . || echo 0)
    _tui_zhivyh=$(compose ps --services --filter status=running 2>/dev/null | grep -c . || echo 0)
    printf 'konteynerov=%s\n' "${_tui_vsego:-0}"
    printf 'zhivyh=%s\n' "${_tui_zhivyh:-0}"
    # Имена лежачих — то, ради чего вообще смотрят на счётчик.
    _tui_legli=$(compose ps --services 2>/dev/null | while IFS= read -r _tui_s; do
        compose ps --services --filter status=running 2>/dev/null | grep -qx "$_tui_s" || printf '%s ' "$_tui_s"
    done)
    printf 'legli=%s\n' "$(printf '%s' "$_tui_legli" | sed 's/ *$//')"

    # Версия и обновления — у обновлятора, он единственный знает про ветку и
    # про то, чем кончился прошлый заход.
    #
    # `--cached` обязателен, и это не бережливость. Обычный `status` спрашивает
    # GitHub; шапка обновляется раз в пятнадцать секунд, то есть давала бы 240
    # обращений в час при лимите в 60 для анонимного клиента. Дальше GitHub
    # отвечает 403 — и ломается не шапка, а НАСТОЯЩЕЕ обновление, потому что
    # лимит один на весь адрес. Ровно это и случилось на боевом сервере.
    #
    # Запомненный ответ не устаревает: демон опрашивает ветку раз в пять минут и
    # кладёт голову в состояние, откуда её и берёт `--cached`.
    _tui_st=$(autoupdate status --cached 2>/dev/null || true)
    printf 'versiya=%s\n' "$(printf '%s' "$_tui_st" | sed -n 's/^развёрнуто: *//p' | head -1)"
    printf 'obnova=%s\n'  "$(printf '%s' "$_tui_st" | sed -n 's/^обновление: *//p' | head -1)"
    printf 'avto=%s\n'    "$(printf '%s' "$_tui_st" | sed -n 's/^автообновление: *//p' | head -1)"
    printf 'poslednee=%s\n' "$(printf '%s' "$_tui_st" | sed -n 's/^последнее: *//p' | head -1)"

    # `-h`, а не `-P`: в блоках по килобайту свободное место выглядит как
    # «958484660», и прочитать это глазом нельзя. Флаг не из POSIX, но соседний
    # `cmd_status` пользуется им давно, то есть на этих системах он есть.
    #
    # Каталог берём существующий: у неустановленного сайта `home_dir` указывает
    # туда, где ещё ничего нет, и `df` молчит — строка «диск» в шапке пустела.
    _tui_kuda=$(home_dir)
    [ -d "$_tui_kuda" ] || _tui_kuda="$REPO_DIR"
    _tui_disk=$(df -h "$_tui_kuda" 2>/dev/null | tail -1)
    # Сводка отдаёт ЗНАЧЕНИЕ, а не готовую фразу: слово «свободно» было вшито
    # сюда мимо `tr_`, и в английском интерфейсе шапка говорила «disk 27G
    # свободно». Язык решается при отрисовке, где `tr_` и живёт.
    printf 'disk=%s\n' "$(printf '%s' "$_tui_disk" | awk '{print $4}')"
}

#: Запустить сбор, если прошлый уже закончился. Больше одного разом — незачем:
#: они спрашивают одно и то же и мешают друг другу у докера.
tui_svodka_obnovit() {
    if [ -n "$TUI_SBOR_PID" ] && kill -0 "$TUI_SBOR_PID" 2>/dev/null; then
        return 0
    fi
    # Ошибки гасятся у ВСЕЙ подоболочки, а не у одного сбора. Иначе на выходе
    # из меню человек получает `mv: cannot stat '/tmp/tmp.XXXX.new'`: ловушка
    # сносит временные файлы, пока сборщик ещё идёт, и его `mv` ругается в
    # терминал уже после того, как меню закрылось. Снято с боевого сервера.
    ( tui_svodka_sobrat > "$TUI_SVODKA.new" \
        && mv "$TUI_SVODKA.new" "$TUI_SVODKA" ) 2>/dev/null &
    TUI_SBOR_PID=$!
    TUI_SVODKA_KOGDA=$(tui_teper)
}

tui_teper() { date +%s 2>/dev/null || echo 0; }

tui_pole() {
    [ -f "$TUI_SVODKA" ] || return 0
    sed -n "s/^$1=//p" "$TUI_SVODKA" | head -1
}

# --- отрисовка ------------------------------------------------------------

#: Кадр бегущей строки. Текст короче поля — отдаём как есть; длиннее — крутим.
#:
#: Бегущая строка тут не для красоты: адрес сайта с длинным доменом и строка
#: «последнее обновление» не помещаются в узкое окно, а обрезать их значит
#: спрятать ровно тот хвост, ради которого их читают.
tui_begushchaya() {
    _tui_tekst=$1; _tui_shirina=$2; _tui_faza=$3
    # Поле уже поля — крутить нечего, отдаём как есть: обрезанная в ноль строка
    # хуже вылезшей за край.
    [ "$_tui_shirina" -ge 4 ] || { printf '%s' "$_tui_tekst"; return 0; }
    # По ЗНАКАМ, а не по `${#…}`: последнее в POSIX sh считает байты, и русская
    # строка объявлялась бы длинной вдвое — бежала бы и та, что помещается.
    _tui_dlina=$(tui_shirina "$_tui_tekst")
    if [ "$_tui_dlina" -le "$_tui_shirina" ]; then
        printf '%s' "$_tui_tekst"
        return 0
    fi
    _tui_lenta="$_tui_tekst   ·   "
    _tui_period=$(tui_shirina "$_tui_lenta")
    _tui_sdvig=$(( _tui_faza % _tui_period ))
    tui_srez "$_tui_lenta$_tui_lenta" $(( _tui_sdvig + 1 )) $(( _tui_sdvig + _tui_shirina ))
}

TUI_VERTUSHKA_KADR=0

tui_vertushka() {
    if tui_ramki; then
        _tui_kadry='⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'
    else
        # Обратная косая НЕ последняя нарочно: перед закрывающей кавычкой её
        # читают как попытку эту кавычку экранировать (shellcheck SC1003), а
        # человек, правящий строку следом, прочитает так же. Кадры от порядка
        # не зависят — вертушка крутится по кругу.
        _tui_kadry='-\|/'
    fi
    _tui_vsego=$(tui_shirina "$_tui_kadry")
    _tui_n=$(( TUI_VERTUSHKA_KADR % _tui_vsego ))
    tui_srez "$_tui_kadry" $(( _tui_n + 1 )) $(( _tui_n + 1 ))
}

tui_liniya() {
    _tui_znak=$1; _tui_skolko=$2
    _tui_out=""
    _tui_i=0
    while [ "$_tui_i" -lt "$_tui_skolko" ]; do
        _tui_out="$_tui_out$_tui_znak"
        _tui_i=$(( _tui_i + 1 ))
    done
    printf '%s' "$_tui_out"
}

tui_shapka() {
    if tui_ramki; then _tui_g='─'; _tui_v='│'; _tui_ul='╭'; _tui_ur='╮'; _tui_dl='╰'; _tui_dr='╯'; _tui_tochka='●'
    else _tui_g='-'; _tui_v='|'; _tui_ul='+'; _tui_ur='+'; _tui_dl='+'; _tui_dr='+'; _tui_tochka='*'
    fi
    _tui_w=$(( TUI_STOLBCOV - 2 ))
    [ "$_tui_w" -gt 100 ] && _tui_w=100
    _tui_vnutri=$(( _tui_w - 2 ))

    printf '%s%s%s%s%s%s\n' "$TUI_KE" "$CYAN" "$_tui_ul" "$(tui_liniya "$_tui_g" "$_tui_w")" "$_tui_ur$R" ""

    # Строка 1: имя, состояние, вертушка сбора.
    _tui_obsl=$(tui_pole obsluzhivanie)
    _tui_zd=$(tui_pole zdorov)
    if [ "$_tui_obsl" = "1" ]; then
        _tui_sost="${YELLOW}${_tui_tochka} обслуживание${R}"
        _tui_sost_en="${YELLOW}${_tui_tochka} maintenance${R}"
    elif [ "$_tui_zd" = "1" ]; then
        _tui_sost="${GREEN}${_tui_tochka} работает${R}"; _tui_sost_en="${GREEN}${_tui_tochka} running${R}"
    elif [ -f "$TUI_SVODKA" ]; then
        _tui_sost="${RED}${_tui_tochka} не отвечает${R}"; _tui_sost_en="${RED}${_tui_tochka} not responding${R}"
    else
        _tui_sost="${D}${_tui_tochka} проверяю…${R}"; _tui_sost_en="${D}${_tui_tochka} checking…${R}"
    fi
    _tui_zanyat=""
    if [ -n "$TUI_SBOR_PID" ] && kill -0 "$TUI_SBOR_PID" 2>/dev/null; then
        _tui_zanyat=" ${D}$(tui_vertushka)${R}"
    fi
    tui_stroka_ramki "$_tui_v" "$_tui_vnutri" "${B}OpenCRM${R}  $(tr_ "$_tui_sost" "$_tui_sost_en")$_tui_zanyat"

    # Строка 2: адрес — бегущей строкой, если не влезает.
    tui_stroka_ramki "$_tui_v" "$_tui_vnutri" "${D}$(tui_begushchaya "$(tui_pole url)" $(( _tui_vnutri - 2 )) "$TUI_VERTUSHKA_KADR")${R}"

    # Строка 3: версия, обновление, автообновление.
    _tui_ver=$(tui_pole versiya)
    _tui_obn=$(tui_pole obnova)
    _tui_avto=$(tui_pole avto)
    # Значение приходит из `scripts/autoupdate.py`, а тот печатает
    # «включено»/«выключено» по-русски всегда — в английской шапке выходило
    # «auto-update включено». Переводим здесь, где `tr_` и живёт.
    case "$_tui_avto" in
        включено)  _tui_avto_slovo=$(tr_ "включено" "on") ;;
        выключено) _tui_avto_slovo=$(tr_ "выключено" "off") ;;
        *)         _tui_avto_slovo="—" ;;
    esac
    case "$_tui_obn" in
        есть) _tui_obn_cvet="${YELLOW}$(tr_ "есть обновление" "update available")${R}" ;;
        нет)  _tui_obn_cvet="${D}$(tr_ "последняя версия" "up to date")${R}" ;;
        *)    _tui_obn_cvet="${D}—${R}" ;;
    esac
    tui_stroka_ramki "$_tui_v" "$_tui_vnutri" \
        "$(tr_ "версия" "version") ${B}${_tui_ver:-—}${R}  ·  $_tui_obn_cvet  ·  $(tr_ "автообновление" "auto-update") $_tui_avto_slovo"

    # Строка 4: контейнеры и диск.
    _tui_vsego=$(tui_pole konteynerov); _tui_zhivyh=$(tui_pole zhivyh); _tui_legli=$(tui_pole legli)
    if [ -n "$_tui_legli" ]; then
        _tui_kont="${RED}${_tui_zhivyh:-0}/${_tui_vsego:-0}${R} ${D}($_tui_legli)${R}"
    elif [ -n "$_tui_vsego" ] && [ "$_tui_vsego" != "0" ]; then
        _tui_kont="${GREEN}${_tui_zhivyh}/${_tui_vsego}${R}"
    else
        _tui_kont="${D}—${R}"
    fi
    tui_stroka_ramki "$_tui_v" "$_tui_vnutri" \
        "$(tr_ "контейнеры" "containers") $_tui_kont  ·  $(tr_ "диск" "disk") ${D}$(tui_pole disk) $(tr_ "свободно" "free")${R}"

    # Строка 5: чем кончилось прошлое обновление — бегущей строкой.
    _tui_p=$(tui_pole poslednee)
    [ -n "$_tui_p" ] && tui_stroka_ramki "$_tui_v" "$_tui_vnutri" \
        "${D}$(tr_ "последнее обновление" "last update"): $(tui_begushchaya "$_tui_p" $(( _tui_vnutri - 24 )) "$TUI_VERTUSHKA_KADR")${R}"

    printf '%s%s%s%s%s\n' "$TUI_KE" "$CYAN" "$_tui_dl" "$(tui_liniya "$_tui_g" "$_tui_w")" "$_tui_dr$R"
}

#: Строка внутри рамки с выравниванием по видимой ширине.
#:
#: Считать длину напрямую нельзя: в тексте живут управляющие
#: последовательности цвета, и `${#s}` посчитал бы их знаками, а рамка уехала
#: бы вправо ровно на длину раскраски.
tui_stroka_ramki() {
    _tui_v=$1; _tui_shirina=$2; _tui_tekst=$3
    _tui_vidno=$(tui_shirina "$_tui_tekst")
    # Не влезло — режем, а не оставляем терминалу. Перенесённая им строка
    # сдвигает вниз весь остаток кадра, и рамка разъезжается: в окне 60–71
    # колонки третья строка шапки длиннее рамки просто по своему тексту.
    if [ "$_tui_vidno" -gt "$_tui_shirina" ]; then
        _tui_tekst=$(tui_obrezat "$_tui_tekst" "$_tui_shirina")
        _tui_vidno=$(tui_shirina "$_tui_tekst")
    fi
    _tui_hvost=$(( _tui_shirina - _tui_vidno ))
    [ "$_tui_hvost" -lt 0 ] && _tui_hvost=0
    printf '%s%s%s%s %s%s %s%s%s\n' "$TUI_KE" "$CYAN" "$_tui_v" "$R" "$_tui_tekst" "$(tui_liniya ' ' "$_tui_hvost")" "$CYAN" "$_tui_v" "$R"
}

# --- дерево пунктов -------------------------------------------------------
#
# Список строками `действие|подпись|пояснение`, а не разбросанным по коду
# `case`: подпись, пояснение и вызов стоят вместе, и добавить пункт — значит
# дописать одну строку, а не три в разных местах. Разъехаться им негде.
#
# Разделов шесть, и деление не выдумано: оно повторяет то, зачем сюда приходят.
# «Посмотреть» отделено от «сделать», а редкое и опасное (восстановление,
# фаервол, обслуживание) убрано с первого экрана, чтобы не жать по привычке.

tui_punkty() {
    case "$1" in
    glavnoe)
        tr_ 'razdel:sostoyanie|Состояние|Что сейчас с сайтом, контейнерами и диском
razdel:upravlenie|Управление|Запуск, перезапуск, остановка, логи
razdel:obnovlenie|Обновление|Обновить сейчас, автообновление, журнал заходов
razdel:kopii|Копии|Снять копию, восстановиться из копии
razdel:dostup|Доступ и сеть|Домен, HTTPS, фаервол, пароль администратора
razdel:nablyudenie|Наблюдение|Мониторинг, оповещения, диагностика
razdel:redkoe|Редкое|Обслуживание, починка прав после sudo
vyhod|Выход|Закрыть меню' \
'razdel:sostoyanie|Status|What is going on with the site, containers and disk
razdel:upravlenie|Control|Start, restart, stop, logs
razdel:obnovlenie|Updates|Update now, auto-update, update journal
razdel:kopii|Backups|Take a backup, restore from one
razdel:dostup|Access and network|Domain, HTTPS, firewall, admin password
razdel:nablyudenie|Observability|Monitoring, alerts, diagnostics
razdel:redkoe|Rare|Maintenance mode, ownership repair after sudo
vyhod|Exit|Close the menu' ;;
    sostoyanie)
        tr_ 'cmd_status|Состояние и здоровье|Контейнеры, ответ сайта, версия, свободное место
cmd_doctor|Диагностика|Полная проверка установки: схема, копии, метрики, сертификат
nazad|Назад|К главному списку' \
'cmd_status|Status and health|Containers, site answer, version, free space
cmd_doctor|Diagnostics|Full check: schema, backups, metrics, certificate
nazad|Back|To the main list' ;;
    upravlenie)
        tr_ 'cmd_start|Запустить|Поднять контейнеры и дождаться ответа сайта
cmd_restart|Перезапустить|Перезапустить контейнеры, не трогая данные
tui_stop|Остановить|Погасить сайт (спросит подтверждение)
cmd_logs|Логи|Живой поток всех служб; Ctrl+C — выйти
nazad|Назад|К главному списку' \
'cmd_start|Start|Bring the containers up and wait for the site
cmd_restart|Restart|Restart the containers, data untouched
tui_stop|Stop|Take the site down (asks for confirmation)
cmd_logs|Logs|Live stream of all services; Ctrl+C to leave
nazad|Back|To the main list' ;;
    obnovlenie)
        tr_ 'cmd_update|Обновить сейчас|Вытянуть свежий код, прогнать проверки, пересобрать
cmd_autoupdate|Автообновление|Включить или выключить обновление по расписанию
cmd_history|Журнал обновлений|Чем кончились прошлые заходы
nazad|Назад|К главному списку' \
'cmd_update|Update now|Pull the latest code, run the checks, rebuild
cmd_autoupdate|Auto-update|Turn scheduled updates on or off
cmd_history|Update journal|How the previous attempts ended
nazad|Back|To the main list' ;;
    kopii)
        tr_ 'cmd_backup|Снять копию|Дамп базы, архив файлов, ключ шифрования и проверка годности
cmd_restore|Восстановить из копии|Вернуть базу и файлы; спросит, из какой именно
nazad|Назад|К главному списку' \
'cmd_backup|Take a backup|Database dump, files archive, secret key and a validity check
cmd_restore|Restore from a backup|Bring back the database and files; asks which one
nazad|Back|To the main list' ;;
    dostup)
        tr_ 'cmd_domain|Домен и HTTPS|Привязать домен, выпустить или обновить сертификат
cmd_firewall|Фаервол|Закрыть лишние порты, оставить нужные
cmd_password|Пароль администратора|Сбросить пароль владельца системы
nazad|Назад|К главному списку' \
'cmd_domain|Domain and HTTPS|Attach a domain, issue or renew the certificate
cmd_firewall|Firewall|Close the extra ports, keep the needed ones
cmd_password|Admin password|Reset the owner password
nazad|Back|To the main list' ;;
    nablyudenie)
        tr_ 'cmd_monitoring|Мониторинг и оповещения|Панель, тревоги в телеграм, метрики базы и Redis
cmd_doctor|Диагностика|Полная проверка установки
nazad|Назад|К главному списку' \
'cmd_monitoring|Monitoring and alerts|Dashboard, Telegram alerts, database and Redis metrics
cmd_doctor|Diagnostics|Full check of the installation
nazad|Back|To the main list' ;;
    redkoe)
        tr_ 'menu_maintenance|Режим обслуживания|Закрыть сайт заглушкой или открыть обратно
cmd_repair|Починка прав|Вернуть владельца файлам после запуска под sudo
nazad|Назад|К главному списку' \
'menu_maintenance|Maintenance mode|Close the site behind a stub page or open it back
cmd_repair|Repair ownership|Give files back to their owner after a run under sudo
nazad|Back|To the main list' ;;
    esac
}

#: Остановка — единственное действие, которое гасит сайт, и потому спрашивает.
tui_stop() {
    if confirm "$(tr_ "    Остановить сайт?" "    Stop the site?")" n; then
        cmd_stop
    else
        info "$(tr_ "отменено" "cancelled")"
    fi
}

tui_pole_stroki() { printf '%s' "$1" | cut -d'|' -f"$2"; }

# --- главный цикл ---------------------------------------------------------

tui_narisovat() {
    _tui_razdel=$1; _tui_vybor=$2
    # `\033[H` вместо очистки: курсор в начало и дорисовка поверх. Полная
    # очистка на каждом кадре даёт мигание, заметное в ssh с задержкой.
    printf '\033[H' > /dev/tty
    {
        tui_shapka
        printf '%s\n' "$TUI_KE"
        _tui_n=0
        printf '%s\n' "$(tui_punkty "$_tui_razdel")" | while IFS= read -r _tui_stroka; do
            [ -n "$_tui_stroka" ] || continue
            _tui_n=$(( _tui_n + 1 ))
            _tui_deystvie=$(tui_pole_stroki "$_tui_stroka" 1)
            _tui_podpis=$(tui_pole_stroki "$_tui_stroka" 2)
            case "$_tui_deystvie" in
                razdel:*) _tui_znak=$(tui_ramki && printf '▸' || printf '>') ;;
                nazad)    _tui_znak=$(tui_ramki && printf '◂' || printf '<') ;;
                vyhod)    _tui_znak=$(tui_ramki && printf '×' || printf 'x') ;;
                *)        _tui_znak=' ' ;;
            esac
            if [ "$_tui_n" = "$_tui_vybor" ]; then
                printf '%s  %s%s %-40s%s\n' "$TUI_KE" "$CYAN$B" "$_tui_znak" "$_tui_podpis" "$R"
            else
                printf '%s  %s %s\n' "$TUI_KE" "$_tui_znak" "$_tui_podpis"
            fi
        done
        printf '%s\n' "$TUI_KE"
        # Пояснение к выбранному — бегущей строкой, если длинное.
        _tui_tek=$(printf '%s\n' "$(tui_punkty "$_tui_razdel")" | sed -n "${_tui_vybor}p")
        printf '%s  %s%s%s\n' "$TUI_KE" "$D" \
            "$(tui_begushchaya "$(tui_pole_stroki "$_tui_tek" 3)" $(( TUI_STOLBCOV - 6 )) "$TUI_VERTUSHKA_KADR")" "$R"
        printf '%s\n' "$TUI_KE"
        # Подсказка длиннее окна переносится и уводит кадр вниз ровно так же,
        # как строка шапки, — она просто короче и рвётся в окне поуже.
        printf '%s  %s%s%s\n' "$TUI_KE" "$D" \
            "$(tui_obrezat "$(tr_ '↑↓ выбор · → Enter открыть · ← назад · 1-9 быстрый выбор · q выход' \
                   '↑↓ move · → Enter open · ← back · 1-9 quick pick · q quit')" $(( TUI_STOLBCOV - 4 )))" "$R"
        # Дочищаем хвост экрана: прошлый кадр мог быть длиннее нынешнего.
        printf '\033[J'
    } > /dev/tty
}

#: Выполнить пункт: выйти из сырого режима, отдать терминал команде, вернуться.
#:
#: Выход из сырого режима обязателен. Команды спрашивают подтверждения через
#: `ask`, а он читает строку целиком — в сыром режиме `read` вернулся бы после
#: первого же знака, и человек отвечал бы «y» на вопрос, которого не дочитал.
tui_vypolnit() {
    _tui_deystvie=$1
    tui_vyklyuchit
    printf '\033[?25h' > /dev/tty
    clear 2>/dev/null || printf '\033[2J\033[H' > /dev/tty
    # Ошибка внутри команды не должна ронять меню: человек остался бы без пульта
    # ровно в тот момент, когда что-то пошло не так.
    #
    # Одного `||` для этого мало: `die` — это `exit`, он закрывает ТУ ЖЕ
    # оболочку. В подоболочке он становится кодом возврата, а вывод и терминал
    # у неё общие с меню — не выходит наружу только присвоенное ею.
    ( "$_tui_deystvie" ) || warn "$(tr_ "команда закончилась ошибкой" "the command ended with an error")"
    printf '\n%s' "$D"
    ask "$(tr_ "  Enter — вернуться в меню" "  Enter — back to the menu")" "" >/dev/null
    printf '%s' "$R"
    tui_vklyuchit
    clear 2>/dev/null || printf '\033[2J\033[H' > /dev/tty
    # После возврата сводка почти наверняка устарела — команда могла всё
    # поменять (запустить, остановить, обновить). Через `tui_ubrat`, а не
    # голым `rm`: сборщик мог остаться с прошлого кадра, и его `mv` вернул бы
    # только что снесённую сводку обратно — уже неверную.
    tui_ubrat
    tui_svodka_obnovit
}

#: Убрать за собой: сначала сборщик, потом его файлы.
#:
#: Порядок обязателен. Снеси файлы первыми — и живой ещё сборщик доедет до
#: `mv`, не найдёт своего `.new` и напишет об этом в терминал, из которого меню
#: уже вышло. Человек видит ругань про `/tmp/tmp.XXXX.new` вместо приглашения
#: оболочки и справедливо считает, что что-то сломал.
tui_ubrat() {
    if [ -n "$TUI_SBOR_PID" ]; then
        kill "$TUI_SBOR_PID" 2>/dev/null || true
        TUI_SBOR_PID=""
    fi
    rm -f "$TUI_SVODKA" "$TUI_SVODKA.new"
}

tui_menu() {
    TUI_SVODKA=$(mktemp 2>/dev/null) || return 1
    # Терминал возвращается КАК БЫ ЦИКЛ НИ КОНЧИЛСЯ. Сырой режим, оставленный
    # после обрыва, — это потеря управления сервером до переподключения по ssh.
    trap 'tui_vyklyuchit; tui_ubrat; exit 0' INT TERM
    trap 'tui_vyklyuchit; tui_ubrat' EXIT

    tui_vklyuchit || { tui_ubrat; return 1; }
    clear 2>/dev/null || printf '\033[2J\033[H' > /dev/tty
    tui_svodka_obnovit

    _razdel=glavnoe
    _vybor=1
    _stek=""
    while :; do
        tui_razmer
        _vsego=$(printf '%s\n' "$(tui_punkty "$_razdel")" | grep -c .)
        [ "$_vybor" -gt "$_vsego" ] && _vybor=$_vsego
        [ "$_vybor" -lt 1 ] && _vybor=1
        tui_narisovat "$_razdel" "$_vybor"

        _k=$(tui_klavisha)
        TUI_VERTUSHKA_KADR=$(( TUI_VERTUSHKA_KADR + 1 ))

        case "$_k" in
            timeout)
                # Пятнадцать секунд — не «почаще, чтобы живее». `compose ps` и
                # `autoupdate status` дёргают докер и python; раз в секунду они
                # съедали бы процессор сервера ради шапки, на которую смотрят
                # минуту в день.
                if [ $(( $(tui_teper) - TUI_SVODKA_KOGDA )) -ge 15 ]; then
                    tui_svodka_obnovit
                fi
                continue ;;
            up)    _vybor=$(( _vybor - 1 )); [ "$_vybor" -lt 1 ] && _vybor=$_vsego ;;
            down)  _vybor=$(( _vybor + 1 )); [ "$_vybor" -gt "$_vsego" ] && _vybor=1 ;;
            left)
                if [ -n "$_stek" ]; then
                    _razdel=${_stek%%:*}
                    _vybor=${_stek#*:}
                    _stek=""
                fi ;;
            cifra:*)
                _n=${_k#cifra:}
                # Явный `if`, а не `A && B || C`. Последнее читается как
                # «если-иначе», но им не является: `C` выполнится и тогда, когда
                # `A` истинно, а споткнётся `B` (shellcheck SC2015). Здесь
                # присваивание не спотыкается никогда, и разницы сегодня нет, —
                # но запись, которая ВЫГЛЯДИТ условием и им не является, однажды
                # прочитается неверно тем, кто добавит сюда третью проверку.
                if [ "$_n" -ge 1 ] && [ "$_n" -le "$_vsego" ]; then
                    _vybor=$_n
                    _k=enter
                else
                    continue
                fi ;;
            quit) break ;;
        esac

        case "$_k" in right|enter)
            _tek=$(printf '%s\n' "$(tui_punkty "$_razdel")" | sed -n "${_vybor}p")
            _deystvie=$(tui_pole_stroki "$_tek" 1)
            case "$_deystvie" in
                razdel:*)
                    _stek="$_razdel:$_vybor"
                    _razdel=${_deystvie#razdel:}
                    _vybor=1 ;;
                nazad)
                    if [ -n "$_stek" ]; then
                        _razdel=${_stek%%:*}; _vybor=${_stek#*:}; _stek=""
                    fi ;;
                vyhod) break ;;
                *) tui_vypolnit "$_deystvie" ;;
            esac ;;
        esac
    done

    tui_vyklyuchit
    tui_ubrat
    trap - INT TERM EXIT
    clear 2>/dev/null || printf '\033[2J\033[H' > /dev/tty
    return 0
}

#: Выполнить пункт номерного меню. Отдельной функцией — ради проверяемого
#: вызова.
#:
#: Голый вызов внутри `case` внутри `while` — НЕ проверяемый контекст, и под
#: `set -e` ненулевой код любой команды завершал скрипт целиком: человек
#: вылетал в приглашение оболочки без объяснения и без «Enter — вернуться в
#: меню», ровно в ту минуту, когда что-то пошло не так и пульт нужнее всего.
#: В живом меню это давно защищено (`"$_tui_deystvie" || warn ...`), в
#: номерном — не было.
menu_vypolnit() {
    case "$1" in
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
        17) menu_maintenance ;;
        0|q|Q|"") say ""; exit 0 ;;
        *)  warn "$(tr_ "нет такого пункта" "no such item")" ;;
    esac
}

menu() {
    tty_zapomnit
    # Живое меню — если терминал его выдержит. Разбор условий и почему их так
    # много — в шапке блока TUI выше. Не выдержал (пайп, cron, `-y`, узкое
    # окно, TERM=dumb) — работает номерное меню ниже, без единого отличия от
    # того, каким оно было.
    if tui_dostupen && tui_menu; then
        return 0
    fi
    while :; do
        tty_vernut
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
        # Пункт нужен ровно тому, кто в беде: сайт закрыт оборванным переездом,
        # владелец проходит по устройству режима и видит сайт рабочим, а команду
        # `./opencrm.sh maintenance off` в этот момент никто не вспоминает —
        # человек открывает меню. Поэтому здесь, а не только в справке.
        menu_item 17 "$(tr_ "Режим обслуживания: закрыть / открыть сайт" "Maintenance mode: close / open the site")"
        say ""
        menu_item 0  "$(tr_ "Выход" "Exit")"
        say ""
        _choice=$(ask "$(tr_ "  Выбор" "  Choice")" "0")
        menu_vypolnit "$_choice" \
            || warn "$(tr_ "команда закончилась ошибкой" "the command ended with an error")"
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
  ./opencrm.sh maintenance [on|off|status]
                                  режим обслуживания: закрыть сайт или открыть
                                  обратно, если нужно закрыть его руками

Флаги установки (для неинтерактивного запуска):
  --domain example.com   домен сайта; --domain "" — работать по IP без HTTPS
  --email you@example.com  логин администратора и контакт для Let's Encrypt
  --yes                    не задавать вопросов, брать значения по умолчанию

Переменные окружения:
  OPENCRM_TUI=0            прежнее номерное меню вместо живого
                           (OPENCRM_TUI=0 ./opencrm.sh)
  OPENCRM_LANG=ru|en       язык вывода скрипта
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
            # Значение проверяется ДО сдвига. `${2:-}` написано затем, чтобы
            # пережить отсутствующее значение под `set -u`, — но следом стоял
            # `shift 2`, а `shift` специальная встроенная команда: когда просят
            # сдвинуть больше, чем есть, dash печатает «shift: can't shift that
            # many» и ЗАВЕРШАЕТ неинтерактивную оболочку немедленно. До
            # заготовленной пустоты дело не доходило никогда.
            #
            # Пустой домен обязан остаться законным (`--domain ""` — работа по
            # IP), поэтому смотрим на число аргументов, а не на пустоту значения.
            --domain)
                if [ $# -lt 2 ]; then
                    die "$(tr_ "--domain без значения; работа по IP — это --domain \"\"" "--domain needs a value; for IP-only use --domain \"\"")"
                fi
                ARG_DOMAIN=$2; ARG_DOMAIN_SET=1; shift 2 ;;
            --email)
                if [ $# -lt 2 ]; then
                    die "$(tr_ "--email без значения" "--email needs a value")"
                fi
                ARG_EMAIL=$2; shift 2 ;;
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
        svodka)     cmd_svodka ;;
        tg-uborka)  cmd_tg_uborka "$@" ;;
        restore)    cmd_restore ;;
        https)      cmd_https ;;
        domain)     cmd_domain ;;
        firewall)   cmd_firewall ;;
        password)   cmd_password ;;
        doctor)     cmd_doctor ;;
        repair)     cmd_repair ;;
        monitoring) cmd_monitoring "${1:-}" ;;
        maintenance) cmd_maintenance "${1:-status}" ;;
        "")
            if installed; then menu; else cmd_install; fi
            ;;
        *) die "$(tr_ "неизвестная команда: $_command (см. ./opencrm.sh help)" "unknown command: $_command (see ./opencrm.sh help)")" ;;
    esac
}

main "$@"
