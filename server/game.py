import json
import html
import hashlib
import re
import threading
import time
import uuid

from .config import get_env
from .db import connect, initialize
from .llm import HttpJsonLlmGateway
from .mail import SmtpMailTransport
from .printer import CommandPrinterDriver


PHASES = ["opening", "early_dev", "mid_crisis", "deadline", "pitch", "awards"]
PHASE_DURATIONS = {
    "opening": 2 * 60,
    "early_dev": 4 * 60,
    "mid_crisis": 3 * 60,
    "deadline": 2 * 60,
    "pitch": 3 * 60,
    "awards": 1 * 60,
}
LOCATIONS = ["工位区A", "工位区B", "工位区C", "泡面咖啡角", "天台", "路演台", "评委席"]
IDEA_REJECTION_MESSAGE = "组委会认为这个点子过于超前,换一个吧"
SENSITIVE_WORDS = ("毁灭", "杀", "血腥", "色情", "政治", "广告", "全世界")
ARTIFACT_STYLE_TOKENS = {
    "--bg-obsidian": "oklch(0.98 0.008 75)",
    "--bg-card": "oklch(1.0 0.005 75 / 0.8)",
    "--bg-card-hover": "oklch(1.0 0.005 75 / 0.95)",
    "--accent-cyan": "oklch(0.76 0.11 85)",
    "--accent-cyan-glow": "oklch(0.76 0.11 85 / 0.35)",
    "--accent-crimson": "oklch(0.68 0.18 355)",
    "--accent-crimson-glow": "oklch(0.68 0.18 355 / 0.35)",
    "--accent-orange": "oklch(0.78 0.13 60)",
    "--accent-orange-glow": "oklch(0.78 0.13 60 / 0.35)",
    "--text-primary": "oklch(0.24 0.03 75)",
    "--text-secondary": "oklch(0.40 0.02 75)",
    "--text-muted": "oklch(0.60 0.015 75)",
    "--glass-border": "oklch(0.24 0.03 75 / 0.08)",
    "--glass-border-focus": "oklch(0.68 0.18 355 / 0.25)",
    "--glass-shadow": "0 8px 32px 0 oklch(0.24 0.03 75 / 0.08)",
    "--font-family": "'Outfit', 'Noto Sans SC', -apple-system, BlinkMacSystemFont, sans-serif",
}
BLOCKED_ARTIFACT_TERMS = (
    "运行 demo",
    "原始 idea",
    "项目进度",
    "ai hackathon demo",
    "猫的照片识别成需求文档",
    "demo-stage",
    "idea-chip",
)


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
        economy_mode=True,
    ):
        self.db_path = db_path
        self.conn = connect(db_path)
        initialize(self.conn)
        self.llm = llm if llm is not None else HttpJsonLlmGateway()
        self.mail_transport = mail_transport if mail_transport is not None else SmtpMailTransport()
        self.printer_driver = printer_driver if printer_driver is not None else CommandPrinterDriver()
        self.economy_mode = economy_mode
        self.lock = threading.RLock()
        with self.lock:
            self.ensure_world()

    def close(self):
        self.conn.close()

    def ensure_world(self, now=None):
        if now is None:
            now = int(time.time())
        edition = self.conn.execute("SELECT * FROM editions ORDER BY id DESC LIMIT 1").fetchone()
        if edition is None:
            self.conn.execute(
                """
                INSERT INTO editions (id, no, phase, phase_ends_at, started_at)
                VALUES (1, 1, 'opening', ?, ?)
                """,
                (now + PHASE_DURATIONS["opening"], now),
            )
            self._seed_agents()
            self._form_teams(edition_id=1)
            self._event(1, "edition_started", {"no": 1, "phase": "opening"}, now)
            self.conn.commit()
        return {
            "edition": self._edition_public(self._edition()),
            "agents": self.conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0],
        }

    def submit_idea(self, payload):
        self._require_mapping(payload)
        text = self._validate_idea_text(payload.get("text"))
        investor_name = self._optional_text(payload.get("investorName"), 24)
        email = self._optional_text(payload.get("email"), 120)
        now = int(time.time())
        with self.lock:
            edition = self._edition()
            receipt_no = self._new_receipt_no(edition["no"])
            self.conn.execute(
                """
                INSERT INTO ideas
                (text, investor_name, email, receipt_no, status, current_form,
                 progress, current_bug, created_at)
                VALUES (?, ?, ?, ?, 'pooled', ?, 0, NULL, ?)
                """,
                (text, investor_name, email, receipt_no, text, now),
            )
            idea_id = self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            self._enqueue_print("receipt", {
                "ideaId": idea_id,
                "receiptNo": receipt_no,
                "idea": text,
                "investorName": investor_name,
                "editionNo": edition["no"],
                "trackingId": receipt_no,
                "qrUrl": self._tracking_url(idea_id, receipt_no),
            })
            self._event(edition["id"], "idea_received", {
                "ideaId": idea_id,
                "receiptNo": receipt_no,
                "idea": text,
                "investorName": investor_name,
            }, now)
            self._log_input("idea", payload, "accepted")
            self.conn.commit()
            return {"ideaId": idea_id, "receiptNo": receipt_no}

    def idea(self, idea_id):
        with self.lock:
            row = self._idea_row(idea_id)
            team = self._team_row(row["team_id"]) if row["team_id"] else None
            gossip = [
                json.loads(event["payload_json"]).get("text")
                for event in self.conn.execute(
                    """
                    SELECT payload_json FROM events
                    WHERE type IN ('gossip', 'conversation') AND payload_json LIKE ?
                    ORDER BY id DESC LIMIT 5
                    """,
                    (f"%{row['text']}%",),
                ).fetchall()
            ]
            return {
                "ideaId": row["id"],
                "receiptNo": row["receipt_no"],
                "status": row["status"],
                "teamName": None if team is None else team["name"],
                "projectId": row["id"] if row["team_id"] else None,
                "currentForm": row["current_form"],
                "progress": row["progress"],
                "currentBug": row["current_bug"],
                "artifact": self._artifact_summary_for_idea(row["id"]),
                "gossip": [item for item in gossip if item],
                "review": row["review"],
                "rank": row["rank"],
            }

    def project(self, project_id):
        with self.lock:
            row = self._idea_row(project_id)
            if not row["team_id"]:
                raise ApiError("NOT_FOUND", "这个项目还没有被队伍认领。", status=404)
            team = self._team_row(row["team_id"])
            return {
                "projectId": row["id"],
                "ideaId": row["id"],
                "receiptNo": row["receipt_no"],
                "teamName": team["name"],
                "ideaText": row["text"],
                "currentForm": row["current_form"],
                "progress": row["progress"],
                "currentBug": row["current_bug"],
                "tasks": self._tasks_for_idea(row["id"]),
                "commits": self._commits_for_idea(row["id"]),
                "artifact": self._artifact_summary_for_idea(row["id"]),
            }

    def artifact(self, artifact_id):
        with self.lock:
            row = self.conn.execute("SELECT * FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()
            if row is None:
                raise ApiError("NOT_FOUND", "找不到这个演示产物。", status=404)
            return {
                "artifactId": row["id"],
                "ideaId": row["idea_id"],
                "version": row["version"],
                "type": row["type"],
                "title": row["title"],
                "summary": row["summary"],
                "contentType": row["content_type"],
                "body": row["body"],
            }

    def idea_page(self, idea_id):
        with self.lock:
            idea = self._idea_row(idea_id)
            artifact = self.conn.execute(
                "SELECT * FROM artifacts WHERE idea_id = ? ORDER BY version DESC LIMIT 1",
                (idea["id"],),
            ).fetchone()
            return {
                "contentType": "text/html; charset=utf-8",
                "body": self._idea_progress_html(idea, artifact),
            }

    def visual_artifact_page(self, idea_id):
        with self.lock:
            idea = self._idea_row(idea_id)
            artifact = self.conn.execute(
                "SELECT * FROM artifacts WHERE idea_id = ? ORDER BY version DESC LIMIT 1",
                (idea["id"],),
            ).fetchone()
            if artifact is None:
                return {
                    "contentType": "text/html; charset=utf-8",
                    "body": self._waiting_artifact_html(idea),
                }
            return {"contentType": artifact["content_type"], "body": artifact["body"]}

    def tick(self, now=None):
        if now is None:
            now = int(time.time())
        with self.lock:
            self.ensure_world(now=now)
            edition = self._edition()
            if now >= edition["phase_ends_at"]:
                edition = self._advance_phase(edition, now)
            if edition["phase"] == "awards":
                self._award_ideas(edition, now)
            elif edition["phase"] == "pitch":
                self._pitch_ideas(edition, now)
            else:
                self._claim_waiting_ideas(edition, now)
                self._advance_projects(edition, now)
                self._conversation_tick(edition, now)
            self.conn.commit()
            return {"advanced": True, "edition": self._edition_public(self._edition())}

    def world(self, after=0):
        after = self._coerce_non_negative_int(after)
        with self.lock:
            edition = self._edition()
            agents = [self._agent_public(row) for row in self.conn.execute("SELECT * FROM agents ORDER BY id").fetchall()]
            conversations = [
                self._conversation_public(row)
                for row in self.conn.execute(
                    "SELECT * FROM conversations WHERE edition_id = ? ORDER BY id DESC LIMIT 12",
                    (edition["id"],),
                ).fetchall()
            ]
            projects = [self._project_public(row) for row in self.conn.execute(
                "SELECT * FROM ideas WHERE team_id IS NOT NULL ORDER BY id"
            ).fetchall()]
            events = [
                self._event_public(row)
                for row in self.conn.execute(
                    "SELECT * FROM events WHERE id > ? ORDER BY id LIMIT 100",
                    (after,),
                ).fetchall()
            ]
            return {
                "edition": self._edition_public(edition),
                "agents": agents,
                "conversations": list(reversed(conversations)),
                "projects": projects,
                "events": events,
            }

    def agent(self, agent_id):
        with self.lock:
            row = self.conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
            if row is None:
                raise ApiError("NOT_FOUND", "找不到这位参赛者。", status=404)
            memories = [
                {"id": item["id"], "text": item["text"], "importance": item["importance"]}
                for item in self.conn.execute(
                    "SELECT * FROM memories WHERE agent_id = ? ORDER BY id DESC LIMIT 10",
                    (agent_id,),
                ).fetchall()
            ]
            return {
                "card": {
                    "id": row["id"],
                    "name": row["name"],
                    "persona": row["persona"],
                    "stack": row["stack"],
                    "catchphrase": row["catchphrase"],
                    "role": row["role"],
                },
                "intent": row["intent"],
                "memories": memories,
            }

    def host(self, payload):
        self._require_mapping(payload)
        action = payload.get("action")
        now = int(time.time())
        with self.lock:
            edition = self._edition()
            if action == "start":
                self.reset_story({"confirm": "RESET"})
            elif action == "skip_phase":
                phase = payload.get("phase")
                if phase not in PHASES:
                    phase = self._next_phase(edition["phase"])
                self.conn.execute(
                    "UPDATE editions SET phase = ?, phase_ends_at = ? WHERE id = ?",
                    (phase, now + PHASE_DURATIONS[phase], edition["id"]),
                )
                self._event(edition["id"], "phase_changed", {"phase": phase}, now)
            elif action == "finale":
                self.conn.execute(
                    "UPDATE editions SET phase = 'awards', phase_ends_at = ? WHERE id = ?",
                    (now + PHASE_DURATIONS["awards"], edition["id"]),
                )
                self._award_ideas(self._edition(), now)
            else:
                raise ApiError("REJECTED", "主持人动作无效。")
            self.conn.commit()
            return {"edition": self._edition_public(self._edition())}

    def admin(self):
        with self.lock:
            settings = self._admin_settings()
            stats = {
                "ideas": self.conn.execute("SELECT COUNT(*) FROM ideas").fetchone()[0],
                "agents": self.conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0],
                "events": self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0],
                "pendingPrintJobs": self.conn.execute("SELECT COUNT(*) FROM print_queue WHERE status = 'pending'").fetchone()[0],
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
                story_background = self._optional_text(payload["storyBackground"], 1200)
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
            for table in [
                "editions",
                "agents",
                "teams",
                "ideas",
                "memories",
                "conversations",
                "events",
                "project_tasks",
                "project_commits",
                "artifacts",
                "print_queue",
                "mail_queue",
                "inputs_log",
            ]:
                self.conn.execute(f"DELETE FROM {table}")
            self.conn.commit()
            self.ensure_world()
            result = self.admin()
            result["reset"] = True
            return result

    def print_pending(self, token=None, limit=5):
        limit = max(1, min(self._coerce_non_negative_int(limit) or 5, 20))
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM print_queue WHERE status = 'pending' ORDER BY id LIMIT ?",
                (limit,),
            ).fetchall()
            return [
                {"ticketId": row["id"], "kind": row["kind"], "payload": json.loads(row["payload_json"])}
                for row in rows
            ]

    def print_ack(self, payload, token=None):
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

    def process_next_print_job(self):
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM print_queue WHERE status = 'pending' ORDER BY id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            payload = json.loads(row["payload_json"])
            ticket = {
                "id": row["id"],
                "kind": row["kind"],
                "payload": payload,
                "qrUrl": self._ticket_qr_url(payload),
            }
            try:
                self.printer_driver.print_ticket(ticket)
            except Exception:
                self.conn.commit()
                return {"id": row["id"], "kind": row["kind"], "payload": payload, "status": row["status"]}
            self.conn.execute("UPDATE print_queue SET status = 'printed' WHERE id = ?", (row["id"],))
            self.conn.commit()
            return {"id": row["id"], "kind": row["kind"], "payload": payload, "status": "printed"}

    def process_next_mail_job(self):
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM mail_queue WHERE status = 'pending' ORDER BY id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            message = {
                "to": row["email"],
                "subject": "你的 AI 黑客松项目有新进展",
                "body": row["in_world_reason"],
                "kind": row["kind"],
                "ideaId": row["idea_id"],
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
        return {"accepted": True}

    def enqueue_beat(self, now=None):
        return {"queued": False, "reason": "removed"}

    def process_next_weave_job(self):
        return None

    def maybe_beat(self, now=None, idle_seconds=25):
        if self._admin_settings()["generation_paused"]:
            return None
        return self.tick(now=now)

    def _claim_waiting_ideas(self, edition, now):
        teams = self.conn.execute(
            "SELECT * FROM teams WHERE edition_id = ? ORDER BY id",
            (edition["id"],),
        ).fetchall()
        ideas = self.conn.execute(
            """
            SELECT * FROM ideas
            WHERE status IN ('pooled', 'carried_over') AND team_id IS NULL
            ORDER BY id
            """
        ).fetchall()
        for index, idea in enumerate(ideas):
            team = teams[index % len(teams)]
            current_form = self._mutate_form(idea["text"], edition["phase"])
            self.conn.execute(
                """
                UPDATE ideas
                SET status = 'developing', team_id = ?, current_form = ?, current_bug = ?
                WHERE id = ?
                """,
                (team["id"], current_form, self._bug_for(idea["text"], 0), idea["id"]),
            )
            self.conn.execute("UPDATE teams SET idea_id = ? WHERE id = ?", (idea["id"], team["id"]))
            self._ensure_project_work_items(idea, team, now)
            artifact = self._upsert_artifact(idea, team, current_form, 0, self._bug_for(idea["text"], 0), now)
            self._event(edition["id"], "idea_claimed", {
                "ideaId": idea["id"],
                "projectId": idea["id"],
                "teamName": team["name"],
                "idea": idea["text"],
                "currentForm": current_form,
                "artifact": self._artifact_summary_or_none(artifact),
            }, now)
            self._insert_mail(idea, "claimed", f"你的点子被 {team['name']} 认领了: {current_form}", now)

    def _advance_projects(self, edition, now):
        ideas = self.conn.execute(
            "SELECT * FROM ideas WHERE status IN ('developing', 'pivoted') ORDER BY id"
        ).fetchall()
        for idea in ideas:
            if idea["progress"] >= 100:
                continue
            increment = 18 if edition["phase"] == "deadline" else 9
            progress = min(100, idea["progress"] + increment)
            bug = self._bug_for(idea["text"], progress)
            status = "presented" if edition["phase"] == "pitch" else idea["status"]
            self.conn.execute(
                "UPDATE ideas SET progress = ?, current_bug = ?, status = ? WHERE id = ?",
                (progress, bug, status, idea["id"]),
            )
            team = self._team_row(idea["team_id"])
            self._advance_project_tasks(idea, team, progress, bug, now)
            artifact = self._upsert_artifact(idea, team, idea["current_form"], progress, bug, now)
            self._event(edition["id"], "project_update", {
                "ideaId": idea["id"],
                "projectId": idea["id"],
                "progress": progress,
                "currentBug": bug,
                "artifact": self._artifact_summary_or_none(artifact),
            }, now)

    def _conversation_tick(self, edition, now):
        agents = self.conn.execute(
            """
            SELECT * FROM agents
            WHERE role = 'hacker'
            ORDER BY id LIMIT 2
            """
        ).fetchall()
        if len(agents) < 2:
            return
        idea = self.conn.execute("SELECT * FROM ideas ORDER BY id DESC LIMIT 1").fetchone()
        idea_text = "空白项目"
        if idea is not None:
            idea_text = idea["text"]
        memories = {agent["id"]: self._top_memories(agent["id"]) for agent in agents}
        context = {
            "task": "conversation",
            "mode": "economy",
            "edition": self._edition_public(edition),
            "idea": idea_text,
            "agents": [
                {
                    "id": agent["id"],
                    "name": agent["name"],
                    "persona": agent["persona"],
                    "stack": agent["stack"],
                    "catchphrase": agent["catchphrase"],
                    "intent": agent["intent"],
                    "memories": memories[agent["id"]],
                }
                for agent in agents
            ],
        }
        result = self._coerce_conversation_result(self._weave_with_llm(context), context)
        self.conn.execute(
            """
            INSERT INTO conversations (edition_id, location, agent_ids_json, lines_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                edition["id"],
                "泡面咖啡角",
                json.dumps([agent["id"] for agent in agents], ensure_ascii=False),
                json.dumps(result["lines"], ensure_ascii=False),
                now,
            ),
        )
        conversation_id = self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        for agent in agents:
            memory_text = result["memories"].get(agent["id"]) or f"听说有人想做{idea_text}"
            self._insert_memory(agent["id"], memory_text, 3, now)
            self.conn.execute(
                "UPDATE agents SET intent = ?, location = ?, talking = 0 WHERE id = ?",
                (result["intents"].get(agent["id"]) or "想继续打探别队进度", "泡面咖啡角", agent["id"]),
            )
        gossip_text = f"展区有人提到了你的idea: {idea_text}"
        self._event(edition["id"], "conversation", {
            "conversationId": conversation_id,
            "location": "泡面咖啡角",
            "text": gossip_text,
            "lines": result["lines"],
        }, now)
        self._event(edition["id"], "gossip", {"text": gossip_text, "idea": idea_text}, now)

    def _pitch_ideas(self, edition, now):
        ideas = self.conn.execute(
            "SELECT * FROM ideas WHERE team_id IS NOT NULL AND status IN ('developing', 'pivoted', 'presented') ORDER BY id"
        ).fetchall()
        for idea in ideas:
            review = idea["review"] or f"AI评委: {idea['current_form']} 很有黑客松精神,但 demo 像临时长出来的。"
            self.conn.execute(
                "UPDATE ideas SET status = 'presented', review = ?, progress = 100 WHERE id = ?",
                (review, idea["id"]),
            )
            self._event(edition["id"], "pitch", {"ideaId": idea["id"], "review": review}, now)

    def _award_ideas(self, edition, now):
        ideas = self.conn.execute(
            "SELECT * FROM ideas WHERE team_id IS NOT NULL AND status IN ('presented', 'developing', 'pivoted') ORDER BY progress DESC, id"
        ).fetchall()
        if not ideas:
            return
        leaderboard = []
        for rank, idea in enumerate(ideas, start=1):
            review = idea["review"] or f"AI评委: {idea['current_form']} 让人想投资一包泡面。"
            self.conn.execute(
                "UPDATE ideas SET status = 'awarded', rank = ?, review = ? WHERE id = ?",
                (rank, review, idea["id"]),
            )
            payload = {
                "ideaId": idea["id"],
                "projectId": idea["id"],
                "receiptNo": idea["receipt_no"],
                "idea": idea["text"],
                "investorName": idea["investor_name"],
                "teamName": self._team_row(idea["team_id"])["name"],
                "currentForm": idea["current_form"],
                "artifact": self._artifact_summary_for_idea(idea["id"]),
                "trackingId": idea["receipt_no"],
                "qrUrl": self._tracking_url(idea["id"], idea["receipt_no"]),
                "review": review,
                "rank": rank,
            }
            leaderboard.append(payload)
            self._enqueue_print("certificate", payload)
            self._insert_mail(idea, "award", f"你的点子获得第 {rank} 名: {review}", now)
        self._enqueue_print("leaderboard", {"editionNo": edition["no"], "awards": leaderboard})
        self._event(edition["id"], "awards", {"awards": leaderboard}, now)

    def _advance_phase(self, edition, now):
        next_phase = self._next_phase(edition["phase"])
        if next_phase == "opening" and edition["phase"] == "awards":
            no = edition["no"] + 1
            self.conn.execute(
                "INSERT INTO editions (no, phase, phase_ends_at, started_at) VALUES (?, 'opening', ?, ?)",
                (no, now + PHASE_DURATIONS["opening"], now),
            )
            new_edition = self._edition()
            self._form_teams(new_edition["id"])
            self.conn.execute(
                "UPDATE ideas SET status = 'carried_over' WHERE status = 'pooled'"
            )
            self._event(new_edition["id"], "edition_started", {"no": no, "phase": "opening"}, now)
            return new_edition
        self.conn.execute(
            "UPDATE editions SET phase = ?, phase_ends_at = ? WHERE id = ?",
            (next_phase, now + PHASE_DURATIONS[next_phase], edition["id"]),
        )
        self._event(edition["id"], "phase_changed", {"phase": next_phase}, now)
        return self._edition()

    def _next_phase(self, phase):
        index = PHASES.index(phase)
        return PHASES[(index + 1) % len(PHASES)]

    def _weave_with_llm(self, context):
        try:
            result = self.llm.weave(context)
        except (TypeError, KeyError, ValueError, RuntimeError):
            result = None
        if isinstance(result, dict):
            return result
        return None

    def _coerce_conversation_result(self, result, context):
        agent_a, agent_b = context["agents"]
        if result:
            lines = result.get("lines")
            memories = result.get("memories") or {}
            intents = result.get("intents") or {}
            if isinstance(lines, list) and lines:
                return {"lines": lines, "memories": memories, "intents": intents}
        idea = context["idea"]
        lines = [
            {"speakerId": agent_a["id"], "text": f"听说有人投了“{idea}”,这个用{agent_a['stack']}重写一遍就好了。"},
            {"speakerId": agent_b["id"], "text": f"先别重写,我觉得可以包装成路演故事。{agent_b['catchphrase']}"},
            {"speakerId": agent_a["id"], "text": "那我负责把 bug 命名得像功能。"},
        ]
        return {
            "lines": lines,
            "memories": {
                agent_a["id"]: f"听说新点子是{idea},可能适合技术炫技。",
                agent_b["id"]: f"听说新点子是{idea},也许能包装成好故事。",
            },
            "intents": {
                agent_a["id"]: "想偷看三号桌的进度",
                agent_b["id"]: "想把 demo 讲成愿景",
            },
        }

    def _seed_agents(self):
        if self.conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0] > 0:
            return
        hackers = [
            ("h_backend", "后端仔·倔", "Rust原教旨", "Rust", "这个用Rust重写一遍就好了"),
            ("h_ppt", "PPT侠·嘴强", "零代码叙事专家", "Slides", "demo不重要,故事重要"),
            ("h_css", "像素洁癖", "CSS完美主义", "CSS", "这个间距差了两像素"),
            ("h_ai", "提示词巫师", "Prompt炼金术", "LLM", "先让模型自己想想"),
            ("h_ops", "部署消防员", "凌晨上线体质", "Docker", "我本地是好的"),
            ("h_data", "表格先知", "Excel能解决一切", "SQL", "先建个表"),
            ("h_mobile", "小程序游侠", "扫码入口执念", "MiniApp", "用户只会给你三秒"),
            ("h_game", "玩法拆弹员", "把需求做成游戏", "Canvas", "加个进度条就有反馈"),
            ("h_fullstack", "全栈临时工", "什么都能接", "TypeScript", "我先糊一个能跑的"),
        ]
        judges = [
            ("j_sharp", "毒舌评委", "专治伪需求", "投资", "所以用户是谁"),
            ("j_design", "体验评委", "看重第一眼", "UX", "我需要被打动"),
            ("j_tech", "架构评委", "追问可行性", "System", "边界条件呢"),
        ]
        for agent_id, name, persona, stack, catchphrase in hackers:
            self.conn.execute(
                """
                INSERT INTO agents
                (id, name, persona, stack, catchphrase, role, team_id, location, intent)
                VALUES (?, ?, ?, ?, ?, 'hacker', NULL, '工位区A', '想找队友组队')
                """,
                (agent_id, name, persona, stack, catchphrase),
            )
            self._insert_memory(agent_id, f"{name}刚到现场,正在观察谁靠谱。", 2, int(time.time()))
        for agent_id, name, persona, stack, catchphrase in judges:
            self.conn.execute(
                """
                INSERT INTO agents
                (id, name, persona, stack, catchphrase, role, team_id, location, intent)
                VALUES (?, ?, ?, ?, ?, 'judge', NULL, '评委席', '想找到真正能跑的demo')
                """,
                (agent_id, name, persona, stack, catchphrase),
            )
            self._insert_memory(agent_id, f"{name}准备好点评本届项目。", 3, int(time.time()))

    def _form_teams(self, edition_id):
        if self.conn.execute("SELECT COUNT(*) FROM teams WHERE edition_id = ?", (edition_id,)).fetchone()[0]:
            return
        hackers = [row["id"] for row in self.conn.execute("SELECT id FROM agents WHERE role = 'hacker' ORDER BY id").fetchall()]
        team_defs = [
            ("team_1", "泡面独角兽", hackers[0:3], "工位区A"),
            ("team_2", "Deadline 救援队", hackers[3:6], "工位区B"),
            ("team_3", "天台重构社", hackers[6:9], "工位区C"),
        ]
        for team_id, name, members, location in team_defs:
            self.conn.execute(
                """
                INSERT OR REPLACE INTO teams (id, edition_id, name, member_ids_json, idea_id)
                VALUES (?, ?, ?, ?, NULL)
                """,
                (team_id, edition_id, name, json.dumps(members, ensure_ascii=False)),
            )
            for member in members:
                self.conn.execute(
                    "UPDATE agents SET team_id = ?, location = ?, intent = '想认领一个离谱但能讲的idea' WHERE id = ?",
                    (team_id, location, member),
                )

    def _edition(self):
        row = self.conn.execute("SELECT * FROM editions ORDER BY id DESC LIMIT 1").fetchone()
        if row is None:
            self.ensure_world()
            row = self.conn.execute("SELECT * FROM editions ORDER BY id DESC LIMIT 1").fetchone()
        return row

    def _edition_public(self, row):
        return {"no": row["no"], "phase": row["phase"], "phaseEndsAt": row["phase_ends_at"]}

    def _agent_public(self, row):
        return {
            "id": row["id"],
            "name": row["name"],
            "location": row["location"],
            "teamId": row["team_id"],
            "talking": bool(row["talking"]),
        }

    def _conversation_public(self, row):
        return {
            "id": row["id"],
            "location": row["location"],
            "lines": json.loads(row["lines_json"]),
        }

    def _event_public(self, row):
        payload = json.loads(row["payload_json"])
        payload["id"] = row["id"]
        payload["type"] = row["type"]
        return payload

    def _project_public(self, row):
        team = self._team_row(row["team_id"]) if row["team_id"] else None
        return {
            "projectId": row["id"],
            "ideaId": row["id"],
            "teamName": None if team is None else team["name"],
            "ideaText": row["text"],
            "currentForm": row["current_form"],
            "progress": row["progress"],
            "currentBug": row["current_bug"],
            "artifact": self._artifact_summary_for_idea(row["id"]),
        }

    def _ensure_project_work_items(self, idea, team, now):
        if self.conn.execute("SELECT COUNT(*) FROM project_tasks WHERE idea_id = ?", (idea["id"],)).fetchone()[0]:
            return
        members = json.loads(team["member_ids_json"])
        task_defs = [
            ("产品定义", f"把“{idea['text']}”压成一个评委能看懂的 MVP"),
            ("界面原型", "做出可点击的单页 demo,先保证能展示"),
            ("数据与状态", "设计演示数据、状态切换和假结果"),
            ("路演包装", "准备一句话卖点和偏题解释"),
        ]
        for index, (title, output) in enumerate(task_defs):
            owner = members[index % len(members)]
            self.conn.execute(
                """
                INSERT INTO project_tasks
                (idea_id, team_id, title, owner_agent_id, status, output, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'todo', ?, ?, ?)
                """,
                (idea["id"], team["id"], title, owner, output, now, now),
            )

    def _advance_project_tasks(self, idea, team, progress, bug, now):
        self._ensure_project_work_items(idea, team, now)
        tasks = self.conn.execute(
            "SELECT * FROM project_tasks WHERE idea_id = ? ORDER BY id",
            (idea["id"],),
        ).fetchall()
        if not tasks:
            return
        done_slots = max(1, min(len(tasks), progress // 25 + 1))
        for index, task in enumerate(tasks):
            status = "done" if index < done_slots else "doing" if index == done_slots else "todo"
            if task["status"] == status and status != "done":
                continue
            output = self._task_output(task["title"], idea["current_form"], progress, bug)
            self.conn.execute(
                "UPDATE project_tasks SET status = ?, output = ?, updated_at = ? WHERE id = ?",
                (status, output, now, task["id"]),
            )
            if status == "done" and task["status"] != "done":
                self._insert_project_commit(
                    idea["id"],
                    task["owner_agent_id"],
                    f"完成{task['title']}",
                    output,
                    None,
                    now,
                )

    def _insert_project_commit(self, idea_id, agent_id, message, diff_summary, artifact_id, now):
        self.conn.execute(
            """
            INSERT INTO project_commits
            (idea_id, agent_id, message, diff_summary, artifact_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (idea_id, agent_id, self._limit(message, 80), self._limit(diff_summary, 240), artifact_id, now),
        )

    def _upsert_artifact(self, idea, team, current_form, progress, bug, now):
        previous = self.conn.execute(
            "SELECT * FROM artifacts WHERE idea_id = ? ORDER BY version DESC LIMIT 1",
            (idea["id"],),
        ).fetchone()
        version = 1 if previous is None else previous["version"] + 1
        title = self._artifact_title(current_form)
        plan = self._artifact_plan(idea["text"], current_form)
        summary = f"{team['name']} 做出的 {plan['label']} 前端原型: {current_form}"
        body = self._artifact_body(idea, team, title, current_form, progress, bug, plan)
        if body is None:
            return previous
        fallback_body = 'data-artifact-status="llm-unavailable"' in body
        if previous is None:
            self.conn.execute(
                """
                INSERT INTO artifacts
                (idea_id, version, type, title, summary, body, content_type, created_at, updated_at)
                VALUES (?, ?, 'html', ?, ?, ?, 'text/html; charset=utf-8', ?, ?)
                """,
                (idea["id"], version, title, summary, body, now, now),
            )
            artifact_id = self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        else:
            artifact_id = previous["id"]
            self.conn.execute(
                """
                UPDATE artifacts
                SET version = ?, title = ?, summary = ?, body = ?, updated_at = ?
                WHERE id = ?
                """,
                (version, title, summary, body, now, artifact_id),
            )
        self._insert_project_commit(
            idea["id"],
            json.loads(team["member_ids_json"])[0],
            f"挂载容错界面 v{version}" if fallback_body else f"发布 demo v{version}",
            f"LLM 生成接口暂不可用,展示容错界面: {bug}" if fallback_body else f"更新 {plan['label']} HTML 原型到 {progress}%: {bug}",
            artifact_id,
            now,
        )
        return self.conn.execute("SELECT * FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()

    def _artifact_body(self, idea, team, title, current_form, progress, bug, plan):
        generated = self._coerce_artifact_html(self._weave_with_llm(
            self._artifact_context(idea, team, title, current_form, progress, bug, plan)
        ))
        if generated is not None:
            return generated
        return self._fallback_artifact_html(idea, team, title, current_form, progress, bug)

    def _artifact_context(self, idea, team, title, current_form, progress, bug, plan):
        fingerprint = hashlib.sha1(f"{idea['id']}:{idea['text']}:{current_form}".encode("utf-8")).hexdigest()[:8]
        return {
            "task": "artifact",
            "mode": "frontend-html",
            "idea": {
                "id": idea["id"],
                "text": idea["text"],
                "receiptNo": idea["receipt_no"],
            },
            "team": {
                "id": team["id"],
                "name": team["name"],
                "memberIds": json.loads(team["member_ids_json"]),
            },
            "artifact": {
                "title": title,
                "currentForm": current_form,
                "progress": progress,
                "bug": bug,
                "demoKind": plan["kind"],
                "visualLabel": plan["label"],
                "url": self._visual_artifact_url(idea["id"]),
                "ideaFingerprint": fingerprint,
                "styleTokens": ARTIFACT_STYLE_TOKENS,
            },
            "tasks": self._tasks_for_idea(idea["id"]),
            "commits": self._commits_for_idea(idea["id"]),
            "requirements": [
                "Return one self-contained HTML document as result.html.",
                "Generate a real standalone frontend for the exact submitted idea, not a pitch wrapper or status page.",
                "Do not use presets, canned demo kinds, topic templates, or generic project-progress shells.",
                "Use distinct layout, copy, controls, and visual concept derived from this idea.",
                "Use the provided OKLCH styleTokens from styles.css in the document CSS.",
                "Include data-project-id on the main element.",
                "Include data-idea-fingerprint on the main element using artifact.ideaFingerprint.",
                "Include at least one button with meaningful interaction.",
                "Do not include progress dashboards, run-demo buttons, original-idea badges, or explanation of the hackathon system.",
            ],
        }

    def _coerce_artifact_html(self, result):
        if not isinstance(result, dict):
            return None
        body = result.get("html") or result.get("body")
        if not isinstance(body, str):
            return None
        stripped = body.strip()
        lowered = stripped.lower()
        if "<!doctype html" not in lowered or "</html>" not in lowered:
            return None
        if "data-project-id" not in stripped or "data-idea-fingerprint" not in stripped:
            return None
        if "<button" not in lowered:
            return None
        if any(term in lowered for term in BLOCKED_ARTIFACT_TERMS):
            return None
        return stripped

    def _artifact_summary_for_idea(self, idea_id):
        row = self.conn.execute(
            "SELECT * FROM artifacts WHERE idea_id = ? ORDER BY version DESC LIMIT 1",
            (idea_id,),
        ).fetchone()
        if row is None:
            return None
        return self._artifact_summary(row)

    def _artifact_summary(self, row):
        return {
            "artifactId": row["id"],
            "type": row["type"],
            "title": row["title"],
            "summary": row["summary"],
            "version": row["version"],
            "url": self._visual_artifact_url(row["idea_id"]),
            "apiUrl": f"/api/artifact/{row['id']}",
        }

    def _artifact_summary_or_none(self, row):
        return None if row is None else self._artifact_summary(row)

    def _tasks_for_idea(self, idea_id):
        return [
            {
                "id": row["id"],
                "title": row["title"],
                "ownerAgentId": row["owner_agent_id"],
                "status": row["status"],
                "output": row["output"],
            }
            for row in self.conn.execute(
                "SELECT * FROM project_tasks WHERE idea_id = ? ORDER BY id",
                (idea_id,),
            ).fetchall()
        ]

    def _commits_for_idea(self, idea_id):
        return [
            {
                "id": row["id"],
                "agentId": row["agent_id"],
                "message": row["message"],
                "diffSummary": row["diff_summary"],
                "artifactId": row["artifact_id"],
                "createdAt": row["created_at"],
            }
            for row in self.conn.execute(
                "SELECT * FROM project_commits WHERE idea_id = ? ORDER BY id",
                (idea_id,),
            ).fetchall()
        ]

    def _task_output(self, title, current_form, progress, bug):
        if title == "产品定义":
            return f"MVP 不是完整实现“{current_form}”,而是展示用户输入后得到一个可解释结果。"
        if title == "界面原型":
            return f"单页 demo 已有标题、输入框、按钮和结果区,完成度 {progress}%。"
        if title == "数据与状态":
            return f"用假数据驱动演示状态; 当前已知问题: {bug}"
        return f"路演说法: 这个 demo 先证明方向,偏题部分包装成黑客松特色。"

    def _artifact_title(self, current_form):
        cleaned = re.sub(r"[^\w\u4e00-\u9fff]+", "", current_form)[:12] or "AI黑客松Demo"
        return f"{cleaned} Demo"

    def _artifact_plan(self, idea_text, current_form):
        return {"kind": "generated-frontend", "label": "AI Generated Frontend"}

    def _fallback_artifact_html(self, idea, team, title, current_form, progress, bug):
        safe_title = html.escape(title)
        safe_idea = html.escape(idea["text"])
        safe_form = html.escape(current_form)
        safe_team = html.escape(team["name"])
        safe_receipt = html.escape(idea["receipt_no"])
        safe_bug = html.escape(bug or "LLM 生成接口暂不可用")
        progress_value = max(0, min(int(progress), 100))
        tokens = "; ".join(f"{name}: {value}" for name, value in ARTIFACT_STYLE_TOKENS.items())
        return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="6">
  <title>{safe_title} - 生成容错</title>
  <style>
    :root {{ color-scheme: light; {tokens}; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; min-height: 100vh; background: var(--bg-obsidian); color: var(--text-primary); font-family: var(--font-family); display: grid; place-items: center; padding: 24px; }}
    main {{ width: min(880px, 100%); border: 1px solid var(--glass-border); border-radius: 16px; background: var(--bg-card); box-shadow: var(--glass-shadow); padding: clamp(22px, 5vw, 48px); }}
    .status {{ display: inline-flex; align-items: center; gap: 10px; border: 1px solid var(--glass-border-focus); border-radius: 999px; padding: 8px 12px; color: var(--text-secondary); font-size: 14px; font-weight: 800; }}
    .dot {{ width: 10px; height: 10px; border-radius: 50%; background: var(--accent-orange); box-shadow: 0 0 18px var(--accent-orange-glow); }}
    h1 {{ margin: 18px 0 12px; font-size: clamp(34px, 7vw, 72px); line-height: .95; letter-spacing: 0; }}
    p {{ color: var(--text-secondary); line-height: 1.65; }}
    .grid {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; margin-top: 24px; }}
    .tile {{ min-height: 120px; border: 1px solid var(--glass-border); border-radius: 12px; background: var(--bg-card-hover); padding: 16px; }}
    .tile span {{ display: block; color: var(--text-muted); font-size: 13px; font-weight: 800; }}
    .tile strong {{ display: block; margin-top: 8px; font-size: clamp(22px, 4vw, 36px); line-height: 1; }}
    .progress {{ height: 12px; border-radius: 99px; overflow: hidden; background: oklch(0.24 0.03 75 / .08); margin-top: 24px; }}
    .bar {{ width: {progress_value}%; height: 100%; background: linear-gradient(90deg, var(--accent-cyan), var(--accent-orange), var(--accent-crimson)); }}
    @media (max-width: 720px) {{ .grid {{ grid-template-columns: 1fr; }} main {{ border-radius: 12px; }} }}
  </style>
</head>
<body>
  <main data-artifact-status="llm-unavailable" data-idea-id="{idea['id']}" data-receipt-no="{safe_receipt}">
    <div class="status"><i class="dot"></i><span>AI 生成接口暂不可用，正在自动重试</span></div>
    <h1>{safe_idea}</h1>
    <p>这个地址已经挂载成功，但当前展示的是容错界面，不是最终 demo。多 agent 仍在推进任务和提交记录；当模型 API 恢复并返回合格 HTML 后，本页会自动替换为基于 idea 真实生成的前端。</p>
    <div class="grid">
      <div class="tile"><span>当前形态</span><strong>{safe_form}</strong></div>
      <div class="tile"><span>队伍</span><strong>{safe_team}</strong></div>
      <div class="tile"><span>状态</span><strong>{progress_value}%</strong></div>
    </div>
    <p>{safe_bug}</p>
    <div class="progress" aria-label="项目进度"><div class="bar"></div></div>
  </main>
</body>
</html>"""

    def _idea_progress_html(self, idea, artifact):
        team = self._team_row(idea["team_id"]) if idea["team_id"] else None
        tasks = self._tasks_for_idea(idea["id"]) if idea["team_id"] else []
        commits = self._commits_for_idea(idea["id"])
        safe_idea = html.escape(idea["text"])
        safe_status = html.escape(idea["status"])
        safe_team = html.escape(team["name"] if team else "等待 AI 队伍认领")
        safe_form = html.escape(idea["current_form"])
        safe_bug = html.escape(idea["current_bug"] or "暂无")
        safe_receipt = html.escape(idea["receipt_no"])
        progress = max(0, min(int(idea["progress"]), 100))
        task_items = "\n".join(
            f"<li><strong>{html.escape(task['title'])}</strong> "
            f"<span>{html.escape(task['status'])}</span><p>{html.escape(task.get('output') or '')}</p></li>"
            for task in tasks
        ) or "<li><strong>排队中</strong><span>waiting</span><p>AI 黑客正在挑选这个 idea。</p></li>"
        commit_items = "\n".join(
            f"<li>{html.escape(commit['agentId'])}: {html.escape(commit['message'])}</li>"
            for commit in commits[-6:]
        ) or "<li>还没有提交记录。</li>"
        artifact_url = html.escape(self._artifact_summary(artifact)["url"]) if artifact else ""
        artifact_note = (
            f"<p class=\"meta\">AI 前端可视化: <a href=\"{artifact_url}\">{artifact_url}</a></p>"
            if artifact_url else
            f"<p class=\"meta\">AI 前端可视化生成中: <code>{html.escape(self._visual_artifact_url(idea['id']))}</code></p>"
        )
        return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="5">
  <title>{safe_idea} - 项目进度</title>
  <style>
    body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f5f7fb; color: #162033; }}
    main {{ max-width: 860px; margin: 0 auto; padding: 28px 18px; }}
    section {{ background: white; border: 1px solid #d9e1ec; border-radius: 8px; padding: 18px; margin: 14px 0; }}
    h1 {{ font-size: 28px; margin: 8px 0 12px; }}
    h2 {{ font-size: 18px; margin: 0 0 12px; }}
    .meta {{ color: #65748b; font-size: 14px; }}
    .progress {{ height: 14px; background: #dfe6f0; border-radius: 999px; overflow: hidden; }}
    .bar {{ width: {progress}%; height: 100%; background: #17a36b; }}
    ul {{ list-style: none; padding: 0; margin: 0; }}
    li {{ border-top: 1px solid #edf1f6; padding: 10px 0; }}
    li:first-child {{ border-top: 0; }}
    li span {{ float: right; color: #165dff; font-size: 13px; }}
    code {{ background: #eef3f8; border-radius: 4px; padding: 2px 5px; }}
  </style>
</head>
<body>
  <main data-idea-id="{idea['id']}" data-receipt-no="{safe_receipt}">
    <p class="meta">收据 {safe_receipt} / 状态 {safe_status}</p>
    <h1>{safe_idea}</h1>
    <section>
      <h2>项目进度</h2>
      <div class="progress" aria-label="项目进度"><div class="bar"></div></div>
      <p><strong>{progress}%</strong> / {safe_team}</p>
      <p>当前形态: {safe_form}</p>
      <p class="meta">当前 bug: {safe_bug}</p>
      {artifact_note}
    </section>
    <section>
      <h2>A2A 任务动态</h2>
      <ul>{task_items}</ul>
    </section>
    <section>
      <h2>提交记录</h2>
      <ul>{commit_items}</ul>
    </section>
  </main>
</body>
</html>"""

    def _waiting_artifact_html(self, idea):
        safe_idea = html.escape(idea["text"])
        safe_receipt = html.escape(idea["receipt_no"])
        return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="5">
  <title>{safe_idea} - AI 前端生成中</title>
  <style>
    body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f6f8fb; color: #172033; }}
    main {{ max-width: 760px; margin: 0 auto; padding: 32px 20px; }}
    section {{ background: white; border: 1px solid #d9e0ea; border-radius: 8px; padding: 20px; }}
    .meta {{ color: #5d6b82; font-size: 14px; }}
  </style>
</head>
<body>
  <main data-idea-id="{idea['id']}" data-receipt-no="{safe_receipt}">
    <section>
      <p class="meta">收据 {safe_receipt}</p>
      <h1>{safe_idea}</h1>
      <p>AI 队伍正在认领这个 idea。生成第一个前端可视化 HTML 后，本页会自动切换成 demo。</p>
    </section>
  </main>
</body>
</html>"""

    def _tracking_url(self, idea_id, receipt_no=None):
        public_base = get_env("EOOVE_PUBLIC_BASE_URL", "https://eoove.tianmiao.fun").rstrip("/")
        return f"{public_base}/idea/{idea_id}"

    def _visual_artifact_url(self, idea_id):
        return f"/artifacts/idea-{idea_id}.html"

    def _idea_row(self, idea_id):
        row = self.conn.execute("SELECT * FROM ideas WHERE id = ?", (idea_id,)).fetchone()
        if row is None:
            raise ApiError("NOT_FOUND", "找不到这个点子。", status=404)
        return row

    def _team_row(self, team_id):
        row = self.conn.execute("SELECT * FROM teams WHERE id = ?", (team_id,)).fetchone()
        if row is None:
            raise ApiError("NOT_FOUND", "找不到这支队伍。", status=404)
        return row

    def _event(self, edition_id, type_, payload, now):
        self.conn.execute(
            "INSERT INTO events (edition_id, type, payload_json, created_at) VALUES (?, ?, ?, ?)",
            (edition_id, type_, json.dumps(payload, ensure_ascii=False), now),
        )

    def _insert_memory(self, agent_id, text, importance, now):
        self.conn.execute(
            "INSERT INTO memories (agent_id, text, importance, created_at) VALUES (?, ?, ?, ?)",
            (agent_id, self._limit(text, 120), max(1, min(int(importance), 5)), now),
        )

    def _top_memories(self, agent_id):
        return [
            {"text": row["text"], "importance": row["importance"]}
            for row in self.conn.execute(
                "SELECT text, importance FROM memories WHERE agent_id = ? ORDER BY importance DESC, id DESC LIMIT 5",
                (agent_id,),
            ).fetchall()
        ]

    def _mutate_form(self, text, phase):
        if phase == "mid_crisis":
            return f"{text} -> 帮用户写道歉PPT"
        return text

    def _bug_for(self, text, progress):
        if progress >= 80:
            return f"{text} 的演示按钮状态提示还不够清楚"
        if progress >= 40:
            return f"{text} 的输入结果还需要更明确的下一步"
        return f"{text} 的首屏信息层级还需要整理"

    def _new_receipt_no(self, edition_no):
        next_id = self.conn.execute("SELECT COUNT(*) FROM ideas").fetchone()[0] + 1
        return f"E{edition_no:02d}-I{next_id:04d}"

    def _validate_idea_text(self, value):
        if not isinstance(value, str) or not value.strip():
            raise ApiError("REJECTED", IDEA_REJECTION_MESSAGE)
        text = value.strip()
        if len(text) > 30:
            raise ApiError("REJECTED", IDEA_REJECTION_MESSAGE)
        compact = re.sub(r"\s+", "", text)
        if any(word in compact for word in SENSITIVE_WORDS):
            raise ApiError("REJECTED", IDEA_REJECTION_MESSAGE)
        return text

    def _optional_text(self, value, max_len):
        if value is None:
            return None
        if not isinstance(value, str):
            return None
        value = value.strip()
        return self._limit(value, max_len) if value else None

    def _admin_settings(self):
        row = self.conn.execute("SELECT * FROM admin_settings WHERE id = 1").fetchone()
        if row is not None:
            return row
        self.conn.execute(
            """
            INSERT INTO admin_settings
            (id, story_background, beat_interval_seconds, generation_paused)
            VALUES (1, '', 25, 0)
            """
        )
        return self.conn.execute("SELECT * FROM admin_settings WHERE id = 1").fetchone()

    def _admin_public(self, row):
        return {
            "storyBackground": row["story_background"],
            "beatIntervalSeconds": row["beat_interval_seconds"],
            "generationPaused": bool(row["generation_paused"]),
        }

    def _coerce_admin_interval(self, value):
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            raise ApiError("REJECTED", "事件生成时间必须是数字。")
        if parsed < 1 or parsed > 3600:
            raise ApiError("REJECTED", "事件生成时间必须在 1 到 3600 秒之间。")
        return parsed

    def _coerce_non_negative_int(self, value):
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = 0
        return max(parsed, 0)

    def _enqueue_print(self, kind, payload):
        self.conn.execute(
            "INSERT INTO print_queue (kind, payload_json, status) VALUES (?, ?, 'pending')",
            (kind, json.dumps(payload, ensure_ascii=False)),
        )
        self._prune_print_queue()

    def _prune_print_queue(self):
        pending_count = self.conn.execute("SELECT COUNT(*) FROM print_queue WHERE status = 'pending'").fetchone()[0]
        if pending_count <= 20:
            return
        rows = self.conn.execute(
            "SELECT id FROM print_queue WHERE status = 'pending' AND kind = 'receipt' ORDER BY id LIMIT ?",
            (pending_count - 20,),
        ).fetchall()
        for row in rows:
            self.conn.execute("UPDATE print_queue SET status = 'dropped' WHERE id = ?", (row["id"],))

    def _insert_mail(self, idea, kind, reason, now):
        email = idea["email"]
        if not email:
            return None
        status = "pending" if self._mail_allowed(email) else "simulated"
        self.conn.execute(
            """
            INSERT INTO mail_queue (idea_id, email, kind, in_world_reason, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (idea["id"], email, kind, reason, status, now),
        )
        return self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    def _mail_allowed(self, email):
        whitelist = {
            item.strip().lower()
            for item in get_env("MAIL_WHITELIST", "").split(",")
            if item.strip()
        }
        return bool(whitelist) and email.lower() in whitelist

    def _ticket_qr_url(self, payload):
        if payload.get("qrUrl"):
            return payload["qrUrl"]
        if payload.get("ideaId") is not None:
            return self._tracking_url(payload["ideaId"])
        return "/"

    def _mail_public(self, row):
        return {
            "id": row["id"],
            "ideaId": row["idea_id"],
            "email": row["email"],
            "kind": row["kind"],
            "status": row["status"],
            "inWorldReason": row["in_world_reason"],
        }

    def _log_input(self, type_, payload, verdict):
        self.conn.execute(
            "INSERT INTO inputs_log (type, payload_json, verdict, created_at) VALUES (?, ?, ?, ?)",
            (type_, json.dumps(payload, ensure_ascii=False), verdict, int(time.time())),
        )

    def _require_mapping(self, payload):
        if not isinstance(payload, dict):
            raise ApiError("REJECTED", "世界只回应完整的叙述。")

    def _limit(self, text, max_len):
        return text if len(text) <= max_len else text[: max_len - 1] + "…"


class AsyncGameService:
    def __init__(self, game, weave_runtime=None):
        self.game = game
        self.weave_runtime = weave_runtime

    def submit_idea(self, payload):
        return self.game.submit_idea(payload)

    def idea(self, idea_id):
        return self.game.idea(idea_id)

    def project(self, project_id):
        return self.game.project(project_id)

    def artifact(self, artifact_id):
        return self.game.artifact(artifact_id)

    def idea_page(self, idea_id):
        return self.game.idea_page(idea_id)

    def visual_artifact_page(self, idea_id):
        return self.game.visual_artifact_page(idea_id)

    def tick(self, now=None):
        return self.game.tick(now=now)

    def world(self, after=0):
        return self.game.world(after=after)

    def agent(self, agent_id):
        return self.game.agent(agent_id)

    def host(self, payload):
        return self.game.host(payload)

    def admin(self):
        return self.game.admin()

    def update_admin(self, payload):
        return self.game.update_admin(payload)

    def reset_story(self, payload):
        return self.game.reset_story(payload)

    def print_pending(self, token=None, limit=5):
        return self.game.print_pending(token=token, limit=limit)

    def print_ack(self, payload, token=None):
        return self.game.print_ack(payload, token=token)

    def process_next_print_job(self):
        return self.game.process_next_print_job()

    def process_next_mail_job(self):
        return self.game.process_next_mail_job()

    def maybe_beat(self, now=None, idle_seconds=25):
        return self.game.maybe_beat(now=now, idle_seconds=idle_seconds)

    def ingest_mail_reply(self, email, text):
        return self.game.ingest_mail_reply(email, text)
