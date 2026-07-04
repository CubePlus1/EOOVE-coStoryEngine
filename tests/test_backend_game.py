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
from server.game import ApiError, AsyncGameService, GameService, PHASE_DURATIONS
from server.llm import HttpJsonLlmGateway
from server.printer import CommandPrinterDriver


class FakeLlmGateway:
    def __init__(self):
        self.configured = True
        self.results = []
        self.contexts = []

    def generate_character(self, self_desc):
        return None

    def moderate(self, payload):
        raise AssertionError("v4 idea moderation must stay local")

    def weave(self, context):
        self.contexts.append(context)
        if self.results:
            result = self.results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        return None


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

            result = gateway.weave({"task": "conversation"})

            self.assertEqual(result, {"ok": True})
            self.assertEqual(received[0]["task"], "weaver")
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

            result = gateway.weave({"task": "pitch"})

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

            result = driver.print_ticket({"kind": "receipt", "payload": {"idea": "猫相亲"}})

            self.assertEqual(result["returncode"], 0)
            written = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(written["payload"]["idea"], "猫相亲")


class GameServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "game.sqlite")
        self.llm = FakeLlmGateway()
        self.mail = FakeMailTransport()
        self.game = GameService(self.db_path, llm=self.llm, mail_transport=self.mail)

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

    def test_initializes_v4_schema_and_first_edition(self):
        edition = self.query_one("SELECT no, phase FROM editions WHERE id = 1")

        self.assertEqual(dict(edition), {"no": 1, "phase": "opening"})
        for table in [
            "agents",
            "teams",
            "ideas",
            "memories",
            "conversations",
            "events",
            "print_queue",
            "mail_queue",
        ]:
            self.assertIsNotNone(self.query_value(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,),
            ))
        self.assertIsNone(self.query_value("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'characters'"))
        self.assertGreaterEqual(self.query_value("SELECT COUNT(*) FROM agents WHERE role = 'hacker'"), 9)
        self.assertGreaterEqual(self.query_value("SELECT COUNT(*) FROM agents WHERE role = 'judge'"), 2)

    def test_edition_budget_is_fifteen_minutes(self):
        self.assertEqual(sum(PHASE_DURATIONS.values()), 15 * 60)

        for table in ["editions", "teams", "events"]:
            self.game.conn.execute(f"DELETE FROM {table}")
        self.game.conn.commit()
        self.game.ensure_world(now=1000)
        edition = self.query_one("SELECT phase, phase_ends_at FROM editions ORDER BY id DESC LIMIT 1")

        self.assertEqual(edition["phase"], "opening")
        self.assertEqual(edition["phase_ends_at"], 1000 + PHASE_DURATIONS["opening"])

    def test_submit_idea_returns_receipt_and_prints_receipt_ticket(self):
        result = self.game.submit_idea({
            "text": "给猫做相亲App",
            "investorName": "七色",
            "email": "hero@example.com",
        })

        self.assertEqual(set(result), {"ideaId", "receiptNo"})
        self.assertRegex(result["receiptNo"], r"^E01-I[0-9]{4}$")
        idea = self.query_one("SELECT text, investor_name, status FROM ideas WHERE id = ?", (result["ideaId"],))
        self.assertEqual(dict(idea), {"text": "给猫做相亲App", "investor_name": "七色", "status": "pooled"})
        payload = json.loads(self.query_value("SELECT payload_json FROM print_queue WHERE kind = 'receipt'"))
        self.assertEqual(payload["idea"], "给猫做相亲App")
        self.assertEqual(payload["qrUrl"], f"https://eoove.tianmiao.fun/idea/{result['ideaId']}")
        self.assertEqual(payload["trackingId"], result["receiptNo"])

    def test_idea_page_links_to_separate_visual_artifact_page(self):
        idea = self.game.submit_idea({"text": "给猫做相亲App"})

        progress_page = self.game.idea_page(idea["ideaId"])

        self.assertIn("text/html", progress_page["contentType"])
        self.assertIn("给猫做相亲App", progress_page["body"])
        self.assertIn("项目进度", progress_page["body"])
        self.assertIn("0%", progress_page["body"])
        self.assertNotIn("data-project-id", progress_page["body"])

        self.game.tick(now=100)

        updated_progress_page = self.game.idea_page(idea["ideaId"])
        visual_page = self.game.visual_artifact_page(idea["ideaId"])

        self.assertIn("项目进度", updated_progress_page["body"])
        self.assertIn("/artifacts/idea-1.html", updated_progress_page["body"])
        self.assertNotIn("data-project-id", updated_progress_page["body"])
        self.assertIn("text/html", visual_page["contentType"])
        self.assertIn("给猫做相亲App", visual_page["body"])
        self.assertIn("data-project-id", visual_page["body"])
        self.assertIn("demo-stage", visual_page["body"])
        self.assertIn("idea-chip", visual_page["body"])
        self.assertIn("linear-gradient", visual_page["body"])
        self.assertIn("<button", visual_page["body"])
        self.assertNotIn("完成度", visual_page["body"])
        self.assertNotIn("项目进度", visual_page["body"])

    def test_finished_project_does_not_keep_releasing_artifacts(self):
        idea = self.game.submit_idea({"text": "给猫做相亲App"})
        for offset in range(20):
            self.game.tick(now=100 + offset)
        finished_version = self.game.idea(idea["ideaId"])["artifact"]["version"]
        finished_commits = self.query_value("SELECT COUNT(*) FROM project_commits")

        self.game.tick(now=200)
        self.game.tick(now=201)

        self.assertEqual(self.game.idea(idea["ideaId"])["progress"], 100)
        self.assertEqual(self.game.idea(idea["ideaId"])["artifact"]["version"], finished_version)
        self.assertEqual(self.query_value("SELECT COUNT(*) FROM project_commits"), finished_commits)

    def test_idea_validation_rejects_sensitive_or_long_text_without_mutation(self):
        with self.assertRaises(ApiError) as raised:
            self.game.submit_idea({"text": "毁灭全世界"})

        self.assertEqual(raised.exception.code, "REJECTED")
        self.assertEqual(self.query_value("SELECT COUNT(*) FROM ideas"), 0)

        with self.assertRaises(ApiError):
            self.game.submit_idea({"text": "这是一条明确超过三十个字的黑客松点子用于测试长度限制必须被系统拒绝"})

        self.assertEqual(self.query_value("SELECT COUNT(*) FROM ideas"), 0)

    def test_tick_claims_idea_generates_conversation_memories_and_events(self):
        idea = self.game.submit_idea({"text": "给猫做相亲App", "investorName": "七色"})

        tick = self.game.tick(now=100)
        world = self.game.world(after=0)

        tracked = self.game.idea(idea["ideaId"])
        self.assertEqual(tick["advanced"], True)
        self.assertEqual(tracked["status"], "developing")
        self.assertIsNotNone(tracked["teamName"])
        self.assertGreater(tracked["progress"], 0)
        self.assertTrue(tracked["gossip"])
        self.assertGreaterEqual(len(world["conversations"]), 1)
        self.assertGreaterEqual(len(world["events"]), 2)
        self.assertGreaterEqual(self.query_value("SELECT COUNT(*) FROM memories"), 2)
        self.assertEqual(self.llm.contexts[-1]["task"], "conversation")
        self.assertIn("memories", self.llm.contexts[-1]["agents"][0])

    def test_tick_builds_project_artifact_tasks_and_commits(self):
        idea = self.game.submit_idea({"text": "给猫做相亲App", "investorName": "七色"})

        self.game.tick(now=100)
        tracked = self.game.idea(idea["ideaId"])
        project = self.game.project(tracked["projectId"])
        artifact = self.game.artifact(project["artifact"]["artifactId"])

        self.assertEqual(tracked["artifact"]["type"], "html")
        self.assertEqual(tracked["artifact"]["url"], f"/artifacts/idea-{idea['ideaId']}.html")
        self.assertIn("/api/artifact/", tracked["artifact"]["apiUrl"])
        self.assertEqual(project["ideaId"], idea["ideaId"])
        self.assertEqual(project["receiptNo"], idea["receiptNo"])
        self.assertGreaterEqual(len(project["tasks"]), 3)
        self.assertTrue(all(task["ownerAgentId"] for task in project["tasks"]))
        self.assertGreaterEqual(len(project["commits"]), 1)
        self.assertIn("html", artifact["contentType"])
        self.assertIn("给猫做相亲App", artifact["body"])
        self.assertIn("demo-stage", artifact["body"])
        self.assertIn("linear-gradient", artifact["body"])
        self.assertIn("<button", artifact["body"])
        self.assertIn("data-project-id", artifact["body"])
        self.assertNotIn("完成度", artifact["body"])

    def test_each_idea_gets_distinct_visual_demo_shape(self):
        first = self.game.submit_idea({"text": "给猫做相亲App"})
        second = self.game.submit_idea({"text": "给会议做总结器"})

        self.game.tick(now=100)

        first_body = self.game.visual_artifact_page(first["ideaId"])["body"]
        second_body = self.game.visual_artifact_page(second["ideaId"])["body"]

        self.assertIn('data-demo-kind="cat-match"', first_body)
        self.assertIn('data-demo-kind="workflow-brief"', second_body)
        self.assertIn('class="pet-card"', first_body)
        self.assertIn('class="brief-card"', second_body)
        self.assertIn("--accent:", first_body)
        self.assertIn("--accent:", second_body)
        self.assertNotEqual(first_body, second_body)

    def test_artifact_generation_asks_llm_for_frontend_html(self):
        self.llm.results = [
            {
                "html": (
                    "<!doctype html><html><body><main data-project-id=\"1\" "
                    "data-demo-kind=\"llm-v1\">LLM artifact v1</main><button>Run</button></body></html>"
                )
            },
            {
                "html": (
                    "<!doctype html><html><body><main data-project-id=\"1\" "
                    "data-demo-kind=\"llm-v2\">LLM artifact v2</main><button>Run</button></body></html>"
                )
            },
        ]
        idea = self.game.submit_idea({"text": "给猫做相亲App"})

        self.game.tick(now=100)
        artifact = self.game.artifact(self.game.idea(idea["ideaId"])["artifact"]["artifactId"])

        artifact_contexts = [context for context in self.llm.contexts if context.get("task") == "artifact"]
        self.assertTrue(artifact_contexts)
        self.assertEqual(artifact_contexts[0]["idea"]["text"], "给猫做相亲App")
        self.assertGreaterEqual(len(artifact_contexts[0]["tasks"]), 3)
        self.assertIn("LLM artifact v2", artifact["body"])

    def test_unclaimed_idea_gets_first_reaction_within_next_tick(self):
        idea = self.game.submit_idea({"text": "给评委写借口生成器"})

        self.game.tick(now=100)
        tracked = self.game.idea(idea["ideaId"])

        self.assertTrue(any("提到" in item or "听说" in item for item in tracked["gossip"]))

    def test_host_skip_phase_pitch_and_award_generate_certificate_and_mail(self):
        previous_whitelist = os.environ.get("MAIL_WHITELIST")
        os.environ["MAIL_WHITELIST"] = "hero@example.com"
        try:
            idea = self.game.submit_idea({
                "text": "给猫做相亲App",
                "investorName": "七色",
                "email": "hero@example.com",
            })
            self.game.tick(now=100)

            self.game.host({"action": "skip_phase", "phase": "pitch"})
            self.game.tick(now=200)
            self.game.host({"action": "skip_phase", "phase": "awards"})
            self.game.tick(now=300)

            tracked = self.game.idea(idea["ideaId"])
            self.assertEqual(tracked["status"], "awarded")
            self.assertIsNotNone(tracked["review"])
            self.assertIsNotNone(tracked["rank"])
            kinds = [row["kind"] for row in self.game.conn.execute("SELECT kind FROM print_queue ORDER BY id")]
            self.assertIn("certificate", kinds)
            self.assertIn("leaderboard", kinds)
            self.assertGreaterEqual(self.query_value("SELECT COUNT(*) FROM mail_queue"), 1)
        finally:
            if previous_whitelist is None:
                os.environ.pop("MAIL_WHITELIST", None)
            else:
                os.environ["MAIL_WHITELIST"] = previous_whitelist

    def test_world_after_uses_event_cursor_and_agent_detail_exposes_memory(self):
        self.game.submit_idea({"text": "给猫做相亲App"})
        self.game.tick(now=100)
        first = self.game.world(after=0)
        latest_event_id = first["events"][-1]["id"]
        incremental = self.game.world(after=latest_event_id)
        agent_id = first["agents"][0]["id"]
        agent = self.game.agent(agent_id)

        self.assertEqual(incremental["events"], [])
        self.assertEqual(set(first), {"edition", "agents", "conversations", "projects", "events"})
        self.assertIn("card", agent)
        self.assertIn("intent", agent)
        self.assertTrue(agent["memories"])

    def test_print_proxy_pending_and_ack_do_not_require_token(self):
        self.game.submit_idea({"text": "给猫做相亲App"})

        pending = self.game.print_pending(limit=5)
        self.assertEqual(pending[0]["kind"], "receipt")
        acked = self.game.print_ack({"ticketIds": [pending[0]["ticketId"]]})
        self.assertEqual(acked, {"acked": 1})
        self.assertEqual(self.game.print_pending(), [])

    def test_process_next_print_and_mail_retry_failures(self):
        printer = FakePrinterDriver()
        printer.fail_next = True
        game = GameService(self.db_path, printer_driver=printer)
        game.submit_idea({"text": "给猫做相亲App"})

        first = game.process_next_print_job()
        second = game.process_next_print_job()

        self.assertEqual(first["status"], "pending")
        self.assertEqual(second["status"], "printed")
        self.assertEqual(
            printer.printed[0]["qrUrl"],
            f"https://eoove.tianmiao.fun/idea/{printer.printed[0]['payload']['ideaId']}",
        )

    def test_process_print_job_without_idea_keeps_neutral_qr_url(self):
        printer = FakePrinterDriver()
        game = GameService(self.db_path, printer_driver=printer)
        game._enqueue_print("leaderboard", {"editionNo": 1, "awards": []})

        result = game.process_next_print_job()

        self.assertEqual(result["status"], "printed")
        self.assertEqual(printer.printed[0]["qrUrl"], "/")

    def test_reset_requires_confirmation_and_starts_fresh_edition(self):
        self.game.submit_idea({"text": "给猫做相亲App"})

        with self.assertRaises(ApiError):
            self.game.reset_story({"confirm": "WRONG"})

        reset = self.game.reset_story({"confirm": "RESET"})

        self.assertEqual(reset["reset"], True)
        self.assertEqual(self.query_value("SELECT COUNT(*) FROM ideas"), 0)
        self.assertEqual(self.query_value("SELECT no FROM editions WHERE id = 1"), 1)
        self.assertGreaterEqual(self.game.ensure_world()["agents"], 11)
        self.assertEqual(self.query_value("SELECT COUNT(*) FROM project_tasks"), 0)
        self.assertEqual(self.query_value("SELECT COUNT(*) FROM project_commits"), 0)
        self.assertEqual(self.query_value("SELECT COUNT(*) FROM artifacts"), 0)


class HttpContractTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "game.sqlite")
        self.game = GameService(self.db_path)
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

    def request_raw(self, method, path):
        host, port = self.server.server_address
        conn = HTTPConnection(host, port, timeout=2)
        conn.request(method, path)
        response = conn.getresponse()
        data = response.read().decode("utf-8")
        content_type = response.getheader("Content-Type")
        conn.close()
        return response.status, content_type, data

    def test_http_idea_world_agent_host_and_print_contract(self):
        status, idea = self.request("POST", "/api/idea", {"text": "给猫做相亲App", "investorName": "七色"})
        self.assertEqual(status, 200)
        self.assertIn("receiptNo", idea)

        status, tracked = self.request("GET", f"/api/idea/{idea['ideaId']}")
        self.assertEqual(status, 200)
        self.assertEqual(tracked["status"], "pooled")

        status, content_type, body = self.request_raw("GET", f"/idea/{idea['ideaId']}")
        self.assertEqual(status, 200)
        self.assertIn("text/html", content_type)
        self.assertIn("项目进度", body)
        self.assertIn("给猫做相亲App", body)

        self.game.tick(now=100)
        status, world = self.request("GET", "/api/world?after=0")
        self.assertEqual(status, 200)
        self.assertEqual(set(world), {"edition", "agents", "conversations", "projects", "events"})
        self.assertIn("artifact", world["projects"][0])

        status, agent = self.request("GET", f"/api/agent/{world['agents'][0]['id']}")
        self.assertEqual(status, 200)
        self.assertIn("memories", agent)

        status, project = self.request("GET", f"/api/project/{world['projects'][0]['projectId']}")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(project["tasks"]), 3)
        self.assertEqual(project["artifact"]["url"], f"/artifacts/idea-{idea['ideaId']}.html")
        self.assertTrue(project["artifact"]["apiUrl"].startswith("/api/artifact/"))

        status, artifact = self.request("GET", project["artifact"]["apiUrl"])
        self.assertEqual(status, 200)
        self.assertIn("text/html", artifact["contentType"])
        self.assertIn("给猫做相亲App", artifact["body"])

        status, content_type, body = self.request_raw("GET", f"/idea/{idea['ideaId']}")
        self.assertEqual(status, 200)
        self.assertIn("text/html", content_type)
        self.assertIn("项目进度", body)
        self.assertIn(f"/artifacts/idea-{idea['ideaId']}.html", body)
        self.assertNotIn("data-project-id", body)

        status, content_type, body = self.request_raw("GET", f"/artifacts/idea-{idea['ideaId']}.html")
        self.assertEqual(status, 200)
        self.assertIn("text/html", content_type)
        self.assertIn("data-project-id", body)

        status, host = self.request("POST", "/api/host", {"action": "skip_phase", "phase": "pitch"})
        self.assertEqual(status, 200)
        self.assertEqual(host["edition"]["phase"], "pitch")

        status, pending = self.request("GET", "/api/print/pending", headers={"X-Print-Token": "ignored"})
        self.assertEqual(status, 200)
        self.assertEqual(pending[0]["kind"], "receipt")

        status, acked = self.request("POST", "/api/print/ack", {"ticketIds": [pending[0]["ticketId"]]})
        self.assertEqual(status, 200)
        self.assertEqual(acked["acked"], 1)

    def test_removed_v3_routes_return_not_found(self):
        status, rejected = self.request("GET", "/api/template")
        self.assertEqual(status, 404)
        self.assertEqual(rejected["error"]["code"], "NOT_FOUND")

        status, rejected = self.request("POST", "/api/join", {"templateId": "t_001"})
        self.assertEqual(status, 404)
        self.assertEqual(rejected["error"]["code"], "NOT_FOUND")


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
    def test_async_wrapper_delegates_v4_methods(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = GameService(str(Path(tmp) / "game.sqlite"))
            service = AsyncGameService(game, None)

            idea = service.submit_idea({"text": "给猫做相亲App"})
            service.tick(now=100)

            self.assertEqual(service.idea(idea["ideaId"])["status"], "developing")
            self.assertIn("edition", service.world())
            game.close()


if __name__ == "__main__":
    unittest.main()
