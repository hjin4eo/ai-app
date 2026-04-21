"""
recovery.py — 시스템 부팅 시 비정상 종료 복구 매니저
- 임시/락 파일 정리
- Circuit Breaker: 파일별 재시도 추적, 한계 초과 시 격리
"""
import json
import shutil
import time
import logging
from pathlib import Path

from bot_config import DATA_DIR, KNOWLEDGE_DIR

log = logging.getLogger(__name__)

# ── 상수 ─────────────────────────────────────────────────────────────────────
ROOT_DIR = Path(__file__).parent.parent
MAX_RETRIES = 3
RETRY_STATE_FILE = DATA_DIR / "retry_state.json"
FAILED_DIR = ROOT_DIR / "00_Raw" / "failed"


class RetryTracker:
    """Circuit Breaker: 파일별 재시도 횟수 추적, 한계 초과 시 격리."""

    def __init__(self):
        self._state: dict[str, int] = {}
        self._load()

    def _load(self):
        if RETRY_STATE_FILE.exists():
            try:
                self._state = json.loads(RETRY_STATE_FILE.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as e:
                log.warning(f"retry_state.json 로드 실패, 초기화: {e}")
                self._state = {}

    def _save(self):
        RETRY_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        RETRY_STATE_FILE.write_text(
            json.dumps(self._state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def record_failure(self, file_path: Path) -> int:
        key = str(file_path)
        self._state[key] = self._state.get(key, 0) + 1
        self._save()
        return self._state[key]

    def record_success(self, file_path: Path):
        key = str(file_path)
        if key in self._state:
            del self._state[key]
            self._save()

    def is_quarantined(self, file_path: Path) -> bool:
        return self._state.get(str(file_path), 0) >= MAX_RETRIES

    def get_retryable_files(self) -> list[str]:
        return [p for p, count in self._state.items() if count < MAX_RETRIES]

    def quarantine_file(self, file_path: Path):
        if not file_path.exists():
            return
        FAILED_DIR.mkdir(parents=True, exist_ok=True)
        dest = FAILED_DIR / file_path.name
        if dest.exists():
            dest = FAILED_DIR / f"{file_path.stem}_{int(time.time())}{file_path.suffix}"
        try:
            shutil.move(str(file_path), str(dest))
            log.info(f"파일 격리 완료: {file_path.name} -> {dest}")
        except Exception as e:
            log.error(f"파일 격리 실패 ({file_path.name}): {e}")


# 모듈 레벨 싱글톤
retry_tracker = RetryTracker()


def run_startup_recovery():
    """시스템 부팅 시 비정상 종료된 작업 복구."""
    log.info("시스템 복구 매니저 가동 중...")

    # 1. 잔류 임시 파일(.tmp) 정리
    count_tmp = 0
    for target_dir in [ROOT_DIR, KNOWLEDGE_DIR, DATA_DIR]:
        for tmp_file in target_dir.rglob("*.tmp"):
            try:
                tmp_file.unlink()
                count_tmp += 1
            except Exception as e:
                log.warning(f"임시 파일 삭제 실패 ({tmp_file}): {e}")
    if count_tmp > 0:
        log.info(f"잔류 임시 파일 {count_tmp}개 정리 완료.")

    # 2. 락 파일(.lock) 정리
    count_lock = 0
    for lock_file in ROOT_DIR.rglob("*.lock"):
        try:
            lock_file.unlink()
            count_lock += 1
        except OSError:
            pass
    if count_lock > 0:
        log.info(f"잔류 락 파일 {count_lock}개 해제 완료.")

    # 3. Circuit Breaker 상태 보고
    retryable = retry_tracker.get_retryable_files()
    quarantined = [p for p, c in retry_tracker._state.items() if c >= MAX_RETRIES]
    if retryable:
        log.info(f"재시도 대상 파일 {len(retryable)}개 (워커 스캔 시 자동 재처리)")
    if quarantined:
        log.warning(f"격리된 파일 {len(quarantined)}개 (수동 확인 필요)")

    log.info("시스템 복구 프로세스 완료.")
