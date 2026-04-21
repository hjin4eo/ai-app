#!/usr/bin/env python3
"""
rag_manager.py — Local Knowledge Indexing & Retrieval Engine (VRAM Optimized)
Manages document embeddings and vector storage using ChromaDB and Ollama.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import sys
from pathlib import Path
from typing import List

import aiohttp
import chromadb
import fitz  # PyMuPDF
from langchain_text_splitters import RecursiveCharacterTextSplitter
_agent_dir = Path(__file__).parent.parent.resolve()
if str(_agent_dir) not in sys.path:
    sys.path.append(str(_agent_dir))

from core.bot_config import (
    DATA_DIR,
    EMBEDDING_BACKEND,
    EMBEDDING_MODEL,
    EMBEDDING_URL,
    KNOWLEDGE_DIR,
    CHUNK_SIZE,
    CHUNK_OVERLAP,
    VISION_NUM_CTX,
)
from core.bot_utils import ask_ollama

log = logging.getLogger(__name__)

# ── Utilities ─────────────────────────────────────────────────────────────

async def _get_embedding(text: str) -> List[float]:
    """
    Fetches embedding vector from configured backend (Ollama or LM Studio).
    """
    async with aiohttp.ClientSession() as session:
        if EMBEDDING_BACKEND == "lm-studio":
            # OpenAI 호환 형식 (/v1/embeddings)
            payload = {"model": EMBEDDING_MODEL, "input": text}
            async with session.post(f"{EMBEDDING_URL}/v1/embeddings", json=payload, timeout=30) as resp:
                if resp.status != 200:
                    error_body = await resp.text()
                    log.error(f"Embedding error {resp.status}: {error_body}")
                    raise Exception(f"Embedding failed with status {resp.status}")
                data = await resp.json()
                return data["data"][0]["embedding"]
        else:
            # Ollama 형식 (/api/embeddings)
            payload = {"model": EMBEDDING_MODEL, "prompt": text}
            async with session.post(f"{EMBEDDING_URL}/api/embeddings", json=payload, timeout=30) as resp:
                if resp.status != 200:
                    error_body = await resp.text()
                    log.error(f"Embedding error {resp.status}: {error_body}")
                    raise Exception(f"Embedding failed with status {resp.status}")
                data = await resp.json()
                return data["embedding"]

async def _get_vision_caption(path: Path) -> str:
    """
    Generates a descriptive caption for an image using the Ollama Vision model.
    """
    try:
        # Use to_thread for file I/O
        content = await asyncio.to_thread(path.read_bytes)
        img_b64 = base64.b64encode(content).decode("utf-8")
        
        system_prompt = (
            "You are an objective image analyzer. Describe objects, text, and features strictly and concisely. "
            "Output in Korean."
        )
        prompt = "이 이미지에서 감지된 모든 객체, 텍스트(OCR), 주요 특징을 목록 형태로 나열해줘."
        
        response = await ask_ollama(
            prompt, 
            system_prompt, 
            images=[img_b64], 
            num_ctx=VISION_NUM_CTX,
            keep_alive="5m"
        )
        
        return f"### [이미지 분석 정보]\n- 파일명: {path.name}\n\n{response.strip()}"
    except Exception as e:
        log.error(f"Vision analysis failed ({path.name}): {e}")
        return ""

async def _get_file_hash(path: Path) -> str:
    """Calculates SHA-256 hash of file contents asynchronously."""
    def _calc():
        sha256 = hashlib.sha256()
        with open(path, "rb") as f:
            while chunk := f.read(8192):
                sha256.update(chunk)
        return sha256.hexdigest()
    return await asyncio.to_thread(_calc)

# ── Initialization ─────────────────────────────────────────────────────────

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

# ── Public API ─────────────────────────────────────────────────────────────

async def index_file(file_path: Path, force: bool = False) -> str:
    """
    Indexes a single file (image or document) into the vector store.
    """
    if not file_path.exists():
        return f"File does not exist: {file_path.name}"

    file_hash = await _get_file_hash(file_path)
    ext = file_path.suffix.lower()
    
    # Check for existing data via hash
    existing = _collection.get(
        where={"source": str(file_path)},
        include=["metadatas"]
    )
    
    if not force and existing["metadatas"]:
        prev_hash = existing["metadatas"][0].get("file_hash")
        if prev_hash == file_hash:
            return f"SKIP: '{file_path.name}' is up to date."

    # Extract content
    try:
        if ext in (".jpg", ".jpeg", ".png", ".webp"):
            content = await _get_vision_caption(file_path)
        elif ext == ".pdf":
            def _parse_pdf():
                doc = fitz.open(file_path)
                return "".join(page.get_text() for page in doc)
            content = await asyncio.to_thread(_parse_pdf)
        elif ext in (".md", ".txt", ".py", ".json", ".yaml", ".yml"):
            content = await asyncio.to_thread(file_path.read_text, encoding="utf-8", errors="replace")
        else:
            return f"SKIP: Unsupported extension ({ext})"
    except Exception as e:
        return f"ERROR: Failed to read '{file_path.name}': {e}"

    if not content.strip():
        return f"FAIL: Content extraction yielded empty result for '{file_path.name}'"

    # Split text and generate embeddings
    chunks = _splitter.split_text(content)
    
    # Clear orphans from previous indexing of this file
    old_ids = existing["ids"]
    new_count = len(chunks)
    if len(old_ids) > new_count:
        ids_to_del = [f"{file_path.name}_{i}" for i in range(new_count, len(old_ids))]
        _collection.delete(ids=[i for i in ids_to_del if i in old_ids])

    # Generate embeddings and upsert
    try:
        embeddings = []
        for c in chunks:
            embeddings.append(await _get_embedding(c))
        
        ids = [f"{file_path.name}_{i}" for i in range(len(chunks))]
        metadatas = [{"source": str(file_path), "filename": file_path.name, "file_hash": file_hash} for _ in range(len(chunks))]
        
        _collection.upsert(
            ids=ids,
            embeddings=embeddings,
            metadatas=metadatas,
            documents=chunks
        )
        return f"SUCCESS: Indexed '{file_path.name}' ({len(chunks)} chunks)"
    except Exception as e:
        log.error(f"Indexing storage failed ({file_path.name}): {e}")
        return f"ERROR: Storage failed for '{file_path.name}'"

async def index_knowledge(include_code: bool = False) -> str:
    """
    Indexes the knowledge directory and optionally the source code.
    """
    if not KNOWLEDGE_DIR.exists():
        KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)

    targets = [KNOWLEDGE_DIR]
    if include_code:
        targets.append(Path(__file__).parent.parent)
    
    results = []
    for target in targets:
        for file_path in target.rglob("*"):
            if file_path.is_dir() or any(p in str(file_path) for p in ["venv", "__pycache__", ".git"]):
                continue
            
            if file_path.suffix.lower() not in (
                ".pdf", ".md", ".txt", ".py", ".json", ".yaml", ".yml", 
                ".jpg", ".jpeg", ".png", ".webp"
            ):
                continue
                
            res = await index_file(file_path)
            results.append(res)

    success_count = sum(1 for r in results if "SUCCESS" in r)
    skip_count = sum(1 for r in results if "SKIP" in r)
    fail_count = len(results) - success_count - skip_count
    
    msg = f"✅ Indexing complete: Success {success_count}, Skip {skip_count}"
    if fail_count > 0:
        msg += f", Fail {fail_count}"
    return msg

async def query_knowledge(query: str, n_results: int = 5, distance_threshold: float = 0.22) -> str:
    """
    Retrieves the most relevant knowledge pieces for a given query.
    distance_threshold: 코사인 거리 상한선 (낮을수록 엄격, 기본 0.45)
                        거리 0 = 완전 일치, 거리 1 = 완전 무관
    """
    try:
        query_embedding = await _get_embedding(query)
        results = await asyncio.to_thread(lambda: _collection.query(
            query_embeddings=[query_embedding],
            n_results=n_results,
            include=["documents", "metadatas", "distances"],
        ))

        if not results or not results["documents"] or not results["documents"][0]:
            return ""

        # 유사도 필터: 임계값 초과 문서 제거
        filtered_docs = []
        for i, doc in enumerate(results["documents"][0]):
            dist = results["distances"][0][i]
            if dist <= distance_threshold:
                source = results["metadatas"][0][i]["filename"]
                filtered_docs.append((source, doc, dist))

        if not filtered_docs:
            log.debug(f"RAG: 쿼리 '{query[:30]}' — 관련 문서 없음 (min_dist={results['distances'][0][0]:.3f} > {distance_threshold})")
            return ""

        context = "### [로컬 지식 검색 결과]\n"
        for source, doc, dist in filtered_docs:
            context += f"출처: {source} (유사도: {1-dist:.2f})\n 내용: {doc}\n\n"
        return context

    except Exception as e:
        log.error(f"Knowledge search error: {e}")
        return ""


if __name__ == "__main__":
    # Test execution
    logging.basicConfig(level=logging.INFO)
    async def _test():
        print("Starting indexing...")
        res = await index_knowledge()
        print(res)
        print("\nTest Query: 'Structure analysis'")
        print(await query_knowledge("코드 구조"))
    
    asyncio.run(_test())

if __name__ == "__main__":
    # 독립 실행 테스트
    logging.basicConfig(level=logging.INFO)
    print("인덱싱 시작...")
    res = index_knowledge()
    print(res)
    
    print("\n테스트 쿼리: '코드의 구조를 설명해줘'")
    print(query_knowledge("코드의 구조를 설명해줘"))
