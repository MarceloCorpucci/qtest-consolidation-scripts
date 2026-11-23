"""Execute a reviewed plan against the target instance.

``apply`` never re-plans: it replays ``plan.json`` + ``testcases.jsonl`` of an
earlier ``plan`` run, so what was reviewed is what is written.  The source
instance is only ever read (its client is read-only).  Progress is recorded in
the local SQLite mapping after every created object, so an interrupted run can
simply be applied again: already created objects are skipped.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from .approvals import apply_approvals
from .stepfiles import apply_step_files
from .attachments import apply_attachments
from .client import ApiError, QtestClient
from .db import StateDb
from .cycles import apply_cycles
from .releases import apply_releases
from .logs import apply_logs
from .runs import apply_runs
from .runindex import find_run_dir
from .runlog import RunContext
from .journal import create_settled, reconcile_intents

MAX_CONSECUTIVE_FAILURES = 25
PROGRESS_EVERY = 25


class ApplyError(RuntimeError):
    pass


def load_plan(runs_dir: Path, plan_run_id: str) -> tuple[dict, Path]:
    run_dir = find_run_dir(runs_dir, plan_run_id)
    plan_file = run_dir / "plan.json"
    if not plan_file.is_file():
        raise ApplyError(f"No plan found at {plan_file}")
    plan = json.loads(plan_file.read_text(encoding="utf-8"))
    return plan, run_dir


def _error_text(exc: ApiError) -> str:
    return f"HTTP {exc.status}: {exc.detail}" if exc.status else f"connection error: {exc.detail}"


def apply_plan(ctx: RunContext, tgt: QtestClient, db: StateDb, plan: dict, plan_dir: Path,
               src: QtestClient | None = None) -> dict:
    src_name, sp = plan["source"]["instance"], plan["source"]["project_id"]
    tgt_name, tp = plan["target"]["instance"], plan["target"]["project_id"]
    if tgt.name != tgt_name:
        raise ApplyError(f"Plan targets '{tgt_name}' but the client is for '{tgt.name}'")
    for side, client in (("target", tgt), ("source", src)):
        planned = plan[side].get("url")
        if client is not None and planned and client.instance.web_url != planned:
            raise ApplyError(f"The {side} URL differs from the reviewed plan ({planned}); restore it or create and review a new plan")
    if not tgt.allow_writes:
        raise ApplyError("Target client is read-only")

    # ------------------------------------------- settle writes cut off by an earlier network loss
    unresolved = reconcile_intents(ctx, tgt, db, src_name, sp, tgt_name, tp,
                                   {r["source_id"]: r.get("source_pid") for r in plan["testcases"]})

    # -------------------------------------------------------------- folders
    folders = plan["folders"]
    resolved: dict[int, int | None] = {}  # folder idx -> real target id (None = failed)
    results_folders: list[dict] = []
    ctx.log.info("Applying %d folder(s) to %s project %s", len(folders), tgt_name, tp)
    aborted = ""
    for folder in folders:
        row = {**folder, "status": "", "error": ""}
        if aborted:
            row.update(status="not-attempted", error=aborted)
            results_folders.append(row)
            continue
        parent_idx = folder["parent_idx"]
        parent_id = plan["scope"]["target_parent_id"] if parent_idx is None else resolved.get(parent_idx)
        if parent_idx is not None and parent_id is None:
            row.update(status="skipped", error="parent folder was not created")
        elif folder["action"] in ("reuse", "reuse-mapped"):
            resolved[folder["idx"]] = folder["target_id"]
            row.update(status="reused", target_id=folder["target_id"])
        elif (db.folder_target(src_name, sp, folder["source_id"], tgt_name, tp)
              not in (None, folder.get("stale_target_id"))):  # a mapping the plan found stale is ignored
            mapped_id = db.folder_target(src_name, sp, folder["source_id"], tgt_name, tp)
            resolved[folder["idx"]] = mapped_id
            row.update(status="reused", target_id=mapped_id)  # created by an earlier apply of this plan
        elif ("folder", folder["source_id"]) in unresolved:
            row.update(status="failed", error=unresolved[("folder", folder["source_id"])])
        else:
            intent = db.open_intent("folder", src_name, sp, folder["source_id"], tgt_name, tp, parent_id, folder["name"], ctx.run_id)
            try:
                created = create_settled(ctx, tgt, db, intent, lambda: tgt.create_module(tp, folder["name"], parent_id, folder.get("description") or ""))
                target_id = created["id"]
                resolved[folder["idx"]] = target_id
                db.put_folder(src_name, sp, folder["source_id"], tgt_name, tp, target_id, folder["path"], ctx.run_id)
                db.close_intent(intent, "done", target_id)
                row.update(status="created", target_id=target_id)
            except (ApiError, KeyError) as exc:
                if isinstance(exc, ApiError) and not exc.ambiguous:
                    db.close_intent(intent, "failed")  # the server answered: nothing was created
                row.update(status="failed", error=_error_text(exc) if isinstance(exc, ApiError) else f"no id in response: {exc}")
                if isinstance(exc, ApiError) and exc.status in (401, 403):
                    aborted = f"aborted after authorization failure ({row['error']})"
                    ctx.log.error("Stopping: %s", aborted)
        if row["status"] == "reused" and folder["action"] == "reuse":
            db.put_folder(src_name, sp, folder["source_id"], tgt_name, tp, folder["target_id"], folder["path"], ctx.run_id)
        level = 40 if row["status"] in ("failed", "skipped") else 20
        ctx.log.log(level, "folder %-8s %s%s", row["status"], folder["path"], f"  [{row['error']}]" if row["error"] else "")
        ctx.event("folder", row["status"], source_id=folder["source_id"], target_id=row.get("target_id"),
                  path=folder["path"], error=row["error"])
        results_folders.append(row)

    # ----------------------------------------------------------- test cases
    payloads: dict[int, dict] = {}
    with open(plan_dir / "testcases.jsonl", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                record = json.loads(line)
                payloads[record["n"]] = record["payload"]
    rows = plan["testcases"]
    to_create = [r for r in rows if r["action"] == "create"]
    ctx.log.info("Creating %d test case(s) (%d skipped/deferred by plan)", len(to_create), len(rows) - len(to_create))
    results_tcs: list[dict] = []
    consecutive_failures = 0
    done = 0
    for plan_row in rows:
        row = {**plan_row, "status": plan_row["action"], "error": ""}
        if plan_row["action"] != "create":
            results_tcs.append(row)
            continue
        if aborted:
            row.update(status="not-attempted", error=aborted)
            results_tcs.append(row)
            continue
        parent = resolved.get(plan_row["folder_idx"])
        if parent is None:
            row.update(status="skipped", error="target folder is not available")
        else:
            already = db.testcase_target(src_name, sp, plan_row["source_id"], tgt_name, tp)
            stale_id = plan_row.get("stale_target_id")
            if stale_id and already and already["target_id"] == stale_id:
                db.forget_target_testcase(tgt_name, tp, stale_id)  # gone from the target: its files and approval must be redone
                already = None
            if already:
                row.update(status="skip-mapped", target_id=already["target_id"], target_pid=already["target_pid"])
            elif ("testcase", plan_row["source_id"]) in unresolved:
                row.update(status="failed", error=unresolved[("testcase", plan_row["source_id"])])
            else:
                intent = db.open_intent("testcase", src_name, sp, plan_row["source_id"], tgt_name, tp, parent,
                                        plan_row["name"], ctx.run_id)
                try:
                    created = create_settled(ctx, tgt, db, intent, lambda: tgt.create_testcase(tp, parent, payloads[plan_row["n"]]),
                                             {plan_row["source_id"]: plan_row.get("source_pid")})
                    target_id, target_pid = created["id"], created.get("pid")
                    db.put_testcase(src_name, sp, plan_row["source_id"], plan_row.get("source_pid"), tgt_name, tp,
                                    target_id, target_pid, plan_row["name"], ctx.run_id)
                    db.close_intent(intent, "done", target_id)
                    row.update(status="created", target_id=target_id, target_pid=target_pid)
                    consecutive_failures = 0
                except (ApiError, KeyError) as exc:
                    if isinstance(exc, ApiError) and not exc.ambiguous:
                        db.close_intent(intent, "failed")
                    message = _error_text(exc) if isinstance(exc, ApiError) else f"no id in response: {exc}"
                    row.update(status="failed", error=message)
                    consecutive_failures += 1
                    if isinstance(exc, ApiError) and exc.status in (401, 403):
                        aborted = f"aborted after authorization failure ({message})"
                    elif consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        aborted = f"aborted after {MAX_CONSECUTIVE_FAILURES} consecutive failures"
                    if aborted:
                        ctx.log.error("Stopping test case upload: %s", aborted)
        done += 1
        ctx.event("testcase", row["status"], source_id=row["source_id"], target_id=row.get("target_id"),
                  name=row["name"], path=row["path"], error=row["error"])
        if row["status"] == "failed":
            ctx.log.error("test case FAILED  %s (source %s): %s", row["name"], row["source_pid"] or row["source_id"], row["error"])
        elif done % PROGRESS_EVERY == 0 or done == len(to_create):
            tally = Counter(r["status"] for r in results_tcs)
            ctx.log.info("  progress %d/%d  created=%d failed=%d", done, len(to_create), tally["created"], tally["failed"])
        results_tcs.append(row)

    results_atts: list[dict] = []
    if plan["scope"].get("attachments"):
        if src is None:
            raise ApplyError("The plan includes attachments but no source client was given")
        results_atts, aborted = apply_attachments(ctx, src, tgt, db, plan, results_tcs, aborted)

    results_step_files: list[dict] = []
    if plan["scope"].get("step_files"):
        if src is None:
            raise ApplyError("The plan includes step files but no source client was given")
        results_step_files, aborted = apply_step_files(ctx, src, tgt, db, plan, results_tcs, aborted)

    results_approvals: list[dict] = []
    if plan["scope"].get("approve"):
        results_approvals, aborted = apply_approvals(ctx, tgt, db, tp, results_tcs, aborted)

    results_releases: list[dict] = []
    if plan.get("releases"):
        results_releases, aborted = apply_releases(ctx, tgt, db, plan, aborted)

    results_cycles: list[dict] = []
    if plan.get("cycles"):
        results_cycles, aborted = apply_cycles(ctx, tgt, db, plan, results_releases, aborted)

    results_runs: list[dict] = []
    if plan.get("runs"):
        results_runs, aborted = apply_runs(ctx, tgt, db, plan, results_cycles, aborted, src)

    results_logs: list[dict] = []
    if plan.get("logs"):
        if src is None:
            raise ApplyError("The plan includes test logs (and their files) but no source client was given")
        results_logs, aborted = apply_logs(ctx, src, tgt, db, plan, results_runs, aborted)

    result = {**{k: plan[k] for k in ("source", "target", "scope", "issues")},
              "plan_run_id": plan["run_id"], "run_id": ctx.run_id, "folders": results_folders,
              "testcases": results_tcs, "attachments": results_atts, "step_files": results_step_files, "releases": results_releases, "cycles": results_cycles, "runs": results_runs, "logs": results_logs, "approvals": results_approvals, "aborted": aborted,
              "counts": {"folders": dict(Counter(r["status"] for r in results_folders)),
                         "testcases": dict(Counter(r["status"] for r in results_tcs)),
                         **({"attachments": dict(Counter(r["status"] for r in results_atts))}
                            if plan["scope"].get("attachments") else {}),
                         **({"step_files": dict(Counter(r["status"] for r in results_step_files))}
                            if plan["scope"].get("step_files") else {}),
                         **({"approvals": dict(Counter(r["status"] for r in results_approvals))}
                            if plan["scope"].get("approve") else {}),
                         **({"releases": dict(Counter(r["result"] for r in results_releases)),
                             "builds": dict(Counter(b["result"] for r in results_releases for b in r["builds"]))}
                            if plan.get("releases") else {}),
                         **({"cycles": dict(Counter(r["result"] for r in results_cycles if r["kind"] == "cycle")),
                             "suites": dict(Counter(r["result"] for r in results_cycles if r["kind"] == "suite"))}
                            if plan.get("cycles") else {}),
                         **({"runs": dict(Counter(r["result"] for r in results_runs))} if plan.get("runs") else {}),
                         **({"logs": dict(Counter(r["result"] for r in results_logs))} if plan.get("logs") else {})},
              "api": {tgt.name: tgt.stats}}
    ctx.write_json("result.json", result)
    return result
