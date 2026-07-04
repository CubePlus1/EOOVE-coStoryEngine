import json
import os
from pathlib import Path
from urllib import error, request


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
    def __init__(self, endpoint=None, api_key=None, prompts_dir=None, timeout_overrides=None):
        self.endpoint = endpoint or os.environ.get("EOOVE_LLM_ENDPOINT")
        self.api_key = api_key or os.environ.get("EOOVE_LLM_API_KEY")
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
        body = json.dumps(
            {
                "task": task,
                "prompt": self._prompt(prompt_name),
                "payload": payload,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = request.Request(self.endpoint, data=body, headers=headers, method="POST")
        try:
            with request.urlopen(req, timeout=timeout) as response:
                decoded = json.loads(response.read().decode("utf-8"))
        except (OSError, error.URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError):
            return None
        if isinstance(decoded, dict) and "result" in decoded:
            return decoded["result"]
        return decoded

    def _prompt(self, name):
        path = self.prompts_dir / f"{name}.txt"
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return ""
