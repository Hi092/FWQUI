#!/usr/bin/env python3
"""OneCloud Server Monitor v4 - Compact Ring Dashboard"""

import http.server
import socketserver
import json
import subprocess
import os
import time
import hashlib
import secrets
import threading
import urllib.request
import re
import shlex
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urlparse, parse_qs
import logging
import logging.handlers

PAUSED_FILE = '/opt/monitor/paused.json'
PUSHPLUS_TOKEN = os.environ.get('PUSHPLUS_TOKEN', '')

# === 网络流量历史记录 ===
_NET_HISTORY_FILE = '/opt/monitor/net_history.json'
_net_history_lock = threading.Lock()
_service_traffic_cache = {}
_service_traffic_lock = threading.Lock()

def _collect_service_traffic():
    """Background collector for service traffic data."""
    global _service_traffic_cache
    result = {}
    # iptables per-port traffic
    try:
        iptables_output = run('iptables -L -v -n -x 2>/dev/null')
        lines = iptables_output.split('\n')
        port_stats = {}
        current_chain = ''
        for line in lines:
            if line.startswith('Chain INPUT'):
                current_chain = 'INPUT'
            elif line.startswith('Chain OUTPUT'):
                current_chain = 'OUTPUT'
            elif not line.startswith('Chain ') and current_chain:
                parts = line.split()
                if len(parts) >= 8 and 'tcp' in line:
                    dport = sport = ''
                    for p in parts:
                        if p.startswith('dpt:'): dport = p[4:]
                        elif p.startswith('spt:'): sport = p[4:]
                    bytes_count = int(parts[1]) if parts[1].isdigit() else 0
                    if current_chain == 'INPUT' and dport in ['8080','8088','445']:
                        port_stats.setdefault(dport,{'rx':0,'tx':0})['rx'] += bytes_count
                    elif current_chain == 'OUTPUT' and sport in ['8080','8088','445']:
                        port_stats.setdefault(sport,{'rx':0,'tx':0})['tx'] += bytes_count
        port_map = {
            '8080': {'key': 'sales', 'name': 'sales'},
            '8088': {'key': 'filebrowser', 'name': 'FileBrowser'},
            '445': {'key': 'smb', 'name': 'SMB'}
        }
        for port, info in port_map.items():
            if port in port_stats:
                result[info['key']] = {
                    'name': info['name'],
                    'rx': port_stats[port]['rx'],
                    'tx': port_stats[port]['tx']
                }
    except Exception:
        pass
    with _service_traffic_lock:
        _service_traffic_cache = result

def _start_service_traffic_collector():
    def _loop():
        while True:
            try:
                _collect_service_traffic()
            except Exception:
                pass
            time.sleep(10)
    t = threading.Thread(target=_loop, daemon=True)
    t.start()


def _load_net_history():
    """加载网络流量历史"""
    try:
        if os.path.exists(_NET_HISTORY_FILE):
            with open(_NET_HISTORY_FILE, 'r') as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def _save_net_history(data):
    """保存网络流量历史"""
    try:
        with open(_NET_HISTORY_FILE, 'w') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def _record_net_traffic():
    """Record current traffic to history, handle reboot counter reset."""
    try:
        net = _get_net_bytes()
        today = datetime.now().strftime('%Y-%m-%d')
        with _net_history_lock:
            history = _load_net_history()
            if today not in history:
                history[today] = {
                    'rx_start': net[0], 'tx_start': net[1],
                    'rx_end': net[0], 'tx_end': net[1],
                    'rx_accumulated': 0, 'tx_accumulated': 0,
                    'recorded_at': datetime.now().isoformat()
                }
            else:
                d = history[today]
                prev_rx = d.get('rx_end', d['rx_start'])
                prev_tx = d.get('tx_end', d['tx_start'])
                # Detect reboot: counter went backwards
                if net[0] < prev_rx or net[1] < prev_tx:
                    d['rx_accumulated'] = d.get('rx_accumulated', 0) + max(0, prev_rx - d['rx_start'])
                    d['tx_accumulated'] = d.get('tx_accumulated', 0) + max(0, prev_tx - d['tx_start'])
                    d['rx_start'] = net[0]
                    d['tx_start'] = net[1]
                d['rx_end'] = net[0]
                d['tx_end'] = net[1]
                d['updated_at'] = datetime.now().isoformat()
            if len(history) > 30:
                for old in sorted(history.keys())[:-30]:
                    del history[old]
            _save_net_history(history)
    except Exception:
        pass

def _start_net_history_recorder():
    """启动流量记录线程"""
    def recorder():
        while True:
            try:
                _record_net_traffic()
            except Exception:
                pass
            time.sleep(3600)  # 每小时记录一次
    
    t = threading.Thread(target=recorder, daemon=True)
    t.start()
ZHIPU_API_KEY = os.environ.get('ZHIPU_API_KEY', '')
ZHIPU_API_URL = 'https://open.bigmodel.cn/api/paas/v4/chat/completions'
DEFAULT_AI_PROVIDERS = [
    {'id': 'zhipu', 'name': '智谱AI', 'url': ZHIPU_API_URL, 'model': 'glm-4-flash', 'api_key': ZHIPU_API_KEY},
    {'id': 'openai', 'name': 'OpenAI', 'url': 'https://api.openai.com/v1/chat/completions', 'model': 'gpt-4o-mini', 'api_key': ''},
    {'id': 'deepseek', 'name': 'DeepSeek', 'url': 'https://api.deepseek.com/v1/chat/completions', 'model': 'deepseek-chat', 'api_key': ''},
    {'id': 'siliconflow', 'name': 'SiliconFlow', 'url': 'https://api.siliconflow.cn/v1/chat/completions', 'model': 'Qwen/Qwen2.5-7B-Instruct', 'api_key': ''},
]
_prev_svc_state = {}

PORT = int(os.environ.get('MONITOR_PORT', 9090))
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')
CONFIG_FILE = '/opt/monitor/config.json'
SERVICES_FILE = '/opt/monitor/services.json'
SESSIONS_FILE = '/opt/monitor/sessions.json'
DEFAULT_PASSWORD = '123000'
_login_attempts = {}  # ip -> (count, first_attempt_time)
_login_lock = threading.Lock()

def _cleanup_login_attempts():
    """每小时清理过期的登录失败记录"""
    while True:
        time.sleep(3600)
        now = time.time()
        with _login_lock:
            expired = [ip for ip, v in _login_attempts.items() if now - v[1] > 3600]
            for ip in expired:
                del _login_attempts[ip]

threading.Thread(target=_cleanup_login_attempts, daemon=True).start()

DEFAULT_FEATURES = {
    'cpu': True, 'memory': True, 'disk': True,
    'services': True, 'temperature': True, 'uptime': True, 'network': True,
}

DEFAULT_SERVICES = [
    {'id': 'filebrowser', 'name': '网盘(FileBrowser)', 'port': 8088,
     'start_cmd': 'systemctl start filebrowser.service',
     'stop_cmd': 'systemctl stop filebrowser.service',
     'check_cmd': 'test "$(systemctl is-active filebrowser.service)" = "active"'},
    {'id': 'smb', 'name': '网盘(SMB共享)', 'port': 445,
     'start_cmd': 'systemctl start smbd.service',
     'stop_cmd': 'systemctl stop smbd.service',
     'check_cmd': 'test "$(systemctl is-active smbd.service)" = "active"'},
    {'id': 'pospal', 'name': '销售日报', 'port': 8080,
     'start_cmd': 'systemctl start pospal-web.service',
     'stop_cmd': 'systemctl stop pospal-web.service',
     'check_cmd': 'test "$(systemctl is-active pospal-web.service)" = "active"'},
    {'id': 'tailscale', 'name': 'Tailscale', 'port': None,
     'start_cmd': 'systemctl start tailscaled.service',
     'stop_cmd': 'systemctl stop tailscaled.service',
     'check_cmd': 'test "$(systemctl is-active tailscaled.service)" = "active"'},
    {'id': 'weather', 'name': '天气监控', 'port': None,
     'start_cmd': 'systemctl start weather-monitor.timer',
     'stop_cmd': 'systemctl stop weather-monitor.timer',
     'check_cmd': 'test "$(systemctl is-active weather-monitor.timer)" = "active"'},
    {'id': 'starlink', 'name': 'Starlink', 'port': None,
     'start_cmd': 'systemctl start starlink-sub.service',
     'stop_cmd': 'systemctl stop starlink-sub.service',
     'check_cmd': 'test "$(systemctl is-active starlink-sub.service)" = "active"'},
    {'id': 'monitor', 'name': '监控面板', 'port': 9090,
     'start_cmd': 'systemctl start monitor.service',
     'stop_cmd': 'systemctl stop monitor.service',
     'check_cmd': 'test "$(systemctl is-active monitor.service)" = "active"'},
]

LOG_FILE = '/opt/monitor/access.log'

SESSION_LOCK = threading.Lock()
sessions = {}

def load_sessions():
    global sessions
    if os.path.exists(SESSIONS_FILE):
        try:
            with open(SESSIONS_FILE, 'r') as f:
                saved = json.load(f)
                now = time.time()
                with SESSION_LOCK:
                    sessions = {k: v for k, v in saved.items() if v.get('expires', 0) > now}
        except Exception: pass

def save_sessions():
    try:
        with open(SESSIONS_FILE, 'w') as f:
            json.dump(sessions, f)
    except Exception: pass

load_sessions()

_config_cache = None
_config_mtime = 0

def load_config():
    global _config_cache, _config_mtime
    try:
        mt = os.path.getmtime(CONFIG_FILE) if os.path.exists(CONFIG_FILE) else 0
        if _config_cache and mt == _config_mtime:
            return _config_cache
        with open(CONFIG_FILE, 'r') as f:
            _config_cache = json.load(f)
            _config_mtime = mt
            return _config_cache
    except Exception: pass
    return {'password_hash': hashlib.sha256(DEFAULT_PASSWORD.encode()).hexdigest(), 'features': DEFAULT_FEATURES.copy(), 'remember_days': 30}

def save_config(config):
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    tmp = CONFIG_FILE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    os.rename(tmp, CONFIG_FILE)

_svc_cache = None
_svc_mtime = 0

def load_services():
    global _svc_cache, _svc_mtime
    try:
        mt = os.path.getmtime(SERVICES_FILE) if os.path.exists(SERVICES_FILE) else 0
        if _svc_cache is not None and mt == _svc_mtime:
            return _svc_cache
        with open(SERVICES_FILE, 'r') as f:
            _svc_cache = json.load(f)
            _svc_mtime = mt
            return _svc_cache
    except Exception: pass
    return DEFAULT_SERVICES

def verify_password(password, config):
    return hashlib.sha256(password.encode()).hexdigest() == config['password_hash']

def create_session(token=None):
    if token is None: token = secrets.token_hex(32)
    with SESSION_LOCK:
        now = time.time()
        sessions[token] = {'created': now, 'expires': now + 7 * 86400}
        expired = [k for k, v in sessions.items() if v.get('expires', 0) < now]
        for k in expired:
            del sessions[k]
    save_sessions()
    return token

def verify_session(token):
    with SESSION_LOCK:
        if token and token in sessions:
            if time.time() < sessions[token]['expires']: return True
            del sessions[token]
            save_sessions()
    return False

def run(cmd, timeout=5):
    try: return subprocess.check_output(cmd, shell=True, stderr=subprocess.DEVNULL, timeout=timeout).decode().strip()
    except Exception: return ""

def run_ok(cmd, timeout=5):
    try: subprocess.run(cmd, shell=True, stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL, timeout=timeout, check=True); return True
    except Exception: return False

# ── 状态缓存 ──
_status_cache = None
_status_cache_ts = 0
_STATUS_CACHE_TTL = 2  # 缓存2秒
_status_lock = threading.Lock()

# Cache for delta calculation
_prev_stat = None
_prev_net = None
_prev_disk_io = None
_prev_time = 0

def _read_file(path):
    try:
        with open(path) as f: return f.read()
    except Exception: return ''

def _get_net_bytes():
    rx = tx = 0
    for line in _read_file('/proc/net/dev').split('\n')[2:]:
        p = line.split()
        if len(p) >= 10 and p[0].rstrip(':') != 'lo':
            rx += int(p[1]); tx += int(p[9])
    return rx, tx

def _parse_net_io(size_str):
    """解析网络IO大小字符串（如 '1.31MB'）为字节数"""
    size_str = size_str.strip().upper()
    multipliers = {'B': 1, 'KB': 1024, 'MB': 1024**2, 'GB': 1024**3, 'TB': 1024**4}
    
    for suffix, mult in sorted(multipliers.items(), key=lambda x: -len(x[0])):
        if size_str.endswith(suffix):
            num_part = size_str[:-len(suffix)].strip()
            try:
                return int(float(num_part) * mult)
            except ValueError:
                return 0
    return 0

def _get_disk_io():
    """Read disk IO bytes from /proc/diskstats (512 bytes per sector)"""
    read_sectors = 0
    write_sectors = 0
    for line in _read_file('/proc/diskstats').split('\n'):
        p = line.split()
        if len(p) >= 14:
            name = p[2]
            # Only count whole disks (mmcblk0, sda, vda), not partitions
            if re.match(r'^(mmcblk\d+|sd[a-z]+|vd[a-z]+|nvme\d+n\d+)$', name):
                read_sectors += int(p[5])
                write_sectors += int(p[9])
    return read_sectors * 512, write_sectors * 512  # Convert to bytes

def _get_port_listening(port):
    if not port: return False
    try:
        for proto in ('/proc/net/tcp', '/proc/net/tcp6'):
            for line in _read_file(proto).split('\n')[1:]:
                p = line.split()
                if len(p) >= 4 and p[3] == '0A':
                    local = p[1].split(':')
                    if int(local[1], 16) == port: return True
    except Exception: pass
    return False

def _get_top_procs():
    """Get top 5 processes by CPU and memory"""
    procs = []
    try:
        out = subprocess.check_output(
            ['ps', 'aux', '--sort=-%cpu'],
            stderr=subprocess.DEVNULL, timeout=5
        ).decode()
        for line in out.strip().split('\n')[1:6]:  # Skip header, take top 5
            p = line.split(None, 10)
            if len(p) >= 11:
                procs.append({
                    'user': p[0],
                    'cpu': float(p[2]),
                    'mem': float(p[3]),
                    'rss': int(p[5]),
                    'cmd': p[10][:60]
                })
    except Exception: pass
    return procs

def get_status(features):
    global _prev_stat, _prev_net, _prev_disk_io, _prev_time
    global _status_cache, _status_cache_ts
    now = time.time()
    with _status_lock:
        if _status_cache and (now - _status_cache_ts) < _STATUS_CACHE_TTL:
            return _status_cache
    status = {}

    if features.get('cpu', True):
        loadavg = _read_file('/proc/loadavg').split()
        status['load'] = {'1m': loadavg[0], '5m': loadavg[1], '15m': loadavg[2]} if len(loadavg) >= 3 else {}
        try:
            fields = [int(x) for x in _read_file('/proc/stat').split('\n')[0].split()[1:]]
            net = _get_net_bytes()
            disk_io = _get_disk_io()
            dt = now - _prev_time if _prev_time else 0
            if _prev_stat and dt > 0:
                idle_d = fields[3] - _prev_stat[3]
                total_d = sum(fields) - sum(_prev_stat)
                cpu_pct = round(100.0 * (1 - idle_d / max(total_d, 1)), 1)
                cpu_pct = max(0, min(100, cpu_pct))
                rx_spd = round((net[0] - _prev_net[0]) / dt)
                tx_spd = round((net[1] - _prev_net[1]) / dt)
                status['net'] = {'rx_speed': max(0, rx_spd), 'tx_speed': max(0, tx_spd), 'rx_total': net[0], 'tx_total': net[1]}
                # Disk IO speed
                if _prev_disk_io:
                    r_spd = round((disk_io[0] - _prev_disk_io[0]) / dt)
                    w_spd = round((disk_io[1] - _prev_disk_io[1]) / dt)
                    status['disk_io'] = {'read_speed': max(0, r_spd), 'write_speed': max(0, w_spd), 'read_total': disk_io[0], 'write_total': disk_io[1]}
                else:
                    status['disk_io'] = {'read_speed': 0, 'write_speed': 0, 'read_total': disk_io[0], 'write_total': disk_io[1]}
                _prev_disk_io = disk_io
            else:
                cpu_pct = 0
                status['net'] = {'rx_speed': 0, 'tx_speed': 0, 'rx_total': net[0], 'tx_total': net[1]}
                status['disk_io'] = {'read_speed': 0, 'write_speed': 0, 'read_total': 0, 'write_total': 0}
            _prev_stat = fields; _prev_net = net; _prev_time = now
        except Exception:
            cpu_pct = -1
            status['net'] = {'rx_speed': 0, 'tx_speed': 0, 'rx_total': 0, 'tx_total': 0}
            status['disk_io'] = {'read_speed': 0, 'write_speed': 0, 'read_total': 0, 'write_total': 0}
        status['cpu_percent'] = cpu_pct

    if features.get('temperature', True):
        temp_val = None
        for tz in sorted(os.listdir('/sys/class/thermal/') if os.path.isdir('/sys/class/thermal/') else []):
            if tz.startswith('thermal_zone'):
                t = _read_file(f'/sys/class/thermal/{tz}/temp').strip()
                if t.isdigit() and int(t) > 0:
                    temp_val = round(int(t) / 1000, 1)
                    break
        status['temp'] = temp_val

    if features.get('memory', True):
        meminfo = {}
        for line in _read_file('/proc/meminfo').split('\n'):
            p = line.split()
            if len(p) >= 2: meminfo[p[0].rstrip(':')] = int(p[1])
        total = meminfo.get('MemTotal', 0)
        avail = meminfo.get('MemAvailable', 0)
        cached = meminfo.get('Cached', 0) + meminfo.get('Buffers', 0)
        actual_used = total - avail - cached
        if actual_used < 0: actual_used = 0
        status['memory'] = {'total_mb': round(total/1024), 'used_mb': round(actual_used/1024), 'avail_mb': round(avail/1024), 'cached_mb': round(cached/1024), 'percent': round(100*(total-avail)/max(total,1),1)}
        st = meminfo.get('SwapTotal', 0)
        st = meminfo.get('SwapTotal', 0)
        sf = meminfo.get('SwapFree', 0)
        # Per-device swap details
        swap_devices = []
        swapon_out = run('swapon --show --bytes --noheadings').strip()
        for line in swapon_out.split('\n'):
            if not line.strip(): continue
            parts = line.split()
            if len(parts) >= 4:
                dev_name = parts[0]
                dev_type = parts[1]
                dev_size = int(parts[2])
                dev_used = int(parts[3])
                dev_prio = parts[4] if len(parts) > 4 else '0'
                is_zram = 'zram' in dev_name
                swap_devices.append({
                    'name': dev_name, 'type': 'ZRAM' if is_zram else 'Disk',
                    'size_mb': round(dev_size/1048576), 'used_mb': round(dev_used/1048576),
                    'percent': round(100*dev_used/max(dev_size,1),1), 'priority': dev_prio
                })
        status['swap'] = {'total_mb': round(st/1024), 'used_mb': round((st-sf)/1024), 'percent': round(100*(st-sf)/max(st,1),1), 'devices': swap_devices}

    if features.get('disk', True):
        disks = []
        for line in run("df -h | grep -E '^/dev'").split('\n'):
            p = line.split()
            if len(p) >= 6: disks.append({'mount': p[5], 'total': p[1], 'used': p[2], 'avail': p[3], 'percent': p[4]})
        status['disks'] = disks

    if features.get('uptime', True):
        secs = float(_read_file('/proc/uptime').split()[0] or 0)
        d, h, m = int(secs//86400), int((secs%86400)//3600), int((secs%3600)//60)
        parts = []
        if d > 0: parts.append(f"{d}天")
        if h > 0: parts.append(f"{h}时")
        parts.append(f"{m}分")
        status['uptime'] = ''.join(parts)

    if features.get('services', True):
        services = []
        svc_list = load_services()
        with ThreadPoolExecutor(max_workers=min(16, len(svc_list) or 1)) as pool:
            futs = {pool.submit(_check_svc_with_timeout, svc): svc for svc in svc_list}
            port_futs = {pool.submit(_get_port_listening, svc.get('port')): svc for svc in svc_list}
            results = {}
            for fut in as_completed(futs, timeout=8):
                svc = futs[fut]
                try: results[svc['id']] = fut.result()
                except Exception: results[svc['id']] = False
            port_results = {}
            for fut in as_completed(port_futs, timeout=5):
                svc = port_futs[fut]
                try: port_results[svc['id']] = fut.result()
                except Exception: port_results[svc['id']] = False
        paused_set = _load_paused()
        for svc in svc_list:
            is_running = results.get(svc['id'], False)
            entry = {'id': svc['id'], 'name': svc['name'], 'port': svc.get('port'),
                     'running': is_running,
                     'listening': port_results.get(svc['id'], False)}
            if svc.get('type'): entry['type'] = svc['type']
            if svc.get('host'): entry['host'] = svc['host']
            if svc.get('link'): entry['link'] = svc['link']
            # 手动暂停：不在运行 + 在暂停列表中
            if not is_running and svc['id'] in paused_set:
                entry['paused'] = True
            services.append(entry)
        status['services'] = services

    # Top processes (always collect, lightweight)
    status['top_procs'] = _get_top_procs()

    # Network IP info
    try:
        ips = []
        for line in run("ip -4 addr show").split('\n'):
            m = re.search(r'inet (\d+\.\d+\.\d+\.\d+)/(\d+)\s+.*\s+(\S+)$', line.strip())
            if m and m.group(3) != 'lo':
                ips.append({'iface': m.group(3), 'ip': m.group(1), 'mask': m.group(2)})
        status['ip_info'] = ips
    except Exception:
        status['ip_info'] = []

    # Weather data (cached 60s)
    try:
        weather_file = '/opt/weather_monitor/weather_history.json'
        _now_ts = time.time()
        if not hasattr(get_status, '_wcache') or _now_ts - get_status._wts > 60:
            if os.path.exists(weather_file):
                with open(weather_file) as f:
                    get_status._wcache = json.load(f)
                get_status._wts = _now_ts
        wh = get_status._wcache if hasattr(get_status, '_wcache') else None
        if wh:
            # 取第一个非_prev的地点
            loc = next((k for k in wh if k != '_prev'), None)
            if loc and isinstance(wh[loc], dict):
                w = wh[loc]
                status['weather'] = {
                    'temp': w.get('temp'),
                    'humidity': w.get('humidity'),
                    'wind': w.get('wind_kmh'),
                    'pressure': w.get('pressure'),
                    'city': loc
                }
    except Exception:
        pass

    status['timestamp'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    status['features'] = features
    status['hostname'] = run('hostname').strip()
    # 写缓存
    with _status_lock:
        _status_cache = status
        _status_cache_ts = time.time()
    return status

def _pushplus(title, content):
    try:
        body = json.dumps({'token': PUSHPLUS_TOKEN, 'title': title, 'content': content, 'template': 'html'}).encode()
        req = urllib.request.Request('http://www.pushplus.plus/send', data=body, headers={'Content-Type':'application/json'})
        urllib.request.urlopen(req, timeout=10)
    except Exception: pass

def _check_svc_with_timeout(svc, timeout=3):
    """检查单个服务，超时返回False。支持普通服务和远程主机(ping)"""
    if svc.get('type') == 'remote':
        host = svc.get('host', '')
        if not host: return False
        try:
            subprocess.run(['ping', '-c1', '-W2', host], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=3, check=True)
            return True
        except Exception:
            return False
    try:
        subprocess.run(svc['check_cmd'], shell=True, stderr=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, timeout=timeout, check=True)
        return True
    except Exception:
        return False

def _svc_monitor():
    global _prev_svc_state
    time.sleep(10)
    while True:
        try:
            svcs = load_services()
            paused = _load_paused()
            new_state = {}
            with ThreadPoolExecutor(max_workers=min(16, len(svcs) or 1)) as pool:
                futs = {pool.submit(_check_svc_with_timeout, svc): svc for svc in svcs}
                for fut in as_completed(futs, timeout=8):
                    svc = futs[fut]
                    try:
                        running = fut.result()
                    except Exception:
                        running = False
                    sid = svc['id']
                    new_state[sid] = running
                    prev = _prev_svc_state.get(sid)
                    # 自动从暂停列表移除已恢复的服务
                    if running and sid in paused:
                        paused.discard(sid)
                        _save_paused(paused)
                    # 只对非手动暂停的服务推送通知
                    if prev is not None and prev != running and sid not in paused:
                        now = datetime.now().strftime('%H:%M:%S')
                        if running:
                            _pushplus('服务恢复', f"<b>{svc['name']}</b> 已恢复运行 ({now})")
                        else:
                            _pushplus('服务宕机', f"<b>{svc['name']}</b> 已停止运行 ({now})")
            # 只更新成功获取的状态，超时的保留上一轮
            for sid, running in new_state.items():
                _prev_svc_state[sid] = running
        except Exception: pass
        time.sleep(30)

def _cleanup_job_logs():
    """每小时清理超过24小时的后台任务日志"""
    while True:
        time.sleep(3600)
        try:
            for f in os.listdir('/tmp'):
                if f.startswith('job_') and f.endswith('.log'):
                    fp = os.path.join('/tmp', f)
                    if time.time() - os.path.getmtime(fp) > 86400:
                        os.remove(fp)
        except Exception: pass

threading.Thread(target=_cleanup_job_logs, daemon=True).start()
threading.Thread(target=_svc_monitor, daemon=True).start()


def _load_paused():
    try:
        with open(PAUSED_FILE) as f:
            d = json.load(f)
            return set(d) if isinstance(d, list) else set()
    except Exception:
        return set()

def _save_paused(paused):
    try:
        with open(PAUSED_FILE, 'w') as f:
            json.dump(list(paused), f)
    except Exception: pass

def control_service(service_id, action):
    services = load_services()
    svc = next((s for s in services if s['id'] == service_id), None)
    if not svc: return {'success': False, 'message': '服务不存在'}
    cmd = svc['start_cmd'] if action == 'start' else svc['stop_cmd'] if action == 'stop' else None
    if not cmd: return {'success': False, 'message': '无效操作'}
    t0 = time.time()
    try:
        run(cmd)
        time.sleep(1.5)
        running = run_ok(svc['check_cmd'])
        elapsed = round(time.time() - t0, 2)
        paused = _load_paused()
        if action == 'stop' and not running:
            paused.add(service_id)
            _save_paused(paused)
        elif action == 'start':
            paused.discard(service_id)
            _save_paused(paused)
        msg = f"{svc['name']} {'已启动' if action == 'start' else '已停止'}"
        return {'success': True, 'running': running, 'message': msg}
    except Exception as e:
        return {'success': False, 'message': str(e)}



MAX_LOG_BYTES = 100 * 1024  # 100KB per file
LOG_BACKUP_COUNT = 1         # 保留1个备份

_logger = logging.getLogger('monitor_access')
_logger.setLevel(logging.INFO)
_logger.propagate = False
if not _logger.handlers:
    _handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=MAX_LOG_BYTES, backupCount=LOG_BACKUP_COUNT, encoding='utf-8'
    )
    _handler.setFormatter(logging.Formatter('%(message)s'))
    _logger.addHandler(_handler)

def _log_access(ip, method, path, detail=''):
    """Write access log entry via RotatingFileHandler"""
    try:
        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        msg = f"[{ts}] {ip} {method} {path}"
        if detail:
            msg += f" ({detail})"
        _logger.info(msg)
    except Exception: pass

class Handler(http.server.BaseHTTPRequestHandler):
    _speed_lock = threading.Lock()
    def _get_token(self, params=None, data=None):
        """从Cookie获取token，fallback到query/body"""
        cookie = self.headers.get('Cookie','')
        for part in cookie.split(';'):
            kv = part.strip().split('=',1)
            if len(kv)==2 and kv[0].strip()=='m_t':
                t = kv[1].strip()
                if t and verify_session(t): return t
        for src in (params, data):
            if src:
                t = src.get('token',[''])[0] if isinstance(src.get('token'), list) else src.get('token','')
                if t and verify_session(t): return t
        return ''

    def _set_cookie(self, token):
        """设置HttpOnly Cookie，30天过期"""
        self.send_header('Set-Cookie', f'm_t={token}; Path=/; Max-Age=2592000; HttpOnly; SameSite=Lax')

    def send_json(self, data, code=200):
        """统一JSON响应"""
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        path = parsed.path
        if path in ('/login','/login.html'):
            self.send_response(200); self.send_header('Content-Type','text/html; charset=utf-8'); self.send_header('Cache-Control','no-cache, no-store, must-revalidate'); self.send_header('Pragma','no-cache'); self.send_header('Expires','0'); self.end_headers()
            try:
                with open(os.path.join(STATIC_DIR, 'login.html'), 'rb') as f:
                    self.wfile.write(f.read())
            except FileNotFoundError:
                self.wfile.write(b'<h1>Login page not found</h1>')
            return
        if path == '/api/verify':
            token = self._get_token(params=params)
            self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers()
            self.wfile.write(json.dumps({'valid':bool(token)}).encode()); return
        if path == '/api/logout':
            self.send_response(200); self.send_header('Content-Type','application/json')
            self.send_header('Set-Cookie','m_t=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax'); self.end_headers()
            self.wfile.write(json.dumps({'ok':True}).encode()); return
        if path == '/api/status':
            # 不记录status日志，太频繁
            token = self._get_token(params=params)
            if not verify_session(token):
                self.send_json({'error':'unauthorized'}, 401); return
            config = load_config()
            data = get_status(config.get('features',DEFAULT_FEATURES))
            data['svc_hide'] = config.get('svc_hide',[])
            data['svc_order'] = config.get('svc_order',[])
            self.send_response(200); self.send_header('Content-Type','application/json; charset=utf-8'); self.end_headers()
            self.wfile.write(json.dumps(data,ensure_ascii=False).encode()); return
        if path == '/api/cron':
            token = self._get_token(params=params)
            if not verify_session(token):
                self.send_json({'error':'unauthorized'}, 401); return
            jobs = []
            for f in os.listdir('/etc/cron.d/'):
                if f.startswith('.') or f == 'placeholder': continue
                try:
                    with open(f'/etc/cron.d/{f}') as fh:
                        for line in fh:
                            line = line.strip()
                            if line and not line.startswith('#'):
                                jobs.append({'source': f, 'line': line})
                except Exception: pass
            try:
                out = subprocess.check_output(['crontab', '-l'], stderr=subprocess.DEVNULL, timeout=3).decode()
                for line in out.strip().split('\n'):
                    line = line.strip()
                    if line and not line.startswith('#'):
                        jobs.append({'source': 'crontab', 'line': line})
            except Exception: pass
            timers = []
            try:
                out = subprocess.check_output(['systemctl','list-timers','--no-pager','all'], stderr=subprocess.DEVNULL, timeout=5).decode()
                for line in out.split('\n')[1:]:
                    line = line.strip()
                    if not line: continue
                    if '.timer' in line:
                        parts = line.split()
                        timer_name = [p for p in parts if p.endswith('.timer')]
                        m = re.search(r'(\S+\s+left)', line)
                        left = m.group(1) if m else ''
                        name = timer_name[0] if timer_name else parts[-1]
                        timers.append({'left': left, 'name': name})
            except Exception: pass
            self.send_response(200); self.send_header('Content-Type','application/json; charset=utf-8'); self.end_headers()
            self.wfile.write(json.dumps({'jobs': jobs, 'timers': timers}, ensure_ascii=False).encode()); return
        if path == '/api/sysinfo':
            token = self._get_token(params=params)
            if not verify_session(token):
                self.send_json({'error':'unauthorized'}, 401); return
            hostname = run('hostname').strip()
            os_info = run('cat /etc/os-release | grep PRETTY_NAME | cut -d= -f2 | tr -d \'"\'').strip() or run('uname -o').strip()
            os_info = os_info.split('\n')[0].strip() if os_info else '未知'
            kernel = run('uname -r').strip()
            arch = run('uname -m').strip()
            ip = run("ip -4 addr show eth0 2>/dev/null | grep inet | awk '{print $2}' | cut -d/ -f1").strip()
            if not ip:
                hip = run("hostname -I").strip()
                ip = hip.split()[0] if hip else ''
            tailscale = run('tailscale ip -4 2>/dev/null').strip()
            loadavg = run('cat /proc/loadavg').strip().split()[:3]
            load_str = ' '.join(loadavg) if loadavg else '--'
            users = run('who | wc -l').strip()
            # CPU型号
            cpu_model = ''
            for line in _read_file('/proc/cpuinfo').split('\n'):
                if 'model name' in line or 'Hardware' in line:
                    cpu_model = line.split(':',1)[-1].strip()
                    break
            # 主板
            board = run("cat /sys/firmware/devicetree/base/model 2>/dev/null | tr -d '\\0'").strip()
            if not board:
                board = run("cat /sys/devices/virtual/dmi/id/board_name 2>/dev/null").strip()
            # 内存总量
            mem_total = ''
            for line in _read_file('/proc/meminfo').split('\n'):
                if line.startswith('MemTotal:'):
                    mem_total = str(round(int(line.split()[1]) / 1024)) + 'MB'
                    break
            # 磁盘总量（一次df完成）
            df_line = run("df -h / | tail -1").strip().split()
            disk_total = df_line[1] if len(df_line) >= 2 else ''
            disk_used = df_line[2] if len(df_line) >= 3 else ''
            disk_avail = df_line[3] if len(df_line) >= 4 else ''
            self.send_response(200); self.send_header('Content-Type','application/json; charset=utf-8'); self.end_headers()
            self.wfile.write(json.dumps({'hostname':hostname,'os':os_info,'kernel':kernel,'arch':arch,'ip':ip,'tailscale':tailscale,'load':load_str,'users':users,'cpu_model':cpu_model,'board':board,'mem_total':mem_total,'disk_total':disk_total,'disk_used':disk_used,'disk_avail':disk_avail}, ensure_ascii=False).encode()); return
        if path == '/api/network':
            token = self._get_token(params=params)
            if not verify_session(token):
                self.send_json({'error':'unauthorized'}, 401); return
            interfaces = []
            # Get all network interfaces
            ip_out = run("ip -j addr show 2>/dev/null")
            try:
                ip_data = json.loads(ip_out)
                for iface in ip_data:
                    if iface.get('ifname') == 'lo':
                        continue
                    info = {
                        'name': iface.get('ifname', ''),
                        'mac': iface.get('address', ''),
                        'state': iface.get('operstate', 'UNKNOWN'),
                        'mtu': iface.get('mtu', 1500),
                        'ips': [],
                        'type': 'ethernet'
                    }
                    # Determine type
                    if iface.get('ifname', '').startswith('wl'):
                        info['type'] = 'wifi'
                    elif iface.get('ifname', '').startswith('tailscale'):
                        info['type'] = 'vpn'
                    # Get IPs
                    for addr_info in iface.get('addr_info', []):
                        if addr_info.get('family') == 'inet':
                            info['ips'].append(addr_info.get('local', ''))
                    interfaces.append(info)
            except Exception:
                pass
            # Get WiFi info if available
            wifi_iface = next((i for i in interfaces if i['type'] == 'wifi'), None)
            if wifi_iface:
                try:
                    iw_out = run(f"iw dev {wifi_iface['name']} link 2>/dev/null")
                    if 'Connected to' in iw_out:
                        wifi_iface['connected'] = True
                        for line in iw_out.split('\n'):
                            line = line.strip()
                            if line.startswith('SSID:'):
                                wifi_iface['ssid'] = line[5:].strip()
                            elif line.startswith('freq:'):
                                wifi_iface['freq'] = line[5:].strip()
                            elif line.startswith('signal:'):
                                wifi_iface['signal'] = line[7:].strip()
                    else:
                        wifi_iface['connected'] = False
                except Exception:
                    wifi_iface['connected'] = False
            self.send_response(200); self.send_header('Content-Type','application/json; charset=utf-8'); self.end_headers()
            self.wfile.write(json.dumps({'interfaces': interfaces}, ensure_ascii=False).encode()); return
        if path == '/api/network/scan':
            token = self._get_token(params=params)
            if not verify_session(token):
                self.send_json({'error':'unauthorized'}, 401); return
            iface = params.get('iface', ['wlx0087361f7b1a'])[0]
            # Bring interface up
            run(f'ip link set {iface} up 2>/dev/null')
            time.sleep(1)
            # Scan networks (需要更长超时)
            scan_out = run(f'iw dev {iface} scan 2>/dev/null', timeout=15)
            networks = []
            current = {}
            for line in scan_out.split('\n'):
                line = line.strip()
                if line.startswith('BSS '):
                    if current:
                        networks.append(current)
                    current = {}
                elif line.startswith('SSID:'):
                    current['ssid'] = line[5:].strip()
                elif line.startswith('signal:'):
                    current['signal'] = line[7:].strip()
                elif line.startswith('freq:'):
                    current['freq'] = line[5:].strip()
            if current:
                networks.append(current)
            # Filter out empty SSIDs
            networks = [n for n in networks if n.get('ssid')]
            self.send_response(200); self.send_header('Content-Type','application/json; charset=utf-8'); self.end_headers()
            self.wfile.write(json.dumps({'networks': networks}, ensure_ascii=False).encode()); return
        if path == '/api/network/connect':
            token = self._get_token(data=data)
            if not verify_session(token):
                self.send_json({'error':'unauthorized'}, 401); return
            ssid = data.get('ssid', '')
            password = data.get('password', '')
            iface = data.get('iface', 'wlx0087361f7b1a')
            if not ssid:
                self.send_json({'error': 'SSID required'}, 400); return
            # Generate wpa_supplicant config
            if password:
                wpa_conf = run(f'wpa_passphrase "{ssid}" "{password}" 2>/dev/null')
            else:
                wpa_conf = f'network={{\n\tssid="{ssid}"\n\tkey_mgmt=NONE\n}}'
            # Write config
            try:
                with open('/etc/wpa_supplicant/wifi.conf', 'w') as f:
                    f.write(wpa_conf)
            except Exception as e:
                self.send_json({'error': str(e)}, 500); return
            # Kill existing wpa_supplicant
            run('killall wpa_supplicant 2>/dev/null')
            time.sleep(1)
            # Start wpa_supplicant
            run(f'wpa_supplicant -B -i {iface} -c /etc/wpa_supplicant/wifi.conf 2>/dev/null')
            time.sleep(5)
            # Get IP via DHCP (if available) or static
            dhcp_ok = False
            if os.path.exists('/usr/bin/dhclient'):
                run(f'dhclient {iface} 2>/dev/null')
                dhcp_ok = True
            elif os.path.exists('/sbin/udhcpc'):
                run(f'udhcpc -i {iface} -n 2>/dev/null')
                dhcp_ok = True
            # Check connection
            iw_out = run(f'iw dev {iface} link 2>/dev/null')
            connected = 'Connected to' in iw_out
            _log_access(self.client_address[0], 'POST', '/api/network/connect', f'{ssid} {"connected" if connected else "failed"}')
            self.send_response(200); self.send_header('Content-Type','application/json; charset=utf-8'); self.end_headers()
            self.wfile.write(json.dumps({'success': connected, 'dhcp': dhcp_ok, 'message': f'{"已连接" if connected else "连接失败"}到 {ssid}'}, ensure_ascii=False).encode()); return
        if path == '/api/network/disconnect':
            token = self._get_token(data=data)
            if not verify_session(token):
                self.send_json({'error':'unauthorized'}, 401); return
            iface = data.get('iface', 'wlx0087361f7b1a')
            run('killall wpa_supplicant 2>/dev/null')
            run(f'ip addr flush dev {iface} 2>/dev/null')
            run(f'ip link set {iface} down 2>/dev/null')
            _log_access(self.client_address[0], 'POST', '/api/network/disconnect', iface)
            self.send_response(200); self.send_header('Content-Type','application/json; charset=utf-8'); self.end_headers()
            self.wfile.write(json.dumps({'success': True, 'message': 'WiFi已断开'}, ensure_ascii=False).encode()); return
        if path == '/api/net-history':
            # 网络流量历史API
            token = self._get_token(params=params)
            if not verify_session(token):
                self.send_json({'error':'unauthorized'}, 401); return
            
            date = params.get('date', [''])[0] if params else ''
            update = params.get('update', [''])[0] if params else ''
            
            # 如果请求更新，先记录一次当前流量
            if update == 'true':
                _record_net_traffic()
            
            history = _load_net_history()
            
            if date:
                # 返回指定日期的数据
                if date in history:
                    data = history[date]
                    # 计算当日流量差值
                    rx_total = data["rx_end"] - data["rx_start"] + data.get("rx_accumulated", 0)
                    tx_total = data["tx_end"] - data["tx_start"] + data.get("tx_accumulated", 0)
                    result = {
                        'date': date,
                        'rx_total': max(0, rx_total),
                        'tx_total': max(0, tx_total),
                        'rx_start': data['rx_start'],
                        'tx_start': data['tx_start'],
                        'rx_end': data['rx_end'],
                        'tx_end': data['tx_end'],
                        'recorded_at': data.get('recorded_at', ''),
                        'updated_at': data.get('updated_at', '')
                    }
                else:
                    result = {'error': 'No data for this date'}
            else:
                # 返回所有历史数据摘要
                summary = []
                for d, data in sorted(history.items(), reverse=True):
                    rx_total = data["rx_end"] - data["rx_start"] + data.get("rx_accumulated", 0)
                    tx_total = data["tx_end"] - data["tx_start"] + data.get("tx_accumulated", 0)
                    summary.append({
                        'date': d,
                        'rx_total': max(0, rx_total),
                        'tx_total': max(0, tx_total)
                    })
                result = {'history': summary[:30]}  # 最多30天
            
            self.send_json(result)
            return
        if path == '/api/service-traffic':
            # 服务流量统计API - 使用后台缓存，响应 <1ms
            token = self._get_token(params=params)
            if not verify_session(token):
                self.send_json({'error':'unauthorized'}, 401); return
            
            with _service_traffic_lock:
                result = dict(_service_traffic_cache)
            
            self.send_json(result)
            return
        if path == '/api/logs':
            token = self._get_token(params=params)
            if not verify_session(token):
                self.send_json({'error':'unauthorized'}, 401); return
            log_cfg = {}
            try:
                cfg_raw = params.get('cfg',[''])[0]
                if cfg_raw: log_cfg = json.loads(cfg_raw)
            except Exception: pass
            show_ts = log_cfg.get('timestamp', True)
            show_ip = log_cfg.get('ip', True)
            lines = []
            try:
                # Panel access log
                if os.path.exists(LOG_FILE):
                    with open(LOG_FILE) as f:
                        all_lines = f.readlines()
                    for l in all_lines[-200:]:
                        l=l.strip()
                        if not l: continue
                        # 分类并过滤
                        if '/login' in l:
                            if not log_cfg.get('login', True): continue
                        elif '/api/service' in l:
                            if not log_cfg.get('service', True): continue
                        elif '/api/cron' in l or '定时任务' in l:
                            if not log_cfg.get('cron', True): continue
                        elif '/api/features' in l or '/api/svc-settings' in l or '/api/change-password' in l or 'hostname' in l.lower() or '修改' in l:
                            if not log_cfg.get('settings', True): continue
                        elif '/api/chat' in l or 'AI对话' in l:
                            if not log_cfg.get('ai', False): continue
                        elif '/api/exec' in l:
                            if not log_cfg.get('terminal', False): continue
                        elif '/api/speedtest' in l:
                            if not log_cfg.get('speedtest', True): continue
                        elif '/api/sys-action' in l:
                            if not log_cfg.get('sysaction', True): continue
                        else:
                            # 未分类（verify/status等旧日志）默认跳过
                            continue
                        display_l = l
                        if not show_ts and l.startswith('['):
                            display_l = l.split('] ', 1)[-1] if '] ' in l else l
                        if not show_ip:
                            parts = display_l.split(' ')
                            if len(parts) > 2 and (parts[1].count('.')==3 or parts[1].count(':')>0):
                                parts.pop(1)
                            display_l = ' '.join(parts)
                        lines.append(display_l)
                # Pospal access log
                if log_cfg.get('pospal', True):
                    try:
                        out = subprocess.check_output(['journalctl','-u','pospal-web','--no-pager','-n','30'], stderr=subprocess.DEVNULL, timeout=5).decode()
                        for l in out.strip().split('\n'):
                            if 'INFO' in l and '"' in l:
                                try:
                                    ts=l[:19]
                                    info_part=l.split('[INFO]')[1].strip()
                                    ip=info_part.split('-')[0].strip()
                                    req=info_part.split('"')[1] if '"' in info_part else ''
                                    path=req.split(' ')[1] if ' ' in req else req
                                    desc='查看销售日报' if 'report' in path else '访问首页' if path=='/' else path
                                    entry = ''
                                    if show_ts: entry += f'[{ts}] '
                                    if show_ip: entry += f'{ip} '
                                    entry += f'销售日报: {desc}'
                                    lines.append(entry)
                                except Exception: pass
                    except Exception: pass
                # FileBrowser log
                if log_cfg.get('filebrowser', True):
                    try:
                        out = subprocess.check_output(['journalctl','-u','filebrowser','--no-pager','-n','50'], stderr=subprocess.DEVNULL, timeout=5).decode()
                        for l in out.strip().split('\n'):
                            if 'login' in l.lower() or 'upload' in l.lower() or 'download' in l.lower() or 'create' in l.lower() or 'delete' in l.lower():
                                lines.append(l.strip())
                    except Exception: pass
                # SMB log
                if log_cfg.get('smb', False):
                    try:
                        out = subprocess.check_output(['journalctl','-u','smbd','--no-pager','-n','30'], stderr=subprocess.DEVNULL, timeout=5).decode()
                        for l in out.strip().split('\n'):
                            if 'connect' in l.lower() or 'disconnect' in l.lower() or 'open' in l.lower():
                                lines.append(l.strip())
                    except Exception: pass
                lines.sort(reverse=True)
                lines = lines[:50]
            except Exception: pass
            if not lines:
                lines=['暂无访问日志']
            self.send_response(200); self.send_header('Content-Type','application/json; charset=utf-8'); self.end_headers()
            self.wfile.write(json.dumps({'lines': lines}, ensure_ascii=False).encode()); return
        if path in ('/','/index.html'):
            self.send_response(200); self.send_header('Content-Type','text/html; charset=utf-8'); self.send_header('Cache-Control','no-cache, no-store, must-revalidate'); self.send_header('Pragma','no-cache'); self.send_header('Expires','0'); self.end_headers()
            try:
                with open(os.path.join(STATIC_DIR, 'index.html'), 'rb') as f:
                    self.wfile.write(f.read())
            except FileNotFoundError:
                self.wfile.write(b'<h1>Dashboard not found</h1>')
            return
        if path == '/api/op-logs':
            token = self._get_token(params=params)
            if not verify_session(token):
                self.send_json({'error':'unauthorized'}, 401); return
            limit = 50
            try: limit = int(params.get('limit',['50'])[0])
            except Exception: pass
            limit = min(limit, 500)
            logs = []
            self.send_response(200); self.send_header('Content-Type','application/json; charset=utf-8'); self.end_headers()
            self.wfile.write(json.dumps({'logs': logs}, ensure_ascii=False).encode()); return
        if path == '/api/op-logs/export':
            token = self._get_token(params=params)
            if not verify_session(token):
                self.send_json({'error':'unauthorized'}, 401); return
            fmt = params.get('format',['csv'])[0]
            logs = []
            if fmt == 'csv':
                lines = ['时间,IP,操作,结果,耗时']
                for l in logs:
                    lines.append(f"{l.get('time','')},{l.get('ip','')},{l.get('action','')},{l.get('result','')},{l.get('elapsed','')}")
                content = '\n'.join(lines)
                self.send_response(200); self.send_header('Content-Type','text/csv; charset=utf-8')
                self.send_header('Content-Disposition','attachment; filename=op_logs.csv'); self.end_headers()
                self.wfile.write(content.encode('utf-8-sig'))
            else:
                self.send_response(200); self.send_header('Content-Type','application/json; charset=utf-8')
                self.send_header('Content-Disposition','attachment; filename=op_logs.json'); self.end_headers()
                self.wfile.write(json.dumps(logs, ensure_ascii=False, indent=2).encode())
            return
        self.send_response(404); self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        # CSRF防护：验证Referer
        referer = self.headers.get('Referer', '')
        if referer and not any(referer.startswith(h) for h in ['http://localhost', 'http://127.0', 'http://192.168', 'http://100.']):
            host = self.headers.get('Host', '')
            if host and host not in referer:
                self.send_response(403); self.send_header('Content-Type','application/json'); self.end_headers()
                self.wfile.write(json.dumps({'error':'CSRF rejected'}).encode()); return
        cl = int(self.headers.get('Content-Length',0))
        body = self.rfile.read(cl).decode() if cl>0 else '{}'
        try: data = json.loads(body)
        except Exception: data = {}
        if path == '/api/logout':
            self.send_response(200); self.send_header('Content-Type','application/json')
            self.send_header('Set-Cookie','m_t=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax'); self.end_headers()
            self.wfile.write(json.dumps({'ok':True}).encode()); return
        if path == '/api/login':
            # 暴力破解防护：5分钟内同一IP失败5次锁定
            client_ip = self.client_address[0]
            now = time.time()
            with _login_lock:
                entry = _login_attempts.get(client_ip)
            if entry and entry[0] >= 5 and now - entry[1] < 300:
                self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers()
                self.wfile.write(json.dumps({'success':False,'message':'登录过于频繁，请5分钟后再试'}).encode()); return
            config = load_config()
            if verify_password(data.get('password',''),config):
                with _login_lock:
                    _login_attempts.pop(client_ip, None)
                token = create_session()
                _log_access(client_ip, 'POST', '/api/login', '登录成功')
                self.send_response(200); self.send_header('Content-Type','application/json'); self._set_cookie(token); self.end_headers()
                self.wfile.write(json.dumps({'success':True}).encode())
            else:
                with _login_lock:
                    ent = _login_attempts.get(client_ip)
                    if ent and now - ent[1] < 300:
                        _login_attempts[client_ip] = (ent[0] + 1, ent[1])
                    else:
                        _login_attempts[client_ip] = (1, now)
                _log_access(client_ip, 'POST', '/api/login', '密码错误')
                self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers()
                self.wfile.write(json.dumps({'success':False,'message':'密码错误'}).encode())
        elif path == '/api/service':
            if not verify_session(self._get_token(data=data)):
                self.send_json({'error':'unauthorized'}, 401); return
            result = control_service(data.get('id',''), data.get('action',''))
            _log_access(self.client_address[0], 'POST', '/api/service', f"{data.get('id','')} {data.get('action','')}")
            self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers()
            self.wfile.write(json.dumps(result,ensure_ascii=False).encode())
        elif path == '/api/features':
            if not verify_session(self._get_token(data=data)):
                self.send_json({'error':'unauthorized'}, 401); return
            config = load_config()
            feat = data.get('feature','')
            enabled = data.get('enabled',True)
            if feat:
                if 'features' not in config:
                    config['features'] = DEFAULT_FEATURES.copy()
                config['features'][feat] = enabled
                save_config(config)
            self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers()
            self.wfile.write(json.dumps({'success':True}).encode())
        elif path == '/api/change-password':
            config = load_config()
            if not verify_session(self._get_token(data=data)):
                self.send_json({'error':'unauthorized'}, 401); return
            if not verify_password(data.get('old_password',''),config):
                self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers()
                self.wfile.write(json.dumps({'success':False,'message':'当前密码错误'}).encode()); return
            new_pw = data.get('new_password','')
            if len(new_pw) < 4:
                self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers()
                self.wfile.write(json.dumps({'success':False,'message':'密码至少4位'}).encode()); return
            config['password_hash'] = hashlib.sha256(new_pw.encode()).hexdigest()
            save_config(config)
            self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers()
            self.wfile.write(json.dumps({'success':True}).encode())
        elif path == '/api/svc-settings':
            if not verify_session(self._get_token(data=data)):
                self.send_json({'error':'unauthorized'}, 401); return
            # 改名
            rename_id = data.get('rename_id','')
            rename_name = data.get('rename_name','')
            if rename_id and rename_name:
                svcs = load_services()
                found = False
                for s in svcs:
                    if s.get('id') == rename_id:
                        s['custom_name'] = rename_name
                        found = True
                        break
                if found:
                    with open(SERVICES_FILE, 'w') as f:
                        json.dump(svcs, f, ensure_ascii=False, indent=2)
                    _log_access(self.client_address[0], 'POST', '/api/svc-settings', f'改名: {rename_id} -> {rename_name}')
                    self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers()
                    self.wfile.write(json.dumps({'success':True,'message':'已改名'}).encode())
                else:
                    self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers()
                    self.wfile.write(json.dumps({'success':False,'message':'服务不存在'}).encode())
                return
            # 重置改名
            reset_rename = data.get('reset_rename','')
            if reset_rename:
                svcs = load_services()
                for s in svcs:
                    if s.get('id') == reset_rename and 'custom_name' in s:
                        del s['custom_name']
                with open(SERVICES_FILE, 'w') as f:
                    json.dump(svcs, f, ensure_ascii=False, indent=2)
                self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers()
                self.wfile.write(json.dumps({'success':True,'message':'已恢复默认名称'}).encode())
                return
            config = load_config()
            sh = data.get('svc_hide',config.get('svc_hide',[]))
            so = data.get('svc_order',config.get('svc_order',[]))
            config['svc_hide'] = sh if isinstance(sh, list) else []
            config['svc_order'] = so if isinstance(so, list) else []
            save_config(config)
            self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers()
            self.wfile.write(json.dumps({'success':True}).encode())
        elif path == '/api/sys-action':
            if not verify_session(self._get_token(data=data)):
                self.send_json({'error':'unauthorized'}, 401); return
            action = data.get('action','')
            if action == 'reboot':
                _log_access(self.client_address[0], 'POST', '/api/sys-action', '重启服务器')
                self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers()
                self.wfile.write(json.dumps({'success':True,'message':'服务器正在重启...'}).encode())
                threading.Thread(target=lambda: (time.sleep(1), os.system('reboot')), daemon=True).start()
            elif action == 'cleanup':
                before = run("df / | tail -1 | awk '{print $4}'").strip()
                try: before_kb = int(before)
                except Exception: before_kb = 0
                # 清理临时文件
                run('rm -rf /tmp/* /var/tmp/* 2>/dev/null')
                # 清理pip缓存
                run('rm -rf /root/.cache/pip 2>/dev/null')
                # 清理apt缓存
                run('apt-get clean 2>/dev/null')
                run('rm -rf /var/cache/apt/archives/*.deb 2>/dev/null')
                # 清理系统缓存
                run('sync && echo 3 > /proc/sys/vm/drop_caches 2>/dev/null')
                after = run("df / | tail -1 | awk '{print $4}'").strip()
                try: after_kb = int(after)
                except Exception: after_kb = 0
                freed_kb = after_kb - before_kb
                avail_after = run("df -h / | tail -1 | awk '{print $4}'").strip()
                if freed_kb > 1024:
                    msg = f'清理完成，释放了 {freed_kb//1024}MB，可用空间 {avail_after}'
                elif freed_kb > 0:
                    msg = f'清理完成，释放了 {freed_kb}KB，可用空间 {avail_after}'
                else:
                    msg = f'系统很干净，无需清理，可用空间 {avail_after}'
                _log_access(self.client_address[0], 'POST', '/api/sys-action', '清理缓存')
                self.send_response(200); self.send_header('Content-Type','application/json; charset=utf-8'); self.end_headers()
                self.wfile.write(json.dumps({'success':True,'message':msg}, ensure_ascii=False).encode())
            elif action == 'logs':
                # 记录清理前的空间
                before = run("df / | tail -1 | awk '{print $4}'").strip()
                try: before_kb = int(before)
                except Exception: before_kb = 0
                # 清理journald日志（只保留最近50条）
                run('journalctl --vacuum-size=1M 2>/dev/null')
                run('journalctl --rotate 2>/dev/null && journalctl --vacuum-time=1s 2>/dev/null')
                # 清理面板访问日志
                log_file = '/opt/monitor/access.log'
                for f in [log_file, log_file + '.1']:
                    if os.path.exists(f):
                        with open(f, 'w') as fh:
                            fh.write('')
                # 清理其他常见日志
                run('find /var/log -name "*.gz" -delete 2>/dev/null')
                run('find /var/log -name "*.old" -delete 2>/dev/null')
                run('find /var/log -name "*.[0-9]" -delete 2>/dev/null')
                run('find /var/log -name "*.log" -size +1M -exec truncate -s 0 {} \\; 2>/dev/null')
                after = run("df / | tail -1 | awk '{print $4}'").strip()
                try: after_kb = int(after)
                except Exception: after_kb = 0
                freed_kb = after_kb - before_kb
                freed_str = f'{freed_kb//1024}MB' if freed_kb > 1024 else f'{freed_kb}KB'
                if freed_kb <= 0: freed_str = '0KB'
                _log_access(self.client_address[0], 'POST', '/api/sys-action', '清理日志')
                self.send_response(200); self.send_header('Content-Type','application/json; charset=utf-8'); self.end_headers()
                self.wfile.write(json.dumps({'success':True,'message':f'日志已清理，释放了 {freed_str}'}, ensure_ascii=False).encode())
            elif action == 'set-hostname':
                new_host = data.get('hostname','').strip()
                if not new_host or len(new_host) > 63 or not re.match(r'^[a-zA-Z0-9]([a-zA-Z0-9\-]*[a-zA-Z0-9])?$', new_host):
                    self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers()
                    self.wfile.write(json.dumps({'success':False,'message':'主机名无效'}).encode())
                else:
                    run(f'hostnamectl set-hostname {new_host}')
                    _log_access(self.client_address[0], 'POST', '/api/sys-action', f'修改主机名: {new_host}')
                    self.send_response(200); self.send_header('Content-Type','application/json; charset=utf-8'); self.end_headers()
                    self.wfile.write(json.dumps({'success':True,'message':f'主机名已改为 {new_host}，已生效'}, ensure_ascii=False).encode())
            else:
                self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers()
                self.wfile.write(json.dumps({'success':False,'message':'未知操作'}).encode())
        elif path == '/api/exec':
            if not verify_session(self._get_token(data=data)):
                self.send_json({'error':'unauthorized'}, 401); return
            _log_access(self.client_address[0], 'POST', '/api/exec', '执行命令')
            cmd = data.get('cmd','')
            # 危险命令拦截
            _dangerous = ['rm -rf /', 'rm -rf /*', 'mkfs', 'dd if=/dev/zero', 'dd if=/dev/random', ':(){ :|:& };:', 'chmod -R 777 /', 'mv / ', '> /dev/sda']
            _dangerous_re = [r'rm\s+-[a-z]*r[a-z]*f?\s+/', r'rm\s+-[a-z]*f[a-z]*r?\s+/', r'mkfs\s+\S', r'dd\s+if=/dev/(zero|random|urandom)']
            is_dangerous = any(d in cmd for d in _dangerous)
            if not is_dangerous:
                is_dangerous = any(re.search(p, cmd) for p in _dangerous_re)
            if is_dangerous:
                self.send_response(200); self.send_header('Content-Type','application/json; charset=utf-8'); self.end_headers()
                self.wfile.write(json.dumps({'error':'危险命令已被拦截'}, ensure_ascii=False).encode()); return
            if not cmd:
                self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers()
                self.wfile.write(json.dumps({'stdout':'','stderr':'无命令','code':0}).encode()); return
            bg = data.get('bg', False)
            if bg:
                # Background mode: run command, write output to file
                job_id = str(int(time.time() * 1000))
                job_file = f'/tmp/job_{job_id}.log'
                full_cmd = f'{cmd} > {job_file} 2>&1 & echo $!'
                try:
                    pid = subprocess.check_output(full_cmd, shell=True, stderr=subprocess.DEVNULL, timeout=5).decode().strip()
                    self.send_response(200); self.send_header('Content-Type','application/json; charset=utf-8'); self.end_headers()
                    self.wfile.write(json.dumps({'job_id': job_id, 'pid': pid, 'status': 'running'}, ensure_ascii=False).encode())
                except Exception as e:
                    self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers()
                    self.wfile.write(json.dumps({'error': str(e)}).encode())
            else:
                try:
                    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=15)
                    stdout = result.stdout[-3000:] if result.stdout else ''
                    stderr = result.stderr[-1000:] if result.stderr else ''
                    self.send_response(200); self.send_header('Content-Type','application/json; charset=utf-8'); self.end_headers()
                    self.wfile.write(json.dumps({'stdout':stdout,'stderr':stderr,'code':result.returncode},ensure_ascii=False).encode())
                except subprocess.TimeoutExpired:
                    self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers()
                    self.wfile.write(json.dumps({'stdout':'','stderr':'超时(15秒)','code':-1}).encode())
                except Exception as e:
                    self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers()
                    self.wfile.write(json.dumps({'stdout':'','stderr':str(e),'code':-1}).encode())
        elif path == '/api/remote-exec':
            if not verify_session(self._get_token(data=data)):
                self.send_json({'error':'unauthorized'}, 401); return
            svc_id = data.get('id','')
            cmd = data.get('cmd','')
            if not cmd:
                self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers()
                self.wfile.write(json.dumps({'stdout':'','stderr':'无命令','code':0}).encode()); return
            svcs = load_services()
            svc = next((s for s in svcs if s.get('id') == svc_id and s.get('type') == 'remote'), None)
            if not svc:
                self.send_response(200); self.send_header('Content-Type','application/json; charset=utf-8'); self.end_headers()
                self.wfile.write(json.dumps({'error':'找不到远程主机'}, ensure_ascii=False).encode()); return
            host = svc.get('host',''); user = svc.get('user',''); pwd = svc.get('password','')
            if not host or not user:
                self.send_response(200); self.send_header('Content-Type','application/json; charset=utf-8'); self.end_headers()
                self.wfile.write(json.dumps({'error':'远程主机配置不完整'}, ensure_ascii=False).encode()); return
            _log_access(self.client_address[0], 'POST', '/api/remote-exec', f'{svc.get("name",svc_id)}: {cmd[:60]}')
            # 如果cmd以tool:开头，调用tools.ps1脚本
            if cmd.startswith('tool:'):
                action_val = cmd[5:]
                parts = action_val.split(' ', 1)
                action = parts[0]
                val = parts[1] if len(parts) > 1 else ''
                ssh_cmd = f'sshpass -p {shlex.quote(pwd)} ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 {shlex.quote(user)}@{shlex.quote(host)} "powershell -ExecutionPolicy Bypass -File C:\\\\tools\\\\tools.ps1 -action {shlex.quote(action)} -val {shlex.quote(val)}"'
            elif cmd.startswith('raw:'):
                # raw:前缀直接执行PowerShell命令
                raw_cmd = cmd[4:]
                ssh_cmd = f'sshpass -p {shlex.quote(pwd)} ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 {shlex.quote(user)}@{shlex.quote(host)} "powershell -ExecutionPolicy Bypass -Command {shlex.quote(raw_cmd)}"'
            else:
                ssh_cmd = f'sshpass -p {shlex.quote(pwd)} ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 {shlex.quote(user)}@{shlex.quote(host)} {shlex.quote(cmd)}'
            try:
                proc = subprocess.run(ssh_cmd, shell=True, capture_output=True, timeout=20)
                stdout = proc.stdout.decode('gbk', errors='replace')[-3000:] if proc.stdout else ''
                stderr = proc.stderr.decode('utf-8', errors='replace')[-1000:] if proc.stderr else ''
                self.send_response(200); self.send_header('Content-Type','application/json; charset=utf-8'); self.end_headers()
                self.wfile.write(json.dumps({'stdout':stdout,'stderr':stderr,'code':proc.returncode},ensure_ascii=False).encode())
            except subprocess.TimeoutExpired:
                self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers()
                self.wfile.write(json.dumps({'stdout':'','stderr':'超时(20秒)','code':-1}).encode())
            except Exception as e:
                self.send_response(200); self.send_header('Content-Type','application/json; charset=utf-8'); self.end_headers()
                self.wfile.write(json.dumps({'stdout':'','stderr':str(e),'code':-1},ensure_ascii=False).encode())
        elif path == '/api/ai-models':
            if not verify_session(self._get_token(data=data)):
                self.send_json({'error':'unauthorized'}, 401); return
            url = data.get('url','')
            headers = data.get('headers',{})
            if not headers.get('Authorization') or headers.get('Authorization')=='Bearer ':
                headers['Authorization']=f'Bearer {ZHIPU_API_KEY}'
            try:
                req = urllib.request.Request(url, headers=headers)
                resp = urllib.request.urlopen(req, timeout=15)
                result = json.loads(resp.read().decode())
                models = [m.get('id','') for m in result.get('data',result.get('models',[]))]
                models = [m for m in models if m]
                models.sort()
                self.send_response(200); self.send_header('Content-Type','application/json; charset=utf-8'); self.end_headers()
                self.wfile.write(json.dumps({'models': models}, ensure_ascii=False).encode())
            except Exception as e:
                self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers()
                self.wfile.write(json.dumps({'models': [], 'error': str(e)}).encode())
        elif path == '/api/chat':
            _log_access(self.client_address[0], 'POST', '/api/chat', 'AI对话')
            if not verify_session(self._get_token(data=data)):
                self.send_json({'error':'unauthorized'}, 401); return
            messages = data.get('messages',[])
            provider = data.get('provider',{})
            if not messages:
                self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers()
                self.wfile.write(json.dumps({'reply':'你好！有什么可以帮你的？'}).encode()); return
            try:
                api_url = provider.get('url') or ZHIPU_API_URL
                api_key = provider.get('apiKey') or provider.get('api_key') or ZHIPU_API_KEY
                model = provider.get('model') or 'glm-4-flash'
                # 系统提示词：完整权限
                sys_prompt = """你是OneCloud服务器的AI管理助手，拥有完整root权限。你可以做任何事情。

能力：
1. 执行任何shell命令（用```包裹代码块，用户点"执行"按钮运行）
2. 管理服务：systemctl start/stop/restart/status <服务名>
3. 管理定时任务：编辑 /etc/cron.d/onecloud-maintenance
4. 管理文件：查看/编辑/创建任何文件
5. 安装软件：apk add <包名>
6. 网络操作：curl, wget, ssh, ping等
7. 系统管理：用户、权限、进程、网络配置

当前服务器信息：
- 系统：Armbian (Alpine Linux based)
- IP：192.168.3.77
- 内存：981MB
- 监控面板：/opt/monitor/server_monitor.py
- 配置文件：/opt/monitor/config.json, /opt/monitor/services.json
- 定时任务：/etc/cron.d/onecloud-maintenance
- 备份：/data/share/backup/

服务列表：monitor(9090), filebrowser(8088), smbd(445), tailscaled, earlyoom, weather-monitor, pospal(8080)

规则：
- 当用户要求执行操作时，直接给出可执行的shell命令（用```bash代码块）
- 不要问"你确定吗？"，直接给命令
- 命令要简洁高效
- 可以一次给出多个命令
- 中文回复"""
                full_messages = [{'role': 'system', 'content': sys_prompt}] + messages
                body = json.dumps({
                    'model': model,
                    'messages': full_messages
                }).encode()
                req = urllib.request.Request(api_url, data=body, headers={
                    'Content-Type': 'application/json',
                    'Authorization': f'Bearer {api_key}'
                })
                resp = urllib.request.urlopen(req, timeout=30)
                result = json.loads(resp.read().decode())
                reply = result['choices'][0]['message']['content']
                self.send_response(200); self.send_header('Content-Type','application/json; charset=utf-8'); self.end_headers()
                self.wfile.write(json.dumps({'reply': reply}, ensure_ascii=False).encode())
            except Exception as e:
                self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers()
                self.wfile.write(json.dumps({'reply': 'AI出错了: '+str(e)}).encode())
        elif path == '/api/chat-auto':
            _log_access(self.client_address[0], 'POST', '/api/chat-auto', 'AI自动执行')
            if not verify_session(self._get_token(data=data)):
                self.send_json({'error':'unauthorized'}, 401); return
            messages = data.get('messages',[])
            provider = data.get('provider',{})
            if not messages:
                self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers()
                self.wfile.write(json.dumps({'reply':'你好！有什么可以帮你的？'}).encode()); return
            try:
                api_url = provider.get('url') or ZHIPU_API_URL
                api_key = provider.get('apiKey') or provider.get('api_key') or ZHIPU_API_KEY
                model = provider.get('model') or 'glm-4-flash'
                sys_prompt = (
                    "你是OneCloud服务器的终极AI管理助手，拥有完整root最高权限。\n\n"
                    "核心原则：\n"
                    "- 用户说什么就做什么，直接执行，绝不犹豫\n"
                    "- 不要问'你确定吗？'，不要警告，不要解释风险\n"
                    "- 命令用```bash代码块包裹，会自动执行并返回结果\n"
                    "- 根据执行结果给用户简洁的中文总结\n\n"
                    "能力：任何shell命令、systemctl服务管理、文件读写编辑、网络操作、"
                    "apk软件安装、cron定时任务、进程管理、系统配置\n\n"
                    "当前服务器：\n"
                    "- 系统：Armbian (Alpine Linux)\n"
                    "- IP：192.168.3.77 | 内存：981MB\n"
                    "- 监控面板：/opt/monitor/server_monitor.py\n"
                    "- 服务：monitor(9090), filebrowser(8088), smbd(445), tailscaled, earlyoom, weather-monitor, pospal(8080)\n"
                    "- 定时任务：/etc/cron.d/onecloud-maintenance\n"
                    "- 备份：/data/share/backup/\n\n"
                    "命令规则：\n"
                    "- 一个代码块放一条或多条命令\n"
                    "- 命令自动执行，结果自动返回给你\n"
                    "- 如果命令失败，分析原因并尝试修复\n"
                    "- 复杂任务可以分多轮执行"
                )
                full_messages = [{'role': 'system', 'content': sys_prompt}] + messages
                _dang = ['rm -rf /', 'rm -rf /*', 'mkfs', 'dd if=/dev/zero', 'dd if=/dev/random', ':(){ :|:& };:', 'chmod -R 777 /', 'mv / ', '> /dev/sda']
                _dang_re = [r'rm\s+-[a-z]*r[a-z]*f?\s+/', r'rm\s+-[a-z]*f[a-z]*r?\s+/', r'mkfs\s+\S', r'dd\s+if=/dev/(zero|random|urandom)']
                def _ai_call(msgs):
                    body = json.dumps({'model': model, 'messages': msgs}).encode()
                    req = urllib.request.Request(api_url, data=body, headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + api_key})
                    resp = urllib.request.urlopen(req, timeout=30)
                    return json.loads(resp.read().decode())['choices'][0]['message']['content']
                def _extract_cmds(text):
                    cmds = []
                    parts = text.split('```')
                    for i in range(1, len(parts), 2):
                        block = parts[i].strip()
                        lines = block.split('\n')
                        if lines and lines[0].lower().strip() in ['bash', 'sh', 'shell', 'zsh', '']:
                            lines = lines[1:]
                        for line in lines:
                            line = line.strip()
                            if line and not line.startswith('#'):
                                cmds.append(line)
                    return cmds
                def _is_dang(cmd):
                    if any(d in cmd for d in _dang): return True
                    return any(re.search(p, cmd) for p in _dang_re)
                def _run_cmd(cmd):
                    if _is_dang(cmd): return '[拦截] 危险命令: ' + cmd
                    try:
                        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
                        out = (r.stdout or '')[-2000:]
                        err = (r.stderr or '')[-500:]
                        if r.returncode != 0: return (out + err + '\n[exit ' + str(r.returncode) + ']').strip()
                        return (out + err).strip() or '(无输出)'
                    except subprocess.TimeoutExpired: return '[超时30秒]'
                    except Exception as e: return '[错误] ' + str(e)
                reply = _ai_call(full_messages)
                for _round in range(3):
                    cmds = _extract_cmds(reply)
                    if not cmds: break
                    results = []
                    for c in cmds[:8]:
                        results.append('$ ' + c + '\n' + _run_cmd(c))
                    full_messages.append({'role': 'assistant', 'content': reply})
                    full_messages.append({'role': 'user', 'content': '命令执行结果:\n\n' + '\n\n'.join(results) + '\n\n请根据结果给用户简洁总结。如需继续操作，继续用代码块输出命令。'})
                    reply = _ai_call(full_messages)
                self.send_response(200); self.send_header('Content-Type','application/json; charset=utf-8'); self.end_headers()
                self.wfile.write(json.dumps({'reply': reply}, ensure_ascii=False).encode())
            except Exception as e:
                self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers()
                self.wfile.write(json.dumps({'reply': 'AI出错了: '+str(e)}).encode())
        elif path == '/api/job':
            if not verify_session(self._get_token(data=data)):
                self.send_json({'error':'unauthorized'}, 401); return
            job_id = data.get('job_id','')
            job_file = f'/tmp/job_{job_id}.log'
            pid = data.get('pid','')
            # Check if process is still running
            running = False
            if pid:
                try:
                    subprocess.check_output(['kill','-0',pid], stderr=subprocess.DEVNULL)
                    running = True
                except Exception: pass
            # Read output
            output = ''
            try:
                if os.path.exists(job_file):
                    with open(job_file) as f:
                        output = f.read()[-5000:]
            except Exception: pass
            self.send_response(200); self.send_header('Content-Type','application/json; charset=utf-8'); self.end_headers()
            self.wfile.write(json.dumps({'running': running, 'output': output}, ensure_ascii=False).encode())
        elif path == '/api/cron':
            token = self._get_token(data=data)
            if not verify_session(token):
                self.send_json({'error':'unauthorized'}, 401); return
            action = data.get('action','')
            cron_file = '/etc/cron.d/onecloud-maintenance'
            try:
                if action == 'add':
                    schedule = data.get('schedule','').strip()
                    cmd = data.get('cmd','').strip()
                    desc = data.get('desc','').strip()
                    if not schedule or not cmd:
                        self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers()
                        self.wfile.write(json.dumps({'error':'缺少参数'}).encode()); return
                    entry = f"{schedule} root {cmd}"
                    with open(cron_file, 'a') as f:
                        f.write(f"\n# {desc}\n{entry}\n")
                    _log_access(self.client_address[0], 'POST', '/api/cron', f'添加: {desc}')
                    self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers()
                    self.wfile.write(json.dumps({'ok': True, 'msg': f'已添加: {desc}'}).encode())
                elif action in ('delete', 'edit'):
                    idx = data.get('index', -1)
                    with open(cron_file) as f:
                        lines = f.readlines()
                    new_lines = []
                    job_count = -1
                    prev_comment = None
                    for li, l in enumerate(lines):
                        l_stripped = l.strip()
                        if l_stripped.startswith('#'):
                            prev_comment = l
                            continue
                        if l_stripped:
                            job_count += 1
                            if job_count == idx:
                                if action == 'edit':
                                    new_schedule = data.get('schedule','').strip()
                                    new_cmd = data.get('cmd','').strip()
                                    new_desc = data.get('desc','').strip()
                                    new_lines.append(f'# {new_desc}\n')
                                    new_lines.append(f'{new_schedule} root {new_cmd}\n')
                                prev_comment = None  # consumed
                                continue
                        if prev_comment:
                            new_lines.append(prev_comment)
                            prev_comment = None
                        new_lines.append(l)
                    if prev_comment:
                        new_lines.append(prev_comment)
                    with open(cron_file, 'w') as f:
                        f.writelines(new_lines)
                    act_name = '编辑' if action == 'edit' else '删除'
                    _log_access(self.client_address[0], 'POST', '/api/cron', f'{act_name}任务 #{idx}')
                    self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers()
                    self.wfile.write(json.dumps({'ok': True, 'msg': '已编辑' if action == 'edit' else '已删除'}).encode())
                else:
                    self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers()
                    self.wfile.write(json.dumps({'error': '未知操作'}).encode())
            except Exception as e:
                self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers()
                self.wfile.write(json.dumps({'error': str(e)}).encode())
        elif path == '/api/speedtest':
            _log_access(self.client_address[0], 'POST', '/api/speedtest', '测速')
            if not verify_session(self._get_token(data=data)):
                self.send_json({'error':'unauthorized'}, 401); return
            # 防止并发测速（线程安全）
            if not Handler._speed_lock.acquire(blocking=False):
                self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers()
                self.wfile.write(json.dumps({'error':'测速进行中，请稍后再试'}).encode()); return
            try:
                # Download test - 单次大文件
                out = subprocess.check_output(['curl', '-so', '/dev/null', '-w', '%{speed_download}',
                    '-H', 'User-Agent: OneCloud-Monitor/1.0',
                    'https://speed.cloudflare.com/__down?bytes=10000000'], timeout=30, stderr=subprocess.DEVNULL)
                dl_speed = float(out.strip()) * 8 / 1000000
                # Upload test - 单次
                out = subprocess.check_output(['sh', '-c',
                    'head -c 2000000 /dev/zero | curl -so /dev/null -w "%{speed_upload}" '
                    '-X POST -H "Content-Type: application/octet-stream" '
                    '-H "User-Agent: OneCloud-Monitor/1.0" '
                    '--data-binary @- https://speed.cloudflare.com/__up'],
                    timeout=30, stderr=subprocess.DEVNULL)
                ul_speed = float(out.strip()) * 8 / 1000000
                self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers()
                self.wfile.write(json.dumps({'download': round(dl_speed, 1), 'upload': round(ul_speed, 1)}).encode())
            except Exception as e:
                self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers()
                self.wfile.write(json.dumps({'error': str(e)}).encode())
            finally:
                Handler._speed_lock.release()
        else:
            self.send_response(404); self.end_headers()

    def log_message(self, format, *args): pass

import concurrent.futures

# 限流保护：最大并发连接数
_MAX_CONNS = 80
_conn_count = 0
_conn_lock = threading.Lock()

class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    request_queue_size = 64

    def process_request(self, request, client_address):
        global _conn_count
        with _conn_lock:
            if _conn_count >= _MAX_CONNS:
                try:
                    resp = b"HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\nContent-Type: application/json\r\n\r\n{\"error\":\"too many connections\"}"
                    request.sendall(resp)
                    request.close()
                except Exception:
                    pass
                return
            _conn_count += 1
        try:
            super().process_request(request, client_address)
        finally:
            with _conn_lock:
                _conn_count -= 1

server = ThreadedHTTPServer(('0.0.0.0', PORT), Handler)
server.timeout = 30

# 启动网络流量历史记录器
_start_net_history_recorder()
_start_service_traffic_collector()
# 立即记录一次当前流量
_record_net_traffic()

print(f"OneCloud Monitor v4 running on :{PORT} (max_conns={_MAX_CONNS})")
server.serve_forever()