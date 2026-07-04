import json
import re
import threading
import time
import uuid
from pathlib import Path

from .db import connect, initialize
from .config import get_env
from .llm import HttpJsonLlmGateway
from .mail import SmtpMailTransport
from .printer import CommandPrinterDriver


TWIST_AT = 8
REVEAL_AT = 20
QUEUE_BACKLOG_LIMIT = 3
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
    ):
        self.db_path = db_path
        self.legends = self._load_legends(legends_path)
        self.conn = connect(db_path)
        initialize(self.conn, self.legends[0]["legend"])
        self.llm = llm if llm is not None else HttpJsonLlmGateway()
        self.mail_transport = mail_transport if mail_transport is not None else SmtpMailTransport()
        self.printer_driver = printer_driver if printer_driver is not None else CommandPrinterDriver()
        self.lock = threading.RLock()
        self.occupied_characters = set()
        self.last_beat_at = 0
        with self.lock:
            self._seed_templates_if_needed()

    def close(self):
        self.conn.close()

    def admin(self):
        with self.lock:
            settings = self._admin_settings()
            stats = {
                "characters": self.conn.execute("SELECT COUNT(*) FROM characters").fetchone()[0],
                "acts": self.conn.execute("SELECT COUNT(*) FROM acts").fetchone()[0],
                "pendingWeaveJobs": self.conn.execute(
                    "SELECT COUNT(*) FROM weave_queue WHERE status = 'pending'"
                ).fetchone()[0],
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
                """
                UPDATE world
                SET legend_index = 0, legend_text = ?, clue_count = 0, act_seq = 0
                WHERE id = 1
                """,
                (self.legends[0]["legend"],),
            )
            for table in [
                "characters",
                "acts",
                "triples",
                "weave_queue",
                "print_queue",
                "mail_queue",
                "inputs_log",
            ]:
                self.conn.execute(f"DELETE FROM {table}")
            self.conn.execute("UPDATE templates SET used = 0")
            self.occupied_characters.clear()
            self.last_beat_at = 0
            self.conn.commit()
            result = self.admin()
            result["reset"] = True
            return result

    def template(self):
        with self.lock:
            self._seed_templates_if_needed()
            row = self.conn.execute(
                """
                SELECT * FROM templates
                WHERE used = 0
                ORDER BY id LIMIT 1
                """
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
        email = payload.get("email")

        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM templates WHERE id = ? AND used = 0",
                (template_id,),
            ).fetchone()
            if row is None:
                raise ApiError("NOT_FOUND", "这张卡已经滑进别人的故事。", status=404)
            tags = json.loads(row["tags_json"])
            name = self._edited_name(edits.get("name"), row["name"])
            tags = self._edited_tags(edits.get("tagSwap"), tags)
            self._validate_join_edits(name, tags)
            char_id = self._new_char_id()
            profile = row["profile"]
            now = int(time.time())
            self.conn.execute(
                """
                INSERT INTO characters
                (id, name, profile, tags_json, type, status, email, ending, last_seen_act, joined_at)
                VALUES (?, ?, ?, ?, 'human', 'active', ?, NULL, 0, ?)
                """,
                (char_id, name, profile, json.dumps(tags, ensure_ascii=False), email, now),
            )
            self.conn.execute("UPDATE templates SET used = 1 WHERE id = ?", (template_id,))
            first_act_id = self._create_story_act(
                type_="act",
                involved=self._involved_for_join(char_id),
                focus_char_id=char_id,
                action_text="刚刚抽卡进入世界",
                importance=4,
            )
            self._enqueue_print("charcard", {
                "charId": char_id,
                "name": name,
                "profile": profile,
                "tags": tags,
            })
            self._log_input("join", payload, "accepted")
            self.conn.commit()
            return {
                "charId": char_id,
                "name": name,
                "profile": profile,
                "tags": tags,
                "firstActId": first_act_id,
            }

    def enqueue_beat(self, now=None):
        if now is None:
            now = int(time.time())
        with self.lock:
            if self._admin_settings()["generation_paused"]:
                return {"queued": False, "reason": "paused"}
            pending = self.conn.execute(
                "SELECT COUNT(*) FROM weave_queue WHERE status = 'pending'"
            ).fetchone()[0]
            if pending > QUEUE_BACKLOG_LIMIT:
                return {"queued": False, "reason": "backlog"}
            characters = self._pick_cold_characters(limit=3)
            if not characters:
                return {"queued": False, "reason": "empty"}
            queue_id = self._enqueue_weave_job(
                "beat",
                {"charIds": [row["id"] for row in characters], "text": "世界节拍撞见了他们"},
                priority=100,
                now=now,
            )
            self.conn.commit()
            return {"queued": True, "jobId": queue_id}

    def process_next_weave_job(self):
        with self.lock:
            row = self.conn.execute(
                """
                SELECT * FROM weave_queue
                WHERE status = 'pending'
                ORDER BY priority ASC, id ASC LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            payload = json.loads(row["payload_json"])
            char_ids = self._available_character_ids(payload.get("charIds", []))
            if not char_ids:
                self.conn.execute("UPDATE weave_queue SET status = 'completed', completed_at = ? WHERE id = ?", (int(time.time()), row["id"]))
                self.conn.commit()
                return {"id": row["id"], "status": "skipped"}
            for char_id in char_ids:
                self.occupied_characters.add(char_id)
            try:
                self.conn.execute(
                    "UPDATE weave_queue SET status = 'running', started_at = ? WHERE id = ?",
                    (int(time.time()), row["id"]),
                )
                act_id = self._create_story_act(
                    type_=row["kind"],
                    involved=char_ids,
                    focus_char_id=char_ids[0],
                    action_text=payload.get("text") or "世界节拍撞见了他们",
                    importance=2 if row["kind"] == "beat" else 3,
                )
                self.conn.execute(
                    "UPDATE weave_queue SET status = 'completed', completed_at = ? WHERE id = ?",
                    (int(time.time()), row["id"]),
                )
                self.conn.commit()
                return {"id": row["id"], "status": "completed", "actId": act_id}
            finally:
                for char_id in char_ids:
                    self.occupied_characters.discard(char_id)

    def story(self, after=0):
        after = self._coerce_non_negative_int(after)
        with self.lock:
            world = self._world()
            acts = [
                self._act_public(row)
                for row in self.conn.execute(
                    "SELECT * FROM acts WHERE id > ? ORDER BY id LIMIT 80",
                    (after,),
                ).fetchall()
            ]
            characters = [
                self._character_public(row)
                for row in self.conn.execute(
                    "SELECT * FROM characters ORDER BY joined_at, id"
                ).fetchall()
            ]
            return {
                "world": self._world_public(world),
                "acts": acts,
                "characters": characters,
            }

    def me(self, char_id):
        with self.lock:
            char = self._character(char_id, active_only=False)
            timeline = []
            for row in self.conn.execute(
                "SELECT id, seq, personal_json FROM acts WHERE personal_json IS NOT NULL ORDER BY id"
            ).fetchall():
                personal = json.loads(row["personal_json"] or "{}")
                if char_id in personal:
                    timeline.append({
                        "actId": row["id"],
                        "seq": row["seq"],
                        "text": personal[char_id],
                    })
            return {
                "profile": char["profile"],
                "tags": json.loads(char["tags_json"]),
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
            self._insert_act(
                seq=self._next_seq(),
                type_="act",
                narrative=ending,
                chronicle=f"{char['name']}把退场结局别在传说背面。",
                involved=[char["id"]],
                personal={char["id"]: ending},
                importance=4,
                print_data={"worthy": True, "ticket": "ending"},
                advances_clue=False,
            )
            self._enqueue_print("ending", {"charId": char["id"], "ending": ending})
            self._log_input("leave", payload, "accepted")
            self.conn.commit()
            return {"cardUrl": f"/card/{char['id']}"}

    def maybe_beat(self, now=None, idle_seconds=25):
        if now is None:
            now = int(time.time())
        settings = self.admin()
        if settings["generationPaused"]:
            return None
        idle_seconds = settings["beatIntervalSeconds"] if idle_seconds is None else idle_seconds
        with self.lock:
            if self.last_beat_at and now - self.last_beat_at < idle_seconds:
                return None
            self.last_beat_at = now
        queued = self.enqueue_beat(now=now)
        if not queued.get("queued"):
            return None
        return self.process_next_weave_job()

    def record_mail_for_act(self, involved, importance, in_world_reason, now=None):
        if importance < 4:
            return []
        if now is None:
            now = int(time.time())
        created = []
        with self.lock:
            for char_id in involved:
                char = self.conn.execute(
                    """
                    SELECT * FROM characters
                    WHERE id = ? AND status = 'ended' AND email IS NOT NULL AND email != ''
                    """,
                    (char_id,),
                ).fetchone()
                if char is None or not self._mail_rate_limit_allows(char["id"], now):
                    continue
                created.append(self._insert_mail(
                    char_id=char["id"],
                    email=char["email"],
                    kind="act",
                    reason=in_world_reason,
                    now=now,
                ))
            self.conn.commit()
        return created

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
                reason=char["ending"] or "你的结局正在世界边缘显影。",
                now=now,
            )
            self.conn.commit()
            return mail

    def process_next_mail_job(self):
        with self.lock:
            row = self.conn.execute(
                """
                SELECT * FROM mail_queue
                WHERE status = 'pending'
                ORDER BY id LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            message = {
                "to": row["email"],
                "subject": "开放世界仍在呼唤你",
                "body": row["in_world_reason"],
                "kind": row["kind"],
                "charId": row["char_id"],
            }
            try:
                self.mail_transport.send(message)
            except Exception:
                self.conn.commit()
                return self._mail_public(row)
            self.conn.execute(
                "UPDATE mail_queue SET status = 'sent' WHERE id = ?",
                (row["id"],),
            )
            self.conn.commit()
            sent = dict(row)
            sent["status"] = "sent"
            return self._mail_public(sent)

    def ingest_mail_reply(self, email, text):
        if not isinstance(email, str) or not email.strip():
            raise ApiError("NOT_FOUND", "世界找不到这封回信的主人。", status=404)
        self._validate_text(text, "回信")
        with self.lock:
            char = self.conn.execute(
                """
                SELECT * FROM characters
                WHERE lower(email) = lower(?) AND status = 'active'
                ORDER BY joined_at DESC, id DESC LIMIT 1
                """,
                (email.strip(),),
            ).fetchone()
            if char is None:
                raise ApiError("NOT_FOUND", "世界找不到这封回信的主人。", status=404)
            act_id = self._create_story_act(
                type_="act",
                involved=self._involved_with_old_characters(char["id"]),
                focus_char_id=char["id"],
                action_text=text,
                importance=3,
            )
            self.conn.commit()
            return {"accepted": True, "actId": act_id}

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
            ticket = {
                "id": row["id"],
                "kind": row["kind"],
                "payload": payload,
                "qrUrl": "/",
            }
            try:
                self.printer_driver.print_ticket(ticket)
            except Exception:
                self.conn.commit()
                return {
                    "id": row["id"],
                    "kind": row["kind"],
                    "payload": payload,
                    "status": row["status"],
                }
            self.conn.execute(
                "UPDATE print_queue SET status = 'printed' WHERE id = ?",
                (row["id"],),
            )
            self.conn.commit()
            return {
                "id": row["id"],
                "kind": row["kind"],
                "payload": payload,
                "status": "printed",
            }

    def _create_story_act(self, type_, involved, focus_char_id, action_text, importance):
        characters = self._characters_by_id(involved)
        world = self._world()
        woven = self._coerce_weaver_result(
            self._weave_with_llm(world, characters, action_text, type_),
            world,
            characters,
            action_text,
            type_,
        )
        act_id = self._insert_act(
            seq=self._next_seq(),
            type_=type_,
            narrative=woven["narrative"],
            chronicle=woven["chronicle"],
            involved=woven["involved"],
            personal=woven["personal"],
            importance=woven["importance"] if importance is None else importance,
            print_data=woven.get("print"),
            advances_clue=True,
        )
        self._apply_state_changes(woven.get("stateChanges", {}))
        self._write_new_triples(woven.get("newTriples", []), act_id)
        self._update_last_seen(woven["involved"])
        self._advance_clues()
        return act_id

    def _insert_act(self, seq, type_, narrative, chronicle, involved, personal, importance, print_data, advances_clue):
        if not involved:
            involved = ["world"]
        self.conn.execute(
            """
            INSERT INTO acts
            (seq, type, narrative, chronicle, involved_json, personal_json,
             importance, print_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                seq,
                type_,
                narrative,
                chronicle,
                json.dumps(involved, ensure_ascii=False),
                json.dumps(personal or {}, ensure_ascii=False),
                importance,
                json.dumps(print_data, ensure_ascii=False) if print_data is not None else None,
                int(time.time()),
            ),
        )
        act_id = self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        if not advances_clue:
            return act_id
        return act_id

    def _advance_clues(self):
        world = self._world()
        clue_count = world["clue_count"] + 1
        self.conn.execute("UPDATE world SET clue_count = ? WHERE id = 1", (clue_count,))
        if clue_count == TWIST_AT:
            self._insert_twist_act()
        if clue_count == REVEAL_AT:
            self._insert_reveal_act()

    def _insert_twist_act(self):
        world = self._world()
        legend = self.legends[world["legend_index"] % len(self.legends)]
        names = self._recent_character_names()
        text = legend["twist"].format(names=names or "众人")
        seq = self._next_seq()
        self.conn.execute("UPDATE world SET legend_text = ? WHERE id = 1", (text,))
        self._insert_act(
            seq=seq,
            type_="twist",
            narrative=f"转折公告: {text}",
            chronicle=f"第{seq}幕,传说拧出了新的岔路。",
            involved=self._active_character_ids() or ["world"],
            personal={},
            importance=5,
            print_data={"worthy": True, "ticket": "twist"},
            advances_clue=False,
        )
        self._enqueue_print("twist", {"legend": text})

    def _insert_reveal_act(self):
        world = self._world()
        legend = self.legends[world["legend_index"] % len(self.legends)]
        names = self._recent_character_names()
        text = legend["reveal"].format(names=names or "众人")
        seq = self._next_seq()
        self._insert_act(
            seq=seq,
            type_="reveal",
            narrative=f"真相公告: {text}",
            chronicle=f"第{seq}幕,传说揭开了锅盖。",
            involved=self._active_character_ids() or ["world"],
            personal={},
            importance=5,
            print_data={"worthy": True, "ticket": "reveal"},
            advances_clue=False,
        )
        self._enqueue_print("reveal", {"legend": text})
        next_index = (world["legend_index"] + 1) % len(self.legends)
        self.conn.execute(
            """
            UPDATE world SET legend_index = ?, legend_text = ?, clue_count = 0
            WHERE id = 1
            """,
            (next_index, self.legends[next_index]["legend"]),
        )

    def _next_seq(self):
        world = self._world()
        next_seq = world["act_seq"] + 1
        self.conn.execute("UPDATE world SET act_seq = ? WHERE id = 1", (next_seq,))
        return next_seq

    def _weave_with_llm(self, world, characters, action_text, type_):
        context = self._weaver_context(world, characters, action_text, type_)
        for _ in range(2):
            try:
                result = self.llm.weave(context)
            except (TypeError, KeyError, ValueError, RuntimeError):
                result = None
            if result is None:
                continue
            try:
                return self._validate_weaver_result(result, characters)
            except (TypeError, KeyError, ValueError):
                continue
        return None

    def _weaver_context(self, world, characters, action_text, type_):
        recent_rows = self.conn.execute(
            "SELECT narrative, chronicle FROM acts ORDER BY id DESC LIMIT 5"
        ).fetchall()
        settings = self._admin_settings()
        return {
            "tone": "无厘头、无压力,但必须围绕当前传说推进发现/争论/搅局。",
            "storyBackground": settings["story_background"],
            "world": self._world_public(world),
            "input": {"text": action_text, "kind": type_},
            "characters": [
                {
                    "id": row["id"],
                    "name": row["name"],
                    "profile": row["profile"],
                    "tags": json.loads(row["tags_json"]),
                }
                for row in characters
            ],
            "recentActs": [row["narrative"] for row in reversed(recent_rows)],
        }

    def _coerce_weaver_result(self, weaver_result, world, characters, action_text, type_):
        if weaver_result is not None:
            return weaver_result
        if getattr(self.llm, "configured", False):
            return self._fallback_weaver_result(world, characters)
        return self._default_weaver_result(world, characters, action_text, type_)

    def _validate_weaver_result(self, result, characters):
        if not isinstance(result, dict):
            raise TypeError("weaver result must be object")
        character_ids = {row["id"] for row in characters}
        narrative = self._bounded_text(result["narrative"], 240)
        chronicle = self._bounded_text(result["chronicle"], 60)
        involved = result["involved"]
        if not isinstance(involved, list) or not involved:
            raise ValueError("involved must be non-empty")
        involved = [str(item) for item in involved]
        for char_id in character_ids:
            if char_id not in involved:
                involved.append(char_id)
        personal = result.get("personal") or {}
        if not isinstance(personal, dict):
            raise TypeError("personal must be object")
        importance = int(result["importance"])
        if importance < 1 or importance > 5:
            raise ValueError("importance out of range")
        state_changes = result.get("stateChanges") or {}
        if not isinstance(state_changes, dict):
            raise TypeError("stateChanges must be object")
        new_triples = result.get("newTriples") or []
        if not isinstance(new_triples, list):
            raise TypeError("newTriples must be list")
        return {
            "narrative": narrative,
            "chronicle": chronicle,
            "involved": involved,
            "personal": personal,
            "importance": importance,
            "print": result.get("print"),
            "stateChanges": state_changes,
            "newTriples": new_triples,
        }

    def _default_weaver_result(self, world, characters, action_text, type_):
        names = "、".join(row["name"] for row in characters) or "某个路人"
        first_id = characters[0]["id"] if characters else "world"
        legend = world["legend_text"]
        verb = "撞见" if type_ == "beat" else "入场"
        return {
            "narrative": self._limit(
                f"{names}{verb}了传说的边角: {legend}。他们围着“{action_text}”争了三句,最后发现线索藏在一只会打嗝的铜锅里。",
                240,
            ),
            "chronicle": self._limit(f"第{world['act_seq'] + 1}幕,{names}把传说搅出新线索。", 60),
            "involved": [row["id"] for row in characters] or ["world"],
            "personal": {
                first_id: self._limit(f"你撞上了 @{names} 的命运,还摸到传说的一枚热乎线索。", 160)
            },
            "importance": 3,
            "print": {"worthy": False, "ticket": None},
            "stateChanges": {},
            "newTriples": [],
        }

    def _fallback_weaver_result(self, world, characters):
        ids = [row["id"] for row in characters] or ["world"]
        return {
            "narrative": "世界轻轻震颤了一下,传说从桌底滚出半枚线索。",
            "chronicle": "世界短暂震颤。",
            "involved": ids,
            "personal": {ids[0]: "你感到世界轻轻震颤了一下。"},
            "importance": 1,
            "print": {"worthy": False, "ticket": None},
            "stateChanges": {},
            "newTriples": [],
        }

    def _apply_state_changes(self, state_changes):
        characters = state_changes.get("characters", {})
        if not isinstance(characters, dict):
            return
        for char_id, changes in characters.items():
            if not isinstance(changes, dict):
                continue
            status = changes.get("status")
            if status == "ended":
                ending = self._limit(str(changes.get("ending") or "这个角色被世界写到了边缘。"), 180)
                self.conn.execute(
                    "UPDATE characters SET status = 'ended', ending = ? WHERE id = ?",
                    (ending, char_id),
                )

    def _write_new_triples(self, triples, act_id):
        for triple in triples:
            if not isinstance(triple, (list, tuple)) or len(triple) != 3:
                continue
            subject, relation, object_ = [str(part) for part in triple]
            self.conn.execute(
                "INSERT INTO triples (subject, relation, object, act_id) VALUES (?, ?, ?, ?)",
                (subject, relation, object_, act_id),
            )

    def _involved_for_join(self, char_id):
        return self._involved_with_old_characters(char_id)

    def _involved_with_old_characters(self, char_id):
        rows = self.conn.execute(
            """
            SELECT id FROM characters
            WHERE id != ? AND status = 'active'
            ORDER BY last_seen_act ASC, joined_at ASC LIMIT 2
            """,
            (char_id,),
        ).fetchall()
        return [char_id] + [row["id"] for row in rows]

    def _pick_cold_characters(self, limit):
        return self.conn.execute(
            """
            SELECT * FROM characters
            WHERE status = 'active'
            ORDER BY last_seen_act ASC, CASE type WHEN 'human' THEN 0 ELSE 1 END, joined_at ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    def _available_character_ids(self, char_ids):
        cleaned = []
        for char_id in char_ids:
            if not isinstance(char_id, str) or char_id in self.occupied_characters:
                continue
            if self.conn.execute(
                "SELECT 1 FROM characters WHERE id = ? AND status = 'active'",
                (char_id,),
            ).fetchone() is None:
                continue
            cleaned.append(char_id)
        if cleaned:
            return cleaned
        fallback = self._pick_cold_characters(limit=3)
        return [row["id"] for row in fallback if row["id"] not in self.occupied_characters]

    def _characters_by_id(self, char_ids):
        rows = []
        for char_id in char_ids:
            row = self.conn.execute("SELECT * FROM characters WHERE id = ?", (char_id,)).fetchone()
            if row is not None:
                rows.append(row)
        return rows

    def _update_last_seen(self, involved):
        seq = self._world()["act_seq"]
        for char_id in involved:
            self.conn.execute(
                "UPDATE characters SET last_seen_act = ? WHERE id = ?",
                (seq, char_id),
            )

    def _recent_character_names(self):
        rows = self.conn.execute(
            "SELECT name FROM characters ORDER BY last_seen_act DESC, joined_at DESC LIMIT 3"
        ).fetchall()
        return "、".join(row["name"] for row in rows)

    def _enqueue_weave_job(self, kind, payload, priority, now):
        self.conn.execute(
            """
            INSERT INTO weave_queue (kind, payload_json, status, priority, created_at)
            VALUES (?, ?, 'pending', ?, ?)
            """,
            (kind, json.dumps(payload, ensure_ascii=False), priority, now),
        )
        return self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    def _world(self):
        return self.conn.execute("SELECT * FROM world WHERE id = 1").fetchone()

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

    def _world_public(self, world):
        return {
            "legend": world["legend_text"],
            "clueCount": world["clue_count"],
            "nextTwistAt": TWIST_AT,
            "nextRevealAt": REVEAL_AT,
            "actSeq": world["act_seq"],
        }

    def _act_public(self, row):
        return {
            "id": row["id"],
            "seq": row["seq"],
            "type": row["type"],
            "narrative": row["narrative"],
            "chronicle": row["chronicle"],
            "involved": json.loads(row["involved_json"]),
            "importance": row["importance"],
        }

    def _character_public(self, row):
        return {
            "charId": row["id"],
            "name": row["name"],
            "type": row["type"],
            "status": row["status"],
            "tags": json.loads(row["tags_json"]),
            "lastSeenAct": row["last_seen_act"],
        }

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
        return f"{char['name']}离开了这个世界,但名字仍在传说背面发光。"

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
        if pending_count <= 8:
            return
        overflow = pending_count - 8
        rows = self.conn.execute(
            """
            SELECT id FROM print_queue
            WHERE status = 'pending' AND kind NOT IN ('charcard', 'ending', 'reveal')
            ORDER BY id LIMIT ?
            """,
            (overflow,),
        ).fetchall()
        for row in rows:
            self.conn.execute("UPDATE print_queue SET status = 'dropped' WHERE id = ?", (row["id"],))

    def _mail_rate_limit_allows(self, char_id, now):
        latest = self.conn.execute(
            """
            SELECT created_at FROM mail_queue
            WHERE char_id = ? AND kind = 'act'
            ORDER BY created_at DESC LIMIT 1
            """,
            (char_id,),
        ).fetchone()
        return latest is None or now - latest["created_at"] >= 3600

    def _insert_mail(self, char_id, email, kind, reason, now):
        in_world_reason = f"{reason}\n\n直接回复这封邮件,你的话将进入世界"
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
            "对传说毫无敬畏,所以经常第一个摸到真相边缘。",
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

    def _load_legends(self, legends_path):
        path = Path(legends_path) if legends_path else Path(__file__).with_name("prompts") / "legends.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = []
        legends = data.get("legends") if isinstance(data, dict) else data
        if isinstance(legends, list) and legends:
            cleaned = []
            for item in legends:
                if not isinstance(item, dict):
                    continue
                try:
                    cleaned.append({
                        "legend": self._limit(str(item["legend"]).strip(), 160),
                        "twist": self._limit(str(item["twist"]).strip(), 180),
                        "reveal": self._limit(str(item["reveal"]).strip(), 180),
                    })
                except KeyError:
                    continue
            if cleaned:
                return cleaned
        return [
            {
                "legend": "昨夜,镇上所有的钟停在了同一秒。",
                "twist": "{names}发现停钟不是坏掉,而是在等一口会报时的锅醒来。",
                "reveal": "{names}揭开真相: 所有钟都在替一只迟到的猫保密。",
            },
            {
                "legend": "广场喷泉每天吐出一张写着明天的煎饼。",
                "twist": "{names}发现煎饼背面有同一个咬痕,像某种地图。",
                "reveal": "{names}终于看懂: 喷泉只是想找回它丢失的早餐。",
            },
            {
                "legend": "每到黄昏,影子都会排队去邮局寄信。",
                "twist": "{names}截住一封影子信,里面只写着别相信脚后跟。",
                "reveal": "{names}确认真相: 邮局局长其实是一盏怕黑的路灯。",
            },
        ]

    def _active_character_ids(self):
        return [
            row["id"]
            for row in self.conn.execute(
                "SELECT id FROM characters WHERE status = 'active' ORDER BY joined_at, id"
            ).fetchall()
        ]

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

    def leave(self, payload):
        return self.game.leave(payload)

    def story(self, after=0):
        return self.game.story(after=after)

    def me(self, char_id):
        return self.game.me(char_id)

    def card(self, char_id):
        return self.game.card(char_id)

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
