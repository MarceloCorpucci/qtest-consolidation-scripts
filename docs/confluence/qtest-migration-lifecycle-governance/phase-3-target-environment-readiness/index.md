---
title: "Phase 3 — Target Environment Readiness"
parent: "qTest Migration Lifecycle & Governance"
labels: [qtest-migration, phase-3]
---

# Phase 3 — Target Environment Readiness

Prepare target qTest, permissions, integrations, configurations, and migration tooling.

## Phase summary

| Field | Description |
| --- | --- |
| Phase owner | Platform Team |
| Entry criteria | See below |
| Activities | See below |
| Deliverables | See below |
| Exit criteria | See below |
| Approval | Platform Owner |
| Status | Not Started |

## Entry criteria

- [ ] Phase 2 gate approved
- [ ] Target instance and projects provisioned

## Activities

- Create / confirm target projects and their ids
- Align project configuration: test case fields and allowed values, statuses, approval workflow
- Confirm the API token user has the required permissions on the target project
- Confirm target requirements exist (needed for requirement links) and Jira keys are present
- Install and configure `qmig` on the migration workstation (`setup`, `.env`, `doctor`)
- Run `preflight` to compare fields between source and target (read-only)

## Deliverables

- Target readiness checklist
- Field/config comparison (`preflight` output)
- Working `doctor` result for source and target
- Access and permission confirmation

## Phase Exit Criteria & Approval

Gate: **Phase 3 — Target Environment Readiness → Phase 4 — Migration Development & Configuration**

The next phase cannot start until:

- [ ] `doctor` passes for both instances
- [ ] `preflight` differences resolved or accepted
- [ ] Required requirements exist in the target

| Item | Value |
| --- | --- |
| Gate status | Pending |
| Approver | Platform Owner |
| Approval date | |
| Comments | |

## Risks and issues

| ID | Description | Owner | Mitigation | Status |
| --- | --- | --- | --- | --- |
| | | | | |

## Related pages

- Previous: Phase 2 — Migration Strategy & Planning
- Next: Phase 4 — Migration Development & Configuration
- [qTest Migration Lifecycle & Governance](../index.md)
