# Telegram Bot 운영 가이드

ai-worker 텔레그램 봇 실행/관리 방법 정리.

---

## 디렉토리 구조

```
/home/home/ai-worker/
├── .env                        # 환경변수 (토큰, ID 등)
├── config.yaml                 # 봇 설정 (모델, RAG, 경로 등)
├── bot.log                     # 구 로그 (현재는 journald 사용)
├── venv/                       # Python 가상환경
└── agent/
    ├── telegram_bot.py         # ★ 봇 메인 진입점
    ├── core/
    │   ├── bot_config.py       # 설정값 로드 (.env + config.yaml)
    │   ├── bot_utils.py        # 인증, 히스토리, Ollama 호출
    │   ├── agent.py            # 외부 검색 (Naver, 웹)
    │   ├── shared.py           # 공유 상수 (도시 목록 등)
    │   └── recovery.py        # 시작 시 복구 루틴
    ├── handlers/
    │   └── bot_commands.py     # 슬래시 명령어 핸들러
    └── services/
        ├── rag_manager.py      # RAG 지식 검색/인덱싱
        └── weather_service.py  # 날씨 캐시 및 동기화
```

---

## 실행 방법

### 방법 1 — systemd 서비스 (권장, 현재 운영 방식)

```bash
# 상태 확인
systemctl status telegram-bot.service

# 시작
systemctl start telegram-bot.service

# 중지
systemctl stop telegram-bot.service

# 재시작
systemctl restart telegram-bot.service

# 부팅 시 자동 시작 등록
systemctl enable telegram-bot.service
```

서비스 파일 위치: `/etc/systemd/system/telegram-bot.service`

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

### 방법 2 — 직접 실행 (디버깅용)

```bash
cd /home/home/ai-worker/agent
source ../venv/bin/activate
python telegram_bot.py
```

> **주의**: 직접 실행 시 systemd 서비스가 이미 켜져 있으면 `Conflict: terminated by other getUpdates request` 에러 발생.
> 반드시 서비스를 먼저 중지할 것.
> ```bash
> systemctl stop telegram-bot.service
> ```

---

## 로그 확인

봇은 **journald**에 로그를 기록한다 (`bot.log`는 구버전 흔적, 현재 미사용).

```bash
# 최근 50줄
journalctl -u telegram-bot.service -n 50 --no-pager

# 실시간 감시
journalctl -u telegram-bot.service -f

# 에러만
journalctl -u telegram-bot.service -p err --no-pager

# 오늘 전체
journalctl -u telegram-bot.service --since today --no-pager
```

스크립트 사용:
```bash
bash /home/home/ai-worker/scripts/check_logs.sh       # 최근 50줄
bash /home/home/ai-worker/scripts/check_logs.sh -f    # 실시간
bash /home/home/ai-worker/scripts/check_logs.sh -s    # 통계
bash /home/home/ai-worker/scripts/check_logs.sh -h    # 전체 옵션
```

---

## 환경변수 (.env)

위치: `/home/home/ai-worker/.env`

| 변수명 | 설명 |
|--------|------|
| `TELEGRAM_BOT_TOKEN` | 봇 토큰 (BotFather 발급) |
| `TELEGRAM_CHAT_ID` | 관리자 chat_id |
| `TELEGRAM_ALLOWED_CHAT_IDS` | 허용 사용자 chat_id 목록 (콤마 구분) |
| `MODEL_BACKEND` | `ollama` 또는 `llama-cpp` |
| `EMBEDDING_BACKEND` | `ollama` 또는 `lm-studio` |
| `NAVER_CLIENT_ID` / `NAVER_CLIENT_SECRET` | 네이버 검색 API |

---

## 주요 설정 (config.yaml)

위치: `/home/home/ai-worker/config.yaml`

| 항목 | 현재값 | 설명 |
|------|--------|------|
| `agent.model` | `llama-cpp` | AI 백엔드 |
| `agent.ollama_model` | `gemma4:e4b-it-q8_0` | Ollama 모델명 |
| `llama_cpp.url` | `http://localhost:1234` | LM Studio 엔드포인트 |
| `agent.rag_enabled` | `true` | RAG 활성 여부 |
| `agent.knowledge_dir` | `/home/home/Jin_lifestyle/10_Wiki` | 지식 저장소 |
| `agent.embedding_backend` | `lm-studio` | 임베딩 백엔드 |

---

## 봇 명령어

| 명령어 | 설명 | 권한 |
|--------|------|------|
| 일반 메시지 | AI 자동 답변 (RAG + 웹 검색) | 허용 사용자 |
| `/search <검색어>` | 네이버 웹 검색 + AI 요약 | 허용 사용자 |
| `/ask <질문>` | RAG 지식 기반 질문 | 허용 사용자 |
| `/status` | 서버 CPU/RAM/GPU 상태 | 허용 사용자 |
| `/ps` | 프로세스 목록 | 허용 사용자 |
| `/clear` | 대화 기록 초기화 | 허용 사용자 |
| `/weather_sync` | 날씨 데이터 수동 동기화 | 허용 사용자 |
| `/sh <명령>` | 셸 명령 실행 | **관리자** |
| `/service <이름> <start\|stop\|restart>` | 서비스 제어 | **관리자** |
| `/allow <chat_id>` | 사용자 허용 | **관리자** |
| `/deny <chat_id>` | 사용자 차단 | **관리자** |
| `/users` | 허용 목록 조회 | **관리자** |
| `/index` | RAG 인덱스 전체 갱신 | **관리자** |
| `/reinforce` | 지식 강화 파이프라인 실행 | **관리자** |
| `/switch [lm\|ollama]` | AI 모델 백엔드 전환 | **관리자** |

---

## 자주 발생하는 문제

### Conflict 에러
```
telegram.error.Conflict: terminated by other getUpdates request
```
**원인**: 봇 인스턴스가 2개 이상 실행 중.  
**해결**: `ps aux | grep telegram_bot` 으로 확인 후 중복 프로세스 종료.

### 봇이 응답 안 함
1. `systemctl status telegram-bot.service` 로 실행 상태 확인
2. `journalctl -u telegram-bot.service -n 30` 으로 에러 확인
3. LM Studio / Ollama가 떠 있는지 확인 (`http://localhost:1234`, `http://localhost:11434`)

### RAG 인덱스 오류
```bash
# 텔레그램에서 직접 실행
/index
```
또는 봇 재시작 (시작 시 `run_startup_recovery()` 자동 실행됨).
