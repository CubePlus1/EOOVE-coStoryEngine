import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .game import ApiError, AsyncGameService, GameService
from .rate_limit import RateLimiter
from .runtime import BackgroundRuntime, WeaveWorkerRuntime


def create_handler(game):
    class GameRequestHandler(BaseHTTPRequestHandler):
        service = game
        max_body_bytes = 16 * 1024
        rate_limiter = RateLimiter()

        def do_OPTIONS(self):
            self._send_no_content(204)

        def do_GET(self):
            try:
                parsed = urlparse(self.path)
                if parsed.path == "/api/story":
                    query = parse_qs(parsed.query)
                    after = query.get("after", ["0"])[0]
                    self._send_json(200, self.service.story(after=after))
                    return
                if parsed.path == "/api/template":
                    self._send_json(200, self.service.template())
                    return
                if parsed.path.startswith("/api/me/"):
                    char_id = parsed.path.rsplit("/", 1)[-1]
                    self._send_json(200, self.service.me(char_id))
                    return
                if parsed.path.startswith("/api/card/"):
                    char_id = parsed.path.rsplit("/", 1)[-1]
                    self._send_json(200, self.service.card(char_id))
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
                if parsed.path == "/api/join":
                    self._send_json(200, self.service.join(payload))
                    return
                if parsed.path == "/api/leave":
                    self._send_json(200, self.service.leave(payload))
                    return
                if parsed.path == "/api/mail/reply":
                    self._send_json(200, self.service.ingest_mail_reply(
                        payload.get("email"),
                        payload.get("text"),
                    ))
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
            if path not in {"/api/join", "/api/leave", "/api/mail/reply"}:
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

        def _cors_headers(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")

    return GameRequestHandler


def run(host="127.0.0.1", port=8000, db_path="server/db.sqlite"):
    game = GameService(db_path)
    background_runtime = BackgroundRuntime(game)
    weave_runtime = WeaveWorkerRuntime(game)
    service = AsyncGameService(game, weave_runtime)
    server = ThreadingHTTPServer((host, port), create_handler(service))
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
