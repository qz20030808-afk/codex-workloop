"""Evidence-backed task state and closed-period reports for a local Codex automation."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")


def parse_time(value: str) -> datetime:
    stamp = datetime.fromisoformat(value)
    if stamp.utcoffset() is None:
        raise ValueError("Timestamp needs a time zone offset.")
    return stamp.astimezone(timezone.utc)


def midnight(day):
    return datetime.combine(day, time(), SHANGHAI).astimezone(timezone.utc)


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def publish_file(path: Path, body: bytes) -> None:
    """Materialize a committed report; leave an edited public copy alone."""
    if path.parent.is_symlink() or path.is_symlink():
        raise ValueError("Report output cannot be a symbolic link.")
    if path.exists():
        if path.read_bytes() != body:
            raise ValueError(f"Report differs from the committed version: {path.name}.")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent) as temp:
        temp.write(body)
        temp.flush()
        try:
            os.link(temp.name, path)
        except FileExistsError:
            if path.read_bytes() != body:
                raise ValueError(f"Report differs from the committed version: {path.name}.")


class Workloop:
    """One task per state directory. SQLite serializes state and report commits."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        database = self.root / "workloop.sqlite3"
        if database.is_symlink():
            raise ValueError("The state database cannot be a symbolic link.")
        self.db = sqlite3.connect(database, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY, at TEXT NOT NULL, kind TEXT NOT NULL,
                data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reports (
                key TEXT PRIMARY KEY, closes_at TEXT NOT NULL,
                body TEXT NOT NULL, sha256 TEXT NOT NULL
            );
        """)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.db.close()

    @contextmanager
    def transaction(self):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            yield

    def events(self, before: datetime | None = None) -> list[dict]:
        if before is None:
            rows = self.db.execute("SELECT * FROM events ORDER BY seq")
        else:
            rows = self.db.execute(
                "SELECT * FROM events WHERE at < ? ORDER BY seq", (before.isoformat(),)
            )
        return [dict(row) | {"data": json.loads(row["data"])} for row in rows]

    def append(self, at: datetime, kind: str, data: dict) -> None:
        stamp = parse_time(at.isoformat()).isoformat()
        last_event = self.db.execute("SELECT max(at) FROM events").fetchone()[0]
        last_report = self.db.execute("SELECT max(closes_at) FROM reports").fetchone()[0]
        if (last_event and stamp < last_event) or (last_report and stamp < last_report):
            raise ValueError("Event falls before the latest event or a committed report.")
        self.db.execute(
            "INSERT INTO events(at, kind, data) VALUES (?, ?, ?)",
            (stamp, kind, json.dumps(data, ensure_ascii=False, sort_keys=True)),
        )

    def start(self, goal: str, requirements: dict[str, str], at: datetime) -> None:
        if not goal.strip() or not isinstance(requirements, dict) or not requirements or not all(
            isinstance(key, str) and isinstance(value, str) and key.strip() and value.strip()
            for key, value in requirements.items()
        ):
            raise ValueError("A goal and named acceptance requirements are required.")
        with self.transaction():
            if self.events():
                raise ValueError("This directory already contains a task.")
            self.append(at, "started", {"goal": goal, "requirements": requirements})

    @staticmethod
    def snapshot(events: list[dict]) -> dict:
        if not events:
            raise ValueError("No task has been started.")
        original = events[0]["data"]
        evidence = {}
        receipt = None
        for event in events:
            if event["kind"] == "accepted":
                evidence[event["data"]["requirement"]] = event["data"]
            elif event["kind"] == "completed":
                receipt = event["data"]
        return original | {
            "evidence": evidence,
            "pending": [key for key in original["requirements"] if key not in evidence],
            "receipt": receipt,
        }

    def artifact(self, name: str) -> Path:
        base = (self.root / "artifacts")
        if base.is_symlink() or Path(name).is_absolute():
            raise ValueError("Artifacts must be stored under the state directory.")
        path = (base / name).resolve()
        if not path.is_relative_to(base) or not path.is_file():
            raise ValueError(f"Artifact missing or outside the artifacts directory: {name}.")
        return path

    def accept(self, requirement: str, name: str, note: str, at: datetime) -> dict:
        """Record a verified work artifact, with its acceptance note and digest."""
        if not note.strip():
            raise ValueError("Acceptance needs an explanation of the check performed.")
        with self.transaction():
            state = self.snapshot(self.events())
            if state["receipt"] or requirement not in state["requirements"]:
                raise ValueError("Task is complete or the requirement is unknown.")
            artifact = self.artifact(name)
            item = {
                "requirement": requirement, "path": artifact.relative_to(self.root).as_posix(),
                "sha256": digest(artifact.read_bytes()), "check": note,
            }
            if state["evidence"].get(requirement) != item:
                self.append(at, "accepted", item)
        return item

    def complete(self, at: datetime) -> dict:
        with self.transaction():
            state = self.snapshot(self.events())
            if state["pending"]:
                raise ValueError("Unmet acceptance requirements: " + ", ".join(state["pending"]))
            for item in state["evidence"].values():
                actual = self.artifact(item["path"].removeprefix("artifacts/"))
                if digest(actual.read_bytes()) != item["sha256"]:
                    raise ValueError(f"Accepted artifact changed: {item['path']}.")
            if state["receipt"]:
                return state["receipt"]
            receipt = {
                "goal": state["goal"], "completed_at": parse_time(at.isoformat()).isoformat(),
                "evidence": list(state["evidence"].values()),
            }
            receipt["sha256"] = digest(json.dumps(receipt, sort_keys=True).encode())
            self.append(at, "completed", receipt)
        return receipt

    def report_body(self, key: str, start: datetime, end: datetime) -> str:
        events = self.events(before=end)
        state = self.snapshot(events)
        lines = [
            f"# {key}", "", f"Period: {start.astimezone(SHANGHAI).date()} through "
            f"{(end.astimezone(SHANGHAI).date() - timedelta(days=1))} (Asia/Shanghai)",
            "", f"Goal: {state['goal']}", "", "## Work in this period",
        ]
        activity = [event for event in events if event["at"] >= start.isoformat()]
        for event in activity:
            item = event["data"]
            if event["kind"] == "accepted":
                lines.append(f"- Accepted {item['requirement']}: {item['check']}")
            elif event["kind"] == "resumed":
                lines.append(f"- Resumed from {item['from_report']}.")
            elif event["kind"] == "completed":
                lines.append(f"- Completed with receipt `{item['sha256']}`.")
            else:
                lines.append("- Task opened.")
        if not activity:
            lines.append("- No new task events.")
        lines.extend(["", "## Accepted evidence as of close"])
        for key, item in state["evidence"].items():
            lines.append(f"- {key}: `{item['path']}` · SHA-256 `{item['sha256']}`")
        if not state["evidence"]:
            lines.append("- None yet.")
        lines.extend(["", "## Next actions"])
        lines.extend(f"- {key}: {state['requirements'][key]}" for key in state["pending"])
        if not state["pending"]:
            lines.append("- Task completed." if state["receipt"] else "- Review and issue completion receipt.")
        return "\n".join(lines) + "\n"

    def report(self, at: datetime) -> list[dict]:
        """Catch up daily reports and closed Monday–Sunday weeks."""
        with self.transaction():
            events = self.events()
            if not events:
                return []
            first = parse_time(events[0]["at"]).astimezone(SHANGHAI).date()
            last = parse_time(at.isoformat()).astimezone(SHANGHAI).date() - timedelta(days=1)
            old_keys = {row[0] for row in self.db.execute("SELECT key FROM reports")}
            day = first
            while day <= last:
                end = midnight(day + timedelta(days=1))
                periods = [(f"daily-{day}", day)]
                if day.weekday() == 6:
                    periods.append((f"weekly-{day}", day - timedelta(days=6)))
                for key, since in periods:
                    if key not in old_keys:
                        body = self.report_body(key, midnight(since), end)
                        self.db.execute(
                            "INSERT INTO reports VALUES (?, ?, ?, ?)",
                            (key, end.isoformat(), body, digest(body.encode())),
                        )
                day += timedelta(days=1)
            rows = self.db.execute(
                "SELECT * FROM reports WHERE closes_at <= ? ORDER BY closes_at, key",
                (parse_time(at.isoformat()).isoformat(),),
            ).fetchall()
        for row in rows:
            publish_file(self.root / "reports" / (row["key"] + ".md"), row["body"].encode())
        return [{"key": row["key"], "sha256": row["sha256"], "new": row["key"] not in old_keys}
                for row in rows]

    def resume(self, at: datetime) -> dict:
        """Return the latest closed-day report and the still-pending work."""
        at = parse_time(at.isoformat())
        with self.transaction():
            state = self.snapshot(self.events())
            if state["receipt"]:
                raise ValueError("Task already completed.")
            row = self.db.execute(
                "SELECT * FROM reports WHERE key LIKE 'daily-%' AND closes_at <= ? "
                "ORDER BY closes_at DESC LIMIT 1", (at.isoformat(),)
            ).fetchone()
            if row is None:
                raise ValueError("Generate a closed-day report before resuming.")
            path = self.root / "reports" / (row["key"] + ".md")
            if not path.is_file() or digest(path.read_bytes()) != row["sha256"]:
                raise ValueError("Resume requires a matching report file.")
            handoff = {
                "from_report": row["key"], "report_sha256": row["sha256"],
                "pending": state["pending"],
            }
            last_event = self.events()[-1]
            if last_event["kind"] != "resumed" or last_event["data"] != handoff:
                self.append(at, "resumed", handoff)
        return handoff


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--state-dir", type=Path, required=True)
    cli.add_argument("--at", type=parse_time, help="ISO timestamp with offset; defaults to now.")
    commands = cli.add_subparsers(dest="action", required=True)
    start = commands.add_parser("start")
    start.add_argument("goal")
    start.add_argument("requirements_json", type=json.loads)
    accept = commands.add_parser("accept")
    accept.add_argument("requirement")
    accept.add_argument("relative_artifact")
    accept.add_argument("check")
    for name in ("status", "report", "resume", "complete"):
        commands.add_parser(name)
    args = cli.parse_args()
    at = args.at or datetime.now(timezone.utc)
    try:
        with Workloop(args.state_dir) as loop:
            if args.action == "start":
                loop.start(args.goal, args.requirements_json, at)
                result = loop.snapshot(loop.events())
            elif args.action == "accept":
                result = loop.accept(args.requirement, args.relative_artifact, args.check, at)
            elif args.action == "status":
                result = loop.snapshot(loop.events())
            else:
                result = getattr(loop, args.action)(at)
    except (ValueError, OSError, sqlite3.Error) as error:
        cli.exit(2, f"{error}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
