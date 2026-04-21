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
    def plan_changes(self, request: str) -> dict:
        """Phase 2: 실행 전 계획 수립.

        Returns:
            {"files": [관련 파일 경로 목록], "approach": "구현 방법 설명"}
        """


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

    def plan_changes(self, request: str) -> dict:
        msg = self._client.messages.create(
            model=self._model,
            max_tokens=512,
            system=PLAN_PROMPT,
            messages=[{"role": "user", "content": request}],
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

    def plan_changes(self, request: str) -> dict:
        text = self._call(PLAN_PROMPT, request, max_tokens=512, timeout=60)
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

    def plan_changes(self, request: str) -> dict:
        text = self._call(PLAN_PROMPT, request, timeout=60)
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text.strip())


def get_model(cfg: dict) -> BaseModel:
    choice = cfg["agent"]["model"]
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
    else:
        raise ValueError(f"Unknown model: {choice}. Choose claude / openai / ollama / llama-cpp")
