import unittest
import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
from unittest.mock import Mock, patch

from scripts import calculate_scores


class FetchExternalRepoIssuesTests(unittest.TestCase):
    @staticmethod
    def response(issues, has_next_page=False, status_code=200):
        response = Mock()
        response.status_code = status_code
        response.json.return_value = issues
        response.links = {"next": {"url": "next-page"}} if has_next_page else {}
        response.text = ""
        return response

    @staticmethod
    def details(node_id):
        return {
            "id": node_id,
            "comments": {
                "nodes": [
                    {"createdAt": "2099-01-01T00:00:00Z", "author": {"login": "user-1"}},
                    {"createdAt": "2099-01-02T00:00:00Z", "author": {"login": "user-2"}}
                ]
            }
        }

    @patch("scripts.calculate_scores.fetch_issue_graphql_details")
    @patch("scripts.calculate_scores.client.request")
    def test_paginates_beyond_100_issues_without_result_cap(self, get, details):
        first_page = [
            {"number": number, "node_id": f"issue-{number}", "labels": [], "comments": 2}
            for number in range(1, 101)
        ]
        second_page = [{"number": 101, "node_id": "issue-101", "labels": [], "comments": 2}]
        get.side_effect = [
            self.response(first_page, has_next_page=True),
            self.response(second_page)
        ]
        details.side_effect = lambda ids: {node_id: self.details(node_id) for node_id in ids}

        issues = calculate_scores.fetch_external_repo_issues(
            "microsoft/windows-containers"
        )

        self.assertEqual(len(issues), 101)
        self.assertEqual(details.call_count, 11)
        self.assertTrue(all(len(call.args[0]) <= 10 for call in details.call_args_list))
        self.assertEqual(get.call_count, 2)
        self.assertEqual(get.call_args_list[0].kwargs["params"]["page"], 1)
        self.assertEqual(get.call_args_list[1].kwargs["params"]["page"], 2)
        self.assertEqual(
            get.call_args_list[0].kwargs["params"]["since"],
            calculate_scores.ISSUE_LOOKBACK_TIMESTAMP
        )
        self.assertEqual(calculate_scores.ISSUE_LOOKBACK_DAYS, 1825)

    @patch("scripts.calculate_scores.fetch_issue_graphql_details")
    @patch("scripts.calculate_scores.client.request")
    def test_aks_accepts_only_windows_or_win_labels_case_insensitively(
        self, get, details
    ):
        get.return_value = self.response([
            {"number": 1, "node_id": "windows", "labels": [{"name": "WIndows"}]},
            {"number": 2, "node_id": "win", "labels": [{"name": "wIN"}]},
            {
                "number": 3,
                "node_id": "windows-server",
                "labels": [{"name": "windows-server"}]
            },
            {"number": 4, "node_id": "darwin", "labels": [{"name": "darwin"}]}
        ])
        details.side_effect = lambda ids: {node_id: self.details(node_id) for node_id in ids}

        issues = calculate_scores.fetch_external_repo_issues("Azure/AKS")

        self.assertEqual([issue["id"] for issue in issues], ["windows", "win"])
        self.assertEqual(details.call_count, 1)
        details.assert_called_once_with(["windows", "win"])

    @patch("scripts.calculate_scores.fetch_issue_graphql_details")
    @patch("scripts.calculate_scores.client.request")
    def test_skips_pull_requests_and_low_total_comment_counts(self, get, details):
        get.return_value = self.response([
            {"node_id": "pr", "pull_request": {}, "comments": 100},
            {"node_id": "quiet", "comments": 1},
            {"node_id": "active", "comments": 2, "labels": []},
        ])
        details.return_value = {"active": self.details("active")}
        issues = calculate_scores.fetch_external_repo_issues("microsoft/windows-containers")
        self.assertEqual([issue["id"] for issue in issues], ["active"])
        details.assert_called_once_with(["active"])

    @patch("scripts.calculate_scores.fetch_issue_graphql_details")
    @patch("scripts.calculate_scores.client.request")
    def test_total_comments_do_not_replace_recent_activity_check(self, get, details):
        get.return_value = self.response([{"number": 1, "node_id": "old", "comments": 100}])
        old = self.details("old")
        for comment in old["comments"]["nodes"]:
            comment["createdAt"] = "2000-01-01T00:00:00Z"
        details.return_value = {"old": old}
        self.assertEqual(calculate_scores.fetch_external_repo_issues("microsoft/windows-containers"), [])

    @patch("scripts.calculate_scores.client.request", side_effect=RuntimeError("rate limit"))
    def test_rest_failure_is_not_treated_as_empty_repository(self, request):
        with self.assertRaisesRegex(RuntimeError, "rate limit"):
            calculate_scores.fetch_external_repo_issues("microsoft/windows-containers")

    @patch("scripts.calculate_scores.run_graphql")
    def test_batch_details_map_by_id_not_response_order(self, graphql):
        graphql.return_value = {"nodes": [self.details("b"), self.details("a")]}
        details = calculate_scores.fetch_issue_graphql_details(["a", "b"])
        self.assertEqual(set(details), {"a", "b"})
        self.assertEqual(graphql.call_args.args[1], {"ids": ["a", "b"]})
        self.assertIn("comments(last: 100)", graphql.call_args.args[0])

    @patch("scripts.calculate_scores.run_graphql")
    def test_missing_batch_node_aborts_collection(self, graphql):
        graphql.return_value = {"nodes": [self.details("a"), None]}
        with self.assertRaisesRegex(RuntimeError, "incomplete issue details"):
            calculate_scores.fetch_issue_graphql_details(["a", "b"])


class SyncToGithubProjectTests(unittest.TestCase):
    @patch("scripts.calculate_scores.run_graphql")
    def test_adds_issue_with_supported_mutation_then_updates_score(self, graphql):
        graphql.side_effect = [
            {"addProjectV2ItemById": {"item": {"id": "item-1", "type": "ISSUE"}}},
            {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "item-1"}}}
        ]

        calculate_scores.sync_to_github_project("project-1", "field-1", "repo-field", "issue-1", "example/repo", 11.5)

        self.assertEqual(graphql.call_count, 2)
        add_call, update_call = graphql.call_args_list
        self.assertIn(
            "addProjectV2ItemById(input: {projectId: $projectId, contentId: $contentId})",
            add_call.args[0]
        )
        self.assertNotIn("addProjectV2ItemByNodeId", add_call.args[0])
        self.assertEqual(add_call.args[1], {
            "projectId": "project-1", "contentId": "issue-1"
        })
        self.assertIn("updateProjectV2ItemFieldValue(", update_call.args[0])
        self.assertIn("value: { number: $scoreValue }", update_call.args[0])
        self.assertIn("value: { text: $textValue }", update_call.args[0])
        self.assertEqual(update_call.args[1], {
            "projectId": "project-1",
            "itemId": "item-1",
            "scoreFieldId": "field-1",
            "scoreValue": 11.5,
            "repoFieldId": "repo-field",
            "textValue": "example/repo",
            "updateScore": True,
            "updateSource": True,
        })

    @patch("scripts.calculate_scores.run_graphql")
    def test_add_failure_does_not_attempt_score_update(self, graphql):
        graphql.side_effect = Exception("GraphQL Errors: undefinedField")

        with self.assertRaisesRegex(Exception, "undefinedField"):
            calculate_scores.sync_to_github_project("project-1", "field-1", "repo-field", "issue-1", "example/repo", 11.5)

        graphql.assert_called_once()

    @patch("scripts.calculate_scores.run_graphql")
    def test_unchanged_item_requires_no_mutations(self, graphql):
        existing = {"id": "item-1", "score": {"number": 11.5}, "source": {"text": "example/repo"}}
        calculate_scores.sync_to_github_project(
            "project-1", "field-1", "repo-field", "issue-1", "example/repo", 11.5, existing
        )
        graphql.assert_not_called()

    @patch("scripts.calculate_scores.run_graphql")
    def test_changed_score_reuses_item_and_skips_source_update(self, graphql):
        existing = {"id": "item-1", "score": {"number": 10}, "source": {"text": "example/repo"}}
        calculate_scores.sync_to_github_project(
            "project-1", "field-1", "repo-field", "issue-1", "example/repo", 11.5, existing
        )
        graphql.assert_called_once()
        variables = graphql.call_args.args[1]
        self.assertEqual(variables["itemId"], "item-1")
        self.assertTrue(variables["updateScore"])
        self.assertFalse(variables["updateSource"])


class ProjectReconciliationTests(unittest.TestCase):
    @staticmethod
    def item(node_id, repo="example/repo", source=None):
        return {
            "id": f"item-{node_id}",
            "content": {"id": node_id, "repository": {"nameWithOwner": repo}},
            "source": {"text": repo if source is None else source},
        }

    @patch("scripts.calculate_scores.run_graphql")
    def test_snapshots_all_project_pages(self, graphql):
        graphql.side_effect = [
            {"node": {"items": {
                "nodes": [self.item("a")],
                "pageInfo": {"hasNextPage": True, "endCursor": "page-2"},
            }}},
            {"node": {"items": {
                "nodes": [self.item("b")],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }}},
        ]
        items = calculate_scores.get_project_items("project")
        self.assertEqual([item["content"]["id"] for item in items], ["a", "b"])
        self.assertIsNone(graphql.call_args_list[0].args[1]["after"])
        self.assertEqual(graphql.call_args_list[1].args[1]["after"], "page-2")

    @patch("scripts.calculate_scores.run_graphql")
    def test_nonadvancing_project_cursor_fails(self, graphql):
        graphql.return_value = {"node": {"items": {
            "nodes": [], "pageInfo": {"hasNextPage": True, "endCursor": None},
        }}}
        with self.assertRaisesRegex(RuntimeError, "pagination did not advance"):
            calculate_scores.get_project_items("project")

    @patch.object(calculate_scores, "TARGET_REPOS", ["example/repo"])
    @patch("scripts.calculate_scores.get_project_items")
    @patch("scripts.calculate_scores.sync_to_github_project")
    @patch("scripts.calculate_scores.run_graphql")
    def test_reuses_items_and_deletes_only_stale_managed_issues_last(self, graphql, sync, snapshot):
        retained = self.item("retained")
        snapshot.return_value = [
            retained, self.item("stale"), self.item("unmonitored", "other/repo"),
            self.item("manual", source=""), {"id": "draft", "content": {}},
            {"id": "deleted", "content": None},
        ]
        events = []
        sync.side_effect = lambda *args, **kwargs: events.append("sync")
        graphql.side_effect = lambda *args, **kwargs: events.append("delete")
        desired = [({"id": "retained", "number": 1, "repo_full_name": "example/repo"}, 10)]
        calculate_scores.reconcile_project("project", "score", "source", desired)
        self.assertEqual(events, ["sync", "delete"])
        self.assertEqual(sync.call_args.kwargs["existing_item"], retained)
        graphql.assert_called_once()
        self.assertEqual(graphql.call_args.args[1]["itemId"], "item-stale")

    @patch.object(calculate_scores, "TARGET_REPOS", ["example/repo"])
    @patch("scripts.calculate_scores.get_project_items")
    @patch("scripts.calculate_scores.sync_to_github_project", side_effect=RuntimeError("rate limit"))
    @patch("scripts.calculate_scores.run_graphql")
    def test_failed_sync_does_not_delete_stale_items(self, graphql, sync, snapshot):
        snapshot.return_value = [self.item("stale")]
        desired = [({"id": "new", "number": 1, "repo_full_name": "example/repo"}, 10)]
        with self.assertRaisesRegex(RuntimeError, "rate limit"):
            calculate_scores.reconcile_project("project", "score", "source", desired)
        graphql.assert_not_called()

    @patch.object(calculate_scores, "TARGET_REPOS", ["example/one", "example/two"])
    @patch("scripts.calculate_scores.get_project_and_field_ids", return_value=("project", "score", "source"))
    @patch("scripts.calculate_scores.fetch_external_repo_issues")
    @patch("scripts.calculate_scores.reconcile_project")
    @patch("scripts.calculate_scores.publish_weekly_digest")
    @patch("scripts.calculate_scores.write_project_snapshot")
    @patch("scripts.calculate_scores.write_scored_issues")
    def test_collection_failure_never_changes_board_or_publishes_digest(
        self, export, snapshot, digest, reconcile, fetch, discovery
    ):
        fetch.side_effect = [
            [{"id": "one", "repo_full_name": "example/one"}],
            RuntimeError("rate limit"),
        ]
        with self.assertRaisesRegex(RuntimeError, "rate limit"):
            calculate_scores.main()
        reconcile.assert_not_called()
        digest.assert_not_called()
        snapshot.assert_not_called()
        export.assert_not_called()

    @patch.object(calculate_scores, "TARGET_REPOS", ["example/one", "example/two"])
    @patch("scripts.calculate_scores.get_project_and_field_ids", return_value=("project", "score", "source"))
    @patch("scripts.calculate_scores.fetch_external_repo_issues")
    @patch("scripts.calculate_scores.reconcile_project")
    @patch("scripts.calculate_scores.publish_weekly_digest")
    @patch("scripts.calculate_scores.write_project_snapshot")
    @patch("scripts.calculate_scores.write_scored_issues")
    def test_collects_before_sync_then_publishes_digest(
        self, export, snapshot, digest, reconcile, fetch, discovery
    ):
        events = []
        issues = [{"id": str(n), "repo_full_name": "example/one", "raw_label_names": []} for n in range(12)]
        fetch.side_effect = lambda repo: events.append(repo) or issues
        reconcile.side_effect = lambda *args: events.append("sync")
        digest.side_effect = lambda *args: events.append("digest")
        snapshot.side_effect = lambda *args: events.append("snapshot")
        export.side_effect = lambda *args: events.append("export")
        with patch.dict("os.environ", {"PUBLISH_WEEKLY_DIGEST": "true"}):
            calculate_scores.main()
        self.assertEqual(
            events,
            ["example/one", "example/two", "sync", "digest", "snapshot", "export"],
        )
        self.assertEqual(len(reconcile.call_args.args[3]), 20)
        self.assertTrue(all(len(top) == 3 for top in digest.call_args.args[0].values()))
        self.assertEqual(len(export.call_args.args[0]), 24)

    def test_exports_scored_issue_identity_and_score_without_project_cap(self):
        issues = [
            ({"id": str(n), "number": n, "title": f"Nano Server {n}",
              "url": f"https://github.com/example/repo/issues/{n}",
              "repo_full_name": "example/repo"}, float(n))
            for n in range(12)
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scoring-output" / "scored-issues.json"
            self.assertEqual(calculate_scores.write_scored_issues(issues, path), path)
            records = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(len(records), 12)
        self.assertEqual(records[-1], {**issues[-1][0], "score": 11.0})

    def test_exports_empty_scored_list(self):
        with tempfile.TemporaryDirectory() as directory:
            path = calculate_scores.write_scored_issues([], Path(directory) / "issues.json")
            self.assertEqual(json.loads(path.read_text()), [])

    def test_scoring_weights_and_recent_comment_sample_unchanged(self):
        issue = {
            "repo_full_name": "microsoft/windows-containers",
            "raw_label_names": ["bug", "customer-reported"],
            "comments": {"nodes": [
                {"author": {"login": "a"}}, {"author": {"login": "a"}},
                {"author": {"login": "b"}}, {"author": None},
            ]},
        }
        self.assertEqual(calculate_scores.compute_priority_score(issue), 125)


class ProjectSnapshotTests(unittest.TestCase):
    def test_writes_dated_pdf_with_project_and_issue_details(self):
        generated_at = datetime(2026, 10, 23, 4, 0, tzinfo=timezone.utc)
        items = [{
            "content": {
                "number": 42,
                "title": "Windows container startup",
                "url": "https://github.com/example/repo/issues/42",
                "repository": {"nameWithOwner": "example/repo"},
            },
            "score": {"number": 17.5},
        }]
        with tempfile.TemporaryDirectory() as directory:
            output_path = calculate_scores.write_project_snapshot(
                "project-id",
                items=items,
                output_directory=directory,
                generated_at=generated_at,
            )

            self.assertEqual(
                output_path,
                Path(directory) / "windows-container-issue-snapshot-2026-10-23.pdf",
            )
            pdf = output_path.read_bytes()
            self.assertTrue(pdf.startswith(b"%PDF-1.4"))
            self.assertIn(b"ProjectV2: https://github.com/orgs/msftjonw-labs/projects/1", pdf)
            self.assertIn(b"example/repo #42: Windows container startup", pdf)
            self.assertIn(b"Priority score: 17.5", pdf)
            self.assertIn(b"https://github.com/example/repo/issues/42", pdf)


class RunGraphqlTests(unittest.TestCase):
    def setUp(self):
        self.client_patch = patch.object(calculate_scores, "client", calculate_scores.GitHubClient("placeholder"))
        self.client_patch.start()
        self.addCleanup(self.client_patch.stop)

    @patch("scripts.calculate_scores.time.sleep")
    @patch("scripts.github_client.requests.request")
    def test_retries_transient_gateway_timeout(self, post, sleep):
        timeout_response = Mock()
        timeout_response.status_code = 504
        timeout_response.headers = {}
        timeout_response.text = "Gateway Timeout"
        success_response = Mock()
        success_response.status_code = 200
        success_response.headers = {}
        success_response.json.return_value = {"data": {"viewer": {"login": "user"}}}
        post.side_effect = [timeout_response, success_response]

        result = calculate_scores.run_graphql("query { viewer { login } }")

        self.assertEqual(result, {"viewer": {"login": "user"}})
        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once_with(1)

    @patch("scripts.calculate_scores.time.sleep")
    @patch("scripts.github_client.requests.request")
    def test_retries_transient_internal_error(self, post, sleep):
        transient_response = Mock()
        transient_response.status_code = 200
        transient_response.headers = {}
        transient_response.json.return_value = {
            "errors": [{"message": "Something went wrong while executing your query."}]
        }
        success_response = Mock()
        success_response.status_code = 200
        success_response.headers = {}
        success_response.json.return_value = {"data": {"viewer": {"login": "user"}}}
        post.side_effect = [transient_response, success_response]

        result = calculate_scores.run_graphql("query { viewer { login } }")

        self.assertEqual(result, {"viewer": {"login": "user"}})
        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once_with(1)

    @patch("scripts.calculate_scores.time.sleep")
    @patch("scripts.github_client.requests.request")
    def test_does_not_retry_non_transient_error(self, post, sleep):
        response = Mock()
        response.status_code = 200
        response.headers = {}
        response.json.return_value = {
            "errors": [{"message": "Resource not accessible by integration"}]
        }
        post.return_value = response

        with self.assertRaisesRegex(Exception, "Resource not accessible by integration"):
            calculate_scores.run_graphql("query { viewer { login } }")

        post.assert_called_once()
        sleep.assert_not_called()

    @patch("scripts.calculate_scores.time.sleep")
    @patch("scripts.github_client.requests.request")
    def test_stops_after_maximum_attempts(self, post, sleep):
        response = Mock()
        response.status_code = 200
        response.headers = {}
        response.json.return_value = {
            "errors": [{"message": "Something went wrong while executing your query."}]
        }
        post.return_value = response

        with self.assertRaisesRegex(Exception, "Something went wrong"):
            calculate_scores.run_graphql("query { viewer { login } }")

        self.assertEqual(post.call_count, calculate_scores.client.MAX_ATTEMPTS)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2])

if __name__ == "__main__":
    unittest.main()
