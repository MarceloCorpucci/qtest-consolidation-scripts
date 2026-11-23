"""Releases and their builds: describe (plan), create (apply) and check (verify).

A release is matched by name: one that already exists in the target is reused (and mapped, so later steps
can link cycles and suites to it); the others are created with the same name, description, note and the
original start and end dates.  Builds are created under the target release.  Mappings are kept in SQLite
so a re-run never creates anything twice.  Users are not involved.
"""
from __future__ import annotations

from collections import Counter

from .client import ApiError, QtestClient
from .cycles import gone
from .db import StateDb
from .journal import journaled, repeat_if_unsure
from .runlog import RunContext

MAX_CONSECUTIVE_FAILURES = 10


def _key(value) -> str:
    return str(value or "").strip().casefold()


def _status(release: dict) -> tuple[str, str]:
    """(value, label) of the release's Status property, '' when absent."""
    for prop in release.get("properties") or []:
        if _key(prop.get("field_name")) == "status":
            return str(prop.get("field_value") or ""), str(prop.get("field_value_name") or "")
    return "", ""


def release_payload(release: dict) -> dict:
    payload = {"name": str(release.get("name") or "").strip() or f"(unnamed release {release.get('id')})",
               "description": release.get("description") or ""}
    for key in ("note", "start_date", "end_date"):
        if release.get(key):
            payload[key] = release[key]
    return payload


def build_payload(build: dict) -> dict:
    payload = {"name": str(build.get("name") or "").strip() or f"(unnamed build {build.get('id')})"}
    if build.get("description"):
        payload["description"] = build["description"]
    return payload


def plan_release_rows(ctx: RunContext, src: QtestClient, tgt: QtestClient, db: StateDb, sp: int, tp: int,
                      builds: bool = True, only_ids: tuple[int, ...] = ()) -> tuple[list[dict], list[dict]]:
    """Rows for every source release (with its builds) and the issues found while reading."""
    issues: list[dict] = []
    target_by_name: dict[str, dict] = {}
    target_releases = tgt.list_releases(tp)
    present_ids = {t["id"] for t in target_releases}
    for t in target_releases:
        target_by_name.setdefault(_key(t.get("name")), t)
    rows: list[dict] = []
    seen: set[str] = set()
    project_start = (tgt.get_project(tp) or {}).get("start_date")
    for r in src.list_releases(sp):
        if only_ids and r["id"] not in only_ids:
            continue
        status_value, status_label = _status(r)
        mapped = db.release_target(src.name, sp, r["id"], tgt.name, tp)
        stale = None
        if mapped and mapped not in present_ids and gone(tgt, f"/projects/{tp}/releases/{mapped}"):
            stale, mapped = mapped, None  # deleted in the target since it was migrated: it is created again
            issues.append({"severity": "WARN", "area": "release",
                           "message": f"Release '{r.get('name')}' was migrated earlier but no longer exists in the target; it will be created again"})
        match = target_by_name.get(_key(r.get("name")))
        row = {"source_id": r["id"], "source_pid": r.get("pid"), "name": r.get("name"),
               "start_date": r.get("start_date") or "", "end_date": r.get("end_date") or "",
               "status": status_label, "status_value": status_value, "payload": release_payload(r),
               "action": "skip-mapped" if mapped else "reuse" if match else "create",
               "target_id": mapped or (match or {}).get("id"), "builds": [], "stale_target_id": stale}
        if row["action"] == "create" and project_start:
            moved = [k for k in ("start_date", "end_date") if r.get(k) and not _not_before(r[k], project_start)]
            for k in moved:
                row["payload"][k] = project_start  # qTest rejects releases that start before the project does
            if moved:
                row["date_adjusted"] = True
                issues.append({"severity": "INFO", "area": "release dates",
                               "message": f"Release '{r.get('name')}' starts {r['start_date'][:10]}, before the target project start "
                                          f"{project_start[:10]}: the project start date is used instead"})
        if _key(r.get("name")) in seen:
            issues.append({"severity": "WARN", "area": "release",
                           "message": f"Source has more than one release named '{r.get('name')}'; they would share one target release"})
        seen.add(_key(r.get("name")))
        if builds:
            try:
                existing = {_key(b.get("name")) for b in tgt.list_builds(tp, row["target_id"])} if row["target_id"] else set()
                for b in src.list_builds(sp, r["id"]):
                    done = not stale and db.build_target(src.name, sp, b["id"], tgt.name, tp) is not None
                    row["builds"].append({"source_id": b["id"], "name": b.get("name"), "payload": build_payload(b),
                                          "action": "skip-mapped" if done else "reuse" if _key(b.get("name")) in existing else "create",
                                          "target_id": None})
            except ApiError as exc:
                issues.append({"severity": "WARN", "area": "builds",
                               "message": f"Cannot read builds of release '{r.get('name')}': {exc}"})
        rows.append(row)
        ctx.event("release", f"plan-{row['action']}", source_id=r["id"], target_id=row["target_id"], name=r.get("name"))
    return rows, issues


def summarize(rows: list[dict]) -> dict:
    return {"releases": dict(Counter(r["action"] for r in rows)),
            "builds": dict(Counter(b["action"] for r in rows for b in r["builds"]))}


def _fix_status(ctx: RunContext, tgt: QtestClient, tp: int, row: dict, created: dict) -> str:
    """Set the source status when it differs from what the new release got (best effort, never fatal)."""
    want = row.get("status_value")
    if not want:
        return ""
    have_value, _ = _status(created)
    if have_value == want:
        return ""
    field = next((p for p in created.get("properties") or [] if _key(p.get("field_name")) == "status"), None)
    if not field or field.get("field_id") is None:
        return "status not set: target release does not report a Status field"
    try:
        repeat_if_unsure(lambda: tgt.update_release(tp, created["id"], {"name": row["payload"]["name"],
                                               "properties": [{"field_id": field["field_id"], "field_value": want}]}))
    except ApiError as exc:
        ctx.log.warning("release '%s': status '%s' not set: %s", row["name"], row["status"], exc)
        return f"status not set: {exc}"
    return ""


def apply_releases(ctx: RunContext, tgt: QtestClient, db: StateDb, plan: dict, aborted: str = "") -> tuple[list[dict], str]:
    src_name, sp = plan["source"]["instance"], plan["source"]["project_id"]
    tgt_name, tp = plan["target"]["instance"], plan["target"]["project_id"]
    results: list[dict] = []
    ctx.log.info("Applying %d release(s)", len(plan["releases"]))
    consecutive = 0
    for plan_row in plan["releases"]:
        if plan_row.get("stale_target_id"):
            db.forget_target_object("release", tgt_name, tp, plan_row["stale_target_id"])
        row = {**plan_row, "builds": [], "status_note": "", "error": "", "result": plan_row["action"]}
        if aborted:
            row.update(result="not-attempted", error=aborted)
        elif plan_row["action"] in ("reuse", "skip-mapped"):
            if plan_row["action"] == "reuse":
                db.put_release(src_name, sp, plan_row["source_id"], tgt_name, tp, plan_row["target_id"], plan_row["name"], ctx.run_id)
        elif db.release_target(src_name, sp, plan_row["source_id"], tgt_name, tp):
            row.update(result="skip-mapped", target_id=db.release_target(src_name, sp, plan_row["source_id"], tgt_name, tp))
        else:
            try:
                created = journaled(ctx, tgt, db, "release", src_name, sp, plan_row["source_id"], tgt_name, tp, None, plan_row["name"],
                                   lambda: tgt.create_release(tp, plan_row["payload"]))
                row.update(result="created", target_id=created["id"])
                db.put_release(src_name, sp, plan_row["source_id"], tgt_name, tp, created["id"], plan_row["name"], ctx.run_id)
                row["status_note"] = _fix_status(ctx, tgt, tp, plan_row, created)
                consecutive = 0
                ctx.log.info("release created  %s", plan_row["name"])
            except (ApiError, KeyError) as exc:
                row.update(result="failed", error=f"HTTP {exc.status}: {exc.detail}" if isinstance(exc, ApiError) and exc.status
                           else str(exc))
                consecutive += 1
                ctx.log.error("release FAILED  %s: %s", plan_row["name"], row["error"])
                if isinstance(exc, ApiError) and exc.status in (401, 403):
                    aborted = f"aborted after authorization failure ({row['error']})"
                elif consecutive >= MAX_CONSECUTIVE_FAILURES:
                    aborted = f"aborted after {MAX_CONSECUTIVE_FAILURES} consecutive release failures"
        for b in plan_row["builds"]:
            brow = {**b, "result": b["action"], "error": ""}
            if not row.get("target_id"):
                brow.update(result="skipped", error="release was not created")
            elif b["action"] == "create" and db.build_target(src_name, sp, b["source_id"], tgt_name, tp):
                brow.update(result="skip-mapped")
            elif b["action"] == "create" and not aborted:
                try:
                    made = journaled(ctx, tgt, db, "build", src_name, sp, b["source_id"], tgt_name, tp, row["target_id"], b["name"],
                                 lambda: tgt.create_build(tp, row["target_id"], b["payload"]))
                    brow.update(result="created", target_id=made["id"])
                    db.put_build(src_name, sp, b["source_id"], tgt_name, tp, made["id"], row["target_id"], b["name"], ctx.run_id)
                except (ApiError, KeyError) as exc:
                    brow.update(result="failed", error=f"HTTP {exc.status}: {exc.detail}" if isinstance(exc, ApiError) and exc.status
                                else str(exc))
                    ctx.log.error("build FAILED  %s / %s: %s", plan_row["name"], b["name"], brow["error"])
            row["builds"].append(brow)
            ctx.event("build", brow["result"], source_id=b["source_id"], target_id=brow.get("target_id"),
                      name=b["name"], release=plan_row["name"], error=brow["error"])
        ctx.event("release", row["result"], source_id=row["source_id"], target_id=row.get("target_id"),
                  name=row["name"], error=row["error"])
        results.append(row)
    return results, aborted


def verify_releases(ctx: RunContext, tgt: QtestClient, tp: int, rows: list[dict]) -> dict:
    """Every release (and build) created or reused by apply must exist in the target with the same dates."""
    report = {"checked": 0, "ok": 0, "missing": [], "date_mismatch": [], "builds_missing": [], "errors": []}
    try:
        present = {r["id"]: r for r in tgt.list_releases(tp)}
    except ApiError as exc:
        report["errors"].append(f"cannot list releases: {exc}")
        return report
    for row in rows:
        if row["result"] not in ("created", "reuse", "skip-mapped") or not row.get("target_id"):
            continue
        report["checked"] += 1
        found = present.get(row["target_id"])
        if found is None:
            report["missing"].append({"name": row["name"], "target_id": row["target_id"]})
            continue
        if row["result"] == "created":
            expected = row["payload"]
            differs = [k for k in ("start_date", "end_date") if expected.get(k) and not _same_instant(expected[k], found.get(k))]
            if differs:
                report["date_mismatch"].append({"name": row["name"], "target_id": row["target_id"],
                                                "expected": {k: expected[k] for k in differs},
                                                "actual": {k: found.get(k) for k in differs}})
                continue
        report["ok"] += 1
        wanted = [b for b in row["builds"] if b["result"] in ("created", "skip-mapped", "reuse")]
        if wanted:
            try:
                names = Counter(_key(b.get("name")) for b in tgt.list_builds(tp, row["target_id"]))
            except ApiError as exc:
                report["errors"].append(f"builds of release {row['name']}: {exc}")
                continue
            for b in wanted:
                if names.get(_key(b["name"]), 0) == 0:
                    report["builds_missing"].append({"release": row["name"], "build": b["name"]})
    ctx.log.info("Releases verified: %d checked, %d ok, %d missing, %d with different dates", report["checked"],
                 report["ok"], len(report["missing"]), len(report["date_mismatch"]))
    return report


def _not_before(a, b) -> bool:
    from datetime import datetime
    try:
        return datetime.fromisoformat(str(a)) >= datetime.fromisoformat(str(b))
    except ValueError:
        return True


def _same_instant(a, b) -> bool:
    from datetime import datetime
    try:
        return datetime.fromisoformat(str(a)) == datetime.fromisoformat(str(b))
    except ValueError:
        return str(a) == str(b)
