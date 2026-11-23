"""Reports of the requirement-link commands (Markdown + CSV)."""
from __future__ import annotations

from pathlib import Path

from .report import _api, _counts_table, _csv, _issues, _table
from .runlog import RunContext

PLAN_COLUMNS = ["action", "matched_by", "jira_key", "requirement_name", "source_testcase_id", "target_testcase_id",
                "target_testcase_pid", "source_requirement_id", "target_requirement_id", "target_requirement_pid", "detail"]
ACTIONS = ("`link` = will be linked, `exists` = already linked in the target, `not-found` = no target requirement with this "
           "Jira key (or name), `ambiguous` = more than one target requirement matches, `tc-not-migrated` = the test case is "
           "not in the target, `no-identifier` = the requirement has neither key nor name.")


def _ends(data: dict) -> list[str]:
    s, t = data["source"], data["target"]
    return [f"- Source: **{s['instance']}** project {s['project_id']}" + (f" - {s['project']}" if s.get("project") else ""),
            f"- Target: **{t['instance']}** project {t['project_id']}" + (f" - {t['project']}" if t.get("project") else "")]


def write_capture_report(ctx: RunContext, data: dict) -> Path:
    c = data["counts"]
    lines = ["# qmig requirement links - capture (source was only read)", "", f"- Run: `{ctx.run_id}`", *_ends(data), "",
             "## Summary", "",
             f"- Migrated test cases checked: {c['testcases_checked']}",
             f"- With at least one requirement: {c['testcases_with_requirements']}",
             f"- Without requirements: {c['testcases_without_requirements']}",
             f"- Links stored: {c['links']} to {c['requirements']} distinct requirement(s)",
             f"- Requirements without a Jira key: {c['requirements_without_key']}", ""]
    lines += _issues(data)
    lines += ["## Next step", "",
              "Create the requirements in the target, then run:", "",
              f"    python -m qmig links plan --source-project {data['source']['project_id']} "
              f"--target-project {data['target']['project_id']}", ""]
    _csv(ctx.dir / "captured_links.csv", data["rows"],
         ["source_testcase_id", "source_testcase_pid", "testcase_name", "source_requirement_id", "source_requirement_pid",
          "jira_key", "requirement_name"])
    lines += ["## Files", "", "`captured_links.csv`, `events.jsonl`, `run.log`", ""]
    path = ctx.dir / "report.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def write_links_plan_report(ctx: RunContext, plan: dict) -> Path:
    actions = plan["counts"]["actions"]
    lines = ["# qmig requirement links - dry-run plan (nothing was written to the target)", "", f"- Run: `{ctx.run_id}`",
             *_ends(plan), f"- Name match when the Jira key is not found: "
             f"{'yes' if plan['scope']['name_on_key_miss'] else 'no (key only; name only for requirements without a key)'}",
             *([f"- **Key map (pilot only): {', '.join(f'{a} -> {b}' for a, b in plan['scope']['key_map'].items())}**"]
               if plan["scope"].get("key_map") else []), "",
             "## Summary", ""]
    lines += _counts_table("Links", actions)
    if plan["counts"]["matched_by"]:
        lines += _counts_table("Matched by (link + exists)", plan["counts"]["matched_by"])
    lines += [ACTIONS, ""]
    lines += _issues(plan)
    problems = [r for r in plan["rows"] if r["action"] not in ("link", "exists")]
    if problems:
        lines += ["## Not linkable", ""] + _table(
            ["Action", "Jira key", "Requirement", "Source test case", "Detail"],
            [[r["action"], r["jira_key"], r["requirement_name"][:70], r["source_testcase_id"], r["detail"]]
             for r in problems[:100]]) + [""]
        if len(problems) > 100:
            lines += [f"... and {len(problems) - 100} more in `links.csv`.", ""]
    lines += ["## Next step", "", "Review this report and `links.csv`, then run:", "",
              f"    python -m qmig links apply --plan {ctx.run_id}", ""]
    lines += _api(plan)
    lines += ["## Files", "", "`links_plan.json`, `links.csv`, `events.jsonl`, `run.log`", ""]
    _csv(ctx.dir / "links.csv", plan["rows"], PLAN_COLUMNS)
    path = ctx.dir / "report.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def write_links_apply_report(ctx: RunContext, result: dict) -> Path:
    actions = result["counts"]["actions"]
    lines = ["# qmig requirement links - apply report", "", f"- Run: `{ctx.run_id}` (plan `{result['plan_run_id']}`)",
             *_ends(result), ""]
    if result["aborted"]:
        lines += [f"**STOPPED EARLY: {result['aborted']}**", ""]
    lines += ["## Summary", ""] + _counts_table("Links", actions)
    lines += ["`linked` = created now, `skip-mapped` = created by an earlier apply of this plan, `failed` = see the error, "
              "`not-attempted` = apply stopped before reaching it. Other values are the plan's actions and were not touched.", ""]
    failed = [r for r in result["rows"] if r["status"] in ("failed", "not-attempted")]
    if failed:
        lines += ["## Failed", ""] + _table(
            ["Status", "Jira key", "Requirement", "Target test case", "Target requirement", "Error"],
            [[r["status"], r["jira_key"], r["requirement_name"][:50], r["target_testcase_id"],
              r["target_requirement_id"], r["error"]] for r in failed[:100]]) + [""]
    lines += ["## Next step", "", f"    python -m qmig links verify --run {ctx.run_id}", ""]
    lines += _api(result)
    _csv(ctx.dir / "links.csv", [{**r, "action": r["status"], "detail": r["error"] or r["detail"]} for r in result["rows"]],
         PLAN_COLUMNS)
    path = ctx.dir / "report.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def write_links_verify_report(ctx: RunContext, report: dict) -> Path:
    lines = [f"# qmig requirement links - verify {'PASSED' if report['passed'] else 'FAILED'}", "",
             f"- Run: `{ctx.run_id}` (checks apply run `{report['apply_run_id']}`)", *_ends(report), "",
             "## Summary", "", f"- Links checked: {report['checked']}", f"- Present in target: {report['ok']}",
             f"- Missing in target: {len(report['missing'])}", f"- Reads that failed: {len(report['errors'])}", ""]
    if report["missing"]:
        lines += ["## Missing", ""] + _table(
            ["Target test case", "Target requirement", "Jira key", "Requirement"],
            [[r["target_testcase_id"], r["target_requirement_id"], r["jira_key"], r["requirement_name"][:60]]
             for r in report["missing"][:100]]) + [""]
    if report["errors"]:
        lines += ["## Errors", ""] + [f"- {e}" for e in report["errors"]] + [""]
    path = ctx.dir / "report.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
