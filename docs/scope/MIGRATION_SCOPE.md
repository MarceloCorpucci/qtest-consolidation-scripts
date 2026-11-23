# Migration scope: HS -> HCSC (`qmig`)

Status: Done = usable now. Planned = in `docs/execution/EXTENDED_MIGRATION_PLAN.md`. Out = not migrated.
HS is read-only (GET). Every write to HCSC needs approval. Re-runs never duplicate (SQLite maps).

## Objects

| Object | Status | What moves | Matched / mapped by |
| --- | --- | --- | --- |
| Folders (modules) | Done | Full tree, nesting kept. `Recycle Bin` and its content never move | Name + parent path; `folder_map` |
| Test cases | Done | Name, description, precondition, steps (description, expected), fields | Name within folder; `testcase_map` |
| Test case fields | Done | Properties such as Test Phase, Type, Priority | By field name, value by label (see mapping) |
| Automated test cases | Done | What makes a test case automated in qTest is its fields: Type = Automation, Automation (Yes, Tosca ...) and Automation Content. They are migrated with the other fields, checked live (HS 25760 TC-1311 to HCSC 26256). Anything an automation tool links to the test case outside these fields is not copied | By field name, value by label |
| Assigned To | Done | Not set; previous names written into the description as "Migrated metadata" | Names only |
| Test case attachments | Done | Files copied byte for byte | `attachment_map` |
| Test case approval | Done | Approved in HS -> approved in HCSC (`--approve source`, or `all`) | Version `1.0` = approved, `0.1` = draft; `approval_map` |
| Test case -> requirement links | Done | Links re-created to requirements that already exist in HCSC | Jira key; name only if no key; `qmig links` |
| Releases | Done | Name, description, note, start/end dates, Status | Name; `release_map` |
| Builds | Done | Name, description, under the target release | Name per release; `build_map` |
| Test cycles | Done | Name, description, dates; same tree under the mapped release or cycle (up to 4 levels) | Name under the same parent; `cycle_map` (kind `cycle`) |
| Test suites | Done | Name, description, dates; under their release or cycle | Name under the same parent; `cycle_map` (kind `suite`) |
| Test runs | Done | Name; points at the migrated test case; under the mapped cycle or suite | `run_map`; skipped, and listed, if the test case is not migrated (includes Recycle Bin test cases) |
| Test logs | Done | All logs by default, oldest first; status, original dates, note (plus original tester); `--logs last` for the newest only | Status by name; `log_map` |
| Test log attachments | Done | Files of each migrated log | `attachment_map` |
| Per-step results in logs | Done | Status and actual result of each step, matched to the target step by order (`--no-step-results` to skip) | Step order |
| Test run attachments | Done | Files attached to the run itself, copied with the run | `attachment_map` (object type `test-runs`) |
| Test step attachments | Done | Files attached to steps, copied to the step with the same position (`--step-files`) | `attachment_map` (object type `test-steps`) |
| Requirements | Out | Created in HCSC separately; only the links are migrated | - |
| Users | Out | Everything is created by the API token user | - |
| Defects, parameters, comments | Out | Not migrated | - |

## Mappings

| What | Rule |
| --- | --- |
| Project | One HS project -> one HCSC project, by id (`hs-<id>__hcsc-<id>`) |
| Folder | Same name under the mapped parent; existing folder reused |
| Test case | Same name in the mapped folder; existing one is skipped (`skip-exists`) |
| Field value | By label, case-insensitive; no match -> field left empty and listed in the report |
| Status (release) | By value: 300 Planned, 301 In Progress, 302 Completed, 303 Closed |
| Release dates | Kept; a start before the target project start is replaced by the project start |
| Execution status | By name (Passed, Failed, Blocked, Incomplete, Unexecuted) |
| Executor / submitter / creator | Token user of HCSC (HCSC sets it; the API cannot change it) |
| Run Assigned To | Token user (HCSC default; it refuses an empty value). The previous assignee is written to the run's `Test Run Details` field |
| Deleted in HCSC after migration | Detected at `plan`, object is recreated |

## Rules

- Never move anything from `Recycle Bin`.
- Nothing is pushed or written without approval; `plan` is a dry run.
- Selection by type and id is possible (`--releases-only`, `--cycles-only`, `--runs-only`, `--logs`, `--release-id`, `--limit`).
- Output: `output/runs/<src>-<id>__<tgt>-<id>/<run_id>/`, `index.csv`, per-pair `status.md`.
