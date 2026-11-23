"""Read-only check that what ``apply`` reported is really in the target."""
from __future__ import annotations

import json
from pathlib import Path

from .approvals import verify_approvals
from .attachments import verify_attachments
from .stepfiles import verify_step_files
from .client import ApiError, QtestClient
from .cycles import verify_cycles
from .releases import verify_releases
from .logs import verify_logs
from .runs import verify_runs
from .runindex import find_run_dir
from .runlog import RunContext


def _key(name) -> str:
    return str(name or "").strip().casefold()


def load_result(runs_dir: Path, apply_run_id: str) -> dict:
    path = find_run_dir(runs_dir, apply_run_id) / "result.json"
    if not path.is_file():
        raise FileNotFoundError(f"No apply result at {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def verify_result(ctx: RunContext, tgt: QtestClient, result: dict) -> dict:
    tp = result["target"]["project_id"]
    by_folder: dict[int, list[dict]] = {}
    for row in result["testcases"]:
        by_folder.setdefault(row["folder_idx"], []).append(row)

    report = {"run_id": ctx.run_id, "apply_run_id": result["run_id"], "target": result["target"],
              "checked": 0, "ok": 0, "missing": [], "name_mismatch": [], "step_mismatch": [], "folders": [],
              "errors": []}
    for folder in result["folders"]:
        target_id = folder.get("target_id")
        rows = by_folder.get(folder["idx"], [])
        if not target_id or folder["status"] not in ("created", "reused"):
            continue
        try:
            tgt.get_module(tp, target_id)  # an empty folder must exist too
            existing = {tc["id"]: tc for tc in tgt.list_testcases(tp, target_id)}
        except ApiError as exc:
            report["errors"].append(f"{folder['path']}: {exc}")
            ctx.log.error("Cannot read target folder '%s': %s", folder["path"], exc)
            continue
        folder_summary = {"path": folder["path"], "target_id": target_id,
                          "source_count": len(rows), "target_count": len(existing),
                          "expected_in_target": sum(1 for r in rows if r.get("target_id"))}
        report["folders"].append(folder_summary)
        for row in rows:
            if row["status"] not in ("created", "skip-mapped", "skip-exists") or not row.get("target_id"):
                continue
            report["checked"] += 1
            found = existing.get(row["target_id"])
            problem = None
            if found is None:
                report["missing"].append({"name": row["name"], "target_id": row["target_id"], "path": folder["path"]})
                problem = "missing"
            elif _key(found.get("name")) != _key(row["name"]):
                report["name_mismatch"].append({"expected": row["name"], "actual": found.get("name"),
                                                "target_id": row["target_id"]})
                problem = "name-mismatch"
            elif row["status"] in ("created", "skip-mapped") and len(found.get("test_steps") or []) != row["steps"]:
                report["step_mismatch"].append({"name": row["name"], "target_id": row["target_id"],
                                                "expected_steps": row["steps"],
                                                "actual_steps": len(found.get("test_steps") or [])})
                problem = "step-mismatch"
            if problem is None:
                report["ok"] += 1
            ctx.event("verify", problem or "ok", target_id=row["target_id"], name=row["name"], path=folder["path"])
        ctx.log.info("  verified '%s': %d expected, %d present in folder", folder["path"],
                     folder_summary["expected_in_target"], len(existing))
    if result.get("attachments"):
        report["attachments"] = verify_attachments(ctx, tgt, tp, result["attachments"])
    if result.get("step_files"):
        report["step_files"] = verify_step_files(ctx, tgt, tp, result["step_files"])
    if result.get("approvals"):
        report["approvals"] = verify_approvals(ctx, tgt, tp, result["approvals"])
    if result.get("releases"):
        report["releases"] = verify_releases(ctx, tgt, tp, result["releases"])
    if result.get("cycles"):
        report["cycles"] = verify_cycles(ctx, tgt, tp, result["cycles"])
    if result.get("runs"):
        report["runs"] = verify_runs(ctx, tgt, tp, result["runs"])
    if result.get("logs"):
        report["logs"] = verify_logs(ctx, tgt, tp, result["logs"])
    lg = report.get("logs") or {"missing": [], "different": [], "attachments_missing": [], "steps_different": [], "run_status": [], "errors": []}
    rn = report.get("runs") or {"missing": [], "wrong_test_case": [], "files_missing": [], "errors": []}
    cyc = report.get("cycles") or {"missing": [], "name_mismatch": [], "errors": []}
    rels = report.get("releases") or {"missing": [], "date_mismatch": [], "builds_missing": [], "errors": []}
    apps = report.get("approvals") or {"not_approved": [], "errors": []}
    atts = report.get("attachments") or {"missing": [], "errors": []}
    sfs = report.get("step_files") or {"missing": [], "errors": []}
    report["passed"] = not (report["missing"] or report["name_mismatch"] or report["step_mismatch"] or report["errors"]
                            or atts["missing"] or atts["errors"] or sfs["missing"] or sfs["errors"] or apps["not_approved"] or apps["errors"]
                            or lg["missing"] or lg["different"] or lg["attachments_missing"] or lg["steps_different"] or lg["run_status"] or lg["errors"] or rn["missing"] or rn["wrong_test_case"] or rn["files_missing"] or rn["errors"] or cyc["missing"] or cyc["name_mismatch"] or cyc["errors"] or rels["missing"] or rels["date_mismatch"] or rels["builds_missing"] or rels["errors"])
    ctx.write_json("verify.json", report)
    return report
