# Extended migration plan: attachments, releases, cycles, suites, runs, test logs

Status: steps 0-7 are implemented. Builds on `qmig` (folders and test cases, see README).

## Decisions taken

| Question | Answer |
| --- | --- |
| Releases, cycles, suites | Create them in HCSC, linked to each other as in HS so the references show in the UI |
| Users | Not migrated. Submitter and executor become the user of the API token |
| Dates | Keep the original execution dates where qTest accepts them |
| History | All test logs of each test run, oldest first, so the newest ends up as the latest. `--logs last` migrates only the latest |
| Step attachments | Files attached to test steps are copied too; checked first on HS with GET only |
| Granularity | Objects can be migrated one type at a time and by id |

## Object model

```
Project
├─ Folders (modules) ── Test cases ── Steps
│                            └─ Attachments (test case; steps via blob-handles)
├─ Releases ── Builds
│    └─ Test cycles (nest inside cycles)
│         ├─ Test suites
│         │    └─ Test runs ──► point at a TEST CASE
│         └─ Test runs (directly under a cycle)
│               └─ Test logs = execution history
│                     └─ Attachments (on the log)
└─ Requirements ── links to test cases (out of scope for now)
```

- A test run needs the target test case to exist first. The test case map in SQLite is the foundation.
- A test log needs the run, an execution status, dates and optionally a build and release.
- Statuses, builds and releases have different IDs per instance and are mapped by name. Users are not migrated.

## Users

Users are not migrated and not matched. Everything created in HCSC (test cases, cycles, runs, logs) belongs to the user of the HCSC API token. Original executors are not carried over.

## Test log history

Default: every test log of a migrated test run is copied, oldest first, with status, start and end dates, notes, build and the log's attachments. Creating them in date order makes the newest one the run's latest log.
Option `--logs last` copies only the newest log (`GET .../test-runs/{id}/test-logs/last-run`; fallback: newest by execution date). Skipped logs are counted in the report.
Check first on HS (GET only) how many logs per run exist and whether HCSC accepts the original dates and an older log created after a newer one.

## Test step attachments

Step attachments can also be uploaded through `blob-handles`. Test case attachments already work with multipart upload (step 1), and `blob-handles` did not work on HCSC. Step 7 first lists which HS projects have step attachments (GET only), then tries the multipart upload on a step; `blob-handles` is the fallback.

Automated test cases need no step of their own: Type, Automation and Automation Content are ordinary test case fields and are carried by step 1's field mapping (checked live, HS 25760 TC-1311 to HCSC 26256).

Step 7 result: a read-only scan of HS 25804, 25761, 25799 and 25760 found step files only in 25760 (5 files on 5 steps). HS serves a step file at `GET /test-steps/{id}/attachments/{id}` and sends a step's files only with `GET /test-cases/{id}?expand=teststep` (the bulk listing leaves them out). HCSC accepts the upload at `POST /test-steps/{id}/attachments` (multipart); checked live with one test case (HS 25760 to HCSC 26256, file identical).

## Endpoints checked (GET only, HS project 25761, HCSC project 25022)

| Need | Endpoint | Result |
| --- | --- | --- |
| Execution statuses | `GET /projects/{p}/test-runs/execution-statuses` | Works; same 5 statuses and ids (601-605) in both instances, still matched by name |
| Releases | `GET /projects/{p}/releases` | Works; has name, start and end date. HS: 7 releases, HCSC: 2 (no name overlap) |
| Cycles | `GET /projects/{p}/test-cycles?parentId=&parentType=release\|test-cycle` | Works. Nothing sits at `root`; cycles hang under a release and nest under cycles |
| Suites | `GET /projects/{p}/test-suites?parentId=&parentType=test-cycle` | Works |
| Runs | `GET /projects/{p}/test-runs?parentId=&parentType=test-cycle\|test-suite` | Works; each item has `testCaseId` and `latest_test_log` (status and dates) |
| Last execution | `GET /projects/{p}/test-runs/{id}/test-logs/last-run` | Works; returns status, `exe_start_date`/`exe_end_date` with time zone, `user_id`, step logs, `attachments` |
| Builds | `GET /projects/{p}/builds?releaseId=` | Works but returned 0 builds in this HS project; `GET .../releases/{id}/builds` answers 405 |
| Test case attachments | `GET /projects/{p}/test-cases/{id}/attachments` | Works; metadata (name, content type, `web_url`). Download call not tested yet |
| Test log attachments | `GET /projects/{p}/test-logs/{id}/attachments` | Works; the HS sample had none |

Still unchecked, because they need writes: creating releases, cycles, suites, runs and logs, and whether original execution dates are accepted. Test these only with your approval.

## Progress

- Step 0 (preflight mappings) is implemented: `python -m qmig preflight` now also reports status mapping by name and which releases would be created. Read-only.

- Step 1 (test case attachments) is implemented: `plan --attachments`, then `apply` and `verify`. Checked live on HS 25804 to HCSC 32859: 3 files copied, byte-identical. Upload uses `POST .../test-cases/{id}/attachments` (multipart); `blob-handles` stored the raw multipart body and attached nothing on HCSC, so it is not used.

- Step 2 (releases and builds) is implemented: `plan --releases-only` (or `--releases`), `apply`, `verify`. Matched by name, dates and note kept, Status set after creation, builds created under the new release. Tested on the fake server and planned read-only against HS 25761 (7 releases, 0 builds). Not yet written to HCSC: the first real release needs the user's approval.

- Step 2a (approval of migrated test cases) is implemented: `plan --approve [source|all]`. qTest only lets a test case be linked to a requirement once it is approved, and a test case is approved with `PUT .../test-cases/{id}/approve` before linking. A test case is approved when its version is a whole number (`1.0`); a freshly created one is `0.1`. Policy `source` (default) approves only the test cases that are approved in HS; `all` approves every migrated one. Done before steps 3-5 because runs and links need approved test cases.

- Step 3 (test cycles and suites) is implemented: `plan --cycles-only` (or `--cycles`), `apply`, `verify`. Same tree as HS under the mapped release, created top-down, reused by name under the same parent, `cycle_map` in SQLite. Tested on the fake server and planned read-only against HS 25761 (22 cycles, 33 suites). Not yet written to HCSC.
- Step 4 (test runs) is implemented: `plan --runs-only` (or `--runs`), `apply`, `verify`. Runs are created under the mapped cycle or suite and point at the migrated test case; runs whose test case is not migrated (Recycle Bin included) are skipped and listed. `run_map` in SQLite. Tested on the fake server and planned read-only against HS 25804. Not yet written to HCSC; whether HCSC needs an approved test case for a run is still to be seen on the first real write.
- Steps 5 and 5a (test logs) are implemented: `plan --runs-only --logs [all|last]`, `apply`, `verify`. Status by name, original dates and note kept, original tester added to the note, log files copied, per-step results copied by step order (`--no-step-results` to skip), `log_map` in SQLite. Tested on the fake server and planned read-only against the 6 pilot runs of HS 25804. Checked live on HCSC 26256 (6 runs of HS 25804): 6 logs created, original dates kept, runs show the log's status. HCSC accepts logs only on approved test case versions, so the order is test cases, requirement links, approve, runs, logs; runs created on a draft followed the approval to `1.0`.
- Step 5b (test run attachments) is implemented with the runs: the files of each migrated run are copied after the run is created (`plan --runs`, `apply`, `verify`; same upload as test case and test log files, never twice). Tested on the fake server. Not yet seen on real HCSC: the run files endpoint.
- Read-only scan (GET) of HS: 25804 has 634 runs (153 never executed, 10 on Recycle Bin test cases), 17 cycles, 114 suites; 25761 has 285 runs (90 never executed), 22 cycles, 33 suites. Most runs have 0 or 1 log, at most 3; 1 log attachment in total. Runs on Recycle Bin test cases are skipped and listed.

## Next phase: resilience, batch, parallel runs, report (planned)

Decisions: the batch input is a simple CSV (source project, target project, optional module id). The network can fail for a long time, even fully, and a run must come back to exactly where it stopped. The report is one page: mostly numbers and tables, a few charts and timings.

| Phase | What | Notes |
| --- | --- | --- |
| 0 | Measure | Read-only: time per request type and watch for HTTP 429 on the biggest project (HS 25799, 6,293 test cases). Sets how much parallelism is safe |
| 1 | Resilience | See below |
| 2 | `qmig batch` | `batch plan` / `apply` / `verify` / `status` from the CSV. One review of all plans, one approval. A failing project does not stop the others. Resumable. Order per project: missing fields, folders and test cases, files and step files, approval (after links), releases, cycles, runs, logs |
| 3 | Parallel runs | Only as far as phase 0 supports. Across projects first (own run folder each), then parallel downloads and parallel writes across folders; sequential inside one folder so test case numbering keeps the source order; logs oldest first per run. Shared rate limiter that slows down on 429. Default `--workers 1` |
| 4 | One-page HTML report | `qmig report html`: one self-contained file (inline styles and charts, no external resources, prints to PDF). See below |

### Phase 0 result (measured on HS 25799, read-only, no retries)

| Probe | Result |
|---|---|
| One test case with steps, one at a time | median 333 ms, p95 454 ms, max 689 ms (40 calls, all 200) |
| Module list page, one at a time | median 191 ms (10 calls) |
| 2 / 4 / 8 parallel readers (40 calls each) | 6.4 / 12.3 / 21.6 requests per second, no 429, latency unchanged |
| Server limit (response headers) | 5,000 requests per minute (about 83 per second) |

Reading: one reader does about 3 test cases per second, so 6,293 test cases cost about 35 minutes sequentially and about 5 minutes with 8 readers. Eight readers use about a quarter of the limit. One 429 was seen earlier on HS 25761 with a sequential reader, so the limiter must still handle 429 and honour Retry-After. Phase 3 can default to 4 readers and allow up to 8. Writes to HCSC are not measured yet: that needs a real write and your approval.

### Phase 1: what "come back to where it broke" means

- **Network wait.** A connection error (read or write) does not count as a failed item. The run pauses and probes a cheap GET until the instance answers, then continues. Three settings, all configurable on the command line and in `.env`: `--network-retries N` (default 10: how many probes before giving up), `--network-retry-interval <time>` (default 30 s: pause before the first probe), `--network-retry-max-interval <time>` (default 10 min: the pause grows by doubling up to this value, so 10 retries wait about 30 s, 1 min, 2 min, 4 min, 8 min, then 10 min each, roughly 1 h 5 min in total). `--network-retries 0` turns the wait off (fail at once, as today). For a long outage raise the number, for example `--network-retries 100`. When the retries are used up, the run saves its state, exits with the status "interrupted" and prints the resume command. Every wait is logged and counted for the report (interruptions and time lost).
- **Intent journal.** Before each write the run records the intent in SQLite (kind, source id, name, parent). After the answer the entry is closed. On the next start, every open entry is reconciled against the target (found by name, parent, date or file name) and either adopted into the map or created. This closes today's hole: a write that HCSC executed but whose answer was lost (a test case that then gets no files or approval, a log or a file created twice).
- **Reads.** A long outage no longer throws the plan away; reads wait and resume too.
- **Visible state.** `index.csv` shows a run as `interrupted`; `status.md` says what to run next.
- **Tests.** The fake server gets "drop the connection after processing the request" and "outage window" modes.

### Phase 1 status (branch `feature/qmig-resilience`)

Done and tested on the fake server (no real instance touched):
- Network wait for every read and write, three settings (flags and `QMIG_NETWORK_*` in `.env`), exit code 75 and the "resume with" command, run status `interrupted`, `interrupted.json` in the run folder.
- Intent journal for **every created object**: folders, test cases, releases, builds, cycles, suites, runs, logs, and the files of test cases, steps, runs and logs. A write whose reply was lost is looked up in the target (by name under the same parent; logs by start time; files by name on their object): adopted if it exists, created if it does not, never guessed when several candidates exist or the target cannot be searched. Approvals and release status updates are repeated (harmless).
- A write refused by a lost connection is never repeated blindly (only "no route / DNS / refused" writes, which provably never left, are repeated).
- Test: for each of the 10 kinds, the reply of one write is lost followed by a long outage; after resuming, the target equals the target of an undisturbed run (and the test fails for all kinds when the journal is switched off).

- `index.csv` and `status.md` show interrupted runs with the command to resume (every command). `links apply` and `fields apply` check the target after a lost reply before sending again.

Not done yet: nothing is tried on the real instances (a small HCSC pilot with the network switched off for a minute, needs approval).

### Phase 2 status (branch `feature/qmig-batch`)

Done and tested on the fake server: `qmig batch plan | apply | verify | status` from a CSV (source_project, target_project, optional module_id), options of `qmig plan` passed after `--`, optional `--fields`, one confirmation, `--rows`, state in `output/batches/<id>/batch.json` plus a readable `status.md` with time per project, a failing project does not stop the others, a network stop ends the batch with exit 75 and the same command resumes it. Run ids are now unique per second (a batch made two runs in one second collide before).

Also done: `qmig batch links plan | apply | verify` (capture and plan, one confirmation, apply, verify; `nothing-to-link` is not an error; a Links column in the status table).

Not done: parallel projects (phase 3), the HTML report (phase 4). Not tried on the real instances.

### Pilot on the real instances (2026-11-04, HS 25760 and 25804 into HCSC 26256)

- `batch plan`, `batch apply --rows`, `batch verify` worked on real data; every project verified (test cases, files, steps).
- Real writes: about 3 s per file of 0.6 to 2.7 MB, 62 s for 10 test cases with 10 files, 8 folders; 2 s for 2 test cases without files.
- Found and fixed: HCSC refuses an automation test case with an empty Automation Content (HTTP 400); it now gets `auto`.
- Found: `--limit N` still creates the whole folder tree (1,352 folders for 25760); use a folder (`module_id` in the CSV) for pilots.
- Found: approving needs linked requirements in some projects; the pilot ran without `--approve`. For the real projects either the same Jira keys exist in HCSC, or the project setting is switched off during the migration.
- Network cut on the real HCSC (Wi-Fi off, DNS unreachable): the run waited (checks after 30 s and 1 min), found the network after 1 min 30 s and finished on its own; 3 of 3 test cases created, verify passed, no duplicates. A reply lost in the middle of a write is covered by the fake-server tests only.
- Pilot data in HCSC 26256 (delete in the UI): the test cases and folders created from folders `AEP` (25760) and `QNXT Decouple` (25804).

### Phase 4: the one-page report

Content (sample mockup agreed in chat): headline numbers (projects completed of 69, verified-correct share, items migrated, forecast finish); projects completed over time against a target; where the 69 projects stand; a table per object type (planned, migrated, skipped, failed, verified); timings (total, average and longest project, throughput, network interruptions and time lost); needs attention (skipped by design, field values without a match, test cases without a requirement link, failed items to retry, projects awaiting sign-off, what is not migrated by decision). Source: the SQLite maps, `result.json`, `verify.json` and the batch tables.

### Phase 4 status (branch `feature/qmig-report`)

Done: `qmig report html [--batch ID ...] [--out FILE] [--deadline DATE]` writes the one-page, self-contained HTML report described above from `batch.json` and the run files (plan, result, verify, links). Tested on the fake server and made from the real pilot batches. Not done: "awaiting sign-off" (the tool has no sign-off step, so the report shows verified projects instead), a per-day throughput history across many batches, PDF output without a browser.

## Implementation steps

Each step gets `plan`, `apply` and `verify`, its own SQLite map table (re-runs never duplicate), a CSV and a report, and `--only <type>` / `--id <id>` selection. The read-only dry run guarantee stays.

| # | Object | Needs | Notes |
| --- | --- | --- | --- |
| 0 | Preflight additions | | Map statuses and releases by name; list what is missing in HCSC |
| 1 | Test case attachments | Test case map | Download from HS, upload to HCSC; verify compares name and size |
| 2 | Releases and builds | Step 0 | Created in HCSC, name kept, dates kept |
| 2a | Approve test cases | Test case map | `PUT .../approve` after creation; needed before requirement links and runs; policy `source` or `all` |
| 3 | Test cycles and suites | Step 2 | Created top-down as a tree; parent links (release, cycle) kept |
| 4 | Test runs | Steps 1 and 3 | Skipped with a reason if the test case was not migrated |
| 5 | Test logs | Step 4, status and build map | Original dates; the log's attachments included; all logs by default |
| 5a | All test log history | Step 5 | `--logs all` (default) or `--logs last`; oldest first; dates kept |
| 5b | Test run attachments | Step 4 | Files attached to the run itself (`GET/POST .../test-runs/{id}/attachments`), copied with the run |
| 6 | Requirements and links | Done separately | `qmig links` re-links test cases to target requirements by Jira key; requirements themselves are not migrated |
| 7 | Test step attachments | Step 1 | `--step-files`; copied to the step with the same position |

Suggested order: 0, 1, then 2 to 5 as one block, since runs and logs are only useful with their cycles.

## Open items before building

- Confirm the qTest endpoints and payloads on HS and HCSC: attachments (download and upload), releases, builds, cycles, suites, runs, test logs, last-run.
- Confirm qTest accepts original execution start and end dates when creating a test log.
- Attachment size limits and API rate limits on HCSC; large projects will take longer.
- Decide the behaviour for runs whose test case sits in a folder that was not migrated.

## Risks

- Original executors are lost because users are not migrated.
- Cycle and suite trees can be deep; top-down creation and the map table are what make re-runs safe.
- Execution status sets can differ between the instances; unmapped statuses need an explicit rule (fail the item or use a default).
