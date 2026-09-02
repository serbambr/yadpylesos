# Ручная установка Я.Д-Пылесос (YA.D-Pylesos)

Инструкция для тех, кто хочет пройти процесс установки пошагово, либо не может воспользоваться скриптом автоматической установки (`./install.sh`).

## Шаг 1. Требования к системе

1. Linux ARM64 (QNAP, Synology, Raspberry Pi) или Linux x86-64 (Ubuntu, Fedora, Debian).
2. Docker. На QNAP — приложение Container Station; на обычном Linux — Docker Engine.
3. Доступ к терминалу (SSH) с правами администратора (`sudo`).
4. Доступ в интернет для Docker (базовые образы и зависимости ставятся через зеркала; критичные бинарники поставляются в комплекте — см. Шаг 6).

## Шаг 2. Файлы проекта и артефакты сборки

Клонируйте репозиторий и перейдите в каталог проекта:

```bash
git clone <URL-репозитория> && cd yadpylesos
```

**Артефакты сборки.** Каталог `build_assets/` содержит крупные бинарники и поставляется отдельно (не входит в репозиторий). Скачайте и разложите:

```bash
mkdir -p build_assets
# Xray-core для ARM64:
wget https://github.com/XTLS/Xray-core/releases/download/v26.7.28/Xray-linux-arm64-v8a.zip -O build_assets/Xray-linux-arm64-v8a.zip
# bgutil PO-провайдер (исходники сервера):
curl -sL -o /tmp/bgutil-src.tar.gz "https://github.com/Brainicism/bgutil-ytdlp-pot-provider/archive/refs/tags/1.3.2.tar.gz"
tar -xzf /tmp/bgutil-src.tar.gz -C /tmp/
mkdir -p build_assets/bgutil-server
cp -r /tmp/bgutil-ytdlp-pot-provider-1.3.2/server build_assets/bgutil-server
```

Затем собрать серверные зависимости (Node-модули) во временном контейнере:

```bash
docker run --rm -v "$PWD/build_assets/bgutil-server/server":/build -w /build node:22-slim \
  sh -c 'npm ci --registry https://registry.npmmirror.com && npx tsc'
```

После этого в `build_assets/bgutil-server/server/build/main.js` должен существовать собранный сервер.

> Версии Xray и bgutil должны совпадать с указанными в Dockerfile (`ARG XRAY_VERSION`, пин провайдера).

Для x86-64-хостов используется `Xray-linux-64.zip` (см. `MANUAL_INSTALL_FEDORA44WS.md`).

## Шаг 3. Структура каталогов

```bash
mkdir -p auth db report vpn config history backup build_assets cache/yt-dlp
chmod 700 auth
```

Назначение: `auth/` — секреты (700), `db/` — базы ссылок и телеметрия, `report/` — логи и отчёты, `vpn/` — пул серверов, `config/` — конфигурация, `history/` — архив скачанного YouTube, `cache/yt-dlp/` — persistent-кэш решателя JS-челленджей, `build_assets/` — поставляемые бинарники/артефакты.

## Шаг 4. Файл конфигурации config/config.yaml

Файл создаётся **в каталоге config/** (монтируется в контейнер как /config):

```bash
cat << 'CFG' > config/config.yaml
paths:
  db_dir: "/db"
  report_dir: "/report"
  download_dir: "/download"
  auth_dir: "/auth"
  vpn_dir: "/vpn"
CFG
```

## Шаг 5. Файл переменных окружения (.env)

```bash
cat << 'ENV' > .env
TZ=Europe/Moscow
MAX_RUNTIME=21600
LOG_MAX_SIZE=10m
LOG_MAX_FILES=5
MAX_RETRIES=5
CHUNK_SIZE_MB=10
QUEUE_LIMIT=10000
MULTITHREAD_SIZE_MB=100
DOCKER_IMG=yadpylesos-slim
DNS_SERVERS="8.8.8.8 1.1.1.1"
THERMAL_CPU_PATH="/sys/devices/virtual/thermal/thermal_zone0/temp"
BROWSER_CHOICE=1
CACHE_TTL_DAYS=7
XDG_CACHE_HOME=/cache-yt
TRACE_INTERVAL=60
STATUS_LOG_INTERVAL=300
CACHE_SAVE_INTERVAL=300
FAILED_REPORT_INTERVAL=1800
TELEMETRY_RETENTION_DAYS=30
DB_STATS_LIMIT=10
TEMP_CPU_HIGH=75
TEMP_CPU_LOW=60
LOAD_HIGH=4.0
LOAD_LOW=2.0
IO_LATENCY_THRESHOLD_MS=2000
LOG_RETENTION_DAYS=14
VIDEO_ENGINE_UPDATE_DAYS=1
VIDEO_DOWNLOAD_TIMEOUT=300
ANTIBAN_429_SERIES=5
ANTIBAN_PAUSE_SEC=60
PROXY_PORT=10808
ENV
```

Все переменные валидируются при старте: ошибка формата даёт человекочитаемое сообщение, а не traceback.

## Шаг 6. Сборка Docker-образа

```bash
sudo docker build -t yadpylesos-slim -f Dockerfile.yadpylesos-slim .
```

Сборка включает: Python 3.12-slim, Node.js 24 (через NodeSource), Xray-core и bgutil-провайдер из `build_assets/` (получены в Шаге 2), Python-зависимости из requirements.txt (yt-dlp, mutagen, curl_cffi, bgutil-плагин). apt-слои используют зеркало Debian — сборка не зависит от CDN, недоступных из части сетей.

Проверка сборки:

```bash
docker run --rm yadpylesos-slim sh -c 'node --version && xray version | head -n 1 && ls /opt/bgutil/build/main.js'
```

## Шаг 7. Ярлык запуска

```bash
ln -s yadpylesos.sh yadpylesos
```

## Шаг 8. Авторизация

### Яндекс (OAuth — рекомендуется)
1. Получите OAuth-токен на `https://oauth.yandex.ru/` (права: «Чтение всего Диска», «Доступ к информации о Диске»).
2. Сохраните токен в `auth/.yad_token`.
3. Включите: `./yadpylesos --auth-enable`.
Альтернатива — куки Яндекс: экспортируйте плагином «Get cookies.txt-Locally» с сайта disk.yandex.ru в файл `auth/ya.ru.txt`.

### YouTube (cookies — обязательны для 18+ и приватного контента)
1. Откройте **инкогнито-вкладку**, войдите в аккаунт YouTube (аккаунт должен иметь подтверждённый возраст).
2. На открытой вкладке экспортируйте куки плагином «Get cookies locally».
3. **Сразу закройте инкогнито-вкладку** и больше этой сессией не пользуйтесь.
4. Положите файл как `auth/youtube.com.txt`.
5. Проверка: `./yadpylesos --auth-status '<youtube-ссылка>'`.

Дальше файл куки самообслуживается (yt-dlp обновляет его). Признак протухания — Telegram-уведомление «Куки ротированы» или недоступность 18+ → повторить экспорт.

## Шаг 9. VPN (необязательно)

1. Создайте в каталоге `vpn/` файлы `link*.txt` (например, `link1.txt`) — по ссылке `vless://`, `trojan://` или JSON Xray (включая hysteria) на строку/файл.
2. Файлы с префиксом `_` (например, `_link_old.txt`) исключаются из пула — способ «выключить» сервер.
3. Туннель Xray запускается автоматически при банах/блокировках или принудительно с флагом `--vpn`. Порты: SOCKS=`PROXY_PORT`, HTTP=`PROXY_PORT+1` (по умолчанию 10808/10809).
4. Проверка пула: `./yadpylesos --vpn-test` (таблица серверов с IP).

## Готово!

Быстрый тест:

```bash
./yadpylesos 'my_folder' 'https://disk.yandex.ru/d/XXXX' '/share/...' 0 -v
```

Дальше — см. `USAGE.md` (полное руководство) и `README.md` (обзор возможностей). Для проверки качества кода — `./check.sh`.
