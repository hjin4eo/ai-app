"""
멀티모델 추상화 - Claude / OpenAI / Ollama 단일 인터페이스
"""
from __future__ import annotations

import abc
import json
from typing import Optional


class BaseModel(abc.ABC):
    @abc.abstractmethod
    def fix_code(self, error_log: str, code_context: str) -> str:
        """오류 로그와 코드 컨텍스트를 받아 수정된 코드 패치를 반환."""

    @abc.abstractmethod
    def generate_code(self, request: str) -> dict[str, str]:
        """사용자 요청을 받아 {파일경로: 내용} dict를 반환."""

    @abc.abstractmethod
    def generate_patch(self, request: str, code_context: str) -> str:
        """사용자 요청과 코드 컨텍스트를 받아 Unified Diff 패치를 반환."""

    @abc.abstractmethod
    def decompose_task(self, request: str) -> dict:
        """Phase 4: 복잡한 태스크를 서브태스크로 분해.

        Returns:
            {
              "is_complex": bool,
              "subtasks": ["구체적 서브태스크 설명", ...]  # is_complex=False면 빈 리스트
            }
        """

    @abc.abstractmethod
    def plan_changes(self, request: str) -> dict:
        """Phase 2: 실행 전 계획 수립.

        Returns:
            {"files": [관련 파일 경로 목록], "approach": "구현 방법 설명"}
        """

    @abc.abstractmethod
    def review_code(self, request: str, diff: str) -> dict:
        """Phase 3: 생성된 코드 리뷰.

        Returns:
            {
              "one_line": "한 줄 요약 (텔레그램 알림용)",
              "summary": "파일별 설명",
              "score": 1~10,
              "issues": ["문제점 목록"] or []
            }
        """


DECOMPOSE_PROMPT = """You are a senior software engineer breaking down a development task.
Analyze the task description and decide if it is too large or vague to implement in one step.

A task IS complex (is_complex: true) if ANY of these apply:
- Longer than 200 characters
- Mentions a "system", "pipeline", "architecture", or "document"
- Requires creating or modifying more than 2 files
- Contains multiple independent features or steps
- Is vague about which specific file or function to implement

A task is simple (is_complex: false) only if it clearly names ONE file and ONE function/command to implement.

If complex, break it into 2-5 specific, actionable subtasks. Each subtask MUST:
- Name the exact file path to create or modify
- Describe exactly one function/class/command
- Be implementable in a single coding session

Return ONLY valid JSON:
{"is_complex": true, "subtasks": ["agent/services/scraper.py 만들어줘. WebScraper 클래스, scrape(url) → str 반환.", "agent/handlers/bot_commands.py에 /scrape <url> 명령어 추가. WebScraper 호출 후 결과 텔레그램 응답."]}

If truly simple:
{"is_complex": false, "subtasks": []}"""

REVIEW_PROMPT = """You are a senior software engineer reviewing AI-generated code.
You will be given a task description and a git diff of the changes made.
Analyze the diff and return a JSON review with:
- "one_line": one sentence (Korean) summarizing what was built, e.g. "URL 크롤링 WebScraper 클래스 추가"
- "summary": 2-4 sentences (Korean) describing each file changed and what it does
- "score": integer 1-10 quality score (10=excellent, 1=broken)
- "issues": list of strings (Korean) describing bugs, missing error handling, or concerns (empty list if none)

Return ONLY valid JSON. Example:
{"one_line": "WebScraper 클래스 추가 — URL 크롤링 후 00_Raw 저장", "summary": "agent/services/scraper.py: WebScraper 클래스 신설. scrape(url) 메서드가 BeautifulSoup으로 텍스트 추출 후 파일 저장.", "score": 7, "issues": ["에러 시 빈 파일 생성될 수 있음", "저장 경로 하드코딩"]}"""

PLAN_PROMPT = """You are an expert software engineer. You will be given a task description.
Analyze the task and return a JSON plan with:
- "files": list of file paths (relative to repo root) most relevant to this task
- "approach": 1-3 sentence description of how you will implement the changes

Return ONLY valid JSON. Example:
{"files": ["agent/handlers/bot_commands.py", "agent/core/task_queue.py"], "approach": "Add /stats command to bot_commands.py that calls list_tasks() and formats a summary."}"""

SYSTEM_PROMPT = """You are an expert software engineer.
You will be given a test failure log and the relevant source code.
Analyze the root cause and return a unified diff patch that fixes the issue.
Return ONLY the patch in unified diff format (--- a/file +++ b/file), no explanation."""

GENERATE_PATCH_PROMPT = """You are an expert software engineer.
You will be given a user request and the relevant source code context.
Analyze the request and return a unified diff patch that implements the changes.
Return ONLY the patch in unified diff format (--- a/file +++ b/file), no explanation."""

GENERATE_PROMPT = """You are an expert software engineer and web developer.
The user wants you to build something. Generate a complete implementation.

Return ONLY a valid JSON object where keys are file paths and values are complete file contents.
Example: {"app/main.py": "...", "tests/runner.py": "..."}

Rules:
1. Build a Python Flask web app runnable with: python app/main.py (port 5000)
2. Include all necessary HTML/CSS/JS inside templates/ or as static files
3. Always include an updated tests/runner.py that:
   - Starts the Flask app as a subprocess on port 5000
   - Uses Playwright (chromium) to test the UI
   - Takes a screenshot to /tmp/test_fail_{timestamp}.png on failure
   - Exits with code 0 on success, 1 on failure
4. Return ONLY valid JSON, no markdown fences, no explanation."""


class ClaudeModel(BaseModel):
    def __init__(self, api_key: str, model: str = "claude-sonnet-4-6"):
        import anthropic
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model

    def fix_code(self, error_log: str, code_context: str) -> str:
        msg = self._client.messages.create(
            model=self._model,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": f"## Test failure log\n```\n{error_log}\n```\n\n## Source code\n```\n{code_context}\n```",
            }],
        )
        return msg.content[0].text

    def generate_code(self, request: str) -> dict[str, str]:
        msg = self._client.messages.create(
            model=self._model,
            max_tokens=8192,
            system=GENERATE_PROMPT,
            messages=[{"role": "user", "content": request}],
        )
        return json.loads(msg.content[0].text)

    def generate_patch(self, request: str, code_context: str) -> str:
        msg = self._client.messages.create(
            model=self._model,
            max_tokens=8192,
            system=GENERATE_PATCH_PROMPT,
            messages=[{
                "role": "user",
                "content": f"## User Request\n{request}\n\n## Source code\n```\n{code_context}\n```",
            }],
        )
        return msg.content[0].text

    def decompose_task(self, request: str) -> dict:
        msg = self._client.messages.create(
            model=self._model,
            max_tokens=1024,
            system=DECOMPOSE_PROMPT,
            messages=[{"role": "user", "content": request}],
        )
        return json.loads(msg.content[0].text)

    def plan_changes(self, request: str) -> dict:
        msg = self._client.messages.create(
            model=self._model,
            max_tokens=512,
            system=PLAN_PROMPT,
            messages=[{"role": "user", "content": request}],
        )
        return json.loads(msg.content[0].text)

    def review_code(self, request: str, diff: str) -> dict:
        msg = self._client.messages.create(
            model=self._model,
            max_tokens=1024,
            system=REVIEW_PROMPT,
            messages=[{"role": "user", "content": f"## 태스크\n{request}\n\n## Git Diff\n```diff\n{diff}\n```"}],
        )
        return json.loads(msg.content[0].text)


class OpenAIModel(BaseModel):
    def __init__(self, api_key: str, model: str = "gpt-4o"):
        from openai import OpenAI
        self._client = OpenAI(api_key=api_key)
        self._model = model

    def fix_code(self, error_log: str, code_context: str) -> str:
        resp = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": (
                    f"## Test failure log\n```\n{error_log}\n```\n\n"
                    f"## Source code\n```\n{code_context}\n```"
                )},
            ],
            max_tokens=4096,
        )
        return resp.choices[0].message.content

    def generate_code(self, request: str) -> dict[str, str]:
        resp = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": GENERATE_PROMPT},
                {"role": "user", "content": request},
            ],
            max_tokens=8192,
        )
        return json.loads(resp.choices[0].message.content)

    def generate_patch(self, request: str, code_context: str) -> str:
        resp = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": GENERATE_PATCH_PROMPT},
                {"role": "user", "content": (
                    f"## User Request\n{request}\n\n"
                    f"## Source code\n```\n{code_context}\n```"
                )},
            ],
            max_tokens=8192,
        )
        return resp.choices[0].message.content

    def decompose_task(self, request: str) -> dict:
        resp = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": DECOMPOSE_PROMPT},
                {"role": "user", "content": request},
            ],
            max_tokens=1024,
        )
        return json.loads(resp.choices[0].message.content)

    def plan_changes(self, request: str) -> dict:
        resp = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": PLAN_PROMPT},
                {"role": "user", "content": request},
            ],
            max_tokens=512,
        )
        return json.loads(resp.choices[0].message.content)

    def review_code(self, request: str, diff: str) -> dict:
        resp = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": REVIEW_PROMPT},
                {"role": "user", "content": f"## 태스크\n{request}\n\n## Git Diff\n```diff\n{diff}\n```"},
            ],
            max_tokens=1024,
        )
        return json.loads(resp.choices[0].message.content)


class LlamaCppModel(BaseModel):
    """LM Studio / llama-cpp-python OpenAI 호환 서버용 모델."""

    def __init__(self, base_url: str = "http://localhost:1234", model: str = ""):
        self._base_url = base_url.rstrip("/")
        self._model = model

    def _call(self, system: str, user: str, max_tokens: int = 4096, timeout: int = 300) -> str:
        import urllib.request
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        body: dict = {"messages": messages, "max_tokens": max_tokens}
        if self._model:
            body["model"] = self._model
        payload = json.dumps(body).encode()
        req = urllib.request.Request(
            f"{self._base_url}/v1/chat/completions",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        import re
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = json.loads(resp.read())["choices"][0]["message"]["content"]
        text = re.sub(r'<think>[\s\S]*?</think>', '', text)
        return re.sub(r'<\|[^|]*\|>', '', text).strip()

    def fix_code(self, error_log: str, code_context: str) -> str:
        return self._call(
            SYSTEM_PROMPT,
            f"## Test failure log\n```\n{error_log}\n```\n\n## Source code\n```\n{code_context}\n```",
        )

    def generate_code(self, request: str) -> dict[str, str]:
        text = self._call(GENERATE_PROMPT, request, max_tokens=8192, timeout=600)
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text.strip())

    def generate_patch(self, request: str, code_context: str) -> str:
        return self._call(
            GENERATE_PATCH_PROMPT,
            f"## User Request\n{request}\n\n## Source code\n```\n{code_context}\n```",
            max_tokens=8192, timeout=600
        )

    def decompose_task(self, request: str) -> dict:
        text = self._call(DECOMPOSE_PROMPT, request, max_tokens=1024, timeout=120)
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text.strip())

    def plan_changes(self, request: str) -> dict:
        text = self._call(PLAN_PROMPT, request, max_tokens=512, timeout=60)
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text.strip())

    def review_code(self, request: str, diff: str) -> dict:
        text = self._call(
            REVIEW_PROMPT,
            f"## 태스크\n{request}\n\n## Git Diff\n```diff\n{diff}\n```",
            max_tokens=1024, timeout=120
        )
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text.strip())


class OllamaModel(BaseModel):
    def __init__(self, base_url: str = "http://localhost:11434", model: str = "qwen2.5-coder:32b"):
        import urllib.request
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._urlrequest = urllib.request

    def _call(self, system: str, user: str, timeout: int = 180) -> str:
        import urllib.request
        payload = json.dumps({
            "model": self._model,
            "prompt": f"{system}\n\n{user}",
            "stream": False,
        }).encode()
        req = urllib.request.Request(
            f"{self._base_url}/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())["response"]

    def fix_code(self, error_log: str, code_context: str) -> str:
        return self._call(
            SYSTEM_PROMPT,
            f"## Test failure log\n```\n{error_log}\n```\n\n## Source code\n```\n{code_context}\n```",
        )

    def generate_code(self, request: str) -> dict[str, str]:
        text = self._call(GENERATE_PROMPT, request, timeout=300)
        # JSON 블록만 추출 (모델이 마크다운 감쌀 경우 대비)
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text.strip())

    def generate_patch(self, request: str, code_context: str) -> str:
        return self._call(
            GENERATE_PATCH_PROMPT,
            f"## User Request\n{request}\n\n## Source code\n```\n{code_context}\n```",
            timeout=300
        )

    def decompose_task(self, request: str) -> dict:
        text = self._call(DECOMPOSE_PROMPT, request, timeout=120)
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text.strip())

    def plan_changes(self, request: str) -> dict:
        text = self._call(PLAN_PROMPT, request, timeout=60)
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text.strip())

    def review_code(self, request: str, diff: str) -> dict:
        text = self._call(
            REVIEW_PROMPT,
            f"## 태스크\n{request}\n\n## Git Diff\n```diff\n{diff}\n```",
            timeout=120
        )
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text.strip())


class UnslothModel(BaseModel):
    """Unsloth Studio OpenAI 호환 서버용 모델."""

    def __init__(self, base_url: str = "http://127.0.0.1:8888/v1", api_key: str = "", model: str = ""):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model

    def _call(self, system: str, user: str, max_tokens: int = 4096, timeout: int = 300) -> str:
        import urllib.request, re
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        body: dict = {"messages": messages, "max_tokens": max_tokens}
        if self._model:
            body["model"] = self._model
        payload = json.dumps(body).encode()
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        req = urllib.request.Request(
            f"{self._base_url}/chat/completions",
            data=payload,
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = json.loads(resp.read())["choices"][0]["message"]["content"]
        text = re.sub(r'<think>[\s\S]*?</think>', '', text)
        return re.sub(r'<\|[^|]*\|>', '', text).strip()

    def fix_code(self, error_log: str, code_context: str) -> str:
        return self._call(
            SYSTEM_PROMPT,
            f"## Test failure log\n```\n{error_log}\n```\n\n## Source code\n```\n{code_context}\n```",
        )

    def generate_code(self, request: str) -> dict[str, str]:
        text = self._call(GENERATE_PROMPT, request, max_tokens=8192, timeout=600)
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text.strip())


def get_model(cfg: dict) -> BaseModel:
    import os
    choice = os.environ.get("MODEL_BACKEND") or cfg["agent"]["model"]
    if choice == "claude":
        return ClaudeModel(api_key=cfg["anthropic"]["api_key"])
    elif choice == "openai":
        return OpenAIModel(api_key=cfg["openai"]["api_key"])
    elif choice == "ollama":
        return OllamaModel(
            model=cfg["agent"].get("ollama_model", "qwen2.5-coder:32b"),
            base_url=cfg.get("ollama", {}).get("base_url", "http://localhost:11434")
        )
    elif choice == "llama-cpp":
        return LlamaCppModel(
            base_url=cfg.get("llama_cpp", {}).get("url", "http://localhost:1234"),
            model=cfg.get("llama_cpp", {}).get("model", ""),
        )
    elif choice == "unsloth":
        return UnslothModel(
            base_url=os.environ.get("UNSLOTH_BASE_URL", "http://127.0.0.1:8888/v1"),
            api_key=os.environ.get("UNSLOTH_API_KEY", ""),
            model=os.environ.get("UNSLOTH_MODEL", ""),
        )
    else:
        raise ValueError(f"Unknown model: {choice}. Choose claude / openai / ollama / llama-cpp / unsloth")
