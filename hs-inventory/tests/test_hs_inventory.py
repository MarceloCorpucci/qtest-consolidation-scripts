import json
import os
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError
from unittest import mock
from unittest.mock import patch

from hs_inventory.export_inventory import (
    Client, InventoryError, NoRedirect, export, extract_jira, load_credentials, load_dotenv,
)
from hs_inventory.build_project_inventory import (
    classify_size, count_execution_hierarchy, count_modules, fetch_unpaged,
    render_markdown,
)


class InventoryTests(unittest.TestCase):
    def test_project_size_bands_use_total_entity_count(self):
        self.assertEqual(classify_size(0), "XS")
        self.assertEqual(classify_size(1), "S")
        self.assertEqual(classify_size(5000), "S")
        self.assertEqual(classify_size(5001), "M")
        self.assertEqual(classify_size(25000), "M")
        self.assertEqual(classify_size(25001), "L")

    def test_parent_scoped_response_is_checked_without_following_repeated_page(self):
        client = Client("https://example.test/api/v3", "test")
        with patch.object(client, "get", return_value=([{"id": 1, "name": "One"}], None)) as get:
            rows = fetch_unpaged(client, "/projects/1/test-suites", {"parentId": 2})
        self.assertEqual([row["id"] for row in rows], [1])
        self.assertEqual(get.call_count, 1)
        self.assertEqual(get.call_args.args[1]["pageSize"], 999)

    def test_large_parent_response_repeated_on_next_page_is_counted_once(self):
        client = Client("https://example.test/api/v3", "test")
        items = [{"id": index} for index in range(999)]
        with patch.object(client, "get", side_effect=[(items, None), (items, None)]) as get:
            rows = fetch_unpaged(client, "/projects/1/modules", {"parentId": 2})
        self.assertEqual(len(rows), 999)
        self.assertEqual(get.call_count, 2)

    def test_large_parent_response_collects_distinct_pages(self):
        client = Client("https://example.test/api/v3", "test")
        first_page = [{"id": index} for index in range(999)]
        with patch.object(client, "get", side_effect=[
            (first_page, None), ([{"id": 999}], None),
        ]) as get:
            rows = fetch_unpaged(client, "/projects/1/modules", {"parentId": 2})
        self.assertEqual(len(rows), 1000)
        self.assertEqual(get.call_count, 2)

    def test_module_count_walks_expanded_descendant_trees_once(self):
        client = Client("https://example.test/api/v3", "test")
        with patch(
            "hs_inventory.build_project_inventory.fetch_unpaged",
            side_effect=[
                [{"id": 10}, {"id": 20}],
                [{"id": 11, "children": [{"id": 12}]}],
                [],
            ],
        ) as fetch:
            self.assertEqual(count_modules(client, 1), 4)
        self.assertEqual(fetch.call_count, 3)
        self.assertEqual(fetch.call_args_list[1].args[2]["expand"], "descendants")

    def test_execution_counts_include_nested_cycles_suites_and_runs(self):
        client = Client("https://example.test/api/v3", "test")
        with patch(
            "hs_inventory.build_project_inventory.fetch_unpaged",
            side_effect=[
                [{"id": 10}],
                [{"id": 11}], [], [{"id": 20}], [{"id": 30}, {"id": 31}],
                [], [{"id": 33}], [{"id": 21}], [{"id": 32}],  # 33: a run directly under a cycle
            ],
        ):
            counts = count_execution_hierarchy(client, 1)
        self.assertEqual(counts, {
            "test_cycle_count": 2, "test_suite_count": 2, "test_run_count": 4,
        })

    def test_inventory_markdown_contains_artifact_totals_and_caveats(self):
        rows = [{
            "project_id": 1, "project_name": "Small Project", "size": "S",
            "requirement_count": 20, "module_count": 2, "testcase_count": 3,
            "test_step_count": 4, "test_cycle_count": 1, "test_suite_count": 2,
            "test_run_count": 5, "release_count": 1,
        }]
        report = render_markdown(rows, {"completed_at": "snapshot", "source": "HS"}, "counted")
        self.assertIn("| S | 1 | 38 | 20 | 2 | 3 | 4 | 1 | 2 | 5 | 1 |", report)
        self.assertIn("XS = 0, S = 1-5,000, M = 5,001-25,000, and L = over 25,000", report)
        self.assertIn("Execution logs, step-level execution logs, and attachments", report)

    def test_credentials_come_from_the_hs_environment_variables_only(self):
        with mock.patch("hs_inventory.export_inventory.load_dotenv"), mock.patch.dict(os.environ, {
            "QMIG_HS_URL": "https://healthspring.qtestnet.com/api/v3/",
            "QMIG_HS_TOKEN": "Bearer abc=def", "QMIG_HCSC_TOKEN": "other"}, clear=True):
            self.assertEqual(load_credentials(), ("https://healthspring.qtestnet.com/api/v3", "abc=def"))
        with mock.patch("hs_inventory.export_inventory.load_dotenv"), mock.patch.dict(os.environ, {
                "QMIG_HS_URL": "https://other.example/api/v3", "QMIG_HS_TOKEN": "abc"}, clear=True):
            with self.assertRaises(InventoryError):
                load_credentials()
        with mock.patch("hs_inventory.export_inventory.load_dotenv"), mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(InventoryError):
                load_credentials()

    def test_dotenv_keeps_existing_variables_and_equals_in_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("# c\nexport QMIG_HS_TOKEN='a=b'\nQMIG_HS_URL=https://x\n")
            with mock.patch.dict(os.environ, {"QMIG_HS_URL": "kept"}, clear=True):
                load_dotenv(path)
                self.assertEqual((os.environ["QMIG_HS_TOKEN"], os.environ["QMIG_HS_URL"]), ("a=b", "kept"))

    def test_external_jira_key_beats_name_and_qtest_pid(self):
        result = extract_jira({
            "id": 10, "pid": "RQ-10", "name": "OTHER-123 misleading title",
            "external_properties": {"id": "ABC-42", "link": "https://jira.example/browse/ABC-42"},
            "properties": [
                {"field_name": "Issue Type", "field_value": "10001", "field_value_name": "User Story"},
                {"field_name": "Type", "field_value_name": "Functional"},
            ],
        })
        self.assertEqual(result["issue_key"], "ABC-42")
        self.assertEqual(result["issue_key_source"], "external_properties.id")
        self.assertEqual(result["issue_type"], "User Story")
        self.assertFalse(result["needs_review"])

    def test_name_fallback_is_labeled_and_does_not_invent_type(self):
        result = extract_jira({"name": "User Story: ABC-1 Full title"})
        self.assertEqual(result["issue_key"], "ABC-1")
        self.assertEqual(result["issue_type"], "User Story")
        self.assertEqual(result["issue_type_source"], "name_prefix")
        result = extract_jira({"name": "ABC-1 As a user I want to see a story"})
        self.assertIsNone(result["issue_type"])
        self.assertTrue(result["needs_review"])
        self.assertIsNone(result["issue_summary"])

    def test_conflicting_keys_are_not_silently_selected(self):
        result = extract_jira({
            "name": "Title", "external_properties": {
                "id": "ABC-1", "link": "https://jira.example/browse/ABC-2",
            },
        })
        self.assertIsNone(result["issue_key"])
        self.assertEqual(len(result["issue_key_candidates"]), 2)

    def test_numeric_issue_type_is_not_a_name(self):
        result = extract_jira({"name": "Title", "issueType": "10001"})
        self.assertIsNone(result["issue_type"])

    def test_no_false_positive_type_in_ordinary_title(self):
        for name in ("Story about access", "Bug fixes for reports", "Task management"):
            self.assertIsNone(extract_jira({"name": name})["issue_type"])

    def test_pagination_continues_past_server_page_cap(self):
        client = Client("https://example.test/api/v3", "test", 200)
        with patch.object(client, "get", side_effect=[
            ([{"id": 1, "name": "A"}], None),
            ([{"id": 2, "name": "B"}], None), ([], None),
        ]) as get:
            self.assertEqual(len(client.collect("/projects")), 2)
        self.assertEqual(get.call_count, 3)

    def test_pagination_uses_total_and_parameters(self):
        client = Client("https://example.test/api/v3", "test")
        with patch.object(client, "get", return_value=({"items": [{"id": 1, "name": "A"}], "total": 1}, None)) as get:
            self.assertEqual(len(client.collect("/projects")), 1)
        self.assertEqual(get.call_args.args[1]["page"], 1)

    def test_bad_or_incomplete_pages_fail(self):
        cases = (
            [([{"id": 1, "name": "A"}], None), ([{"id": 1, "name": "A"}], None)],
            [([{"id": 1, "name": "A"}], 2), ([], 2)],
            [([{"id": 1, "name": "A"}], 3), ([], 4)],
            [({"unexpected": []}, None)],
            [([{"name": "missing ID"}], None)],
        )
        for pages in cases:
            with self.subTest(pages=pages):
                client = Client("https://example.test/api/v3", "test")
                with patch.object(client, "get", side_effect=pages):
                    with self.assertRaises(InventoryError):
                        client.collect("/projects")

    def test_redirect_is_refused(self):
        with self.assertRaises(InventoryError):
            NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.example")

    def test_get_authorization_total_and_retry(self):
        client = Client("https://example.test/api/v3", "private-token")
        response = BytesIO(b'[{"id": 1, "name": "One"}]')
        response.headers = {"X-Item-Total": "1"}
        error = HTTPError("https://example.test", 429, "", {"Retry-After": "1"}, BytesIO())
        with patch.object(client.opener, "open", side_effect=[error, response]) as open_request, \
                patch("hs_inventory.export_inventory.time.sleep") as sleep:
            payload, total = client.get("/projects", {"page": 1})
        self.assertEqual(total, 1)
        self.assertEqual(payload[0]["id"], 1)
        request = open_request.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer private-token")
        self.assertEqual(request.get_method(), "GET")
        sleep.assert_called_once_with(1)

    def test_forbidden_request_fails_without_retry_or_response_body(self):
        client = Client("https://example.test/api/v3", "test")
        error = HTTPError("https://example.test", 403, "", {}, BytesIO(b"private details"))
        with patch.object(client.opener, "open", side_effect=error) as open_request:
            with self.assertRaisesRegex(InventoryError, "^GET /projects: HTTP 403$"):
                client.get("/projects", {})
        self.assertEqual(open_request.call_count, 1)

    def test_export_output_shape_counts_and_raw_data(self):
        client = Client("https://example.test/api/v3", "secret")
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "export"
            with patch.object(client, "collect", side_effect=[
                [{"id": 1, "name": "One"}, {"id": 2, "name": "Empty"}],
                [{"id": 101, "name": "User Story: ABC-1 Full title", "pid": "RQ-1"}], [],
            ]) as collect:
                manifest = export(client, out)
            self.assertEqual(manifest["status"], "complete")
            self.assertEqual(manifest["project_count"], 2)
            self.assertEqual(manifest["requirement_count"], 1)
            self.assertEqual(collect.call_args_list[1].args[1]["includeExternalProperties"], "true")
            projects = json.loads((out / "projects.json").read_text())
            self.assertEqual([row["requirement_count"] for row in projects], [1, 0])
            requirements = json.loads((out / "requirements.json").read_text())
            self.assertEqual(requirements[0]["project_id"], 1)
            self.assertEqual(requirements[0]["requirement_id"], 101)
            self.assertTrue((out / "projects.csv").exists())
            self.assertTrue((out / "requirements.csv").exists())
            self.assertTrue((out / "raw/requirements_2.json").exists())
            self.assertNotIn("secret", (out / "manifest.json").read_text())
            with self.assertRaises(FileExistsError):
                export(client, out)

    def test_partial_run_does_not_claim_success(self):
        client = Client("https://example.test/api/v3", "test")
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "export"
            with patch.object(client, "collect", side_effect=InventoryError("HTTP 403")):
                with self.assertRaises(InventoryError):
                    export(client, out)
            self.assertEqual(json.loads((out / "manifest.json").read_text())["status"], "in_progress")


if __name__ == "__main__":
    unittest.main()
