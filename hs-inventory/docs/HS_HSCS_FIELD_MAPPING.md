# HS vs HSCS Field Mapping

Compared 2026-10-29 via GET only: HSCS sample project 25022 vs HS projects 25746, 25780, 25804, 25763, 25739. Fields are the same only if label, type and dropdown options all match. *Dynamic* = options are people, modules or releases, so only label and type are compared.

**Result:** 69 same, 11 missing in HSCS, 5 different, 4 only in HSCS.


## requirements

| Field | Kind | HS type | HSCS type | Options | Verdict | Detail |
|---|---|---|---|---|---|---|
| Assigned To | System | ArrayNumber | ArrayNumber | dynamic | ✅ Same |  |
| Description | System | LongText | LongText | none | ✅ Same |  |
| Frequency | System | String | String | none | ✅ Same |  |
| Impact | System | String | String | none | ✅ Same |  |
| Jira Id | Custom | — | String | — | ➕ Only in HSCS |  |
| Priority | System | Number | Number | 3 | ✅ Same |  |
| Risk Weight | System | String | String | none | ✅ Same |  |
| Status | System | Number | Number | 4 | ✅ Same |  |
| Type | System | Number | Number | 2 | ✅ Same |  |
| Vera Approval Route | System | String | String | none | ✅ Same |  |
| Vera Approval Status | System | String | String | none | ✅ Same |  |
| Vera Id | System | String | String | none | ✅ Same |  |
| Vera Pending Tasks | System | String | String | none | ✅ Same |  |
| Vera Rejection Reason | System | String | String | none | ✅ Same |  |
| Vera Revision | System | String | String | none | ✅ Same |  |

## test-cases

| Field | Kind | HS type | HSCS type | Options | Verdict | Detail |
|---|---|---|---|---|---|---|
| Assigned To | System | ArrayNumber | ArrayNumber | dynamic | ✅ Same |  |
| Automation | System | Number | Number | 4 | ✅ Same |  |
| Automation Content | System | LongText | LongText | none | ✅ Same |  |
| Comments (Archive) | Custom | String | — | none | ❌ Missing in HSCS |  |
| Description | System | LongText | LongText | none | ✅ Same |  |
| Eligible to Automate | Custom | Number | — | 2 | ❌ Missing in HSCS |  |
| Estimated Time | Custom | Number | — | none | ❌ Missing in HSCS |  |
| Hierarchy | Custom | String | — | none | ❌ Missing in HSCS |  |
| Precondition | System | LongText | LongText | none | ✅ Same |  |
| Priority | System | Number | Number | 5 | ✅ Same |  |
| Projects Shared to | System | ArrayNumber/String | String | none | ⚠️ Different | type HS ArrayNumber/String vs HSCS String; options only in HS: Agile Aces, Archons Team, Avengers Fulfillment Team, BT - Automation Team, BT - Billing and Reconciliation Team, BT - Broker Team, BT - Claims Team, BT - DAR Team, BT - Digital Availity Team, BT - Enrollment Team, BT - Fulfillment Team, BT - Gateway Team, BT - Marketing Team, BT - Member Digital, BT - Migration Team, BT - Provider Network Team, BT - QNXT Team, BT - Service Team, Billing Squad, Brewers, Bug Slayers Fulfillment Team, Chameleons Team, Claim Classyx Quality Engineering Team, Compliance QE Team, DMOM Member Integration Test, Data And Integration GMR QE Team, Data And Integration Gateway QE Team, Data And Integration USMIS QE Team, Digital Underground Team, E2E HealthSpring QE Testing, E2EPhoenix, FEE-Bulous, FEE-Nix Team, FEE-Nom Team, Fulfillment: Fix Force Team (CFETM), GBS SPANP Avengers, Group A Team, HSB QE Team, KM - Hawking Team, MA Diverstiture Provider Domain, Magic Scepter Team, Marketing QE Team, Medicare Data Purge, New Billing Team, Nomadics, OnBase Team, PRV Data Migration Team, PRV New Integration Team, PRV OSS Integration Team, PRV Ribbon PDQ Team, PRV Symplr SOT Team, Pharmacy QE Team, Product QE Team, Provider Services, Provider Transformers, RA ART Quality Management Team, SER Service Integration Team, SalesBroker QE Team, Scrum Knights Team, Scrum Warriors, Scrumglasses at Night Team, Sparkling Stars System Team, Sprint Sharks, Storm Riders, Task Titans, WhizQEWonders, qConnect - Sample Project |
| Regression Flag | Custom | Number | — | 2 | ❌ Missing in HSCS |  |
| Shared | System | Boolean/String | String | none | ⚠️ Different | type HS Boolean/String vs HSCS String |
| Status | System | Number | Number | 4 | ⚠️ Different | options only in HS: Not Applicable |
| Team Specific | Custom | LongText | — | none | ❌ Missing in HSCS |  |
| Test Phase | Custom | Number | — | 14 | ❌ Missing in HSCS |  |
| Test Scenario | Custom | LongText | — | none | ❌ Missing in HSCS |  |
| Test Scenario Id | Custom | String | — | none | ❌ Missing in HSCS |  |
| Type | System | Number | Number | 4 | ✅ Same |  |
| Zephyr Test Case ID | Custom | String | — | none | ❌ Missing in HSCS |  |

## test-suites

| Field | Kind | HS type | HSCS type | Options | Verdict | Detail |
|---|---|---|---|---|---|---|
| Assigned To | System | ArrayNumber | ArrayNumber | dynamic | ✅ Same |  |
| Description | System | LongText | LongText | none | ✅ Same |  |
| Environment | System | Number | Number | none | ✅ Same |  |
| Execution Type | System | Number | Number | 7 | ✅ Same |  |
| Planned End Date | System | Date | Date | none | ✅ Same |  |
| Planned Start Date | System | Date | Date | none | ✅ Same |  |
| Target Release/Build | System | Number | Number | dynamic | ✅ Same |  |

## test-cycles

| Field | Kind | HS type | HSCS type | Options | Verdict | Detail |
|---|---|---|---|---|---|---|

## test-runs

| Field | Kind | HS type | HSCS type | Options | Verdict | Detail |
|---|---|---|---|---|---|---|
| Actual Time | System | String | Number | none | ⚠️ Different | type HS String vs HSCS Number |
| Assigned To | System | Number | Number | dynamic | ✅ Same |  |
| Average Execution Time | System | — | String | — | ➕ Only in HSCS |  |
| Current Test Configuration | System | String | String | none | ✅ Same |  |
| Environment | System | Number | Number | none | ✅ Same |  |
| Executed with Test Configuration | System | String | String | none | ✅ Same |  |
| Execution Type | System | Number | Number | 7 | ✅ Same |  |
| Execution User | Custom | String | — | none | ❌ Missing in HSCS |  |
| Planned End Date | System | Date | Date | none | ✅ Same |  |
| Planned Start Date | System | Date | Date | none | ✅ Same |  |
| Planned Time | System | String | Number | none | ⚠️ Different | type HS String vs HSCS Number |
| Priority | System | Number | Number | 5 | ✅ Same |  |
| Run Order | System | Number | Number | none | ✅ Same |  |
| Status | System | Number | Number | 5 | ✅ Same |  |
| Target Release/Build | System | Number | Number | dynamic | ✅ Same |  |
| Total Actual Time | System | — | String | — | ➕ Only in HSCS |  |
| Total Planned Time | System | — | String | — | ➕ Only in HSCS |  |

## releases

| Field | Kind | HS type | HSCS type | Options | Verdict | Detail |
|---|---|---|---|---|---|---|
| Description | System | LongText | LongText | none | ✅ Same |  |
| End Date | System | Date | Date | none | ✅ Same |  |
| Release Note | System | LongText | LongText | none | ✅ Same |  |
| Start Date | System | Date | Date | none | ✅ Same |  |
| Status | System | Number | Number | 4 | ✅ Same |  |

## defects

| Field | Kind | HS type | HSCS type | Options | Verdict | Detail |
|---|---|---|---|---|---|---|
| Affected Release/Build | System | Number | Number | dynamic | ✅ Same |  |
| Assigned To | System | Number | Number | dynamic | ✅ Same |  |
| Category | System | Number | Number | 17 | ✅ Same |  |
| Closed Date | System | DateTime | DateTime | none | ✅ Same |  |
| Description | System | LongText | LongText | none | ✅ Same |  |
| Environment | System | Number | Number | none | ✅ Same |  |
| Fixed Release/Build | System | Number | Number | dynamic | ✅ Same |  |
| Module | System | Number | Number | dynamic | ✅ Same |  |
| Priority | System | Number | Number | 5 | ✅ Same |  |
| Reason | System | Number | Number | 17 | ✅ Same |  |
| Root Cause | System | Number | Number | 13 | ✅ Same |  |
| Severity | System | Number | Number | 5 | ✅ Same |  |
| Status | System | Number | Number | 6 | ✅ Same |  |
| Submitted Date | System | DateTime | DateTime | none | ✅ Same |  |
| Submitter | System | String | String | dynamic | ✅ Same |  |
| Summary | System | String | String | none | ✅ Same |  |
| Target Date | System | Date | Date | none | ✅ Same |  |
| Target Release/Build | System | Number | Number | dynamic | ✅ Same |  |
| Type | System | Number | Number | 4 | ✅ Same |  |
| Vera Approval Route | System | String | String | none | ✅ Same |  |
| Vera Approval Status | System | String | String | none | ✅ Same |  |
| Vera Id | System | String | String | none | ✅ Same |  |
| Vera Pending Tasks | System | String | String | none | ✅ Same |  |
| Vera Rejection Reason | System | String | String | none | ✅ Same |  |
| Vera Revision | System | String | String | none | ✅ Same |  |

## Notes

- **Status (test cases):** HS has an extra option "Not Applicable" (all 5 HS projects); HSCS has 4 options. Add it to HSCS.
- **Shared / Projects Shared to:** type follows the project sharing setting (Boolean/ArrayNumber when on, String when off), not a config mismatch.
- **Planned / Actual Time (test runs):** String in HS, Number in HSCS; values must be numeric.
- **Only in HSCS:** Jira Id is custom; the three run time totals are filled by qTest.
- **Missing custom fields:** Test Phase (14 options), Eligible to Automate (N, Y) and Regression Flag (N, Y) are dropdowns; options must be copied exactly when creating them in HSCS.

## Upload coverage

The upload (`CUSTOM_FIELDS`, `scripts/upload_tc/upload_testcases_threaded.py:116`) sends only Status, Type, Priority, Test Phase, Assigned To, Shared, BCML3, BTC. Fields filled in 20/20 sampled HS test cases (Member Digital) but not sent:

| Field | Sent by upload |
|---|---|
| Eligible to Automate | No |
| Estimated Time | No |
| Zephyr Test Case ID | No |
| Hierarchy | No |

Regression Flag, Team Specific, Test Scenario, Test Scenario Id and Comments (Archive) were empty in that sample and are also not sent.
