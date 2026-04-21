#!/usr/bin/env python3
"""
agent.py — 지식 순환 에이전트 (ai-worker 통합)

워크플로우:
  query
    │
    ├─[1] RagManager.query_knowledge()  ── ChromaDB 시맨틱 검색
    │         ├── 결과 있음 ──▶ 즉시 context 반환
    │         └── 결과 없음
    │                │
    │               [2] _fetch_external_data()  ── 전체 소스 병렬 수집
    │                     ├─ Naver 웹 검색 API
    │                     ├─ Wikipedia KO API
    │                     ├─ arXiv (CS/Physics/Math 논문)
    │                     ├─ Semantic Scholar (학술 논문 전반)
    │                     └─ PubMed (의학/생물학 논문)
    │                          │
    │                          ├── [BG] asyncio.create_task(_extract_and_save())
    │                          │         └─▶ Ollama 구조화 → KB 파일 저장 → RAG 재인덱싱
    │                          │
    │                          └── combined_raw → 즉시 응답
    │
    └── (전체 실패 시) "" 반환
"""
from __future__ import annotations

import asyncio
import datetime
import logging
import re
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

import httpx

from .bot_config import (
    KNOWLEDGE_DIR,
    NAVER_CLIENT_ID,
    NAVER_CLIENT_SECRET,
)
from .bot_utils import ask_ollama
from services import rag_manager

log = logging.getLogger(__name__)

_HEADERS = {"User-Agent": "ai-worker/1.0 (https://github.com/home/ai-worker)"}
_TIMEOUT = 10.0


class Agent:
    """
    RAG 검색이 실패할 때 외부 데이터를 수집·구조화·저장하여 지식 공백을 채우는 에이전트.

    핵심 설계:
    - 모든 외부 소스를 asyncio.gather()로 병렬 수집 → 속도 최적화
    - 결과는 즉시 LLM 컨텍스트로 전달 → 빠른 응답
    - _extract_and_save()는 백그라운드 태스크 → 다음 질문부터 KB 활용
    """

    # ── 가격/상품 쿼리 감지 ─────────────────────────────────────────────
    _PRICE_KEYWORDS = [
        # 가격 직접 표현
        "가격", "최저가", "최고가", "얼마", "얼마야", "얼마예", "얼마임", "얼마에요",
        "정가", "할인", "판매", "구매", "사고싶은", "사려고",
        "price", "buy", "shop", "커머스",
        # 하드웨어 부품 (가격 문맥이 없어도 쇼핑 API 유용)
        "램", "ram", "ddr", "ssd", "hdd", "nvme", "gpu", "그래픽카드",
        "cpu", "프로세서", "메모리", "저장장치", "파워", "케이스", "쿨러",
        "마더보드", "메인보드",
    ]

    def _is_price_query(self, query: str) -> bool:
        """가격·구매 관련 쿼리인지 판별."""
        return any(kw in query for kw in self._PRICE_KEYWORDS)

    async def process_query(self, query: str) -> str:
        """
        쿼리를 받아 지식 순환 주기를 실행합니다.
        Returns: knowledge_context 문자열 (빈 문자열이면 지식 없음)
        """
        # ── Step 1: ChromaDB 벡터 검색 ─────────────────────────────────────
        knowledge_context = await rag_manager.query_knowledge(query)
        if knowledge_context:
            log.debug(f"[Agent] RAG 히트: '{query[:30]}'")
            return knowledge_context

        # ── Step 1.5: 가격 쿼리 → 쇼핑 API 우선 실행 ──────────────────────
        if self._is_price_query(query):
            shopping_result = await self._fetch_naver_shopping(query)
            if shopping_result:
                asyncio.create_task(self._extract_and_save(query, shopping_result))
                return f"### [쇼핑 가격 정보 — Naver Shopping | 신규 학습 중]\n{shopping_result}"

        # ── Step 2: 전체 외부 소스 병렬 수집 ──────────────────────────────
        combined, sources = await self._fetch_external_data(query)
        if not combined:
            log.info(f"[Agent] 외부 검색 전체 실패: '{query[:30]}'")
            return ""

        log.info(f"[Agent] 외부 수집 성공 [{', '.join(sources)}]: '{query[:30]}'")

        # ── Step 3: 백그라운드 저장·재인덱싱 (즉시 응답 차단하지 않음) ────
        asyncio.create_task(self._extract_and_save(query, combined))

        # ── Step 4: 원본 데이터를 컨텍스트로 즉시 반환 ───────────────────
        source_label = " | ".join(sources)
        return f"### [외부 검색 결과 — {source_label} | 신규 학습 중]\n{combined}"

    # ── 외부 데이터 수집 (병렬) ───────────────────────────────────────────

    async def _fetch_external_data(self, query: str) -> tuple[str, list[str]]:
        """
        모든 외부 소스를 병렬로 수집하고 결과를 합칩니다.
        Returns: (combined_text, [source_names])
        """
        results = await asyncio.gather(
            self._fetch_naver_shopping(query),   # 쇼핑(가격) 우선
            self._fetch_naver(query),
            self._fetch_wikipedia(query),
            self._fetch_arxiv(query),
            self._fetch_semantic_scholar(query),
            self._fetch_pubmed(query),
            return_exceptions=True,
        )

        source_names = ["NaverShopping", "Naver", "Wikipedia", "arXiv", "SemanticScholar", "PubMed"]
        parts: list[str] = []
        active_sources: list[str] = []

        for name, result in zip(source_names, results):
            if isinstance(result, Exception):
                log.warning(f"[Agent] {name} 예외: {result}")
                continue
            if result and result.strip():
                parts.append(f"[{name}]\n{result}")
                active_sources.append(name)

        return "\n\n".join(parts), active_sources

    # ── 개별 소스 ─────────────────────────────────────────────────────────

    async def _fetch_naver_shopping(self, query: str) -> str:
        """
        Naver 쇼핑 검색 API (shop.json).
        lprice(최저가), hprice(최고가), mallName, brand, maker, category 반환.
        가격 없음(0) 항목은 제외.
        """
        if not NAVER_CLIENT_ID or not NAVER_CLIENT_SECRET:
            return ""
        try:
            url = (
                "https://openapi.naver.com/v1/search/shop.json"
                f"?query={urllib.parse.quote(query)}&display=5&sort=sim"
                "&exclude=used:cbshop"   # 중고·해외직구 제외
            )
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.get(url, headers={
                    "X-Naver-Client-Id":     NAVER_CLIENT_ID,
                    "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
                })
                resp.raise_for_status()
                items = resp.json().get("items", [])

            if not items:
                return ""

            lines = []
            for item in items[:5]:
                title  = re.sub(r"<[^>]+>", "", item.get("title", ""))
                lprice = int(item.get("lprice") or 0)
                hprice = int(item.get("hprice") or 0)
                mall   = item.get("mallName", "")
                brand  = item.get("brand", "")
                maker  = item.get("maker", "")
                cat    = " > ".join(filter(None, [
                    item.get("category1", ""),
                    item.get("category2", ""),
                    item.get("category3", ""),
                ]))

                # 가격 포맷
                if lprice and hprice and lprice != hprice:
                    price_str = f"{lprice:,}원 ~ {hprice:,}원"
                elif lprice:
                    price_str = f"{lprice:,}원~"
                else:
                    continue  # 가격 정보 없는 항목 스킵

                meta = " | ".join(filter(None, [brand or maker, mall, cat]))
                lines.append(f"- {title}: {price_str}  ({meta})")

            return "\n".join(lines) if lines else ""
        except Exception as e:
            log.warning(f"[Agent] Naver Shopping 오류: {e}")
            return ""

    async def _fetch_naver(self, query: str) -> str:
        """Naver 웹 검색 API."""
        if not NAVER_CLIENT_ID or not NAVER_CLIENT_SECRET:
            return ""
        try:
            url = (
                "https://openapi.naver.com/v1/search/webkr.json"
                f"?query={urllib.parse.quote(query)}&display=5"
            )
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.get(url, headers={
                    "X-Naver-Client-Id":     NAVER_CLIENT_ID,
                    "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
                })
                resp.raise_for_status()
                items = resp.json().get("items", [])

            lines = []
            for item in items[:3]:
                title = re.sub(r"<[^>]+>", "", item.get("title", ""))
                desc  = re.sub(r"<[^>]+>", "", item.get("description", ""))
                lines.append(f"- {title}: {desc}")
            return "\n".join(lines)
        except Exception as e:
            log.warning(f"[Agent] Naver 오류: {e}")
            return ""

    async def _fetch_wikipedia(self, query: str) -> str:
        """Wikipedia 한국어 API (summary → 검색 순)."""
        try:
            summary_url = (
                "https://ko.wikipedia.org/api/rest_v1/page/summary/"
                + urllib.parse.quote(query)
            )
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.get(summary_url, headers=_HEADERS)

            if resp.status_code == 200:
                data    = resp.json()
                extract = data.get("extract", "").strip()
                title   = data.get("title", query)
                if extract:
                    return f"- {title}: {extract[:400]}"

            # fallback: 검색 API
            search_url = (
                "https://ko.wikipedia.org/w/api.php"
                "?action=query&list=search&format=json"
                f"&srsearch={urllib.parse.quote(query)}&srlimit=3"
            )
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.get(search_url, headers=_HEADERS)
                resp.raise_for_status()

            results = resp.json().get("query", {}).get("search", [])
            lines = []
            for r in results[:3]:
                snippet = re.sub(r"<[^>]+>", "", r.get("snippet", ""))
                lines.append(f"- {r['title']}: {snippet}")
            return "\n".join(lines)
        except Exception as e:
            log.warning(f"[Agent] Wikipedia 오류: {e}")
            return ""

    async def _fetch_arxiv(self, query: str) -> str:
        """
        arXiv API (Atom XML).
        CS·Physics·Math 논문 검색. 키 불필요.
        """
        try:
            url = (
                "https://export.arxiv.org/api/query"
                f"?search_query=all:{urllib.parse.quote(query)}"
                "&max_results=3&sortBy=relevance"
            )
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.get(url, headers=_HEADERS)
                resp.raise_for_status()

            # Atom XML 파싱
            ns = {"atom": "http://www.w3.org/2005/Atom"}
            root = ET.fromstring(resp.text)
            entries = root.findall("atom:entry", ns)

            if not entries:
                return ""

            lines = []
            for entry in entries[:3]:
                title   = (entry.findtext("atom:title",   "", ns) or "").strip().replace("\n", " ")
                summary = (entry.findtext("atom:summary", "", ns) or "").strip().replace("\n", " ")
                lines.append(f"- [{title}] {summary[:250]}")

            return "\n".join(lines)
        except Exception as e:
            log.warning(f"[Agent] arXiv 오류: {e}")
            return ""

    async def _fetch_semantic_scholar(self, query: str) -> str:
        """
        Semantic Scholar Graph API.
        학술 논문 전반. 키 불필요 (100req/5min 제한).
        429 응답 시 2초 대기 후 1회 재시도.
        """
        url = (
            "https://api.semanticscholar.org/graph/v1/paper/search"
            f"?query={urllib.parse.quote(query)}"
            "&fields=title,abstract,year,authors"
            "&limit=3"
        )
        for attempt in range(2):  # 최대 2회 시도
            try:
                async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                    resp = await client.get(url, headers=_HEADERS)

                if resp.status_code == 429:
                    if attempt == 0:
                        log.warning("[Agent] Semantic Scholar 429 → 2초 후 재시도")
                        await asyncio.sleep(2)
                        continue
                    return ""  # 재시도 후에도 429면 포기

                resp.raise_for_status()
                papers = resp.json().get("data", [])
                if not papers:
                    return ""

                lines = []
                for p in papers[:3]:
                    title    = p.get("title", "")
                    year     = p.get("year", "")
                    abstract = (p.get("abstract") or "")[:200]
                    lines.append(f"- [{year}] {title}: {abstract}")

                return "\n".join(lines)

            except Exception as e:
                log.warning(f"[Agent] Semantic Scholar 오류: {e}")
                return ""
        return ""

    async def _fetch_pubmed(self, query: str) -> str:
        """
        PubMed E-utilities API.
        의학·생물학 논문. 키 불필요 (없이도 동작, API key로 rate limit 완화 가능).
        """
        try:
            base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

            # Step 1: 검색 → PMID 목록
            search_url = (
                f"{base}/esearch.fcgi"
                f"?db=pubmed&term={urllib.parse.quote(query)}"
                "&retmax=3&retmode=json"
            )
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.get(search_url, headers=_HEADERS)
                resp.raise_for_status()

            ids = resp.json().get("esearchresult", {}).get("idlist", [])
            if not ids:
                return ""

            # Step 2: PMID로 요약 정보 조회
            summary_url = (
                f"{base}/esummary.fcgi"
                f"?db=pubmed&id={','.join(ids)}&retmode=json"
            )
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.get(summary_url, headers=_HEADERS)
                resp.raise_for_status()

            ui_list = resp.json().get("result", {}).get("uids", [])
            result_map = resp.json().get("result", {})

            lines = []
            for uid in ui_list[:3]:
                paper = result_map.get(uid, {})
                title = paper.get("title", "")
                pub_date = paper.get("pubdate", "")
                source   = paper.get("source", "")
                lines.append(f"- [{pub_date} | {source}] {title}")

            return "\n".join(lines)
        except Exception as e:
            log.warning(f"[Agent] PubMed 오류: {e}")
            return ""

    # ── 백그라운드: 구조화 저장 + RAG 재인덱싱 ─────────────────────────

    async def _extract_and_save(self, query: str, raw_data: str) -> Optional[Path]:
        """
        [백그라운드 태스크]
        Ollama LLM으로 raw_data를 구조화 → KB 파일 저장 → RAG 재인덱싱.
        """
        try:
            prompt = (
                f"아래 검색 결과를 한국어 마크다운 문서로 구조화해줘.\n\n"
                f"검색어: {query}\n"
                f"검색 결과:\n{raw_data}\n\n"
                f"[응답 형식]\n"
                f"# 제목\n## 핵심 내용\n내용...\n## 출처\n출처..."
            )
            content = await ask_ollama(prompt)
            if not content or content.startswith("[Ollama"):
                log.warning(f"[Agent] LLM 구조화 실패: '{query[:30]}'")
                return None

            safe_name = re.sub(r"[^\w가-힣\-]", "_", query)[:50]
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            wiki_path = KNOWLEDGE_DIR / f"{safe_name}_{timestamp}.md"
            wiki_path.parent.mkdir(parents=True, exist_ok=True)

            tmp = wiki_path.with_suffix(".tmp")
            tmp.write_text(content, encoding="utf-8")
            tmp.replace(wiki_path)

            log.info(f"[Agent] KB 저장 완료: {wiki_path.name}")

            result = await rag_manager.index_file(wiki_path)
            log.info(f"[Agent] RAG 재인덱싱: {result}")

            return wiki_path

        except Exception as e:
            log.error(f"[Agent] _extract_and_save 오류: {e}")
            return None


# ── 싱글톤 ────────────────────────────────────────────────────────────────────
_agent: Optional[Agent] = None


def get_agent() -> Agent:
    global _agent
    if _agent is None:
        _agent = Agent()
    return _agent
