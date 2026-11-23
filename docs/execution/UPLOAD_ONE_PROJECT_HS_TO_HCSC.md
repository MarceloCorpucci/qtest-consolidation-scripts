# Upload one project from HS to HCSC (qmig)

Scope: folders, test cases, attachments (also on test steps), releases/builds, test cycles/suites, test runs, test logs (execution history) and test case -> requirement links (no requirements yet).
Works the same on Windows and macOS. On Windows use `py -m qmig` where this guide shows `python3 -m qmig`.

## Before you start

- Python 3.10+ is installed. `qmig` needs no other packages.
- Credentials for both instances are in `.env` in the repo root (git-ignored, never commit it). Copy the template and fill in the tokens:

  ```bash
  cp .env.example .env
  ```

  ```
  QMIG_HS_URL=https://healthspring.qtestnet.com
  QMIG_HS_TOKEN=...
  QMIG_HCSC_URL=https://hcsctest.qtestnet.com
  QMIG_HCSC_TOKEN=...
  ```

  `python3 -m qmig setup` creates this template if `.env` is missing. Real environment variables win over `.env`.
- Use a project you can clean up. `apply` creates real folders and test cases in HCSC and there is no rollback command; deleting them is manual in qTest.
- The HCSC project must already exist.
- You know both project IDs. The ID is in the qTest URL: `.../p/<ID>/portal/project`.

Run all commands from the repo root.

## 1. Check API access

```bash
python3 -m qmig doctor
```

`connection hs` and `connection hcsc` must be `OK`. If a token is rejected, fix it in `.env`.

## 2. Compare the fields (read-only)

```bash
python3 -m qmig preflight --source-project <HS_ID> --target-project <HCSC_ID>
```

Open `output/runs/<run id>/report.md`. It lists:

- fields or options missing in HCSC (those values are dropped),
- required HCSC fields with no source field (the default value is used, or creation may be rejected),
- execution statuses matched by name (a status missing in HCSC is a warning),
- releases matched by name: `create` means the release does not exist in HCSC yet. To migrate them, see "Releases and builds" below.

Fix important gaps in HCSC before continuing.

### 2a. Create the missing test case fields (do this before the first upload)

Values of a test case field that does not exist in the HCSC project are dropped, so create the missing custom fields first. The source is only read; only the target project is written.

```bash
python3 -m qmig fields plan --source-project <HS_ID> --target-project <HCSC_ID>
python3 -m qmig fields apply --plan <fields plan run id>
python3 -m qmig fields verify --run <fields apply run id>
```

- `plan` writes nothing. `report.md` lists each custom field as `create` (missing in HCSC), `exists` (same label, type and options) or `manual` (exists with other options or type, for example Test Phase without `New Value`).
- `apply` creates the `create` fields with the same label, type, options, default option and required flag as in HS. It asks you to type the target project id (or pass `--yes`), skips a field that already exists, and stops after 3 failures in a row. Existing fields are never changed: fix `manual` rows by hand in qTest.
- `verify` reads the target fields back and checks label, type and options.
- Without `--skip-fields` the plan follows the stakeholder decisions: it only creates `Test Scenario Id`. Left out on purpose: the six fields kept in the description, `Assigned To`, `Test Phase` (not changed), and `Team Specific` / `Test Scenario` (waiting for the QE DSU decision). Pass `--skip-fields` with your own list to change that.
- Test case fields only. System fields are skipped. Run it once per HCSC project; the project-level create request is unconfirmed on the real instance, so try it on a sample project first.

## 3. Dry run with a small sample (writes nothing)

```bash
python3 -m qmig plan --source-project <HS_ID> --target-project <HCSC_ID> --limit 5
```

Review in `output/runs/<run id>/`:

- `report.md` - summary, issues, what is not covered
- `folders.csv` and `testcases.csv` - one row per item with the planned action

To also migrate the attachments of the test cases, add `--attachments` (one extra read request per test case). The report then counts the files to copy and writes `attachments.csv`. Only test cases that `apply` creates, or created in an earlier run, get attachments; a test case that already exists in HCSC under the same name is left alone.

If the token cannot read the field settings of the HCSC project (preflight or plan stops with HTTP 403), add `--no-properties` to `preflight` and `plan`: custom field values are then not copied, only name, description, precondition and steps.

To try one folder only, add `--module-id <HS folder id>`. Its parent folders are created empty in HCSC (shown as `context_only` in `folders.csv`, without their test cases) so the folder keeps its place in the tree, and existing parents with the same name are reused. Add `--without-parents` to put the folder at the top instead. To create under an existing HCSC folder, add `--target-parent-id <HCSC folder id>`.

Planned actions: `create`, `reuse` / `reuse-mapped` (folder already in target), `skip-mapped` (done by an earlier run), `skip-exists` (same name already in the target folder), `deferred` (beyond `--limit`).

## 4. Apply the sample

```bash
python3 -m qmig apply --plan <plan run id>
```

With `--attachments` in the plan it also downloads each file from HS and uploads it to the new HCSC test case; a file that fails is listed in the report and sent again on the next `apply` of the same plan (copied files are skipped). Uploads can be slow on large files. It asks you to type the HCSC project ID to confirm (use `--yes` for non-interactive runs). It replays exactly the reviewed plan. Re-running is safe: created items are recorded in `data/qmig.sqlite` and skipped.

## 5. Verify

```bash
python3 -m qmig verify --run <apply run id>
```

With attachments, verify also checks that every copied file is listed on its HCSC test case. Then open the project in HCSC and check the folders, test cases and attachments.

## Where the run output goes

Every command writes its run into a folder of its project pair, so many projects stay easy to find:

```
output/runs/
  hs-25804__hcsc-26256/
    20261103-014905-plan-pilot/      <- one folder per run (log, report, CSV files, run.json)
    20261103-015116-apply-pilot/
    status.md                        <- latest plan / apply / verify of this pair
  index.csv                          <- one row per run of all projects (opens in Excel)
```

- Commands still take just the run id, e.g. `apply --plan 20261103-014905-plan-pilot`; qmig finds the folder.
- `plan --label pilot` adds a name to the run id; `apply` and `verify` of that plan keep it. Letters, digits and dashes only.
- `index.csv` and `status.md` are rebuilt after every command. They show project names, status and the folder of each run.
- Runs created before this layout sit directly in `output/runs`; they are still found by id, but are not in `index.csv`. You can delete them.

## If you delete migrated items in HCSC

This also covers releases, builds, cycles, suites and runs: `plan` notices they are gone (a warning says so) and `apply` creates them again, together with everything that was below them.

`plan` checks that every folder and test case it remembers from an earlier run still exists in HCSC. Anything you deleted there is listed as a warning ("migrated earlier but no longer exist") and is created again by `apply`, with its attachments and approval. Items that still exist are left alone.

## Previous "Assigned To" and unneeded fields kept in the description

Users are not migrated, so the Assigned To field is not copied. Instead the name shown in HS is added to the end of the HCSC test case description (after the existing text, on a new line):

```
<b>Migrated metadata</b>:
(Previously) "Assigned To" : "John Smith"
```

The name comes straight from the HS test case (no extra user lookup); several assignees are each quoted and separated by commas, e.g. `(Previously) "Assigned To" : "John Smith", "Jane Doe"`. A test case with nobody assigned keeps its description unchanged.

The values of HS fields that HSCS does not need are added the same way, one line each, only when the test case has a value (empty, `_NULL_` and zero values are ignored): `Eligible to Automate`, `Regression Flag`, `Estimated Time`, `Hierarchy`, `Zephyr Test Case ID`, `Comments (Archive)`. Example: `(Previously) "Eligible to Automate" : "N"`. In HS, Eligible to Automate and Regression Flag default to `N`, so most test cases get those two lines. Use `plan --no-metadata` to switch this off. It is added only when a test case is created: test cases migrated earlier are not changed.

To try one test case first, combine `--module-id <its folder>`, `--without-parents` and `--test-case-id <source id>` (repeatable); only that test case is planned, and the folder it sits in is created.

To try one test case first, combine `--module-id <its folder>`, `--without-parents` and `--test-case-id <source id>` (repeatable); only that test case is planned, and the folder it sits in is created.

## Automated test cases

A test case is "automated" in qTest through three fields: Type = Automation, Automation (Yes, No, Tosca, Tosca Cloud) and Automation Content (free text). They are copied with the other test case fields, so no extra option is needed; Type and Automation use the same value ids in HS and HCSC. Checked live: HS 25760 TC-1311 (Automation, Yes, `auto`) arrived in HCSC 26256 with the same three values. Only HS 25760 has such test cases among the projects checked (187 of 1,321); 25804 and 25761 are all Manual. Anything an automation tool links to a test case outside these fields is not copied. Custom fields that HCSC lacks, such as "Eligible to Automate", are dropped with a warning unless they are created first.

Automation test cases without Automation Content: HS allows an automated test case (Type Automation, or Automation Yes/Tosca/Tosca Cloud) with an empty Automation Content, HCSC refuses it ("Required Automation Content for Automation test case"). Such a test case gets the content `auto`; the plan report says how many. Real content is never changed.

## Files attached to test steps

Add `--step-files` to the plan to also copy the files attached to test steps (the images and spreadsheets people put on a single step). It costs one extra read request per test case on HS, so it is not on by default; an HS check of 5 projects found step files in only one of them (5 files).

- `plan --step-files` lists, per test case, the steps that carry files and the position the step gets in HCSC; the report counts the files to copy. A step that has no text is not migrated, so its files are not copied either; this is shown as a warning.
- `apply` downloads each file from HS (read-only) and uploads it to the step with the same position on the new HCSC test case. It runs right after the test case files and before approval. A file is never uploaded twice, a file that fails is listed in the report ("Step file problems", `step_files.csv`) and is sent again on the next `apply` of the same plan.
- Only test cases that `apply` creates, or created in an earlier run, get step files; a test case that already exists in HCSC under the same name is left alone.
- `verify` reads each target test case and checks that every copied file is on its step.
- The upload goes to `POST /test-steps/{id}/attachments` of HCSC (multipart, like the test case files). Checked live: HS 25760 test case TC-2744 to HCSC 26256, the `.xlsx` on step 1 arrived byte for byte identical and `verify` passed.

## Approving test cases

New test cases in HCSC are drafts (version `0.1`). HCSC (project 26256) refuses to approve a test case that has no linked requirement, so the order for a project is: create the test cases, link the requirements (`links`, below, or by hand in the UI), then approve. Add `--approve` to `plan` for the approval step:

```bash
python -m qmig plan --source-project <HS_ID> --target-project <HCSC_ID> --approve        # approve those approved in HS
python -m qmig plan --source-project <HS_ID> --target-project <HCSC_ID> --approve all    # approve every migrated test case
```

`apply` approves after the test cases and attachments are created, and `verify` checks that each one now has a whole-number version (`1.0`). Re-running never approves twice, and test cases that already existed in HCSC under the same name are left alone. To approve test cases migrated earlier, plan the same folder again with `--approve`; they appear as `skip-mapped` and only the approval is done. The report has an "Approvals" section and `approvals.csv`.

## Releases and builds (one object type at a time)

Releases (and their builds) can be migrated on their own, before any cycles or suites exist in HCSC:

```bash
python -m qmig plan --source-project <HS_ID> --target-project <HCSC_ID> --releases-only
python -m qmig apply --plan <plan run id>
python -m qmig verify --run <apply run id>
```

- `--releases-only` plans no folders or test cases. Use `--releases` instead to add releases to a normal folders + test cases plan.
- A release is matched by name. One that already exists in HCSC is reused (not changed) and remembered for the later cycle/suite step; the others are created with the same name, description, note, start/end dates and Status.
- Builds of each release are created under the HCSC release. Builds are read with `GET /builds?releaseId=`; neither HS project has any builds today, so this part is only tested against the test server.
- The report has a "Releases and builds" section and `releases.csv`. Re-running `apply` never creates a release or build twice. `verify` checks that each release exists with the same dates.
- Release custom fields other than Status are not copied. Users are not involved.

## Test cycles and suites (after the releases)

Cycles and suites are created under the same release or cycle as in HS, so the tree looks the same in HCSC (HS projects go up to 4 levels):

```bash
python -m qmig plan --source-project <HS_ID> --target-project <HCSC_ID> --cycles-only --label cycles
python -m qmig apply --plan <plan_run_id>
python -m qmig verify --run <apply_run_id>
```

- `--cycles-only` plans releases, builds, cycles and suites, no folders or test cases. `--cycles` adds them to a normal plan. Both imply `--releases`, because cycles hang under releases; `--release-id` limits the plan to one release.
- `apply` creates releases first, then cycles and suites top-down. If a cycle fails, everything below it is `skipped`; a re-run creates only what is missing. A cycle or suite with the same name under the same parent is reused (`reuse`).
- Name, description and dates are kept. Runs are covered in the next section.
- The report has a "Test cycles and suites" section and `cycles.csv`. `verify` checks that each one is listed under its target parent with the same name.
- Checked read-only on HS 25761: 22 cycles and 33 suites planned for HCSC 26256. The first real write needs your approval.

## Test runs (after the test cases)

Order per project: test cases, requirement links, approve, then runs and logs.

A run sits in a cycle or suite and points at a test case. The target run points at the migrated test case, so migrate the test cases first:

```bash
python -m qmig plan --source-project <HS_ID> --target-project <HCSC_ID> --runs-only --label runs
python -m qmig apply --plan <plan_run_id>
python -m qmig verify --run <apply_run_id>
```

- `--runs-only` plans releases, cycles, suites and runs, no folders or test cases; `--runs` adds them to a normal plan (then runs whose test case is in the same plan are created too). Both imply `--cycles`.
- A run whose test case is not migrated is **skipped and listed** (`skip-no-testcase`, with the source test case id, in `runs.csv` and the report). This covers every test case in the Recycle Bin.
- **Assigned To is the user of the API token**, which is what HCSC assigns to a new run (users are not migrated; HCSC 26256 refuses an empty Assigned To). The creator of a run is the same user and cannot be changed through the API.
- **The previous Assigned To** is written to the run's `Test Run Details` field (plain text, as runs have no description): `Migrated metadata: (Previously) "Assigned To" : "Jeffrey Jandi"`. `--no-metadata` leaves it out. If the target has no such field, the report says it was not recorded.
- Files attached to the run itself are copied after the run is created (`run_attachments` in the plan counts, a "Test run files" table in the report). A file is never uploaded twice, and `verify` checks that each file is on the target run.
- Besides the files, only the run itself (name, test case, place in the tree) is copied now. Planned dates, priority and similar run fields are not. Execution results (test logs) come in the next step.
- A run with the same name and test case under the same target parent is reused. Re-running creates nothing twice. `verify` checks each run is listed under its parent and points at the migrated test case.
- `--cycle-id <id>` (repeatable, with `--cycles`/`--runs`) plans one source cycle with everything below it; the cycles above it are created empty (`context only`) so it keeps its place, and only the releases above are planned. Use it for a small first test.
- Checked read-only on HS 25804: 634 runs, of which 6 can be created now (only 3 test cases are migrated to HCSC 26256) and 628 are skipped for that reason. The first real write needs your approval.

## Test logs: execution history (after the runs)

```bash
python -m qmig plan --source-project <HS_ID> --target-project <HCSC_ID> --runs-only --logs --label logs
python -m qmig apply --plan <plan_run_id>
python -m qmig verify --run <apply_run_id>
```

- `--logs` (same as `--logs all`) copies every log of each migrated run, **oldest first**, so the newest log ends up as the run's result. `--logs last` copies only the newest log of each run. It implies `--runs`; use it together with `--runs-only` to skip folders and test cases, and with `--cycle-id` for a small first test.
- Each log keeps its status (matched by name), original start and end dates and note, plus its files. The executor is the API token user (users are not migrated); the original tester's name is added to the note as "Migrated metadata: originally executed by ...". `--no-metadata` leaves the note as it was.
- A log whose status does not exist in HCSC is `skip-no-status`; a log of a skipped run is not planned. The per-step results (Passed, Failed ... of each step, with the actual result) are copied too, matched to the target steps by step order; `--no-step-results` leaves them out. If the target refuses the step results, the log is still created without them and the report lists it under "Step results with a problem".
- Re-running creates nothing twice (`log_map`). If you delete runs in HCSC, `plan` notices and `apply` creates the runs and their logs again.
- `verify` checks that each log is listed under its run with the same status and start date, that its step results have the same status, that its files are there, and that each run shows the status of its newest log.
- **HCSC accepts logs only on an approved test case version** ("Invalid test case version. Please specify an approved version."). Approve the test cases first (`--approve`, after the requirements are linked). A run created while its test case was a draft followed the approval to `1.0` on HCSC 26256, so it does not have to be recreated. `plan` skips the logs of a run whose test case version is not approved (`skip-not-approved`) and says why.
- Checked live on HCSC 26256 (6 runs of HS 25804): 6 logs created, original execution dates kept, each run now shows Passed, a second apply created nothing.

## Requirement links (after the requirements exist in HCSC)

Requirements are not migrated, but the HS test case -> requirement coverage is kept. A requirement is found in HCSC by its **Jira issue key** (`external_properties.id`, e.g. `POD-233`; a key at the start of the requirement name is used when there is no `external_properties`). Only a requirement **without** a key is matched by its name (case, spaces, HTML entities and invisible characters ignored). Anything that matches more than one HCSC requirement is reported and not linked. This assumes the HCSC requirements are created with the same Jira keys as in HS.

Run after the test cases are migrated (`apply`), in this order:

```bash
python -m qmig links capture --source-project <HS_ID> --target-project <HCSC_ID>
python -m qmig links plan    --source-project <HS_ID> --target-project <HCSC_ID>
python -m qmig links apply   --plan <links plan run id>
python -m qmig links verify  --run <links apply run id>
```

- `capture` only reads HS. It stores, for every migrated test case, the requirements it covers (Jira key and name) in `data/qmig.sqlite`. It can run before the requirements exist in HCSC; run it again to pick up newly migrated test cases.
- `plan` writes nothing. Open `output/runs/<run id>/report.md` and `links.csv`: `link` = will be linked, `exists` = already linked in HCSC, `not-found` = no HCSC requirement with this key (create it, then plan again), `ambiguous` = more than one match, `tc-not-migrated` = the test case is not in HCSC.
- `plan --name-on-key-miss` also tries the name when a requirement has a Jira key that HCSC does not (off by default, because a name can match the wrong requirement).
- `plan --key-map ABC=SCRUM,BCA=SCRUM` is for **pilots only**, when the HS and HCSC test projects use different Jira projects: `ABC-111` is looked up as `SCRUM-111`. `ABC-5=SCRUM-9` maps one exact key (it wins over a prefix rule). Not used unless you pass it, so a production run keeps the normal key-then-name matching. The mapping is written in the plan (`scope.key_map`, report line, and `mapped_key` per row). It works the same in `batch links plan --key-map ...`.
- `apply` links test cases to each requirement in batches (`--batch-size`, max 100) with `POST /requirements/{id}/link?type=test-cases`. Re-running the same plan never links twice. It stops on HTTP 401/403 or after 25 failures in a row.
- `verify` reads the links back from HCSC.
- Not tested against the real HCSC API yet (only against the test server): the read side (`linked-artifacts`) was checked on HS 25804, the write call is taken from the qTest documentation. Try one project first.

## 6. Full project

If the sample looks right, repeat step 3 without `--limit`, review the plan, then run `apply` and `verify`.

## Never migrated: Recycle Bin

A folder named **Recycle Bin** (any level, any letter case), with every sub-folder and test case inside it, is skipped by `plan` and `apply`. The plan report notes how many folders were left out (`excluded_folders` in `plan.json`). HS 25804 has one at the top: 32 folders and 52 test cases that stay in HS. Asking for a folder inside it with `--module-id` stops with an error.

## Not migrated yet

`apply` creates folders and test cases (with steps and properties) and, with `--attachments`, their attachments. Releases and builds are migrated with `--releases` / `--releases-only` (see above). Test case -> requirement links are re-created with `links` once the requirements exist (see above). Test cycles and suites are migrated with `--cycles` / `--cycles-only` (see above). Test runs are migrated with `--runs` / `--runs-only` (see above). Test logs are migrated with `--logs` (see above). Requirements themselves are not migrated. Users are not migrated.

## Reading the results

Every run writes `output/runs/<run id>/`:

| File | Content |
| --- | --- |
| `report.md` | Summary, issues, failures, API usage, next command |
| `run.log` | Full log, every API call with status and time |
| `events.jsonl` | One line per action |
| `folders.csv`, `testcases.csv` | Per-item status, target IDs, warnings, errors |
| `plan.json`, `testcases.jsonl` | Plan and exact payloads (plan runs) |
| `result.json`, `verify.json` | Outcome of apply / verify |

Exit codes: `0` ok, `1` failures (failed test cases, verify mismatch), `2` configuration or usage error.
`python3 -m qmig runs` lists previous runs.

## Safety

- `plan` and `preflight` cannot write: the target client is read-only.
- HS is never written to by any command.
- `apply` stops immediately on HTTP 401/403, or after 25 failures in a row.
- Tokens are masked in logs.

## Many projects at once (batch)

For the 69 projects, use a CSV and the `batch` commands. They run the same plan, apply and verify you would run by hand, one project after another, and add one overview.

1. Make a CSV (a header row, then one row per project; `module_id` is optional, `;` also works, lines starting with `#` are ignored):

   ```
   source_project,target_project,module_id
   25804,26256,
   25761,26257,
   ```

2. Plan every project (read-only). Everything after `--` is given to `qmig plan` for each project:

   ```bash
   python -m qmig batch plan --csv projects.csv -- --attachments --step-files --approve all
   ```

   Add `--fields` before the `--` to also plan the missing test case fields of each project. The batch id is printed (and `output/batches/<id>/status.md` is the readable overview, with the plan counts and the time per project). A project that cannot be planned is listed with its error; the others are not affected.

3. Review the plans (each is an ordinary run folder with its `report.md`), then apply all of them with one confirmation:

   ```bash
   python -m qmig batch apply --batch <id>
   ```

   You type the number of projects to continue (or pass `--yes`). `--rows 1,3` applies only those CSV rows (useful for a first try with one project). A project with problems does not stop the others.

4. Check the result and verify:

   ```bash
   python -m qmig batch status --batch <id>
   python -m qmig batch verify --batch <id>
   ```

**If it stops** (network, or you close the window): run the same `batch apply` again. Projects already applied are skipped; the project that was running continues with its own plan, and a lost reply is settled as described below. Projects that finished with errors are tried again, only the missing items are created. The batch stops with exit code 75 when the network does not come back (see the settings below); `--network-retries` and friends work on `batch` commands too. Without `--batch` the latest batch is used.

### Links for all projects of the batch

Links restore which test cases cover which requirements. Requirements are not migrated, so run this **after the test cases are applied and the requirements exist in HCSC** (for example after the Jira sync). Same pattern as above:

```bash
python -m qmig batch links plan --batch <id>
python -m qmig batch links apply --batch <id>
python -m qmig batch links verify --batch <id>
```

- `links plan` reads the covered requirements of each migrated test case in HS, then matches them to the HCSC requirements (Jira key first, then name for requirements without a key; add `--name-on-key-miss` to also match by name when HCSC does not know the key). It writes nothing. A project whose test cases cover no requirement shows `nothing-to-link`, which is not an error. The apply step lists, per project, how many links will be created and how many cannot (`not-found`: the requirement is not in HCSC yet; `ambiguous`: several candidates, never guessed).
- `links apply` asks once (type the number of projects, or `--yes`), `--rows 1,3` limits it. A failed project does not stop the others; a network stop ends it with exit code 75 and the same command continues; a link already made is never sent twice.
- Run `links plan` again after more requirements arrive in HCSC, then `links apply`: only the missing links are created.
- The status table has a "Links" column.

Projects run one after another (parallel runs are a later step).

## One-page status report (for stakeholders)

```bash
python -m qmig report html --batch <id> --deadline 2026-12-31
```

One self-contained HTML file (`report.html` in the batch folder, or `--out <file>`): no scripts and no external resources, so it can be e-mailed and opens in any browser. Print it to PDF from the browser (it is laid out for one A4 page). Repeat `--batch` to combine several batches in one report; `--deadline` is optional and draws the target line in the progress chart. The report can be made at any time, also while a batch is running or interrupted.

It contains: projects verified of the total, the verified-correct share, items created, a forecast finish at the current speed; projects completed over time (with the target line); where the projects stand (verified, applied, errors, interrupted, planned ...); timings (total, average and longest project, items per minute, network interruptions and the time they cost); a table per object type (planned, created, already there, left out, failed, verified ok); a row per project; what needs attention (failed items to retry, errors, field values that have no match in HCSC, links that cannot be made, folders left out by design); and what is not migrated by decision.

## If the network breaks

Every command waits for a lost connection instead of failing items. It pauses, checks whether the instance answers (first after 30 s, then 1 min, 2 min, 4 min, 8 min, then every 10 min) and continues by itself. With the defaults it gives up after 10 checks (about 1 h 5 min).

| Setting | Flag | `.env` | Default |
|---|---|---|---|
| Number of checks before stopping (`0` = stop at once) | `--network-retries N` | `QMIG_NETWORK_RETRIES` | 10 |
| Pause before the first check | `--network-retry-interval 30s` | `QMIG_NETWORK_RETRY_INTERVAL` | 30s |
| Longest pause | `--network-retry-max-interval 10m` | `QMIG_NETWORK_RETRY_MAX_INTERVAL` | 10m |

For an outage you expect to last hours, raise the count (for example `--network-retries 100`).

When the checks are used up, the command stops with exit code 75 and `INTERRUPTED`, saves everything done so far and prints the command to run again. **Run exactly that command (the same plan) when the network is back**; it continues where it stopped. For `apply`, the run is marked `interrupted` and the run folder has `interrupted.json`.

If the connection was cut while something was being created (folder, test case, release, build, cycle, suite, run, log, or a file of a test case, step, run or log), the next run first looks for it in the target before creating it: by name in the same folder, release or cycle; logs by start time; files by name on their object. If there is exactly one match it is taken over and carries on like any other (files, approval, children); if there is none it is created; if there are several, or the target cannot be searched, it is not guessed: that item is reported as failed with the reason (and the ids), remove the extra object in the HCSC UI and apply the same plan again. Approving a test case and setting a release status are simply repeated, they are harmless to repeat.

Use the same plan for the second run (the command printed when the run stopped). A run that was stopped by the network shows as `interrupted` in `output/runs/index.csv`, and the `status.md` of its project pair has an "Interrupted: run again to continue" section with the command to run. It disappears once the run has been repeated. The same applies to `qmig links apply` and `qmig fields apply`: after a lost reply they first look at what the target has now (links already there, field already created) and only send what is still missing.

## If something fails

- Failed test cases are listed in the apply `report.md` with the HTTP error. Fix the cause and run `apply` with the same plan again.
- If a command errors on an API call, send the `run.log` from that run folder. This is the first run against the real API, so endpoint differences are possible.
- TLS errors on macOS: run `Install Certificates.command` for python.org builds, or `pip install certifi`.

## Items deleted in HCSC after they were migrated

Deleting migrated folders or test cases in HCSC is safe. Run `plan` and `apply` again: `plan` checks the target, finds the migrated items that no longer exist and creates them again (attachments and approvals are redone too). If you run `links apply` before that, links to a deleted test case fail with HTTP 404 and the run ends with a hint to run `plan` and `apply` again first. `verify` and `batch verify` print the same hint when test cases are missing. No extra requests are made for these hints.
