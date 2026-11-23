import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from qmig import cli
from qmig.runindex import find_run_dir
from qmig.db import StateDb
from qmig.reqlinks import LinksError, map_key, normalize_name, parse_key_map, requirement_key, resolve_requirement

from .fakeqtest import FakeQtest, set_creds
from .test_qmig import SRC_FIELDS, TGT_FIELDS, tc_props


class KeyMapTest(unittest.TestCase):
    def test_prefix_and_exact_rules(self):
        rules = parse_key_map("abc=scrum, BCA=SCRUM;ABC-5=SCRUM-90")
        self.assertEqual(map_key("ABC-111", rules), "SCRUM-111")
        self.assertEqual(map_key("bca-7", rules), "SCRUM-7")
        self.assertEqual(map_key("ABC-5", rules), "SCRUM-90")  # exact beats prefix
        self.assertEqual(map_key("XYZ-1", rules), "XYZ-1")  # no rule: unchanged
        self.assertEqual(map_key("ABC-111", {}), "ABC-111")  # default: no mapping
        self.assertEqual(parse_key_map(None), {})

    def test_bad_rule_is_refused(self):
        with self.assertRaises(LinksError):
            parse_key_map("ABC")


class RequirementKeyTest(unittest.TestCase):
    def test_key_comes_from_external_properties_first(self):
        req = {"name": "POD-1 title", "external_properties": {"id": "abc-9"}}
        self.assertEqual(requirement_key(req), "ABC-9")

    def test_key_falls_back_to_name_prefix(self):
        self.assertEqual(requirement_key({"name": " pod-233 PMI: something"}), "POD-233")
        self.assertEqual(requirement_key({"name": "POD-233: something"}), "POD-233")

    def test_no_key(self):
        self.assertIsNone(requirement_key({"name": "Plain requirement"}))
        self.assertIsNone(requirement_key({"name": "POD-12abc not a key"}))
        self.assertIsNone(requirement_key({"name": "", "external_properties": {}}))

    def test_name_normalisation(self):
        self.assertEqual(normalize_name("  Plain ​ Req&zwnj;\r\n  X "), "plain req x")
        self.assertEqual(normalize_name("A &amp; B"), "a & b")
        self.assertEqual(normalize_name(None), "")

    def test_resolution_order(self):
        by_key = {"POD-1": [{"id": 1}], "DUP-1": [{"id": 2}, {"id": 3}]}
        by_name = {"plain": [{"id": 4}], "twice": [{"id": 5}, {"id": 6}]}
        self.assertEqual(resolve_requirement({"jira_key": "pod-1", "requirement_name": "plain"}, by_key, by_name),
                         ("match", {"id": 1}, "key"))
        self.assertEqual(resolve_requirement({"jira_key": "DUP-1"}, by_key, by_name)[0], "ambiguous")
        # a key that the target lacks is NOT matched by name unless asked
        miss = {"jira_key": "POD-9", "requirement_name": "plain"}
        self.assertEqual(resolve_requirement(miss, by_key, by_name), ("not-found", None, "key"))
        self.assertEqual(resolve_requirement(miss, by_key, by_name, name_on_key_miss=True), ("match", {"id": 4}, "name"))
        self.assertEqual(resolve_requirement({"requirement_name": "Plain"}, by_key, by_name)[2], "name")
        self.assertEqual(resolve_requirement({"requirement_name": "twice"}, by_key, by_name)[0], "ambiguous")
        self.assertEqual(resolve_requirement({"requirement_name": "nope"}, by_key, by_name)[0], "not-found")
        self.assertEqual(resolve_requirement({"requirement_name": "  "}, by_key, by_name)[0], "no-identifier")


class RequirementLinksTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.src = FakeQtest("src-token", page_limit=3)
        self.tgt = FakeQtest("tgt-token", page_limit=3)
        set_creds(self, self.src, self.tgt, hs_token="Bearer src-token", hcsc_token="tgt-token")
        patcher = patch.dict(os.environ, {"QMIG_HOME": str(self.root), "QMIG_ALLOW_HTTP": "1"})
        patcher.start()
        for cleanup in (patcher.stop, self.tmp.cleanup, self.src.close, self.tgt.close):
            self.addCleanup(cleanup)

        self.src.add_project(1, "HS Project", SRC_FIELDS)
        a = self.src.add_module(1, "A", None, mid=10)
        self.tc1 = self.src.add_testcase(1, a, "tc one", properties=tc_props(601, "New"))
        self.tc2 = self.src.add_testcase(1, a, "tc two", properties=tc_props(602, "Done"))
        self.tc3 = self.src.add_testcase(1, a, "tc three", properties=tc_props(602, "Done"))
        self.tc4 = self.src.add_testcase(1, a, "tc four (no requirement)", properties=tc_props(602, "Done"))

        s = self.src
        r_login = s.add_requirement(1, "POD-1 Login", "POD-1")
        r_logout = s.add_requirement(1, "POD-2 Logout", "POD-2")
        r_plain = s.add_requirement(1, "Plain  Req​")  # no key -> matched by name
        r_dup_name = s.add_requirement(1, "Dup name")  # no key, two in target -> ambiguous
        r_dup_key = s.add_requirement(1, "POD-5 Five", "POD-5")  # two in target -> ambiguous
        r_name_key = s.add_requirement(1, "POD-9 Nine", "POD-9")  # target has it only as a name prefix
        s.add_link(1, self.tc1, r_login)
        s.add_link(1, self.tc1, r_logout)
        s.add_link(1, self.tc2, r_plain)
        s.add_link(1, self.tc2, r_dup_name)
        s.add_link(1, self.tc3, r_dup_key)
        s.add_link(1, self.tc3, r_name_key)

        self.tgt.add_project(2, "HCSC Project", TGT_FIELDS)
        t = self.tgt
        self.t_login = t.add_requirement(2, "POD-1 Login (renamed in HCSC)", "POD-1")
        self.t_plain = t.add_requirement(2, "plain req")
        t.add_requirement(2, "dup name")
        t.add_requirement(2, "Dup  Name")
        t.add_requirement(2, "POD-5 Five a", "POD-5")
        t.add_requirement(2, "POD-5 Five b", "POD-5")
        self.t_name_key = t.add_requirement(2, "POD-9 Nine (no external key)")
        self.t_other_key = t.add_requirement(2, "POD-2 Logout", "POD-77")  # same name, different key

    def run_cli(self, *argv) -> int:
        with patch("sys.stdout"):
            return cli.main(list(argv))

    def runs(self, suffix: str) -> list[str]:
        return sorted(p.name for p in (self.root / "output" / "runs").glob("*/*") if p.name.endswith(suffix))

    def migrate_test_cases(self):
        self.assertEqual(self.run_cli("plan", "--source-project", "1", "--target-project", "2"), 0)
        self.assertEqual(self.run_cli("apply", "--plan", self.runs("-plan")[-1], "--yes"), 0)
        self.target_tc = {t["name"]: t["id"] for t in self.tgt.projects[2]["testcases"].values()}

    def capture(self) -> int:
        return self.run_cli("links", "capture", "--source-project", "1", "--target-project", "2")

    def plan(self, *extra) -> dict:
        self.assertEqual(self.run_cli("links", "plan", "--source-project", "1", "--target-project", "2", *extra), 0)
        run_id = self.runs("-links-plan")[-1]
        return json.loads((find_run_dir(self.root / "output" / "runs", run_id) / "links_plan.json").read_text())

    def ready(self) -> dict:
        self.migrate_test_cases()
        self.assertEqual(self.capture(), 0)
        return self.plan()

    def by_label(self, plan: dict) -> dict:
        return {(r["source_testcase_id"], r["source_requirement_id"]): r for r in plan["rows"]}

    # ------------------------------------------------------------------ capture
    def test_capture_requires_migrated_test_cases(self):
        self.assertEqual(self.capture(), 2)  # nothing migrated yet -> clear error
        self.assertEqual(self.src.writes(), [])

    def test_capture_stores_links_and_only_reads(self):
        self.migrate_test_cases()
        self.assertEqual(self.capture(), 0)
        self.assertEqual(self.src.writes(), [])
        db = StateDb(self.root / "data" / "qmig.sqlite")
        rows = db.requirement_links("hs", 1)
        db.close()
        self.assertEqual(len(rows), 6)  # test runs linked to the test cases are ignored
        keys = {r["source_requirement_pid"]: r["jira_key"] for r in rows}
        self.assertEqual(sorted(k for k in keys.values() if k), ["POD-1", "POD-2", "POD-5", "POD-9"])
        self.assertEqual(sum(1 for r in rows if r["jira_key"] is None), 2)
        report = (find_run_dir(self.root / "output" / "runs", self.runs("-links-capture")[-1]) / "report.md").read_text()
        self.assertIn("Requirements without a Jira key: 2", report)

    def test_capture_replaces_stale_links(self):
        self.migrate_test_cases()
        self.capture()
        self.src.projects[1]["links"] = [(t, r) for t, r in self.src.projects[1]["links"] if t != self.tc3]
        self.capture()
        db = StateDb(self.root / "data" / "qmig.sqlite")
        left = {r["source_testcase_id"] for r in db.requirement_links("hs", 1)}
        db.close()
        self.assertNotIn(self.tc3, left)

    # --------------------------------------------------------------------- plan
    def test_plan_matches_by_key_then_name_and_is_read_only(self):
        self.migrate_test_cases()
        self.capture()
        self.tgt.add_link(2, self.target_tc["tc one"], self.t_login)  # already linked in the target
        writes_before = len(self.tgt.writes())
        plan = self.plan()
        self.assertEqual(len(self.tgt.writes()), writes_before)
        self.assertEqual(self.src.writes(), [])
        rows = list(self.by_label(plan).values())

        def row(name_part):
            return next(r for r in rows if name_part in r["requirement_name"])
        login = row("POD-1 Login")
        self.assertEqual((login["action"], login["matched_by"], login["target_requirement_id"]),
                         ("exists", "key", self.t_login))
        self.assertEqual(row("POD-2 Logout")["action"], "not-found")  # key POD-2 is not in the target; name is NOT used
        plain = row("Plain")
        self.assertEqual((plain["action"], plain["matched_by"], plain["target_requirement_id"]),
                         ("link", "name", self.t_plain))
        self.assertEqual(row("Dup name")["action"], "ambiguous")
        self.assertEqual(row("POD-5")["action"], "ambiguous")
        nine = row("POD-9")  # target requirement has no external key, only the key in its name
        self.assertEqual((nine["action"], nine["matched_by"], nine["target_requirement_id"]),
                         ("link", "key", self.t_name_key))
        self.assertEqual(plan["counts"]["actions"], {"exists": 1, "not-found": 1, "link": 2, "ambiguous": 2})
        self.assertIn("more than one", " ".join(i["message"] for i in plan["issues"]))
        run_dir = find_run_dir(self.root / "output" / "runs", self.runs("-links-plan")[-1])
        for name in ("links_plan.json", "links.csv", "report.md", "events.jsonl", "run.log"):
            self.assertTrue((run_dir / name).is_file(), name)
        self.assertNotIn("tgt-token", (run_dir / "run.log").read_text())

    def test_plan_name_on_key_miss(self):
        self.migrate_test_cases()
        self.capture()
        plan = self.plan("--name-on-key-miss")
        logout = next(r for r in plan["rows"] if "POD-2" in r["requirement_name"])
        self.assertEqual((logout["action"], logout["matched_by"], logout["target_requirement_id"]),
                         ("link", "name", self.t_other_key))

    def test_plan_key_map_links_to_the_mapped_key_only_when_asked(self):
        self.migrate_test_cases()
        self.capture()
        logout = lambda plan: next(r for r in plan["rows"] if "POD-2" in r["requirement_name"])
        self.assertEqual(logout(self.plan())["action"], "not-found")  # default: unchanged behaviour
        plan = self.plan("--key-map", "POD-2=POD-77")
        row = logout(plan)
        self.assertEqual((row["action"], row["matched_by"], row["target_requirement_id"], row["mapped_key"]),
                         ("link", "key", self.t_other_key, "POD-77"))
        self.assertEqual(plan["scope"]["key_map"], {"POD-2": "POD-77"})
        run_dir = find_run_dir(self.root / "output" / "runs", self.runs("-links-plan")[-1])
        self.assertIn("Key map (pilot only): POD-2 -> POD-77", (run_dir / "report.md").read_text())

    def test_plan_marks_unmigrated_test_cases(self):
        self.migrate_test_cases()
        self.capture()
        db = StateDb(self.root / "data" / "qmig.sqlite")
        db.conn.execute("DELETE FROM testcase_map WHERE source_id=?", (self.tc3,))
        db.conn.commit()
        db.close()
        plan = self.plan()
        actions = {r["action"] for r in plan["rows"] if r["source_testcase_id"] == self.tc3}
        self.assertEqual(actions, {"tc-not-migrated"})

    def test_plan_without_capture_fails_clearly(self):
        self.migrate_test_cases()
        self.assertEqual(self.run_cli("links", "plan", "--source-project", "1", "--target-project", "2"), 2)

    # -------------------------------------------------------------------- apply
    def test_apply_says_what_to_do_when_a_test_case_was_deleted_in_the_target(self):
        self.migrate_test_cases()
        self.capture()
        self.plan()
        plan_id = self.runs("-links-plan")[-1]
        self.tgt.projects[2]["testcases"].pop(self.target_tc["tc two"])  # someone deleted it in the target
        self.run_cli("links", "apply", "--plan", plan_id, "--yes")
        result = json.loads((find_run_dir(self.root / "output" / "runs", self.runs("-links-apply")[-1])
                             / "links_result.json").read_text())
        self.assertTrue([r for r in result["rows"] if r["status"] == "failed"])
        self.assertTrue([i for i in result["issues"] if "deleted there" in i["message"]])

    def test_apply_creates_links_once_and_verify_passes(self):
        self.ready()
        plan_id = self.runs("-links-plan")[-1]
        self.assertEqual(self.run_cli("links", "apply", "--plan", plan_id, "--yes"), 0)
        self.assertEqual(self.src.writes(), [])
        links = self.tgt.links(2)
        self.assertIn((self.target_tc["tc two"], self.t_plain), links)
        self.assertIn((self.target_tc["tc three"], self.t_name_key), links)
        self.assertIn((self.target_tc["tc one"], self.t_login), links)
        self.assertEqual(len(links), 3)  # unmatched, ambiguous and not-found links are never created
        link_posts = [r for r in self.tgt.writes() if r[1].endswith("/link")]
        self.assertEqual(len(link_posts), 3)  # one request per target requirement

        apply_id = self.runs("-links-apply")[-1]
        result = json.loads((find_run_dir(self.root / "output" / "runs", apply_id) / "links_result.json").read_text())
        self.assertEqual(result["counts"]["actions"], {"linked": 3, "not-found": 1, "ambiguous": 2})

        # same plan again: nothing is posted twice
        self.assertEqual(self.run_cli("links", "apply", "--plan", plan_id, "--yes"), 0)
        self.assertEqual(len([r for r in self.tgt.writes() if r[1].endswith("/link")]), 3)
        again = json.loads((find_run_dir(self.root / "output" / "runs", self.runs("-links-apply")[-1]) / "links_result.json").read_text())
        self.assertEqual(again["counts"]["actions"]["skip-mapped"], 3)

        self.assertEqual(self.run_cli("links", "verify", "--run", apply_id), 0)
        verify = json.loads((find_run_dir(self.root / "output" / "runs", self.runs("-links-verify")[-1]) / "links_verify.json").read_text())
        self.assertEqual((verify["checked"], verify["ok"], verify["passed"]), (3, 3, True))

        # a fresh plan now sees both links in the target
        fresh = self.plan()
        self.assertEqual(fresh["counts"]["actions"].get("exists"), 3)
        self.assertNotIn("link", fresh["counts"]["actions"])

    def test_verify_detects_removed_link(self):
        self.ready()
        self.run_cli("links", "apply", "--plan", self.runs("-links-plan")[-1], "--yes")
        self.tgt.projects[2]["links"].clear()
        self.assertEqual(self.run_cli("links", "verify", "--run", self.runs("-links-apply")[-1]), 1)

    def test_a_lost_reply_is_checked_before_linking_again(self):
        self.ready()
        plan_id = self.runs("-links-plan")[-1]
        self.tgt.lose_reply_of = [r"POST .*/requirements/\d+/link$"]
        self.assertEqual(self.run_cli("links", "apply", "--plan", plan_id, "--yes", "--network-retry-interval", "0"), 0)
        self.assertEqual(len(self.tgt.links(2)), 3)
        self.assertEqual(len([r for r in self.tgt.writes() if r[1].endswith("/link")]), 3)  # the cut-off one was not sent again

    def test_apply_batches_requests(self):
        self.migrate_test_cases()
        for i in range(5):  # five more test cases covering the same target requirement
            tc = self.src.add_testcase(1, 10, f"extra {i}", properties=tc_props(602, "Done"))
            self.src.add_link(1, tc, next(iter(self.src.projects[1]["requirements"])))  # POD-1 Login
        self.assertEqual(self.run_cli("plan", "--source-project", "1", "--target-project", "2"), 0)
        self.assertEqual(self.run_cli("apply", "--plan", self.runs("-plan")[-1], "--yes"), 0)
        self.assertEqual(self.capture(), 0)
        plan_id = self.plan() and self.runs("-links-plan")[-1]
        self.assertEqual(self.run_cli("links", "apply", "--plan", plan_id, "--yes", "--batch-size", "2"), 0)
        posts = [r for r in self.tgt.writes() if r[1].endswith(f"/requirements/{self.t_login}/link")]
        self.assertEqual(len(posts), 3)  # 6 test cases cover POD-1 (tc one + 5 extra), 2 per request
        self.assertEqual(sum(1 for t, r in self.tgt.links(2) if r == self.t_login), 6)

    def test_failed_requirement_does_not_stop_the_others(self):
        self.ready()
        self.tgt.fail_link_for_requirements.add(self.t_plain)
        self.assertEqual(self.run_cli("links", "apply", "--plan", self.runs("-links-plan")[-1], "--yes"), 1)
        links = self.tgt.links(2)
        self.assertIn((self.target_tc["tc three"], self.t_name_key), links)
        self.assertNotIn((self.target_tc["tc two"], self.t_plain), links)
        result = json.loads((find_run_dir(self.root / "output" / "runs", self.runs("-links-apply")[-1]) / "links_result.json").read_text())
        self.assertEqual(result["counts"]["actions"]["failed"], 1)
        failed = next(r for r in result["rows"] if r["status"] == "failed")
        self.assertIn("HTTP 400", failed["error"])

        # fixing the cause and applying the same plan again links the rest
        self.tgt.fail_link_for_requirements.clear()
        self.assertEqual(self.run_cli("links", "apply", "--plan", self.runs("-links-plan")[-1], "--yes"), 0)
        self.assertIn((self.target_tc["tc two"], self.t_plain), self.tgt.links(2))

    def test_apply_requires_confirmation_when_not_interactive(self):
        self.ready()
        with patch("sys.stdin.isatty", return_value=False):
            self.assertEqual(self.run_cli("links", "apply", "--plan", self.runs("-links-plan")[-1]), 1)
        self.assertEqual([r for r in self.tgt.writes() if r[1].endswith("/link")], [])

    def test_unauthorised_apply_stops(self):
        self.ready()
        set_creds(self, self.src, self.tgt, hs_token="Bearer src-token", hcsc_token="wrong")
        self.assertEqual(self.run_cli("links", "apply", "--plan", self.runs("-links-plan")[-1], "--yes"), 1)
        result = json.loads((find_run_dir(self.root / "output" / "runs", self.runs("-links-apply")[-1]) / "links_result.json").read_text())
        self.assertIn("authorization", result["aborted"])
        self.assertEqual(self.tgt.links(2), set())


if __name__ == "__main__":
    unittest.main()
