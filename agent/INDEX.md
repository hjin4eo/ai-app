# ai-worker/agent 코드 인덱스

> 최종 갱신: 2026-04-21 (Phase 2 완료 — plan→execute, import 그래프, PR 자동화, 분할 커밋, 자동 검수)

---

## 🚀 최근 주요 업데이트 & 안정화 (2026-04-21)

| 항목 | 내용 |
|------|------|
| **Git 성능 최적화** | 홈 디렉토리 전체가 아닌 `ai-worker` 폴더만 인덱싱하도록 범위 제한 및 `.gitignore` 강화 |
| **Ollama 포트 고정** | LLM 백엔드를 Ollama(11434)로 정렬하고 연결 버그 수정 |
| **고신뢰도 폴백** | 로컬 모델의 패치 생성 실패 시 '전체 파일 쓰기' 모드로 자동 전환하여 성공률 극대화 |
| **CI 감시 최적화** | 권한 에러(401)가 발생하는 CI 루프를 정리하여 채팅 태스크 처리 속도 개선 |
| **개발해봇 분리** | `dev_bot.py` 신설 — `/task`, `/merge`, `/reject`, `/diff`, `/log` 개발 전용 봇 (`LOG_TELEGRAM_BOT_TOKEN`) |
| **보호 파일 목록** | `loop.py`, `config.yaml` 등 핵심 파일은 AI 태스크가 수정 불가 (3중 차단) |
| **diff 알림** | 태스크 완료 시 변경 파일 목록 + 통계를 텔레그램으로 자동 전송 |
| **push 에러 상세화** | 실패 원인(commit 실패/push 실패/변경없음)을 텔레그램에 정확히 표시 |
| **Phase 2 — plan→execute** | `plan_changes()` 로 관련 파일 + 구현 방향 먼저 결정 후 패치 생성 |
| **Phase 2 — import 그래프** | `context_selector.py` — ast 파싱 BFS로 의존 파일 자동 발견 (최대 2hop) |
| **Phase 2 — PR 자동화** | push 후 GitHub PR 자동 생성, `/merge`는 squash merge |
| **Phase 2 — 분할 커밋** | 변경 파일을 타입/스코프별 atomic commit으로 자동 분리 |
| **Phase 2 — 자동 검수** | 패치 적용 후 `tests/runner.py` 자동 실행, 실패 시 스크린샷 텔레그램 전송 |

---

## 🗺 향후 로드맵: AI 정보 자동화 파이프라인 (3단계)

현진님의 '자동화 우선' 원칙에 따라 구축될 지식 큐레이션 시스템의 단계별 계획입니다.

### 🥇 1단계: 데이터 수집 엔진 구축 (MVP)
- **목표**: 외부 AI 트렌드 소스(Meta, Anthropic 등)를 자동으로 크롤링하여 `00_Raw/`에 저장.
- **주요 모듈**: `core/scraper.py` (신규), `config.yaml` 소스 리스트업.

### 🥈 2단계: 지식 통합 및 구조화 (Core Automation)
- **목표**: 수집된 원본을 LLM으로 요약하고 `10_Wiki/synthesis/`에 지식으로 통합.
- **핵심 로직**: `reinforce.py` 확장 및 맞춤형 요약 프롬프트 적용.

### 🥉 3단계: 배포 및 최적화 (Refinement & Scaling)
- **목표**: 매일 요약된 지식을 텔레그램으로 브리핑하고, 개인 학습(전기기사 등) 맞춤형 연결.
- **알림 기능**: `bot_config.py` 확장 및 일일 자동 브리핑 봇 가동.

---

## 🤖 봇 구조

| 봇 | 서비스 | 토큰 환경변수 | 용도 |
|----|--------|--------------|------|
| **알리미** | `telegram-bot` | `TELEGRAM_BOT_TOKEN` | 일반 AI 챗봇, 검색, 날씨, 지식 Q&A |
| **개발해봇** | `dev-bot` | `LOG_TELEGRAM_BOT_TOKEN` | 개발 워크플로우 전용 |

### 개발해봇 명령어 (`dev_bot.py`)
| 명령어 | 설명 |
|--------|------|
| `/task <작업 설명>` | AI에게 개발 작업 요청 → 브랜치 생성 + 자동 구현 |
| `/task status [id]` | 태스크 현황 조회 |
| `/task cancel <id>` | 태스크 취소 + 브랜치 삭제 |
| `/diff <id>` | 태스크 브랜치 변경 파일 확인 |
| `/merge <id>` | 태스크 브랜치를 master에 머지 |
| `/reject <id>` | 태스크 브랜치 삭제 (거부) |
| `/ci_status` | GitHub Actions 최근 CI 결과 |
| `/loop_status` | AI 루프 실행 상태 |
| `/log [n]` | loop.log 최근 n줄 (기본 30) |

### AI 태스크 워크플로우 (Phase 2)
```
/task <작업 설명>
      │
      ▼
loop.py 감지 (30초 폴링)
      │
      ├─ 1. ai/task-<id> 브랜치 생성
      ├─ 2. plan_changes() — 관련 파일 + 구현 방향 결정
      ├─ 3. select_context() — import 그래프 BFS 컨텍스트 선택
      ├─ 4. generate_patch() — AI 패치 생성
      ├─ 5. apply_patch() — 적용 (실패 시 generate_code() 폴백)
      ├─ 6. tests/runner.py 자동 검수 (HEADLESS=1)
      │       ├─ 실패 → 스크린샷 텔레그램 전송 → 재시도
      │       └─ 통과 ↓
      ├─ 7. commit_in_groups() — 타입/스코프별 분할 커밋 + push
      └─ 8. GitHub PR 자동 생성
            │
            ▼
    ✅ PR URL + /merge 안내 텔레그램 전송
    📋 변경 파일 목록 전송
            │
      ┌─────┴─────┐
      ▼           ▼
  /merge <id>  /reject <id>
  PR squash    브랜치 삭제
  머지 (API)
```

---

## 실행 진입점

| 파일 | 역할 |
|------|------|
| `telegram_bot.py` | 알리미 메인 (systemd: `telegram-bot`) |
| `dev_bot.py` | 개발해봇 메인 (systemd: `dev-bot`) |
| `discord_bot.py` | 디스코드 봇 메인 |
| `loop.py` | 백그라운드 자동화 루프 |

---

## 폴더 구조

```
agent/
├── telegram_bot.py        ← 알리미 진입점
├── dev_bot.py             ← 개발해봇 진입점 (Phase 1 신설)
├── discord_bot.py         ← 디스코드 봇 진입점
├── loop.py                ← 자동화 루프
│
├── core/                  ← 최하단 공통 레이어 (의존성 없음)
│   ├── bot_config.py      ← 설정 로드, 상수, 환경변수 (133줄)
│   ├── bot_utils.py       ← LLM 호출, 인증, 대화 기록 (586줄)
│   ├── shared.py          ← 동기 버전 유틸 (493줄)
│   ├── agent.py           ← 지식 순환 에이전트 (446줄)
│   ├── context_manager.py ← DB 기반 대화 컨텍스트 관리 (198줄)
│   ├── models.py          ← LLM 백엔드 추상화 + plan_changes() 🔒
│   ├── context_selector.py← import 그래프 BFS 컨텍스트 선택 (Phase 2)
│   ├── task_queue.py      ← 파일시스템 기반 AI 태스크 큐 🔒
│   └── recovery.py        ← 시작 시 복구/재시도 로직 (116줄)
│
├── handlers/
│   └── bot_commands.py    ← 알리미 명령어 핸들러 전체
│
├── services/              ← 외부 서비스 연동
│   ├── rag_manager.py     ← ChromaDB 벡터 인덱싱/검색 (302줄)
│   ├── weather_service.py ← 기상청 날씨 API (396줄)
│   ├── search_cache.py    ← 네이버 검색 + 캐시 (126줄)
│   ├── web_clipper.py     ← URL 클리핑 (옵시디언 양식 호환)
│   ├── telegram_notify.py ← 텔레그램 알림 발송 유틸 (106줄)
│   └── discord_notify.py  ← 디스코드 웹훅 발송 유틸 (107줄)
│
└── scripts/               ← 단독 실행 스크립트 (테스트/점검용)
    ├── request_handler.py
    ├── final_bot_test.py
    ├── check_integrated_weather.py
    └── verify_time.py
```

---

## 🔒 보호 파일 (AI 태스크 수정 불가)

| 파일 | 이유 |
|------|------|
| `loop.py` | AI 루프 자체 — 수정되면 보호 로직 무력화 |
| `config.yaml` | 핵심 설정 — work_dir 등 덮어쓰면 오동작 |
| `.env` | 시크릿 |
| `core/task_queue.py` | 큐 로직 — 수정되면 태스크 유실 |
| `core/bot_config.py` | 설정 로더 |
| `core/models.py` | LLM 추상화 — 수정되면 모든 AI 호출 중단 |

---

## core/

### `bot_config.py` — 설정 로드
| 항목 | 내용 |
|------|------|
| `CFG` | config.yaml 전체 딕셔너리 |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | 텔레그램 인증 |
| `OLLAMA_MODEL` / `MODEL_BACKEND` | LLM 백엔드 설정 |
| `KNOWLEDGE_DIR` | RAG 지식 저장소 경로 (`/home/home/Jin_lifestyle/10_Wiki`) |
| `RAG_ENABLED` / `CHUNK_SIZE` / `EMBEDDING_MODEL` | RAG 파라미터 |
| `load_allowed_users()` | 허용 사용자 동적 로드 |

### `bot_utils.py` — LLM 호출 + 인증 + 대화 기록 (async)
| 함수 | 설명 |
|------|------|
| `_load_context_prompt()` | CLAUDE.md + 나의핵심맥락.md 로드 |
| `_admin(update)` | 관리자 여부 확인 |
| `_auth(update)` | 허용 사용자 여부 확인 (async) |
| `_send_long(update, text)` | 4000자 초과 메시지 분할 전송 |
| `_strip_tags(text)` | HTML 태그 제거 |
| `_escape_html(text)` | HTML 이스케이프 |
| `save_chat(user_id, role, content)` | DB에 대화 저장 |
| `load_context(chat_id)` | DB에서 대화 + 요약 로드 |
| `ask_ollama(prompt, ...)` | LLM 호출 (backend 자동 선택) |
| `ask_ollama_with_tools(messages, ...)` | 툴콜링 루프 (파일 읽기/쓰기) |
| `safe_summarize(chat_id)` | 대화 요약 생성 |
| `check_and_summarize(chat_id)` | 임계치 초과 시 자동 요약 |

### `agent.py` — 지식 순환 에이전트
| 항목 | 설명 |
|------|------|
| `class Agent` | RAG → 외부검색 → 00_Raw 저장 → reinforce 트리거 |
| `Agent.process_query(query)` | 쿼리 처리 메인 (RAG 히트/미스 라우팅) |
| `Agent._fetch_external_data(query)` | 네이버/Wikipedia/arXiv/Semantic Scholar/PubMed 병렬 수집 |
| `Agent._extract_and_save(query, raw)` | `00_Raw/` 저장 후 reinforce 백그라운드 트리거 |
| `Agent._run_reinforce()` | `Jin_lifestyle/reinforce.py` 실행 |
| `get_agent()` | 싱글톤 Agent 반환 |

### `context_manager.py` — 대화 컨텍스트 DB
| 항목 | 설명 |
|------|------|
| `class ContextManager` | SQLite 기반 대화 기록 + 요약 관리 |
| `get_context(chat_id, limit)` | 최근 메시지 + 요약 반환 |
| `context_manager_db` | 전역 싱글톤 인스턴스 |

### `models.py` — LLM 백엔드 추상화 🔒
| 클래스/함수 | 설명 |
|------------|------|
| `BaseModel` | 추상 기반 클래스 |
| `ClaudeModel` | Anthropic API |
| `OpenAIModel` | OpenAI API |
| `LlamaCppModel` | LM Studio (OpenAI 호환) |
| `OllamaModel` | Ollama |
| `get_model(cfg)` | config 기반 백엔드 선택 |
| `.plan_changes(request)` | Phase 2 — `{"files":[...], "approach":"..."}` 반환 |

### `context_selector.py` — import 그래프 컨텍스트 선택 (Phase 2)
| 함수 | 설명 |
|------|------|
| `select_context(plan, work_dir, protected_check)` | plan 파일 → BFS 관련 파일 → 스캔 순 컨텍스트 구성 |
| `build_import_graph(work_dir, protected_check)` | ast 파싱으로 레포 내 Python import 그래프 빌드 |
| `find_related(seed_files, graph, max_hop)` | BFS로 직접/간접 의존 파일 탐색 (기본 2hop) |

### `recovery.py` — 시작 시 복구
| 항목 | 설명 |
|------|------|
| `class RetryTracker` | 재시도 횟수 추적 |
| `run_startup_recovery()` | 봇 시작 시 상태 복구 실행 |

### `shared.py` — 동기 버전 유틸 (discord_bot 등에서 사용)
`bot_utils.py`의 동기 버전. 함수 시그니처 동일하나 `async` 없음.

---

## handlers/

### `bot_commands.py` — 알리미 명령어 핸들러
| 명령어 | 함수 | 설명 |
|--------|------|------|
| `/allow` | `cmd_allow` | 사용자 허용 (관리자) |
| `/deny` | `cmd_deny` | 사용자 차단 (관리자) |
| `/users` | `cmd_users` | 허용 목록 조회 |
| `/index` | `cmd_index` | 지식 수동 인덱싱 |
| `/roadmap` | `cmd_roadmap` | AI 정보 자동화 로드맵 |
| `/ask` | `cmd_ask` | RAG + graph 탐색 질문 답변 (출처 명시) |
| `/stats` | `cmd_stats` | 위키 현황 통계 (페이지·엣지·카테고리·최근이력) |
| `/lint` | `cmd_lint` | 위키 건강 점검 (깨진 링크·고아·미등록 탐지) |
| `/status` | `cmd_status` | CPU/RAM/GPU 상태 |
| `/sh` | `cmd_sh` | 셸 명령 실행 (관리자) |
| `/py` | `cmd_py` | Python 코드 실행 (관리자) |
| `/ps` | `cmd_ps` | 프로세스 목록 |
| `/service` | `cmd_service` | 서비스 재시작/조회 |
| `/capture` | `cmd_capture` | 스크린샷 캡처 |
| `/restart_runner` | `cmd_restart_runner` | GitHub Actions 러너 재시작 (관리자) |
| `/weather_sync` | `cmd_weather_sync` | 날씨 강제 동기화 |
| `/reinforce` | `cmd_reinforce` | 00_Raw → 10_Wiki 구조화 + RAG 재인덱싱 |
| `/reindex` | `cmd_reindex` | ChromaDB 초기화 후 전체 재인덱싱 (관리자) |
| `/search` | `cmd_search` | 웹 검색 + AI 요약 |
| `/clear` | `cmd_clear` | 대화 기록 초기화 |
| `/switch` | `cmd_switch` | LLM 백엔드 전환 (ollama ↔ lm-studio) |
| `/clip` | `cmd_clip` | URL 클리핑 → 옵시디언 양식으로 00_Raw 저장 + reinforce |
| `/task` | `cmd_task` | AI 태스크 등록·조회·취소 (큐 기반) |
| `/ci_status` | `cmd_ci_status` | 최근 CI 실행 결과 |
| `/loop_status` | `cmd_loop_status` | AI 루프 실행 상태 |
| — | `weather_sync_loop` | 날씨 백그라운드 자동 동기화 루프 |

---

## services/

### `rag_manager.py` — ChromaDB 벡터 인덱싱/검색
| 함수 | 설명 |
|------|------|
| `index_file(path, force)` | 단일 파일 인덱싱 (SHA-256 변경 감지) |
| `index_knowledge(include_code)` | 전체 지식 폴더 일괄 인덱싱 |
| `query_knowledge(query, n, threshold)` | 시맨틱 검색 (코사인 유사도) |
| `reset_and_reindex()` | 컬렉션 드롭 후 전체 재인덱싱 |
| `_get_embedding(text)` | Ollama/LM Studio 임베딩 호출 |
| `_get_vision_caption(path)` | 이미지 Vision 캡션 생성 |

**인덱싱 대상**: `KNOWLEDGE_DIR` (`Jin_lifestyle/10_Wiki`) + `include_code=True` 시 소스코드 포함

### `wiki_navigator.py` — Jin_lifestyle 위키 구조 탐색기
| 함수 | 설명 |
|------|------|
| `load_graph()` | `20_Meta/graph.json` 로드 |
| `get_related_pages(title, n)` | graph에서 연결된 노드 n개 반환 |
| `parse_index()` | `10_Wiki/index.md` 파싱 → 카테고리별 딕셔너리 |
| `tail_log(n)` | `10_Wiki/log.md` 최근 n줄 |
| `find_wiki_file(title)` | `10_Wiki/**/{title}.md` 탐색 |
| `count_raw_pending()` | 미처리 00_Raw 파일 수 |
| `wiki_stats()` | 위키 전체 현황 딕셔너리 (`/stats` 명령에서 사용) |

### `weather_service.py` — 기상청 날씨
| 함수 | 설명 |
|------|------|
| `sync_city(city)` | 특정 도시 날씨 동기화 |
| `sync_all()` | 전체 지원 도시 동기화 |
| `get_cached_weather(query)` | 캐시된 날씨 반환 |
| `fetch_weather(nx, ny)` | 기상청 격자 API 호출 |
| `fetch_air_quality(station)` | 대기질 API 호출 |

**지원 도시**: `SUPPORTED_CITIES` (core/shared.py 정의)

### `search_cache.py` — 네이버 검색
| 함수 | 설명 |
|------|------|
| `search_naver(query, max_results)` | 네이버 검색 API 호출 + TTL 캐시 |
| `clear_expired()` | 만료 캐시 삭제 |

### `telegram_notify.py` / `discord_notify.py` — 알림 발송
텔레그램/디스코드로 텍스트·파일 발송. 봇 외부에서 단독 호출 가능.

---

## tests/ — 자동 검수 (Phase 2 Step 5)

| 파일 | 설명 |
|------|------|
| `tests/runner.py` | Playwright 테스트 실행기. `HEADLESS=1` 시 headless 모드. 실패 시 `/tmp/test_fail_*.png` 스크린샷 |
| `tests/suites/__init__.py` | 스위트 디렉토리 |
| `tests/suites/smoke.py` | 기본 스모크 테스트 — 앱 응답 + 500 에러 여부 확인 |

**loop.py 연동**: 패치 적용 후 `_run_tests()` 호출 → 실패 시 스크린샷 텔레그램 전송 + 재시도, 통과 시 커밋/PR 진행.

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

---

## 데이터 흐름

```
텔레그램 메시지
    │
    ├─ 날씨 키워드 → weather_service → 캐시/API
    │
    ├─ RAG 키워드 → rag_manager.query_knowledge()
    │                    ├─ 히트 → 즉답
    │                    └─ 미스 → Agent.process_query()
    │                                  └─ 외부 수집 (Naver/Wiki/arXiv...)
    │                                        └─ 00_Raw/ 저장
    │                                              └─ reinforce.py 트리거
    │                                                    └─ 10_Wiki/ 구조화
    │                                                          └─ ChromaDB 갱신
    │
    └─ 일반 대화 → ask_ollama() → LLM 응답
```
