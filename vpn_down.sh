#!/bin/bash
set -euo pipefail
# ==========================================
# Скрипт: vpn_down.sh
# Назначение: Остановка VPN-контейнера и очистка
# ==========================================

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
REPORT_DIR="$BASE_DIR/report"
CONFIG_RUNTIME="$BASE_DIR/vpn/config_runtime.yaml"
ENV_FILE="$BASE_DIR/.env"

# Загрузка переменных из .env (или дефолтные значения)
VPN_CONTAINER_NAME="yadp-vpn"
VPN_NETWORK_NAME="yad-vpn-net"

if [ -f "$ENV_FILE" ]; then
    set -a
    . "$ENV_FILE"
    set +a
fi

# 1. Сохранение логов перед удалением (Гравитационная очистка)
if docker ps -a --format '{{.Names}}' | grep -qw "$VPN_CONTAINER_NAME"; then
    echo "[INFO] Сохранение логов VPN-контейнера..."
    docker logs "$VPN_CONTAINER_NAME" > "$REPORT_DIR/docker_log_vpn_$(date +%Y%m%d_%H%M%S).txt" 2>&1
    echo "[INFO] Остановка VPN-контейнера..."
    docker stop "$VPN_CONTAINER_NAME" > /dev/null 2>&1 || true
    docker rm -f "$VPN_CONTAINER_NAME" > /dev/null 2>&1 || true
fi

# 2. Удаление временного конфига (Сборка мусора)
if [ -f "$CONFIG_RUNTIME" ]; then
    rm -f "$CONFIG_RUNTIME"
fi

# 3. Удаление изолированной сети (Очистка за собой)
if docker network ls --format '{{.Name}}' | grep -qw "$VPN_NETWORK_NAME"; then
    docker network rm "$VPN_NETWORK_NAME" > /dev/null 2>&1 || true
fi

echo "[INFO] VPN-контейнер остановлен и удален."
exit 0
