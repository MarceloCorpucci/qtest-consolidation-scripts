---
title: "Phase 6 — Production Migration & Cutover"
parent: "qTest Migration Lifecycle & Governance"
labels: [qtest-migration, phase-6]
---

# Phase 6 — Production Migration & Cutover

Execute the approved migration, reconcile data, and transition users to the target instance.

## Phase summary

| Field | Description |
| --- | --- |
| Phase owner | Migration Lead |
| Entry criteria | See below |
| Activities | See below |
| Deliverables | See below |
| Exit criteria | See below |
| Approval | Program Lead |
| Status | Not Started |

## Entry criteria

- [ ] Phase 5 gate approved
- [ ] Cutover window and communication agreed
- [ ] Source freeze announced

## Activities

- Freeze changes in the source projects for the window
- Run `plan` and have it reviewed
- Run `apply --plan <run id>` for the reviewed plan (type the target project id to confirm)
- Run `verify --run <run id>` and reconcile
- Resolve failures and re-run (idempotent)
- Communicate go-live and switch users to the target

## Deliverables

- Production run reports (`output/runs/...`)
- Reconciliation report
- Cutover communication
- Issue list

## Phase Exit Criteria & Approval

Gate: **Phase 6 — Production Migration & Cutover → Phase 7 — Post-Migration Validation & Stabilization**

The next phase cannot start until:

- [ ] All in-scope objects migrated and verified
- [ ] Reconciliation signed off
- [ ] Users informed and working in the target

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

- Previous: Phase 5 — Migration Dry Run & Validation
- Next: Phase 7 — Post-Migration Validation & Stabilization
- [qTest Migration Lifecycle & Governance](../index.md)
