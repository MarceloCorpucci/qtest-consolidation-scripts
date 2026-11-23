"""Files attached to test steps: describe (plan), copy (apply) and check (verify).

A step file is downloaded from ``/test-steps/{id}/attachments/{id}`` of the source (read-only) and uploaded to the
step of the new test case that has the same position.  Steps are matched by order, as for step results, so only test
cases created by qmig (or mapped by an earlier run) receive them.  A file already in ``attachment_map`` is never
uploaded twice, so a re-run only sends what is still missing.
"""
from __future__ import annotations

from collections import Counter

from .attachments import copy_files, describe
from .client import ApiError, QtestClient
from .db import StateDb
from .runlog import RunContext

OBJECT_TYPE = "test-steps"
MAX_CONSECUTIVE_FAILURES = 25
ELIGIBLE = ("created", "skip-mapped")


def kept_steps(steps) -> list[dict]:
    """Source steps in the order they are created in the target: by order, steps without text left out."""
    rows = [s for s in (steps or []) if isinstance(s, dict)]
    rows.sort(key=lambda s: (s.get("order") is None, s.get("order") or 0))
    return [s for s in rows if (s.get("description") or "") or (s.get("expected") or s.get("expected_result") or "")]


def plan_step_files(src: QtestClient, db: StateDb, sp: int, tgt_name: str, tp: int, source_id: int) -> tuple[list[dict], int]:
    """Steps of one source test case that carry files, with their position in the target, and how many such steps
    would be left out because they have no text."""
    steps = src.test_case_steps(sp, source_id)
    position = {id(s): i for i, s in enumerate(kept_steps(steps), 1)}
    out, dropped = [], 0
    for step in steps:
        if not step.get("attachments"):
            continue
        if id(step) not in position:
            dropped += 1
            continue
        out.append({"source_step_id": step["id"], "order": position[id(step)],
                    "files": describe(step["attachments"], db, src.name, sp, tgt_name, tp)})
    return out, dropped


def _entry(row: dict, step: dict, file: dict, status: str, error: str = "", target_step_id=None) -> dict:
    return {"test_case_n": row["n"], "test_case": row["name"], "source_test_case_id": row["source_id"],
            "target_test_case_id": row.get("target_id"), "order": step["order"], "source_step_id": step["source_step_id"],
            "target_step_id": target_step_id or "", "source_attachment_id": file["id"], "name": file["name"],
            "status": status, "error": error}


def apply_step_files(ctx: RunContext, src: QtestClient, tgt: QtestClient, db: StateDb, plan: dict,
                     test_case_rows: list[dict], aborted: str) -> tuple[list[dict], str]:
    sp, tp = plan["source"]["project_id"], plan["target"]["project_id"]
    total = sum(len(s["files"]) for r in test_case_rows for s in r.get("step_files") or [])
    ctx.log.info("Copying %d step file(s)", total)
    results: list[dict] = []
    consecutive = 0
    for row in test_case_rows:
        for step in row.get("step_files") or []:
            if row["status"] not in ELIGIBLE or not row.get("target_id"):
                results += [_entry(row, step, f, "skipped", "test case was not created") for f in step["files"]]
                continue
            if aborted:
                results += [_entry(row, step, f, "not-attempted", aborted) for f in step["files"]]
                continue
            # the step is looked up first: a mapping only counts when it points at this very step (a recreated test
            # case has new steps)
            target_step, error = None, ""
            try:
                target_step = next((s for s in tgt.test_case_steps(tp, row["target_id"]) if s.get("order") == step["order"]), None)
                if target_step is None:
                    error = f"target test case has no step {step['order']}"
            except ApiError as exc:
                error = f"cannot read steps of the target test case: {exc}"
            for f in step["files"]:
                if target_step is not None and db.attachment_on(src.name, sp, f["id"], tgt.name, tp, OBJECT_TYPE, target_step["id"]):
                    results.append(_entry(row, step, f, "skip-mapped", target_step_id=target_step["id"]))
                    continue
                if aborted:
                    results.append(_entry(row, step, f, "not-attempted", aborted))
                    continue
                if error:
                    results.append(_entry(row, step, f, "failed", error))
                    continue
                done = copy_files(ctx, src, tgt, db, sp, tp, OBJECT_TYPE, step["source_step_id"], target_step["id"], [f],
                                  step_ref=(row["target_id"], step["order"]))[0]
                results.append(_entry(row, step, f, done["status"], done["error"], target_step["id"]))
                if done["status"] == "failed":
                    consecutive += 1
                    if done["error"].startswith(("HTTP 401", "HTTP 403")):
                        aborted = f"aborted after authorization failure ({done['error']})"
                    elif consecutive >= MAX_CONSECUTIVE_FAILURES:
                        aborted = f"aborted after {MAX_CONSECUTIVE_FAILURES} consecutive step file failures"
                    if aborted:
                        ctx.log.error("Stopping step file upload: %s", aborted)
                else:
                    consecutive = 0
    for e in results:
        ctx.event("step-file", e["status"], source_id=e["source_attachment_id"], target_test_case_id=e["target_test_case_id"],
                  step=e["order"], name=e["name"], error=e["error"])
    return results, aborted


def verify_step_files(ctx: RunContext, tgt: QtestClient, tp: int, entries: list[dict]) -> dict:
    """Every step file created or mapped by apply must be on its step of the target test case."""
    wanted: dict[int, dict[int, Counter]] = {}
    for e in entries:
        if e["status"] in ELIGIBLE and e.get("target_test_case_id"):
            wanted.setdefault(e["target_test_case_id"], {}).setdefault(e["order"], Counter())[e["name"]] += 1
    report = {"checked": 0, "ok": 0, "missing": [], "errors": []}
    for test_case_id, by_order in wanted.items():
        try:
            steps = {s.get("order"): s for s in tgt.test_case_steps(tp, test_case_id)}
        except ApiError as exc:
            report["errors"].append(f"test case {test_case_id}: {exc}")
            continue
        for order, names in by_order.items():
            present = Counter(a.get("name") or "" for a in (steps.get(order) or {}).get("attachments") or [])
            for name, count in names.items():
                report["checked"] += count
                found = min(count, present.get(name, 0))
                report["ok"] += found
                if found < count:
                    report["missing"].append({"test_case_id": test_case_id, "step": order, "name": name,
                                              "expected": count, "present": present.get(name, 0)})
    ctx.log.info("Step files verified: %d checked, %d ok, %d missing", report["checked"], report["ok"], len(report["missing"]))
    return report
