"""Re-link migrated test cases to requirements that were created in the target later.

Requirements are not migrated, but the HS test case -> requirement coverage must survive.  The only identifier a
requirement shares across the two instances is its Jira issue key (``external_properties.id``, e.g. ``POD-233``), so:

``capture``  read-only on the source: for every migrated test case, store the requirements it covers (Jira key and
             name included) in the local database.  Can run before the target requirements exist.
``plan``     dry run against the target: find each captured requirement in the target project - by Jira key first;
             by normalised name only for a requirement that has no key - and report what would be linked.
``apply``    create the links of a reviewed plan (batched, idempotent).
``verify``   read the links back from the target.

A requirement with a key is never matched by name unless ``name_on_key_miss`` is set, and anything that matches more
than one target requirement is reported instead of linked: a missing link is cheaper than a wrong one.
"""
from __future__ import annotations

import html
import json
import re
from collections import Counter
from pathlib import Path

from .client import ApiError, QtestClient
from .db import StateDb
from .runindex import find_run_dir
from .runlog import RunContext

LINK_BATCH = 100  # test cases per linked-artifacts / link request (the documented default limit)
MAX_CONSECUTIVE_FAILURES = 25

_KEY_PREFIX = re.compile(r"^\s*([A-Za-z][A-Za-z0-9_]*-\d+)(?![A-Za-z0-9_])")
_INVISIBLE = re.compile(r"[​-‏⁠﻿]")


class LinksError(RuntimeError):
    pass


def normalize_name(name) -> str:
    """Name used for the backup match: HTML entities decoded, invisible characters dropped, spaces collapsed."""
    text = _INVISIBLE.sub("", html.unescape(str(name or "")))
    return re.sub(r"\s+", " ", text).strip().casefold()


def requirement_key(requirement: dict) -> str | None:
    """Jira issue key of a requirement: the integration's ``external_properties.id``, else a key at the start of the name."""
    external = (requirement.get("external_properties") or {}).get("id")
    if external and str(external).strip():
        return str(external).strip().upper()
    match = _KEY_PREFIX.match(str(requirement.get("name") or ""))
    return match.group(1).upper() if match else None


def _is_requirement(linked: dict) -> bool:
    return "/requirements/" in str(linked.get("self") or "") or str(linked.get("pid") or "").upper().startswith("RQ-")


def _batches(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _issue(severity: str, area: str, message: str) -> dict:
    return {"severity": severity, "area": area, "message": message}


def _error_text(exc: ApiError) -> str:
    return f"HTTP {exc.status}: {exc.detail}" if exc.status else f"connection error: {exc.detail}"


def _project_name(client: QtestClient, project_id: int) -> str:
    try:
        return client.get_project(project_id).get("name") or ""
    except ApiError:
        return ""


def _ends(src: QtestClient, tgt: QtestClient, sp: int, tp: int) -> dict:
    return {"source": {"instance": src.name, "url": src.instance.web_url, "project_id": sp,
                       "project": _project_name(src, sp)},
            "target": {"instance": tgt.name, "url": tgt.instance.web_url, "project_id": tp,
                       "project": _project_name(tgt, tp)}}


# ---------------------------------------------------------------------------- capture
def capture_links(ctx: RunContext, src: QtestClient, tgt_name: str, db: StateDb, sp: int, tp: int) -> dict:
    """Store which requirements each migrated source test case covers.  Reads the source only."""
    mapped = db.mapped_testcases(src.name, sp, tgt_name, tp)
    if not mapped:
        raise LinksError(f"No migrated test cases are recorded for {src.name}:{sp} -> {tgt_name}:{tp}. "
                         "Run 'apply' for the test cases first.")
    requirements = {r["id"]: r for r in src.list_requirements(sp) if isinstance(r, dict) and "id" in r}
    ctx.log.info("Source project %s: %d requirement(s); reading links of %d migrated test case(s)",
                 sp, len(requirements), len(mapped))
    issues: list[dict] = []
    rows: list[dict] = []
    with_links = 0
    for batch in _batches(list(mapped), LINK_BATCH):
        linked = src.linked_artifacts(sp, "test-cases", [m["source_id"] for m in batch])
        for tc in batch:
            found = [o for o in linked.get(tc["source_id"], []) if _is_requirement(o)]
            links = []
            for obj in found:
                req = requirements.get(obj["id"])
                if req is None:  # not in the listing (e.g. in another branch): read it directly
                    req = src.get_requirement(sp, obj["id"]) or {}
                    requirements[obj["id"]] = req
                links.append({"requirement_id": obj["id"], "pid": req.get("pid") or obj.get("pid"),
                              "jira_key": requirement_key(req) if req else None, "name": req.get("name") or ""})
            db.replace_requirement_links(src.name, sp, tc["source_id"], links, ctx.run_id)
            with_links += bool(links)
            for link in links:
                rows.append({"source_testcase_id": tc["source_id"], "source_testcase_pid": tc["source_pid"],
                             "testcase_name": tc["name"], "source_requirement_id": link["requirement_id"],
                             "source_requirement_pid": link["pid"], "jira_key": link["jira_key"] or "",
                             "requirement_name": link["name"]})
                ctx.event("link", "captured", testcase=tc["source_id"], requirement=link["requirement_id"],
                          jira_key=link["jira_key"])
    no_key = [r for r in rows if not r["jira_key"]]
    if no_key:
        issues.append(_issue("WARN", "requirement", f"{len({r['source_requirement_id'] for r in no_key})} requirement(s) "
                             "have no Jira key; they can only be matched by name"))
    distinct = {r["source_requirement_id"] for r in rows}
    return {"run_id": ctx.run_id, "source": {"instance": src.name, "project_id": sp},
            "target": {"instance": tgt_name, "project_id": tp}, "issues": issues, "rows": rows,
            "counts": {"testcases_checked": len(mapped), "testcases_with_requirements": with_links,
                       "testcases_without_requirements": len(mapped) - with_links, "links": len(rows),
                       "requirements": len(distinct),
                       "requirements_without_key": len({r["source_requirement_id"] for r in no_key})}}


# ------------------------------------------------------------------------------ plan
def parse_key_map(text: str | None) -> dict[str, str]:
    """``ABC=SCRUM,BCA=SCRUM`` (prefix: ABC-111 -> SCRUM-111) or ``ABC-5=SCRUM-9`` (one exact key).

    For pilots whose source and target projects use different Jira projects.  Empty by default: no mapping.
    """
    rules: dict[str, str] = {}
    for part in (text or "").replace(";", ",").split(","):
        if not part.strip():
            continue
        left, sep, right = part.partition("=")
        left, right = left.strip().upper(), right.strip().upper()
        if not sep or not left or not right:
            raise LinksError(f"--key-map: expected SOURCE=TARGET (ABC=SCRUM or ABC-5=SCRUM-9), got {part.strip()!r}")
        rules[left] = right
    return rules


def map_key(key: str, rules: dict[str, str]) -> str:
    """Apply the key map: an exact key wins, then the prefix before the last dash."""
    key = (key or "").strip().upper()
    if not key or not rules:
        return key
    if key in rules:
        return rules[key]
    prefix, dash, number = key.rpartition("-")
    if dash and prefix in rules:
        return f"{rules[prefix]}-{number}"
    return key


def _target_index(requirements: list[dict]) -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
    by_key: dict[str, list[dict]] = {}
    by_name: dict[str, list[dict]] = {}
    for req in requirements:
        if not isinstance(req, dict) or "id" not in req:
            continue
        key = requirement_key(req)
        if key:
            by_key.setdefault(key, []).append(req)
        name = normalize_name(req.get("name"))
        if name:
            by_name.setdefault(name, []).append(req)
    return by_key, by_name


def resolve_requirement(link: dict, by_key: dict, by_name: dict, *, name_on_key_miss: bool = False) -> tuple[str, dict | None, str]:
    """Return ``(outcome, target requirement or None, matched_by)``.

    Outcomes: ``match``, ``not-found``, ``ambiguous``, ``no-identifier``.
    """
    key = (link.get("jira_key") or "").strip().upper()
    if key:
        found = by_key.get(key, [])
        if len(found) == 1:
            return "match", found[0], "key"
        if len(found) > 1:
            return "ambiguous", None, "key"
        if not name_on_key_miss:
            return "not-found", None, "key"
    name = normalize_name(link.get("requirement_name"))
    if not name:
        return "no-identifier", None, ""
    found = by_name.get(name, [])
    if len(found) == 1:
        return "match", found[0], "name"
    return ("ambiguous" if found else "not-found"), None, "name"


def plan_links(ctx: RunContext, src: QtestClient, tgt: QtestClient, db: StateDb, sp: int, tp: int, *,
               name_on_key_miss: bool = False, key_map: dict[str, str] | None = None) -> dict:
    """Dry run: match the captured links to target requirements and test cases.  Writes nothing."""
    links = db.requirement_links(src.name, sp)
    if not links:
        raise LinksError(f"No captured links for {src.name}:{sp}. Run 'links capture' first "
                         "(it records nothing if the migrated test cases cover no requirements).")
    target_requirements = tgt.list_requirements(tp)
    by_key, by_name = _target_index(target_requirements)
    ctx.log.info("Target project %s: %d requirement(s), %d distinct Jira key(s); %d captured link(s)",
                 tp, len(target_requirements), len(by_key), len(links))

    target_tc = {}
    for link in links:
        row = db.testcase_target(src.name, sp, link["source_testcase_id"], tgt.name, tp)
        if row:
            target_tc[link["source_testcase_id"]] = row
    existing: set[tuple[int, int]] = set()
    tc_ids = sorted({r["target_id"] for r in target_tc.values()})
    for batch in _batches(tc_ids, LINK_BATCH):
        for tc_id, objs in tgt.linked_artifacts(tp, "test-cases", batch).items():
            existing.update((tc_id, o["id"]) for o in objs if _is_requirement(o))

    rows: list[dict] = []
    for n, link in enumerate(links, start=1):
        row = {"n": n, "source_testcase_id": link["source_testcase_id"],
               "source_requirement_id": link["source_requirement_id"],
               "source_requirement_pid": link["source_requirement_pid"], "jira_key": link["jira_key"] or "",
               "requirement_name": link["requirement_name"], "target_testcase_id": None, "target_testcase_pid": None,
               "target_requirement_id": None, "target_requirement_pid": None, "matched_by": "", "action": "", "detail": ""}
        tc = target_tc.get(link["source_testcase_id"])
        if tc is None:
            row.update(action="tc-not-migrated", detail="the test case is not in the target (not migrated yet)")
        else:
            row.update(target_testcase_id=tc["target_id"], target_testcase_pid=tc["target_pid"])
            outcome, req, matched_by = resolve_requirement(
                {"jira_key": map_key(link["jira_key"], key_map or {}), "requirement_name": link["requirement_name"]},
                by_key, by_name,
                name_on_key_miss=name_on_key_miss)
            row["matched_by"] = matched_by
            mapped = map_key(link["jira_key"], key_map or {})
            if key_map and mapped and mapped != (link["jira_key"] or "").strip().upper():
                row["mapped_key"] = mapped
            if outcome == "match":
                row.update(target_requirement_id=req["id"], target_requirement_pid=req.get("pid"))
                row["action"] = "exists" if (tc["target_id"], req["id"]) in existing else "link"
            elif outcome == "ambiguous":
                what = f"Jira key {row['jira_key']}" if matched_by == "key" else "name"
                row.update(action="ambiguous", detail=f"more than one target requirement has this {what}")
            elif outcome == "no-identifier":
                row.update(action="no-identifier", detail="the requirement has neither a Jira key nor a name")
            else:
                what = f"Jira key {row.get('mapped_key') or row['jira_key']}" if matched_by == "key" else "name"
                row.update(action="not-found", detail=f"no target requirement with this {what}")
        rows.append(row)

    issues: list[dict] = []
    for action, severity, text in (
            ("ambiguous", "WARN", "link(s) not made: the requirement matches more than one target requirement"),
            ("not-found", "WARN", "link(s) not made: the requirement does not exist in the target yet"),
            ("no-identifier", "WARN", "link(s) not made: the requirement has no Jira key and no name"),
            ("tc-not-migrated", "WARN", "link(s) not made: the test case is not migrated")):
        picked = [r for r in rows if r["action"] == action]
        if picked:
            sample = ", ".join(sorted({r["jira_key"] or r["requirement_name"][:40] for r in picked})[:5])
            issues.append(_issue(severity, "requirement link", f"{len(picked)} {text} (e.g. {sample})"))
    plan = {"run_id": ctx.run_id, **_ends(src, tgt, sp, tp),
            "scope": {"name_on_key_miss": name_on_key_miss, "key_map": key_map or {}}, "issues": issues, "rows": rows,
            "counts": {"actions": dict(Counter(r["action"] for r in rows)),
                       "matched_by": dict(Counter(r["matched_by"] for r in rows if r["action"] in ("link", "exists")))},
            "api": {src.name: src.stats, tgt.name: tgt.stats}}
    ctx.write_json("links_plan.json", plan)
    return plan


# ----------------------------------------------------------------------------- apply
def load_links_plan(runs_dir: Path, plan_run_id: str) -> dict:
    path = find_run_dir(runs_dir, plan_run_id) / "links_plan.json"
    if not path.is_file():
        raise LinksError(f"No links plan found at {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def apply_links(ctx: RunContext, tgt: QtestClient, db: StateDb, plan: dict, *, batch_size: int = LINK_BATCH) -> dict:
    src_name, sp = plan["source"]["instance"], plan["source"]["project_id"]
    tgt_name, tp = plan["target"]["instance"], plan["target"]["project_id"]
    if tgt.name != tgt_name:
        raise LinksError(f"Plan targets '{tgt_name}' but the client is for '{tgt.name}'")
    if not tgt.allow_writes:
        raise LinksError("Target client is read-only")
    batch_size = max(1, min(batch_size, LINK_BATCH))

    results = [{**r, "status": r["action"], "error": ""} for r in plan["rows"]]
    pending: dict[int, list[dict]] = {}
    for row in results:
        if row["action"] != "link":
            continue
        if db.requirement_link_done(src_name, sp, row["source_testcase_id"], row["source_requirement_id"], tgt_name, tp):
            row["status"] = "skip-mapped"  # linked by an earlier apply of this plan
            continue
        pending.setdefault(row["target_requirement_id"], []).append(row)

    aborted = ""
    consecutive = 0

    def record(row: dict) -> None:
        db.put_requirement_link_map(src_name, sp, row["source_testcase_id"], row["source_requirement_id"], tgt_name, tp,
                                    row["target_testcase_id"], row["target_requirement_id"], row["matched_by"], ctx.run_id)
        row["status"] = "linked"
        ctx.event("link", "linked", testcase=row["target_testcase_id"], requirement=row["target_requirement_id"],
                  matched_by=row["matched_by"])

    def attempt(requirement_id: int, rows: list[dict]) -> ApiError | None:
        for round_ in (1, 2):
            try:
                tgt.link_testcases_to_requirement(tp, requirement_id, [r["target_testcase_id"] for r in rows])
            except ApiError as exc:
                if not exc.ambiguous or round_ == 2:
                    return exc
                # the reply was lost: look at what is linked now, so that nothing is linked twice
                try:
                    linked = tgt.linked_artifacts(tp, "test-cases", [r["target_testcase_id"] for r in rows])
                except ApiError:
                    return exc
                done = [r for r in rows if requirement_id in {o["id"] for o in linked.get(r["target_testcase_id"], [])
                                                              if _is_requirement(o)}]
                for r in done:
                    record(r)
                rows = [r for r in rows if r not in done]
                if not rows:
                    return None
                continue
            break
        for r in rows:
            record(r)
        return None

    total = sum(len(v) for v in pending.values())
    ctx.log.info("Linking %d test case(s) to %d requirement(s) in %s project %s", total, len(pending), tgt_name, tp)
    for requirement_id, rows in pending.items():
        for batch in _batches(rows, batch_size):
            if aborted:
                for r in batch:
                    r.update(status="not-attempted", error=aborted)
                continue
            error = attempt(requirement_id, batch)
            if error is None:
                consecutive = 0
                continue
            if error.status in (401, 403):
                aborted = f"aborted after authorization failure ({_error_text(error)})"
                ctx.log.error("Stopping: %s", aborted)
                for r in batch:
                    r.update(status="not-attempted", error=aborted)
                continue
            ctx.log.warning("Batch of %d for requirement %s failed (%s); retrying one by one", len(batch),
                            requirement_id, _error_text(error))
            for r in batch:
                if aborted:
                    r.update(status="not-attempted", error=aborted)
                    continue
                single = attempt(requirement_id, [r])
                if single is None:
                    consecutive = 0
                    continue
                r.update(status="failed", error=_error_text(single))
                ctx.event("link", "failed", testcase=r["target_testcase_id"], requirement=requirement_id, error=r["error"])
                ctx.log.error("link FAILED test case %s -> requirement %s: %s", r["target_testcase_id"], requirement_id, r["error"])
                consecutive += 1
                if single.status in (401, 403):
                    aborted = f"aborted after authorization failure ({r['error']})"
                elif consecutive >= MAX_CONSECUTIVE_FAILURES:
                    aborted = f"aborted after {MAX_CONSECUTIVE_FAILURES} consecutive failures"
                if aborted:
                    ctx.log.error("Stopping: %s", aborted)

    issues = list(plan["issues"])
    gone = [r for r in results if r.get("status") == "failed" and (r.get("error") or "").startswith("HTTP 404")]
    if gone:
        message = (f"{len(gone)} link(s) failed with HTTP 404: a test case or requirement is missing in the target "
                   "(deleted there?). Run 'plan' and 'apply' again first (they create deleted test cases again), then "
                   "'links plan' and 'links apply'.")
        issues.append(_issue("WARN", "requirement link", message))
        ctx.log.warning(message)
    result = {"run_id": ctx.run_id, "plan_run_id": plan["run_id"], "source": plan["source"], "target": plan["target"],
              "scope": plan["scope"], "issues": issues, "rows": results, "aborted": aborted,
              "counts": {"actions": dict(Counter(r["status"] for r in results))}, "api": {tgt.name: tgt.stats}}
    ctx.write_json("links_result.json", result)
    return result


# ---------------------------------------------------------------------------- verify
def load_links_result(runs_dir: Path, apply_run_id: str) -> dict:
    path = find_run_dir(runs_dir, apply_run_id) / "links_result.json"
    if not path.is_file():
        raise FileNotFoundError(f"No links apply result at {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def verify_links(ctx: RunContext, tgt: QtestClient, result: dict) -> dict:
    """Read the links back from the target and confirm every link that apply reported."""
    tp = result["target"]["project_id"]
    expected = [r for r in result["rows"] if r["status"] in ("linked", "skip-mapped", "exists")]
    by_tc: dict[int, list[dict]] = {}
    for row in expected:
        by_tc.setdefault(row["target_testcase_id"], []).append(row)
    report = {"run_id": ctx.run_id, "apply_run_id": result["run_id"], "source": result["source"],
              "target": result["target"], "checked": 0, "ok": 0, "missing": [], "errors": []}
    for batch in _batches(sorted(by_tc), LINK_BATCH):
        try:
            linked = tgt.linked_artifacts(tp, "test-cases", batch)
        except ApiError as exc:
            report["errors"].append(f"test cases {batch[0]}..{batch[-1]}: {exc}")
            ctx.log.error("Cannot read links of test cases %s..%s: %s", batch[0], batch[-1], exc)
            continue
        for tc_id in batch:
            have = {o["id"] for o in linked.get(tc_id, []) if _is_requirement(o)}
            for row in by_tc[tc_id]:
                report["checked"] += 1
                if row["target_requirement_id"] in have:
                    report["ok"] += 1
                    ctx.event("verify", "ok", testcase=tc_id, requirement=row["target_requirement_id"])
                else:
                    report["missing"].append({"target_testcase_id": tc_id, "target_requirement_id": row["target_requirement_id"],
                                              "jira_key": row["jira_key"], "requirement_name": row["requirement_name"]})
                    ctx.event("verify", "missing", testcase=tc_id, requirement=row["target_requirement_id"])
    report["passed"] = not (report["missing"] or report["errors"])
    ctx.write_json("links_verify.json", report)
    return report
