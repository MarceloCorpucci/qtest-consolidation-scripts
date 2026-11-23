---
title: "Phase 8 — Migration Closure & Decommissioning"
parent: "qTest Migration Lifecycle & Governance"
labels: [qtest-migration, phase-8]
---

# Phase 8 — Migration Closure & Decommissioning

Obtain final sign-off, archive evidence, document lessons learned, and retire the source instance if approved.

## Phase summary

| Field | Description |
| --- | --- |
| Phase owner | Program Lead |
| Entry criteria | See below |
| Activities | See below |
| Deliverables | See below |
| Exit criteria | See below |
| Approval | Sponsor |
| Status | Not Started |

## Entry criteria

- [ ] Phase 7 gate approved

## Activities

- Archive evidence: run output, reports, SQLite map database, approvals
- Document lessons learned
- Obtain final sign-off
- If approved: set the source instance read-only and decommission per retention policy
- Remove API tokens and clean up `.env`

## Deliverables

- Final sign-off
- Archived migration evidence
- Lessons learned
- Decommissioning record

## Phase Exit Criteria & Approval

Gate: **Phase 8 — Migration Closure & Decommissioning → — (end of lifecycle)**

The next phase cannot start until:

- [ ] Final sign-off recorded
- [ ] Evidence archived
- [ ] Source retirement decision documented

| Item | Value |
| --- | --- |
| Gate status | Pending |
| Approver | Sponsor |
| Approval date | |
| Comments | |

## Risks and issues

| ID | Description | Owner | Mitigation | Status |
| --- | --- | --- | --- | --- |
| | | | | |

## Related pages

- Previous: Phase 7 — Post-Migration Validation & Stabilization
- Next: — (end of lifecycle)
- [qTest Migration Lifecycle & Governance](../index.md)
