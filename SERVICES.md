# 자동 실행 서비스 목록

> WSL 부팅 시 systemd로 자동 실행되는 서비스들

## 시스템 레벨 (`sudo systemctl`)

| 서비스 | 설명 | 실행 경로 | venv |
|---|---|---|---|
| `telegram-bot` | 알리미 — 일반 AI 챗봇 | `/home/home/ai-worker/agent/telegram_bot.py` | `/home/home/ai-worker/venv` |
| `dev-bot` | 개발해봇 — 개발 워크플로우 전용 | `/home/home/ai-worker/agent/dev_bot.py` | `/home/home/ai-worker/venv` |
| `discord-agent` | Discord AI 봇 | `/home/home/discord-agent/bot.py` | `/home/home/discord-agent/venv` |
| `llama-cpp` | llama-cpp-python 서버 (비전 지원) | 모델: `/mnt/c/ollamamodels/gemma-4-E4B-it.Q8_0.gguf` | `/home/home/llama-env` |
| `nanoclaw-monitor` | 서버 모니터 (Discord+Telegram) | `/home/home/server-monitor/monitor.py` | `/home/home/server-monitor/venv` |
| `github-runner` | GitHub Actions Runner | `/home/home/actions-runner/run.sh` | - |
| `ollama` | Ollama (현재 **disabled**) | `/usr/local/bin/ollama serve` | - |

## 서비스 파일 위치

모두 `/etc/systemd/system/` 아래:
- `telegram-bot.service`
- `dev-bot.service`
- `discord-agent.service`
- `llama-cpp.service`
- `nanoclaw-monitor.service`
- `github-runner.service`
- `ollama.service` (disabled)

## 주의: 사용자 레벨 서비스 (disabled)

이전에 `systemctl --user`로도 등록되어 있었으나 **중복 실행** 문제로 비활성화함:
- `~/.config/systemd/user/telegram-bot.service` → disabled
- `~/.config/systemd/user/discord-bot.service` → disabled

## 자주 쓰는 명령

```bash
# 상태 확인
sudo systemctl status telegram-bot discord-agent llama-cpp nanoclaw-monitor github-runner

# 재시작
sudo systemctl restart telegram-bot
sudo systemctl restart discord-agent
sudo systemctl restart llama-cpp

# 로그 확인
sudo journalctl -u telegram-bot --no-pager -n 50
sudo journalctl -u discord-agent --no-pager -n 50
sudo journalctl -u llama-cpp --no-pager -n 50
```
