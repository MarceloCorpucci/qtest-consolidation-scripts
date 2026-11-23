"""Network loss during plan/apply: wait, stop cleanly, resume, no duplicates."""
from __future__ import annotations

import json
import unittest

from qmig.db import StateDb
from qmig.config import get_paths
from qmig.network import NetworkPolicy, format_seconds, parse_seconds, policy_from
from qmig.runindex import find_run_dir

from . import test_qmig

FAST = ("--network-retry-interval", "0")


class PolicyTest(unittest.TestCase):
    def test_pause_doubles_up_to_the_longest_pause(self):
        policy = NetworkPolicy(10, 30, 600)
        self.assertEqual([policy.delay(n) for n in range(1, 9)], [30, 60, 120, 240, 480, 600, 600, 600])
        self.assertEqual(sum(policy.delay(n) for n in range(1, 11)), 3930)  # about 1 h 5 min

    def test_times_and_defaults(self):
        self.assertEqual(parse_seconds("90"), 90)
        self.assertEqual(parse_seconds("10m"), 600)
        self.assertEqual(parse_seconds("2h"), 7200)
        with self.assertRaises(ValueError):
            parse_seconds("soon")
        self.assertEqual(format_seconds(3930), "1 h 5 min 30 s")
        self.assertEqual(policy_from(), NetworkPolicy(10, 30, 600))
        self.assertEqual(policy_from(retries=100, interval="1m", max_interval="1h"), NetworkPolicy(100, 60, 3600))
        self.assertFalse(policy_from(retries=0).enabled)


class ResilienceTest(unittest.TestCase):
    setUp = test_qmig.EndToEndTest.setUp
    run_cli = test_qmig.EndToEndTest.run_cli
    plan = test_qmig.EndToEndTest.plan

    def names(self):
        return sorted(t["name"] for t in self.tgt.projects[2]["testcases"].values())

    def last_status(self):
        db = StateDb(get_paths().db)
        try:
            return db.conn.execute("SELECT status FROM runs WHERE mode='apply' ORDER BY rowid DESC LIMIT 1").fetchone()[0]
        finally:
            db.close()

    def test_a_short_outage_is_waited_out_and_nothing_fails(self):
        run_id = self.plan()
        self.tgt.drop_next = 3
        self.assertEqual(self.run_cli("apply", "--plan", run_id, "--yes", *FAST), 0)
        self.assertEqual(self.names(), ["TC One", "tc three", "tc two"])
        result = json.loads(next((get_paths().runs).glob("*/*-apply/result.json")).read_text())
        self.assertGreaterEqual(result["api"]["hcsc"]["network_interruptions"], 1)
        self.assertEqual(result["counts"]["testcases"].get("failed", 0), 0)

    def test_a_long_outage_stops_cleanly_and_resumes_without_duplicates(self):
        run_id = self.plan()
        self.tgt.drop_next = 10_000
        self.assertEqual(self.run_cli("apply", "--plan", run_id, "--yes", "--network-retries", "2", *FAST), 75)
        self.assertEqual(self.last_status(), "interrupted")
        self.assertTrue(next(get_paths().runs.glob("*/*-apply/interrupted.json")).is_file())
        self.tgt.drop_next = 0  # the network is back
        self.assertEqual(self.run_cli("apply", "--plan", run_id, "--yes", *FAST), 0)
        self.assertEqual(self.names(), ["TC One", "tc three", "tc two"])
        self.assertEqual(sorted(m["name"] for m in self.tgt.projects[2]["modules"].values()), ["A", "A1", "B"])

    def test_a_lost_reply_is_settled_at_once_and_created_only_once(self):
        run_id = self.plan()
        self.tgt.drop_reply_for_names = {"tc two"}
        self.assertEqual(self.run_cli("apply", "--plan", run_id, "--yes", *FAST), 0)
        self.assertEqual(self.names(), ["TC One", "tc three", "tc two"])  # the first write counted, no second one

    def test_a_write_cut_off_by_a_long_outage_is_adopted_on_the_next_run(self):
        for name, kind in (("tc two", "testcase"), ("A1", "folder")):
            with self.subTest(name):
                self.setUp()
                run_id = self.plan()
                self.tgt.drop_reply_for_names = {name}
                self.tgt.outage_after_lost_reply = 10_000
                self.assertEqual(self.run_cli("apply", "--plan", run_id, "--yes", "--network-retries", "2", *FAST), 75)
                self.tgt.drop_next = 0
                self.assertEqual(self.run_cli("apply", "--plan", run_id, "--yes", *FAST), 0)
                self.assertEqual(self.names(), ["TC One", "tc three", "tc two"])
                self.assertEqual(sorted(m["name"] for m in self.tgt.projects[2]["modules"].values()), ["A", "A1", "B"])

    def test_two_candidates_after_a_cut_off_write_are_not_guessed(self):
        run_id = self.plan()
        self.tgt.drop_reply_for_names = {"tc two"}
        self.tgt.outage_after_lost_reply = 10_000
        self.assertEqual(self.run_cli("apply", "--plan", run_id, "--yes", "--network-retries", "2", *FAST), 75)
        self.tgt.drop_next = 0
        folder = next(t["parent_id"] for t in self.tgt.projects[2]["testcases"].values() if t["name"] == "tc two")
        self.tgt.add_testcase(2, folder, "tc two")  # somebody else made another one meanwhile
        self.assertEqual(self.run_cli("apply", "--plan", run_id, "--yes", *FAST), 1)
        self.assertEqual(self.names().count("tc two"), 2)  # nothing new was created
        result = json.loads(sorted(get_paths().runs.glob("*/*-apply/result.json"))[-1].read_text())
        failed = [r for r in result["testcases"] if r["status"] == "failed"]
        self.assertEqual(len(failed), 1)
        self.assertIn("check them in the target", failed[0]["error"])

    def test_an_interrupted_run_shows_in_the_index_and_status_with_its_resume_command(self):
        run_id = self.plan()
        self.tgt.drop_next = 10_000
        self.assertEqual(self.run_cli("apply", "--plan", run_id, "--yes", "--network-retries", "2", *FAST), 75)
        runs = get_paths().runs
        index = (runs / "index.csv").read_text(encoding="utf-8-sig")
        self.assertIn("interrupted", index)
        status = next(runs.glob("*/status.md")).read_text(encoding="utf-8")
        self.assertIn("Interrupted: run again to continue", status)
        self.assertIn(f"python -m qmig apply --plan {run_id}", status)
        self.tgt.drop_next = 0
        self.assertEqual(self.run_cli("apply", "--plan", run_id, "--yes", *FAST), 0)
        self.assertNotIn("Interrupted: run again", next(runs.glob("*/status.md")).read_text(encoding="utf-8"))  # resumed: gone

    def test_a_plan_cut_off_by_the_network_is_marked_interrupted_too(self):
        self.tgt.drop_next = 10_000
        self.assertEqual(self.run_cli("plan", "--source-project", "1", "--target-project", "2", "--network-retries", "1", *FAST), 75)
        self.assertIn("interrupted", (get_paths().runs / "index.csv").read_text(encoding="utf-8-sig"))

    def test_with_zero_retries_a_connection_error_fails_at_once(self):
        self.tgt.drop_next = 10_000
        self.assertEqual(self.run_cli("plan", "--source-project", "1", "--target-project", "2", "--network-retries", "0"), 2)  # preflight cannot reach the target


class EveryKindTest(unittest.TestCase):
    """A reply lost on any kind of write, followed by a long outage, must end with the same target as an undisturbed run."""
    setUp = test_qmig.EndToEndTest.setUp
    run_cli = test_qmig.EndToEndTest.run_cli
    plan = test_qmig.EndToEndTest.plan
    locals().update({n: getattr(test_qmig.EndToEndTest, n) for n in dir(test_qmig.EndToEndTest) if n.startswith("_add_")})

    FLAGS = ("--logs", "--attachments", "--step-files", "--approve", "all")

    def scenario(self):
        self._add_logs()
        self._add_attachments()
        self._add_step_files()
        two = next(r for r in self.src.projects[1]["runs"] if r["name"] == "run two")
        two["attachments"] = [{"id": 5001, "name": "run-notes.txt", "content_type": "text/plain", "data": b"notes"}]
        self.tgt.projects[2]["testcases"].clear()
        self.tgt.projects[2]["statuses"] = [{"id": 801, "name": "passed"}, {"id": 802, "name": "failed"}]

    def snapshot(self):
        p = self.tgt.projects[2]
        names = lambda items: sorted(i["name"] for i in items)
        holders = {}
        for t in p["testcases"].values():
            holders[("tc", t["name"])] = names(t.get("attachments", []))
            for st in t.get("test_steps", []):
                if st.get("attachments"):
                    holders[("step", t["name"], st["order"])] = names(st["attachments"])
        for r in p["runs"]:
            holders[("run", r["name"])] = names(r.get("attachments", []))
        runs = {r["id"]: r["name"] for r in p["runs"]}
        logs = sorted((runs.get(l["run_id"]), l["exe_start_date"], tuple(names(l.get("attachments", [])))) for l in p["logs"])
        return {"releases": names(p["releases"]), "builds": names(p["builds"]), "cycles": names(p["cycles"]),
                "suites": names(p["suites"]), "runs": names(p["runs"]), "logs": logs, "holders": holders,
                "testcases": names(p["testcases"].values()), "approved": sorted(t["name"] for t in p["testcases"].values()
                                                                               if str(t.get("version", "")).endswith(".0"))}

    def apply_all(self, *extra):
        run_id = self.plan(*self.FLAGS)
        return run_id, self.run_cli("apply", "--plan", run_id, "--yes", *FAST, *extra)

    def test_every_kind_of_write(self):
        self.scenario()
        _, code = self.apply_all()
        self.assertEqual(code, 0)
        baseline = self.snapshot()
        self.assertTrue(baseline["logs"] and baseline["builds"] and baseline["suites"] and baseline["holders"])
        markers = {
            "release": r"POST .*/releases$", "build": r"POST .*/builds$", "cycle": r"POST .*/test-cycles$",
            "suite": r"POST .*/test-suites$", "run": r"POST .*/test-runs$", "log": r"POST .*/test-runs/\d+/test-logs$",
            "test case file": r"POST .*/test-cases/\d+/attachments$", "step file": r"POST .*/test-steps/\d+/attachments$",
            "run file": r"POST .*/test-runs/\d+/attachments$", "log file": r"POST .*/test-logs/\d+/attachments$",
        }
        for label, pattern in markers.items():
            with self.subTest(label):
                self.setUp()
                self.scenario()
                self.tgt.lose_reply_of = [pattern]
                self.tgt.outage_after_lost_reply = 10_000
                run_id = self.plan(*self.FLAGS)
                code = self.run_cli("apply", "--plan", run_id, "--yes", "--network-retries", "2", *FAST)
                self.assertEqual(code, 75, "the cut-off write must interrupt the run")
                self.tgt.drop_next = 0
                self.assertEqual(self.run_cli("apply", "--plan", run_id, "--yes", *FAST), 0)
                self.assertEqual(self.snapshot(), baseline)

    def test_a_lost_approval_reply_is_simply_repeated(self):
        self.scenario()
        self.tgt.lose_reply_of = [r"PUT .*/approve$"]
        _, code = self.apply_all()
        self.assertEqual(code, 0)
        self.assertTrue(self.snapshot()["approved"])
