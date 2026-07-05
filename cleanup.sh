#!/bin/bash
# 自动清理脚本 - 每天执行
# 日志: /opt/monitor/cleanup.log

LOG="/opt/monitor/cleanup.log"
echo "========== $(date '+%Y-%m-%d %H:%M:%S') 开始清理 ==========" >> "$LOG"

# 1. 清理 apt 缓存
apt-get clean 2>/dev/null
echo "[apt] clean done" >> "$LOG"

# 2. 清理 journal 日志（保留 7 天，最大 50MB）
journalctl --vacuum-time=7d --vacuum-size=50M 2>&1 | tail -1 >> "$LOG"

# 3. 清理下载残留文件（超过 1 天的 .part .tmp）
find /data/share/ -type f \( -name "*.part" -o -name "*.tmp" -o -name "*.temp" \) -mtime +1 -delete 2>/dev/null
echo "[download] cleaned stale .part/.tmp files" >> "$LOG"

# 4. 截断过大的监控日志（超过 5MB）
for f in /opt/monitor/*.log; do
    size=$(stat -c%s "$f" 2>/dev/null)
    if [ -n "$size" ] && [ "$size" -gt 5242880 ]; then
        tail -c 1048576 "$f" > "${f}.tmp" && mv "${f}.tmp" "$f"
        echo "[log] truncated $f ($size → 1MB)" >> "$LOG"
    fi
done

# 5. 清理 /tmp（超过 3 天的文件）
find /tmp -type f -mtime +3 -delete 2>/dev/null
echo "[tmp] cleaned" >> "$LOG"

# 6. 清理缓存目录
rm -rf /root/.cache/* 2>/dev/null
echo "[cache] cleaned" >> "$LOG"

echo "========== $(date '+%Y-%m-%d %H:%M:%S') 清理完成 ==========" >> "$LOG"
echo "" >> "$LOG"

# 限制 cleanup.log 不超过 1MB
LOGSIZE=$(stat -c%s "$LOG" 2>/dev/null)
if [ -n "$LOGSIZE" ] && [ "$LOGSIZE" -gt 1048576 ]; then
    tail -c 524288 "$LOG" > "${LOG}.tmp" && mv "${LOG}.tmp" "$LOG"
fi
