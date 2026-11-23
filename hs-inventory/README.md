# HS project and entity inventory

Read-only export from `https://healthspring.qtestnet.com/api/v3`.
Uses `QMIG_HS_URL` and `QMIG_HS_TOKEN` from the environment or from a local,
git-ignored `.env` (in this folder or the parent repo root; copy `.env.example`
there). Environment variables win over `.env`. It never uses the HCSC credentials.

From the `hs-inventory` directory, using Python 3.10+ (standard library only, no
dependencies):

```sh
python3 -m hs_inventory.export_inventory --all-projects
```

Optional arguments:

- `--output PATH`: new output directory (existing directories are not overwritten).
- `--page-size 200`: requested page size, 1-999.
- `--all-projects`: request `assigned=false`, which requires an admin account.
  Omit this option to request only projects accessible to the token. Neither mode bypasses
  requirement permissions. A forbidden project fails the export explicitly.

## Reusable output

Each run creates a timestamped directory under
`output/generated/hs_inventory/` (ignored by Git):

| File | Contents |
| --- | --- |
| `projects.json` / `projects.csv` | Project ID, name, description, status ID, dates, sample/automation flags, UUID, requirement count |
| `requirements.json` / `requirements.csv` | Project ID/name, requirement ID/PID/name, parent module, dates, web URL, Jira key/type/summary and their sources, review flag |
| `raw/projects.json` | Original project API responses, including additional fields |
| `raw/requirements_<project_id>.json` | Original requirements including properties and external integration metadata |
| `manifest.json` | Schema version, scope, timestamps, counts, extraction coverage, completion status |

Use JSON as the source for subsequent scripts; CSV is for review. The stable
requirement identity is `(project_id, requirement_id)`; `requirement_pid` is the
qTest display ID, **not** the Jira issue key. Project status is preserved as an
ID rather than assigning undocumented status labels. Empty projects are retained.

## Project size classification

The generated project inventory classifies workload using the combined count of
requirements, modules, test cases, test steps, test cycles, test suites, test
runs, and releases: XS = 0, S = 1-5,000, M = 5,001-25,000, and L = over
25,000. Test steps count as entities because they are part of the migration
workload. These bands describe qTest data volume, not company or team size.

Requirement pages request `includeExternalProperties=true` and omit `parentId`,
so they include all modules. Pagination checks total headers when available and
otherwise continues to an empty page, even if the server caps page sizes.
Duplicate IDs, changed totals, malformed responses and HTTP failures fail loudly.
Only a manifest with `status: "complete"` is safe for downstream use.
This is a live read, not a transactional snapshot; avoid concurrent source edits.

## Jira extraction

- Prefer explicit Jira key fields and qTest's `external_properties.id` / `link`.
  Fall back to Jira-shaped keys in the requirement name.
- Prefer named `Issue Type` / `Jira Issue Type` metadata. A qTest `Type` such as
  `Functional` is **not** treated as Jira's issue type.
- Recognize explicit leading type labels such as `User Story: ABC-123 Title`
  as a **name-based inference**, identified by `issue_type_source: "name_prefix"`.
  Do not classify an arbitrary title containing the word "story".
- Preserve the original full requirement name. `issue_summary` is populated only
  from explicit summary metadata, not by silently rewriting the requirement title.
- Unknown or conflicting keys/types stay null; JSON includes candidate values
  and provenance, and `needs_review` marks missing key/type values.
- If Jira issue types are absent from qTest, resolving them authoritatively requires
  a separate, authorized Jira lookup. This script does not send requests to Jira.

Exports may contain internal business data. Keep them local and out of Git.
Tokens are not written to output or passed on the command line. TLS verification
remains enabled; redirects are refused.

## Tests

```sh
python3 -m unittest discover -s tests -p test_hs_inventory.py -v
```
