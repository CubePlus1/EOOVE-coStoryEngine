import json
import os
import re
import sqlite3
import threading
import time
import uuid

from .constants import (
    DEFAULT_DIRECTIVES,
    FORBIDDEN_NODE_WORDS,
    HEARTBEAT_HINTS,
    HOPE_INITIAL,
    HOPE_THRESHOLD,
    LOCATION_BY_ID,
    LOCATIONS,
    MAX_ROUND,
    NODE_EVENTS,
)
from .db import connect, initialize
from .llm import HttpJsonLlmGateway
from .mail import SmtpMailTransport
from .printer import CommandPrinterDriver


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
    ):
        self.db_path = db_path
        self.conn = connect(db_path)
        initialize(self.conn)
        self.llm = llm if llm is not None else HttpJsonLlmGateway()
        self.mail_transport = mail_transport if mail_transport is not None else SmtpMailTransport()
        self.printer_driver = printer_driver if printer_driver is not None else CommandPrinterDriver()
        self.lock = threading.RLock()
        self.heartbeat_index = 0
        self.last_accepted_at = 0

    def close(self):
        self.conn.close()

    def join(self, payload):
        self._require_mapping(payload)
        self._ensure_not_settling()
        self._validate_text(payload.get("selfDesc"), "自述")

        with self.lock:
            world = self._world()
            char_id = self._new_char_id()
            location = self._assign_location()
            name, profile = self._make_character(payload["selfDesc"])
            email = payload.get("email")

            self.conn.execute(
                """
                INSERT INTO characters
                (id, name, profile, type, status, location, email, ending, echo, cycle_joined)
                VALUES (?, ?, ?, 'human', 'active', ?, ?, NULL, NULL, ?)
                """,
                (char_id, name, profile, location, email, world["cycle"]),
            )
            self._enqueue_print("charcard", {
                "charId": char_id,
                "name": name,
                "profile": profile,
                "location": location,
            })
            self._log_input("join", payload, "accepted")
            self._mark_accepted()
            self.conn.commit()

        return {
            "charId": char_id,
            "name": name,
            "profile": profile,
            "location": location,
            "cycle": world["cycle"],
        }

    def act(self, payload):
        self._require_mapping(payload)
        kind = payload.get("kind")
        if kind == "heartbeat":
            return self.heartbeat(payload.get("hint"))
        if kind not in {"action", "narration", "oracle"}:
            raise ApiError("REJECTED", "世界听不懂这种行动。")

        text = payload.get("text")
        try:
            self._validate_text(text, "行动")
            with self.lock:
                self._ensure_not_settling()
                char = self._character(payload.get("charId"), active_only=True)
                rejection = self._moderate_input(payload)
                if rejection:
                    self._log_input(kind, payload, "rejected")
                    self.conn.commit()
                    return {"accepted": False, "reason": rejection}
                oracle_status = None
                oracle_applied = None
                promoted_oracle_id = None
                scope = payload.get("scope")
                if kind == "oracle":
                    rejection = self._moderate_oracle(text)
                    if rejection:
                        self._log_input("oracle", payload, "rejected")
                        self.conn.commit()
                        return {"accepted": False, "reason": rejection}
                    oracle_status = self._register_oracle(char, text, scope)
                    oracle_applied = text if oracle_status == "applied" else None

                world = self._increment_round()
                node_data = NODE_EVENTS.get(world["round"])
                if node_data is not None:
                    self._create_node_acts(world, node_data)

                if kind != "oracle" and oracle_applied is None:
                    promoted = self._promote_pooled_oracle(world, char["location"])
                    if promoted is not None:
                        promoted_oracle_id, oracle_applied = promoted

                self._create_action_act(
                    world,
                    char,
                    text,
                    kind,
                    oracle_applied,
                    payload.get("weaverResult"),
                )
                if promoted_oracle_id is not None:
                    self.conn.execute(
                        "UPDATE oracle_pool SET status = 'applied' WHERE id = ?",
                        (promoted_oracle_id,),
                    )
                self._expire_old_oracles(world)
                self._log_input(kind, payload, "accepted")
                self._mark_accepted()

                if world["round"] >= MAX_ROUND:
                    self._settle_world(world)

                self.conn.commit()

            response = {"accepted": True, "round": world["round"]}
            if oracle_status is not None:
                response["oracleStatus"] = oracle_status
            return response
        except ApiError:
            raise

    def heartbeat(self, hint=None):
        with self.lock:
            active = self.conn.execute(
                "SELECT * FROM characters WHERE status = 'active' ORDER BY id LIMIT 1"
            ).fetchone()
            if active is None:
                return {"accepted": False, "reason": "世界等待第一个名字。"}
            if hint is None:
                hint = HEARTBEAT_HINTS[self.heartbeat_index % len(HEARTBEAT_HINTS)]
                self.heartbeat_index += 1
            result = self.act({
                "charId": active["id"],
                "text": hint,
                "kind": "narration",
            })
            act_id = self.conn.execute("SELECT MAX(id) FROM acts").fetchone()[0]
            self.conn.execute(
                "UPDATE acts SET type = 'heartbeat', importance = 2 WHERE id = ?",
                (act_id,),
            )
            self._mark_accepted()
            self.conn.commit()
            return result

    def leave(self, payload):
        self._require_mapping(payload)
        with self.lock:
            self._ensure_not_settling()
            char = self._character(payload.get("charId"), active_only=False)
            if char["status"] == "ended":
                return {"cardUrl": f"/card/{char['id']}"}

            world = self._world()
            ending = self._make_ending(char, world)
            self.conn.execute(
                "UPDATE characters SET status = 'ended', ending = ? WHERE id = ?",
                (ending, char["id"]),
            )
            self._insert_act(
                cycle=world["cycle"],
                round_no=world["round"],
                location=char["location"],
                type_="act",
                narrative=ending,
                chronicle=f"{char['name']}写下退场结局。",
                directive=DEFAULT_DIRECTIVES[char["location"]],
                involved=[char["id"]],
                personal={char["id"]: ending},
                importance=4,
                hope_delta=0,
            )
            self._enqueue_print("ending", {"charId": char["id"], "ending": ending})
            self._log_input("leave", payload, "accepted")
            self._mark_accepted()
            self.conn.commit()
            return {"cardUrl": f"/card/{char['id']}"}

    def maybe_heartbeat(self, now=None, idle_seconds=10):
        if now is None:
            now = int(time.time())
        with self.lock:
            if self.last_accepted_at == 0:
                self.last_accepted_at = now
                return None
            if now - self.last_accepted_at < idle_seconds:
                return None
        return self.heartbeat()

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
                "subject": "轮回世界仍在呼唤你",
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
                ORDER BY cycle_joined DESC, id DESC LIMIT 1
                """,
                (email.strip(),),
            ).fetchone()
            if char is None:
                char = self.conn.execute(
                    """
                    SELECT * FROM characters
                    WHERE lower(email) = lower(?)
                    ORDER BY cycle_joined DESC, id DESC LIMIT 1
                    """,
                    (email.strip(),),
                ).fetchone()
            if char is None:
                raise ApiError("NOT_FOUND", "世界找不到这封回信的主人。", status=404)
        return self.act({
            "charId": char["id"],
            "text": text,
            "kind": "narration",
        })

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

    def process_async_location_job(self, payload):
        self._require_mapping(payload)
        async_meta = payload.get("_async")
        if not isinstance(async_meta, dict):
            return self.act(payload)
        kind = payload.get("kind")
        text = payload.get("text")
        self._validate_text(text, "行动")
        with self.lock:
            char = self._character(payload.get("charId"), active_only=True)
            world = {
                "cycle": async_meta["cycle"],
                "round": async_meta["round"],
            }
            oracle_applied = async_meta.get("oracleApplied")
            promoted_oracle_id = None
            if kind != "oracle" and oracle_applied is None:
                promoted = self._promote_pooled_oracle(world, char["location"])
                if promoted is not None:
                    promoted_oracle_id, oracle_applied = promoted
            self._create_action_act(
                world,
                char,
                text,
                kind,
                oracle_applied,
                payload.get("weaverResult"),
            )
            if promoted_oracle_id is not None:
                self.conn.execute(
                    "UPDATE oracle_pool SET status = 'applied' WHERE id = ?",
                    (promoted_oracle_id,),
                )
            if async_meta["round"] >= MAX_ROUND:
                self._settle_world(world)
            self.conn.commit()
        response = {"accepted": True, "round": async_meta["round"]}
        if kind == "oracle":
            response["oracleStatus"] = "applied" if oracle_applied else "pooled"
        return response

    def story(self, after=0):
        after = self._coerce_non_negative_int(after)
        with self.lock:
            world = self._world()
            acts = [
                self._act_public(row)
                for row in self.conn.execute(
                    "SELECT * FROM acts WHERE id > ? ORDER BY id LIMIT 50",
                    (after,),
                ).fetchall()
            ]
            characters = [
                {
                    "charId": row["id"],
                    "name": row["name"],
                    "type": row["type"],
                    "status": row["status"],
                    "location": row["location"],
                }
                for row in self.conn.execute(
                    "SELECT * FROM characters ORDER BY cycle_joined, id"
                ).fetchall()
            ]
            directives = self._current_directives()

        return {
            "world": {
                "cycle": world["cycle"],
                "round": world["round"],
                "maxRound": MAX_ROUND,
                "hopeHint": self._hope_hint(world["hope"]),
                "status": world["status"],
            },
            "locations": [
                {
                    "id": location["id"],
                    "name": location["name"],
                    "directive": directives.get(location["id"], location["directive"]),
                }
                for location in LOCATIONS
            ],
            "acts": acts,
            "characters": characters,
        }

    def me(self, char_id):
        with self.lock:
            char = self._character(char_id, active_only=False)
            timeline = []
            for row in self.conn.execute(
                "SELECT id, round, personal_json FROM acts WHERE personal_json IS NOT NULL ORDER BY id"
            ).fetchall():
                personal = json.loads(row["personal_json"])
                if char_id in personal:
                    timeline.append({
                        "actId": row["id"],
                        "round": row["round"],
                        "text": personal[char_id],
                    })
            return {
                "profile": char["profile"],
                "status": char["status"],
                "location": char["location"],
                "timeline": timeline,
                "ending": char["ending"],
                "echo": char["echo"],
            }

    def card(self, char_id):
        with self.lock:
            char = self._character(char_id, active_only=False)
            return {
                "name": char["name"],
                "profile": char["profile"],
                "ending": char["ending"],
                "cycle": char["cycle_joined"],
                "qrUrl": "/",
            }

    def _world(self):
        return self.conn.execute("SELECT * FROM world WHERE id = 1").fetchone()

    def _ensure_not_settling(self):
        if self._world()["status"] == "settling":
            raise ApiError("SETTLING", "世界正在被审判", status=409)

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

    def _assign_location(self):
        count = self.conn.execute("SELECT COUNT(*) FROM characters").fetchone()[0]
        return LOCATIONS[count % len(LOCATIONS)]["id"]

    def _make_character(self, self_desc):
        try:
            result = self.llm.generate_character(self_desc)
            if isinstance(result, dict):
                name = self._bounded_text(result["name"], 24)
                profile = self._bounded_text(result["profile"], 240)
                return name, profile
        except (TypeError, KeyError, ValueError, RuntimeError):
            pass
        name = self._make_name(self_desc)
        return name, f"{name}: {self_desc}。世界在轮回中为你留下一个位置。"

    def _make_name(self, self_desc):
        cleaned = re.sub(r"\s+", "", self_desc).strip("。,，.!！?？")
        if not cleaned:
            cleaned = "无名者"
        seed = cleaned[:4]
        suffix = ["守夜人", "拾荒者", "观测者"][len(cleaned) % 3]
        return f"{seed}{suffix}"

    def _increment_round(self):
        world = self._world()
        next_round = world["round"] + 1
        self.conn.execute("UPDATE world SET round = ? WHERE id = 1", (next_round,))
        return self._world()

    def _create_action_act(self, world, char, text, kind, oracle_applied, weaver_result=None):
        seed = self._consume_location_seed(char["location"])
        woven = self._coerce_weaver_result(
            weaver_result=weaver_result,
            world=world,
            char=char,
            text=text,
            kind=kind,
            seed=seed,
        )
        directive = woven["directive"]
        narrative = woven["narrative"]
        personal = woven["personal"]
        hope_delta = woven["hopeDelta"]
        importance = woven["importance"]
        if importance >= 4:
            self._enqueue_print("bulletin", {"round": world["round"], "text": narrative})
        self._insert_act(
            cycle=world["cycle"],
            round_no=world["round"],
            location=char["location"],
            type_="act",
            narrative=narrative,
            chronicle=woven["chronicle"],
            directive=directive,
            involved=woven["involved"],
            personal=personal,
            importance=importance,
            hope_delta=hope_delta,
            oracle_applied=oracle_applied,
        )
        self.conn.execute(
            "UPDATE world SET hope = MAX(0, MIN(100, hope + ?)) WHERE id = 1",
            (hope_delta,),
        )
        self.conn.execute(
            "INSERT INTO triples (subject, relation, object, cycle, round) VALUES (?, ?, ?, ?, ?)",
            (char["id"], "acted", self._limit(text, 40), world["cycle"], world["round"]),
        )
        self._apply_state_changes(woven["stateChanges"])
        self._write_new_triples(woven["newTriples"], world)
        self._enqueue_cross_location(woven["crossLocation"])

    def _coerce_weaver_result(self, weaver_result, world, char, text, kind, seed):
        if weaver_result is None:
            weaver_result = self._weave_with_llm(world, char, text, kind, seed)
        if weaver_result is None:
            if getattr(self.llm, "configured", False):
                return self._fallback_weaver_result(char)
            weaver_result = self._default_weaver_result(world, char, text, kind, seed)
        try:
            return self._validate_weaver_result(weaver_result, char)
        except (TypeError, KeyError, ValueError):
            return self._fallback_weaver_result(char)

    def _weave_with_llm(self, world, char, text, kind, seed):
        context = self._weaver_context(world, char, text, kind, seed)
        for _ in range(2):
            try:
                result = self.llm.weave(context)
            except (TypeError, KeyError, ValueError, RuntimeError):
                result = None
            if result is None:
                continue
            try:
                self._validate_weaver_result(result, char)
            except (TypeError, KeyError, ValueError):
                continue
            return result
        return None

    def _weaver_context(self, world, char, text, kind, seed):
        recent_rows = self.conn.execute(
            """
            SELECT narrative, chronicle FROM acts
            WHERE location = ?
            ORDER BY id DESC LIMIT 3
            """,
            (char["location"],),
        ).fetchall()
        chronicle_rows = self.conn.execute(
            "SELECT chronicle FROM acts WHERE chronicle IS NOT NULL ORDER BY id"
        ).fetchall()
        triple_rows = self.conn.execute(
            """
            SELECT subject, relation, object FROM triples
            WHERE subject = ? OR object = ?
            ORDER BY id DESC LIMIT 10
            """,
            (char["id"], char["id"]),
        ).fetchall()
        return {
            "world": {
                "cycle": world["cycle"],
                "round": world["round"],
                "hopeHint": self._hope_hint(self._world()["hope"]),
            },
            "location": char["location"],
            "directive": self._current_directives().get(
                char["location"],
                DEFAULT_DIRECTIVES[char["location"]],
            ),
            "input": {"text": text, "kind": kind},
            "character": {
                "id": char["id"],
                "name": char["name"],
                "profile": char["profile"],
                "location": char["location"],
                "echo": char["echo"],
            },
            "chronicle": [row["chronicle"] for row in chronicle_rows],
            "recentActs": [row["narrative"] for row in reversed(recent_rows)],
            "triples": [list(row) for row in triple_rows],
            "seed": None if seed is None else seed["seed"],
        }

    def _default_weaver_result(self, world, char, text, kind, seed):
        prefix = "神谕降下" if kind == "oracle" else "行动被写入命运"
        seed_text = f" 远处传来线索:{seed['seed']}。" if seed is not None else ""
        return {
            "narrative": self._limit(f"{prefix}: {char['name']}说:{text}{seed_text}", 150),
            "directive": DEFAULT_DIRECTIVES[char["location"]],
            "chronicle": self._limit(
                f"第{world['round']}轮,{char['name']}改变了{LOCATION_BY_ID[char['location']]['name']}。",
                30,
            ),
            "involved": [char["id"]],
            "personal": {char["id"]: f"你让命运记住了: {text}"},
            "importance": 3 if kind != "oracle" else 4,
            "hopeDelta": self._hope_delta(text, oracle=kind == "oracle"),
            "stateChanges": {},
            "newTriples": [],
            "crossLocation": [],
            "print": {"worthy": False, "ticket": None},
        }

    def _validate_weaver_result(self, result, char):
        if not isinstance(result, dict):
            raise TypeError("weaver result must be object")
        narrative = self._bounded_text(result["narrative"], 150)
        directive = result["directive"]
        if not isinstance(directive, dict):
            raise TypeError("directive must be object")
        directive = {
            "situation": self._bounded_text(directive["situation"], 40),
            "hint": self._bounded_text(directive["hint"], 30),
        }
        chronicle = self._bounded_text(result["chronicle"], 30)
        involved = result["involved"]
        if not isinstance(involved, list) or not involved:
            raise ValueError("involved must be non-empty")
        involved = [str(item) for item in involved]
        if char["id"] not in involved:
            involved.append(char["id"])
        personal = result.get("personal") or {}
        if not isinstance(personal, dict):
            raise TypeError("personal must be object")
        importance = int(result["importance"])
        if importance < 1 or importance > 5:
            raise ValueError("importance out of range")
        hope_delta = int(result["hopeDelta"])
        if hope_delta < -5 or hope_delta > 5:
            raise ValueError("hopeDelta out of range")
        state_changes = result.get("stateChanges") or {}
        if not isinstance(state_changes, dict):
            raise TypeError("stateChanges must be object")
        new_triples = result.get("newTriples") or []
        if not isinstance(new_triples, list):
            raise TypeError("newTriples must be list")
        cross_location = result.get("crossLocation") or []
        if not isinstance(cross_location, list):
            raise TypeError("crossLocation must be list")
        return {
            "narrative": narrative,
            "directive": directive,
            "chronicle": chronicle,
            "involved": involved,
            "personal": personal,
            "importance": importance,
            "hopeDelta": hope_delta,
            "stateChanges": state_changes,
            "newTriples": new_triples,
            "crossLocation": cross_location,
        }

    def _fallback_weaver_result(self, char):
        return {
            "narrative": "世界轻轻震颤了一下",
            "directive": DEFAULT_DIRECTIVES[char["location"]],
            "chronicle": "世界短暂震颤。",
            "involved": [char["id"]],
            "personal": {char["id"]: "你感到世界轻轻震颤了一下。"},
            "importance": 1,
            "hopeDelta": 0,
            "stateChanges": {},
            "newTriples": [],
            "crossLocation": [],
        }

    def _bounded_text(self, value, max_len):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("text must be non-empty")
        return self._limit(value.strip(), max_len)

    def _apply_state_changes(self, state_changes):
        characters = state_changes.get("characters", {})
        if not isinstance(characters, dict):
            return
        for char_id, changes in characters.items():
            if not isinstance(changes, dict):
                continue
            location = changes.get("location")
            if location in LOCATION_BY_ID:
                self.conn.execute(
                    "UPDATE characters SET location = ? WHERE id = ?",
                    (location, char_id),
                )

    def _write_new_triples(self, triples, world):
        for triple in triples:
            if not isinstance(triple, (list, tuple)) or len(triple) != 3:
                continue
            subject, relation, object_ = [str(part) for part in triple]
            self.conn.execute(
                "INSERT INTO triples (subject, relation, object, cycle, round) VALUES (?, ?, ?, ?, ?)",
                (subject, relation, object_, world["cycle"], world["round"]),
            )

    def _enqueue_cross_location(self, cross_location):
        for item in cross_location:
            if not isinstance(item, dict):
                continue
            location = item.get("to")
            seed = item.get("seed")
            if location not in LOCATION_BY_ID or not isinstance(seed, str) or not seed.strip():
                continue
            self.conn.execute(
                """
                INSERT INTO location_seed_queue (location, seed, status, created_at)
                VALUES (?, ?, 'pending', ?)
                """,
                (location, self._limit(seed.strip(), 120), int(time.time())),
            )

    def _consume_location_seed(self, location):
        row = self.conn.execute(
            """
            SELECT * FROM location_seed_queue
            WHERE location = ? AND status = 'pending'
            ORDER BY id LIMIT 1
            """,
            (location,),
        ).fetchone()
        if row is None:
            return None
        self.conn.execute(
            "UPDATE location_seed_queue SET status = 'consumed', consumed_at = ? WHERE id = ?",
            (int(time.time()), row["id"]),
        )
        return row

    def _create_node_acts(self, world, node_data):
        for location in LOCATIONS:
            self._insert_act(
                cycle=world["cycle"],
                round_no=world["round"],
                location=location["id"],
                type_="node",
                narrative=node_data["narrative"],
                chronicle=node_data["chronicle"],
                directive=node_data["directive"],
                involved=self._active_ids_for_location(location["id"]),
                personal={},
                importance=5,
                hope_delta=0,
            )
        self._enqueue_print("apocalypse", {"round": world["round"], "event": node_data["chronicle"]})

    def _settle_world(self, world):
        self.conn.execute("UPDATE world SET status = 'settling' WHERE id = 1")
        current = self._world()
        saved = current["hope"] >= HOPE_THRESHOLD
        narrative = "hope汇成火种,世界在毁灭边缘被救回。" if saved else "hope未能抵达阈值,世界在灰烬中合拢。"
        self._insert_act(
            cycle=world["cycle"],
            round_no=world["round"],
            location="global",
            type_="settlement",
            narrative=narrative,
            chronicle="本轮回完成审判。",
            directive={"situation": "新轮回即将开启", "hint": "带着残响再次进入世界"},
            involved=self._active_character_ids(),
            personal={},
            importance=5,
            hope_delta=0,
        )
        active = self.conn.execute("SELECT * FROM characters WHERE status = 'active' ORDER BY id").fetchall()
        echo_fragment = self._make_echo(world)
        for index, char in enumerate(active):
            ending = f"{char['name']}在第{world['cycle']}轮回终章中{'幸存' if saved else '成为残响'}。"
            echo = echo_fragment if index % 3 == 0 else char["echo"]
            self.conn.execute(
                "UPDATE characters SET status = 'ended', ending = ?, echo = ? WHERE id = ?",
                (ending, echo, char["id"]),
            )
        self.conn.execute(
            "UPDATE world SET cycle = cycle + 1, round = 0, hope = ?, status = 'running' WHERE id = 1",
            (HOPE_INITIAL,),
        )
        new_world = self._world()
        for location in LOCATIONS:
            self._insert_act(
                cycle=new_world["cycle"],
                round_no=0,
                location=location["id"],
                type_="act",
                narrative=f"新轮回开启:第{new_world['cycle']}轮回,{location['name']}重新听见命运的脚步。",
                chronicle=f"第{new_world['cycle']}轮回,{location['name']}复苏。",
                directive=DEFAULT_DIRECTIVES[location["id"]],
                involved=["world"],
                personal={},
                importance=2,
                hope_delta=0,
            )

    def _insert_act(
        self,
        cycle,
        round_no,
        location,
        type_,
        narrative,
        chronicle,
        directive,
        involved,
        personal,
        importance,
        hope_delta,
        oracle_applied=None,
    ):
        if not involved:
            involved = self._active_character_ids() or ["world"]
        self.conn.execute(
            """
            INSERT INTO acts
            (cycle, round, location, type, narrative, chronicle, directive_json,
             involved_json, personal_json, importance, hope_delta, oracle_applied, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cycle,
                round_no,
                location,
                type_,
                narrative,
                chronicle,
                json.dumps(directive, ensure_ascii=False),
                json.dumps(involved, ensure_ascii=False),
                json.dumps(personal or {}, ensure_ascii=False),
                importance,
                hope_delta,
                oracle_applied,
                int(time.time()),
            ),
        )

    def _register_oracle(self, char, text, scope):
        if not scope:
            raise ApiError("REJECTED", "神谕必须说明降向何处。")
        if scope != "global" and scope not in LOCATION_BY_ID:
            raise ApiError("REJECTED", "神谕找不到落点。")

        world = self._world()
        exists = self.conn.execute(
            """
            SELECT 1 FROM oracle_pool
            WHERE round_submitted = ? AND scope = ? AND status = 'applied'
            """,
            (world["round"] + 1, scope),
        ).fetchone()
        status = "pooled" if exists else "applied"
        self.conn.execute(
            """
            INSERT INTO oracle_pool (char_id, scope, text, round_submitted, status)
            VALUES (?, ?, ?, ?, ?)
            """,
            (char["id"], scope, text, world["round"] + 1, status),
        )
        return status

    def _expire_old_oracles(self, world):
        self.conn.execute(
            """
            UPDATE oracle_pool SET status = 'expired'
            WHERE status = 'pooled' AND round_submitted <= ?
            """,
            (world["round"] - 5,),
        )

    def _promote_pooled_oracle(self, world, location):
        scopes = ("global", location)
        placeholders = ",".join("?" for _ in scopes)
        row = self.conn.execute(
            f"""
            SELECT id, text FROM oracle_pool
            WHERE status = 'pooled' AND scope IN ({placeholders}) AND round_submitted < ?
            ORDER BY id LIMIT 1
            """,
            (*scopes, world["round"]),
        ).fetchone()
        if row is None:
            return None
        return row["id"], row["text"]

    def _moderate_oracle(self, text):
        normalized = re.sub(r"\s+", "", text)
        for word in FORBIDDEN_NODE_WORDS:
            if word in normalized:
                return "节点事件已刻入轮回,你的意志无法取消或改期。"
        return None

    def _moderate_input(self, payload):
        try:
            result = self.llm.moderate(payload)
        except (TypeError, KeyError, ValueError, RuntimeError):
            return None
        if not isinstance(result, dict):
            return None
        if result.get("pass") is False:
            reason = result.get("reason")
            if isinstance(reason, str) and reason.strip():
                return self._limit(reason.strip(), 120)
            return "世界拒绝了这段低语。"
        return None

    def _make_ending(self, char, world):
        try:
            result = self.llm.generate_ending(char, world)
            if isinstance(result, dict):
                return self._bounded_text(result["ending"], 180)
            if isinstance(result, str):
                return self._bounded_text(result, 180)
        except (TypeError, KeyError, ValueError, RuntimeError):
            pass
        return f"{char['name']}离开了这个世界,但名字仍在第{world['cycle']}轮回里发光。"

    def _make_echo(self, world):
        chronicle = [
            row["chronicle"]
            for row in self.conn.execute(
                """
                SELECT chronicle FROM acts
                WHERE cycle = ? AND chronicle IS NOT NULL
                ORDER BY id
                """,
                (world["cycle"],),
            ).fetchall()
        ]
        try:
            result = self.llm.generate_echo(chronicle, world)
            if isinstance(result, dict):
                return self._bounded_text(result["echo"], 100)
            if isinstance(result, str):
                return self._bounded_text(result, 100)
        except (TypeError, KeyError, ValueError, RuntimeError):
            pass
        return "你总觉得这段毁灭,曾经在梦里发生过。"

    def _current_directives(self):
        directives = dict(DEFAULT_DIRECTIVES)
        rows = self.conn.execute(
            """
            SELECT location, directive_json FROM acts
            WHERE directive_json IS NOT NULL
            ORDER BY id
            """
        ).fetchall()
        for row in rows:
            if row["location"] in directives:
                directives[row["location"]] = json.loads(row["directive_json"])
        return directives

    def _active_ids_for_location(self, location):
        ids = [
            row["id"]
            for row in self.conn.execute(
                "SELECT id FROM characters WHERE status = 'active' AND location = ? ORDER BY id",
                (location,),
            ).fetchall()
        ]
        return ids or self._active_character_ids() or ["world"]

    def _active_character_ids(self):
        return [
            row["id"]
            for row in self.conn.execute(
                "SELECT id FROM characters WHERE status = 'active' ORDER BY id"
            ).fetchall()
        ]

    def _act_public(self, row):
        return {
            "id": row["id"],
            "cycle": row["cycle"],
            "round": row["round"],
            "location": row["location"],
            "type": row["type"],
            "narrative": row["narrative"],
            "chronicle": row["chronicle"],
            "involved": json.loads(row["involved_json"]),
            "importance": row["importance"],
        }

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
        if pending_count <= 5:
            return
        overflow = pending_count - 5
        rows = self.conn.execute(
            """
            SELECT id FROM print_queue
            WHERE status = 'pending' AND kind = 'bulletin'
            ORDER BY id LIMIT ?
            """,
            (overflow,),
        ).fetchall()
        for row in rows:
            self.conn.execute(
                "UPDATE print_queue SET status = 'dropped' WHERE id = ?",
                (row["id"],),
            )

    def _mark_accepted(self):
        self.last_accepted_at = int(time.time())

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
            for item in os.environ.get("MAIL_WHITELIST", "").split(",")
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

    def _validate_text(self, value, label):
        if not isinstance(value, str) or not value.strip():
            raise ApiError("REJECTED", f"{label}必须留下文字。")
        if len(value) > 500:
            raise ApiError("REJECTED", f"{label}太长,世界暂时承载不了。")

    def _coerce_non_negative_int(self, value):
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = 0
        return max(parsed, 0)

    def _hope_hint(self, hope):
        if hope < 35:
            return "low"
        if hope >= 65:
            return "high"
        return "mid"

    def _hope_delta(self, text, oracle):
        lower = text.lower()
        positive_words = ["救", "保护", "修", "唱歌", "水", "希望", "点亮"]
        negative_words = ["毁", "杀", "放弃", "背叛", "燃烧"]
        score = 0
        if any(word in lower for word in positive_words):
            score += 2
        if any(word in lower for word in negative_words):
            score -= 2
        limit = 5 if oracle else 3
        return max(-limit, min(limit, score))

    def _limit(self, text, max_len):
        return text if len(text) <= max_len else text[: max_len - 1] + "…"


class AsyncGameService:
    def __init__(self, game, location_runtime):
        self.game = game
        self.location_runtime = location_runtime
        self._pending_settlement = False

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

    def ingest_mail_reply(self, email, text):
        if not isinstance(email, str) or not email.strip():
            raise ApiError("NOT_FOUND", "世界找不到这封回信的主人。", status=404)
        self.game._validate_text(text, "回信")
        with self.game.lock:
            char = self.game.conn.execute(
                """
                SELECT * FROM characters
                WHERE lower(email) = lower(?) AND status = 'active'
                ORDER BY cycle_joined DESC, id DESC LIMIT 1
                """,
                (email.strip(),),
            ).fetchone()
            if char is None:
                char = self.game.conn.execute(
                    """
                    SELECT * FROM characters
                    WHERE lower(email) = lower(?)
                    ORDER BY cycle_joined DESC, id DESC LIMIT 1
                    """,
                    (email.strip(),),
                ).fetchone()
            if char is None:
                raise ApiError("NOT_FOUND", "世界找不到这封回信的主人。", status=404)
        return self.act({
            "charId": char["id"],
            "text": text,
            "kind": "narration",
        })

    def act(self, payload):
        self.game._require_mapping(payload)
        kind = payload.get("kind")
        if kind == "heartbeat":
            return self.game.heartbeat(payload.get("hint"))
        if kind not in {"action", "narration", "oracle"}:
            raise ApiError("REJECTED", "世界听不懂这种行动。")

        text = payload.get("text")
        self.game._validate_text(text, "行动")
        with self.game.lock:
            if self._pending_settlement:
                raise ApiError("SETTLING", "世界正在被审判", status=409)
            self.game._ensure_not_settling()
            char = self.game._character(payload.get("charId"), active_only=True)
            rejection = self.game._moderate_input(payload)
            if rejection:
                self.game._log_input(kind, payload, "rejected")
                self.game.conn.commit()
                return {"accepted": False, "reason": rejection}
            oracle_status = None
            oracle_applied = None
            scope = payload.get("scope")
            if kind == "oracle":
                rejection = self.game._moderate_oracle(text)
                if rejection:
                    self.game._log_input("oracle", payload, "rejected")
                    self.game.conn.commit()
                    return {"accepted": False, "reason": rejection}
                oracle_status = self.game._register_oracle(char, text, scope)
                oracle_applied = text if oracle_status == "applied" else None

            world = self.game._increment_round()
            node_data = NODE_EVENTS.get(world["round"])
            if node_data is not None:
                self.game._create_node_acts(world, node_data)
            self.game._expire_old_oracles(world)
            self.game._log_input(kind, payload, "accepted")
            self.game._mark_accepted()
            if world["round"] >= MAX_ROUND:
                self._pending_settlement = True
            self.game.conn.commit()

            job = dict(payload)
            job["_async"] = {
                "cycle": world["cycle"],
                "round": world["round"],
                "oracleApplied": oracle_applied,
            }
            self.location_runtime.enqueue(char["location"], job)

        response = {"accepted": True, "round": world["round"]}
        if oracle_status is not None:
            response["oracleStatus"] = oracle_status
        return response

    def maybe_heartbeat(self, now=None, idle_seconds=10):
        return self.game.maybe_heartbeat(now=now, idle_seconds=idle_seconds)

    def process_next_print_job(self):
        return self.game.process_next_print_job()

    def complete_async_location_job(self, payload):
        try:
            return self.game.process_async_location_job(payload)
        finally:
            async_meta = payload.get("_async") if isinstance(payload, dict) else None
            if isinstance(async_meta, dict) and async_meta.get("round", 0) >= MAX_ROUND:
                with self.game.lock:
                    self._pending_settlement = False
