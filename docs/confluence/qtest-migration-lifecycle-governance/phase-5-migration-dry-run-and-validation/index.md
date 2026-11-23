---
title: "Phase 5 — Migration Dry Run & Validation"
parent: "qTest Migration Lifecycle & Governance"
labels: [qtest-migration, phase-5]
---

# Phase 5 — Migration Dry Run & Validation

Execute test migrations, validate data integrity, performance, dependencies, and completeness.

## Phase summary

| Field | Description |
| --- | --- |
| Phase owner | QA / Migration Team |
| Entry criteria | See below |
| Activities | See below |
| Deliverables | See below |
| Exit criteria | See below |
| Approval | QA Lead + Business Owner |
| Status | Not Started |

## Entry criteria

- [ ] Phase 4 gate approved
- [ ] Non-production or trial target project available

## Activities

- Run `plan` (dry run, writes nothing) and review `report.md`
- Create trial plans with `plan --limit` for a few test cases, then `plan --module-id` for one folder; run `apply --plan <run id>` for each reviewed trial plan
- Run `verify` and reconcile counts source vs target per object type
- Check attachments, approvals, requirement links, releases, cycles, runs and logs
- Re-run to prove idempotency (no duplicates)
- Measure duration and API usage to size the production run
- Log defects and fix; repeat until clean

## Deliverables

- Dry-run reports per project
- Reconciliation results
- Defect log with resolutions
- Performance measurements
- Dry-run sign-off record

## Phase Exit Criteria & Approval

Gate: **Phase 5 — Migration Dry Run & Validation → Phase 6 — Production Migration & Cutover**

The next phase cannot start until:

- [ ] Dry run completed successfully
- [ ] Migration reconciliation completed
- [ ] Data integrity validated
- [ ] Critical defects resolved
- [ ] Migration performance meets agreed targets
- [ ] Business and technical stakeholders approve results

| Item | Value |
| --- | --- |
| Gate status | Pending |
| Approver | QA Lead + Business Owner |
| Approval date | |
| Comments | |

## Risks and issues

| ID | Description | Owner | Mitigation | Status |
| --- | --- | --- | --- | --- |
| | | | | |

## Related pages

- Previous: Phase 4 — Migration Development & Configuration
- Next: Phase 6 — Production Migration & Cutover
- [qTest Migration Lifecycle & Governance](../index.md)
