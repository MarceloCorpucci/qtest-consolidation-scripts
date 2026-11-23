"""Local SQLite state: run history and source -> target ID mappings.

One file (``data/qmig.sqlite``), created automatically, no server, identical
on Windows and macOS.  The mappings make ``apply`` idempotent: anything already
migrated for the same instance/project pair is skipped on a re-run.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY, mode TEXT NOT NULL, status TEXT NOT NULL,
    source_instance TEXT, source_project INTEGER, target_instance TEXT, target_project INTEGER,
    plan_run_id TEXT, started_at TEXT, finished_at TEXT, summary TEXT
);
CREATE TABLE IF NOT EXISTS folder_map (
    source_instance TEXT, source_project INTEGER, source_id INTEGER,
    target_instance TEXT, target_project INTEGER, target_id INTEGER,
    path TEXT, run_id TEXT, created_at TEXT,
    PRIMARY KEY (source_instance, source_project, source_id, target_instance, target_project)
);
CREATE TABLE IF NOT EXISTS testcase_map (
    source_instance TEXT, source_project INTEGER, source_id INTEGER, source_pid TEXT,
    target_instance TEXT, target_project INTEGER, target_id INTEGER, target_pid TEXT,
    name TEXT, run_id TEXT, created_at TEXT,
    PRIMARY KEY (source_instance, source_project, source_id, target_instance, target_project)
);
CREATE TABLE IF NOT EXISTS attachment_map (
    source_instance TEXT, source_project INTEGER, source_attachment_id INTEGER,
    target_instance TEXT, target_project INTEGER, target_attachment_id INTEGER,
    object_type TEXT, source_object_id INTEGER, target_object_id INTEGER,
    name TEXT, size INTEGER, run_id TEXT, created_at TEXT,
    PRIMARY KEY (source_instance, source_project, source_attachment_id, target_instance, target_project)
);
CREATE TABLE IF NOT EXISTS approval_map (
    target_instance TEXT, target_project INTEGER, target_id INTEGER, run_id TEXT, created_at TEXT,
    PRIMARY KEY (target_instance, target_project, target_id)
);
CREATE TABLE IF NOT EXISTS release_map (
    source_instance TEXT, source_project INTEGER, source_id INTEGER,
    target_instance TEXT, target_project INTEGER, target_id INTEGER,
    name TEXT, run_id TEXT, created_at TEXT,
    PRIMARY KEY (source_instance, source_project, source_id, target_instance, target_project)
);
CREATE TABLE IF NOT EXISTS build_map (
    source_instance TEXT, source_project INTEGER, source_id INTEGER,
    target_instance TEXT, target_project INTEGER, target_id INTEGER,
    release_target_id INTEGER, name TEXT, run_id TEXT, created_at TEXT,
    PRIMARY KEY (source_instance, source_project, source_id, target_instance, target_project)
);
CREATE TABLE IF NOT EXISTS cycle_map (
    source_instance TEXT, source_project INTEGER, kind TEXT, source_id INTEGER,
    target_instance TEXT, target_project INTEGER, target_id INTEGER,
    name TEXT, run_id TEXT, created_at TEXT,
    PRIMARY KEY (source_instance, source_project, kind, source_id, target_instance, target_project)
);
CREATE TABLE IF NOT EXISTS run_map (
    source_instance TEXT, source_project INTEGER, source_id INTEGER,
    target_instance TEXT, target_project INTEGER, target_id INTEGER,
    name TEXT, run_id TEXT, created_at TEXT,
    PRIMARY KEY (source_instance, source_project, source_id, target_instance, target_project)
);
CREATE TABLE IF NOT EXISTS log_map (
    source_instance TEXT, source_project INTEGER, source_id INTEGER,
    target_instance TEXT, target_project INTEGER, target_id INTEGER,
    run_target_id INTEGER, run_id TEXT, created_at TEXT,
    PRIMARY KEY (source_instance, source_project, source_id, target_instance, target_project)
);
CREATE TABLE IF NOT EXISTS requirement_link (
    source_instance TEXT, source_project INTEGER, source_testcase_id INTEGER, source_requirement_id INTEGER,
    source_requirement_pid TEXT, jira_key TEXT, requirement_name TEXT, run_id TEXT, created_at TEXT,
    PRIMARY KEY (source_instance, source_project, source_testcase_id, source_requirement_id)
);
CREATE TABLE IF NOT EXISTS requirement_link_map (
    source_instance TEXT, source_project INTEGER, source_testcase_id INTEGER, source_requirement_id INTEGER,
    target_instance TEXT, target_project INTEGER, target_testcase_id INTEGER, target_requirement_id INTEGER,
    matched_by TEXT, run_id TEXT, created_at TEXT,
    PRIMARY KEY (source_instance, source_project, source_testcase_id, source_requirement_id,
                 target_instance, target_project)
);
CREATE TABLE IF NOT EXISTS intents (
    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, source_instance TEXT, source_project INTEGER, source_id INTEGER,
    target_instance TEXT, target_project INTEGER, parent_id INTEGER, name TEXT,
    status TEXT, target_id INTEGER, run_id TEXT, created_at TEXT, parent_type TEXT, extra TEXT
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class StateDb:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        columns = {r[1] for r in self.conn.execute("PRAGMA table_info(intents)")}
        for column in ("parent_type", "extra"):  # a database created by an earlier version
            if column not in columns:
                self.conn.execute(f"ALTER TABLE intents ADD COLUMN {column} TEXT")

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------ runs
    def start_run(self, run_id: str, mode: str, src: str, sp: int, tgt: str, tp: int,
                  plan_run_id: str | None = None) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, mode, "running", src, sp, tgt, tp, plan_run_id, now(), None, None))
        self.conn.commit()

    def finish_run(self, run_id: str, status: str, summary: str = "") -> None:
        self.conn.execute("UPDATE runs SET status=?, finished_at=?, summary=? WHERE run_id=?",
                          (status, now(), summary, run_id))
        self.conn.commit()

    def list_runs(self, limit: int = 20) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)))

    # -------------------------------------------------------------- mappings
    # ------------------------------------------------------------- write intents
    # An intent is saved BEFORE a write and closed after the answer.  An intent that stays "open" means the answer was
    # lost, so the object may exist in the target without being in the mapping; apply reconciles it on the next run.
    def open_intent(self, kind: str, src: str, sp: int, sid: int, tgt: str, tp: int, parent_id: int | None,
                    name: str, run_id: str, parent_type: str | None = None, extra: str | None = None) -> int:
        cur = self.conn.execute("INSERT INTO intents (kind, source_instance, source_project, source_id, target_instance, "
                                "target_project, parent_id, name, status, run_id, created_at, parent_type, extra) "
                                "VALUES (?,?,?,?,?,?,?,?,'open',?,?,?,?)",
                                (kind, src, sp, sid, tgt, tp, parent_id, name, run_id, now(), parent_type, extra))
        self.conn.commit()
        return cur.lastrowid

    def close_intent(self, intent_id: int, status: str, target_id: int | None = None) -> None:
        self.conn.execute("UPDATE intents SET status=?, target_id=? WHERE id=?", (status, target_id, intent_id))
        self.conn.commit()

    def intent(self, intent_id: int) -> sqlite3.Row:
        return self.conn.execute("SELECT * FROM intents WHERE id=?", (intent_id,)).fetchone()

    def pending_intents(self, kind: str, src: str, sp: int, sid: int, tgt: str, tp: int) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM intents WHERE kind=? AND source_instance=? AND source_project=? AND source_id=? AND "
            "target_instance=? AND target_project=? AND status IN ('open','unresolved') ORDER BY id",
            (kind, src, sp, sid, tgt, tp)))

    def intents(self, tgt: str, tp: int, status: str, kind: str | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM intents WHERE target_instance=? AND target_project=? AND status=?"
        args: list = [tgt, tp, status]
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        return list(self.conn.execute(sql + " ORDER BY id", args))

    def folder_target(self, src: str, sp: int, sid: int, tgt: str, tp: int) -> int | None:
        row = self.conn.execute(
            "SELECT target_id FROM folder_map WHERE source_instance=? AND source_project=? AND source_id=? "
            "AND target_instance=? AND target_project=?", (src, sp, sid, tgt, tp)).fetchone()
        return row["target_id"] if row else None

    def put_folder(self, src, sp, sid, tgt, tp, tid, path, run_id) -> None:
        self.conn.execute("INSERT OR REPLACE INTO folder_map VALUES (?,?,?,?,?,?,?,?,?)",
                          (src, sp, sid, tgt, tp, tid, path, run_id, now()))
        self.conn.commit()

    def testcase_target(self, src: str, sp: int, sid: int, tgt: str, tp: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM testcase_map WHERE source_instance=? AND source_project=? AND source_id=? "
            "AND target_instance=? AND target_project=?", (src, sp, sid, tgt, tp)).fetchone()

    def put_testcase(self, src, sp, sid, spid, tgt, tp, tid, tpid, name, run_id) -> None:
        self.conn.execute("INSERT OR REPLACE INTO testcase_map VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                          (src, sp, sid, spid, tgt, tp, tid, tpid, name, run_id, now()))
        self.conn.commit()

    def attachment_target(self, src: str, sp: int, att_id: int, tgt: str, tp: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM attachment_map WHERE source_instance=? AND source_project=? AND source_attachment_id=? "
            "AND target_instance=? AND target_project=?", (src, sp, att_id, tgt, tp)).fetchone()

    def attachment_on(self, src: str, sp: int, att_id: int, tgt: str, tp: int, object_type: str, target_object_id: int):
        """The mapping of a file only if it points at this very target object (a recreated object has a new id)."""
        row = self.attachment_target(src, sp, att_id, tgt, tp)
        if row is not None and row["object_type"] == object_type and row["target_object_id"] == target_object_id:
            return row
        return None

    def put_attachment(self, src, sp, att_id, tgt, tp, target_att_id, object_type, source_object_id,
                       target_object_id, name, size, run_id) -> None:
        self.conn.execute("INSERT OR REPLACE INTO attachment_map VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (src, sp, att_id, tgt, tp, target_att_id, object_type, source_object_id,
                           target_object_id, name, size, run_id, now()))
        self.conn.commit()

    def release_target(self, src: str, sp: int, sid: int, tgt: str, tp: int) -> int | None:
        row = self.conn.execute(
            "SELECT target_id FROM release_map WHERE source_instance=? AND source_project=? AND source_id=? "
            "AND target_instance=? AND target_project=?", (src, sp, sid, tgt, tp)).fetchone()
        return row["target_id"] if row else None

    def put_release(self, src, sp, sid, tgt, tp, tid, name, run_id) -> None:
        self.conn.execute("INSERT OR REPLACE INTO release_map VALUES (?,?,?,?,?,?,?,?,?)",
                          (src, sp, sid, tgt, tp, tid, name, run_id, now()))
        self.conn.commit()

    def build_target(self, src: str, sp: int, sid: int, tgt: str, tp: int) -> int | None:
        row = self.conn.execute(
            "SELECT target_id FROM build_map WHERE source_instance=? AND source_project=? AND source_id=? "
            "AND target_instance=? AND target_project=?", (src, sp, sid, tgt, tp)).fetchone()
        return row["target_id"] if row else None

    def put_build(self, src, sp, sid, tgt, tp, tid, release_tid, name, run_id) -> None:
        self.conn.execute("INSERT OR REPLACE INTO build_map VALUES (?,?,?,?,?,?,?,?,?,?)",
                          (src, sp, sid, tgt, tp, tid, release_tid, name, run_id, now()))
        self.conn.commit()

    def cycle_target(self, src: str, sp: int, kind: str, sid: int, tgt: str, tp: int) -> int | None:
        """Target id of a migrated test cycle (``kind`` 'cycle') or test suite (``kind`` 'suite')."""
        row = self.conn.execute(
            "SELECT target_id FROM cycle_map WHERE source_instance=? AND source_project=? AND kind=? AND source_id=? "
            "AND target_instance=? AND target_project=?", (src, sp, kind, sid, tgt, tp)).fetchone()
        return row["target_id"] if row else None

    def put_cycle(self, src, sp, kind, sid, tgt, tp, tid, name, run_id) -> None:
        self.conn.execute("INSERT OR REPLACE INTO cycle_map VALUES (?,?,?,?,?,?,?,?,?,?)",
                          (src, sp, kind, sid, tgt, tp, tid, name, run_id, now()))
        self.conn.commit()

    def run_target(self, src: str, sp: int, sid: int, tgt: str, tp: int) -> int | None:
        """Target id of a migrated test run."""
        row = self.conn.execute(
            "SELECT target_id FROM run_map WHERE source_instance=? AND source_project=? AND source_id=? "
            "AND target_instance=? AND target_project=?", (src, sp, sid, tgt, tp)).fetchone()
        return row["target_id"] if row else None

    def put_run(self, src, sp, sid, tgt, tp, tid, name, run_id) -> None:
        self.conn.execute("INSERT OR REPLACE INTO run_map VALUES (?,?,?,?,?,?,?,?,?)",
                          (src, sp, sid, tgt, tp, tid, name, run_id, now()))
        self.conn.commit()

    def log_target(self, src: str, sp: int, sid: int, tgt: str, tp: int) -> int | None:
        """Target id of a migrated test log."""
        row = self.conn.execute(
            "SELECT target_id FROM log_map WHERE source_instance=? AND source_project=? AND source_id=? "
            "AND target_instance=? AND target_project=?", (src, sp, sid, tgt, tp)).fetchone()
        return row["target_id"] if row else None

    def put_log(self, src, sp, sid, tgt, tp, tid, run_target_id, run_id) -> None:
        self.conn.execute("INSERT OR REPLACE INTO log_map VALUES (?,?,?,?,?,?,?,?,?)",
                          (src, sp, sid, tgt, tp, tid, run_target_id, run_id, now()))
        self.conn.commit()

    # ------------------------------------------------------- requirement links
    def mapped_testcases(self, src: str, sp: int, tgt: str, tp: int) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM testcase_map WHERE source_instance=? AND source_project=? AND target_instance=? "
            "AND target_project=? ORDER BY source_id", (src, sp, tgt, tp)))

    def replace_requirement_links(self, src: str, sp: int, testcase_id: int, links: list[dict], run_id: str) -> None:
        """Store the requirements a source test case covers (replaces what an earlier capture stored)."""
        self.conn.execute("DELETE FROM requirement_link WHERE source_instance=? AND source_project=? "
                          "AND source_testcase_id=?", (src, sp, testcase_id))
        for link in links:
            self.conn.execute("INSERT OR REPLACE INTO requirement_link VALUES (?,?,?,?,?,?,?,?,?)",
                              (src, sp, testcase_id, link["requirement_id"], link.get("pid"), link.get("jira_key"),
                               link.get("name"), run_id, now()))
        self.conn.commit()

    def requirement_links(self, src: str, sp: int) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM requirement_link WHERE source_instance=? AND source_project=? "
            "ORDER BY source_testcase_id, source_requirement_id", (src, sp)))

    def put_requirement_link_map(self, src, sp, tc_id, req_id, tgt, tp, target_tc_id, target_req_id, matched_by,
                                 run_id) -> None:
        self.conn.execute("INSERT OR REPLACE INTO requirement_link_map VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                          (src, sp, tc_id, req_id, tgt, tp, target_tc_id, target_req_id, matched_by, run_id, now()))
        self.conn.commit()

    def requirement_link_done(self, src: str, sp: int, tc_id: int, req_id: int, tgt: str, tp: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM requirement_link_map WHERE source_instance=? AND source_project=? AND source_testcase_id=? "
            "AND source_requirement_id=? AND target_instance=? AND target_project=?",
            (src, sp, tc_id, req_id, tgt, tp)).fetchone()

    def is_approved(self, tgt: str, tp: int, target_id: int) -> bool:
        return self.conn.execute("SELECT 1 FROM approval_map WHERE target_instance=? AND target_project=? AND target_id=?",
                                 (tgt, tp, target_id)).fetchone() is not None

    def put_approval(self, tgt: str, tp: int, target_id: int, run_id: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO approval_map VALUES (?,?,?,?,?)", (tgt, tp, target_id, run_id, now()))
        self.conn.commit()

    def forget_target_object(self, kind: str, tgt: str, tp: int, target_id: int) -> None:
        """Drop the mapping of a release, build, cycle, suite or run that no longer exists in the target.
        Deleting a release or cycle also deletes what was below it, so those mappings go too."""
        c = self.conn
        if kind == "release":
            c.execute("DELETE FROM release_map WHERE target_instance=? AND target_project=? AND target_id=?", (tgt, tp, target_id))
            c.execute("DELETE FROM build_map WHERE target_instance=? AND target_project=? AND release_target_id=?", (tgt, tp, target_id))
        elif kind in ("cycle", "suite"):
            c.execute("DELETE FROM cycle_map WHERE target_instance=? AND target_project=? AND kind=? AND target_id=?",
                      (tgt, tp, kind, target_id))
        elif kind == "run":
            c.execute("DELETE FROM run_map WHERE target_instance=? AND target_project=? AND target_id=?", (tgt, tp, target_id))
            c.execute("DELETE FROM log_map WHERE target_instance=? AND target_project=? AND run_target_id=?", (tgt, tp, target_id))
        elif kind == "log":
            c.execute("DELETE FROM log_map WHERE target_instance=? AND target_project=? AND target_id=?", (tgt, tp, target_id))
        c.commit()

    def forget_target_testcase(self, tgt: str, tp: int, target_id: int) -> None:
        """Drop what is recorded about a target test case that no longer exists (deleted in the target)."""
        self.conn.execute("DELETE FROM attachment_map WHERE target_instance=? AND target_project=? AND object_type='test-cases' "
                          "AND target_object_id=?", (tgt, tp, target_id))
        self.conn.execute("DELETE FROM approval_map WHERE target_instance=? AND target_project=? AND target_id=?",
                          (tgt, tp, target_id))
        self.conn.commit()
