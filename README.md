# qTest Consolidation

Instance-to-instance qTest migration (currently **HS -> HCSC**): folders and test cases.
`qmig` runs the same way on **Windows and macOS**, needs **only Python 3.10+** (standard library,
nothing to `pip install`), and keeps all state **locally** (one SQLite file, no server).

## Quick start

```bash
python -m qmig setup      # Windows: py -m qmig setup
```

`setup` creates the local folders and database. Credentials go in a git-ignored `.env` in the repo root (copy `.env.example`):

```bash
cp .env.example .env      # Windows: copy .env.example .env
```

```
QMIG_HS_URL=https://healthspring.qtestnet.com
QMIG_HS_TOKEN=...
QMIG_HCSC_URL=https://hcsctest.qtestnet.com
QMIG_HCSC_TOKEN=...
```

Real environment variables win over `.env`. `.env` is git-ignored. Never commit tokens or paste them into chat or tickets.

```bash
python -m qmig doctor                      # checks Python, files, credentials and API access
python -m qmig preflight --source-project 25746 --target-project 25022   # compare fields, read-only
python -m qmig plan      --source-project 25746 --target-project 25022 --limit 5   # DRY RUN, writes nothing
python -m qmig apply     --plan <run id printed by plan>                  # creates folders + test cases
python -m qmig verify    --run  <run id printed by apply>                 # confirms they exist in the target
python -m qmig runs                                                       # history
```

Useful `plan` options: `--module-id N` (migrate one source folder and its sub-folders),
`--target-parent-id N` (create under an existing target folder), `--limit N` (first trial with a few test cases),
`--skip-fields "Assigned To,..."`. Instance names default to `--source hs --target hcsc`.

## Safety

* `plan` and `preflight` cannot write: the target client is read-only, any non-GET request raises an error.
* The source instance is never written to by any command.
* `apply` replays exactly the reviewed plan, asks you to type the target project id (or needs `--yes`),
  and is idempotent: created folders/test cases are recorded in `data/qmig.sqlite` and skipped on re-run.
* Test cases that already exist (same name) in a target folder are skipped, never duplicated.
* Tokens are never written to logs; redirects are refused so the token cannot leave the host.

## Output of every run (`output/runs/<source>-<id>__<target>-<id>/<run id>/`)

Runs are grouped by project pair (e.g. `hs-25804__hcsc-26256`); `output/runs/index.csv` lists every run and each pair folder has a `status.md`. `plan --label <name>` adds a name to the run id.

| File | Content |
| --- | --- |
| `report.md` | Summary, issues, failures, API usage, what is **not** covered, next command |
| `run.log` | Full log incl. every API call with status and duration |
| `events.jsonl` | One line per action (folder/test case created, skipped, failed ...) |
| `folders.csv`, `testcases.csv` | Row per item with status, target IDs, warnings, errors (opens in Excel) |
| `plan.json`, `testcases.jsonl` | The plan and exact payloads (plan runs) |
| `result.json` / `verify.json` | Outcome of apply / verify runs |

Exit codes: `0` ok, `1` failures (e.g. failed test cases, verify mismatch), `2` configuration/usage error.

## Not covered yet

Comments, version history, parameters, called test cases, defects and user assignment. Requirements are not
created by `qmig` (only the links to them are migrated). See `docs/scope/MIGRATION_SCOPE.md` for the full scope.

## Development

```bash
python -m unittest discover -s tests/qmig -t .
```
