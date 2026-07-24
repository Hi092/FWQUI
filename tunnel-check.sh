#!/bin/bash
# 检查Cloudflare隧道地址变化并推送微信
# 逻辑：1. 确保cloudflared在运行 2. 获取地址 3. 验证地址可访问 4. 推送

TUNNEL_FILE="/opt/monitor/last_tunnel_url"
PUSHPLUS_TOKEN="8a1b556664db413e921c09fdc6d75a3d"

# 1. 检查cloudflared是否在运行，没运行就启动
if ! pgrep -x cloudflared > /dev/null; then
    logger -t tunnel-check "cloudflared not running, starting..."
    nohup /usr/local/bin/cloudflared tunnel --url http://127.0.0.1:8088 --metrics localhost:51888 --no-autoupdate --edge-ip-version 4 --retries 10 --proxy-tcp-keepalive 15s --loglevel info > /var/log/cloudflared.log 2>&1 &
    sleep 8  # 等待tunnel建立（给够时间）
fi

# 2. 获取当前隧道地址（最多尝试5次，每次间隔3秒）
NEW_URL=""
for i in 1 2 3 4 5; do
    NEW_URL=$(curl -s --max-time 5 http://localhost:51888/quicktunnel 2>/dev/null | grep -o "https://[a-z0-9\-]*\.trycloudflare\.com" || echo "")
    if [ -n "$NEW_URL" ]; then
        break
    fi
    sleep 3
done

if [ -z "$NEW_URL" ]; then
    logger -t tunnel-check "Failed to get tunnel URL after 5 attempts"
    exit 0
fi

# 3. 验证地址可访问（通过tunnel访问filebrowser登录页，最多尝试3次）
ACCESSIBLE=0
for i in 1 2 3; do
    HTTP_CODE=$(curl -sk --max-time 10 -o /dev/null -w "%{http_code}" "${NEW_URL}/login" 2>/dev/null)
    if [ "$HTTP_CODE" = "200" ] || [ "$HTTP_CODE" = "302" ] || [ "$HTTP_CODE" = "301" ]; then
        ACCESSIBLE=1
        break
    fi
    sleep 3
done

if [ "$ACCESSIBLE" -ne 1 ]; then
    logger -t tunnel-check "Tunnel URL not accessible: $NEW_URL (HTTP $HTTP_CODE)"
    exit 0
fi

# 4. 读取旧地址
OLD_URL=""
if [ -f "$TUNNEL_FILE" ]; then
    OLD_URL=$(cat "$TUNNEL_FILE")
fi

# 5. 推送条件：地址变了，或者旧地址为空（首次获取）
if [ "$NEW_URL" != "$OLD_URL" ]; then
    if [ -z "$OLD_URL" ]; then
        TITLE="Cloudflare Tunnel 新地址"
        CONTENT="<p>网盘地址: <a href=\"$NEW_URL\">$NEW_URL</a></p><p>（已验证可访问）</p>"
    else
        TITLE="Cloudflare Tunnel 已更新"
        CONTENT="<p>旧地址: $OLD_URL</p><p>新地址: <a href=\"$NEW_URL\">$NEW_URL</a></p><p>（已验证可访问）</p>"
    fi
    
    # 推送微信通知
    curl -s -X POST "http://www.pushplus.plus/send" \
        -H "Content-Type: application/json" \
        -d "{\"token\":\"$PUSHPLUS_TOKEN\",\"title\":\"$TITLE\",\"content\":\"$CONTENT\"}" > /dev/null 2>&1
    
    logger -t tunnel-check "Pushed verified URL: $NEW_URL"
fi

# 6. 保存当前地址
echo "$NEW_URL" > "$TUNNEL_FILE"
