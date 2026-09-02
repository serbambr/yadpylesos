#!/bin/bash
# shellcheck disable=SC2001,SC2015,SC2004  # осознанные идиомы: sed читабельнее ${//}; A&&B||C на guard-ветках безопасен (присваивание не падает); $ в арифметике стилистика
# ==========================================
# Проект: Я.Д-Пылесос / YA.D-Pylesos
# Скрипт: yadpylesos.sh
# Версия: 14.0
# ==========================================

set -euo pipefail
cd "$(dirname "$0")" || exit 1

# Безопасное расширение PATH для cron
export PATH=$PATH:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
NC='\033[0m'

# Функция логирования для Bash (добавляет время и цвет, как в Python)
bash_log() {
    local color=$1
    local msg=$2
    local ts
    # Получаем время из Docker-образа, используя TZ из .env. Если Docker недоступен - fallback на date
    ts=$($DOCKER_CMD run --rm -e TZ="$TZ" "$DOCKER_IMG" python3 -c "from datetime import datetime; print(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))" 2>/dev/null || date '+%Y-%m-%d %H:%M:%S')
    echo -e "${color}[${ts}] [INFO]${NC} ${msg}"
}

if docker info > /dev/null 2>&1; then
    DOCKER_CMD="docker"
elif sudo docker info > /dev/null 2>&1; then
    DOCKER_CMD="sudo docker"
else
    echo -e "${RED}[ОШИБКА]${NC} Docker не установлен или нет прав доступа!"
    exit 1
fi

# Дефолты критичных переменных (F-85): отсутствие их в .env не должно убивать скрипт по set -u
DOCKER_IMG="${DOCKER_IMG:-yadpylesos-slim}"
MAX_RUNTIME="${MAX_RUNTIME:-21600}"

# Раскручиваем симлинк, чтобы найти реальный путь скрипта
SCRIPT_RESOLVED="$(readlink -f "$0" 2>/dev/null || echo "$0")"
BASE_DIR="$(cd "$(dirname "$SCRIPT_RESOLVED")" && pwd)"
SCRIPT_PATH="$BASE_DIR/yadpylesos.py"
PID_FILE="$BASE_DIR/.pid"
LOCK_FILE="$BASE_DIR/.lock"

REPORT_DIR="$BASE_DIR/report"
DB_DIR="$BASE_DIR/db"
AUTH_DIR="$BASE_DIR/auth"
SOURCES_TXT="$BASE_DIR/config/source_links.txt"
# BL-Env: Поддержка файла .env
ENV_FILE="$BASE_DIR/.env"
ENV_ARGS=()
if [ -f "$ENV_FILE" ]; then
    # Читаем .env локально для bash (для LOG_OPTS и MAX_RUNTIME)
    set -a
    # shellcheck disable=SC1090  # динамический source пользовательского .env — замысел
    . "$ENV_FILE"
    set +a
    # Явно экспортируем часовой пояс для команды date в Bash
    export TZ="${TZ:-Europe/Moscow}"
    # Формируем аргументы для проброса в контейнер
    ENV_ARGS=(--env-file "$ENV_FILE")
fi

# Лимиты логов Docker (используют переменные из .env или дефолтные значения)
LOG_MAX_SIZE="${LOG_MAX_SIZE:-10m}"
LOG_MAX_FILES="${LOG_MAX_FILES:-3}"
LOG_OPTS="--log-opt max-size=${LOG_MAX_SIZE} --log-opt max-file=${LOG_MAX_FILES}"

show_help() {
    echo -e "${CYAN}=============================================${NC}"
    echo -e " Я.Д-Пылесос ${GREEN}v14.0${NC}"
    echo -e " Использование: ./yadpylesos '<Имя_контейнера>' '<Ссылка_на_источник>' '<Папка_назначения>' [Опции]"
    echo -e " Или для пакетного режима: ./yadpylesos --batch"
    echo -e "${CYAN}=============================================${NC}"
    echo " АРГУМЕНТЫ:"
    echo "  <Имя_контейнера>      Уникальное имя для процесса."
    echo "  <Ссылка_на_источник>  Ссылка на источник (Яндекс.Диск / YouTube)."
    echo "  <Папка_назначения>    Локальный путь для сохранения файлов (напр. /path/to/download)."
    echo "  [Кол-во_файлов]       Ожидаемое количество файлов (для прогресс-бара)."
    echo -e "${CYAN}=============================================${NC}"
    echo "  --batch               Интерактивный менеджер ссылок."
    echo "  --batch-auto          Автоматический пакетный режим для cron."
    echo "  --refresh-cache       Принудительно обновить кэш дерева."
    echo "  --build-queue         Создать список для скачивания без самого скачивания."
    echo -e "${CYAN}=============================================${NC}"
    echo "  --threads=N           Потоки для одного файла (1-8)."
    echo "  --quantity-files=N    Параллельное скачивание файлов (1-8)."
    echo "  --move-extra='/путь/' Перенос осиротевших файлов в карантин."
    echo "  --force               Принудительное скачивание (игнорировать архив)."
    echo "  --md5='имя_файла'     Проверить MD5 конкретного файла."
    echo -e "${CYAN}=============================================${NC}"
    echo "  --vpn                 Принудительно запускать VPN при старте."
    echo "  --vpn-test            Проверить работоспособность VPN (поднять туннель и узнать IP)."
    echo "  --auth-enable         Включить глобальную авторизацию (OAuth 2.0)."
    echo "  --auth-disable        Отключить глобальную авторизацию."
    echo "  --auth-status         Проверить статус авторизации (токен, куки)."
    echo "  --ssl-off             Отключить проверку SSL-сертификатов."
    echo "  --homeostasis-off     Отключить гомеостаз (авто-снижение потоков)."
    echo "  --simulate-ban        Симулировать бан API (для теста VPN)."
    echo -e "${CYAN}=============================================${NC}"
    echo "  --db-stats            Статистика всех БД (включая телеметрию)."
    echo "  --db-check            Проверка целостности всех БД."
    echo "  --vacuum              Сжатие и оптимизация всех баз данных (VACUUM)."
    echo -e "${CYAN}=============================================${NC}"
    echo "  -v, --verbose         Подробное логирование."
    echo "  --trace-status        Вывод панели состояния (CPU, RAM, Диск, VPN) раз в 60 сек."
    echo "  --trace-mem           Анализ количества потребляемой RAM."
    echo "  --notify-tg           Отправка уведомления в Телеграм после окончания работы."
    echo "  -h, --help            Помощь."
    echo -e "${CYAN}=============================================${NC}"
}

ask_yes_no() {
    local prompt="$1"
    local response
    while true; do
        read -r -p "$prompt (yes/no): " response
        response=$(echo "$response" | tr '[:upper:]' '[:lower:]')
        case "$response" in
            y|yes|д|да) return 0 ;;
            n|no|н|нет) return 1 ;;
            *) echo -e "${RED}Ошибка: необходимо строго прописать yes / да или no / нет${NC}" ;;
        esac
    done
}

validate_folder() {
    local folder="$1"
    if [ -d "$folder" ]; then return 0; fi
    local parent_dir
    parent_dir=$(dirname "$folder")
    if [ -d "$parent_dir" ]; then
        echo -e "${YELLOW}[ВНИМАНИЕ]${NC} Папка '$folder' не существует."
        if ask_yes_no "Создать её?"; then
            mkdir -p "$folder" && echo -e "${GREEN}[УСПЕХ]${NC} Папка создана." && return 0
        fi
    fi
    echo -e "${RED}[ОШИБКА]${NC} Папка не существует или не может быть создана."
    return 1
}

upsert_source() {
    [ ! -f "$SOURCES_TXT" ] && echo "# Имя | Ссылка | Папка | Файлов | Опции" > "$SOURCES_TXT"
    local u_cname="$1" u_link="$2" u_dest="$3" u_total="$4" u_opts="${5:-}"
    local tmp_file
    tmp_file=$(mktemp)
    local found=0
    
    while IFS= read -r line || [ -n "$line" ]; do
        local clean_line
        clean_line=$(echo "$line" | sed 's/^#//')
        local line_cname
        line_cname=$(echo "$clean_line" | cut -d'|' -f1 | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
        
        if [ "$line_cname" == "$u_cname" ]; then
            # Строка существует. Берем флаги СТРОГО из старой записи, игнорируя переданные.
            local old_opts
            old_opts=$(echo "$clean_line" | cut -d'|' -f5 | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
            local new_line="# $u_cname | $u_link | $u_dest | $u_total | $old_opts"
            echo "$new_line" >> "$tmp_file"
            found=1
        else
            echo "$line" >> "$tmp_file"
        fi
    done < "$SOURCES_TXT"
    
    # Если строка не найдена (новый контейнер), добавляем с переданными опциями
    if [ "$found" -eq 0 ]; then
        local new_line="# $u_cname | $u_link | $u_dest | $u_total | $u_opts"
        echo "$new_line" >> "$tmp_file"
    fi
    mv "$tmp_file" "$SOURCES_TXT"
}

parse_args() {
    while [[ "$#" -gt 0 ]]; do
        case $1 in
            -v|--verbose) VERBOSE_FLAG="1" ;;
            --refresh-cache) REFRESH_CACHE="1" ;;
            --build-queue) BUILD_QUEUE="1" ;;
            --vpn) USE_VPN="1" ;;
            --auth-enable) ;;
            --auth-disable) ;;
            --homeostasis-off) HOMEOSTASIS_OFF="1" ;;
            --simulate-ban) SIMULATE_BAN="1" ;;
            --ssl-off) SSL_OFF="1" ;;
            --notify-tg) NOTIFY_TG="1" ;;
            --force) FORCE_FLAG="1" ;;
            --trace-mem) TRACE_MEM="1" ;;
            --trace-status) TRACE_STATUS="1" ;;
            --threads=*)
                NUM_THREADS="${1#*=}"
                if ! [[ "$NUM_THREADS" =~ ^[0-9]+$ ]] || [ "$NUM_THREADS" -lt 1 ] || [ "$NUM_THREADS" -gt 8 ]; then
                    echo -e "${RED}[ОШИБКА]${NC} --threads от 1 до 8."; exit 1
                fi ;;
            --quantity-files=*)
                Q_FILES="${1#*=}"
                if ! [[ "$Q_FILES" =~ ^[0-9]+$ ]] || [ "$Q_FILES" -lt 1 ]; then
                    echo -e "${RED}[ОШИБКА]${NC} --quantity-files положительное число."; exit 1
                fi ;;
            --md5=*) MD5_TARGET="${1#*=}" ;;
            --move-extra=*) MOVE_EXTRA="${1#*=}" ;;
            *) [[ "$1" =~ ^[0-9]+$ ]] && TOTAL_FILES=$1 || { echo -e "${RED}[ОШИБКА]${NC} Неизвестный аргумент: $1"; exit 1; } ;;
        esac
        shift
    done
}

cleanup() {
    local CNAME=$1
    if [ -z "$CNAME" ]; then return; fi
    if $DOCKER_CMD inspect -f '{{.State.Status}}' "$CNAME" 2>/dev/null | grep -qw "exited"; then
        $DOCKER_CMD rm "$CNAME" > /dev/null 2>&1
        bash_log "${GREEN}" "Контейнер $CNAME удален."
    fi
    if [ -z "${DOCKER_IMG:-}" ]; then return; fi
    local stale_ids
    stale_ids=$($DOCKER_CMD ps -aq --filter "ancestor=$DOCKER_IMG" --filter "status=exited" 2>/dev/null || true)
    if [ -n "$stale_ids" ]; then
        # shellcheck disable=SC2086  # умышленный word-splitting: stale_ids содержит список ID через пробел
        $DOCKER_CMD rm $stale_ids > /dev/null 2>&1 || true
        bash_log "${GREEN}" "Очистка: удалены остановленные контейнеры Пылесоса."
    fi
}

run_download() {
    local CONTAINER_NAME=$1
    local DL_LINK=$2
    local DEST=$3
    local T_FILES=$4

    trap 'cleanup "$CONTAINER_NAME"' EXIT

    if ! $DOCKER_CMD image inspect "$DOCKER_IMG" > /dev/null 2>&1; then
        echo -e "${RED}[КРИТИЧНО]${NC} Docker образ '$DOCKER_IMG' не найден!"
        echo -e "${YELLOW}[INFO]${NC} Соберите его командой: docker build -t $DOCKER_IMG -f Dockerfile.yadpylesos-slim ."
        exit 1
    fi

    local DISK_INFO AVAIL SIZE MOUNT_POINT
    DISK_INFO=$(df -hP "$DEST" | tail -n 1)
    AVAIL=$(echo "$DISK_INFO" | awk '{print $4}')
    SIZE=$(echo "$DISK_INFO" | awk '{print $2}')
    MOUNT_POINT=$(echo "$DISK_INFO" | awk '{print $6}')
    
    if [ "$MOUNT_POINT" == "/" ]; then
        echo -e "${RED}[КРИТИЧНО]${NC} Папка назначения '$DEST' находится в корневой файловой системе (/)!"
        echo -e "${RED}[КРИТИЧНО]${NC} Скачивание в корень может переполнить диск NAS. Создайте отдельную папку."
        exit 1
    fi

    echo -e "${CYAN}[INFO]${NC} Папка назначения: $DEST"
    echo -e "${CYAN}[INFO]${NC} Свободное место: ${GREEN}${AVAIL}${NC} (Всего на томе: ${SIZE})"

    while $DOCKER_CMD ps -a --format '{{.Names}}' | grep -qw "$CONTAINER_NAME"; do
        echo -e "${YELLOW}[ВНИМАНИЕ]${NC} Контейнер с именем '$CONTAINER_NAME' уже существует!"
        if [ "$AUTO_MODE" == "1" ]; then
            echo -e "${YELLOW}[INFO]${NC} Авто-удаление старого контейнера (AUTO_MODE)..."
            $DOCKER_CMD stop "$CONTAINER_NAME" > /dev/null 2>&1 || true
            $DOCKER_CMD rm -f "$CONTAINER_NAME" > /dev/null 2>&1 || true
            break
        fi
        if ask_yes_no "Остановить и удалить старый контейнер?"; then
            local i=1
            while [ -f "$REPORT_DIR/docker_log_${CONTAINER_NAME}_$(printf "%02d" $i).txt" ]; do
                i=$((i+1))
            done
            local LOG_NUM
            LOG_NUM=$(printf "%02d" $i)
            $DOCKER_CMD logs "$CONTAINER_NAME" > "$REPORT_DIR/docker_log_${CONTAINER_NAME}_${LOG_NUM}.txt" 2>&1
            echo -e "${GREEN}[INFO]${NC} Лог старого контейнера сохранен в docker_log_${CONTAINER_NAME}_${LOG_NUM}.txt"
            $DOCKER_CMD stop "$CONTAINER_NAME" > /dev/null 2>&1 || true
            $DOCKER_CMD rm "$CONTAINER_NAME" > /dev/null 2>&1 || true
            echo -e "${GREEN}[INFO]${NC} Старый контейнер остановлен и удален."
            break
        else
            read -r -p "Введите новое имя (или Enter для отмены): " NEW_NAME
            if [ -z "$NEW_NAME" ]; then echo -e "${RED}[ОТМЕНА]${NC} Скрипт остановлен."; exit 1; fi
            CONTAINER_NAME="$NEW_NAME"
            trap 'cleanup "$CONTAINER_NAME"' EXIT
        fi
    done

    # Браузер берем из .env (дефолт 1, если не указан)
    BROWSER_CHOICE="${BROWSER_CHOICE:-1}"
    
    # Переменные для проброса путей и лимитов
    TAIL_LINES="${TAIL_LINES:-50}"
    THERMAL_CPU_PATH="${THERMAL_CPU_PATH:-/sys/devices/virtual/thermal/thermal_zone0/temp}"
    
    # Формируем массив DNS
    local DNS_ARGS=()
    if [ -z "${DNS_SERVERS:-}" ]; then
        echo -e "${RED}[КРИТИЧНО]${NC} DNS_SERVERS не задан в .env (пример: DNS_SERVERS=\"8.8.8.8 1.1.1.1\"). Запуск невозможен." >&2
        exit 1
    fi
    for ip in $DNS_SERVERS; do
        DNS_ARGS+=("--dns" "$ip")
    done

    if [ "$AUTO_MODE" == "0" ] && [ -z "$MD5_TARGET" ]; then
        local str_vpn str_tg str_verbose str_ssl str_homeo str_sim_ban str_move str_md5 str_files str_auth
        str_vpn=$([ "$USE_VPN" == "1" ] && echo -e "${GREEN}Вкл${NC}" || echo "Выкл")
        
        # Читаем статус авторизации из файла
        str_auth="Анонимный"
        if [ -f "$AUTH_DIR/.status" ]; then
            str_auth=$(cat "$AUTH_DIR/.status")
        fi
        str_tg=$([ "$NOTIFY_TG" == "1" ] && echo -e "${GREEN}Вкл${NC}" || echo "Выкл")
        str_verbose=$([ "$VERBOSE_FLAG" == "1" ] && echo -e "${GREEN}Вкл${NC}" || echo "Выкл")
        str_ssl=$([ "$SSL_OFF" == "1" ] && echo -e "${RED}Выкл${NC}" || echo -e "${GREEN}Вкл${NC}")
        str_homeo=$([ "$HOMEOSTASIS_OFF" == "1" ] && echo "Выкл" || echo -e "${GREEN}Вкл${NC}")
        str_sim_ban=$([ "$SIMULATE_BAN" == "1" ] && echo -e "${RED}Вкл${NC}" || echo "Выкл")
        str_move=$([ -n "$MOVE_EXTRA" ] && echo "$MOVE_EXTRA" || echo "Нет")
        str_md5=$([ -n "$MD5_TARGET" ] && echo "$MD5_TARGET" || echo "Нет")
        if [ "$T_FILES" -gt 0 ] 2>/dev/null; then
            str_files="$T_FILES файлов"
        else
            str_files="Авто"
        fi

        echo -e "${CYAN}============================================="
        echo -e " ${GREEN}🚀 Я.Д-Пылесос | Подготовка к запуску${NC}"
        echo -e "${CYAN}=============================================${NC}"
        echo -e " 📦 Контейнер:   ${YELLOW}$CONTAINER_NAME${NC}"
        echo -e " 🔗 Ссылка:      $DL_LINK"
        echo -e " 📁 Папка:       $DEST"
        echo -e " 📄 Ожидается:   $str_files"
        echo -e "${CYAN}---------------------------------------------${NC}"
        echo -e " ⚙️  Параметры скачивания:"
        echo -e "   • Потоки (на файл):    $NUM_THREADS"
        echo -e "   • Параллельно файлов:  $Q_FILES"
        echo -e "   • Гомеостаз:           $str_homeo"
        echo -e "   • Карантин (orphans):  $str_move"
        echo -e "   • Проверка MD5:        $str_md5"
        echo -e "   • Обновить кэш:        $([ "$REFRESH_CACHE" == "1" ] && echo -e "${GREEN}Да${NC}" || echo "Нет")"
        echo -e "   • Только очередь:      $([ "$BUILD_QUEUE" == "1" ] && echo -e "${GREEN}Да${NC}" || echo "Нет")"
        echo -e "${CYAN}---------------------------------------------${NC}"
        echo -e " 🛡️  Сеть и Безопасность:"
        echo -e "   • VPN:                 $str_vpn"
        echo -e "   • Авторизация:         $str_auth"
        echo -e "   • Проверка SSL:        $str_ssl"
        echo -e "   • Симуляция бана:      $str_sim_ban"
        echo -e "${CYAN}---------------------------------------------${NC}"
        echo -e " 📲 Уведомления и Логи:"
        echo -e "   • Telegram:            $str_tg"
        echo -e "   • Подробный лог (-v):  $str_verbose"
        echo -e "   • Панель статуса:      $([ "$TRACE_STATUS" == "1" ] && echo -e "${GREEN}Вкл${NC}" || echo "Выкл")"
        echo -e "   • Трассировка RAM:     $([ "$TRACE_MEM" == "1" ] && echo -e "${GREEN}Вкл${NC}" || echo "Выкл")"
        echo -e "${CYAN}=============================================${NC}"

        if ! ask_yes_no "Параметры верны? Продолжить запуск"; then
            echo -e "${RED}[ОТМЕНА]${NC} Запуск отменен пользователем."
            exit 1
        fi
    fi

    PY_ARGS=("$DL_LINK" "$T_FILES" "$BROWSER_CHOICE" "$CONTAINER_NAME" "$VERBOSE_FLAG")
    [ "$REFRESH_CACHE" == "1" ] && PY_ARGS+=("--refresh-cache")
    [ "$BUILD_QUEUE" == "1" ] && PY_ARGS+=("--build-queue")
    [ "$USE_VPN" == "1" ] && PY_ARGS+=("--force-vpn")
    [ "$HOMEOSTASIS_OFF" == "1" ] && PY_ARGS+=("--homeostasis-off")
    [ "$SIMULATE_BAN" == "1" ] && PY_ARGS+=("--simulate-ban")
    [ "$SSL_OFF" == "1" ] && PY_ARGS+=("--ssl-off")
    [ "$NOTIFY_TG" == "1" ] && PY_ARGS+=("--notify-tg")
    [ "$FORCE_FLAG" == "1" ] && PY_ARGS+=("--force")
    [ "$TRACE_MEM" == "1" ] && PY_ARGS+=("--trace-mem")
    [ "$TRACE_STATUS" == "1" ] && PY_ARGS+=("--trace-status")
    [ -n "$NUM_THREADS" ] && PY_ARGS+=("--num-threads=$NUM_THREADS")
    [ -n "$Q_FILES" ] && PY_ARGS+=("--quantity-files=$Q_FILES")
    [ -n "$MD5_TARGET" ] && PY_ARGS+=("--md5-target=$MD5_TARGET")
    [ -n "$MOVE_EXTRA" ] && PY_ARGS+=("--move-extra-path=$MOVE_EXTRA")
    PY_ARGS+=("--host-dest=$DEST")

    local LOG_OPTS="--log-opt max-size=$LOG_MAX_SIZE --log-opt max-file=$LOG_MAX_FILES"

    if [ -n "$MD5_TARGET" ]; then
        # shellcheck disable=SC2086,SC2046  # LOG_OPTS разворачивается в флаги; id возвращает число
        $DOCKER_CMD run --rm -it --name "$CONTAINER_NAME" "${ENV_ARGS[@]}" $LOG_OPTS \
          --user $(id -u):$(id -g) \
          "${DNS_ARGS[@]}" \
          -v "$SCRIPT_PATH":/app/yadpylesos.py -v "$BASE_DIR/apicloudyandex.py":/app/apicloudyandex.py -v "$BASE_DIR/vpnmanager.py":/app/vpnmanager.py -v "$BASE_DIR/apivideo.py":/app/apivideo.py \
          -v "$BASE_DIR/config":/config:ro -v "$BASE_DIR/cache/yt-dlp":/cache-yt \
          -v "$AUTH_DIR":/auth -v "$BASE_DIR/vpn":/vpn -v "$REPORT_DIR":/report \
          -v "$DB_DIR":/db -v "$DEST":/download \
          -v "$THERMAL_CPU_PATH":/sys_thermal_cpu:ro \
          "$DOCKER_IMG" python3 -u /app/yadpylesos.py "${PY_ARGS[@]}"
        cleanup "$CONTAINER_NAME"
        return
    fi

    # shellcheck disable=SC2086,SC2046  # LOG_OPTS разворачивается в флаги; id возвращает число
    $DOCKER_CMD run -d --name "$CONTAINER_NAME" "${ENV_ARGS[@]}" $LOG_OPTS \
      --user $(id -u):$(id -g) \
      "${DNS_ARGS[@]}" \
      -v "$SCRIPT_PATH":/app/yadpylesos.py -v "$BASE_DIR/apicloudyandex.py":/app/apicloudyandex.py -v "$BASE_DIR/vpnmanager.py":/app/vpnmanager.py -v "$BASE_DIR/apivideo.py":/app/apivideo.py \
      -v "$BASE_DIR/config":/config:ro -v "$BASE_DIR/cache/yt-dlp":/cache-yt \
      -v "$AUTH_DIR":/auth -v "$BASE_DIR/vpn":/vpn -v "$REPORT_DIR":/report \
      -v "$DB_DIR":/db -v "$DEST":/download \
      -v "$BASE_DIR/history":/history \
      -v "$THERMAL_CPU_PATH":/sys_thermal_cpu:ro \
      "$DOCKER_IMG" python3 -u /app/yadpylesos.py "${PY_ARGS[@]}"

    if ! $DOCKER_CMD ps --format '{{.Names}}' | grep -qw "$CONTAINER_NAME"; then
        echo -e "${RED}[ОШИБКА]${NC} Контейнер не запустился."
        return 1
    fi

    if [ "$AUTO_MODE" == "0" ]; then
        echo -e "${GREEN}[УСПЕХ]${NC} Контейнер $CONTAINER_NAME запущен."
        echo -e "${GREEN}[INFO]${NC} Для просмотра логов: $DOCKER_CMD logs --tail $TAIL_LINES -f $CONTAINER_NAME"
        echo -e "${YELLOW}[INFO]${NC} Для остановки и удаления: $DOCKER_CMD stop $CONTAINER_NAME && $DOCKER_CMD rm -f $CONTAINER_NAME"
        echo -e "${GREEN}[INFO]${NC} Для выхода из логов нажмите Ctrl+C (контейнер продолжит работу в фоне)."
        echo -e "============================================="
    fi

    if [ "$AUTO_MODE" == "1" ]; then
        if [ "$BATCH_AUTO" == "1" ]; then
            local elapsed=0
            local poll_int="${CONTAINER_POLL_INTERVAL:-10}"
            # Ждем завершения контейнера
            while $DOCKER_CMD inspect -f '{{.State.Running}}' "$CONTAINER_NAME" 2>/dev/null | grep -qw "true"; do
                sleep "$poll_int"
                elapsed=$((elapsed + poll_int))
                if [ "$elapsed" -ge "$MAX_RUNTIME" ]; then
                    echo -e "${RED}[ВНИМАНИЕ]${NC} Превышен лимит времени ($MAX_RUNTIME сек). Принудительная остановка."
                    $DOCKER_CMD stop "$CONTAINER_NAME" > /dev/null 2>&1 || true
                    break
                fi
            done
            # Контейнер остановлен. Выгружаем весь лог разом в BATCH_LOG
            $DOCKER_CMD logs "$CONTAINER_NAME"
        else
            $DOCKER_CMD logs -f "$CONTAINER_NAME"
        fi
        cleanup "$CONTAINER_NAME"
        return 0
    fi
    
    $DOCKER_CMD logs --tail "$TAIL_LINES" -f "$CONTAINER_NAME" &
    local logs_pid=$!
    
    trap 'kill $logs_pid 2>/dev/null' SIGINT
    wait $logs_pid 2>/dev/null || true
    trap - SIGINT
    
    local status
    status=$($DOCKER_CMD inspect -f '{{.State.Status}}' "$CONTAINER_NAME" 2>/dev/null)
    
    if [ "$status" == "running" ]; then
        echo -e "${GREEN}[INFO]${NC} Контейнер ушел в фон. Продолжаю скачивать текущую ссылку в фоне."
        echo -e "${GREEN}[INFO]${NC} Для просмотра логов: $DOCKER_CMD logs --tail $TAIL_LINES -f $CONTAINER_NAME"
        echo -e "${YELLOW}[INFO]${NC} Для остановки и удаления: $DOCKER_CMD stop $CONTAINER_NAME && $DOCKER_CMD rm -f $CONTAINER_NAME"
        exit 0
    else
        cleanup "$CONTAINER_NAME"
    fi
}

CNAME=""; DOWNLOAD_LINK=""; DEST_FOLDER=""; CONTAINER_NAME=""
AUTO_MODE="0"; BATCH_AUTO="0"; REFRESH_CACHE="0"; BUILD_QUEUE="0"
USE_VPN="0"; HOMEOSTASIS_OFF="0"
SIMULATE_BAN="0"; SSL_OFF="0"; TRACE_MEM="0"; TRACE_STATUS="0"; NOTIFY_TG="0"; NUM_THREADS=1; Q_FILES=1
MD5_TARGET=""; MOVE_EXTRA=""; TOTAL_FILES=0; VERBOSE_FLAG="0"; FORCE_FLAG="0"

if [ "$#" -eq 0 ] || [ "$1" == "-h" ] || [ "$1" == "--help" ]; then show_help; exit 0; fi

if [ "$1" == "--auth-enable" ]; then 
    mkdir -p "$AUTH_DIR"
    touch "$AUTH_DIR/.auth_enabled"
    echo "OAuth 2.0" > "$AUTH_DIR/.status"
    echo "Auth ON"
    exit 0
fi
if [ "$1" == "--auth-disable" ]; then 
    rm -f "$AUTH_DIR/.auth_enabled"
    echo "Анонимный" > "$AUTH_DIR/.status"
    echo "Auth OFF"
    exit 0
fi

# Блок запуска служебных команд (--db-stats, --db-check, --vacuum, --auth-status)
if [ "$1" == "--db-stats" ] || [ "$1" == "--db-check" ] || [ "$1" == "--vacuum" ] || [ "$1" == "--auth-status" ] || [ "$1" == "--vpn-test" ]; then
    mkdir -p "$REPORT_DIR" "$DB_DIR" "$BASE_DIR/vpn"
    # Пробрасываем все аргументы ("$@") и добавляем папку vpn на случай, если с --auth-status передан --vpn
    $DOCKER_CMD run --rm -it -e SERVICE_MODE=1 "${ENV_ARGS[@]}" \
      -v "$SCRIPT_PATH":/app/yadpylesos.py -v "$BASE_DIR/apicloudyandex.py":/app/apicloudyandex.py -v "$BASE_DIR/vpnmanager.py":/app/vpnmanager.py -v "$BASE_DIR/apivideo.py":/app/apivideo.py \
      -v "$BASE_DIR/config":/config:ro \
      -v "$REPORT_DIR":/report -v "$DB_DIR":/db -v "$AUTH_DIR":/auth -v "$BASE_DIR/vpn":/vpn \
      -v "$THERMAL_CPU_PATH":/sys_thermal_cpu:ro \
      "$DOCKER_IMG" python3 -u /app/yadpylesos.py "$@"
    exit $?
fi

if [ "$1" == "--vpn-test" ]; then
    mkdir -p "$REPORT_DIR" "$AUTH_DIR" "$BASE_DIR/vpn"
    $DOCKER_CMD run --rm -it -e SERVICE_MODE=1 "${ENV_ARGS[@]}" \
      -v "$SCRIPT_PATH":/app/yadpylesos.py -v "$BASE_DIR/apicloudyandex.py":/app/apicloudyandex.py -v "$BASE_DIR/vpnmanager.py":/app/vpnmanager.py -v "$BASE_DIR/apivideo.py":/app/apivideo.py \
      -v "$BASE_DIR/config":/config:ro \
      -v "$REPORT_DIR":/report -v "$AUTH_DIR":/auth -v "$BASE_DIR/vpn":/vpn \
      -v "$THERMAL_CPU_PATH":/sys_thermal_cpu:ro \
      "$DOCKER_IMG" python3 -u /app/yadpylesos.py "$1"
    exit $?
fi

# BL-Bulkhead: Защита от параллельного запуска (Инструмент 21)
if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE" 2>/dev/null)
    if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
        echo -e "${RED}[ОШИБКА]${NC} Скрипт уже запущен (PID: $PID)! Параллельный запуск запрещен."
        exit 1
    else
        echo -e "${YELLOW}[ВНИМАНИЕ]${NC} Найден устаревший .pid файл (процесс умер). Удаление..."
        rm -f "$PID_FILE"
    fi
fi
echo $$ > "$PID_FILE"
# Гарантированное удаление .pid при любом выходе из скрипта
trap 'rm -f "$PID_FILE"' EXIT

if [ "$1" == "--batch" ] || [ "$1" == "--batch-auto" ] || [ "$1" == "--auto" ]; then
    AUTO_MODE="1"
    [ "$1" == "--batch-auto" ] && BATCH_AUTO="1"

    # Парсим дополнительные флаги (например, --notify-tg), чтобы применить их ко всему пакету
    shift
    if [ "$#" -gt 0 ]; then
        parse_args "$@"
    fi
    GLOBAL_BATCH_OPTS=""
    [ "$VERBOSE_FLAG" == "1" ] && GLOBAL_BATCH_OPTS+=" -v"
    [ "$NOTIFY_TG" == "1" ] && GLOBAL_BATCH_OPTS+=" --notify-tg"
    [ "$TRACE_STATUS" == "1" ] && GLOBAL_BATCH_OPTS+=" --trace-status"
    [ "$TRACE_MEM" == "1" ] && GLOBAL_BATCH_OPTS+=" --trace-mem"

    if [ ! -f "$SOURCES_TXT" ]; then
        echo "# Имя | Ссылка | Папка | Файлов | Опции" > "$SOURCES_TXT"
        echo -e "${GREEN}[INFO]${NC} Создан новый $SOURCES_TXT"
    elif ! head -n 1 "$SOURCES_TXT" | grep -q "Имя"; then
        echo -e "${YELLOW}[ВНИМАНИЕ]${NC} Неожиданный заголовок $SOURCES_TXT — файл НЕ перезаписан. Проверьте вручную при необходимости."
    fi

    if [ "$BATCH_AUTO" == "0" ]; then
        echo -e "${CYAN}=== Менеджер ссылок ===${NC}"
        $DOCKER_CMD rm -f yadpylesos-manager > /dev/null 2>&1
        $DOCKER_CMD run --rm -it --name "yadpylesos-manager" \
          -v "$SCRIPT_PATH":/app/yadpylesos.py -v "$BASE_DIR/apicloudyandex.py":/app/apicloudyandex.py -v "$BASE_DIR/vpnmanager.py":/app/vpnmanager.py -v "$BASE_DIR/apivideo.py":/app/apivideo.py \
          -v "$BASE_DIR/config":/config:ro -v "$BASE_DIR/config/source_links.txt":/config/source_links.txt \
          -v "$REPORT_DIR":/report \
          "$DOCKER_IMG" python3 -u /app/yadpylesos.py --manage
        run_rc=$?
        if [ "$run_rc" -ne 0 ]; then echo "Выход"; exit 0; fi
    fi

    declare -a BATCH_ARRAY
    while IFS= read -r line || [ -n "$line" ]; do BATCH_ARRAY+=("$line"); done < "$SOURCES_TXT"
    
    # Очистка старого состояния пакетной обработки
    rm -f "$LOCK_FILE"
    
    # Получаем время для имени файла из Docker-образа (для корректного TZ)
    batch_ts=$($DOCKER_CMD run --rm -e TZ="$TZ" "$DOCKER_IMG" python3 -c "from datetime import datetime; print(datetime.now().strftime('%Y%m%d_%H%M%S'))" 2>/dev/null || date +%Y%m%d_%H%M%S)
    BATCH_LOG="$REPORT_DIR/batch_${batch_ts}.txt"

    run_batch() {
        for i in "${!BATCH_ARRAY[@]}"; do
            line="${BATCH_ARRAY[$i]}"
            clean_check=$(echo "$line" | sed 's/^[# ]*//')
            [ -z "$clean_check" ] || [ "$clean_check" == "Имя" ] && continue
            [[ "$line" == \#* ]] && continue
            
            cname=$(echo "$line" | cut -d'|' -f1 | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
            link=$(echo "$line" | cut -d'|' -f2 | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
            dest=$(echo "$line" | cut -d'|' -f3 | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
            total=$(echo "$line" | cut -d'|' -f4 | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
            opts=$(echo "$line" | cut -d'|' -f5 | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
            
            REFRESH_CACHE="0"; BUILD_QUEUE="0"
            USE_VPN="0"; HOMEOSTASIS_OFF="0"; SIMULATE_BAN="0"; SSL_OFF="0"
            NUM_THREADS=1; Q_FILES=1; MD5_TARGET=""; MOVE_EXTRA=""; VERBOSE_FLAG="0"
            NOTIFY_TG="0"; TRACE_MEM="0"; TRACE_STATUS="0"
            
            # Применяем флаги из source_links.txt И глобальные флаги --batch-auto
            if [ -n "$opts" ] || [ -n "$GLOBAL_BATCH_OPTS" ]; then
                read -ra _OPTS_ARRAY <<< "$opts $GLOBAL_BATCH_OPTS"
                parse_args "${_OPTS_ARRAY[@]}"
            fi
            
            run_download "$cname" "$link" "$dest" "$total" || true
            BATCH_ARRAY[$i]="# $line"
        done
        # shellcheck disable=SC2188  # легитимный truncate: обнулить файл списка перед перезаписью
        > "$SOURCES_TXT"
        for l in "${BATCH_ARRAY[@]}"; do echo "$l" >> "$SOURCES_TXT"; done
        bash_log "${GREEN}" "Пакетная выгрузка завершена."
    }

    if [ "$BATCH_AUTO" == "1" ]; then
        bash_log "${GREEN}" "Запуск в фоновом режиме (cron)..."
        # Очищаем файл перед стартом, а затем дописываем (>>) на каждой итерации
        # shellcheck disable=SC2188  # легитимный truncate: обнулить лог пакета перед стартом
        > "$BATCH_LOG"
        run_batch >> "$BATCH_LOG" 2>&1 &
        echo "PID: $! | Лог: $BATCH_LOG"
        exit 0
    else
        echo -e "${GREEN}[INFO]${NC} Запуск пакетной обработки..."
        echo -e "${YELLOW}[INFO]${NC} Для выхода из просмотра логов нажмите Ctrl+C. Обработка продолжится в фоне."
        
        # Запускаем цикл в фоне, весь его вывод идет в BATCH_LOG
        run_batch > "$BATCH_LOG" 2>&1 &
        BATCH_PID=$!
        disown $BATCH_PID 2>/dev/null || true
        
        # Обработчик Ctrl+C
        # shellcheck disable=SC2317  # вся функция вызывается через trap SIGINT — shellcheck trap-вызовы не видит
        handle_ctrl_c() {
            local remaining_names=""
            local remaining_count=0
            local current_cname=""
            
            while IFS= read -r line || [ -n "$line" ]; do
                clean_check=$(echo "$line" | sed 's/^[# ]*//')
                [ -z "$clean_check" ] || [ "$clean_check" == "Имя" ] && continue
                if [[ "$line" != \#* ]]; then
                    cname=$(echo "$line" | cut -d'|' -f1 | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
                    if [ -z "$current_cname" ]; then
                        current_cname="$cname"
                    fi
                    remaining_names="$remaining_names $cname"
                    remaining_count=$((remaining_count + 1))
                fi
            done < "$SOURCES_TXT"
            
            if [ -n "$current_cname" ]; then
                echo -e "\n${YELLOW}[INFO]${NC} Пакетная обработка продолжает работу в фоне."
                echo -e "${YELLOW}[INFO]${NC} Осталось контейнеров: $remaining_count (Имена:$remaining_names)."
                echo -e "${YELLOW}[INFO]${NC} Текущий работающий контейнер: $current_cname"
                echo -e "${YELLOW}[INFO]${NC} Для просмотра логов введите: $DOCKER_CMD logs -f $current_cname"
                echo -e "${YELLOW}[INFO]${NC} Для просмотра работающего контейнера в любой момент времени воспользуйтесь командой $DOCKER_CMD ps"
            else
                echo -e "\n${YELLOW}[INFO]${NC} Пакетная обработка завершена."
            fi
            
            if [ -n "$TAIL_PID" ]; then
                kill "$TAIL_PID" 2>/dev/null || true
            fi
            exit 0
        }
        
        trap 'handle_ctrl_c' SIGINT
        
        # Транслируем логи на экран
        sleep 1
        tail -f "$BATCH_LOG" &
        TAIL_PID=$!
        
        # Ждем завершения фоновой задачи
        wait $BATCH_PID 2>/dev/null || true
        
        # Если задача завершилась сама (без Ctrl+C)
        if [ -n "$TAIL_PID" ]; then
            kill $TAIL_PID 2>/dev/null || true
            wait $TAIL_PID 2>/dev/null || true
        fi
        trap - SIGINT
        echo -e "${GREEN}[INFO]${NC} === Пакетная обработка завершена ==="
        exit 0
    fi
fi

CNAME=$(echo "${1:-}" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
DOWNLOAD_LINK=$(echo "${2:-}" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
DEST_FOLDER=$(echo "${3:-}" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
shift 3

[ -z "$CNAME" ] && { echo "Нет имени"; exit 1; }
[ -z "$DOWNLOAD_LINK" ] && { echo "Нет ссылки"; exit 1; }
[ -z "$DEST_FOLDER" ] && { echo "Нет папки"; exit 1; }
[[ ! "$DOWNLOAD_LINK" == http* ]] && { echo "Ссылка должна начинаться с http"; exit 1; }

parse_args "$@"
mkdir -p "$REPORT_DIR" "$DB_DIR" "$AUTH_DIR"
chmod 700 "$AUTH_DIR" 2>/dev/null || true
validate_folder "$DEST_FOLDER" || exit 1

MANUAL_OPTS=""
[ "$VERBOSE_FLAG" == "1" ] && MANUAL_OPTS+=" -v"
[ "$REFRESH_CACHE" == "1" ] && MANUAL_OPTS+=" --refresh-cache"
[ "$USE_VPN" == "1" ] && MANUAL_OPTS+=" --vpn"
[ "$SSL_OFF" == "1" ] && MANUAL_OPTS+=" --ssl-off"
[ "$NOTIFY_TG" == "1" ] && MANUAL_OPTS+=" --notify-tg"
[ -n "$MOVE_EXTRA" ] && MANUAL_OPTS+=" --move-extra=$MOVE_EXTRA"
[ -n "$MD5_TARGET" ] && MANUAL_OPTS+=" --md5=$MD5_TARGET"
upsert_source "$CNAME" "$DOWNLOAD_LINK" "$DEST_FOLDER" "$TOTAL_FILES" ""

run_download "$CNAME" "$DOWNLOAD_LINK" "$DEST_FOLDER" "$TOTAL_FILES"
