---
title: "qTest Migration Lifecycle & Governance"
parent: ""
labels: [qtest-migration, governance]
---

# qTest Migration Lifecycle & Governance

Master page for migration strategy, phases, dependencies, governance, progress tracking, and approvals.

> Naming principle: **Lifecycle** is the overall process, **Phase** is each major stage, **Gate** is the mandatory approval that allows progression to the next stage.

## Migration Control Dashboard

| Phase | Status | Owner | Exit approval |
| --- | --- | --- | --- |
| [Discovery & Assessment](phase-1-discovery-and-assessment/index.md) | Completed | Migration Lead | Approved |
| [Strategy & Planning](phase-2-migration-strategy-and-planning/index.md) | In Progress | QA Lead | Pending |
| [Target Readiness](phase-3-target-environment-readiness/index.md) | Not Started | Platform Team | Pending |
| [Development & Configuration](phase-4-migration-development-and-configuration/index.md) | Not Started | Migration Team | Pending |
| [Dry Run & Validation](phase-5-migration-dry-run-and-validation/index.md) | Not Started | QA / Migration Team | Pending |
| [Production Migration](phase-6-production-migration-and-cutover/index.md) | Not Started | Migration Lead | Pending |
| [Stabilization](phase-7-post-migration-validation-and-stabilization/index.md) | Not Started | QA / Business | Pending |
| [Closure](phase-8-migration-closure-and-decommissioning/index.md) | Not Started | Program Lead | Pending |

Status values: Not Started / In Progress / Blocked / Completed. Update this table whenever a gate changes.

## Lifecycle

1. **Phase 1 — Discovery & Assessment** — Identify source data, migration scope, dependencies, risks, and data volumes.
2. **Phase 2 — Migration Strategy & Planning** — Define migration approach, mapping, sequencing, ownership, timeline, and acceptance criteria.
3. **Phase 3 — Target Environment Readiness** — Prepare target qTest, permissions, integrations, configurations, and migration tooling.
4. **Phase 4 — Migration Development & Configuration** — Build and configure custom qTest API migration scripts, mappings, and validation mechanisms.
5. **Phase 5 — Migration Dry Run & Validation** — Execute test migrations, validate data integrity, performance, dependencies, and completeness.
6. **Phase 6 — Production Migration & Cutover** — Execute the approved migration, reconcile data, and transition users to the target instance.
7. **Phase 7 — Post-Migration Validation & Stabilization** — Confirm business acceptance, resolve issues, monitor the target, and stabilize operations.
8. **Phase 8 — Migration Closure & Decommissioning** — Obtain final sign-off, archive evidence, document lessons learned, and retire the source instance if approved.

## Page hierarchy

- [Migration Overview & Scope](01-migration-overview-and-scope.md)
- [Migration Governance & Decision Log](02-migration-governance-and-decision-log.md)
- [Phase 1 — Discovery & Assessment](phase-1-discovery-and-assessment/index.md)
- [Phase 2 — Migration Strategy & Planning](phase-2-migration-strategy-and-planning/index.md)
- [Phase 3 — Target Environment Readiness](phase-3-target-environment-readiness/index.md)
- [Phase 4 — Migration Development & Configuration](phase-4-migration-development-and-configuration/index.md)
- [Phase 5 — Migration Dry Run & Validation](phase-5-migration-dry-run-and-validation/index.md)
- [Phase 6 — Production Migration & Cutover](phase-6-production-migration-and-cutover/index.md)
- [Phase 7 — Post-Migration Validation & Stabilization](phase-7-post-migration-validation-and-stabilization/index.md)
- [Phase 8 — Migration Closure & Decommissioning](phase-8-migration-closure-and-decommissioning/index.md)

## Phase gates

Every phase has a standard **Phase Exit Criteria & Approval** section: Phase owner, Entry criteria, Activities, Deliverables, Exit criteria, Approval, Status.

### Example: Phase 5 → Phase 6

Migration Dry Run & Validation → Production Migration & Cutover. The production migration cannot start until:

- [ ] Dry run completed successfully
- [ ] Migration reconciliation completed
- [ ] Data integrity validated
- [ ] Critical defects resolved
- [ ] Migration performance meets agreed targets
- [ ] Business and technical stakeholders approve results

Gate status: Pending approval
