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
import yaml

from yadpylesos import CONFIG, logger


# ==========================================
# КЛАСС: VpnManager
# ==========================================
class VpnManager:
    """BL-VPN: Автономный модуль управления VPN (mihomo)"""
    def __init__(self, app, vpn_dir, report_dir, stop_event):
        self.app = app
        self.vpn_dir = vpn_dir
        self.report_dir = report_dir
        self.stop_event = stop_event
        self.runtime_file = os.path.join(vpn_dir, 'config_runtime.yaml')
        self.log_file = os.path.join(report_dir, 'mihomo.log')
        self.audit_file = os.path.join(report_dir, 'vpn_audit.log')
        self.process = None
        self.available = False
        self.proxies = None 

    def _parse_uri(self, line):
        try:
            parsed = urllib.parse.urlparse(line)
            name = urllib.parse.unquote(parsed.fragment) if parsed.fragment else f"{parsed.hostname}:{parsed.port}"
            proto = parsed.scheme.lower()
            raw_params = parsed.query.split('&')
            params = {}
            for p in raw_params:
                if '=' in p:
                    k, v = p.split('=', 1)
                    params[k] = urllib.parse.unquote(v)
            if proto == 'trojan': return self._parse_trojan(parsed, name, params)
            elif proto == 'vless': return self._parse_vless(parsed, name, params)
            elif proto in ('hysteria2', 'hy2'): return self._parse_hysteria2(parsed, name, params)
        except (ValueError, TypeError, AttributeError) as e:
            logger.warning(f"[ВНИМАНИЕ] VPN парсер URI: Ошибка чтения '{line[:50]}': {e}")
        return None

    def _parse_trojan(self, parsed, name, params):
        return {'name': name, 'type': 'trojan', 'server': parsed.hostname, 'port': parsed.port, 'password': urllib.parse.unquote(parsed.username) if parsed.username else "", 'sni': params.get('sni', parsed.hostname), 'skip-cert-verify': True, 'tls': True}

    def _parse_vless(self, parsed, name, params):
        proxy = {'name': name, 'type': 'vless', 'server': parsed.hostname, 'port': parsed.port, 'uuid': urllib.parse.unquote(parsed.username) if parsed.username else "", 'network': params.get('type', 'tcp'), 'servername': params.get('sni', params.get('peer', parsed.hostname)), 'skip-cert-verify': True}
        security = params.get('security', 'none')
        if security in ('tls', 'reality'): proxy['tls'] = True
        if params.get('flow'): proxy['flow'] = params.get('flow')
        if security == 'reality':
            proxy['reality-opts'] = {'public-key': params.get('pbk', ''), 'short-id': params.get('sid', '')}
            proxy['client-fingerprint'] = params.get('fp', 'chrome')
        return proxy

    def _parse_hysteria2(self, parsed, name, params):
        return {'name': name, 'type': 'hysteria2', 'server': parsed.hostname, 'port': parsed.port, 'password': urllib.parse.unquote(parsed.username) if parsed.username else "", 'sni': params.get('sni', parsed.hostname), 'skip-cert-verify': True}

    def _parse_xray_json(self, content):
        try:
            data = json.loads(content)
            outbounds = data.get('outbounds', [])
            for ob in outbounds:
                if ob.get('tag') in ('proxy', 'vless', 'trojan', 'hysteria'):
                    proto = ob.get('protocol')
                    settings = ob.get('settings', {})
                    stream = ob.get('streamSettings', {})
                    if proto == 'vless':
                        vnext = settings.get('vnext', [{}])[0]
                        users = vnext.get('users', [{}])[0]
                        proxy = {'name': data.get('remarks', 'VLESS_JSON'), 'type': 'vless', 'server': vnext.get('address'), 'port': vnext.get('port'), 'uuid': users.get('id'), 'network': stream.get('network', 'tcp'), 'servername': stream.get('realitySettings', {}).get('serverName', stream.get('tlsSettings', {}).get('serverName')), 'skip-cert-verify': True}
                        if users.get('flow'): proxy['flow'] = users.get('flow')
                        if stream.get('security') == 'reality':
                            proxy['tls'] = True
                            proxy['reality-opts'] = {'public-key': stream.get('realitySettings', {}).get('publicKey', ''), 'short-id': stream.get('realitySettings', {}).get('shortId', '')}
                            proxy['client-fingerprint'] = stream.get('realitySettings', {}).get('fingerprint', 'chrome')
                        return proxy
                    elif proto == 'trojan':
                        server = settings.get('servers', [{}])[0]
                        return {'name': data.get('remarks', 'TROJAN_JSON'), 'type': 'trojan', 'server': server.get('address'), 'port': server.get('port'), 'password': server.get('password'), 'sni': stream.get('tlsSettings', {}).get('serverName', server.get('address')), 'skip-cert-verify': True, 'tls': True}
                    elif proto in ('hysteria', 'hysteria2'):
                        return {'name': data.get('remarks', 'HYSTERIA_JSON'), 'type': 'hysteria2', 'server': settings.get('address'), 'port': settings.get('port'), 'password': stream.get('hysteriaSettings', {}).get('auth', ''), 'sni': stream.get('tlsSettings', {}).get('serverName', settings.get('address')), 'skip-cert-verify': True}
        except (json.JSONDecodeError, KeyError, IndexError) as e:
            logger.warning(f"[ВНИМАНИЕ] VPN парсер JSON: Ошибка чтения: {e}")
        return None

    def _load_proxies_from_files(self):
        proxies = []
        for filepath in glob.glob(os.path.join(self.vpn_dir, 'link*.txt')):
            file_proxies = self._load_proxies_from_file(filepath)
            proxies.extend(file_proxies)
        return proxies

    def _load_proxies_from_file(self, filepath):
        proxies = []
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read().strip()
                if not content: return proxies
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
        return proxies

    def _save_runtime_config(self, proxies):
        config_data = {
            'mixed-port': 7890, 'mode': 'rule', 'log-level': 'warning', 'allow-lan': True,
            'external-controller': '127.0.0.1:9090',
            'dns': {'enable': True, 'ipv6': False, 'default-nameserver': ['1.1.1.1', '8.8.8.8'], 'nameserver': ['https://1.1.1.1/dns-query', 'https://8.8.8.8/dns-query']},
            'proxies': proxies,
            'proxy-groups': [{'name': '🛡️ Yandex VPN', 'type': 'fallback', 'proxies': [p['name'] for p in proxies], 'url': 'http://www.gstatic.com/generate_204', 'interval': 300}],
            'rules': ['MATCH,🛡️ Yandex VPN']
        }
        try:
            with open(self.runtime_file, 'w', encoding='utf-8') as f:
                yaml.dump(config_data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
            return True
        except yaml.YAMLError as e:
            logger.error(f"[ОШИБКА] Не удалось сгенерировать YAML: {e}")
            return False

    def generate_config(self):
        proxies = self._load_proxies_from_files()
        if not proxies:
            self.available = False
            return False
        if self._save_runtime_config(proxies):
            self.available = True
            logger.info(f"[INFO] VPN конфиг: Сгенерирован. Найдено серверов: {len(proxies)}.")
            return True
        self.available = False
        return False

    def _wait_for_port(self, port=7890, timeout=15):
        start_time = time.time()
        while time.time() - start_time < timeout:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(1)
                if s.connect_ex(('127.0.0.1', port)) == 0: return True
            time.sleep(1)
        return False

    def start(self):
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
        logger.info("[INFO] Запуск процесса mihomo (VPN)...")
        try:
            with open(self.log_file, 'a', encoding='utf-8') as mihomo_log:
                self.process = subprocess.Popen(['mihomo', '-d', '/etc/mihomo', '-f', self.runtime_file], stdout=mihomo_log, stderr=subprocess.STDOUT)
                return self._verify_vpn_started()
        except OSError as e:
            logger.error(f"[ОШИБКА] Не удалось запустить mihomo: {e}")
            return False

    def _verify_vpn_started(self):
        if not self._wait_for_port(7890, 15):
            logger.error("[КРИТИЧНО] Порт VPN (7890) не открылся. Проверьте report/mihomo.log")
            return False
        server_name = "неизвестно"
        try:
            resp = requests.get('http://127.0.0.1:9090/proxies/🛡️ Yandex VPN', timeout=5)
            if resp.status_code == 200:
                server_name = resp.json().get('now', 'неизвестно')
        except requests.exceptions.RequestException: pass
        
        # Даем mihomo 5 секунд на полное поднятие маршрутов и DNS
        time.sleep(5)
        
        # Запрос реального IP-адреса через HTTP-прокси
        ip_address = "Н/Д"
        try:
            ip_resp = requests.get('https://api.ipify.org?format=json', proxies={'http': 'http://127.0.0.1:7890', 'https': 'http://127.0.0.1:7890'}, timeout=15)
            if ip_resp.status_code == 200:
                ip_address = ip_resp.json().get('ip', 'Н/Д')
        except requests.exceptions.RequestException as e:
            logger.warning(f"[ВНИМАНИЕ] Не удалось получить IP-адрес через VPN: {e}")
            
        logger.info(f"[УСПЕХ] VPN-туннель активирован. Сервер: {server_name} | IP: {ip_address}")
        self._log_vpn_event("VPN запущен", f"{server_name} (IP: {ip_address})")
        self.app.telemetry.log_vpn_event(server_name, ip_address, "started", "mihomo")
        return True

    def stop(self):
        if self.process and self.process.poll() is None:
            logger.info("[INFO] Остановка процесса mihomo...")
            self.process.terminate()
            try: self.process.wait(timeout=5)
            except subprocess.TimeoutExpired: self.process.kill()
            self.process = None
            
        try:
            if os.path.exists(self.runtime_file): os.remove(self.runtime_file)
        except OSError: pass

    def test_tunnel(self):
        try:
            resp = requests.get('http://www.gstatic.com/generate_204', proxies={'http': 'http://127.0.0.1:7890', 'https': 'http://127.0.0.1:7890'}, timeout=10)
            return resp.status_code == 204
        except requests.exceptions.RequestException:
            return False

    def _check_mihomo_api(self):
        try:
            resp = requests.get('http://127.0.0.1:9090/version', timeout=5)
            return resp.status_code == 200
        except requests.exceptions.RequestException:
            return False

    def _watchdog_loop(self):
        failed_checks = 0
        restart_attempts = 0
        while not self.stop_event.wait(60):
            if not self.available and not self.proxies: continue
            if self.process is None or self.process.poll() is not None:
                restart_attempts = self._handle_watchdog_crash(restart_attempts)
                continue
            if not self._check_mihomo_api():
                failed_checks += 1
                if failed_checks >= 3:
                    logger.error("[КРИТИЧНО] mihomo завис. Принудительная остановка...")
                    self.process.terminate()
                    try: self.process.wait(timeout=5)
                    except subprocess.TimeoutExpired: self.process.kill()
            else:
                failed_checks = 0

    def _handle_watchdog_crash(self, restart_attempts):
        logger.error("[КРИТИЧНО] Процесс mihomo упал!")
        self._log_vpn_event("Сбой mihomo (Crash)")
        if self.start(): 
            return 0
        restart_attempts += 1
        if restart_attempts >= 3:
            logger.error("[АПОПТОЗ] Не удалось восстановить VPN (3 попытки).")
            raise SystemExit("mihomo process crashed")
        return restart_attempts

    def start_watchdog(self):
        for t in threading.enumerate():
            if t.name == "VpnWatchdog": return
        watchdog_thread = threading.Thread(target=self._watchdog_loop, name="VpnWatchdog", daemon=True)
        watchdog_thread.start()
        logger.info("[INFO] API Watchdog запущен (мониторинг каждые 60 сек).")

    def _log_vpn_event(self, event_type, server_name="Н/Д"):
        try:
            with open(self.audit_file, 'a', encoding='utf-8') as f:
                timestamp = datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S')
                f.write(f"[{timestamp}] Событие: {event_type} | Сервер: {server_name}\n")
        except OSError: pass
