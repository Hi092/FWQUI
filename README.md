# OneCloud 监控面板 v9.8 完整代码

三个文件：

1. **server_monitor.py** — Python后端（1215行）
2. **static/index.html** — 前端页面（1651行）
3. **tools.ps1** — Windows远程控制脚本（72行）

## 部署方法

```bash
# 1. 在服务器上创建目录
mkdir -p /opt/monitor/static /opt/tools

# 2. 上传三个文件
# server_monitor.py → /opt/monitor/server_monitor.py
# index.html → /opt/monitor/static/index.html
# tools.ps1 → C:/tools/tools.ps1 (公司电脑)

# 3. 创建systemd服务
cat > /etc/systemd/system/monitor.service << 'EOF'
[Unit]
Description=OneCloud Monitor
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/monitor
ExecStart=/usr/bin/python3 /opt/monitor/server_monitor.py
Restart=always
RestartSec=3
Environment=PUSHPLUS_TOKEN=你的token
Environment=ZHIPU_API_KEY=你的key

[Install]
WantedBy=multi-user.target
EOF

# 4. 启动
systemctl daemon-reload
systemctl enable --now monitor
```

## 依赖

- Python 3.8+（标准库，无需pip安装）
- OpenSSH客户端（远程控制公司电脑用）

## 访问

- 局域网：http://服务器IP:9090
- 外网（Tailscale）：http://tailscale-ip:9090
- 默认密码：123000
