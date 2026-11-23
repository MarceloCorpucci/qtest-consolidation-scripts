---
title: "Phase 1 — Discovery & Assessment"
parent: "qTest Migration Lifecycle & Governance"
labels: [qtest-migration, phase-1]
---

# Phase 1 — Discovery & Assessment

Identify source data, migration scope, dependencies, risks, and data volumes.

## Phase summary

| Field | Description |
| --- | --- |
| Phase owner | Migration Lead |
| Entry criteria | See below |
| Activities | See below |
| Deliverables | See below |
| Exit criteria | See below |
| Approval | Program Lead |
| Status | Completed |

## Entry criteria

- [ ] Migration request approved and sponsor named
- [ ] Read-only API access to the source instance (HS) available

## Activities

- Inventory source projects, folders, test cases, releases, builds, cycles, suites, runs, logs and attachments
- Measure data volumes per project (counts, attachment sizes) with read-only API calls
- Identify what already exists in the target (HCSC): projects, requirements, users
- Classify objects as Done / Planned / Out of scope (see `docs/scope/MIGRATION_SCOPE.md`)
- Record risks: API rate limits, missing requirements, Recycle Bin content, user mapping

## Deliverables

- Scope and object inventory
- Data volume report per project
- Dependency map (test case → run → log; requirement links)
- Initial risk register

## Phase Exit Criteria & Approval

Gate: **Phase 1 — Discovery & Assessment → Phase 2 — Migration Strategy & Planning**

The next phase cannot start until:

- [ ] Scope agreed with stakeholders
- [ ] Volumes and dependencies documented
- [ ] Top risks have an owner and mitigation

| Item | Value |
| --- | --- |
| Gate status | Approved |
| Approver | Program Lead |
| Approval date | |
| Comments | |

## Risks and issues

| ID | Description | Owner | Mitigation | Status |
| --- | --- | --- | --- | --- |
| | | | | |

## Related pages

- Previous: — (start of lifecycle)
- Next: Phase 2 — Migration Strategy & Planning
- [qTest Migration Lifecycle & Governance](../index.md)
