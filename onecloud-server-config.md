# OneCloud 服务器完整配置文档

> 最后更新: 2026-07-11
> 设备: OneCloud (迅雷玩客云)
> SoC: Amlogic Meson8b (S805), ARMv7 Cortex-A5, 4核 1.6GHz
> 内存: 981MB
> 存储: 系统盘 mmcblk1p2 6.9G eMMC + 数据盘 mmcblk0p1 115G SD卡 + 50G loop镜像
> 网络: 局域网 192.168.3.77 / Tailscale 100.127.126.90

---

## 一、硬件性能优化

### 1.1 CPU 满血模式
- **服务**: `/etc/systemd/system/fix-eth0-power.service` → `/usr/local/bin/performance-setup.sh`
- **配置**: cpufrequtils GOVERNOR=performance, MIN_SPEED=96000, MAX_SPEED=1608000
- **文件**: `/etc/default/cpufrequtils` (immutable保护)
- **效果**: CPU锁频1608MHz，不降频

### 1.2 AES 硬件加速
- **模块**: `aes-arm-bs` (NEON bitsliced AES)
- **配置**: `/etc/modules` 加载 aes-arm-bs
- **效果**: cbc/ecb/ctr/xts 全部走 neonbs 驱动，比 aes-generic 快3-5倍
- **背景**: CPU Features有neon但没有aes指令集，SoC有硬件加密引擎但驱动未加载

### 1.3 网卡/USB/MMC 禁用省电
- **eth0电源**: `/sys/class/net/eth0/power/control` = on
- **USB autosuspend**: usb1/usb2 = -1
- **MMC/SD卡**: power/control = on
- **Wake-on-LAN**: ethtool -s eth0 wol g
- **持久化**: performance-setup.sh + rc.local 兜底

### 1.4 磁盘预读 + IO调度器
- **预读**: mmcblk0/mmcblk1/loop0 = 1024KB (2048扇区)
- **IO调度**: mq-deadline (适合eMMC/SD卡)
- **持久化**: performance-setup.sh

### 1.5 irqbalance
- **服务**: irqbalance.service (enabled)
- **效果**: 4核CPU中断分散，避免单核过载

---

## 二、内存管理优化

### 2.1 统一sysctl配置
- **文件**: `/etc/sysctl.d/99-onecloud.conf`
- **旧配置**: 9个冲突文件已备份到 `/etc/sysctl.d/old-bak/`

### 2.2 关键参数
```bash
vm.swappiness = 150                    # ZRAM友好，积极使用swap
vm.vfs_cache_pressure = 100            # 平衡缓存回收
vm.min_free_kbytes = 32768             # 32MB保留（之前150MB太高）
vm.watermark_boost_factor = 15000      # 标准值（之前50000太高）
vm.watermark_scale_factor = 100        # 标准值（之前500太高）
vm.page-cluster = 0                    # ZRAM不需要预读
vm.overcommit_memory = 0               # 默认启发式
vm.zone_reclaim_mode = 0               # 禁用NUMA回收
vm.extra_free_kbytes = 4096
vm.max_map_count = 262144
```

### 2.3 脏页策略 (保护eMMC/SD卡寿命)
```bash
vm.dirty_ratio = 5                     # 5%脏页强制写入
vm.dirty_background_ratio = 2          # 2%开始后台写入
vm.dirty_writeback_centisecs = 1500    # 15秒检查周期
vm.dirty_expire_centisecs = 3000       # 30秒脏页过期
```

### 2.4 ZRAM Swap
- **设备**: /dev/zram0, 981MB
- **算法**: lzo-rle
- **配置**: `/etc/default/armbian-zram-config` (ZRAM_PERCENTAGE=100)

### 2.5 OOM保护
- **earlyoom**: active, 配置 -r 10 -m 5 -s 95 -p --prefer '(python3)' --avoid '(systemd|sshd|agetty)'
- **vm.panic_on_oom = 0**
- **vm.oom_kill_allocating_task = 0**

---

## 三、网络优化

### 3.1 TCP参数
```bash
net.ipv4.tcp_congestion_control = bbr  # BBR拥塞控制
net.core.default_qdisc = fq            # 公平队列
net.ipv4.tcp_tw_reuse = 1              # TIME_WAIT复用
net.ipv4.tcp_fin_timeout = 15          # FIN超时缩短
net.ipv4.tcp_fastopen = 3              # TCP快速打开
net.ipv4.tcp_window_scaling = 1        # 窗口缩放
net.ipv4.tcp_sack = 1                  # 选择性确认
net.ipv4.tcp_mtu_probing = 1           # MTU探测
net.ipv4.tcp_slow_start_after_idle = 0 # 空闲后不慢启动
net.ipv4.tcp_keepalive_time = 300      # 5分钟保活
net.ipv4.tcp_keepalive_intvl = 30
net.ipv4.tcp_keepalive_probes = 5
```

### 3.2 缓冲区
```bash
net.core.rmem_max = 4194304            # 4MB接收缓冲
net.core.wmem_max = 4194304            # 4MB发送缓冲
net.ipv4.tcp_rmem = 4096 131072 4194304
net.ipv4.tcp_wmem = 4096 65536 4194304
```

### 3.3 连接队列
```bash
net.core.somaxconn = 4096
net.ipv4.tcp_max_syn_backlog = 8192
net.core.netdev_max_backlog = 8192
net.core.dev_weight = 128
net.core.netdev_budget = 600
```

### 3.4 端口范围
```bash
net.ipv4.ip_local_port_range = 10240 65535
```

### 3.5 ARP
```bash
net.ipv4.conf.all.arp_filter = 1
net.ipv4.conf.all.arp_ignore = 1
net.ipv4.conf.all.arp_announce = 2
```

---

## 四、文件系统优化

### 4.1 挂载参数 (/etc/fstab, immutable保护)
```
/         ext4 noatime,commit=5,errors=remount-ro    # 根分区
/data     ext4 noatime,commit=5                      # 数据盘
/data/share ext4 loop,noatime,commit=5               # loop镜像
```

### 4.2 关键改进
- **commit**: 120 → 5 (断电最多丢30秒数据，之前丢2分半)
- **noatime**: 减少写入，延长eMMC/SD卡寿命
- **ext4 ordered data mode**: 数据先写再记日志，最安全

### 4.3 文件系统限制
```bash
fs.file-max = 1048576
fs.nr_open = 1048576
fs.aio-max-nr = 1048576
fs.inotify.max_user_watches = 524288
fs.inotify.max_user_instances = 1024
```

### 4.4 定时维护
- **fstrim.timer**: 每周TRIM
- **e2scrub_all.timer**: 每周文件系统检查
- **logrotate.timer**: 日志轮转
- **journalctl --vacuum-size=50M**: 每天3点清理日志

---

## 五、开机自启服务清单

### 5.1 性能/系统
| 服务 | 说明 |
|------|------|
| fix-eth0-power.service | CPU满血+网卡/USB/MMC不休眠+磁盘预读+IO调度 |
| armbian-hardware-optimize.service | Armbian硬件优化(读cpufrequtils) |
| irqbalance.service | 中断分散 |
| earlyoom.service | OOM保护 |
| armbian-ramlog.service | RAM日志(eMMC寿命) |
| armbian-zram-config.service | ZRAM swap |
| armbian-led-state.service | LED控制 |
| leds-off.service | 关闭LED |
| fake-hwclock.service | 时钟保存(无RTC) |
| chrony.service | NTP时间同步 |
| fstrim.timer | 每周TRIM |
| logrotate.timer | 日志轮转 |
| e2scrub_all.timer | 文件系统检查 |

### 5.2 业务服务
| 服务 | 端口 | 说明 |
|------|------|------|
| filebrowser.service | 8088 | 网盘 |
| cloudflared.service | - | Cloudflare隧道 |
| monitor.service | 9090 | 服务器监控面板 |
| pospal-web.service | - | 银豹日报Web |
| smbd.service | 445 | Samba文件共享 |
| nmbd.service | 137/138 | Samba NetBIOS |
| tailscaled.service | - | Tailscale VPN |
| weather-monitor.timer | - | 天气监测(每2小时) |

### 5.3 基础服务
| 服务 | 说明 |
|------|------|
| ssh.service | SSH远程登录 |
| NetworkManager.service | 网络管理 |
| systemd-resolved.service | DNS解析 |
| cron.service | 定时任务 |
| console-setup.service | 控制台 |
| keyboard-setup.service | 键盘 |

### 5.4 已禁用的冲突服务
| 服务 | 原因 |
|------|------|
| cpu-governor.service | 设schedutil抢performance |
| crash-watcher.service | 每5秒写日志浪费资源 |
| arm-optimize.service | 跟fix-eth0-power重复 |

---

## 六、定时任务

### 6.1 系统cron (/etc/cron.d/onecloud-maintenance)
```
* * * * *     root /etc/filebrowser/cf-health-check.sh    # 隧道健康检查(每分钟)
1 15 * * *    root /root/.acme.sh/acme.sh --cron           # SSL证书续期(每天15:01)
0 3 * * *     root journalctl --vacuum-size=50M            # 日志清理(每天3点)
@reboot       root sleep 10 && systemctl restart cloudflared  # 隧道重启
```

### 6.2 用户cron (crontab -l)
```
0 * * * *     /usr/local/bin/check-disk-space.sh           # 磁盘空间告警(每小时)
```

### 6.3 Armbian系统cron
```
*/15 * * * *  root /usr/lib/armbian/armbian-truncate-logs  # 日志截断(每15分钟)
```

---

## 七、服务详情

### 7.1 FileBrowser
- **端口**: 8088 (cloudflared反代)
- **根目录**: /data/share
- **配置**: /etc/filebrowser/filebrowser.yml
- **会话超时**: 720h (30天)
- **重启策略**: Restart=always, RestartSec=5

### 7.2 Cloudflare隧道
- **健康检查**: 每分钟cf-health-check.sh，连续2次失败重启
- **@reboot**: 延迟10秒重启确保tun设备就绪
- **重启策略**: Restart=always, RestartSec=2

### 7.3 服务器监控
- **端口**: 9090
- **功能**: CPU/内存/磁盘/网络监控 + 下载管理
- **重启策略**: Restart=always, RestartSec=5

### 7.4 Samba
- **协议**: SMB2+ (禁用SMB1)
- **Mac兼容**: fruit:model = MacSamba
- **共享**: /data/share

---

## 八、保护措施

### 8.1 文件保护
- `/etc/sysctl.conf` — immutable (chattr +i)
- `/etc/fstab` — immutable (chattr +i)
- `/etc/default/cpufrequtils` — immutable (chattr +i)

### 8.2 数据安全
- ext4 ordered data mode (最安全)
- commit=5 (5秒刷盘)
- dirty_ratio=5 / dirty_background_ratio=2
- 断电最多丢~30秒数据

### 8.3 硬件保护
- earlyoom防止OOM
- 低脏页比例减少eMMC/SD卡写入
- noatime减少写入
- fstrim每周TRIM维护

---

## 九、网络服务地址

| 服务 | 局域网 | Tailscale |
|------|--------|-----------|
| 网盘 | http://192.168.3.77:8088 | http://100.127.126.90:8088 |
| 监控 | http://192.168.3.77:9090 | http://100.127.126.90:9090 |
| Samba | \\192.168.3.77 | \\100.127.126.90 |
| SSH | ssh root@192.168.3.77 | ssh root@100.127.126.90 |

---

## 十、已知限制

1. **断电风险**: SD卡硬件写缓存不受内核控制，极端情况下仍可能丢数据
2. **无UPS**: 建议加装小型UPS(几十块钱)
3. **无RTC**: 断电后时间靠fake-hwclock恢复，开机后chrony同步
4. **内存有限**: 981MB，ZRAM已用78MB swap
5. **无硬件AES**: AES靠NEON bitsliced软件加速
