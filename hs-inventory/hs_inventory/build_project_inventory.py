#!/usr/bin/env python3
"""Build a read-only HS project-size and export-object inventory."""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from hs_inventory.export_inventory import Client, InventoryError, load_credentials

LOG = logging.getLogger(__name__)
PAGE_SIZE = 999
ARTIFACT_COLUMNS = (
    "project_id", "project_name", "size", "requirement_count", "module_count",
    "testcase_count", "test_step_count", "test_cycle_count", "test_suite_count",
    "test_run_count", "release_count",
)


def classify_size(entity_count: int) -> str:
    if entity_count == 0:
        return "XS"
    if entity_count <= 5000:
        return "S"
    if entity_count <= 25000:
        return "M"
    return "L"


def total_entity_count(row: dict) -> int:
    return sum(int(row[field]) for field in ARTIFACT_COLUMNS[3:])


def _items(payload: object, path: str) -> tuple[list[dict], int | None]:
    if isinstance(payload, list):
        return payload, None
    if isinstance(payload, dict):
        for key in ("items", "data", "results", "value"):
            if isinstance(payload.get(key), list):
                total = payload.get("total")
                return payload[key], int(total) if total is not None else None
    raise InventoryError(f"GET {path}: unexpected response shape")


def fetch_unpaged(client: Client, path: str, params: dict | None = None) -> list[dict]:
    """Fetch parent-scoped rows, tolerating endpoints that repeat page one."""
    rows: list[dict] = []
    seen_ids: set[str] = set()
    signatures: set[tuple[str, ...]] = set()
    expected_total = None
    for page in range(1, 10001):
        query = {
            **(params or {}), "page": page, "pageSize": PAGE_SIZE, "size": PAGE_SIZE,
        }
        payload, header_total = client.get(path, query)
        items, payload_total = _items(payload, path)
        total = header_total if header_total is not None else payload_total
        if total is not None:
            if expected_total is not None and total != expected_total:
                raise InventoryError(f"GET {path}: total changed during pagination")
            expected_total = total
        ids = [str(row.get("id", "")) for row in items if isinstance(row, dict)]
        if len(ids) != len(items) or any(not item_id for item_id in ids):
            raise InventoryError(f"GET {path}: record missing ID")
        if len(set(ids)) != len(ids):
            raise InventoryError(f"GET {path}: duplicate IDs in parent-scoped response")
        if not items:
            if expected_total is not None and len(rows) != expected_total:
                raise InventoryError(f"GET {path}: count differs from declared total")
            return rows
        signature = tuple(ids)
        if signature in signatures:
            if expected_total is None or len(rows) == expected_total:
                return rows
            raise InventoryError(f"GET {path}: repeated page before declared total was reached")
        signatures.add(signature)
        if seen_ids.intersection(ids):
            raise InventoryError(f"GET {path}: overlapping IDs across pages")
        seen_ids.update(ids)
        rows.extend(items)
        if expected_total is not None:
            if len(rows) > expected_total:
                raise InventoryError(f"GET {path}: received more records than declared total")
            if len(rows) == expected_total:
                return rows
        if expected_total is None and len(items) < PAGE_SIZE:
            return rows
    raise InventoryError(f"GET {path}: exceeded pagination safety limit")


def count_modules(client: Client, project_id: int) -> int:
    path = f"/projects/{project_id}/modules"
    roots = fetch_unpaged(client, path, {"type": "TEST_DESIGN"})
    seen: set[str] = set()

    def add_tree(nodes: list[dict]) -> None:
        for module in nodes:
            module_id = str(module["id"])
            if module_id in seen:
                raise InventoryError(f"Project {project_id}: duplicate module ID {module_id}")
            seen.add(module_id)
            children = module.get("children", [])
            if not isinstance(children, list):
                raise InventoryError(f"Project {project_id}, module {module_id}: invalid descendants")
            add_tree(children)

    for root in roots:
        add_tree([root])
        add_tree(fetch_unpaged(
            client, path, {
                "parentId": root["id"], "type": "TEST_DESIGN",
                "expand": "descendants",
            },
        ))
    return len(seen)


def count_execution_hierarchy(client: Client, project_id: int) -> dict[str, int]:
    prefix = f"/projects/{project_id}"
    cycle_path = f"{prefix}/test-cycles"
    queue = fetch_unpaged(client, cycle_path)
    seen_cycles: set[str] = set()
    seen_suites: set[str] = set()
    seen_runs: set[str] = set()

    while queue:
        cycle = queue.pop(0)
        cycle_id = str(cycle["id"])
        if cycle_id in seen_cycles:
            raise InventoryError(f"Project {project_id}: cycle {cycle_id} appears more than once")
        seen_cycles.add(cycle_id)
        queue.extend(fetch_unpaged(
            client, cycle_path,
            {"parentId": cycle_id, "parentType": "test-cycle"},
        ))
        for run in fetch_unpaged(client, f"{prefix}/test-runs", {"parentId": cycle_id, "parentType": "test-cycle"}):
            run_id = str(run["id"])  # runs directly under a cycle
            if run_id in seen_runs:
                raise InventoryError(f"Project {project_id}: run {run_id} appears more than once")
            seen_runs.add(run_id)
        suites = fetch_unpaged(
            client, f"{prefix}/test-suites",
            {"parentId": cycle_id, "parentType": "test-cycle"},
        )
        for suite in suites:
            suite_id = str(suite["id"])
            if suite_id in seen_suites:
                raise InventoryError(f"Project {project_id}: suite {suite_id} appears more than once")
            seen_suites.add(suite_id)
            runs = fetch_unpaged(
                client, f"{prefix}/test-runs",
                {"parentId": suite_id, "parentType": "test-suite"},
            )
            for run in runs:
                run_id = str(run["id"])
                if run_id in seen_runs:
                    raise InventoryError(f"Project {project_id}: run {run_id} appears more than once")
                seen_runs.add(run_id)

    return {
        "test_cycle_count": len(seen_cycles),
        "test_suite_count": len(seen_suites),
        "test_run_count": len(seen_runs),
    }


def count_project_artifacts(client: Client, project: dict) -> dict:
    project_id = int(project["project_id"])
    prefix = f"/projects/{project_id}"

    cases = client.collect(f"{prefix}/test-cases", {"expandSteps": "true"})
    step_count = 0
    for case in cases:
        steps = case.get("test_steps", case.get("steps"))
        if not isinstance(steps, list):
            raise InventoryError(
                f"Project {project_id}, testcase {case['id']}: expanded steps missing"
            )
        step_count += len(steps)

    execution_counts = count_execution_hierarchy(client, project_id)
    releases = fetch_unpaged(client, f"{prefix}/releases")
    result = {
        "project_id": project_id,
        "project_name": project["project_name"].strip(),
        "requirement_count": int(project["requirement_count"]),
        "module_count": count_modules(client, project_id),
        "testcase_count": len(cases),
        "test_step_count": step_count,
        **execution_counts,
        "release_count": len(releases),
    }
    result["size"] = classify_size(total_entity_count(result))
    return result


def load_projects(source_dir: Path) -> tuple[list[dict], dict]:
    manifest_path = source_dir / "manifest.json"
    projects_path = source_dir / "projects.csv"
    if not manifest_path.is_file() or not projects_path.is_file():
        raise InventoryError(f"Source inventory is missing manifest.json or projects.csv: {source_dir}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("status") != "complete" or manifest.get("scope") != "all_projects_admin":
        raise InventoryError("Source inventory must be complete and cover all HS projects")
    with projects_path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    projects = [
        row for row in rows
        if row.get("sample", "").strip().lower() != "true"
        and "sample" not in row.get("project_name", "").lower()
    ]
    return projects, manifest


def render_markdown(rows: list[dict], source_manifest: dict, counted_at: str) -> str:
    classes = ("XS", "S", "M", "L")
    fields = ARTIFACT_COLUMNS[3:]
    summary = []
    for size in classes:
        selected = [row for row in rows if row["size"] == size]
        totals = [sum(int(row[field]) for row in selected) for field in fields]
        entity_total = sum(totals)
        summary.append(
            f"| {size} | {len(selected)} | {entity_total:,} | "
            + " | ".join(f"{value:,}" for value in totals) + " |"
        )

    rows.sort(key=lambda row: (
        classes.index(row["size"]),
        -total_entity_count(row), row["project_name"].casefold(),
    ))
    detail = [
        "| Size | Project ID | HS project | Total entities | Requirements | Modules | Test cases | Test steps | Test cycles | Test suites | Test runs | Releases |",
        "|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        project_name = row["project_name"].replace("|", "&#124;")
        detail.append(
            "| {} | {} | {} | {} | {} |".format(
                row["size"], row["project_id"], project_name, f"{total_entity_count(row):,}",
                " | ".join(f"{int(row[field]):,}" for field in ARTIFACT_COLUMNS[3:]),
            )
        )

    totals_header = "| Size | Projects | Total entities | Requirements | Modules | Test cases | Test steps | Test cycles | Test suites | Test runs | Releases |"
    totals_rule = "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"
    return "\n".join([
        "# HS Project Export Inventory",
        "",
        f"Inventory counted: {counted_at}",
        f"Source requirement snapshot completed: {source_manifest.get('completed_at', 'unknown')}",
        f"Source: {source_manifest.get('source', 'HS qTest')} (all-project admin scope)",
        "Sample projects are excluded. Size is based on the total count of all entities listed in this inventory: XS = 0, S = 1-5,000, M = 5,001-25,000, and L = over 25,000.",
        "",
        "Total entities is the sum of requirements, modules, test cases, test steps, test cycles, test suites, test runs, and releases. Each counted record contributes one, including test steps; the total is an inventory workload indicator, not a count of unique business objects.",
        "",
        "## Totals by Size",
        "",
        totals_header,
        totals_rule,
        *summary,
        "",
        "## Project Counts",
        "",
        *detail,
        "",
        "## Counting Notes",
        "",
        "- Requirements come from the complete local HS inventory snapshot; the other counts are read from the HS qTest API when this report is built.",
        "- Test steps are counted from expanded test-case records. Modules are counted by traversing the Test Design hierarchy. Suites are queried by cycle parent because the unfiltered suite endpoint repeats rows.",
        "- Test cycles, suites, and runs are counted recursively through the same cycle -> suite -> run hierarchy used by migration downloads. Recycle Bin entries are included if returned by qTest.",
        "- Size bands are operational workload categories based on the combined count of all entity types above; they do not represent project or company headcount.",
        "- Execution logs, step-level execution logs, and attachments are exportable data but are not counted here; counting them requires per-run, per-log, or per-case API requests. Their contents are not included in the totals above.",
        "- This is a read-only inventory, not a transactional snapshot. Source data can change while the count is running.",
        "",
    ])


def _latest_source() -> Path:
    candidates = sorted(
        (path.parent for path in (REPO_ROOT / "output/generated/hs_inventory").glob("*/manifest.json")),
        reverse=True,
    )
    for path in candidates:
        try:
            manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if manifest.get("status") == "complete" and manifest.get("scope") == "all_projects_admin":
            return path
    raise InventoryError("No complete all-project HS inventory snapshot found")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument(
        "--markdown", type=Path,
        default=REPO_ROOT / "docs/HS_PROJECT_SIZE_INVENTORY.md",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    source_dir = args.source_dir or _latest_source()
    out = args.output_dir or (
        REPO_ROOT / "output/generated/hs_inventory" /
        datetime.now(timezone.utc).strftime("object_counts_%Y%m%dT%H%M%S%fZ")
    )
    try:
        projects, source_manifest = load_projects(source_dir)
        base_url, token = load_credentials()
        client = Client(base_url, token, PAGE_SIZE)
        out.mkdir(parents=True, exist_ok=False)
        manifest = {
            "status": "in_progress", "source_inventory": str(source_dir),
            "scope": "all_non_sample_projects", "project_count": len(projects),
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        results = []
        for index, project in enumerate(projects, 1):
            LOG.info("Counting HS project %s (%s/%s)", project["project_name"], index, len(projects))
            results.append(count_project_artifacts(client, project))
        counted_at = datetime.now(timezone.utc).isoformat()
        with (out / "project_artifacts.csv").open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=ARTIFACT_COLUMNS)
            writer.writeheader()
            writer.writerows(results)
        (out / "project_artifacts.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        markdown = render_markdown(results, source_manifest, counted_at)
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(markdown, encoding="utf-8")
        manifest.update({
            "status": "complete", "completed_at": counted_at,
            "project_count": len(results), "markdown": str(args.markdown),
        })
        (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        LOG.info("Complete: %s non-Sample projects counted; report: %s", len(results), args.markdown)
    except (InventoryError, OSError, ValueError, KeyError) as exc:
        LOG.error("Inventory failed: %s. Incomplete results must not be used.", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())