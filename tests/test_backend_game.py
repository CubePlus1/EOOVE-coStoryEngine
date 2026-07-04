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
from server.rate_limit import RateLimiter
from server.runtime import BackgroundRuntime, LocationWorkerRuntime


class FakeLlmGateway:
    def __init__(self):
        self.configured = True
        self.character_results = []
        self.moderation_results = []
        self.weaver_results = []
        self.ending_results = []
        self.echo_results = []

    def generate_character(self, self_desc):
        if self.character_results:
            result = self.character_results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        return None

    def moderate(self, payload):
        if self.moderation_results:
            result = self.moderation_results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        return None

    def weave(self, context):
        if self.weaver_results:
            result = self.weaver_results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        return None

    def generate_ending(self, character, world):
        if self.ending_results:
            result = self.ending_results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        return None

    def generate_echo(self, chronicle, world):
        if self.echo_results:
            result = self.echo_results.pop(0)
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
            driver = CommandPrinterDriver(
                command=f"cat > {output}",
                timeout=1,
            )

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
        self.tmp.cleanup()

    def query_one(self, sql, params=()):
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(sql, params).fetchone()

    def query_value(self, sql, params=()):
        row = self.query_one(sql, params)
        return None if row is None else row[0]

    def test_initializes_schema_and_default_world(self):
        world = self.query_one("SELECT cycle, round, hope, status FROM world WHERE id = 1")

        self.assertEqual(dict(world), {"cycle": 1, "round": 0, "hope": 40, "status": "running"})
        for table in [
            "characters",
            "acts",
            "oracle_pool",
            "triples",
            "print_queue",
            "mail_queue",
            "inputs_log",
        ]:
            self.assertEqual(self.query_value(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,),
            ), table)

    def test_join_creates_character_print_job_and_story_contract(self):
        joined = self.game.join({"selfDesc": "一个修理无线电的守夜人", "email": "hero@example.com"})

        self.assertRegex(joined["charId"], r"^c_[0-9a-f]{8}$")
        self.assertEqual(joined["cycle"], 1)
        self.assertIn(joined["location"], {"loc_shelter", "loc_ruins", "loc_observatory"})
        self.assertTrue(joined["name"])
        self.assertIn("一个修理无线电的守夜人", joined["profile"])
        self.assertEqual(self.query_value("SELECT kind FROM print_queue"), "charcard")

        story = self.game.story(after=0)
        self.assertEqual(story["world"], {
            "cycle": 1,
            "round": 0,
            "maxRound": 60,
            "hopeHint": "mid",
            "status": "running",
        })
        self.assertEqual([loc["id"] for loc in story["locations"]], [
            "loc_shelter",
            "loc_ruins",
            "loc_observatory",
        ])
        self.assertEqual(story["characters"][0]["charId"], joined["charId"])

    def test_join_uses_llm_character_when_available_and_falls_back_on_error(self):
        llm = FakeLlmGateway()
        llm.character_results.append({
            "name": "灯塔译者",
            "profile": "灯塔译者: 能把静电翻成预言。",
        })
        game = GameService(self.db_path, llm=llm)

        joined = game.join({"selfDesc": "会听懂电流的人"})

        self.assertEqual(joined["name"], "灯塔译者")
        self.assertEqual(joined["profile"], "灯塔译者: 能把静电翻成预言。")

        llm.character_results.append(RuntimeError("model timeout"))
        fallback = game.join({"selfDesc": "备用角色"})

        self.assertIn("备用角色", fallback["profile"])
        self.assertNotEqual(fallback["name"], "灯塔译者")

    def test_action_advances_round_creates_incremental_story_and_personal_timeline(self):
        joined = self.game.join({"selfDesc": "废墟里的拾荒者"})

        result = self.game.act({
            "charId": joined["charId"],
            "text": "我撬开锈门寻找水泵",
            "kind": "action",
        })

        self.assertEqual(result, {"accepted": True, "round": 1})
        story = self.game.story(after=0)
        self.assertEqual(story["world"]["round"], 1)
        self.assertEqual(len(story["acts"]), 1)
        act = story["acts"][0]
        self.assertEqual(act["round"], 1)
        self.assertEqual(act["type"], "act")
        self.assertEqual(act["involved"], [joined["charId"]])
        self.assertIn("我撬开锈门寻找水泵", act["narrative"])
        self.assertEqual(self.game.story(after=act["id"])["acts"], [])

        me = self.game.me(joined["charId"])
        self.assertEqual(me["status"], "active")
        self.assertEqual(me["timeline"], [{
            "actId": act["id"],
            "round": 1,
            "text": f"你让命运记住了: 我撬开锈门寻找水泵",
        }])

    def test_business_rejection_does_not_advance_round(self):
        joined = self.game.join({"selfDesc": "观测站学徒"})

        result = self.game.act({
            "charId": joined["charId"],
            "text": "取消第20轮节点事件",
            "kind": "oracle",
            "scope": "global",
        })

        self.assertFalse(result["accepted"])
        self.assertIn("节点", result["reason"])
        self.assertEqual(self.game.story(after=0)["world"]["round"], 0)

    def test_llm_moderation_rejection_does_not_advance_round(self):
        llm = FakeLlmGateway()
        llm.moderation_results.append({"pass": False, "reason": "群星拒绝这句低语。"})
        game = GameService(self.db_path, llm=llm)
        joined = game.join({"selfDesc": "会触碰禁语的人"})

        result = game.act({
            "charId": joined["charId"],
            "text": "这是一句需要审核拒绝的话",
            "kind": "action",
        })

        self.assertEqual(result, {"accepted": False, "reason": "群星拒绝这句低语。"})
        self.assertEqual(game.story(after=0)["world"]["round"], 0)

    def test_oracle_quota_applies_per_round_and_pools_when_slot_is_taken(self):
        first = self.game.join({"selfDesc": "第一个神谕者"})
        second = self.game.join({"selfDesc": "第二个神谕者"})

        applied = self.game.act({
            "charId": first["charId"],
            "text": "让孩子们唱歌",
            "kind": "oracle",
            "scope": "global",
        })
        self.assertEqual(applied, {"accepted": True, "round": 1, "oracleStatus": "applied"})

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO oracle_pool (char_id, scope, text, round_submitted, status)
                VALUES (?, 'global', '预占下一轮神谕', 2, 'applied')
                """,
                (first["charId"],),
            )
            conn.commit()

        pooled = self.game.act({
            "charId": second["charId"],
            "text": "让火焰指向北方",
            "kind": "oracle",
            "scope": "global",
        })

        self.assertEqual(pooled, {"accepted": True, "round": 2, "oracleStatus": "pooled"})
        statuses = [
            row[0] for row in sqlite3.connect(self.db_path)
            .execute("SELECT status FROM oracle_pool ORDER BY id")
            .fetchall()
        ]
        self.assertEqual(statuses, ["applied", "applied", "pooled"])

    def test_pooled_oracle_is_promoted_fifo_when_next_weave_has_no_new_oracle(self):
        first = self.game.join({"selfDesc": "第一个神谕者"})
        second = self.game.join({"selfDesc": "第二个神谕者"})

        self.game.act({
            "charId": first["charId"],
            "text": "让孩子们唱歌",
            "kind": "oracle",
            "scope": "global",
        })
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO oracle_pool (char_id, scope, text, round_submitted, status)
                VALUES (?, 'global', '让火焰指向北方', 1, 'pooled')
                """,
                (second["charId"],),
            )
            conn.commit()

        self.game.act({
            "charId": second["charId"],
            "text": "我跟随火焰的影子前进",
            "kind": "action",
        })

        with sqlite3.connect(self.db_path) as conn:
            promoted = conn.execute(
                "SELECT status FROM oracle_pool WHERE text = '让火焰指向北方'"
            ).fetchone()[0]
            act_oracle = conn.execute(
                "SELECT oracle_applied FROM acts WHERE type = 'act' ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]
        self.assertEqual(promoted, "applied")
        self.assertEqual(act_oracle, "让火焰指向北方")

    def test_weaver_schema_applies_state_triples_and_cross_location_seed(self):
        joined = self.game.join({"selfDesc": "废墟信使"})

        result = self.game.act({
            "charId": joined["charId"],
            "text": "我把烟柱消息带去观测站",
            "kind": "action",
            "weaverResult": {
                "narrative": "信使穿过废墟,把烟柱的方向刻在铁片上。",
                "directive": {"situation": "烟柱指向观测站", "hint": "追踪烟柱来源"},
                "chronicle": "信使记录烟柱。",
                "involved": [joined["charId"]],
                "personal": {joined["charId"]: "你把烟柱方向刻在铁片上。"},
                "importance": 4,
                "hopeDelta": 3,
                "stateChanges": {
                    "characters": {
                        joined["charId"]: {"location": "loc_observatory", "note": "携带烟柱铁片"}
                    }
                },
                "newTriples": [[joined["charId"], "携带", "烟柱铁片"]],
                "crossLocation": [{"to": "loc_shelter", "seed": "废墟方向升起烟柱"}],
                "print": {"worthy": True, "ticket": "烟柱告示"},
            },
        })

        self.assertEqual(result, {"accepted": True, "round": 1})
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            char = conn.execute(
                "SELECT location FROM characters WHERE id = ?",
                (joined["charId"],),
            ).fetchone()
            triple = conn.execute(
                "SELECT subject, relation, object FROM triples WHERE relation = '携带'"
            ).fetchone()
            queued = conn.execute(
                "SELECT location, seed, status FROM location_seed_queue"
            ).fetchone()
            act = conn.execute("SELECT narrative, hope_delta FROM acts WHERE type = 'act'").fetchone()
        self.assertEqual(char["location"], "loc_observatory")
        self.assertEqual(tuple(triple), (joined["charId"], "携带", "烟柱铁片"))
        self.assertEqual(tuple(queued), ("loc_shelter", "废墟方向升起烟柱", "pending"))
        self.assertEqual(tuple(act), ("信使穿过废墟,把烟柱的方向刻在铁片上。", 3))

    def test_cross_location_seed_is_consumed_by_target_location_without_extra_round(self):
        shelter = self.game.join({"selfDesc": "避难所守门人"})
        ruins = self.game.join({"selfDesc": "废墟巡逻者"})
        observatory = self.game.join({"selfDesc": "观测站记录员"})
        self.assertEqual(shelter["location"], "loc_shelter")
        self.assertEqual(ruins["location"], "loc_ruins")
        self.assertEqual(observatory["location"], "loc_observatory")
        self.game.act({
            "charId": ruins["charId"],
            "text": "我看见烟柱",
            "kind": "action",
            "weaverResult": {
                "narrative": "废墟巡逻者看见烟柱升起。",
                "directive": {"situation": "废墟有烟", "hint": "辨认烟的方向"},
                "chronicle": "废墟生烟。",
                "involved": [ruins["charId"]],
                "personal": {ruins["charId"]: "你看见烟柱升起。"},
                "importance": 2,
                "hopeDelta": 0,
                "stateChanges": {},
                "newTriples": [],
                "crossLocation": [{"to": "loc_shelter", "seed": "废墟方向升起烟柱"}],
                "print": {"worthy": False, "ticket": None},
            },
        })

        result = self.game.act({
            "charId": shelter["charId"],
            "text": "我检查避难所外墙",
            "kind": "action",
        })

        self.assertEqual(result, {"accepted": True, "round": 2})
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            latest = conn.execute(
                "SELECT narrative FROM acts WHERE type = 'act' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            seed_status = conn.execute("SELECT status FROM location_seed_queue").fetchone()
        self.assertIn("废墟方向升起烟柱", latest["narrative"])
        self.assertEqual(seed_status["status"], "consumed")

    def test_invalid_weaver_output_falls_back_without_breaking_pipeline(self):
        joined = self.game.join({"selfDesc": "故障见证者"})

        result = self.game.act({
            "charId": joined["charId"],
            "text": "我尝试描述无法解析的梦",
            "kind": "action",
            "weaverResult": {
                "narrative": "",
                "directive": {},
                "chronicle": "",
                "involved": [],
                "personal": {},
                "importance": 9,
                "hopeDelta": 99,
            },
        })

        self.assertEqual(result, {"accepted": True, "round": 1})
        story = self.game.story(after=0)
        self.assertEqual(story["acts"][0]["narrative"], "世界轻轻震颤了一下")
        self.assertEqual(story["acts"][0]["importance"], 1)
        self.assertEqual(story["acts"][0]["involved"], [joined["charId"]])

    def test_llm_weaver_retries_once_then_uses_valid_result(self):
        llm = FakeLlmGateway()
        game = GameService(self.db_path, llm=llm)
        joined = game.join({"selfDesc": "点灯的人"})
        llm.weaver_results.extend([
            RuntimeError("invalid json"),
            {
                "narrative": "灯被重新点亮,墙上的地图显出暗河。",
                "directive": {"situation": "暗河路线显现", "hint": "确认水源"},
                "chronicle": "地图显出暗河。",
                "involved": [joined["charId"]],
                "personal": {},
                "importance": 4,
                "hopeDelta": 2,
                "stateChanges": {},
                "newTriples": [],
                "crossLocation": [],
                "print": {"worthy": True, "ticket": "暗河"},
            },
        ])

        result = game.act({
            "charId": joined["charId"],
            "text": "我点亮墙上的地图",
            "kind": "action",
        })

        self.assertEqual(result, {"accepted": True, "round": 1})
        story = game.story(after=0)
        self.assertEqual(story["acts"][0]["narrative"], "灯被重新点亮,墙上的地图显出暗河。")
        self.assertEqual(story["acts"][0]["involved"], [joined["charId"]])

    def test_llm_weaver_falls_back_after_two_failures(self):
        llm = FakeLlmGateway()
        llm.weaver_results.extend([
            RuntimeError("invalid json"),
            RuntimeError("invalid json again"),
        ])
        game = GameService(self.db_path, llm=llm)
        joined = game.join({"selfDesc": "见证故障的人"})

        result = game.act({
            "charId": joined["charId"],
            "text": "我等待模型恢复",
            "kind": "action",
        })

        self.assertEqual(result, {"accepted": True, "round": 1})
        story = game.story(after=0)
        self.assertEqual(story["acts"][0]["narrative"], "世界轻轻震颤了一下")

    def test_heartbeat_creates_low_importance_heartbeat_act_without_printing(self):
        joined = self.game.join({"selfDesc": "守着水泵的人"})

        result = self.game.heartbeat("水管深处传来短促回声")

        self.assertEqual(result, {"accepted": True, "round": 1})
        with sqlite3.connect(self.db_path) as conn:
            act = conn.execute(
                "SELECT type, importance, involved_json FROM acts ORDER BY id DESC LIMIT 1"
            ).fetchone()
            print_kinds = [
                row[0] for row in conn.execute("SELECT kind FROM print_queue ORDER BY id").fetchall()
            ]
        self.assertEqual(act[0], "heartbeat")
        self.assertLessEqual(act[1], 2)
        self.assertEqual(json.loads(act[2]), [joined["charId"]])
        self.assertEqual(print_kinds, ["charcard"])

    def test_print_queue_drops_old_bulletins_when_backlog_exceeds_five(self):
        joined = self.game.join({"selfDesc": "不断发出神谕的人"})

        for index in range(7):
            self.game.act({
                "charId": joined["charId"],
                "text": f"让第{index}束希望点亮",
                "kind": "oracle",
                "scope": "global",
            })

        with sqlite3.connect(self.db_path) as conn:
            dropped = conn.execute(
                "SELECT COUNT(*) FROM print_queue WHERE kind = 'bulletin' AND status = 'dropped'"
            ).fetchone()[0]
            active_protected = conn.execute(
                """
                SELECT COUNT(*) FROM print_queue
                WHERE kind IN ('charcard', 'apocalypse', 'ending') AND status = 'pending'
                """
            ).fetchone()[0]
        self.assertGreaterEqual(dropped, 1)
        self.assertEqual(active_protected, 1)

    def test_node_events_and_settlement_rollover_are_persisted(self):
        joined = self.game.join({"selfDesc": "轮回见证者"})

        for index in range(60):
            self.game.act({
                "charId": joined["charId"],
                "text": f"行动 {index}",
                "kind": "action",
            })

        story = self.game.story(after=0)
        with sqlite3.connect(self.db_path) as conn:
            node_rounds = [
                row[0] for row in conn.execute(
                    "SELECT DISTINCT round FROM acts WHERE type = 'node' ORDER BY round"
                ).fetchall()
            ]
            node_count = conn.execute(
                "SELECT COUNT(*) FROM acts WHERE type = 'node'"
            ).fetchone()[0]
            settlement_count = conn.execute(
                "SELECT COUNT(*) FROM acts WHERE type = 'settlement'"
            ).fetchone()[0]
        self.assertEqual(node_rounds, [20, 40, 60])
        self.assertEqual(node_count, 9)
        self.assertEqual(settlement_count, 1)
        self.assertEqual(story["world"]["cycle"], 2)
        self.assertEqual(story["world"]["round"], 0)
        self.assertEqual(story["world"]["status"], "running")
        self.assertEqual(story["world"]["hopeHint"], "mid")
        self.assertEqual(self.query_value("SELECT COUNT(*) FROM print_queue WHERE kind = 'apocalypse'"), 3)
        self.assertEqual(
            [loc["directive"]["situation"] for loc in story["locations"]],
            [
                "地下水源正在枯竭,居民出现幻觉",
                "废墟深处传出规律的敲击声",
                "天象仪指向了不存在的星座",
            ],
        )

        with sqlite3.connect(self.db_path) as conn:
            opening = conn.execute(
                """
                SELECT cycle, round, type, narrative FROM acts
                WHERE type = 'settlement' OR (cycle = 2 AND round = 0)
                ORDER BY id DESC LIMIT 1
                """
            ).fetchone()
        self.assertEqual(opening[0], 2)
        self.assertEqual(opening[1], 0)
        self.assertEqual(opening[2], "act")
        self.assertIn("新轮回", opening[3])

    def test_settlement_uses_llm_echo_when_available(self):
        llm = FakeLlmGateway()
        llm.echo_results.append({"echo": "你梦见暗河仍在墙后流动。"})
        game = GameService(self.db_path, llm=llm)
        joined = game.join({"selfDesc": "会记住暗河的人"})

        for index in range(60):
            game.act({
                "charId": joined["charId"],
                "text": f"行动 {index}",
                "kind": "action",
            })

        me = game.me(joined["charId"])
        self.assertEqual(me["echo"], "你梦见暗河仍在墙后流动。")

    def test_leave_marks_character_ended_and_card_is_available(self):
        joined = self.game.join({"selfDesc": "准备退场的旅人"})

        response = self.game.leave({"charId": joined["charId"]})

        self.assertEqual(response, {"cardUrl": f"/card/{joined['charId']}"})
        me = self.game.me(joined["charId"])
        self.assertEqual(me["status"], "ended")
        self.assertIsNotNone(me["ending"])
        card = self.game.card(joined["charId"])
        self.assertEqual(card["name"], joined["name"])
        self.assertEqual(card["ending"], me["ending"])
        self.assertEqual(card["qrUrl"], "/")

    def test_leave_uses_llm_ending_when_available_and_falls_back_on_error(self):
        llm = FakeLlmGateway()
        llm.ending_results.append({"ending": "你把最后一盏灯交给了后来者。"})
        game = GameService(self.db_path, llm=llm)
        joined = game.join({"selfDesc": "会交灯的人"})

        game.leave({"charId": joined["charId"]})

        self.assertEqual(game.me(joined["charId"])["ending"], "你把最后一盏灯交给了后来者。")

        llm.ending_results.append(RuntimeError("model timeout"))
        fallback = game.join({"selfDesc": "备用退场者"})
        game.leave({"charId": fallback["charId"]})

        self.assertIn("离开了这个世界", game.me(fallback["charId"])["ending"])

    def test_unknown_character_raises_not_found_error_envelope(self):
        with self.assertRaises(ApiError) as error:
            self.game.me("c_missing")

        self.assertEqual(error.exception.code, "NOT_FOUND")
        self.assertIn("找不到", error.exception.message)


class HttpApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "http.sqlite")
        self.game = GameService(self.db_path)
        handler = create_handler(self.game)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host, self.port = self.server.server_address

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()
        self.tmp.cleanup()

    def request(self, method, path, body=None):
        conn = HTTPConnection(self.host, self.port, timeout=5)
        payload = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json"} if body is not None else {}
        conn.request(method, path, body=payload, headers=headers)
        response = conn.getresponse()
        data = response.read()
        conn.close()
        return response.status, dict(response.getheaders()), json.loads(data.decode("utf-8"))

    def raw_request(self, method, path, payload):
        conn = HTTPConnection(self.host, self.port, timeout=5)
        conn.request(method, path, body=payload, headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        data = response.read()
        conn.close()
        return response.status, json.loads(data.decode("utf-8"))

    def test_http_join_story_act_me_and_card_contract(self):
        status, headers, joined = self.request("POST", "/api/join", {"selfDesc": "会修灯塔的人"})

        self.assertEqual(status, 200)
        self.assertEqual(headers["Access-Control-Allow-Origin"], "*")
        self.assertIn("charId", joined)

        status, _, accepted = self.request("POST", "/api/act", {
            "charId": joined["charId"],
            "text": "我点亮观测站的旧灯",
            "kind": "narration",
        })
        self.assertEqual(status, 200)
        self.assertEqual(accepted, {"accepted": True, "round": 1})

        status, _, story = self.request("GET", "/api/story?after=0")
        self.assertEqual(status, 200)
        self.assertEqual(story["acts"][0]["involved"], [joined["charId"]])

        status, _, me = self.request("GET", f"/api/me/{joined['charId']}")
        self.assertEqual(status, 200)
        self.assertEqual(len(me["timeline"]), 1)

        status, _, left = self.request("POST", "/api/leave", {"charId": joined["charId"]})
        self.assertEqual(status, 200)
        self.assertEqual(left["cardUrl"], f"/card/{joined['charId']}")

        status, _, card = self.request("GET", f"/api/card/{joined['charId']}")
        self.assertEqual(status, 200)
        self.assertIsNotNone(card["ending"])

    def test_http_errors_use_standard_envelope_and_options_supports_cors(self):
        status, _, missing = self.request("GET", "/api/me/c_missing")
        self.assertEqual(status, 404)
        self.assertEqual(missing["error"]["code"], "NOT_FOUND")

        conn = HTTPConnection(self.host, self.port, timeout=5)
        conn.request("OPTIONS", "/api/story")
        response = conn.getresponse()
        response.read()
        headers = dict(response.getheaders())
        conn.close()

        self.assertEqual(response.status, 204)
        self.assertEqual(headers["Access-Control-Allow-Origin"], "*")
        self.assertIn("POST", headers["Access-Control-Allow-Methods"])

    def test_http_mail_reply_ingestion_contract(self):
        status, _, joined = self.request("POST", "/api/join", {
            "selfDesc": "会从邮件回来的旅人",
            "email": "reply@example.com",
        })
        self.assertEqual(status, 200)

        status, _, accepted = self.request("POST", "/api/mail/reply", {
            "email": "reply@example.com",
            "text": "我沿着邮件里的灯回来",
        })

        self.assertEqual(status, 200)
        self.assertEqual(accepted, {"accepted": True, "round": 1})
        status, _, story = self.request("GET", "/api/story?after=0")
        self.assertEqual(status, 200)
        self.assertEqual(story["acts"][0]["involved"], [joined["charId"]])
        self.assertIn("我沿着邮件里的灯回来", story["acts"][0]["narrative"])

    def test_http_rejects_oversized_json_body_before_dispatch(self):
        self.server.RequestHandlerClass.max_body_bytes = 20

        status, payload = self.raw_request("POST", "/api/join", b'{"selfDesc":"' + b"x" * 64 + b'"}')

        self.assertEqual(status, 413)
        self.assertEqual(payload["error"]["code"], "REJECTED")
        self.assertEqual(self.game.story(after=0)["characters"], [])

    def test_http_act_rate_limit_rejects_without_advancing_round(self):
        limiter = RateLimiter(limit=1, window_seconds=60, now_func=lambda: 100)
        self.server.RequestHandlerClass.rate_limiter = limiter
        status, _, joined = self.request("POST", "/api/join", {"selfDesc": "限流测试者"})
        self.assertEqual(status, 200)

        first_status, _, first = self.request("POST", "/api/act", {
            "charId": joined["charId"],
            "text": "我第一次行动",
            "kind": "action",
        })
        second_status, _, second = self.request("POST", "/api/act", {
            "charId": joined["charId"],
            "text": "我第二次行动",
            "kind": "action",
        })

        self.assertEqual(first_status, 200)
        self.assertEqual(first, {"accepted": True, "round": 1})
        self.assertEqual(second_status, 429)
        self.assertEqual(second["error"]["code"], "QUOTA")
        self.assertEqual(self.game.story(after=0)["world"]["round"], 1)

    def test_http_act_can_ack_before_location_worker_persists_act(self):
        runtime = LocationWorkerRuntime(self.game)
        async_service = AsyncGameService(self.game, runtime)
        self.server.RequestHandlerClass.service = async_service
        try:
            status, _, joined = self.request("POST", "/api/join", {"selfDesc": "异步行动者"})
            self.assertEqual(status, 200)

            status, _, accepted = self.request("POST", "/api/act", {
                "charId": joined["charId"],
                "text": "我把行动交给地点编织器",
                "kind": "action",
            })

            self.assertEqual(status, 200)
            self.assertEqual(accepted, {"accepted": True, "round": 1})
            status, _, story_before = self.request("GET", "/api/story?after=0")
            self.assertEqual(status, 200)
            self.assertEqual(story_before["world"]["round"], 1)
            self.assertEqual(story_before["acts"], [])
            runtime.start()
            self.assertTrue(runtime.wait_until_idle(timeout=1))
            status, _, story_after = self.request("GET", "/api/story?after=0")
            self.assertEqual(status, 200)
            self.assertEqual(len(story_after["acts"]), 1)
            self.assertIn("我把行动交给地点编织器", story_after["acts"][0]["narrative"])
        finally:
            runtime.stop(timeout=1)

    def test_http_mail_reply_works_with_async_production_service(self):
        runtime = LocationWorkerRuntime(self.game)
        async_service = AsyncGameService(self.game, runtime)
        self.server.RequestHandlerClass.service = async_service
        try:
            status, _, joined = self.request("POST", "/api/join", {
                "selfDesc": "异步邮件旅人",
                "email": "async-reply@example.com",
            })
            self.assertEqual(status, 200)

            status, _, accepted = self.request("POST", "/api/mail/reply", {
                "email": "async-reply@example.com",
                "text": "我从异步入口回信",
            })

            self.assertEqual(status, 200)
            self.assertEqual(accepted, {"accepted": True, "round": 1})
            runtime.start()
            self.assertTrue(runtime.wait_until_idle(timeout=1))
            status, _, story = self.request("GET", "/api/story?after=0")
            self.assertEqual(status, 200)
            self.assertEqual(story["acts"][0]["involved"], [joined["charId"]])
        finally:
            runtime.stop(timeout=1)

    def test_async_act_rejects_new_input_after_round_sixty_is_queued(self):
        joined = self.game.join({"selfDesc": "异步终局见证者"})
        runtime = LocationWorkerRuntime(self.game)
        async_service = AsyncGameService(self.game, runtime)

        for index in range(60):
            accepted = async_service.act({
                "charId": joined["charId"],
                "text": f"异步行动 {index}",
                "kind": "action",
            })

        self.assertEqual(accepted, {"accepted": True, "round": 60})
        with self.assertRaises(ApiError) as error:
            async_service.act({
                "charId": joined["charId"],
                "text": "第61次行动不应进入终局队列",
                "kind": "action",
            })
        self.assertEqual(error.exception.code, "SETTLING")


class SkillsAndSchedulerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "skills.sqlite")
        self.original_whitelist = os.environ.get("MAIL_WHITELIST")
        os.environ["MAIL_WHITELIST"] = "hero@example.com"
        self.game = GameService(self.db_path)

    def tearDown(self):
        if self.original_whitelist is None:
            os.environ.pop("MAIL_WHITELIST", None)
        else:
            os.environ["MAIL_WHITELIST"] = self.original_whitelist
        self.tmp.cleanup()

    def rows(self, sql, params=()):
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(sql, params).fetchall()

    def test_important_act_for_ended_email_character_records_whitelist_mail_once_per_hour(self):
        joined = self.game.join({"selfDesc": "会收到信的人", "email": "hero@example.com"})
        self.game.leave({"charId": joined["charId"]})

        self.game.record_mail_for_act(
            involved=[joined["charId"]],
            importance=4,
            in_world_reason="你曾经点亮的灯又亮了。",
            now=100,
        )
        self.game.record_mail_for_act(
            involved=[joined["charId"]],
            importance=5,
            in_world_reason="第二封太近了。",
            now=200,
        )
        self.game.record_mail_for_act(
            involved=[joined["charId"]],
            importance=5,
            in_world_reason="一小时后,世界再次呼唤你。",
            now=3701,
        )

        mails = self.rows("SELECT char_id, email, status, in_world_reason FROM mail_queue ORDER BY id")
        self.assertEqual(len(mails), 2)
        self.assertEqual([mail["status"] for mail in mails], ["pending", "pending"])
        self.assertEqual([mail["email"] for mail in mails], ["hero@example.com", "hero@example.com"])
        self.assertIn("直接回复这封邮件", mails[0]["in_world_reason"])
        self.assertIn("一小时后", mails[1]["in_world_reason"])

    def test_non_whitelisted_ending_mail_is_recorded_as_simulated_and_exempt_from_rate_limit(self):
        joined = self.game.join({"selfDesc": "白名单外的人", "email": "outsider@example.com"})

        self.game.leave({"charId": joined["charId"]})
        self.game.record_ending_mail(joined["charId"], now=100)
        self.game.record_ending_mail(joined["charId"], now=101)

        mails = self.rows("SELECT email, status, kind FROM mail_queue ORDER BY id")
        self.assertEqual(len(mails), 2)
        self.assertEqual([mail["status"] for mail in mails], ["simulated", "simulated"])
        self.assertEqual([mail["kind"] for mail in mails], ["ending", "ending"])

    def test_mail_transport_sends_pending_mail_and_skips_simulated_mail(self):
        transport = FakeMailTransport()
        game = GameService(self.db_path, mail_transport=transport)
        joined = game.join({"selfDesc": "会收到真实信的人", "email": "hero@example.com"})
        game.leave({"charId": joined["charId"]})
        game.record_ending_mail(joined["charId"], now=100)
        outsider = game.join({"selfDesc": "白名单外的人", "email": "outsider@example.com"})
        game.leave({"charId": outsider["charId"]})
        game.record_ending_mail(outsider["charId"], now=101)

        sent = game.process_next_mail_job()
        skipped = game.process_next_mail_job()

        self.assertEqual(sent["status"], "sent")
        self.assertEqual(transport.sent[0]["to"], "hero@example.com")
        self.assertIn("直接回复这封邮件", transport.sent[0]["body"])
        self.assertIsNone(skipped)
        statuses = self.rows("SELECT email, status FROM mail_queue ORDER BY id")
        self.assertEqual(
            [(row["email"], row["status"]) for row in statuses],
            [("hero@example.com", "sent"), ("outsider@example.com", "simulated")],
        )

    def test_simulated_mail_does_not_block_later_pending_mail(self):
        transport = FakeMailTransport()
        game = GameService(self.db_path, mail_transport=transport)
        outsider = game.join({"selfDesc": "先进入的白名单外的人", "email": "outsider@example.com"})
        game.leave({"charId": outsider["charId"]})
        game.record_ending_mail(outsider["charId"], now=100)
        hero = game.join({"selfDesc": "后进入的白名单用户", "email": "hero@example.com"})
        game.leave({"charId": hero["charId"]})
        game.record_ending_mail(hero["charId"], now=101)

        sent = game.process_next_mail_job()

        self.assertEqual(sent["email"], "hero@example.com")
        self.assertEqual(sent["status"], "sent")
        self.assertEqual(len(transport.sent), 1)

    def test_failed_mail_send_stays_pending_for_retry(self):
        transport = FakeMailTransport()
        transport.fail_next = True
        game = GameService(self.db_path, mail_transport=transport)
        joined = game.join({"selfDesc": "需要重试邮件的人", "email": "hero@example.com"})
        game.leave({"charId": joined["charId"]})
        game.record_ending_mail(joined["charId"], now=100)

        failed = game.process_next_mail_job()
        retried = game.process_next_mail_job()

        self.assertEqual(failed["status"], "pending")
        self.assertEqual(retried["status"], "sent")
        self.assertEqual(len(transport.sent), 1)

    def test_email_reply_ingestion_creates_narration_for_matching_character(self):
        joined = self.game.join({"selfDesc": "会回信的人", "email": "hero@example.com"})

        result = self.game.ingest_mail_reply("hero@example.com", "我从邮件里点亮旧灯")

        self.assertEqual(result, {"accepted": True, "round": 1})
        story = self.game.story(after=0)
        self.assertEqual(story["world"]["round"], 1)
        self.assertIn("我从邮件里点亮旧灯", story["acts"][0]["narrative"])
        self.assertEqual(story["acts"][0]["involved"], [joined["charId"]])

    def test_print_worker_processes_pending_jobs_in_order_and_skips_dropped(self):
        joined = self.game.join({"selfDesc": "要打印的人"})
        self.game.act({
            "charId": joined["charId"],
            "text": "让希望点亮墙上的地图",
            "kind": "oracle",
            "scope": "global",
        })
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO print_queue (kind, payload_json, status) VALUES ('bulletin', '{}', 'dropped')"
            )
            conn.commit()

        first = self.game.process_next_print_job()
        second = self.game.process_next_print_job()
        third = self.game.process_next_print_job()

        self.assertEqual(first["kind"], "charcard")
        self.assertEqual(second["kind"], "bulletin")
        self.assertIsNone(third)
        statuses = self.rows("SELECT kind, status FROM print_queue ORDER BY id")
        self.assertEqual(
            [(row["kind"], row["status"]) for row in statuses],
            [("charcard", "printed"), ("bulletin", "printed"), ("bulletin", "dropped")],
        )

    def test_print_worker_sends_pending_job_to_printer_driver(self):
        printer = FakePrinterDriver()
        game = GameService(self.db_path, printer_driver=printer)
        joined = game.join({"selfDesc": "需要真实打印的人"})

        printed = game.process_next_print_job()

        self.assertEqual(printed["status"], "printed")
        self.assertEqual(printer.printed[0]["kind"], "charcard")
        self.assertEqual(printer.printed[0]["payload"]["charId"], joined["charId"])
        statuses = self.rows("SELECT kind, status FROM print_queue ORDER BY id")
        self.assertEqual([(row["kind"], row["status"]) for row in statuses], [("charcard", "printed")])

    def test_failed_printer_job_stays_pending_for_retry(self):
        printer = FakePrinterDriver()
        printer.fail_next = True
        game = GameService(self.db_path, printer_driver=printer)
        game.join({"selfDesc": "打印机离线时加入的人"})

        failed = game.process_next_print_job()
        retried = game.process_next_print_job()

        self.assertEqual(failed["status"], "pending")
        self.assertEqual(retried["status"], "printed")
        self.assertEqual(len(printer.printed), 1)

    def test_default_unconfigured_printer_still_marks_jobs_printed_for_local_demo(self):
        joined = self.game.join({"selfDesc": "本地演示打印的人"})

        printed = self.game.process_next_print_job()

        self.assertEqual(printed["status"], "printed")
        self.assertEqual(printed["payload"]["charId"], joined["charId"])

    def test_idle_heartbeat_triggers_after_threshold_and_not_before(self):
        joined = self.game.join({"selfDesc": "等待心跳的人"})
        self.game.last_accepted_at = 1
        self.assertIsNone(self.game.maybe_heartbeat(now=9))

        result = self.game.maybe_heartbeat(now=11)

        self.assertEqual(result, {"accepted": True, "round": 1})
        self.assertIsNone(self.game.maybe_heartbeat(now=15))
        story = self.game.story(after=0)
        self.assertEqual(story["acts"][0]["type"], "heartbeat")
        self.assertEqual(story["acts"][0]["involved"], [joined["charId"]])


class RuntimeLoopTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "runtime.sqlite")
        self.game = GameService(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_background_runtime_processes_print_jobs_and_idle_heartbeat_until_stopped(self):
        joined = self.game.join({"selfDesc": "等待后台循环的人"})
        self.game.last_accepted_at = 1
        runtime = BackgroundRuntime(
            self.game,
            heartbeat_interval=0.01,
            print_interval=0.01,
            now_func=lambda: 12,
        )

        runtime.start()
        runtime.wait_until_idle(timeout=1)
        runtime.stop(timeout=1)

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            print_status = conn.execute("SELECT status FROM print_queue WHERE kind = 'charcard'").fetchone()
            heartbeat = conn.execute("SELECT type, involved_json FROM acts WHERE type = 'heartbeat'").fetchone()
        self.assertEqual(print_status["status"], "printed")
        self.assertEqual(json.loads(heartbeat["involved_json"]), [joined["charId"]])
        self.assertFalse(runtime.is_running())

    def test_background_runtime_can_stop_without_starting_work_twice(self):
        runtime = BackgroundRuntime(self.game, heartbeat_interval=0.01, print_interval=0.01)

        runtime.start()
        runtime.start()
        runtime.stop(timeout=1)

        self.assertFalse(runtime.is_running())

    def test_location_workers_serialize_same_location_and_parallelize_different_locations(self):
        shelter = self.game.join({"selfDesc": "避难所守门人"})
        ruins = self.game.join({"selfDesc": "废墟巡逻者"})
        runtime = LocationWorkerRuntime(self.game)
        started = []
        release = {
            "shelter-1": threading.Event(),
            "shelter-2": threading.Event(),
            "ruins-1": threading.Event(),
        }

        def handler(job):
            label = job["label"]
            started.append(label)
            release[label].wait(timeout=1)
            return {
                "charId": job["charId"],
                "text": job["text"],
                "kind": "action",
                "weaverResult": {
                    "narrative": f"{label} 完成",
                    "directive": {"situation": f"{label} 局势", "hint": "继续"},
                    "chronicle": f"{label} 史记",
                    "involved": [job["charId"]],
                    "personal": {job["charId"]: f"{label} 视角"},
                    "importance": 2,
                    "hopeDelta": 0,
                    "stateChanges": {},
                    "newTriples": [],
                    "crossLocation": [],
                    "print": {"worthy": False, "ticket": None},
                },
            }

        runtime.start(handler=handler)
        runtime.enqueue("loc_shelter", {
            "label": "shelter-1",
            "charId": shelter["charId"],
            "text": "第一件避难所任务",
        })
        runtime.enqueue("loc_shelter", {
            "label": "shelter-2",
            "charId": shelter["charId"],
            "text": "第二件避难所任务",
        })
        runtime.enqueue("loc_ruins", {
            "label": "ruins-1",
            "charId": ruins["charId"],
            "text": "第一件废墟任务",
        })

        self.assertTrue(runtime.wait_for_started(["shelter-1", "ruins-1"], timeout=1))
        self.assertNotIn("shelter-2", started)
        release["shelter-1"].set()
        self.assertTrue(runtime.wait_for_started(["shelter-2"], timeout=1))
        release["shelter-2"].set()
        release["ruins-1"].set()
        self.assertTrue(runtime.wait_until_idle(timeout=1))
        runtime.stop(timeout=1)

        story = self.game.story(after=0)
        narratives = [act["narrative"] for act in story["acts"] if act["type"] == "act"]
        self.assertEqual(set(narratives), {"shelter-1 完成", "shelter-2 完成", "ruins-1 完成"})
        self.assertLess(narratives.index("shelter-1 完成"), narratives.index("shelter-2 完成"))
        self.assertFalse(runtime.is_running())


if __name__ == "__main__":
    unittest.main()
