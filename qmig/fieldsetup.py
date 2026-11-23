"""Create the test case custom fields that exist in the source project but not in the target project.

Run this phase first: ``plan`` / ``apply`` / ``verify`` for fields, then the test case upload.  The upload maps field
values by label (see fields.py), so a field that is missing in the target silently loses its values.

``plan``    dry run: compare the source and target test case fields (label, type, dropdown options) and list what
            ``apply`` would create and what needs a manual change (a field that exists with different options or type).
``apply``   create the missing custom fields in the target project (idempotent: a field that exists is skipped).
``verify``  read the target fields back and check that every created field exists with the same type and options.

Only non-system, active custom fields are created.  Existing target fields are never changed.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from .client import ApiError, QtestClient
from .fields import DEFAULT_SKIP_FIELDS, _key
from .report import _api, _counts_table, _csv, _issues, _table
from .runindex import find_run_dir
from .runlog import RunContext

MAX_CONSECUTIVE_FAILURES = 3
# Decisions: Test Phase is left as it is; Team Specific and Test Scenario wait for the QE DSU. The fields in
# fields.METADATA_FIELDS are not created (their values go into the description).
FIELDS_PLAN_SKIP = DEFAULT_SKIP_FIELDS + ("Test Phase", "Team Specific", "Test Scenario")
COLUMNS = ["action", "label", "attribute_type", "options", "required", "detail"]
ACTIONS = ("`create` = missing in the target, apply creates it; `exists` = same label, type and options; `manual` = exists "
           "with other options or type, change it in the target project by hand (it is never changed here).")


class FieldSetupError(RuntimeError):
    pass


def _labels(field: dict) -> list[str]:
    return [str(o.get("label")) for o in field.get("allowed_values") or [] if o.get("label") is not None and o.get("is_active", True)]


def _row(action: str, field: dict, detail: str = "") -> dict:
    return {"action": action, "label": field.get("label"), "attribute_type": field.get("attribute_type"),
            "data_type": field.get("data_type"), "options": _labels(field), "required": bool(field.get("required")),
            "defaults": [o.get("label") for o in field.get("allowed_values") or [] if o.get("is_default")],
            "multiple": bool(field.get("multiple")), "detail": detail}


def _project_name(client: QtestClient, project_id: int) -> str:
    try:
        return client.get_project(project_id).get("name") or ""
    except ApiError:
        return ""


def _ends(src: QtestClient, tgt: QtestClient, sp: int, tp: int) -> dict:
    return {"source": {"instance": src.name, "url": src.instance.web_url, "project_id": sp, "project": _project_name(src, sp)},
            "target": {"instance": tgt.name, "url": tgt.instance.web_url, "project_id": tp, "project": _project_name(tgt, tp)}}


def plan_fields(ctx: RunContext, src: QtestClient, tgt: QtestClient, sp: int, tp: int,
                skip_fields: tuple[str, ...] = FIELDS_PLAN_SKIP) -> dict:
    skip = {_key(s) for s in skip_fields}
    target = {_key(f.get("label")): f for f in tgt.test_case_fields(tp) if f.get("label")}
    rows, issues = [], []
    for field in src.test_case_fields(sp):
        label = field.get("label") or ""
        if not label or field.get("system_field") or field.get("is_active") is False or _key(label) in skip:
            continue
        have = target.get(_key(label))
        if have is None:
            rows.append(_row("create", field))
            continue
        problems = []
        if have.get("attribute_type") != field.get("attribute_type"):
            problems.append(f"type {field.get('attribute_type')} in the source, {have.get('attribute_type')} in the target")
        missing = [o for o in _labels(field) if _key(o) not in {_key(x) for x in _labels(have)}]
        if missing:
            problems.append("options missing in the target: " + ", ".join(missing))
        dupes = sorted({o for o in _labels(have) if [_key(x) for x in _labels(have)].count(_key(o)) > 1})
        if dupes:
            problems.append("duplicate options in the target: " + ", ".join(dupes))
        rows.append(_row("manual" if problems else "exists", field, "; ".join(problems)))
        if problems:
            issues.append({"severity": "WARN", "area": "fields", "message": f"Field '{label}': " + "; ".join(problems)})
    plan = {"run_id": ctx.run_id, **_ends(src, tgt, sp, tp), "scope": {}, "rows": rows, "issues": issues,
            "counts": {"actions": dict(Counter(r["action"] for r in rows))},
            "api": {src.name: src.stats, tgt.name: tgt.stats}}
    ctx.write_json("fields_plan.json", plan)
    return plan


def load_fields_plan(runs_dir: Path, run_id: str) -> dict:
    path = find_run_dir(runs_dir, run_id) / "fields_plan.json"
    if not path.is_file():
        raise FieldSetupError(f"No fields plan found at {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _body(row: dict) -> dict:
    body = {"label": row["label"], "data_type": row["data_type"], "required": row["required"]}
    if row["options"]:
        body["allowed_values"] = [{"label": o, "is_default": o in row["defaults"], "is_active": True} for o in row["options"]]
    return body


def apply_fields(ctx: RunContext, tgt: QtestClient, plan: dict) -> dict:
    if plan["target"].get("url") and tgt.instance.web_url != plan["target"]["url"]:
        raise FieldSetupError(f"The target URL differs from the reviewed fields plan ({plan['target']['url']}); "
                              "restore it or create and review a new plan")
    tp = plan["target"]["project_id"]
    existing = {_key(f.get("label")) for f in tgt.test_case_fields(tp)}
    rows, failures, aborted = [], 0, ""
    for row in plan["rows"]:
        out = {**row}
        if row["action"] != "create":
            rows.append(out)
            continue
        if aborted:
            out["status"] = "not-attempted"
        elif _key(row["label"]) in existing:
            out["status"] = "skip-exists"
        else:
            try:
                try:
                    tgt.create_test_case_field(tp, _body(row))
                except ApiError as exc:
                    if not exc.ambiguous:
                        raise
                    if any(_key(f.get("label")) == _key(row["label"]) for f in tgt.test_case_fields(tp)):
                        ctx.log.warning("A write was cut off but succeeded: field '%s' exists", row["label"])
                    else:
                        tgt.create_test_case_field(tp, _body(row))  # it did not happen: once more
                out["status"], failures = "created", 0
                ctx.log.info("Created field '%s' (%s)", row["label"], row["attribute_type"])
            except ApiError as exc:
                out["status"], out["error"] = "failed", f"HTTP {exc.status}: {exc.detail}" if exc.status else str(exc.detail)
                failures += 1
                ctx.log.error("Cannot create field '%s': %s", row["label"], out["error"])
                if failures >= MAX_CONSECUTIVE_FAILURES:
                    aborted = f"{failures} fields in a row could not be created"
        ctx.event("field", out.get("status", row["action"]), label=row["label"])
        rows.append(out)
    counts = Counter(r.get("status") or r["action"] for r in rows)
    result = {"run_id": ctx.run_id, "plan_run_id": plan["run_id"], "source": plan["source"], "target": plan["target"],
              "rows": rows, "aborted": aborted, "issues": [], "counts": {"actions": dict(counts)},
              "api": {tgt.name: tgt.stats}}
    ctx.write_json("fields_result.json", result)
    return result


def load_fields_result(runs_dir: Path, run_id: str) -> dict:
    path = find_run_dir(runs_dir, run_id) / "fields_result.json"
    if not path.is_file():
        raise FileNotFoundError(f"No fields apply result at {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def verify_fields(ctx: RunContext, tgt: QtestClient, result: dict) -> dict:
    target = {_key(f.get("label")): f for f in tgt.test_case_fields(result["target"]["project_id"])}
    checked, bad = 0, []
    for row in result["rows"]:
        if row.get("status") not in ("created", "skip-exists"):
            continue
        checked += 1
        have = target.get(_key(row["label"]))
        if have is None:
            bad.append({"label": row["label"], "problem": "missing in the target"})
        elif have.get("attribute_type") != row["attribute_type"]:
            bad.append({"label": row["label"], "problem": f"type {have.get('attribute_type')}, expected {row['attribute_type']}"})
        elif {_key(o) for o in _labels(have)} != {_key(o) for o in row["options"]}:
            bad.append({"label": row["label"], "problem": "options differ from the source"})
    report = {"run_id": ctx.run_id, "apply_run_id": result["run_id"], "source": result["source"], "target": result["target"],
              "checked": checked, "ok": checked - len(bad), "bad": bad, "passed": not bad}
    ctx.write_json("fields_verify.json", report)
    return report


# ---------------------------------------------------------------------------- reports
def _ends_lines(data: dict) -> list[str]:
    s, t = data["source"], data["target"]
    return [f"- Source: **{s['instance']}** project {s['project_id']}" + (f" - {s['project']}" if s.get("project") else ""),
            f"- Target: **{t['instance']}** project {t['project_id']}" + (f" - {t['project']}" if t.get("project") else "")]


def write_fields_plan_report(ctx: RunContext, plan: dict) -> Path:
    lines = ["# qmig fields - dry-run plan (nothing was written to the target)", "", f"- Run: `{ctx.run_id}`",
             *_ends_lines(plan), "", "## Summary", ""] + _counts_table("Test case fields", plan["counts"]["actions"]) + [ACTIONS, ""]
    shown = [r for r in plan["rows"] if r["action"] != "exists"]
    if shown:
        lines += ["## To do", ""] + _table(["Action", "Field", "Type", "Options", "Required", "Detail"],
                                         [[r["action"], r["label"], r["attribute_type"], ", ".join(r["options"]) or "none",
                                           "yes" if r["required"] else "no", r["detail"]] for r in shown]) + [""]
    lines += _issues(plan)
    lines += ["## Next step", "", "Review this report, then run:", "", f"    python -m qmig fields apply --plan {ctx.run_id}", ""]
    lines += _api(plan)
    _csv(ctx.dir / "fields.csv", plan["rows"], COLUMNS)
    path = ctx.dir / "report.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def write_fields_apply_report(ctx: RunContext, result: dict) -> Path:
    lines = ["# qmig fields - apply report", "", f"- Run: `{ctx.run_id}` (plan `{result['plan_run_id']}`)",
             *_ends_lines(result), ""]
    if result["aborted"]:
        lines += [f"**STOPPED EARLY: {result['aborted']}**", ""]
    lines += ["## Summary", ""] + _counts_table("Test case fields", result["counts"]["actions"])
    failed = [r for r in result["rows"] if r.get("status") in ("failed", "not-attempted")]
    if failed:
        lines += ["## Failed", ""] + _table(["Status", "Field", "Error"], [[r["status"], r["label"], r.get("error", "")] for r in failed]) + [""]
    lines += ["## Next step", "", f"    python -m qmig fields verify --run {ctx.run_id}", ""] + _api(result)
    _csv(ctx.dir / "fields.csv", [{**r, "action": r.get("status") or r["action"], "detail": r.get("error") or r["detail"]}
                                  for r in result["rows"]], COLUMNS)
    path = ctx.dir / "report.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def write_fields_verify_report(ctx: RunContext, report: dict) -> Path:
    lines = [f"# qmig fields - verify {'PASSED' if report['passed'] else 'FAILED'}", "",
             f"- Run: `{ctx.run_id}` (checks apply run `{report['apply_run_id']}`)", *_ends_lines(report), "",
             "## Summary", "", f"- Fields checked: {report['checked']}", f"- Correct in target: {report['ok']}", ""]
    if report["bad"]:
        lines += ["## Problems", ""] + _table(["Field", "Problem"], [[b["label"], b["problem"]] for b in report["bad"]]) + [""]
    path = ctx.dir / "report.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
