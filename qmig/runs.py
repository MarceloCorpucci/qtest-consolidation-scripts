"""Test runs: describe (plan), create (apply) and check (verify).

A run lives under a cycle or suite and points at a test case.  The target run points at the *migrated* test case
(looked up in the test case map when the run is created), so a run whose test case was not migrated - for example one
that sits in the Recycle Bin - is skipped and listed with its source test case id.  A run with the same name and test
case under the same target parent is reused.  Mappings are kept in SQLite, so a re-run never creates a run twice.
Execution history (test logs) is a later step.
"""
from __future__ import annotations

from collections import Counter

from .attachments import copy_files, list_files, verify_files
from .client import ApiError, QtestClient
from .cycles import PARENT_TYPE, gone, left_parent
from .journal import journaled
from .db import StateDb
from .runlog import RunContext

MAX_CONSECUTIVE_FAILURES = 10
OBJECT_TYPE = "test-runs"
NOT_MIGRATED = "test case was not migrated (not in the test case map; Recycle Bin test cases are never migrated)"
DELETED = "test case was migrated earlier but no longer exists in the target: migrate the test cases again first"


def _key(value) -> str:
    return str(value or "").strip().casefold()


def _tc_id(run: dict):
    return run.get("testCaseId") or run.get("test_case_id") or (run.get("test_case") or {}).get("id")


DETAILS_FIELD = "Test Run Details"  # custom plain-text run field: runs have no description, so the old assignee goes here


def previous_assignee_text(run: dict) -> str:
    """Plain-text record of the run's Assigned To in the source (users are not migrated), or ''."""
    from .plan import assigned_to_names  # imported here: plan imports this module

    names = assigned_to_names(run)
    return 'Migrated metadata: (Previously) "Assigned To" : ' + ", ".join(f'"{n}"' for n in names) if names else ""


def plan_run_rows(ctx: RunContext, src: QtestClient, tgt: QtestClient, db: StateDb, sp: int, tp: int,
                  cycle_rows: list[dict], planned_testcases: set[int], metadata: bool = True) -> tuple[list[dict], list[dict]]:
    issues: list[dict] = []
    rows: list[dict] = []
    seen: set[int] = set()
    listed: dict[tuple, list[dict]] = {}
    tc_gone: dict[int, bool] = {}
    for node in cycle_rows:
        if node.get("context_only"):
            continue
        try:
            runs = src.list_runs(sp, node["source_id"], PARENT_TYPE[node["kind"]])
        except ApiError as exc:
            issues.append({"severity": "WARN", "area": "runs", "message": f"Cannot read runs of {node['kind']} '{node['path']}': {exc}"})
            continue
        for r in runs:
            if r["id"] in seen:
                continue
            seen.add(r["id"])
            tc = _tc_id(r)
            tc_row = db.testcase_target(src.name, sp, tc, tgt.name, tp) if tc else None
            deleted = False
            if tc_row and tc not in planned_testcases:  # a migrated test case may have been deleted in the target since
                if tc not in tc_gone:
                    tc_gone[tc] = gone(tgt, f"/projects/{tp}/test-cases/{tc_row['target_id']}")
                deleted = tc_gone[tc]
                if deleted:
                    tc_row = None
            mapped = db.run_target(src.name, sp, r["id"], tgt.name, tp)
            parent_target = node.get("target_id")
            stale = None
            if mapped:
                present = {}
                if parent_target:
                    key = (node["kind"], parent_target)
                    if key not in listed:
                        try:
                            listed[key] = tgt.list_runs(tp, parent_target, PARENT_TYPE[node["kind"]])
                        except ApiError:
                            listed[key] = []
                    present = {t["id"] for t in listed[key]}
                if not parent_target or (mapped not in present and left_parent(tgt, tp, "run", mapped, parent_target, node["kind"], None)):
                    stale, mapped = mapped, None  # deleted or moved away in the target (or its parent is being created again)
                    issues.append({"severity": "WARN", "area": "runs",
                                   "message": f"Run '{r.get('name')}' was migrated earlier but is no longer in the target (deleted or moved away); it will be created again"})
            row = {"kind": "run", "source_id": r["id"], "source_pid": r.get("pid"), "name": r.get("name"),
                   "parent_kind": node["kind"], "parent_source_id": node["source_id"], "path": f"{node['path']} / {r.get('name')}",
                   "test_case_source_id": tc, "target_id": mapped, "stale_target_id": stale,
                   "previous_assignee": previous_assignee_text(r) if metadata else "",
                   "payload": {"name": str(r.get("name") or "").strip() or f"(unnamed run {r['id']})"}}
            if mapped:
                row["action"] = "skip-mapped"
            elif not tc or not (tc_row or tc in planned_testcases):
                row.update(action="skip-no-testcase", error=DELETED if deleted else NOT_MIGRATED)
            else:
                row["action"] = "create"
                if parent_target:
                    key = (node["kind"], parent_target)
                    if key not in listed:
                        try:
                            listed[key] = tgt.list_runs(tp, parent_target, PARENT_TYPE[node["kind"]])
                        except ApiError:
                            listed[key] = []
                    want_tc = tc_row["target_id"] if tc_row else None
                    match = next((t for t in listed[key] if _key(t.get("name")) == _key(row["payload"]["name"])
                                  and _tc_id(t) == want_tc), None)
                    if match:
                        row.update(action="reuse", target_id=match["id"])
            row.setdefault("error", "")
            row["attachments"] = []
            if row["action"] != "skip-no-testcase":
                try:
                    row["attachments"] = list_files(src, sp, OBJECT_TYPE, r["id"])
                except ApiError as exc:  # for example 403 on a run the token may not read: the run is still migrated
                    issues.append({"severity": "WARN", "area": "run files",
                                   "message": f"Cannot list the files of run '{r.get('name')}' (source id {r['id']}): {exc}; they are not copied"})
            rows.append(row)
            ctx.event("run", f"plan-{row['action']}", source_id=r["id"], target_id=row["target_id"], name=row["name"])
    skipped = [r for r in rows if r["action"] == "skip-no-testcase"]
    if skipped:
        issues.append({"severity": "WARN", "area": "runs",
                       "message": f"{len(skipped)} run(s) are skipped because their test case is not migrated (see runs.csv)"})
    return rows, issues


def summarize(rows: list[dict]) -> dict:
    return {"runs": dict(Counter(r["action"] for r in rows)),
            "run_attachments": {"to-copy": sum(len(r["attachments"]) for r in rows if r["action"] in ("create", "reuse", "skip-mapped"))}}


def apply_runs(ctx: RunContext, tgt: QtestClient, db: StateDb, plan: dict, cycle_results: list[dict],
               aborted: str = "", src: QtestClient | None = None) -> tuple[list[dict], str]:
    src_name, sp = plan["source"]["instance"], plan["source"]["project_id"]
    tgt_name, tp = plan["target"]["instance"], plan["target"]["project_id"]
    parents = {(r["kind"], r["source_id"]): r["target_id"] for r in cycle_results
               if r["result"] in ("created", "reuse", "skip-mapped") and r.get("target_id")}
    results: list[dict] = []
    details_field = tgt.run_field_id(tp, DETAILS_FIELD) if any(r.get("previous_assignee") for r in plan["runs"]) else None
    ctx.log.info("Applying %d test run(s)", len(plan["runs"]))
    consecutive = 0
    for plan_row in plan["runs"]:
        row = {**plan_row, "result": plan_row["action"], "target_test_case_id": None, "parent_target_id": None}
        parent = parents.get((plan_row["parent_kind"], plan_row["parent_source_id"])) \
            or db.cycle_target(src_name, sp, plan_row["parent_kind"], plan_row["parent_source_id"], tgt_name, tp)
        row["parent_target_id"] = parent
        tc = db.testcase_target(src_name, sp, plan_row["test_case_source_id"], tgt_name, tp) if plan_row["test_case_source_id"] else None
        row["target_test_case_id"] = tc["target_id"] if tc else None
        if plan_row.get("stale_target_id"):
            db.forget_target_object("run", tgt_name, tp, plan_row["stale_target_id"])
        mapped = db.run_target(src_name, sp, plan_row["source_id"], tgt_name, tp)
        if plan_row["action"] == "skip-no-testcase":
            row["result"] = "skipped"
        elif aborted:
            row.update(result="not-attempted", error=aborted)
        elif mapped:
            row.update(result="skip-mapped", target_id=mapped)
        elif plan_row["action"] == "reuse" and plan_row.get("target_id"):
            db.put_run(src_name, sp, plan_row["source_id"], tgt_name, tp, plan_row["target_id"], plan_row["name"], ctx.run_id)
        elif tc is None:
            row.update(result="skipped", error=NOT_MIGRATED)
        elif parent is None:
            row.update(result="skipped", error=f"the {plan_row['parent_kind']} above it was not created")
        else:
            try:
                body = {**plan_row["payload"], "test_case": {"id": tc["target_id"]}}
                details = []
                if plan_row.get("previous_assignee"):
                    if details_field is not None:
                        details = [{"field_id": details_field, "field_value": plan_row["previous_assignee"]}]
                    else:
                        row["assignee_note"] = f"the target has no '{DETAILS_FIELD}' run field: the previous Assigned To was not recorded"
                # Assigned To is not sent: HCSC assigns the run to the token user (it refuses an empty value).
                if details:
                    body["properties"] = details
                def make_run():
                    nonlocal details_field
                    try:
                        return tgt.create_run(tp, parent, PARENT_TYPE[plan_row["parent_kind"]], body)
                    except ApiError as exc:
                        if not details or exc.status != 400:
                            raise
                        ctx.log.warning("run refused with the Test Run Details note (%s): created without it", exc.detail)
                        row["assignee_note"] = f"the previous Assigned To could not be recorded ({exc.detail})"
                        details_field = None
                        return tgt.create_run(tp, parent, PARENT_TYPE[plan_row["parent_kind"]], {k: v for k, v in body.items() if k != "properties"})

                created = journaled(ctx, tgt, db, "run", src_name, sp, plan_row["source_id"], tgt_name, tp, parent, plan_row["name"],
                                    make_run, parent_type=PARENT_TYPE[plan_row["parent_kind"]])
                row.update(result="created", target_id=created["id"])
                db.put_run(src_name, sp, plan_row["source_id"], tgt_name, tp, created["id"], plan_row["name"], ctx.run_id)
                consecutive = 0
            except (ApiError, KeyError) as exc:
                row.update(result="failed", error=f"HTTP {exc.status}: {exc.detail}" if isinstance(exc, ApiError) and exc.status
                           else str(exc))
                consecutive += 1
                ctx.log.error("run FAILED  %s: %s", plan_row["path"], row["error"])
                if isinstance(exc, ApiError) and exc.status in (401, 403):
                    aborted = f"aborted after authorization failure ({row['error']})"
                elif consecutive >= MAX_CONSECUTIVE_FAILURES:
                    aborted = f"aborted after {MAX_CONSECUTIVE_FAILURES} consecutive run failures"
        row["attachment_results"] = []
        if src is not None and row["result"] in ("created", "reuse", "skip-mapped") and row.get("target_id") and plan_row.get("attachments") and not aborted:
            row["attachment_results"] = copy_files(ctx, src, tgt, db, sp, tp, OBJECT_TYPE, plan_row["source_id"], row["target_id"],
                                                   plan_row["attachments"])
        ctx.event("run", row["result"], source_id=row["source_id"], target_id=row.get("target_id"), name=row["name"],
                  error=row.get("error", ""))
        results.append(row)
    done = Counter(r["result"] for r in results)
    ctx.log.info("Runs: %s", dict(done))
    return results, aborted


def verify_runs(ctx: RunContext, tgt: QtestClient, tp: int, rows: list[dict]) -> dict:
    """Every run created, reused or mapped by apply must be listed under its parent and point at the migrated test case."""
    report = {"checked": 0, "ok": 0, "missing": [], "wrong_test_case": [], "files_missing": [], "errors": []}
    cache: dict[tuple, dict[int, dict]] = {}
    for row in rows:
        if row["result"] not in ("created", "reuse", "skip-mapped") or not row.get("target_id") or row.get("parent_target_id") is None:
            continue
        report["checked"] += 1
        key = (row["parent_kind"], row["parent_target_id"])
        if key not in cache:
            try:
                cache[key] = {t["id"]: t for t in tgt.list_runs(tp, row["parent_target_id"], PARENT_TYPE[row["parent_kind"]])}
            except ApiError as exc:
                report["errors"].append(f"runs under {row['parent_kind']} {row['parent_target_id']}: {exc}")
                cache[key] = {}
        found = cache[key].get(row["target_id"])
        if found is None:
            report["missing"].append({"path": row["path"], "target_id": row["target_id"]})
        elif row.get("target_test_case_id") and _tc_id(found) not in (None, row["target_test_case_id"]):
            report["wrong_test_case"].append({"path": row["path"], "expected": row["target_test_case_id"], "actual": _tc_id(found)})
        else:
            report["ok"] += 1
        missing, error = verify_files(tgt, tp, OBJECT_TYPE, row["target_id"], row.get("attachment_results") or [])
        report["files_missing"] += [{"path": row["path"], "name": n} for n in missing]
        if error:
            report["errors"].append(error)
    ctx.log.info("Runs verified: %d checked, %d ok, %d missing", report["checked"], report["ok"], len(report["missing"]))
    return report
