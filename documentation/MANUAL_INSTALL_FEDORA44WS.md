# Установка на Fedora WS (и аналогичные десктопные Linux)

Инструкция для установки и использования Я.Д-Пылесос на десктопных Linux (Fedora, Ubuntu, Debian). Пошаговые пояснения — в `MANUAL_INSTALL.md` (NAS-версия); этот документ — краткий маршрут с десктопной спецификой.

## Шаг 1. Требования

1. Linux x86-64 (Fedora, Ubuntu, Debian и т.д.).
2. Docker Engine:
```bash
sudo dnf install docker-ce docker-ce-cli containerd.io
sudo systemctl enable --now docker
sudo usermod -aG docker $USER   # перелогиниться после этого
```

## Шаг 2. Файлы проекта

Клонируйте репозиторий и перейдите в каталог проекта:

```bash
git clone <URL-репозитория> && cd yadpylesos
```

**Артефакты сборки.** Каталог `build_assets/` содержит крупные бинарники и поставляется отдельно (не входит в репозиторий). Скачайте и разложите:

```bash
mkdir -p build_assets
# Xray-core для x86-64 (amd64):
wget https://github.com/XTLS/Xray-core/releases/download/v26.7.28/Xray-linux-64.zip -O build_assets/Xray-linux-64.zip
# bgutil PO-провайдер (исходники сервера):
curl -sL -o /tmp/bgutil-src.tar.gz "https://github.com/Brainicism/bgutil-ytdlp-pot-provider/archive/refs/tags/1.3.2.tar.gz"
tar -xzf /tmp/bgutil-src.tar.gz -C /tmp/
mkdir -p build_assets/bgutil-server
cp -r /tmp/bgutil-ytdlp-pot-provider-1.3.2/server build_assets/bgutil-server
```

Затем собрать серверные зависимости (Node-модули) внутри временного контейнера:

```bash
docker run --rm -v "$PWD/build_assets/bgutil-server/server":/build -w /build node:22-slim \
  sh -c 'npm ci --registry https://registry.npmmirror.com && npx tsc'
```

После этого в `build_assets/bgutil-server/server/build/main.js` должен существовать собранный сервер.

> Версии Xray и bgutil должны совпадать с указанными в Dockerfile (`ARG XRAY_VERSION`, ветка/пин провайдера).

## Шаг 3. Структура каталогов

```bash
mkdir -p auth db report vpn config history backup cache/yt-dlp
chmod 700 auth
```

## Шаг 4. Конфигурация

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

## Шаг 5. Переменные окружения (.env)

Проще всего — сгенерировать шаблон автоматическим установщиком (не собирая образ):

```bash
./install.sh   # создаст .env и структуру каталогов; сборку можно прервать/повторить отдельно
```
Затем отредактируйте `.env` (минимум — `DNS_SERVERS`). Ручной шаблон — в `MANUAL_INSTALL.md` (Шаг 5). Все переменные валидируются при старте.

## Шаг 6. Сборка образа

```bash
docker build -t yadpylesos-slim -f Dockerfile.yadpylesos-slim .
```

Проверка:

```bash
docker run --rm yadpylesos-slim sh -c 'node --version && xray version | head -n 1 && ls /opt/bgutil/build/main.js'
```

## Шаг 7. Ярлык

```bash
sudo ln -sf $(pwd)/yadpylesos.sh /usr/local/bin/yadpylesos
```

## Шаг 8. Авторизация

*   **Яндекс:** OAuth-токен в `auth/.yad_token` + `./yadpylesos --auth-enable`; либо куки `auth/ya.ru.txt`.
*   **YouTube (для 18+ и приватного):** куки из **инкогнито-вкладки** (плагин «Get cookies locally») → `auth/youtube.com.txt`, вкладку закрыть сразу после экспорта. Протокол и признаки протухания — в `USAGE.md`.
*   Статус: `./yadpylesos --auth-status '<ссылка>'`.

## Шаг 9. VPN (необязательно)

Пул серверов — файлы `vpn/link*.txt` (`vless://`, `trojan://`, JSON Xray, включая hysteria). Файлы с префиксом `_` — вне пула. Туннель поднимается автоматически при банах или с флагом `--vpn`. Проверка: `./yadpylesos --vpn-test`.

## Шаг 10. Запуск

```bash
yadpylesos 'my_folder' 'https://disk.yandex.ru/d/XXXX' '/home/ваш_пользователь/Downloads' 0 -v
yadpylesos 'mychannel' 'https://www.youtube.com/@name/videos' '/home/ваш_пользователь/Downloads/yt' 0 -v --notify-tg
```

Скачанные файлы принадлежат вашему пользователю (UID/GID пробрасывается в контейнер автоматически).

## Шаг 11. Обслуживание

*   Логи сессий — `report/<имя>_<дата>.txt`; консольные — `docker logs -f <имя>`.
*   Статистика БД и телеметрия — `yadpylesos --db-stats`; целостность — `--db-check`; сжатие — `--vacuum`.
*   Качество кода — `./check.sh`.

Дальнейшее — в `USAGE.md` (полное руководство: флаги, куки-модель YouTube, VPN, Telegram, антибан).
