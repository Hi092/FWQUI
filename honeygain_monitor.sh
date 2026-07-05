#!/bin/sh
LOG_FILE="/tmp/honeygain_monitor.log"
CONTAINER_NAME="honeygain"
CHECK_INTERVAL=60
MAX_RESTARTS=5
RESTART_COUNT=0
LAST_RESTART_TIME=0

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$LOG_FILE"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1"
}

check_container() {
    docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"
}

check_disconnected() {
    local logs=$(docker logs --tail 20 "$CONTAINER_NAME" 2>&1)
    echo "$logs" | grep -q "service disconnected" && return 0
    echo "$logs" | grep -q "API Ping Error" && return 0
    echo "$logs" | grep -q "not_valid_login_credentials" && return 0
    return 1
}

restart_container() {
    local current_time=$(date +%s)
    local time_diff=$((current_time - LAST_RESTART_TIME))
    if [ $time_diff -lt 300 ]; then
        log "距离上次重启不足5分钟，跳过"
        return 1
    fi
    if [ $RESTART_COUNT -ge $MAX_RESTARTS ]; then
        log "已达到最大重启次数，等待1小时重置"
        sleep 3600
        RESTART_COUNT=0
    fi
    log "检测到断连，正在重启..."
    docker restart "$CONTAINER_NAME"
    if [ $? -eq 0 ]; then
        RESTART_COUNT=$((RESTART_COUNT + 1))
        LAST_RESTART_TIME=$current_time
        log "重启成功 (第${RESTART_COUNT}次)"
        sleep 30
    else
        log "重启失败"
    fi
}

log "Honeygain 监控脚本启动"
while true; do
    if check_container; then
        check_disconnected && restart_container
    else
        log "容器未运行，正在启动..."
        docker start "$CONTAINER_NAME"
        sleep 10
    fi
    sleep "$CHECK_INTERVAL"
done
