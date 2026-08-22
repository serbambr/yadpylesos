Ручная установка Я.Д-Пылесос (YA.D-Pylesos)
 
Данная инструкция предназначена для тех, кто хочет разобраться в процессе установки пошагово, либо не может воспользоваться скриптом автоматической установки (`install.sh`).

## Шаг 1. Требования к системе
Для работы скрипта потребуется:
1. Linux ARM64 (QNAP, Synology, Raspberry Pi) или Linux x86/64 (Ubuntu, Fedora, Debian).
2. Docker. На QNAP необходимо установить приложение Container Station. На обычном Linux — Docker Engine.
3. Доступ к терминалу (SSH) с правами администратора (`sudo`).
4. Доступ в интернет (Docker должен уметь скачивать образы).

## Шаг 2. Скачивание файлов проекта
Если вы читаете этот файл, скорее всего, вы уже скачали архив с GitHub или склонировали репозиторий. 
Перейдите в папку проекта:

cd /путь/к/папке/yadpylesos

## Шаг 3. Создание структуры папок
Скрипту нужны рабочие директории для базы данных, логов, конфигурации VPN и секретов. Создадим их:

mkdir -p auth db report vpn backups
Установим строгие права на папку с секретами (чтобы другие пользователи NAS не могли прочитать токен):

chmod 700 auth

## Шаг 4. Создание файла конфигурации config.yaml
В корне проекта создайте файл `config.yaml` со следующими путями:

cat << 'CFG' > config.yaml
paths:
  db_dir: "/db"
  report_dir: "/report"
  download_dir: "/download"
  auth_dir: "/auth"
  vpn_dir: "/vpn"
CFG

## Шаг 5. Создание файла переменных окружения (.env)
В корне проекта ОБЯЗАТЕЛЬНО нужно создать файл `.env`. В нем хранятся все настройки, пути к Docker-образу и системные лимиты.
Создайте файл `.env` и скопируйте в него базовый шаблон:

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
Скрипт работает в изолированном контейнере. Соберем образ из предоставленного Dockerfile:

sudo docker build -t yadpylesos-slim -f Dockerfile.yadpylesos-slim .

*Примечание: Процесс займет 1-2 минуты (будет скачан базовый образ Python, установлены библиотеки и бинарник mihomo для VPN).*

## Шаг 7. Создание ярлыка для запуска
Чтобы не писать каждый раз `.sh`, создадим символическую ссылку:

ln -s yadpylesos.sh yadpylesos

## Шаг 8. Настройка авторизации (Необязательно)
Если Яндекс блокирует ваш IP или нужно качать закрытые ссылки:
1. Получите OAuth токен на сайте https://oauth.yandex.ru/ (выдайте права "Чтение всего диска" и "Доступ к информации о диске").
2. Создайте файл `auth/.yad_token` и вставьте туда токен.
3. Вы можете использовать cookies. Для этого воспользуйтесь плагином Get-cookies.txt-Locally, назовите файл `cookies.txt` и положите его в папку `auth`.
4. Включите глобальную авторизацию:

./yadpylesos --auth-enable

## Шаг 9. Настройка VPN (Необязательно)
Если вы планируете использовать обходчик блокировок:
1. Создайте в папке `vpn` файлы `link*.txt` (например, `link1.txt`).
2. Вставьте туда ссылки протоколов `vless://` или `trojan://` (по одной на строку).
3. Скрипт автоматически сгенерирует конфиг `config_runtime.yaml` и запустит `mihomo` при срабатывании бана или при использовании флага `--vpn`. (Сборка отдельного VPN-образа больше не требуется).

## Готово!

### Теперь вы можете запустить скрипт:

./yadpylesos 'my_folder' 'https://disk.yandex.ru/d/XXXX' '/share/...' 0 -v
