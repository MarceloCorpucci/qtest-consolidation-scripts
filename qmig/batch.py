"""Many projects in one go: ``qmig batch plan | apply | verify | status``.

A batch is a CSV of project pairs::

    source_project,target_project,module_id
    25804,26256,
    25761,26257,1234567

``module_id`` is optional (a source folder to migrate instead of the whole project).  ``batch plan`` makes one ordinary
``plan`` per row (read-only), ``batch apply`` shows all plans once, asks once, and applies them one after another with the
ordinary ``apply``, ``batch verify`` verifies them.  Nothing new is migrated by the batch itself: every step is the same
command you would run by hand, so reports, run folders, the journal and the network wait all behave the same.

* A project that fails does not stop the others; its error is in the batch status.
* A network stop (``interrupted``, exit code 75) ends the batch cleanly.  Run the same command again: projects that are
  done are skipped, the interrupted one continues with its own plan.
* State lives in ``output/batches/<batch id>/batch.json`` (written after every step); ``status.md`` next to it is the
  readable summary with durations per project.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .client import ApiError
from .config import ConfigError, Paths
from .network import NetworkInterrupted, format_seconds

NETWORK_FLAGS = ("network_retries", "network_retry_interval", "network_retry_max_interval")
STEPS = ("plan", "fields_plan", "fields_apply", "apply", "verify", "links_capture", "links_plan", "links_apply", "links_verify")
COLUMNS = {"source_project": "source_project", "source": "source_project", "target_project": "target_project",
           "target": "target_project", "module_id": "module_id", "module": "module_id"}


class BatchError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def batches_dir(paths: Paths) -> Path:
    return paths.runs.parent / "batches"


# ------------------------------------------------------------------------------ input
def parse_csv(path: Path) -> list[dict]:
    """Rows of ``{"source_project", "target_project", "module_id"}``; clear errors with the line number."""
    if not path.is_file():
        raise BatchError(f"CSV file not found: {path}")
    lines = [(n, l) for n, l in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1)
             if l.strip() and not l.lstrip().startswith("#")]
    if not lines:
        raise BatchError(f"{path} has no rows")
    header = lines[0][1]
    delimiter = max(",;\t", key=header.count)  # Excel in some countries writes ';'
    names = [COLUMNS.get(re.sub(r"[^a-z]+", "_", h.strip().lower()).strip("_")) for h in header.split(delimiter)]
    if "source_project" not in names or "target_project" not in names:
        raise BatchError(f"{path}: the first row must name the columns source_project and target_project "
                         "(and optionally module_id); found: " + header.strip())
    rows, seen = [], {}
    for number, line in lines[1:]:
        cells = next(csv.reader(io.StringIO(line), delimiter=delimiter))
        record = {name: (cells[i].strip() if i < len(cells) else "") for i, name in enumerate(names) if name}
        values = {}
        for key in ("source_project", "target_project", "module_id"):
            text = record.get(key, "")
            if not text and key == "module_id":
                values[key] = None
            elif not text.isdigit():
                raise BatchError(f"{path} line {number}: {key} must be a whole number (got {text!r})")
            else:
                values[key] = int(text)
        key = tuple(values.values())
        if key in seen:
            raise BatchError(f"{path} line {number}: the same row appears on line {seen[key]}")
        seen[key] = number
        rows.append(values)
    if not rows:
        raise BatchError(f"{path} has a header but no rows")
    return rows


# ------------------------------------------------------------------------------ state
class Batch:
    def __init__(self, folder: Path, data: dict):
        self.folder, self.data = folder, data

    @property
    def id(self) -> str:
        return self.data["batch_id"]

    @property
    def projects(self) -> list[dict]:
        return self.data["projects"]

    def save(self) -> None:
        temp = self.folder / "batch.json.tmp"
        temp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        os.replace(temp, self.folder / "batch.json")
        (self.folder / "status.md").write_text(render_markdown(self), encoding="utf-8")

    @classmethod
    def create(cls, paths: Paths, csv_path: Path, rows: list[dict], source: str, target: str, plan_options: list[str],
               fields: bool) -> "Batch":
        batch_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-batch"
        folder = batches_dir(paths) / batch_id
        folder.mkdir(parents=True, exist_ok=True)
        data = {"batch_id": batch_id, "created": _now(), "csv": str(csv_path), "source": source, "target": target,
                "plan_options": plan_options, "fields": fields,
                "projects": [{"n": i, **row, **{k: {} for k in STEPS}}
                             for i, row in enumerate(rows, 1)]}
        batch = cls(folder, data)
        batch.save()
        return batch

    @classmethod
    def load(cls, paths: Paths, batch_id: str | None) -> "Batch":
        root = batches_dir(paths)
        if batch_id in (None, "", "latest"):
            found = sorted(p.name for p in root.glob("*-batch")) if root.is_dir() else []
            if not found:
                raise BatchError("No batch yet: run 'qmig batch plan --csv <file>' first")
            batch_id = found[-1]
        folder = root / batch_id
        if not (folder / "batch.json").is_file():
            raise BatchError(f"No batch '{batch_id}' under {root}")
        data = json.loads((folder / "batch.json").read_text(encoding="utf-8"))
        for entry in data["projects"]:  # a batch made before links were added
            for step in STEPS:
                entry.setdefault(step, {})
        return cls(folder, data)


# ------------------------------------------------------------------------------ one step
def _network_argv(args) -> list[str]:
    out: list[str] = []
    for name in NETWORK_FLAGS:
        value = getattr(args, name, None)
        if value not in (None, ""):
            out += ["--" + name.replace("_", "-"), str(value)]
    return out


def _run_step(batch: Batch, entry: dict, key: str, argv: list[str], command, paths: Paths, ok: str, errors: str,
              summary=None, soft: dict[str, str] | None = None) -> bool:
    """Run one ordinary command for one project and record what happened.  Returns True when it fully succeeded."""
    from . import cli
    from .apply import ApplyError
    from .fieldsetup import FieldSetupError
    from .plan import PreflightError
    from .reqlinks import LinksError

    record = entry[key] = {"status": "running", "started": _now(), "error": "", "run_id": None}
    batch.save()
    cli.LAST_RUN.pop("ctx", None)
    started = time.monotonic()
    code: int | None = None
    try:
        code = command(cli.build_parser().parse_args(argv), paths)
        record["status"] = ok if code == 0 else errors if code == 1 else "failed"
    except NetworkInterrupted as exc:
        record.update(status="interrupted", error=str(exc))
        ctx = cli.LAST_RUN.get("ctx")
        record["run_id"] = ctx.run_id if ctx else None
        record["seconds"] = round(time.monotonic() - started)
        batch.save()
        raise
    except (ConfigError, PreflightError, ApplyError, FieldSetupError, LinksError, ApiError, FileNotFoundError) as exc:
        status = next((s for text, s in (soft or {}).items() if text in str(exc)), None)  # an expected "nothing to do"
        record.update(status=status or "failed", error="" if status else str(exc), note=str(exc) if status else "")
        if status:
            code = 0
    ctx = cli.LAST_RUN.get("ctx")
    record["run_id"] = ctx.run_id if ctx else None
    record["seconds"] = round(time.monotonic() - started)
    record["finished"] = _now()
    if summary and record["run_id"]:
        try:
            record["counts"] = summary(paths, record["run_id"])
        except (OSError, ValueError, KeyError, ConfigError):
            pass
    batch.save()
    return code == 0


def _counts_of(file: str):
    def read(paths: Paths, run_id: str) -> dict:
        from .runindex import find_run_dir
        return json.loads((find_run_dir(paths.runs, run_id) / file).read_text(encoding="utf-8"))["counts"]
    return read


def _label(entry: dict) -> str:
    return f"{entry['source_project']} -> {entry['target_project']}" + (f" (folder {entry['module_id']})" if entry.get("module_id") else "")


def _scope(batch: Batch, entry: dict) -> list[str]:
    return ["--source", batch.data["source"], "--target", batch.data["target"],
            "--source-project", str(entry["source_project"]), "--target-project", str(entry["target_project"])]


# ------------------------------------------------------------------------------ commands
def batch_plan(args, paths: Paths) -> int:
    from . import cli
    rows = parse_csv(Path(args.csv))
    options = [o for o in (args.plan_options or []) if o != "--"]
    net = _network_argv(args)
    try:  # a mistake in the plan options must show before 69 projects are read
        cli.build_parser().parse_args(["plan", "--source-project", "1", "--target-project", "2", *options])
    except SystemExit:
        raise BatchError("the options after '--' are not valid for 'qmig plan' (see 'qmig plan --help')") from None
    batch = Batch.create(paths, Path(args.csv), rows, args.source, args.target, options, args.fields)
    print(f"Batch {batch.id}: {len(rows)} project(s). Planning (read-only) ...")
    failed = 0
    for entry in batch.projects:
        print(f"[{entry['n']}/{len(rows)}] plan {_label(entry)}")
        if args.fields:
            _run_step(batch, entry, "fields_plan", ["fields", "plan", *_scope(batch, entry), *net], cli.cmd_fields, paths,
                      "planned", "planned", _counts_of("fields_plan.json"))
        extra = ["--module-id", str(entry["module_id"])] if entry.get("module_id") else []
        if not _run_step(batch, entry, "plan", ["plan", *_scope(batch, entry), *extra, *options, *net], cli.cmd_plan, paths,
                         "planned", "failed", _counts_of("plan.json")):
            failed += 1
    print()
    print(render_text(batch))
    print(f"\nNext: review the plans, then  python -m qmig batch apply --batch {batch.id}")
    return cli.EXIT_FAILED if failed else cli.EXIT_OK


def _plan_counts(entry: dict) -> tuple[int, int]:
    counts = entry["plan"].get("counts") or {}
    return (counts.get("folders", {}).get("create", 0), counts.get("testcases", {}).get("create", 0))


def _rows_filter(text: str | None, batch: Batch) -> set[int] | None:
    if not text:
        return None
    wanted = set()
    for part in text.split(","):
        part = part.strip()
        if not part.isdigit() or not 1 <= int(part) <= len(batch.projects):
            raise BatchError(f"--rows: '{part}' is not a row number of this batch (1 to {len(batch.projects)})")
        wanted.add(int(part))
    return wanted


def batch_apply(args, paths: Paths) -> int:
    from . import cli
    batch = Batch.load(paths, args.batch)
    wanted = _rows_filter(args.rows, batch)
    net = _network_argv(args)
    todo = [e for e in batch.projects if (wanted is None or e["n"] in wanted)
            and e["plan"].get("status") == "planned" and e["apply"].get("status") != "applied"]
    skipped_unplanned = [e for e in batch.projects if (wanted is None or e["n"] in wanted) and e["plan"].get("status") != "planned"]
    if not todo:
        print("Nothing to apply: every planned project is already applied.")
        print(render_text(batch))
        return cli.EXIT_FAILED if skipped_unplanned else cli.EXIT_OK
    folders = sum(_plan_counts(e)[0] for e in todo)
    cases = sum(_plan_counts(e)[1] for e in todo)
    print(f"Batch {batch.id}: this will WRITE to {batch.data['target']} for {len(todo)} project(s): "
          f"{folders} folder(s) and {cases} test case(s) to create, plus whatever the plan options add "
          f"({' '.join(batch.data['plan_options']) or 'no extra options'}).")
    for e in todo:
        f, c = _plan_counts(e)
        again = " (continues an earlier attempt)" if e["apply"].get("status") else ""
        print(f"  [{e['n']}] {_label(e)}: {f} folder(s), {c} test case(s){again}")
    if skipped_unplanned:
        print("  not applied (no plan): " + ", ".join(f"[{e['n']}]" for e in skipped_unplanned))
    if not args.yes:
        if not sys.stdin.isatty():
            print("Refusing to write without confirmation: pass --yes when running non-interactively.")
            return cli.EXIT_FAILED
        if input(f"Type the number of projects ({len(todo)}) to continue: ").strip() != str(len(todo)):
            print("Aborted, nothing written.")
            return cli.EXIT_FAILED
    problems = 0
    for e in todo:
        print(f"[{e['n']}/{len(batch.projects)}] apply {_label(e)}")
        if batch.data.get("fields") and e["fields_plan"].get("run_id") and e["fields_apply"].get("status") != "applied":
            if not _run_step(batch, e, "fields_apply", ["fields", "apply", "--plan", e["fields_plan"]["run_id"], "--yes", *net],
                             cli.cmd_fields, paths, "applied", "applied-with-errors", _counts_of("fields_result.json")):
                problems += 1
                continue  # the test cases need their fields: do not upload this project half-prepared
        if not _run_step(batch, e, "apply", ["apply", "--plan", e["plan"]["run_id"], "--yes", *net], cli.cmd_apply, paths,
                         "applied", "applied-with-errors", _counts_of("result.json")):
            problems += 1
    print()
    print(render_text(batch))
    nxt = "python -m qmig batch " + ("apply" if problems else "verify") + f" --batch {batch.id}"
    print(f"\n{'Some projects had problems: see the status above, fix the cause and run' if problems else 'Next:'}  {nxt}")
    return cli.EXIT_FAILED if problems else cli.EXIT_OK


def batch_verify(args, paths: Paths) -> int:
    from . import cli
    batch = Batch.load(paths, args.batch)
    wanted = _rows_filter(args.rows, batch)
    net = _network_argv(args)
    failed = 0
    todo = [e for e in batch.projects if (wanted is None or e["n"] in wanted) and e["apply"].get("run_id")
            and e["apply"].get("status") in ("applied", "applied-with-errors")]
    for e in todo:
        print(f"[{e['n']}/{len(batch.projects)}] verify {_label(e)}")
        if not _run_step(batch, e, "verify", ["verify", "--run", e["apply"]["run_id"], *net], cli.cmd_verify, paths,
                         "passed", "failed"):
            failed += 1
    print()
    print(render_text(batch))
    if failed:
        print(cli.VERIFY_MISSING_HINT.replace("Missing test cases", "If test cases are missing, they"))
    return cli.EXIT_FAILED if failed else cli.EXIT_OK


def _links_options(entry_args) -> list[str]:
    out = ["--name-on-key-miss"] if getattr(entry_args, "name_on_key_miss", False) else []
    if getattr(entry_args, "key_map", None):
        out += ["--key-map", entry_args.key_map]
    return out


def batch_links_plan(args, paths: Paths) -> int:
    """Capture the requirement links of every migrated project in HS and match them to the requirements now in HCSC."""
    from . import cli
    batch = Batch.load(paths, args.batch)
    wanted = _rows_filter(args.rows, batch)
    net = _network_argv(args)
    todo = [e for e in batch.projects if (wanted is None or e["n"] in wanted)
            and e["apply"].get("status") in ("applied", "applied-with-errors")]
    if not todo:
        print("Nothing to do: no project of this batch has its test cases migrated yet (run 'batch apply' first).")
        return cli.EXIT_FAILED
    failed = 0
    for e in todo:
        print(f"[{e['n']}/{len(batch.projects)}] links plan {_label(e)}")
        scope = _scope(batch, e)
        if not _run_step(batch, e, "links_capture", ["links", "capture", *scope, *net], cli.cmd_links, paths, "captured", "failed"):
            failed += 1
            continue
        if not _run_step(batch, e, "links_plan", ["links", "plan", *scope, *_links_options(args), *net], cli.cmd_links, paths,
                         "planned", "failed", _counts_of("links_plan.json"),
                         soft={"No captured links": "nothing-to-link"}):
            failed += 1
    print()
    print(render_text(batch))
    return cli.EXIT_FAILED if failed else cli.EXIT_OK


def _link_counts(entry: dict) -> dict:
    return (entry["links_plan"].get("counts") or {}).get("actions", {})


def batch_links_apply(args, paths: Paths) -> int:
    from . import cli
    batch = Batch.load(paths, args.batch)
    wanted = _rows_filter(args.rows, batch)
    net = _network_argv(args)
    todo = [e for e in batch.projects if (wanted is None or e["n"] in wanted) and e["links_plan"].get("status") == "planned"
            and e["links_apply"].get("status") != "applied" and _link_counts(e).get("link", 0) > 0]
    if not todo:
        print("Nothing to link: no project has links planned (run 'batch links plan'), or all are applied.")
        print(render_text(batch))
        return cli.EXIT_OK
    total = sum(_link_counts(e).get("link", 0) for e in todo)
    print(f"Batch {batch.id}: this will CREATE {total} test case -> requirement link(s) in {batch.data['target']} "
          f"for {len(todo)} project(s):")
    for e in todo:
        c = _link_counts(e)
        left = ", ".join(f"{c[k]} {k}" for k in ("not-found", "ambiguous") if c.get(k))
        print(f"  [{e['n']}] {_label(e)}: {c.get('link', 0)} link(s)" + (f"; not linked: {left}" if left else ""))
    if not args.yes:
        if not sys.stdin.isatty():
            print("Refusing to write without confirmation: pass --yes when running non-interactively.")
            return cli.EXIT_FAILED
        if input(f"Type the number of projects ({len(todo)}) to continue: ").strip() != str(len(todo)):
            print("Aborted, nothing written.")
            return cli.EXIT_FAILED
    problems = 0
    for e in todo:
        print(f"[{e['n']}/{len(batch.projects)}] links apply {_label(e)}")
        if not _run_step(batch, e, "links_apply", ["links", "apply", "--plan", e["links_plan"]["run_id"], "--yes", *net], cli.cmd_links,
                         paths, "applied", "applied-with-errors", _counts_of("links_result.json")):
            problems += 1
    print()
    print(render_text(batch))
    return cli.EXIT_FAILED if problems else cli.EXIT_OK


def batch_links_verify(args, paths: Paths) -> int:
    from . import cli
    batch = Batch.load(paths, args.batch)
    wanted = _rows_filter(args.rows, batch)
    net = _network_argv(args)
    failed = 0
    for e in [e for e in batch.projects if (wanted is None or e["n"] in wanted) and e["links_apply"].get("run_id")
              and e["links_apply"].get("status") in ("applied", "applied-with-errors")]:
        print(f"[{e['n']}/{len(batch.projects)}] links verify {_label(e)}")
        if not _run_step(batch, e, "links_verify", ["links", "verify", "--run", e["links_apply"]["run_id"], *net], cli.cmd_links,
                         paths, "passed", "failed"):
            failed += 1
    print()
    print(render_text(batch))
    return cli.EXIT_FAILED if failed else cli.EXIT_OK


def batch_status(args, paths: Paths) -> int:
    batch = Batch.load(paths, args.batch)
    print(render_text(batch))
    return 0


# ------------------------------------------------------------------------------ rendering
def _cell(step: dict) -> str:
    return step.get("status") or "-"


def _next_hint(batch: Batch) -> str:
    states = [e["apply"].get("status") for e in batch.projects]
    stopped = next(((k, e) for e in batch.projects for k in STEPS if e[k].get("status") == "interrupted"), None)
    if stopped:
        command = {"links_capture": "links plan", "links_plan": "links plan", "links_apply": "links apply",
                   "links_verify": "links verify", "verify": "verify", "plan": "plan"}.get(stopped[0], "apply")
        return f"Interrupted by the network. Run again to continue: python -m qmig batch {command} --batch {batch.id}"
    if any(e["plan"].get("status") == "failed" for e in batch.projects):
        return "Some projects could not be planned (see the error column); fix the cause and make a new batch for them."
    if any(s in ("failed", "applied-with-errors") for s in states):
        return f"Some projects have errors. Fix the cause, then run again: python -m qmig batch apply --batch {batch.id}"
    if all(e["plan"].get("status") == "planned" for e in batch.projects) and not any(states):
        return f"Planned. Review, then: python -m qmig batch apply --batch {batch.id}"
    if any(e["links_plan"].get("status") == "planned" and e["links_apply"].get("status") != "applied" and _link_counts(e).get("link", 0)
           for e in batch.projects):
        return f"Links planned. Review, then: python -m qmig batch links apply --batch {batch.id}"
    if all(s == "applied" for s in states) and not all(e["verify"].get("status") == "passed" for e in batch.projects):
        return f"Applied. Next: python -m qmig batch verify --batch {batch.id}"
    return ""


def _rows(batch: Batch) -> list[list[str]]:
    out = []
    for e in batch.projects:
        steps = [e[k] for k in STEPS]
        errors = next((s["error"] for s in steps if s.get("error")), "")
        seconds = sum(s.get("seconds", 0) for s in steps)
        links = e["links_verify"].get("status") or e["links_apply"].get("status") or e["links_plan"].get("status") or "-"
        out.append([str(e["n"]), _label(e), _cell(e["plan"]), _cell(e["apply"]), _cell(e["verify"]), links,
                    format_seconds(seconds) if seconds else "-", errors[:160]])
    return out


def render_text(batch: Batch) -> str:
    header = ["#", "Projects", "Plan", "Apply", "Verify", "Links", "Time", "Error"]
    rows = [header] + _rows(batch)
    widths = [max(len(r[i]) for r in rows) for i in range(len(header) - 1)]
    lines = ["  ".join(c.ljust(w) for c, w in zip(r, widths)) + ("  " + r[-1] if r[-1] else "") for r in rows]
    total = sum(sum(e[k].get("seconds", 0) for k in STEPS) for e in batch.projects)
    done = sum(e["apply"].get("status") == "applied" for e in batch.projects)
    lines += ["", f"Batch {batch.id}: {done} of {len(batch.projects)} project(s) applied, total time {format_seconds(total)}"]
    hint = _next_hint(batch)
    return "\n".join(lines + ([hint] if hint else []))


def render_markdown(batch: Batch) -> str:
    lines = [f"# Batch {batch.id}", "", f"- CSV: `{batch.data['csv']}`", f"- {batch.data['source']} -> {batch.data['target']}",
             f"- Plan options: `{' '.join(batch.data['plan_options']) or 'none'}`", "",
             "| # | Projects | Plan | Apply | Verify | Links | Time | Error |", "|---|---|---|---|---|---|---|---|"]
    lines += ["| " + " | ".join(c.replace("|", "/") for c in r) + " |" for r in _rows(batch)]
    hint = _next_hint(batch)
    return "\n".join(lines + ([f"", f"**Next:** {hint}"] if hint else [])) + "\n"
