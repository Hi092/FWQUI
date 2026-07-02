#!/bin/sh
# 监控面板数据备份，保留7天
BACKUP_DIR=/data/share/backup
DATE=$(date +%Y%m%d_%H%M%S)
mkdir -p $BACKUP_DIR

# 备份
tar czf $BACKUP_DIR/monitor_$DATE.tar.gz \
    /opt/monitor/config.json \
    /opt/monitor/services.json \
    /etc/cron.d/onecloud-maintenance \
    2>/dev/null

# 删除7天前的备份
find $BACKUP_DIR -name "monitor_*.tar.gz" -mtime +7 -delete

echo "Backup done: monitor_$DATE.tar.gz"
