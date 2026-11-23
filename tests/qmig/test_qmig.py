import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from qmig import cli
from qmig.runindex import find_run_dir
from qmig.client import ApiError, QtestClient, WriteBlocked
from qmig.config import ConfigError, Instance, normalize_token, normalize_url
from qmig.fields import FieldMapper

from .fakeqtest import FakeQtest, set_creds

SRC_FIELDS = [
    {"id": 100, "label": "Status", "required": True,
     "allowed_values": [{"value": 601, "label": "New", "is_default": True}, {"value": 602, "label": "Done"},
                        {"value": 603, "label": "Legacy"}]},
    {"id": 101, "label": "Assigned To", "required": False, "allowed_values": []},
    {"id": 102, "label": "Team", "required": False, "allowed_values": []},
]
TGT_FIELDS = [
    {"id": 200, "label": "Status", "required": True,
     "allowed_values": [{"value": 701, "label": "New", "is_default": True}, {"value": 703, "label": "Done"}]},
    {"id": 201, "label": "Assigned To", "required": False, "allowed_values": []},
    {"id": 202, "label": "Team", "required": False, "allowed_values": []},
]


def tc_props(status_id, status_name, team="Core"):
    return [{"field_id": 100, "field_name": "Status", "field_value": str(status_id), "field_value_name": status_name},
            {"field_id": 101, "field_name": "Assigned To", "field_value": "77", "field_value_name": "someone"},
            {"field_id": 102, "field_name": "Team", "field_value": team, "field_value_name": team}]


class EndToEndTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.src = FakeQtest("src-token")
        self.tgt = FakeQtest("tgt-token")
        set_creds(self, self.src, self.tgt, hs_token="Bearer src-token", hcsc_token="tgt-token")
        patcher = patch.dict(os.environ, {"QMIG_HOME": str(self.root), "QMIG_ALLOW_HTTP": "1"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.src.close)
        self.addCleanup(self.tgt.close)

        self.src.add_project(1, "HS Project", SRC_FIELDS)
        a = self.src.add_module(1, "A", None, mid=10)
        a1 = self.src.add_module(1, "A1", a, mid=11)
        self.src.add_module(1, "B", None, mid=12)
        steps = [{"order": 2, "description": "second", "expected": "ok"}, {"order": 1, "description": "first", "expected": ""},
                 {"order": 3, "description": "", "expected": ""}]
        self.src.add_testcase(1, a, "tc one", test_steps=steps, properties=tc_props(601, "New"))
        self.src.add_testcase(1, a, "tc two", test_steps=steps, properties=tc_props(603, "Legacy"))
        self.src.add_testcase(1, a1, "tc three", test_steps=steps[:1], properties=tc_props(602, "Done"))

        self.tgt.add_project(2, "HCSC Project", TGT_FIELDS)
        ta = self.tgt.add_module(2, "A", None, mid=50)
        self.tgt.add_testcase(2, ta, "TC One")  # same name (case-insensitive) as 'tc one'

    def run_cli(self, *argv) -> int:
        with patch("sys.stdout"):
            return cli.main(list(argv))

    def plan(self, *extra) -> str:
        code = self.run_cli("plan", "--source-project", "1", "--target-project", "2", *extra)
        self.assertEqual(code, 0)
        return sorted(p.name for p in (self.root / "output" / "runs").glob("*/*") if p.name.endswith("-plan"))[-1]

    def test_plan_is_read_only_and_reports(self):
        run_id = self.plan()
        self.assertEqual(self.tgt.writes(), [])
        self.assertEqual(self.src.writes(), [])
        run_dir = find_run_dir(self.root / "output" / "runs", run_id)
        self.assertEqual(run_dir.parent.name, "hs-1__hcsc-2")  # runs are grouped by project pair
        for name in ("plan.json", "run.json", "testcases.jsonl", "report.md", "folders.csv", "testcases.csv", "events.jsonl", "run.log"):
            self.assertTrue((run_dir / name).is_file(), name)
        plan = json.loads((run_dir / "plan.json").read_text())
        self.assertEqual(plan["counts"]["folders"], {"reuse": 1, "create": 2})
        self.assertEqual(plan["counts"]["testcases"], {"skip-exists": 1, "create": 2})
        warnings = " ".join(i["message"] for i in plan["issues"])
        self.assertIn("'Legacy'", warnings)
        log = (run_dir / "run.log").read_text()
        self.assertNotIn("src-token", log)
        self.assertNotIn("tgt-token", log)
        self.assertIn("DRY RUN", (run_dir / "report.md").read_text().replace("dry-run", "DRY RUN").upper())

    def test_apply_refuses_a_target_url_that_differs_from_the_reviewed_plan(self):
        run_id = self.plan()
        plan_file = find_run_dir(self.root / "output" / "runs", run_id) / "plan.json"
        plan = json.loads(plan_file.read_text())
        plan["target"]["url"] = "https://some-other-server.example"
        plan_file.write_text(json.dumps(plan))
        self.assertNotEqual(self.run_cli("apply", "--plan", run_id, "--yes"), 0)
        self.assertEqual(self.tgt.writes(), [])

    def test_a_file_mapping_only_counts_for_the_target_object_it_was_made_for(self):
        from qmig.db import StateDb
        db = StateDb(self.root / "x.sqlite")
        db.put_attachment("hs", 1, 9, "hcsc", 2, 90, "test-cases", 5, 700, "a.png", 3, "r1")
        self.assertIsNotNone(db.attachment_on("hs", 1, 9, "hcsc", 2, "test-cases", 700))
        self.assertIsNone(db.attachment_on("hs", 1, 9, "hcsc", 2, "test-cases", 701))  # test case recreated
        self.assertIsNone(db.attachment_on("hs", 1, 9, "hcsc", 2, "test-steps", 700))

    def test_apply_creates_maps_fields_and_is_idempotent(self):
        run_id = self.plan()
        self.assertEqual(self.run_cli("apply", "--plan", run_id, "--yes"), 0)
        self.assertEqual(self.src.writes(), [])
        project = self.tgt.projects[2]
        names = sorted(m["name"] for m in project["modules"].values())
        self.assertEqual(names, ["A", "A1", "B"])
        a1 = next(m for m in project["modules"].values() if m["name"] == "A1")
        self.assertEqual(a1["parent_id"], 50)
        created = {t["name"]: t for t in project["testcases"].values()}
        self.assertEqual(set(created), {"TC One", "tc two", "tc three"})
        two = created["tc two"]
        self.assertEqual([s["order"] for s in two["test_steps"]], [1, 2])  # sorted, renumbered, empty step dropped
        self.assertEqual(two["test_steps"][0]["description"], "first")
        status = next(p for p in two["properties"] if p["field_id"] == 200)
        self.assertEqual(status["field_value"], "701")  # 'Legacy' missing in target -> required default 'New'
        self.assertFalse(any(p["field_id"] == 201 for p in two["properties"]))  # Assigned To skipped
        team = next(p for p in two["properties"] if p["field_id"] == 202)
        self.assertEqual(team["field_value"], "Core")
        three = created["tc three"]
        self.assertEqual(next(p for p in three["properties"] if p["field_id"] == 200)["field_value"], "703")

        # second apply of the same plan must not create anything new
        before = len(project["testcases"]), len(project["modules"])
        self.assertEqual(self.run_cli("apply", "--plan", run_id, "--yes"), 0)
        self.assertEqual((len(project["testcases"]), len(project["modules"])), before)

        # a fresh plan now sees everything as already migrated
        plan_id = self.plan()
        plan = json.loads((find_run_dir(self.root / "output" / "runs", plan_id) / "plan.json").read_text())
        self.assertNotIn("create", plan["counts"]["testcases"])

    def folder_paths(self, project):
        mods = project["modules"]

        def path(m):
            return (path(mods[m["parent_id"]]) + "/" if m["parent_id"] else "") + m["name"]
        return sorted(path(m) for m in mods.values())

    def test_single_folder_keeps_its_parents(self):
        self.src.add_module(1, "A1x", 11, mid=13)  # A / A1 / A1x
        self.src.add_testcase(1, 13, "deep case")
        run_id = self.plan("--module-id", "11")  # A1 sits under A
        plan = json.loads((find_run_dir(self.root / "output" / "runs", run_id) / "plan.json").read_text())
        context = {f["path"]: f["context_only"] for f in plan["folders"]}
        self.assertEqual(context, {"A": True, "A / A1": False, "A / A1 / A1x": False})
        self.assertEqual({r["name"] for r in plan["testcases"]}, {"tc three", "deep case"})  # not the parent's cases
        self.assertEqual(self.run_cli("apply", "--plan", run_id, "--yes"), 0)
        self.assertEqual(self.folder_paths(self.tgt.projects[2]), ["A", "A/A1", "A/A1/A1x"])  # A reused (id 50)
        a1 = next(m for m in self.tgt.projects[2]["modules"].values() if m["name"] == "A1")
        self.assertEqual(a1["parent_id"], 50)

    def test_very_deep_folder_tree_keeps_every_level(self):
        parent = self.src.add_module(1, "L1", None, mid=200)
        for level in range(2, 26):  # 25 levels deep
            parent = self.src.add_module(1, f"L{level}", parent, mid=199 + level)
        self.src.add_testcase(1, parent, "deepest case")
        run_id = self.plan("--module-id", "224")  # the deepest folder: 24 ancestors are rebuilt above it
        self.assertEqual(self.run_cli("apply", "--plan", run_id, "--yes"), 0)
        mods = self.tgt.projects[2]["modules"]
        deepest = next(m for m in mods.values() if m["name"] == "L25")
        chain, cursor = [], deepest
        while cursor:
            chain.append(cursor["name"])
            cursor = mods.get(cursor["parent_id"])
        self.assertEqual(chain[::-1], [f"L{i}" for i in range(1, 26)])
        self.assertEqual([t["name"] for t in self.tgt.projects[2]["testcases"].values()
                          if t["parent_id"] == deepest["id"]], ["deepest case"])

    def test_recycle_bin_folders_are_never_migrated(self):
        bin_ = self.src.add_module(1, "Recycle Bin", None, mid=900)
        old = self.src.add_module(1, "zzz old", bin_, mid=901)
        self.src.add_module(1, "deeper", old, mid=902)
        nested = self.src.add_module(1, "recycle bin ", 10, mid=903)  # any level, any case
        self.src.add_testcase(1, bin_, "deleted one")
        self.src.add_testcase(1, nested, "deleted two")
        run_id = self.plan()
        plan = json.loads((find_run_dir(self.root / "output" / "runs", run_id) / "plan.json").read_text())
        self.assertEqual(plan["scope"]["excluded_folders"], 4)
        self.assertEqual(sorted(f["name"] for f in plan["folders"]), ["A", "A1", "B"])
        self.assertEqual(sorted(r["name"] for r in plan["testcases"]), ["tc one", "tc three", "tc two"])
        self.assertEqual(self.run_cli("apply", "--plan", run_id, "--yes"), 0)
        names = {m["name"] for m in self.tgt.projects[2]["modules"].values()}
        self.assertFalse({"Recycle Bin", "zzz old", "deeper", "recycle bin "} & names)
        self.assertEqual(self.run_cli("plan", "--source-project", "1", "--target-project", "2", "--module-id", "902"), 2)

    def _approved_source(self):
        self.src.projects[1]["testcases"][next(t["id"] for t in self.src.projects[1]["testcases"].values()
                                              if t["name"] == "tc two")]["version"] = "1.0"  # approved in HS

    def test_approve_source_policy_approves_only_what_is_approved_in_the_source(self):
        self._approved_source()
        run_id = self.plan("--approve")
        plan = json.loads((find_run_dir(self.root / "output" / "runs", run_id) / "plan.json").read_text())
        self.assertEqual(plan["counts"]["approvals"], {"to-approve": 1, "left-as-draft": 1})
        self.assertEqual(self.tgt.writes(), [])
        self.assertEqual(self.run_cli("apply", "--plan", run_id, "--yes"), 0)
        versions = {t["name"]: t["version"] for t in self.tgt.projects[2]["testcases"].values()}
        self.assertEqual((versions["tc two"], versions["tc three"], versions["TC One"]), ("1.0", "0.1", "0.1"))  # TC One existed: untouched
        self.assertEqual(self.run_cli("verify", "--run", self._run_ids("-apply")[-1]), 0)
        puts = [r for r in self.tgt.writes() if r[0] == "PUT"]
        self.assertEqual(self.run_cli("apply", "--plan", run_id, "--yes"), 0)  # same plan: nothing approved twice
        self.assertEqual(puts, [r for r in self.tgt.writes() if r[0] == "PUT"])

    def test_approve_all_and_failures_are_reported(self):
        self.tgt.fail_approve_names = {"tc three"}
        run_id = self.plan("--approve", "all")
        self.assertEqual(self.run_cli("apply", "--plan", run_id, "--yes"), 1)
        result = json.loads((find_run_dir(self.root / "output" / "runs", self._run_ids("-apply")[-1]) / "result.json").read_text())
        self.assertEqual(result["counts"]["approvals"], {"approved": 1, "failed": 1})
        self.tgt.fail_approve_names = set()
        self.assertEqual(self.run_cli("apply", "--plan", run_id, "--yes"), 0)  # only the failed one is retried
        versions = {t["name"]: t["version"] for t in self.tgt.projects[2]["testcases"].values() if t["name"] != "TC One"}
        self.assertEqual(set(versions.values()), {"1.0"})

    def test_approval_stops_at_once_when_the_project_requires_a_linked_requirement(self):
        self.tgt.approve_needs_requirement = True
        run_id = self.plan("--approve", "all")
        self.assertEqual(self.run_cli("apply", "--plan", run_id, "--yes"), 1)
        result = json.loads((find_run_dir(self.root / "output" / "runs", self._run_ids("-apply")[-1]) / "result.json").read_text())
        self.assertEqual(result["counts"]["approvals"], {"failed": 1, "not-attempted": 1})  # one call, not one per test case
        self.assertIn("qmig links", result["aborted"])
        self.assertEqual(len([r for r in self.tgt.requests if r[0] == "PUT"]), 1)

    def test_assigned_to_is_kept_as_text_in_the_description(self):
        tcs = self.src.projects[1]["testcases"]
        for t in tcs.values():
            if t["name"] == "tc two":
                t["description"] = "<p>Existing text</p>"
                t["properties"] = [p for p in t["properties"] if p["field_name"] != "Assigned To"] + [
                    {"field_id": 101, "field_name": "Assigned To", "field_value": "[661273,5]", "field_value_name": "[Jeffrey Jandi, A & B]"}]
            if t["name"] == "tc three":
                t["properties"] = [p for p in t["properties"] if p["field_name"] != "Assigned To"]  # nobody assigned
        self.assertEqual(self.run_cli("apply", "--plan", self.plan(), "--yes"), 0)
        made = {t["name"]: t for t in self.tgt.projects[2]["testcases"].values()}
        self.assertEqual(made["tc two"]["description"], '<p>Existing text</p><br><b>Migrated metadata</b>:<br>'
                         '(Previously) "Assigned To" : "Jeffrey Jandi", "A &amp; B"')
        self.assertEqual(made["tc three"]["description"], "")  # no assignee: description untouched

    def test_assigned_to_names_split_only_when_the_count_matches_the_ids(self):
        from qmig.plan import assigned_to_names
        prop = lambda ids, names: {"properties": [{"field_name": "Assigned To", "field_value": ids, "field_value_name": names}]}
        self.assertEqual(assigned_to_names(prop("[1]", "[Jeffrey Jandi]")), ["Jeffrey Jandi"])
        self.assertEqual(assigned_to_names(prop("[1,2,3]", "[A B, C D, E F]")), ["A B", "C D", "E F"])
        self.assertEqual(assigned_to_names(prop("[1]", "[Smith, John]")), ["Smith, John"])  # one user, comma in the name
        self.assertEqual(assigned_to_names(prop("[1,2]", "[Smith, John]")), ["Smith", "John"])  # indistinguishable: split
        self.assertEqual(assigned_to_names(prop("", "")), [])
        self.assertEqual(assigned_to_names({"properties": []}), [])

    def test_objects_deleted_in_the_target_are_created_again(self):
        self._add_attachments()
        self.assertEqual(self.run_cli("apply", "--plan", self.plan("--attachments", "--approve", "all"), "--yes"), 0)
        project = self.tgt.projects[2]
        for mid in [m for m, v in project["modules"].items() if v["name"] in ("A1", "B")]:  # user deletes folders in the UI
            del project["modules"][mid]
        for tid in [t for t, v in project["testcases"].items() if v["name"] == "tc three"]:
            del project["testcases"][tid]
        run_id = self.plan("--attachments", "--approve", "all")
        plan = json.loads((find_run_dir(self.root / "output" / "runs", run_id) / "plan.json").read_text())
        self.assertTrue(any("migrated earlier but is no longer in the target" in i["message"] for i in plan["issues"]))
        self.assertEqual(self.run_cli("apply", "--plan", run_id, "--yes"), 0)
        again = {t["name"]: t for t in project["testcases"].values()}
        self.assertEqual(sorted(a["name"] for a in again["tc three"]["attachments"]), ["spec.docx", "umlé.png"])  # files re-sent
        self.assertEqual(again["tc three"]["version"], "1.0")  # approved again
        self.assertEqual(len([t for t in project["testcases"].values() if t["name"] == "tc three"]), 1)
        self.assertEqual(sorted(m["name"] for m in project["modules"].values()), ["A", "A1", "B"])
        self.assertEqual(self.run_cli("verify", "--run", self._run_ids("-apply")[-1]), 0)

    def test_objects_moved_away_in_the_target_are_created_again(self):
        self.assertEqual(self.run_cli("apply", "--plan", self.plan("--approve", "all"), "--yes"), 0)
        project = self.tgt.projects[2]
        bin_module = self.tgt.add_module(2, "Recycle Bin", None)  # the user cannot delete, so moves things there
        by_name = {m["name"]: mid for mid, m in project["modules"].items()}
        project["modules"][by_name["B"]]["parent_id"] = bin_module  # a migrated folder moved away
        for t in project["testcases"].values():
            if t["name"] == "tc three":
                t["parent_id"] = bin_module  # a migrated test case moved away
        plan = json.loads((find_run_dir(self.root / "output" / "runs", self.plan("--approve", "all")) / "plan.json").read_text())
        folders = {f["name"]: f["action"] for f in plan["folders"]}
        self.assertEqual(folders["B"], "create")
        self.assertEqual(folders["A1"], "reuse-mapped")
        cases = {t["name"]: t["action"] for t in plan["testcases"]}
        self.assertEqual(cases["tc three"], "create")
        self.assertEqual(cases["tc two"], "skip-mapped")

    def test_run_overview_files_and_labels(self):
        runs = self.root / "output" / "runs"
        self.assertEqual(self.run_cli("plan", "--source-project", "1", "--target-project", "2", "--label", "Pilot Run!"), 0)
        plan_id = self._run_ids("-plan-pilot-run")[-1]  # label cleaned to lowercase words
        self.assertEqual(self.run_cli("apply", "--plan", plan_id, "--yes"), 0)
        apply_id = self._run_ids("-apply-pilot-run")[-1]  # apply and verify inherit the label
        self.assertEqual(self.run_cli("verify", "--run", apply_id), 0)
        self.assertEqual(len(self._run_ids("-verify-pilot-run")), 1)
        index = (runs / "index.csv").read_text(encoding="utf-8-sig").splitlines()
        self.assertEqual(len(index), 4)  # header + plan, apply, verify
        self.assertTrue(all(",HS Project," in line and ",HCSC Project," in line for line in index[1:]))  # names on every row
        status = (runs / "hs-1__hcsc-2" / "status.md").read_text()
        self.assertIn("HS Project", status)
        self.assertIn(apply_id, status)
        self.assertEqual(find_run_dir(runs, plan_id).parent.name, "hs-1__hcsc-2")
        with self.assertRaises(FileNotFoundError):
            find_run_dir(runs, "20200101-000000-plan")
        with self.assertRaises(ConfigError):
            find_run_dir(runs, "../x")

    def test_old_flat_run_folders_are_still_found(self):
        flat = self.root / "output" / "runs" / "20250101-000000-plan"
        flat.mkdir(parents=True)
        self.assertEqual(find_run_dir(self.root / "output" / "runs", "20250101-000000-plan"), flat)

    def test_no_metadata_flag_leaves_descriptions_alone(self):
        self.assertEqual(self.run_cli("apply", "--plan", self.plan("--no-metadata"), "--yes"), 0)
        self.assertEqual({t["description"] for t in self.tgt.projects[2]["testcases"].values()}, {""})

    def test_plan_without_approve_flag_never_approves(self):
        self._approved_source()
        run_id = self.plan()
        self.assertEqual(self.run_cli("apply", "--plan", run_id, "--yes"), 0)
        self.assertFalse([r for r in self.tgt.writes() if r[0] == "PUT"])

    def test_single_folder_without_parents_goes_to_the_top(self):
        run_id = self.plan("--module-id", "11", "--without-parents")
        self.assertEqual(self.run_cli("apply", "--plan", run_id, "--yes"), 0)
        a1 = next(m for m in self.tgt.projects[2]["modules"].values() if m["name"] == "A1")
        self.assertIsNone(a1["parent_id"])

    def test_verify_detects_missing_testcase(self):
        run_id = self.plan()
        self.run_cli("apply", "--plan", run_id, "--yes")
        apply_id = sorted(p.name for p in (self.root / "output" / "runs").glob("*/*") if p.name.endswith("-apply"))[-1]
        self.assertEqual(self.run_cli("verify", "--run", apply_id), 0)
        victim = next(t for t in self.tgt.projects[2]["testcases"].values() if t["name"] == "tc three")
        del self.tgt.projects[2]["testcases"][victim["id"]]
        self.assertEqual(self.run_cli("verify", "--run", apply_id), 1)

    def test_limit_and_failure_handling(self):
        run_id = self.plan("--limit", "1")
        plan = json.loads((find_run_dir(self.root / "output" / "runs", run_id) / "plan.json").read_text())
        self.assertEqual(plan["counts"]["testcases"]["create"], 1)
        self.assertTrue(plan["scope"]["scan_stopped_by_limit"])  # folder A1 was never scanned
        self.assertEqual(len(plan["testcases"]), 2)

        full = self.plan()
        self.tgt.fail_post_for_names.add("tc two")
        self.assertEqual(self.run_cli("apply", "--plan", full, "--yes"), 1)  # failure -> non-zero exit
        apply_id = sorted(p.name for p in (self.root / "output" / "runs").glob("*/*") if p.name.endswith("-apply"))[-1]
        result = json.loads((find_run_dir(self.root / "output" / "runs", apply_id) / "result.json").read_text())
        self.assertEqual(result["counts"]["testcases"]["failed"], 1)
        self.assertEqual(result["counts"]["testcases"]["created"], 1)
        self.assertIn("HTTP 400", (find_run_dir(self.root / "output" / "runs", apply_id) / "report.md").read_text())

    def _add_attachments(self):
        cases = {t["name"]: t for t in self.src.projects[1]["testcases"].values()}
        cases["tc one"]["attachments"] = [{"id": 9004, "name": "ignored.txt", "content_type": "text/plain", "data": b"x"}]
        cases["tc two"]["attachments"] = [{"id": 9003, "name": "notes.txt", "content_type": "text/plain", "data": b"hello"}]
        cases["tc three"]["attachments"] = [
            {"id": 9001, "name": "spec.docx", "content_type": "application/msword", "data": b"\x00\x01binary\xff" * 50},
            {"id": 9002, "name": "uml\u00e9.png", "content_type": "image/png", "data": b"PNGDATA"}]

    def _run_ids(self, suffix):
        return sorted(p.name for p in (self.root / "output" / "runs").glob("*/*") if p.name.endswith(suffix))

    def test_attachments_are_copied_once_and_verified(self):
        self._add_attachments()
        run_id = self.plan("--attachments")
        plan = json.loads((find_run_dir(self.root / "output" / "runs", run_id) / "plan.json").read_text())
        self.assertEqual(plan["counts"]["attachments"], {"to-copy": 3, "already-copied": 0})
        self.assertEqual(self.tgt.writes(), [])  # planning wrote nothing
        self.assertEqual(self.run_cli("apply", "--plan", run_id, "--yes"), 0)
        self.assertEqual(self.src.writes(), [])
        created = {t["name"]: t for t in self.tgt.projects[2]["testcases"].values()}
        self.assertEqual([a["data"] for a in created["tc two"]["attachments"]], [b"hello"])
        three = {a["name"]: a for a in created["tc three"]["attachments"]}
        self.assertEqual(sorted(three), ["spec.docx", "umlé.png"])
        self.assertEqual(three["spec.docx"]["data"], b"\x00\x01binary\xff" * 50)  # binary survives intact
        self.assertEqual(three["spec.docx"]["content_type"], "application/msword")
        self.assertEqual(created["TC One"].get("attachments"), None)  # existed already: left alone
        self.assertEqual(self.run_cli("verify", "--run", self._run_ids("-apply")[-1]), 0)
        before = [len(t.get("attachments", [])) for t in self.tgt.projects[2]["testcases"].values()]
        self.assertEqual(self.run_cli("apply", "--plan", run_id, "--yes"), 0)  # same plan again
        self.assertEqual(before, [len(t.get("attachments", [])) for t in self.tgt.projects[2]["testcases"].values()])
        again = self.plan("--attachments")  # a fresh plan sees them as done
        replan = json.loads((find_run_dir(self.root / "output" / "runs", again) / "plan.json").read_text())
        self.assertEqual(replan["counts"]["attachments"]["to-copy"], 0)

    def test_attachment_failure_is_reported_and_retried_later(self):
        self._add_attachments()
        run_id = self.plan("--attachments")
        self.tgt.fail_upload_names = {"notes.txt"}
        self.assertEqual(self.run_cli("apply", "--plan", run_id, "--yes"), 1)
        apply_id = self._run_ids("-apply")[-1]
        result = json.loads((find_run_dir(self.root / "output" / "runs", apply_id) / "result.json").read_text())
        self.assertEqual(result["counts"]["attachments"], {"failed": 1, "created": 2})
        self.assertIn("notes.txt", (find_run_dir(self.root / "output" / "runs", apply_id) / "report.md").read_text())
        self.tgt.fail_upload_names = set()
        self.assertEqual(self.run_cli("apply", "--plan", run_id, "--yes"), 0)  # only the failed file is sent again
        names = sorted(a["name"] for t in self.tgt.projects[2]["testcases"].values() for a in t.get("attachments", []))
        self.assertEqual(names, ["notes.txt", "spec.docx", "umlé.png"])  # each file exactly once

    def _add_step_files(self):
        cases = {t["name"]: t for t in self.src.projects[1]["testcases"].values()}
        for case in cases.values():
            case["test_steps"] = [dict(s) for s in case["test_steps"]]  # the fixture shares its step dicts
        steps = cases["tc one"]["test_steps"]  # orders 2, 1 and 3 (empty, so it is not migrated)
        steps[1]["attachments"] = [{"id": 9101, "name": "first.png", "content_type": "image/png", "data": b"PNG\x00\x01" * 20}]
        steps[0]["attachments"] = [{"id": 9102, "name": "second.xlsx", "content_type": "application/vnd.ms-excel", "data": b"XLS"},
                                   {"id": 9103, "name": "more.txt", "content_type": "text/plain", "data": b"more"}]
        steps[2]["attachments"] = [{"id": 9104, "name": "textless.txt", "content_type": "text/plain", "data": b"lost"}]
        cases["tc three"]["test_steps"][0]["attachments"] = [{"id": 9105, "name": "three.txt", "content_type": "text/plain", "data": b"3"}]

    def test_step_files_are_copied_to_the_matching_step_once_and_verified(self):
        self._add_step_files()
        self.tgt.projects[2]["testcases"].clear()  # 'TC One' already in the target would be left alone
        run_id = self.plan("--step-files")
        plan = json.loads((find_run_dir(self.root / "output" / "runs", run_id) / "plan.json").read_text())
        self.assertEqual(plan["counts"]["step_files"], {"to-copy": 4, "already-copied": 0})
        self.assertTrue([i for i in plan["issues"] if i["area"] == "step files"])  # the textless step's file
        self.assertEqual(self.tgt.writes(), [])
        self.assertEqual(self.run_cli("apply", "--plan", run_id, "--yes"), 0)
        self.assertEqual(self.src.writes(), [])
        created = {t["name"]: t for t in self.tgt.projects[2]["testcases"].values()}
        by_order = {s["order"]: [a["name"] for a in s.get("attachments", [])] for s in created["tc one"]["test_steps"]}
        self.assertEqual(by_order, {1: ["first.png"], 2: ["second.xlsx", "more.txt"]})
        first = next(a for s in created["tc one"]["test_steps"] if s["order"] == 1 for a in s["attachments"])
        self.assertEqual(first["data"], b"PNG\x00\x01" * 20)
        self.assertEqual([a["name"] for a in created["tc three"]["test_steps"][0]["attachments"]], ["three.txt"])
        self.assertEqual(self.run_cli("verify", "--run", self._run_ids("-apply")[-1]), 0)
        self.assertEqual(self.run_cli("apply", "--plan", run_id, "--yes"), 0)  # same plan again: nothing is sent twice
        total = sum(len(s.get("attachments", [])) for t in self.tgt.projects[2]["testcases"].values() for s in t["test_steps"])
        self.assertEqual(total, 4)
        again = json.loads((find_run_dir(self.root / "output" / "runs", self.plan("--step-files")) / "plan.json").read_text())
        self.assertEqual(again["counts"]["step_files"]["to-copy"], 0)

    def test_step_file_failure_is_reported_and_retried_later(self):
        self._add_step_files()
        self.tgt.projects[2]["testcases"].clear()
        run_id = self.plan("--step-files")
        self.tgt.fail_upload_names = {"more.txt"}
        self.assertEqual(self.run_cli("apply", "--plan", run_id, "--yes"), 1)
        result = json.loads((find_run_dir(self.root / "output" / "runs", self._run_ids("-apply")[-1]) / "result.json").read_text())
        self.assertEqual(result["counts"]["step_files"], {"created": 3, "failed": 1})
        self.tgt.fail_upload_names = set()
        self.assertEqual(self.run_cli("apply", "--plan", run_id, "--yes"), 0)  # only the failed file is sent again
        total = sum(len(s.get("attachments", [])) for t in self.tgt.projects[2]["testcases"].values() for s in t["test_steps"])
        self.assertEqual(total, 4)

    def test_test_case_id_limits_the_plan_to_that_test_case(self):
        self.tgt.projects[2]["testcases"].clear()
        run_id = self.plan("--test-case-id", str(next(t["id"] for t in self.src.projects[1]["testcases"].values() if t["name"] == "tc two")))
        plan = json.loads((find_run_dir(self.root / "output" / "runs", run_id) / "plan.json").read_text())
        self.assertEqual([r["name"] for r in plan["testcases"]], ["tc two"])

    def test_without_the_step_files_flag_steps_are_not_read_one_by_one(self):
        self._add_step_files()
        self.plan("--attachments")
        self.assertFalse([r for r in self.src.requests if re.search(r"/test-cases/\d+$|/test-steps", r[1])])

    def test_plan_without_attachments_flag_does_not_touch_attachments(self):
        self._add_attachments()
        self.plan()
        self.assertFalse([r for r in self.src.requests if r[1].endswith("/attachments")])

    def _add_releases(self):
        r1 = self.src.add_release(1, "2026", status="301")
        self.src.add_build(1, r1, "build-1")
        self.src.add_build(1, r1, "build-2")
        self.src.add_release(1, "ENT 26.5", description="<p>x</p>", note="n")
        self.tgt.add_release(2, "ent 26.5")  # same name: reused, not created
        return r1

    def test_releases_only_plan_writes_nothing_and_apply_creates_them_with_dates_and_status(self):
        self._add_releases()
        run_id = self.plan("--releases-only")
        plan = json.loads((find_run_dir(self.root / "output" / "runs", run_id) / "plan.json").read_text())
        self.assertEqual(plan["counts"]["releases"], {"create": 1, "reuse": 1})
        self.assertEqual(plan["counts"]["builds"], {"create": 2})
        self.assertEqual(plan["folders"], [])
        self.assertEqual(self.tgt.writes(), [])
        self.assertEqual(self.run_cli("apply", "--plan", run_id, "--yes"), 0)
        self.assertEqual(self.src.writes(), [])
        by_name = {r["name"]: r for r in self.tgt.projects[2]["releases"]}
        self.assertEqual(sorted(by_name), ["2026", "ent 26.5"])  # nothing duplicated
        made = by_name["2026"]
        self.assertEqual((made["start_date"], made["end_date"]), ("2026-02-09T18:00:00-06:00", "2026-12-30T18:00:00-06:00"))
        self.assertEqual(made["properties"][0]["field_value_name"], "In Progress")  # status carried over
        self.assertEqual(sorted(b["name"] for b in self.tgt.projects[2]["builds"]), ["build-1", "build-2"])
        self.assertEqual(self.tgt.projects[2]["testcases"].get(0), None)
        self.assertEqual(len(self.tgt.projects[2]["testcases"]), 1)  # no test cases were migrated
        self.assertEqual(self.run_cli("verify", "--run", self._run_ids("-apply")[-1]), 0)

    def _add_cycles(self):
        r1 = self._add_releases()
        r2 = next(r["id"] for r in self.src.projects[1]["releases"] if r["name"] == "ENT 26.5")
        c1 = self.src.add_cycle(1, "Sprint 1", "release", r1)
        c1a = self.src.add_cycle(1, "Smoke", "test-cycle", c1)
        self.src.add_suite(1, "Login", "test-cycle", c1a)
        self.src.add_suite(1, "Loose suite", "release", r1)
        self.src.add_cycle(1, "Reg", "release", r2)
        tr = self.tgt.projects[2]["releases"][0]["id"]
        self.tgt.add_cycle(2, "reg", "release", tr)  # same name under the same parent: reused
        return r1

    def test_cycles_and_suites_are_created_top_down_under_their_release(self):
        self._add_cycles()
        run_id = self.plan("--cycles-only")
        plan = json.loads((find_run_dir(self.root / "output" / "runs", run_id) / "plan.json").read_text())
        self.assertEqual(plan["counts"]["cycles"], {"create": 2, "reuse": 1})
        self.assertEqual(plan["counts"]["suites"], {"create": 2})
        self.assertEqual(plan["folders"], [])
        self.assertEqual(self.tgt.writes(), [])
        self.assertEqual(self.run_cli("apply", "--plan", run_id, "--yes"), 0)
        self.assertEqual(self.src.writes(), [])
        proj = self.tgt.projects[2]
        rel = {r["name"]: r["id"] for r in proj["releases"]}
        cyc = {c["name"]: c for c in proj["cycles"]}
        self.assertEqual(sorted(cyc), ["Smoke", "Sprint 1", "reg"])  # 'reg' reused, nothing duplicated
        self.assertEqual((cyc["Sprint 1"]["parent_type"], cyc["Sprint 1"]["parent_id"]), ("release", rel["2026"]))
        self.assertEqual((cyc["Smoke"]["parent_type"], cyc["Smoke"]["parent_id"]), ("test-cycle", cyc["Sprint 1"]["id"]))
        suites = {x["name"]: x for x in proj["suites"]}
        self.assertEqual((suites["Login"]["parent_type"], suites["Login"]["parent_id"]), ("test-cycle", cyc["Smoke"]["id"]))
        self.assertEqual((suites["Loose suite"]["parent_type"], suites["Loose suite"]["parent_id"]), ("release", rel["2026"]))
        self.assertEqual(self.run_cli("verify", "--run", self._run_ids("-apply")[-1]), 0)

    def test_cycles_apply_twice_creates_nothing_again(self):
        self._add_cycles()
        run_id = self.plan("--cycles-only")
        self.assertEqual(self.run_cli("apply", "--plan", run_id, "--yes"), 0)
        self.assertEqual(self.run_cli("apply", "--plan", run_id, "--yes"), 0)
        self.assertEqual((len(self.tgt.projects[2]["cycles"]), len(self.tgt.projects[2]["suites"])), (3, 2))
        again = self.plan("--cycles-only")
        replan = json.loads((find_run_dir(self.root / "output" / "runs", again) / "plan.json").read_text())
        self.assertEqual(replan["counts"]["cycles"], {"skip-mapped": 3})
        self.assertEqual(replan["counts"]["suites"], {"skip-mapped": 2})

    def test_cycle_failure_skips_its_children_and_is_retried(self):
        self._add_cycles()
        run_id = self.plan("--cycles-only")
        self.tgt.fail_post_for_names = {"Sprint 1"}
        self.assertEqual(self.run_cli("apply", "--plan", run_id, "--yes"), 1)
        names = {c["name"] for c in self.tgt.projects[2]["cycles"]} | {x["name"] for x in self.tgt.projects[2]["suites"]}
        self.assertFalse(names & {"Smoke", "Login"})  # nothing below the cycle that failed
        self.tgt.fail_post_for_names = set()
        self.assertEqual(self.run_cli("apply", "--plan", run_id, "--yes"), 0)
        self.assertEqual((len(self.tgt.projects[2]["cycles"]), len(self.tgt.projects[2]["suites"])), (3, 2))

    def test_cycles_of_one_release_only(self):
        r1 = self._add_cycles()
        run_id = self.plan("--cycles-only", "--release-id", str(r1))
        plan = json.loads((find_run_dir(self.root / "output" / "runs", run_id) / "plan.json").read_text())
        self.assertEqual(sorted(r["name"] for r in plan["cycles"] if r["kind"] == "cycle"), ["Smoke", "Sprint 1"])

    def test_plan_without_cycles_flag_does_not_read_them(self):
        self._add_cycles()
        self.plan("--releases")
        self.assertFalse([r for r in self.src.requests if "test-cycles" in r[1] or "test-suites" in r[1]])

    def _add_runs(self):
        """Source tree from _add_cycles plus runs; 'tc one'/'tc two' are migrated by the plan, 'bin tc' is not."""
        r1 = self._add_cycles()
        c1 = next(c["id"] for c in self.src.projects[1]["cycles"] if c["name"] == "Sprint 1")
        login = next(x["id"] for x in self.src.projects[1]["suites"] if x["name"] == "Login")
        tcs = {t["name"]: t["id"] for t in self.src.projects[1]["testcases"].values()}
        bin_module = self.src.add_module(1, "Recycle Bin", None)
        bin_tc = self.src.add_testcase(1, bin_module, "bin tc")
        self.src.add_run(1, "run two", "test-cycle", c1, tcs["tc two"])
        self.src.add_run(1, "run three", "test-suite", login, tcs["tc three"])
        self.src.add_run(1, "run bin", "test-suite", login, bin_tc)
        return r1, tcs

    def test_runs_point_at_migrated_test_cases_and_runs_on_recycle_bin_are_skipped(self):
        self._add_runs()
        run_id = self.plan("--runs")
        plan = json.loads((find_run_dir(self.root / "output" / "runs", run_id) / "plan.json").read_text())
        self.assertEqual(plan["counts"]["runs"], {"create": 2, "skip-no-testcase": 1})
        self.assertEqual(self.tgt.writes(), [])
        self.assertEqual(self.run_cli("apply", "--plan", run_id, "--yes"), 0)
        self.assertEqual(self.src.writes(), [])
        proj = self.tgt.projects[2]
        by_name = {t["name"]: t["id"] for t in proj["testcases"].values()}
        runs = {r["name"]: r for r in proj["runs"]}
        self.assertEqual(sorted(runs), ["run three", "run two"])  # 'run bin' was not migrated
        self.assertEqual(runs["run two"]["testCaseId"], by_name["tc two"])
        self.assertEqual(runs["run three"]["testCaseId"], by_name["tc three"])
        cyc = {c["name"]: c["id"] for c in proj["cycles"]}
        suites = {x["name"]: x["id"] for x in proj["suites"]}
        self.assertEqual((runs["run two"]["parent_type"], runs["run two"]["parent_id"]), ("test-cycle", cyc["Sprint 1"]))
        self.assertEqual((runs["run three"]["parent_type"], runs["run three"]["parent_id"]), ("test-suite", suites["Login"]))
        self.assertEqual(self.run_cli("verify", "--run", self._run_ids("-apply")[-1]), 0)
        result = json.loads((find_run_dir(self.root / "output" / "runs", self._run_ids("-apply")[-1]) / "result.json").read_text())
        skipped = [r for r in result["runs"] if r["result"] == "skipped"]
        self.assertEqual([r["name"] for r in skipped], ["run bin"])

    def test_runs_apply_twice_creates_nothing_again(self):
        self._add_runs()
        run_id = self.plan("--runs")
        self.assertEqual(self.run_cli("apply", "--plan", run_id, "--yes"), 0)
        self.assertEqual(self.run_cli("apply", "--plan", run_id, "--yes"), 0)
        self.assertEqual(len(self.tgt.projects[2]["runs"]), 2)
        again = self.plan("--runs")
        replan = json.loads((find_run_dir(self.root / "output" / "runs", again) / "plan.json").read_text())
        self.assertEqual(replan["counts"]["runs"], {"skip-mapped": 2, "skip-no-testcase": 1})

    def test_run_whose_test_case_is_not_migrated_yet_is_skipped_until_it_is(self):
        self._add_runs()
        run_id = self.plan("--runs-only")  # test cases are not migrated yet
        plan = json.loads((find_run_dir(self.root / "output" / "runs", run_id) / "plan.json").read_text())
        self.assertEqual(plan["counts"]["runs"], {"skip-no-testcase": 3})
        self.assertEqual(self.run_cli("apply", "--plan", run_id, "--yes"), 0)
        self.assertEqual(self.tgt.projects[2]["runs"], [])

    def test_a_run_moved_away_in_the_target_is_created_again_and_a_recycle_bin_cycle_is_ignored(self):
        self._add_runs()
        c1 = next(c["id"] for c in self.src.projects[1]["cycles"] if c["name"] == "Sprint 1")
        bin_cycle = self.src.add_cycle(1, "Recycle Bin", "release", self.src.projects[1]["releases"][0]["id"])
        self.src.add_run(1, "run in bin", "test-cycle", bin_cycle, next(iter(self.src.projects[1]["testcases"])))
        run_id = self.plan("--runs", "--approve", "all")
        plan = self._plan_json(run_id)
        self.assertNotIn("Recycle Bin", [r["name"] for r in plan["cycles"]])
        self.assertNotIn("run in bin", [r["name"] for r in plan["runs"]])
        self.assertEqual(self.run_cli("apply", "--plan", run_id, "--yes"), 0)
        moved = next(r for r in self.tgt.projects[2]["runs"] if r["name"] == "run two")
        moved["parent_type"], moved["parent_id"] = "test-cycle", 999999  # moved to a Recycle Bin cycle in the UI
        replan = self._plan_json(self.plan("--runs", "--approve", "all"))
        self.assertEqual(replan["counts"]["runs"], {"create": 1, "skip-mapped": 1, "skip-no-testcase": 1})
        self.assertTrue([i for i in replan["issues"] if "moved away" in i["message"]])

    def test_run_of_a_test_case_deleted_in_the_target_is_skipped_with_that_reason(self):
        self._add_runs()
        run_id = self.plan("--runs")
        self.assertEqual(self.run_cli("apply", "--plan", run_id, "--yes"), 0)
        proj = self.tgt.projects[2]
        gone_tc = next(r["testCaseId"] for r in proj["runs"] if r["name"] == "run two")
        del proj["testcases"][gone_tc]  # deleted in the UI
        proj["runs"] = []
        again = self.plan("--runs-only")
        replan = json.loads((find_run_dir(self.root / "output" / "runs", again) / "plan.json").read_text())
        by_name = {r["name"]: r for r in replan["runs"]}
        self.assertEqual(by_name["run two"]["action"], "skip-no-testcase")
        self.assertIn("no longer exists", by_name["run two"]["error"])

    def test_files_attached_to_a_run_are_copied_once(self):
        self._add_runs()
        two = next(r for r in self.src.projects[1]["runs"] if r["name"] == "run two")
        two["attachments"] = [{"id": 5001, "name": "run-notes.txt", "content_type": "text/plain", "data": b"notes"}]
        run_id = self.plan("--runs", "--approve", "all")
        self.assertEqual(self._plan_json(run_id)["counts"]["run_attachments"], {"to-copy": 1})
        self.assertEqual(self.tgt.writes(), [])
        self.assertEqual(self.run_cli("apply", "--plan", run_id, "--yes"), 0)
        self.assertEqual(self.run_cli("apply", "--plan", run_id, "--yes"), 0)
        target = next(r for r in self.tgt.projects[2]["runs"] if r["name"] == "run two")
        self.assertEqual([(a["name"], a["data"]) for a in target["attachments"]], [("run-notes.txt", b"notes")])
        self.assertEqual(self.src.writes(), [])
        self.assertEqual(self.run_cli("verify", "--run", self._run_ids("-apply")[-1]), 0)

    def test_a_run_whose_files_cannot_be_listed_is_still_planned_and_the_problem_is_reported(self):
        self._add_runs()
        self.src.forbid_file_lists = True
        plan = self._plan_json(self.plan("--runs", "--approve", "all"))
        self.assertEqual(plan["counts"]["runs"], {"create": 2, "skip-no-testcase": 1})
        self.assertTrue([i for i in plan["issues"] if i["area"] == "run files" and "403" in i["message"]])

    def test_previous_assigned_to_of_a_run_is_kept_in_test_run_details(self):
        r1, tcs = self._add_runs()
        for run in self.src.projects[1]["runs"]:
            if run["name"] == "run two":
                run["properties"] = [{"field_name": "Assigned To", "field_value": "661273", "field_value_name": "Jeffrey Jandi"}]
        self.assertEqual(self.run_cli("apply", "--plan", self.plan("--runs"), "--yes"), 0)
        runs = {r["name"]: r for r in self.tgt.projects[2]["runs"]}
        self.assertEqual(runs["run two"]["assigned_to"], "Token User")  # HCSC default: the user of the API token
        self.assertEqual(runs["run two"]["details"], 'Migrated metadata: (Previously) "Assigned To" : "Jeffrey Jandi"')
        self.assertEqual(runs["run three"]["details"], "")  # nobody was assigned in the source

    def test_no_metadata_keeps_the_previous_run_assignee_out_of_test_run_details(self):
        self._add_runs()
        for run in self.src.projects[1]["runs"]:
            run["properties"] = [{"field_name": "Assigned To", "field_value": "1", "field_value_name": "Someone"}]
        self.assertEqual(self.run_cli("apply", "--plan", self.plan("--runs", "--no-metadata"), "--yes"), 0)
        self.assertEqual({r["details"] for r in self.tgt.projects[2]["runs"]}, {""})

    def test_runs_are_assigned_to_the_token_user_and_still_created_when_the_note_is_refused(self):
        self._add_runs()
        for run in self.src.projects[1]["runs"]:
            run["properties"] = [{"field_name": "Assigned To", "field_value": "1", "field_value_name": "Someone"}]
        self.tgt.refuse_run_properties = True
        self.assertEqual(self.run_cli("apply", "--plan", self.plan("--runs"), "--yes"), 0)
        runs = self.tgt.projects[2]["runs"]
        self.assertEqual(len(runs), 2)
        self.assertEqual({r["assigned_to"] for r in runs}, {"Token User"})
        result = json.loads((find_run_dir(self.root / "output" / "runs", self._run_ids("-apply")[-1]) / "result.json").read_text())
        self.assertTrue([r for r in result["runs"] if "could not be recorded" in r.get("assignee_note", "")])

    def test_run_failure_is_reported_and_retried(self):
        self._add_runs()
        run_id = self.plan("--runs")
        self.tgt.fail_post_for_names = {"run two"}
        self.assertEqual(self.run_cli("apply", "--plan", run_id, "--yes"), 1)
        self.assertEqual([r["name"] for r in self.tgt.projects[2]["runs"]], ["run three"])
        self.tgt.fail_post_for_names = set()
        self.assertEqual(self.run_cli("apply", "--plan", run_id, "--yes"), 0)
        self.assertEqual(len(self.tgt.projects[2]["runs"]), 2)

    def test_one_cycle_can_be_planned_with_its_subtree_and_parents_are_context_only(self):
        r1, _ = self._add_runs()
        top = next(c["id"] for c in self.src.projects[1]["cycles"] if c["name"] == "Sprint 1")
        smoke = next(c["id"] for c in self.src.projects[1]["cycles"] if c["name"] == "Smoke")
        run_id = self.plan("--runs", "--cycle-id", str(smoke))
        plan = json.loads((find_run_dir(self.root / "output" / "runs", run_id) / "plan.json").read_text())
        rows = {r["name"]: r for r in plan["cycles"]}
        self.assertEqual(sorted(rows), ["Login", "Smoke", "Sprint 1"])  # no 'Reg', no 'Loose suite'
        self.assertTrue(rows["Sprint 1"].get("context_only"))
        self.assertFalse(rows["Smoke"].get("context_only"))
        self.assertEqual([r["name"] for r in plan["releases"]], ["2026"])  # only the release above it
        self.assertEqual(sorted(r["name"] for r in plan["runs"]), ["run bin", "run three"])  # 'run two' is in the context cycle
        self.assertEqual(self.run_cli("apply", "--plan", run_id, "--yes"), 0)
        self.assertEqual(sorted(c["name"] for c in self.tgt.projects[2]["cycles"]), ["Smoke", "Sprint 1", "reg"])
        self.assertEqual([r["name"] for r in self.tgt.projects[2]["runs"]], ["run three"])

    def test_everything_deleted_in_the_target_is_created_again(self):
        self._add_runs()
        run_id = self.plan("--runs")
        self.assertEqual(self.run_cli("apply", "--plan", run_id, "--yes"), 0)
        proj = self.tgt.projects[2]
        gone_release = next(r["id"] for r in proj["releases"] if r["name"] == "2026")
        proj["releases"] = [r for r in proj["releases"] if r["id"] != gone_release]  # deleted in the UI, with all below it
        proj["builds"] = [b for b in proj["builds"] if b["release_id"] != gone_release]
        proj["cycles"] = [c for c in proj["cycles"] if c["name"] == "reg"]
        proj["suites"], proj["runs"] = [], []
        again = self.plan("--runs")
        replan = json.loads((find_run_dir(self.root / "output" / "runs", again) / "plan.json").read_text())
        self.assertEqual(replan["counts"]["releases"], {"create": 1, "skip-mapped": 1})
        self.assertEqual(replan["counts"]["cycles"], {"create": 2, "skip-mapped": 1})
        self.assertEqual(replan["counts"]["runs"], {"create": 2, "skip-no-testcase": 1})
        self.assertTrue([i for i in replan["issues"] if "no longer exists" in i["message"]])
        self.assertEqual(self.run_cli("apply", "--plan", again, "--yes"), 0)
        self.assertEqual(sorted(r["name"] for r in proj["runs"]), ["run three", "run two"])
        self.assertEqual(sorted(c["name"] for c in proj["cycles"]), ["Smoke", "Sprint 1", "reg"])
        self.assertEqual(len([r for r in proj["releases"] if r["name"] == "2026"]), 1)
        self.assertEqual(self.run_cli("verify", "--run", self._run_ids("-apply")[-1]), 0)

    def _add_logs(self):
        r1, tcs = self._add_runs()
        self.src.projects[1]["statuses"] = [{"id": 601, "name": "Passed"}, {"id": 602, "name": "Failed"}]
        self.tgt.projects[2]["statuses"] = [{"id": 801, "name": "passed"}, {"id": 802, "name": "failed"}]
        runs = {r["name"]: r["id"] for r in self.src.projects[1]["runs"]}
        self.src.add_log(1, runs["run two"], (602, "Failed"), "2026-07-01T10:00:00-05:00", "2026-07-01T10:05:00-05:00", tester="Jeffrey Jandi")
        self.src.add_log(1, runs["run two"], (601, "Passed"), "2026-07-02T10:00:00-05:00", "2026-07-02T10:05:00-05:00", tester="Jeffrey Jandi",
                         note="rerun ok", files=(("evidence.txt", b"proof"),))
        self.src.add_log(1, runs["run three"], (602, "Failed"), "2026-07-03T09:00:00-05:00", "2026-07-03T09:01:00-05:00")
        self.src.add_log(1, runs["run bin"], (601, "Passed"), "2026-07-03T09:00:00-05:00", "2026-07-03T09:01:00-05:00")
        return runs

    def _plan_json(self, run_id):
        return json.loads((find_run_dir(self.root / "output" / "runs", run_id) / "plan.json").read_text())

    def test_all_logs_are_copied_oldest_first_with_status_dates_note_and_files(self):
        self._add_logs()
        run_id = self.plan("--logs", "--approve", "all")
        plan = self._plan_json(run_id)
        self.assertEqual(plan["counts"]["logs"], {"create": 3})  # the Recycle Bin run has none planned
        self.assertEqual(plan["counts"]["log_attachments"], {"to-copy": 1})
        self.assertEqual(self.tgt.writes(), [])
        self.assertEqual(self.run_cli("apply", "--plan", run_id, "--yes"), 0)
        self.assertEqual(self.src.writes(), [])
        proj = self.tgt.projects[2]
        two = next(r["id"] for r in proj["runs"] if r["name"] == "run two")
        logs = sorted((l for l in proj["logs"] if l["run_id"] == two), key=lambda l: l["id"])  # creation order
        self.assertEqual([(l["status"]["id"], l["status"]["name"]) for l in logs], [(802, "Failed"), (801, "Passed")])
        self.assertEqual([l["exe_start_date"] for l in logs], ["2026-07-01T10:00:00-05:00", "2026-07-02T10:00:00-05:00"])
        self.assertIn("rerun ok", logs[1]["note"])
        self.assertIn("originally executed by Jeffrey Jandi", logs[1]["note"])
        self.assertEqual([a["name"] for a in logs[1]["attachments"]], ["evidence.txt"])
        self.assertEqual(logs[1]["attachments"][0]["data"], b"proof")
        self.assertEqual(self.run_cli("verify", "--run", self._run_ids("-apply")[-1]), 0)

    def test_only_the_last_log_of_each_run_with_logs_last(self):
        self._add_logs()
        plan = self._plan_json(self.plan("--logs", "last", "--approve", "all"))
        self.assertEqual(plan["counts"]["logs"], {"create": 2, "skip-older": 1})

    def test_logs_apply_twice_creates_nothing_again(self):
        self._add_logs()
        run_id = self.plan("--logs", "--approve", "all")
        self.assertEqual(self.run_cli("apply", "--plan", run_id, "--yes"), 0)
        self.assertEqual(self.run_cli("apply", "--plan", run_id, "--yes"), 0)
        self.assertEqual(len(self.tgt.projects[2]["logs"]), 3)
        self.assertEqual(sum(len(l["attachments"]) for l in self.tgt.projects[2]["logs"]), 1)
        replan = self._plan_json(self.plan("--logs", "--approve", "all"))
        self.assertEqual(replan["counts"]["logs"], {"skip-mapped": 3})

    def test_logs_are_created_again_when_their_run_was_deleted_in_the_target(self):
        self._add_logs()
        self.assertEqual(self.run_cli("apply", "--plan", self.plan("--logs", "--approve", "all"), "--yes"), 0)
        proj = self.tgt.projects[2]
        proj["runs"], proj["logs"] = [], []  # runs deleted in the UI, logs go with them
        replan_id = self.plan("--logs", "--approve", "all")
        replan = self._plan_json(replan_id)
        self.assertEqual(replan["counts"]["logs"], {"create": 3})
        self.assertEqual(self.run_cli("apply", "--plan", replan_id, "--yes"), 0)
        self.assertEqual(len(proj["runs"]), 2)
        self.assertEqual(len(proj["logs"]), 3)

    def test_log_with_a_status_missing_in_the_target_is_skipped_and_listed(self):
        self._add_logs()
        self.tgt.projects[2]["statuses"] = [{"id": 801, "name": "Passed"}]
        plan = self._plan_json(self.plan("--logs", "--approve", "all"))
        self.assertEqual(plan["counts"]["logs"], {"create": 1, "skip-no-status": 2})
        self.assertIn("does not exist in the target", next(r for r in plan["logs"] if r["action"] == "skip-no-status")["error"])

    def test_no_metadata_leaves_the_log_note_as_it_was(self):
        self._add_logs()
        run_id = self.plan("--logs", "--no-metadata", "--approve", "all")
        self.assertEqual(self.run_cli("apply", "--plan", run_id, "--yes"), 0)
        notes = sorted(l["note"] for l in self.tgt.projects[2]["logs"])
        self.assertEqual(notes, ["", "", "rerun ok"])

    def test_log_failure_is_reported_and_retried(self):
        self._add_logs()
        run_id = self.plan("--logs", "--approve", "all")
        self.tgt.projects[2]["statuses"] = []  # the target refuses every log
        self.assertEqual(self.run_cli("apply", "--plan", run_id, "--yes"), 1)
        self.assertEqual(self.tgt.projects[2]["logs"], [])
        self.tgt.projects[2]["statuses"] = [{"id": 801, "name": "passed"}, {"id": 802, "name": "failed"}]
        self.assertEqual(self.run_cli("apply", "--plan", run_id, "--yes"), 0)
        self.assertEqual(len(self.tgt.projects[2]["logs"]), 3)

    def test_step_results_are_copied_with_the_log_matched_by_step_order(self):
        runs = self._add_logs()
        two = next(l for l in self.src.projects[1]["logs"] if l["run_id"] == runs["run two"] and l["status"]["name"] == "Passed")
        two["test_step_logs"] = [{"order": 1, "status": {"id": 601, "name": "Passed"}, "actual_result": "fine"},
                                 {"order": 2, "status": {"id": 602, "name": "Failed"}, "actual_result": "bad"}]
        run_id = self.plan("--logs", "--approve", "all")
        plan = self._plan_json(run_id)
        self.assertEqual(plan["counts"]["log_steps"], {"to-copy": 2})
        self.assertEqual(self.run_cli("apply", "--plan", run_id, "--yes"), 0)
        proj = self.tgt.projects[2]
        by_tc = {t["name"]: t["id"] for t in proj["testcases"].values()}
        two_run = next(r["id"] for r in proj["runs"] if r["name"] == "run two")
        newest = max((l for l in proj["logs"] if l["run_id"] == two_run), key=lambda l: l["exe_end_date"])
        done = {sl["order"]: (sl["status"]["name"], sl["actual_result"]) for sl in newest["test_step_logs"]}
        self.assertEqual(done[1], ("Passed", "fine"))
        self.assertEqual(done[2], ("Failed", "bad"))
        self.assertEqual(self.run_cli("verify", "--run", self._run_ids("-apply")[-1]), 0)

    def test_no_step_results_flag_and_a_target_that_refuses_them(self):
        runs = self._add_logs()
        two = next(l for l in self.src.projects[1]["logs"] if l["run_id"] == runs["run two"] and l["status"]["name"] == "Passed")
        two["test_step_logs"] = [{"order": 1, "status": {"id": 601, "name": "Passed"}, "actual_result": ""}]
        self.assertEqual(self._plan_json(self.plan("--logs", "--approve", "all", "--no-step-results"))["counts"]["log_steps"], {"to-copy": 0})
        self.tgt.refuse_step_logs = True
        run_id = self.plan("--logs", "--approve", "all")
        self.assertEqual(self.run_cli("apply", "--plan", run_id, "--yes"), 0)  # the logs are created anyway
        self.assertEqual(len(self.tgt.projects[2]["logs"]), 3)
        result = json.loads((find_run_dir(self.root / "output" / "runs", self._run_ids("-apply")[-1]) / "result.json").read_text())
        self.assertTrue([r for r in result["logs"] if "not accepted" in r.get("steps_note", "")])

    def test_logs_wait_for_an_approved_test_case_version(self):
        self._add_logs()
        self.tgt.logs_need_approved = True
        run_id = self.plan("--logs")  # test cases are created by this plan: version not known yet, logs are planned
        self.assertEqual(self.run_cli("apply", "--plan", run_id, "--yes"), 1)  # HCSC refuses them
        self.assertEqual(self.tgt.projects[2]["logs"], [])
        plan = self._plan_json(self.plan("--logs"))  # now the test cases and runs exist as drafts
        self.assertEqual(plan["counts"]["logs"], {"skip-not-approved": 3})
        self.assertIn("approve the test case", plan["logs"][0]["error"].lower())
        for tc in self.tgt.projects[2]["testcases"].values():  # approve, but the runs stay bound to the draft version
            tc["version"] = "1.0"
        self.assertEqual(self._plan_json(self.plan("--logs"))["counts"]["logs"], {"skip-not-approved": 3})
        self.tgt.projects[2]["runs"] = []  # the drafts are deleted in the UI and created again against 1.0
        replan = self.plan("--logs")
        self.assertEqual(self._plan_json(replan)["counts"]["logs"], {"create": 3})
        self.assertEqual(self.run_cli("apply", "--plan", replan, "--yes"), 0)
        self.assertEqual(len(self.tgt.projects[2]["logs"]), 3)

    def test_plan_without_logs_flag_does_not_read_them(self):
        self._add_logs()
        self.plan("--runs")
        self.assertFalse([r for r in self.src.requests if "test-logs" in r[1]])

    def test_plan_without_runs_flag_does_not_read_them(self):
        self._add_runs()
        self.plan("--cycles")
        self.assertFalse([r for r in self.src.requests if "test-runs" in r[1]])

    def test_release_starting_before_the_project_uses_the_project_start(self):
        self.tgt.projects[2]["start_date"] = "2026-06-01T00:00:00+00:00"
        self.src.add_release(1, "Old", start_date="2026-02-09T18:00:00-06:00", end_date="2026-12-30T18:00:00-06:00")
        run_id = self.plan("--releases-only")
        plan = json.loads((find_run_dir(self.root / "output" / "runs", run_id) / "plan.json").read_text())
        self.assertEqual(plan["releases"][0]["payload"]["start_date"], "2026-06-01T00:00:00+00:00")
        self.assertEqual(plan["releases"][0]["payload"]["end_date"], "2026-12-30T18:00:00-06:00")  # later than project start: kept
        self.assertEqual(self.run_cli("apply", "--plan", run_id, "--yes"), 0)
        self.assertEqual(self.tgt.projects[2]["releases"][0]["start_date"], "2026-06-01T00:00:00+00:00")
        self.assertEqual(self.run_cli("verify", "--run", self._run_ids("-apply")[-1]), 0)

    def test_releases_apply_twice_creates_nothing_again(self):
        self._add_releases()
        run_id = self.plan("--releases-only")
        self.assertEqual(self.run_cli("apply", "--plan", run_id, "--yes"), 0)
        self.assertEqual(self.run_cli("apply", "--plan", run_id, "--yes"), 0)
        self.assertEqual(len(self.tgt.projects[2]["releases"]), 2)
        self.assertEqual(len(self.tgt.projects[2]["builds"]), 2)
        again = self.plan("--releases-only")  # a fresh plan sees them all as done
        replan = json.loads((find_run_dir(self.root / "output" / "runs", again) / "plan.json").read_text())
        self.assertEqual(replan["counts"]["releases"], {"skip-mapped": 2})  # the reused one is mapped too
        self.assertEqual(replan["counts"]["builds"], {"skip-mapped": 2})

    def test_release_failure_is_reported_and_retried(self):
        self._add_releases()
        run_id = self.plan("--releases-only")
        self.tgt.fail_post_for_names = {"2026"}
        self.assertEqual(self.run_cli("apply", "--plan", run_id, "--yes"), 1)
        self.assertEqual(self.tgt.projects[2]["builds"], [])  # no builds under a release that failed
        self.tgt.fail_post_for_names = set()
        self.assertEqual(self.run_cli("apply", "--plan", run_id, "--yes"), 0)
        self.assertEqual(len(self.tgt.projects[2]["releases"]), 2)
        self.assertEqual(len(self.tgt.projects[2]["builds"]), 2)

    def test_releases_can_be_planned_together_with_test_cases(self):
        self._add_releases()
        run_id = self.plan("--releases")
        plan = json.loads((find_run_dir(self.root / "output" / "runs", run_id) / "plan.json").read_text())
        self.assertTrue(plan["folders"] and plan["testcases"] and plan["releases"])
        self.assertEqual(self.run_cli("apply", "--plan", run_id, "--yes"), 0)
        self.assertEqual(len(self.tgt.projects[2]["releases"]), 2)
        self.assertEqual(len(self.tgt.projects[2]["testcases"]), 3)

    def test_plan_without_releases_flag_does_not_read_releases(self):
        self._add_releases()
        self.plan()
        self.assertFalse([r for r in self.src.requests if r[1].endswith("/releases") or r[1].endswith("/builds")])

    def test_preflight_maps_statuses_and_releases_read_only(self):
        self.src.projects[1]["statuses"] = [{"id": 601, "name": "Passed"}, {"id": 602, "name": "Failed"}]
        self.tgt.projects[2]["statuses"] = [{"id": 801, "name": "passed"}]
        self.src.projects[1]["releases"] = [{"id": 1, "name": "R1", "start_date": "2026-01-01T00:00:00Z"},
                                            {"id": 2, "name": "R2"}]
        self.tgt.projects[2]["releases"] = [{"id": 9, "name": "r1"}]
        self.assertEqual(self.run_cli("preflight", "--source-project", "1", "--target-project", "2"), 0)
        self.assertEqual(self.tgt.writes(), [])
        self.assertEqual(self.src.writes(), [])
        run_dir = find_run_dir(self.root / "output" / "runs", self._run_ids("-preflight")[-1])
        data = json.loads((run_dir / "preflight.json").read_text())
        maps = data["mappings"]
        self.assertEqual([(r["name"], r["target_id"]) for r in maps["statuses"]["rows"]], [("Passed", 801), ("Failed", None)])
        self.assertEqual([(r["name"], r["action"]) for r in maps["releases"]["rows"]], [("R1", "exists"), ("R2", "create")])
        report = (run_dir / "report.md").read_text()
        for heading in ("## Execution statuses", "## Releases"):
            self.assertIn(heading, report)
        self.assertIn("'Failed'", " ".join(i["message"] for i in data["issues"]))

    def test_preflight_survives_a_failing_mapping_endpoint(self):
        del self.tgt.projects[2]["releases"]  # the fake server answers 404 for it
        self.assertEqual(self.run_cli("preflight", "--source-project", "1", "--target-project", "2"), 0)

    def test_apply_requires_confirmation_when_not_interactive(self):
        run_id = self.plan()
        with patch("sys.stdin.isatty", return_value=False):
            self.assertEqual(self.run_cli("apply", "--plan", run_id), 1)
        self.assertEqual(self.tgt.writes(), [])

    def test_bad_token_gives_clear_error(self):
        set_creds(self, self.src, self.tgt, hs_token="wrong", hcsc_token="tgt-token")
        self.assertEqual(self.run_cli("plan", "--source-project", "1", "--target-project", "2"), 2)


class ClientTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeQtest("t", page_limit=2)
        self.addCleanup(self.fake.close)
        patcher = patch.dict(os.environ, {"QMIG_ALLOW_HTTP": "1"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.fake.add_project(1, "P", [])
        self.module = self.fake.add_module(1, "M")
        for i in range(5):
            self.fake.add_testcase(1, self.module, f"tc{i}")

    def client(self, **kw):
        return QtestClient(Instance("x", normalize_url(self.fake.url), "t"), sleep=lambda s: None, **kw)

    def test_read_only_client_blocks_writes(self):
        client = self.client(allow_writes=False)
        with self.assertRaises(WriteBlocked):
            client.create_module(1, "nope", None)
        self.assertEqual(self.fake.writes(), [])
        self.assertEqual(client.stats["blocked_writes"], 1)

    def test_pagination_collects_everything_despite_server_page_cap(self):
        client = self.client(page_size=100)  # server caps at 2 per page
        self.assertEqual(len(client.list_testcases(1, self.module)), 5)

    def test_endpoint_ignoring_page_terminates(self):
        self.assertEqual(len(self.client().list_modules(1)), 1)

    def test_wrong_token(self):
        client = QtestClient(Instance("x", normalize_url(self.fake.url), "bad"), sleep=lambda s: None)
        with self.assertRaises(ApiError) as caught:
            client.get_project(1)
        self.assertEqual(caught.exception.status, 401)


class UnitTest(unittest.TestCase):
    def test_url_and_token_normalisation(self):
        self.assertEqual(normalize_url("https://a.qtestnet.com/"), "https://a.qtestnet.com/api/v3")
        self.assertEqual(normalize_url("https://a.qtestnet.com/api/v3"), "https://a.qtestnet.com/api/v3")
        for bad in ("http://a.com", "https://u:p@a.com", "https://a.com/x", "https://a.com?x=1"):
            with self.assertRaises(ConfigError):
                normalize_url(bad)
        self.assertEqual(normalize_token("Bearer abc"), "abc")
        with self.assertRaises(ConfigError):
            normalize_token("a b")

    def test_multi_select_values_are_mapped_by_label(self):
        src = [{"id": 1, "label": "Tags", "allowed_values": [{"value": 1, "label": "a"}, {"value": 2, "label": "b"}]}]
        tgt = [{"id": 9, "label": "Tags", "allowed_values": [{"value": 20, "label": "B"}, {"value": 10, "label": "A"}]}]
        props, warnings = FieldMapper(src, tgt).map_properties([{"field_id": 1, "field_name": "Tags", "field_value": "[1,2]"}])
        self.assertEqual(props, [{"field_id": 9, "field_value": "[10,20]"}])
        self.assertEqual(warnings, [])

    def test_automated_test_case_fields_are_carried(self):
        """Type, Automation and Automation Content are what makes a test case automated in qTest."""
        def fields(base):
            return [{"id": base + 1, "label": "Type", "allowed_values": [{"value": 701, "label": "Manual"}, {"value": 702, "label": "Automation"}]},
                    {"id": base + 2, "label": "Automation", "allowed_values": [{"value": 711, "label": "Yes"}, {"value": 712, "label": "No"}]},
                    {"id": base + 3, "label": "Automation Content", "allowed_values": []}]
        source = [{"field_id": 1, "field_name": "Type", "field_value": "702", "field_value_name": "Automation"},
                  {"field_id": 2, "field_name": "Automation", "field_value": "711", "field_value_name": "Yes"},
                  {"field_id": 3, "field_name": "Automation Content", "field_value": "auto"}]
        props, warnings = FieldMapper(fields(0), fields(10)).map_properties(source)
        self.assertEqual(props, [{"field_id": 11, "field_value": "702"}, {"field_id": 12, "field_value": "711"},
                                 {"field_id": 13, "field_value": "auto"}])
        self.assertEqual(warnings, [])

    def test_empty_automation_content_gets_auto_because_the_target_refuses_it(self):
        def fields(base):
            return [{"id": base + 1, "label": "Type", "allowed_values": [{"value": 701, "label": "Manual"}, {"value": 702, "label": "Automation"}]},
                    {"id": base + 2, "label": "Automation", "allowed_values": [{"value": 711, "label": "Yes"}, {"value": 712, "label": "No"}]},
                    {"id": base + 3, "label": "Automation Content", "allowed_values": []}]
        def source(type_value, automation_value, content=None):
            props = [{"field_id": 1, "field_name": "Type", "field_value": str(type_value)},
                     {"field_id": 2, "field_name": "Automation", "field_value": str(automation_value)}]
            return props + ([{"field_id": 3, "field_name": "Automation Content", "field_value": content}] if content else [])
        mapper = FieldMapper(fields(0), fields(10))
        added = {"field_id": 13, "field_value": "auto"}
        self.assertIn(added, mapper.map_properties(source(702, 711))[0])      # automation, no content
        self.assertIn(added, mapper.map_properties(source(701, 711))[0])      # Automation = Yes alone is enough
        self.assertIn(added, mapper.map_properties(source(702, 712))[0])      # so is Type = Automation
        self.assertNotIn(added, mapper.map_properties(source(701, 712))[0])   # a manual test case gets nothing
        self.assertEqual([p for p in mapper.map_properties(source(702, 711, "my script"))[0] if p["field_id"] == 13],
                         [{"field_id": 13, "field_value": "my script"}])      # real content is never replaced
        self.assertEqual(mapper.auto_content_added, 3)

    def test_paths_use_pathlib_only(self):
        with patch.dict(os.environ, {"QMIG_HOME": tempfile.gettempdir()}):
            paths = cli.get_paths()
            self.assertEqual(paths.db.name, "qmig.sqlite")
            self.assertTrue(paths.runs.is_absolute())


if __name__ == "__main__":
    unittest.main()
