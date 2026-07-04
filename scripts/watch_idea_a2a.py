#!/usr/bin/env python3
import argparse
import json
import sys
import time
from urllib import error, parse, request


def main():
    parser = argparse.ArgumentParser(
        description="Submit an idea and continuously watch its A2A/project activity."
    )
    parser.add_argument("idea", nargs="?", help="Idea text. If omitted, read from stdin.")
    parser.add_argument("--base", default="http://127.0.0.1:8015", help="Backend base URL.")
    parser.add_argument("--investor", default="", help="Optional investor name.")
    parser.add_argument("--email", default="", help="Optional investor email.")
    parser.add_argument("--interval", type=float, default=2.0, help="Polling interval seconds.")
    parser.add_argument("--fast", action="store_true", help="Set beatIntervalSeconds=1 before watching.")
    parser.add_argument(
        "--public-base",
        default="https://eoove.tianmiao.fun",
        help="Public base URL printed on receipt QR codes.",
    )
    parser.add_argument("--no-submit", type=int, metavar="IDEA_ID", help="Watch an existing idea id.")
    parser.add_argument("--reset", action="store_true", help="Reset story before submitting.")
    parser.add_argument("--history", action="store_true", help="Also print events that existed before this run.")
    args = parser.parse_args()

    base = args.base.rstrip("/")
    public_base = args.public_base.rstrip("/")
    if args.reset:
        post_json(base, "/api/admin/reset", {"confirm": "RESET"})
        print("[admin] reset ok")
    if args.fast:
        post_json(base, "/api/admin", {"beatIntervalSeconds": 1, "generationPaused": False})
        print("[admin] beatIntervalSeconds=1, generationPaused=false")

    after = 0 if args.history else latest_event_id(base)

    if args.no_submit is not None:
        idea_id = args.no_submit
        tracked = get_json(base, f"/api/idea/{idea_id}")
        receipt_no = tracked.get("receiptNo", "")
        idea_text = tracked.get("currentForm") or tracked.get("ideaText") or ""
    else:
        idea_text = args.idea or input("idea> ").strip()
        if not idea_text:
            raise SystemExit("idea is required")
        payload = {"text": idea_text}
        if args.investor:
            payload["investorName"] = args.investor
        if args.email:
            payload["email"] = args.email
        accepted = post_json(base, "/api/idea", payload)
        idea_id = accepted["ideaId"]
        receipt_no = accepted["receiptNo"]
        print(f"[idea] accepted ideaId={idea_id} receiptNo={receipt_no}")

    print(f"[page] local={base}/idea/{idea_id}")
    print(f"[qr] print={public_base}/idea/{idea_id}")
    print(f"[watch] base={base} ideaId={idea_id} receiptNo={receipt_no}")
    print("[watch] Ctrl-C to stop\n")

    seen_commits = set()
    seen_tasks = {}
    seen_artifact_version = None
    last_status = None
    while True:
        try:
            world = get_json(base, f"/api/world?after={after}")
            for event in world.get("events", []):
                after = max(after, int(event.get("id", after)))
                print_event(event, idea_id, idea_text)

            tracked = get_json(base, f"/api/idea/{idea_id}")
            status_key = (
                tracked.get("status"),
                tracked.get("teamName"),
                tracked.get("progress"),
                tracked.get("currentBug"),
            )
            if status_key != last_status:
                last_status = status_key
                print(
                    f"[idea] status={tracked.get('status')} team={tracked.get('teamName')} "
                    f"progress={tracked.get('progress')} bug={tracked.get('currentBug')}"
                )
                if int(tracked.get("progress") or 0) >= 100:
                    print(f"[page] final artifact is now visible at {base}/idea/{idea_id}")
                if tracked.get("gossip"):
                    print(f"[gossip] {tracked['gossip'][0]}")

            project_id = tracked.get("projectId")
            if project_id:
                project = get_json(base, f"/api/project/{project_id}")
                for task in project.get("tasks", []):
                    task_key = (task.get("status"), task.get("output"))
                    if seen_tasks.get(task["id"]) != task_key:
                        seen_tasks[task["id"]] = task_key
                        print(
                            f"[task] #{task['id']} {task.get('title')} "
                            f"owner={task.get('ownerAgentId')} status={task.get('status')}"
                        )
                        if task.get("output"):
                            print(f"       {task['output']}")
                for commit in project.get("commits", []):
                    if commit["id"] not in seen_commits:
                        seen_commits.add(commit["id"])
                        print(
                            f"[commit] #{commit['id']} {commit.get('agentId')} "
                            f"{commit.get('message')}: {commit.get('diffSummary')}"
                        )
                artifact = project.get("artifact")
                if artifact and artifact.get("version") != seen_artifact_version:
                    seen_artifact_version = artifact.get("version")
                    print(
                        f"[artifact] v{artifact.get('version')} {artifact.get('title')} "
                        f"{artifact.get('url')}"
                    )
            sys.stdout.flush()
            time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\n[watch] stopped")
            return
        except (error.URLError, TimeoutError, OSError, ValueError, KeyError) as exc:
            print(f"[warn] {exc}", file=sys.stderr)
            time.sleep(args.interval)


def print_event(event, idea_id, idea_text):
    event_type = event.get("type")
    if event.get("ideaId") not in (None, idea_id):
        return
    if event_type == "idea_received" and event.get("ideaId") == idea_id:
        print(f"[event] idea_received receipt={event.get('receiptNo')} text={event.get('idea')}")
    elif event_type == "idea_claimed" and event.get("ideaId") == idea_id:
        artifact = event.get("artifact") or {}
        print(
            f"[event] idea_claimed team={event.get('teamName')} "
            f"form={event.get('currentForm')} artifact={artifact.get('url')}"
        )
    elif event_type == "project_update" and event.get("ideaId") == idea_id:
        artifact = event.get("artifact") or {}
        print(
            f"[event] project_update progress={event.get('progress')} "
            f"bug={event.get('currentBug')} artifact=v{artifact.get('version')}"
        )
    elif event_type == "conversation":
        text = event.get("text", "")
        if text and idea_text and idea_text in json.dumps(event, ensure_ascii=False):
            print(f"[a2a] {event.get('location')}: {text}")
            for line in event.get("lines", []):
                print(f"      {line.get('speakerId')}: {line.get('text')}")
    elif event_type == "gossip" and event.get("idea") == idea_text:
        print(f"[gossip-event] {event.get('text')}")
    elif event_type in {"pitch", "awards"} and event.get("ideaId") == idea_id:
        print(f"[event] {event_type}: {event}")


def get_json(base, path):
    with request.urlopen(f"{base}{path}", timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def latest_event_id(base):
    world = get_json(base, "/api/world?after=0")
    events = world.get("events", [])
    if not events:
        return 0
    return max(int(event.get("id", 0)) for event in events)


def post_json(base, path, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = request.Request(
        f"{base}{path}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"POST {path} failed: HTTP {exc.code} {detail}")


if __name__ == "__main__":
    main()
