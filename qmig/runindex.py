"""Where run folders live and the overview files built from them.

Runs are grouped by project pair so 69 projects do not end up in one long list::

    output/runs/hs-25804__hcsc-26256/20261103-014905-plan/     (one folder per run)
    output/runs/hs-25804__hcsc-26256/status.md                 (latest plan / apply / verify of this pair)
    output/runs/index.csv                                      (one row per run, all pairs)

``find_run_dir`` locates a run by its id, so commands such as ``apply --plan <id>`` need no path.  Run folders
that sit directly in ``output/runs`` (older layout) are still found.
"""
from __future__ import annotations

import csv
import json
import re
import sqlite3
from pathlib import Path

from .config import ConfigError, Paths

RUN_ID = re.compile(r"^\d{8}-\d{6}-[a-z0-9-]+$")
INDEX_COLUMNS = ["started", "run_id", "mode", "label", "source", "source_project", "source_name", "target",
                 "target_project", "target_name", "status", "folder"]
MODES = ("preflight", "plan", "apply", "verify", "links-capture", "links-plan", "links-apply", "links-verify")


def pair_folder(src: str, sp: int, tgt: str, tp: int) -> str:
    return f"{src}-{sp}__{tgt}-{tp}"


def clean_label(label: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (label or "").lower()).strip("-")[:30]


def find_run_dir(runs_dir: Path, run_id: str) -> Path:
    """The folder of a run, wherever it is; a clear error for an unknown or ambiguous id."""
    if "/" in run_id or "\\" in run_id or run_id in ("", ".", ".."):
        raise ConfigError(f"'{run_id}' is not a run id (use the id printed by the command, e.g. 20260101-120000-plan)")
    found = [p for p in [runs_dir / run_id, *sorted(runs_dir.glob(f"*/{run_id}"))] if p.is_dir()]
    if not found:
        raise FileNotFoundError(f"No run '{run_id}' under {runs_dir}")
    if len(found) > 1:
        raise ConfigError(f"Run id '{run_id}' exists in several folders: " + ", ".join(str(p) for p in found))
    return found[0]


def write_run_info(run_dir: Path, info: dict) -> None:
    (run_dir / "run.json").write_text(json.dumps(info, indent=2), encoding="utf-8")


def _project_names(run_dir: Path) -> tuple[str, str]:
    for name in ("plan.json", "result.json", "preflight.json", "links_plan.json", "links_result.json"):
        path = run_dir / name
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                return (data.get("source") or {}).get("project") or "", (data.get("target") or {}).get("project") or ""
            except (ValueError, OSError):
                continue
    return "", ""


def _statuses(db_path: Path) -> dict[str, str]:
    if not db_path.is_file():
        return {}
    try:
        with sqlite3.connect(db_path) as conn:
            return {r[0]: r[1] for r in conn.execute("SELECT run_id, status FROM runs")}
    except sqlite3.Error:
        return {}


def collect(paths: Paths) -> list[dict]:
    statuses = _statuses(paths.db)
    rows = []
    for info_file in sorted(paths.runs.glob("*/*/run.json")):
        try:
            info = json.loads(info_file.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        sname, tname = _project_names(info_file.parent)
        rows.append({"started": info.get("started", ""), "run_id": info["run_id"], "mode": info.get("mode", ""),
                     "label": info.get("label", ""), "source": info["source"], "source_project": info["source_project"],
                     "source_name": sname.strip(), "target": info["target"], "target_project": info["target_project"],
                     "target_name": tname.strip(), "status": statuses.get(info["run_id"], ""),
                     "folder": str(info_file.parent.relative_to(paths.runs)).replace("\\", "/")})
    known: dict[str, tuple[str, str]] = {}  # a run that does not record project names borrows them from its pair
    for row in rows:
        folder = row["folder"].split("/")[0]
        if row["source_name"] or row["target_name"]:
            known.setdefault(folder, (row["source_name"], row["target_name"]))
    for row in rows:
        names = known.get(row["folder"].split("/")[0], ("", ""))
        row["source_name"], row["target_name"] = row["source_name"] or names[0], row["target_name"] or names[1]
    rows.sort(key=lambda r: r["run_id"], reverse=True)
    return rows


def refresh_index(paths: Paths) -> None:
    """Rewrite ``index.csv`` and every pair's ``status.md``.  Never raises: this is a convenience."""
    try:
        rows = collect(paths)
        with open(paths.runs / "index.csv", "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=INDEX_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        pairs: dict[str, list[dict]] = {}
        for row in rows:
            pairs.setdefault(row["folder"].split("/")[0], []).append(row)
        for folder, items in pairs.items():
            first = items[0]
            names = next(((r["source_name"], r["target_name"]) for r in items if r["source_name"] or r["target_name"]), ("", ""))
            lines = [f"# {first['source']} project {first['source_project']} -> {first['target']} project {first['target_project']}", "",
                     f"- Source project: {names[0] or 'unknown'}", f"- Target project: {names[1] or 'unknown'}", "",
                     "## Latest run of each kind", "", "| Kind | Run | Status |", "|---|---|---|"]
            for mode in MODES:
                latest = next((r for r in items if r["mode"] == mode), None)
                if latest:
                    lines.append(f"| {mode} | `{latest['run_id']}` | {latest['status'] or '-'} |")
            stopped: list[dict] = []
            for mode in MODES:
                latest = next((r for r in items if r["mode"] == mode), None)
                if latest and latest["status"] == "interrupted":
                    stopped.append(latest)
            if stopped:
                lines += ["", "## Interrupted: run again to continue", ""]
                for r in stopped:
                    command = ""
                    try:
                        command = json.loads((paths.runs / r["folder"] / "interrupted.json").read_text(encoding="utf-8")).get("resume_command", "")
                    except (ValueError, OSError):
                        pass
                    lines.append(f"- `{r['run_id']}` ({r['mode']}) stopped because the network did not come back."
                                 + (f" Continue with: `{command}`" if command else " Run the same command again."))
            lines += ["", "## All runs (newest first)", "", "| Run | Label | Status |", "|---|---|---|"]
            lines += [f"| `{r['run_id']}` | {r['label'] or ''} | {r['status'] or '-'} |" for r in items]
            (paths.runs / folder / "status.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:  # noqa: BLE001 - an overview file must never break a migration
        pass
