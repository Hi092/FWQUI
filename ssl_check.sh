#!/bin/sh
# SSL证书检查+续期通知
LOG=/opt/monitor/ssl_notify.txt
CERT=/etc/filebrowser/ssl/cert.pem
BEFORE=$(openssl x509 -in $CERT -noout -enddate 2>/dev/null)
/root/.acme.sh/acme.sh --cron --home /root/.acme.sh > /dev/null 2>&1
AFTER=$(openssl x509 -in $CERT -noout -enddate 2>/dev/null)
if [ "$BEFORE" != "$AFTER" ]; then
    echo "$(date "+%Y-%m-%d %H:%M") SSL证书已续期 → $AFTER" > $LOG
else
    echo "$(date "+%Y-%m-%d %H:%M") SSL证书未到期 $AFTER" > $LOG
fi
