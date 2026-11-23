"""Command line interface: ``python -m qmig <command>`` (or ``qmig <command>`` once installed)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__, doctor
from .apply import ApplyError, apply_plan, load_plan
from .batch import BatchError
from .client import ApiError, QtestClient, WriteBlocked
from .config import ConfigError, Paths, get_paths, load_dotenv, instance_names, load_instance
from .db import StateDb
from .fields import DEFAULT_SKIP_FIELDS
from .fieldsetup import (FIELDS_PLAN_SKIP, FieldSetupError, apply_fields, load_fields_plan, load_fields_result, plan_fields, verify_fields,
                         write_fields_apply_report, write_fields_plan_report, write_fields_verify_report)
from .linkreport import (write_capture_report, write_links_apply_report, write_links_plan_report,
                         write_links_verify_report)
from .mapping import preflight_mappings
from .network import NetworkInterrupted, format_seconds, policy_from
from .plan import PreflightError, build_plan, preflight
from .reqlinks import (LINK_BATCH, LinksError, apply_links, capture_links, load_links_plan, load_links_result, parse_key_map, plan_links,
                       verify_links)
from .report import write_apply_report, write_plan_report, write_preflight_report, write_verify_report
from .runindex import refresh_index
from .runlog import RunContext
from .verify import load_result, verify_result

KEY_MAP_HELP = ("PILOT ONLY: treat source Jira keys as other keys in the target, e.g. ABC=SCRUM,BCA=SCRUM (ABC-111 is "
                "looked up as SCRUM-111) or ABC-5=SCRUM-9 for one key. Not used by default.")
VERIFY_MISSING_HINT = ("Missing test cases were probably deleted in the target. Run 'plan' and 'apply' again: they detect "
                       "migrated items that no longer exist and create them again.")
EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_INTERRUPTED = 0, 1, 2, 75  # 75 = temporary failure: run the same command again


LAST_RUN: dict = {}


def _skip_fields(text: str | None) -> tuple[str, ...]:
    if text is None:
        return DEFAULT_SKIP_FIELDS
    return tuple(s.strip() for s in text.split(",") if s.strip())


def _clients(args, paths: Paths, *, write_target: bool) -> tuple[QtestClient, QtestClient]:
    if args.source == args.target:
        raise ConfigError("--source and --target must be different instances")
    try:
        network = policy_from(getattr(args, "network_retries", None), getattr(args, "network_retry_interval", None),
                              getattr(args, "network_retry_max_interval", None))
    except ValueError as exc:
        raise ConfigError(str(exc)) from None
    src = QtestClient(load_instance(args.source), allow_writes=False, network=network)  # the source is NEVER written
    tgt = QtestClient(load_instance(args.target), allow_writes=write_target, network=network)
    return src, tgt


def _common(p: argparse.ArgumentParser, *, scope: bool = True) -> None:
    p.add_argument("--source", default="hs", help="source instance name (default: hs)")
    p.add_argument("--target", default="hcsc", help="target instance name (default: hcsc)")
    if scope:
        p.add_argument("--source-project", type=int, required=True, help="source qTest project ID")
        p.add_argument("--target-project", type=int, required=True, help="target qTest project ID")
        p.add_argument("--no-properties", action="store_true",
                       help="do not copy custom field values (use when the token cannot read the target field settings)")
        p.add_argument("--skip-fields", help=f"comma separated field labels to ignore (default: {','.join(DEFAULT_SKIP_FIELDS)}; "
                       "pass an empty value to map everything)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="qmig", description="qTest instance-to-instance migration "
                                     "(folders and test cases). Works the same on Windows and macOS.")
    parser.add_argument("--version", action="version", version=f"qmig {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("setup", help="create local folders, database and a credentials template, then run doctor")

    p = sub.add_parser("doctor", help="check Python, local files, credentials and API access")
    p.add_argument("--source", default="hs")
    p.add_argument("--target", default="hcsc")
    p.add_argument("--offline", action="store_true", help="skip network checks")

    p = sub.add_parser("preflight", help="read-only: check access and compare test case fields of both projects")
    _common(p)

    p = sub.add_parser("plan", help="DRY RUN: read both instances and report what apply would create (writes nothing)")
    _common(p)
    p.add_argument("--module-id", type=int, help="source folder to migrate with its sub-folders (default: whole project)")
    p.add_argument("--without-parents", action="store_true",
                   help="with --module-id: put the folder at the top of the target instead of recreating its parent folders")
    p.add_argument("--target-parent-id", type=int, help="existing target folder to create the tree under (default: project root)")
    p.add_argument("--limit", type=int, help="plan at most N test cases to create (use a small number for a first trial)")
    p.add_argument("--attachments", action="store_true",
                   help="also plan the attachments of the test cases (one extra read request per test case)")
    p.add_argument("--test-case-id", type=int, action="append",
                   help="migrate only this source test case (repeat for several); its folders are still created")
    p.add_argument("--step-files", action="store_true",
                   help="also copy the files attached to test steps (one extra read request per test case)")
    p.add_argument("--label", help="short name added to the run id, e.g. 'pilot' (apply and verify of this plan inherit it)")
    p.add_argument("--no-metadata", action="store_true",
                   help="do not add the 'Migrated metadata' block (previous Assigned To) to the test case description")
    p.add_argument("--approve", nargs="?", const="source", choices=("source", "all"),
                   help="also approve the migrated test cases (needed before they can be linked to requirements): "
                        "'source' (default when given) approves those that are approved in the source, 'all' approves every one")
    p.add_argument("--releases", action="store_true",
                   help="also plan the releases (and their builds); existing releases are matched by name")
    p.add_argument("--release-id", type=int, action="append",
                   help="with --releases/--releases-only: plan only this source release (repeatable)")
    p.add_argument("--releases-only", action="store_true",
                   help="plan only the releases and builds, no folders or test cases")
    p.add_argument("--cycles", action="store_true",
                   help="also plan the test cycles and suites under the releases (implies --releases)")
    p.add_argument("--cycles-only", action="store_true",
                   help="plan only releases, builds, cycles and suites, no folders or test cases")
    p.add_argument("--cycle-id", type=int, action="append",
                   help="with --cycles/--runs: plan only this source cycle with everything below it (repeatable); "
                        "the cycles above it are created empty so it keeps its place")
    p.add_argument("--runs", action="store_true",
                   help="also plan the test runs in the cycles and suites (implies --cycles); needs the test cases migrated first")
    p.add_argument("--logs", nargs="?", const="all", choices=("all", "last"),
                   help="also plan the test logs (execution history) of the migrated runs, with their files; implies --runs. "
                        "'all' (default when given) copies every log oldest first, 'last' only the newest of each run")
    p.add_argument("--no-step-results", action="store_true",
                   help="with --logs: do not copy the per-step results (Passed/Failed of each step) of the logs")
    p.add_argument("--runs-only", action="store_true",
                   help="plan only releases, cycles, suites and runs, no folders or test cases (test cases must already be migrated)")

    p = sub.add_parser("apply", help="create the folders and test cases of a reviewed plan in the target")
    p.add_argument("--plan", required=True, help="run id of the plan, e.g. 20260101-120000-plan")
    p.add_argument("--target", help="override target instance name (must match the plan)")
    p.add_argument("--yes", action="store_true", help="do not ask for confirmation")

    p = sub.add_parser("verify", help="read-only: check that the test cases created by an apply run exist in the target")
    p.add_argument("--run", required=True, help="run id of the apply run")
    p.add_argument("--target")

    p = sub.add_parser("fields", help="create the test case custom fields that the target project is missing "
                       "(run before the test case upload)")
    fsub = p.add_subparsers(dest="fields_command", required=True)
    fp = fsub.add_parser("plan", help="DRY RUN: compare the test case fields of both projects (writes nothing)")
    _common(fp)
    fp = fsub.add_parser("apply", help="create the missing fields of a reviewed fields plan in the target project")
    fp.add_argument("--plan", required=True, help="run id of the fields plan")
    fp.add_argument("--target", help="override target instance name (must match the plan)")
    fp.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    fp = fsub.add_parser("verify", help="read-only: check that the fields of an apply run exist in the target")
    fp.add_argument("--run", required=True, help="run id of the fields apply run")
    fp.add_argument("--target")

    p = sub.add_parser("links", help="re-link migrated test cases to the requirements created in the target "
                       "(matched by Jira issue key, then by name)")
    lsub = p.add_subparsers(dest="links_command", required=True)
    lp = lsub.add_parser("capture", help="read-only on the source: store which requirements each migrated test case covers")
    _common(lp, scope=False)
    lp.add_argument("--source-project", type=int, required=True)
    lp.add_argument("--target-project", type=int, required=True)
    lp = lsub.add_parser("plan", help="DRY RUN: match the captured links to the target requirements (writes nothing)")
    _common(lp, scope=False)
    lp.add_argument("--source-project", type=int, required=True)
    lp.add_argument("--target-project", type=int, required=True)
    lp.add_argument("--name-on-key-miss", action="store_true",
                    help="also match by name when a requirement HAS a Jira key that the target does not (default: only "
                    "requirements without a key are matched by name)")
    lp.add_argument("--key-map", help=KEY_MAP_HELP)
    lp = lsub.add_parser("apply", help="create the links of a reviewed links plan in the target")
    lp.add_argument("--plan", required=True, help="run id of the links plan")
    lp.add_argument("--target", help="override target instance name (must match the plan)")
    lp.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    lp.add_argument("--batch-size", type=int, default=LINK_BATCH, help=f"test cases per link request (max {LINK_BATCH})")
    lp = lsub.add_parser("verify", help="read-only: check that the links of an apply run exist in the target")
    lp.add_argument("--run", required=True, help="run id of the links apply run")
    lp.add_argument("--target")

    p = sub.add_parser("batch", help="many projects in one go from a CSV file (plan, apply, verify, status)")
    bsub = p.add_subparsers(dest="batch_command", required=True)
    bp = bsub.add_parser("plan", help="DRY RUN: plan every project of the CSV (writes nothing)",
                         epilog="Put the options of 'qmig plan' after '--', e.g.:  qmig batch plan --csv projects.csv -- "
                                "--attachments --step-files --approve all")
    bp.add_argument("--csv", required=True, help="CSV with the columns source_project, target_project and optional module_id")
    bp.add_argument("--source", default="hs", help="source instance name (default: hs)")
    bp.add_argument("--target", default="hcsc", help="target instance name (default: hcsc)")
    bp.add_argument("--fields", action="store_true",
                    help="also plan and (at apply) create the test case fields each target project is missing")
    bp.add_argument("plan_options", nargs=argparse.REMAINDER, help="options of 'qmig plan' applied to every project, after '--'")
    bp = bsub.add_parser("apply", help="apply the plans of a batch: one review, one confirmation; continues an earlier attempt")
    bp.add_argument("--batch", help="batch id (default: the latest)")
    bp.add_argument("--rows", help="only these rows of the CSV, e.g. 1,3 (default: all)")
    bp.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    bp = bsub.add_parser("verify", help="read-only: verify every applied project")
    bp.add_argument("--batch", help="batch id (default: the latest)")
    bp.add_argument("--rows", help="only these rows of the CSV, e.g. 1,3 (default: all)")
    bp = bsub.add_parser("links", help="re-link the migrated test cases to the requirements now in the target (after the test cases "
                         "are migrated and the requirements exist in the target)")
    blsub = bp.add_subparsers(dest="batch_links_command", required=True)
    for name, text in (("plan", "capture the links in the source and match them to the target requirements (writes nothing)"),
                       ("apply", "create the planned links of every project, one confirmation"),
                       ("verify", "read-only: check that the created links exist")):
        lp = blsub.add_parser(name, help=text)
        lp.add_argument("--batch", help="batch id (default: the latest)")
        lp.add_argument("--rows", help="only these rows of the CSV, e.g. 1,3 (default: all)")
        if name == "plan":
            lp.add_argument("--name-on-key-miss", action="store_true",
                            help="also match by name when a requirement HAS a Jira key that the target does not")
            lp.add_argument("--key-map", help=KEY_MAP_HELP)
        if name == "apply":
            lp.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    bp = bsub.add_parser("status", help="show the state of a batch")
    bp.add_argument("--batch", help="batch id (default: the latest)")

    p = sub.add_parser("report", help="status reports for stakeholders")
    rsub = p.add_subparsers(dest="report_command", required=True)
    rp = rsub.add_parser("html", help="one-page HTML status report of a batch (numbers, tables, two charts, timings); "
                         "one self-contained file that also prints to PDF")
    rp.add_argument("--batch", action="append", help="batch id (default: the latest); repeat to combine several batches in one report")
    rp.add_argument("--out", help="where to write the file (default: report.html in the batch folder)")
    rp.add_argument("--deadline", help="target finish date YYYY-MM-DD: draws the target line in the progress chart")

    p = sub.add_parser("runs", help="list previous runs")
    p.add_argument("--limit", type=int, default=20)
    _add_network_options(sub)
    return parser


def _add_network_options(sub) -> None:
    """Every command that talks to an instance can wait for a lost network (see qmig/network.py)."""
    for name, child in sub.choices.items():
        nested = [a for a in child._actions if isinstance(a, argparse._SubParsersAction)]
        if nested:
            for inner in nested:
                _add_network_options(inner)
        elif name not in ("setup", "doctor", "runs", "html"):
            group = child.add_argument_group("network problems")
            group.add_argument("--network-retries", type=int, metavar="N",
                               help="how many times to check whether the network is back before stopping (default 10; "
                                    "0 = stop at once). Env QMIG_NETWORK_RETRIES")
            group.add_argument("--network-retry-interval", metavar="TIME",
                               help="pause before the first check, e.g. 30s (default 30s); it doubles each time. "
                                    "Env QMIG_NETWORK_RETRY_INTERVAL")
            group.add_argument("--network-retry-max-interval", metavar="TIME",
                               help="longest pause between two checks, e.g. 10m (default 10m). "
                                    "Env QMIG_NETWORK_RETRY_MAX_INTERVAL")


# --------------------------------------------------------------------- commands
def cmd_setup(args, paths: Paths) -> int:
    for line in doctor.setup(paths):
        print(line)
    print()
    return cmd_doctor(argparse.Namespace(source="hs", target="hcsc", offline=True), paths)


def cmd_doctor(args, paths: Paths) -> int:
    results = doctor.run_checks(paths, [args.source, args.target], online=not args.offline)
    for status, name, detail in results:
        print(f"[{status:<4}] {name:<22} {detail}")
    failed = [r for r in results if r[0] == doctor.FAIL]
    print("\nAll checks passed." if not failed else f"\n{len(failed)} check(s) failed.")
    return EXIT_FAILED if failed else EXIT_OK


def _run(mode: str, paths: Paths, clients: tuple[QtestClient, ...], pair: tuple[str, int, str, int],
         label: str | None = None) -> RunContext:
    ctx = RunContext(paths, mode, secrets=[c.instance.token for c in clients], pair=pair, label=label)
    LAST_RUN["ctx"] = ctx  # so that a network stop can mark this run as interrupted
    return ctx


def cmd_preflight(args, paths: Paths) -> int:
    src, tgt = _clients(args, paths, write_target=False)
    ctx = _run("preflight", paths, (src, tgt), (src.name, args.source_project, tgt.name, args.target_project))
    try:
        result = preflight(ctx, src, tgt, args.source_project, args.target_project, _skip_fields(args.skip_fields),
                           not args.no_properties)
        extra = preflight_mappings(ctx, src, tgt, args.source_project, args.target_project)
        data = {"source": {"instance": src.name, "url": src.instance.web_url, "project_id": args.source_project,
                           "project": result["source_project"]["name"]},
                "target": {"instance": tgt.name, "url": tgt.instance.web_url, "project_id": args.target_project,
                           "project": result["target_project"]["name"]},
                "scope": {}, "issues": result["issues"] + extra["issues"],
                "mappings": {k: v for k, v in extra.items() if k != "issues"}, "api": {src.name: src.stats, tgt.name: tgt.stats}}
        ctx.write_json("preflight.json", data)
        report = write_preflight_report(ctx, data)
        warnings = sum(i["severity"] == "WARN" for i in result["issues"])
        ctx.log.info("Preflight finished: %d warning(s). Report: %s", warnings, report)
        return EXIT_OK
    finally:
        ctx.close()


def cmd_plan(args, paths: Paths) -> int:
    src, tgt = _clients(args, paths, write_target=False)
    ctx = _run("plan", paths, (src, tgt), (src.name, args.source_project, tgt.name, args.target_project), args.label)
    db = StateDb(paths.db)
    db.start_run(ctx.run_id, "plan", src.name, args.source_project, tgt.name, args.target_project)
    status = "failed"
    try:
        plan = build_plan(ctx, src, tgt, db, sp=args.source_project, tp=args.target_project,
                          module_id=args.module_id, target_parent_id=args.target_parent_id,
                          with_parents=not args.without_parents,
                          limit=args.limit, skip_fields=_skip_fields(args.skip_fields),
                          attachments=args.attachments, properties=not args.no_properties,
                          releases=args.releases, only_releases=args.releases_only or args.cycles_only or args.runs_only,
                          cycles=args.cycles or args.cycles_only or args.runs_only or args.runs or bool(args.logs), runs=args.runs or args.runs_only or bool(args.logs), cycle_ids=tuple(args.cycle_id or ()), logs=args.logs, step_results=not args.no_step_results, step_files=args.step_files, test_case_ids=tuple(args.test_case_id or ()),
                          release_ids=tuple(args.release_id or ()), approve=args.approve, metadata=not args.no_metadata)
        report = write_plan_report(ctx, plan)
        status = "completed"
        c = plan["counts"]
        ctx.log.info("DRY RUN complete - nothing was written. Folders %s | Test cases %s%s", c["folders"], c["testcases"],
                     (f" | Releases {c['releases']} | Builds {c['builds']}" if "releases" in c else "")
                     + (f" | Cycles {c['cycles']} | Suites {c['suites']}" if "cycles" in c else "")
                     + (f" | Runs {c['runs']}" if "runs" in c else "")
                     + (f" | Logs {c['logs']}" if "logs" in c else ""))
        ctx.log.info("Report: %s", report)
        ctx.log.info("Next: python -m qmig apply --plan %s", ctx.run_id)
        return EXIT_OK
    finally:
        db.finish_run(ctx.run_id, status)
        db.close()
        ctx.close()


def _confirm(args, plan: dict) -> bool:
    if args.yes:
        return True
    if not sys.stdin.isatty():
        print("Refusing to write without confirmation: pass --yes when running non-interactively.")
        return False
    t = plan["target"]
    creates = plan["counts"]["testcases"].get("create", 0)
    print(f"This will CREATE {plan['counts']['folders'].get('create', 0)} folder(s) and {creates} test case(s) in "
          f"{t['instance']} project {t['project_id']} ({t.get('project')}).")
    if "releases" in plan["counts"]:
        print(f"and {plan['counts']['releases'].get('create', 0)} release(s) with "
              f"{plan['counts']['builds'].get('create', 0)} build(s).")
    if "cycles" in plan["counts"]:
        print(f"and {plan['counts']['cycles'].get('create', 0)} test cycle(s) and {plan['counts']['suites'].get('create', 0)} suite(s).")
    if "runs" in plan["counts"]:
        print(f"and {plan['counts']['runs'].get('create', 0)} test run(s) with {plan['counts']['run_attachments']['to-copy']} file(s).")
    if "logs" in plan["counts"]:
        print(f"and {plan['counts']['logs'].get('create', 0)} test log(s) with {plan['counts']['log_attachments']['to-copy']} file(s).")
    if "approvals" in plan["counts"]:
        print(f"and approve {plan['counts']['approvals']['to-approve']} test case(s).")
    atts = plan["counts"].get("attachments", {}).get("to-copy")
    if atts is not None:
        print(f"and copy {atts} attachment(s).")
    step_files = plan["counts"].get("step_files", {}).get("to-copy")
    if step_files is not None:
        print(f"and copy {step_files} file(s) attached to test steps.")
    return input(f"Type the target project id ({t['project_id']}) to continue: ").strip() == str(t["project_id"])


def cmd_apply(args, paths: Paths) -> int:
    plan, plan_dir = load_plan(paths.runs, args.plan)
    if args.target and args.target != plan["target"]["instance"]:
        raise ConfigError(f"--target {args.target} does not match the plan target {plan['target']['instance']}")
    if not _confirm(args, plan):
        print("Aborted, nothing written.")
        return EXIT_FAILED
    args.source, args.target = plan["source"]["instance"], plan["target"]["instance"]
    src, tgt = _clients(args, paths, write_target=True)
    ctx = _run("apply", paths, (src, tgt), (src.name, plan["source"]["project_id"], tgt.name, plan["target"]["project_id"]),
               plan["scope"].get("label"))
    db = StateDb(paths.db)
    db.start_run(ctx.run_id, "apply", src.name, plan["source"]["project_id"], tgt.name,
                 plan["target"]["project_id"], plan_run_id=plan["run_id"])
    status = "failed"
    try:
        result = apply_plan(ctx, tgt, db, plan, plan_dir, src)
        report = write_apply_report(ctx, result)
        c = result["counts"]
        failed = (c["testcases"].get("failed", 0) + c["folders"].get("failed", 0) + c["folders"].get("skipped", 0)
                  + c.get("attachments", {}).get("failed", 0) + c.get("step_files", {}).get("failed", 0) + c.get("releases", {}).get("failed", 0)
                  + c.get("builds", {}).get("failed", 0) + c.get("cycles", {}).get("failed", 0) + c.get("suites", {}).get("failed", 0) + c.get("runs", {}).get("failed", 0) + c.get("logs", {}).get("failed", 0) + c.get("approvals", {}).get("failed", 0)
                  + sum(1 for r in result["runs"] + result["logs"] for a in r.get("attachment_results") or [] if a["status"] in ("failed", "too-large")))
        status = "completed" if not failed and not result["aborted"] else "completed-with-errors"
        ctx.log.info("APPLY finished: folders %s | test cases %s%s", c["folders"], c["testcases"],
                     (f" | attachments {c['attachments']}" if "attachments" in c else "")
                     + (f" | step files {c['step_files']}" if "step_files" in c else "")
                     + (f" | approvals {c['approvals']}" if "approvals" in c else "")
                     + (f" | releases {c['releases']} | builds {c['builds']}" if "releases" in c else "")
                     + (f" | cycles {c['cycles']} | suites {c['suites']}" if "cycles" in c else "")
                     + (f" | runs {c['runs']}" if "runs" in c else "")
                     + (f" | logs {c['logs']}" if "logs" in c else ""))
        ctx.log.info("Report: %s", report)
        ctx.log.info("Next: python -m qmig verify --run %s", ctx.run_id)
        return EXIT_FAILED if failed or result["aborted"] else EXIT_OK
    except NetworkInterrupted as exc:
        status = "interrupted"
        ctx.log.error("APPLY INTERRUPTED: %s. Created objects are saved; run the same apply command again to continue.", exc)
        ctx.write_json("interrupted.json", {"run_id": ctx.run_id, "plan_run_id": plan["run_id"], "reason": str(exc),
                                           "waited": format_seconds(exc.waited), "api": {tgt.name: tgt.stats, src.name: src.stats}})
        raise
    finally:
        db.finish_run(ctx.run_id, status)
        db.close()
        ctx.close()


def cmd_verify(args, paths: Paths) -> int:
    result = load_result(paths.runs, args.run)
    args.source, args.target = result["source"]["instance"], result["target"]["instance"]
    _, tgt = _clients(args, paths, write_target=False)
    ctx = _run("verify", paths, (tgt,), (result["source"]["instance"], result["source"]["project_id"], tgt.name,
                                         result["target"]["project_id"]), (result.get("scope") or {}).get("label"))
    try:
        report = verify_result(ctx, tgt, result)
        path = write_verify_report(ctx, report)
        ctx.log.info("VERIFY %s: checked %d, ok %d, missing %d, name differs %d, steps differ %d. Report: %s",
                     "PASSED" if report["passed"] else "FAILED", report["checked"], report["ok"],
                     len(report["missing"]), len(report["name_mismatch"]), len(report["step_mismatch"]), path)
        if report["missing"]:
            ctx.log.warning(VERIFY_MISSING_HINT)
        return EXIT_OK if report["passed"] else EXIT_FAILED
    finally:
        ctx.close()


def cmd_runs(args, paths: Paths) -> int:
    db = StateDb(paths.db)
    rows = db.list_runs(args.limit)
    db.close()
    if not rows:
        print("No runs yet.")
    for r in rows:
        print(f"{r['run_id']:<28} {r['mode']:<9} {r['status']:<22} {r['source_instance']}:{r['source_project']} -> "
              f"{r['target_instance']}:{r['target_project']}  {r['plan_run_id'] or ''}")
    return EXIT_OK


def _links_run_summary(counts: dict) -> str:
    return ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "nothing"


def cmd_links_capture(args, paths: Paths) -> int:
    src, tgt = _clients(args, paths, write_target=False)
    ctx = _run("links-capture", paths, (src,), (src.name, args.source_project, tgt.name, args.target_project))
    db = StateDb(paths.db)
    db.start_run(ctx.run_id, "links-capture", src.name, args.source_project, tgt.name, args.target_project)
    status = "failed"
    try:
        data = capture_links(ctx, src, tgt.name, db, args.source_project, args.target_project)
        report = write_capture_report(ctx, {**data, "source": {**data["source"], "url": src.instance.web_url},
                                            "target": {**data["target"], "url": tgt.instance.web_url}})
        status = "completed"
        c = data["counts"]
        ctx.log.info("CAPTURE finished: %d of %d test case(s) cover requirements, %d link(s) to %d requirement(s). Report: %s",
                     c["testcases_with_requirements"], c["testcases_checked"], c["links"], c["requirements"], report)
        return EXIT_OK
    finally:
        db.finish_run(ctx.run_id, status)
        db.close()
        ctx.close()


def cmd_links_plan(args, paths: Paths) -> int:
    src, tgt = _clients(args, paths, write_target=False)
    ctx = _run("links-plan", paths, (src, tgt), (src.name, args.source_project, tgt.name, args.target_project))
    db = StateDb(paths.db)
    db.start_run(ctx.run_id, "links-plan", src.name, args.source_project, tgt.name, args.target_project)
    status = "failed"
    try:
        plan = plan_links(ctx, src, tgt, db, args.source_project, args.target_project,
                          name_on_key_miss=args.name_on_key_miss,
                          key_map=parse_key_map(getattr(args, "key_map", None)))
        report = write_links_plan_report(ctx, plan)
        status = "completed"
        ctx.log.info("DRY RUN complete - nothing was written. Links: %s", _links_run_summary(plan["counts"]["actions"]))
        ctx.log.info("Report: %s", report)
        ctx.log.info("Next: python -m qmig links apply --plan %s", ctx.run_id)
        return EXIT_OK
    finally:
        db.finish_run(ctx.run_id, status)
        db.close()
        ctx.close()


def _confirm_links(args, plan: dict) -> bool:
    if args.yes:
        return True
    if not sys.stdin.isatty():
        print("Refusing to write without confirmation: pass --yes when running non-interactively.")
        return False
    t = plan["target"]
    print(f"This will CREATE {plan['counts']['actions'].get('link', 0)} test case -> requirement link(s) in "
          f"{t['instance']} project {t['project_id']} ({t.get('project')}).")
    return input(f"Type the target project id ({t['project_id']}) to continue: ").strip() == str(t["project_id"])


def cmd_links_apply(args, paths: Paths) -> int:
    plan = load_links_plan(paths.runs, args.plan)
    if args.target and args.target != plan["target"]["instance"]:
        raise ConfigError(f"--target {args.target} does not match the plan target {plan['target']['instance']}")
    if not _confirm_links(args, plan):
        print("Aborted, nothing written.")
        return EXIT_FAILED
    args.source, args.target = plan["source"]["instance"], plan["target"]["instance"]
    _, tgt = _clients(args, paths, write_target=True)
    ctx = _run("links-apply", paths, (tgt,), (args.source, plan["source"]["project_id"], tgt.name, plan["target"]["project_id"]))
    db = StateDb(paths.db)
    db.start_run(ctx.run_id, "links-apply", args.source, plan["source"]["project_id"], tgt.name,
                 plan["target"]["project_id"], plan_run_id=plan["run_id"])
    status = "failed"
    try:
        result = apply_links(ctx, tgt, db, plan, batch_size=args.batch_size)
        report = write_links_apply_report(ctx, result)
        failed = result["counts"]["actions"].get("failed", 0) + result["counts"]["actions"].get("not-attempted", 0)
        status = "completed" if not failed and not result["aborted"] else "completed-with-errors"
        ctx.log.info("APPLY finished: links %s", _links_run_summary(result["counts"]["actions"]))
        ctx.log.info("Report: %s", report)
        ctx.log.info("Next: python -m qmig links verify --run %s", ctx.run_id)
        return EXIT_FAILED if failed or result["aborted"] else EXIT_OK
    finally:
        db.finish_run(ctx.run_id, status)
        db.close()
        ctx.close()


def cmd_links_verify(args, paths: Paths) -> int:
    result = load_links_result(paths.runs, args.run)
    args.source, args.target = result["source"]["instance"], result["target"]["instance"]
    _, tgt = _clients(args, paths, write_target=False)
    ctx = _run("links-verify", paths, (tgt,), (result["source"]["instance"], result["source"]["project_id"], tgt.name,
                                              result["target"]["project_id"]))
    try:
        report = verify_links(ctx, tgt, result)
        path = write_links_verify_report(ctx, report)
        ctx.log.info("VERIFY %s: checked %d, present %d, missing %d. Report: %s",
                     "PASSED" if report["passed"] else "FAILED", report["checked"], report["ok"], len(report["missing"]), path)
        return EXIT_OK if report["passed"] else EXIT_FAILED
    finally:
        ctx.close()


def cmd_fields_plan(args, paths: Paths) -> int:
    src, tgt = _clients(args, paths, write_target=False)
    ctx = _run("fields-plan", paths, (src, tgt), (src.name, args.source_project, tgt.name, args.target_project))
    try:
        plan = plan_fields(ctx, src, tgt, args.source_project, args.target_project,
                           FIELDS_PLAN_SKIP if args.skip_fields is None else _skip_fields(args.skip_fields))
        report = write_fields_plan_report(ctx, plan)
        ctx.log.info("DRY RUN complete - nothing was written. Fields: %s", plan["counts"]["actions"])
        ctx.log.info("Report: %s", report)
        ctx.log.info("Next: python -m qmig fields apply --plan %s", ctx.run_id)
        return EXIT_OK
    finally:
        ctx.close()


def _confirm_fields(args, plan: dict) -> bool:
    if args.yes:
        return True
    if not sys.stdin.isatty():
        print("Refusing to write without confirmation: pass --yes when running non-interactively.")
        return False
    t = plan["target"]
    print(f"This will CREATE {plan['counts']['actions'].get('create', 0)} test case custom field(s) in "
          f"{t['instance']} project {t['project_id']} ({t.get('project')}).")
    return input(f"Type the target project id ({t['project_id']}) to continue: ").strip() == str(t["project_id"])


def cmd_fields_apply(args, paths: Paths) -> int:
    plan = load_fields_plan(paths.runs, args.plan)
    if args.target and args.target != plan["target"]["instance"]:
        raise ConfigError(f"--target {args.target} does not match the plan target {plan['target']['instance']}")
    if not _confirm_fields(args, plan):
        print("Aborted, nothing written.")
        return EXIT_FAILED
    args.source, args.target = plan["source"]["instance"], plan["target"]["instance"]
    _, tgt = _clients(args, paths, write_target=True)
    ctx = _run("fields-apply", paths, (tgt,), (args.source, plan["source"]["project_id"], tgt.name, plan["target"]["project_id"]))
    try:
        result = apply_fields(ctx, tgt, plan)
        report = write_fields_apply_report(ctx, result)
        failed = result["counts"]["actions"].get("failed", 0) + result["counts"]["actions"].get("not-attempted", 0)
        ctx.log.info("APPLY finished: fields %s. Report: %s", result["counts"]["actions"], report)
        ctx.log.info("Next: python -m qmig fields verify --run %s", ctx.run_id)
        return EXIT_FAILED if failed else EXIT_OK
    finally:
        ctx.close()


def cmd_fields_verify(args, paths: Paths) -> int:
    result = load_fields_result(paths.runs, args.run)
    args.source, args.target = result["source"]["instance"], result["target"]["instance"]
    _, tgt = _clients(args, paths, write_target=False)
    ctx = _run("fields-verify", paths, (tgt,), (args.source, result["source"]["project_id"], tgt.name, result["target"]["project_id"]))
    try:
        report = verify_fields(ctx, tgt, result)
        path = write_fields_verify_report(ctx, report)
        ctx.log.info("VERIFY %s: checked %d, correct %d. Report: %s", "PASSED" if report["passed"] else "FAILED",
                     report["checked"], report["ok"], path)
        return EXIT_OK if report["passed"] else EXIT_FAILED
    finally:
        ctx.close()


def cmd_fields(args, paths: Paths) -> int:
    return {"plan": cmd_fields_plan, "apply": cmd_fields_apply, "verify": cmd_fields_verify}[args.fields_command](args, paths)


def cmd_links(args, paths: Paths) -> int:
    return {"capture": cmd_links_capture, "plan": cmd_links_plan, "apply": cmd_links_apply,
            "verify": cmd_links_verify}[args.links_command](args, paths)


def cmd_report(args, paths: Paths) -> int:
    from datetime import datetime
    from .batch import Batch
    from .htmlreport import write_report
    try:
        deadline = datetime.strptime(args.deadline, "%Y-%m-%d").astimezone() if args.deadline else None
    except ValueError:
        raise ConfigError("--deadline must look like 2026-12-31") from None
    batches = [Batch.load(paths, b) for b in (args.batch or [None])]
    batch = batches[0]
    if len(batches) > 1:  # several batches in one report: the projects of all of them, numbered again
        batch.data = {**batch.data, "batch_id": " + ".join(b.id for b in batches),
                      "projects": [{**p, "n": i} for i, p in enumerate((p for b in batches for p in b.projects), 1)]}
    path = write_report(paths, batch, Path(args.out) if args.out else None, deadline)
    print(f"Report written: {path}")
    return EXIT_OK


def cmd_batch(args, paths: Paths) -> int:
    from . import batch
    if args.batch_command == "links":
        return {"plan": batch.batch_links_plan, "apply": batch.batch_links_apply,
                "verify": batch.batch_links_verify}[args.batch_links_command](args, paths)
    return {"plan": batch.batch_plan, "apply": batch.batch_apply, "verify": batch.batch_verify,
            "status": batch.batch_status}[args.batch_command](args, paths)


COMMANDS = {"batch": cmd_batch, "report": cmd_report, "setup": cmd_setup, "doctor": cmd_doctor, "preflight": cmd_preflight, "plan": cmd_plan,
            "apply": cmd_apply, "verify": cmd_verify, "fields": cmd_fields, "links": cmd_links, "runs": cmd_runs}


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # Windows consoles (cp1252) must not crash on non-ASCII test case names
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    args = build_parser().parse_args(argv)
    paths = get_paths()
    try:
        load_dotenv(paths.root / ".env")
        return COMMANDS[args.command](args, paths)
    except (ConfigError, PreflightError, ApplyError, FieldSetupError, LinksError, BatchError, FileNotFoundError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except WriteBlocked as exc:
        print(f"INTERNAL SAFETY STOP: {exc}", file=sys.stderr)
        return EXIT_FAILED
    except ApiError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_FAILED
    except NetworkInterrupted as exc:
        command = f"python -m qmig {' '.join(sys.argv[1:] if argv is None else argv)}"
        print(f"\nINTERRUPTED: {exc}\nEverything done so far is saved. When the network is back, run the same command again "
              f"to continue where it stopped:\n  {command}", file=sys.stderr)
        _mark_interrupted(paths, str(exc), command)
        return EXIT_INTERRUPTED
    except KeyboardInterrupt:
        print("Interrupted. Created objects are recorded; run apply again with the same plan to continue.", file=sys.stderr)
        return 130
    finally:
        if args.command not in ("setup", "doctor", "runs"):
            refresh_index(paths)


def _mark_interrupted(paths: Paths, reason: str, command: str) -> None:
    """The run that was cut off by the network shows as ``interrupted`` (index.csv, status.md) with its resume command."""
    ctx = LAST_RUN.pop("ctx", None)
    if ctx is None:
        return
    try:
        db = StateDb(paths.db)
        try:
            db.conn.execute("UPDATE runs SET status='interrupted' WHERE run_id=?", (ctx.run_id,))
            db.conn.commit()
        finally:
            db.close()
        file = ctx.dir / "interrupted.json"
        data = json.loads(file.read_text(encoding="utf-8")) if file.is_file() else {}
        file.write_text(json.dumps({**data, "run_id": ctx.run_id, "reason": reason, "resume_command": command}, indent=2),
                        encoding="utf-8")
    except Exception:  # noqa: BLE001 - bookkeeping must not hide the real stop
        pass


if __name__ == "__main__":
    sys.exit(main())
