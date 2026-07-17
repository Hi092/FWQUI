#!/bin/sh
# Cloudflare tunnel health check + keepalive
# Runs every minute via cron; keeps QUIC alive + restarts if truly down

COUNT_FILE="/tmp/cf_health_fail_count"
MAX_FAILS=5

# Try to reach tunnel endpoint
if curl -s --max-time 5 http://127.0.0.1:51888/quicktunnel | grep -q "trycloudflare.com"; then
    echo 0 > "$COUNT_FILE"
    # Keepalive: send a real request through the tunnel
    TUNNEL_URL=$(curl -s --max-time 3 http://127.0.0.1:51888/quicktunnel | python3 -c "import sys,json; print(json.load(sys.stdin).get('hostname',''))" 2>/dev/null)
    if [ -n "$TUNNEL_URL" ]; then
        curl -sk --max-time 10 -o /dev/null "https://${TUNNEL_URL}/login" 2>/dev/null &
    fi
    exit 0
fi

# Tunnel not responding
FAILS=$(cat "$COUNT_FILE" 2>/dev/null || echo 0)
FAILS=$((FAILS + 1))
echo "$FAILS" > "$COUNT_FILE"

logger -t cf-health "Tunnel unreachable (fail $FAILS/$MAX_FAILS)"

if [ "$FAILS" -ge "$MAX_FAILS" ]; then
    logger -t cf-health "Tunnel down too long, restarting cloudflared..."
    pkill cloudflared 2>/dev/null
    sleep 2
    nohup /usr/local/bin/cloudflared tunnel --url http://127.0.0.1:8088 --metrics localhost:51888 --no-autoupdate --edge-ip-version 4 --retries 10 --proxy-tcp-keepalive 15s --loglevel info > /var/log/cloudflared.log 2>&1 &
    echo 0 > "$COUNT_FILE"
fi