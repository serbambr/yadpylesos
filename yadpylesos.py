# ==========================================
# Проект: Я.Д-Пылесос / YA.D-Pylesos
# Скрипт: yadpylesos.py
# Версия: 13.1
# ==========================================

import argparse
import concurrent.futures
import csv
import errno
import glob
import hashlib
import http.cookiejar
import json
import logging
import math
import os
import random
import re
import readline
import shlex
import shutil
import signal
import sqlite3
import sys
import textwrap
import threading
import time
import unicodedata
from collections import deque
from datetime import datetime
from logging.handlers import RotatingFileHandler

import requests
import yaml


# --- ЗАГРУЗКА КОНФИГУРАЦИИ ---
def load_app_config():
    default_config = {'db_dir': '/db', 'report_dir': '/report', 'download_dir': '/download', 'auth_dir': '/auth', 'vpn_dir': '/vpn'}
    try:
        with open('/app/config.yaml', 'r', encoding='utf-8') as f:
            file_cfg = yaml.safe_load(f)
            if file_cfg and 'paths' in file_cfg:
                default_config.update(file_cfg['paths'])
    except (FileNotFoundError, yaml.YAMLError): pass
    return default_config

APP_CONFIG = load_app_config()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG = {
    "CACHE_SAVE_INTERVAL": 300, "STATUS_LOG_INTERVAL": 300, "FAILED_REPORT_INTERVAL": 1800,
    "CHUNK_SIZE": 10 * 1024 * 1024, "API_TIMEOUT": (10, 30), "CDN_TIMEOUT": (15, 60),
    "MAX_RETRIES": 5, "MAX_BAN_WAIT": 300, "INITIAL_BAN_WAIT": 30,
    "MAX_LOG_SIZE": 5 * 1024 * 1024, "MAX_LOG_FILES": 3, "PART_EXT": ".part", "QUEUE_LIMIT": 10000
}

DB_DIR = APP_CONFIG['db_dir']
REPORT_DIR = APP_CONFIG['report_dir']
DOWNLOAD_DIR = APP_CONFIG['download_dir']
AUTH_DIR = APP_CONFIG['auth_dir']
VPN_DIR = APP_CONFIG['vpn_dir']

IGNORE_FILES = [".ds_store", "thumbs.db", "desktop.ini"]

class DiskFullError(Exception):
    """Кастомное исключение для мягкой остановки при нехватке места на диске."""

class Telemetry:
    """Класс для работы с глобальной БД телеметрии (telemetry.db)"""
    def __init__(self, db_path):
        self.db_path = db_path
        self.conn = None
        try:
            self.conn = sqlite3.connect(db_path, check_same_thread=False)
            self._init_db()
            self._cleanup_old_records()
        except sqlite3.Error as e:
            logger.error(f"[ТЕЛЕМЕТРИЯ] Ошибка инициализации: {e}")
            self.conn = None

    def _init_db(self):
        c = self.conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS api_sessions (
            timestamp TEXT, provider TEXT, link TEXT, auth_mode TEXT,
            total_requests INTEGER, total_bans INTEGER, avg_response_time_ms INTEGER,
            peak_cpu_temp REAL, peak_load_avg REAL, peak_io_latency_ms REAL
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS vpn_events (
            timestamp TEXT, protocol TEXT, server_name TEXT, ip_address TEXT, event_type TEXT
        )""")
        self.conn.commit()

    def _cleanup_old_records(self):
        days = int(os.environ.get('TELEMETRY_RETENTION_DAYS', '30'))
        try:
            c = self.conn.cursor()
            c.execute("DELETE FROM api_sessions WHERE timestamp < datetime('now', ?)", (f'-{days} days',))
            c.execute("DELETE FROM vpn_events WHERE timestamp < datetime('now', ?)", (f'-{days} days',))
            self.conn.commit()
        except sqlite3.Error: pass

    def log_vpn_event(self, server_name, ip_address, event_type, protocol="unknown"):
        if not self.conn: return
        try:
            c = self.conn.cursor()
            c.execute("INSERT INTO vpn_events VALUES (datetime('now'), ?, ?, ?, ?)",
                      (protocol, server_name, ip_address, event_type))
            self.conn.commit()
        except sqlite3.Error as e:
            logger.warning(f"[ТЕЛЕМЕТРИЯ] Ошибка записи VPN: {e}")

    def finalize_session(self, app):
        if not self.conn: return
        try:
            c = self.conn.cursor()
            auth_mode = "anonymous"
            if app.yad_token: auth_mode = "oauth"
            elif os.path.exists(app.cookie_file): auth_mode = "cookies"

            avg_api_ms = (app.stats['api_time'] / app.stats['api_req_count'] * 1000) if app.stats['api_req_count'] > 0 else 0

            c.execute("""INSERT INTO api_sessions VALUES (
                datetime('now'), 'yandex', ?, ?, ?, ?, ?, ?, ?, ?
            )""", (
                app.link, auth_mode, app.stats['api_req_count'], app.stats['retries'],
                int(avg_api_ms), app.stats.get('peak_cpu_temp', 0), app.stats.get('peak_load_avg', 0),
                app.stats.get('peak_io_latency_ms', 0)
            ))
            self.conn.commit()
        except sqlite3.Error as e:
            logger.warning(f"[ТЕЛЕМЕТРИЯ] Ошибка записи сессии: {e}")

    def close(self):
        if self.conn:
            self.conn.close()

class DownloadStatus:
    """Статусы файлов в очереди скачивания (State-Transition)"""
    PENDING = 'pending'
    DOWNLOADING = 'downloading'
    DOWNLOADED = 'downloaded'
    FAILED = 'failed'

class Colors:
    GREEN = '\033[0;32m'
    RED = '\033[0;31m'
    YELLOW = '\033[0;33m'
    CYAN = '\033[0;36m'
    NC = '\033[0m'

class ColorFormatter(logging.Formatter):
    def format(self, record):
        msg = super().format(record)
        if record.levelno >= logging.ERROR:
            return f"{Colors.RED}{msg}{Colors.NC}"
        elif record.levelno >= logging.WARNING:
            return f"{Colors.YELLOW}{msg}{Colors.NC}"
        elif record.levelno >= logging.INFO:
            if any(x in msg for x in ["[КЭШ", "[ПРОВЕРЯЕМ", "[ПРИМЕНЕН"]):
                return f"{Colors.CYAN}{msg}{Colors.NC}"
            return f"{Colors.GREEN}{msg}{Colors.NC}"
        return msg

PROFILES = {
    '1': {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36','Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8','Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7','sec-ch-ua': '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"','sec-ch-ua-mobile': '?0','sec-ch-ua-platform': '"Windows"'},
    '2': {'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0','Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8','Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7'},
    '3': {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15','Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8','Accept-Language': 'ru-RU,ru;q=0.9'}
}

class SecretFilter(logging.Filter):
    def filter(self, record):
        record.msg = re.sub(r'(token|access_token|oauth_token|client_secret)=[^\s&]+', r'\\1=[СЕКРЕТ]', str(record.msg))
        return True

logger = logging.getLogger("YaD-Pylesos")

class BaseCloudProvider:
    """Базовый абстрактный класс для облачных провайдеров"""
    def __init__(self, app):
        self.app = app

# ==========================================
# КЛАСС: DatabaseManager
# ==========================================
class DatabaseManager:
    def __init__(self, db_file):
        self.db_file = db_file
        self.conn = None
        self.yandex_cache = {}
        
    def _execute(self, query, params=(), fetch=False, commit=False):
        try:
            c = self.conn.cursor()
            c.execute(query, params)
            if fetch: return c.fetchall()
            if commit: self.conn.commit()
        except sqlite3.DatabaseError as e:
            logger.error(f"[КРИТИЧНО] БД повреждена на лету: {e}. Запрос: {query} | Параметры: {params}")
            try:
                os.makedirs(os.path.join(DB_DIR, 'quarantine'), exist_ok=True)
                q_path = os.path.join(DB_DIR, f"yadpylesos.db.corrupt_{datetime.now().astimezone().strftime('%Y%m%d_%H%M%S')}")
                self.conn.close()
                os.replace(self.db_file, q_path)
                logger.warning(f"[ВНИМАНИЕ] БД перемещена в карантин: {q_path}")
                self.init_db()
                logger.info("[УСПЕХ] БД восстановлена (пустая). Завершение работы для пересоздания кэша.")
                raise SystemExit(1)
            except sqlite3.DatabaseError as ex:
                logger.error(f"[АПОПТОЗ] Не удалось восстановить БД: {ex}")
                raise SystemExit(1)

    def init_db(self, read_only=False):
        try:
            if read_only:
                self.conn = sqlite3.connect(f"file:{self.db_file}?mode=ro", uri=True)
            else:
                self.conn = sqlite3.connect(self.db_file, check_same_thread=False)
                
            c = self.conn.cursor()
            c.execute("PRAGMA busy_timeout = 30000")
            
            if not read_only:
                c.execute("PRAGMA journal_mode=WAL")
                c.execute("PRAGMA synchronous=NORMAL")
                c.execute("CREATE TABLE IF NOT EXISTS local_files (path TEXT PRIMARY KEY, size INTEGER, md5_local TEXT, seen_on_yandex INTEGER DEFAULT 0, mtime INTEGER DEFAULT 0, scanned INTEGER DEFAULT 0)")
                c.execute("CREATE TABLE IF NOT EXISTS download_queue (api_path TEXT PRIMARY KEY, local_path TEXT, yandex_size INTEGER, status TEXT)")
                c.execute("CREATE INDEX IF NOT EXISTS idx_local_path ON local_files(path)")
                c.execute("CREATE INDEX IF NOT EXISTS idx_queue_status ON download_queue(status)")
                c.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
                c.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', '1')")
                
                self._run_db_migrations(c)
                    
                wal_file = self.db_file + "-wal"
                if os.path.exists(wal_file) and os.path.getsize(wal_file) > 100 * 1024 * 1024:
                    logger.warning("[ВНИМАНИЕ] Обнаружен большой WAL-файл. Слияние...")
                    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    logger.info("[УСПЕХ] WAL слит.")
                    
                self.conn.commit()
        except sqlite3.OperationalError as e:
            if "locked" in str(e) or "busy" in str(e):
                print(f"{Colors.RED}[КРИТИЧНО] БД занята другим процессом yadpylesos.{Colors.NC}")
                raise SystemExit(1)
            else:
                print(f"{Colors.RED}[КРИТИЧНО] Ошибка БД: {e}{Colors.NC}")
                raise SystemExit(1)
        except sqlite3.DatabaseError as e:
            logger.error(f"[КРИТИЧНО] БД повреждена: {e}")
            os.makedirs(os.path.join(DB_DIR, 'quarantine'), exist_ok=True)
            os.replace(self.db_file, os.path.join(DB_DIR, f"yadpylesos.db.corrupt_{datetime.now().astimezone().strftime('%Y%m%d_%H%M%S')}"))
            self.init_db()

    def _run_db_migrations(self, c):
        try: c.execute("ALTER TABLE local_files ADD COLUMN seen_on_yandex INTEGER DEFAULT 0")
        except sqlite3.OperationalError: pass 
        try: c.execute("ALTER TABLE local_files ADD COLUMN mtime INTEGER DEFAULT 0")
        except sqlite3.OperationalError: pass
        try: c.execute("ALTER TABLE local_files ADD COLUMN scanned INTEGER DEFAULT 0")
        except sqlite3.OperationalError: pass
                
        c.execute("SELECT value FROM meta WHERE key='schema_version'")
        row = c.fetchone()
        schema_ver = int(row[0]) if row else 1
        if schema_ver < 2:
            c.execute("DROP TABLE IF EXISTS yandex_tree")
            logger.info("[INFO] BL-20: Миграция БД. Таблица yandex_tree пересоздана (привязка к ссылке).")
            c.execute("UPDATE meta SET value='2' WHERE key='schema_version'")
            
        c.execute("CREATE TABLE IF NOT EXISTS yandex_tree (public_key TEXT, path TEXT, revision TEXT, items TEXT, host_dest TEXT, cached_at INTEGER, PRIMARY KEY (public_key, path))")
        c.execute("CREATE INDEX IF NOT EXISTS idx_tree_path ON yandex_tree(path)")

    def upsert_local_file(self, path, size, md5_local=None, seen=0, mtime=0):
        self._execute(
            "INSERT INTO local_files (path, size, md5_local, seen_on_yandex, mtime, scanned) VALUES (?, ?, ?, ?, ?, 1) "
            "ON CONFLICT(path) DO UPDATE SET size=excluded.size, md5_local=COALESCE(excluded.md5_local, md5_local), seen_on_yandex=excluded.seen_on_yandex, mtime=excluded.mtime, scanned=1",
            (path, size, md5_local, seen, mtime), commit=True
        )
    def delete_local_file(self, path): self._execute("DELETE FROM local_files WHERE path=?", (path,), commit=True)
    def add_to_queue(self, api_path, local_path, yandex_size): self._execute("INSERT OR REPLACE INTO download_queue VALUES (?, ?, ?, ?)", (api_path, local_path, yandex_size, DownloadStatus.PENDING), commit=True)
    def mark_as_downloading(self, api_path): self._execute("UPDATE download_queue SET status=? WHERE api_path=?", (DownloadStatus.DOWNLOADING, api_path))
    def mark_as_downloaded(self, api_path): self._execute("UPDATE download_queue SET status=? WHERE api_path=?", (DownloadStatus.DOWNLOADED, api_path))
    def mark_as_failed(self, api_path): self._execute("UPDATE download_queue SET status=? WHERE api_path=?", (DownloadStatus.FAILED, api_path))
    def commit(self):
        try: self.conn.commit()
        except sqlite3.DatabaseError: pass
    def set_md5(self, path, md5): self._execute("UPDATE local_files SET md5_local=? WHERE path=?", (md5, path), commit=True)
    def mark_as_seen(self, path): self._execute("UPDATE local_files SET seen_on_yandex=1 WHERE path=?", (path,))
    def get_file_size(self, path):
        res = self._execute("SELECT size FROM local_files WHERE path=?", (path,), fetch=True)
        return res[0][0] if res else None
    def get_file_info(self, path):
        res = self._execute("SELECT size, mtime FROM local_files WHERE path=?", (path,), fetch=True)
        return res[0] if res else None
    def get_md5(self, path):
        res = self._execute("SELECT md5_local FROM local_files WHERE path=?", (path,), fetch=True)
        return res[0][0] if res else None
    def get_pending_item(self):
        res = self._execute("SELECT api_path, local_path, yandex_size FROM download_queue WHERE status=? LIMIT 1", (DownloadStatus.PENDING,), fetch=True)
        return res[0] if res else None
    def get_queue_count(self, status):
        res = self._execute("SELECT count(*) FROM download_queue WHERE status=?", (status,), fetch=True)
        return res[0][0] if res else 0
    def get_queue_total_size(self):
        res = self._execute("SELECT SUM(yandex_size) FROM download_queue WHERE status=?", (DownloadStatus.PENDING,), fetch=True)
        return res[0][0] if res and res[0][0] else 0
    def get_orphan_files(self): return self._execute("SELECT path, size FROM local_files WHERE seen_on_yandex=0", fetch=True)
    def get_downloading_files(self): return self._execute("SELECT local_path FROM download_queue WHERE status=?", (DownloadStatus.DOWNLOADING,), fetch=True)
    def reset_downloading(self): self._execute("UPDATE download_queue SET status=? WHERE status=?", (DownloadStatus.PENDING, DownloadStatus.DOWNLOADING), commit=True)
    def mark_all_unscanned(self): self._execute("UPDATE local_files SET scanned=0", commit=True)
    def delete_unscanned(self): self._execute("DELETE FROM local_files WHERE scanned=0", commit=True)
    def mark_scanned(self, path): self._execute("UPDATE local_files SET scanned=1 WHERE path=?", (path,))

    def load_global_state(self, app):
        try:
            c = self.conn.cursor()
            c.execute("SELECT value FROM meta WHERE key='optimal_api_pause'")
            row = c.fetchone()
            if row: app.api.current_api_pause = float(row[0])
            c.execute("SELECT value FROM meta WHERE key='best_user_agent'")
            row = c.fetchone()
            if row: app.browser_choice = row[0]
            c.execute("SELECT value FROM meta WHERE key='optimal_cdn_pause'")
            row = c.fetchone()
            if row: app.engine.current_cdn_pause = float(row[0])
            logger.info(f"[INFO] Антихрупкость: Загружен опыт (API: {app.api.current_api_pause:.1f}с, CDN: {app.engine.current_cdn_pause:.1f}с, Профиль: {app.browser_choice}).")
        except (sqlite3.DatabaseError, ValueError): pass

    def save_global_state(self, app):
        try:
            c = self.conn.cursor()
            c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('optimal_api_pause', ?)", (str(app.api.current_api_pause),))
            c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('best_user_agent', ?)", (app.browser_choice,))
            c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('optimal_cdn_pause', ?)", (str(app.engine.current_cdn_pause),))
            self.conn.commit()
        except sqlite3.DatabaseError: pass

    def load_cache(self, app):
        c = self.conn.cursor()
        c.execute("SELECT count(*) FROM yandex_tree WHERE public_key=?", (app.link,))
        count = c.fetchone()[0]
        if count > 0 and not app.refresh_cache:
            logger.info(f"[INFO] Кэш дерева: Найдено {count} папок в SQLite.")
        else:
            if app.refresh_cache: logger.warning("[ВНИМАНИЕ] Режим --refresh-cache. Кэш будет сброшен.")
            else: logger.info("[INFO] Кэш дерева: Не обнаружен. Будет создан новый.")
            
    def save_cache(self, app):
        if app.refresh_cache and self.yandex_cache:
            try:
                c = self.conn.cursor()
                c.execute("DELETE FROM yandex_tree WHERE public_key=?", (app.link,))
                data = [(app.link, path, d.get('revision',''), json.dumps(d.get('items',[])), app.host_dest, int(time.time())) for path, d in self.yandex_cache.items()]
                c.executemany("INSERT INTO yandex_tree (public_key, path, revision, items, host_dest, cached_at) VALUES (?, ?, ?, ?, ?, ?)", data)
                self.conn.commit()
            except (sqlite3.DatabaseError, OSError): pass

# ==========================================
# КЛАСС: TelegramService
# ==========================================
class TelegramService:
    def __init__(self, app):
        self.app = app

    def load_telegram_config(self):
        tg_file = os.path.join(AUTH_DIR, 'tg_config.yaml')
        if not os.path.exists(tg_file): return None
        try:
            with open(tg_file, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f)
                if config and 'telegram' in config:
                    tg = config['telegram']
                    if 'bot_token' in tg and 'chat_id' in tg: return tg
        except (yaml.YAMLError, OSError): pass
        return None

    def _translate_error(self, error_msg):
        err_lower = error_msg.lower()
        rules = [
            ("readonly database", "[БД] Ошибка записи: База данных доступна только для чтения. Проверьте права доступа к папке /db."),
            ("database is locked", "[БД] Ошибка блокировки: База данных занята другим процессом. Возможен параллельный запуск скрипта."),
            ("malformed", "[БД] Критическая ошибка: Файл базы данных поврежден. Требуется удаление и пересоздание."),
            ("unable to open database file", "[БД] Ошибка: Не удается открыть файл базы данных. Проверьте наличие свободных инодов и прав на папку /db."),
            ("no space left", "[Диск] Критическая ошибка: Закончилось свободное место на диске."),
            ("read-only file system", "[Диск] Критическая ошибка: Файловая система доступна только для чтения."),
            ("permission denied", "[Диск] Ошибка доступа: Недостаточно прав для записи файла. Проверьте UID/GID."),
            ("file name too long", "[Диск] Ошибка: Имя файла слишком длинное для файловой системы."),
            ("max retries exceeded", "[Сеть] Ошибка: Превышено количество попыток подключения. Нет связи с интернетом или API Яндекса."),
            ("readtimeout", "[Сеть] Ошибка: Превышено время ожидания ответа от сервера (Timeout)."),
            ("sslerror", "[Сеть] Ошибка SSL-сертификата. Возможно, требуется флаг --ssl-off."),
            ("яндекс изменил api", "[API] Критическая ошибка: Изменилась структура ответа API Яндекса. Требуется обновление скрипта."),
            ("port vpn (7890) not opened", "[VPN] Ошибка: Порт VPN не открылся. Неверный конфигурационный файл или нет рабочих серверов."),
            ("mihomo process crashed", "[VPN] Ошибка: Процесс mihomo упал и не смог восстановиться (Zombie-процесс)."),
            ("vpn tunnel test failed", "[VPN] Ошибка: Туннель поднялся, но тестовый запрос не прошел. Сервер недоступен."),
            ("config file not found", "[Конфиг] Ошибка: Не найден файл config.yaml или tg_config.yaml."),
            ("invalid link", "[Конфиг] Ошибка: Неверный формат ссылки Яндекс.Диска."),
            ("socks support", "[Конфиг] Ошибка: Не установлена библиотека PySocks для работы SOCKS-прокси.")
        ]
        for keyword, translation in rules:
            if keyword in err_lower: return translation
        return f"Неперехваченная ошибка: {error_msg}"

    def _format_telegram_message(self, is_success, error_msg=""):
        status_emoji = "✅" if is_success else "❌"
        status_text = "Успешно" if is_success else "Ошибка"
        error_block = ""
        if not is_success and error_msg:
            error_text = self._translate_error(error_msg)
            error_block = f"\n💥 Причина ошибки:\n{error_text}\n"
            
        stats = self.app.stats
        api_m, api_s = divmod(int(stats['api_time']), 60)
        cdn_m, cdn_s = divmod(int(stats['cdn_time']), 60)
        sl_m, sl_s = divmod(int(stats['sleep_time']), 60)
        
        msg = (
            f"🚀 Я.Д-Пылесос: Завершено\n"
            f"📦 Контейнер: {self.app.container_name}\n"
            f"{status_emoji} Статус: {status_text}\n"
            f"{error_block}"
            f"\n📊 Статистика файлов:\n"
            f"  Увидено: {stats['files_seen']}\n"
            f"  Пропущено: {stats['skipped']}\n"
            f"  Скачано: {stats['downloaded']} ({self.app.human_readable_size(stats['downloaded_bytes'])})\n"
            f"  Ошибок: {stats['errors']}\n\n"
            f"🗑 Карантин:\n"
            f"  Найдено осиротевших: {stats['orphan_total']}\n"
            f"  Перенесено: {stats.get('moved_extra', 0)}\n\n"
            f"🛡 Защита API:\n"
            f"  Срабатываний бана (429): {stats['retries']}\n\n"
            f"⏱ Время:\n"
            f"  API: {api_m}м {api_s}с | CDN: {cdn_m}м {cdn_s}с | Сон: {sl_m}м {sl_s}с"
        )
        return msg

    def send_telegram_notification(self, is_success, error_msg=""):
        tg_config = self.load_telegram_config()
        if not tg_config:
            print(f"{Colors.YELLOW}[ВНИМАНИЕ] Файл auth/tg_config.yaml не найден или некорректен. Уведомление в Telegram отменено.{Colors.NC}")
            return
        message = self._format_telegram_message(is_success, error_msg)
        url = f"https://api.telegram.org/bot{tg_config['bot_token']}/sendMessage"
        payload = {'chat_id': tg_config['chat_id'], 'text': message}
        proxies = {'http': 'socks5h://127.0.0.1:7890', 'https': 'socks5h://127.0.0.1:7890'}
        
        vpn_manager = self.app.vpn_manager
        stop_event = self.app.stop_event
        vpn_started_for_tg = False
        
        if vpn_manager.process is None or vpn_manager.process.poll() is not None:
            logger.info("[INFO] Запуск VPN для отправки уведомления в Telegram...")
            if not vpn_manager.start():
                logger.error("[ОШИБКА] Не удалось запустить VPN для Telegram. Отправка отменена.")
                return
            vpn_started_for_tg = True
            
        tunnel_ok = self._check_tunnel_for_tg(vpn_manager, stop_event)
            
        if not tunnel_ok:
            logger.error("[ОШИБКА] VPN-туннель не поднялся. Отправка в Telegram отменена.")
        else:
            try:
                resp = requests.post(url, json=payload, proxies=proxies, timeout=15)
                if resp.status_code == 200:
                    logger.info("[УСПЕХ] Уведомление в Telegram успешно отправлено.")
                else:
                    logger.error(f"[ОШИБКА] Telegram API вернул ошибку: {resp.status_code}. Тело: {resp.text}")
            except requests.exceptions.RequestException as e:
                logger.error(f"[ОШИБКА] Сбой сети при отправке в Telegram: {e}")
        
        if vpn_started_for_tg:
            vpn_manager.stop()

    def _check_tunnel_for_tg(self, vpn_manager, stop_event):
        for attempt in range(3):
            logger.info(f"[INFO] Проверка VPN-туннеля (попытка {attempt+1}/3)...")
            if vpn_manager.test_tunnel():
                logger.info("[УСПЕХ] VPN-туннель протестирован.")
                return True
            logger.warning("[ВНИМАНИЕ] VPN-туннель не отвечает. Ожидание 5 сек...")
            stop_event.wait(timeout=5)
        return False

# ==========================================
# КЛАСС: ScannerAgent
# ==========================================
class ScannerAgent:
    def __init__(self, app):
        self.app = app

    def _scan_file_entry(self, entry):
        try:
            stat = entry.stat()
            sz = stat.st_size
            mtime = stat.st_mtime_ns
            file_info = self.app.db.get_file_info(entry.path)
            if file_info and file_info[0] == sz and file_info[1] == mtime:
                self.app.db.mark_scanned(entry.path)
            else:
                self.app.db.upsert_local_file(entry.path, sz, mtime=mtime)
            if CONFIG["PART_EXT"] in entry.name: return 1, 0
            return 0, sz
        except OSError as e:
            logger.warning(f"[ВНИМАНИЕ] Нет прав на чтение или битый симлинк: {entry.path} ({e})")
            return 0, 0

    def pre_scan(self, dest):
        # НЕ обнуляем total_files, чтобы сохранить значение, переданное пользователем.
        # Мы возьмем max(пользовательское_значение, локальные_файлы) в конце сканирования.
        logger.info("=== ПРЕДВАРИТЕЛЬНОЕ СКАНИРОВАНИЕ ДИСКА (ИНКРЕМЕНТАЛЬНОЕ) ===")
        local_files, local_dirs, local_size, part_files = 0, 0, 0, 0
        
        self.app.db.mark_all_unscanned()
        
        if os.path.exists(dest):
            dirs_to_scan = [dest]
            while dirs_to_scan:
                current_dir = dirs_to_scan.pop()
                local_dirs += 1
                try:
                    with os.scandir(current_dir) as it:
                        for entry in it:
                            if entry.is_dir(follow_symlinks=False):
                                dirs_to_scan.append(entry.path)
                            elif entry.is_file(follow_symlinks=False):
                                local_files += 1
                                p_add, sz_add = self._scan_file_entry(entry)
                                part_files += p_add
                                local_size += sz_add
                except OSError: pass
            
        self.app.db.delete_unscanned()
        self.app.db.commit()

        self.app.total_files = max(self.app.total_files, local_files)
        logger.info(f"Локальных папок: {local_dirs}")
        logger.info(f"Локальных файлов: {local_files} (включая {part_files} *.part)")
        logger.info(f"Локальный объем: {self.app.human_readable_size(local_size)}")
        logger.info("========================================")

    def _handle_zero_byte_file(self, p, local_size, safe_name):
        if local_size > 0:
            logger.warning(f"[ВНИМАНИЕ] 📄 '{safe_name}' на диске {local_size}Б, а на Яндексе 0Б. Сохраняем локальный.", show_progress=False)
            try: mtime = os.path.getmtime(p)
            except OSError: mtime = 0
            self.app.db.upsert_local_file(p, local_size, seen=1, mtime=mtime)
            return
        if local_size != 0:
            try:
                with open(p, 'w'): pass
                self.app.chown_file(p)
                try: mtime = os.path.getmtime(p)
                except OSError: mtime = 0
                self.app.db.upsert_local_file(p, 0, seen=1, mtime=mtime)
                self.app.verbose_log(f"[СОЗДАН ПУСТОЙ ФАЙЛ] 📄 '{safe_name}'", show_progress=False)
                with self.app.stats_lock: self.app.stats['zero_byte_created'] += 1
            except OSError as e:
                self.app.check_apoptosis(e)
                with self.app.stats_lock: self.app.stats['errors'] += 1

    def process_item(self, item, public_key, local_dest):
        mapped = self.app.api.map_yandex_item(item)
        safe_name = self.app.sanitize_filename(mapped['name'])
        p = os.path.join(local_dest, safe_name)
        try:
            if mapped['type'] == 'file':
                with self.app.stats_lock: self.app.stats['files_seen'] += 1
                self.app.db.mark_as_seen(p)
                sz = mapped['size']
                if sz == 0:
                    local_size = os.path.getsize(p) if os.path.exists(p) else -1
                    self._handle_zero_byte_file(p, local_size, safe_name)
                    return None
                if self.app.db.get_file_size(p) == sz:
                    with self.app.stats_lock: self.app.stats['skipped'] += 1
                    self.app.verbose_log(f"[ПРОПУСК] 📄 '{safe_name}' (уже скачан)", show_progress=True)
                    return None
                self._add_file_to_queue(mapped, p, sz, safe_name)
            elif mapped['type'] == 'dir':
                with self.app.stats_lock: self.app.stats['dirs_seen'] += 1
                self.app.verbose_log(f"[ОБХОД] 📁 {safe_name}", show_progress=True)
                return (public_key, mapped['api_path'], p)
        except (ValueError, KeyError, OSError):
            with self.app.stats_lock: self.app.stats['errors'] += 1
        return None

    def _add_file_to_queue(self, mapped, p, sz, safe_name):
        if self.app.db.get_queue_count('pending') >= CONFIG["QUEUE_LIMIT"]:
            logger.warning(f"[ВНИМАНИЕ] Очередь достигла лимита ({CONFIG['QUEUE_LIMIT']}). Пауза Фазы 2 на 60 сек.")
            self.app.stop_event.wait(timeout=60)
        with self.app.stats_lock: self.app.stats['queued_bytes'] += sz
        self.app.verbose_log(f"[В ОЧЕРЕДЬ] 📄 '{safe_name}' [{self.app.human_readable_size(sz)}]", show_progress=True)
        self.app.db.add_to_queue(mapped['api_path'], p, sz)

    def _process_single_orphan(self, path, size, dest, counts):
        if self.app.move_extra_path:
            try: 
                target_path = os.path.join(self.app.move_extra_path, os.path.relpath(path, dest))
                os.makedirs(os.path.dirname(target_path), exist_ok=True)
                if os.path.exists(target_path):
                    base, ext = os.path.splitext(target_path)
                    counter = 1
                    while os.path.exists(f"{base}_{counter}{ext}"): counter += 1
                    target_path = f"{base}_{counter}{ext}"
                    logger.warning(f"  -> [ВНИМАНИЕ] Файл в карантине уже существует. Переименован в: {os.path.basename(target_path)}")
                shutil.move(path, target_path)
                counts['moved'] += 1
                counts['moved_size'] += size
                with self.app.stats_lock: self.app.stats['moved_extra'] = counts['moved']
            except (OSError, shutil.Error) as e:
                logger.error(f"  -> [ОШИБКА ПЕРЕНОСА] 📄 '{os.path.basename(path)}': {e}")
        elif size == 0:
            try: 
                os.remove(path)
                counts['deleted'] += 1
                with self.app.stats_lock: self.app.stats['zero_byte_deleted'] = counts['deleted']
            except OSError as e:
                logger.error(f"  -> [ОШИБКА УДАЛЕНИЯ] 🗑 '{os.path.basename(path)}': {e}")
        self.app.db.delete_local_file(path)

    def process_orphan_files(self, dest):
        logger.info("=== Проверка осиротевших файлов ===")
        orphan_files = []
        for path, size in self.app.db.get_orphan_files():
            base_name = os.path.basename(path)
            if CONFIG["PART_EXT"] not in base_name and base_name.lower() not in IGNORE_FILES:
                orphan_files.append((path, size))
        total_orphan_count = len(orphan_files)
        total_orphan_size = sum(s for p, s in orphan_files)
        with self.app.stats_lock: self.app.stats['orphan_total'] = total_orphan_count
        if total_orphan_count == 0:
            logger.info("[INFO] Осиротевших файлов не найдено.")
            return
        counts = {'moved': 0, 'moved_size': 0, 'deleted': 0}
        for path, size in orphan_files:
            self._process_single_orphan(path, size, dest, counts)
        self.app.db.commit()
        if self.app.move_extra_path:
            logger.info(f"[INFO] Карантин: всего {total_orphan_count} файлов ({self.app.human_readable_size(total_orphan_size)}). Успешно перенесено: {counts['moved']} ({self.app.human_readable_size(counts['moved_size'])}).")

    def orbital_garbage_collector(self, dest):
        self.app.log("=== Орбитальная очистка (Удаление блуждающих .part) ===")
        cleaned_count = 0
        c = self.app.db.conn.cursor()
        c.execute("SELECT local_path FROM download_queue WHERE status='downloading'")
        active_downloads = {row[0] for row in c.fetchall()}
        
        def scan_for_junk(path):
            nonlocal cleaned_count
            try:
                with os.scandir(path) as it:
                    for entry in it:
                        if entry.is_dir(follow_symlinks=False): scan_for_junk(entry.path)
                        elif entry.is_file(follow_symlinks=False) and CONFIG["PART_EXT"] in entry.name:
                            base_path = entry.path.split(CONFIG["PART_EXT"])[0]
                            if base_path not in active_downloads:
                                os.remove(entry.path)
                                cleaned_count += 1
            except OSError: pass
        if os.path.exists(dest): scan_for_junk(dest)
        if cleaned_count > 0: self.app.log(f"[УСПЕХ] Удалено блуждающих .part файлов: {cleaned_count}")

    def cleanup_interrupted_files(self):
        interrupted = self.app.db.get_downloading_files()
        if not interrupted: return
        logger.warning(f"[ВНИМАНИЕ] Обнаружено {len(interrupted)} прерванных файлов. Проверка...")
        for row in interrupted:
            p_path = row[0] + CONFIG["PART_EXT"]
            cleaned_mt = False
            for i in range(self.app.num_threads):
                if os.path.exists(f"{p_path}.{i}"): os.remove(f"{p_path}.{i}"); cleaned_mt = True
            if cleaned_mt and os.path.exists(p_path): os.remove(p_path)
            if os.path.exists(p_path + '.gluing'):
                logger.warning(f"  -> [ВНИМАНИЕ] Найден прерванный маркер склейки. Удаляем {p_path}")
                os.remove(p_path + '.gluing')
                if os.path.exists(p_path): os.remove(p_path)
        self.app.db.reset_downloading()
        logger.info("[УСПЕХ] Статусы сброшены.")

    def _is_cache_fresh(self, row, public_key, cp, api_url):
        if not row: return False
        rev, items_str, cached_at = row
        cache_ttl = int(os.environ.get('CACHE_TTL_DAYS', '7')) * 86400
        if (time.time() - (cached_at or 0)) >= cache_ttl: return False
        try:
            check_resp = self.app.api.session_api.get(api_url, params={"public_key": public_key, "limit": 1, "path": cp}, timeout=CONFIG["API_TIMEOUT"]).json()
            if check_resp.get('revision') == rev: return json.loads(items_str)
        except (requests.exceptions.RequestException, json.JSONDecodeError): pass
        return False

    def process_folder(self, public_key, cp, ld, api_url):
        try: os.makedirs(ld, exist_ok=True); self.app.chown_file(ld)
        except OSError: return None, None
        c = self.app.db.conn.cursor()
        c.execute("SELECT revision, items, cached_at FROM yandex_tree WHERE public_key=? AND path=?", (self.app.link, cp))
        row = c.fetchone()
        items = self._is_cache_fresh(row, public_key, cp, api_url)
        if items: return items, True
        items, rev = self.app.api.fetch_api_items(public_key, cp, api_url)
        if items is None: return None, None
        if rev:
            c.execute("INSERT OR REPLACE INTO yandex_tree (public_key, path, revision, items, host_dest, cached_at) VALUES (?, ?, ?, ?, ?, ?)",
                      (self.app.link, cp, rev, json.dumps(items), self.app.host_dest, int(time.time())))
            self.app.db.conn.commit()
        return items, False

    def refresh_tree_cache(self, public_key, dest="/download"):
        self.app.set_status("[СТАТУС] Запрос списка файлов и папок у API Яндекс")
        queue = deque([(public_key, "", dest)]) 
        api_url = "https://cloud-api.yandex.net/v1/disk/public/resources"
        while queue:
            self.app.check_status()
            pk, cp, ld = queue.popleft()
            try: os.makedirs(ld, exist_ok=True); self.app.chown_file(ld)
            except OSError: continue
            all_items, current_rev = self.app.api.fetch_api_items(pk, cp, api_url)
            if all_items is None: return None
            if current_rev: self.app.db.yandex_cache[cp] = {'revision': current_rev, 'items': all_items}
            for item in all_items:
                if item['type'] == 'dir': queue.append((pk, item['path'], os.path.join(ld, self.app.sanitize_filename(item['name']))))
                else: 
                    with self.app.stats_lock: self.app.stats['files_seen'] += 1
            self.app.stop_event.wait(timeout=max(1.0, random.gauss(self.app.api.current_api_pause, 1.0)))
        self.app.db.save_cache(self.app)
        self.app.log("[INFO] Принудительное обновление кэша завершено.")
        return True

    def phase_1_and_2_build_queue(self, public_key, dest="/download"):
        self.app.log("=== СТУПЕНЬ 1: Загрузка кэша дерева ===")
        self.app.db.load_cache(self.app)
        if self.app.refresh_cache:
            if self.refresh_tree_cache(public_key, dest) is None: return None
            return True
        self.pre_scan(dest)
        self.app.log("=== СТУПЕНЬ 2: Сверка файлов и построение очереди ===")
        queue = deque([(public_key, "", dest)])
        api_url = "https://cloud-api.yandex.net/v1/disk/public/resources"
        while queue:
            self.app.check_status()
            pk, cp, ld = queue.popleft()
            items, use_cache = self.process_folder(pk, cp, ld, api_url)
            if items is None: return None
            for item in items:
                self.app.check_status()
                nxt = self.process_item(item, pk, ld)
                if nxt: queue.append(nxt)
            if not use_cache:
                t = time.time()
                time.sleep(max(1.0, random.gauss(self.app.api.current_api_pause, 1.0)))
                with self.app.stats_lock: self.app.stats['sleep_time'] += time.time() - t
        self.process_orphan_files(dest)
        self.app.db.save_cache(self.app)
        return True
        
# ==========================================
# КЛАСС: DownloadEngine
# ==========================================
class DownloadEngine:
    def __init__(self, app):
        self.app = app
        self.api_lock = threading.Lock()
        self.current_cdn_pause = 1.0
        self.MIN_CDN_PAUSE, self.MAX_CDN_PAUSE = 0.5, 10.0
        self.consecutive_failures = 0

    def _handle_fs_error(self, e):
        err = e.errno
        if err == errno.ENOSPC: return 'disk_full'
        if err in (errno.EROFS, errno.EFBIG, errno.EDQUOT): return 'apoptosis'
        if err in (errno.EBUSY, errno.EAGAIN, errno.EWOULDBLOCK): return 'retry'
        return 'fail'

    def _safe_write(self, f, chunk):
        for attempt in range(3):
            try:
                t_io = time.time()
                f.write(chunk)
                io_lat = (time.time() - t_io) * 1000
                with self.app.stats_lock:
                    self.app.stats['io_latency_ms'] = io_lat
                    self.app.stats['peak_io_latency_ms'] = max(self.app.stats['peak_io_latency_ms'], io_lat)
                return True
            except OSError as e:
                action = self._handle_fs_error(e)
                if action == 'disk_full':
                    raise DiskFullError(str(e))
                if action == 'apoptosis':
                    self.app.check_apoptosis(e)
                    return False
                if action == 'retry':
                    self.app.log(f"  -> [ВНИМАНИЕ] Файл заблокирован. Повтор {attempt+1}/3...", show_progress=True)
                    self.app.stop_event.wait(timeout=2 * (attempt + 1))
                    if attempt == 2: return False
                else:
                    return False
        return False

    def _log_download_speed(self, dest, curr, yandex_size, t_diff, threads_str):
        if t_diff <= 0: return
        speed = curr / t_diff
        eta_sec = (yandex_size - curr) / speed if speed > 0 else 0
        eta_str = f"{int(eta_sec // 60)}м {int(eta_sec % 60)}с" if eta_sec > 0 else "0с"
        self.app.log(f"[СТАТУС] Скачивание 📄 '{os.path.basename(dest)}' ({self.app.human_readable_size(curr)} из {self.app.human_readable_size(yandex_size)} | Скорость: {self.app.human_readable_size(speed)}/с | Осталось: {eta_str}) [{threads_str}]", show_progress=True)

    def _write_worker_chunk(self, f, chunk, state):
        if not self._safe_write(f, chunk):
            state['error'] = True
            state['error_msg'] = "Filesystem error or file locked"
            return False
        with state['lock']:
            state['downloaded'] += len(chunk)
        return True

    def _fetch_worker_chunks(self, url, p_path, start, end, f, state):
        curr = start
        retries = 0
        while curr <= end:
            if state['error']: 
                os.remove(p_path)
                return False
            headers = {'Range': f'bytes={curr}-{min(curr + CONFIG["CHUNK_SIZE"] - 1, end)}'}
            try:
                with self.app.api.session_cdn.get(url, headers=headers, stream=True, timeout=CONFIG["CDN_TIMEOUT"]) as r:
                    r.raise_for_status()
                    for chunk in r.iter_content(chunk_size=1024*1024):
                        if not chunk: continue
                        if not self._write_worker_chunk(f, chunk, state):
                            os.remove(p_path)
                            return False
                        curr += len(chunk)
            except requests.exceptions.RequestException as e:
                retries += 1
                if retries >= CONFIG["MAX_RETRIES"]: 
                    state['error']=True; state['error_msg']=str(e); os.remove(p_path); return False
                self.current_cdn_pause = min(self.MAX_CDN_PAUSE, self.current_cdn_pause * 1.5)
                logger.warning(f"Адаптивный CDN: Обрыв связи. Увеличение паузы до {self.current_cdn_pause:.1f}с.")
                self.app.stop_event.wait(timeout=self.current_cdn_pause)
        return True

    def download_worker(self, url, p_path, start, end, _tid, state):
        try:
            with open(p_path, 'wb') as f:
                if not self._fetch_worker_chunks(url, p_path, start, end, f, state):
                    return False
            self.current_cdn_pause = max(self.MIN_CDN_PAUSE, self.current_cdn_pause - 0.1)
            return True
        except OSError as e:
            state['error']=True; state['error_msg']=str(e); return False
        finally:
            if 'chunk_done_event' in state: state['chunk_done_event'].set()

    def cleanup_mt_parts(self, part_path):
        if os.path.exists(part_path): os.remove(part_path)
        for i in range(self.app.num_threads):
            if os.path.exists(f"{part_path}.{i}"): os.remove(f"{part_path}.{i}")

    def glue_parts(self, part_path, actual_threads):
        marker = part_path + '.gluing'
        if actual_threads <= 0: raise ValueError("Контракт нарушен: actual_threads <= 0")
        try:
            with open(marker, 'w') as f: f.write('gluing')
            with open(part_path, 'wb') as f_out:
                for i in range(actual_threads):
                    p = f"{part_path}.{i}"
                    with open(p, 'rb') as f_in: shutil.copyfileobj(f_in, f_out, length=CONFIG["CHUNK_SIZE"])
                    os.remove(p)
            os.remove(marker)
            if not os.path.exists(part_path): raise ValueError("Контракт нарушен: склеенный файл не существует")
            return True
        except OSError:
            if os.path.exists(part_path): os.remove(part_path)
            if os.path.exists(marker): os.remove(marker)
            for i in range(actual_threads):
                if os.path.exists(f"{part_path}.{i}"): os.remove(f"{part_path}.{i}")
            return False

    def safe_rename(self, src, dst):
        for attempt in range(3):
            try:
                os.replace(src, dst)
                return True
            except OSError as e:
                if self._handle_fs_error(e) == 'retry':
                    self.app.log(f"  -> [ВНИМАНИЕ] Файл заблокирован. Повтор {attempt+1}/3...", show_progress=True)
                    self.app.stop_event.wait(timeout=2 * (attempt + 1))
                else:
                    return False
        return False

    def _validate_mt_parts(self, p_path, actual, yandex_size):
        total_size = 0
        for i in range(actual):
            p = f"{p_path}.{i}"
            if not os.path.exists(p): return False
            total_size += os.path.getsize(p)
        return total_size == yandex_size

    def write_single_chunks(self, r, p_path, mode, existing, dest, yandex_size):
        try:
            with open(p_path, mode) as f:
                written = 0
                t_chunk = time.time()
                for chunk in r.iter_content(chunk_size=CONFIG["CHUNK_SIZE"]):
                    if chunk:
                        if not self._safe_write(f, chunk): return False
                        written += len(chunk)
                        curr_downloaded = existing + written
                        
                        # Обновляем статистику на лету, чтобы trace-status видел реальную скорость и прогресс файла
                        with self.app.stats_lock:
                            self.app.stats['downloaded_bytes'] += len(chunk)
                            self.app.stats['current_file_downloaded'] += len(chunk)
                        
                        if time.time() - t_chunk > CONFIG["STATUS_LOG_INTERVAL"]:
                            self._log_download_speed(dest, curr_downloaded, yandex_size, time.time() - t_chunk, "1 поток")
                            t_chunk = time.time()
                            
            return self._validate_chunk_size(p_path, yandex_size)
        except OSError as e:
            if self._handle_fs_error(e) == 'apoptosis': self.app.check_apoptosis(e)
            if os.path.exists(p_path): os.remove(p_path)
            return False

    def _validate_chunk_size(self, p_path, yandex_size):
        if yandex_size > 0 and os.path.getsize(p_path) != yandex_size:
            os.remove(p_path)
            return False
        return True

    def _handle_single_download_errors(self, e, dest):
        if isinstance(e, requests.exceptions.ReadTimeout):
            logger.error(f"  -> [ОШИБКА: TIMEOUT] 📄 '{os.path.basename(dest)}'")
        elif isinstance(e, OSError):
            self.app.check_apoptosis(e)
            logger.error(f"  -> [ОШИБКА: ОС] 📄 '{os.path.basename(dest)}': {e}")
        else:
            logger.error(f"  -> [ОШИБКА: UNKNOWN] 📄 '{os.path.basename(dest)}': {e}")

    def _prepare_single_download(self, dest, p_path, yandex_size):
        if os.path.exists(dest) and yandex_size > 0 and os.path.getsize(dest) == yandex_size:
            logger.info(f"  -> [ПРОПУСК] 📄 '{os.path.basename(dest)}' (уже существует)")
            self.app.db.upsert_local_file(dest, yandex_size)
            return None, None
        existing = self.app.db.get_file_size(p_path) or 0
        if existing > 0 and yandex_size > 0 and existing > yandex_size:
            logger.warning("  -> [БИТЫЙ .part] 🗑 Удаляем")
            os.remove(p_path)
            existing = 0
        headers = {'Range': f'bytes={existing}-'} if existing > 0 else {}
        return headers, existing

    def _prepare_mt_state(self, yandex_size):
        actual = min(self.app.num_threads, yandex_size // (10 * 1024 * 1024))
        actual = max(actual, 1)
        state = {'downloaded': 0, 'error': False, 'error_msg': '', 'lock': threading.Lock(), 'chunk_done_event': threading.Event()}
        return actual, state

    def _finalize_mt_download(self, p_path, dest, yandex_size, actual):
        if not self._validate_mt_parts(p_path, actual, yandex_size):
            logger.warning("  -> [БИТЫЙ .part] 🗑 Размеры кусков не совпадают. Удаляем")
            self.cleanup_mt_parts(p_path)
            return False
        if not self.glue_parts(p_path, actual): return False
        if os.path.getsize(p_path) != yandex_size:
            os.remove(p_path)
            return False
        if not self.safe_rename(p_path, dest): return False
        self.app.chown_file(dest)
        if not (os.path.exists(dest) and os.path.getsize(dest) == yandex_size):
            raise ValueError("Контракт нарушен: итоговый файл неверного размера")
        return True

    def _finalize_single_download(self, p_path, dest, yandex_size):
        if not self.safe_rename(p_path, dest): return False
        self.app.chown_file(dest)
        if yandex_size > 0 and not (os.path.exists(dest) and os.path.getsize(dest) == yandex_size):
            raise ValueError("Контракт нарушен: итоговый файл неверного размера")
        return True

    def download_file_single(self, url, dest, yandex_size):
        if yandex_size < 0: raise ValueError("Контракт нарушен: yandex_size < 0")
        p_path = dest + CONFIG["PART_EXT"]
        headers, existing = self._prepare_single_download(dest, p_path, yandex_size)
        if headers is None: return True
        try:
            with self.app.api.session_cdn.get(url, headers=headers, stream=True, timeout=CONFIG["CDN_TIMEOUT"]) as r:
                r.raise_for_status()
                mode = 'ab' if r.status_code == 206 and existing > 0 else 'wb'
                if not self.write_single_chunks(r, p_path, mode, existing, dest, yandex_size): return False
                return self._finalize_single_download(p_path, dest, yandex_size)
        except (requests.exceptions.RequestException, OSError, ValueError) as e:
            self._handle_single_download_errors(e, dest)
            return False

    def monitor_threads(self, threads, state, dest, yandex_size):
        last_seen = 0
        local_timer = time.time()
        while any(t.is_alive() for t in threads):
            if state['chunk_done_event'].wait(timeout=5):
                state['chunk_done_event'].clear()
                if not any(t.is_alive() for t in threads): break
            with state['lock']: curr = state['downloaded']
            with self.app.stats_lock: self.app.stats['downloaded_bytes'] += (curr - last_seen)
            last_seen = curr
            if time.time() - local_timer > CONFIG["STATUS_LOG_INTERVAL"]:
                self._log_download_speed(dest, curr, yandex_size, time.time() - local_timer, f"{len(threads)} потока")
                local_timer = time.time()
        for t in threads: t.join()
        with state['lock']: curr = state['downloaded']
        with self.app.stats_lock: self.app.stats['downloaded_bytes'] += (curr - last_seen)

    def download_file_multithreaded(self, url, dest, yandex_size):
        if yandex_size <= 0: raise ValueError("Контракт нарушен: yandex_size <= 0")
        p_path = dest + CONFIG["PART_EXT"]
        marker_path = p_path + '.gluing'
        if os.path.exists(marker_path):
            logger.warning("  -> [ВНИМАНИЕ] Прерванная склейка. Удаляем .part")
            self.cleanup_mt_parts(p_path)
            if os.path.exists(marker_path): os.remove(marker_path)
        self.cleanup_mt_parts(p_path)
        actual, state = self._prepare_mt_state(yandex_size)
        chunk = yandex_size // actual
        threads = []
        for i in range(actual):
            start = i * chunk
            end = yandex_size - 1 if i == actual - 1 else (start + chunk - 1)
            t = threading.Thread(target=self.download_worker, args=(url, f"{p_path}.{i}", start, end, i, state))
            threads.append(t); t.start()
        self.monitor_threads(threads, state, dest, yandex_size)
        if state['error']:
            logger.error(f"  -> [ОШИБКА МНОГОПОТОЧНОСТИ] {state['error_msg']}. Удаляем .part*")
            self.cleanup_mt_parts(p_path)
            if "No space left" in state.get('error_msg', ''):
                self.app.db.save_cache(self.app)
                raise SystemExit(1)
            return False
        return self._finalize_mt_download(p_path, dest, yandex_size, actual)

    def _execute_download_with_retry(self, func, href, local_path, yandex_size, api_url, dl_params, public_key):
        for attempt in range(CONFIG["MAX_RETRIES"]):
            t_cdn = time.time()
            if func(href, local_path, yandex_size):
                with self.app.stats_lock: self.app.stats['cdn_time'] += time.time() - t_cdn
                return 'downloaded'
            with self.app.stats_lock: self.app.stats['cdn_time'] += time.time() - t_cdn
            if attempt < CONFIG["MAX_RETRIES"] - 1:
                self.app.stop_event.wait(timeout=max(2, random.gauss(5, 2)))
                with self.api_lock:
                    dl_json = self.app.api.fetch_yandex_api(f"{api_url}/download", dl_params)
                if dl_json and dl_json.get('href'): href = dl_json.get('href')
                else: break
        logger.error(f"  -> [ПРОВАЛ] {CONFIG['MAX_RETRIES']} попыток исчерпано для '{os.path.basename(local_path)}'")
        return 'failed'

    def process_download_attempt(self, api_path, local_path, yandex_size, public_key, slot_num, total_slots):
        safe_name = os.path.basename(local_path)
        api_url = "https://cloud-api.yandex.net/v1/disk/public/resources"
        dl_params = {"public_key": public_key, "path": api_path}
        with self.api_lock:
            dl_json = self.app.api.fetch_yandex_api(f"{api_url}/download", dl_params)
        if not dl_json or not dl_json.get('href'):
            self.app.log(f"  -> [ОШИБКА] Файл недоступен: {dl_json.get('message', '') if dl_json else 'Нет ответа'}", show_progress=True)
            return 'failed', api_path, local_path, yandex_size
        href = dl_json.get('href')
        t_num = 1
        if yandex_size >= self.app.multithread_size and self.app.num_threads > 1:
            t_num = min(self.app.num_threads, yandex_size // (10 * 1024 * 1024))
        t_str = self.app.pluralize(t_num, "поток", "потока", "потоков")
        
        with self.app.stats_lock:
            self.app.stats['downloading'] += 1
            self.app.stats['in_progress_bytes'] += yandex_size
            # Добавляем счетчики для отображения текущего файла в --trace-status
            self.app.stats['current_file_total'] = yandex_size
            self.app.stats['current_file_downloaded'] = 0
            
        self.app.log(f"[{slot_num}/{total_slots}] [СКАЧИВАНИЕ] 📄 '{safe_name}' [{self.app.human_readable_size(yandex_size)} | Скорость: 0.0 МБ/с | Осталось: ~ | {t_str}]", show_progress=True)
        
        use_mt = yandex_size >= self.app.multithread_size and self.app.num_threads > 1
        func = self.download_file_multithreaded if use_mt else self.download_file_single
        status = self._execute_download_with_retry(func, href, local_path, yandex_size, api_url, dl_params, public_key)
        return status, api_path, local_path, yandex_size

    def _process_future_result(self, status, api_path, local_path, yandex_size):
        with self.app.stats_lock:
            if status == DownloadStatus.DOWNLOADED:
                self.app.stats['downloaded'] += 1
                self.consecutive_failures = 0
            else:
                self.app.stats['errors'] += 1
                self.consecutive_failures += 1

        if self.consecutive_failures >= 5:
            logger.error(f"{Colors.RED}[JIDOKA] 5 файлов подряд скачаны с ошибкой. Остановка конвейера для защиты от циклов.{Colors.NC}")
            return True # Сигнал к остановке

        if status == DownloadStatus.DOWNLOADED:
            self.app.db.mark_as_downloaded(api_path)
            try: mtime = os.path.getmtime(local_path)
            except OSError: mtime = 0
            self.app.db.upsert_local_file(local_path, yandex_size, mtime=mtime)
            if self.app.stats['downloaded'] % 50 == 0: self.app.db.commit()
        else:
            self.app.db.mark_as_failed(api_path)
            self.app.failed_downloads.append({'api_path': api_path, 'local_path': local_path})
        self.app.db.commit()
        self.app.adaptive_pause(yandex_size)
        return False

    def _handle_future_result(self, future, api_path, futures):
        try:
            status, _, local_path, yandex_size = future.result()
        except DiskFullError:
            self.app.log(f"{Colors.RED}[КРИТИЧНО] На диске закончилось место. Остановка конвейера для защиты данных.{Colors.NC}", show_progress=True)
            self.app.db.reset_downloading()
            for f in futures: f.cancel()
            return True
        except Exception as e:  # noqa: BLE001
            self.app.log(f"[КРИТИЧНО] Поток скачивания упал с ошибкой: {e}", show_progress=True)
            with self.app.stats_lock: self.app.stats['errors'] += 1
            return False
        # Jidoka: Остановка конвейера, если _process_future_result вернет True
        return self._process_future_result(status, api_path, local_path, yandex_size)

    def phase_3_download_queue(self, public_key):
        self.app.total_queue_count = self.app.db.get_queue_count(DownloadStatus.PENDING)
        total_size = self.app.db.get_queue_total_size()
        files_str = self.app.pluralize(self.app.total_queue_count, "файл", "файла", "файлов")
        q_str = self.app.pluralize(self.app.quantity_files, "файл", "файла", "файлов")
        self.app.log(f"=== СТУПЕНЬ 3: Скачивание ({files_str} на {self.app.human_readable_size(total_size)}, параллельных: {q_str}) ===")
        self.app.current_status = "[СТАТУС] Скачивание недостающих файлов"
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.app.quantity_files) as executor:
            futures = {}
            slot_counter = 0 
            while True:
                while len(futures) < self.app.quantity_files:
                    item = self.app.db.get_pending_item()
                    if not item: break
                    api_path, local_path, yandex_size = item
                    self.app.db.mark_as_downloading(api_path)
                    slot_counter += 1
                    slot_num = (slot_counter % self.app.quantity_files) + 1
                    future = executor.submit(self.process_download_attempt, api_path, local_path, yandex_size, public_key, slot_num, self.app.quantity_files)
                    futures[future] = api_path
                    
                if not futures: break
                
                done, _ = concurrent.futures.wait(futures, timeout=5.0, return_when=concurrent.futures.FIRST_COMPLETED)
                
                for future in done:
                    api_path = futures.pop(future)
                    with self.app.stats_lock: self.app.stats['downloading'] -= 1
                    if self._handle_future_result(future, api_path, futures):
                        return
                    
                self.app.check_status()
                if time.time() - self.app.last_report_time > CONFIG["FAILED_REPORT_INTERVAL"] or len(self.app.failed_downloads) >= 20:
                    self.generate_failed_report(public_key)
 
    def generate_failed_report(self, public_key):
        if not self.app.failed_downloads: return
        report_path = f"/report/failed_{self.app.container_name}_{self.app.report_num:02d}.txt"
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(f"=== ОТЧЕТ О ПРОБЛЕМНЫХ ФАЙЛАХ ===\nДата: {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
            f.writelines(f"Локальный путь: {item['local_path']}\nПуть на Яндексе: {item['api_path']}\n---\n" for item in self.app.failed_downloads)
        self.app.chown_file(report_path)
        self.app.failed_downloads = []
        self.app.last_report_time = time.time()
        self.app.log(f"[INFO] Сформирован отчет об ошибках: {report_path}")
        self.app.report_num += 1

    def _calculate_local_md5(self, local_path):
        h = hashlib.md5()
        try:
            with open(local_path, "rb") as f:
                while True:
                    chunk = f.read(CONFIG["CHUNK_SIZE"])
                    if not chunk: break
                    h.update(chunk)
            return h.hexdigest()
        except OSError as e:
            logger.error(f"[ОШИБКА] Не удалось прочитать файл: {e}")
            return None

    def _fetch_yandex_md5(self, selected):
        yandex_md5 = selected.get('md5')
        if not yandex_md5:
            logger.info("[INFO] MD5 отсутствует в кэше. Запрос к API...")
            resp = self.app.api.fetch_yandex_api("https://cloud-api.yandex.net/v1/disk/public/resources", {"public_key": self.app.link, "path": selected['api_path']})
            if not resp or 'md5' not in resp:
                logger.error("[ОШИБКА] Не удалось получить MD5 от Яндекса.")
                return None
            yandex_md5 = resp['md5']
        return yandex_md5

    def _get_local_md5_to_compare(self, local_path):
        local_md5_hex = self.app.db.get_md5(local_path)
        if local_md5_hex:
            logger.info(f"[INFO] Найден сохраненный MD5 в БД: {local_md5_hex}")
            if input("Использовать сохраненный MD5? (yes - использовать, no - пересчитать): ").lower() not in ['y','yes','д']:
                local_md5_hex = None
        if not local_md5_hex:
            logger.info("[INFO] Подсчет локального MD5 (это может занять время)...")
            local_md5_hex = self._calculate_local_md5(local_path)
            if not local_md5_hex: return None
            self.app.db.set_md5(local_path, local_md5_hex)
            logger.info("[INFO] Локальный MD5 сохранен в БД.")
        return local_md5_hex

    def find_md5_targets(self):
        exact, fuzzy = [], []
        c = self.app.db.conn.cursor()
        c.execute("SELECT path, items FROM yandex_tree WHERE public_key=?", (self.app.link,))
        for path, items_str in c.fetchall():
            for item in json.loads(items_str):
                if item.get('type') == 'file':
                    mapped = self.app.api.map_yandex_item(item)
                    if mapped['name'] == self.app.md5_target or mapped['api_path'] == self.app.md5_target: exact.append(mapped)
                    elif self.app.md5_target.lower() in mapped['name'].lower(): fuzzy.append(mapped)
        return exact if exact else fuzzy

    def select_md5_target(self, targets):
        if len(targets) > 20:
            self.app.log(f"[ОШИБКА] По запросу найдено {len(targets)} файлов. Слишком много совпадений. Уточните имя файла.")
            return None
        if len(targets) == 1: return targets[0]
        self.app.log(f"[ВНИМАНИЕ] Найдено {len(targets)} файлов.")
        for i, f in enumerate(targets):
            self.app.log(f"  {i+1}. [{self.app.human_readable_size(f['size'])}] {f['api_path']}")
        try:
            return targets[int(input("Выберите номер: ")) - 1]
        except (ValueError, IndexError):
            self.app.log("[ОШИБКА] Неверный номер.")
            return None

    def check_md5(self):
        logger.info("=== РЕЖИМ ПРОВЕРКИ MD5 ===")
        c = self.app.db.conn.cursor()
        c.execute("SELECT count(*) FROM yandex_tree WHERE public_key=?", (self.app.link,))
        if c.fetchone()[0] == 0:
            logger.error("[ОШИБКА] Кэш дерева пуст в БД. Сначала запустите скачивание или обновите кэш.")
            return False
        targets = self.find_md5_targets()
        if not targets:
            logger.error(f"[ОШИБКА] Файл '{self.app.md5_target}' не найден в кэше дерева.")
            return False
        selected = self.select_md5_target(targets)
        if not selected: return False
        logger.info(f"[INFO] Выбран файл: '{selected['name']}' ({selected['api_path']})")
        yandex_md5 = self._fetch_yandex_md5(selected)
        if not yandex_md5: return False
        logger.info(f"[INFO] MD5 Яндекса: {yandex_md5}")
        local_path = os.path.join(DOWNLOAD_DIR, selected['api_path'].lstrip('/'))
        if not os.path.exists(local_path):
            logger.error(f"[ОШИБКА] Локальный файл не найден: {local_path}")
            return False
        local_md5_hex = self._get_local_md5_to_compare(local_path)
        if not local_md5_hex: return False
        logger.info(f"[INFO] Локальный MD5: {local_md5_hex}")
        if local_md5_hex == yandex_md5:
            logger.info("[УСПЕХ] MD5 совпадает! Файл скачан без ошибок.")
        else:
            logger.error("[КРИТИЧНО] MD5 НЕ совпадает! Файл поврежден.")
        return True


# ==========================================
# КЛАСС: YadpylesosMain (Утилиты и Проверки)
# ==========================================
class YadpylesosMain:
    def _init_args(self):
        parser = argparse.ArgumentParser(description="Я.Д-Пылесос")
        parser.add_argument('link', nargs='?', default='', help="Ссылка Яндекс.Диска")
        parser.add_argument('total_files', type=int, nargs='?', default=0, help="Ожидаемое количество файлов")
        parser.add_argument('browser_choice', nargs='?', default='1', help="Профиль браузера")
        parser.add_argument('container_name', nargs='?', default='default', help="Имя контейнера")
        parser.add_argument('verbose_flag', nargs='?', default='0', help="Флаг подробного лога (1/0)")
        parser.add_argument('--db-stats', action='store_true', help="Вывести статистику БД и выйти")
        parser.add_argument('--db-check', action='store_true', help="Проверить целостность БД и выйти")
        parser.add_argument('--vacuum', action='store_true', help="Сжать и оптимизировать все базы данных (VACUUM)")
        parser.add_argument('--auth-status', action='store_true', help="Проверить статус авторизации (токен, куки) и выйти")
        parser.add_argument('--auth-enable', action='store_true', help="Включить глобальную авторизацию (OAuth 2.0)")
        parser.add_argument('--auth-disable', action='store_true', help="Отключить глобальную авторизацию")
        parser.add_argument('--unattended', action='store_true', help="Тихий режим для cron")
        parser.add_argument('--simulate-ban', action='store_true', help="Симулировать бан API для теста VPN")
        parser.add_argument('--refresh-cache', action='store_true', help="Принудительно обновить кэш дерева")
        parser.add_argument('--md5-target', default='', help="Проверить MD5 конкретного файла")
        parser.add_argument('--move-extra-path', default='', help="Перенос локальных файлов, удаленных с Яндекса, в указанную папку")
        parser.add_argument('--build-queue', action='store_true', help="Создать список для скачивания без скачивания")
        parser.add_argument('--host-dest', default='/download', help="Локальный путь для сохранения файлов (внутри контейнера)")
        parser.add_argument('--num-threads', type=int, default=1, help="Количество потоков для скачивания одного файла")
        parser.add_argument('--quantity-files', type=int, default=1, help="Количество одновременных файлов для скачивания")
        parser.add_argument('--force-vpn', action='store_true', help="Принудительно запускать VPN при старте")
        parser.add_argument('--homeostasis-off', action='store_true', help="Отключить гомеостаз")
        parser.add_argument('--ssl-off', action='store_true', help="Отключить проверку SSL-сертификатов (обход просроченных сертификатов Яндекса)")
        parser.add_argument('--manage', action='store_true', help="Запустить интерактивный менеджер ссылок (source_links.txt)")
        parser.add_argument('--trace-mem', action='store_true', help="Запуск профилировщика памяти (tracemalloc) для поиска утечек RAM")
        parser.add_argument('--trace-status', action='store_true', help="Вывод панели состояния (CPU, RAM, Диск, VPN) раз в 60 сек")
        parser.add_argument('--notify-tg', action='store_true', help="Отправить итоговый отчет в Telegram по завершении работы")
        self.args = parser.parse_args()

    def __init__(self):
        self._init_args()
        
        self.quantity_files = self.args.quantity_files
        if not 1 <= self.quantity_files <= 8:
            print("[КРИТИЧНО] --quantity-files должно быть от 1 до 8.")
            sys.exit(1)

        self.link = self.args.link
        self.total_files = self.args.total_files
        if self.total_files < 0:
            print(f"{Colors.RED}[КРИТИЧНО] total_files не может быть отрицательным. Установлено в 0.{Colors.NC}")
            self.total_files = 0
            
        self.browser_choice = self.args.browser_choice
        self.container_name = self.args.container_name
        safe_cname = self.container_name if self.container_name else 'default'
        self.db_file = os.path.join(DB_DIR, f"{safe_cname}.db")
        
        self.verbose = self.args.verbose_flag in ('1', '-v', '--verbose', 'true')
        self.refresh_cache = self.args.refresh_cache
        self.md5_target = self.args.md5_target
        self.move_extra_path = self.args.move_extra_path
        self.build_queue_mode = self.args.build_queue
        self.host_dest = self.args.host_dest

        self.num_threads = self.args.num_threads
        if not 1 <= self.num_threads <= 8: 
            print("[КРИТИЧНО] --num-threads от 1 до 8")
            sys.exit(1)
            
        self.multithread_size = int(os.environ.get('MULTITHREAD_SIZE_MB', '100')) * 1024 * 1024

        self.FORCE_VPN = self.args.force_vpn

        self.yad_token = ''
        if os.path.exists('/auth/.auth_enabled'):
            token_file_path = '/auth/.yad_token'
            if os.path.exists(token_file_path):
                try:
                    with open(token_file_path, 'r', encoding='utf-8') as f: self.yad_token = f.read().strip()
                except OSError: pass
        else:
            self.auth_status_msg = "Анонимный (отключено через --auth-disable)"

        self.cookie_file = "/auth/cookies.txt"
        self.auth_status_msg = "Анонимный"
        self.auth_details_msg = ""

        self.stats = {'skipped': 0, 'downloaded': 0, 'errors': 0, 'retries': 0, 'files_seen': 0, 'dirs_seen': 0, 'downloaded_bytes': 0, 'zero_byte_created': 0, 'zero_byte_deleted': 0, 'moved_extra': 0, 'orphan_total': 0, 'api_time': 0.0, 'sleep_time': 0.0, 'cdn_time': 0.0, 'downloading': 0, 'queued_bytes': 0, 'in_progress_bytes': 0, 'io_latency_ms': 0.0, 'api_req_count': 0, 'peak_cpu_temp': 0.0, 'peak_load_avg': 0.0, 'peak_io_latency_ms': 0.0, 'current_file_total': 0, 'current_file_downloaded': 0}
        self.failed_downloads = []
        self.report_num = 1
        self.last_log_time = time.time()
        self.last_cache_save_time = time.time()
        self.last_report_time = time.time()
        self.last_trace_time = time.time()
        self.last_trace_bytes = 0
        self.current_status = "Инициализация"
        self.total_queue_count = 0
        self.homeostasis_off = self.args.homeostasis_off
        self.simulate_ban = self.args.simulate_ban
        self.original_num_threads = self.num_threads
        self.original_quantity_files = self.quantity_files

        self.log_lock = threading.Lock()
        self.stats_lock = threading.Lock()
        self.stop_event = threading.Event()

        self.LOG_FILE = os.path.join(REPORT_DIR, f"{self.container_name}_{datetime.now().astimezone().strftime('%Y%m%d_%H%M%S')}.txt")
        
        logger.setLevel(logging.DEBUG)
        ch = logging.StreamHandler()
        ch.setLevel(logging.INFO if not self.verbose else logging.DEBUG)
        ch.setFormatter(ColorFormatter('[%(asctime)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
        ch.addFilter(SecretFilter())
        logger.addHandler(ch)
        
        # Файл лога создается только при реальном скачивании (не служебные команды)
        if not self._is_service_mode():
            fh = RotatingFileHandler(self.LOG_FILE, maxBytes=CONFIG["MAX_LOG_SIZE"], backupCount=CONFIG["MAX_LOG_FILES"], encoding='utf-8')
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(logging.Formatter('[%(asctime)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
            fh.addFilter(SecretFilter())
            logger.addHandler(fh)

        # Инициализация компонентов
        self.db = DatabaseManager(self.db_file)
        if not self._is_service_mode():
            self.telemetry = Telemetry(os.path.join(DB_DIR, 'telemetry.db'))
        else:
            self.telemetry = None

        from apiyandex import YandexAPIService
        self.api = YandexAPIService(self)
        self.scanner = ScannerAgent(self)
        self.engine = DownloadEngine(self)
        
        from vpn_manager import VpnManager
        self.vpn_manager = VpnManager(self, VPN_DIR, REPORT_DIR, self.stop_event)
        
        self.tg = TelegramService(self)

    def _is_service_mode(self):
        return any([self.args.db_stats, self.args.db_check, self.args.vacuum, self.args.auth_status, self.args.manage, self.args.auth_enable, self.args.auth_disable])
        
    def chown_file(self, filepath):
        try:
            if os.path.isdir(filepath): os.chmod(filepath, 0o755)
            else: os.chmod(filepath, 0o644)
        except PermissionError: pass
        except OSError as e:
            logger.error(f"[ОШИБКА CHMOD] {filepath}: {e}")

    def pluralize(self, n, one, few, many):
        if n % 10 == 1 and n % 100 != 11: return f"{n} {one}"
        elif 2 <= n % 10 <= 4 and not (12 <= n % 100 <= 14): return f"{n} {few}"
        else: return f"{n} {many}"

    def human_readable_size(self, size_bytes):
        if size_bytes == 0: return "0Б"
        size_name = ("Б", "КБ", "МБ", "ГБ", "ТБ", "ПБ")
        i = math.floor(math.log(size_bytes, 1024))
        return f"{round(size_bytes / math.pow(1024, i), 2)}{size_name[i]}"

    def get_progress_str(self):
        if self.stats['files_seen'] > self.total_files > 0: self.total_files = self.stats['files_seen']
        if "Скачивание" in self.current_status:
            done = self.stats['downloaded'] + self.stats['downloading']
            files_str = f"{done}/{self.total_queue_count}" if self.total_queue_count > 0 else f"{done}/?"
            size_str = self.human_readable_size(self.stats['in_progress_bytes'])
            return f"📄{files_str} ✓{self.stats['downloaded']} 📁{self.stats['dirs_seen']} ⬇{size_str}"
        else:
            done = self.stats['files_seen']
            files_str = f"{done}/{self.total_files}" if self.total_files > 0 else f"{done}/?"
            size_str = self.human_readable_size(self.stats['queued_bytes'])
            return f"📄{files_str} ✓{self.stats['skipped']} 📁{self.stats['dirs_seen']} ⬇{size_str}"

    def log(self, msg, show_progress=False):
        with self.log_lock:
            if show_progress: msg = f"{self.get_progress_str()} {msg}"
            logger.info(msg); self.last_log_time = time.time()

    def verbose_log(self, msg, show_progress=False):
        if self.verbose:
            with self.log_lock:
                if show_progress: msg = f"{self.get_progress_str()} {msg}"
                logger.info(msg)

    def set_status(self, new_status):
        if new_status != self.current_status:
            self.current_status = new_status; self.last_log_time = time.time()
            self.log(f"{self.current_status}", show_progress=True)

    def check_status(self):
        self.check_homeostasis()
        if time.time() - self.last_log_time > CONFIG["STATUS_LOG_INTERVAL"]:
            if not self.verbose: logger.info(f"{self.current_status}", show_progress=True)
            self.last_log_time = time.time()
        if time.time() - self.last_cache_save_time > CONFIG["CACHE_SAVE_INTERVAL"]:
            self.db.save_cache(self); self.db.commit()
            try: self.db.conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            except sqlite3.DatabaseError: pass
            self.last_cache_save_time = time.time()
        trace_interval = int(os.environ.get('TRACE_INTERVAL', '60'))
        if self.args.trace_status and time.time() - self.last_trace_time > trace_interval:
            self._print_trace_status()
            self.last_trace_time = time.time()

    def _print_trace_status(self):
        load1 = "Н/Д"
        try:
            with open("/proc/loadavg", "r") as f: load1 = f.read().split()[0]
            with self.stats_lock:
                self.stats['peak_load_avg'] = max(self.stats['peak_load_avg'], float(load1))
        except OSError: pass
        
        temp_cpu = "Н/Д"
        try:
            with open("/sys_thermal_cpu", "r") as f: temp_val = float(f.read().strip()) / 1000
            temp_cpu = f"{temp_val:.1f}°C"
            with self.stats_lock:
                self.stats['peak_cpu_temp'] = max(self.stats['peak_cpu_temp'], temp_val)
        except (FileNotFoundError, ValueError): pass
            
        mem_str = self._get_host_memory()
        cpu_freq = self._get_cpu_freq()
        disk_str = self._get_disk_info()
        
        bytes_now = self.stats['downloaded_bytes']
        bytes_diff = bytes_now - self.last_trace_bytes
        speed_mb = (bytes_diff / (60 * 1024 * 1024)) if bytes_diff > 0 else 0
        self.last_trace_bytes = bytes_now
        total_gb = bytes_now / (1024 * 1024 * 1024)
        
        io_lat = self.stats.get('io_latency_ms', 0)
        api_req = self.stats.get('api_req_count', 0)
        avg_api_ms = (self.stats['api_time'] / api_req * 1000) if api_req > 0 else 0
        
        vpn_status, vpn_server = self._get_vpn_status_str()
        
        logger.info(f"[СТАТУС 60с] ЦП: {temp_cpu} ({cpu_freq} МГц) | Нагрузка: {load1} | Потоки: {self.num_threads}/{self.quantity_files}")
        logger.info(f"[СТАТУС 60с] ОЗУ (NAS): {mem_str} | Диск: Свободно {disk_str} | I/O: {io_lat:.0f} мс")
        curr_file_dl = self.stats.get('current_file_downloaded', 0)
        curr_file_tot = self.stats.get('current_file_total', 0)
        file_str = f"{self.human_readable_size(curr_file_dl)}/{self.human_readable_size(curr_file_tot)}" if curr_file_tot > 0 else "Н/Д"
        
        logger.info(f"[СТАТУС 60с] Сеть: Ск {speed_mb:.1f} МБ/с | Файл: {file_str} | API: {avg_api_ms:.0f} мс | Всего: {total_gb:.2f} ГБ")
        logger.info(f"[СТАТУС 60с] VPN: {vpn_status} | Сервер: {vpn_server}")

    def _get_vpn_status_str(self):
        if not (self.vpn_manager.process and self.vpn_manager.process.poll() is None):
            return "ВЫКЛ", "Н/Д"
        try:
            resp = requests.get('http://127.0.0.1:9090/proxies/🛡️ Yandex VPN', timeout=2)
            if resp.status_code == 200: 
                return "АКТИВЕН", resp.json().get('now', 'Н/Д')
        except requests.exceptions.RequestException: 
            pass
        return "АКТИВЕН", "Н/Д"

    def _get_host_memory(self):
        try:
            mem_total = mem_free = 0
            with open("/proc/meminfo", "r") as f:
                for line in f:
                    if line.startswith("MemTotal:"): mem_total = int(line.split()[1])
                    elif line.startswith("MemAvailable:"): mem_free = int(line.split()[1])
            mem_used_mb = (mem_total - mem_free) / 1024
            mem_total_mb = mem_total / 1024
            return f"{mem_used_mb:.0f} / {mem_total_mb:.0f} МБ"
        except OSError:
            return "Н/Д"

    def _get_cpu_freq(self):
        try:
            with open("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq", "r") as f:
                return int(f.read().strip()) // 1000  # Переводим kHz в MHz
        except (FileNotFoundError, ValueError, OSError):
            return "Н/Д"

    def _get_disk_info(self):
        try:
            stat = os.statvfs("/download")
            free_bytes = stat.f_bavail * stat.f_frsize
            free_pct = (stat.f_bavail / stat.f_blocks) * 100 if stat.f_blocks > 0 else 0
            free_str = self.human_readable_size(free_bytes)
            return f"{free_str} ({free_pct:.1f}%) | Иноды: {stat.f_favail}"
        except OSError:
            return "Н/Д"
            
    def adaptive_pause(self, file_size):
        if file_size == 0: return
        t = time.time()
        if file_size < 1024 * 1024: time.sleep(random.uniform(0.5, 1.5))
        else: time.sleep(max(1.0, random.gauss(self.api.current_api_pause, 1.0)))
        with self.stats_lock: self.stats['sleep_time'] += time.time() - t

    def sanitize_filename(self, name):
        name = name.replace('/', '_').replace('\\', '_').replace('\0', '_').replace('"', '')
        name = "".join(c for c in name if c.isspace() or not unicodedata.category(c).startswith('C'))
        name_base, ext = os.path.splitext(name)
        while len(name.encode('utf-8')) > 250: name_base = name_base[:-1]; name = name_base + ext
        return name.strip(' .')

    def check_apoptosis(self, error):
        err_str = str(error).lower()
        if "read-only file system" in err_str or "no space left" in err_str:
            self.log(f"{Colors.RED}[КРИТИЧНО: АПОПТОЗ] {error}. Завершение работы для защиты данных.{Colors.NC}")
            self.db.save_cache(self)
            raise SystemExit(str(error))

    def _check_required_dirs(self):
        for d in [DB_DIR, REPORT_DIR, DOWNLOAD_DIR]:
            if not os.path.isdir(d): os.makedirs(d, exist_ok=True)
            if not os.access(d, os.W_OK): 
                print(f"[КРИТИЧНО] Нет прав на запись в {d}")
                sys.exit(1)

    def _cleanup_old_logs(self):
        import glob
        days = int(os.environ.get('LOG_RETENTION_DAYS', '14'))
        now = time.time()
        # Маски: batch_*.txt (сессии), failed_* (ошибки), *_*.txt (детальные логи контейнеров), *_*.txt.* (ротация логов)
        log_patterns = ["batch_*.txt", "failed_*", "*_*.txt", "*_*.txt.*"]
        for pattern in log_patterns:
            for filepath in glob.glob(os.path.join(REPORT_DIR, pattern)):
                # Строгая защита журнала VPN и БД телеметрии от случайного удаления
                if "vpn_audit.log" in filepath or "telemetry" in filepath: continue
                try:
                    if now - os.path.getmtime(filepath) > days * 86400:
                        os.remove(filepath)
                except OSError: pass
                
    def preflight_check(self):
        self._check_required_dirs()
        try: os.chmod("/db", 0o700)
        except OSError: pass
        self._cleanup_old_logs()
        try:
            statvfs = os.statvfs("/download")
            if statvfs.f_favail < 1000: 
                print(f"{Colors.RED}[КРИТИЧНО] Недостаточно инодов: {statvfs.f_favail}{Colors.NC}")
                raise SystemExit(1)
        except OSError: pass
        try:
            requests.get('https://ya.ru', timeout=3)
        except requests.exceptions.RequestException:
            print(f"{Colors.RED}[КРИТИЧНО] Нет сетевого соединения (ya.ru недоступен).{Colors.NC}")
            raise SystemExit(1)
        logger.info("[INFO] Предстартовая проверка пройдена.")

    def detect_disk_type(self):
        try:
            stat = os.stat("/download")
            dev = f"{os.major(stat.st_dev)}:{os.minor(stat.st_dev)}"
            with open(f"/sys/dev/block/{dev}/queue/rotational", "r") as f:
                rotational = f.read().strip()
            if rotational == "0": return "SSD"
            else: return "HDD"
        except OSError:
            return "Unknown"

    def _check_disk_space_and_inodes(self):
        try:
            statvfs = os.statvfs("/download")
            free_space = statvfs.f_bavail * statvfs.f_frsize
            free_inodes = statvfs.f_favail
        except OSError as e:
            logger.error(f"[КРИТИЧНО] Ошибка проверки диска: {e}")
            raise SystemExit(1)
        queue_size = self.db.get_queue_total_size()
        queue_count = self.db.get_queue_count('pending')
        if queue_count > free_inodes:
            logger.error(f"[КРИТИЧНО] Недостаточно инодов! В очереди: {queue_count}, свободно: {free_inodes}")
            raise SystemExit(1)
        if queue_size > free_space:
            logger.error(f"[КРИТИЧНО] Недостаточно места! В очереди: {self.human_readable_size(queue_size)}, свободно: {self.human_readable_size(free_space)}")
            raise SystemExit(1)
        if free_space > 0 and queue_size / free_space > 0.95:
            logger.warning("[ВНИМАНИЕ] После скачивания останется менее 5% свободного места на диске!")
        if free_inodes > 0 and queue_count / free_inodes > 0.95:
            logger.warning("[ВНИМАНИЕ] После скачивания останется менее 5% свободных инодов!")

    def _adjust_homeostasis_limits(self, load1, temp_cpu):
        temp_high = int(os.environ.get('TEMP_CPU_HIGH', '75'))
        temp_low = int(os.environ.get('TEMP_CPU_LOW', '60'))
        load_high = 4.0
        load_low = 2.0
        
        if (load1 > load_high) or (temp_cpu > temp_high):
            self._apply_throttle(load1, temp_cpu)
        elif (load1 < load_low) and (temp_cpu < temp_low):
            self._apply_cooldown(load1, temp_cpu)

    def _apply_throttle(self, load1, temp_cpu):
        if self.quantity_files > 1:
            self.log(f"[ВНИМАНИЕ] Гомеостаз: Перегрев (Load: {load1}, Temp: {temp_cpu}°C). Снижаем кол-во файлов до 1.")
            self.quantity_files = 1
        elif self.num_threads > 1:
            self.log(f"[ВНИМАНИЕ] Гомеостаз: Перегрев (Load: {load1}, Temp: {temp_cpu}°C). Снижаем потоки до 1.")
            self.num_threads = 1
        else:
            self.engine.current_cdn_pause = min(self.engine.MAX_CDN_PAUSE, self.engine.current_cdn_pause * 1.2)

    def _apply_cooldown(self, load1, temp_cpu):
        if self.num_threads == 1 and self.original_num_threads > 1:
            self.log(f"[INFO] Гомеостаз: Остывание (Load: {load1}, Temp: {temp_cpu}°C). Восстанавливаем потоки ({self.original_num_threads}).")
            self.num_threads = self.original_num_threads
        elif self.quantity_files == 1 and self.original_quantity_files > 1:
            self.log(f"[INFO] Гомеостаз: Остывание (Load: {load1}, Temp: {temp_cpu}°C). Восстанавливаем файлы ({self.original_quantity_files}).")
            self.quantity_files = self.original_quantity_files
        else:
            self.engine.current_cdn_pause = max(self.engine.MIN_CDN_PAUSE, self.engine.current_cdn_pause - 0.1)

    def check_homeostasis(self):
        if self.homeostasis_off: return
        try:
            with open("/proc/loadavg", "r") as f: load1 = float(f.read().split()[0])
            temp_cpu = 0.0
            try:
                with open("/sys_thermal_cpu", "r") as f: temp_cpu = float(f.read().strip()) / 1000
            except (FileNotFoundError, ValueError): pass
            self._adjust_homeostasis_limits(load1, temp_cpu)
        except (OSError, ValueError): pass

    # --- CLI: Менеджер ссылок и БД-статы ---

    def _path_completer(self, text, state):
        if not text.startswith('/'): text = './' + text
        matches = glob.glob(text + '*')
        matches = [m + '/' if os.path.isdir(m) else m for m in matches]
        if state < len(matches): return matches[state]
        return None

    def _enable_tab_completion(self):
        readline.set_completer(self._path_completer)
        readline.parse_and_bind('tab: complete')

    def _disable_tab_completion(self):
        readline.set_completer(None)

    def _command_completer(self, text, state):
        cli_commands = ['run', 'edit', 'add', 'del', 'toggle', 'exit', 'запустить', 'редактировать', 'добавить', 'удалить', 'переключить', 'выход']
        matches = [c for c in cli_commands if c.startswith(text.lower())]
        if state < len(matches): return matches[state]
        return None

    def _enable_command_completion(self):
        readline.set_completer(self._command_completer)
        readline.parse_and_bind('tab: complete')

    def _print_sources(self, sources):
        cols = shutil.get_terminal_size().columns
        print("\n=== Текущий список ссылок ===")
        if not sources:
            print("Список пуст. Используйте 'add' для добавления.")
            return
        for i, s in enumerate(sources):
            status = "✓" if s['active'] else "✗"
            line = f"[{i+1}] [{status}] {s['name']} | {s['link']} | {s['dest']} | {s['opts']}"
            print(textwrap.fill(line, width=cols, subsequent_indent="    "))

    def _add_source_cli(self, sources):
        print("--- Добавление ссылки ---")
        name = input("Имя контейнера: ").strip()
        link = input("Ссылка Я.Диска: ").strip()
        self._enable_tab_completion()
        dest = input("Папка назначения (нажмите Tab для подсказок): ").strip()
        self._disable_tab_completion()
        total = input("Кол-во файлов (или 0): ").strip() or "0"
        opts = input("Опции (напр. -v --threads=4): ").strip()
        if not name:
            print("[ОШИБКА] Имя контейнера не может быть пустым.")
            return
        if not link.startswith(('http://', 'https://')):
            print("[ОШИБКА] Ссылка должна начинаться с http:// или https://")
            return
        if not dest.startswith('/'):
            print("[ОШИБКА] Путь должен быть абсолютным (начинаться с /).")
            return
        sources.append({'active': True, 'name': name, 'link': link, 'dest': dest, 'total': total, 'opts': opts})
        print("[УСПЕХ] Добавлено.")

    def _del_source_cli(self, sources):
        try:
            idx = int(input("Номер строки для удаления: ")) - 1
            if 0 <= idx < len(sources):
                sources.pop(idx)
                print("[УСПЕХ] Удалено.")
            else: print("[ОШИБКА] Неверный номер.")
        except ValueError: print("[ОШИБКА] Введите число.")

    def _toggle_source_cli(self, sources):
        try:
            idx = int(input("Номер строки для переключения: ")) - 1
            if 0 <= idx < len(sources):
                sources[idx]['active'] = not sources[idx]['active']
                print("[УСПЕХ] Переключено.")
            else: print("[ОШИБКА] Неверный номер.")
        except ValueError: print("[ОШИБКА] Введите число.")

    def _apply_edits_to_source(self, s):
        print("--- Редактирование (Enter = оставить как есть) ---")
        s['name'] = input(f"Имя [{s['name']}]: ").strip() or s['name']
        new_link = input(f"Ссылка [{s['link']}]: ").strip() or s['link']
        if not new_link.startswith(('http://', 'https://')):
            print("[ОШИБКА] Ссылка должна начинаться с http:// или https://. Изменения отклонены.")
        else:
            s['link'] = new_link
        self._enable_tab_completion()
        new_dest = input(f"Папка [{s['dest']}]: ").strip() or s['dest']
        self._disable_tab_completion()
        if not new_dest.startswith('/'):
            print("[ОШИБКА] Путь должен быть абсолютным. Изменения отклонены.")
        else:
            s['dest'] = new_dest
        s['total'] = input(f"Файлов [{s['total']}]: ").strip() or s['total']
        s['opts'] = input(f"Опции [{s['opts']}]: ").strip() or s['opts']
        print("[УСПЕХ] Обновлено.")

    def _edit_source_cli(self, sources):
        try:
            idx = int(input("Номер строки для редактирования: ")) - 1
        except ValueError:
            print("[ОШИБКА] Введите число.")
            return
        if not (0 <= idx < len(sources)):
            print("[ОШИБКА] Неверный номер.")
            return
        self._apply_edits_to_source(sources[idx])

    def _validate_sources_format(self, sources):
        has_errors = False
        valid_flags = ['-v', '--verbose', '--refresh-cache', '--build-queue', '--vpn', '--force-vpn', '--auth', '--auth-disable', '--homeostasis-off', '--simulate-ban', '--ssl-off', '--notify-tg']
        valid_prefixes = ['--threads=', '--multithread-size=', '--quantity-files=', '--md5=', '--move-extra=']
        for i, s in enumerate(sources):
            err_msg = self._validate_single_source(s, valid_flags, valid_prefixes)
            if err_msg:
                print(f"{Colors.RED}[ОШИБКА] Строка {i+1}: {err_msg}{Colors.NC}")
                has_errors = True
        return has_errors

    def _validate_single_source(self, s, valid_flags, valid_prefixes):
        if not s['name']: return "Имя контейнера не может быть пустым."
        if not s['link'].startswith(('http://', 'https://')): return "Ссылка должна начинаться с http:// или https://."
        if not s['dest'].startswith('/'): return "Путь должен быть абсолютным (начинаться с /)."
        try:
            tokens = shlex.split(s['opts'])
            for token in tokens:
                if token in valid_flags: continue
                if any(token.startswith(p) for p in valid_prefixes): continue
                return f"Неизвестная или некорректная опция: {token}"
        except ValueError:
            return "Синтаксическая ошибка в опциях (проверьте кавычки)."
        return ""

    def manage_sources_cli(self):
        sources_file = os.path.join(BASE_DIR, 'source_links.txt')
        if not os.path.exists(sources_file):
            try:
                with open(sources_file, 'w', encoding='utf-8') as f:
                    f.write("# Имя | Ссылка | Папка | Файлов | Опции\n")
            except OSError as e:
                print(f"{Colors.RED}[КРИТИЧНО] Не удалось создать файл source_links.txt: {e}{Colors.NC}")
                raise SystemExit(1)
                
        def load_sources():
            sources = []
            try:
                with open(sources_file, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if not line: continue
                        is_active = not line.startswith('#')
                        clean_line = line.lstrip('#').strip()
                        parts = next(csv.reader([clean_line], delimiter='|'))
                        parts = [p.strip() for p in parts]
                        if not parts or parts[0].lower() == 'имя': continue
                        while len(parts) < 5: parts.append('')
                        sources.append({
                            'active': is_active, 'name': parts[0], 'link': parts[1], 'dest': parts[2],
                            'total': parts[3], 'opts': parts[4]
                        })
            except OSError as e:
                print(f"{Colors.RED}[КРИТИЧНО] Ошибка чтения source_links.txt: {e}{Colors.NC}")
            return sources

        def save_sources(sources):
            try:
                with open(sources_file, 'w', encoding='utf-8') as f:
                    f.write("# Имя | Ссылка | Папка | Файлов | Опции\n")
                    for s in sources:
                        total = s['total'] if s['total'] else '0'
                        line = f"{s['name']} | {s['link']} | {s['dest']} | {total} | {s['opts']}"
                        if not s['active']: line = f"# {line}"
                        f.write(line + "\n")
            except OSError as e:
                print(f"{Colors.RED}[КРИТИЧНО] Ошибка сохранения source_links.txt: {e}{Colors.NC}")

        def execute_action(action):
            if action in ('exit', 'выход'):
                if self._validate_sources_format(sources):
                    print("[ВНИМАНИЕ] В списке есть ошибки формата. Сначала исправьте их.")
                    return False
                save_sources(sources)
                print("Изменения сохранены. Выход без запуска.")
                raise SystemExit(1)
            elif action in ('run', 'запустить'):
                if self._validate_sources_format(sources):
                    print("[ВНИМАНИЕ] В списке есть ошибки формата. Сначала исправьте их.")
                    return False
                save_sources(sources)
                print("Запуск пакетной обработки...")
                raise SystemExit(0)
            elif action in ('add', 'добавить'):
                self._add_source_cli(sources)
                save_sources(sources)
            elif action in ('del', 'удалить', 'delete'):
                self._del_source_cli(sources)
                save_sources(sources)
            elif action in ('toggle', 'переключить'):
                self._toggle_source_cli(sources)
                save_sources(sources)
            elif action in ('edit', 'редактировать'):
                self._edit_source_cli(sources)
                save_sources(sources)
            else:
                print("Неизвестная команда.")
            return True

        sources = load_sources()
        try:
            while True:
                self._print_sources(sources)
                print("\nДействия: run, edit, add, del, toggle, exit")
                self._enable_command_completion()
                action = input("Выберите действие: ").lower().strip()
                execute_action(action)
        except KeyboardInterrupt:
            save_sources(sources)
            print("\n[ВНИМАНИЕ] Прервано пользователем. Изменения сохранены. Выход.")
            raise SystemExit(1)

    def _print_db_stats_for_file(self, db_path):
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            c = conn.cursor()
            c.execute("SELECT count(*) FROM local_files"); local = c.fetchone()[0]
            c.execute("SELECT count(*) FROM download_queue WHERE status=?", (DownloadStatus.DOWNLOADED,)); dl = c.fetchone()[0]
            c.execute("SELECT count(*) FROM download_queue WHERE status=?", (DownloadStatus.PENDING,)); pend = c.fetchone()[0]
            c.execute("SELECT count(*) FROM yandex_tree"); tree = c.fetchone()[0]
            print(f"БД: {os.path.basename(db_path)}")
            print(f"  Локальных файлов: {local} | Папок в кэше: {tree} | Скачано: {dl} | В очереди: {pend}")
            conn.close()
        except sqlite3.OperationalError:
            print(f"БД: {os.path.basename(db_path)} (занята другим процессом)")

    def handle_cli_modes(self):
        if self.args.db_stats:
            self._run_db_command("=== СТАТИСТИКА БАЗ ДАННЫХ ===", self._print_db_stats_for_file)
            sys.exit(0)
            
        if self.args.db_check:
            self._run_db_command("=== ПРОВЕРКА БД ===", self._check_db_integrity_for_file)
            sys.exit(0)

        if self.args.vacuum:
            self._run_db_command("=== ОПТИМИЗАЦИЯ БД (VACUUM) ===", self._vacuum_db_file)
            sys.exit(0)

        if self.args.auth_status:
            self._print_auth_status()
            sys.exit(0)

    def _run_db_command(self, title, action_func):
        db_files = glob.glob(os.path.join(DB_DIR, '*.db'))
        if not db_files:
            print("Базы данных не найдены.")
            return
        print(title)
        for f in db_files:
            if "telemetry.db" in f and action_func == self._print_db_stats_for_file:
                self._print_telemetry_stats(f)
            elif os.path.isfile(f):
                action_func(f)

    def _print_telemetry_stats(self, db_path):
        limit = int(os.environ.get('DB_STATS_LIMIT', '20'))
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            c = conn.cursor()
            print(f"\nБД: {os.path.basename(db_path)} (Последние {limit} сессий)")
            c.execute("""SELECT timestamp, provider, auth_mode, total_requests, total_bans, 
                          avg_response_time_ms, peak_cpu_temp, peak_load_avg, peak_io_latency_ms 
                          FROM api_sessions ORDER BY timestamp DESC LIMIT ?""", (limit,))
            rows = c.fetchall()
            if not rows:
                print("  Записей пока нет.")
            for row in rows:
                ts, prov, auth, req, bans, api_ms, cpu_temp, load_avg, io_lat = row
                print(f"  [{ts}] {prov} | Auth: {auth} | Запросов: {req} | Банов: {bans} | ЦП: {cpu_temp:.1f}°C | Нагрузка: {load_avg:.1f} | I/O: {io_lat:.0f}мс | API: {api_ms}мс")
            conn.close()
        except sqlite3.OperationalError:
            print(f"БД: {os.path.basename(db_path)} (занята другим процессом)")

    def log_startup_info(self):
        if self.FORCE_VPN: self.log("[INFO] VPN модуль: Принудительный режим (весь трафик API через VPN).")
        self.log(f"[INFO] Режим авторизации: {self.auth_status_msg}")
        if self.yad_token: self.log(f"[INFO] {self.auth_details_msg}")
        
        t_str_mt = self.pluralize(self.num_threads, "поток", "потока", "потоков")
        if self.num_threads == 1:
            self.log("[INFO] Многопоточность: 1 поток")
        else:
            self.log(f"[INFO] Многопоточность: {self.num_threads} {t_str_mt} (порог: {self.multithread_size//(1024*1024)} МБ)")
            
        self.log(f"[INFO] Параллельное скачивание: {self.quantity_files} файл(ов) одновременно")

    def _vacuum_db_file(self, db_path):
        try:
            # VACUUM требует доступа на запись, поэтому открываем без mode=ro
            conn = sqlite3.connect(db_path)
            conn.execute("VACUUM")
            conn.close()
            print(f"БД: {os.path.basename(db_path)} - успешно сжата.")
        except sqlite3.OperationalError as e:
            print(f"БД: {os.path.basename(db_path)} - ошибка (занята?): {e}")

    def _print_auth_status(self):
        print("=== ПРОВЕРКА АВТОРИЗАЦИИ ===")
        
        mode = "Анонимный"
        status_file = '/auth/.status'
        if os.path.exists(status_file):
            with open(status_file, 'r', encoding='utf-8') as f:
                mode = f.read().strip()
        print(f"Установленный режим: {mode}")
        
        self._print_token_status()
        self._print_cookies_status()

    def _print_token_status(self):
        token_path = '/auth/.yad_token'
        if not os.path.exists(token_path):
            print("\nOAuth Токен: Не найден.")
            return
            
        print("\nOAuth Токен: Обнаружен.")
        print("Выполняется запрос к API Яндекса с использованием OAuth токена...")
        try:
            with open(token_path, 'r', encoding='utf-8') as f:
                token = f.read().strip()
            sess = requests.Session()
            sess.headers.update({'Authorization': f'OAuth {token}'})
            resp = sess.get('https://cloud-api.yandex.net/v1/disk/', timeout=10)
            self._print_api_response_status(resp)
        except requests.exceptions.RequestException as e:
            print(f"[ОШИБКА] Сбой сети: {e}")

    def _print_cookies_status(self):
        cookie_path = '/auth/cookies.txt'
        if not os.path.exists(cookie_path):
            print("\nCookies: Не найдены.")
            return
            
        print("\nCookies: Обнаружены.")
        print("Выполняется запрос к API Яндекса с использованием Cookies...")
        try:
            sess = requests.Session()
            sess.cookies = http.cookiejar.MozillaCookieJar(cookie_path)
            sess.cookies.load(ignore_discard=True)
            resp = sess.get('https://cloud-api.yandex.net/v1/disk/', timeout=10)
            self._print_api_response_status(resp)
        except (http.cookiejar.LoadError, OSError, requests.exceptions.RequestException) as e:
            print(f"[ОШИБКА] Сбой сети или ошибка загрузки куков: {e}")

    def _print_api_response_status(self, resp):
        if resp.status_code == 200:
            print("[УСПЕХ] Валидны (200 OK).")
        elif resp.status_code == 401:
            print("[ОШИБКА] Невалидны (401).")
        else:
            print(f"[ВНИМАНИЕ] Неожиданный ответ: {resp.status_code}")

    def _check_db_integrity_for_file(self, db_path):
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            c = conn.cursor()
            c.execute("PRAGMA integrity_check")
            res = c.fetchone()[0]
            print(f"БД: {os.path.basename(db_path)} - " + ("ОК" if res == "ok" else "ОШИБКА"))
            conn.close()
        except sqlite3.OperationalError:
            print(f"БД: {os.path.basename(db_path)} - занята другим процессом")

    def process_all(self, public_key, dest="/download"):
        self._cleanup_old_logs()
        if self.args.manage:
            self.manage_sources_cli()
            return True

        self.handle_cli_modes()
        self.db.init_db()
        
        if self.md5_target:
            self.db.load_cache(self)
            raise SystemExit(0 if self.engine.check_md5() else 1)

        self.log_startup_info()
        self.preflight_check()
        self.api.preflight_api_check()
        
        self._init_vpn()
            
        logger.info("=== ЗАПУСК СКАЧИВАНИЯ ===")
        
        self._log_disk_type_warnings()
                
        self.scanner.orbital_garbage_collector(dest)
        self.db.load_global_state(self)
        self.scanner.cleanup_interrupted_files()

        c = self.db.conn.cursor()
        c.execute("DELETE FROM download_queue")
        self.db.conn.commit()

        res = self.scanner.phase_1_and_2_build_queue(public_key, dest)
        if res is None:
            self._finalize_process_all(public_key)
            return False
        
        if self.build_queue_mode:
            logger.info("[УСПЕХ] Ступень 3 отменена (--build-queue). Очередь в БД.")
            self._finalize_process_all(public_key)
            return True
            
        if not self.refresh_cache:
            self._check_disk_space_and_inodes()
            if self.db.get_queue_count('pending') > 0:
                self.engine.phase_3_download_queue(public_key)
            else:
                logger.info("[INFO] Очередь пуста. Все файлы на месте.")
                
        self._finalize_process_all(public_key)
        return True

    def _init_vpn(self):
        if self.FORCE_VPN:
            if self.vpn_manager.start():
                self.vpn_manager.proxies = {'http': 'http://127.0.0.1:7890', 'https': 'http://127.0.0.1:7890'}
                self.api.session_api.proxies = self.vpn_manager.proxies
                self.api.session_cdn.proxies = self.vpn_manager.proxies
                self.log("[INFO] VPN модуль: Принудительный режим (весь трафик через VPN).")
                self.vpn_manager.start_watchdog()
            else:
                raise SystemExit("port vpn (7890) not opened")
        elif self.vpn_manager.generate_config():
            self.log("[INFO] VPN модуль: Конфигурация обнаружена. Режим: По требованию.")

    def _log_disk_type_warnings(self):
        disk_type = self.detect_disk_type()
        if disk_type == "SSD" and self.quantity_files == 1:
            logger.info("[INFO] Обнаружен SSD. Для ускорения рекомендуется использовать --quantity-files=3")
        elif disk_type == "HDD" and self.quantity_files > 1:
            logger.warning(f"[ВНИМАНИЕ] Обнаружен HDD. Параллельная скачка ({self.quantity_files} файлов) может снизить производительность диска.")

    def _finalize_process_all(self, public_key):
        if self.failed_downloads:
            self.engine.generate_failed_report(public_key)
        self.db.save_cache(self)
        self.db.save_global_state(self)

    def signal_handler(self, _sig, _frame):
        self.log("[ВНИМАНИЕ] Получен сигнал остановки. Прерывание ожиданий...")
        self.stop_event.set()
        self.set_status("[СТАТУС] Остановка по сигналу. Сохранение кэша")
        self.db.save_cache(self)
        if self.db.conn:
            self.db.conn.commit()
            self.db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        raise SystemExit(0)

    def run(self):
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, self.signal_handler)

        if self.args.manage:
            self.manage_sources_cli()
            sys.exit(0)

        import tracemalloc
        if self.args.trace_mem:
            tracemalloc.start()
            print(f"{Colors.CYAN}[INFO] Профилировщик памяти включен.{Colors.NC}")

        exit_code = 1
        critical_error = ""
        try:
            exit_code = 0 if self.process_all(self.link, DOWNLOAD_DIR) else 1
            self._print_final_report()

        except BrokenPipeError:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
            sys.exit(0)
        except SystemExit as e:
            # Перехватываем SystemExit от signal_handler (docker stop)
            # Код 0 означает успешную остановку, 1 - ошибку.
            exit_code = e.code if isinstance(e.code, int) else 1
            if exit_code != 0:
                critical_error = "Скрипт остановлен сигналом или критической ошибкой"
        except Exception as e:  # noqa: BLE001
            translated_err = self.tg._translate_error(str(e))
            logger.critical(f"[КРИТИЧНО] {translated_err}")
            critical_error = translated_err
            exit_code = 1
        finally:
            self._finalize_execution(exit_code, critical_error)

    def _print_final_report(self):
        logger.info("\n=== ИТОГОВЫЙ ОТЧЕТ ===")
        logger.info(f"Увидено файлов: {self.stats['files_seen']}")
        logger.info(f"Пропущено: {self.stats['skipped']}")
        logger.info(f"Скачано: {self.stats['downloaded']}")
        logger.info(f"Размер: {self.human_readable_size(self.stats['downloaded_bytes'])}")
        logger.info(f"Ошибок: {self.stats['errors']}")
        
        orphan_count = self.stats.get('orphan_total', 0)
        if orphan_count > 0 and not self.move_extra_path:
            logger.warning(f"\n[ВНИМАНИЕ] Количество файлов, отсутствующих на Я.Диске: {orphan_count}")
            logger.warning("[ВНИМАНИЕ] Запустите скрипт повторно, указав папку для карантина (--move-extra='/путь/'), для переноса файлов и их анализа.")
        else:
            moved = self.stats.get('moved_extra', 0)
            logger.info(f"Файлов для карантина: {orphan_count} | Успешно перенесено: {moved}")
            
        api_m, api_s = divmod(int(self.stats['api_time']), 60)
        sl_m, sl_s = divmod(int(self.stats['sleep_time']), 60)
        cdn_m, cdn_s = divmod(int(self.stats['cdn_time']), 60)
        logger.info(f"Время API: {api_m}м {api_s}с | Время CDN: {cdn_m}м {cdn_s}с | Время сна: {sl_m}м {sl_s}с")

    def _finalize_execution(self, exit_code, critical_error):
        # 1. Записываем итоги сессии в телеметрию
        if self.telemetry:
            self.telemetry.finalize_session(self)
        
        self._dump_trace_mem()
            
        # 2. Отправка уведомления (может поднять VPN, который напишет в телеметрию)
        if self.args.notify_tg:
            err_to_send = critical_error if exit_code == 1 else ""
            self.tg.send_telegram_notification(exit_code == 0, err_to_send)
            
        # 3. Останавливаем VPN
        self.vpn_manager.stop()
        
        # 4. Теперь, когда всё завершено, безопасно закрываем телеметрию
        if self.telemetry:
            self.telemetry.close()
            
        sys.exit(exit_code)

    def _dump_trace_mem(self):
        import tracemalloc
        if self.args.trace_mem and tracemalloc.is_tracing():
            snapshot = tracemalloc.take_snapshot()
            top_stats = snapshot.statistics('lineno')
            print(f"\n{Colors.CYAN}=== ТОП-10 ПОТРЕБИТЕЛЕЙ ПАМЯТИ (--trace-mem) ==={Colors.NC}")
            filtered_stats = [stat for stat in top_stats if 'yadpylesos.py' in str(stat.traceback)]
            for stat in filtered_stats[:10]:
                print(f"[{stat.size // 1024} KiB] {stat.count} blocks")
                for line in stat.traceback.format():
                    print(f"    {line.strip()}")
            print(f"{Colors.CYAN}============================================={Colors.NC}")
            tracemalloc.stop()

if __name__ == "__main__":
    app = YadpylesosMain()
    app.run()
