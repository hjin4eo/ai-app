#!/usr/bin/env python3
"""
rag_manager.py — 지식 인덱싱 및 검색 엔진 (VRAM 방어형)
- ChromaDB: 벡터 저장소
- PyMuPDF: PDF 파싱
- Ollama: nomic-embed-text 기반 임베딩
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import urllib.request
from pathlib import Path

import fitz  # PyMuPDF
import chromadb
from chromadb.config import Settings
from langchain_text_splitters import RecursiveCharacterTextSplitter

from bot_config import (
    DATA_DIR,
    EMBEDDING_MODEL,
    KNOWLEDGE_DIR,
    OLLAMA_URL,
    CHUNK_SIZE,
    CHUNK_OVERLAP,
    VISION_NUM_CTX,
)
from bot_utils import ask_ollama
import base64
import time
from functools import wraps

log = logging.getLogger(__name__)

# ── 유틸리티 ──────────────────────────────────────────────────────────────────
def retry(max_retries=3, delay=2):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_err = None
            for i in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_err = e
                    log.warning(f"Retry {i+1}/{max_retries} for {func.__name__}: {e}")
                    time.sleep(delay)
            raise last_err
        return wrapper
    return decorator

@retry(max_retries=3)
def _get_embedding_with_retry(text: str):
    return _get_embedding(text)

@retry(max_retries=3)
def _ask_ollama_with_retry(*args, **kwargs):
    return ask_ollama(*args, **kwargs)

# ── 초기화 ────────────────────────────────────────────────────────────────────
_db_path = DATA_DIR / "chroma_db"
_client = chromadb.PersistentClient(path=str(_db_path))
_collection = _client.get_or_create_collection(
    name="knowledge_base",
    metadata={"hnsw:space": "cosine"}
)

_splitter = RecursiveCharacterTextSplitter(
    chunk_size=CHUNK_SIZE,
    chunk_overlap=CHUNK_OVERLAP,
    length_function=len,
    is_separator_regex=False,
)

# ── 임베딩 호출 (Ollama) ──────────────────────────────────────────────────────
def _get_embedding(text: str) -> list[float]:
    payload = json.dumps({
        "model": EMBEDDING_MODEL,
        "prompt": text,
    }).encode()
    
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/embeddings",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())["embedding"]

def _get_vision_caption(path: Path) -> str:
    """Ollama Vision 모델을 사용하여 이미지 설명을 생성합니다."""
    try:
        with open(path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode("utf-8")
        
        system_prompt = (
            "You are an objective image analyzer. "
            "Describe the objects, text, and features strictly and concisely without conversational filler. "
            "Output in Korean."
        )
        prompt = "이 이미지에서 감지된 모든 객체, 텍스트(OCR), 주요 특징을 목록 형태로 나열해줘."
        
        # 비전 전용 컨텍스트(7k) 사용
        response = _ask_ollama_with_retry(
            prompt, 
            system_prompt, 
            images=[img_b64], 
            num_ctx=VISION_NUM_CTX,
            keep_alive="5m"
        )
        
        enhanced_caption = f"### [이미지 분석 정보]\n- 파일명: {path.name}\n\n{response.strip()}"
        return enhanced_caption
    except Exception as e:
        log.error(f"이미지 분석 실패 ({path.name}): {e}")
        return ""

def _get_embeddings_batch(texts: list[str]) -> list[list[float]]:
    """배치 임베딩 처리 (효율적)"""
    # Ollama /api/embeddings는 현재 단일 prompt만 지원하는 경우가 많으므로 루프 처리
    # (최신 버전은 배치 지원 가능하나 호환성을 위해 개별 호출)
    embeddings = []
    for t in texts:
        embeddings.append(_get_embedding(t))
    return embeddings

def _get_file_hash(path: Path) -> str:
    """파일 내용의 SHA-256 해시를 계산합니다."""
    sha256 = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(8192):
            sha256.update(chunk)
    return sha256.hexdigest()

# ── 공개 API ──────────────────────────────────────────────────────────────────
def index_file(file_path: Path, force: bool = False) -> str:
    """단일 파일을 인덱싱합니다. 이미지/문서 모두 지원."""
    if not file_path.exists():
        return f"파일이 존재하지 않습니다: {file_path.name}"

    file_hash = _get_file_hash(file_path)
    ext = file_path.suffix.lower()
    
    # 1. 기존 데이터 확인 (해시 비교)
    existing = _collection.get(
        where={"source": str(file_path)},
        include=["metadatas"]
    )
    
    if not force and existing["metadatas"]:
        prev_hash = existing["metadatas"][0].get("file_hash")
        if prev_hash == file_hash:
            return f"SKIP: '{file_path.name}'은(는) 이미 최신 상태입니다."

    # 2. 내용 추출
    if ext in (".jpg", ".jpeg", ".png", ".webp"):
        content = _get_vision_caption(file_path)
    else:
        # 문서 파싱 (기존 로직 유지하되 통합)
        try:
            if ext == ".pdf":
                doc = fitz.open(file_path)
                content = "".join(page.get_text() for page in doc)
            elif ext in (".md", ".txt", ".py", ".json", ".yaml", ".yml"):
                content = file_path.read_text(encoding="utf-8", errors="replace")
            else:
                return f"SKIP: 지원하지 않는 확장자 ({ext})"
        except Exception as e:
            return f"ERROR: '{file_path.name}' 읽기 실패: {e}"

    if not content.strip():
        return f"FAIL: '{file_path.name}' 분석 결과가 비어있습니다."

    # 3. 텍스트 분할 및 임베딩 생성
    chunks = _splitter.split_text(content)
    
    # 4. 정밀 ID 기반 고아 데이터 제거 (Expert Tip 반영)
    old_ids = existing["ids"]
    new_count = len(chunks)
    
    # 기존 데이터가 더 많다면 남는 뒷부분 삭제
    if len(old_ids) > new_count:
        ids_to_del = [f"{file_path.name}_{i}" for i in range(new_count, len(old_ids))]
        # 실제 DB에 존재하는지 확인 후 삭제 (오폭 방지)
        _collection.delete(ids=[i for i in ids_to_del if i in old_ids])
        log.info(f"Cleaned up {len(ids_to_del)} orphan chunks for {file_path.name}")

    # 5. Upsert (add 사용 시 기존 ID와 중복되면 오류가 날 수 있으므로 덮어쓰기 로직 필요)
    # ChromaDB add는 중복 ID 시 오류나므로, 안전하게 전체 삭제 후 추가하거나 upsert 사용
    # 여기서는 정석대로 파일명 기반 ID(filename_index)를 생성
    ids = [f"{file_path.name}_{i}" for i in range(len(chunks))]
    metadatas = [{"source": str(file_path), "filename": file_path.name, "file_hash": file_hash} for _ in range(len(chunks))]
    
    try:
        embeddings = []
        for c in chunks:
            embeddings.append(_get_embedding_with_retry(c))
        
        _collection.upsert(
            ids=ids,
            embeddings=embeddings,
            metadatas=metadatas,
            documents=chunks
        )
        return f"SUCCESS: '{file_path.name}' 학습 완료 ({len(chunks)}개 조각)"
    except Exception as e:
        log.error(f"인덱싱 저장 실패 ({file_path.name}): {e}")
        return f"ERROR: '{file_path.name}' 처리 중 오류 발생"

def index_knowledge(include_code: bool = False) -> str:
    """knowledge/ 폴더(및 선택적으로 agent/ 소스)를 인덱싱합니다."""
    if not KNOWLEDGE_DIR.exists():
        KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)

    targets = [KNOWLEDGE_DIR]
    if include_code:
        targets.append(Path(__file__).parent)
    
    results = []
    file_count = 0
    
    for target in targets:
        for file_path in target.rglob("*"):
            if file_path.is_dir() or "venv" in str(file_path) or "__pycache__" in str(file_path):
                continue
            
            if file_path.suffix.lower() not in (".pdf", ".md", ".txt", ".py", ".json", ".yaml", ".yml", ".jpg", ".jpeg", ".png", ".webp"):
                continue
                
            res = index_file(file_path)
            results.append(res)
            if "SUCCESS" in res:
                file_count += 1

    success_count = sum(1 for r in results if "SUCCESS" in r)
    skip_count = sum(1 for r in results if "SKIP" in r)
    fail_count = len(results) - success_count - skip_count
    
    msg = f"✅ 인덱싱 완료: 성공 {success_count}, 스킵 {skip_count}"
    if fail_count > 0:
        msg += f", 실패 {fail_count}"
    return msg

def query_knowledge(query: str, n_results: int = 5) -> str:
    """질문과 가장 관련 있는 지식 조각들을 검색합니다."""
    try:
        query_embedding = _get_embedding(query)
        results = _collection.query(
            query_embeddings=[query_embedding],
            n_results=n_results
        )
        
        if not results or not results["documents"] or not results["documents"][0]:
            return ""
            
        context = "### [로컬 지식 검색 결과]\n"
        for i, doc in enumerate(results["documents"][0]):
            source = results["metadatas"][0][i]["filename"]
            context += f"출처: {source}\n 내용: {doc}\n\n"
        return context
    except Exception as e:
        log.error(f"지식 검색 중 오류: {e}")
        return ""

if __name__ == "__main__":
    # 독립 실행 테스트
    logging.basicConfig(level=logging.INFO)
    print("인덱싱 시작...")
    res = index_knowledge()
    print(res)
    
    print("\n테스트 쿼리: '코드의 구조를 설명해줘'")
    print(query_knowledge("코드의 구조를 설명해줘"))
