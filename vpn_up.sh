#!/bin/bash
set -euo pipefail
# ==========================================
# Скрипт: vpn_up.sh
# Назначение: Запуск VPN-контейнера (mihomo)
# ==========================================

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
REPORT_DIR="$BASE_DIR/report"
CONFIG_RUNTIME="$BASE_DIR/vpn/config_runtime.yaml"
ENV_FILE="$BASE_DIR/.env"

# Загрузка переменных из .env (или дефолтные значения)
VPN_CONTAINER_NAME="yadp-vpn"
VPN_NETWORK_NAME="yad-vpn-net"
VPN_INIT_TIMEOUT=15
VPN_IMAGE="metacubex/mihomo:v1.18.5"

if [ -f "$ENV_FILE" ]; then
    set -a
    . "$ENV_FILE"
    set +a
fi

mkdir -p "$REPORT_DIR"

# 1. Проверка наличия сгенерированного конфига
if [ ! -f "$CONFIG_RUNTIME" ]; then
    echo "[ОШИБКА] Файл конфигурации $CONFIG_RUNTIME не найден. Сначала запустите основной скрипт."
    exit 1
fi

# 2. Проверка, не запущен ли уже контейнер (Идемпотентность)
if docker ps --format '{{.Names}}' | grep -qw "$VPN_CONTAINER_NAME"; then
    echo "[INFO] VPN-контейнер $VPN_CONTAINER_NAME уже запущен."
    exit 0
fi

# 3. Очистка старого остановленного контейнера (если есть)
if docker ps -a --format '{{.Names}}' | grep -qw "$VPN_CONTAINER_NAME"; then
    docker rm -f "$VPN_CONTAINER_NAME" > /dev/null 2>&1
fi

# 4. Создание изолированной сети (если её нет)
docker network create "$VPN_NETWORK_NAME" > /dev/null 2>&1 || true

echo "[INFO] Запуск VPN-контейнера (mihomo)..."
docker run -d --name "$VPN_CONTAINER_NAME" --network "$VPN_NETWORK_NAME" \
  -e TZ=Europe/Moscow \
  -v "$CONFIG_RUNTIME":/root/.config/mihomo/config.yaml \
  "$VPN_IMAGE" > /dev/null 2>&1

# 5. Проверка успешности старта (Контракт)
if [ $? -ne 0 ]; then
    echo "[ОШИБКА] Не удалось запустить VPN-контейнер!"
    echo "[INFO] Логи mihomo:"
    docker logs "$VPN_CONTAINER_NAME" 2>&1
    exit 1
fi

echo "[INFO] VPN-контейнер запущен. Ожидание инициализации ($VPN_INIT_TIMEOUT сек)..."
sleep "$VPN_INIT_TIMEOUT"

# 6. Финальная проверка, что контейнер не упал (Апоптоз)
if ! docker ps --format '{{.Names}}' | grep -qw "$VPN_CONTAINER_NAME"; then
    echo "[КРИТИЧНО] VPN-контейнер упал во время инициализации."
    docker logs "$VPN_CONTAINER_NAME" > "$REPORT_DIR/docker_log_vpn_crash_$(date +%Y%m%d_%H%M%S).txt" 2>&1
    docker rm -f "$VPN_CONTAINER_NAME" > /dev/null 2>&1
    exit 1
fi

echo "[УСПЕХ] VPN-туннель активен."
exit 0
