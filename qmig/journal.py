"""Settle writes whose answer was lost (network cut while writing).

Before creating anything in the target, ``apply`` saves an *intent* in SQLite and closes it after the answer.  An
intent that is still ``open`` means "the target may have created it".  The target is then searched for that object
(by name in its folder, release or cycle; logs by start time; files by name on their object):

* exactly one object that is not yet in the mapping: it is adopted, so it is not created twice and still gets its
  files, step files and approval;
* none: the write did not happen, the item is created normally;
* more than one candidate, or the target cannot be searched: nothing is guessed.  The item is reported as failed
  with the explanation, and the user decides in the target UI.

Folders and test cases are settled in one pass when ``apply`` starts; every other kind is settled just before that
very item would be created (``journaled``), whether in the same run or a later one.
"""
from __future__ import annotations

import json
from typing import Callable

from .client import ApiError, QtestClient
from .db import StateDb
from .runlog import RunContext


def _key(name) -> str:  # same comparison as the planner (case and surrounding blanks do not matter)
    return str(name or "").strip().casefold()


def _same_name(row: dict, intent) -> bool:
    return _key(row.get("name")) == _key(intent["name"])


def _same_start(row: dict, intent) -> bool:
    from .logs import same_instant
    return same_instant(row.get("exe_start_date"), intent["name"])


def _attachments_of(tgt: QtestClient, tp: int, intent) -> list[dict]:
    extra = json.loads(intent["extra"] or "{}")
    if intent["kind"] == "stepfile":  # step files are only listed inside the test case
        step = next((s for s in tgt.test_case_steps(tp, extra["target_test_case_id"]) if s.get("order") == extra["order"]), None)
        return list((step or {}).get("attachments") or [])
    return tgt.list_attachments(tp, intent["parent_type"], intent["parent_id"])


# kind -> (list the candidates, compare with the intent, mapping table, extra WHERE on that table)
LOOKUPS: dict[str, tuple[Callable, Callable, str, str]] = {
    "folder": (lambda t, tp, i: t.list_modules(tp, i["parent_id"]), _same_name, "folder_map", ""),
    "testcase": (lambda t, tp, i: t.list_testcases(tp, i["parent_id"], expand=False), _same_name, "testcase_map", ""),
    "release": (lambda t, tp, i: t.list_releases(tp), _same_name, "release_map", ""),
    "build": (lambda t, tp, i: t.list_builds(tp, i["parent_id"]), _same_name, "build_map", ""),
    "cycle": (lambda t, tp, i: t.list_cycles(tp, i["parent_id"], i["parent_type"]), _same_name, "cycle_map", "kind='cycle'"),
    "suite": (lambda t, tp, i: t.list_suites(tp, i["parent_id"], i["parent_type"]), _same_name, "cycle_map", "kind='suite'"),
    "run": (lambda t, tp, i: t.list_runs(tp, i["parent_id"], i["parent_type"]), _same_name, "run_map", ""),
    "log": (lambda t, tp, i: t.list_logs(tp, i["parent_id"]), _same_start, "log_map", ""),
    "attachment": (_attachments_of, lambda row, i: (row.get("name") or "") == i["name"], "attachment_map", ""),
    "stepfile": (_attachments_of, lambda row, i: (row.get("name") or "") == i["name"], "attachment_map", ""),
}


def _taken_ids(db: StateDb, tgt: str, tp: int, kind: str) -> set[int]:
    _, _, table, where = LOOKUPS[kind]
    column = "target_attachment_id" if table == "attachment_map" else "target_id"
    sql = f"SELECT {column} FROM {table} WHERE target_instance=? AND target_project=?" + (f" AND {where}" if where else "")
    return {r[0] for r in db.conn.execute(sql, (tgt, tp))}


def settle_intent(ctx: RunContext, tgt: QtestClient, db: StateDb, intent, source_pids: dict[int, str] | None = None
                  ) -> tuple[str, dict | None, str]:
    """Look in the target for the object of one open intent.

    Returns ``("adopted", object, "")``, ``("not-created", None, "")`` or ``("unresolved", None, reason)``.
    """
    kind, src_name, sp, tgt_name, tp = (intent["kind"], intent["source_instance"], intent["source_project"],
                                        intent["target_instance"], intent["target_project"])
    label = f"{kind} '{intent['name']}' (source {intent['source_id']})"
    lister, same, _, _ = LOOKUPS[kind]
    try:
        rows = lister(tgt, tp, intent)
    except ApiError as exc:
        if exc.status == 404:  # the folder, release or object does not exist (any more): nothing was created in it
            db.close_intent(intent["id"], "not-created")
            return "not-created", None, ""
        reason = f"a write was cut off and the target cannot be searched to settle it ({exc}); check {label} in the target"
        db.close_intent(intent["id"], "unresolved")
        ctx.log.error("Cannot settle %s: %s", label, reason)
        return "unresolved", None, reason
    taken = _taken_ids(db, tgt_name, tp, kind)
    candidates = [r for r in rows if same(r, intent) and r.get("id") not in taken]
    if len(candidates) == 1:
        found = candidates[0]
        if kind == "folder":
            db.put_folder(src_name, sp, intent["source_id"], tgt_name, tp, found["id"], intent["name"], ctx.run_id)
        elif kind == "testcase":
            db.put_testcase(src_name, sp, intent["source_id"], (source_pids or {}).get(intent["source_id"]), tgt_name, tp,
                            found["id"], found.get("pid"), intent["name"], ctx.run_id)
        db.close_intent(intent["id"], "adopted", found["id"])
        ctx.log.warning("A write was cut off but succeeded: adopted %s as target %s", label, found["id"])
        ctx.event("journal", "adopted", kind=kind, source_id=intent["source_id"], target_id=found["id"])
        return "adopted", found, ""
    if not candidates:
        db.close_intent(intent["id"], "not-created")
        ctx.log.info("A write was cut off and did not happen: %s will be created", label)
        return "not-created", None, ""
    reason = (f"a write was cut off and {len(candidates)} objects with this name exist under the same parent in the target "
              f"(ids {', '.join(str(c['id']) for c in candidates)}); check them in the target and remove the extra ones, "
              "then apply again")
    db.close_intent(intent["id"], "unresolved")
    ctx.log.error("Cannot settle %s: %s", label, reason)
    return "unresolved", None, reason


def reconcile_intents(ctx: RunContext, tgt: QtestClient, db: StateDb, src_name: str, sp: int, tgt_name: str,
                      tp: int, source_pids: dict[int, str] | None = None) -> dict[tuple[str, int], str]:
    """Settle every open or unresolved intent of this project pair.  Returns ``{(kind, source_id): reason}`` for the
    ambiguous ones."""
    unresolved: dict[tuple[str, int], str] = {}
    for kind in ("folder", "testcase"):
        for intent in db.intents(tgt_name, tp, "open", kind) + db.intents(tgt_name, tp, "unresolved", kind):
            if intent["source_instance"] != src_name or intent["source_project"] != sp:
                continue
            outcome, _, reason = settle_intent(ctx, tgt, db, intent, source_pids)
            if outcome == "unresolved":
                unresolved[(kind, intent["source_id"])] = reason
    return unresolved


def create_settled(ctx: RunContext, tgt: QtestClient, db: StateDb, intent_id: int, create, source_pids=None) -> dict:
    """Run ``create()``.  If its reply was lost (``ApiError.ambiguous``), look in the target first: adopt the object if
    it exists, create it again if it does not, and refuse to guess when several candidates exist."""
    try:
        return create()
    except ApiError as exc:
        if not exc.ambiguous:
            raise
        outcome, found, reason = settle_intent(ctx, tgt, db, db.intent(intent_id), source_pids)
        if outcome == "adopted":
            return found
        if outcome == "unresolved":
            raise ApiError(exc.method, exc.path, None, reason, ambiguous=True) from None  # keep the intent open
        db.conn.execute("UPDATE intents SET status='open' WHERE id=?", (intent_id,))
        db.conn.commit()
        return create()  # the first write did not happen; a second cut-off stays an open intent for the next run


def journaled(ctx: RunContext, tgt: QtestClient, db: StateDb, kind: str, src_name: str, sp: int, source_id: int, tgt_name: str,
              tp: int, parent_id: int | None, name: str, create: Callable[[], dict], *, parent_type: str | None = None,
              extra: dict | None = None) -> dict:
    """Create one object with a write intent around it.  Returns what ``create()`` returned, or the object an earlier
    cut-off write had already created.  Raises ``ApiError`` when the write failed or cannot be settled."""
    for old in db.pending_intents(kind, src_name, sp, source_id, tgt_name, tp):  # left by an earlier, cut-off run
        outcome, found, reason = settle_intent(ctx, tgt, db, old)
        if outcome == "adopted":
            return found
        if outcome == "unresolved":
            raise ApiError("POST", kind, None, reason)
    intent = db.open_intent(kind, src_name, sp, source_id, tgt_name, tp, parent_id, name, ctx.run_id, parent_type,
                            json.dumps(extra) if extra else None)
    try:
        created = create_settled(ctx, tgt, db, intent, create)
    except ApiError as exc:
        if not exc.ambiguous:
            db.close_intent(intent, "failed")  # the server answered: nothing was created
        raise
    db.close_intent(intent, "done", (created or {}).get("id"))
    return created


def repeat_if_unsure(call: Callable[[], dict]) -> dict:
    """For writes that are harmless to repeat (approving, setting a status): once more after a lost reply."""
    try:
        return call()
    except ApiError as exc:
        if exc.ambiguous:
            return call()
        raise
