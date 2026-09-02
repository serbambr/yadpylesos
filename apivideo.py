from datetime import datetime
import os
import queue
import re
import signal
import subprocess
import threading
import time
from urllib.parse import urlparse

from yadpylesos import CONFIG, BaseCloudProvider, logger


class VideoAPIService(BaseCloudProvider):
    """Провайдер для скачивания видео через yt-dlp (OmniReaper)"""

    _COOKIE_LOGIN_MARKERS = {
        'youtube.com': ('LOGIN_INFO', 'SID', '__Secure-1PSID', '__Secure-3PSID'),
        'boosty.to': ('auth',),
    }

    _OUTPUT_TEMPLATES = {
        'boosty.to': '%(title)s [%(id)s].%(ext)s',
    }

    def __init__(self, app):
        super().__init__(app)
        self.failed_links = []
        self.last_video_error = ''
        self.video_attempts = 0
        self.video_skipped = 0
        self.video_bans = 0
        self.extraction_started_at = None
        self.antiban_series_count = 0
        self.antiban_successes_since_pause = 0
        self.antiban_tg_notified = False
        self.antiban_abort = False
        self.silence_grace = False
        # Контракт ядра: save_global_state читает атрибут у любого провайдера при финализации
        self.current_api_pause = 4.0

        domain = self._get_domain(app.link)
        cookie_file = f'/auth/{domain}.txt'
        if os.path.exists(cookie_file):
            app.auth_status_msg = f"Cookies для {domain} найдены"
        else:
            app.auth_status_msg = "Анонимный (видео)"

    def preflight_api_check(self):
        logger.info("[INFO] Видео-провайдер (yt-dlp) активирован.")

    def _get_domain(self, url):
        parsed = urlparse(url)
        netloc = parsed.netloc.lower()
        # Нормализация доменов (m.youtube.com -> youtube.com, youtu.be -> youtube.com)
        if 'youtube.com' in netloc or 'youtu.be' in netloc: return 'youtube.com'
        if 'm.' in netloc: netloc = netloc.replace('m.', '')
        if 'www.' in netloc: netloc = netloc.replace('www.', '')
        return netloc or 'unknown'

    @staticmethod
    def _parse_cookie_line(line):
        """Строка Netscape -> (domain, name) или None"""
        if line.startswith('#HttpOnly_'):
            line = line[len('#HttpOnly_'):]
        elif line.startswith('#') or not line.strip():
            return None
        parts = line.rstrip('\n').split('\t')
        if len(parts) != 7: return None
        return parts[0].lstrip('.'), parts[5]

    def _validate_cookie_file(self, cookie_file, domain):
        """Формат Netscape + признаки залогиненной сессии (BOM и комментарии игнорируются).
        Маркерные имена кук — по домену; для незнакомых доменов достаточно доменных кук."""
        domain_suffix = '.' + domain
        has_domain_cookies = False
        has_login = False
        login_names = self._COOKIE_LOGIN_MARKERS.get(domain, ())
        try:
            with open(cookie_file, 'r', encoding='utf-8-sig') as f:
                for line in f:
                    parsed = self._parse_cookie_line(line)
                    if parsed is None: continue
                    c_domain, c_name = parsed
                    if domain == c_domain or c_domain.endswith(domain_suffix):
                        has_domain_cookies = True
                        if c_name in login_names: has_login = True
        except OSError:
            return False, False
        if not login_names:
            has_login = has_domain_cookies
        return has_domain_cookies, has_login

    def _build_ytdlp_command(self, public_key, dest):
        domain = self._get_domain(public_key)
        output_template = self._OUTPUT_TEMPLATES.get(domain, '%(uploader)s/%(title)s.%(ext)s')
        cmd = ['yt-dlp', '--config-location', '/config/yt-dlp.conf', '--js-runtimes', 'node',
               '-P', dest, '-o', output_template]

        if not self.app.args.force:
            cmd.extend(['--download-archive', f'/history/{domain}.txt'])
        else:
            logger.warning("[ВНИМАНИЕ] Режим --force: история игнорируется. Существующие файлы не перезаписываются — докачается только отсутствующее.")

        cookie_file = f'/auth/{domain}.txt'
        if not os.path.exists(cookie_file):
            logger.info("[INFO] Куки не найдены. Скачивание анонимное.")
        else:
            has_cookies, has_login = self._validate_cookie_file(cookie_file, domain)
            if has_cookies:
                cmd.extend(['--cookies', cookie_file])
                if has_login:
                    logger.info(f"[INFO] Куки подключены: auth/{domain}.txt (сессия обнаружена).")
                else:
                    logger.warning(f"[ВНИМАНИЕ] auth/{domain}.txt без признаков залогиненной сессии. Контент 18+ и подписки будут недоступны.")
            else:
                logger.warning(f"[ВНИМАНИЕ] В auth/{domain}.txt нет кук для {domain} (неверный формат или чужой файл). Скачивание анонимное.")

        if self.app.FORCE_VPN and self.app.vpn_manager.proxies:
            proxy_port = self.app.vpn_manager.http_proxy_port
            cmd.extend(['--proxy', f'http://127.0.0.1:{proxy_port}'])

        cmd.append(public_key)
        return cmd

    def check_auth_status(self, link):
        domain = self._get_domain(link)
        cookie_file = f'/auth/{domain}.txt'

        if not os.path.exists(cookie_file):
            print(f"\n[-] {domain} (cookies): Не найдены.")
            return

        has_cookies, has_login = self._validate_cookie_file(cookie_file, domain)
        if not has_cookies:
            print(f"\n[-] {domain} (cookies): Файл не распознан (не Netscape-формат или нет кук домена).")
            return
        if has_login:
            print(f"\n[i] {domain} (cookies): Формат верный, залогиненная сессия обнаружена.")
        else:
            print(f"\n[!] {domain} (cookies): Формат верный, но признаков залогиненной сессии нет — закрытый контент может быть недоступен.")
        print(f"[i] {domain} (cookies): Выполняется тестовый запрос...")

        cmd = ['yt-dlp', '--simulate', '--no-warnings', '--cookies', cookie_file, link]
        if self.app.FORCE_VPN and self.app.vpn_manager.proxies:
            proxy_port = self.app.vpn_manager.http_proxy_port
            cmd.extend(['--proxy', f'http://127.0.0.1:{proxy_port}'])

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
            output = result.stdout + result.stderr
            self._print_auth_result(domain, output)
        except subprocess.TimeoutExpired:
            print(f"[-] {domain} (cookies): Ошибка (Таймаут). Сайт не ответил за 30 сек.")
        except (subprocess.SubprocessError, OSError) as e:
            print(f"[-] {domain} (cookies): Ошибка ({e}).")

    def _print_auth_result(self, domain, output):
        if 'ERROR:' in output or 'Forbidden' in output or 'Sign in' in output:
            print(f"[-] {domain} (cookies): Невалидны или требуется авторизация.")
            for line in output.splitlines():
                if 'ERROR' in line or 'Sign in' in line:
                    print(f"  -> {line}")
        else:
            print(f"[+] {domain} (cookies): Валидны (Доступ получен).")

    def _parse_progress(self, line):
        """Парсинг вывода yt-dlp и обновление статистики"""
        match = re.search(r'\[download\]\s+([\d.]+)\s*%\s+of\s+~?([\d.]+)\s*([KMG]i?B)\s+at\s+([\d.]+)\s*([KMG]i?B/s)\s+ETA\s+([\d:]+)', line)
        if match:
            self._update_standard_progress(match.groups())
            return

        match_frag = re.search(r'\[download\]\s+Downloading fragment\s+(\d+)\s+of\s+(\d+)', line)
        if match_frag:
            self._update_fragment_progress(int(match_frag.group(1)), int(match_frag.group(2)))

    def _update_standard_progress(self, groups):
        pct, size, unit, speed, sp_unit, eta = groups
        with self.app.stats_lock:
            new_downloaded = float(size) * (1024 if 'Ki' in unit else 1024**2 if 'Mi' in unit else 1024**3)
            delta = new_downloaded - self.app.stats.get('current_file_downloaded', 0)
            if delta > 0: self.app.stats['downloaded_bytes'] += delta
            self.app.stats['current_file_downloaded'] = new_downloaded
            self.app.stats['current_file_total'] = new_downloaded / (float(pct) / 100) if float(pct) > 0 else 0

        if time.time() - self.app.last_log_time > CONFIG["STATUS_LOG_INTERVAL"]:
            self.app.log(f"[СТАТУС] 📹 {pct}% of {size}{unit} | Ск: {speed}{sp_unit} | ETA: {eta}")
            self.app.last_log_time = time.time()

    def _update_fragment_progress(self, curr_frag, total_frag):
        if total_frag > 0:
            with self.app.stats_lock:
                total_size = self.app.stats.get('current_file_total', 0)
                if total_size > 0:
                    self.app.stats['current_file_downloaded'] = total_size * (curr_frag / total_frag)

        if time.time() - self.app.last_log_time > CONFIG["STATUS_LOG_INTERVAL"]:
            self.app.log(f"[СКАЧИВАНИЕ] 📹 Фрагмент {curr_frag}/{total_frag}")
            self.app.last_log_time = time.time()

    def _process_ytdlp_line(self, part):
        """Обработка одной строки вывода yt-dlp"""
        if 'ERROR:' in part:
            self._handle_ytdlp_error(part)
        elif '[Merger] Merging formats into' in part:
            with self.app.stats_lock:
                self.app.stats['downloaded'] += 1
            self.silence_grace = True
            logger.info(f"[yt-dlp] {part}")
        elif 'Downloading' in part and 'items' in part:
            self._handle_playlist_line(part)
        elif re.search(r'Downloading \d+ format', part):
            self._handle_format_line(part)
        elif '[download]' in part:
            self._handle_download_line(part)
        else:
            self._handle_misc_line(part)

    def _handle_misc_line(self, part):
        """Остальные строки: фиксация старта извлечения + сквозной лог"""
        if 'Extracting URL' in part and self.extraction_started_at is None:
            self.extraction_started_at = time.time()
            self.silence_grace = False
            self._log_video_event('extract')
        logger.info(f"[yt-dlp] {part}")

    def _handle_format_line(self, part):
        """Строка выбора формата: попытка скачивания (включая последующие пропуски)"""
        self.video_attempts += 1
        with self.app.stats_lock:
            self.app.stats['files_seen'] += 1
        self._antiban_register_success()
        if self.extraction_started_at is not None:
            self.extraction_started_at = None
        logger.info(f"[yt-dlp] {part}")

    def _handle_download_line(self, part):
        """Строки [download]: пропуски по архиву/файлу или прогресс"""
        if 'has already been recorded in the archive' in part or 'has already been downloaded' in part:
            self.video_skipped += 1
            logger.info(f"[yt-dlp] {part}")
            with self.app.stats_lock:
                self.app.stats['skipped'] += 1
            return
        self._parse_progress(part)

    def _handle_ytdlp_error(self, part):
        logger.error(f"[yt-dlp] {part}")
        self.last_video_error = part[:300]
        with self.app.stats_lock:
            self.app.stats['errors'] += 1
        match = re.search(r'\[youtube\] ([\w-]{11}):', part)
        if match:
            vid_id = match.group(1)
            if vid_id not in self.failed_links: self.failed_links.append(vid_id)
        if 'Forbidden' in part or '403' in part or 'Sign in' in part:
            logger.error("[ОШИБКА] Куки отклонены или требуется авторизация для этого видео.")
        if 'HTTP Error 429' in part:
            self.video_bans += 1
            self.antiban_series_count += 1
            self.antiban_successes_since_pause = 0
            self._log_video_event('429')
            self._antiban_maybe_pause()

    def _handle_playlist_line(self, part):
        match = re.search(r'Downloading\s+(\d+)\s+items', part)
        if match:
            self.app.total_files = max(self.app.total_files, int(match.group(1)))
        logger.info(f"[yt-dlp] {part}")

    def _finalize_video_telemetry(self, elapsed):
        """Строка сессии видео-провайдера в api_sessions (честные данные: без выдумок)"""
        telemetry = self.app.telemetry
        if telemetry is None:
            return
        requests_count = max(self.video_attempts, 0)
        avg_ms = int(elapsed / requests_count * 1000) if requests_count > 0 else 0
        telemetry.finalize_video_session(requests_count, self.video_bans, avg_ms)

    def _handle_download_result(self, process, start_time):
        """Обработка кода завершения yt-dlp и обновление статистики"""
        elapsed = time.time() - start_time
        with self.app.stats_lock:
            self.app.stats['cdn_time'] += elapsed
            if self.app.stats['downloaded_bytes'] == 0 and self.app.stats['current_file_total'] > 0:
                self.app.stats['downloaded_bytes'] += self.app.stats['current_file_total']

        self._write_failed_video_report()
        self._finalize_video_telemetry(elapsed)

        reconciled = max(0, self.video_attempts - len(self.failed_links) - self.video_skipped)
        with self.app.stats_lock:
            if reconciled > self.app.stats['downloaded']:
                self.app.stats['downloaded'] = reconciled

        if self.antiban_abort:
            self.last_video_error = "antiban abort: серия 429 не снялась паузами"
            logger.warning("[АНТИБАН] Сессия завершена ради охлаждения рейт-лимита.")
            return False
        if process.returncode == 0:
            logger.info("[УСПЕХ] Видео-скачивание завершено без ошибок.")
            return True
        if process.returncode == 1 and self.app.stats['downloaded'] > 0:
            logger.info("[УСПЕХ] Видео-скачивание завершено (были проигнорированы некоторые ошибки).")
            return True
        if self.app.stats['downloaded'] == 0:
            logger.warning("[ВНИМАНИЕ] Ни одного файла не скачано. Причина — в логе выше и в уведомлении.")
        logger.error(f"[ОШИБКА] yt-dlp завершился с кодом {process.returncode}. Сессия считается проваленной.")
        return False

    def _write_failed_video_report(self):
        """Отчет о нескачанных ссылках (адрес зависит от контейнера)"""
        if not self.failed_links:
            return
        report_path = f"/report/failed_video_links_{self.app.container_name}.txt"
        try:
            with open(report_path, "w", encoding="utf-8") as f:
                f.write(f"=== НЕ СКАЧАННЫЕ ВИДЕО ===\nДата: {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
                for vid in self.failed_links:
                    f.write(f"https://www.youtube.com/watch?v={vid}\n")
            logger.info(f"[INFO] Сформирован отчет об ошибках: {report_path}")
        except OSError:
            pass

    def _log_video_event(self, event):
        """Событие видео-сессии в telemetry.db (материал для проактивного антибана)"""
        telemetry = self.app.telemetry
        if telemetry is not None:
            telemetry.log_video_event(event)

    def _antiban_maybe_pause(self):
        """Ступень 'а': серия 429 -> прерываемая пауза; три паузы без успеха -> достойный отступ"""
        series_limit = CONFIG["ANTIBAN_429_SERIES"]
        if self.antiban_series_count < series_limit:
            return
        if not self.antiban_successes_since_pause and self.antiban_tg_notified:
            self.antiban_abort = True
            self._log_video_event('antiban_abort')
            logger.warning("[АНТИБАН] Паузы не помогают. Достойный отступ: остановка сессии.")
            return
        self.antiban_series_count = 0
        pause = CONFIG["ANTIBAN_PAUSE_SEC"]
        logger.warning(f"[АНТИБАН] Серия {series_limit} x 429. Пауза {pause} сек...")
        self.app.stop_event.wait(timeout=pause)
        if self.antiban_tg_notified is False:
            self.antiban_tg_notified = True
            self.app.log("[АНТИБАН] YouTube рейт-лимит: сессия замедлена автоматически.", show_progress=True)
        if not self.app.stop_event.is_set():
            self.antiban_successes_since_pause = 0

    def _antiban_register_success(self):
        """Успешное извлечение сбрасывает давление серии"""
        self.antiban_successes_since_pause += 1
        self.antiban_series_count = max(0, self.antiban_series_count - 1)

    def _terminate_child(self, process):
        """Гарантированная остановка потомка yt-dlp (защита от сироты при shutdown)"""
        if process is None: return
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        except OSError:
            pass

    _ARTIFACT_RE = re.compile(
        r'\.(f\d{1,5}\.(mp4|webm|m4a|opus|ogg)|temp\.(mp4|mkv|webm)|part(-Frag\d+)?|ytdl)$',
        re.IGNORECASE
    )

    def _cleanup_ytdlp_artifacts(self, dest):
        """Удаление обрывков yt-dlp старше часа (фрагменты форматов, temp-склейки, .part).
        Безопасно: вызывается до запуска нового yt-dlp; свежие файлы не трогаются
        (защита параллельных контейнеров, Q-009)."""
        removed = 0

        def walk(path):
            nonlocal removed
            try:
                with os.scandir(path) as it:
                    for entry in it:
                        if entry.is_dir(follow_symlinks=False):
                            walk(entry.path)
                        elif entry.is_file(follow_symlinks=False) and self._ARTIFACT_RE.search(entry.name):
                            try:
                                if time.time() - entry.stat().st_mtime > 3600:
                                    os.remove(entry.path)
                                    removed += 1
                            except OSError:
                                pass
            except OSError:
                pass

        walk(dest)
        if removed:
            logger.info(f"[INFO] Очистка обрывков yt-dlp: удалено {removed} файлов.")

    def process_video_download(self, public_key, dest):
        """Главный метод скачивания видео (Оркестратор)"""
        self._cleanup_ytdlp_artifacts(dest)
        cmd = self._build_ytdlp_command(public_key, dest)
        logger.info(f"[INFO] Запуск yt-dlp: {' '.join(cmd[:5])} ...")

        timeout = CONFIG["VIDEO_DOWNLOAD_TIMEOUT"]
        start_time = time.time()
        process = None
        try:
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, universal_newlines=True)
            self._monitor_ytdlp_output(process, timeout)
            process.wait()
            return self._handle_download_result(process, start_time)
        except FileNotFoundError:
            logger.critical("[КРИТИЧНО] yt-dlp не найден в системе! Проверьте Dockerfile.")
            return False
        except (subprocess.SubprocessError, OSError) as e:
            logger.critical(f"[КРИТИЧНО] Сбой видео-движка: {e}")
            return False
        finally:
            self._terminate_child(process)
            pot = getattr(self.app, '_pot_provider_process', None)
            if pot is not None and pot.poll() is None:
                pot.terminate()
                self.app._pot_provider_process = None

    def _monitor_ytdlp_output(self, process, timeout):
        q = queue.Queue()
        def reader():
            try:
                for line in process.stdout:
                    q.put(line)
            finally:
                q.put(None)
        threading.Thread(target=reader, daemon=True).start()
        last_output_time = time.time()
        while True:
            try:
                line = q.get(timeout=max(1.0, timeout / 10))
            except queue.Empty:
                if self._handle_silence(process, last_output_time, timeout):
                    return False
                continue
            if line is None: break
            last_output_time = time.time()
            if not self._consume_output(process, line):
                return False
        return True

    def _handle_silence(self, process, last_output_time, timeout):
        """True - сессия останавливается (watchdog зависания сработал)"""
        if time.time() - last_output_time <= timeout:
            return False
        if self.silence_grace:
            logger.info("[ИНФО] Тихая фаза (склейка/постобработка) длится дольше таймаута — watchdog ожидает завершения.")
            return False
        logger.error(f"[ОШИБКА] yt-dlp завис (нет вывода {timeout} сек). Принудительная остановка.")
        process.kill()
        return True

    def _consume_output(self, process, line):
        """Разбор строки вывода. False - требуется остановка сессии (достойный отступ)"""
        for part in line.split('\r'):
            part = part.strip()
            if not part: continue
            self.app.check_status()
            self._check_vpn_status(process)
            self._process_ytdlp_line(part)
            if self.antiban_abort:
                self._abort_session(process)
                return False
        return True

    def _abort_session(self, process):
        logger.warning("[АНТИБАН] Останавливаем yt-dlp (достойный отступ).")
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()

    def _check_vpn_status(self, process):
        if not self.app.FORCE_VPN: return
        vpn_mgr = self.app.vpn_manager
        if vpn_mgr.critical_failure:
            logger.error("[КРИТИЧНО] Все VPN сервера недоступны. Остановка скачивания.")
            process.kill()
            return False
        elif vpn_mgr.process and vpn_mgr.process.poll() is not None:
            logger.warning("[ВНИМАНИЕ] VPN упал. Приостанавливаем yt-dlp (SIGSTOP)...")
            try: os.kill(process.pid, signal.SIGSTOP)
            except ProcessLookupError: pass
            if vpn_mgr.switch_to_next_server():
                logger.info("[УСПЕХ] VPN переключен. Возобновляем yt-dlp (SIGCONT)...")
                if self.app.stop_event.wait(timeout=3):
                    try: os.kill(process.pid, signal.SIGCONT)
                    except ProcessLookupError: pass
                    self._terminate_child(process)
                    return False
                try: os.kill(process.pid, signal.SIGCONT)
                except ProcessLookupError: pass
            else:
                logger.error("[КРИТИЧНО] Все VPN сервера недоступны. Завершение работы.")
                process.kill()
                return False

    # Заглушки методов Яндекса
    def fetch_yandex_api(self, url, params): pass
    def fetch_api_items(self, public_key, current_path, api_url): return [], None
    def map_yandex_item(self, raw_item): return {}
