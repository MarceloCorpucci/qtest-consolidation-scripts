---
title: "Phase 2 — Migration Strategy & Planning"
parent: "qTest Migration Lifecycle & Governance"
labels: [qtest-migration, phase-2]
---

# Phase 2 — Migration Strategy & Planning

Define migration approach, mapping, sequencing, ownership, timeline, and acceptance criteria.

## Phase summary

| Field | Description |
| --- | --- |
| Phase owner | QA Lead |
| Entry criteria | See below |
| Activities | See below |
| Deliverables | See below |
| Exit criteria | See below |
| Approval | Program Lead |
| Status | In Progress |

## Entry criteria

- [ ] Phase 1 gate approved
- [ ] Scope and volumes available

## Activities

- Define the approach: one source project → one target project (`hs-<id>__hcsc-<id>`), API-driven, idempotent re-runs
- Define mappings: folders, test cases, field values by label, execution statuses by name, release status values
- Decide user handling: users are not migrated; everything is created by the API token user, previous assignee kept in text
- Define sequencing: folders and test cases → attachments → approvals → requirement links → releases/builds → cycles/suites → runs → logs
- Define acceptance criteria and reconciliation rules (counts, spot checks, `verify`)
- Agree timeline, cutover window and rollback / re-run strategy

## Deliverables

- Migration strategy document
- Mapping specification
- Sequencing and timeline
- Acceptance criteria
- RACI / ownership

## Phase Exit Criteria & Approval

Gate: **Phase 2 — Migration Strategy & Planning → Phase 3 — Target Environment Readiness**

The next phase cannot start until:

- [ ] Strategy and mappings reviewed and approved
- [ ] Acceptance criteria agreed by business and technical owners
- [ ] Cutover window proposed

| Item | Value |
| --- | --- |
| Gate status | Pending |
| Approver | Program Lead |
| Approval date | |
| Comments | |

## Risks and issues

| ID | Description | Owner | Mitigation | Status |
| --- | --- | --- | --- | --- |
| | | | | |

## Related pages

- Previous: Phase 1 — Discovery & Assessment
- Next: Phase 3 — Target Environment Readiness
- [qTest Migration Lifecycle & Governance](../index.md)
