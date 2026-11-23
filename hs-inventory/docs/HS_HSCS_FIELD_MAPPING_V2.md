# HS vs HSCS Field Differences (V2)

Compared 2026-11-02, GET only: HS project 25804 (Chameleons Team) vs HSCS project 26256. Fields are matched by **label** (IDs differ per instance); a field is the same only if label, type and dropdown options all match. Only differences are listed here; every other field of requirements, test cases, test suites, test runs, releases, builds, test steps and defects is identical in both. Test cycles, modules and test logs have no fields on either side.

## 1. Custom fields: HS data that cannot be migrated until HSCS is changed

| Object | Field | Type | Options | HSCS status | Decision |
|---|---|---|---|---|---|
| test cases | Test Phase | Number (dropdown) | 14 in HS | Exists, different options | Ignored: nothing is planned |
| test cases | Test Scenario Id | String | none | Missing | **Create in HSCS**: needed by the Automation Team for all projects |
| test cases | Team Specific | LongText | none | Missing | **Open**: Srini asks on the QE DSU (Tuesday) |
| test cases | Test Scenario | LongText | none | Missing | **Open**: Srini asks on the QE DSU (Tuesday) |
| test cases | Eligible to Automate | Number (dropdown) | `Y`, `N` | Missing | Not created; the value, if any, goes into the test case description as migration metadata |
| test cases | Regression Flag | Number (dropdown) | `Y`, `N` | Missing | Not created; the value, if any, goes into the test case description as migration metadata |
| test cases | Estimated Time | Number | none | Missing | Not created; the value, if any, goes into the test case description as migration metadata |
| test cases | Hierarchy | String | none | Missing | Not created; the value, if any, goes into the test case description as migration metadata |
| test cases | Zephyr Test Case ID | String | none | Missing | Not created; the value, if any, goes into the test case description as migration metadata |
| test cases | Comments (Archive) | String | none | Missing | Not created; the value, if any, goes into the test case description as migration metadata |

`qmig fields plan` follows these decisions by default (creates only Test Scenario Id). The upload adds the non-empty values of the six "not created" fields to the test case description as `(Previously) "<field>" : "<value>"`; empty values are ignored.

**Test project 26256:** all 9 missing fields were created there as a test of `qmig fields apply`. The six not-created ones can only be hidden or removed in the qTest UI. The options of Eligible to Automate and Regression Flag were created inactive.

Test Phase options in HS (for reference): `_NULL_`, Epic Testing, Feature Testing, Post Implementation Verification Testing, Production Readiness Testing, Regression Testing, Release Testing, System Testing, Unit Testing, User Acceptance Testing, Business Capability Testing, New Value, Cross Product Testing, User Story Testing.

Dropdown options must be copied exactly (same labels), or values will not map.

Not listed because they do not block HS to HSCS migration: extra HSCS options (test case Type, test run Status) and fields that exist only in HSCS (extra test run, release and test case fields); nothing in HS fills them and they are not required. Exception: test case `BCML3` and `BTC` (String) are sent by the upload from the input sheet.

## 2. Lists that differ (per-instance data, compared on label and type only)

Label and type match; the option lists are people, projects, modules or releases.

| Field | Objects | HS | HSCS | Effect on migration |
|---|---|---|---|---|
| Assigned To, Submitter | requirements, test cases, test suites, test runs, defects (Submitter: defects) | 34 users | 27 users | 30 HS users are not in HSCS (23 HSCS users are not in HS). Items assigned to them cannot keep the same person. |
| Projects Shared to | test cases | 67 projects | 4 projects | Matters only if test cases are shared across projects |
| Target / Affected / Fixed Release/Build | test suites, test runs, defects | 1 entry (`2026`) | 846 entries | `2026` is missing in HSCS. HSCS has duplicate names (Oct Activations, TBD, Undefined, Unscheduled, test123, test18), so name matching is ambiguous. |
| Module | defects | 220 | 572 | None of the 220 HS modules exists under the same name |

## 3. Jira data

The Jira key is not a field. It is in a separate `external_properties` object (`{id, link, unique_id}`), returned only with `includeExternalProperties=true`.

- HS requirement 11785062: `POD-233`, link `jira.healthspring-jira-prod.aws.zilverton.com`. HSCS requirements: e.g. `SCRUM-1002`, link `jira.test.fyiblue.com`. The two instances use different Jira servers.
- In HS the key is only in `external_properties` and at the start of the requirement name; HSCS has a requirement field `Jira Id` (String, will exist in all projects) that no HS field fills.
- Test cases have no `external_properties` in either project (20 sampled each). No defects exist in either project, so defects were not checked.
- Re-linking test cases to requirements by Jira key is done by `python -m qmig links` (see `docs/execution/UPLOAD_ONE_PROJECT_HS_TO_HCSC.md`).
