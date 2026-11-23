"""qmig batch: several projects from a CSV, with failures, interruption and resume."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from qmig.batch import BatchError, parse_csv
from qmig.config import get_paths
from qmig.runindex import find_run_dir

from . import test_links, test_qmig

FAST = ("--network-retry-interval", "0")


class CsvTest(unittest.TestCase):
    def read(self, text: str):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "projects.csv"
            path.write_text(text, encoding="utf-8")
            return parse_csv(path)

    def test_columns_comments_blank_module_and_semicolons(self):
        rows = self.read("# my projects\nsource_project;target_project;module_id\n25804;26256;\n\n25761;26257;1234\n")
        self.assertEqual(rows, [{"source_project": 25804, "target_project": 26256, "module_id": None},
                                {"source_project": 25761, "target_project": 26257, "module_id": 1234}])

    def test_module_column_is_optional_and_names_are_forgiving(self):
        self.assertEqual(self.read("Source,Target\n1,2\n"), [{"source_project": 1, "target_project": 2, "module_id": None}])

    def test_mistakes_say_what_and_where(self):
        for text, part in (("a,b\n1,2\n", "source_project and target_project"), ("source,target\n1,x\n", "line 2"),
                           ("source,target\n1,2\n1,2\n", "same row"), ("source,target\n", "no rows")):
            with self.assertRaises(BatchError) as caught:
                self.read(text)
            self.assertIn(part, str(caught.exception))


class BatchTest(unittest.TestCase):
    setUp0 = test_qmig.EndToEndTest.setUp
    run_cli = test_qmig.EndToEndTest.run_cli

    def setUp(self):
        self.setUp0()
        self.second_pair()

    def second_pair(self):
        self.src.add_project(3, "HS Two", test_qmig.SRC_FIELDS)
        m = self.src.add_module(3, "Z", None, mid=30)
        self.src.add_testcase(3, m, "two tc", properties=test_qmig.tc_props(601, "New"))
        self.tgt.add_project(4, "HCSC Two", test_qmig.TGT_FIELDS)
        self.csv = self.root / "projects.csv"
        self.csv.write_text("source_project,target_project\n1,2\n3,4\n", encoding="utf-8")

    def batch_json(self) -> dict:
        return json.loads(sorted(get_paths().root.glob("output/batches/*/batch.json"))[-1].read_text(encoding="utf-8"))

    def plan(self, *options, expect=0):
        code = self.run_cli("batch", "plan", "--csv", str(self.csv), "--", *options)
        self.assertEqual(code, expect)
        return self.batch_json()["batch_id"]

    def names(self, project):
        return sorted(t["name"] for t in self.tgt.projects[project]["testcases"].values())

    def test_plan_is_read_only_and_a_bad_project_does_not_stop_the_others(self):
        self.csv.write_text("source_project,target_project\n1,2\n3,4\n1,99\n", encoding="utf-8")
        self.plan(expect=1)
        self.assertEqual(self.tgt.writes(), [])
        self.assertEqual(self.src.writes(), [])
        data = self.batch_json()
        self.assertEqual([p["plan"]["status"] for p in data["projects"]], ["planned", "planned", "failed"])
        self.assertTrue(data["projects"][2]["plan"]["error"])
        self.assertEqual(data["projects"][0]["plan"]["counts"]["testcases"], {"skip-exists": 1, "create": 2})
        status = (get_paths().root / "output" / "batches" / data["batch_id"] / "status.md").read_text(encoding="utf-8")
        self.assertIn("1 -> 99", status)

    def test_wrong_plan_options_are_refused_before_anything_is_read(self):
        self.assertEqual(self.run_cli("batch", "plan", "--csv", str(self.csv), "--", "--no-such-option"), 2)
        self.assertEqual(self.src.requests, [])

    def test_apply_all_then_verify_then_nothing_left_to_do(self):
        batch = self.plan("--approve", "all")
        self.assertEqual(self.run_cli("batch", "apply", "--batch", batch, "--yes"), 0)
        self.assertEqual(self.names(2), ["TC One", "tc three", "tc two"])
        self.assertEqual(self.names(4), ["two tc"])
        self.assertEqual(self.src.writes(), [])
        self.assertEqual(self.run_cli("batch", "verify", "--batch", batch), 0)
        data = self.batch_json()
        self.assertEqual([p["apply"]["status"] for p in data["projects"]], ["applied", "applied"])
        self.assertEqual([p["verify"]["status"] for p in data["projects"]], ["passed", "passed"])
        self.assertTrue(all(p["apply"]["seconds"] >= 0 for p in data["projects"]))
        posts = len(self.tgt.writes())
        self.assertEqual(self.run_cli("batch", "apply", "--batch", batch, "--yes"), 0)
        self.assertEqual(len(self.tgt.writes()), posts)  # nothing is sent twice

    def test_a_failing_project_does_not_stop_the_others_and_is_retried_alone(self):
        batch = self.plan()
        self.tgt.fail_post_for_names = {"tc two"}
        self.assertEqual(self.run_cli("batch", "apply", "--batch", batch, "--yes"), 1)
        data = self.batch_json()
        self.assertEqual([p["apply"]["status"] for p in data["projects"]], ["applied-with-errors", "applied"])
        self.assertEqual(self.names(4), ["two tc"])
        self.tgt.fail_post_for_names = set()
        second_project_writes = len([r for r in self.tgt.writes() if "/projects/4/" in r[1]])
        self.assertEqual(self.run_cli("batch", "apply", "--batch", batch, "--yes"), 0)
        self.assertEqual(self.names(2), ["TC One", "tc three", "tc two"])
        self.assertEqual(len([r for r in self.tgt.writes() if "/projects/4/" in r[1]]), second_project_writes)  # untouched

    def test_a_network_stop_ends_the_batch_cleanly_and_the_same_command_resumes_it(self):
        batch = self.plan()
        self.tgt.drop_next = 10_000
        self.assertEqual(self.run_cli("batch", "apply", "--batch", batch, "--yes", "--network-retries", "2", *FAST), 75)
        data = self.batch_json()
        self.assertEqual(data["projects"][0]["apply"]["status"], "interrupted")
        self.assertEqual(data["projects"][1]["apply"], {})  # not started
        status = (get_paths().root / "output" / "batches" / batch / "status.md").read_text(encoding="utf-8")
        self.assertIn("Interrupted by the network", status)
        self.tgt.drop_next = 0
        self.assertEqual(self.run_cli("batch", "apply", "--batch", batch, "--yes", *FAST), 0)
        self.assertEqual(self.names(2), ["TC One", "tc three", "tc two"])
        self.assertEqual(self.names(4), ["two tc"])
        self.assertEqual([p["apply"]["status"] for p in self.batch_json()["projects"]], ["applied", "applied"])

    def test_rows_limit_what_is_applied(self):
        batch = self.plan()
        self.assertEqual(self.run_cli("batch", "apply", "--batch", batch, "--rows", "2", "--yes"), 0)
        self.assertEqual(self.names(4), ["two tc"])
        self.assertEqual(self.names(2), ["TC One"])  # project 1 untouched
        self.assertEqual(self.run_cli("batch", "apply", "--batch", batch, "--rows", "7", "--yes"), 2)

    def test_apply_refuses_without_confirmation_when_not_interactive(self):
        batch = self.plan()
        self.assertEqual(self.run_cli("batch", "apply", "--batch", batch), 1)
        self.assertEqual(self.tgt.writes(), [])

    def test_runs_made_in_the_same_second_get_different_ids(self):
        from qmig.runlog import unique_run_id
        paths = get_paths()
        first = unique_run_id(paths, "plan")
        (paths.runs / "hs-1__hcsc-2" / first).mkdir(parents=True)
        self.assertNotEqual(unique_run_id(paths, "plan"), first)

    def test_status_shows_the_latest_batch(self):
        self.plan()
        self.assertEqual(self.run_cli("batch", "status"), 0)

    def test_fields_option_plans_the_fields_of_every_project(self):
        self.assertEqual(self.run_cli("batch", "plan", "--csv", str(self.csv), "--fields"), 0)
        self.assertEqual([p["fields_plan"]["status"] for p in self.batch_json()["projects"]], ["planned", "planned"])


class BatchLinksTest(unittest.TestCase):
    """Links for several projects: capture, plan, apply, verify, with a project that has nothing to link."""
    setUp0 = test_links.RequirementLinksTest.setUp
    run_cli = test_qmig.EndToEndTest.run_cli
    batch_json = BatchTest.batch_json

    def setUp(self):
        self.setUp0()
        # a second pair with one covered test case, a third pair that covers nothing
        self.src.add_project(3, "HS Two", test_qmig.SRC_FIELDS)
        m = self.src.add_module(3, "Z", None, mid=30)
        tc = self.src.add_testcase(3, m, "two tc", properties=test_qmig.tc_props(601, "New"))
        req = self.src.add_requirement(3, "POD-50 Fifty", "POD-50")
        self.src.add_link(3, tc, req)
        self.tgt.add_project(4, "HCSC Two", test_qmig.TGT_FIELDS)
        self.t_fifty = self.tgt.add_requirement(4, "POD-50 Fifty", "POD-50")
        self.src.add_project(5, "HS Three", test_qmig.SRC_FIELDS)
        m = self.src.add_module(5, "Y", None, mid=50)
        self.src.add_testcase(5, m, "three tc", properties=test_qmig.tc_props(601, "New"))
        self.tgt.add_project(6, "HCSC Three", test_qmig.TGT_FIELDS)
        self.csv = self.root / "projects.csv"
        self.csv.write_text("source_project,target_project\n1,2\n3,4\n5,6\n", encoding="utf-8")
        self.assertEqual(self.run_cli("batch", "plan", "--csv", str(self.csv), "--", "--approve", "all"), 0)
        self.batch = self.batch_json()["batch_id"]
        self.assertEqual(self.run_cli("batch", "apply", "--batch", self.batch, "--yes"), 0)

    def links_of(self, project):
        return self.tgt.links(project)

    def test_links_for_every_project_and_nothing_to_link_is_not_an_error(self):
        self.assertEqual(self.run_cli("batch", "links", "plan", "--batch", self.batch), 0)
        statuses = [p["links_plan"]["status"] for p in self.batch_json()["projects"]]
        self.assertEqual(statuses, ["planned", "planned", "nothing-to-link"])
        self.assertEqual([w for w in self.tgt.writes() if w[1].endswith("/link")], [])
        self.assertEqual(self.run_cli("batch", "links", "apply", "--batch", self.batch, "--yes"), 0)
        self.assertEqual(len(self.links_of(2)), 3)
        self.assertEqual(len(self.links_of(4)), 1)
        self.assertEqual(self.run_cli("batch", "links", "verify", "--batch", self.batch), 0)
        data = self.batch_json()
        self.assertEqual([p["links_apply"].get("status") for p in data["projects"]], ["applied", "applied", None])
        self.assertEqual([p["links_verify"].get("status") for p in data["projects"]], ["passed", "passed", None])
        posts = len([w for w in self.tgt.writes() if w[1].endswith("/link")])
        self.assertEqual(self.run_cli("batch", "links", "apply", "--batch", self.batch, "--yes"), 0)
        self.assertEqual(len([w for w in self.tgt.writes() if w[1].endswith("/link")]), posts)  # nothing sent twice

    def test_key_map_is_passed_to_every_project_plan(self):
        self.assertEqual(self.run_cli("batch", "links", "plan", "--batch", self.batch, "--key-map", "POD=SCRUM"), 0)
        run_ids = [p["links_plan"]["run_id"] for p in self.batch_json()["projects"]
                   if p["links_plan"].get("status") == "planned"]
        self.assertTrue(run_ids)
        for run_id in run_ids:
            plan = json.loads((find_run_dir(self.root / "output" / "runs", run_id) / "links_plan.json").read_text())
            self.assertEqual(plan["scope"]["key_map"], {"POD": "SCRUM"})

    def test_a_network_stop_during_links_resumes_with_the_same_command(self):
        self.assertEqual(self.run_cli("batch", "links", "plan", "--batch", self.batch), 0)
        self.tgt.drop_next = 10_000
        self.assertEqual(self.run_cli("batch", "links", "apply", "--batch", self.batch, "--yes", "--network-retries", "2", *FAST), 75)
        self.assertEqual(self.batch_json()["projects"][0]["links_apply"]["status"], "interrupted")
        self.tgt.drop_next = 0
        self.assertEqual(self.run_cli("batch", "links", "apply", "--batch", self.batch, "--yes", *FAST), 0)
        self.assertEqual((len(self.links_of(2)), len(self.links_of(4))), (3, 1))

    def test_links_need_the_test_cases_first(self):
        self.csv.write_text("source_project,target_project\n1,2\n", encoding="utf-8")
        self.assertEqual(self.run_cli("batch", "plan", "--csv", str(self.csv)), 0)
        self.assertEqual(self.run_cli("batch", "links", "plan"), 1)  # not applied yet: nothing to do

    def test_the_status_table_has_a_links_column(self):
        self.run_cli("batch", "links", "plan", "--batch", self.batch)
        status = (get_paths().root / "output" / "batches" / self.batch / "status.md").read_text(encoding="utf-8")
        self.assertIn("| Links |", status)
        self.assertIn("nothing-to-link", status)
        self.assertIn("batch links apply", status)


class ReportTest(unittest.TestCase):
    setUp0 = test_qmig.EndToEndTest.setUp
    second_pair = BatchTest.second_pair
    run_cli = test_qmig.EndToEndTest.run_cli
    batch_json = BatchTest.batch_json
    plan = BatchTest.plan

    def setUp(self):
        self.setUp0()
        self.second_pair()

    def report(self, *extra) -> str:
        out = self.root / "report.html"
        self.assertEqual(self.run_cli("report", "html", "--out", str(out), *extra), 0)
        return out.read_text(encoding="utf-8")

    def test_report_of_a_finished_batch_has_the_numbers_and_stays_self_contained(self):
        batch = self.plan("--attachments")
        self.assertEqual(self.run_cli("batch", "apply", "--batch", batch, "--yes"), 0)
        self.assertEqual(self.run_cli("batch", "verify", "--batch", batch), 0)
        page = self.report("--deadline", "2099-01-01")
        self.assertIn("qTest migration status", page)
        self.assertIn("2 / 2", page)                        # projects verified
        self.assertIn("100.0%", page)                       # verified-correct share
        self.assertIn("Test cases", page)
        self.assertIn("Where the projects stand", page)
        self.assertEqual(page.count("<svg"), 1)             # the progress chart; the status bar is plain HTML
        self.assertIn("target, all 2 by", page)             # the --deadline line
        self.assertNotIn("<script", page)
        self.assertNotIn("http://", page.replace("http://www.w3.org", ""))
        self.assertNotIn("https://", page)                  # no external resources at all
        self.assertIn("field value group(s) have no match", page)  # the Legacy status the target does not have
        self.assertIn("Not migrated by decision", page)

    def test_report_of_a_batch_with_problems_says_what_needs_attention(self):
        batch = self.plan()
        self.tgt.fail_post_for_names = {"tc two"}
        self.assertEqual(self.run_cli("batch", "apply", "--batch", batch, "--yes"), 1)
        page = self.report()
        self.assertIn("Errors", page)
        self.assertIn("failed", page)
        self.assertIn("run `batch apply` again", page)

    def test_report_of_a_batch_that_is_only_planned_does_not_crash(self):
        self.plan()
        page = self.report()
        self.assertIn("0 / 2", page)
        self.assertIn("no project applied yet", page)

    def test_report_interruptions_are_counted(self):
        batch = self.plan()
        self.tgt.drop_next = 10_000
        self.assertEqual(self.run_cli("batch", "apply", "--batch", batch, "--yes", "--network-retries", "2", *FAST), 75)
        self.tgt.drop_next = 0
        self.assertEqual(self.run_cli("batch", "apply", "--batch", batch, "--yes", *FAST), 0)
        page = self.report()
        self.assertIn("Applied, not verified", page)
