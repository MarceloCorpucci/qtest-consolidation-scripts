---
title: "Migration Governance & Decision Log"
parent: "qTest Migration Lifecycle & Governance"
labels: [qtest-migration, governance]
---

# Migration Governance & Decision Log

## Governance principles

- No phase starts until the gate of the previous phase is approved.
- Nothing is written to the target without an approved plan.
- The source instance is read-only throughout.
- Every decision, risk and change is logged on this page.

## Phase gate model

| Field | Description |
| --- | --- |
| Phase owner | Person responsible for completion |
| Entry criteria | Conditions required to start |
| Activities | Work performed during the phase |
| Deliverables | Required outputs |
| Exit criteria | Conditions that must be satisfied |
| Approval | Person/team authorizing progression |
| Status | Not Started / In Progress / Blocked / Completed |

## Roles (RACI)

| Activity | Program Lead | Migration Lead | QA Lead | Platform Team | Business Owner |
| --- | --- | --- | --- | --- | --- |
| Scope and strategy | A | R | C | C | C |
| Target readiness | I | C | C | R/A | I |
| Development | I | A | C | C | I |
| Dry run validation | I | R | A | C | C |
| Production migration | A | R | C | C | I |
| Acceptance | I | C | R | I | A |
| Closure | A | R | C | C | C |

## Decision log

| ID | Date | Decision | Rationale | Decided by |
| --- | --- | --- | --- | --- |
| D-001 | | Users are not migrated; the API token user owns all created objects | HCSC sets creator/executor and the API cannot change it | |
| D-002 | | All test logs of a run are migrated, oldest first | Newest ends up as latest (`--logs last` optional) | |
| D-003 | | Recycle Bin content never migrates | Deleted data is not in scope | |
| D-004 | | Run Assigned To is the token user; previous assignee goes to `Test Run Details` | HCSC refuses an empty assignee | |
| D-005 | | Requirements are created in HCSC separately; only links migrate | Requirements already exist in the target | |

## Risk register

| ID | Risk | Impact | Likelihood | Mitigation | Owner | Status |
| --- | --- | --- | --- | --- | --- | --- |
| R-001 | Requirements missing in target when links are migrated | Links skipped | Medium | Check in Phase 3; `qmig links` report | | Open |
| R-002 | API errors/limits on large volumes (e.g. 403 on file listing) | Partial migration | Medium | Idempotent re-runs; problems reported in `report.md` | | Open |
| R-003 | Field values without a target match | Fields left empty | Medium | `preflight` comparison | | Open |

## Change log

| Date | Change | Requested by | Approved by |
| --- | --- | --- | --- |
| | | | |
