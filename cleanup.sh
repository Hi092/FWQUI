#!/bin/bash
# 自动清理脚本 - 每天凌晨3点执行
# 日志: /opt/monitor/cleanup.log
# 原则: 只清明确的垃圾，不动任何可能有用的文件

LOG="/opt/monitor/cleanup.log"
echo "========== $(date '+%Y-%m-%d %H:%M:%S') 开始清理 ==========" >> "$LOG"

# 1. 清理 apt 缓存（安全的系统操作）
apt-get clean 2>/dev/null
echo "[apt] clean done" >> "$LOG"

# 2. 清理 journal 日志（保留 7 天，最大 50MB）
journalctl --vacuum-time=7d --vacuum-size=50M 2>&1 | tail -1 >> "$LOG"

# 3. 清理下载残留 .part 文件（仅 .part，超过 7 天没人管 = 废弃下载）
#    .tmp / .temp 不碰 —— 可能是正常文件
DELETED=$(find /data/share/ -type f -name "*.part" -mtime +7 -print -delete 2>/dev/null)
if [ -n "$DELETED" ]; then
    echo "[download] deleted stale .part files:" >> "$LOG"
    echo "$DELETED" >> "$LOG"
else
    echo "[download] no stale .part files" >> "$LOG"
fi

# 4. 截断过大的监控日志（超过 5MB → 保留最后 1MB）
for f in /opt/monitor/*.log; do
    size=$(stat -c%s "$f" 2>/dev/null)
    if [ -n "$size" ] && [ "$size" -gt 5242880 ]; then
        tail -c 1048576 "$f" > "${f}.logtmp" && mv "${f}.logtmp" "$f"
        echo "[log] truncated $(basename $f) ($size bytes → 1MB)" >> "$LOG"
    fi
done

# 5. 清理 /tmp（超过 7 天的文件）
find /tmp -type f -mtime +7 -delete 2>/dev/null
echo "[tmp] cleaned" >> "$LOG"

# 6. 清理 root 缓存目录
rm -rf /root/.cache/* 2>/dev/null
echo "[cache] cleaned" >> "$LOG"

echo "========== $(date '+%Y-%m-%d %H:%M:%S') 清理完成 ==========" >> "$LOG"
echo "" >> "$LOG"

# 限制自身日志不超过 1MB
LOGSIZE=$(stat -c%s "$LOG" 2>/dev/null)
if [ -n "$LOGSIZE" ] && [ "$LOGSIZE" -gt 1048576 ]; then
    tail -c 524288 "$LOG" > "${LOG}.logtmp" && mv "${LOG}.logtmp" "$LOG"
fi
