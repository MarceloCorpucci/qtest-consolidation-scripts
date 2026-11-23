#!/usr/bin/env python3
"""Read-only HS project and requirement inventory, with Jira metadata provenance."""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from hs_inventory.qtest_network import configure_qtest_proxy

LOG = logging.getLogger(__name__)
ISSUE_KEY = re.compile(r"(?<![A-Za-z0-9_])([A-Z][A-Z0-9_]*-\d+)(?![A-Za-z0-9_])")
ISSUE_TYPE = re.compile(
    r"^\s*[\[(]?\s*(user[\s_-]+story|story|epic|bug|task|sub[\s_-]*task|"
    r"feature|initiative|improvement|defect|requirement)\b"
    r"(?:\s*[\]):|/-]\s*|\s+(?=[A-Z][A-Z0-9_]*-\d+))",
    re.IGNORECASE,
)


class InventoryError(RuntimeError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise InventoryError("API redirect refused to protect the authorization header")


def load_dotenv(path: Path) -> None:
    """Put ``KEY=VALUE`` lines of a ``.env`` file into the environment; variables that are already set win."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key.strip()):
            raise InventoryError(f"Expected KEY=value lines in {path.name}")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def load_credentials() -> tuple[str, str]:
    """HS URL and token from QMIG_HS_URL / QMIG_HS_TOKEN (environment, then ``.env`` here or in the parent repo)."""
    for env_file in (REPO_ROOT / ".env", REPO_ROOT.parent / ".env"):
        load_dotenv(env_file)
    values = {"hs-url": os.environ.get("QMIG_HS_URL", ""), "hs-token": os.environ.get("QMIG_HS_TOKEN", "")}
    if not values["hs-url"] or not values["hs-token"]:
        raise InventoryError("Set QMIG_HS_URL and QMIG_HS_TOKEN in .env or the environment")
    url = values["hs-url"].rstrip("/")
    parts = urlsplit(url)
    if (
        parts.scheme != "https" or parts.hostname != "healthspring.qtestnet.com"
        or parts.username or parts.password or parts.query or parts.fragment
        or parts.path not in ("", "/api/v3") or parts.port not in (None, 443)
    ):
        raise InventoryError("QMIG_HS_URL must be the HTTPS healthspring.qtestnet.com API v3 URL")
    token = values["hs-token"]
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not token or "\n" in token or "\r" in token:
        raise InventoryError("Invalid QMIG_HS_TOKEN")
    return f"https://{parts.netloc}/api/v3", token


class Client:
    def __init__(self, base_url: str, token: str, page_size: int = 200):
        configure_qtest_proxy()
        self.base_url = base_url
        self.token = token
        self.page_size = page_size
        self.opener = build_opener(NoRedirect())

    def get(self, path: str, params: dict) -> tuple[object, int | None]:
        request = Request(
            f"{self.base_url}{path}?{urlencode(params)}",
            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"},
        )
        for attempt in range(4):
            try:
                with self.opener.open(request, timeout=60) as response:
                    total = response.headers.get("X-Item-Total")
                    return json.load(response), int(total) if total is not None else None
            except HTTPError as exc:
                status = exc.code
                retry_after = exc.headers.get("Retry-After", "")
                exc.close()
                if status not in (429, 500, 502, 503, 504) or attempt == 3:
                    raise InventoryError(f"GET {path}: HTTP {status}") from None
                delay = min(int(retry_after), 120) if retry_after.isdigit() else 2 ** attempt
            except (URLError, TimeoutError) as exc:
                if attempt == 3:
                    raise InventoryError(f"GET {path}: connection or TLS failure ({exc})") from None
                delay = 2 ** attempt
            LOG.warning("Retrying GET %s in %ss (attempt %s/4)", path, delay, attempt + 2)
            time.sleep(delay)
        raise InventoryError(f"GET {path}: retries exhausted")

    def collect(self, path: str, params: dict | None = None) -> list[dict]:
        rows: list[dict] = []
        seen = set()
        expected_total = None
        for page in range(1, 100001):
            payload, header_total = self.get(
                path, {**(params or {}), "page": page,
                       "size": self.page_size, "pageSize": self.page_size},
            )
            total = header_total
            if isinstance(payload, list):
                items = payload
            elif isinstance(payload, dict) and isinstance(payload.get("items"), list):
                items = payload["items"]
                if payload.get("total") is not None:
                    total = int(payload["total"])
            else:
                raise InventoryError(f"GET {path}: unexpected response shape on page {page}")
            if total is not None:
                if total < 0 or (expected_total is not None and total != expected_total):
                    raise InventoryError(f"GET {path}: total changed during pagination")
                expected_total = total
            for item in items:
                if not isinstance(item, dict) or item.get("id") is None or not isinstance(item.get("name"), str):
                    raise InventoryError(f"GET {path}: record missing id/name")
                object_id = str(item["id"])
                if object_id in seen:
                    raise InventoryError(f"GET {path}: repeated ID {object_id}; export is not complete")
                seen.add(object_id)
                rows.append(item)
            if expected_total is not None and len(rows) > expected_total:
                raise InventoryError(f"GET {path}: received more records than declared total")
            if not items or (expected_total is not None and len(rows) == expected_total):
                if expected_total is not None and len(rows) != expected_total:
                    raise InventoryError(f"GET {path}: count differs from declared total")
                return rows
            # Fetch until empty: servers can impose a smaller page size than requested.
        raise InventoryError(f"GET {path}: exceeded pagination safety limit")


def scalar(value: object) -> str | None:
    if isinstance(value, dict):
        for key in ("name", "value", "label"):
            if key in value:
                return scalar(value[key])
        return None
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        return str(value).strip() or None
    return None


def metadata_fields(node: object, prefix: str = "") -> list[tuple[str, str, object]]:
    """Flatten named fields and qTest field/value arrays without interpreting IDs."""
    result = []
    if isinstance(node, dict):
        label = node.get("field_name") or node.get("name")
        if isinstance(label, str):
            for key in ("field_value_name", "value", "field_value", "field_value_names"):
                if key in node:
                    result.append((label, f"{prefix}.{key}", node[key]))
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else key
            result.append((key, path, value))
            if isinstance(value, (dict, list)):
                result.extend(metadata_fields(value, path))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            result.extend(metadata_fields(value, f"{prefix}[{index}]"))
    return result


def extract_jira(requirement: dict) -> dict:
    fields = metadata_fields({
        key: value for key, value in requirement.items()
        if key not in ("name", "description", "links")
    })
    key_candidates = []
    type_candidates = []
    summary_candidates = []
    for label, path, value in fields:
        normalized = re.sub(r"[^a-z0-9]", "", label.lower())
        text = scalar(value)
        if not text:
            continue
        if normalized in ("issuekey", "jiraissuekey", "jirakey", "externalkey", "key", "externalurl", "issueurl", "jiraurl") or (
            path.startswith("external_properties.") and normalized in ("id", "link")
        ):
            for match in ISSUE_KEY.finditer(text):
                key_candidates.append({"value": match.group(1), "source": path})
        if normalized in ("issuetype", "jiraissuetype", "workitemtype"):
            # Jira issue type IDs cannot be resolved to names without Jira metadata.
            if not text.isdigit():
                type_candidates.append({"value": text, "source": path})
        if normalized in ("summary", "issuesummary", "jirasummary"):
            summary_candidates.append({"value": text, "source": path})
    name = requirement["name"]
    if not key_candidates:
        key_candidates = [
            {"value": match.group(1), "source": "name"} for match in ISSUE_KEY.finditer(name)
        ]
    if not type_candidates:
        match = ISSUE_TYPE.match(name)
        if match:
            type_candidates.append({"value": match.group(1), "source": "name_prefix"})

    def selected(candidates: list[dict]) -> tuple[str | None, str | None]:
        unique = {item["value"] for item in candidates}
        return (candidates[0]["value"], candidates[0]["source"]) if len(unique) == 1 else (None, None)

    issue_key, key_source = selected(key_candidates)
    issue_type, type_source = selected(type_candidates)
    summary, summary_source = selected(summary_candidates)
    return {
        "issue_key": issue_key, "issue_key_source": key_source,
        "issue_type": issue_type, "issue_type_source": type_source,
        "issue_summary": summary, "issue_summary_source": summary_source,
        "issue_key_candidates": key_candidates, "issue_type_candidates": type_candidates,
        "needs_review": not issue_key or not issue_type,
    }


PROJECT_COLUMNS = (
    "project_id", "project_name", "description", "status_id", "start_date",
    "end_date", "sample", "automation", "uuid", "requirement_count",
)
REQUIREMENT_COLUMNS = (
    "project_id", "project_name", "requirement_id", "requirement_pid", "name",
    "parent_id", "created_date", "last_modified_date", "web_url", "issue_key",
    "issue_key_source", "issue_type", "issue_type_source", "issue_summary",
    "issue_summary_source", "needs_review",
)


def export(client: Client, out: Path, all_projects: bool = False) -> dict:
    out.mkdir(parents=True, exist_ok=False)
    manifest = {
        "schema_version": 1, "source": client.base_url, "status": "in_progress",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "scope": "all_projects_admin" if all_projects else "token_accessible_projects",
    }
    manifest_path = out / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    raw_dir = out / "raw"
    raw_dir.mkdir()
    raw_projects = client.collect("/projects", {"assigned": str(not all_projects).lower()})
    (raw_dir / "projects.json").write_text(
        json.dumps(raw_projects, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    projects, requirements = [], []
    for index, project in enumerate(raw_projects, 1):
        project_id = project["id"]
        LOG.info("Collecting requirements: project %s (%s/%s)", project_id, index, len(raw_projects))
        raw_requirements = client.collect(
            f"/projects/{project_id}/requirements", {"includeExternalProperties": "true"},
        )
        (raw_dir / f"requirements_{project_id}.json").write_text(
            json.dumps(raw_requirements, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        projects.append({
            "project_id": project_id, "project_name": project["name"],
            **{key: project.get(key) for key in PROJECT_COLUMNS[2:-1]},
            "requirement_count": len(raw_requirements),
        })
        for requirement in raw_requirements:
            requirements.append({
                "project_id": project_id, "project_name": project["name"],
                "requirement_id": requirement["id"], "requirement_pid": requirement.get("pid"),
                "name": requirement["name"],
                **{key: requirement.get(key) for key in (
                    "parent_id", "created_date", "last_modified_date", "web_url",
                )},
                **extract_jira(requirement),
            })
    for name, rows, columns in (
        ("projects", projects, PROJECT_COLUMNS),
        ("requirements", requirements, REQUIREMENT_COLUMNS),
    ):
        (out / f"{name}.json").write_text(
            json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        with (out / f"{name}.csv").open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
    manifest.update({
        "status": "complete", "completed_at": datetime.now(timezone.utc).isoformat(),
        "project_count": len(projects), "requirement_count": len(requirements),
        "requirements_with_issue_key": sum(bool(row["issue_key"]) for row in requirements),
        "requirements_with_issue_type": sum(bool(row["issue_type"]) for row in requirements),
        "requirements_needing_review": sum(row["needs_review"] for row in requirements),
        "issue_type_counts": dict(Counter(row["issue_type"] for row in requirements if row["issue_type"])),
    })
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--page-size", type=int, default=200)
    parser.add_argument("--all-projects", action="store_true", help="Admin only: request assigned=false")
    args = parser.parse_args()
    if not 1 <= args.page_size <= 999:
        parser.error("--page-size must be between 1 and 999")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    out = args.output or (
        REPO_ROOT / "output/generated/hs_inventory" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    )
    try:
        base_url, token = load_credentials()
        manifest = export(Client(base_url, token, args.page_size), out, args.all_projects)
    except (InventoryError, OSError, ValueError) as exc:
        LOG.error("Export failed: %s. Any incomplete output is NOT a complete inventory.", exc)
        return 1
    LOG.info("Complete: %s projects, %s requirements. Output: %s",
             manifest["project_count"], manifest["requirement_count"], out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
