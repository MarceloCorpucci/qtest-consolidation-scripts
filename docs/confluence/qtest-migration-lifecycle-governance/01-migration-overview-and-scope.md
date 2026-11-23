---
title: "Migration Overview & Scope"
parent: "qTest Migration Lifecycle & Governance"
labels: [qtest-migration, scope]
---

# Migration Overview & Scope

## Purpose

Instance-to-instance qTest migration from **HS** (`healthspring.qtestnet.com`, source, read-only) to **HCSC** (`hcsctest.qtestnet.com`, target). One source project maps to one target project. The target already contains Projects and Requirements.

## Approach

- Custom tool `qmig` (Python 3.10+, standard library only, Windows and macOS), local state in one SQLite file.
- Workflow per project pair: `preflight` → `plan` (dry run) → `apply` (reviewed plan) → `verify`.
- Re-runs are idempotent: nothing is duplicated; objects deleted in the target are recreated at the next `plan`.
- Source is never written to; every write to the target needs approval.

## Objects in scope

| Object | Status | Notes |
| --- | --- | --- |
| Folders (modules) | Done | Full tree; `Recycle Bin` never moves |
| Test cases, steps, fields | Done | Fields mapped by label |
| Test case attachments | Done | Copied byte for byte |
| Test case approval | Done | Version 1.0 = approved, 0.1 = draft |
| Test case → requirement links | Done | Matched by Jira key to existing requirements |
| Releases, builds | Done | Matched by name |
| Test cycles, suites | Done | Same tree under the mapped release/cycle |
| Test runs, test logs | Done | Logs oldest first; attachments and step results included |
| Test step attachments | Planned | |
| Requirements | Out of scope | Exist in the target; only links migrated |
| Users | Out of scope | Everything created by the API token user |
| Defects, parameters, comments | Out of scope | |

Source of truth: `docs/scope/MIGRATION_SCOPE.md` in the repository.

## Key mappings

| What | Rule |
| --- | --- |
| Project | One HS project → one HCSC project, by id |
| Folder / test case | Same name under the mapped parent; existing reused/skipped |
| Field value | By label, case-insensitive; no match → left empty and reported |
| Execution status | By name |
| Executor / creator / run assignee | Token user of HCSC; previous assignee recorded in text |

## Assumptions and constraints

- API tokens for both instances are available to the migration workstation.
- Requirements must exist in HCSC before links are migrated.
- Users are not migrated; original executors are not carried over.

## Stakeholders

| Role | Name | Responsibility |
| --- | --- | --- |
| Program Lead | | |
| Migration Lead | | |
| QA Lead | | |
| Platform Team | | |
| Business Owner | | |
