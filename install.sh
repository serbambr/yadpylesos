#!/bin/bash
# ==========================================
# Проект: Я.Д-Пылесос / YA.D-Pylesos
# Скрипт: install.sh (Автоматическая установка)
# ==========================================

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
NC='\033[0m'

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"

echo -e "${CYAN}=== НАЧАЛО УСТАНОВКИ Я.Д-Пылесос ===${NC}"

# 1. Проверка Docker и прав доступа
echo -e "${YELLOW}[1/7]${NC} Проверка Docker..."
if docker info &> /dev/null; then
    DOCKER_CMD="docker"
elif sudo docker info &> /dev/null; then
    DOCKER_CMD="sudo docker"
    echo -e "${YELLOW}Требуются права sudo для Docker.${NC}"
else
    echo -e "${RED}[ОШИБКА]${NC} Docker не установлен или нет прав доступа!"
    echo -e "Установите Docker и добавьте пользователя в группу docker, либо запускайте скрипт с sudo."
    exit 1
fi
echo -e "${GREEN}Docker найден и доступен.${NC}"

# 2. Создание структуры папок
echo -e "${YELLOW}[2/7]${NC} Создание структуры папок..."
mkdir -p "$BASE_DIR/auth" "$BASE_DIR/backups" "$BASE_DIR/db" "$BASE_DIR/report" "$BASE_DIR/test" "$BASE_DIR/vpn"
chmod 700 "$BASE_DIR/auth"
echo -e "${GREEN}Папки (auth, backups, db, report, test, vpn) созданы.${NC}"

# 3. Создание файла конфигурации config.yaml
echo -e "${YELLOW}[3/7]${NC} Проверка config.yaml..."
if [ ! -f "$BASE_DIR/config.yaml" ]; then
    cat << 'CFG' > "$BASE_DIR/config.yaml"
paths:
  db_dir: "/db"
  report_dir: "/report"
  download_dir: "/download"
  auth_dir: "/auth"
  vpn_dir: "/vpn"
CFG
    echo -e "${GREEN}Файл config.yaml создан.${NC}"
else
    echo -e "${GREEN}Файл config.yaml уже существует.${NC}"
fi

# 4. Создание файла .env (SSOT)
echo -e "${YELLOW}[4/7]${NC} Проверка .env..."
if [ ! -f "$BASE_DIR/.env" ]; then
    cat << 'ENV' > "$BASE_DIR/.env"
TZ=Europe/Moscow
MAX_RUNTIME=21600
LOG_MAX_SIZE=10m
LOG_MAX_FILES=3
MAX_RETRIES=5
CHUNK_SIZE_MB=10
QUEUE_LIMIT=10000
MULTITHREAD_SIZE_MB=100
DOCKER_IMG=yadpylesos-slim
ENV
    echo -e "${GREEN}Файл .env создан.${NC}"
else
    echo -e "${GREEN}Файл .env уже существует.${NC}"
fi

# 5. Создание шаблона source_links.txt
echo -e "${YELLOW}[5/7]${NC} Проверка файла ссылок..."
if [ ! -f "$BASE_DIR/source_links.txt" ]; then
    echo "# Имя | Ссылка | Папка | Файлов | Опции" > "$BASE_DIR/source_links.txt"
    echo -e "${GREEN}Файл source_links.txt создан.${NC}"
else
    echo -e "${GREEN}Файл source_links.txt уже существует.${NC}"
fi

# 6. Сборка Docker-образа
echo -e "${YELLOW}[6/7]${NC} Сборка Docker-образа yadpylesos-slim..."
if [ -f "$BASE_DIR/Dockerfile.yadpylesos-slim" ]; then
    $DOCKER_CMD build -t yadpylesos-slim -f "$BASE_DIR/Dockerfile.yadpylesos-slim" "$BASE_DIR"
    if [ $? -ne 0 ]; then
        echo -e "${RED}[ОШИБКА]${NC} Не удалось собрать Docker-образ."
        exit 1
    fi
    echo -e "${GREEN}Образ yadpylesos-slim успешно собран.${NC}"
else
    echo -e "${RED}[ОШИБКА]${NC} Файл Dockerfile.yadpylesos-slim не найден!"
    exit 1
fi

# 7. Создание алиаса (символической ссылки)
echo -e "${YELLOW}[7/7]${NC} Создание ярлыка запуска..."
if [ ! -L "$BASE_DIR/yadpylesos" ]; then
    ln -s yadpylesos.sh yadpylesos
    echo -e "${GREEN}Ярлык 'yadpylesos' создан.${NC}"
else
    echo -e "${GREEN}Ярлык 'yadpylesos' уже существует.${NC}"
fi

echo -e "${CYAN}=============================================${NC}"
echo -e "${GREEN}УСТАНОВКА ЗАВЕРШЕНА УСПЕШНО!${NC}"
echo -e "${CYAN}=============================================${NC}"
echo ""
echo "Для запуска скачивания используйте:"
echo -e "  ${GREEN}./yadpylesos 'имя' 'https://disk.yandex.ru/d/xxxx' '/share/...' 0 -v${NC}"
echo ""
echo "Для запуска пакетного режима (менеджер ссылок):"
echo -e "  ${GREEN}./yadpylesos --batch${NC}"
echo ""
echo "Для просмотра справки:"
echo -e "  ${GREEN}./yadpylesos -h${NC}"
