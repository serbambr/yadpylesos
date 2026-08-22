Установка на Fedora WS (и аналогичные десктопные Linux)

Данная инструкция описывает процесс установки утилиты на десктопные версии Linux (Fedora, Ubuntu, Debian).

## Шаг 1. Требования к системе
1. Linux x86/64 (Fedora, Ubuntu, Debian и т.д.).
2. Docker Engine. На Fedora установите командой: `sudo dnf install docker-ce docker-ce-cli containerd.io`.
3. Доступ к терминалу.
4. Добавьте своего пользователя в группу docker, чтобы не писать sudo каждый раз: `sudo usermod -aG docker $USER` (после этого нужно перелогиниться).

## Шаг 2. Скачивание файлов проекта
Склонируйте репозиторий или скачайте архив и распакуйте его. Перейдите в папку проекта:

cd /home/ваш_пользователь/docker/yadpylesos

## Шаг 3. Создание структуры папок
Создадим рабочие директории:

mkdir -p auth db report vpn backups
chmod 700 auth

## Шаг 4. Создание файла конфигурации
В корне проекта создайте файл `config.yaml`:

cat << 'CFG' > config.yaml
paths:
  db_dir: "/db"
  report_dir: "/report"
  download_dir: "/download"
  auth_dir: "/auth"
  vpn_dir: "/vpn"
CFG

## Шаг 5. Настройка переменных окружения (.env)
В корне проекта создайте файл `.env` с лимитами и настройками (SSOT). Пример:

cat << 'ENV' > .env
TZ=Europe/Moscow
MAX_RUNTIME=21600
LOG_MAX_SIZE=10m
LOG_MAX_FILES=3
MAX_RETRIES=5
CHUNK_SIZE_MB=10
QUEUE_LIMIT=10000
MULTITHREAD_SIZE_MB=100
DOCKER_IMG=yadpylesos-slim
DNS_SERVERS="8.8.8.8 1.1.1.1"
THERMAL_CPU_PATH="/sys/devices/virtual/thermal/thermal_zone0/temp"
BROWSER_CHOICE=1
CACHE_TTL_DAYS=7
TRACE_INTERVAL=60
TELEMETRY_RETENTION_DAYS=30
DB_STATS_LIMIT=20
VPN_CONTAINER_NAME="yadp-vpn"
VPN_NETWORK_NAME="yad-vpn-net"
TEMP_CPU_HIGH=75
TEMP_CPU_LOW=60
ENV

## Шаг 6. Сборка Docker-образа
Соберем образ из предоставленного Dockerfile:

docker build -t yadpylesos-slim -f Dockerfile.yadpylesos-slim .

## Шаг 7. Создание ярлыка для запуска
Чтобы запускать скрипт из любой папки, создадим символическую ссылку в системной папке:

sudo ln -sf /home/ваш_пользователь/docker/yadpylesos/yadpylesos.sh /usr/local/bin/yadpylesos

## Шаг 8. Настройка авторизации и VPN (Необязательно)
*   **Авторизация:** Положите OAuth-токен в `auth/.yad_token`. Включите: `./yadpylesos --auth-enable`.
*   **VPN:** Положите ссылки `vless://` в `vpn/link1.txt`. (Сборка отдельного VPN-образа больше не требуется, используется оригинальный образ mihomo).

## Шаг 9. Запуск
Теперь вы можете запускать скрипт из любой папки:

yadpylesos 'my_folder' 'https://disk.yandex.ru/d/XXXX' '/home/ваш_пользователь/Downloads' 0 -v

*Примечание для десктопа: Скрипт автоматически определит ваш UID и GID, поэтому все скачанные файлы будут принадлежать вам, без необходимости менять права через chmod.*
