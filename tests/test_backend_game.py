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
from server.runtime import BackgroundRuntime, WeaveWorkerRuntime


class FakeLlmGateway:
    def __init__(self):
        self.configured = True
        self.weaver_results = []
        self.contexts = []

    def generate_character(self, self_desc):
        return None

    def moderate(self, payload):
        raise AssertionError("v2 join moderation must not call the LLM")

    def weave(self, context):
        self.contexts.append(context)
        if self.weaver_results:
            result = self.weaver_results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        return None

    def generate_ending(self, character, world):
        return {"ending": f"{character['name']}把自己的结局折进传说背面。"}

    def generate_echo(self, chronicle, world):
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
                body = json.dumps({
                    "result": {
                        "name": "风暴记录员",
                        "profile": "风暴记录员: 记录每一次雷声。",
                    }
                }).encode("utf-8")
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
            self.assertEqual(received[0]["payload"], {"selfDesc": "追雷的人"})
            self.assertIn("角色", received[0]["prompt"])
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()


class PrinterDriverTest(unittest.TestCase):
    def test_command_printer_driver_streams_ticket_json_to_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "ticket.json"
            driver = CommandPrinterDriver(command=f"cat > {output}", timeout=1)

            result = driver.print_ticket({
                "kind": "charcard",
                "payload": {"name": "灯塔守夜人"},
            })

            self.assertEqual(result["returncode"], 0)
            written = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(written["payload"]["name"], "灯塔守夜人")

    def test_command_printer_driver_raises_on_failed_command(self):
        driver = CommandPrinterDriver(command="exit 7", timeout=1)

        with self.assertRaises(RuntimeError):
            driver.print_ticket({"kind": "charcard", "payload": {}})


class GameServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "game.sqlite")
        self.game = GameService(self.db_path)

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

    def test_initializes_v2_schema_world_and_template_pool(self):
        world = self.query_one(
            "SELECT legend_index, legend_text, clue_count, act_seq FROM world WHERE id = 1"
        )

        self.assertEqual(world["legend_index"], 0)
        self.assertIn("钟", world["legend_text"])
        self.assertEqual(world["clue_count"], 0)
        self.assertEqual(world["act_seq"], 0)
        self.assertEqual(self.query_value(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'oracle_pool'"
        ), None)
        self.assertGreaterEqual(self.query_value("SELECT COUNT(*) FROM templates"), 50)

    def test_template_endpoint_contract_and_join_consumes_template(self):
        template = self.game.template()

        self.assertEqual(set(template.keys()), {"templateId", "name", "profile", "tags", "tagOptions"})
        self.assertEqual(len(template["tags"]), 3)
        joined = self.game.join({
            "templateId": template["templateId"],
            "edits": {"name": "铜锅侠", "tagSwap": template["tagOptions"][-1]},
            "email": "hero@example.com",
        })

        self.assertRegex(joined["charId"], r"^c_[0-9a-f]{8}$")
        self.assertEqual(joined["name"], "铜锅侠")
        self.assertIn("firstActId", joined)
        self.assertNotIn("cycle", joined)
        self.assertNotIn("location", joined)
        self.assertEqual(self.query_value("SELECT used FROM templates WHERE id = ?", (template["templateId"],)), 1)
        self.assertEqual(self.query_value("SELECT kind FROM print_queue ORDER BY id LIMIT 1"), "charcard")

    def test_join_rejects_bad_edits_with_v2_message_and_no_mutation(self):
        template = self.game.template()

        with self.assertRaises(ApiError) as raised:
            self.game.join({"templateId": template["templateId"], "edits": {"name": "毁灭全世界"}})

        self.assertEqual(raised.exception.envelope(), {
            "error": {"code": "REJECTED", "message": "这个名字被世界吞掉了,换一个吧"}
        })
        self.assertEqual(self.query_value("SELECT COUNT(*) FROM characters"), 0)
        self.assertEqual(self.query_value("SELECT used FROM templates WHERE id = ?", (template["templateId"],)), 0)

    def test_story_contract_removes_v1_world_and_location_fields(self):
        template = self.game.template()
        joined = self.game.join({"templateId": template["templateId"]})

        story = self.game.story(after=0)

        self.assertEqual(story["world"], {
            "legend": story["world"]["legend"],
            "clueCount": 1,
            "nextTwistAt": 8,
            "nextRevealAt": 20,
            "actSeq": 1,
        })
        self.assertNotIn("locations", story)
        self.assertNotIn("cycle", story["world"])
        self.assertNotIn("round", story["world"])
        self.assertEqual(story["acts"][0]["id"], joined["firstActId"])
        self.assertEqual(story["acts"][0]["type"], "act")
        self.assertNotIn("location", story["acts"][0])
        self.assertNotIn("directive", story["acts"][0])

    def test_clue_progression_inserts_twist_and_reveal_then_rotates_legend(self):
        for _ in range(8):
            self.game.join({"templateId": self.game.template()["templateId"]})

        story = self.game.story(after=0)
        self.assertEqual(story["world"]["clueCount"], 8)
        self.assertEqual(story["acts"][-1]["type"], "twist")
        twist_legend = story["world"]["legend"]

        for _ in range(12):
            self.game.join({"templateId": self.game.template()["templateId"]})

        final = self.game.story(after=0)
        self.assertEqual(final["world"]["clueCount"], 0)
        self.assertEqual(final["world"]["actSeq"], 22)
        self.assertEqual(final["acts"][-1]["type"], "reveal")
        self.assertNotEqual(final["world"]["legend"], twist_legend)
        kinds = [
            row["kind"]
            for row in self.game.conn.execute("SELECT kind FROM print_queue ORDER BY id").fetchall()
        ]
        self.assertIn("twist", kinds)
        self.assertIn("reveal", kinds)

    def test_beat_uses_cold_characters_and_weaver_context_has_current_legend(self):
        llm = FakeLlmGateway()
        game = GameService(self.db_path, llm=llm)
        first = game.join({"templateId": game.template()["templateId"], "edits": {"name": "甲"}})
        second = game.join({"templateId": game.template()["templateId"], "edits": {"name": "乙"}})
        game.conn.execute("UPDATE characters SET last_seen_act = 0 WHERE id = ?", (first["charId"],))
        game.conn.execute("UPDATE characters SET last_seen_act = 2 WHERE id = ?", (second["charId"],))
        game.conn.commit()

        result = game.enqueue_beat(now=999)
        game.process_next_weave_job()

        story = game.story(after=0)
        self.assertEqual(result["queued"], True)
        self.assertEqual(story["acts"][-1]["type"], "beat")
        self.assertIn(first["charId"], story["acts"][-1]["involved"])
        self.assertIn("legend", llm.contexts[-1]["world"])
        self.assertEqual(len(llm.contexts[-1]["recentActs"]), 2)

    def test_leave_card_mail_and_print_contracts_remain_available(self):
        template = self.game.template()
        joined = self.game.join({"templateId": template["templateId"], "email": "hero@example.com"})

        left = self.game.leave({"charId": joined["charId"]})
        me = self.game.me(joined["charId"])
        card = self.game.card(joined["charId"])

        self.assertEqual(left, {"cardUrl": f"/card/{joined['charId']}"})
        self.assertEqual(me["status"], "ended")
        self.assertEqual(me["timeline"][0]["actId"], joined["firstActId"])
        self.assertEqual(card["name"], joined["name"])
        self.assertNotIn("cycle", card)
        self.assertEqual(self.query_value("SELECT kind FROM print_queue WHERE kind = 'ending'"), "ending")

    def test_print_failure_leaves_job_pending_for_retry(self):
        printer = FakePrinterDriver()
        printer.fail_next = True
        game = GameService(self.db_path, printer_driver=printer)
        game.join({"templateId": game.template()["templateId"]})

        first = game.process_next_print_job()
        second = game.process_next_print_job()

        self.assertEqual(first["status"], "pending")
        self.assertEqual(second["status"], "printed")
        self.assertEqual(len(printer.printed), 1)

    def test_mail_failure_leaves_job_pending_for_retry(self):
        mail = FakeMailTransport()
        mail.fail_next = True
        previous_whitelist = os.environ.get("MAIL_WHITELIST")
        os.environ["MAIL_WHITELIST"] = "hero@example.com"
        game = GameService(self.db_path, mail_transport=mail)
        try:
            joined = game.join({
                "templateId": game.template()["templateId"],
                "email": "hero@example.com",
            })
            game.leave({"charId": joined["charId"]})
            game.record_ending_mail(joined["charId"])

            first = game.process_next_mail_job()
            second = game.process_next_mail_job()

            self.assertEqual(first["status"], "pending")
            self.assertEqual(second["status"], "sent")
            self.assertEqual(len(mail.sent), 1)
        finally:
            if previous_whitelist is None:
                os.environ.pop("MAIL_WHITELIST", None)
            else:
                os.environ["MAIL_WHITELIST"] = previous_whitelist


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

    def request(self, method, path, payload=None):
        host, port = self.server.server_address
        conn = HTTPConnection(host, port, timeout=2)
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"} if body is not None else {}
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        data = response.read()
        conn.close()
        parsed = json.loads(data.decode("utf-8")) if data else None
        return response.status, parsed

    def test_http_template_join_story_and_removed_act_route(self):
        status, template = self.request("GET", "/api/template")
        self.assertEqual(status, 200)

        status, joined = self.request("POST", "/api/join", {"templateId": template["templateId"]})
        self.assertEqual(status, 200)
        self.assertIn("firstActId", joined)

        status, story = self.request("GET", "/api/story?after=0")
        self.assertEqual(status, 200)
        self.assertEqual(story["world"]["clueCount"], 1)

        status, rejected = self.request("POST", "/api/act", {
            "charId": joined["charId"],
            "text": "旧接口不再推进世界",
            "kind": "action",
        })
        self.assertEqual(status, 404)
        self.assertEqual(rejected["error"]["code"], "NOT_FOUND")


class RuntimeTest(unittest.TestCase):
    def test_background_runtime_processes_print_and_beats_until_stopped(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "game.sqlite")
            printer = FakePrinterDriver()
            game = GameService(db_path, printer_driver=printer)
            game.join({"templateId": game.template()["templateId"]})
            runtime = BackgroundRuntime(
                game,
                beat_interval=0.01,
                print_interval=0.01,
                mail_interval=0.01,
                now_func=lambda: 100,
            )

            runtime.start()
            try:
                self.assertTrue(runtime.wait_until_idle(timeout=2))
            finally:
                runtime.stop(timeout=2)
                game.close()

            self.assertGreaterEqual(len(printer.printed), 1)

    def test_weave_workers_process_global_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "game.sqlite")
            game = GameService(db_path)
            game.join({"templateId": game.template()["templateId"]})
            runtime = WeaveWorkerRuntime(game, slots=2)

            runtime.start()
            try:
                queued = game.enqueue_beat(now=123)
                self.assertTrue(queued["queued"])
                self.assertTrue(runtime.wait_until_idle(timeout=2))
            finally:
                runtime.stop(timeout=2)

            self.assertEqual(game.story(after=0)["acts"][-1]["type"], "beat")
            game.close()


class AsyncGameServiceTest(unittest.TestCase):
    def test_async_wrapper_delegates_v2_methods_without_location_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = GameService(str(Path(tmp) / "game.sqlite"))
            service = AsyncGameService(game, None)

            template = service.template()
            joined = service.join({"templateId": template["templateId"]})

            self.assertEqual(service.story()["acts"][0]["id"], joined["firstActId"])
            game.close()


if __name__ == "__main__":
    unittest.main()
