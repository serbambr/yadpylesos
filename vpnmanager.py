import glob
import json
import os
import socket
import subprocess
import threading
import time
import urllib.parse
from datetime import datetime

import requests

from yadpylesos import CONFIG, Colors, logger


class VpnManager:
    """BL-VPN: Автономный модуль управления VPN (Xray-core)"""
    def __init__(self, app, vpn_dir, report_dir, stop_event):
        self.app = app
        self.vpn_dir = vpn_dir
        self.report_dir = report_dir
        self.stop_event = stop_event
        self.runtime_file = os.path.join(vpn_dir, 'config_runtime.json')
        self.log_file = os.path.join(report_dir, 'xray.log')
        self.audit_file = os.path.join(report_dir, 'vpn_audit.log')
        self.process = None
        self.available = False
        self.proxies = None
        try:
            self.proxy_port = int(str(os.environ.get('PROXY_PORT', '10808')).strip())
        except ValueError:
            raise SystemExit("[КРИТИЧНО] PROXY_PORT не является числом. Проверьте .env.")
        self.http_proxy_port = self.proxy_port + 1 # 10809
        self.http_proxy_url = f"http://127.0.0.1:{self.http_proxy_port}"

        # Новые переменные для Failover
        self.all_proxies = []
        self.current_proxy_idx = 0
        self.critical_failure = False

    def _parse_uri(self, line):
        """Парсинг vless://, trojan:// в формат Xray JSON"""
        try:
            parsed = urllib.parse.urlparse(line)
            name = urllib.parse.unquote(parsed.fragment) if parsed.fragment else f"{parsed.hostname}:{parsed.port}"
            proto = parsed.scheme.lower()
            params = {}
            for p in parsed.query.split('&'):
                if '=' in p:
                    k, v = p.split('=', 1)
                    params[k] = urllib.parse.unquote(v)

            if proto == 'vless':
                user = {'id': parsed.username, 'encryption': 'none'}
                if params.get('flow'): user['flow'] = params.get('flow')

                stream = {'network': params.get('type', 'tcp')}
                security = params.get('security', 'none')

                if security == 'reality':
                    stream['security'] = 'reality'
                    stream['realitySettings'] = {
                        'serverName': params.get('sni', ''),
                        'fingerprint': params.get('fp', 'chrome'),
                        'publicKey': params.get('pbk', ''),
                        'shortId': params.get('sid', ''),
                        'spiderX': params.get('spx', '/')
                    }
                elif security == 'tls':
                    stream['security'] = 'tls'
                    stream['tlsSettings'] = {'serverName': params.get('sni', '')}

                return {
                    'tag': name,
                    'protocol': 'vless',
                    'settings': {'vnext': [{'address': parsed.hostname, 'port': parsed.port, 'users': [user]}]},
                    'streamSettings': stream
                }
            elif proto == 'trojan':
                stream = {'network': params.get('type', 'tcp'), 'security': 'tls', 'tlsSettings': {'serverName': params.get('sni', parsed.hostname)}}
                return {
                    'tag': name,
                    'protocol': 'trojan',
                    'settings': {'servers': [{'address': parsed.hostname, 'port': parsed.port, 'password': parsed.username}]},
                    'streamSettings': stream
                }
        except (ValueError, TypeError, AttributeError, KeyError) as e:  # Fix BLE001
            logger.warning(f"[ВНИМАНИЕ] VPN парсер URI: Ошибка чтения '{line[:50]}': {e}")
        return None

    def _parse_xray_json(self, content):
        """Извлечение outbound из готового JSON конфига Xray"""
        try:
            data = json.loads(content)
            outbounds = data.get('outbounds', [])
            for ob in outbounds:
                if ob.get('tag') in ('proxy', 'vless', 'trojan', 'hysteria'):
                    proto = ob.get('protocol')
                    if proto == 'vless':
                        addr = ob.get('settings', {}).get('vnext', [{}])[0].get('address', 'unknown')
                    elif proto == 'trojan':
                        addr = ob.get('settings', {}).get('servers', [{}])[0].get('address', 'unknown')
                    else:
                        addr = 'unknown'

                    port = ob.get('settings', {}).get('vnext', [{}])[0].get('port', 'unknown') if proto == 'vless' else ob.get('settings', {}).get('servers', [{}])[0].get('port', 'unknown')
                    base_name = data.get('remarks', '') or f"{proto}_{addr}:{port}"
                    ob['tag'] = base_name
                    return ob
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as e:  # Fix BLE001
            logger.warning(f"[ВНИМАНИЕ] VPN парсер JSON: Ошибка чтения: {e}")
        return None

    def _load_proxies_from_files(self):
        proxies = []
        for filepath in glob.glob(os.path.join(self.vpn_dir, 'link*.txt')):
            self._load_proxies_from_file(filepath, proxies)

        self._ensure_unique_names(proxies)
        return proxies

    def _load_proxies_from_file(self, filepath, proxies):
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read().strip()
                if not content: return
                if content.startswith('{'):
                    p = self._parse_xray_json(content)
                    if p: proxies.append(p)
                else:
                    for line in content.splitlines():
                        line = line.strip()
                        if not line or line.startswith('#'): continue
                        p = self._parse_uri(line)
                        if p: proxies.append(p)
        except OSError: pass

    def _ensure_unique_names(self, proxies):
        seen_tags = set()
        for i, p in enumerate(proxies):
            base_tag = p.get('tag', f'proxy_{i}')
            if base_tag == 'proxy' or not base_tag: base_tag = f'proxy_{i}'
            new_tag = base_tag
            counter = 1
            while new_tag in seen_tags:
                new_tag = f"{base_tag}_{counter}"
                counter += 1
            seen_tags.add(new_tag)
            p['tag'] = new_tag

    def _generate_config(self, proxy=None):
        """Генерация config_runtime.json: сервер из ротации по индексу либо явно переданный (тест сервера)"""
        if not self.all_proxies:
            self.all_proxies = self._load_proxies_from_files()

        if proxy is None:
            if not self.all_proxies: return None
            proxy = self.all_proxies[self.current_proxy_idx]

        self.active_server_name = proxy.get('tag', 'unknown')
        outbound = dict(proxy)
        outbound['tag'] = 'proxy'

        inbounds = [
            {"tag": "socks-in", "port": self.proxy_port, "listen": "127.0.0.1", "protocol": "socks", "settings": {"udp": True}},
            {"tag": "http-in", "port": self.http_proxy_port, "listen": "127.0.0.1", "protocol": "http", "settings": {}}
        ]
        outbounds = [outbound, {"tag": "direct", "protocol": "freedom", "settings": {}}, {"tag": "block", "protocol": "blackhole", "settings": {}}]
        routing = {"domainStrategy": "AsIs", "rules": [{"type": "field", "ip": ["geoip:private"], "outboundTag": "direct"}]}

        return {
            "log": {"loglevel": "warning"},
            "inbounds": inbounds,
            "outbounds": outbounds,
            "routing": routing
        }

    def generate_config(self):
        config_data = self._generate_config()
        if not config_data:
            self.available = False
            return False
        try:
            with open(self.runtime_file, 'w', encoding='utf-8') as f:
                json.dump(config_data, f, indent=2)
            self.available = True
            logger.info(f"[INFO] VPN конфиг: Сгенерирован для сервера {self.current_proxy_idx + 1}/{len(self.all_proxies)}.")
            return True
        except (OSError, TypeError) as e:
            logger.error(f"[ОШИБКА] Не удалось сгенерировать JSON: {e}")
            self.available = False
            return False

    def _wait_for_port(self, port, timeout=15):
        start_time = time.time()
        while time.time() - start_time < timeout:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(1)
                if s.connect_ex(('127.0.0.1', port)) == 0: return True
            time.sleep(1)
        return False

    def switch_to_next_server(self):
        """Переключение на следующий доступный сервер"""
        self.current_proxy_idx += 1
        if self.current_proxy_idx >= len(self.all_proxies):
            self.critical_failure = True
            logger.error("[КРИТИЧНО] Все доступные VPN серверы исчерпаны.")
            return False

        logger.warning(f"[ВНИМАНИЕ] Переключение на сервер {self.current_proxy_idx + 1}/{len(self.all_proxies)}...")
        self.stop()
        return self.start()

    def start(self, skip_ip_check=False):
        if self.process and self.process.poll() is None:
            logger.info("[INFO] VPN процесс уже запущен.")
            return True
        if not self.generate_config():
            logger.error("[КРИТИЧНО] Не найдено ни одного VPN сервера. Проверьте link*.txt")
            return False

        try:
            if os.path.exists(self.log_file) and os.path.getsize(self.log_file) > CONFIG["MAX_LOG_SIZE"]:
                os.remove(self.log_file)
        except OSError: pass

        logger.info("[INFO] Запуск процесса Xray (VPN)...")
        try:
            env = os.environ.copy()
            env['XRAY_LOCATION_ASSET'] = '/etc/xray'

            with open(self.log_file, 'a', encoding='utf-8') as xray_log:
                self.process = subprocess.Popen(['xray', 'run', '-c', self.runtime_file], stdout=xray_log, stderr=subprocess.STDOUT, env=env)

                if not self._wait_for_port(self.http_proxy_port, 15):
                    logger.error(f"[КРИТИЧНО] Порт VPN ({self.http_proxy_port}) не открылся. Проверьте report/xray.log")
                    return False

                if skip_ip_check:
                    logger.info("[INFO] VPN-туннель поднят. Ожидание команд тестирования...")
                    return True

                return self._verify_vpn_started()
        except OSError as e:
            logger.error(f"[ОШИБКА] Не удалось запустить Xray: {e}")
            return False

    def _verify_vpn_started(self):
        self.stop_event.wait(timeout=3)
        if self.stop_event.is_set(): return True
        ip_address = "Н/Д"
        proxies = {'http': f'http://127.0.0.1:{self.http_proxy_port}', 'https': f'http://127.0.0.1:{self.http_proxy_port}'}
        try:
            ip_resp = requests.get('https://api.ipify.org?format=json', proxies=proxies, timeout=15)
            if ip_resp.status_code == 200:
                ip_address = ip_resp.json().get('ip', 'Н/Д')
        except (requests.RequestException, ValueError) as e:  # Fix BLE001
            logger.warning(f"[ВНИМАНИЕ] Не удалось получить IP: {e}")

        logger.info(f"[УСПЕХ] VPN-туннель активирован. IP: {ip_address}")
        self._log_vpn_event("VPN запущен", f"IP: {ip_address}")
        if self.app.telemetry:
            self.app.telemetry.log_vpn_event("Xray", ip_address, "started", "xray")
        return True

    def stop(self):
        if self.process and self.process.poll() is None:
            logger.info("[INFO] Остановка процесса Xray...")
            self.process.terminate()
            try: self.process.wait(timeout=5)
            except subprocess.TimeoutExpired: self.process.kill()
            self.process = None
        try:
            if os.path.exists(self.runtime_file): os.remove(self.runtime_file)
        except OSError: pass

    def test_tunnel(self):
        try:
            proxies = {'http': f'http://127.0.0.1:{self.http_proxy_port}', 'https': f'http://127.0.0.1:{self.http_proxy_port}'}
            resp = requests.get('http://www.gstatic.com/generate_204', proxies=proxies, timeout=10)
            return resp.status_code == 204
        except requests.RequestException:  # Fix BLE001
            return False

    def _watchdog_loop(self):
        failed_checks = 0
        while not self.stop_event.wait(60):
            if self.critical_failure or (not self.available and not self.proxies): continue

            if self.process is None or self.process.poll() is not None:
                if not self._handle_vpn_crash(): break
                failed_checks = 0
                continue

            if self.test_tunnel():
                failed_checks = 0
            else:
                failed_checks = self._handle_tunnel_failure(failed_checks)

    def _handle_vpn_crash(self):
        logger.error("[КРИТИЧНО] Процесс Xray упал!")
        return self.switch_to_next_server()

    def _handle_tunnel_failure(self, failed_checks):
        failed_checks += 1
        if failed_checks >= 5:
            logger.error("[КРИТИЧНО] Xray завис (5 неудачных проверок). Принудительная остановка...")
            self.process.terminate()
            try: self.process.wait(timeout=5)
            except subprocess.TimeoutExpired: self.process.kill()
        return failed_checks

    def start_watchdog(self):
        for t in threading.enumerate():
            if t.name == "VpnWatchdog": return
        watchdog_thread = threading.Thread(target=self._watchdog_loop, name="VpnWatchdog", daemon=True)
        watchdog_thread.start()
        logger.info("[INFO] API Watchdog запущен (мониторинг каждые 60 сек).")

    def test_all_servers(self):
        """Массовое тестирование всех серверов из конфига"""
        proxies_list = self._load_proxies_from_files()
        if not proxies_list:
            print(f"{Colors.RED}[ОШИБКА]{Colors.NC} Сервера не найдены.")
            return

        print(f"Найдено серверов: {len(proxies_list)}. Начинаю тестирование...\n")
        print(f"{'Имя сервера':<35} | {'IP-адрес':<18} | {'Статус':<15}")
        print("-" * 75)

        for proxy in proxies_list:
            self._test_single_server(proxy)
        self.stop()

    def _test_single_server(self, proxy):
        server_name = proxy.get('tag', 'unknown')
        config_data = self._generate_config(proxy=proxy)
        if not config_data: return

        try:
            with open(self.runtime_file, 'w') as f:
                json.dump(config_data, f)
        except (OSError, TypeError): pass

        if self.process:
            self.process.terminate()
            try: self.process.wait(timeout=5)
            except subprocess.TimeoutExpired: self.process.kill()

        try:
            env = os.environ.copy()
            env['XRAY_LOCATION_ASSET'] = '/etc/xray'
            with open(self.log_file, 'a') as xray_log:
                self.process = subprocess.Popen(['xray', 'run', '-c', self.runtime_file], stdout=xray_log, stderr=subprocess.STDOUT, env=env)
            self.stop_event.wait(timeout=3)

            if not self._wait_for_port(self.http_proxy_port, 10):
                print(f"{server_name:<35} | {'Н/Д':<18} | {Colors.RED}Нет связи{Colors.NC}")
                return

            self._check_server_ip(server_name)
        except (OSError, subprocess.SubprocessError) as e:
            print(f"{server_name:<35} | {'Н/Д':<18} | {Colors.RED}Ошибка ({e}){Colors.NC}")

    def _check_server_ip(self, server_name):
        proxies = {'http': f'http://127.0.0.1:{self.http_proxy_port}', 'https': f'http://127.0.0.1:{self.http_proxy_port}'}
        ip_address = "Н/Д"
        ip_success = False

        try:
            ip_resp = requests.get('https://api.ipify.org?format=json', proxies=proxies, timeout=15)
            if ip_resp.status_code == 200:
                ip_address = ip_resp.json().get('ip', 'Н/Д')
                ip_success = True
        except (requests.RequestException, ValueError): pass

        if not ip_success:
            try:
                ip_resp = requests.get('http://ip-api.com/json', proxies=proxies, timeout=15)
                if ip_resp.status_code == 200:
                    ip_address = ip_resp.json().get('query', 'Н/Д')
                    ip_success = True
            except (requests.RequestException, ValueError): pass

        test_resp = requests.get('http://www.gstatic.com/generate_204', proxies=proxies, timeout=15)
        if test_resp.status_code == 204 and ip_success:
            print(f"{server_name:<35} | {ip_address:<18} | {Colors.GREEN}Успех{Colors.NC}")
        else:
            print(f"{server_name:<35} | {'Н/Д':<18} | {Colors.RED}Нет связи{Colors.NC}")

    def _log_vpn_event(self, event_type, server_name="Н/Д"):
        try:
            with open(self.audit_file, 'a', encoding='utf-8') as f:
                timestamp = datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S')
                f.write(f"[{timestamp}] Событие: {event_type} | Сервер: {server_name}\n")
        except OSError: pass
