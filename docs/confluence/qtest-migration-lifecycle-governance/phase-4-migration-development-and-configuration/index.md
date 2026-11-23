---
title: "Phase 4 — Migration Development & Configuration"
parent: "qTest Migration Lifecycle & Governance"
labels: [qtest-migration, phase-4]
---

# Phase 4 — Migration Development & Configuration

Build and configure custom qTest API migration scripts, mappings, and validation mechanisms.

## Phase summary

| Field | Description |
| --- | --- |
| Phase owner | Migration Team |
| Entry criteria | See below |
| Activities | See below |
| Deliverables | See below |
| Exit criteria | See below |
| Approval | QA Lead |
| Status | Not Started |

## Entry criteria

- [ ] Phase 3 gate approved
- [ ] Mapping specification baselined

## Activities

- Develop `qmig` capabilities per object type (plan / apply / verify)
- Keep the source read-only and the plan step write-free (read-only target client)
- Implement idempotency maps in SQLite (`folder_map`, `testcase_map`, `run_map`, `log_map`, `attachment_map`, ...)
- Implement reports and run output (`report.md`, `run.log`, CSVs, `index.csv`, per-pair `status.md`)
- Cover the remaining planned items (e.g. step attachments)
- Maintain unit tests (`python -m unittest discover -s tests/qmig -t .`)

## Deliverables

- Tested `qmig` release
- Execution guide (`docs/execution`)
- Scope document kept in sync (`docs/scope`)
- Test results

## Phase Exit Criteria & Approval

Gate: **Phase 4 — Migration Development & Configuration → Phase 5 — Migration Dry Run & Validation**

The next phase cannot start until:

- [ ] All in-scope objects supported by plan, apply and verify
- [ ] Unit tests pass
- [ ] Code reviewed; no open critical defects

| Item | Value |
| --- | --- |
| Gate status | Pending |
| Approver | QA Lead |
| Approval date | |
| Comments | |

## Risks and issues

| ID | Description | Owner | Mitigation | Status |
| --- | --- | --- | --- | --- |
| | | | | |

## Related pages

- Previous: Phase 3 — Target Environment Readiness
- Next: Phase 5 — Migration Dry Run & Validation
- [qTest Migration Lifecycle & Governance](../index.md)
