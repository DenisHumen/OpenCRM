#!/bin/sh
# Установщик почтового сервера OpenCRM: docker-mailserver в проекте compose
# `opencrm-mail` (docs/bloki/32-pochtovyy-server.md, «Установщик»).
#
#   sh ustanovit.sh check|install|remove
#
# Работает на ЦЕЛЕВОЙ машине: на этой (зовёт обновлятор или `./opencrm.sh mail`)
# или на другой (CRM заливает его по SSH). Параметры — файл ./ustanovka.env
# (КЛЮЧ=значение, 0600); скрипт удаляет его сразу после чтения: в нём пароли.
#
# Строки, начинающиеся с `##`, — протокол для CRM (шаги, итог, данные),
# остальное — сырой журнал. Провал после первой правки — откат всего, что
# сделал ЭТОТ запуск, в обратном порядке; чужое и бывшее до нас не трогается.
#
# POSIX sh (dash): `local` нет, переменные функций — с подчёркиванием.

set -u
exec 2>&1

OP=${1:-}
ENV_FILE="${PWD}/ustanovka.env"
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
PROJECT=opencrm-mail

MAIL_DIR="" MAIL_HOSTNAME="" MAIL_DOMAIN="" MAIL_IMAGE="" MAIL_CONTAINER=""
MAIL_NETWORK="" MAIL_TLS="selfsigned" MAIL_ACME_EMAIL="" MAIL_WEBROOT="" MAIL_LE_DIR=""
MAIL_FIRST="" MAIL_FIRST_PASSWORD="" MAIL_MASTER_USER="" MAIL_MASTER_PASSWORD=""
MAIL_AGENT_TOKEN="" MAIL_AGENT_SRC="" MAIL_ALLOW_DOCKER_INSTALL=0 MAIL_SUDO_PASSWORD=""
MAIL_PORTS="25 465 587 993" MAIL_DELETE_DATA=0 MAIL_SWAPFILE=/swapfile

ubrat_env() { rm -f "$ENV_FILE" "${PWD}/.parol.tmp" 2>/dev/null; }

# Оборванный SSH не убивает скрипт сразу: он падает на первой записи (SIGPIPE),
# и без этого половина установки оставалась бы стоять. Дальше журнал — в файл.
V_RABOTE=0
obryv() {
    trap '' HUP PIPE INT TERM
    ubrat_env
    exec >>"${PWD}/obryv.log" 2>&1
    echo "# $(date -u '+%Y-%m-%dT%H:%M:%SZ') interrupted by SIG$1"
    [ "$V_RABOTE" = 1 ] && otkat
    exit 130
}
trap ubrat_env EXIT
trap 'obryv HUP' HUP
trap 'obryv PIPE' PIPE
trap 'obryv INT' INT
trap 'obryv TERM' TERM

# --- протокол ---
p_step() { printf '##STEP %s\n' "$1"; }
p_ok() { printf '##OK %s %s\n' "$1" "${2:-}"; }
p_warn() { printf '##WARN %s %s\n' "$1" "$2"; }
p_fail() { printf '##FAIL %s %s %s\n' "$1" "$2" "$3"; }
p_data() { printf '##DATA %s %s\n' "$1" "$2"; }
p_end() { printf '##END %s\n' "$1"; }

# Значения читаются построчно, а не через `.`: в паролях бывают `$`, кавычки и `;`.
prochest_env() {
    [ -r "$ENV_FILE" ] || return 0
    while IFS= read -r _stroka || [ -n "$_stroka" ]; do
        _klyuch=${_stroka%%=*}
        _znachenie=${_stroka#*=}
        case $_klyuch in
            MAIL_[A-Z_]*)
                case $_klyuch in *[!A-Z_]*) continue ;; esac
                eval "$_klyuch=\$_znachenie"
                ;;
        esac
    done < "$ENV_FILE"
}

has() { command -v "$1" >/dev/null 2>&1; }

# --- права: root, sudo без пароля, sudo с паролем или ничего ---
PRIV=none
as_root() {
    case $PRIV in
        root) "$@" ;;
        sudo) sudo -n "$@" ;;
        sudopw) printf '%s\n' "$MAIL_SUDO_PASSWORD" | sudo -S -p '' "$@" ;;
        *) return 1 ;;
    esac
}

opredelit_prava() {
    if [ "$(id -u)" -eq 0 ]; then
        PRIV=root
    elif has sudo && sudo -n true >/dev/null 2>&1; then
        PRIV=sudo
    elif has sudo && [ -n "$MAIL_SUDO_PASSWORD" ] \
        && printf '%s\n' "$MAIL_SUDO_PASSWORD" | sudo -S -p '' true >/dev/null 2>&1; then
        PRIV=sudopw
    fi
}

# --- docker: напрямую (группа docker) или через права root ---
DK=plain
dk() {
    if [ "$DK" = root ]; then as_root docker "$@"; else docker "$@"; fi
}

opredelit_docker() {
    if has docker && docker info >/dev/null 2>&1; then
        DK=plain
        return 0
    fi
    if has docker && as_root docker info >/dev/null 2>&1; then
        DK=root
        return 0
    fi
    return 1
}

docker_cmd() {
    if [ "$DK" = plain ] || [ "$PRIV" = root ]; then
        echo docker
    elif [ "$PRIV" = sudo ]; then
        echo "sudo -n docker"
    else
        # sudo с паролем съел бы stdin, а агент приходит именно по нему: только группа.
        echo docker
    fi
}

compose() {
    dk compose -p "$PROJECT" -f "$MAIL_DIR/compose.yml" --env-file "$MAIL_DIR/.env" "$@"
}

# Порт кто-то слушает: /proc есть везде, `ss` — не везде.
port_zanyat() {
    _hex=$(printf '%04X' "$1")
    awk -v p="$_hex" 'FNR > 1 { n = split($2, a, ":"); if (a[n] == p && $4 == "0A") f = 1 } END { exit !f }' \
        /proc/net/tcp /proc/net/tcp6 2>/dev/null
}

port_vladelets() {
    _kto=$(dk ps --filter "publish=$1" --format '{{.Names}}' 2>/dev/null | head -n 1)
    if [ -n "$_kto" ]; then
        echo "container:$_kto"
        return 0
    fi
    if has ss; then
        _kto=$(as_root ss -Hltnp "sport = :$1" 2>/dev/null || ss -Hltnp "sport = :$1" 2>/dev/null)
        _kto=$(printf '%s' "$_kto" | sed -n 's/.*users:(("\([^"]*\)".*/\1/p' | head -n 1)
    fi
    echo "${_kto:-unknown}"
}

# TCP до host:port; третий аргумент 1 — ждать приветствия (SMTP), иначе только соединение.
tcp_proba() {
    if has python3; then
        python3 - "$1" "$2" "${3:-0}" <<'PY'
import socket, sys
try:
    s = socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=7)
    if sys.argv[3] == "1":
        s.settimeout(10)
        if not s.recv(64).startswith(b"220"):
            sys.exit(2)
    s.close()
except Exception:
    sys.exit(1)
PY
        return $?
    fi
    if has bash && has timeout; then
        # shellcheck disable=SC2016  # $0 и $1 раскрывает уже bash, а не мы
        timeout 8 bash -c 'exec 3<>"/dev/tcp/$0/$1"' "$1" "$2" 2>/dev/null
        return $?
    fi
    if has curl; then
        curl -s --connect-timeout 7 -m 9 "telnet://$1:$2" </dev/null >/dev/null 2>&1
        [ $? -ne 7 ]
        return $?
    fi
    return 1
}

skachat() {
    if has curl; then
        curl -fsS -m 8 "$1" 2>/dev/null
    elif has wget; then
        wget -qO- -T 8 "$1" 2>/dev/null
    else
        return 1
    fi
}

# --- проверки ---
OS_ID="" OS_CODENAME=""
FAILED=0
proverka_os() {
    p_step os
    if [ -r /etc/os-release ]; then
        OS_ID=$(sed -n 's/^ID=//p' /etc/os-release | tr -d '"')
        OS_CODENAME=$(sed -n 's/^UBUNTU_CODENAME=//p' /etc/os-release | tr -d '"')
        [ -n "$OS_CODENAME" ] || OS_CODENAME=$(sed -n 's/^VERSION_CODENAME=//p' /etc/os-release | tr -d '"')
        _imya=$(sed -n 's/^PRETTY_NAME=//p' /etc/os-release | tr -d '"')
    else
        _imya=$(uname -s)
    fi
    echo "system: ${_imya:-unknown}, kernel $(uname -r), arch $(uname -m)"
    case $(uname -m) in
        x86_64|aarch64|arm64) ;;
        *) p_fail os os_unsupported "architecture $(uname -m) is not supported by docker-mailserver"; FAILED=1; return ;;
    esac
    case $OS_ID in
        ubuntu|debian) p_ok os "$_imya" ;;
        *)
            if has docker; then
                p_ok os "$_imya (docker is present, automatic docker install is not needed)"
            else
                p_fail os os_unsupported "$_imya: automatic docker install supports only Debian and Ubuntu"
                FAILED=1
            fi
            ;;
    esac
}

proverka_prav() {
    p_step privileges
    opredelit_prava
    if [ "$PRIV" != none ]; then
        p_ok privileges "$PRIV as $(id -un)"
        return
    fi
    if has docker && docker info >/dev/null 2>&1; then
        p_ok privileges "docker group as $(id -un), no root"
        return
    fi
    if has sudo; then
        p_fail privileges sudo_password_needed "user $(id -un) needs sudo with a password or the docker group"
    else
        p_fail privileges no_privileges "user $(id -un) has neither root, sudo nor the docker group"
    fi
    FAILED=1
}

NEED_DOCKER=0
proverka_docker() {
    p_step docker
    if opredelit_docker; then
        p_ok docker "Docker $(dk version --format '{{.Server.Version}}' 2>/dev/null) via $(docker_cmd)"
        p_data docker_cmd "$(docker_cmd)"
        return
    fi
    if has docker; then
        if [ "$PRIV" = none ]; then
            p_fail docker no_privileges "docker is installed but $(id -un) cannot reach the daemon"
        else
            p_fail docker docker_missing "docker is installed but the daemon does not answer"
        fi
        FAILED=1
        return
    fi
    NEED_DOCKER=1
    if [ "$MAIL_ALLOW_DOCKER_INSTALL" = 1 ] && [ "$PRIV" != none ]; then
        p_warn docker "docker_missing docker is not installed: it will be installed"
    else
        p_fail docker docker_missing "docker is not installed"
        FAILED=1
    fi
}

proverka_compose() {
    p_step compose
    if [ "$NEED_DOCKER" = 1 ]; then
        p_warn compose "compose_missing compose comes with docker"
        return
    fi
    _v=$(dk compose version --short 2>/dev/null)
    if [ -n "$_v" ]; then
        p_ok compose "compose $_v"
    else
        p_fail compose compose_missing "docker compose plugin is not installed"
        FAILED=1
    fi
}

NASH_KONTEYNER=0
proverka_portov() {
    p_step ports
    _zanyaty=""
    for _p in $MAIL_PORTS; do
        if port_zanyat "$_p"; then
            _kto=$(port_vladelets "$_p")
            p_data "port_owner_$_p" "$_kto"
            if [ "$_kto" = "container:$MAIL_CONTAINER" ]; then
                NASH_KONTEYNER=1
                continue
            fi
            _zanyaty="$_zanyaty $_p($_kto)"
        fi
    done
    if [ -n "$_zanyaty" ]; then
        p_fail ports port_busy "busy:$_zanyaty"
        FAILED=1
    elif [ "$NASH_KONTEYNER" = 1 ]; then
        p_ok ports "ports are served by $MAIL_CONTAINER"
    else
        p_ok ports "free: $MAIL_PORTS"
    fi
}

# Память и swap — из /proc/meminfo: `free` печатает по-разному в разных системах.
MEMINFO=/proc/meminfo
NUZHEN_SWAP=0
proverka_resursov() {
    p_step resources
    _pamyat=$(awk '/^MemTotal:/ { print int($2 / 1024) }' "$MEMINFO" 2>/dev/null)
    _pamyat=${_pamyat:-0}
    _swap=$(awk '/^SwapTotal:/ { print int($2 / 1024) }' "$MEMINFO" 2>/dev/null)
    _swap=${_swap:-0}
    _gde=$MAIL_DIR
    while [ -n "$_gde" ] && [ ! -d "$_gde" ]; do _gde=$(dirname "$_gde"); done
    _disk=$(df -Pk "${_gde:-/}" 2>/dev/null | awk 'NR == 2 { print int($4 / 1024) }')
    _disk=${_disk:-0}
    echo "memory ${_pamyat} MB, swap ${_swap} MB, free disk ${_disk} MB at ${_gde:-/}"
    if [ "$_pamyat" -lt 900 ]; then
        p_fail resources low_memory "memory ${_pamyat} MB, at least 1 GB (900 MB usable) is needed"
        FAILED=1
    elif [ "$_disk" -lt 2048 ]; then
        p_fail resources low_disk "free disk ${_disk} MB, at least 2048 MB is needed"
        FAILED=1
    elif [ "$_pamyat" -lt 1800 ] && [ "$_swap" -lt 1024 ]; then
        # 1 ГБ без swap: Rspamd, Dovecot и `docker pull` разом будят OOM-killer.
        if [ "$PRIV" = none ]; then
            p_warn resources "low_memory memory ${_pamyat} MB and no swap, no root to add it: run the commands from the instruction, then install"
        elif [ "$_disk" -lt 4608 ]; then
            p_warn resources "low_memory memory ${_pamyat} MB and no swap, too little disk (${_disk} MB) for a 2 GB swap file"
        else
            NUZHEN_SWAP=1
            p_warn resources "low_memory memory ${_pamyat} MB and no swap: the install creates a 2 GB swap file $MAIL_SWAPFILE"
        fi
    else
        p_ok resources "memory ${_pamyat} MB, swap ${_swap} MB, disk ${_disk} MB free"
    fi
}

proverka_25() {
    p_step egress25
    for _mx in gmail-smtp-in.l.google.com mx.yandex.ru; do
        if tcp_proba "$_mx" 25 1; then
            p_ok egress25 "outbound port 25 is open ($_mx answered)"
            return
        fi
    done
    p_warn egress25 "egress25_blocked outbound port 25 is closed: mail to other servers needs a relay"
}

proverka_mta() {
    p_step mta
    _mta=""
    for _imya in postfix exim4 exim sendmail; do
        if has systemctl && systemctl is-active --quiet "$_imya" 2>/dev/null; then
            _mta="$_mta $_imya"
        fi
    done
    if [ -n "$_mta" ] && port_zanyat 25 && [ "$NASH_KONTEYNER" = 0 ]; then
        p_fail mta mta_running "a local mail server is running:$_mta"
        FAILED=1
    elif [ -n "$_mta" ]; then
        p_warn mta "mta_running local mail service is active but not on port 25:$_mta"
    else
        p_ok mta "no other mail server"
    fi
}

SUSHCHESTVUET=""
proverka_sushchestvuyushchego() {
    p_step existing
    if ! opredelit_docker; then
        p_ok existing "docker is not installed yet"
        return
    fi
    _nashli=0
    for _stroka in $(dk ps -a --format '{{.Names}}|{{.Image}}|{{.Ports}}' 2>/dev/null | tr ' ' '_'); do
        case $_stroka in
            *docker-mailserver*) ;;
            *) continue ;;
        esac
        _imya=${_stroka%%|*}
        p_data existing_container "$_imya"
        _nashli=1
        case $_stroka in
            *:25-\>*) [ -z "$SUSHCHESTVUET" ] && SUSHCHESTVUET=$_imya ;;
        esac
        [ "$_imya" = "$MAIL_CONTAINER" ] && SUSHCHESTVUET=$_imya
    done
    if [ -n "$SUSHCHESTVUET" ]; then
        if [ "$OP" = install ]; then
            p_fail existing dms_exists "mail server container $SUSHCHESTVUET already exists: connect it instead"
            FAILED=1
        else
            p_warn existing "dms_exists mail server container $SUSHCHESTVUET already exists"
        fi
    elif [ "$_nashli" = 1 ]; then
        p_ok existing "other mail containers do not serve port 25"
    else
        p_ok existing "no mail server found"
    fi
}

proverka_ip() {
    p_step public_ip
    _ip=$(skachat https://api.ipify.org || skachat https://ifconfig.me/ip)
    case $_ip in
        *[!0-9a-fA-F.:]*|"") p_warn public_ip "public address is unknown (no internet or no curl)" ;;
        *) p_data public_ip "$_ip"; p_ok public_ip "$_ip" ;;
    esac
}

PORT80_ZANYAT=0
proverka_80() {
    p_step port80
    case $MAIL_TLS in
        letsencrypt)
            if port_zanyat 80; then
                PORT80_ZANYAT=1
                p_warn port80 "port80_busy port 80 is busy ($(port_vladelets 80)): the certificate will be self-signed"
            else
                p_ok port80 "port 80 is free for Let's Encrypt"
            fi
            ;;
        webroot)
            if [ -d "$MAIL_WEBROOT" ]; then
                p_ok port80 "certificate via the web server of OpenCRM ($MAIL_WEBROOT)"
            else
                p_fail port80 tls_failed "webroot $MAIL_WEBROOT does not exist: is OpenCRM installed here?"
                FAILED=1
            fi
            ;;
        *) p_ok port80 "self-signed certificate, port 80 is not needed" ;;
    esac
}

proverki() {
    proverka_os
    proverka_prav
    proverka_docker
    proverka_compose
    proverka_portov
    proverka_resursov
    proverka_25
    proverka_mta
    proverka_sushchestvuyushchego
    proverka_ip
    proverka_80
}

# --- откат ---
SDELANO_DIR=0 SDELANO_UP=0 UFW_DOBAVLENO="" DOCKER_POSTAVLEN=0
SDELANO_SWAP=0 SDELANO_FSTAB=0 SWAPPINESS_BYLO=""
SYSCTL_FAYL=/etc/sysctl.d/99-opencrm-mail.conf

swap_ubrat() {
    as_root swapoff "$MAIL_SWAPFILE" >/dev/null 2>&1
    as_root rm -f "$MAIL_SWAPFILE"
    if [ "$SDELANO_FSTAB" = 1 ]; then
        as_root sed -i "\\|^${MAIL_SWAPFILE}[[:space:]]|d" /etc/fstab
    fi
    if [ -n "$SWAPPINESS_BYLO" ]; then
        as_root sysctl -q -w "vm.swappiness=$SWAPPINESS_BYLO" >/dev/null 2>&1
        as_root rm -f "$SYSCTL_FAYL"
    fi
    SDELANO_SWAP=0 SDELANO_FSTAB=0 SWAPPINESS_BYLO=""
}

steret_katalog() {
    # Файлы в томах пишет root контейнера: без root снести их можно только им же.
    case $MAIL_DIR in /|/*/*) ;; *) echo "refusing to delete '$MAIL_DIR'"; return 1 ;; esac
    if [ "$PRIV" != none ]; then
        as_root rm -rf "$MAIL_DIR"
    else
        dk run --rm -v "$MAIL_DIR:/m" --entrypoint sh "$MAIL_IMAGE" -c 'rm -rf /m/* /m/.[!.]* /m/..?*' >/dev/null 2>&1
        rm -rf "$MAIL_DIR"
    fi
}

otkat() {
    p_step rollback
    printf '##ROLLBACK\n'
    if [ "$SDELANO_UP" = 1 ]; then
        echo "stopping $PROJECT"
        compose down --remove-orphans || true
    fi
    for _p in $UFW_DOBAVLENO; do
        echo "ufw: removing the rule for $_p/tcp"
        as_root ufw delete allow "$_p/tcp" >/dev/null 2>&1 || true
    done
    if [ "$SDELANO_DIR" = 1 ]; then
        echo "removing $MAIL_DIR (created by this run)"
        steret_katalog || true
    fi
    if [ "$DOCKER_POSTAVLEN" = 1 ]; then
        echo "docker installed by this run stays: other software may need it"
    fi
    if [ "$SDELANO_SWAP" = 1 ]; then
        echo "removing the swap file $MAIL_SWAPFILE (created by this run)"
        swap_ubrat
    fi
    p_ok rollback "everything done by this run is undone"
    p_end rolled_back
    exit 1
}

provalit() {
    # $1 шаг, $2 код беды, $3 подробно
    p_fail "$1" "$2" "$3"
    otkat
}

# --- установка ---
shag_swap() {
    p_step swap
    if [ "$NUZHEN_SWAP" != 1 ]; then
        p_ok swap "not needed"
        return
    fi
    if [ -e "$MAIL_SWAPFILE" ]; then
        # Чужой файл не трогаем: может, его выключили нарочно.
        p_warn swap "low_memory $MAIL_SWAPFILE exists but is not on: run swapon $MAIL_SWAPFILE"
        return
    fi
    echo "creating a 2 GB swap file $MAIL_SWAPFILE"
    SDELANO_SWAP=1
    if ! as_root fallocate -l 2G "$MAIL_SWAPFILE" 2>/dev/null; then
        # fallocate умеют не все файловые системы — тогда честными нулями.
        as_root rm -f "$MAIL_SWAPFILE"
        as_root dd if=/dev/zero of="$MAIL_SWAPFILE" bs=1M count=2048 status=none
    fi
    if ! as_root chmod 600 "$MAIL_SWAPFILE" || ! as_root mkswap "$MAIL_SWAPFILE" >/dev/null \
        || ! as_root swapon "$MAIL_SWAPFILE"; then
        swap_ubrat
        p_warn swap "low_memory the swap file could not be turned on here: add swap by hand (see the instruction)"
        return
    fi
    if ! grep -qs "^${MAIL_SWAPFILE}[[:space:]]" /etc/fstab; then
        # shellcheck disable=SC2016  # $1 раскрывает внутренний sh: путь — аргументом, не текстом команды
        as_root sh -c 'printf "%s none swap sw 0 0\n" "$1" >> /etc/fstab' sh "$MAIL_SWAPFILE" && SDELANO_FSTAB=1
    fi
    SWAPPINESS_BYLO=$(cat /proc/sys/vm/swappiness 2>/dev/null)
    as_root sysctl -q -w vm.swappiness=10 >/dev/null 2>&1
    # shellcheck disable=SC2016  # $1 раскрывает внутренний sh
    as_root sh -c 'printf "vm.swappiness=10\n" > "$1"' sh "$SYSCTL_FAYL" 2>/dev/null
    p_data swap_created "$MAIL_SWAPFILE"
    p_ok swap "2 GB swap file $MAIL_SWAPFILE is on (swappiness 10)"
}

# Свежий VPS в первые минуты сам ставит обновления и держит замок apt:
# ждём его, а не падаем с «Could not get lock».
apt_get() {
    _popytka=1
    while :; do
        as_root env DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=300 "$@" && return 0
        [ "$_popytka" -ge 5 ] && return 1
        echo "apt-get $1 failed (attempt $_popytka), retrying in 20 s"
        _popytka=$((_popytka + 1))
        sleep 20
    done
}

shag_docker() {
    p_step docker_install
    if [ "$NEED_DOCKER" = 0 ]; then
        p_ok docker_install "already installed"
    else
        echo "installing docker from download.docker.com"
        as_root install -m 0755 -d /etc/apt/keyrings || provalit docker_install docker_install_failed "cannot create /etc/apt/keyrings"
        as_root sh -c "curl -fsSL 'https://download.docker.com/linux/$OS_ID/gpg' -o /etc/apt/keyrings/docker.asc && chmod a+r /etc/apt/keyrings/docker.asc" \
            || provalit docker_install docker_install_failed "cannot download the docker repository key"
        printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/%s %s stable\n' \
            "$(dpkg --print-architecture)" "$OS_ID" "$OS_CODENAME" > "${PWD}/docker.list"
        as_root cp "${PWD}/docker.list" /etc/apt/sources.list.d/docker.list
        rm -f "${PWD}/docker.list"
        apt_get update -qq || provalit docker_install docker_install_failed "apt-get update failed"
        apt_get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin \
            || provalit docker_install docker_install_failed "apt-get install docker-ce failed"
        DOCKER_POSTAVLEN=1
        if [ -d /run/systemd/system ]; then
            as_root systemctl enable --now docker >/dev/null 2>&1 || true
        else
            as_root service docker start >/dev/null 2>&1 || true
        fi
        _zhdat=20
        while [ "$_zhdat" -gt 0 ] && ! opredelit_docker; do sleep 1; _zhdat=$((_zhdat - 1)); done
        opredelit_docker || provalit docker_install docker_install_failed "docker daemon does not answer after install"
        p_ok docker_install "Docker $(dk version --format '{{.Server.Version}}' 2>/dev/null)"
    fi
    # Агент по SSH приходит через stdin `docker exec -i`: sudo с паролем его съел бы.
    if [ "$DK" = root ] && [ "$PRIV" != root ]; then
        if id -nG "$(id -un)" | tr ' ' '\n' | grep -qx docker; then
            :
        elif as_root usermod -aG docker "$(id -un)"; then
            echo "user $(id -un) is added to the docker group (works from the next login)"
        fi
    fi
    p_data docker_cmd "$(docker_cmd)"
}

shag_obraz() {
    p_step image
    if dk image inspect "$MAIL_IMAGE" >/dev/null 2>&1; then
        p_ok image "$MAIL_IMAGE is already here"
        return
    fi
    echo "pulling $MAIL_IMAGE (about 300 MB)"
    _popytka=1
    while [ "$_popytka" -le 3 ]; do
        if dk pull -q "$MAIL_IMAGE"; then
            p_ok image "$MAIL_IMAGE"
            return
        fi
        _popytka=$((_popytka + 1))
        sleep 5
    done
    provalit image image_pull_failed "docker pull $MAIL_IMAGE failed"
}

shag_katalogi() {
    p_step dirs
    case $MAIL_DIR in /*/*) ;; *) provalit dirs start_failed "MAIL_DIR must be an absolute path: '$MAIL_DIR'" ;; esac
    if [ ! -e "$MAIL_DIR" ]; then
        SDELANO_DIR=1
    elif [ -n "$(ls -A "$MAIL_DIR" 2>/dev/null)" ] && [ -f "$MAIL_DIR/compose.yml" ]; then
        provalit dirs dms_exists "$MAIL_DIR already holds a mail server (compose.yml): connect it instead"
    fi
    if ! mkdir -p "$MAIL_DIR" 2>/dev/null; then
        as_root mkdir -p "$MAIL_DIR" || provalit dirs start_failed "cannot create $MAIL_DIR"
        as_root chown "$(id -u):$(id -g)" "$MAIL_DIR" || provalit dirs start_failed "cannot own $MAIL_DIR"
    fi
    chmod 700 "$MAIL_DIR" 2>/dev/null || true
    for _d in config config/opencrm data state logs tls; do
        mkdir -p "$MAIL_DIR/$_d" || provalit dirs start_failed "cannot create $MAIL_DIR/$_d"
    done
    p_ok dirs "$MAIL_DIR"
}

CERT_DIR="" CERT_MOUNT="" CERT_PATH="" KEY_PATH=""
samopodpisannyy() {
    if [ ! -s "$MAIL_DIR/tls/fullchain.pem" ]; then
        dk run --rm --entrypoint openssl -v "$MAIL_DIR/tls:/out" "$MAIL_IMAGE" \
            req -x509 -newkey rsa:2048 -nodes -days 3650 -subj "/CN=$MAIL_HOSTNAME" \
            -addext "subjectAltName=DNS:$MAIL_HOSTNAME" \
            -keyout /out/privkey.pem -out /out/fullchain.pem \
            || provalit tls tls_failed "openssl could not make a self-signed certificate"
    fi
    _pin=$(dk run --rm --entrypoint openssl -v "$MAIL_DIR/tls:/out:ro" "$MAIL_IMAGE" \
        x509 -in /out/fullchain.pem -noout -fingerprint -sha256 | sed 's/.*=//; s/://g' | tr 'A-F' 'a-f')
    [ -n "$_pin" ] || provalit tls tls_failed "cannot read the certificate fingerprint"
    p_data tls_pin "$_pin"
}

# Let's Encrypt напрямую (сервер по SSH): certbot сам поднимает порт 80 на минуту,
# а сертификат копируется в tls/ — туда же потом кладёт продление (compose.yml, `#@le`).
le_vypustit() {
    [ "$PORT80_ZANYAT" = 1 ] && return 1
    if [ -n "$MAIL_ACME_EMAIL" ]; then
        set -- --email "$MAIL_ACME_EMAIL"
    else
        set -- --register-unsafely-without-email
    fi
    dk run --rm -p 80:80 -v "$MAIL_LE_DIR:/etc/letsencrypt" \
        certbot/certbot certonly --standalone -d "$MAIL_HOSTNAME" \
        --agree-tos -n --keep-until-expiring "$@" || return 1
    # shellcheck disable=SC2016  # $1 раскрывает sh в контейнере certbot
    dk run --rm -v "$MAIL_LE_DIR:/etc/letsencrypt:ro" -v "$MAIL_DIR/tls:/tls" --entrypoint sh certbot/certbot \
        -c 'cp -L "/etc/letsencrypt/live/$1/fullchain.pem" "/etc/letsencrypt/live/$1/privkey.pem" /tls/' sh "$MAIL_HOSTNAME"
}

shag_tls() {
    p_step tls
    case $MAIL_TLS in
        selfsigned)
            CERT_DIR="$MAIL_DIR/tls" CERT_MOUNT=/tls
            CERT_PATH=/tls/fullchain.pem KEY_PATH=/tls/privkey.pem
            samopodpisannyy
            p_ok tls "self-signed for $MAIL_HOSTNAME, sha256 $_pin"
            ;;
        letsencrypt)
            CERT_DIR="$MAIL_DIR/tls" CERT_MOUNT=/tls
            CERT_PATH=/tls/fullchain.pem KEY_PATH=/tls/privkey.pem
            [ -n "$MAIL_LE_DIR" ] || MAIL_LE_DIR="$MAIL_DIR/letsencrypt"
            mkdir -p "$MAIL_LE_DIR" 2>/dev/null || as_root mkdir -p "$MAIL_LE_DIR" || true
            if le_vypustit; then
                p_data tls_mode letsencrypt
                p_ok tls "Let's Encrypt certificate for $MAIL_HOSTNAME"
                return
            fi
            # Не выпустился (A-запись ещё не разошлась, порт 80 закрыт) — не откат:
            # почта работает с самоподписанным, а настоящий выпустит служба certbot сама.
            rm -f "$MAIL_DIR/tls/fullchain.pem" "$MAIL_DIR/tls/privkey.pem" 2>/dev/null
            samopodpisannyy
            p_data tls_mode selfsigned
            p_warn tls "tls_selfsigned Let's Encrypt did not issue a certificate for $MAIL_HOSTNAME: self-signed for now, a real one comes by itself once DNS points here"
            ;;
        webroot)
            [ -n "$MAIL_LE_DIR" ] || MAIL_LE_DIR="$MAIL_DIR/letsencrypt"
            CERT_DIR="$MAIL_LE_DIR" CERT_MOUNT=/etc/letsencrypt
            CERT_PATH="/etc/letsencrypt/live/$MAIL_HOSTNAME/fullchain.pem"
            KEY_PATH="/etc/letsencrypt/live/$MAIL_HOSTNAME/privkey.pem"
            mkdir -p "$MAIL_LE_DIR" 2>/dev/null || as_root mkdir -p "$MAIL_LE_DIR" || true
            if dk run --rm --entrypoint sh -v "$MAIL_LE_DIR:/etc/letsencrypt:ro" "$MAIL_IMAGE" \
                -c "test -s $CERT_PATH" >/dev/null 2>&1; then
                p_ok tls "certificate for $MAIL_HOSTNAME already exists"
                return
            fi
            if [ -n "$MAIL_ACME_EMAIL" ]; then
                set -- --email "$MAIL_ACME_EMAIL"
            else
                set -- --register-unsafely-without-email
            fi
            dk run --rm -v "$MAIL_LE_DIR:/etc/letsencrypt" -v "$MAIL_WEBROOT:/var/www/certbot" \
                certbot/certbot certonly --webroot -w /var/www/certbot -d "$MAIL_HOSTNAME" \
                --agree-tos -n --keep-until-expiring "$@" \
                || provalit tls tls_failed "Let's Encrypt refused $MAIL_HOSTNAME (webroot)"
            p_ok tls "Let's Encrypt certificate for $MAIL_HOSTNAME"
            ;;
        *) provalit tls tls_failed "unknown MAIL_TLS '$MAIL_TLS'" ;;
    esac
}

# Хэш пароля для файлов DMS. Пароль — файлом, а не аргументом: argv видно в `ps`.
khesh() {
    ( umask 077; printf '%s' "$1" > "${PWD}/.parol.tmp" )
    dk run --rm --entrypoint sh -v "${PWD}/.parol.tmp:/p:ro" "$MAIL_IMAGE" \
        -c 'openssl passwd -6 -stdin < /p' 2>/dev/null
    rm -f "${PWD}/.parol.tmp"
}

shag_konfig() {
    p_step config
    _shablon="$SCRIPT_DIR/compose.yml"
    [ -r "$_shablon" ] || provalit config start_failed "compose template $_shablon is missing"
    # Строки шаблона с меткой `#@agent ` / `#@net ` / `#@le ` включаются по надобности,
    # `#@ports` раскрывается списком портов.
    _vkl=""
    [ -n "$MAIL_AGENT_SRC" ] && _vkl="$_vkl agent"
    [ -n "$MAIL_NETWORK" ] && _vkl="$_vkl net"
    [ "$MAIL_TLS" = letsencrypt ] && _vkl="$_vkl le"
    _filtr=""
    for _metka in agent net le; do
        case " $_vkl " in
            *" $_metka "*) _filtr="$_filtr -e s/^#@$_metka.//" ;;
            *) _filtr="$_filtr -e /^#@$_metka./d" ;;
        esac
    done
    # shellcheck disable=SC2086  # список выражений sed должен разбиться на слова
    awk -v porty="$MAIL_PORTS" '/^#@ports$/ { n = split(porty, a, " "); for (i = 1; i <= n; i++) printf "      - \"%s:%s\"\n", a[i], a[i]; next } { print }' "$_shablon" \
        | sed $_filtr > "$MAIL_DIR/compose.yml" \
        || provalit config start_failed "cannot write $MAIL_DIR/compose.yml"
    {
        echo "MAIL_IMAGE=$MAIL_IMAGE"
        echo "MAIL_CONTAINER=$MAIL_CONTAINER"
        echo "MAIL_HOSTNAME=$MAIL_HOSTNAME"
        echo "MAIL_DIR=$MAIL_DIR"
        echo "MAIL_NETWORK=$MAIL_NETWORK"
        echo "MAIL_CERT_DIR=$CERT_DIR"
        echo "MAIL_CERT_MOUNT=$CERT_MOUNT"
        echo "MAIL_CERT_PATH=$CERT_PATH"
        echo "MAIL_KEY_PATH=$KEY_PATH"
        echo "MAIL_AGENT_SRC=$MAIL_AGENT_SRC"
        echo "MAIL_AGENT_CONF=$(dirname "${MAIL_AGENT_SRC:-/x}")/opencrm-agent.conf"
        echo "MAIL_LE_DIR=$MAIL_LE_DIR"
        if [ -n "$MAIL_ACME_EMAIL" ]; then
            echo "MAIL_ACME_ARGS=--email $MAIL_ACME_EMAIL"
        else
            echo "MAIL_ACME_ARGS=--register-unsafely-without-email"
        fi
    } > "$MAIL_DIR/.env"
    chmod 600 "$MAIL_DIR/.env" "$MAIL_DIR/compose.yml"
    if [ -n "$MAIL_AGENT_TOKEN" ]; then
        ( umask 077; printf '%s\n' "$MAIL_AGENT_TOKEN" > "$MAIL_DIR/config/opencrm/agent.token" )
    fi
    compose config -q || provalit config start_failed "compose.yml does not validate"
    p_ok config "$MAIL_DIR/compose.yml"
}

shag_yashchiki() {
    p_step accounts
    case $MAIL_FIRST in
        *@*) ;;
        *) provalit accounts start_failed "first mailbox address is missing" ;;
    esac
    _h=$(khesh "$MAIL_FIRST_PASSWORD")
    case $_h in \$6\$*) ;; *) provalit accounts start_failed "cannot hash the first mailbox password" ;; esac
    _f="$MAIL_DIR/config/postfix-accounts.cf"
    if ! awk -F'|' -v a="$MAIL_FIRST" '$1 == a { f = 1 } END { exit !f }' "$_f" 2>/dev/null; then
        printf '%s|{SHA512-CRYPT}%s\n' "$MAIL_FIRST" "$_h" >> "$_f"
    fi
    if [ -n "$MAIL_MASTER_USER" ]; then
        _h=$(khesh "$MAIL_MASTER_PASSWORD")
        case $_h in \$6\$*) ;; *) provalit accounts start_failed "cannot hash the master password" ;; esac
        _f="$MAIL_DIR/config/dovecot-masters.cf"
        awk -F'|' -v u="$MAIL_MASTER_USER" '$1 != u' "$_f" 2>/dev/null > "$_f.tmp"
        printf '%s|{SHA512-CRYPT}%s\n' "$MAIL_MASTER_USER" "$_h" >> "$_f.tmp"
        mv "$_f.tmp" "$_f"
    fi
    chmod 600 "$MAIL_DIR/config/postfix-accounts.cf" "$MAIL_DIR/config/dovecot-masters.cf" 2>/dev/null || true
    p_ok accounts "first mailbox $MAIL_FIRST"
}

shag_start() {
    p_step start
    if [ -n "$MAIL_NETWORK" ] && ! dk network inspect "$MAIL_NETWORK" >/dev/null 2>&1; then
        provalit start start_failed "docker network $MAIL_NETWORK does not exist: is OpenCRM running?"
    fi
    SDELANO_UP=1
    _vyvod=$(compose up -d 2>&1)
    _kod=$?
    printf '%s\n' "$_vyvod"
    if [ "$_kod" -ne 0 ]; then
        case $_vyvod in
            *"address already in use"*|*"port is already allocated"*)
                provalit start port_busy "a mail port became busy while starting" ;;
            *) provalit start start_failed "docker compose up failed" ;;
        esac
    fi
    p_ok start "$MAIL_CONTAINER started"
}

shag_zdorov() {
    p_step healthy
    _zhdat=240
    _sost=""
    while [ "$_zhdat" -gt 0 ]; do
        _sost=$(dk inspect -f '{{.State.Health.Status}}' "$MAIL_CONTAINER" 2>/dev/null)
        [ "$_sost" = healthy ] && break
        _beg=$(dk inspect -f '{{.State.Running}}' "$MAIL_CONTAINER" 2>/dev/null)
        [ "$_beg" = false ] && break
        sleep 3
        _zhdat=$((_zhdat - 3))
    done
    if [ "$_sost" != healthy ]; then
        echo "--- last log lines of $MAIL_CONTAINER ---"
        dk logs --tail 60 "$MAIL_CONTAINER" 2>&1 | tail -n 60
        provalit healthy unhealthy "container state: ${_sost:-unknown}"
    fi
    p_ok healthy "$MAIL_CONTAINER is healthy"
}

shag_dkim() {
    p_step dkim
    _fayl="$MAIL_DIR/config/rspamd/dkim/rsa-2048-mail-$MAIL_DOMAIN.public.dns.txt"
    if ! dk exec "$MAIL_CONTAINER" test -s "/tmp/docker-mailserver/rspamd/dkim/rsa-2048-mail-$MAIL_DOMAIN.public.dns.txt"; then
        dk exec "$MAIL_CONTAINER" setup config dkim keysize 2048 domain "$MAIL_DOMAIN" | tail -n 5
    fi
    _txt=$(dk exec "$MAIL_CONTAINER" cat "/tmp/docker-mailserver/rspamd/dkim/rsa-2048-mail-$MAIL_DOMAIN.public.dns.txt" 2>/dev/null \
        | tr -d '\r\n"()' | sed 's/.*\(v=DKIM1\)/\1/; s/[[:space:]]\{1,\}/ /g; s/[; ]*$//')
    case $_txt in
        v=DKIM1*)
            p_data dkim_selector mail
            p_data dkim_txt "$_txt"
            p_ok dkim "selector mail for $MAIL_DOMAIN"
            ;;
        *) p_warn dkim "dkim_failed DKIM key for $MAIL_DOMAIN was not created ($_fayl): retry from the server card" ;;
    esac
}

shag_firewall() {
    p_step firewall
    if ! has ufw || [ "$PRIV" = none ]; then
        p_ok firewall "ufw is not managed here: docker publishes mail ports itself"
        return
    fi
    if ! as_root ufw status 2>/dev/null | grep -q '^Status: active'; then
        p_ok firewall "ufw is not active"
        return
    fi
    _pravila=$(as_root ufw status 2>/dev/null)
    for _p in $MAIL_PORTS; do
        if printf '%s\n' "$_pravila" | grep -Eq "^$_p(/tcp)?[[:space:]]+ALLOW"; then
            continue
        fi
        if as_root ufw allow "$_p/tcp" >/dev/null 2>&1; then
            UFW_DOBAVLENO="$UFW_DOBAVLENO $_p"
        else
            p_warn firewall "firewall_failed ufw allow $_p/tcp failed"
            return
        fi
    done
    printf '%s\n' "$UFW_DOBAVLENO" > "$MAIL_DIR/.ufw"
    p_ok firewall "ufw: opened${UFW_DOBAVLENO:- nothing new}"
}

shag_samoproverka() {
    p_step selfcheck
    _plokho=""
    for _p in $MAIL_PORTS; do
        _banner=0
        [ "$_p" = 25 ] && _banner=1
        tcp_proba 127.0.0.1 "$_p" "$_banner" || _plokho="$_plokho $_p"
    done
    if [ -n "$_plokho" ] && ! has python3 && ! has bash && ! has curl; then
        p_warn selfcheck "no tool to probe ports from the host"
        _plokho=""
    fi
    [ -z "$_plokho" ] || provalit selfcheck unhealthy "ports do not answer on this host:$_plokho"
    if [ -n "$MAIL_AGENT_SRC" ]; then
        _otvet=""
        _zhdat=20
        while [ "$_zhdat" -gt 0 ]; do
            _otvet=$(dk exec "$MAIL_CONTAINER" python3 -c '
import json, urllib.request
t = open("/tmp/docker-mailserver/opencrm/agent.token").read().strip()
r = urllib.request.Request("http://127.0.0.1:8790/v1/ping", data=b"{}", headers={"Authorization": "Bearer " + t})
print(json.loads(urllib.request.urlopen(r, timeout=5).read())["ok"])' 2>/dev/null)
            [ "$_otvet" = True ] && break
            sleep 2
            _zhdat=$((_zhdat - 2))
        done
        [ "$_otvet" = True ] || provalit selfcheck unhealthy "OpenCRM agent inside $MAIL_CONTAINER does not answer on 8790"
    fi
    p_ok selfcheck "ports answer:$MAIL_PORTS"
}

ustanovit() {
    proverki
    if [ "$FAILED" = 1 ]; then
        p_end failed
        exit 1
    fi
    V_RABOTE=1
    shag_swap
    shag_docker
    shag_obraz
    shag_katalogi
    shag_tls
    shag_konfig
    shag_yashchiki
    shag_start
    shag_zdorov
    shag_dkim
    shag_firewall
    shag_samoproverka
    p_end ok
}

udalit() {
    p_step privileges
    opredelit_prava
    p_ok privileges "$PRIV as $(id -un)"
    p_step docker
    opredelit_docker || { p_fail docker docker_missing "docker does not answer"; p_end failed; exit 1; }
    p_ok docker "via $(docker_cmd)"
    p_step stop
    if [ -f "$MAIL_DIR/compose.yml" ]; then
        compose down --remove-orphans || { p_fail stop start_failed "docker compose down failed"; p_end failed; exit 1; }
    elif dk inspect "$MAIL_CONTAINER" >/dev/null 2>&1; then
        _proekt=$(dk inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$MAIL_CONTAINER" 2>/dev/null)
        if [ "$_proekt" = "$PROJECT" ]; then
            dk rm -f "$MAIL_CONTAINER"
        else
            p_fail stop dms_exists "container $MAIL_CONTAINER belongs to project '$_proekt', not $PROJECT: not touching it"
            p_end failed
            exit 1
        fi
    fi
    p_ok stop "$PROJECT is stopped"
    p_step firewall
    if [ -f "$MAIL_DIR/.ufw" ] && [ "$PRIV" != none ]; then
        for _p in $(tr '\n' ' ' < "$MAIL_DIR/.ufw"); do
            as_root ufw delete allow "$_p/tcp" >/dev/null 2>&1 || true
        done
        p_ok firewall "ufw rules of the mail server are removed"
    else
        p_ok firewall "nothing to remove"
    fi
    p_step data
    if [ "$MAIL_DELETE_DATA" = 1 ] && [ -d "$MAIL_DIR" ]; then
        steret_katalog || { p_fail data start_failed "cannot delete $MAIL_DIR"; p_end failed; exit 1; }
        p_ok data "$MAIL_DIR is deleted with all mail"
    else
        p_ok data "mail is kept in $MAIL_DIR"
    fi
    p_end ok
}

prochest_env
# Пароли прочитаны — файл больше не нужен: оборви кто-нибудь установку, он не останется.
ubrat_env
[ -n "$MAIL_IMAGE" ] || MAIL_IMAGE=ghcr.io/docker-mailserver/docker-mailserver:16.0.1
[ -n "$MAIL_CONTAINER" ] || MAIL_CONTAINER=opencrm-mail
[ -n "$MAIL_DIR" ] || MAIL_DIR="${HOME:-/root}/opencrm-mail"
case $MAIL_CONTAINER in *[!a-zA-Z0-9_.-]*) echo "bad MAIL_CONTAINER"; p_end failed; exit 2 ;; esac
case $MAIL_HOSTNAME in *[!a-z0-9.-]*) echo "bad MAIL_HOSTNAME"; p_end failed; exit 2 ;; esac
case $MAIL_DOMAIN in *[!a-z0-9.-]*) echo "bad MAIL_DOMAIN"; p_end failed; exit 2 ;; esac
case $MAIL_PORTS in *[!0-9\ ]*) echo "bad MAIL_PORTS"; p_end failed; exit 2 ;; esac
case $MAIL_IMAGE in *[!a-zA-Z0-9./:_@-]*) echo "bad MAIL_IMAGE"; p_end failed; exit 2 ;; esac
case $MAIL_ACME_EMAIL in *[!a-zA-Z0-9.@_+-]*) echo "bad MAIL_ACME_EMAIL"; p_end failed; exit 2 ;; esac
case $MAIL_SWAPFILE in /[a-zA-Z0-9]*) ;; *) echo "bad MAIL_SWAPFILE"; p_end failed; exit 2 ;; esac
case $MAIL_SWAPFILE in *[!a-zA-Z0-9/._-]*|*..*) echo "bad MAIL_SWAPFILE"; p_end failed; exit 2 ;; esac

case $OP in
    check)
        proverki
        if [ "$FAILED" = 1 ]; then p_end failed; exit 1; fi
        p_end ok
        ;;
    install) ustanovit ;;
    remove) udalit ;;
    *)
        echo "usage: sh ustanovit.sh check|install|remove (parameters in ./ustanovka.env)"
        exit 2
        ;;
esac
