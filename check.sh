#!/bin/bash
# Единая точка проверок качества (Спринт 5)
set -euo pipefail
cd "$(dirname "$0")"

LINT_IMAGE="yadpylesos-lint"

if ! docker image inspect "$LINT_IMAGE" > /dev/null 2>&1; then
    echo "[INFO] Сборка образа линтеров..."
    sudo docker build -t "$LINT_IMAGE" -f Dockerfile.lint .
fi

echo "=== 1/5 py_compile ==="
docker run --rm -v "$PWD":/app -w /app yadpylesos-slim \
    python3 -m py_compile yadpylesos.py vpnmanager.py apicloudyandex.py apivideo.py

echo "=== 2/5 ruff (ошибки + сложность CC<=9) ==="
docker run --rm -v "$PWD":/app -w /app "$LINT_IMAGE" ruff check .

echo "=== 3/5 radon (верификация CC, информационно; гейт — ruff C901) ==="
docker run --rm -v "$PWD":/app -w /app "$LINT_IMAGE" \
    radon cc -s -n C yadpylesos.py apicloudyandex.py apivideo.py vpnmanager.py || true

echo "=== 4/5 vulture (мёртвый код) ==="
docker run --rm -v "$PWD":/app -w /app "$LINT_IMAGE" \
    vulture yadpylesos.py apicloudyandex.py apivideo.py vpnmanager.py vulture-whitelist.txt --min-confidence 80

echo "=== 5/5 shellcheck (bash) ==="
docker run --rm -v "$PWD":/app -w /app "$LINT_IMAGE" shellcheck yadpylesos.sh install.sh

echo "=== ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ ==="
