import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from scripts import generate_hypothesis_report as report
from scripts import hypothesis_evolution as evolution


NOW = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)


def records(count=3, body="Windows containers DNS resolution fails.", now=NOW,
            owner="community", private=False):
    result = []
    for index in range(count):
        repo = f"{owner}/repo{index}"
        node = {
            "__typename": "Issue", "id": f"I{index}", "number": index + 1,
            "title": "Windows containers", "body": body, "state": "OPEN",
            "url": f"https://github.com/{repo}/issues/1",
            "updatedAt": now.isoformat(),
            "repository": {"nameWithOwner": repo, "isPrivate": private},
            "comments": {"nodes": [], "totalCount": 0},
        }
        result.append({"issue": node, "pr": None, "reference_count": 0,
                       "scored": {"repo_full_name": repo, "score": 1}})
    return result


def evolve(data, now=NOW, previous=None):
    return evolution.evolve(data, now, previous, report.HYPOTHESES, report.triage)


class WeeklyEvolutionTests(unittest.TestCase):
    def test_zero_one_two_three_public_repositories_and_first_qualifying_week(self):
        for count in range(4):
            with self.subTest(count=count):
                state, _ = evolve(records(count))
                item = state["slots"][0]
                self.assertEqual(item["fresh_repos"], count)
                self.assertEqual(item["streak"], int(count >= 3))
                self.assertEqual(item["status"], "qualifying" if count >= 3 else "provisional" if count else "deferred")
                next_week = NOW + timedelta(weeks=1)
                state, _ = evolve(records(count, now=next_week), next_week, state)
                self.assertEqual(state["slots"][0]["streak"], 2 if count >= 3 else 0)
                self.assertEqual(state["slots"][0]["status"],
                                 "qualifying" if count >= 3 else "provisional" if count else "deferred")

    def test_microsoft_azure_case_known_and_configured_exclusions(self):
        for owner in ("MiCroSoft", "AZURE", "AKS", "dotnet", "PowerShell"):
            with self.subTest(owner=owner):
                state, lines = evolve(records(owner=owner))
                self.assertEqual(state["slots"][0]["status"], "deferred")
                self.assertIn("context only", "\n".join(lines))
        with patch.dict("os.environ", {"HYPOTHESIS_EXCLUDED_OWNERS": " COMMUNITY, Other "}):
            self.assertEqual(evolve(records())[0]["slots"][0]["status"], "deferred")

    def test_public_verification_and_full_name_not_owner_uniqueness(self):
        for private in (True, None, "false"):
            self.assertEqual(evolve(records(private=private))[0]["slots"][0]["fresh_repos"], 0)
        state, _ = evolve(records())
        self.assertEqual(state["slots"][0]["fresh_repos"], 3)  # Same owner is allowed.
        data = records()
        for item in data:
            item["issue"]["repository"]["nameWithOwner"] = "COMMUNITY/REPO0"
        self.assertEqual(evolve(data)[0]["slots"][0]["fresh_repos"], 1)

    def test_shared_crosslinked_pr_actual_repo_deduplicated(self):
        data = records(body="Routine maintenance", owner="microsoft")
        pr = records(1)[0]["issue"]
        pr["url"] = "https://github.com/community/repo0/pull/7"
        pr["id"] = "PR7"
        for item in data:
            item["pr"] = copy.deepcopy(pr)
        state, lines = evolve(data)
        self.assertEqual(state["slots"][0]["fresh_repos"], 1)
        self.assertEqual("\n".join(lines).count("Context excerpt: Windows containers DNS resolution fails."), 1)
        pr["repository"] = {"nameWithOwner": "Azure/other", "isPrivate": False}
        for item in data:
            item["pr"] = pr
        self.assertEqual(evolve(data)[0]["slots"][0]["fresh_repos"], 0)

    def test_unrelated_selected_repository_does_not_supply_windows_context(self):
        data = records(body="DNS resolution fails", owner="microsoft")
        for item in data:
            item["issue"]["title"] = "Linux Kubernetes"
        state, lines = evolve(data)
        self.assertEqual(state["slots"][0]["status"], "deferred")
        self.assertIn("networking: no detected evidence", "\n".join(lines))

    def test_negation_resolution_and_nonbarriers_are_reported_separately(self):
        for body, category in (
            ("Windows containers DNS issue is resolved.", "contradicting"),
            ("Windows containers DNS is not a barrier.", "contradicting"),
            ("Windows containers DNS is not failing.", "inconclusive"),
            ("Windows containers DNS is not broken.", "inconclusive"),
            ("Windows containers have no DNS failures.", "inconclusive"),
            ("Windows containers DNS is not fixed.", "inconclusive"),
            ("Windows containers DNS tests pass.", "contradicting"),
        ):
            with self.subTest(body=body):
                state, lines = evolve(records(body=body))
                self.assertEqual(state["slots"][0]["fresh_repos"], 0)
                self.assertIn(category, "\n".join(lines))
        data = records(body="Windows containers DNS fails.\n\nWindows containers DNS is now fixed.")
        state, lines = evolve(data)
        self.assertEqual(state["slots"][0]["fresh_repos"], 3)
        self.assertIn("contradicting", "\n".join(lines))

    def test_area_coverage_does_not_combine_different_signals_into_qualification(self):
        data = records(2) + records(2, body="Windows containers network connectivity fails.", owner="other")
        data += records(3, owner="Microsoft")
        data += records(3, body="Windows containers DNS is resolved.", owner="contrary")
        state, lines = evolve(data)
        text = "\n".join(lines)
        self.assertIn("networking: 4 distinct eligible supporting repositories; 4 fresh", text)
        self.assertIn("Area totals are not qualifications or validations", text)
        self.assertIn("networking:dns: eligible supporting repositories=2; fresh=2", text)
        self.assertIn("networking:connectivity: eligible supporting repositories=2; fresh=2", text)
        self.assertEqual(text.count("current >=3 fresh repository threshold: NOT MET"), 2)
        self.assertIn("Supporting repositories: community/repo0, community/repo1", text)
        self.assertIn("area: networking", text)
        self.assertTrue(all(s["status"] == "provisional" for s in state["slots"]))

    def test_strongest_repo_ranking_not_comments_or_scores(self):
        data = records(3) + records(2, body="Windows containers authentication fails.", owner="other")
        for index in range(10):
            data[-1]["issue"]["comments"]["nodes"].append({
                "body": "Windows containers authentication fails.", "url": f"https://github.com/other/repo1/issues/1#issuecomment-{index}",
                "updatedAt": NOW.isoformat(),
            })
        state, _ = evolve(data)
        self.assertEqual([s["key"] for s in state["slots"]], ["networking:dns", "identity:authentication"])

    def test_issue_count_then_stable_signal_id_break_ties(self):
        data = records(2) + records(2, body="Windows containers authentication fails.", owner="other")
        self.assertEqual(evolve(data)[0]["slots"][0]["key"], "identity:authentication")
        extra = copy.deepcopy(data[0])
        extra["issue"]["url"] += "0"
        data.append(extra)
        self.assertEqual(evolve(data)[0]["slots"][0]["key"], "networking:dns")

    def test_originals_tracked_separately_and_unsupported_original_replaced(self):
        data = records(body="Nano Server missing APIs limit developer testing.")
        state, _ = evolve(data)
        self.assertEqual(state["slots"][1]["key"], "original:h2")
        self.assertEqual(state["slots"][0]["key"], "original:h1")
        self.assertEqual(state["slots"][0]["status"], "deferred")
        self.assertEqual(state["slots"][1]["title"], report.HYPOTHESES[1])
        self.assertEqual(state["slots"][1]["status"], "qualifying")
        self.assertEqual(state["slots"][1]["streak"], 1)
        state, _ = evolve(records(1))
        self.assertEqual(state["slots"][0]["key"], "networking:dns")
        self.assertEqual(state["slots"][0]["status"], "provisional")
        self.assertEqual(state["slots"][1]["status"], "deferred")

    def test_fresh_originals_qualify_first_observation_but_future_sources_do_not(self):
        bodies = (
            "Windows containers development licensing cost is a barrier to community adoption.",
            "Nano Server missing APIs limit developer testing.",
        )
        for index, body in enumerate(bodies):
            with self.subTest(original=index + 1):
                state, _ = evolve(records(body=body))
                slot = state["slots"][index]
                self.assertEqual(slot["key"], f"original:h{index + 1}")
                self.assertEqual(slot["status"], "qualifying")
                self.assertEqual(slot["streak"], 1)
                for offset in (timedelta(hours=1), timedelta(weeks=1), -timedelta(weeks=1)):
                    state, _ = evolve(records(body=body, now=NOW + offset))
                    self.assertEqual(state["slots"][index]["streak"], 0)
                    self.assertEqual(state["slots"][index]["fresh_repos"], 0)

    def test_no_evidence_defers_without_invention(self):
        state, lines = evolve(records(body="Routine maintenance"))
        self.assertEqual([s["key"] for s in state["slots"]], ["original:h1", "original:h2"])
        self.assertTrue(all(s["status"] == "deferred" for s in state["slots"]))
        self.assertIn("No relevant evidence", "\n".join(lines))

    def test_original_with_one_or_two_sources_is_replaced_not_retained(self):
        body = "Nano Server missing APIs limit developer testing."
        for count in (1, 2):
            state, _ = evolve(records(count, body=body))
            self.assertEqual(state["slots"][0]["key"], "images:nano-api")
            self.assertEqual(state["slots"][0]["status"], "provisional")
            self.assertEqual(state["slots"][1]["status"], "deferred")
        stale = records(body=body, now=NOW - timedelta(weeks=1))
        state, _ = evolve(stale)
        self.assertEqual(state["slots"][0]["key"], "images:nano-api")
        self.assertEqual(state["slots"][0]["status"], "provisional")
        self.assertEqual(state["slots"][0]["streak"], 0)

    def test_same_week_is_frozen_but_report_evidence_current(self):
        state, _ = evolve(records())
        original = copy.deepcopy(state)
        rerun, lines = evolve(records(body="Windows containers authentication fails."),
                              NOW + timedelta(hours=1), state)
        self.assertEqual(rerun, original)
        self.assertEqual(state, original)
        self.assertIn("identity:authentication", "\n".join(lines))
        self.assertIn("frozen decision", "\n".join(lines))

    def test_frozen_and_completed_witnesses_survive_absent_current_evidence(self):
        state, _ = evolve(records(4))
        self.assertEqual(len(state["slots"][0]["witnesses"]), 3)
        frozen, lines = evolve([], NOW + timedelta(hours=1), state)
        self.assertEqual(frozen["slots"][0]["witnesses"], state["slots"][0]["witnesses"])
        self.assertIn(state["slots"][0]["witnesses"][0]["url"], "\n".join(lines))
        for week in range(1, 3):
            now = NOW + timedelta(weeks=week)
            state, _ = evolve(records(now=now), now, state)
        witnesses = copy.deepcopy(state["slots"][0]["witnesses"])
        completed, lines = evolve([], NOW + timedelta(weeks=3), state)
        self.assertEqual(completed["slots"][0]["witnesses"], witnesses)
        self.assertEqual(completed["slots"][0]["status"], "completed")
        self.assertIn(witnesses[0]["url"], "\n".join(lines))
        self.assertIn("Frozen decision witnesses", "\n".join(lines))

    def test_witness_types_preserve_actual_pr_comment_provenance(self):
        data = records(1, owner="microsoft", body="Routine maintenance")
        pr = records(1)[0]["issue"]
        pr["url"] = "https://github.com/community/repo0/pull/7"
        pr["body"] = "Routine maintenance"
        pr["comments"]["nodes"] = [{
            "body": "Windows containers DNS fails.",
            "url": pr["url"] + "#issuecomment-99",
            "updatedAt": NOW.isoformat(),
        }]
        data[0]["pr"] = pr
        state, _ = evolve(data)
        witness = state["slots"][0]["witnesses"][0]
        self.assertEqual(witness["repo"], "community/repo0")
        self.assertEqual(witness["source"], "pull_request_comment")
        self.assertEqual(witness["url"], pr["comments"]["nodes"][0]["url"])
        self.assertEqual(set(witness), {"repo", "url", "date", "source"})

    def test_three_consecutive_fresh_weeks_exit_and_never_increment_again(self):
        state, _ = evolve(records())
        self.assertEqual(state["slots"][0]["streak"], 1)
        for week in range(1, 3):
            now = NOW + timedelta(weeks=week)
            state, _ = evolve(records(now=now), now, state)
            self.assertEqual(state["slots"][0]["streak"], week + 1)
        self.assertEqual(state["slots"][0]["status"], "completed")
        now = NOW + timedelta(weeks=4)
        final, lines = evolve([], now, state)
        self.assertEqual(final["slots"][0], state["slots"][0])
        self.assertEqual(final["completed"], [{"id": "H1", "key": "networking:dns"}])
        self.assertIn("reports continue", "\n".join(lines))

    def test_stale_repeats_do_not_exit(self):
        state, _ = evolve(records())
        for week in range(1, 5):
            now = NOW + timedelta(weeks=week)
            state, _ = evolve(records(), now, state)
            self.assertEqual(state["slots"][0]["streak"], 0)
            self.assertEqual(state["slots"][0]["fresh_repos"], 0)
            self.assertEqual(state["slots"][0]["status"], "provisional")

    def test_missing_week_resets_and_iso_year_rollover_is_consecutive(self):
        start = datetime(2026, 12, 28, 12, tzinfo=timezone.utc)
        state, _ = evolve(records(now=start), start)
        for offset, expected in ((1, 2), (3, 1), (4, 2)):
            now = start + timedelta(weeks=offset)
            state, _ = evolve(records(now=now), now, state)
            self.assertEqual(state["slots"][0]["streak"], expected)
        self.assertTrue(state["observed_week"].startswith("2027-"))

    def test_revision_resets_previous_streak_but_current_fresh_week_qualifies(self):
        state, _ = evolve(records())
        next_week = NOW + timedelta(weeks=1)
        state, _ = evolve(records(now=next_week), next_week, state)
        self.assertEqual(state["slots"][0]["streak"], 2)
        now = NOW + timedelta(weeks=2)
        state, _ = evolve(records(body="Windows containers authentication fails.", now=now), now, state)
        self.assertEqual(state["slots"][0]["key"], "identity:authentication")
        self.assertEqual(state["slots"][0]["streak"], 1)
        self.assertEqual(state["slots"][0]["status"], "qualifying")

    def test_fresh_comment_uses_own_date_not_parent_update(self):
        data = records(now=NOW - timedelta(weeks=3))
        for item in data:
            item["issue"]["comments"]["nodes"].append({
                "body": "Windows containers DNS fails.", "url": item["issue"]["url"] + "#issuecomment-1",
                "updatedAt": NOW.isoformat(),
            })
        self.assertEqual(evolve(data)[0]["slots"][0]["fresh_repos"], 3)
        future = records(now=NOW + timedelta(days=1))
        self.assertEqual(evolve(future)[0]["slots"][0]["fresh_repos"], 0)

    def test_monday_observation_counts_preceding_tuesday_through_sunday(self):
        monday = datetime(2026, 10, 26, 17, tzinfo=timezone.utc)
        prior = monday - timedelta(weeks=1)
        previous, _ = evolve([], prior)
        data = records(now=monday)
        for item, offset in zip(data, (1, 3, 6)):
            item["issue"]["updatedAt"] = (prior + timedelta(days=offset)).isoformat()
        state, lines = evolve(data, monday, previous)
        self.assertEqual(state["slots"][0]["fresh_repos"], 3)
        self.assertEqual(state["slots"][0]["streak"], 1)
        self.assertEqual(state["observed_at"], monday.isoformat())
        self.assertEqual(state["history"][-1]["observed_at"], monday.isoformat())
        self.assertIn("trailing 7 days", "\n".join(lines))
        repeated, _ = evolve(data, monday + timedelta(weeks=1), state)
        self.assertEqual(repeated["slots"][0]["fresh_repos"], 0)
        self.assertEqual(repeated["slots"][0]["streak"], 0)

    def test_observed_at_excludes_overlap_and_exact_prior_boundary(self):
        prior = datetime(2026, 10, 19, 22, tzinfo=timezone.utc)
        previous, _ = evolve(records(now=prior - timedelta(hours=1)), prior)
        monday = datetime(2026, 10, 26, 20, tzinfo=timezone.utc)
        data = records(now=monday)
        for item, when in zip(data, (prior - timedelta(hours=1), prior, prior + timedelta(days=1))):
            item["issue"]["updatedAt"] = when.isoformat()
        state, _ = evolve(data, monday, previous)
        self.assertEqual(state["slots"][0]["fresh_repos"], 1)
        self.assertEqual(state["slots"][0]["streak"], 0)
        # The first observation is immutable even if a same-week rerun is later.
        same_week, _ = evolve(records(now=monday + timedelta(hours=1)),
                              monday + timedelta(hours=1), state)
        self.assertEqual(same_week["observed_at"], monday.isoformat())

    def test_missing_week_uses_only_trailing_seven_days_and_resets_streak(self):
        state, _ = evolve(records())
        second = NOW + timedelta(weeks=1)
        state, _ = evolve(records(now=second), second, state)
        self.assertEqual(state["slots"][0]["streak"], 2)
        fourth = NOW + timedelta(weeks=3)
        old, _ = evolve(records(now=second + timedelta(days=1)), fourth, state)
        self.assertEqual(old["slots"][0]["fresh_repos"], 0)
        current, _ = evolve(records(now=fourth - timedelta(days=6)), fourth, state)
        self.assertEqual(current["slots"][0]["fresh_repos"], 3)
        self.assertEqual(current["slots"][0]["streak"], 1)

    def test_no_slot_collision_when_only_one_existing_candidate_is_available(self):
        state, _ = evolve(records() + records(2, body="Windows containers authentication fails.", owner="other"))
        now = NOW + timedelta(weeks=1)
        state, _ = evolve(records(1, body="Windows containers authentication fails.", now=now), now, state)
        self.assertEqual(len({s["key"] for s in state["slots"]}), 2)

    def test_state_json_bounded_history_and_validation(self):
        state = None
        for week in range(25):
            now = NOW + timedelta(weeks=week)
            state, _ = evolve(records(1, now=now), now, state)
        self.assertEqual(len(state["history"]), 12)
        self.assertLess(len(json.dumps(state)), 20000)
        self.assertEqual(evolution.validate_state(json.loads(json.dumps(state))), state)
        mutations = [
            ("version", True), ("version", 4), ("observed_week", "2026-W99"),
            ("observed_week", "2026-10-23"), ("history", []), ("completed", [{}]),
            ("observed_at", None), ("observed_at", "2026-10-23T12:00:00"),
            ("observed_at", "2020-01-01T12:00:00Z"),
        ]
        for field, value in mutations:
            bad = copy.deepcopy(state)
            bad[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                evolution.validate_state(bad)
        for field, value in (("id", "H3"), ("key", "anything"), ("status", "validated"),
                             ("streak", -1), ("fresh_repos", True), ("streak", 3)):
            bad = copy.deepcopy(state)
            bad["slots"][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                evolution.validate_state(bad)
        with self.assertRaises(ValueError):
            evolve([], NOW, state)

    def test_maximum_slim_witness_state_stays_under_issue_body_budget(self):
        state = None
        for week in range(12):
            now = NOW + timedelta(weeks=week)
            state, _ = evolve(records(1), now, state)
        for entry in state["history"]:
            for item in entry["slots"]:
                item.update(title="t" * 400, status="qualifying", streak=1, fresh_repos=3)
                item["witnesses"] = []
                for index in range(3):
                    repo = f"owner{index}/" + "r" * 153
                    url = f"https://github.com/{repo}/issues/1#"
                    url += "x" * (240 - len(url))
                    item["witnesses"].append(dict(
                        repo=repo, url=url, date=entry["observed_at"], source="pull_request_comment"))
        state["slots"] = copy.deepcopy(state["history"][-1]["slots"])
        evolution.validate_state(state)
        self.assertLess(len(json.dumps(state, separators=(",", ":"))), 59000)
        bad = copy.deepcopy(state)
        bad["slots"][0]["witnesses"][0]["date"] = "invalid"
        with self.assertRaises(ValueError):
            evolution.validate_state(bad)

class PersistenceBoundaryTests(unittest.TestCase):
    def test_authoritative_completed_decision_and_areas_precede_seed_reference_and_appendix(self):
        state = None
        for week in range(3):
            now = NOW + timedelta(weeks=week)
            data = records(now=now)
            state, _ = evolve(data, now, state)
        with patch.object(report, "load_scored_issues", return_value=[{}]), \
                patch.object(report, "collect_issue", return_value=data[0]), \
                patch.object(Path, "mkdir"), patch.object(report, "_write_pdf") as writer:
            report.generate_report(generated_at=now, previous_state=state)
        text = "\n".join(writer.call_args.args[0])
        self.assertLess(text.index("Generated:"), text.index("Adaptive weekly hypotheses"))
        self.assertLess(text.index("Decision: completed"), text.index("Researched areas"))
        self.assertLess(text.index("Researched areas"), text.index("Adaptive method and limitations"))
        self.assertLess(text.index("Researched areas"), text.index("Original seed hypotheses"))
        self.assertLess(text.index("Original seed hypotheses"), text.index("Issue and linked PR appendix"))
        self.assertNotIn("insufficient evidence", text)
        self.assertNotIn("Hypothesis 1 candidate signals", text)

    def test_callback_only_after_successful_pdf_and_return_api_unchanged(self):
        callback = Mock()
        order = []
        callback.side_effect = lambda state: order.append("callback")
        with patch.object(report, "load_scored_issues", return_value=[]), \
                patch.object(Path, "mkdir"), \
                patch.object(report, "_write_pdf", side_effect=lambda *args: order.append("pdf")):
            path = report.generate_report(generated_at=NOW, on_success=callback)
        self.assertIsInstance(path, Path)
        self.assertEqual(order, ["pdf", "callback"])
        evolution.validate_state(callback.call_args.args[0])

    def test_collection_render_directory_and_pdf_errors_do_not_save_state(self):
        for target in ("load_scored_issues", "collect_issue", "build_report", "_write_pdf"):
            callback = Mock()
            with patch.object(report, "load_scored_issues", return_value=[{}]), \
                    patch.object(report, "collect_issue", return_value=records(1)[0]), \
                    patch.object(Path, "mkdir"), \
                    patch.object(report, "_write_pdf"), \
                    patch.object(report, target, side_effect=RuntimeError("failed")):
                with self.subTest(target=target), self.assertRaises(RuntimeError):
                    report.generate_report(generated_at=NOW, on_success=callback)
            callback.assert_not_called()
        callback = Mock()
        with patch.object(report, "load_scored_issues", return_value=[]), \
                patch.object(Path, "mkdir", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                report.generate_report(generated_at=NOW, on_success=callback)
        callback.assert_not_called()

    def test_malformed_state_fails_before_collection_and_callback_failure_propagates(self):
        with patch.object(report, "load_scored_issues") as load:
            with self.assertRaises(ValueError):
                report.generate_report(previous_state={})
        load.assert_not_called()
        with patch.object(report, "load_scored_issues", return_value=[]), \
                patch.object(Path, "mkdir"), patch.object(report, "_write_pdf") as writer:
            with self.assertRaisesRegex(OSError, "persist"):
                report.generate_report(generated_at=NOW, on_success=Mock(side_effect=OSError("persist")))
        writer.assert_called_once()


if __name__ == "__main__":
    unittest.main()
