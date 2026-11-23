"""Test logs (execution history): describe (plan), create (apply) and check (verify).

Every log of a migrated run is copied with its status (matched by name), original start and end dates, note and
attachments.  Logs are created oldest first so the newest ends up as the run's latest result; ``mode='last'`` copies only
the newest.  The executor is the user of the API token (users are not migrated); the original tester's name is written
into the note unless metadata is switched off.  Per-step results are not copied.  Mappings are kept in SQLite so a re-run
never creates a log twice.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime

from .approvals import is_approved_version
from .attachments import copy_files, list_files
from .journal import journaled
from .client import ApiError, QtestClient
from .cycles import gone
from .db import StateDb
from .runlog import RunContext

OBJECT_TYPE = "test-logs"
MAX_ATTACHMENT_BYTES = 100 * 1024 * 1024
MAX_CONSECUTIVE_FAILURES = 10


def _key(value) -> str:
    return str(value or "").strip().casefold()


def _instant(value):
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def same_instant(a, b) -> bool:
    x, y = _instant(a), _instant(b)
    return x == y if x is not None and y is not None else str(a) == str(b)


def _sort_key(log: dict):
    when = _instant(log.get("exe_end_date") or log.get("exe_start_date"))
    return (when.timestamp() if when else 0, log.get("id") or 0)


def tester_name(log: dict) -> str:
    for prop in log.get("properties") or []:
        if _key(prop.get("field_name")) in ("tester", "execution user") and prop.get("field_value_name"):
            return str(prop["field_value_name"])
    return ""


def log_note(log: dict, metadata: bool) -> str:
    note = str(log.get("note") or "").strip()
    if metadata:
        who = tester_name(log)
        meta = f"Migrated metadata: originally executed by {who}" if who else "Migrated metadata: executed in the source instance"
        note = f"{note}\n\n{meta} (source log {log.get('id')})" if note else f"{meta} (source log {log.get('id')})"
    return note


NOT_APPROVED = ("the run is bound to test case version {version}, which is not approved: HCSC accepts logs only on approved "
                "versions. Approve the test case, then delete this run in HCSC so it is created again")
NOT_APPROVED_YET = "the test case is not approved yet (version {version}): approve it before creating runs and logs"


def _bound_version(tgt: QtestClient, tp: int, db: StateDb, src_name: str, sp: int, run: dict, cache: dict) -> tuple[str, bool]:
    """(version, known) the target run uses, or the version a new run would use (the test case's current one)."""
    try:
        if run.get("target_id"):
            return str(tgt.get_run(tp, run["target_id"]).get("test_case_version") or ""), True
        tc = db.testcase_target(src_name, sp, run["test_case_source_id"], tgt.name, tp) if run.get("test_case_source_id") else None
        if tc is None:
            return "", False  # the test case is created by the same plan: its version is not known yet
        if tc["target_id"] not in cache:
            cache[tc["target_id"]] = str(tgt.request("GET", f"/projects/{tp}/test-cases/{tc['target_id']}")[0].get("version") or "")
        return cache[tc["target_id"]], True
    except ApiError:
        return "", False


def step_log_rows(log: dict, status_by_name: dict) -> list[dict]:
    """The log's per-step results with the status mapped to the target's status id (steps with an unknown status are left out)."""
    out = []
    for sl in sorted(log.get("test_step_logs") or [], key=lambda x: x.get("order") or 0):
        name = (sl.get("status") or {}).get("name") or ""
        status_id = status_by_name.get(_key(name))
        if status_id is None or sl.get("order") is None:
            continue
        out.append({"order": sl["order"], "status": name, "status_id": status_id, "actual_result": sl.get("actual_result") or "",
                    "exe_date": sl.get("exe_date")})
    return out


def step_payload(rows: list[dict], target_steps: list[dict]) -> tuple[list[dict], int]:
    """Step results for the target log, matched to the target test case's steps by order; also how many could not be matched."""
    by_order = {s.get("order"): s["id"] for s in target_steps if s.get("id") is not None}
    payload, unmatched = [], 0
    for r in rows:
        step_id = by_order.get(r["order"])
        if step_id is None:
            unmatched += 1
            continue
        item = {"test_step_id": step_id, "order": r["order"], "status": {"id": r["status_id"], "name": r["status"]},
                "actual_result": r["actual_result"]}
        if r.get("exe_date"):
            item["exe_date"] = r["exe_date"]
        payload.append(item)
    return payload, unmatched


def plan_log_rows(ctx: RunContext, src: QtestClient, tgt: QtestClient, db: StateDb, sp: int, tp: int,
                  run_rows: list[dict], mode: str = "all", metadata: bool = True,
                  step_results: bool = True) -> tuple[list[dict], list[dict]]:
    issues: list[dict] = []
    try:
        status_by_name = {_key(s.get("name")): s["id"] for s in tgt.execution_statuses(tp)}
    except ApiError as exc:
        issues.append({"severity": "WARN", "area": "logs", "message": f"Cannot read the target execution statuses: {exc}"})
        status_by_name = {}
    rows: list[dict] = []
    versions: dict[int, str] = {}
    for run in run_rows:
        if run["action"] == "skip-no-testcase":
            continue
        version, known = _bound_version(tgt, tp, db, src.name, sp, run, versions)
        not_approved = known and not is_approved_version(version)
        try:
            logs = sorted(src.list_logs(sp, run["source_id"]), key=_sort_key)
        except ApiError as exc:
            issues.append({"severity": "WARN", "area": "logs", "message": f"Cannot read the logs of run '{run['name']}': {exc}"})
            continue
        run_target = run.get("target_id")
        target_logs: dict[int, dict] | None = None
        for index, log in enumerate(logs, 1):
            status = log.get("status") or {}
            row = {"kind": "log", "source_id": log["id"], "run_source_id": run["source_id"], "run_path": run["path"], "n": index,
                   "of": len(logs), "test_case_source_id": run.get("test_case_source_id"), "status": status.get("name") or "", "exe_start_date": log.get("exe_start_date"),
                   "exe_end_date": log.get("exe_end_date"), "tester": tester_name(log), "target_id": None, "stale_target_id": None,
                   "attachments": [], "error": "", "action": "create"}
            if mode == "last" and index != len(logs):
                row["action"] = "skip-older"
                rows.append(row)
                continue
            if not_approved:
                row.update(action="skip-not-approved", error=(NOT_APPROVED if run.get("target_id") else NOT_APPROVED_YET).format(version=version or "?"))
                rows.append(row)
                continue
            target_status = status_by_name.get(_key(status.get("name")))
            if target_status is None:
                row.update(action="skip-no-status", error=f"status '{status.get('name')}' does not exist in the target")
                rows.append(row)
                continue
            row["payload"] = {"status": {"id": target_status, "name": status.get("name")},
                              "exe_start_date": log.get("exe_start_date"), "exe_end_date": log.get("exe_end_date") or log.get("exe_start_date")}
            note = log_note(log, metadata)
            if note:
                row["payload"]["note"] = note
            row["step_logs"] = step_log_rows(log, status_by_name) if step_results else []
            try:
                row["attachments"] = list_files(src, sp, OBJECT_TYPE, log["id"])
            except ApiError as exc:
                issues.append({"severity": "WARN", "area": "log files",
                               "message": f"Cannot list the files of log {index} of run '{run['name']}' (source id {log['id']}): {exc}; they are not copied"})
            mapped = db.log_target(src.name, sp, log["id"], tgt.name, tp)
            if mapped:
                if run_target is None:
                    row["stale_target_id"], mapped = mapped, None  # its run is being created again
                else:
                    if target_logs is None:
                        try:
                            target_logs = {t["id"]: t for t in tgt.list_logs(tp, run_target)}
                        except ApiError:
                            target_logs = {}
                    if mapped not in target_logs and gone(tgt, f"/projects/{tp}/test-runs/{run_target}/test-logs/{mapped}"):
                        row["stale_target_id"], mapped = mapped, None
                if row["stale_target_id"]:
                    issues.append({"severity": "WARN", "area": "logs",
                                   "message": f"Log {index} of run '{run['name']}' was migrated earlier but no longer exists in the target; it will be created again"})
            if mapped:
                row.update(action="skip-mapped", target_id=mapped)
            elif run_target is not None:
                if target_logs is None:
                    try:
                        target_logs = {t["id"]: t for t in tgt.list_logs(tp, run_target)}
                    except ApiError:
                        target_logs = {}
                match = next((t for t in target_logs.values() if same_instant(t.get("exe_start_date"), log.get("exe_start_date"))
                              and _key((t.get("status") or {}).get("name")) == _key(status.get("name"))), None)
                if match:
                    row.update(action="reuse", target_id=match["id"])
            rows.append(row)
            ctx.event("log", f"plan-{row['action']}", source_id=log["id"], target_id=row["target_id"], run=run["name"])
    return rows, issues


def summarize(rows: list[dict]) -> dict:
    out = {"logs": dict(Counter(r["action"] for r in rows))}
    out["log_steps"] = {"to-copy": sum(len(r.get("step_logs") or []) for r in rows if r["action"] == "create")}
    out["log_attachments"] = {"to-copy": sum(len(r["attachments"]) for r in rows if r["action"] in ("create", "reuse"))}
    return out


def apply_logs(ctx: RunContext, src: QtestClient, tgt: QtestClient, db: StateDb, plan: dict, run_results: list[dict],
               aborted: str = "") -> tuple[list[dict], str]:
    src_name, sp = plan["source"]["instance"], plan["source"]["project_id"]
    tgt_name, tp = plan["target"]["instance"], plan["target"]["project_id"]
    steps_cache: dict[int, list[dict]] = {}
    runs = {r["source_id"]: r for r in run_results if r["result"] in ("created", "reuse", "skip-mapped") and r.get("target_id")}
    results: list[dict] = []
    ctx.log.info("Applying %d test log(s)", len(plan["logs"]))
    consecutive = 0
    for plan_row in plan["logs"]:
        row = {**plan_row, "result": plan_row["action"], "run_target_id": None, "attachment_results": []}
        if plan_row["action"].startswith("skip-") and plan_row["action"] != "skip-mapped":
            row["result"] = plan_row["action"]
            results.append(row)
            continue
        run = runs.get(plan_row["run_source_id"])
        run_target = run["target_id"] if run else db.run_target(src_name, sp, plan_row["run_source_id"], tgt_name, tp)
        row["run_target_id"] = run_target
        if plan_row.get("stale_target_id"):
            db.forget_target_object("log", tgt_name, tp, plan_row["stale_target_id"])
        mapped = db.log_target(src_name, sp, plan_row["source_id"], tgt_name, tp)
        if aborted:
            row.update(result="not-attempted", error=aborted)
        elif mapped:
            row.update(result="skip-mapped", target_id=mapped)
        elif run_target is None:
            row.update(result="skipped", error="the test run was not created")
        elif plan_row["action"] == "reuse" and plan_row.get("target_id"):
            db.put_log(src_name, sp, plan_row["source_id"], tgt_name, tp, plan_row["target_id"], run_target, ctx.run_id)
        else:
            try:
                payload = dict(plan_row["payload"])
                if plan_row.get("step_logs"):
                    tc = db.testcase_target(src_name, sp, plan_row["test_case_source_id"], tgt_name, tp) if plan_row.get("test_case_source_id") else None
                    if tc is not None:
                        if tc["target_id"] not in steps_cache:
                            try:
                                steps_cache[tc["target_id"]] = tgt.list_steps(tp, tc["target_id"])
                            except ApiError:
                                steps_cache[tc["target_id"]] = []
                        payload_steps, unmatched = step_payload(plan_row["step_logs"], steps_cache[tc["target_id"]])
                        if payload_steps:
                            payload["test_step_logs"] = payload_steps
                        if unmatched or not payload_steps:
                            row["steps_note"] = f"{unmatched or len(plan_row['step_logs'])} step result(s) could not be matched to a target step"
                row["steps_sent"] = {s["order"]: s["status"]["name"] for s in payload.get("test_step_logs", [])}
                def make_log():
                    try:
                        return tgt.create_log(tp, run_target, payload)
                    except ApiError as exc:
                        if not payload.get("test_step_logs") or exc.status != 400:
                            raise
                        ctx.log.warning("log with step results refused (%s): created without them", exc.detail)
                        row["steps_note"] = f"step results not accepted by the target ({exc.detail}); the log was created without them"
                        row["steps_sent"] = {}
                        payload.pop("test_step_logs")
                        return tgt.create_log(tp, run_target, payload)

                created = journaled(ctx, tgt, db, "log", src_name, sp, plan_row["source_id"], tgt_name, tp, run_target,
                                    str(plan_row["payload"].get("exe_start_date") or ""), make_log)
                row.update(result="created", target_id=created["id"])
                db.put_log(src_name, sp, plan_row["source_id"], tgt_name, tp, created["id"], run_target, ctx.run_id)
                consecutive = 0
            except (ApiError, KeyError) as exc:
                row.update(result="failed", error=f"HTTP {exc.status}: {exc.detail}" if isinstance(exc, ApiError) and exc.status
                           else str(exc))
                consecutive += 1
                ctx.log.error("log FAILED  %s #%s: %s", plan_row["run_path"], plan_row["n"], row["error"])
                if isinstance(exc, ApiError) and exc.status in (401, 403):
                    aborted = f"aborted after authorization failure ({row['error']})"
                elif consecutive >= MAX_CONSECUTIVE_FAILURES:
                    aborted = f"aborted after {MAX_CONSECUTIVE_FAILURES} consecutive log failures"
        if row["result"] in ("created", "reuse", "skip-mapped") and row.get("target_id") and plan_row["attachments"] and not aborted:
            row["attachment_results"] = copy_files(ctx, src, tgt, db, sp, tp, OBJECT_TYPE, row["source_id"], row["target_id"], plan_row["attachments"])
        ctx.event("log", row["result"], source_id=row["source_id"], target_id=row.get("target_id"), error=row.get("error", ""))
        results.append(row)
    ctx.log.info("Logs: %s", dict(Counter(r["result"] for r in results)))
    return results, aborted


def verify_logs(ctx: RunContext, tgt: QtestClient, tp: int, rows: list[dict]) -> dict:
    """Each log must be listed under its run with the same status and start date; the run must show the newest log's status."""
    report = {"checked": 0, "ok": 0, "missing": [], "different": [], "attachments_missing": [], "steps_different": [], "run_status": [], "errors": []}
    cache: dict[int, dict[int, dict]] = {}
    newest: dict[int, dict] = {}
    for row in rows:
        if row["result"] not in ("created", "reuse", "skip-mapped") or not row.get("target_id") or not row.get("run_target_id"):
            continue
        report["checked"] += 1
        rid = row["run_target_id"]
        if rid not in cache:
            try:
                cache[rid] = {t["id"]: t for t in tgt.list_logs(tp, rid)}
            except ApiError as exc:
                report["errors"].append(f"logs of run {rid}: {exc}")
                cache[rid] = {}
        found = cache[rid].get(row["target_id"])
        newest[rid] = row
        if found is None:
            report["missing"].append({"run": row["run_path"], "log": row["n"], "target_id": row["target_id"]})
            continue
        wanted = row.get("payload") or {}
        problems = []
        if wanted.get("exe_start_date") and not same_instant(found.get("exe_start_date"), wanted["exe_start_date"]):
            problems.append(f"start date {found.get('exe_start_date')} instead of {wanted['exe_start_date']}")
        if row.get("status") and _key((found.get("status") or {}).get("name")) != _key(row["status"]):
            problems.append(f"status {(found.get('status') or {}).get('name')} instead of {row['status']}")
        if problems:
            report["different"].append({"run": row["run_path"], "log": row["n"], "target_id": row["target_id"], "problem": "; ".join(problems)})
        else:
            report["ok"] += 1
        sent = {int(k): v for k, v in (row.get("steps_sent") or {}).items()}
        if sent:
            have = {sl.get("order"): ((sl.get("status") or {}).get("name") or "") for sl in found.get("test_step_logs") or []}
            for order, name in sorted(sent.items()):
                if _key(have.get(order)) != _key(name):
                    report["steps_different"].append({"run": row["run_path"], "log": row["n"], "step": order, "expected": name,
                                                      "actual": have.get(order) or "(none)"})
        wanted_atts = [a for a in row.get("attachment_results") or [] if a["status"] in ("created", "skip-mapped")]
        if wanted_atts:
            try:
                names = Counter(a.get("name") or "" for a in tgt.list_attachments(tp, OBJECT_TYPE, row["target_id"]))
            except ApiError as exc:
                report["errors"].append(f"attachments of log {row['target_id']}: {exc}")
                continue
            for a in wanted_atts:
                if names.get(a["name"], 0) == 0:
                    report["attachments_missing"].append({"run": row["run_path"], "log": row["n"], "name": a["name"]})
    for rid, row in newest.items():  # the run's own status must follow its newest log
        try:
            have = next((p for p in tgt.get_run(tp, rid).get("properties") or [] if _key(p.get("field_name")) == "status"), {})
        except ApiError as exc:
            report["errors"].append(f"run {rid}: {exc}")
            continue
        if row.get("status") and _key(have.get("field_value_name")) != _key(row["status"]):
            report["run_status"].append({"run": row["run_path"], "expected": row["status"], "actual": have.get("field_value_name")})
    ctx.log.info("Logs verified: %d checked, %d ok, %d missing, %d different", report["checked"], report["ok"],
                 len(report["missing"]), len(report["different"]))
    return report
