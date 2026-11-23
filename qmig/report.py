"""Human-readable reports (Markdown) and spreadsheet-friendly CSV files."""
from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path

from .runlog import RunContext

NOT_COVERED = [
    "Requirements themselves (not migrated); test case -> requirement links are handled separately by `python -m qmig links` once the requirements exist in the target",
    "Run fields such as planned dates and priority, defects (releases, builds, cycles, suites, runs and test logs are covered when requested with --releases / --cycles / --runs / --logs)",
    "Attachments of requirements, test steps, runs and executions; comments, version history, parameters/data sets, called test cases",
    "Assigned To (user IDs differ between instances) - configurable with --skip-fields",
    "Test case properties whose field or option does not exist in the target project (listed in Issues)",
]


def _csv(path: Path, rows: list[dict], columns: list[str]) -> None:
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:  # BOM so Excel on Windows opens UTF-8
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({c: (" | ".join(row[c]) if isinstance(row.get(c), list) else row.get(c, "")) for c in columns})


def _table(headers: list[str], rows: list[list]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines += ["| " + " | ".join(str(c).replace("|", "\\|").replace("\n", " ") for c in row) + " |" for row in rows]
    return lines


def _counts_table(title: str, counts: dict) -> list[str]:
    if not counts:
        return [f"**{title}:** none", ""]
    return [f"**{title}**", ""] + _table(["Status", "Count"], [[k, v] for k, v in sorted(counts.items())]) + [""]


def _header(ctx: RunContext, data: dict, title: str) -> list[str]:
    s, t = data["source"], data["target"]
    scope = data["scope"]
    return [
        f"# {title}", "",
        f"- Run: `{ctx.run_id}`",
        f"- Source: **{s['instance']}** ({s['url']}) project {s['project_id']} - {s.get('project')}",
        f"- Target: **{t['instance']}** ({t['url']}) project {t['project_id']} - {t.get('project')}",
        f"- Scope: module {scope.get('module_id') or 'all'}, target parent {scope.get('target_parent_id') or 'root'}, "
        f"limit {scope.get('limit') or 'none'}",
        "",
    ]


def _issues(data: dict) -> list[str]:
    issues = data.get("issues") or []
    if not issues:
        return ["## Issues", "", "No issues found.", ""]
    order = {"ERROR": 0, "WARN": 1, "INFO": 2}
    rows = sorted(issues, key=lambda i: order.get(i["severity"], 3))
    return ["## Issues", ""] + _table(["Severity", "Area", "Message"], [[i["severity"], i["area"], i["message"]] for i in rows]) + [""]


def _api(data: dict) -> list[str]:
    lines = ["## API usage", ""]
    rows = [[name, st["requests"], st["retries"], st["errors"], st["writes"], st["blocked_writes"]]
            for name, st in (data.get("api") or {}).items()]
    return lines + _table(["Instance", "Requests", "Retries", "Errors", "Writes", "Blocked writes"], rows) + [""]


def _limits() -> list[str]:
    return ["## Not covered by this run", ""] + [f"- {item}" for item in NOT_COVERED] + [""]


def _write_tables(ctx: RunContext, folders: list[dict], testcases: list[dict]) -> None:
    _csv(ctx.dir / "folders.csv", folders, ["idx", "source_id", "path", "status" if folders and "status" in folders[0] else "action", "context_only", "target_id", "error"])
    _csv(ctx.dir / "testcases.csv", testcases,
         ["n", "source_id", "source_pid", "name", "path", "status" if testcases and "status" in testcases[0] else "action",
          "target_id", "target_pid", "source_steps", "steps", "attachment_count", "warnings", "error"])


def _attachment_rows(testcases: list[dict]) -> list[dict]:
    return [{"test_case": r["name"], "source_test_case_id": r["source_id"], "source_attachment_id": a["id"],
             "name": a["name"], "content_type": a["content_type"], "already_copied": a["done"], "action": r["action"]}
            for r in testcases for a in r.get("attachments") or []]


def _release_lines(rows: list[dict], counts: dict, csv_dir: Path, result: bool) -> list[str]:
    key = "result" if result else "action"
    flat = [{"release": r["name"], "source_id": r["source_id"], "start_date": r.get("start_date"), "end_date": r.get("end_date"),
             "status": r.get("status"), "build": "", key: r[key], "target_id": r.get("target_id"),
             "error": r.get("error", ""), "note": r.get("status_note", "")} for r in rows]
    for r in rows:
        flat += [{"release": r["name"], "source_id": b["source_id"], "build": b["name"], key: b[key if result else "action"],
                  "target_id": b.get("target_id"), "error": b.get("error", "")} for b in r["builds"]]
    _csv(csv_dir / "releases.csv", flat, ["release", "source_id", "start_date", "end_date", "status", "build", key,
                                         "target_id", "error", "note"])
    lines = _counts_table("Releases", counts.get("releases", {})) + _counts_table("Builds", counts.get("builds", {}))
    lines += _table(["Release", "Start", "End", "Status", "Builds", "Result" if result else "Action"],
                    [[r["name"], (r.get("start_date") or "")[:10], (r.get("end_date") or "")[:10], r.get("status") or "",
                      len(r["builds"]), r[key]] for r in rows][:200]) + [""]
    problems = [r for r in rows if r.get("error") or r.get("status_note")]
    if problems:
        lines += ["## Release problems", ""] + _table(["Release", "Problem"], [[r["name"], r.get("error") or r["status_note"]]
                                                                                for r in problems]) + [""]
    return lines


def _cycle_lines(rows: list[dict], counts: dict, csv_dir: Path, result: bool) -> list[str]:
    key = "result" if result else "action"
    _csv(csv_dir / "cycles.csv", [{"kind": r["kind"], "path": r["path"], "source_id": r["source_id"], key: r[key],
                                    "target_id": r.get("target_id"), "error": r.get("error", "")} for r in rows],
         ["kind", "path", "source_id", key, "target_id", "error"])
    lines = _counts_table("Test cycles", counts.get("cycles", {})) + _counts_table("Test suites", counts.get("suites", {}))
    lines += _table(["Type", "Path", "Result" if result else "Action"], [[r["kind"], r["path"] + (" (context only)" if r.get("context_only") else ""), r[key]] for r in rows][:300]) + [""]
    problems = [r for r in rows if r.get("error")]
    if problems:
        lines += ["## Cycle and suite problems", ""] + _table(["Path", "Problem"], [[r["path"], r["error"]] for r in problems]) + [""]
    return lines


def _run_lines(rows: list[dict], counts: dict, csv_dir: Path, result: bool) -> list[str]:
    key = "result" if result else "action"
    _csv(csv_dir / "runs.csv", [{"path": r["path"], "source_id": r["source_id"], "source_test_case_id": r.get("test_case_source_id"),
                                  key: r[key], "target_id": r.get("target_id"), "error": r.get("error", "")} for r in rows],
         ["path", "source_id", "source_test_case_id", key, "target_id", "error"])
    lines = _counts_table("Test runs", counts.get("runs", {}))
    if result:
        files = Counter(a["status"] for r in rows for a in r.get("attachment_results") or [])
        if files:
            lines += _counts_table("Test run files", dict(files))
    elif counts.get("run_attachments", {}).get("to-copy"):
        lines += [f"{counts['run_attachments']['to-copy']} file(s) attached to runs will be copied.", ""]
    notes = [r for r in rows if r.get("assignee_note")]
    if notes:
        lines += [f"## Runs without the previous Assigned To note ({len(notes)})", "", notes[0]["assignee_note"], ""]
    skipped = [r for r in rows if r.get("error")]
    if skipped:
        lines += [f"## Runs not migrated or failed ({len(skipped)})", ""]
        lines += _table(["Run", "Source test case", "Reason"], [[r["path"], r.get("test_case_source_id"), r["error"]] for r in skipped[:200]]) + [""]
    return lines


def _log_lines(rows: list[dict], counts: dict, csv_dir: Path, result: bool) -> list[str]:
    key = "result" if result else "action"
    _csv(csv_dir / "logs.csv", [{"run": r["run_path"], "n": f"{r['n']}/{r['of']}", "source_id": r["source_id"], "status": r["status"],
                                  "exe_start_date": r.get("exe_start_date"), "exe_end_date": r.get("exe_end_date"), "tester": r.get("tester"),
                                  "files": len(r.get("attachments") or []), key: r[key], "target_id": r.get("target_id"),
                                  "error": r.get("error", "")} for r in rows],
         ["run", "n", "source_id", "status", "exe_start_date", "exe_end_date", "tester", "files", key, "target_id", "error"])
    lines = _counts_table("Test logs", counts.get("logs", {}))
    if result:
        files = Counter(a["status"] for r in rows for a in r.get("attachment_results") or [])
        if files:
            lines += _counts_table("Test log files", dict(files))
    notes = [r for r in rows if r.get("steps_note")]
    if notes:
        lines += [f"## Step results with a problem ({len(notes)})", ""]
        lines += _table(["Run", "Log", "Note"], [[r["run_path"], f"{r['n']}/{r['of']}", r["steps_note"]] for r in notes[:200]]) + [""]
    problems = [r for r in rows if r.get("error")]
    if problems:
        lines += [f"## Logs not migrated or failed ({len(problems)})", ""]
        lines += _table(["Run", "Log", "Reason"], [[r["run_path"], f"{r['n']}/{r['of']}", r["error"]] for r in problems[:200]]) + [""]
    return lines


def write_plan_report(ctx: RunContext, plan: dict) -> Path:
    for row in plan["folders"]:
        row.setdefault("error", "")
    for row in plan["testcases"]:
        row.setdefault("error", "")
    lines = _header(ctx, plan, "qmig dry-run plan (nothing was written to the target)")
    lines += ["## Summary", ""]
    lines += _counts_table("Folders", plan["counts"]["folders"])
    lines += _counts_table("Test cases", plan["counts"]["testcases"])
    if "approvals" in plan["counts"]:
        lines += _counts_table("Approvals", plan["counts"]["approvals"])
    if "step_files" in plan["counts"]:
        lines += _counts_table("Step files", plan["counts"]["step_files"])
    if "attachments" in plan["counts"]:
        lines += _counts_table("Attachments (test cases)", plan["counts"]["attachments"])
        _csv(ctx.dir / "attachments.csv", _attachment_rows(plan["testcases"]),
             ["test_case", "source_test_case_id", "source_attachment_id", "name", "content_type", "already_copied", "action"])
    if plan.get("releases"):
        lines += ["## Releases and builds", "", "`reuse` = a release with this name already exists in the target and is only mapped.", ""]
        lines += _release_lines(plan["releases"], plan["counts"], ctx.dir, False)
    if plan.get("cycles"):
        lines += ["## Test cycles and suites", "", "`reuse` = one with this name already exists under the target parent and is only mapped.", ""]
        lines += _cycle_lines(plan["cycles"], plan["counts"], ctx.dir, False)
    if plan.get("runs"):
        lines += ["## Test runs", "", "`skip-no-testcase` = the run's test case is not migrated (for example in the Recycle Bin); full list in `runs.csv`.", ""]
        lines += _run_lines(plan["runs"], plan["counts"], ctx.dir, False)
    if plan.get("logs"):
        lines += ["## Test logs", "", "Logs are created oldest first. `skip-older` = not selected (`--logs last`); full list in `logs.csv`.", ""]
        lines += _log_lines(plan["logs"], plan["counts"], ctx.dir, False)
    parents = sum(1 for f in plan["folders"] if f.get("context_only"))
    if parents:
        lines += [f"{parents} parent folder(s) are created empty (`context_only` in `folders.csv`) so the migrated folder keeps "
                  "its place; their test cases are not migrated. Use `--without-parents` to avoid this.", ""]
    lines += ["Actions: `create` = would be created, `reuse`/`reuse-mapped` = folder already in target, "
              "`skip-mapped` = migrated by an earlier run, `skip-exists` = same name already in the target folder, "
              "`deferred` = beyond `--limit`.", ""]
    lines += _issues(plan)
    per_folder = Counter()
    for row in plan["testcases"]:
        if row["action"] == "create":
            per_folder[row["path"]] += 1
    lines += ["## Test cases to create per folder", ""]
    lines += _table(["Folder", "Test cases"], [[p, n] for p, n in sorted(per_folder.items())][:200]) if per_folder else ["None."]
    lines += ["", "## Next step", "",
              f"Review this report, `folders.csv` and `testcases.csv`, then run:", "",
              f"    python -m qmig apply --plan {ctx.run_id}", ""]
    lines += _api(plan) + _limits()
    lines += ["## Files", "", "`plan.json`, `testcases.jsonl` (payloads), `folders.csv`, `testcases.csv`, `events.jsonl`, `run.log`", ""]
    _write_tables(ctx, plan["folders"], plan["testcases"])
    path = ctx.dir / "report.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def write_apply_report(ctx: RunContext, result: dict) -> Path:
    lines = _header(ctx, result, "qmig apply report")
    lines += [f"- Based on plan: `{result['plan_run_id']}`", ""]
    if result.get("aborted"):
        lines += [f"> **ABORTED:** {result['aborted']}", ""]
    lines += ["## Summary", ""]
    lines += _counts_table("Folders", result["counts"]["folders"])
    lines += _counts_table("Test cases", result["counts"]["testcases"])
    if "approvals" in result["counts"]:
        lines += _counts_table("Approvals", result["counts"]["approvals"])
        _csv(ctx.dir / "approvals.csv", result["approvals"], ["n", "name", "source_id", "target_id", "source_version", "status", "error"])
        bad = [r for r in result["approvals"] if r["status"] in ("failed", "skipped")]
        if bad:
            lines += ["## Approval problems", ""] + _table(["Test case", "Status", "Error"], [[r["name"], r["status"], r["error"]] for r in bad[:100]]) + [""]
    att_failed = [r for r in result.get("attachments") or [] if r["status"] in ("failed", "too-large")]
    if "attachments" in result["counts"]:
        lines += _counts_table("Attachments", result["counts"]["attachments"])
        _csv(ctx.dir / "attachments.csv", result["attachments"],
             ["test_case_n", "test_case", "source_test_case_id", "target_test_case_id", "source_attachment_id", "name",
              "status", "bytes", "target_attachment_id", "error"])
    sf_failed = [r for r in result.get("step_files") or [] if r["status"] in ("failed", "too-large")]
    if "step_files" in result["counts"]:
        lines += _counts_table("Step files", result["counts"]["step_files"])
        _csv(ctx.dir / "step_files.csv", result["step_files"],
             ["test_case_n", "test_case", "source_test_case_id", "target_test_case_id", "order", "source_step_id", "target_step_id",
              "source_attachment_id", "name", "status", "error"])
    if sf_failed:
        lines += [f"## Step file problems ({len(sf_failed)})", ""]
        lines += _table(["Test case", "Step", "File", "Status", "Error"],
                        [[r["test_case"], r["order"], r["name"], r["status"], r["error"]] for r in sf_failed[:100]]) + [""]
    if att_failed:
        lines += [f"## Attachment problems ({len(att_failed)})", ""]
        lines += _table(["Test case", "File", "Status", "Error"],
                        [[r["test_case"], r["name"], r["status"], r["error"]] for r in att_failed[:100]]) + [""]
    if result.get("releases"):
        lines += ["## Releases and builds", ""] + _release_lines(result["releases"], result["counts"], ctx.dir, True)
    if result.get("cycles"):
        lines += ["## Test cycles and suites", ""] + _cycle_lines(result["cycles"], result["counts"], ctx.dir, True)
    if result.get("runs"):
        lines += ["## Test runs", ""] + _run_lines(result["runs"], result["counts"], ctx.dir, True)
    if result.get("logs"):
        lines += ["## Test logs", ""] + _log_lines(result["logs"], result["counts"], ctx.dir, True)
    failed = [r for r in result["testcases"] if r["status"] == "failed"]
    failed_folders = [r for r in result["folders"] if r["status"] in ("failed", "skipped")]
    if failed_folders:
        lines += ["## Folder failures", ""] + _table(["Folder", "Status", "Error"], [[r["path"], r["status"], r["error"]] for r in failed_folders]) + [""]
    if failed:
        lines += [f"## Failed test cases ({len(failed)})", ""]
        lines += _table(["Source", "Name", "Folder", "Error"],
                        [[r["source_pid"] or r["source_id"], r["name"], r["path"], r["error"]] for r in failed[:100]])
        if len(failed) > 100:
            lines += ["", f"... {len(failed) - 100} more in `testcases.csv`."]
        lines += [""]
    lines += _issues(result)
    lines += ["## Next step", "", "Check that everything is in the target:", "",
              f"    python -m qmig verify --run {ctx.run_id}", ""]
    if failed or att_failed:
        lines += ["Failed items are not recorded as migrated; fix the cause and run `apply` with the same plan again "
                  "(created items are skipped).", ""]
    lines += _api(result) + _limits()
    lines += ["## Files", "", "`result.json`, `folders.csv`, `testcases.csv`, `attachments.csv` (with attachments), `events.jsonl`, `run.log`", ""]
    _write_tables(ctx, result["folders"], result["testcases"])
    path = ctx.dir / "report.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def write_verify_report(ctx: RunContext, report: dict) -> Path:
    t = report["target"]
    verdict = "PASSED" if report["passed"] else "FAILED"
    lines = [f"# qmig verify report - {verdict}", "",
             f"- Run: `{ctx.run_id}` (checks apply run `{report['apply_run_id']}`)",
             f"- Target: **{t['instance']}** project {t['project_id']} - {t.get('project')}", "",
             "## Summary", "",
             f"- Test cases checked: {report['checked']}", f"- Found and matching: {report['ok']}",
             f"- Missing in target: {len(report['missing'])}", f"- Name differs: {len(report['name_mismatch'])}",
             f"- Step count differs: {len(report['step_mismatch'])}", f"- Folders that could not be read: {len(report['errors'])}"]
    if "attachments" in report:
        a = report["attachments"]
        lines += [f"- Attachments checked: {a['checked']}, found: {a['ok']}, missing: {len(a['missing'])}, "
                  f"test cases that could not be read: {len(a['errors'])}"]
    lines += [""]
    for title, key, cols in (("Missing", "missing", ["name", "target_id", "path"]),
                             ("Name differs", "name_mismatch", ["expected", "actual", "target_id"]),
                             ("Step count differs", "step_mismatch", ["name", "expected_steps", "actual_steps", "target_id"])):
        if report[key]:
            lines += [f"## {title}", ""] + _table(cols, [[r[c] for c in cols] for r in report[key][:100]]) + [""]
    if "step_files" in report:
        sf = report["step_files"]
        lines += ["## Step files", "", f"{sf['checked']} checked, {sf['ok']} ok, {len(sf['missing'])} missing, {len(sf['errors'])} error(s)", ""]
        if sf["missing"]:
            lines += _table(["Test case (target id)", "Step", "File", "Expected", "Present"],
                            [[r["test_case_id"], r["step"], r["name"], r["expected"], r["present"]] for r in sf["missing"][:100]]) + [""]
    if report.get("attachments", {}).get("missing"):
        lines += ["## Missing attachments", ""] + _table(
            ["Target test case", "File", "Expected", "Present"],
            [[r["test_case_id"], r["name"], r["expected"], r["present"]] for r in report["attachments"]["missing"][:100]]) + [""]
    if "approvals" in report:
        a = report["approvals"]
        lines += [f"- Approvals checked: {a['checked']}, approved: {a['ok']}, still draft: {len(a['not_approved'])}", ""]
        if a["not_approved"]:
            lines += ["## Not approved", ""] + _table(["name", "target_id", "version"], [[r["name"], r["target_id"], r["version"]] for r in a["not_approved"][:100]]) + [""]
        lines += [f"- {e}" for e in a["errors"]]
    if "releases" in report:
        rel = report["releases"]
        lines += [f"- Releases checked: {rel['checked']}, ok: {rel['ok']}, missing: {len(rel['missing'])}, "
                  f"dates differ: {len(rel['date_mismatch'])}, builds missing: {len(rel['builds_missing'])}", ""]
        for title, key, cols in (("Missing releases", "missing", ["name", "target_id"]),
                                 ("Releases with different dates", "date_mismatch", ["name", "expected", "actual"]),
                                 ("Missing builds", "builds_missing", ["release", "build"])):
            if rel[key]:
                lines += [f"## {title}", ""] + _table(cols, [[r[c] for c in cols] for r in rel[key]]) + [""]
        lines += [f"- {e}" for e in rel["errors"]] + [""]
    if "logs" in report:
        lg = report["logs"]
        lines += [f"- Logs checked: {lg['checked']}, ok: {lg['ok']}, missing: {len(lg['missing'])}, different: {len(lg['different'])}, "
                  f"files missing: {len(lg['attachments_missing'])}, step results that differ: {len(lg['steps_different'])}, runs with another status than their newest log: {len(lg['run_status'])}", ""]
        for title, key, cols in (("Missing logs", "missing", ["run", "log", "target_id"]),
                                 ("Logs that differ", "different", ["run", "log", "problem"]),
                                 ("Missing log files", "attachments_missing", ["run", "log", "name"]),
                                 ("Step results that differ", "steps_different", ["run", "log", "step", "expected", "actual"]),
                                 ("Runs whose status differs from their newest log", "run_status", ["run", "expected", "actual"])):
            if lg[key]:
                lines += [f"## {title}", ""] + _table(cols, [[r[c] for c in cols] for r in lg[key]]) + [""]
        lines += [f"- {e}" for e in lg["errors"]] + [""]
    if "runs" in report:
        rn = report["runs"]
        lines += [f"- Runs checked: {rn['checked']}, ok: {rn['ok']}, missing: {len(rn['missing'])}, "
                  f"wrong test case: {len(rn['wrong_test_case'])}, files missing: {len(rn['files_missing'])}", ""]
        for title, key, cols in (("Missing runs", "missing", ["path", "target_id"]),
                                 ("Runs pointing at another test case", "wrong_test_case", ["path", "expected", "actual"]),
                                 ("Missing run files", "files_missing", ["path", "name"])):
            if rn[key]:
                lines += [f"## {title}", ""] + _table(cols, [[r[c] for c in cols] for r in rn[key]]) + [""]
        lines += [f"- {e}" for e in rn["errors"]] + [""]
    if "cycles" in report:
        cyc = report["cycles"]
        lines += [f"- Cycles and suites checked: {cyc['checked']}, ok: {cyc['ok']}, missing: {len(cyc['missing'])}, "
                  f"name differs: {len(cyc['name_mismatch'])}", ""]
        for title, key, cols in (("Missing cycles and suites", "missing", ["kind", "path", "target_id"]),
                                 ("Cycles and suites with a different name", "name_mismatch", ["kind", "path", "expected", "actual"])):
            if cyc[key]:
                lines += [f"## {title}", ""] + _table(cols, [[r[c] for c in cols] for r in cyc[key]]) + [""]
        lines += [f"- {e}" for e in cyc["errors"]] + [""]
    if report["errors"]:
        lines += ["## Errors", ""] + [f"- {e}" for e in report["errors"]] + [""]
    lines += ["## Per folder", ""] + _table(
        ["Folder", "Source test cases", "Expected in target", "Now in target folder"],
        [[f["path"], f["source_count"], f["expected_in_target"], f["target_count"]] for f in report["folders"]]) + [""]
    path = ctx.dir / "report.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _mappings(data: dict) -> list[str]:
    m = data.get("mappings") or {}
    lines: list[str] = []
    if "statuses" in m:
        lines += ["## Execution statuses (matched by name)", ""]
        lines += _table(["Status", "Source id", "Target id"], [[r["name"], r["source_id"], r["target_id"] or "MISSING"]
                                                              for r in m["statuses"]["rows"]]) + [""]
    if "releases" in m:
        rows = m["releases"]["rows"]
        lines += ["## Releases (matched by name)", ""]
        lines += _table(["Release", "Start", "End", "Action"], [[r["name"], r["start_date"], r["end_date"], r["action"]]
                                                               for r in rows]) if rows else ["None."]
        lines += [""]
    return lines


def write_preflight_report(ctx: RunContext, data: dict) -> Path:
    lines = _header(ctx, data, "qmig preflight report (read-only)")
    lines += _issues(data) + _mappings(data) + _api(data)
    lines += ["## Next step", "", "Run a dry-run plan:", "",
              f"    python -m qmig plan --source {data['source']['instance']} --target {data['target']['instance']} "
              f"--source-project {data['source']['project_id']} --target-project {data['target']['project_id']}", ""]
    path = ctx.dir / "report.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
