import copy
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from scripts import generate_hypothesis_report as report


DATE = "2026-10-23T12:00:00Z"


def scored(index=1):
    return {
        "id": f"I{index}", "number": index, "title": "Stale export title",
        "url": f"https://github.com/owner/repo/issues/{index}",
        "repo_full_name": "owner/repo", "score": 10 + index,
    }


def comment(body="Routine discussion"):
    return {
        "body": body, "url": "https://github.com/owner/repo/issues/1#issuecomment-1",
        "createdAt": DATE, "updatedAt": DATE,
    }


def details(kind="Issue", index=1, body="Routine maintenance", comments=None):
    result = {
        "__typename": kind, "id": f"{'I' if kind == 'Issue' else 'P'}{index}",
        "number": index, "title": "Fresh title", "body": body, "state": "OPEN",
        "updatedAt": DATE,
        "url": f"https://github.com/owner/repo/{'issues' if kind == 'Issue' else 'pull'}/{index}",
        "comments": {"totalCount": len(comments or []), "nodes": comments or []},
    }
    if kind == "Issue":
        result["repository"] = {"nameWithOwner": "owner/repo"}
    else:
        result["mergedAt"] = None
    return result


def event(kind="CrossReferencedEvent", index=1, updated=DATE, reference_type="PullRequest"):
    return {
        "__typename": kind,
        "source" if kind == "CrossReferencedEvent" else "subject": {
            "__typename": reference_type, "id": f"P{index}", "updatedAt": updated,
        },
    }


def timeline(nodes=None, next_page=False, cursor=None):
    return {"node": {"timelineItems": {
        "nodes": nodes or [],
        "pageInfo": {"hasNextPage": next_page, "endCursor": cursor},
    }}}


def record(body="Routine maintenance", pr=None, comments=None):
    return {
        "scored": scored(), "issue": details(body=body, comments=comments),
        "pr": pr, "reference_count": int(pr is not None),
    }


class SelectionAndFetchingTests(unittest.TestCase):
    def test_load_all_qualifying_issues_without_project_cap(self):
        exported = [scored(index) for index in range(1, 25)]
        with patch.object(Path, "read_text", return_value=json.dumps(exported)):
            self.assertEqual(report.load_scored_issues(Path("export.json")), exported)

    def test_invalid_exports_fail(self):
        bad = [None, {}, "text", [None], [scored(), scored()]]
        for field, value in (("id", ""), ("number", True), ("score", float("nan")),
                             ("score", "9"), ("repo_full_name", None), ("url", "")):
            item = scored()
            item[field] = value
            bad.append([item])
        for exported in bad:
            with self.subTest(exported=exported):
                with patch.object(Path, "read_text", return_value=json.dumps(exported)):
                    with self.assertRaises(ValueError):
                        report.load_scored_issues("export.json")
        with patch.object(Path, "read_text", return_value="{broken"):
            with self.assertRaises(ValueError):
                report.load_scored_issues("export.json")

    def test_pages_both_event_types_real_pr_only_dedup_and_latest(self):
        responses = [
            timeline([
                event(index=1, updated="2026-01-01T00:00:00Z"),
                event(reference_type="Issue", updated="2027-01-01T00:00:00Z"),
            ], True, "page-2"),
            timeline([
                event("ConnectedEvent", index=2),
                event(index=1, updated="2026-01-01T00:00:00Z"),
            ]),
            {"node": details("PullRequest", 2)},
        ]
        with patch.object(report, "run_graphql", side_effect=responses) as query:
            pr, count = report.latest_linked_pr("I1")
        self.assertEqual((pr["id"], count), ("P2", 2))
        self.assertEqual(query.call_count, 3)
        self.assertEqual(query.call_args_list[0].args[1], {"id": "I1", "after": None})
        self.assertEqual(query.call_args_list[1].args[1], {"id": "I1", "after": "page-2"})
        self.assertEqual(query.call_args_list[2].args[1], {"id": "P2"})
        self.assertIn("source { __typename ... on PullRequest", report.REFERENCES_QUERY)
        self.assertIn("subject { __typename ... on PullRequest", report.REFERENCES_QUERY)

    def test_tie_is_deterministic_and_missing_pr_is_normal(self):
        with patch.object(report, "run_graphql", side_effect=[
            timeline([event(index=1), event("ConnectedEvent", index=2)]),
            {"node": details("PullRequest", 2)},
        ]):
            self.assertEqual(report.latest_linked_pr("I1")[0]["id"], "P2")
        with patch.object(report, "run_graphql", return_value=timeline([event(reference_type="Issue")])) as query:
            self.assertEqual(report.latest_linked_pr("I1"), (None, 0))
        self.assertEqual(query.call_count, 1)

    def test_collect_refreshes_issue_and_pr_details_and_last100_comments(self):
        issue = details(comments=[comment("issue discussion")])
        pr = details("PullRequest", body="Current PR body", comments=[comment("PR discussion")])
        with patch.object(report, "run_graphql", side_effect=[
            {"node": issue}, timeline([event()]), {"node": pr},
        ]) as query:
            collected = report.collect_issue(scored())
        self.assertEqual(collected["issue"]["title"], "Fresh title")
        self.assertEqual(collected["pr"]["body"], "Current PR body")
        self.assertEqual(query.call_count, 3)
        self.assertIn("comments(last: 100)", report.ISSUE_QUERY)
        self.assertIn("comments(last: 100)", report.PR_QUERY)
        self.assertIn("mergedAt", report.PR_QUERY)

    def test_pr_state_and_merge_timestamp_are_preserved(self):
        pr = details("PullRequest")
        pr.update(state="MERGED", mergedAt=DATE)
        self.assertIs(report.validate_details(pr, "PullRequest", "P1"), pr)
        pr["mergedAt"] = None
        with self.assertRaises(ValueError):
            report.validate_details(pr, "PullRequest", "P1")

    def test_identity_mismatch_fails(self):
        issue = details()
        issue["repository"]["nameWithOwner"] = "other/repo"
        with patch.object(report, "run_graphql", return_value={"node": issue}):
            with self.assertRaisesRegex(ValueError, "identity"):
                report.collect_issue(scored())

    def test_repository_identity_comparison_is_case_insensitive(self):
        issue = details()
        issue["repository"]["nameWithOwner"] = "Azure/AKS"
        exported = scored()
        exported["repo_full_name"] = "azure/aks"
        with patch.object(report, "run_graphql", side_effect=[
            {"node": issue}, timeline(),
        ]) as query:
            collected = report.collect_issue(exported)
        self.assertEqual(collected["issue"]["repository"]["nameWithOwner"], "Azure/AKS")
        self.assertEqual(collected["scored"]["repo_full_name"], "azure/aks")
        self.assertIsNone(collected["pr"])
        self.assertEqual(query.call_count, 2)

    def test_api_partial_inaccessible_and_missing_data_fail(self):
        responses = [
            None, {}, {"node": None}, {"node": {}},
            {"errors": ["partial failure"], "node": details()},
        ]
        for response in responses:
            with self.subTest(response=response):
                with patch.object(report, "run_graphql", return_value=response):
                    with self.assertRaises(ValueError):
                        report.collect_issue(scored())
        with patch.object(report, "run_graphql", side_effect=RuntimeError("rate budget")):
            with self.assertRaisesRegex(RuntimeError, "rate budget"):
                report.collect_issue(scored())
        for field in ("body", "state", "comments", "updatedAt", "url"):
            issue = details()
            del issue[field]
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    report.validate_details(issue, "Issue", "I1")

    def test_incomplete_comments_and_invalid_timestamps_fail(self):
        for comments in ({"totalCount": 3, "nodes": []},
                         {"totalCount": 1, "nodes": [None]},
                         {"totalCount": 1, "nodes": [{"body": "text"}]}):
            issue = details()
            issue["comments"] = comments
            with self.assertRaises(ValueError):
                report.validate_details(issue, "Issue", "I1")
        for date in (None, "yesterday", "2026-10-23T12:00:00"):
            with self.assertRaises((ValueError, TypeError)):
                report.timestamp(date)
        issue = details(comments=[comment() for _ in range(100)])
        issue["comments"]["totalCount"] = 120
        self.assertIs(report.validate_details(issue, "Issue", "I1"), issue)

    def test_malformed_timeline_and_cursor_loops_fail(self):
        bad_pages = [
            {"node": {"timelineItems": None}},
            {"node": {"timelineItems": {"nodes": []}}},
            timeline([None]),
            timeline([{"__typename": "CrossReferencedEvent", "source": None}]),
            timeline([{"__typename": "OtherEvent"}]),
            timeline([event(updated=None)]),
            timeline([], True, None),
        ]
        for page in bad_pages:
            with self.subTest(page=page):
                with patch.object(report, "run_graphql", return_value=page):
                    with self.assertRaises(ValueError):
                        report.latest_linked_pr("I1")
        with patch.object(report, "run_graphql", return_value=timeline([], True, "same")) as query:
            with self.assertRaisesRegex(ValueError, "cursor"):
                report.latest_linked_pr("I1")
        self.assertEqual(query.call_count, 2)

    def test_inaccessible_linked_pr_fails_not_no_evidence(self):
        with patch.object(report, "run_graphql", side_effect=[timeline([event()]), {"node": None}]):
            with self.assertRaises(ValueError):
                report.latest_linked_pr("I1")


class EvidenceTests(unittest.TestCase):
    def test_no_evidence_and_no_linked_pr_still_appear(self):
        lines = "\n".join(report.build_report(
            [record()], datetime(2026, 10, 23, tzinfo=timezone.utc), "export.json"
        ))
        self.assertIn("owner/repo #1: Fresh title", lines)
        self.assertIn("Latest linked PR: none found", lines)
        self.assertEqual(lines.count("No relevant candidate evidence detected"), 2)
        for hypothesis in report.HYPOTHESES:
            self.assertIn(hypothesis, lines)
        self.assertIn("cannot establish prevalence, causality, community growth", lines)
        self.assertIn("insufficient evidence; human review required", lines)
        self.assertIn("not automated semantic truth", lines)

    def test_candidate_categories_context_urls_and_dates(self):
        body = (
            "Windows containers development licensing cost is a barrier to community adoption.\n\n"
            "Windows containers licensing is not a barrier for our development team.\n\n"
            "Windows containers are popular in our community."
        )
        signals = report.triage(record(body), 1)
        self.assertEqual([signal["category"] for signal in signals],
                         ["supporting", "contradicting", "inconclusive"])
        self.assertEqual(signals[0]["excerpt"], body.split("\n\n")[0])
        self.assertEqual(signals[0]["date"], DATE)
        self.assertEqual(signals[0]["url"], details()["url"])

    def test_nano_candidates_never_claim_growth_causality(self):
        body = (
            "Nano Server missing APIs limit developer testing.\n\n"
            "Nano Server now supports these APIs and tests pass.\n\n"
            "Nano Server development roadmap."
        )
        signals = report.triage(record(body), 2)
        self.assertEqual([signal["category"] for signal in signals],
                         ["supporting", "contradicting", "inconclusive"])
        self.assertIn("no evidence of growth", signals[0]["reason"])

    def test_negation_does_not_create_support(self):
        bodies = [
            ("Windows containers licensing cost is not a barrier to community development adoption.", 1),
            ("Windows containers are not free; we never avoid development because of community popularity.", 1),
            ("Nano Server is not missing APIs required for developer testing.", 2),
            ("Nano Server gaps never limit developer testing.", 2),
            ("Nano Server is not unsupported for testing.", 2),
        ]
        for body, hypothesis in bodies:
            with self.subTest(body=body):
                self.assertTrue(report.triage(record(body), hypothesis))
                self.assertNotIn("supporting", [
                    signal["category"] for signal in report.triage(record(body), hypothesis)
                ])

    def test_not_free_cost_barrier_is_only_a_supporting_candidate(self):
        body = (
            "Windows containers are not free, so developers avoid them; "
            "our community sees licensing cost as an adoption barrier."
        )
        signals = report.triage(record(body), 1)
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]["category"], "supporting")
        self.assertIn("not proof", signals[0]["reason"])
        self.assertEqual(signals[0]["excerpt"], body)

    def test_not_free_does_not_override_other_non_barrier_negation(self):
        for body in (
            "Windows containers are not free, but licensing cost is not a barrier "
            "to our community's development adoption.",
            "Windows containers are not free, but developers do not avoid them "
            "in our community because of cost.",
            "Windows containers are not free, but that is not a barrier "
            "for developers in our community.",
        ):
            with self.subTest(body=body):
                signals = report.triage(record(body), 1)
                self.assertTrue(signals)
                self.assertNotIn("supporting", [signal["category"] for signal in signals])

    def test_unrelated_windows_or_cost_alone_not_support(self):
        bodies = [
            "Windows licensing cost prevents community development adoption.",
            "Linux containers licensing cost prevents community development adoption.",
            "Windows containers are not free.",
            "Windows containers development requires a free Windows desktop.",
        ]
        for body in bodies:
            with self.subTest(body=body):
                self.assertNotIn("supporting", [s["category"] for s in report.triage(record(body), 1)])
        self.assertEqual(report.triage(record("Windows requires missing APIs for testing."), 2), [])

    def test_no_cross_document_keyword_join(self):
        item = record("Windows containers licensing cost.")
        item["pr"] = details("PullRequest", body="Community development adoption barrier.")
        self.assertNotIn("supporting", [s["category"] for s in report.triage(item, 1)])

    def test_pr_body_comments_included_with_source_and_context(self):
        pr = details("PullRequest", body="Nano Server missing APIs limit testing.",
                     comments=[comment("Nano Server tests pass.")])
        item = record(pr=pr)
        signals = report.triage(item, 2)
        self.assertEqual([s["source"] for s in signals],
                         ["Latest linked PR title/body", "Latest linked PR comment"])
        lines = "\n".join(report.build_report(
            [item], datetime(2026, 10, 23, tzinfo=timezone.utc), "export.json"
        ))
        for text in ("Latest linked PR #1", "PR body excerpt:", "Merged: not merged",
                     pr["url"], "PR comments inspected: 1 of 1", DATE):
            self.assertIn(text, lines)

    def test_long_context_truncation_is_marked_without_hiding_negation_from_rule(self):
        body = "Windows containers development licensing cost community adoption barrier " + "x " * 2000 + "not relevant."
        signals = report.triage(record(body), 1)
        self.assertEqual(signals[0]["category"], "inconclusive")
        self.assertIn("excerpt truncated", signals[0]["excerpt"])


class PathRenderingAndErrorsTests(unittest.TestCase):
    def test_paths_are_root_relative_and_env_override(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(report.input_path(), report.ROOT / "scoring-output/scored-issues.json")
        with patch.dict("os.environ", {"SCORED_ISSUES_PATH": "custom/export.json"}):
            self.assertEqual(report.input_path(), report.ROOT / "custom/export.json")
        absolute = report.ROOT / "other.json"
        with patch.dict("os.environ", {"SCORED_ISSUES_PATH": str(absolute)}):
            self.assertEqual(report.input_path(), absolute)

    def test_generate_fetches_every_issue_and_uses_utc_default_pdf_path(self):
        exported = [scored(i) for i in range(1, 14)]
        clock = datetime(2026, 10, 24, 1, tzinfo=timezone(timedelta(hours=3)))
        with patch.object(report, "load_scored_issues", return_value=exported) as load, \
             patch.object(report, "collect_issue", return_value=record()) as collect, \
             patch.object(Path, "mkdir") as mkdir, \
             patch.object(report, "_write_pdf") as writer:
            path = report.generate_report(generated_at=clock)
        self.assertEqual(collect.call_count, 13)
        self.assertEqual([call.args[0] for call in collect.call_args_list], exported)
        self.assertEqual(path, report.ROOT / "report-history/windows-container-hypothesis-validation-2026-10-23.pdf")
        load.assert_called_once_with(report.input_path())
        mkdir.assert_called_once_with(parents=True, exist_ok=True)
        self.assertEqual(writer.call_args.args[1], path)
        self.assertIn("2026-10-23 22:00:00 UTC", "\n".join(writer.call_args.args[0]))

    def test_empty_export_explicit_zero_sample_not_validation(self):
        with patch.object(report, "load_scored_issues", return_value=[]), \
             patch.object(Path, "mkdir"), patch.object(report, "_write_pdf") as writer:
            report.generate_report()
        lines = "\n".join(writer.call_args.args[0])
        self.assertIn("Selected issues: 0", lines)
        self.assertIn("Hypothesis 1: insufficient evidence", lines)

    def test_failures_do_not_write_a_report_and_cli_exits_nonzero(self):
        for failure in (FileNotFoundError("export missing"), ValueError("bad JSON")):
            with patch.object(report, "load_scored_issues", side_effect=failure), \
                 patch.object(Path, "mkdir") as mkdir, patch.object(report, "_write_pdf") as writer:
                with self.assertRaises(type(failure)):
                    report.generate_report()
            mkdir.assert_not_called()
            writer.assert_not_called()
        with patch.object(report, "load_scored_issues", return_value=[scored(), scored(2)]), \
             patch.object(report, "collect_issue", side_effect=[record(), RuntimeError("API failed")]), \
             patch.object(Path, "mkdir") as mkdir, patch.object(report, "_write_pdf") as writer:
            with self.assertRaisesRegex(RuntimeError, "API failed"):
                report.generate_report()
        mkdir.assert_not_called()
        writer.assert_not_called()
        with patch.object(report, "generate_report", side_effect=RuntimeError("API failed")), \
             patch("sys.stderr", new_callable=io.StringIO) as stderr:
            self.assertEqual(report.main(), 1)
        self.assertIn("Hypothesis report failed: API failed", stderr.getvalue())

    def test_naive_report_clock_rejected(self):
        with self.assertRaisesRegex(ValueError, "timezone"):
            report.generate_report(generated_at=datetime(2026, 10, 23))

    def test_pdf_rendering_uses_real_writer_in_memory_no_files(self):
        item = record(body="Nano Server missing testing APIs. (Unicode: \u2014 \u00e9)")
        item["issue"]["title"] = r"Fresh (title) \ detail"
        lines = report.build_report([copy.deepcopy(item)] * 3,
                                    datetime(2026, 10, 23, tzinfo=timezone.utc), "export.json")
        output = Mock(spec=Path)
        report._write_pdf(lines, output)
        payload = output.write_bytes.call_args.args[0]
        self.assertTrue(payload.startswith(b"%PDF-1.4"))
        self.assertTrue(payload.endswith(b"%%EOF\n"))
        self.assertIn(b"Fresh \\(title\\) \\\\ detail", payload)
        self.assertIn(b"insufficient evidence", payload)
        self.assertGreater(payload.count(b"/Type /Page "), 1)
        xref = int(payload.split(b"startxref\n")[1].splitlines()[0])
        self.assertTrue(payload[xref:].startswith(b"xref\n"))

    def test_output_errors_propagate_and_successful_cli_prints_path(self):
        with patch.object(report, "load_scored_issues", return_value=[]), \
             patch.object(Path, "mkdir"), \
             patch.object(report, "_write_pdf", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                report.generate_report(output_directory=report.ROOT / "unused-report")
        with patch.object(report, "generate_report", return_value=Path("report.pdf")), \
             patch("sys.stdout", new_callable=io.StringIO) as stdout:
            self.assertEqual(report.main(), 0)
        self.assertIn("report.pdf", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
