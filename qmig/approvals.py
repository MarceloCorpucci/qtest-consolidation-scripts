"""Approving test cases in the target (``PUT .../test-cases/{id}/approve``).

qTest only lets a test case be linked to a requirement once it is approved, so approval is part of the
migration.  A test case is *approved* when its version is a whole number (``1.0``, ``2.0``); a version such as
``0.1`` or ``1.1`` is a draft.  Policies: ``source`` approves the target test cases whose source test case is
approved in the source, ``all`` approves every migrated test case.  Only test cases that qmig created (or
created in an earlier run) are touched.  Approving twice is harmless and recorded in SQLite.
"""
from __future__ import annotations

from .client import ApiError, QtestClient
from .db import StateDb
from .journal import repeat_if_unsure
from .runlog import RunContext

POLICIES = ("source", "all")
ELIGIBLE = ("created", "skip-mapped")


def is_approved_version(version) -> bool:
    """``1.0`` is approved, ``0.1`` and ``1.1`` are drafts."""
    parts = str(version or "").strip().split(".")
    try:
        return len(parts) == 2 and int(parts[0]) >= 1 and int(parts[1]) == 0
    except ValueError:
        return False


def wants_approval(policy: str | None, source_version) -> bool:
    return policy == "all" or (policy == "source" and is_approved_version(source_version))


def apply_approvals(ctx: RunContext, tgt: QtestClient, db: StateDb, tp: int, rows: list[dict], aborted: str) -> tuple[list[dict], str]:
    todo = [r for r in rows if r.get("approve")]
    ctx.log.info("Approving %d test case(s)", len(todo))
    results: list[dict] = []
    for row in todo:
        entry = {"n": row["n"], "name": row["name"], "source_id": row["source_id"], "target_id": row.get("target_id"),
                 "source_version": row.get("source_version"), "status": "", "error": ""}
        if row["status"] not in ELIGIBLE or not row.get("target_id"):
            entry.update(status="skipped", error="test case was not created")
        elif aborted:
            entry.update(status="not-attempted", error=aborted)
        elif db.is_approved(tgt.name, tp, row["target_id"]):
            entry["status"] = "skip-mapped"
        else:
            try:
                repeat_if_unsure(lambda: tgt.approve_testcase(tp, row["target_id"]))
                db.put_approval(tgt.name, tp, row["target_id"], ctx.run_id)
                entry["status"] = "approved"
            except ApiError as exc:
                if "already" in str(exc.detail).lower() and "approv" in str(exc.detail).lower():
                    db.put_approval(tgt.name, tp, row["target_id"], ctx.run_id)
                    entry["status"] = "skip-mapped"
                else:
                    entry.update(status="failed", error=f"HTTP {exc.status}: {exc.detail}" if exc.status else str(exc.detail))
                    ctx.log.error("approve FAILED  %s: %s", row["name"], entry["error"])
                    if exc.status in (401, 403):
                        aborted = f"aborted after authorization failure ({entry['error']})"
                        ctx.log.error("Stopping approvals: %s", aborted)
                    elif "linked requirement" in str(exc.detail).lower():
                        aborted = ("aborted: this project does not allow approving a test case without a linked requirement "
                                   "(Test Case settings); link requirements first with `qmig links`, or ask a project admin "
                                   "to change the setting")
                        ctx.log.error("Stopping approvals: %s", aborted)
        ctx.event("approve", entry["status"], source_id=row["source_id"], target_id=entry["target_id"], error=entry["error"])
        results.append(entry)
    return results, aborted


def verify_approvals(ctx: RunContext, tgt: QtestClient, tp: int, entries: list[dict]) -> dict:
    """Each approved test case must now have a whole-number version in the target."""
    report = {"checked": 0, "ok": 0, "not_approved": [], "errors": []}
    for entry in entries:
        if entry["status"] not in ("approved", "skip-mapped"):
            continue
        report["checked"] += 1
        try:
            version = tgt.request("GET", f"/projects/{tp}/test-cases/{entry['target_id']}")[0].get("version")
        except ApiError as exc:
            report["errors"].append(f"test case {entry['target_id']}: {exc}")
            continue
        if is_approved_version(version):
            report["ok"] += 1
        else:
            report["not_approved"].append({"name": entry["name"], "target_id": entry["target_id"], "version": version})
    ctx.log.info("Approvals verified: %d checked, %d ok, %d not approved", report["checked"], report["ok"],
                 len(report["not_approved"]))
    return report
