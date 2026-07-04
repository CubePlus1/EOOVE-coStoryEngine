import json
import re
import threading
import time
import uuid
from pathlib import Path

from .config import get_env
from .db import connect, initialize
from .llm import HttpJsonLlmGateway
from .mail import SmtpMailTransport
from .printer import CommandPrinterDriver


BATCH_TARGET_SIZE = 3
BATCH_MAX_SIZE = 4
BATCH_WINDOW_SECONDS = 45
JOIN_REJECTION_MESSAGE = "这个名字被世界吞掉了,换一个吧"
SENSITIVE_WORDS = ("毁灭", "杀", "血腥", "色情", "政治", "广告", "全世界")


class ApiError(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status

    def envelope(self):
        return {"error": {"code": self.code, "message": self.message}}


class GameService:
    def __init__(
        self,
        db_path="server/db.sqlite",
        llm=None,
        mail_transport=None,
        printer_driver=None,
        legends_path=None,
        incidents_path=None,
        print_token=None,
    ):
        self.db_path = db_path
        self.incidents = self._load_incidents(incidents_path or legends_path)
        self.conn = connect(db_path)
        initialize(self.conn)
        self.llm = llm if llm is not None else HttpJsonLlmGateway()
        self.mail_transport = mail_transport if mail_transport is not None else SmtpMailTransport()
        self.printer_driver = printer_driver if printer_driver is not None else CommandPrinterDriver()
        self.print_token = print_token if print_token is not None else get_env("EOOVE_PRINT_TOKEN", "")
        self.lock = threading.RLock()
        with self.lock:
            self._seed_templates_if_needed()

    def close(self):
        self.conn.close()

    def admin(self):
        with self.lock:
            settings = self._admin_settings()
            stats = {
                "characters": self.conn.execute("SELECT COUNT(*) FROM characters").fetchone()[0],
                "stories": self.conn.execute("SELECT COUNT(*) FROM stories").fetchone()[0],
                "rules": self.conn.execute("SELECT COUNT(*) FROM rules").fetchone()[0],
                "pendingPrintJobs": self.conn.execute(
                    "SELECT COUNT(*) FROM print_queue WHERE status = 'pending'"
                ).fetchone()[0],
            }
            result = self._admin_public(settings)
            result["stats"] = stats
            return result

    def update_admin(self, payload):
        self._require_mapping(payload)
        with self.lock:
            current = self._admin_settings()
            story_background = current["story_background"]
            beat_interval = current["beat_interval_seconds"]
            generation_paused = bool(current["generation_paused"])

            if "storyBackground" in payload:
                story_background = self._bounded_optional_text(payload["storyBackground"], 1200)
            if "beatIntervalSeconds" in payload:
                beat_interval = self._coerce_admin_interval(payload["beatIntervalSeconds"])
            if "generationPaused" in payload:
                if not isinstance(payload["generationPaused"], bool):
                    raise ApiError("REJECTED", "暂停状态必须是真或假。")
                generation_paused = payload["generationPaused"]

            self.conn.execute(
                """
                UPDATE admin_settings
                SET story_background = ?, beat_interval_seconds = ?, generation_paused = ?
                WHERE id = 1
                """,
                (story_background, beat_interval, 1 if generation_paused else 0),
            )
            self.conn.commit()
            return self.admin()

    def reset_story(self, payload):
        self._require_mapping(payload)
        if payload.get("confirm") != "RESET":
            raise ApiError("REJECTED", "重置故事需要输入 RESET。")
        with self.lock:
            self.conn.execute(
                "UPDATE world SET phase = 'running', repair_count = 0, finale_target = 10 WHERE id = 1"
            )
            for table in [
                "characters",
                "batches",
                "batch_members",
                "rules",
                "stories",
                "print_queue",
                "mail_queue",
                "inputs_log",
            ]:
                self.conn.execute(f"DELETE FROM {table}")
            self.conn.execute("UPDATE templates SET used = 0")
            self.conn.commit()
            result = self.admin()
            result["reset"] = True
            return result

    def template(self):
        with self.lock:
            self._seed_templates_if_needed()
            row = self.conn.execute(
                "SELECT * FROM templates WHERE used = 0 ORDER BY id LIMIT 1"
            ).fetchone()
            if row is None:
                self._seed_templates(force=True)
                row = self.conn.execute(
                    "SELECT * FROM templates WHERE used = 0 ORDER BY id LIMIT 1"
                ).fetchone()
            return self._template_public(row)

    def join(self, payload):
        self._require_mapping(payload)
        template_id = payload.get("templateId")
        if not isinstance(template_id, str) or not template_id.strip():
            raise ApiError("REJECTED", "世界没有抽到这张卡。")
        edits = payload.get("edits") or {}
        if not isinstance(edits, dict):
            raise ApiError("REJECTED", JOIN_REJECTION_MESSAGE)

        with self.lock:
            template = self.conn.execute(
                "SELECT * FROM templates WHERE id = ? AND used = 0",
                (template_id,),
            ).fetchone()
            if template is None:
                raise ApiError("NOT_FOUND", "这张卡已经滑进别人的故事。", status=404)
            tags = json.loads(template["tags_json"])
            name = self._edited_name(edits.get("name"), template["name"])
            tags = self._edited_tags(edits.get("tagSwap"), tags)
            self._validate_join_edits(name, tags)
            origin = self._bounded_join_field(payload.get("origin"), "宇宙临时维修区", 40)
            quirk = self._bounded_join_field(payload.get("quirk"), "会把异常拧成蝴蝶结", 60)
            char_id = self._new_char_id()
            now = int(time.time())
            self.conn.execute(
                """
                INSERT INTO characters
                (id, name, profile, tags_json, origin, quirk, type, status, email, ending,
                 last_seen_story, joined_at)
                VALUES (?, ?, ?, ?, ?, ?, 'human', 'active', ?, NULL, 0, ?)
                """,
                (
                    char_id,
                    name,
                    template["profile"],
                    json.dumps(tags, ensure_ascii=False),
                    origin,
                    quirk,
                    payload.get("email"),
                    now,
                ),
            )
            self.conn.execute("UPDATE templates SET used = 1 WHERE id = ?", (template_id,))
            batch = self._current_or_new_batch(now)
            self.conn.execute(
                """
                INSERT INTO batch_members (batch_id, char_id, type, joined_at)
                VALUES (?, ?, 'human', ?)
                """,
                (batch["id"], char_id, now),
            )
            self._enqueue_print("charcard", {
                "charId": char_id,
                "name": name,
                "profile": template["profile"],
                "tags": tags,
                "origin": origin,
                "quirk": quirk,
            })
            self._log_input("join", payload, "accepted")
            self._maybe_complete_batch(batch["id"], now)
            self.conn.commit()
            return {
                "charId": char_id,
                "batchId": batch["id"],
                "etaSeconds": max(0, batch["deadline_at"] - now),
                "name": name,
                "profile": template["profile"],
                "origin": origin,
                "quirk": quirk,
            }

    def batch(self, batch_id, now=None):
        if now is None:
            now = int(time.time())
        with self.lock:
            batch = self._batch_row(batch_id)
            if batch["status"] == "gathering" and now >= batch["deadline_at"]:
                self._complete_batch(batch["id"], now, allow_ai=True)
                self.conn.commit()
                batch = self._batch_row(batch_id)
            return self._batch_public(batch, now)

    def story(self, after=0):
        after = self._coerce_non_negative_int(after)
        with self.lock:
            stories = [
                self._story_public(row)
                for row in self.conn.execute(
                    "SELECT * FROM stories WHERE id > ? ORDER BY id LIMIT 80",
                    (after,),
                ).fetchall()
            ]
            rules = [
                {"id": row["id"], "text": row["text"]}
                for row in self.conn.execute("SELECT id, text FROM rules ORDER BY id").fetchall()
            ]
            return {
                "world": self._world_public(self._world()),
                "rules": rules,
                "stories": stories,
            }

    def me(self, char_id):
        with self.lock:
            char = self._character(char_id, active_only=False)
            timeline = []
            for row in self.conn.execute(
                "SELECT id, personal_json FROM stories ORDER BY id"
            ).fetchall():
                personal = json.loads(row["personal_json"] or "{}")
                if char_id in personal:
                    timeline.append({"storyId": row["id"], "text": personal[char_id]})
            return {
                "profile": char["profile"],
                "tags": json.loads(char["tags_json"]),
                "origin": char["origin"],
                "quirk": char["quirk"],
                "status": char["status"],
                "timeline": timeline,
                "ending": char["ending"],
            }

    def card(self, char_id):
        with self.lock:
            char = self._character(char_id, active_only=False)
            return {
                "name": char["name"],
                "profile": char["profile"],
                "tags": json.loads(char["tags_json"]),
                "origin": char["origin"],
                "quirk": char["quirk"],
                "ending": char["ending"],
                "qrUrl": "/",
            }

    def leave(self, payload):
        self._require_mapping(payload)
        with self.lock:
            char = self._character(payload.get("charId"), active_only=False)
            if char["status"] == "ended":
                return {"cardUrl": f"/card/{char['id']}"}
            ending = self._make_ending(char, self._world())
            self.conn.execute(
                "UPDATE characters SET status = 'ended', ending = ? WHERE id = ?",
                (ending, char["id"]),
            )
            self._enqueue_print("ending", {"charId": char["id"], "ending": ending})
            self._log_input("leave", payload, "accepted")
            self.conn.commit()
            return {"cardUrl": f"/card/{char['id']}"}

    def print_pending(self, token=None, limit=5):
        self._require_print_token(token)
        limit = max(1, min(self._coerce_non_negative_int(limit) or 5, 20))
        with self.lock:
            rows = self.conn.execute(
                """
                SELECT * FROM print_queue
                WHERE status = 'pending'
                ORDER BY id LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [
                {
                    "ticketId": row["id"],
                    "kind": row["kind"],
                    "payload": json.loads(row["payload_json"]),
                }
                for row in rows
            ]

    def print_ack(self, payload, token=None):
        self._require_print_token(token)
        self._require_mapping(payload)
        ticket_ids = payload.get("ticketIds")
        if not isinstance(ticket_ids, list):
            raise ApiError("REJECTED", "缺少票据编号。")
        acked = 0
        with self.lock:
            for ticket_id in ticket_ids:
                try:
                    parsed = int(ticket_id)
                except (TypeError, ValueError):
                    continue
                cursor = self.conn.execute(
                    "UPDATE print_queue SET status = 'printed' WHERE id = ? AND status = 'pending'",
                    (parsed,),
                )
                acked += cursor.rowcount
            self.conn.commit()
            return {"acked": acked}

    def trigger_finale(self, token=None):
        self._require_print_token(token)
        with self.lock:
            world = self._world()
            if world["phase"] != "finale":
                story_id = self._insert_finale_story()
                self.conn.execute("UPDATE world SET phase = 'finale' WHERE id = 1")
                self._enqueue_print("finale", {"storyId": story_id, "participants": self._human_participant_names()})
                self.conn.commit()
            return self.story(after=0)

    def process_next_print_job(self):
        with self.lock:
            row = self.conn.execute(
                """
                SELECT * FROM print_queue
                WHERE status = 'pending'
                ORDER BY id LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            payload = json.loads(row["payload_json"])
            ticket = {"id": row["id"], "kind": row["kind"], "payload": payload, "qrUrl": "/"}
            try:
                self.printer_driver.print_ticket(ticket)
            except Exception:
                self.conn.commit()
                return {"id": row["id"], "kind": row["kind"], "payload": payload, "status": row["status"]}
            self.conn.execute("UPDATE print_queue SET status = 'printed' WHERE id = ?", (row["id"],))
            self.conn.commit()
            return {"id": row["id"], "kind": row["kind"], "payload": payload, "status": "printed"}

    def record_ending_mail(self, char_id, now=None):
        if now is None:
            now = int(time.time())
        with self.lock:
            char = self._character(char_id, active_only=False)
            if not char["email"]:
                return None
            mail = self._insert_mail(
                char_id=char["id"],
                email=char["email"],
                kind="ending",
                reason=char["ending"] or "你的结局正在维修区边缘显影。",
                now=now,
            )
            self.conn.commit()
            return mail

    def process_next_mail_job(self):
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM mail_queue WHERE status = 'pending' ORDER BY id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            message = {
                "to": row["email"],
                "subject": "宇宙维修区仍在呼唤你",
                "body": row["in_world_reason"],
                "kind": row["kind"],
                "charId": row["char_id"],
            }
            try:
                self.mail_transport.send(message)
            except Exception:
                self.conn.commit()
                return self._mail_public(row)
            self.conn.execute("UPDATE mail_queue SET status = 'sent' WHERE id = ?", (row["id"],))
            self.conn.commit()
            sent = dict(row)
            sent["status"] = "sent"
            return self._mail_public(sent)

    def ingest_mail_reply(self, email, text):
        if not isinstance(email, str) or not email.strip():
            raise ApiError("NOT_FOUND", "世界找不到这封回信的主人。", status=404)
        self._validate_text(text, "回信")
        return {"accepted": True}

    def enqueue_beat(self, now=None):
        return {"queued": False, "reason": "removed"}

    def process_next_weave_job(self):
        return None

    def maybe_beat(self, now=None, idle_seconds=25):
        return None

    def record_mail_for_act(self, involved, importance, in_world_reason, now=None):
        return []

    def _maybe_complete_batch(self, batch_id, now):
        members = self._batch_members(batch_id)
        if len(members) >= BATCH_TARGET_SIZE:
            self._complete_batch(batch_id, now, allow_ai=False)

    def _complete_batch(self, batch_id, now, allow_ai):
        batch = self._batch_row(batch_id)
        if batch["status"] != "gathering":
            return batch["story_id"]
        members = self._batch_members(batch_id)
        if allow_ai:
            while len(members) < BATCH_TARGET_SIZE:
                ai_id = self._insert_ai_resident(now)
                self.conn.execute(
                    """
                    INSERT INTO batch_members (batch_id, char_id, type, joined_at)
                    VALUES (?, ?, 'ai', ?)
                    """,
                    (batch_id, ai_id, now),
                )
                members = self._batch_members(batch_id)
        if len(members) < BATCH_TARGET_SIZE:
            return None
        story_id = self._weave_batch_story(batch_id, members, now)
        self.conn.execute(
            "UPDATE batches SET status = 'done', story_id = ? WHERE id = ?",
            (story_id, batch_id),
        )
        return story_id

    def _weave_batch_story(self, batch_id, members, now):
        incident = self._incident_for_next_story()
        rules = [
            {"id": row["id"], "text": row["text"]}
            for row in self.conn.execute("SELECT id, text FROM rules ORDER BY id").fetchall()
        ]
        context = {
            "world": self._world_public(self._world()),
            "storyBackground": self._admin_settings()["story_background"],
            "incident": incident["incident"],
            "endingTemplate": incident["ending"],
            "rules": rules,
            "members": [self._member_context(row) for row in members],
            "tone": "宇宙临时维修区,荒诞、轻快、每个 quirk 至少发挥一次作用。",
        }
        result = self._coerce_story_result(self._weave_with_llm(context), context)
        story_id = self._insert_story(
            kind="repair",
            incident=incident["incident"],
            segments=result["segments"],
            members=[self._member_public(row) for row in members],
            personal=result["personal"],
            created_at=now,
        )
        rule_text = result["rule"]
        rule_id = self._insert_rule(story_id, rule_text, now)
        self.conn.execute("UPDATE stories SET rule_id = ? WHERE id = ?", (rule_id, story_id))
        self.conn.execute("UPDATE world SET repair_count = repair_count + 1 WHERE id = 1")
        self.conn.execute(
            "UPDATE characters SET last_seen_story = ? WHERE id IN (%s)" % ",".join("?" for _ in members),
            (story_id, *[row["id"] for row in members]),
        )
        self._enqueue_print("report", {
            "storyId": story_id,
            "incident": incident["incident"],
            "rule": rule_text,
            "members": [row["name"] for row in members],
        })
        world = self._world()
        if world["repair_count"] >= world["finale_target"]:
            self._insert_finale_story()
            self.conn.execute("UPDATE world SET phase = 'finale' WHERE id = 1")
        return story_id

    def _weave_with_llm(self, context):
        for _ in range(2):
            try:
                result = self.llm.weave(context)
            except (TypeError, KeyError, ValueError, RuntimeError):
                result = None
            if result is None:
                continue
            try:
                return self._validate_story_result(result)
            except (TypeError, KeyError, ValueError):
                continue
        return None

    def _validate_story_result(self, result):
        if not isinstance(result, dict):
            raise TypeError("story result must be object")
        segments = result["segments"]
        if not isinstance(segments, list) or not segments:
            raise ValueError("segments must be non-empty")
        cleaned_segments = []
        for segment in segments:
            if not isinstance(segment, dict):
                raise TypeError("segment must be object")
            focus = segment.get("focusCharIds") or []
            if not isinstance(focus, list):
                raise TypeError("focusCharIds must be list")
            cleaned_segments.append({
                "text": self._bounded_text(segment["text"], 260),
                "focusCharIds": [str(item) for item in focus],
            })
        personal = result.get("personal") or {}
        if not isinstance(personal, dict):
            raise TypeError("personal must be object")
        return {
            "segments": cleaned_segments,
            "rule": self._bounded_text(result["rule"], 30),
            "personal": {str(key): self._limit(str(value), 160) for key, value in personal.items()},
        }

    def _coerce_story_result(self, result, context):
        if result is not None:
            return result
        names = "、".join(member["name"] for member in context["members"])
        segments = []
        personal = {}
        for member in context["members"]:
            text = (
                f"{member['name']}来自{member['origin']},用“{member['quirk']}”修了"
                f"{context['incident']}的一角。"
            )
            segments.append({"text": self._limit(text, 260), "focusCharIds": [member["charId"]]})
            personal[member["charId"]] = self._limit(f"你在维修区被点名: {member['quirk']}派上了用场。", 160)
        ending = context["endingTemplate"].format(names=names, rule="临时规则已生效")
        segments.append({"text": self._limit(ending, 260), "focusCharIds": [member["charId"] for member in context["members"]]})
        return {
            "segments": segments,
            "rule": self._limit(f"自本次修复起,{names[:8]}负责给异常贴标签", 30),
            "personal": personal,
        }

    def _insert_story(self, kind, incident, segments, members, personal, created_at):
        self.conn.execute(
            """
            INSERT INTO stories (kind, incident, segments_json, members_json, personal_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                kind,
                incident,
                json.dumps(segments, ensure_ascii=False),
                json.dumps(members, ensure_ascii=False),
                json.dumps(personal, ensure_ascii=False),
                created_at,
            ),
        )
        return self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    def _insert_rule(self, story_id, text, created_at):
        self.conn.execute(
            "INSERT INTO rules (story_id, text, created_at) VALUES (?, ?, ?)",
            (story_id, text, created_at),
        )
        return self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    def _insert_finale_story(self):
        existing = self.conn.execute("SELECT id FROM stories WHERE kind = 'finale' ORDER BY id LIMIT 1").fetchone()
        if existing is not None:
            return existing["id"]
        rules = [row["text"] for row in self.conn.execute("SELECT text FROM rules ORDER BY id").fetchall()]
        participants = self._human_participant_names()
        text = (
            "终幕维修报告: 所有到场者共同造成了宇宙故障,也用共同创作完成了修复。"
            f"参与者: {'、'.join(participants) or '临时居民'}。规则账本: {';'.join(rules[:5]) or '暂无'}。"
        )
        story_id = self._insert_story(
            kind="finale",
            incident="终幕元叙事维修",
            segments=[{"text": self._limit(text, 260), "focusCharIds": []}],
            members=[],
            personal={},
            created_at=int(time.time()),
        )
        return story_id

    def _story_public(self, row):
        rule = None
        if row["rule_id"]:
            rule_row = self.conn.execute("SELECT text FROM rules WHERE id = ?", (row["rule_id"],)).fetchone()
            rule = None if rule_row is None else rule_row["text"]
        return {
            "id": row["id"],
            "kind": row["kind"],
            "incident": row["incident"],
            "segments": json.loads(row["segments_json"]),
            "members": json.loads(row["members_json"]),
            "rule": rule,
            "personal": json.loads(row["personal_json"] or "{}"),
        }

    def _batch_public(self, row, now):
        result = {
            "status": row["status"],
            "countdown": max(0, row["deadline_at"] - now),
            "members": [self._member_public(member) for member in self._batch_members(row["id"])],
        }
        if row["story_id"]:
            result["storyId"] = row["story_id"]
        return result

    def _current_or_new_batch(self, now):
        row = self.conn.execute(
            """
            SELECT * FROM batches
            WHERE status = 'gathering'
            ORDER BY created_at LIMIT 1
            """
        ).fetchone()
        if row is not None and len(self._batch_members(row["id"])) < BATCH_MAX_SIZE:
            return row
        batch_id = f"b_{uuid.uuid4().hex[:8]}"
        self.conn.execute(
            """
            INSERT INTO batches (id, status, created_at, deadline_at)
            VALUES (?, 'gathering', ?, ?)
            """,
            (batch_id, now, now + BATCH_WINDOW_SECONDS),
        )
        return self._batch_row(batch_id)

    def _batch_row(self, batch_id):
        row = self.conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
        if row is None:
            raise ApiError("NOT_FOUND", "世界找不到这个集结批次。", status=404)
        return row

    def _batch_members(self, batch_id):
        return self.conn.execute(
            """
            SELECT c.*, bm.type AS member_type
            FROM batch_members bm
            JOIN characters c ON c.id = bm.char_id
            WHERE bm.batch_id = ?
            ORDER BY bm.joined_at, c.id
            """,
            (batch_id,),
        ).fetchall()

    def _member_context(self, row):
        return {
            "charId": row["id"],
            "name": row["name"],
            "origin": row["origin"],
            "quirk": row["quirk"],
            "type": row["member_type"],
            "profile": row["profile"],
            "tags": json.loads(row["tags_json"]),
        }

    def _member_public(self, row):
        return {
            "charId": row["id"],
            "name": row["name"],
            "origin": row["origin"],
            "type": row["member_type"],
        }

    def _insert_ai_resident(self, now):
        residents = [
            ("螺丝巡夜人", "来自反向钟表铺", "能听懂松动螺丝的梦话"),
            ("雾面档案员", "来自云端唐楼", "会把故障折成纸鹤"),
            ("恐龙票务员", "来自雨天候车厅", "坚持给恐龙优先购票权"),
        ]
        name, origin, quirk = residents[self.conn.execute("SELECT COUNT(*) FROM characters WHERE type = 'ai'").fetchone()[0] % len(residents)]
        char_id = self._new_char_id()
        profile = f"{name}: 宇宙临时维修区的 AI 居民。"
        self.conn.execute(
            """
            INSERT INTO characters
            (id, name, profile, tags_json, origin, quirk, type, status, email, ending,
             last_seen_story, joined_at)
            VALUES (?, ?, ?, ?, ?, ?, 'ai', 'active', NULL, NULL, 0, ?)
            """,
            (char_id, name, profile, json.dumps(["稳", "怪", "修"], ensure_ascii=False), origin, quirk, now),
        )
        return char_id

    def _incident_for_next_story(self):
        count = self.conn.execute("SELECT COUNT(*) FROM stories WHERE kind = 'repair'").fetchone()[0]
        return self.incidents[count % len(self.incidents)]

    def _world(self):
        return self.conn.execute("SELECT * FROM world WHERE id = 1").fetchone()

    def _world_public(self, world):
        return {
            "phase": world["phase"],
            "repairCount": world["repair_count"],
            "finaleTarget": world["finale_target"],
        }

    def _admin_settings(self):
        row = self.conn.execute("SELECT * FROM admin_settings WHERE id = 1").fetchone()
        if row is not None:
            return row
        self.conn.execute(
            """
            INSERT INTO admin_settings
            (id, story_background, beat_interval_seconds, generation_paused)
            VALUES (1, '', 30, 0)
            """
        )
        return self.conn.execute("SELECT * FROM admin_settings WHERE id = 1").fetchone()

    def _admin_public(self, row):
        return {
            "storyBackground": row["story_background"],
            "beatIntervalSeconds": row["beat_interval_seconds"],
            "generationPaused": bool(row["generation_paused"]),
        }

    def _character(self, char_id, active_only):
        if not char_id:
            raise ApiError("NOT_FOUND", "世界找不到这个角色。", status=404)
        row = self.conn.execute("SELECT * FROM characters WHERE id = ?", (char_id,)).fetchone()
        if row is None:
            raise ApiError("NOT_FOUND", "世界找不到这个角色。", status=404)
        if active_only and row["status"] != "active":
            raise ApiError("REJECTED", "这个角色已经离开故事。")
        return row

    def _new_char_id(self):
        while True:
            char_id = f"c_{uuid.uuid4().hex[:8]}"
            exists = self.conn.execute("SELECT 1 FROM characters WHERE id = ?", (char_id,)).fetchone()
            if exists is None:
                return char_id

    def _template_public(self, row):
        tags = json.loads(row["tags_json"])
        return {
            "templateId": row["id"],
            "name": row["name"],
            "profile": row["profile"],
            "tags": tags,
            "tagOptions": self._tag_options(tags),
        }

    def _tag_options(self, tags):
        options = list(dict.fromkeys(tags + ["莽", "馋", "轴", "怪", "稳", "怂", "甜", "欠"]))
        return options[:8]

    def _edited_name(self, value, fallback):
        if value is None or value == "":
            return fallback
        if not isinstance(value, str):
            raise ApiError("REJECTED", JOIN_REJECTION_MESSAGE)
        return value.strip()

    def _edited_tags(self, value, tags):
        if value is None or value == "":
            return tags
        if not isinstance(value, str):
            raise ApiError("REJECTED", JOIN_REJECTION_MESSAGE)
        option = value.strip()
        if option not in self._tag_options(tags):
            raise ApiError("REJECTED", JOIN_REJECTION_MESSAGE)
        return [option] + [tag for tag in tags if tag != option][:2]

    def _validate_join_edits(self, name, tags):
        values = [name, *tags]
        for value in values:
            if not isinstance(value, str) or not value.strip():
                raise ApiError("REJECTED", JOIN_REJECTION_MESSAGE)
            if len(value.strip()) > 8:
                raise ApiError("REJECTED", JOIN_REJECTION_MESSAGE)
            compact = re.sub(r"\s+", "", value)
            if any(word in compact for word in SENSITIVE_WORDS):
                raise ApiError("REJECTED", JOIN_REJECTION_MESSAGE)

    def _bounded_join_field(self, value, fallback, max_len):
        if value is None or value == "":
            return fallback
        if not isinstance(value, str):
            raise ApiError("REJECTED", JOIN_REJECTION_MESSAGE)
        return self._limit(value.strip() or fallback, max_len)

    def _bounded_optional_text(self, value, max_len):
        if value is None:
            return ""
        if not isinstance(value, str):
            raise ApiError("REJECTED", "故事背景必须是文字。")
        return self._limit(value.strip(), max_len)

    def _coerce_admin_interval(self, value):
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            raise ApiError("REJECTED", "事件生成时间必须是数字。")
        if parsed < 1 or parsed > 3600:
            raise ApiError("REJECTED", "事件生成时间必须在 1 到 3600 秒之间。")
        return parsed

    def _validate_text(self, value, label):
        if not isinstance(value, str) or not value.strip():
            raise ApiError("REJECTED", f"{label}必须留下文字。")
        if len(value) > 500:
            raise ApiError("REJECTED", f"{label}太长,世界暂时承载不了。")

    def _bounded_text(self, value, max_len):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("text must be non-empty")
        return self._limit(value.strip(), max_len)

    def _coerce_non_negative_int(self, value):
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = 0
        return max(parsed, 0)

    def _make_ending(self, char, world):
        try:
            result = self.llm.generate_ending(char, world)
            if isinstance(result, dict):
                return self._bounded_text(result["ending"], 180)
            if isinstance(result, str):
                return self._bounded_text(result, 180)
        except (TypeError, KeyError, ValueError, RuntimeError):
            pass
        return f"{char['name']}离开了维修区,但名字仍在报告背面发光。"

    def _enqueue_print(self, kind, payload):
        self.conn.execute(
            "INSERT INTO print_queue (kind, payload_json, status) VALUES (?, ?, 'pending')",
            (kind, json.dumps(payload, ensure_ascii=False)),
        )
        self._prune_print_queue()

    def _prune_print_queue(self):
        pending_count = self.conn.execute(
            "SELECT COUNT(*) FROM print_queue WHERE status = 'pending'"
        ).fetchone()[0]
        if pending_count <= 20:
            return
        overflow = pending_count - 20
        rows = self.conn.execute(
            """
            SELECT id FROM print_queue
            WHERE status = 'pending' AND kind = 'report'
            ORDER BY id LIMIT ?
            """,
            (overflow,),
        ).fetchall()
        for row in rows:
            self.conn.execute("UPDATE print_queue SET status = 'dropped' WHERE id = ?", (row["id"],))

    def _require_print_token(self, token):
        if self.print_token and token != self.print_token:
            raise ApiError("REJECTED", "打印令牌不正确。", status=403)

    def _insert_mail(self, char_id, email, kind, reason, now):
        in_world_reason = f"{reason}\n\n直接回复这封邮件,你的话将进入维修区。"
        status = "pending" if self._mail_allowed(email) else "simulated"
        self.conn.execute(
            """
            INSERT INTO mail_queue
            (char_id, email, kind, in_world_reason, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (char_id, email, kind, in_world_reason, status, now),
        )
        mail_id = self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        return {
            "id": mail_id,
            "charId": char_id,
            "email": email,
            "kind": kind,
            "status": status,
            "inWorldReason": in_world_reason,
        }

    def _mail_public(self, row):
        return {
            "id": row["id"],
            "charId": row["char_id"],
            "email": row["email"],
            "kind": row["kind"],
            "status": row["status"],
            "inWorldReason": row["in_world_reason"],
        }

    def _mail_allowed(self, email):
        whitelist = {
            item.strip().lower()
            for item in get_env("MAIL_WHITELIST", "").split(",")
            if item.strip()
        }
        return bool(whitelist) and email.lower() in whitelist

    def _log_input(self, type_, payload, verdict):
        self.conn.execute(
            "INSERT INTO inputs_log (type, payload_json, verdict, created_at) VALUES (?, ?, ?, ?)",
            (type_, json.dumps(payload, ensure_ascii=False), verdict, int(time.time())),
        )

    def _require_mapping(self, payload):
        if not isinstance(payload, dict):
            raise ApiError("REJECTED", "世界只回应完整的叙述。")

    def _seed_templates_if_needed(self):
        count = self.conn.execute("SELECT COUNT(*) FROM templates WHERE used = 0").fetchone()[0]
        if count >= 20:
            return
        self._seed_templates(force=False)
        self.conn.commit()

    def _seed_templates(self, force):
        base_count = self.conn.execute("SELECT COUNT(*) FROM templates").fetchone()[0]
        if base_count >= 50 and not force:
            return
        names = [
            "煎饼侠王", "铜锅侠", "半夜鼓手", "门缝诗人", "跑偏侦探",
            "旧钟修理员", "葱花祭司", "雨棚船长", "咸鱼博士", "路灯裁缝",
        ]
        profiles = [
            "总说自己很普通,但随身带着三把会吵架的钥匙。",
            "能把尴尬场面搅成线索,代价是每次都先脸红。",
            "相信所有谜题都能用锅铲解决,目前正确率高得离谱。",
            "走到哪里都能捡到不属于今天的收据。",
            "对异常毫无敬畏,所以经常第一个摸到真相边缘。",
        ]
        tags = [
            ["莽", "馋", "轴"],
            ["怪", "稳", "欠"],
            ["甜", "怂", "灵"],
            ["冷", "快", "贫"],
            ["勇", "慢", "卷"],
        ]
        target = base_count + 50 if force else 50
        for index in range(base_count, target):
            name = names[index % len(names)]
            profile = f"{name}: {profiles[index % len(profiles)]}"
            self.conn.execute(
                """
                INSERT OR IGNORE INTO templates (id, name, profile, tags_json, used)
                VALUES (?, ?, ?, ?, 0)
                """,
                (
                    f"t_{index + 1:03d}",
                    name,
                    profile,
                    json.dumps(tags[index % len(tags)], ensure_ascii=False),
                ),
            )

    def _load_incidents(self, incidents_path):
        path = Path(incidents_path) if incidents_path else Path(__file__).with_name("prompts") / "incidents.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = []
        incidents = data.get("incidents") if isinstance(data, dict) else data
        if isinstance(incidents, list) and incidents:
            cleaned = []
            for item in incidents:
                if not isinstance(item, dict):
                    continue
                try:
                    cleaned.append({
                        "incident": self._limit(str(item["incident"]).strip(), 180),
                        "ending": self._limit(str(item["ending"]).strip(), 220),
                    })
                except KeyError:
                    continue
            if cleaned:
                return cleaned
        return [
            {
                "incident": "第三维修舱的月亮突然开始漏电,每滴月光都会唱错一拍。",
                "ending": "{names}把漏电月光装回舱壁。维修区在报告末尾批注:异常源,疑似与到场者有关。",
            },
            {
                "incident": "恐龙候车厅的购票机只吐出昨天的号码牌。",
                "ending": "{names}把号码牌排成新时刻表。维修区在报告末尾批注:异常源,疑似与到场者有关。",
            },
            {
                "incident": "赛博大唐的云梯卡在半空,不断打印不存在的请假条。",
                "ending": "{names}替云梯盖上临时印章。维修区在报告末尾批注:异常源,疑似与到场者有关。",
            },
        ]

    def _human_participant_names(self):
        rows = self.conn.execute(
            "SELECT name FROM characters WHERE type = 'human' ORDER BY joined_at, id"
        ).fetchall()
        return [row["name"] for row in rows]

    def _limit(self, text, max_len):
        return text if len(text) <= max_len else text[: max_len - 1] + "…"


class AsyncGameService:
    def __init__(self, game, weave_runtime=None):
        self.game = game
        self.weave_runtime = weave_runtime

    def template(self):
        return self.game.template()

    def admin(self):
        return self.game.admin()

    def update_admin(self, payload):
        return self.game.update_admin(payload)

    def reset_story(self, payload):
        return self.game.reset_story(payload)

    def join(self, payload):
        return self.game.join(payload)

    def batch(self, batch_id, now=None):
        return self.game.batch(batch_id, now=now)

    def leave(self, payload):
        return self.game.leave(payload)

    def story(self, after=0):
        return self.game.story(after=after)

    def me(self, char_id):
        return self.game.me(char_id)

    def card(self, char_id):
        return self.game.card(char_id)

    def print_pending(self, token=None, limit=5):
        return self.game.print_pending(token=token, limit=limit)

    def print_ack(self, payload, token=None):
        return self.game.print_ack(payload, token=token)

    def trigger_finale(self, token=None):
        return self.game.trigger_finale(token=token)

    def enqueue_beat(self, now=None):
        return self.game.enqueue_beat(now=now)

    def process_next_weave_job(self):
        return self.game.process_next_weave_job()

    def maybe_beat(self, now=None, idle_seconds=25):
        return self.game.maybe_beat(now=now, idle_seconds=idle_seconds)

    def process_next_print_job(self):
        return self.game.process_next_print_job()

    def process_next_mail_job(self):
        return self.game.process_next_mail_job()

    def ingest_mail_reply(self, email, text):
        return self.game.ingest_mail_reply(email, text)
