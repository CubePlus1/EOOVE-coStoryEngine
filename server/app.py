import json
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .config import get_env, get_int_env
from .game import ApiError, AsyncGameService, GameService
from .rate_limit import RateLimiter
from .runtime import BackgroundRuntime, WeaveWorkerRuntime


def create_handler(game, static_root=None):
    root = Path(static_root).resolve() if static_root is not None else None

    class GameRequestHandler(BaseHTTPRequestHandler):
        service = game
        static_root = root
        max_body_bytes = 16 * 1024
        rate_limiter = RateLimiter()

        def do_OPTIONS(self):
            self._send_no_content(204)

        def do_GET(self):
            try:
                parsed = urlparse(self.path)
                if parsed.path == "/api/world":
                    query = parse_qs(parsed.query)
                    after = query.get("after", ["0"])[0]
                    self._send_json(200, self.service.world(after=after))
                    return
                if parsed.path == "/api/print/pending":
                    query = parse_qs(parsed.query)
                    limit = query.get("limit", ["5"])[0]
                    self._send_json(200, self.service.print_pending(limit=limit))
                    return
                if parsed.path == "/api/admin":
                    self._send_json(200, self.service.admin())
                    return
                if parsed.path.startswith("/api/idea/"):
                    idea_id = parsed.path.rsplit("/", 1)[-1]
                    self._send_json(200, self.service.idea(idea_id))
                    return
                if parsed.path.startswith("/api/project/"):
                    project_id = parsed.path.rsplit("/", 1)[-1]
                    self._send_json(200, self.service.project(project_id))
                    return
                if parsed.path.startswith("/api/artifact/"):
                    artifact_id = parsed.path.rsplit("/", 1)[-1]
                    self._send_json(200, self.service.artifact(artifact_id))
                    return
                if parsed.path.startswith("/api/agent/"):
                    agent_id = parsed.path.rsplit("/", 1)[-1]
                    self._send_json(200, self.service.agent(agent_id))
                    return
                if parsed.path.startswith("/api/"):
                    raise ApiError("NOT_FOUND", "世界没有这条道路。", status=404)
                if parsed.path.startswith("/idea/"):
                    idea_id = parsed.path.rsplit("/", 1)[-1]
                    self._send_html(200, self.service.idea_page(idea_id))
                    return
                if self.static_root is not None:
                    self._send_static(parsed.path)
                    return
                raise ApiError("NOT_FOUND", "世界没有这条道路。", status=404)
            except ApiError as error:
                self._send_json(error.status, error.envelope())
            except Exception:
                self._send_json(500, ApiError("INTERNAL", "世界短暂失语。", 500).envelope())

        def do_POST(self):
            try:
                parsed = urlparse(self.path)
                self._enforce_rate_limit(parsed.path)
                payload = self._read_json()
                if parsed.path == "/api/idea":
                    self._send_json(200, self.service.submit_idea(payload))
                    return
                if parsed.path == "/api/mail/reply":
                    self._send_json(200, self.service.ingest_mail_reply(
                        payload.get("email"),
                        payload.get("text"),
                    ))
                    return
                if parsed.path == "/api/admin":
                    self._send_json(200, self.service.update_admin(payload))
                    return
                if parsed.path == "/api/admin/reset":
                    self._send_json(200, self.service.reset_story(payload))
                    return
                if parsed.path == "/api/print/ack":
                    self._send_json(200, self.service.print_ack(payload))
                    return
                if parsed.path == "/api/host":
                    self._send_json(200, self.service.host(payload))
                    return
                raise ApiError("NOT_FOUND", "世界没有这条道路。", status=404)
            except ApiError as error:
                self._send_json(error.status, error.envelope())
            except json.JSONDecodeError:
                self._send_json(400, ApiError("REJECTED", "世界读不懂这段文字。").envelope())
            except Exception:
                self._send_json(500, ApiError("INTERNAL", "世界短暂失语。", 500).envelope())

        def log_message(self, format, *args):
            return

        def _read_json(self):
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length > self.max_body_bytes:
                raise ApiError("REJECTED", "这段文字太重,世界暂时承载不了。", status=413)
            raw = self.rfile.read(length) if length else b"{}"
            return json.loads(raw.decode("utf-8"))

        def _enforce_rate_limit(self, path):
            if path not in {"/api/idea", "/api/mail/reply", "/api/admin", "/api/admin/reset", "/api/print/ack", "/api/host"}:
                return
            client = self.client_address[0] if self.client_address else "unknown"
            key = f"{client}:{path}"
            if not self.rate_limiter.allow(key):
                raise ApiError("QUOTA", "世界需要片刻喘息,请稍后再试。", status=429)

        def _send_no_content(self, status):
            self.send_response(status)
            self._cors_headers()
            self.end_headers()

        def _send_json(self, status, payload):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self._cors_headers()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, status, payload):
            body = payload["body"].encode("utf-8")
            self.send_response(status)
            self._cors_headers()
            self.send_header("Content-Type", payload["contentType"])
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_static(self, request_path):
            target = self._static_target(request_path)
            if target is None:
                raise ApiError("NOT_FOUND", "世界没有这条道路。", status=404)
            body = target.read_bytes()
            content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            self.send_response(200)
            self._cors_headers()
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _static_target(self, request_path):
            if self.static_root is None:
                return None
            relative = request_path.lstrip("/") or "index.html"
            candidate = (self.static_root / relative).resolve()
            if candidate.is_file() and candidate.is_relative_to(self.static_root):
                return candidate
            if Path(relative).suffix:
                return None
            index = self.static_root / "index.html"
            if index.is_file():
                return index
            return None

        def _cors_headers(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")

    return GameRequestHandler


def run(host=None, port=None, db_path=None):
    host = host or get_env("EOOVE_HOST", "127.0.0.1")
    port = port or get_int_env("EOOVE_PORT", 8000)
    db_path = db_path or get_env("EOOVE_DB_PATH", "server/db.sqlite")
    static_root = get_env("EOOVE_STATIC_ROOT", "dist")
    game = GameService(db_path)
    background_runtime = BackgroundRuntime(game)
    weave_runtime = WeaveWorkerRuntime(game)
    service = AsyncGameService(game, weave_runtime)
    server = ThreadingHTTPServer((host, port), create_handler(service, static_root=static_root))
    background_runtime.start()
    weave_runtime.start()
    try:
        server.serve_forever()
    finally:
        background_runtime.stop(timeout=2)
        weave_runtime.stop(timeout=2)
        game.close()
        server.server_close()


if __name__ == "__main__":
    run()
