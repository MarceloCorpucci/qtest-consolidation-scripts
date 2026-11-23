"""Read-only planning: what would a migration of folders + test cases do?

``build_plan`` only issues GET requests (the target client is read-only, see
client.py).  It writes ``plan.json`` (folders, test case rows, issues, counts)
and ``testcases.jsonl`` (the exact payload that ``apply`` would POST).
"""
from __future__ import annotations

import html
import json
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from .approvals import wants_approval
from .attachments import describe
from .stepfiles import kept_steps, plan_step_files
from .client import ApiError, QtestClient
from .db import StateDb
from .fields import DEFAULT_SKIP_FIELDS, METADATA_FIELDS, FieldMapper
from .cycles import plan_cycle_rows, summarize as summarize_cycles
from .logs import plan_log_rows, summarize as summarize_logs
from .runs import plan_run_rows, summarize as summarize_runs
from .releases import plan_release_rows, summarize
from .runlog import RunContext

PLAN_VERSION = 1

# Folders with these names (any level, case-insensitive) are never migrated, with everything inside them.
EXCLUDED_FOLDER_NAMES = ("recycle bin",)


class PreflightError(RuntimeError):
    pass


def _key(name: Any) -> str:
    return str(name or "").strip().casefold()


def flatten_modules(nodes: list[dict], parent_id: int | None) -> list[dict]:
    """Pre-order flatten of a module tree (parents before children)."""
    rows: list[dict] = []
    for node in nodes:
        rows.append({"source_id": node["id"], "name": str(node.get("name") or "").strip(),
                     "description": node.get("description") or "", "parent_source_id": parent_id})
        children = node.get("children") or node.get("sub_modules") or node.get("subModules") or []
        rows.extend(flatten_modules([c for c in children if isinstance(c, dict) and c.get("id") is not None],
                                    node["id"]))
    return rows


def _exists_in_target(fetch) -> bool:
    """True unless the target answers 404 for the object (any other error is raised, not guessed)."""
    try:
        fetch()
        return True
    except ApiError as exc:
        if exc.status == 404 or (exc.status == 400 and "does not exist" in str(exc.detail).lower()):
            return False
        raise


def _tc_left_folder(tgt: QtestClient, tp: int, test_case_id: int, folder_target: int | None) -> bool:
    """True when a migrated test case is gone from the target or no longer sits in its folder (moved, e.g. to a Recycle Bin)."""
    try:
        found = tgt.request("GET", f"/projects/{tp}/test-cases/{test_case_id}")[0] or {}
    except ApiError as exc:
        if exc.status == 404 or (exc.status == 400 and "does not exist" in str(exc.detail).lower()):
            return True
        raise
    return folder_target is None or found.get("parent_id") not in (None, folder_target)


def _folder_left_parent(tgt: QtestClient, tp: int, folder_id: int, parent_ref: int | None) -> bool:
    """True when a migrated folder is gone from the target or was moved out of its parent."""
    try:
        found = tgt.get_module(tp, folder_id) or {}
    except ApiError as exc:
        if exc.status == 404 or (exc.status == 400 and "does not exist" in str(exc.detail).lower()):
            return True
        raise
    if parent_ref is None:
        return found.get("parent_id") not in (None, 0)  # a top-level folder has no parent: moved if it now has one
    return parent_ref < 0 or found.get("parent_id") not in (None, parent_ref)


def check_projects(src: QtestClient, tgt: QtestClient, sp: int, tp: int) -> tuple[dict, dict]:
    out = []
    for client, pid in ((src, sp), (tgt, tp)):
        try:
            out.append(client.get_project(pid))
        except ApiError as exc:
            hint = {401: "token rejected", 403: "token has no access to this project",
                    404: "project not found on this instance"}.get(exc.status or 0, "request failed")
            raise PreflightError(f"[{client.name}] project {pid}: {hint} ({exc})") from exc
    return out[0], out[1]


def build_field_mapper(src: QtestClient, tgt: QtestClient, sp: int, tp: int,
                       skip_fields: tuple[str, ...], properties: bool = True) -> FieldMapper:
    if not properties:
        return FieldMapper.disabled()
    try:
        return FieldMapper(src.test_case_fields(sp), tgt.test_case_fields(tp), skip_fields)
    except ApiError as exc:
        hint = (" The token cannot read the field settings of a project; ask a project admin for access, "
                "or add --no-properties to migrate without custom field values.") if exc.status == 403 else ""
        raise PreflightError(f"Cannot read test case field settings: {exc}.{hint}") from exc


def preflight(ctx: RunContext, src: QtestClient, tgt: QtestClient, sp: int, tp: int,
              skip_fields: tuple[str, ...] = DEFAULT_SKIP_FIELDS, properties: bool = True) -> dict:
    ctx.log.info("Checking access to source project %s on %s and target project %s on %s", sp, src.name, tp, tgt.name)
    sproj, tproj = check_projects(src, tgt, sp, tp)
    ctx.log.info("Source project: '%s'   Target project: '%s'", sproj.get("name"), tproj.get("name"))
    mapper = build_field_mapper(src, tgt, sp, tp, skip_fields, properties)
    for issue in mapper.issues:
        ctx.log.log(30 if issue["severity"] == "WARN" else 20, "%s", issue["message"])
    return {"source_project": {"id": sp, "name": sproj.get("name")},
            "target_project": {"id": tp, "name": tproj.get("name")},
            "issues": mapper.issues, "mapper": mapper}


def _excluded_ids(folders: list[dict]) -> set[int]:
    """Ids of the excluded folders and all their descendants (``folders`` is in pre-order)."""
    excluded: set[int] = set()
    for folder in folders:
        if _key(folder["name"]) in EXCLUDED_FOLDER_NAMES or folder["parent_source_id"] in excluded:
            excluded.add(folder["source_id"])
    return excluded


def _source_folders(src: QtestClient, sp: int, module_id: int | None, with_parents: bool = True) -> tuple[list[dict], int]:
    """Folders in scope, parents before children.

    With ``module_id`` the folder and its sub-folders are in scope.  Its ancestors are included as
    ``context_only`` folders (created empty, no test cases) so the folder lands where it sits in the
    source; ``with_parents=False`` puts it at the top instead.
    """
    everything = flatten_modules(src.list_modules(sp, None, descendants=True), None)
    excluded = _excluded_ids(everything)
    if module_id in excluded:
        raise PreflightError(f"Source module {module_id} is in a folder that is never migrated "
                             f"({', '.join(EXCLUDED_FOLDER_NAMES)})")
    everything = [f for f in everything if f["source_id"] not in excluded]
    if module_id is None:
        return everything, len(excluded)
    by_id = {f["source_id"]: f for f in everything}
    if module_id not in by_id:
        raise PreflightError(f"Source module {module_id} not found in project {sp}")
    chain, cursor = [], by_id[module_id]["parent_source_id"]
    while cursor is not None:
        chain.append(cursor)
        cursor = by_id[cursor]["parent_source_id"]
    scope: list[dict] = []
    if with_parents:
        scope += [{**by_id[i], "context_only": True} for i in reversed(chain)]
    inside = {module_id}
    for folder in everything:  # pre-order: a parent is always seen before its children
        if folder["source_id"] == module_id:
            scope.append(folder if with_parents else {**folder, "parent_source_id": None})
        elif folder["parent_source_id"] in inside:
            inside.add(folder["source_id"])
            scope.append(folder)
    return scope, len(excluded)


def build_step_list(steps: Any) -> list[dict]:
    return [{"order": i, "description": step.get("description") or "",
             "expected": step.get("expected") or step.get("expected_result") or ""}
            for i, step in enumerate(kept_steps(steps), 1)]


def _unbracket(text: Any) -> str:
    text = str(text or "").strip()
    return text[1:-1].strip() if text.startswith("[") and text.endswith("]") else text


def assigned_to_names(tc: dict) -> list[str]:
    """Names from the Assigned To property: ``[Jeffrey Jandi]`` -> ``['Jeffrey Jandi']``.

    The field can hold several users (``[1,2]`` with names ``[A, B]``).  The names are split only when the
    count matches the number of user ids, so a single name that contains a comma stays in one piece.
    """
    for prop in tc.get("properties") or []:
        if isinstance(prop, dict) and _key(prop.get("field_name")) == "assigned to":
            text = _unbracket(prop.get("field_value_name"))
            if not text:
                return []
            ids = [i for i in _unbracket(prop.get("field_value")).split(",") if i.strip()]
            parts = [n.strip() for n in text.split(", ") if n.strip()]
            return parts if len(ids) > 1 and len(parts) == len(ids) else [text]
    return []


def _metadata_value(prop: dict) -> str:
    """Display value of a property for the description ('' when empty, `_NULL_` or a zero)."""
    text = _unbracket(prop.get("field_value_name")) or _unbracket(prop.get("field_value"))
    return "" if text.strip() in ("", "_NULL_", "0", "0.0") else text.strip()


def metadata_values(tc: dict) -> list[tuple[str, str]]:
    """(label, value) of the properties that are kept in the description instead of a field, in METADATA_FIELDS order."""
    found = {}
    for prop in tc.get("properties") or []:
        if isinstance(prop, dict) and _key(prop.get("field_name")) in {_key(f) for f in METADATA_FIELDS}:
            value = _metadata_value(prop)
            if value:
                found[_key(prop.get("field_name"))] = (prop["field_name"].strip(), value)
    return [found[_key(f)] for f in METADATA_FIELDS if _key(f) in found]


def migrated_metadata(tc: dict) -> str:
    """HTML block recording what is not migrated as a field (users, fields HSCS does not have), or ''."""
    lines = []
    names = assigned_to_names(tc)
    if names:
        lines.append('(Previously) "Assigned To" : ' + ", ".join(f'"{html.escape(n, quote=False)}"' for n in names))
    lines += [f'(Previously) "{html.escape(label, quote=False)}" : "{html.escape(value, quote=False)}"'
              for label, value in metadata_values(tc)]
    return "<b>Migrated metadata</b>:<br>" + "<br>".join(lines) if lines else ""


def build_payload(tc: dict, mapper: FieldMapper, metadata: bool = True) -> tuple[dict, list[str]]:
    properties, warnings = mapper.map_properties(tc.get("properties") or [])
    description = (tc.get("description") or "").strip()
    block = migrated_metadata(tc) if metadata else ""
    if block:
        description = f"{description}<br>{block}" if description else block
    payload: dict[str, Any] = {"name": str(tc.get("name") or "").strip() or f"(unnamed {tc.get('pid') or tc.get('id')})",
                               "description": description if block else (tc.get("description") or ""),
                               "properties": properties,
                               "test_steps": build_step_list(tc.get("test_steps"))}
    if tc.get("precondition"):
        payload["precondition"] = tc["precondition"]
    return payload, warnings


def build_plan(ctx: RunContext, src: QtestClient, tgt: QtestClient, db: StateDb, *, sp: int, tp: int,
               module_id: int | None = None, target_parent_id: int | None = None, limit: int | None = None,
               with_parents: bool = True,
               skip_fields: tuple[str, ...] = DEFAULT_SKIP_FIELDS, attachments: bool = False,
               properties: bool = True, releases: bool = False, only_releases: bool = False,
               release_ids: tuple[int, ...] = (), approve: str | None = None, metadata: bool = True,
               cycles: bool = False, runs: bool = False, cycle_ids: tuple[int, ...] = (), logs: str | None = None, step_results: bool = True,
               step_files: bool = False, test_case_ids: tuple[int, ...] = ()) -> dict:
    if logs:
        runs = True  # logs hang under runs
    if runs:
        cycles = True  # runs hang under cycles and suites
    if cycles:
        releases = True  # cycles hang under releases
    if only_releases:
        releases, properties = True, False  # no folders or test cases, so no field settings are needed
    pre = preflight(ctx, src, tgt, sp, tp, skip_fields, properties)
    mapper: FieldMapper = pre["mapper"]
    issues: list[dict] = list(pre["issues"])

    # ------------------------------------------------------------ folders
    ctx.log.info("Reading source folder tree ...")
    source_folders, excluded_folders = ([], 0) if only_releases else _source_folders(src, sp, module_id, with_parents)
    if excluded_folders:
        issues.append({"severity": "INFO", "area": "scope",
                       "message": f"{excluded_folders} folder(s) named {' / '.join(repr(n) for n in EXCLUDED_FOLDER_NAMES)} "
                                  "(and everything inside them) are not migrated"})
    ctx.log.info("Source folders in scope: %d", len(source_folders))
    children_cache: dict[int | None, dict[str, dict]] = {}

    def target_children(parent: int | None) -> dict[str, dict]:
        if parent not in children_cache:
            rows = tgt.list_modules(tp, parent)
            index: dict[str, dict] = {}
            for row in rows:
                index.setdefault(_key(row.get("name")), row)
            children_cache[parent] = index
        return children_cache[parent]

    folders: list[dict] = []
    ref_of: dict[int, int] = {}  # source folder id -> real target id, or -(idx+1) when it would be created
    path_of: dict[int, str] = {}
    idx_of: dict[int, int] = {}
    for idx, folder in enumerate(source_folders):
        parent_src = folder["parent_source_id"]
        parent_ref = ref_of[parent_src] if parent_src is not None else target_parent_id
        path = f"{path_of[parent_src]} / {folder['name']}" if parent_src is not None else folder["name"]
        path_of[folder["source_id"]] = path
        mapped = db.folder_target(src.name, sp, folder["source_id"], tgt.name, tp)
        stale = None
        if mapped and _folder_left_parent(tgt, tp, mapped, parent_ref):
            stale, mapped = mapped, None  # deleted or moved away in the target since it was migrated: create it again
            issues.append({"severity": "WARN", "area": "folders",
                           "message": f"Folder '{path}' was migrated earlier but is no longer in the target (deleted or moved away); it will be created again"})
        action, target_id = "create", None
        if mapped:
            action, target_id = "reuse-mapped", mapped
        elif parent_ref is None or parent_ref > 0:
            match = target_children(parent_ref).get(_key(folder["name"]))
            if match:
                action, target_id = "reuse", match["id"]
        ref_of[folder["source_id"]] = target_id if target_id else -(idx + 1)
        parent_idx = idx_of.get(parent_src) if parent_src is not None else None
        idx_of[folder["source_id"]] = idx
        folders.append({"idx": idx, "source_id": folder["source_id"], "name": folder["name"],
                        "description": folder["description"], "parent_idx": parent_idx,
                        "path": path, "action": action, "target_id": target_id,
                        "context_only": bool(folder.get("context_only")), "stale_target_id": stale})
        ctx.event("folder", f"plan-{action}", source_id=folder["source_id"], target_id=target_id, path=path)
    creates = sum(f["action"] == "create" for f in folders)
    ctx.log.info("Folders: %d to create, %d already exist in target", creates, len(folders) - creates)

    # --------------------------------------------------------- test cases
    ctx.log.info("Reading source test cases%s ...", f" (stopping after {limit} to create)" if limit else "")
    rows: list[dict] = []
    warning_counter: Counter[str] = Counter()
    to_create = 0
    scan_stopped = False
    payload_file = open(ctx.dir / "testcases.jsonl", "w", encoding="utf-8")
    try:
        for folder in folders:
            if folder["context_only"]:
                continue  # parent folder created only to keep the structure; its test cases are not in scope
            if limit is not None and to_create >= limit:
                scan_stopped = True
                break
            cases = src.list_testcases(sp, folder["source_id"])
            if test_case_ids:
                cases = [c for c in cases if c["id"] in test_case_ids]
            existing: dict[str, dict] = {}
            if folder["target_id"]:
                existing = {_key(c.get("name")): c for c in tgt.list_testcases(tp, folder["target_id"], expand=False)}
            existing_ids = {c.get("id") for c in existing.values()}
            ctx.log.info("  folder '%s': %d source test case(s)", folder["path"], len(cases))
            for tc in cases:
                n = len(rows) + 1
                payload, warnings = build_payload(tc, mapper, metadata)
                for message in warnings:
                    warning_counter[message] += 1
                mapped = db.testcase_target(src.name, sp, tc["id"], tgt.name, tp)
                stale_tc = None
                if mapped and mapped["target_id"] not in existing_ids and _tc_left_folder(tgt, tp, mapped["target_id"], folder["target_id"]):
                    stale_tc, mapped = mapped["target_id"], None  # deleted or moved away in the target: create it again
                row = {"stale_target_id": stale_tc, "n": n, "source_id": tc["id"], "source_pid": tc.get("pid"), "name": payload["name"],
                       "folder_idx": folder["idx"], "path": folder["path"], "action": "create",
                       "target_id": None, "target_pid": None,
                       "source_steps": len(tc.get("test_steps") or []), "steps": len(payload["test_steps"]),
                       "source_version": tc.get("version"), "approve": False, "warnings": warnings[:5]}
                if mapped:
                    row.update(action="skip-mapped", target_id=mapped["target_id"], target_pid=mapped["target_pid"])
                elif _key(payload["name"]) in existing:
                    other = existing[_key(payload["name"])]
                    row.update(action="skip-exists", target_id=other.get("id"), target_pid=other.get("pid"))
                elif limit is not None and to_create >= limit:
                    row["action"] = "deferred"
                else:
                    to_create += 1
                    payload_file.write(json.dumps({"n": n, "payload": payload}, ensure_ascii=False) + "\n")
                row["approve"] = row["action"] in ("create", "skip-mapped") and wants_approval(approve, tc.get("version"))
                row["attachments"] = []
                if attachments and row["action"] in ("create", "skip-mapped"):
                    try:
                        row["attachments"] = describe(src.list_attachments(sp, "test-cases", tc["id"]),
                                                      db, src.name, sp, tgt.name, tp)
                    except ApiError as exc:
                        issues.append({"severity": "WARN", "area": "attachments",
                                       "message": f"Cannot list attachments of test case {tc.get('pid') or tc['id']}: {exc}"})
                row["attachment_count"] = len(row["attachments"])
                row["step_files"] = []
                if step_files and row["action"] in ("create", "skip-mapped"):
                    try:
                        row["step_files"], dropped = plan_step_files(src, db, sp, tgt.name, tp, tc["id"])
                        if dropped:
                            issues.append({"severity": "WARN", "area": "step files",
                                           "message": f"Test case {tc.get('pid') or tc['id']}: {dropped} step(s) with files have no text and are not migrated, so their files are not copied"})
                    except ApiError as exc:
                        issues.append({"severity": "WARN", "area": "step files",
                                       "message": f"Cannot read the steps of test case {tc.get('pid') or tc['id']}: {exc}"})
                rows.append(row)
                ctx.event("testcase", f"plan-{row['action']}", source_id=tc["id"], target_id=row["target_id"],
                          name=payload["name"], path=folder["path"])
    finally:
        payload_file.close()

    for message, count in warning_counter.most_common():
        issues.append({"severity": "WARN", "area": "testcases", "message": f"{count} test case(s): {message}"})
    stale_count = sum(1 for r in rows if r.get("stale_target_id"))
    if stale_count:
        issues.append({"severity": "WARN", "area": "testcases",
                       "message": f"{stale_count} test case(s) were migrated earlier but no longer exist in the target; they will be created again"})
    if scan_stopped:
        issues.append({"severity": "INFO", "area": "scope",
                       "message": f"--limit {limit} reached: remaining folders were not scanned"})
    for client in (src, tgt):
        for message in client.warnings:
            issues.append({"severity": "WARN", "area": "api", "message": f"[{client.name}] {message}"})
    empty = sum(1 for r in rows if r["action"] == "create" and r["steps"] == 0)
    if empty:
        issues.append({"severity": "WARN", "area": "testcases", "message": f"{empty} test case(s) to create have no test steps"})

    counts = Counter(r["action"] for r in rows)
    if mapper.auto_content_added:
        issues.append({"severity": "INFO", "area": "testcases",
                       "message": f"{mapper.auto_content_added} automation test case(s) have no Automation Content in the source; "
                                  f"'{mapper.AUTOMATION_CONTENT_DEFAULT}' was set because the target refuses an empty one"})
    plan = {
        "version": PLAN_VERSION, "run_id": ctx.run_id,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": {"instance": src.name, "url": src.instance.web_url, "project_id": sp, "project": pre["source_project"]["name"]},
        "target": {"instance": tgt.name, "url": tgt.instance.web_url, "project_id": tp, "project": pre["target_project"]["name"]},
        "scope": {"module_id": module_id, "target_parent_id": target_parent_id, "limit": limit,
                  "skip_fields": list(skip_fields), "scan_stopped_by_limit": scan_stopped, "attachments": attachments, "step_files": step_files, "test_case_ids": list(test_case_ids),
                  "with_parents": with_parents, "approve": approve, "metadata": metadata, "label": ctx.label, "excluded_folders": excluded_folders,
                  "properties": properties, "releases": releases, "only_releases": only_releases, "cycles": cycles, "runs": runs, "cycle_ids": list(cycle_ids), "logs": logs, "step_results": step_results},
        "folders": folders, "testcases": rows, "issues": issues,
        "counts": {"folders": dict(Counter(f["action"] for f in folders)), "testcases": dict(counts)},
        "api": {src.name: src.stats, tgt.name: tgt.stats},
    }
    if attachments:
        listed = [a for r in rows for a in r["attachments"]]
        plan["counts"]["attachments"] = {"to-copy": sum(not a["done"] for a in listed),
                                         "already-copied": sum(a["done"] for a in listed)}
        ctx.log.info("Attachments: %d to copy, %d already copied", plan["counts"]["attachments"]["to-copy"],
                     plan["counts"]["attachments"]["already-copied"])
    if step_files:
        listed = [f for r in rows for s in r["step_files"] for f in s["files"]]
        plan["counts"]["step_files"] = {"to-copy": sum(not f["done"] for f in listed),
                                        "already-copied": sum(f["done"] for f in listed)}
        ctx.log.info("Step files: %s", plan["counts"]["step_files"])
    if approve:
        plan["counts"]["approvals"] = {"to-approve": sum(r["approve"] for r in rows),
                                       "left-as-draft": sum(r["action"] in ("create", "skip-mapped") and not r["approve"] for r in rows)}
        ctx.log.info("Approvals (%s): %s", approve, plan["counts"]["approvals"])
    plan["releases"] = []
    if releases:
        ctx.log.info("Reading source releases and builds ...")
        plan["releases"], release_issues = plan_release_rows(ctx, src, tgt, db, sp, tp, only_ids=release_ids)
        plan["issues"] += release_issues
        plan["counts"].update(summarize(plan["releases"]))
        ctx.log.info("Releases %s | builds %s", plan["counts"]["releases"], plan["counts"]["builds"])
    plan["cycles"] = []
    if cycles:
        ctx.log.info("Reading source test cycles and suites ...")
        plan["cycles"], cycle_issues = plan_cycle_rows(ctx, src, tgt, db, sp, tp, plan["releases"], include_root=not release_ids,
                                                       cycle_ids=cycle_ids)
        if cycle_ids:  # only the releases the chosen cycles hang under
            used = {r["parent_source_id"] for r in plan["cycles"] if r["parent_kind"] == "release"}
            plan["releases"] = [r for r in plan["releases"] if r["source_id"] in used]
            plan["counts"].update(summarize(plan["releases"]))
        plan["issues"] += cycle_issues
        plan["counts"].update(summarize_cycles(plan["cycles"]))
        ctx.log.info("Test cycles %s | suites %s", plan["counts"]["cycles"], plan["counts"]["suites"])
    plan["runs"] = []
    if runs:
        ctx.log.info("Reading source test runs ...")
        planned = {r["source_id"] for r in rows if r["action"] == "create"}
        plan["runs"], run_issues = plan_run_rows(ctx, src, tgt, db, sp, tp, plan["cycles"], planned, metadata=metadata)
        plan["issues"] += run_issues
        plan["counts"].update(summarize_runs(plan["runs"]))
        ctx.log.info("Test runs %s | files %s", plan["counts"]["runs"], plan["counts"]["run_attachments"])
    plan["logs"] = []
    if logs:
        ctx.log.info("Reading source test logs (%s) ...", logs)
        plan["logs"], log_issues = plan_log_rows(ctx, src, tgt, db, sp, tp, plan["runs"], mode=logs, metadata=metadata, step_results=step_results)
        plan["issues"] += log_issues
        plan["counts"].update(summarize_logs(plan["logs"]))
        ctx.log.info("Test logs %s | step results %s | files %s", plan["counts"]["logs"], plan["counts"]["log_steps"],
                     plan["counts"]["log_attachments"])
    ctx.write_json("plan.json", plan)
    return plan
