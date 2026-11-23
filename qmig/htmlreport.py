"""One-page status report for stakeholders: ``qmig report html``.

Built from what a batch already stores (``batch.json``) and the files of its runs (``plan.json``, ``result.json``,
``verify.json``, ``links_*.json``).  The result is one self-contained HTML file (inline CSS and SVG, no scripts, no
external resources) that opens in any browser and prints to a single PDF page.  Mostly numbers and tables, two charts
(projects completed over time, where the projects stand) and the timings.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

from .batch import STEPS, Batch
from .config import Paths
from .network import format_seconds
from .runindex import find_run_dir

CREATED = ("created", "approved", "linked")
PRESENT = ("skip-mapped", "reuse", "reused", "reuse-mapped", "skip-exists", "exists")
PROBLEM = ("failed", "not-attempted", "too-large")
LEFT_OUT = ("deferred", "skipped", "skip-no-testcase")
TYPES = (("folders", "Folders"), ("testcases", "Test cases"), ("attachments", "Test case files"), ("step_files", "Step files"),
         ("approvals", "Approvals"), ("releases", "Releases"), ("builds", "Builds"), ("cycles", "Test cycles"),
         ("suites", "Test suites"), ("runs", "Test runs"), ("logs", "Test logs"), ("links", "Requirement links"))
PLANNED_KEYS = ("create", "to-copy", "to-approve", "link")
NOT_MIGRATED = ("Users: items are created by the API user, the original owners are kept as text only where noted",
                "Requirements themselves: only the links from test cases to requirements are restored",
                "Everything in a Recycle Bin (test case folders and test cycles)",
                "Files attached to test cycles, suites and runs that HS does not let the token read")


def _read(paths: Paths, run_id: str | None, name: str) -> dict:
    if not run_id:
        return {}
    try:
        return json.loads((find_run_dir(paths.runs, run_id) / name).read_text(encoding="utf-8"))
    except (OSError, ValueError, FileNotFoundError):
        return {}


def _when(text: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(text) if text else None
    except ValueError:
        return None


def _bucket(counts: dict) -> dict:
    """created / present / left out / problems from a ``{status: n}`` dict."""
    return {"created": sum(counts.get(k, 0) for k in CREATED), "present": sum(counts.get(k, 0) for k in PRESENT),
            "left_out": sum(counts.get(k, 0) for k in LEFT_OUT), "failed": sum(counts.get(k, 0) for k in PROBLEM)}


def _state(entry: dict) -> str:
    """Where a project stands, for the status bar."""
    apply, plan = entry["apply"].get("status"), entry["plan"].get("status")
    if any(entry[k].get("status") == "interrupted" for k in STEPS):
        return "Interrupted"
    if apply == "applied" and entry["verify"].get("status") == "passed":
        return "Verified"
    if apply == "applied" and entry["verify"].get("status") == "failed":
        return "Verify failed"
    if apply == "applied":
        return "Applied, not verified"
    if apply in ("applied-with-errors", "failed"):
        return "Errors"
    if plan == "planned":
        return "Planned"
    return "Not planned" if plan == "failed" else "Waiting"


STATES = ("Verified", "Applied, not verified", "Verify failed", "Errors", "Interrupted", "Planned", "Not planned", "Waiting")


def collect(paths: Paths, batch: Batch, deadline: datetime | None = None) -> dict:
    projects, totals = [], {key: {"planned": 0, "created": 0, "present": 0, "left_out": 0, "failed": 0, "checked": 0, "ok": 0}
                            for key, _ in TYPES}
    attention: list[tuple[str, str]] = []
    interruptions = waited = 0
    for e in batch.projects:
        plan = _read(paths, e["plan"].get("run_id"), "plan.json")
        result = _read(paths, e["apply"].get("run_id"), "result.json")
        verify = _read(paths, e["verify"].get("run_id"), "verify.json")
        links_result = _read(paths, e["links_apply"].get("run_id"), "links_result.json")
        links_verify = _read(paths, e["links_verify"].get("run_id"), "links_verify.json")
        label = f"{e['source_project']} -> {e['target_project']}" + (f" (folder {e['module_id']})" if e.get("module_id") else "")
        names = (plan.get("source", {}).get("project") or "", plan.get("target", {}).get("project") or "")
        plan_counts, counts = plan.get("counts") or e["plan"].get("counts") or {}, result.get("counts") or {}
        for key, _ in TYPES:
            if key == "links":
                planned_counts = (e["links_plan"].get("counts") or {}).get("actions", {})
                done = (links_result.get("counts") or {}).get("actions", {})
            else:
                planned_counts, done = plan_counts.get(key, {}), counts.get(key, {})
            planned = sum(planned_counts.get(k, 0) for k in PLANNED_KEYS)
            b = _bucket(done)
            t = totals[key]
            t["planned"] += planned
            for name in ("created", "present", "left_out", "failed"):
                t[name] += b[name]
            section = links_verify if key == "links" else verify if key == "testcases" else verify.get(key)
            if isinstance(section, dict) and "checked" in section:
                t["checked"] += section.get("checked", 0)
                t["ok"] += section.get("ok", 0)
        for step, name in (("apply", "result.json"), ("fields_apply", "fields_result.json"), ("links_apply", "links_result.json")):
            for api in (_read(paths, e[step].get("run_id"), name).get("api") or {}).values():
                interruptions += api.get("network_interruptions", 0)
                waited += api.get("network_wait_seconds", 0)
        state = _state(e)
        failed_items = sum(_bucket(v)["failed"] for k, v in counts.items() if isinstance(v, dict))
        for step in STEPS:
            if e[step].get("error"):
                attention.append((label, f"{step.replace('_', ' ')}: {e[step]['error']}"))
        if failed_items:
            attention.append((label, f"{failed_items} item(s) failed: run `batch apply` again to retry only those"))
        if result.get("aborted"):
            attention.append((label, str(result["aborted"])))
        dropped = [i for i in plan.get("issues", []) if i.get("area") == "fields" and "dropped" in i.get("message", "")]
        if dropped:
            attention.append((label, f"{len(dropped)} field value group(s) have no match in HCSC and were dropped, e.g. "
                              + dropped[0]["message"][:110] + " ..."))
        excluded = [i for i in plan.get("issues", []) if i.get("area") == "scope"]
        for issue in excluded:
            attention.append((label, issue["message"]))
        links_actions = (e["links_plan"].get("counts") or {}).get("actions", {})
        for kind, text in (("not-found", "link(s) to requirements that are not in HCSC yet"),
                           ("ambiguous", "link(s) with several matching requirements (not guessed)")):
            if links_actions.get(kind):
                attention.append((label, f"{links_actions[kind]} {text}"))
        projects.append({"n": e["n"], "label": label, "names": names, "state": state, "plan": e["plan"].get("status") or "-",
                         "apply": e["apply"].get("status") or "-", "verify": e["verify"].get("status") or "-",
                         "links": e["links_verify"].get("status") or e["links_apply"].get("status") or e["links_plan"].get("status") or "-",
                         "seconds": e["apply"].get("seconds", 0), "finished": _when(e["apply"].get("finished")),
                         "created": sum(_bucket(v)["created"] for k, v in counts.items() if isinstance(v, dict)),
                         "remaining": sum(plan_counts.get(k, {}).get(x, 0) for k in ("folders", "testcases", "attachments")
                                          for x in ("create", "to-copy")) if state in ("Planned", "Errors", "Interrupted") else 0})
    done_projects = [p for p in projects if p["apply"] in ("applied", "applied-with-errors")]
    seconds = sum(p["seconds"] for p in done_projects)
    created = sum(p["created"] for p in projects)
    speed = created / seconds * 60 if seconds and created else 0
    remaining = sum(p["remaining"] for p in projects)
    forecast = None
    if speed and remaining:
        forecast = datetime.now(timezone.utc) + timedelta(minutes=remaining / speed)
    checked, ok = sum(t["checked"] for t in totals.values()), sum(t["ok"] for t in totals.values())
    return {"batch": batch.data, "projects": projects, "totals": totals, "attention": attention, "states": {s: sum(p["state"] == s for p in projects) for s in STATES},
            "created": created, "seconds": seconds, "speed": speed, "remaining": remaining, "forecast": forecast,
            "verified_share": (ok / checked * 100) if checked else None, "checked": checked, "ok": ok,
            "interruptions": interruptions, "waited": waited, "deadline": deadline,
            "longest": max(done_projects, key=lambda p: p["seconds"], default=None),
            "average": seconds / len(done_projects) if done_projects else 0}


# ------------------------------------------------------------------------------ rendering
CSS = """
:root{--bg:#fff;--fg:#1c2530;--muted:#5d6b7a;--line:#d9e0e7;--card:#f4f7fa;--ok:#1f8a4c;--warn:#b7791f;--bad:#c0392b;--info:#2b6cb0;--grey:#98a4b3}
@media (prefers-color-scheme: dark){:root{--bg:#10151b;--fg:#e6ebf0;--muted:#97a4b2;--line:#27313b;--card:#18202a;--ok:#4cc38a;--warn:#e0a84a;--bad:#ef6f61;--info:#6aaee8;--grey:#6b7886}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:1040px;margin:0 auto;padding:20px 16px 28px}h1{font-size:20px;margin:0 0 2px}h2{font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:22px 0 8px}
.sub{color:var(--muted);font-size:12.5px}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;margin-top:14px}
.card{background:var(--card);border-radius:8px;padding:10px 12px}.card b{display:block;font-size:22px;font-variant-numeric:tabular-nums}.card span{color:var(--muted);font-size:12px}
.cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:18px}table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:5px 8px;border-bottom:1px solid var(--line);text-align:right;font-variant-numeric:tabular-nums}th:first-child,td:first-child,th.l,td.l{text-align:left}
th{color:var(--muted);font-weight:600;font-size:12px}.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}.muted{color:var(--muted)}
.bar{display:flex;height:22px;border-radius:5px;overflow:hidden;background:var(--line)}.bar i{display:block;height:100%}
.legend{display:flex;flex-wrap:wrap;gap:4px 14px;margin-top:8px;font-size:12px}.legend span::before{content:"";display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px;background:var(--c)}
svg{width:100%;height:auto;display:block}svg text{fill:var(--muted);font-size:11px}.att li{margin:3px 0}.att{padding-left:18px;margin:0}
.t{display:inline-block;height:7px;border-radius:3px;background:var(--info);vertical-align:middle}
@media print{@page{size:A4;margin:10mm}main{padding:0}body{font-size:11px}h2{margin:12px 0 5px}.card b{font-size:18px}}
"""
STATE_COLORS = {"Verified": "var(--ok)", "Applied, not verified": "var(--info)", "Verify failed": "var(--bad)", "Errors": "var(--bad)",
                "Interrupted": "var(--warn)", "Planned": "var(--grey)", "Not planned": "var(--warn)", "Waiting": "var(--line)"}


def _n(value) -> str:
    return f"{value:,}".replace(",", " ") if isinstance(value, int) else str(value)


def _line_chart(data: dict) -> str:
    projects = data["projects"]
    points = sorted(p["finished"] for p in projects if p["finished"] and p["apply"] in ("applied", "applied-with-errors"))
    created = _when(data["batch"]["created"]) or (points[0] if points else datetime.now(timezone.utc))
    end = max([created + timedelta(hours=1)] + points + ([data["deadline"]] if data["deadline"] else []))
    total = max(len(projects), 1)
    w, h, left, bottom = 560, 190, 34, 24
    span = (end - created).total_seconds() or 1

    def x(t): return left + (t - created).total_seconds() / span * (w - left - 10)
    def y(v): return 10 + (h - 10 - bottom) * (1 - v / total)
    parts = [f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="Projects completed over time">']
    for frac in (0, .5, 1):
        v = round(total * frac)
        parts.append(f'<line x1="{left}" x2="{w - 10}" y1="{y(v):.0f}" y2="{y(v):.0f}" stroke="var(--line)"/><text x="{left - 6}" y="{y(v) + 4:.0f}" text-anchor="end">{v}</text>')
    for frac in (0, .5, 1):
        t = created + timedelta(seconds=span * frac)
        parts.append(f'<text x="{x(t):.0f}" y="{h - 6}" text-anchor="{"start" if frac == 0 else "end" if frac == 1 else "middle"}">{t.astimezone().strftime("%d %b %H:%M")}</text>')
    if data["deadline"]:
        parts.append(f'<line x1="{x(created):.0f}" y1="{y(0):.0f}" x2="{x(data["deadline"]):.0f}" y2="{y(total):.0f}" stroke="var(--grey)" stroke-dasharray="5 4"/>'
                     f'<text x="{w - 10}" y="14" text-anchor="end">dashed: target, all {total} by {data["deadline"].astimezone().strftime("%d %b")}</text>')
    if points:
        path = f"M{x(created):.1f},{y(0):.1f} " + " ".join(f"L{x(t):.1f},{y(i):.1f} L{x(t):.1f},{y(i + 1):.1f}" for i, t in enumerate(points))
        parts.append(f'<path d="{path}" fill="none" stroke="var(--ok)" stroke-width="2"/>')
        for i, t in enumerate(points):
            parts.append(f'<circle cx="{x(t):.1f}" cy="{y(i + 1):.1f}" r="3" fill="var(--ok)"/>')
    else:
        parts.append(f'<text x="{w / 2}" y="{h / 2}" text-anchor="middle">no project applied yet</text>')
    return "".join(parts) + "</svg>"


def render(data: dict) -> str:
    b, p = data["batch"], data["projects"]
    states = data["states"]
    verified = states.get("Verified", 0)
    share = f"{data['verified_share']:.1f}%" if data["verified_share"] is not None else "-"
    forecast = (data["forecast"].astimezone().strftime("%d %b %H:%M") + f" ({_n(data['remaining'])} items left)") if data["forecast"] \
        else ("nothing left to apply" if data["speed"] else "no data yet")
    cards = [(f"{verified} / {len(p)}", "projects verified"), (share, f"verified correct ({_n(data['ok'])} of {_n(data['checked'])} checks)"),
             (_n(data["created"]), "items created"), (forecast, "forecast finish at the current speed (one project at a time)")]
    bar = "".join(f'<i style="width:{states[s] / max(len(p), 1) * 100:.2f}%;background:{STATE_COLORS[s]}" title="{escape(s)}: {states[s]}"></i>'
                  for s in STATES if states[s])
    legend = "".join(f'<span style="--c:{STATE_COLORS[s]}">{escape(s)} {states[s]}</span>' for s in STATES if states[s])
    rows = ""
    for key, title in TYPES:
        t = data["totals"][key]
        if not any(t.values()):
            continue
        ver = f"{_n(t['ok'])} / {_n(t['checked'])}" if t["checked"] else "-"
        rows += (f"<tr><td>{title}</td><td>{_n(t['planned'])}</td><td>{_n(t['created'])}</td><td>{_n(t['present'])}</td>"
                 f"<td>{_n(t['left_out'])}</td><td class=\"{'bad' if t['failed'] else ''}\">{_n(t['failed'])}</td><td>{ver}</td></tr>")
    longest = data["longest"]
    timing = [("Total time applying", format_seconds(data["seconds"]) if data["seconds"] else "-"),
              ("Average per project", format_seconds(data["average"]) if data["average"] else "-"),
              ("Longest project", f"{escape(longest['label'])}: {format_seconds(longest['seconds'])}" if longest and longest["seconds"] else "-"),
              ("Speed", f"{data['speed']:.0f} items per minute" if data["speed"] else "-"),
              ("Network interruptions", f"{data['interruptions']} (time lost {format_seconds(data['waited'])})" if data["interruptions"] else "none")]
    top = max((x["seconds"] for x in p), default=0) or 1
    project_rows = "".join(
        f"<tr><td class=\"l\">{x['n']}</td><td class=\"l\">{escape(x['label'])}<br><span class=\"muted\">{escape(' -> '.join(n for n in x['names'] if n))}</span></td>"
        f"<td class=\"l\">{escape(x['state'])}</td><td>{_n(x['created'])}</td><td>{escape(x['links'])}</td>"
        f"<td class=\"l\"><span class=\"t\" style=\"width:{x['seconds'] / top * 70:.0f}px\"></span> {format_seconds(x['seconds']) if x['seconds'] else '-'}</td></tr>"
        for x in p)
    attention = "".join(f"<li><b>{escape(label)}</b>: {escape(text)}</li>" for label, text in data["attention"][:40]) \
        or "<li>Nothing needs attention.</li>"
    more = f"<li class=\"muted\">... and {len(data['attention']) - 40} more (see the batch status)</li>" if len(data["attention"]) > 40 else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>qTest migration status</title><style>{CSS}</style></head><body><main>
<h1>qTest migration status</h1>
<div class="sub">{escape(b['source'])} to {escape(b['target'])} &middot; batch {escape(b['batch_id'])} &middot; {len(p)} project(s) &middot; made {datetime.now().astimezone().strftime('%d %b %Y %H:%M')}</div>
<div class="cards">{''.join(f'<div class="card"><b>{escape(str(v))}</b><span>{escape(label)}</span></div>' for v, label in cards)}</div>
<div class="cols"><div><h2>Projects completed over time</h2>{_line_chart(data)}</div>
<div><h2>Where the projects stand</h2><div class="bar">{bar}</div><div class="legend">{legend}</div>
<h2>Timings</h2><table>{''.join(f'<tr><td>{a}</td><td>{c}</td></tr>' for a, c in timing)}</table></div></div>
<h2>What was moved</h2>
<table><tr><th>Object type</th><th>Planned</th><th>Created</th><th>Already there</th><th>Left out</th><th>Failed</th><th>Verified ok</th></tr>{rows}</table>
<h2>Projects</h2>
<table><tr><th class="l">#</th><th class="l">Project</th><th class="l">State</th><th>Created</th><th>Links</th><th class="l">Apply time</th></tr>{project_rows}</table>
<h2>Needs attention</h2><ul class="att">{attention}{more}</ul>
<h2>Not migrated by decision</h2><ul class="att">{''.join(f'<li>{escape(x)}</li>' for x in NOT_MIGRATED)}</ul>
</main></body></html>
"""


def write_report(paths: Paths, batch: Batch, out: Path | None = None, deadline: datetime | None = None) -> Path:
    data = collect(paths, batch, deadline)
    target = out or batch.folder / "report.html"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render(data), encoding="utf-8")
    return target
