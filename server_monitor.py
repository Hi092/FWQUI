#!/usr/bin/env python3
"""OneCloud Server Monitor v4 - Compact Ring Dashboard"""

import http.server
import socketserver
import json
import subprocess
import os
import sys
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
import shutil

PAUSED_FILE = '/opt/monitor/paused.json'
PUSHPLUS_TOKEN = os.environ.get('PUSHPLUS_TOKEN', '')

# === 网络流量历史记录 ===
_NET_HISTORY_FILE = '/opt/monitor/net_history.json'
_net_history_lock = threading.Lock()
_service_traffic_cache = {}
_service_traffic_lock = threading.Lock()

def _collect_service_traffic():
    """Background collector for service traffic data."""
    global _service_traffic_cache, _dl_traffic_bytes
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
                    if dport in ['8080','8088','445']:
                        port_stats.setdefault(dport,{'rx':0,'tx':0})['rx'] += bytes_count
                    if sport in ['8080','8088','445']:
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
    # 文件管理下载流量
    result['downloads'] = {
        'name': '文件管理',
        'rx': 0,
        'tx': _dl_traffic_bytes
    }
    with _service_traffic_lock:
        _service_traffic_cache = result
    # 每30秒持久化一次下载流量
    now = time.time()
    if getattr(_collect_service_traffic, '_last_persist', 0) == 0:
        _collect_service_traffic._last_persist = now
    if now - _collect_service_traffic._last_persist > 30:
        _collect_service_traffic._last_persist = now
        _save_dl_traffic()

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
        tmp = _NET_HISTORY_FILE + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _NET_HISTORY_FILE)
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
            time.sleep(60)  # 每分钟记录一次
    
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
        used_fix = total - avail
        if used_fix < 0: used_fix = 0
        status['memory'] = {'total_mb': round(total/1024), 'used_mb': round(used_fix/1024), 'avail_mb': round(avail/1024), 'cached_mb': round(cached/1024), 'percent': round(100*(total-avail)/max(total,1),1)}
        st = meminfo.get('SwapTotal', 0)
        st = meminfo.get('SwapTotal', 0)
        sf = meminfo.get('SwapFree', 0)
        # Per-device swap details（多个zram合并为一个）
        swap_devices = []
        swapon_out = run('swapon --show --bytes --noheadings').strip()
        zram_total = 0
        zram_used = 0
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
                if is_zram:
                    zram_total += dev_size
                    zram_used += dev_used
                else:
                    swap_devices.append({
                        'name': dev_name, 'type': 'Disk',
                        'size_mb': round(dev_size/1048576), 'used_mb': round(dev_used/1048576),
                        'percent': round(100*dev_used/max(dev_size,1),1), 'priority': dev_prio
                    })
        if zram_total > 0:
            swap_devices.append({
                'name': 'zram', 'type': 'ZRAM',
                'size_mb': round(zram_total/1048576), 'used_mb': round(zram_used/1048576),
                'percent': round(100*zram_used/max(zram_total,1),1), 'priority': '-'
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

    # Cloudflare Tunnel 信息（快速隧道版）
    try:
        tn = run('curl -s http://localhost:51888/quicktunnel 2>/dev/null', timeout=3).strip()
        if tn and tn.startswith('{'):
            tinfo = json.loads(tn)
            new_url = 'https://'+tinfo['hostname']
            status['tunnel'] = {'url': new_url, 'active': True}
            # 检测域名变动并推送微信通知
            _tunnel_file = '/opt/monitor/last_tunnel_url'
            try:
                old_url = ''
                if os.path.exists(_tunnel_file):
                    with open(_tunnel_file) as tf:
                        old_url = tf.read().strip()
                if new_url != old_url and old_url:
                    _pushplus('Cloudflare Tunnel 已更新',
                               '<p>旧地址: ' + old_url + '</p><p>新地址: ' + new_url + '</p>')
                with open(_tunnel_file, 'w') as tf:
                    tf.write(new_url)
            except Exception:
                pass
        else:
            status['tunnel'] = {'url': None, 'active': False}
    except Exception:
        status['tunnel'] = {'url': None, 'active': False}

    # FileBrowser 自动登录 token（缓存 1.5 小时，JWT 有效 2 小时）
    try:
        _now = time.time()
        if not hasattr(get_status, '_fbtoken') or _now - get_status._fbts > 5400:
            resp = run("curl -s -X POST http://localhost:8088/api/login "
                       "-H 'Content-Type: application/json' "
                       "-d '{\"username\":\"al2560335@gmail.com\",\"password\":\"lixueyi1998.\"}' 2>/dev/null",
                       timeout=5).strip()
            if resp and resp.startswith('eyJ'):
                get_status._fbtoken = resp
                get_status._fbts = _now
        status['filebrowser_token'] = get_status._fbtoken if hasattr(get_status, '_fbtoken') else None
    except Exception:
        status['filebrowser_token'] = None

    # SSL证书通知
    try:
        with open("/opt/monitor/ssl_notify.txt") as f:
            status["ssl_notify"] = f.read().strip()
    except:
        status["ssl_notify"] = None

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
    except Exception as e:
        try:
            with open('/opt/monitor/pushplus_error.log', 'a') as pf:
                pf.write('[' + datetime.now().isoformat() + '] PushPlus fail: ' + str(e) + '\n')
        except Exception:
            pass

# ============================================================
# 文件管理 - 下载器
# ============================================================

_DOWNLOAD_HISTORY_FILE = '/opt/monitor/download_history.json'
_DL_TASKS_FILE = '/opt/monitor/dl_tasks.json'
_downloads = {}
_download_lock = threading.RLock()
_downloads_cache = []  # 缓存下载状态，避免锁超时时返回"服务器繁忙"
_downloads_cache_ts = 0
_download_queue = []
_active_download = None
_dl_counter = [0]
_ffmpeg_procs = {}  # dl_id -> subprocess.Popen
_direct_procs = {}  # dl_id -> subprocess.Popen (curl/aria2c) (用于暂停时杀进程)
def _kill_ffmpeg(dl_id):
    """杀 ffmpeg（systemd-run 包装的），先 systemctl stop 再 terminate"""
    entry = _ffmpeg_procs.get(dl_id)
    if not entry:
        return
    proc, unit_name = (entry if isinstance(entry, tuple) else (entry, None))
    if unit_name:
        try:
            subprocess.run(['systemctl', 'stop', unit_name + '.service'],
                         capture_output=True, timeout=5)
        except Exception:
            pass
    try:
        proc.terminate()
    except Exception:
        pass
    try:
        proc.wait(timeout=3)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    finally:
        _ffmpeg_procs.pop(dl_id, None)
_dl_traffic_bytes = 0  # 下载累计流量
_DL_TRAFFIC_PERSIST = '/opt/monitor/dl_traffic.json'

def _load_dl_traffic():
    global _dl_traffic_bytes
    try:
        if os.path.exists(_DL_TRAFFIC_PERSIST):
            with open(_DL_TRAFFIC_PERSIST) as f:
                _dl_traffic_bytes = json.load(f).get('total', 0)
    except Exception:
        pass

def _save_dl_traffic():
    try:
        with open(_DL_TRAFFIC_PERSIST, 'w') as f:
            json.dump({'total': _dl_traffic_bytes, 'updated': datetime.now().isoformat()}, f)
    except Exception:
        pass

def _load_dl_history():
    try:
        if os.path.exists(_DOWNLOAD_HISTORY_FILE):
            with open(_DOWNLOAD_HISTORY_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return []

def _save_dl_history(history):
    try:
        history = history[-200:]
        tmp = _DOWNLOAD_HISTORY_FILE + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(history, f, ensure_ascii=False)
        os.replace(tmp, _DOWNLOAD_HISTORY_FILE)
    except Exception:
        pass
def _save_dl_tasks():
    """持久化下载任务"""
    _dbg_cnt = len(_downloads)
    try:
        with open('/opt/monitor/save_called.log', 'a') as _sf:
            _sf.write('[' + datetime.now().isoformat() + '] called cnt=' + str(_dbg_cnt) + '\n')
    except Exception:
        pass
    try:
        with _download_lock:
            tasks = {}
            for dl_id, dl in _downloads.items():
                t = {k: v for k, v in dl.items() if not k.startswith('_')}
                tasks[dl_id] = t
        tmp = _DL_TASKS_FILE + '.tmp.' + str(os.getpid()) + '.' + str(id(object()))
        with open(tmp, 'w') as f:
            json.dump(tasks, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _DL_TASKS_FILE)
    except Exception as _save_err:
        try:
            with open("/opt/monitor/save_debug.log", "a") as _dbg:
                _dbg.write("[" + datetime.now().isoformat() + "] SAVE FAILED: " + str(_save_err) + " tasks_count=" + str(len(tasks)) + "\n")
        except Exception:
            pass

def _load_dl_tasks():
    """启动时恢复下载任务"""
    if not os.path.exists(_DL_TASKS_FILE):
        return
    try:
        with open(_DL_TASKS_FILE, 'r') as f:
            tasks = json.load(f)
        for dl_id, dl in tasks.items():
            status = dl.get('status', '')
            if status in ('completed', 'failed', 'cancelled'):
                _downloads[dl_id] = dl
                continue
            if status in ('downloading', 'paused'):
                dl['status'] = 'interrupted'
                dl['progress'] = 0
                dl['downloaded_bytes'] = 0
                dl['error'] = '服务重启，下载中断' if status == 'downloading' else '服务重启，暂停任务已中断'
                # 清理可能残留的旧 systemd unit（服务重启后孤立 ffmpeg 进程）
                unit_name = 'dl-' + dl_id + '.service'
                try:
                    subprocess.run(['systemctl', 'stop', unit_name],
                                 capture_output=True, timeout=5)
                except Exception:
                    pass
                try:
                    subprocess.run(['systemctl', 'reset-failed', unit_name],
                                 capture_output=True, timeout=3)
                except Exception:
                    pass
            _downloads[dl_id] = dl
            # 恢复排队任务到下载队列（包括中断的任务）
            if dl.get('status') in ('queued', 'interrupted'):
                _download_queue.append(dl_id)
    except Exception:
        pass
    # 触发队列处理
    threading.Thread(target=_process_queue, daemon=True).start()

def _validate_mp4(output_path):
    if not output_path or not os.path.exists(output_path):
        return False
    if os.path.splitext(output_path)[1].lower() not in (".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v"):
        return True
    try:
        r = subprocess.run(["ffprobe", "-v", "info", "-show_streams", output_path], capture_output=True, text=True, timeout=60)
        stderr = r.stderr or ""
        if "moov atom not found" in stderr or "Invalid data found" in stderr:
            tmp = output_path + ".fixing.mp4"
            if os.path.exists(tmp):
                os.remove(tmp)
            r2 = subprocess.run(["ffmpeg", "-y", "-i", output_path, "-c", "copy", "-movflags", "+faststart", "-threads", "1", "-max_muxing_queue_size", "1024", tmp], capture_output=True, text=True, timeout=900)
            if r2.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 0:
                os.replace(tmp, output_path)
                return True
            else:
                if os.path.exists(tmp):
                    os.remove(tmp)
                return False
        if r.returncode != 0 and stderr.strip():
            return False
        if "codec_type=video" not in (r.stdout or ""):
            return False
        return True
    except subprocess.TimeoutExpired:
        return False
    except Exception:
        return False

def _add_to_history(dl):
    try:
        history = _load_dl_history()
        history.append({
            'id': dl['id'],
            'url': dl['url'],
            'filename': dl.get('filename', ''),
            'folder': dl.get('folder', ''),
            'size_mb': round(dl.get('downloaded_bytes', 0) / 1048576, 1),
            'status': dl.get('status', 'completed'),
            'completed_at': dl.get('completed_at', datetime.now().isoformat()),
            'error': dl.get('error', ''),
        })
        _save_dl_history(history)
    except Exception:
        pass

def _get_browse_tree(path):
    # 安全限制：只允许浏览 /data/share 及其子目录
    if not path.startswith('/data/share'):
        path = '/data/share'
    real = os.path.realpath(path)
    if not real.startswith('/data/share'):
        path = '/data/share'
        real = '/data/share'
    if not os.path.exists(real):
        return {'error': '路径不存在', 'path': path}
    if not os.path.isdir(real):
        return {'error': '不是目录', 'path': path}
    items = []
    try:
        for name in sorted(os.listdir(real)):
            full = os.path.join(real, name)
            if os.path.isdir(full) and name not in ("lost+found",) and not name.startswith('.'):
                try:
                    sub_items = len([x for x in os.listdir(full) if os.path.isdir(os.path.join(full, x)) and not x.startswith('.')])
                except Exception:
                    sub_items = 0
                items.append({'name': name, 'path': full, 'type': 'dir', 'children': sub_items})
    except PermissionError:
        return {'error': '无权限访问', 'path': path}
    return {'path': path, 'items': items}

def _get_downloads_status():
    global _downloads_cache, _downloads_cache_ts
    acquired = _download_lock.acquire(timeout=5)
    if not acquired:
        # 返回缓存状态，而不是"服务器繁忙"
        if _downloads_cache:
            return _downloads_cache
        return [{'status': 'downloading', 'progress': 0, 'filename': '下载中...', 'position': 'active', 'status_detail': '加载中...'}]
    try:
        result = []
        now_ts = time.time()
        if _active_download and _active_download in _downloads:
            _compute_speed_eta(_downloads[_active_download], now_ts)
            dl = dict(_downloads[_active_download])
            dl['position'] = 'active'
            result.append(dl)
        for i, dl_id in enumerate(_download_queue):
            if dl_id in _downloads:
                dl = dict(_downloads[dl_id])
                dl['position'] = 'queue_' + str(i + 1)
                result.append(dl)
        seen = set()
        for r in result:
            seen.add(r.get('id', ''))
        for dl_id, dl in list(_downloads.items()):
            if dl.get('status') in ('completed', 'failed', 'cancelled', 'paused'):
                if dl_id not in seen:
                    result.append(dict(dl))
                    seen.add(dl_id)
        # 更新缓存
        _downloads_cache = result
        _downloads_cache_ts = time.time()
        return result
    finally:
        _download_lock.release()

def _compute_speed_eta(dl, now_ts):
    """为活跃下载计算速度和预估剩余时间（3秒滑动窗口）"""
    dl['speed_mbps'] = 0
    dl['eta_sec'] = 0
    # 3秒滑动窗口：用最近3秒的增量算瞬时速度
    last_sample = dl.get('_speed_sample', {})
    started = dl.get('started_at')
    if not started:
        return
    try:
        if isinstance(started, str):
            elapsed = now_ts - datetime.fromisoformat(started).timestamp()
        else:
            elapsed = now_ts - started
    except Exception:
        return
    if elapsed < 0.5:
        return
    
    if dl.get('is_m3u8') and dl.get('total_segments', 0) > 0:
        # m3u8: 用文件实际大小算速度（3秒滑动窗口），时间差算ETA
        out_path = dl.get('output_path', '')
        try:
            cur_bytes = os.path.getsize(out_path) if out_path and os.path.exists(out_path) else 0

        except Exception:
            cur_bytes = 0
        dl['downloaded_bytes'] = cur_bytes
        # 3秒滑动窗口算瞬时速度
        if last_sample and cur_bytes > last_sample.get('bytes', 0):
            delta_bytes = cur_bytes - last_sample['bytes']
            delta_t = now_ts - last_sample['ts']
            if delta_t > 0.5:
                dl['speed_mbps'] = round(delta_bytes / delta_t / 1048576, 2)
        if cur_bytes > 0:
            dl['_speed_sample'] = {'bytes': cur_bytes, 'ts': now_ts}
        # 用总耗时作为 fallback
        if dl['speed_mbps'] == 0 and cur_bytes > 0 and elapsed > 0.5:
            dl['speed_mbps'] = round(cur_bytes / elapsed / 1048576, 2)
        out_secs = dl.get('downloaded_segments', 0)
        total_secs = dl.get('total_segments', 0)
        remaining = max(0, total_secs - out_secs)
        if dl['speed_mbps'] > 0 and cur_bytes > 0:
            dl['eta_sec'] = int(remaining / (out_secs / elapsed)) if out_secs > 0 else 0
        else:
            dl['eta_sec'] = int(remaining)
    else:
        # direct (curl): 用实际文件大小算速度（3秒滑动窗口）
        out_path = dl.get('output_path', '')
        cur_bytes = 0
        try:
            if out_path and os.path.exists(out_path):
                cur_bytes = os.path.getsize(out_path)
            elif out_path:
                part_path = out_path + '.part'
                if os.path.exists(part_path):
                    cur_bytes = os.path.getsize(part_path)
        except Exception:
            pass
        # 3秒滑动窗口
        if last_sample and cur_bytes > last_sample.get('bytes', 0):
            delta_bytes = cur_bytes - last_sample['bytes']
            delta_t = now_ts - last_sample['ts']
            if delta_t > 0.5:
                dl['speed_mbps'] = round(delta_bytes / delta_t / 1048576, 2)
        if cur_bytes > 0:
            dl['_speed_sample'] = {'bytes': cur_bytes, 'ts': now_ts}
        # fallback
        if dl['speed_mbps'] == 0 and cur_bytes > 0 and elapsed > 0.5:
            dl['speed_mbps'] = round(cur_bytes / elapsed / 1048576, 2)
        total = dl.get('total_bytes', 0)
        if total > cur_bytes and dl['speed_mbps'] > 0:
            dl['eta_sec'] = int((total - cur_bytes) / (dl['speed_mbps'] * 1048576))
        else:
            dl['eta_sec'] = 0

def _run_download(dl_id):
    global _active_download
    with _download_lock:
        if dl_id not in _downloads:
            return
        dl = _downloads[dl_id]
        # 已取消/暂停的跳过
        if dl.get('status') in ('cancelling', 'cancelled', 'paused', 'failed', 'completed'):
            return
        dl['status'] = 'downloading'
        dl['error'] = ''
        # 记录下载开始时的文件大小，用于finally块判断
        _out_for_check = dl.get('output_path', '')
        if _out_for_check and os.path.exists(_out_for_check):
            dl['_file_size_at_start'] = os.path.getsize(_out_for_check)
        else:
            dl['_file_size_at_start'] = 0
        try:
            with open('/opt/monitor/run_dl_debug.log', 'a') as _dbgf:
                _dbgf.write('[' + datetime.now().isoformat() + '] _run_download set downloading dl_id=' + str(dl_id) + '\n')
        except Exception:
            pass
        # 续传不重置 started_at，保留原始开始时间
        if not dl.get('_resumed'):
            dl['started_at'] = datetime.now().isoformat()
        else:
            dl['_resumed'] = False
        _active_download = dl_id
    # 立即保存状态，避免进度循环未触发时状态丢失
    try:
        _save_dl_tasks()
    except Exception:
        pass
    url = dl['url']
    folder = dl.get('folder', '/data/share/视频')
    filename = dl.get('filename', '')
    os.makedirs(folder, exist_ok=True)
    if not filename:
        parsed = urlparse(url)
        base = os.path.basename(parsed.path)
        if base.endswith('.m3u8'):
            base = base.rsplit('.', 1)[0] + '.mp4'
        elif not base or '.' not in base:
            base = 'download_' + dl_id[:8] + '.mp4'
        filename = base
    # 续传保留原路径（曾启动过的下载始终复用原路径）
    prev_path = dl.get('output_path', '')
    if prev_path:
        output_path = prev_path
    else:
        output_path = os.path.join(folder, filename)
        counter = 1
        orig_name, ext = os.path.splitext(filename)
        while os.path.exists(output_path):
            filename = orig_name + ' (' + str(counter) + ')' + ext
            output_path = os.path.join(folder, filename)
            counter += 1
        dl['filename'] = filename
        dl['output_path'] = output_path
    # 持久化文件名和路径，避免重启后重新生成
    try:
        _save_dl_tasks()
    except Exception:
        pass
    is_m3u8 = urlparse(url).path.endswith('.m3u8')
    try:
        if is_m3u8:
            _download_m3u8(dl, url, output_path, dl.get('referer', ''))
        else:
            _download_direct(dl, url, output_path)
    except Exception as e:
        with _download_lock:
            if dl_id in _downloads:
                dl2 = _downloads[dl_id]
                cur_status = dl2.get('status', '')
                if cur_status == 'cancelling':
                    dl2['status'] = 'cancelled'
                    dl2['completed_at'] = datetime.now().isoformat()
                    _save_dl_tasks()
                elif cur_status != 'paused':
                    dl2['status'] = 'failed'
                    dl2['error'] = str(e)
                    _kill_ffmpeg(dl_id)  # 杀残留ffmpeg进程
                    try:
                        with open('/opt/monitor/dl_error.log', 'a') as _ef:
                            _ef.write('[' + datetime.now().isoformat() + '] dl_id=' + str(dl_id))
                            _ef.write(chr(10))
                            _ef.write('URL: ' + dl.get('url', ''))
                            _ef.write(chr(10))
                            _ef.write('error: ' + str(e))
                            _ef.write(chr(10) + chr(10))
                    except Exception:
                        pass
                    dl2['completed_at'] = datetime.now().isoformat()
                    _add_to_history(dl2)
                    try:
                        _pushplus('下载失败: ' + filename, '<p>文件: ' + filename + '</p><p>错误: ' + str(e) + '</p>')
                    except Exception:
                        pass
    finally:
        # 兜底：仅当下载进程真正完成（不在队列中、非续传刚启动）才标记完成
        # 避免resume后文件已存在就误判为完成
        try:
            with _download_lock:
                if dl_id in _downloads:
                    _dl = _downloads[dl_id]
                    # 只有当前活跃下载且状态仍为downloading才检查
                    if _dl.get('status') == 'downloading' and _active_download == dl_id:
                        _out = _dl.get('output_path', '')
                        if _out and os.path.exists(_out) and os.path.getsize(_out) > 1024:
                            # 额外检查：文件大小是否比开始时增长了（排除resume场景）
                            _started = _dl.get('_file_size_at_start', 0)
                            _fsize = os.path.getsize(_out)
                            if _fsize > _started:
                                _dl['status'] = 'completed'
                                _dl['progress'] = 100
                                _dl['downloaded_bytes'] = _fsize
                                _dl['size_mb'] = round(_fsize / 1048576, 1)
                                _dl['completed_at'] = datetime.now().isoformat()
                                _dl.pop('status_detail', None)
                                _dl.pop('total_segments', None)
                                _dl.pop('downloaded_segments', None)
                                _save_dl_tasks()
                                _add_to_history(_dl)
        except Exception:
            pass
        with _download_lock:
            if _active_download == dl_id:
                _active_download = None
        _process_queue()

def _download_direct(dl, url, output_path):
    global _dl_traffic_bytes
    dl_id = dl['id']
    existing_bytes = 0
    tmp_path = output_path + '.part'
    if os.path.exists(tmp_path):
        existing_bytes = os.path.getsize(tmp_path)
    elif os.path.exists(output_path):
        existing_bytes = os.path.getsize(output_path)
    # 获取远程文件总大小（HEAD 请求）
    try:
        head_result = subprocess.run(
            ['curl', '-sI', '-L', '--max-time', '15', url],
            capture_output=True, text=True, timeout=20
        )
        for hline in head_result.stdout.split('\n'):
            if 'content-length:' in hline.lower().strip():
                total_bytes = int(hline.split(':')[1].strip())
                with _download_lock:
                    if dl_id in _downloads:
                        dl = _downloads[dl_id]
                        dl['total_bytes'] = total_bytes
                break
    except Exception:
        pass
    # 优先使用 aria2c 多线程，不支持断点续传时 fallback curl
    use_aria2 = True
    try:
        subprocess.run(['aria2c', '--version'], capture_output=True, timeout=3)
    except Exception:
        use_aria2 = False
    
    if use_aria2 and existing_bytes == 0:
        # aria2c 多线程并发下载（默认 5 线程）
        aria2_cmd = ['systemd-run', '--wait', '--pipe', '--collect', '-p', 'MemoryMax=300M',
                     'aria2c', '-x', '5', '-s', '5', '--connect-timeout=30',
                     '--timeout=600', '--summary-interval=1', '-o', output_path, url]
        proc = subprocess.Popen(aria2_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=1, universal_newlines=True)
        using_aria2 = True
    else:
        cmd = "curl -L --compressed --connect-timeout 30 --max-time 7200 -o '" + output_path + "' --progress-bar"
        if existing_bytes > 0:
            cmd += " -C -"
        cmd += " '" + url + "'"
        proc = subprocess.Popen(cmd + " 2>&1", shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=1, universal_newlines=True)
        using_aria2 = False
    _direct_procs[dl_id] = proc
    last_traffic_check = time.time()
    last_file_size = existing_bytes
    _last_direct_status_check = time.time()
    for line in proc.stdout:
        line = line.strip()
        # 检查暂停/取消
        _now = time.time()
        if _now - _last_direct_status_check > 0.5:
            _last_direct_status_check = _now
            with _download_lock:
                if dl_id in _downloads:
                    _s = _downloads[dl_id].get('status', '')
                    if _s in ('cancelling', 'paused'):
                        proc.terminate()
                        _direct_procs.pop(dl_id, None)
                        raise Exception('用户取消' if _s == 'cancelling' else '用户暂停')
        # aria2c 输出: [DOWN] .... 0.0%  1.2MiB/s 1.2/200MiB 2m30s
        if using_aria2 and 'GB' in line or 'MiB' in line or 'KiB' in line:
            try:
                import re as _re
                m = _re.search(r'(\d+\.?\d*)%', line)
                if m:
                    pct = float(m.group(1))
                    with _download_lock:
                        if dl_id in _downloads:
                            _downloads[dl_id]['progress'] = min(pct, 99.9)
                # 捕获速度
                m2 = _re.search(r'(\d+\.?\d+)([KMG])iB/s', line)
                if m2:
                    spd = float(m2.group(1))
                    if m2.group(2) == 'K': spd /= 1024
                    elif m2.group(2) == 'G': spd *= 1024
                    with _download_lock:
                        if dl_id in _downloads:
                            _downloads[dl_id]['speed_mbps'] = round(spd, 2)
            except Exception:
                pass
        elif not using_aria2 and '%' in line:
            try:
                pct_str = line.split('%')[0].strip().split()[-1]
                pct = float(pct_str)
                with _download_lock:
                    if dl_id in _downloads:
                        _downloads[dl_id]['progress'] = min(pct, 99.9)
            except Exception:
                pass
        # 每2秒检查文件大小，更新流量
        now = time.time()
        if now - last_traffic_check > 2:
            last_traffic_check = now
            try:
                cur_size = os.path.getsize(output_path) if os.path.exists(output_path) else (os.path.getsize(tmp_path) if os.path.exists(tmp_path) else 0)
                if cur_size > last_file_size:
                    delta = cur_size - last_file_size
                    _dl_traffic_bytes += delta
                    last_file_size = cur_size
            except Exception:
                pass
    proc.wait()
    _direct_procs.pop(dl_id, None)
    if proc.returncode == 0:
        size = os.path.getsize(output_path) if os.path.exists(output_path) else 0
        # 补上最后一次增量
        if size > last_file_size:
            _dl_traffic_bytes += (size - last_file_size)
        with _download_lock:
            if dl_id in _downloads:
                dl2 = _downloads[dl_id]
                dl2['status'] = 'completed'
                dl2['progress'] = 100
                dl2['downloaded_bytes'] = size
                dl2['size_mb'] = round(size / 1048576, 1)
                dl2['completed_at'] = datetime.now().isoformat()
        _save_dl_tasks()
        _add_to_history(dl2)
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        # 验证视频可播放性
        if not _validate_mp4(output_path):
            with _download_lock:
                if dl_id in _downloads:
                    _downloads[dl_id]['status'] = 'failed'
                    _downloads[dl_id]['error'] = '视频文件损坏'
        try:
            _pushplus('下载完成: ' + dl2.get('filename', ''), '<p>文件: ' + dl2.get('filename', '') + '</p><p>大小: ' + str(dl2.get('size_mb', 0)) + ' MB</p>')
        except Exception:
            pass
    else:
        raise Exception('curl 返回码 ' + str(proc.returncode))

def _download_m3u8_with_png_strip(dl, m3u8_url, output_path, referer=''):
    """处理PNG伪装的m3u8分片：跳过PNG头部提取真实视频数据"""
    global _dl_traffic_bytes
    dl_id = dl['id']
    
    # 下载m3u8内容
    m3u8_content = subprocess.check_output(
        ['curl', '-sL', '--max-time', '30'] +
        (['-H', 'Referer: ' + referer] if referer else []) +
        [m3u8_url],
        timeout=35, stderr=subprocess.DEVNULL
    ).decode('utf-8', errors='replace')
    
    # 检查是否master playlist
    sub_playlists = [l.strip() for l in m3u8_content.split(chr(10))
                     if l.strip() and not l.startswith('#') and '.m3u8' in l]
    if sub_playlists:
        best = sub_playlists[-1].strip()
        base_url = m3u8_url.rsplit('/', 1)[0]
        if not best.startswith('http'):
            if best.startswith('/'):
                pu = urlparse(m3u8_url)
                best = pu.scheme + '://' + pu.netloc + best
            else:
                best = base_url + '/' + best
        m3u8_url = best
        m3u8_content = subprocess.check_output(
            ['curl', '-sL', '--max-time', '30'] +
            (['-H', 'Referer: ' + referer] if referer else []) +
            [m3u8_url],
            timeout=35, stderr=subprocess.DEVNULL
        ).decode('utf-8', errors='replace')
    
    # 提取分片URL
    segment_urls = [l.strip() for l in m3u8_content.split(chr(10))
                    if l.strip() and not l.startswith('#')]
    total_segments = len(segment_urls)
    
    if total_segments == 0:
        raise Exception('无分片数据')
    
    _wg_log('[PNG-M3U8] 共 %d 个分片' % total_segments)
    
    with _download_lock:
        if dl_id in _downloads:
            _downloads[dl_id]['total_segments'] = total_segments
    
    # 临时目录
    tmp_dir = '/tmp/m3u8_segments_' + dl_id
    os.makedirs(tmp_dir, exist_ok=True)
    
    # 下载并提取分片
    downloaded_files = []
    for i, seg_url in enumerate(segment_urls):
        # 检查取消/暂停
        with _download_lock:
            if dl_id in _downloads:
                s = _downloads[dl_id].get('status', '')
                if s == 'cancelling':
                    raise Exception('用户取消')
                if s == 'paused':
                    raise Exception('用户暂停')
        
        # 处理相对URL
        if not seg_url.startswith('http'):
            base = m3u8_url.rsplit('/', 1)[0]
            if seg_url.startswith('/'):
                pu = urlparse(m3u8_url)
                seg_url = pu.scheme + '://' + pu.netloc + seg_url
            else:
                seg_url = base + '/' + seg_url
        
        # 下载分片
        raw_file = os.path.join(tmp_dir, 'raw_%05d.ts' % i)
        clean_file = os.path.join(tmp_dir, 'seg_%05d.ts' % i)
        
        try:
            subprocess.run(
                ['curl', '-sL', '--max-time', '30', '--connect-timeout', '10'] +
                (['-H', 'Referer: ' + referer] if referer else []) +
                ['-o', raw_file, seg_url],
                timeout=35, check=True
            )
        except Exception as e:
            _wg_log('[PNG-M3U8] 分片%d下载失败: %s' % (i, str(e)))
            continue
        
        # 检测并跳过PNG头部
        try:
            with open(raw_file, 'rb') as f:
                header = f.read(512)
            
            # 查找FFmpeg标记
            ffmpeg_pos = header.find(b'FFmpeg')
            if ffmpeg_pos > 0:
                # 跳过PNG头部，提取真实TS数据
                with open(raw_file, 'rb') as fin:
                    fin.seek(ffmpeg_pos)
                    data = fin.read()
                with open(clean_file, 'wb') as fout:
                    fout.write(data)
            else:
                # 无PNG伪装，直接使用
                os.rename(raw_file, clean_file)
        except Exception:
            os.rename(raw_file, clean_file)
        
        downloaded_files.append(clean_file)
        
        # 更新进度
        pct = round((i + 1) / total_segments * 95, 1)
        with _download_lock:
            if dl_id in _downloads:
                _downloads[dl_id]['progress'] = pct
                _downloads[dl_id]['downloaded_segments'] = i + 1
                _downloads[dl_id]['status_detail'] = '分片 %d/%d' % (i + 1, total_segments)
        
        # 每下载10个分片释放内存
        if (i + 1) % 10 == 0:
            _dl_traffic_bytes += os.path.getsize(clean_file) if os.path.exists(clean_file) else 0
    
    if not downloaded_files:
        raise Exception('无有效分片')
    
    _wg_log('[PNG-M3U8] 下载完成，%d个有效分片，开始合并' % len(downloaded_files))
    
    # 创建concat列表
    concat_file = os.path.join(tmp_dir, 'concat.txt')
    with open(concat_file, 'w') as f:
        for fp in sorted(downloaded_files):
            f.write("file '%s'\n" % fp)
    
    # 合并分片
    with _download_lock:
        if dl_id in _downloads:
            _downloads[dl_id]['status_detail'] = '合并分片...'
            _downloads[dl_id]['progress'] = 96
    
    try:
        subprocess.run(
            ['ffmpeg', '-y', '-f', 'concat', '-safe', '0', '-i', concat_file,
             '-c', 'copy', output_path],
            timeout=3600, check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    except Exception as e:
        raise Exception('合并失败: %s' % str(e))
    
    # 清理临时文件
    try:
        import shutil
        shutil.rmtree(tmp_dir)
    except Exception:
        pass
    
    # 标记完成
    fsize = os.path.getsize(output_path) if os.path.exists(output_path) else 0
    with _download_lock:
        if dl_id in _downloads and _downloads[dl_id].get('status') == 'downloading':
            _dl = _downloads[dl_id]
            _dl['status'] = 'completed'
            _dl['progress'] = 100
            _dl['downloaded_bytes'] = fsize
            _dl['size_mb'] = round(fsize / 1048576, 1)
            _dl['completed_at'] = datetime.now().isoformat()
            _dl.pop('status_detail', None)
            _save_dl_tasks()
            _add_to_history(_dl)
            try:
                _pushplus('下载完成: ' + _dl.get('filename', ''),
                         '<p>文件: ' + _dl.get('filename', '') + '</p><p>大小: ' + str(round(fsize/1048576, 1)) + ' MB</p>')
            except Exception:
                pass

def _download_m3u8(dl, m3u8_url, output_path, referer=''):
    # 记录开始时的文件大小，用于watchdog判断
    dl['_file_size_at_start'] = os.path.getsize(output_path) if os.path.exists(output_path) else 0
    
    # 检测是否PNG伪装的m3u8（分片返回假PNG图片）
    try:
        _test_m3u8 = subprocess.check_output(
            ['curl', '-sL', '--max-time', '15'] +
            (['-H', 'Referer: ' + referer] if referer else []) +
            [m3u8_url],
            timeout=20, stderr=subprocess.DEVNULL
        ).decode('utf-8', errors='replace')
        _test_segments = [l.strip() for l in _test_m3u8.split(chr(10))
                         if l.strip() and not l.startswith('#')]
        if _test_segments:
            # 下载第一个分片检测
            _test_url = _test_segments[0]
            if not _test_url.startswith('http'):
                _base = m3u8_url.rsplit('/', 1)[0]
                if _test_url.startswith('/'):
                    _pu = urlparse(m3u8_url)
                    _test_url = _pu.scheme + '://' + _pu.netloc + _test_url
                else:
                    _test_url = _base + '/' + _test_url
            
            _test_file = '/tmp/m3u8_png_test.ts'
            subprocess.run(
                ['curl', '-sL', '--max-time', '10', '-o', _test_file, _test_url],
                timeout=15, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            
            if os.path.exists(_test_file):
                with open(_test_file, 'rb') as f:
                    _magic = f.read(8)
                # 检测PNG魔数
                if _magic[:4] == bytes([0x89, 0x50, 0x4e, 0x47]):
                    _wg_log('[PNG-M3U8] 检测到PNG伪装分片，切换手动下载模式')
                    with _download_lock:
                        if dl_id in _downloads:
                            _downloads[dl_id]['status_detail'] = 'PNG伪装模式下载中...'
                    os.remove(_test_file)
                    return _download_m3u8_with_png_strip(dl, m3u8_url, output_path, referer)
                os.remove(_test_file)
    except Exception as _e:
        _wg_log('[PNG-M3U8] 检测异常: %s' % str(_e))

    """用 ffmpeg 直接下载 m3u8，比逐个 curl ts 分片快 10 倍"""
    global _dl_traffic_bytes
    dl_id = dl['id']
    # 先解析 master playlist 拿到最高码率子列表
    m3u8_content = subprocess.check_output(
        ['curl', '-sL', '--max-time', '30'] +
        (['-H', 'Referer: ' + referer] if referer else []) +
        [m3u8_url],
        timeout=35, stderr=subprocess.DEVNULL
    ).decode('utf-8', errors='replace')
    
    # 检查是否 master playlist（含不同清晰度）
    segments = [l.strip() for l in m3u8_content.split('\n') 
                if l.strip() and not l.startswith('#') and '.m3u8' in l]
    if segments:
        best = segments[-1].strip()
        base_url = m3u8_url.rsplit('/', 1)[0]
        if not best.startswith('http'):
            if best.startswith('/'):
                # Absolute path on same domain
                pu = urlparse(m3u8_url)
                best = pu.scheme + '://' + pu.netloc + best
            else:
                best = base_url + '/' + best
        m3u8_url = best
    
    # 探测视频时长
    try:
        probe = subprocess.check_output(
            ['ffprobe', '-v', 'quiet'] +
            (['-headers', 'Referer: ' + referer] if referer else []) +
            ['-show_entries', 'format=duration',
             '-of', 'csv=p=0', m3u8_url],
            timeout=30, stderr=subprocess.DEVNULL
        ).decode().strip()
        duration_secs = float(probe) if probe else 0
    except Exception:
        duration_secs = 0
    
    if duration_secs > 0:
        with _download_lock:
            if dl_id in _downloads:
                _downloads[dl_id]['total_segments'] = int(duration_secs)
                # 从 master playlist 带宽估算总文件大小
                import re as _re_est
                bw_match = _re_est.search(r'BANDWIDTH=(\d+)', m3u8_content)
                if bw_match:
                    bw = int(bw_match.group(1))  # bits per second
                    est_total = int(bw * duration_secs / 8)  # bytes
                    _downloads[dl_id]['total_bytes'] = est_total
    
    # 用 ffmpeg 一把梭
    if dl.get('_resumed'):
        dl['status_detail'] = 'm3u8 重新下载（不支持续传）'
    else:
        dl['status_detail'] = 'ffmpeg下载中...'
    
    unit_name = 'dl-' + dl_id
    enc_args = ['-c', 'copy']  # 默认无损拷贝
    _ffmpeg_err_log = '/opt/monitor/ffmpeg_error.log'
    proc = subprocess.Popen(
        ['ffmpeg', '-y', '-progress', 'pipe:1', '-nostats'] +
        (['-headers', 'Referer: ' + referer] if referer else []) +
        ['-reconnect', '1', '-reconnect_streamed', '1', '-reconnect_delay_max', '5',
         '-allowed_extensions', 'ALL', '-allowed_segment_extensions', 'ALL', '-i', m3u8_url] + enc_args +
        ['-loglevel', 'error', output_path],
        stdout=subprocess.PIPE,
        stderr=open(_ffmpeg_err_log, 'a'),
        universal_newlines=True
    )
    _ffmpeg_procs[dl_id] = (proc, None)
    import select as _sel
    progress_counter = 0
    last_status_check = time.time()
    last_size_check = time.time()
    last_progress_time = time.time()
    _last_m3u8_size = 0
    last_pct = 0
    last_pct_change_time = time.time()
    _raw_fd = proc.stdout.fileno()
    os.set_blocking(_raw_fd, False)
    _partial = b''
    ffmpeg_exited = False
    try:
        while True:
            if not ffmpeg_exited:
                ret = proc.poll()
                if ret is not None:
                    _wg_log('[DL-SELECT] ffmpeg EXITED ret=%s dl_id=%s' % (str(ret), dl_id))
                    ffmpeg_exited = True
            # select with short timeout; if ffmpeg already exited, just try reading
            if ffmpeg_exited:
                ready = [_raw_fd]
            else:
                ready, _, _ = _sel.select([_raw_fd], [], [], 0.5)
            if ready:
                try:
                    chunk = os.read(_raw_fd, 65536)
                except (BlockingIOError, OSError) as _rderr:
                    _wg_log('[DL-SELECT] os.read err=%s dl_id=%s' % (str(_rderr), dl_id))
                    chunk = b''
                if not chunk:
                    _wg_log('[DL-SELECT] EOF chunk dl_id=%s partial_len=%d' % (dl_id, len(_partial)))
                    # EOF: flush partial line if any
                    if _partial:
                        _line = _partial.decode('utf-8', errors='replace').strip()
                        if _line.startswith('out_time='):
                            try:
                                t = _line.split('=')[1]
                                parts = t.split(':')
                                out_secs = float(parts[0])*3600 + float(parts[1])*60 + float(parts[2])
                                if duration_secs > 0:
                                    last_pct = min(round(out_secs / duration_secs * 100, 1), 99.9)
                            except Exception:
                                pass
                    break
                _partial += chunk
                while b'\n' in _partial:
                    raw_line, _partial = _partial.split(b'\n', 1)
                    line = raw_line.decode('utf-8', errors='replace').strip()
                    if line.startswith('out_time='):
                        try:
                            t = line.split('=')[1]
                            parts = t.split(':')
                            out_secs = float(parts[0])*3600 + float(parts[1])*60 + float(parts[2])
                            progress_counter += 1
                            if duration_secs > 0:
                                pct = min(round(out_secs / duration_secs * 100, 1), 99.9)
                            else:
                                pct = 0
                            if pct != last_pct:
                                last_pct = pct
                                last_pct_change_time = time.time()
                                last_progress_time = time.time()
                            if progress_counter % 10 == 0:
                                with _download_lock:
                                    if dl_id in _downloads:
                                        _downloads[dl_id]['progress'] = pct
                                        _downloads[dl_id]['downloaded_segments'] = int(out_secs)
                                if progress_counter % 200 == 0:
                                    try:
                                        _save_dl_tasks()
                                    except Exception:
                                        pass
                        except Exception:
                            pass
                    elif line.startswith('total_size='):
                        try:
                            ts = int(line.split('=')[1])
                            if ts > 0:
                                with _download_lock:
                                    if dl_id in _downloads:
                                        _downloads[dl_id]['downloaded_bytes'] = ts
                        except Exception:
                            pass
            # 检查取消/暂停
            now = time.time()
            if now - last_status_check > 0.5:
                last_status_check = now
                with _download_lock:
                    if dl_id in _downloads:
                        s = _downloads[dl_id].get('status', '')
                        if s == 'cancelling':
                            _kill_ffmpeg(dl_id)
                            raise Exception('用户取消')
                        if s == 'paused':
                            _kill_ffmpeg(dl_id)
                            raise Exception('用户暂停')
                        if last_pct >= 95 and now - last_pct_change_time > 5:
                            if _downloads[dl_id].get('status_detail') == 'ffmpeg下载中...' or _downloads[dl_id].get('status_detail', '').startswith('m3u8'):
                                _downloads[dl_id]['status_detail'] = '正在优化文件...'
            # 实时更新流量
            if now - last_size_check > 2:
                last_size_check = now
                try:
                    cur_size = os.path.getsize(output_path) if os.path.exists(output_path) else 0
                    if cur_size > _last_m3u8_size:
                        delta = cur_size - _last_m3u8_size
                        _dl_traffic_bytes += delta
                        _last_m3u8_size = cur_size
                except Exception:
                    pass
            # 防止假死: 无进度超过5分钟就kill
            if now - last_progress_time > 300:
                _wg_log('ffmpeg no progress for 300s, killing %s' % dl_id)
                _kill_ffmpeg(dl_id)
                raise Exception('ffmpeg无进度超过5分钟')
    finally:
        _wg_log('[DL-SELECT] FINALLY dl_id=%s rc=%s' % (dl_id, str(proc.returncode)))
        try:
            proc.wait(timeout=30)
        except Exception:
            _wg_log('proc.wait TIMEOUT, killing')
            try:
                proc.kill()
            except Exception:
                pass
            proc.wait()
        _ffmpeg_procs.pop(dl_id, None)
    stderr_out = ''
    try:
        with open(_ffmpeg_err_log, 'r') as _f:
            _f.seek(0, 2)
            # 只读最后 4KB
            _sz = _f.tell()
            _f.seek(max(0, _sz - 4096))
            stderr_out = _f.read()
    except Exception:
        pass    
    # 兜底：不管returncode，文件够大就算完成
    try:
        _fsize = os.path.getsize(output_path) if os.path.exists(output_path) else 0
    except Exception:
        _fsize = 0
    _wg_log('[DL-COMP] checking fsize=%d dl_id=%s rc=%s' % (_fsize, dl_id, str(proc.returncode)))
    if _fsize > 1048576:  # > 1MB
        with _download_lock:
            if dl_id in _downloads and _downloads[dl_id].get("status") == "downloading":
                _dl = _downloads[dl_id]
                _wg_log('[DL-COMP] MARKING COMPLETE dl_id=%s' % dl_id)
                _dl["status"] = "completed"
                _dl["progress"] = 100
                _dl["downloaded_bytes"] = _fsize
                _dl["size_mb"] = round(_fsize / 1048576, 1)
                _dl["completed_at"] = datetime.now().isoformat()
                _dl.pop("status_detail", None)
                _save_dl_tasks()
                _add_to_history(_dl)
                try:
                    _pushplus("下载完成: " + _dl.get("filename", ""), "<p>文件: " + _dl.get("filename", "") + "</p><p>大小: " + str(round(_fsize/1048576, 1)) + " MB</p>")
                except Exception:
                    pass
        if dl_id in _downloads and _downloads[dl_id].get("status") == "completed":
            _active_download = None
            return
    
    if proc.returncode != 0:
        # 如果是编码/容器不兼容（如 PNG keyframe m3u8），自动重试转码
        need_reencode = any(kw in (stderr_out or '') for kw in [
            'does not contain any stream',
            'not in allowed_segment_extensions',
            'Could not find codec parameters',
            'Invalid data found when processing input',
            'Video: png'
        ])
        if need_reencode and enc_args == ['-c', 'copy']:
            try:
                with open('/opt/monitor/ffmpeg_error.log', 'a') as _f:
                    _f.write('[' + datetime.now().isoformat() + '] dl_id=' + str(dl_id) + ' (will retry with re-encode)\n')
                    _f.write('URL: ' + m3u8_url + '\n')
                    _f.write('stderr(' + str(len(stderr_out)) + '): ' + stderr_out + '\n')
                    _f.write('returncode: ' + str(proc.returncode) + '\n\n')
            except Exception:
                pass
            try:
                if os.path.exists(output_path):
                    os.remove(output_path)
            except Exception:
                pass
            # 重新下载，用 libx264 转码
            with _download_lock:
                if dl_id in _downloads:
                    _downloads[dl_id]['status_detail'] = '转码中...'
                    _downloads[dl_id]['progress'] = 0
            _last_m3u8_size = 0
            unit_name2 = unit_name + '_r'
            _ffmpeg_err_log2 = '/opt/monitor/ffmpeg_error.log'
            enc_args = ['-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '28', '-c:a', 'copy']
            proc = subprocess.Popen(
                ['ffmpeg', '-y', '-progress', 'pipe:1', '-nostats'] +
                (['-headers', 'Referer: ' + referer] if referer else []) +
                ['-reconnect', '1', '-reconnect_streamed', '1', '-reconnect_delay_max', '5',
                 '-allowed_extensions', 'ALL', '-allowed_segment_extensions', 'ALL', '-i', m3u8_url] + enc_args +
                ['-loglevel', 'error', output_path],
                stdout=subprocess.PIPE,
                stderr=open(_ffmpeg_err_log2, 'a'),
                universal_newlines=True
            )
            _ffmpeg_procs[dl_id] = (proc, None)
            import select as _sel2
            progress_counter = 0
            last_status_check = time.time()
            last_size_check = time.time()
            last_progress_time = time.time()
            _last_m3u8_size = 0
            last_pct = 0
            last_pct_change_time = time.time()
            _raw_fd2 = proc.stdout.fileno()
            os.set_blocking(_raw_fd2, False)
            _partial2 = b''
            ffmpeg_exited2 = False
            try:
                while True:
                    if not ffmpeg_exited2:
                        ret = proc.poll()
                        if ret is not None:
                            _wg_log('[DL-RETRY] ffmpeg EXITED ret=%s dl_id=%s' % (str(ret), dl_id))
                            ffmpeg_exited2 = True
                    if ffmpeg_exited2:
                        ready2 = [_raw_fd2]
                    else:
                        ready2, _, _ = _sel2.select([_raw_fd2], [], [], 0.5)
                    if ready2:
                        try:
                            chunk2 = os.read(_raw_fd2, 65536)
                        except (BlockingIOError, OSError) as _rderr2:
                            _wg_log('[DL-RETRY] os.read err=%s dl_id=%s' % (str(_rderr2), dl_id))
                            chunk2 = b''
                        if not chunk2:
                            _wg_log('[DL-RETRY] EOF chunk dl_id=%s partial_len=%d' % (dl_id, len(_partial2)))
                            if _partial2:
                                _line2 = _partial2.decode('utf-8', errors='replace').strip()
                                if _line2.startswith('out_time='):
                                    try:
                                        t = _line2.split('=')[1]
                                        parts = t.split(':')
                                        out_secs = float(parts[0])*3600 + float(parts[1])*60 + float(parts[2])
                                        if duration_secs > 0:
                                            last_pct = min(round(out_secs / duration_secs * 100, 1), 99.9)
                                    except Exception:
                                        pass
                            break
                        _partial2 += chunk2
                        while b'\n' in _partial2:
                            raw_line2, _partial2 = _partial2.split(b'\n', 1)
                            line = raw_line2.decode('utf-8', errors='replace').strip()
                            if line.startswith('out_time='):
                                try:
                                    t = line.split('=')[1]
                                    parts = t.split(':')
                                    out_secs = float(parts[0])*3600 + float(parts[1])*60 + float(parts[2])
                                    if duration_secs > 0:
                                        pct = min(round(out_secs / duration_secs * 100, 1), 99.9)
                                        if pct != last_pct:
                                            last_pct = pct
                                            last_pct_change_time = time.time()
                                            last_progress_time = time.time()
                                        progress_counter += 1
                                        if progress_counter % 10 == 0:
                                            with _download_lock:
                                                if dl_id in _downloads:
                                                    _downloads[dl_id]['progress'] = pct
                                                    _downloads[dl_id]['downloaded_segments'] = int(out_secs)
                                except Exception:
                                    pass
                    now = time.time()
                    if now - last_status_check > 0.5:
                        last_status_check = now
                        with _download_lock:
                            if dl_id in _downloads:
                                s = _downloads[dl_id].get('status', '')
                                if s == 'cancelling':
                                    _kill_ffmpeg(dl_id)
                                    raise Exception('用户取消')
                                if s == 'paused':
                                    _kill_ffmpeg(dl_id)
                                    raise Exception('用户暂停')
                                if last_pct >= 95 and now - last_pct_change_time > 5:
                                    if _downloads[dl_id].get('status_detail') in ('ffmpeg下载中...', '转码中...') or _downloads[dl_id].get('status_detail', '').startswith('m3u8'):
                                        _downloads[dl_id]['status_detail'] = '正在优化文件...'
                    if now - last_size_check > 2:
                        last_size_check = now
                        try:
                            cur_size = os.path.getsize(output_path) if os.path.exists(output_path) else 0
                            if cur_size > _last_m3u8_size:
                                delta = cur_size - _last_m3u8_size
                                _dl_traffic_bytes += delta
                                _last_m3u8_size = cur_size
                        except Exception:
                            pass
                    if now - last_progress_time > 300:
                        _wg_log('ffmpeg retry no progress for 300s, killing %s' % dl_id)
                        _kill_ffmpeg(dl_id)
                        raise Exception('ffmpeg无进度超过5分钟')
            finally:
                try:
                    proc.wait(timeout=30)
                except Exception:
                    _wg_log('proc.wait2 TIMEOUT, killing')
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    proc.wait()
                _ffmpeg_procs.pop(dl_id, None)
            stderr_out = ''
            try:
                with open(_ffmpeg_err_log2, 'r') as _f:
                    _f.seek(0, 2)
                    _sz = _f.tell()
                    _f.seek(max(0, _sz - 4096))
                    stderr_out = _f.read()
            except Exception:
                pass

    if proc.returncode != 0:
        try:
            with open('/opt/monitor/ffmpeg_error.log', 'a') as _f:
                _f.write('[' + datetime.now().isoformat() + '] dl_id=' + str(dl_id) + '\n')
                _f.write('URL: ' + m3u8_url + '\n')
                _f.write('stderr(' + str(len(stderr_out)) + '): ' + stderr_out + '\n')
                _f.write('returncode: ' + str(proc.returncode) + '\n\n')
        except Exception:
            pass
        # 清理失败临时文件
        try:
            if os.path.exists(output_path):
                os.remove(output_path)
        except Exception:
            pass
        # 过滤 systemd-run 的 "Running as unit:" 等噪音，取实际 ffmpeg 错误
        clean_err = '\n'.join(line for line in stderr_out.split('\n')
                              if not line.startswith('Running as unit:')
                              and not line.startswith('Finished with result:')).strip()
        raise Exception('ffmpeg 失败: ' + (clean_err[:500] if clean_err else '返回码 ' + str(proc.returncode)))
    
    # 文件已由 ffmpeg 直接写入最终路径
    size = os.path.getsize(output_path) if os.path.exists(output_path) else 0
    if size > _last_m3u8_size:
        _dl_traffic_bytes += (size - _last_m3u8_size)
    with _download_lock:
        if dl_id in _downloads:
            dl2 = _downloads[dl_id]
            dl2['status'] = 'completed'
            dl2['progress'] = 100
            dl2['downloaded_bytes'] = size
            dl2['size_mb'] = round(size / 1048576, 1)
            dl2['completed_at'] = datetime.now().isoformat()
            _save_dl_tasks()
            dl2.pop('status_detail', None)
            dl2.pop('total_segments', None)
            dl2.pop('downloaded_segments', None)
            dl2.pop('_speed_sample', None)  # clean up speed tracking
            # 验证视频可播放性
            if not _validate_mp4(output_path):
                dl2['status'] = 'failed'
                dl2['error'] = '视频文件损坏(moov丢失)'
            _add_to_history(dl2)
    
    try:
        if dl2.get('status') == 'failed':
            _pushplus('下载失败: ' + dl2.get('filename', ''),
                       '<p>文件: ' + dl2.get('filename', '') + '</p><p>错误: ' + dl2.get('error', '未知') + '</p>')
        else:
            _pushplus('下载完成: ' + dl2.get('filename', ''),
                       '<p>文件: ' + dl2.get('filename', '') + '</p><p>大小: ' + str(dl2.get('size_mb', 0)) + ' MB</p>')
    except Exception:
        pass

def _periodic_save():
    """定期保存下载状态到磁盘，不依赖ffmpeg输出"""
    import time as _t
    while True:
        _t.sleep(10)
        try:
            with _download_lock:
                has_active = any(dl.get('status') == 'downloading' for dl in _downloads.values())
            if has_active:
                _save_dl_tasks()
        except Exception:
            pass


def _wg_log(msg):
    with open("/opt/monitor/watchdog_debug.log", "a") as _f:
        _f.write("[%s] %s\n" % (datetime.now().strftime("%H:%M:%S"), msg))

def _download_watchdog():
    """看门狗：检测下载完成但状态未更新的情况"""
    import time as _t
    while True:
        _t.sleep(5)
        try:
            with _download_lock:
                snapshot = list(_downloads.items())
            _wg_log("tick: %d tasks, procs=%d" % (len(snapshot), len(_ffmpeg_procs)))
            for dl_id, dl in snapshot:
                if dl.get("status") != "downloading":
                    continue
                _wg_log("checking %s prog=%s" % (dl_id, dl.get("progress")))
                # Check both _ffmpeg_procs and _direct_procs (aria2c/curl)
                proc_alive = False
                
                # First check _ffmpeg_procs
                proc_info = _ffmpeg_procs.get(dl_id)
                if proc_info:
                    proc, _ = proc_info
                    poll_result = proc.poll()
                    if poll_result is None:
                        try:
                            os.kill(proc.pid, 0)
                            proc_alive = True
                            _wg_log("ffmpeg alive pid=%d for %s" % (proc.pid, dl_id))
                        except OSError:
                            proc_alive = False
                            _wg_log("ffmpeg PID %d dead for %s" % (proc.pid, dl_id))
                    else:
                        _wg_log("ffmpeg exited code=%s for %s" % (poll_result, dl_id))
                
                # Then check _direct_procs (aria2c/curl) if ffmpeg not found
                if not proc_alive:
                    direct_proc = _direct_procs.get(dl_id)
                    if direct_proc:
                        poll_result = direct_proc.poll()
                        if poll_result is None:
                            try:
                                os.kill(direct_proc.pid, 0)
                                proc_alive = True
                                _wg_log("direct alive pid=%d for %s" % (direct_proc.pid, dl_id))
                            except OSError:
                                proc_alive = False
                                _wg_log("direct PID %d dead for %s" % (direct_proc.pid, dl_id))
                        else:
                            _wg_log("direct exited code=%s for %s" % (poll_result, dl_id))
                    else:
                        _wg_log("no proc for %s" % dl_id)
                if proc_alive:
                    continue
                # 进程不存在：只有明确正常退出(returncode=0)才标完成
                # 避免resume后进程还没启动就被误判
                proc_returncode = None
                if proc_info:
                    proc_returncode = proc_info[0].poll()
                if direct_proc:
                    proc_returncode = direct_proc.poll()
                # 只有进程正常退出(0)或确定不存在(不在任何字典里)才检查
                if proc_returncode is not None and proc_returncode != 0:
                    # 进程异常退出，标失败
                    with _download_lock:
                        if dl_id in _downloads:
                            _dl = _downloads[dl_id]
                            if _dl.get("status") == "downloading":
                                _dl["status"] = "failed"
                                _dl["error"] = "进程异常退出 code=" + str(proc_returncode)
                                _dl["completed_at"] = datetime.now().isoformat()
                                _save_dl_tasks()
                                _wg_log("MARKED FAILED %s code=%s" % (dl_id, proc_returncode))
                    continue
                # 进程正常退出或不存在，检查是否刚启动(resume场景)
                out_path = dl.get("output_path", "")
                if out_path and os.path.exists(out_path):
                    fsize = os.path.getsize(out_path)
                    _wg_log("file exists: %s size=%d" % (out_path, fsize))
                    # 检查文件大小是否比开始时增长了
                    _started = dl.get("_file_size_at_start", 0)
                    if fsize > _started and fsize > 1024:
                        with _download_lock:
                            if dl_id in _downloads:
                                _dl = _downloads[dl_id]
                                if _dl.get("status") == "downloading":
                                    _dl["status"] = "completed"
                                    _dl["progress"] = 100
                                    _dl["downloaded_bytes"] = fsize
                                    _dl["size_mb"] = round(fsize / 1048576, 1)
                                    _dl["completed_at"] = datetime.now().isoformat()
                                    _dl.pop("status_detail", None)
                                    _save_dl_tasks()
                                    _add_to_history(_dl)
                                    _wg_log("MARKED COMPLETE %s size=%dMB" % (dl_id, round(fsize/1048576)))
                                    try:
                                        _pushplus("下载完成(看门狗): " + _dl.get("filename", ""),
                                                   "<p>文件: " + _dl.get("filename", "") + "</p><p>大小: " + str(_dl.get("size_mb", 0)) + " MB</p>")
                                    except Exception:
                                        pass
        except Exception as e:
            _wg_log("EXCEPTION: %s" % e)

def _process_queue():
    global _active_download
    with _download_lock:
        if _active_download is not None:
            return
        if not _download_queue:
            return
        next_id = _download_queue.pop(0)
        if next_id not in _downloads:
            return
    def _safe_run(did=next_id):
        try:
            _run_download(did)
        except Exception as _e:
            import traceback
            try:
                with open('/opt/monitor/dl_error.log', 'a') as _ef:
                    _ef.write('[' + datetime.now().isoformat() + '] THREAD CRASH dl_id=' + str(did) + '\n')
                    _ef.write(traceback.format_exc() + '\n\n')
            except Exception:
                pass
            try:
                with _download_lock:
                    if did in _downloads and _downloads[did].get('status') == 'downloading':
                        _out = _downloads[did].get('output_path', '')
                        if _out and os.path.exists(_out) and os.path.getsize(_out) > 1048576:
                            _fsize = os.path.getsize(_out)
                            _downloads[did]['status'] = 'completed'
                            _downloads[did]['progress'] = 100
                            _downloads[did]['downloaded_bytes'] = _fsize
                            _downloads[did]['size_mb'] = round(_fsize / 1048576, 1)
                            _downloads[did]['completed_at'] = datetime.now().isoformat()
                            _save_dl_tasks()
                            _add_to_history(_downloads[did])
                        else:
                            _downloads[did]['status'] = 'failed'
                            _downloads[did]['error'] = '线程崩溃: ' + str(_e)[:200]
                            _save_dl_tasks()
            except Exception:
                pass
            global _active_download
            if _active_download == did:
                _active_download = None
            _process_queue()
    t = threading.Thread(target=_safe_run, daemon=True)
    t.start()

def _start_download(url, folder='/data/share/视频', filename='', referer=''):
    dl_id = 'dl_' + str(int(time.time() * 1000)) + '_' + str(_dl_counter[0])
    _dl_counter[0] += 1
    dl = {
        'id': dl_id,
        'url': url,
        'folder': folder,
        'filename': filename,
        'status': 'queued',
        'progress': 0,
        'downloaded_bytes': 0,
        'created_at': datetime.now().isoformat(),
        'is_m3u8': url.endswith('.m3u8'),
        'referer': referer
    }
    with _download_lock:
        _downloads[dl_id] = dl
        _download_queue.append(dl_id)
    _save_dl_tasks()
    threading.Thread(target=_process_queue, daemon=True).start()
    return dl_id

def _handle_download_post(handler, data):
    global _active_download, _download_queue, _ffmpeg_procs
    path = urlparse(handler.path).path
    token = handler._get_token(data=data)
    if not verify_session(token):
        handler.send_json({'error': 'unauthorized'}, 401)
        return True
    if path == '/api/download':
        url = data.get('url', '').strip()
        folder = data.get('folder', '/data/share/视频').strip()
        filename = data.get('filename', '').strip()
        referer = data.get('referer', '').strip()
        if not url:
            handler.send_json({'error': '缺少链接'}, 400)
            return True
        dl_id = _start_download(url, folder, filename, referer)
        handler.send_json({'id': dl_id, 'status': 'queued'})
        return True
    elif path == '/api/download/batch':
        urls = data.get('urls', [])
        folder = data.get('folder', '/data/share/视频').strip()
        if not urls:
            handler.send_json({'error': '缺少链接'}, 400)
            return True
        ids = []
        for url in urls:
            url = url.strip()
            if url:
                dl_id = _start_download(url, folder, referer=data.get('referer',''))
                ids.append(dl_id)
        handler.send_json({'ids': ids, 'count': len(ids)})
        return True
    elif path == '/api/download/cancel':
        dl_id = data.get('id', '')
        with _download_lock:
            if dl_id in _downloads:
                dl = _downloads[dl_id]
                if dl['status'] in ('queued', 'downloading'):
                    dl['status'] = 'cancelling'
                    if dl_id in _download_queue:
                        _download_queue.remove(dl_id)
                    if _active_download == dl_id:
                        _active_download = None
                elif dl['status'] == 'paused':
                    dl['status'] = 'cancelled'
                    dl['completed_at'] = datetime.now().isoformat()
                else:
                    dl['status'] = 'cancelled'
                    dl['completed_at'] = datetime.now().isoformat()
                if dl_id in _download_queue:
                    _download_queue.remove(dl_id)
            # 清理ffmpeg进程
            _kill_ffmpeg(dl_id)
            if dl_id in _direct_procs:
                try:
                    _direct_procs[dl_id].terminate()
                except Exception:
                    pass
                _direct_procs.pop(dl_id, None)
            # 清理残留的部分文件
            if dl_id in _downloads:
                partial_paths = []
                out_path = _downloads[dl_id].get('output_path', '')
                if out_path:
                    partial_paths.append(out_path)          # m3u8 的输出文件
                    partial_paths.append(out_path + '.part') # direct 的 .part
                    partial_paths.append(out_path + '.tmp')  # m3u8 的临时输出
                for pp in partial_paths:
                    if pp and os.path.exists(pp):
                        try:
                            os.remove(pp)
                        except Exception:
                            pass
        handler.send_json({'ok': True})
        return True
    elif path == '/api/download/remove':
        dl_id = data.get('id', '')
        # 先杀进程
        _kill_ffmpeg(dl_id)
        if dl_id in _direct_procs:
            try:
                _direct_procs[dl_id].terminate()
            except Exception:
                pass
            _direct_procs.pop(dl_id, None)
        # 清理残留文件（只删 .part/.tmp 未完成碎片，不删已完成的文件）
        with _download_lock:
            if dl_id in _downloads:
                dl_tmp = _downloads[dl_id]
                out_path = dl_tmp.get('output_path', '')
                dl_status = dl_tmp.get('status', '')
                for ext in ('.part', '.tmp'):
                    pp = out_path + ext
                    if pp and os.path.exists(pp):
                        try:
                            os.remove(pp)
                        except Exception:
                            pass
        # 从内存中删除
        with _download_lock:
            if dl_id in _downloads:
                del _downloads[dl_id]
            if dl_id in _download_queue:
                _download_queue.remove(dl_id)
            if _active_download == dl_id:
                _active_download = None
        handler.send_json({'ok': True})
        return True
    elif path == '/api/downloads/clear-completed':
        # 清掉所有已完成/失败/取消的任务（不删文件）
        with _download_lock:
            to_remove = [did for did, dl in _downloads.items() if dl.get('status', '') in ('completed', 'failed', 'cancelled')]
            for did in to_remove:
                out_path = _downloads[did].get('output_path', '')
                for ext in ('.part', '.tmp'):
                    pp = out_path + ext
                    if pp and os.path.exists(pp):
                        try: os.remove(pp)
                        except: pass
                if did in _downloads:
                    del _downloads[did]
                if did in _download_queue:
                    _download_queue.remove(did)
                if _active_download == did:
                    _active_download = None
        _save_dl_tasks()
        handler.send_json({'ok': True, 'removed': len(to_remove)})
        return True
    elif path == '/api/download/history/clear':
        try:
            if os.path.exists(_DOWNLOAD_HISTORY_FILE):
                os.remove(_DOWNLOAD_HISTORY_FILE)
        except Exception:
            pass
        handler.send_json({'ok': True})
        return True
    elif path == '/api/browse':
        browse_path = data.get('path', '/data/share')
        tree = _get_browse_tree(browse_path)
        handler.send_json(tree)
        return True
    elif path == '/api/download/pause':
        dl_id = data.get('id', '')
        need_process = False
        with _download_lock:
            if dl_id in _downloads:
                dl = _downloads[dl_id]
                if dl['status'] == 'downloading':
                    dl['status'] = 'paused'
                    if _active_download == dl_id:
                        _active_download = None
                        need_process = True
                elif dl['status'] == 'queued':
                    dl['status'] = 'paused'
                    if dl_id in _download_queue:
                        _download_queue.remove(dl_id)
        # 直接杀进程（systemd-run 包装的，用 systemctl stop）
        _kill_ffmpeg(dl_id)
        if dl_id in _direct_procs:
            try:
                _direct_procs[dl_id].terminate()
            except Exception:
                pass
            _direct_procs.pop(dl_id, None)
        if need_process:
            threading.Thread(target=_process_queue, daemon=True).start()
        _save_dl_tasks()
        handler.send_json({'ok': True})
        return True
    elif path == '/api/download/resume':
        dl_id = data.get('id', '')
        with _download_lock:
            if dl_id in _downloads and _downloads[dl_id]['status'] == 'paused':
                dl = _downloads[dl_id]
                dl['status'] = 'queued'
                dl.pop('_speed_sample', None)
                is_m3u8 = dl.get('is_m3u8') or dl.get('url', '').endswith('.m3u8')
                if is_m3u8:
                    # m3u8 不支持断点续传，ffmpeg -y 会覆盖，必须重置
                    dl['_resumed'] = True
                    dl['status_detail'] = 'm3u8 重新下载中...'
                    dl['progress'] = 0
                    dl['downloaded_bytes'] = 0
                else:
                    # 直接下载支持 curl -C - 断点续传，不重置进度
                    dl['status_detail'] = '续传中...'
                _download_queue.insert(0, dl_id)
                threading.Thread(target=_process_queue, daemon=True).start()
        _save_dl_tasks()
        handler.send_json({'ok': True})
        return True
    elif path == '/api/download/rename':
        dl_id = data.get('id', '')
        new_name = data.get('name', '').strip()
        if not new_name:
            handler.send_json({'error': '名称不能为空'}, 400)
            return True
        with _download_lock:
            if dl_id not in _downloads:
                handler.send_json({'error': '下载不存在'}, 404)
                return True
            dl = _downloads[dl_id]
            old_path = dl.get('output_path', '')
        if old_path and os.path.exists(old_path):
            new_path = os.path.join(os.path.dirname(old_path), new_name)
            if os.path.exists(new_path):
                handler.send_json({'error': '文件名已存在'}, 409)
                return True
            try:
                os.rename(old_path, new_path)
                with _download_lock:
                    if dl_id in _downloads:
                        _downloads[dl_id]['output_path'] = new_path
                        _downloads[dl_id]['filename'] = new_name
            except Exception as e:
                handler.send_json({'error': str(e)}, 500)
                return True
        else:
            # 还没下载完，只改计划文件名
            with _download_lock:
                if dl_id in _downloads:
                    _downloads[dl_id]['filename'] = new_name
        # 更新历史记录
        try:
            history = _load_dl_history()
            for h in history:
                if h.get('id') == dl_id:
                    h['filename'] = new_name
                    with open(_DOWNLOAD_HISTORY_FILE, 'w') as f:
                        json.dump(history, f, ensure_ascii=False)
                    break
        except Exception:
            pass
        handler.send_json({'ok': True, 'new_name': new_name})
        return True
    return False

def _handle_download_get(handler, params):
    path = urlparse(handler.path).path
    token = handler._get_token(params=params)
    if not token:
        handler.send_json({'error': 'unauthorized'}, 401)
        return True
    if path == '/api/downloads':
        downloads = _get_downloads_status()
        handler.send_json({'downloads': downloads})
        return True
    elif path.startswith('/api/download/') and '/progress' in path:
        dl_id = path.split('/api/download/')[1].split('/')[0]
        with _download_lock:
            dl = _downloads.get(dl_id, {})
        handler.send_json(dict(dl))
        return True
    elif path == '/api/download/history':
        history = _load_dl_history()
        handler.send_json({'history': history})
        return True
    elif path == '/api/browse':
        browse_path = params.get('path', ['/data/share'])[0]
        tree = _get_browse_tree(browse_path)
        handler.send_json(tree)
        return True
    return False

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
def _parse_url_for_videos(page_url):
    """解析网页链接，提取可下载的视频源。
    先尝试 yt-dlp，失败则回退到 HTML 正则提取。
    返回: [{'url':..., 'ext':..., 'filesize':..., 'resolution':..., 'note':...}, ...]
    """
    version = sys.version_info
    used_ytdlp = False
    sources = []
    # 方法1: yt-dlp
    try:
        result = subprocess.run(
            ['yt-dlp', '--dump-json', '--no-playlist', '--no-warnings', '-s', page_url],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0 and result.stdout.strip():
            used_ytdlp = True
            lines = result.stdout.strip().split('\n')
            for line in lines:
                try:
                    info = json.loads(line)
                    # 提取 formats
                    fmts = info.get('formats', [info])  # 有时单条也有
                    for f in fmts:
                        url = f.get('url') or f.get('manifest_url') or f.get('fragment_base_url', '')
                        if not url:
                            continue
                        ext = f.get('ext', '')
                        size = f.get('filesize') or f.get('filesize_approx', 0)
                        res = ''
                        h = f.get('height', 0) or 0
                        w = f.get('width', 0) or 0
                        if h > 0:
                            res = str(h) + 'p'
                            if w > 0:
                                res = str(w) + 'x' + str(h)
                        note = f.get('format_note', '') or f.get('format', '') or ext
                        if note == ext and res:
                            note = res + ' ' + ext
                        if ext in ('m3u8', 'mp4', 'webm', 'mkv', 'avi', 'mov', 'flv') or '.m3u8' in url:
                            sources.append({
                                'url': url,
                                'ext': ext if ext else 'mp4',
                                'filesize': size,
                                'resolution': res,
                                'note': note,
                                'from': 'yt-dlp'
                            })
                except Exception:
                    pass
            # 如果 formats 很多，去重+按质量排序
            seen = set()
            deduped = []
            for s in sources:
                if s['url'] not in seen:
                    seen.add(s['url'])
                    deduped.append(s)
            # 按分辨率降序，filesize 降序
            def sort_key(s):
                h = 0
                try:
                    h = int(s['resolution'].replace('p','').split('x')[-1])
                except: pass
                return (-h, -(s['filesize'] or 0))
            deduped.sort(key=sort_key)
            sources = deduped
    except Exception:
        pass
    
    if not sources:
        # 方法2: HTML 正则提取
        try:
            result = subprocess.run(
                ['curl', '-sL', '--max-time', '15', '-A',
                 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                 page_url],
                capture_output=True, text=True, timeout=20
            )
            html = result.stdout
            found = set()
            # 查找 JSON 中的 url 字段 (常见于 maccms 建站)
            import re
            # "url":"https://..."
            for m in re.finditer(r'"[uU][rR][lL]"\s*:\s*"((https?://[^"]+\.(m3u8|mp4)[^"]*))"', html):
                u = m.group(1).replace('\\/', '/').replace('\/', '/')
                if u not in found:
                    found.add(u)
                    sources.append({'url': u, 'ext': m.group(2), 'filesize': 0, 'resolution': '', 'note': m.group(2), 'from': 'html'})
            # data-url / src
            for m in re.finditer(r"(?:data-url|src)\s*=\s*[\"'\u0027]((https?://[^\"'\u0027]+\.(m3u8|mp4)[^\"'\u0027]*))[\"'\u0027]", html):
                u = m.group(1).replace('\\/', '/').replace('\/', '/')
                if u not in found:
                    found.add(u)
                    sources.append({'url': u, 'ext': m.group(2), 'filesize': 0, 'resolution': '', 'note': m.group(2), 'from': 'html'})
            # 通用 m3u8/mp4 URL
            for m in re.finditer(r"""(https?://[^"'<> ]+\.m3u8[^"'<> ]*)""", html):
                u = m.group(0)
                if u not in found:
                    found.add(u)
                    sources.append({'url': u, 'ext': 'm3u8', 'filesize': 0, 'resolution': '', 'note': 'm3u8', 'from': 'html'})
            # 尝试探测大小 (HEAD)
            for s in sources:
                if s['filesize'] == 0 and s['from'] == 'html':
                    try:
                        hr = subprocess.run(['curl', '-sI', '-L', '--max-time', '8', s['url']],
                                            capture_output=True, text=True, timeout=10)
                        for hl in hr.stdout.split('\n'):
                            if 'content-length:' in hl.lower():
                                s['filesize'] = int(hl.split(':')[1].strip())
                                break
                    except Exception:
                        pass
        except Exception:
            pass
    
    if not sources:
        # 方法2.5: maccms / 通用 player_data 提取
        try:
            # 匹配 var player_data = {...}; 或 player_data={...};
            pd_match = re.search(r'player_data\s*=\s*(\{[^;]+\})', html)
            if pd_match:
                pd_str = pd_match.group(1)
                # 修复 \/ 为 /
                pd_str = pd_str.replace('\\/', '/')
                pd = json.loads(pd_str)
                video_url = pd.get('url', '')
                if video_url:
                    ext = 'm3u8' if '.m3u8' in video_url else video_url.rsplit('.',1)[-1]
                    sources.append({
                        'url': video_url,
                        'ext': ext,
                        'filesize': 0,
                        'resolution': '',
                        'note': pd.get('from', 'player_data'),
                        'from': 'player_data',
                        'referer': page_url
                    })
        except Exception:
            pass

    if not sources:
        # 方法3: 尝试用 yt-dlp 的 --list-formats 看有什么
        try:
            result = subprocess.run(
                ['yt-dlp', '-F', '--no-playlist', '--no-warnings', '--no-download', page_url],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0:
                sources.append({'url': page_url, 'ext': 'unknown', 'filesize': 0, 
                               'resolution': '', 'note': 'yt-dlp -F 输出\n' + result.stdout[:500],
                               'from': 'yt-dlp_list'})
        except Exception:
            pass
    
    return sources
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
            token = self._get_token(params=params)
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
        # === 文件管理下载器 GET 路由 ===
        if _handle_download_get(self, params):
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
            if action == 'shutdown':
                _log_access(self.client_address[0], 'POST', '/api/sys-action', '关机')
                self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers()
                self.wfile.write(json.dumps({'success':True,'message':'服务器正在关机...'}).encode())
                threading.Thread(target=lambda: (time.sleep(1), os.system('poweroff')), daemon=True).start()
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
                        err = (r.stderr or "")[:500]
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
        # === 文件管理下载器 POST 路由 ===
        elif path == '/api/file-browse':
            if not verify_session(self._get_token(data=data)):
                self.send_json({'error':'unauthorized'}, 401); return
            subpath = data.get('path', '/data/share').replace('..', '')
            subpath = os.path.normpath('/' + subpath.lstrip('/'))
            if not subpath.startswith('/data/share'):
                self.send_json({'error': '路径不允许'}, 403); return
            try:
                items = []
                for name in sorted(os.listdir(subpath)):
                    fp = os.path.join(subpath, name)
                    st = os.stat(fp)
                    items.append({
                        'name': name,
                        'is_dir': os.path.isdir(fp),
                        'size': st.st_size,
                        'mtime': datetime.fromtimestamp(st.st_mtime).isoformat()
                    })
                items.sort(key=lambda x: (not x['is_dir'], x['name'].lower()))
                self.send_response(200); self.send_header('Content-Type','application/json; charset=utf-8'); self.end_headers()
                self.wfile.write(json.dumps({'path': subpath, 'items': items}, ensure_ascii=False).encode())
            except Exception as e:
                self.send_json({'error': str(e)}, 500)
        elif path == '/api/file-mkdir':
            if not verify_session(self._get_token(data=data)):
                self.send_json({'error':'unauthorized'}, 401); return
            parent = data.get('path', '/data/share').replace('..', '')
            name = data.get('name', '').strip()
            if not name:
                self.send_json({'error': '文件夹名不能为空'}, 400); return
            fp = os.path.normpath(os.path.join(parent, name))
            if not fp.startswith('/data/share'):
                self.send_json({'error': '路径不允许'}, 403); return
            try:
                os.makedirs(fp, exist_ok=True)
                self.send_response(200); self.send_header('Content-Type','application/json; charset=utf-8'); self.end_headers()
                self.wfile.write(json.dumps({'success': True, 'path': fp}, ensure_ascii=False).encode())
            except Exception as e:
                self.send_json({'error': str(e)}, 500)
        elif path == '/api/file-delete':
            if not verify_session(self._get_token(data=data)):
                self.send_json({'error':'unauthorized'}, 401); return
            fp = data.get('path', '').replace('..', '')
            fp = os.path.normpath('/' + fp.lstrip('/'))
            if not fp.startswith('/data/share'):
                self.send_json({'error': '路径不允许'}, 403); return
            if fp == '/data/share' or fp == '/data/share/':
                self.send_json({'error': '不能删除根目录'}, 400); return
            try:
                if os.path.isdir(fp):
                    import shutil
                    shutil.rmtree(fp)
                else:
                    os.remove(fp)
                self.send_response(200); self.send_header('Content-Type','application/json; charset=utf-8'); self.end_headers()
                self.wfile.write(json.dumps({'success': True, 'path': fp}, ensure_ascii=False).encode())
            except Exception as e:
                self.send_json({'error': str(e)}, 500)

        elif path == '/api/download/parse-url':
            if not verify_session(self._get_token(data=data)):
                self.send_json({'error':'unauthorized'}, 401); return
            url = data.get('url', '').strip()
            if not url:
                self.send_json({'error': '请输入链接'}, 400); return
            # 如果本来就是 m3u8/mp4 直链，直接返回一个源
            if url.endswith('.m3u8') or url.endswith('.mp4'):
                self.send_json({'sources': [{'url': url, 'ext': 'm3u8' if url.endswith('.m3u8') else 'mp4',
                                            'filesize': 0, 'resolution': '', 'note': '直链',
                                            'from': 'direct'}], 'url': url})
                return
            sources = _parse_url_for_videos(url)
            self.send_json({'sources': sources, 'url': url})
        elif _handle_download_post(self, data):
            return
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
_load_dl_tasks()
# 启动下载看门狗和定期保存线程
threading.Thread(target=_download_watchdog, daemon=True).start()
threading.Thread(target=_periodic_save, daemon=True).start()
_start_net_history_recorder()
_start_service_traffic_collector()
# 立即记录一次当前流量
_record_net_traffic()

print(f"OneCloud Monitor v4 running on :{PORT} (max_conns={_MAX_CONNS})")
server.serve_forever()