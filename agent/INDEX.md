# ai-worker/agent 코드 인덱스

> 최종 갱신: 2026-04-21 (Phase 1 완료 — 개발해봇 분리, 보호 파일, diff 알림)

---

## 🚀 최근 주요 업데이트 (2026-04-21)

| 항목 | 내용 |
|------|------|
| **개발해봇 분리** | `dev_bot.py` 신설 — `/task`, `/merge`, `/reject`, `/diff`, `/log` 개발 전용 봇 |
| **보호 파일 목록** | `loop.py`, `config.yaml` 등 핵심 파일은 AI 태스크가 수정 불가 (3중 차단) |
| **diff 알림** | 태스크 완료 시 변경 파일 목록 + 통계를 텔레그램으로 자동 전송 |
| **push 에러 상세화** | 실패 원인(commit 실패/push 실패/변경없음)을 텔레그램에 정확히 표시 |
| **work_dir 절대경로** | `config.yaml`의 `work_dir: "/home/home/ai-worker"` 고정 |

---

## 🤖 봇 구조

| 봇 | 서비스 | 토큰 환경변수 | 용도 |
|----|--------|--------------|------|
| **알리미** | `telegram-bot` | `TELEGRAM_BOT_TOKEN` | 일반 AI 챗봇, 검색, 날씨, 지식 Q&A |
| **개발해봇** | `dev-bot` | `LOG_TELEGRAM_BOT_TOKEN` | 개발 워크플로우 전용 |

---

## 📖 사용법

### 알리미 (일반 챗봇)
| 명령어 | 설명 |
|--------|------|
| 일반 메시지 | AI 자동 답변 |
| `/search <검색어>` | 웹 검색 + AI 요약 |
| `/ask <질문>` | RAG 지식 검색 |
| `/clip <URL>` | URL 클리핑 → 지식 저장 |
| `/roadmap` | AI 정보 자동화 3단계 로드맵 |
| `/clear` | 대화 기록 초기화 |
| `/reinforce` | 00_Raw → 10_Wiki 구조화 + RAG 재인덱싱 |
| `/reindex` | ChromaDB 초기화 후 전체 재인덱싱 (관리자) |
| `/index` | RAG 인덱스 수동 갱신 (관리자) |
| `/stats` | 위키 현황 통계 |
| `/lint` | 위키 건강 점검 (깨진 링크·고아 탐지) |
| `/status` | 서버 상태 (CPU/RAM/GPU) |
| `/ps` | 프로세스 목록 |
| `/sh <명령>` | 셸 명령 실행 (관리자) |
| `/py <코드>` | Python 코드 실행 (관리자) |
| `/capture` | 스크린샷 캡처 |
| `/service <이름> <start\|stop\|restart>` | 서비스 제어 (관리자) |
| `/restart_runner` | GitHub Actions 러너 재시작 (관리자) |
| `/weather_sync` | 날씨 강제 동기화 |
| `/switch [lm\|ollama]` | 모델 백엔드 전환 (관리자) |
| `/allow <chat_id>` | 사용자 허용 (관리자) |
| `/deny <chat_id>` | 사용자 차단 (관리자) |
| `/users` | 허용 목록 조회 (관리자) |
| `/ci_status` | 최근 CI 실행 결과 |
| `/loop_status` | AI 루프 실행 상태 |

### 개발해봇 (개발 워크플로우)
| 명령어 | 설명 |
|--------|------|
| `/task <작업 설명>` | AI에게 개발 작업 요청 → 브랜치 생성 + 자동 구현 |
| `/task status` | 전체 태스크 현황 (pending/in_progress/completed/failed) |
| `/task status <id>` | 특정 태스크 상세 정보 |
| `/task cancel <id>` | 태스크 취소 + 브랜치 삭제 |
| `/diff <id>` | 태스크 브랜치의 변경 파일 목록 확인 |
| `/merge <id>` | 태스크 브랜치를 master에 머지 + push |
| `/reject <id>` | 태스크 브랜치 삭제 (거부) |
| `/ci_status` | GitHub Actions 최근 5개 CI 결과 |
| `/loop_status` | AI 루프(loop.py) 실행 상태 |
| `/log [n]` | loop.log 최근 n줄 확인 (기본 30) |

### AI 태스크 워크플로우
```
/task <작업 설명>
      │
      ▼
loop.py 감지 (30초 폴링)
      │
      ├─ ai/task-<id> 브랜치 생성
      ├─ AI 코드 생성 (Ollama)
      ├─ 패치 적용 (실패 시 전체 파일 재생성)
      └─ 브랜치 push
            │
            ▼
    📋 변경 내역 텔레그램 전송
            │
      ┌─────┴─────┐
      ▼           ▼
  /merge <id>  /reject <id>
  master 머지   브랜치 삭제
```

---

## 실행 진입점

| 파일 | 역할 | 실행 방식 |
|------|------|----------|
| `telegram_bot.py` | 알리미 메인 | `systemctl start telegram-bot` |
| `dev_bot.py` | 개발해봇 메인 | `systemctl start dev-bot` |
| `discord_bot.py` | 디스코드 봇 | `systemctl start discord-agent` |
| `loop.py` | AI 자동화 루프 | `nohup python loop.py >> ~/loop.log 2>&1 &` |

---

## 폴더 구조

```
agent/
├── telegram_bot.py        ← 알리미 진입점
├── discord_bot.py         ← 디스코드 봇 진입점
├── dev_bot.py             ← 개발해봇 진입점 (Phase 1 신설)
├── loop.py                ← AI 자동화 루프
│
├── core/                  ← 공통 레이어
│   ├── bot_config.py      ← 설정 로드, 상수, 환경변수
│   ├── bot_utils.py       ← LLM 호출, 인증, 대화 기록 (async)
│   ├── shared.py          ← 동기 버전 유틸
│   ├── agent.py           ← 지식 순환 에이전트
│   ├── context_manager.py ← SQLite 기반 대화 컨텍스트
│   ├── models.py          ← LLM 백엔드 추상화 (Claude/OpenAI/Ollama/LMStudio)
│   ├── task_queue.py      ← 파일시스템 기반 AI 태스크 큐 🔒보호
│   └── recovery.py        ← 시작 시 복구/재시도
│
├── handlers/
│   └── bot_commands.py    ← 알리미 명령어 핸들러
│
└── services/
    ├── rag_manager.py     ← ChromaDB 벡터 인덱싱/검색
    ├── weather_service.py ← 기상청 날씨 API
    ├── search_cache.py    ← 네이버 검색 + 캐시
    ├── telegram_notify.py ← 텔레그램 알림 발송
    └── discord_notify.py  ← 디스코드 웹훅 발송
```

---

## 보호 파일 (AI 태스크 수정 불가 🔒)

| 파일 | 이유 |
|------|------|
| `loop.py` | AI 루프 자체 — 수정되면 보호 로직 무력화 |
| `config.yaml` | 핵심 설정 — work_dir 등 덮어쓰면 오동작 |
| `.env` | 시크릿 — 절대 수정 불가 |
| `core/task_queue.py` | 큐 로직 — 수정되면 태스크 유실 |
| `core/bot_config.py` | 설정 로더 — 의존성 높음 |
| `core/models.py` | LLM 추상화 — 수정되면 모든 AI 호출 중단 |

---

## 외부 연동

| 연동 | 경로/URL |
|------|----------|
| 지식 저장소 (RAG) | `/home/home/Jin_lifestyle/10_Wiki` |
| 00_Raw (검색 원본) | `/home/home/Jin_lifestyle/00_Raw` |
| reinforce 파이프라인 | `/home/home/Jin_lifestyle/reinforce.py` |
| ChromaDB | `ai-worker/data/chroma_db/` |
| Ollama | `http://localhost:11434` |
| LM Studio | `http://localhost:1234` |
| GitHub 레포 | `hjin4eo/ai-app` |
| loop.log | `/home/home/loop.log` |
