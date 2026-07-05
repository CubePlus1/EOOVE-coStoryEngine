import json
from pathlib import Path
from urllib import error, request
from urllib.parse import urlparse, urlunparse

from .config import get_env


DEFAULT_LLM_MODEL = "gpt-5.4-mini"


class LlmGateway:
    configured = False

    def generate_character(self, self_desc):
        return None

    def moderate(self, payload):
        return None

    def weave(self, context):
        return None

    def generate_ending(self, character, world):
        return None

    def generate_echo(self, chronicle, world):
        return None


class HttpJsonLlmGateway(LlmGateway):
    def __init__(self, endpoint=None, api_key=None, prompts_dir=None, timeout_overrides=None, model=None):
        self.endpoint = endpoint or get_env("EOOVE_LLM_ENDPOINT")
        self.api_key = api_key or get_env("EOOVE_LLM_API_KEY")
        self.model = model or get_env("EOOVE_LLM_MODEL", DEFAULT_LLM_MODEL)
        self.prompts_dir = Path(prompts_dir or Path(__file__).with_name("prompts"))
        self.timeout_overrides = timeout_overrides or {}
        self.configured = bool(self.endpoint)

    def generate_character(self, self_desc):
        return self._call_json(
            task="character",
            prompt_name="character",
            payload={"selfDesc": self_desc},
            timeout=self.timeout_overrides.get("character", 8),
        )

    def moderate(self, payload):
        return self._call_json(
            task="moderation",
            prompt_name="moderation",
            payload=payload,
            timeout=self.timeout_overrides.get("moderation", 2),
        )

    def weave(self, context):
        return self._call_json(
            task="weaver",
            prompt_name="weaver",
            payload=context,
            timeout=self.timeout_overrides.get("weaver", 15),
        )

    def generate_ending(self, character, world):
        return self._call_json(
            task="ending",
            prompt_name="ending",
            payload={"character": dict(character), "world": dict(world)},
            timeout=self.timeout_overrides.get("ending", 20),
        )

    def generate_echo(self, chronicle, world):
        return self._call_json(
            task="echo",
            prompt_name="echo",
            payload={"chronicle": chronicle, "world": dict(world)},
            timeout=self.timeout_overrides.get("echo", 20),
        )

    def _call_json(self, task, prompt_name, payload, timeout):
        if not self.endpoint:
            return None
        body = json.dumps(self._chat_payload(task, prompt_name, payload), ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json", "User-Agent": "EOOVE/1.0"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = request.Request(self._chat_completions_endpoint(), data=body, headers=headers, method="POST")
        try:
            with request.urlopen(req, timeout=timeout) as response:
                decoded = json.loads(response.read().decode("utf-8"))
        except (OSError, error.URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError):
            return None
        if isinstance(decoded, dict) and "result" in decoded:
            return decoded["result"]
        openai_result = self._openai_result(decoded)
        if openai_result is not None:
            return openai_result
        return decoded

    def _chat_payload(self, task, prompt_name, payload):
        effective_task = payload.get("task") if task == "weaver" and isinstance(payload, dict) else task
        system_parts = [
            "You are the EOOVE backend LLM worker.",
            f"Task: {effective_task}.",
            "Return only a JSON object. Do not wrap it in Markdown.",
        ]
        if effective_task == "artifact":
            system_parts.extend([
                "Generate frontend HTML for the exact submitted idea.",
                "Return JSON in this shape: {\"html\":\"<!doctype html>...\"}.",
                "The html must be self-contained HTML and include data-project-id, data-idea-fingerprint, and at least one button.",
                "Do not return conversation lines, memories, intents, or a progress dashboard.",
            ])
        else:
            prompt = self._prompt(prompt_name).strip()
            if prompt:
                system_parts.append(prompt)
        user_content = json.dumps(
            {
                "task": task,
                "payload": payload,
            },
            ensure_ascii=False,
            indent=2,
        )
        return {
            "model": self.model,
            "messages": [
                {"role": "system", "content": "\n\n".join(system_parts)},
                {"role": "user", "content": user_content},
            ],
        }

    def _chat_completions_endpoint(self):
        parsed = urlparse(self.endpoint)
        path = parsed.path.rstrip("/")
        if path.endswith("/chat/completions"):
            completed_path = path
        elif path.endswith("/v1"):
            completed_path = f"{path}/chat/completions"
        elif "/v1/" in f"{path}/" or path == "/v1":
            completed_path = f"{path}/chat/completions"
        else:
            completed_path = f"{path}/v1/chat/completions" if path else "/v1/chat/completions"
        return urlunparse(parsed._replace(path=completed_path, params="", query="", fragment=""))

    def _openai_result(self, decoded):
        if not isinstance(decoded, dict):
            return None
        choices = decoded.get("choices")
        if not isinstance(choices, list) or not choices:
            return None
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str):
            return None
        return self._parse_json_content(content)

    def _parse_json_content(self, content):
        stripped = content.strip()
        if stripped.startswith("```"):
            lines = stripped.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            stripped = "\n".join(lines).strip()
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            return {"text": content}
        return parsed if isinstance(parsed, dict) else {"value": parsed}

    def _prompt(self, name):
        path = self.prompts_dir / f"{name}.txt"
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return ""
