#!/usr/bin/env bash
# Xvfb 가상 디스플레이 systemd 서비스 등록
# headed 테스트를 모니터 없이 실행할 수 있게 함 (DISPLAY=:1)

set -e

echo "=== Xvfb 설치 ==="
sudo apt-get install -y xvfb x11-utils

echo "=== systemd 서비스 등록 ==="
sudo tee /etc/systemd/system/xvfb.service > /dev/null << 'EOF'
[Unit]
Description=Xvfb Virtual Display :1
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/Xvfb :1 -screen 0 1920x1080x24 -ac
Restart=on-failure
RestartSec=3s

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now xvfb

echo ""
echo "=== 확인 ==="
sleep 1
DISPLAY=:1 xdpyinfo | head -5 && echo "✅ 가상 디스플레이 DISPLAY=:1 정상 작동"

echo ""
echo "GitHub Actions ci.yml의 DISPLAY를 :1로 변경하려면:"
echo "  env:"
echo "    DISPLAY: \":1\""
