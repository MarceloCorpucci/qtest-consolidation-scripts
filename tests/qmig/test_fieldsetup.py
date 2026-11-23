import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from qmig import cli

from .fakeqtest import FakeQtest, set_creds

SRC = [
    {"id": 1, "label": "Status", "attribute_type": "Number", "data_type": 3, "system_field": True, "is_active": True,
     "allowed_values": [{"value": 1, "label": "New", "is_active": True}]},
    {"id": 2, "label": "Eligible to Automate", "attribute_type": "Number", "data_type": 3, "required": True, "is_active": True,
     "allowed_values": [{"value": 21, "label": "Y", "is_active": True}, {"value": 22, "label": "N", "is_default": True, "is_active": True}]},
    {"id": 3, "label": "Hierarchy", "attribute_type": "String", "data_type": 1, "is_active": True},
    {"id": 4, "label": "Test Phase", "attribute_type": "Number", "data_type": 3, "is_active": True,
     "allowed_values": [{"value": 41, "label": "Unit", "is_active": True}, {"value": 42, "label": "New Value", "is_active": True}]},
    {"id": 5, "label": "Team", "attribute_type": "String", "data_type": 1, "is_active": True},
    {"id": 6, "label": "Assigned To", "attribute_type": "Number", "data_type": 4, "is_active": True},
]
TGT = [
    {"id": 10, "label": "Status", "attribute_type": "Number", "data_type": 3, "system_field": True, "is_active": True,
     "allowed_values": [{"value": 1, "label": "New", "is_active": True}]},
    {"id": 14, "label": "Test Phase", "attribute_type": "Number", "data_type": 3, "is_active": True,
     "allowed_values": [{"value": 141, "label": "Unit", "is_active": True}]},
    {"id": 15, "label": "Team", "attribute_type": "String", "data_type": 1, "is_active": True},
]


class FieldSetupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.src = FakeQtest("src-token")
        self.tgt = FakeQtest("tgt-token")
        set_creds(self, self.src, self.tgt, hs_token="Bearer src-token", hcsc_token="tgt-token")
        patcher = patch.dict(os.environ, {"QMIG_HOME": str(self.root), "QMIG_ALLOW_HTTP": "1"})
        patcher.start()
        for cleanup in (patcher.stop, self.tmp.cleanup, self.src.close, self.tgt.close):
            self.addCleanup(cleanup)
        self.src.add_project(1, "HS Project", [dict(f) for f in SRC])
        self.tgt.add_project(2, "HSCS Project", [dict(f) for f in TGT])

    def run_cli(self, *argv) -> int:
        with patch("sys.stdout"):
            return cli.main(list(argv))

    def plan(self, *extra) -> int:
        return self.run_cli("fields", "plan", "--source-project", "1", "--target-project", "2", *(extra or ("--skip-fields", "Assigned To")))

    def run_dir(self, suffix: str) -> str:
        return sorted(p.name for p in (self.root / "output" / "runs").glob("*/*") if p.name.endswith(suffix))[-1]

    def labels(self):
        return {f["label"] for f in self.tgt.projects[2]["fields"]}

    def test_plan_writes_nothing_and_classifies_fields(self):
        self.assertEqual(self.plan(), 0)
        self.assertFalse([r for r in self.tgt.requests if r[0] != "GET"])
        plan_dir = next((self.root / "output" / "runs").glob("*/*-fields-plan"))
        import json
        rows = {r["label"]: r for r in json.loads((plan_dir / "fields_plan.json").read_text())["rows"]}
        self.assertEqual({k: v["action"] for k, v in rows.items()},
                         {"Eligible to Automate": "create", "Hierarchy": "create", "Test Phase": "manual", "Team": "exists"})
        self.assertIn("New Value", rows["Test Phase"]["detail"])
        self.assertTrue((plan_dir / "report.md").is_file())

    def test_default_plan_follows_the_decisions(self):
        self.run_cli("fields", "plan", "--source-project", "1", "--target-project", "2")
        import json
        plan_dir = next((self.root / "output" / "runs").glob("*/*-fields-plan"))
        rows = {r["label"]: r["action"] for r in json.loads((plan_dir / "fields_plan.json").read_text())["rows"]}
        self.assertEqual(rows, {"Team": "exists"})  # not-needed, pending and Test Phase fields are left out

    def test_apply_creates_missing_fields_then_verifies(self):
        self.plan()
        plan = self.run_dir("-fields-plan")
        self.assertEqual(self.run_cli("fields", "apply", "--plan", plan, "--yes"), 0)
        self.assertEqual(self.labels(), {"Status", "Test Phase", "Team", "Eligible to Automate", "Hierarchy"})
        eligible = next(f for f in self.tgt.projects[2]["fields"] if f["label"] == "Eligible to Automate")
        self.assertTrue(eligible["required"])
        self.assertEqual([(o["label"], o["is_default"]) for o in eligible["allowed_values"]], [("Y", False), ("N", True)])
        self.assertEqual(self.run_cli("fields", "verify", "--run", self.run_dir("-fields-apply")), 0)

    def test_apply_refuses_a_target_url_that_differs_from_the_reviewed_plan(self):
        import json
        self.plan()
        plan_file = next((self.root / "output" / "runs").glob("*/*-fields-plan")) / "fields_plan.json"
        plan = json.loads(plan_file.read_text())
        plan["target"]["url"] = "https://some-other-server.example"
        plan_file.write_text(json.dumps(plan))
        self.assertNotEqual(self.run_cli("fields", "apply", "--plan", self.run_dir("-fields-plan"), "--yes"), 0)
        self.assertEqual(self.labels(), {"Status", "Test Phase", "Team"})

    def test_a_lost_reply_is_checked_before_creating_the_field_again(self):
        self.plan()
        self.tgt.lose_reply_of = [r"POST .*/settings/test-cases/fields$"]
        self.assertEqual(self.run_cli("fields", "apply", "--plan", self.run_dir("-fields-plan"), "--yes", "--network-retry-interval", "0"), 0)
        self.assertEqual(self.labels(), {"Status", "Test Phase", "Team", "Eligible to Automate", "Hierarchy"})
        self.assertEqual(len([r for r in self.tgt.requests if r[0] == "POST"]), 2)  # one per field, none repeated

    def test_apply_is_idempotent(self):
        self.plan()
        plan = self.run_dir("-fields-plan")
        self.run_cli("fields", "apply", "--plan", plan, "--yes")
        posts = len([r for r in self.tgt.requests if r[0] == "POST"])
        self.assertEqual(self.run_cli("fields", "apply", "--plan", plan, "--yes"), 0)
        self.assertEqual(len([r for r in self.tgt.requests if r[0] == "POST"]), posts)

    def test_failed_field_is_reported_and_other_fields_continue(self):
        self.tgt.refuse_field_labels = {"Eligible to Automate"}
        self.plan()
        self.assertEqual(self.run_cli("fields", "apply", "--plan", self.run_dir("-fields-plan"), "--yes"), 1)
        self.assertIn("Hierarchy", self.labels())
        self.assertNotIn("Eligible to Automate", self.labels())

    def test_source_is_never_written(self):
        self.plan()
        self.run_cli("fields", "apply", "--plan", self.run_dir("-fields-plan"), "--yes")
        self.assertFalse([r for r in self.src.requests if r[0] != "GET"])

    def test_apply_needs_confirmation_when_not_interactive(self):
        self.plan()
        with patch("sys.stdin.isatty", return_value=False):
            self.assertEqual(self.run_cli("fields", "apply", "--plan", self.run_dir("-fields-plan")), 1)
        self.assertNotIn("Hierarchy", self.labels())


class MetadataTest(unittest.TestCase):
    def test_values_of_unneeded_fields_go_into_the_description(self):
        from qmig.plan import migrated_metadata
        tc = {"properties": [
            {"field_name": "Eligible to Automate", "field_value": "5", "field_value_name": "Y"},
            {"field_name": "Regression Flag", "field_value": "6", "field_value_name": "_NULL_"},
            {"field_name": "Estimated Time", "field_value": "0.0"},
            {"field_name": "Hierarchy", "field_value": "A > B & C"},
            {"field_name": "Zephyr Test Case ID", "field_value": ""},
            {"field_name": "Comments (Archive)", "field_value": "old note"},
            {"field_name": "Team", "field_value": "Core"}]}
        text = migrated_metadata(tc)
        self.assertEqual(text, '<b>Migrated metadata</b>:<br>(Previously) "Eligible to Automate" : "Y"<br>'
                               '(Previously) "Hierarchy" : "A &gt; B &amp; C"<br>(Previously) "Comments (Archive)" : "old note"')

    def test_no_data_no_block(self):
        from qmig.plan import migrated_metadata
        self.assertEqual(migrated_metadata({"properties": [{"field_name": "Hierarchy", "field_value": None}]}), "")


if __name__ == "__main__":
    unittest.main()
