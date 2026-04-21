# ai-worker 서버 셋업 가이드

> 서버 재설치 또는 새 환경에서 처음 구성할 때 참조.

---

## 1. 사전 요구사항

```bash
# Python 가상환경
python3 -m venv /home/home/ai-worker/venv
/home/home/ai-worker/venv/bin/pip install -r requirements.txt

# Playwright (자동 테스트용)
/home/home/ai-worker/venv/bin/playwright install chromium

# Ollama (로컬 LLM)
ollama pull qwen2.5-coder:32b
ollama pull nomic-embed-text      # RAG 임베딩

# GitHub CLI (선택)
gh auth login
```

---

## 2. 환경변수 (.env)

`/home/home/ai-worker/.env` 에 아래 변수 설정:

```env
TELEGRAM_BOT_TOKEN=...          # 알리미 봇 토큰
TELEGRAM_CHAT_ID=...            # 알리미 채팅 ID
LOG_TELEGRAM_BOT_TOKEN=...      # 개발해봇 토큰
LOG_TELEGRAM_CHAT_ID=...        # 개발해봇 채팅 ID
GITHUB_TOKEN=...                # GitHub PAT (repo + workflow 권한)
```

---

## 3. systemd 서비스 목록

| 서비스 | 역할 | enable |
|--------|------|--------|
| `telegram-bot` | 알리미 챗봇 | ✅ |
| `dev-bot` | 개발해봇 | ✅ |
| `ai-loop` | AI 피드백 루프 (loop.py) | ✅ |
| `ollama` | Ollama LLM 서버 (11434) | ✅ |
| `llama-cpp` | Gemma4 llama-cpp 서버 (8080) | — |
| `xvfb` | 가상 디스플레이 (Playwright headless) | — |
| `discord-agent` | 디스코드 봇 | — |
| `github-runner` | GitHub Actions 자체 호스팅 러너 | — |
| `nanoclaw-monitor` | 모니터링 | — |

> `ai-loop`만 신규 생성 필요 — 나머지는 기존 서버에 이미 존재.

### 서비스 파일 내용

**telegram-bot.service**
```ini
[Unit]
Description=ai-worker Telegram Bot
After=network.target

[Service]
Type=simple
User=home
WorkingDirectory=/home/home/ai-worker/agent
EnvironmentFile=/home/home/ai-worker/.env
ExecStart=/home/home/ai-worker/venv/bin/python telegram_bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

**dev-bot.service**
```ini
[Unit]
Description=ai-worker 개발해봇 (Dev Telegram Bot)
After=network.target

[Service]
Type=simple
User=home
WorkingDirectory=/home/home/ai-worker/agent
EnvironmentFile=/home/home/ai-worker/.env
ExecStart=/home/home/ai-worker/venv/bin/python dev_bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

**ai-loop.service**
```ini
[Unit]
Description=ai-worker AI 피드백 루프 (loop.py)
After=network.target telegram-bot.service

[Service]
Type=simple
User=home
WorkingDirectory=/home/home/ai-worker
EnvironmentFile=/home/home/ai-worker/.env
ExecStart=/home/home/ai-worker/venv/bin/python agent/loop.py
Restart=always
RestartSec=15
StandardOutput=append:/home/home/loop.log
StandardError=append:/home/home/loop.log

[Install]
WantedBy=multi-user.target
```

---

## 4. 최초 등록 명령어

```bash
# 서비스 파일 복사 후
sudo systemctl daemon-reload

# 자동시작 등록 + 즉시 실행
sudo systemctl enable --now telegram-bot
sudo systemctl enable --now dev-bot
sudo systemctl enable --now ai-loop

# 상태 확인
sudo systemctl status telegram-bot dev-bot ai-loop
```

---

## 5. 상태 확인 / 재시작

```bash
# 상태
sudo systemctl status ai-loop

# 재시작
sudo systemctl restart ai-loop

# 로그
journalctl -u ai-loop -f
tail -f /home/home/loop.log       # loop.py 전용 로그
```

---

## 6. 주요 경로

| 항목 | 경로 |
|------|------|
| 레포 | `/home/home/ai-worker` |
| 가상환경 | `/home/home/ai-worker/venv` |
| 환경변수 | `/home/home/ai-worker/.env` |
| 설정 | `/home/home/ai-worker/config.yaml` |
| 루프 로그 | `/home/home/loop.log` |
| 태스크 큐 | `/home/home/ai-worker/tasks/` |
| ChromaDB | `/home/home/ai-worker/data/chroma_db/` |
| 지식베이스 | `/home/home/Jin_lifestyle/10_Wiki` |
| LLM WIKI | `/home/home/LLM WIKI/wiki/` |

---

## 7. config.yaml 핵심 설정

```yaml
agent:
  work_dir: "/home/home/ai-worker"   # 절대경로 필수
  model: "claude"                     # claude / openai / ollama / llama-cpp
  max_retries: 3

github:
  repo: "hjin4eo/ai-app"
  poll_interval: 30
```
