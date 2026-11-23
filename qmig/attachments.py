"""Test case attachments: describe (plan), copy (apply) and check (verify).

Files are downloaded from the source (read-only client) and uploaded to the new test case in the
target.  Every copied file is recorded in the SQLite ``attachment_map`` so a re-run never uploads a
file twice.  Only test cases created by qmig (or mapped by an earlier run) receive attachments;
a test case that already existed in the target under the same name is left alone.
"""
from __future__ import annotations

import mimetypes
from collections import Counter

from .client import ApiError, QtestClient
from .db import StateDb
from .journal import journaled
from .runlog import RunContext

OBJECT_TYPE = "test-cases"
MAX_ATTACHMENT_BYTES = 100 * 1024 * 1024
MAX_CONSECUTIVE_FAILURES = 25
ELIGIBLE = ("created", "skip-mapped")


def _why(exc: ApiError) -> str:
    return f"HTTP {exc.status}: {exc.detail}" if exc.status else str(exc.detail)


def describe(items: list[dict], db: StateDb, src: str, sp: int, tgt: str, tp: int) -> list[dict]:
    out = []
    for item in items:
        if item.get("id") is None:
            continue
        out.append({"id": item["id"], "name": item.get("name") or f"attachment-{item['id']}",
                    "content_type": item.get("content_type") or "",
                    "done": db.attachment_target(src, sp, item["id"], tgt, tp) is not None})
    return out


def _entry(row: dict, att: dict, status: str, error: str = "", **extra) -> dict:
    return {"test_case_n": row["n"], "test_case": row["name"], "source_test_case_id": row["source_id"],
            "target_test_case_id": row.get("target_id"), "source_attachment_id": att["id"], "name": att["name"],
            "status": status, "error": error, "bytes": extra.get("bytes", ""),
            "target_attachment_id": extra.get("target_attachment_id", "")}


def apply_attachments(ctx: RunContext, src: QtestClient, tgt: QtestClient, db: StateDb, plan: dict,
                      test_case_rows: list[dict], aborted: str) -> tuple[list[dict], str]:
    sp, tp = plan["source"]["project_id"], plan["target"]["project_id"]
    total = sum(len(r.get("attachments") or []) for r in test_case_rows)
    ctx.log.info("Copying %d attachment(s)", total)
    results: list[dict] = []
    consecutive = 0
    for row in test_case_rows:
        for att in row.get("attachments") or []:
            if row["status"] not in ELIGIBLE or not row.get("target_id"):
                results.append(_entry(row, att, "skipped", "test case was not created"))
                continue
            if aborted:
                results.append(_entry(row, att, "not-attempted", aborted))
                continue
            mapped = db.attachment_on(src.name, sp, att["id"], tgt.name, tp, OBJECT_TYPE, row["target_id"])
            if mapped:
                results.append(_entry(row, att, "skip-mapped", target_attachment_id=mapped["target_attachment_id"]))
                continue
            try:
                content = src.download_attachment(sp, OBJECT_TYPE, row["source_id"], att["id"])
            except ApiError as exc:
                results.append(_entry(row, att, "failed", f"download from source failed: {exc}"))
                ctx.log.error("attachment FAILED (download) %s of '%s': %s", att["name"], row["name"], exc)
                continue
            if not content:
                results.append(_entry(row, att, "failed", "source returned an empty file"))
                ctx.log.error("attachment FAILED %s of '%s': source returned an empty file", att["name"], row["name"])
                continue
            if len(content) > MAX_ATTACHMENT_BYTES:
                results.append(_entry(row, att, "too-large", f"{len(content)} bytes exceeds {MAX_ATTACHMENT_BYTES}",
                                      bytes=len(content)))
                ctx.log.warning("attachment skipped (too large) %s of '%s'", att["name"], row["name"])
                continue
            content_type = att.get("content_type") or mimetypes.guess_type(att["name"])[0] or "application/octet-stream"
            try:
                created = journaled(ctx, tgt, db, "attachment", src.name, sp, att["id"], tgt.name, tp, row["target_id"], att["name"],
                                    lambda: tgt.upload_attachment(tp, OBJECT_TYPE, row["target_id"], att["name"], content_type, content),
                                    parent_type=OBJECT_TYPE)
            except ApiError as exc:
                results.append(_entry(row, att, "failed", f"upload to target failed: {_why(exc)}",
                                      bytes=len(content)))
                ctx.log.error("attachment FAILED (upload) %s of '%s': %s", att["name"], row["name"], exc)
                consecutive += 1
                if exc.status in (401, 403):
                    aborted = f"aborted after authorization failure ({exc})"
                elif consecutive >= MAX_CONSECUTIVE_FAILURES:
                    aborted = f"aborted after {MAX_CONSECUTIVE_FAILURES} consecutive attachment failures"
                if aborted:
                    ctx.log.error("Stopping attachment upload: %s", aborted)
                continue
            consecutive = 0
            target_att_id = created.get("id") if isinstance(created, dict) else None
            db.put_attachment(src.name, sp, att["id"], tgt.name, tp, target_att_id, OBJECT_TYPE, row["source_id"],
                              row["target_id"], att["name"], len(content), ctx.run_id)
            results.append(_entry(row, att, "created", bytes=len(content), target_attachment_id=target_att_id or ""))
            ctx.log.info("  attachment copied  %s (%d bytes) -> test case %s", att["name"], len(content),
                         row.get("target_pid") or row["target_id"])
    for entry in results:
        ctx.event("attachment", entry["status"], source_id=entry["source_attachment_id"],
                  target_test_case_id=entry["target_test_case_id"], name=entry["name"], error=entry["error"])
    return results, aborted


def verify_attachments(ctx: RunContext, tgt: QtestClient, tp: int, entries: list[dict]) -> dict:
    """Every attachment created or mapped by apply must be listed on its target test case."""
    wanted: dict[int, Counter] = {}
    for e in entries:
        if e["status"] in ("created", "skip-mapped") and e.get("target_test_case_id"):
            wanted.setdefault(e["target_test_case_id"], Counter())[e["name"]] += 1
    report = {"checked": 0, "ok": 0, "missing": [], "errors": []}
    for test_case_id, names in wanted.items():
        try:
            present = Counter((a.get("name") or "") for a in tgt.list_attachments(tp, OBJECT_TYPE, test_case_id))
        except ApiError as exc:
            report["errors"].append(f"test case {test_case_id}: {exc}")
            continue
        for name, count in names.items():
            report["checked"] += count
            found = min(count, present.get(name, 0))
            report["ok"] += found
            if found < count:
                report["missing"].append({"test_case_id": test_case_id, "name": name, "expected": count,
                                          "present": present.get(name, 0)})
    ctx.log.info("Attachments verified: %d checked, %d ok, %d missing", report["checked"], report["ok"],
                 len(report["missing"]))
    return report


def copy_files(ctx: RunContext, src: QtestClient, tgt: QtestClient, db: StateDb, sp: int, tp: int, object_type: str,
               source_id: int, target_id: int, files: list[dict], step_ref: tuple[int, int] | None = None) -> list[dict]:
    """Copy the listed files of one source object (a test run or test log) to its target object; each result has a status
    (created, skip-mapped, failed, too-large).  A file already in ``attachment_map`` is never uploaded twice."""
    out: list[dict] = []
    for att in files:
        entry = {"id": att["id"], "name": att["name"], "status": "", "error": ""}
        if db.attachment_on(src.name, sp, att["id"], tgt.name, tp, object_type, target_id):
            entry["status"] = "skip-mapped"
        else:
            try:
                content = src.download_attachment(sp, object_type, source_id, att["id"])
                if not content:
                    entry.update(status="failed", error="source returned an empty file")
                elif len(content) > MAX_ATTACHMENT_BYTES:
                    entry.update(status="too-large", error=f"{len(content)} bytes exceeds {MAX_ATTACHMENT_BYTES}")
                else:
                    ctype = att.get("content_type") or mimetypes.guess_type(att["name"])[0] or "application/octet-stream"
                    step = step_ref is not None  # a step file: (target test case id, step order) lets a cut-off upload be found again
                    made = journaled(ctx, tgt, db, "stepfile" if step else "attachment", src.name, sp, att["id"], tgt.name, tp,
                                     target_id, att["name"],
                                     lambda: tgt.upload_attachment(tp, object_type, target_id, att["name"], ctype, content),
                                     parent_type=object_type,
                                     extra={"target_test_case_id": step_ref[0], "order": step_ref[1]} if step else None)
                    db.put_attachment(src.name, sp, att["id"], tgt.name, tp, (made or {}).get("id"), object_type, source_id,
                                      target_id, att["name"], len(content), ctx.run_id)
                    entry["status"] = "created"
            except ApiError as exc:
                entry.update(status="failed", error=_why(exc))
        if entry["status"] in ("failed", "too-large"):
            ctx.log.error("%s file %s: %s", object_type, att["name"], entry["error"])
        out.append(entry)
    return out


def list_files(src: QtestClient, sp: int, object_type: str, object_id: int) -> list[dict]:
    return [{"id": a["id"], "name": a.get("name") or f"attachment-{a['id']}", "content_type": a.get("content_type") or ""}
            for a in src.list_attachments(sp, object_type, object_id) if a.get("id") is not None]


def verify_files(tgt: QtestClient, tp: int, object_type: str, object_id: int, results: list[dict]) -> tuple[list[str], str]:
    """Names of copied files that are missing on the target object, and an error text if it could not be read."""
    wanted = [a["name"] for a in results if a["status"] in ("created", "skip-mapped")]
    if not wanted:
        return [], ""
    try:
        have = Counter(a.get("name") or "" for a in tgt.list_attachments(tp, object_type, object_id))
    except ApiError as exc:
        return [], f"files of {object_type} {object_id}: {exc}"
    missing = []
    for name in wanted:
        if have.get(name, 0) == 0:
            missing.append(name)
    return missing, ""
