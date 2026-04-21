#!/usr/bin/env bash
# GitHub Actions Self-hosted Runner 설치 및 systemd 서비스 등록

set -e

RUNNER_VERSION="2.322.0"
RUNNER_DIR="$HOME/actions-runner"
REPO_URL="https://github.com/hjin4eo/ai-app"

echo "=== GitHub Actions Runner v${RUNNER_VERSION} 설치 ==="
mkdir -p "$RUNNER_DIR" && cd "$RUNNER_DIR"

curl -fsSL -o runner.tar.gz \
  "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz"
tar xzf runner.tar.gz
rm runner.tar.gz

echo ""
echo "=== Runner 등록 ==="
echo "GitHub 레포 → Settings → Actions → Runners → New self-hosted runner"
echo "페이지에서 'Configure' 섹션의 토큰을 복사하여 아래에 입력하세요."
echo ""
read -rp "Runner Registration Token: " RUNNER_TOKEN

./config.sh \
  --url "$REPO_URL" \
  --token "$RUNNER_TOKEN" \
  --name "nanoclaw-runner" \
  --labels "self-hosted,linux,x64,gpu" \
  --work "_work" \
  --unattended

echo ""
echo "=== systemd 서비스 등록 ==="
sudo tee /etc/systemd/system/github-runner.service > /dev/null << EOF
[Unit]
Description=GitHub Actions Runner (hjin4eo/ai-app)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$RUNNER_DIR
ExecStart=$RUNNER_DIR/run.sh
Restart=on-failure
RestartSec=10s
Environment=DISPLAY=:1
Environment=HOME=$HOME

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now github-runner

echo ""
echo "✅ Runner 등록 완료"
echo "상태 확인: sudo systemctl status github-runner"
echo "로그 확인: journalctl -u github-runner -f"
