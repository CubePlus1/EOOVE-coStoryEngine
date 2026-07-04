import json
import os
import sqlite3
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from server.app import create_handler
from server.game import ApiError, AsyncGameService, GameService
from server.llm import HttpJsonLlmGateway
from server.printer import CommandPrinterDriver


class FakeLlmGateway:
    def __init__(self):
        self.configured = True
        self.story_results = []
        self.contexts = []

    def generate_character(self, self_desc):
        return None

    def moderate(self, payload):
        raise AssertionError("v3 join moderation must stay local")

    def weave(self, context):
        self.contexts.append(context)
        if self.story_results:
            result = self.story_results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        return None

    def generate_ending(self, character, world):
        return {"ending": f"{character['name']}把自己的结局折进维修报告背面。"}


class FakeMailTransport:
    def __init__(self):
        self.sent = []
        self.fail_next = False

    def send(self, message):
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("smtp unavailable")
        self.sent.append(message)
        return {"messageId": f"m_{len(self.sent)}"}


class FakePrinterDriver:
    def __init__(self):
        self.printed = []
        self.fail_next = False

    def print_ticket(self, ticket):
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("printer offline")
        self.printed.append(ticket)
        return {"jobId": f"p_{len(self.printed)}"}


class LlmGatewayTest(unittest.TestCase):
    def test_http_gateway_returns_none_without_endpoint(self):
        gateway = HttpJsonLlmGateway(endpoint="")

        self.assertIsNone(gateway.generate_character("任意角色"))
        self.assertFalse(gateway.configured)

    def test_http_gateway_posts_prompt_payload_and_unwraps_result(self):
        received = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                received.append(json.loads(self.rfile.read(length).decode("utf-8")))
                body = json.dumps({"result": {"name": "风暴记录员"}}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server.server_address
            gateway = HttpJsonLlmGateway(endpoint=f"http://{host}:{port}")

            result = gateway.generate_character("追雷的人")

            self.assertEqual(result["name"], "风暴记录员")
            self.assertEqual(received[0]["task"], "character")
            self.assertEqual(received[0]["model"], "gpt-5.4-mini")
            self.assertNotIn("thinking", received[0])
            self.assertNotIn("reasoning", received[0])
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

    def test_http_gateway_model_can_be_overridden_without_enabling_thinking(self):
        received = []
        old_model = os.environ.get("EOOVE_LLM_MODEL")
        os.environ["EOOVE_LLM_MODEL"] = "custom-fast-model"

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                received.append(json.loads(self.rfile.read(length).decode("utf-8")))
                body = json.dumps({"result": {"ok": True}}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server.server_address
            gateway = HttpJsonLlmGateway(endpoint=f"http://{host}:{port}")

            result = gateway.weave({"world": {"phase": "running", "repairCount": 0}})

            self.assertEqual(result, {"ok": True})
            self.assertEqual(received[0]["model"], "custom-fast-model")
            self.assertNotIn("thinking", received[0])
            self.assertNotIn("reasoning", received[0])
        finally:
            if old_model is None:
                os.environ.pop("EOOVE_LLM_MODEL", None)
            else:
                os.environ["EOOVE_LLM_MODEL"] = old_model
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()


class PrinterDriverTest(unittest.TestCase):
    def test_command_printer_driver_streams_ticket_json_to_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "ticket.json"
            driver = CommandPrinterDriver(command=f"cat > {output}", timeout=1)

            result = driver.print_ticket({"kind": "charcard", "payload": {"name": "灯塔守夜人"}})

            self.assertEqual(result["returncode"], 0)
            written = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(written["payload"]["name"], "灯塔守夜人")


class GameServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "game.sqlite")
        self.llm = FakeLlmGateway()
        self.game = GameService(self.db_path, llm=self.llm)

    def tearDown(self):
        self.game.close()
        self.tmp.cleanup()

    def query_one(self, sql, params=()):
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(sql, params).fetchone()

    def query_value(self, sql, params=()):
        row = self.query_one(sql, params)
        return None if row is None else row[0]

    def test_initializes_v3_schema(self):
        world = self.query_one("SELECT phase, repair_count, finale_target FROM world WHERE id = 1")

        self.assertEqual(dict(world), {"phase": "running", "repair_count": 0, "finale_target": 10})
        columns = {row["name"] for row in self.game.conn.execute("PRAGMA table_info(world)").fetchall()}
        self.assertEqual(columns, {"id", "phase", "repair_count", "finale_target"})
        self.assertIsNone(self.query_value("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'acts'"))
        self.assertIsNotNone(self.query_value("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'rules'"))
        self.assertIsNotNone(self.query_value("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'stories'"))
        self.assertIsNotNone(self.query_value("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'batches'"))
        self.assertGreaterEqual(self.query_value("SELECT COUNT(*) FROM templates"), 50)

    def test_join_returns_batch_contract_and_prints_charcard(self):
        template = self.game.template()

        joined = self.game.join({
            "templateId": template["templateId"],
            "edits": {"name": "铜锅侠", "tagSwap": template["tagOptions"][-1]},
            "origin": "赛博大唐",
            "quirk": "会给螺丝念诗",
            "email": "hero@example.com",
        })

        self.assertEqual(set(joined), {"charId", "batchId", "etaSeconds", "name", "profile", "origin", "quirk"})
        self.assertRegex(joined["batchId"], r"^b_[0-9a-f]{8}$")
        self.assertEqual(joined["etaSeconds"], 45)
        self.assertEqual(joined["origin"], "赛博大唐")
        self.assertEqual(joined["quirk"], "会给螺丝念诗")
        batch = self.game.batch(joined["batchId"], now=0)
        self.assertEqual(batch["status"], "gathering")
        self.assertEqual(batch["members"][0]["name"], "铜锅侠")
        self.assertEqual(batch["members"][0]["type"], "human")
        self.assertEqual(self.query_value("SELECT kind FROM print_queue ORDER BY id LIMIT 1"), "charcard")

    def test_three_humans_auto_weave_one_story_and_rule(self):
        joined = [
            self.game.join({"templateId": self.game.template()["templateId"], "edits": {"name": name}})
            for name in ["甲", "乙", "丙"]
        ]

        batch = self.game.batch(joined[0]["batchId"], now=10)
        story = self.game.story(after=0)

        self.assertEqual(batch["status"], "done")
        self.assertEqual(batch["storyId"], 1)
        self.assertEqual(story["world"], {"phase": "running", "repairCount": 1, "finaleTarget": 10})
        self.assertEqual(len(story["rules"]), 1)
        self.assertEqual(len(story["stories"]), 1)
        self.assertEqual(story["stories"][0]["members"][0]["type"], "human")
        self.assertIn("rule", story["stories"][0])
        self.assertEqual(self.query_value("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'acts'"), None)
        self.assertIn("rules", self.llm.contexts[-1])
        self.assertEqual(len(self.llm.contexts[-1]["members"]), 3)
        report = self.query_one("SELECT kind, payload_json FROM print_queue WHERE kind = 'report'")
        self.assertIsNotNone(report)
        self.assertIn("rule", json.loads(report["payload_json"]))

    def test_timeout_fills_ai_residents_to_three(self):
        joined = self.game.join({"templateId": self.game.template()["templateId"], "edits": {"name": "独行者"}})
        deadline = self.query_value("SELECT deadline_at FROM batches WHERE id = ?", (joined["batchId"],))

        batch = self.game.batch(joined["batchId"], now=deadline)

        self.assertEqual(batch["status"], "done")
        members = json.loads(self.query_value("SELECT members_json FROM stories WHERE id = ?", (batch["storyId"],)))
        self.assertEqual(len(members), 3)
        self.assertEqual([member["type"] for member in members].count("ai"), 2)

    def test_story_after_uses_story_id_cursor(self):
        for name in ["甲", "乙", "丙"]:
            self.game.join({"templateId": self.game.template()["templateId"], "edits": {"name": name}})

        full = self.game.story(after=0)
        incremental = self.game.story(after=full["stories"][0]["id"])

        self.assertEqual(len(full["stories"]), 1)
        self.assertEqual(incremental["stories"], [])
        self.assertEqual(len(incremental["rules"]), 1)
        self.assertNotIn("acts", incremental)

    def test_print_proxy_pending_and_ack_require_token(self):
        game = GameService(self.db_path, llm=self.llm, print_token="secret")
        game.join({"templateId": game.template()["templateId"]})

        with self.assertRaises(ApiError):
            game.print_pending(token="wrong")

        pending = game.print_pending(token="secret", limit=5)
        self.assertEqual(pending[0]["kind"], "charcard")
        acked = game.print_ack({"ticketIds": [pending[0]["ticketId"]]}, token="secret")
        self.assertEqual(acked, {"acked": 1})
        self.assertEqual(game.print_pending(token="secret"), [])

    def test_trigger_finale_creates_finale_story_and_ticket(self):
        self.game.trigger_finale(token="")

        story = self.game.story(after=0)
        self.assertEqual(story["world"]["phase"], "finale")
        self.assertEqual(story["stories"][0]["kind"], "finale")
        self.assertEqual(self.query_value("SELECT kind FROM print_queue WHERE kind = 'finale'"), "finale")

    def test_admin_reset_requires_confirmation_and_preserves_settings(self):
        self.game.join({"templateId": self.game.template()["templateId"]})
        self.game.update_admin({"storyBackground": "维修区背景", "generationPaused": True})

        with self.assertRaises(ApiError):
            self.game.reset_story({"confirm": "WRONG"})

        reset = self.game.reset_story({"confirm": "RESET"})

        self.assertEqual(reset["reset"], True)
        self.assertEqual(self.query_value("SELECT COUNT(*) FROM characters"), 0)
        self.assertEqual(self.query_value("SELECT COUNT(*) FROM stories"), 0)
        self.assertEqual(self.query_value("SELECT COUNT(*) FROM rules"), 0)
        self.assertEqual(self.query_value("SELECT COUNT(*) FROM print_queue"), 0)
        self.assertEqual(self.game.admin()["storyBackground"], "维修区背景")

    def test_leave_card_mail_and_print_contracts_remain_available(self):
        joined = self.game.join({
            "templateId": self.game.template()["templateId"],
            "email": "hero@example.com",
        })

        left = self.game.leave({"charId": joined["charId"]})
        me = self.game.me(joined["charId"])
        card = self.game.card(joined["charId"])

        self.assertEqual(left, {"cardUrl": f"/card/{joined['charId']}"})
        self.assertEqual(me["status"], "ended")
        self.assertEqual(card["name"], joined["name"])
        self.assertEqual(self.query_value("SELECT kind FROM print_queue WHERE kind = 'ending'"), "ending")


class HttpContractTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "game.sqlite")
        self.game = GameService(self.db_path, print_token="secret")
        handler = create_handler(self.game)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()
        self.game.close()
        self.tmp.cleanup()

    def request(self, method, path, payload=None, headers=None):
        host, port = self.server.server_address
        conn = HTTPConnection(host, port, timeout=2)
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request_headers = {"Content-Type": "application/json"} if body is not None else {}
        request_headers.update(headers or {})
        conn.request(method, path, body=body, headers=request_headers)
        response = conn.getresponse()
        data = response.read()
        conn.close()
        parsed = json.loads(data.decode("utf-8")) if data else None
        return response.status, parsed

    def test_http_join_batch_story_print_and_finale_contract(self):
        status, template = self.request("GET", "/api/template")
        self.assertEqual(status, 200)

        status, joined = self.request("POST", "/api/join", {"templateId": template["templateId"]})
        self.assertEqual(status, 200)
        self.assertIn("batchId", joined)

        status, batch = self.request("GET", f"/api/batch/{joined['batchId']}")
        self.assertEqual(status, 200)
        self.assertEqual(batch["status"], "gathering")

        status, pending = self.request("GET", "/api/print/pending?limit=5", headers={"X-Print-Token": "secret"})
        self.assertEqual(status, 200)
        self.assertEqual(pending[0]["kind"], "charcard")

        status, acked = self.request("POST", "/api/print/ack", {"ticketIds": [pending[0]["ticketId"]]}, headers={"X-Print-Token": "secret"})
        self.assertEqual(status, 200)
        self.assertEqual(acked["acked"], 1)

        status, finale = self.request("POST", "/api/finale", {}, headers={"X-Print-Token": "secret"})
        self.assertEqual(status, 200)
        self.assertEqual(finale["world"]["phase"], "finale")

        status, story = self.request("GET", "/api/story?after=0")
        self.assertEqual(status, 200)
        self.assertEqual(set(story.keys()), {"world", "rules", "stories"})

    def test_print_proxy_rejects_bad_token(self):
        status, rejected = self.request("GET", "/api/print/pending", headers={"X-Print-Token": "bad"})

        self.assertEqual(status, 403)
        self.assertEqual(rejected["error"]["code"], "REJECTED")


class StaticFileTest(unittest.TestCase):
    def test_unknown_api_route_returns_json_not_static_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dist = root / "dist"
            dist.mkdir()
            (dist / "index.html").write_text("<div id='root'></div>", encoding="utf-8")
            game = GameService(str(root / "game.sqlite"))
            server = ThreadingHTTPServer(("127.0.0.1", 0), create_handler(game, static_root=dist))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                host, port = server.server_address
                conn = HTTPConnection(host, port, timeout=2)
                conn.request("GET", "/api/missing")
                response = conn.getresponse()
                data = json.loads(response.read().decode("utf-8"))
                conn.close()
                self.assertEqual(response.status, 404)
                self.assertEqual(data["error"]["code"], "NOT_FOUND")
            finally:
                server.shutdown()
                thread.join(timeout=2)
                server.server_close()
                game.close()


class AsyncGameServiceTest(unittest.TestCase):
    def test_async_wrapper_delegates_v3_methods(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = GameService(str(Path(tmp) / "game.sqlite"))
            service = AsyncGameService(game, None)

            joined = service.join({"templateId": service.template()["templateId"]})

            self.assertEqual(service.batch(joined["batchId"])["status"], "gathering")
            self.assertIn("world", service.story())
            game.close()


if __name__ == "__main__":
    unittest.main()
