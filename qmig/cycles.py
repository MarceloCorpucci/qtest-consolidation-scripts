"""Test cycles and test suites: describe (plan), create (apply) and check (verify).

The source tree is read top-down: each release holds cycles and suites, and a cycle holds more cycles and
suites.  The same tree is created under the mapped target release, so parents are always created before their
children.  A cycle or suite that already exists under the target parent with the same name is reused (and
mapped); mappings are kept in SQLite so a re-run never creates anything twice.  Test runs are a later step.
"""
from __future__ import annotations

from collections import Counter

from .client import ApiError, QtestClient
from .db import StateDb
from .journal import journaled
from .runlog import RunContext

MAX_CONSECUTIVE_FAILURES = 10
EXCLUDED_NAMES = ("recycle bin",)  # never migrated, like the Recycle Bin test case folder
PARENT_TYPE = {"release": "release", "cycle": "test-cycle", "suite": "test-suite", "root": "root"}


def gone(tgt: QtestClient, path: str) -> bool:
    """True only when the target answers that the object does not exist (any other error is raised, not guessed)."""
    try:
        tgt.request("GET", path)
        return False
    except ApiError as exc:
        if exc.status == 404 or (exc.status == 400 and "does not exist" in str(exc.detail).lower()):
            return True
        raise


def left_parent(tgt: QtestClient, tp: int, kind: str, target_id: int, parent_target: int, parent_kind: str, relist) -> bool:
    """True when a migrated object is no longer under its parent in the target: deleted, or moved elsewhere (for example to
    a Recycle Bin cycle).  A run reports its parent; a cycle or suite does not, so it is looked for again in a fresh listing."""
    route = {"cycle": "test-cycles", "suite": "test-suites", "run": "test-runs"}[kind]
    if gone(tgt, f"/projects/{tp}/{route}/{target_id}"):
        return True
    try:
        if kind == "run":
            run = tgt.get_run(tp, target_id)
            parent = run.get("parentId")
            return parent is not None and (parent != parent_target or run.get("parentType") != PARENT_TYPE[parent_kind])
        return target_id not in {t["id"] for t in relist()}
    except ApiError:
        return False


ROUTE = {"release": "releases", "cycle": "test-cycles", "suite": "test-suites", "run": "test-runs"}


def _key(value) -> str:
    return str(value or "").strip().casefold()


def node_payload(item: dict) -> dict:
    payload = {"name": str(item.get("name") or "").strip() or f"(unnamed {item.get('id')})"}
    for key in ("description", "start_date", "end_date"):
        if item.get(key):
            payload[key] = item[key]
    return payload


def read_source_tree(src: QtestClient, sp: int, releases: list[dict], issues: list[dict], include_root: bool) -> list[dict]:
    """Every cycle and suite below the given source releases (and the project root), parents before children."""
    nodes: list[dict] = []

    def walk(parent_kind: str, parent_id: int, parent_name: str, depth: int) -> None:
        ptype = PARENT_TYPE[parent_kind]
        for kind, lister in (("cycle", src.list_cycles), ("suite", src.list_suites)):
            try:
                items = lister(sp, parent_id, ptype)
            except ApiError as exc:
                if parent_kind != "root":
                    issues.append({"severity": "WARN", "area": "cycles",
                                   "message": f"Cannot read {kind}s of {parent_kind} '{parent_name}': {exc}"})
                continue
            for item in items:
                if _key(item.get("name")) in EXCLUDED_NAMES:
                    issues.append({"severity": "INFO", "area": "scope",
                                   "message": f"{kind} '{item.get('name')}' under '{parent_name or 'the project'}' (and everything inside it) is not migrated"})
                    continue
                path = f"{parent_name} / {item.get('name')}" if parent_name else str(item.get("name"))
                nodes.append({"kind": kind, "source_id": item["id"], "source_pid": item.get("pid"), "name": item.get("name"),
                              "parent_kind": parent_kind, "parent_source_id": parent_id, "path": path, "depth": depth,
                              "payload": node_payload(item)})
                if kind == "cycle":
                    walk("cycle", item["id"], path, depth + 1)

    for rel in releases:
        walk("release", rel["source_id"], rel["name"], 1)
    if include_root:
        walk("root", 0, "", 1)
    return nodes


def select_subtrees(nodes: list[dict], cycle_ids: tuple[int, ...]) -> list[dict]:
    """Keep the chosen cycles with everything below them, plus the cycles above them (marked ``context_only``: they are
    created so the chosen cycle keeps its place, but their own runs are not migrated)."""
    wanted = set(cycle_ids)
    by_key = {(n["kind"], n["source_id"]): n for n in nodes}
    keep: dict[tuple, dict] = {}
    for n in nodes:  # parents come before children
        parent = by_key.get((n["parent_kind"], n["parent_source_id"]))
        if (n["kind"] == "cycle" and n["source_id"] in wanted) or (parent is not None and (parent["kind"], parent["source_id"]) in keep
                                                                  and not keep[(parent["kind"], parent["source_id"])].get("context_only")):
            keep[(n["kind"], n["source_id"])] = {**n}
    for key in list(keep):
        cursor = by_key.get((keep[key]["parent_kind"], keep[key]["parent_source_id"]))
        while cursor is not None:
            ckey = (cursor["kind"], cursor["source_id"])
            if ckey not in keep:
                keep[ckey] = {**cursor, "context_only": True}
            cursor = by_key.get((cursor["parent_kind"], cursor["parent_source_id"]))
    return [keep[(n["kind"], n["source_id"])] for n in nodes if (n["kind"], n["source_id"]) in keep]


def plan_cycle_rows(ctx: RunContext, src: QtestClient, tgt: QtestClient, db: StateDb, sp: int, tp: int,
                    release_rows: list[dict], include_root: bool = True,
                    cycle_ids: tuple[int, ...] = ()) -> tuple[list[dict], list[dict]]:
    issues: list[dict] = []
    nodes = read_source_tree(src, sp, release_rows, issues, include_root)
    if cycle_ids:
        nodes = select_subtrees(nodes, cycle_ids)
        missing = set(cycle_ids) - {n["source_id"] for n in nodes if n["kind"] == "cycle"}
        for cid in sorted(missing):
            issues.append({"severity": "WARN", "area": "cycles", "message": f"--cycle-id {cid}: no such cycle under the planned releases"})
    known: dict[tuple[str, int], int | None] = {("release", r["source_id"]): r.get("target_id") for r in release_rows}
    listed: dict[tuple[str, int, str], dict[str, dict]] = {}
    rows: list[dict] = []
    seen: set[tuple] = set()
    for n in nodes:
        mapped = db.cycle_target(src.name, sp, n["kind"], n["source_id"], tgt.name, tp)
        parent_target = known.get((n["parent_kind"], n["parent_source_id"])) if n["parent_kind"] != "root" else 0
        by_id: dict[int, dict] = {}
        by_name: dict[str, dict] = {}
        if parent_target is not None:
            cache_key = (n["parent_kind"], parent_target, n["kind"])
            if cache_key not in listed:
                try:
                    lister = tgt.list_cycles if n["kind"] == "cycle" else tgt.list_suites
                    items = lister(tp, parent_target, PARENT_TYPE[n["parent_kind"]])
                    listed[cache_key] = ({t["id"]: t for t in items}, {_key(t.get("name")): t for t in items})
                except ApiError:
                    listed[cache_key] = ({}, {})
            by_id, by_name = listed[cache_key]
        stale = None
        if mapped and (parent_target is None or (mapped not in by_id and left_parent(
                tgt, tp, n["kind"], mapped, parent_target, n["parent_kind"],
                lambda: (tgt.list_cycles if n["kind"] == "cycle" else tgt.list_suites)(tp, parent_target, PARENT_TYPE[n["parent_kind"]])))):
            stale, mapped = mapped, None  # deleted or moved away in the target (or its parent is being created again)
            issues.append({"severity": "WARN", "area": n["kind"],
                           "message": f"'{n['path']}' was migrated earlier but is no longer in the target (deleted or moved away); it will be created again"})
        match = by_name.get(_key(n["name"])) if not mapped else None
        action = "skip-mapped" if mapped else "reuse" if match else "create"
        target_id = mapped or (match or {}).get("id")
        known[(n["kind"], n["source_id"])] = target_id
        dup = (n["parent_kind"], n["parent_source_id"], n["kind"], _key(n["name"]))
        if dup in seen:
            issues.append({"severity": "WARN", "area": n["kind"],
                           "message": f"'{n['path']}': another {n['kind']} with this name exists under the same parent; they would share one target {n['kind']}"})
        seen.add(dup)
        rows.append({**n, "action": action, "target_id": target_id, "stale_target_id": stale})
        ctx.event(n["kind"], f"plan-{action}", source_id=n["source_id"], target_id=target_id, name=n["name"])
    return rows, issues


def summarize(rows: list[dict]) -> dict:
    return {"cycles": dict(Counter(r["action"] for r in rows if r["kind"] == "cycle")),
            "suites": dict(Counter(r["action"] for r in rows if r["kind"] == "suite"))}


def apply_cycles(ctx: RunContext, tgt: QtestClient, db: StateDb, plan: dict, release_results: list[dict],
                 aborted: str = "") -> tuple[list[dict], str]:
    src_name, sp = plan["source"]["instance"], plan["source"]["project_id"]
    tgt_name, tp = plan["target"]["instance"], plan["target"]["project_id"]
    resolved: dict[tuple[str, int], int | None] = {}
    for r in plan.get("releases") or []:
        resolved[("release", r["source_id"])] = db.release_target(src_name, sp, r["source_id"], tgt_name, tp) or r.get("target_id")
    for r in release_results:
        if r.get("target_id"):
            resolved[("release", r["source_id"])] = r["target_id"]
    results: list[dict] = []
    ctx.log.info("Applying %d test cycle(s) and suite(s)", len(plan["cycles"]))
    consecutive = 0
    for plan_row in plan["cycles"]:
        kind = plan_row["kind"]
        row = {**plan_row, "error": "", "result": plan_row["action"], "parent_target_id": None}
        parent = 0 if plan_row["parent_kind"] == "root" else resolved.get((plan_row["parent_kind"], plan_row["parent_source_id"]))
        row["parent_target_id"] = parent
        if plan_row.get("stale_target_id"):
            db.forget_target_object(kind, tgt_name, tp, plan_row["stale_target_id"])
        mapped = db.cycle_target(src_name, sp, kind, plan_row["source_id"], tgt_name, tp)
        if aborted:
            row.update(result="not-attempted", error=aborted)
        elif mapped:
            row.update(result="skip-mapped", target_id=mapped)
        elif plan_row["action"] == "reuse" and plan_row.get("target_id"):
            db.put_cycle(src_name, sp, kind, plan_row["source_id"], tgt_name, tp, plan_row["target_id"], plan_row["name"], ctx.run_id)
        elif parent is None:
            row.update(result="skipped", error=f"the {plan_row['parent_kind']} above it was not created")
        else:
            try:
                make = tgt.create_cycle if kind == "cycle" else tgt.create_suite
                created = journaled(ctx, tgt, db, kind, src_name, sp, plan_row["source_id"], tgt_name, tp, parent, plan_row["name"],
                                    lambda: make(tp, parent, PARENT_TYPE[plan_row["parent_kind"]], plan_row["payload"]),
                                    parent_type=PARENT_TYPE[plan_row["parent_kind"]])
                row.update(result="created", target_id=created["id"])
                db.put_cycle(src_name, sp, kind, plan_row["source_id"], tgt_name, tp, created["id"], plan_row["name"], ctx.run_id)
                consecutive = 0
                ctx.log.info("%s created  %s", kind, plan_row["path"])
            except (ApiError, KeyError) as exc:
                row.update(result="failed", error=f"HTTP {exc.status}: {exc.detail}" if isinstance(exc, ApiError) and exc.status
                           else str(exc))
                consecutive += 1
                ctx.log.error("%s FAILED  %s: %s", kind, plan_row["path"], row["error"])
                if isinstance(exc, ApiError) and exc.status in (401, 403):
                    aborted = f"aborted after authorization failure ({row['error']})"
                elif consecutive >= MAX_CONSECUTIVE_FAILURES:
                    aborted = f"aborted after {MAX_CONSECUTIVE_FAILURES} consecutive {kind} failures"
        if row["result"] in ("created", "reuse", "skip-mapped") and row.get("target_id"):
            resolved[(kind, plan_row["source_id"])] = row["target_id"]
        ctx.event(kind, row["result"], source_id=row["source_id"], target_id=row.get("target_id"), name=row["name"],
                  error=row["error"])
        results.append(row)
    return results, aborted


def verify_cycles(ctx: RunContext, tgt: QtestClient, tp: int, rows: list[dict]) -> dict:
    """Every cycle and suite created, reused or mapped by apply must be listed under its target parent with its name."""
    report = {"checked": 0, "ok": 0, "missing": [], "name_mismatch": [], "errors": []}
    cache: dict[tuple, dict[int, dict]] = {}
    for row in rows:
        if row["result"] not in ("created", "reuse", "skip-mapped") or not row.get("target_id"):
            continue
        parent = row.get("parent_target_id")
        if parent is None:
            continue
        report["checked"] += 1
        key = (row["parent_kind"], parent, row["kind"])
        if key not in cache:
            try:
                lister = tgt.list_cycles if row["kind"] == "cycle" else tgt.list_suites
                cache[key] = {t["id"]: t for t in lister(tp, parent, PARENT_TYPE[row["parent_kind"]])}
            except ApiError as exc:
                report["errors"].append(f"{row['kind']}s under {row['parent_kind']} {parent}: {exc}")
                cache[key] = {}
        found = cache[key].get(row["target_id"])
        if found is None:
            report["missing"].append({"kind": row["kind"], "path": row["path"], "target_id": row["target_id"]})
        elif _key(found.get("name")) != _key(row["payload"]["name"]):
            report["name_mismatch"].append({"kind": row["kind"], "path": row["path"], "expected": row["payload"]["name"],
                                            "actual": found.get("name")})
        else:
            report["ok"] += 1
    ctx.log.info("Cycles and suites verified: %d checked, %d ok, %d missing", report["checked"], report["ok"], len(report["missing"]))
    return report
