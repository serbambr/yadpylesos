import http.cookiejar
import os
import random
import time

import requests
import urllib3

# Импорт общих компонентов из основного скрипта
from yadpylesos import CONFIG, PROFILES, BaseCloudProvider, Colors, logger


# ==========================================
# КЛАСС: YandexAPIService
# ==========================================
class YandexAPIService(BaseCloudProvider):
    def __init__(self, app):
        super().__init__(app)
        self.session_api = requests.Session()
        self.session_api.headers.update(PROFILES.get(app.browser_choice, PROFILES['1']))
        self.session_api.headers.update({'Referer': 'https://disk.yandex.ru/'})

        self.session_cdn = requests.Session()
        self.session_cdn.headers.update(PROFILES.get(app.browser_choice, PROFILES['1']))

        if app.args.ssl_off:
            self.session_api.verify = False
            self.session_cdn.verify = False
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            print(f"{Colors.YELLOW}[ВНИМАНИЕ] Проверка SSL-сертификатов ОТКЛЮЧЕНА (--ssl-off).{Colors.NC}")

        if app.yad_token:
            self._init_auth_and_cookies(app)

        self.ban_wait_time = CONFIG["INITIAL_BAN_WAIT"]
        self.current_api_pause = 4.0
        self.consecutive_errors = 0
        self.MIN_API_PAUSE, self.MAX_API_PAUSE = 1.0, 5.0

    def _init_auth_and_cookies(self, app):
        # 1. Проверяем токен изолированно
        token_ok = False
        if app.yad_token:
            token_ok = self._test_credential(app.yad_token, is_token=True)
            if token_ok:
                self.session_api.headers.update({'Authorization': f'OAuth {app.yad_token}'})
                app.auth_status_msg = "OAuth 2.0"

        # 2. Проверяем куки изолированно (если файла нет, пропускаем)
        cookies_ok = False
        if os.path.exists(app.cookie_file):
            cookies_ok = self._test_credential(app.cookie_file, is_token=False)
            if cookies_ok:
                self.session_api.cookies = http.cookiejar.MozillaCookieJar(app.cookie_file)
                try: self.session_api.cookies.load(ignore_discard=True)
                except (http.cookiejar.LoadError, OSError): pass
                if not token_ok: app.auth_status_msg = "Cookies"

        # 3. Формируем честный отчет для лога
        self._set_auth_details(app, token_ok, cookies_ok)

    def _set_auth_details(self, app, token_ok, cookies_ok):
        methods = []
        if token_ok: methods.append("OAuth Токен")
        if cookies_ok: methods.append("Cookies")
        
        if methods:
            methods_str = " + ".join(methods)
            app.auth_details_msg = f"Доступ к API разрешен (200 OK). В сессию загружены: {methods_str}."
            if os.path.exists(app.cookie_file) and not cookies_ok:
                app.auth_details_msg += " (Cookies отклонены Яндексом как невалидные)."
        else:
            app.auth_status_msg = "Анонимный"
            app.auth_details_msg = "Токен и Cookies не найдены или невалидны. Работа анонимно."

    def _test_credential(self, credential, is_token=True):
        """Изолированная проверка токена или куков"""
        try:
            sess = requests.Session()
            if is_token:
                sess.headers.update({'Authorization': f'OAuth {credential}'})
            else:
                sess.cookies = http.cookiejar.MozillaCookieJar(credential)
                sess.cookies.load(ignore_discard=True)
            
            resp = sess.get('https://cloud-api.yandex.net/v1/disk/', timeout=10)
            return resp.status_code == 200
        except (requests.exceptions.RequestException, http.cookiejar.LoadError, OSError):
            return False

    def _simulate_network_issues(self):
        rand_val = random.random()
        if rand_val < 0.6:
            class FakeResponse:
                def __init__(self):
                    self.status_code = 429
                    self.headers = {}
                    self.text = '<html>ban</html>'
                def json(self): return {'error': 'simulated ban'}
            return FakeResponse()
        elif rand_val < 0.8:
            raise requests.exceptions.ConnectionError("Simulated connection error")
        else:
            raise requests.exceptions.Timeout("Simulated timeout")

    def _is_silent_ban(self, resp):
        content_type = resp.headers.get('Content-Type', '')
        if 'text/html' in content_type: return True
        try:
            if '<html' in resp.text[:200].lower(): return True
        except (AttributeError, TypeError): pass
        return False

    def _handle_vpn_failover(self):
        if not (self.app.vpn_manager.available and not self.session_api.proxies):
            return False 
        self.app.log("[INFO] Проверка доступности интернета напрямую...")
        try:
            test_resp = self.session_cdn.get('http://www.gstatic.com/generate_204', timeout=10)
            if test_resp.status_code != 204:
                self.app.log("[КРИТИЧНО] Интернет недоступен. VPN не поможет. Завершение работы (Fail-Fast).")
                return False
        except requests.exceptions.RequestException:
            return False
        self.app.log("[КРИТИЧНО] Интернет работает. Вероятно, Яндекс заблокировал IP. Запуск VPN...")
        if self.app.vpn_manager.start():
            if self.app.vpn_manager.test_tunnel():
                self.app.vpn_manager.proxies = {'http': 'http://127.0.0.1:7890', 'https': 'http://127.0.0.1:7890'}
                self.session_api.proxies = self.app.vpn_manager.proxies
                self.app.log("[УСПЕХ] VPN-туннель активирован и протестирован. Трафик API переключен.")
                self.app.vpn_manager.start_watchdog()
                return True
            else:
                self.app.log("[ОШИБКА] VPN-туннель запущен, но тест gstatic не прошел.")
        self.app.log("[ОШИБКА] Не удалось запустить или протестировать VPN процесс. Завершение работы (Fail-Fast).")
        return False

    def _handle_api_429(self, resp, url, params):
        with self.app.stats_lock:
            self.app.stats['retries'] += 1
        self.app.browser_choice = str(int(self.app.browser_choice) % 3 + 1)
        self.session_api.headers.update(PROFILES.get(self.app.browser_choice, PROFILES['1']))
        self.app.log(f"[ВНИМАНИЕ] Смена User-Agent на {self.app.browser_choice} из-за 429.")
        self.current_api_pause = min(self.MAX_API_PAUSE, self.current_api_pause * 1.5)
        retry_after = resp.headers.get('Retry-After')
        wait_time = int(retry_after) + 10 if retry_after and retry_after.isdigit() else self.ban_wait_time
        self.ban_wait_time = min(self.ban_wait_time * 2, CONFIG["MAX_BAN_WAIT"])
        self.consecutive_errors += 1
        
        if self.consecutive_errors >= CONFIG["MAX_RETRIES"]:
            self.app.log("[КРИТИЧНО] Пауза. Сеть или API недоступны.")
            if self._handle_vpn_failover():
                self.consecutive_errors = 0
                self.ban_wait_time = CONFIG["INITIAL_BAN_WAIT"]
                self.current_api_pause = self.MIN_API_PAUSE
                return self.fetch_yandex_api(url, params)
            self.app.db.save_cache(self.app)
            self.app.db.save_global_state(self.app)
            raise SystemExit("vpn tunnel test failed")
            
        self.app.set_status(f"[БАН 429] Ожидание {wait_time} сек")
        self.app.stop_event.wait(timeout=wait_time)
        return 'continue'

    def _handle_api_http_errors(self, resp):
        if resp.status_code in (401, 403):
            logger.error(f"[ОШИБКА] Доступ запрещен ({resp.status_code}). Файл или ссылка недоступны.")
            return 'fail'
        if 500 <= resp.status_code <= 599:
            logger.warning(f"[ВНИМАНИЕ] Сервер Яндекса недоступен ({resp.status_code}). Повтор через 15 сек.")
            self.app.stop_event.wait(timeout=15)
            return 'continue'
        return None

    def _handle_api_exceptions(self, e, url, params):
        if isinstance(e, requests.exceptions.Timeout):
            self.app.set_status("[ОШИБКА: ТИП - TIMEOUT] Ожидание 60 сек")
        elif isinstance(e, requests.exceptions.ConnectionError):
            self.app.set_status("[ОШИБКА: ТИП - CONNECTION] Ожидание 60 сек")
        elif isinstance(e, requests.exceptions.RequestException):
            self.app.set_status(f"[ОШИБКА: ТИП - UNKNOWN] {str(e)[:50]}. Ожидание 60 сек")
        else:
            return False
        logger.info(f"[АУДИТ] URL: {url}, Params: {params}")
        self.app.stop_event.wait(timeout=60)
        self.consecutive_errors += 1
        return True

    def _get_valid_api_response(self, resp, url, params):
        http_err = self._handle_api_http_errors(resp)
        if http_err == 'fail': return None
        if http_err == 'continue': return 'retry'
        
        if resp.status_code == 429:
            if self._handle_api_429(resp, url, params) == 'continue': return 'retry'
            return None
            
        if self._is_silent_ban(resp):
            with self.app.stats_lock: self.app.stats['retries'] += 1
            logger.warning("[ВНИМАНИЕ] API вернуло HTML вместо JSON. Возможен тихий бан.")
            self.app.stop_event.wait(timeout=self.ban_wait_time)
            self.ban_wait_time = min(self.ban_wait_time * 2, CONFIG["MAX_BAN_WAIT"])
            return 'retry'
            
        resp_json = resp.json()
        if 'error' in resp_json:
            with self.app.stats_lock: self.app.stats['retries'] += 1
            self.current_api_pause = min(self.MAX_API_PAUSE, self.current_api_pause * 1.5)
            self.app.set_status(f"[БАН API] {resp_json.get('message', '')}. Ожидание {self.ban_wait_time} сек")
            self.app.stop_event.wait(timeout=self.ban_wait_time)
            self.ban_wait_time = min(self.ban_wait_time * 2, CONFIG["MAX_BAN_WAIT"])
            return 'retry'
        return resp_json

    def fetch_yandex_api(self, url, params):
        for attempt in range(CONFIG["MAX_RETRIES"]):
            try:
                t = time.time()
                if self.app.simulate_ban:
                    resp = self._simulate_network_issues()
                    with self.app.stats_lock: 
                        self.app.stats['api_time'] += time.time() - t
                        self.app.stats['api_req_count'] += 1
                else:
                    resp = self.session_api.get(url, params=params, timeout=CONFIG["API_TIMEOUT"])
                    with self.app.stats_lock: 
                        self.app.stats['api_time'] += time.time() - t
                        self.app.stats['api_req_count'] += 1
                    
                result = self._get_valid_api_response(resp, url, params)
                if result == 'retry': continue
                if result is None: return None
                    
                self.ban_wait_time = CONFIG["INITIAL_BAN_WAIT"]
                self.current_api_pause = max(self.MIN_API_PAUSE, self.current_api_pause - 0.1)
                self.consecutive_errors = 0
                return result
            except Exception as e:
                if not self._handle_api_exceptions(e, url, params):
                    raise
        return None

    def fetch_api_items(self, public_key, current_path, api_url):
        params = {"public_key": public_key, "limit": 1000}
        if current_path: params["path"] = current_path
        offset = 0; all_items = []; rev = None
        while True:
            params["offset"] = offset
            resp = self.fetch_yandex_api(api_url, params)
            if not resp: return None, None
            if '_embedded' not in resp and 'error' not in resp:
                logger.error(f"{Colors.RED}[ТРЕВОГА: ЯНДЕКС ИЗМЕНИЛ API] Отсутствует ключ _embedded.{Colors.NC}")
                self.app.db.save_cache(self.app)
                raise SystemExit("яндекс изменил api")
            items = resp.get('_embedded', {}).get('items', [])
            if not items: break
            all_items.extend(items)
            if offset == 0: rev = resp.get('revision')
            if len(items) < 1000: break
            offset += 1000
        return all_items, rev

    def preflight_api_check(self):
        try:
            resp = self.session_api.get('https://cloud-api.yandex.net/v1/disk/', timeout=10)
            if resp.status_code == 401 and 'Authorization' in self.session_api.headers:
                self.app.log("[КРИТИЧНО] OAuth токен невалиден (401).")
                self.session_api.headers.pop('Authorization', None)
                resp_cookies = self.session_api.get('https://cloud-api.yandex.net/v1/disk/', timeout=10)
                if resp_cookies.status_code == 200:
                    self.app.log("[УСПЕХ] Cookies валидны! Работаем по кукам.")
                    self.app.auth_status_msg = "Только Cookies"
                else:
                    self.app.log("[ВНИМАНИЕ] Переход в анонимный режим через 15 сек...")
                    time.sleep(15)
                    self.app.auth_status_msg = "Анонимный"
            elif resp.status_code == 200:
                self.app.log("[INFO] Проверка API: Токен валиден (200 OK).")
        except requests.exceptions.RequestException as e:
            self.app.log(f"[ВНИМАНИЕ] Ошибка проверки API: {e}")

    def map_yandex_item(self, raw_item):
        return {'api_path': raw_item.get('path', ''), 'name': raw_item.get('name', ''), 'size': raw_item.get('size', 0), 'type': raw_item.get('type', ''), 'md5': raw_item.get('md5', '')}
