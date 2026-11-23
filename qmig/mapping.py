"""Read-only mapping checks between two instances: execution statuses and releases.

IDs differ per instance, so everything is matched by name.  Users are not migrated.
Nothing here writes.
"""
from __future__ import annotations

from typing import Any

from .client import ApiError, QtestClient
from .runlog import RunContext


def _key(value: Any) -> str:
    return str(value or "").strip().casefold()


def _issue(severity: str, area: str, message: str) -> dict:
    return {"severity": severity, "area": area, "message": message}


def map_statuses(source: list[dict], target: list[dict]) -> dict:
    by_name = {_key(s.get("name")): s for s in target}
    rows, issues = [], []
    for s in source:
        t = by_name.get(_key(s.get("name")))
        rows.append({"source_id": s["id"], "name": s.get("name"), "target_id": t["id"] if t else None})
        if t is None:
            issues.append(_issue("WARN", "execution status",
                                 f"Status '{s.get('name')}' does not exist in the target; executions with it cannot be migrated"))
    return {"rows": rows, "issues": issues}


def plan_releases(source: list[dict], target: list[dict]) -> dict:
    by_name: dict[str, dict] = {}
    for t in target:
        by_name.setdefault(_key(t.get("name")), t)
    rows, issues, seen = [], [], set()
    for s in source:
        name = _key(s.get("name"))
        t = by_name.get(name)
        rows.append({"source_id": s["id"], "name": s.get("name"), "start_date": (s.get("start_date") or "")[:10],
                     "end_date": (s.get("end_date") or "")[:10], "action": "exists" if t else "create",
                     "target_id": t["id"] if t else None})
        if name in seen:
            issues.append(_issue("WARN", "release", f"Source has more than one release named '{s.get('name')}'"))
        seen.add(name)
    return {"rows": rows, "issues": issues}


def preflight_mappings(ctx: RunContext, src: QtestClient, tgt: QtestClient, sp: int, tp: int) -> dict:
    """Run the checks; a failing endpoint becomes a WARN so preflight still completes."""
    out: dict[str, Any] = {"issues": []}
    checks = (("statuses", lambda: map_statuses(src.execution_statuses(sp), tgt.execution_statuses(tp))),
              ("releases", lambda: plan_releases(src.list_releases(sp), tgt.list_releases(tp))))
    for name, check in checks:
        try:
            result = check()
        except ApiError as exc:
            out["issues"].append(_issue("WARN", name, f"Could not check {name}: {exc}"))
            ctx.log.warning("Could not check %s: %s", name, exc)
            continue
        out[name] = result
        out["issues"] += result["issues"]
    if "releases" in out:
        r = out["releases"]["rows"]
        ctx.log.info("Releases: %d in source, %d would be created in the target", len(r), sum(x["action"] == "create" for x in r))
    return out
