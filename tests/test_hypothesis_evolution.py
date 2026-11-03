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
    def test_zero_one_two_three_public_repositories_start_a_trial(self):
        for count in range(4):
            with self.subTest(count=count):
                state, _ = evolve(records(count))
                item = state["slots"][0]
                self.assertEqual(item["fresh_repos"], count)
                self.assertEqual(item["trial_week"], int(bool(count)))
                self.assertEqual(item["status"], "testing" if count else "deferred")
                self.assertEqual(len(item["supporting"]), count)
                next_week = NOW + timedelta(weeks=1)
                state, _ = evolve(records(count, now=next_week), next_week, state)
                self.assertEqual(state["slots"][0]["trial_week"], 2 if count else 0)
                self.assertEqual(len(state["slots"][0]["supporting"]), count)

    def test_trial_accumulates_distinct_fresh_repositories_then_retires_supported(self):
        state = None
        for week, owner in enumerate(("a", "b", "c", "d")):
            now = NOW + timedelta(weeks=week)
            state, lines = evolve(records(1, owner=owner, now=now), now, state)
            if week < 3:
                self.assertEqual(state["slots"][0]["key"], "networking:dns")
                self.assertEqual(state["slots"][0]["trial_week"], week + 1)
                self.assertEqual(len(state["slots"][0]["supporting"]), week + 1)
        retired = state["retired"][0]
        self.assertEqual((retired["key"], retired["outcome"], retired["supporting"]),
                         ("networking:dns", "supported", 4))
        self.assertEqual(retired["started_week"], "2026-W43")
        self.assertEqual(len(retired["witnesses"]), 3)
        self.assertEqual(state["slots"][0]["status"], "deferred")
        self.assertIn("outcome: supported", "\n".join(lines))
        self.assertIn("cooldown until", "\n".join(lines))

    def test_contradicting_repositories_refute_and_weak_support_is_inconclusive(self):
        state, _ = evolve(records(1))
        contrary = "Windows containers DNS issue is resolved."
        for week in range(1, 4):
            now = NOW + timedelta(weeks=week)
            data = records(2, owner="contrary", body=contrary, now=now) if week == 1 else []
            state, _ = evolve(data, now, state)
        self.assertEqual(state["retired"][0]["outcome"], "contradicted")
        self.assertEqual((state["retired"][0]["supporting"], state["retired"][0]["contradicting"]), (1, 2))
        state, _ = evolve(records(2))
        for week in range(1, 4):
            state, _ = evolve([], NOW + timedelta(weeks=week), state)
        self.assertEqual(state["retired"][0]["outcome"], "inconclusive")

    def test_retired_hypothesis_cools_down_and_slot_rotates_to_new_candidate(self):
        bodies = (("dns", "DNS resolution fails"), ("auth", "authentication fails"),
                  ("tool", "build fails"), ("gmsa", "gMSA fails"),
                  ("runtime", "runtime compatibility fails"), ("size", "image pull fails"))

        def data(now):
            result = []
            for rank, (owner, body) in enumerate(bodies):
                result += records(6 - rank, owner=owner, body=f"Windows containers {body}.", now=now)
            return result
        state = None
        keys = []
        for week in range(12):
            now = NOW + timedelta(weeks=week)
            state, _ = evolve(data(now), now, state)
            keys.append([slot["key"] for slot in state["slots"]])
        first = ["networking:dns", "identity:authentication", "tooling:build",
                 "identity:gmsa", "runtime:compatibility"]
        self.assertEqual(keys[0], first)
        self.assertEqual(len(state["slots"]), evolution.SLOT_COUNT)
        self.assertEqual(keys[2], keys[0])
        self.assertEqual(keys[3][0], "images:size")
        self.assertFalse(set(first) & set(keys[3] + keys[6] + keys[10]))
        self.assertTrue(set(first) <= set(keys[11]))
        self.assertEqual([item["outcome"] for item in state["retired"][:5]], ["supported"] * 4 + ["inconclusive"])

    def test_overlapping_context_signal_is_not_tested_concurrently(self):
        body = "Windows containers DNS resolution fails on Kubernetes."
        state, _ = evolve(records(body=body))
        self.assertEqual(state["slots"][0]["key"], "networking:dns")
        self.assertEqual(state["slots"][1]["status"], "deferred")
        self.assertFalse(any(slot["key"].startswith("networking:dns@") for slot in state["slots"]))
        state, _ = evolve(records(body=body) + records(
            2, owner="auth", body="Windows containers authentication fails."))
        self.assertEqual([slot["key"] for slot in state["slots"]][:2],
                         ["networking:dns", "identity:authentication"])
        bad = copy.deepcopy(state)
        bad["slots"][1].update(key="networking:dns@kubernetes", area="networking",
                               title=evolution._title("networking:dns@kubernetes", ()))
        bad["history"][-1]["slots"][1]["key"] = "networking:dns@kubernetes"
        with self.assertRaises(ValueError):
            evolution.validate_state(bad)

    def test_context_qualified_signals_create_narrower_hypotheses(self):
        data = records(body="Windows containers DNS resolution fails on LTSC 2022 with Hyper-V.")
        data[0]["issue"]["title"] = "Windows containers on containerd"
        signals = evolution._signals(data, NOW, report.triage, NOW - timedelta(days=7))
        keys = {s["key"] for s in signals if s["repo"] == "community/repo0"}
        self.assertTrue({"networking:dns", "networking:dns@ltsc2022", "networking:dns@hyperv",
                         "networking:dns@containerd"} <= keys)
        self.assertNotIn("networking:dns@containerd",
                         {s["key"] for s in signals if s["repo"] == "community/repo1"})
        self.assertEqual(evolution._title("networking:dns@ltsc2022", ()),
                         "Windows container DNS resolution failures on Windows Server 2022/LTSC 2022")
        state = None
        for week in range(4):
            now = NOW + timedelta(weeks=week)
            state, lines = evolve(records(body=data[1]["issue"]["body"], now=now), now, state)
        self.assertEqual(state["slots"][0]["key"], "networking:dns@hyperv")
        self.assertIn("Context qualifiers:", "\n".join(lines))

    def test_v3_state_migrates_running_hypotheses_into_trials(self):
        weeks = [(NOW - timedelta(weeks=2 - i)) for i in range(3)]
        witness = dict(repo="kubernetes/kubernetes", url="https://github.com/kubernetes/kubernetes/issues/1",
                       date=weeks[0].isoformat(), source="issue")
        def slots(fresh):
            return [dict(id="H1", key="networking:connectivity", title="Windows container network connectivity barriers",
                         area="networking", status="provisional", streak=0, fresh_repos=fresh,
                         witnesses=[witness] if fresh else []),
                    dict(id="H2", key="licensing:cost", title="Windows container licensing/cost barriers",
                         area="licensing/cost", status="completed", streak=3, fresh_repos=3,
                         witnesses=[witness])]
        history = [dict(week=evolution._week(w), observed_at=w.isoformat(), slots=slots(int(i == 0)))
                   for i, w in enumerate(weeks)]
        old = dict(version=3, observed_week=history[-1]["week"], observed_at=weeks[-1].isoformat(),
                   slots=history[-1]["slots"], history=history,
                   completed=[{"id": "H2", "key": "licensing:cost"}])
        state = evolution.upgrade_state(copy.deepcopy(old), report.HYPOTHESES)
        self.assertEqual(state["version"], evolution.VERSION)
        first = state["slots"][0]
        self.assertEqual((first["status"], first["trial_week"], first["started_week"], first["supporting"]),
                         ("testing", 3, "2026-W41", ["kubernetes/kubernetes"]))
        self.assertEqual([slot["key"] for slot in state["slots"][1:]],
                         ["original:h1", "original:h2", "unassigned", "unassigned"])
        self.assertEqual(len(state["history"][0]["slots"]), evolution.SLOT_COUNT)
        self.assertEqual(state["retired"][0]["key"], "licensing:cost")
        self.assertEqual(state["retired"][0]["outcome"], "inconclusive")
        state, _ = evolve([], NOW + timedelta(weeks=1), old)
        self.assertEqual(state["retired"][-1]["key"], "networking:connectivity")
        with self.assertRaises(ValueError):
            evolution.upgrade_state(dict(old, slots=None), report.HYPOTHESES)
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
        self.assertIn("networking:dns: eligible supporting repositories=2; fresh=2; contradicting=3; "
                      "lifecycle: testing in H2.", text)
        self.assertIn("networking:connectivity: eligible supporting repositories=2; fresh=2; contradicting=0; "
                      "lifecycle: testing in H1.", text)
        self.assertNotIn("Supporting repositories:", text)
        self.assertNotIn("Fresh supporting repositories:", text)
        self.assertIn("area: networking", text)
        self.assertTrue(all(s["status"] == "testing" for s in state["slots"][:2]))
        self.assertTrue(all(s["status"] == "deferred" for s in state["slots"][2:]))

    def test_strongest_repo_ranking_not_comments_or_scores(self):
        data = records(3) + records(2, body="Windows containers authentication fails.", owner="other")
        for index in range(10):
            data[-1]["issue"]["comments"]["nodes"].append({
                "body": "Windows containers authentication fails.", "url": f"https://github.com/other/repo1/issues/1#issuecomment-{index}",
                "updatedAt": NOW.isoformat(),
            })
        state, _ = evolve(data)
        self.assertEqual([s["key"] for s in state["slots"]][:2], ["networking:dns", "identity:authentication"])

    def test_issue_count_then_stable_signal_id_break_ties(self):
        data = records(2) + records(2, body="Windows containers authentication fails.", owner="other")
        self.assertEqual(evolve(data)[0]["slots"][0]["key"], "identity:authentication")
        extra = copy.deepcopy(data[0])
        extra["issue"]["url"] += "0"
        data.append(extra)
        self.assertEqual(evolve(data)[0]["slots"][0]["key"], "networking:dns")

    def test_seed_hypothesis_wins_ties_and_excludes_overlapping_alias(self):
        data = records(body="Nano Server missing APIs limit developer testing.")
        state, _ = evolve(data)
        self.assertEqual(state["slots"][0]["key"], "original:h2")
        self.assertEqual(state["slots"][0]["title"], report.HYPOTHESES[1])
        self.assertEqual(state["slots"][0]["status"], "testing")
        self.assertEqual(state["slots"][1]["key"], "original:h1")
        self.assertEqual(state["slots"][1]["status"], "deferred")
        state, _ = evolve(records(1))
        self.assertEqual(state["slots"][0]["key"], "networking:dns")
        self.assertEqual(state["slots"][1]["status"], "deferred")
    def test_fresh_originals_start_trials_but_future_or_stale_sources_add_nothing(self):
        bodies = (
            "Windows containers development licensing cost is a barrier to community adoption.",
            "Nano Server missing APIs limit developer testing.",
        )
        for index, body in enumerate(bodies):
            with self.subTest(original=index + 1):
                state, _ = evolve(records(body=body))
                slot = state["slots"][0]
                self.assertEqual(slot["key"], f"original:h{index + 1}")
                self.assertEqual((slot["status"], slot["fresh_repos"]), ("testing", 3))
                for offset in (timedelta(hours=1), timedelta(weeks=1), -timedelta(weeks=1)):
                    state, _ = evolve(records(body=body, now=NOW + offset))
                    self.assertEqual(state["slots"][0]["fresh_repos"], 0)
                    self.assertEqual(state["slots"][0]["supporting"], [])
    def test_no_evidence_defers_without_invention(self):
        state, lines = evolve(records(body="Routine maintenance"))
        self.assertEqual([s["key"] for s in state["slots"]],
                         ["original:h1", "original:h2", "unassigned", "unassigned", "unassigned"])
        self.assertTrue(all(s["status"] == "deferred" for s in state["slots"]))
        self.assertIn("No relevant evidence", "\n".join(lines))

    def test_same_week_is_frozen_but_report_evidence_current(self):
        state, _ = evolve(records())
        original = copy.deepcopy(state)
        rerun, lines = evolve(records(body="Windows containers authentication fails."),
                              NOW + timedelta(hours=1), state)
        self.assertEqual(rerun, original)
        self.assertEqual(state, original)
        self.assertIn("identity:authentication", "\n".join(lines))
        self.assertIn("frozen decision", "\n".join(lines))

    def test_frozen_and_retired_witnesses_survive_absent_current_evidence(self):
        state, _ = evolve(records(4))
        self.assertEqual(len(state["slots"][0]["witnesses"]), 3)
        frozen, lines = evolve([], NOW + timedelta(hours=1), state)
        self.assertEqual(frozen, state)
        self.assertIn(state["slots"][0]["witnesses"][0]["url"], "\n".join(lines))
        witnesses = copy.deepcopy(state["slots"][0]["witnesses"])
        for week in range(1, 5):
            state, lines = evolve([], NOW + timedelta(weeks=week), state)
        self.assertEqual(state["retired"][0]["witnesses"], witnesses)
        self.assertIn(witnesses[0]["url"], "\n".join(lines))
        self.assertIn("Trial witnesses", "\n".join(lines))
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

    def test_stale_repeats_add_no_trial_evidence(self):
        stale = records(now=NOW - timedelta(weeks=2))
        state, _ = evolve(stale)
        for week in range(1, 4):
            state, _ = evolve(stale, NOW + timedelta(weeks=week), state)
            if week < 3:
                self.assertEqual(state["slots"][0]["supporting"], [])
                self.assertEqual(state["slots"][0]["fresh_repos"], 0)
        self.assertEqual(state["retired"][0]["outcome"], "inconclusive")
        self.assertEqual(state["retired"][0]["supporting"], 0)
    def test_missing_week_counts_observations_and_iso_year_rollover(self):
        start = datetime(2026, 12, 28, 12, tzinfo=timezone.utc)
        state, _ = evolve(records(now=start), start)
        for offset, expected in ((1, 2), (3, 3)):
            now = start + timedelta(weeks=offset)
            state, _ = evolve(records(now=now), now, state)
            self.assertEqual(state["slots"][0]["trial_week"], expected)
        self.assertEqual(state["slots"][0]["started_week"], "2026-W53")
        self.assertTrue(state["observed_week"].startswith("2027-"))
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
        self.assertEqual(len(state["slots"][0]["supporting"]), 3)
        self.assertEqual(state["observed_at"], monday.isoformat())
        self.assertEqual(state["history"][-1]["observed_at"], monday.isoformat())
        self.assertIn("trailing 7 days", "\n".join(lines))
        repeated, _ = evolve(data, monday + timedelta(weeks=1), state)
        self.assertEqual(repeated["slots"][0]["fresh_repos"], 0)
        self.assertEqual(repeated["slots"][0]["trial_week"], 2)

    def test_observed_at_excludes_overlap_and_exact_prior_boundary(self):
        prior = datetime(2026, 10, 19, 22, tzinfo=timezone.utc)
        previous, _ = evolve(records(now=prior - timedelta(hours=1)), prior)
        monday = datetime(2026, 10, 26, 20, tzinfo=timezone.utc)
        data = records(now=monday)
        for item, when in zip(data, (prior - timedelta(hours=1), prior, prior + timedelta(days=1))):
            item["issue"]["updatedAt"] = when.isoformat()
        state, _ = evolve(data, monday, previous)
        self.assertEqual(state["slots"][0]["fresh_repos"], 1)
        self.assertEqual(previous["slots"][0]["fresh_repos"], 3)
        self.assertEqual(len(state["slots"][0]["supporting"]), 3)
        self.assertEqual(state["slots"][0]["trial_week"], 2)
        # The first observation is immutable even if a same-week rerun is later.
        same_week, _ = evolve(records(now=monday + timedelta(hours=1)),
                              monday + timedelta(hours=1), state)
        self.assertEqual(same_week["observed_at"], monday.isoformat())

    def test_missing_week_uses_only_trailing_seven_days(self):
        state, _ = evolve(records(1))
        second = NOW + timedelta(weeks=1)
        state, _ = evolve(records(1, now=second), second, state)
        self.assertEqual(state["slots"][0]["trial_week"], 2)
        fourth = NOW + timedelta(weeks=3)
        old, _ = evolve(records(now=second + timedelta(days=1)), fourth, state)
        self.assertEqual(old["slots"][0]["fresh_repos"], 0)
        current, _ = evolve(records(now=fourth - timedelta(days=6)), fourth, state)
        self.assertEqual(current["slots"][0]["fresh_repos"], 3)
        self.assertEqual(current["slots"][0]["trial_week"], 3)

    def test_no_slot_collision_when_only_one_existing_candidate_is_available(self):
        state, _ = evolve(records() + records(2, body="Windows containers authentication fails.", owner="other"))
        now = NOW + timedelta(weeks=1)
        state, _ = evolve(records(1, body="Windows containers authentication fails.", now=now), now, state)
        keys = [s["key"] for s in state["slots"] if s["key"] != "unassigned"]
        self.assertEqual(len(keys), len(set(keys)))
        bad = copy.deepcopy(state)
        bad["slots"][1]["key"] = bad["slots"][0]["key"]
        with self.assertRaises(ValueError):
            evolution.validate_state(bad)

    def test_state_json_bounded_history_and_validation(self):
        state = None
        for week in range(25):
            now = NOW + timedelta(weeks=week)
            state, _ = evolve(records(1, now=now), now, state)
        self.assertEqual(len(state["history"]), 12)
        self.assertLess(len(json.dumps(state)), 20000)
        self.assertEqual(evolution.validate_state(json.loads(json.dumps(state))), state)
        mutations = [
            ("version", True), ("version", 3), ("version", 5), ("observed_week", "2026-W99"),
            ("observed_week", "2026-10-23"), ("history", []), ("retired", [{}]),
            ("observed_at", None), ("observed_at", "2026-10-23T12:00:00"),
            ("observed_at", "2020-01-01T12:00:00Z"),
        ]
        for field, value in mutations:
            bad = copy.deepcopy(state)
            bad[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                evolution.validate_state(bad)
        for field, value in (("id", "H3"), ("key", "anything"), ("key", "networking:dns@mars"),
                             ("status", "validated"), ("trial_week", -1), ("trial_week", 4),
                             ("fresh_repos", True), ("supporting", ["b/b", "a/a"]),
                             ("supporting", [f"o/r{i}" for i in range(11)])):
            bad = copy.deepcopy(state)
            bad["slots"][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                evolution.validate_state(bad)
        with self.assertRaises(ValueError):
            evolve([], NOW, state)

    def test_maximum_bounded_state_stays_under_issue_body_budget(self):
        state = None
        for week in range(12):
            now = NOW + timedelta(weeks=week)
            state, _ = evolve(records(1), now, state)

        def witnesses(date):
            result = []
            for index in range(3):
                repo = f"owner{index}/" + "r" * 153
                url = f"https://github.com/{repo}/issues/1#"
                result.append(dict(repo=repo, url=url + "x" * (240 - len(url)),
                                   date=date, source="pull_request_comment"))
            return result
        repos = sorted(f"owner{index}/" + "r" * 153 for index in range(10))
        keys = ("identity:authentication@ltsc2025", "tooling:build@containerd", "networking:connectivity@kubernetes",
                "runtime:compatibility@hyperv", "images:size@ltsc2019")
        for item, key in zip(state["slots"], keys):
            item.update(key=key)
            item.update(title=evolution._title(item["key"], ()), area=evolution._area(item["key"]),
                        status="testing", trial_week=3, started_week=state["history"][-3]["week"],
                        fresh_repos=1000000, supporting=repos, contradicting=repos,
                        witnesses=witnesses(state["observed_at"]))
        state["history"][-1]["slots"] = [{f: item[f] for f in evolution.SLIM_FIELDS} for item in state["slots"]]
        state["retired"] = [dict(id="H1", key="identity:authentication@ltsc2025", outcome="contradicted",
                                 started_week=state["history"][0]["week"],
                                 ended_week=state["history"][0]["week"], supporting=10, contradicting=10,
                                 witnesses=witnesses(state["history"][0]["observed_at"]))] * 16
        evolution.validate_state(state)
        self.assertLess(len(json.dumps(state, separators=(",", ":"))), 59000)
        bad = copy.deepcopy(state)
        bad["slots"][0]["witnesses"][0]["date"] = "invalid"
        with self.assertRaises(ValueError):
            evolution.validate_state(bad)
        bad = copy.deepcopy(state)
        bad["retired"].append(bad["retired"][0])
        with self.assertRaises(ValueError):
            evolution.validate_state(bad)

class SourceShowcaseTests(unittest.TestCase):
    def select(self, data, keys=("networking:dns",)):
        signals = evolution._signals(data, NOW, report.triage, NOW - timedelta(days=7))
        return evolution.select_supporting_sources(data, signals, NOW, keys)

    def duplicate(self, item, number):
        item = copy.deepcopy(item)
        item["issue"]["url"] = item["issue"]["url"].rsplit("/", 1)[0] + f"/{number}"
        return item

    def labels(self, item, *names):
        item["issue"]["labels"] = {"nodes": [{"name": name} for name in names]}

    def comments(self, item, dates):
        item["issue"]["comments"]["nodes"] = [
            dict(body="Routine discussion", url=item["issue"]["url"] + f"#issuecomment-{i}",
                 createdAt=created, updatedAt=NOW.isoformat())
            for i, created in enumerate(dates)
        ]

    def test_more_than_five_supporting_sources_and_five_repository_diversity(self):
        data = records(6)
        extras = [self.duplicate(data[0], i + 2) for i in range(4)]
        for item in extras:
            self.labels(item, "p0")
        selected, total = self.select(data + extras)
        self.assertEqual(total, 10)
        self.assertEqual(len(selected), 5)
        self.assertEqual(len({s["repo"] for s in selected}), 5)
        self.assertEqual(selected[0]["url"], extras[0]["issue"]["url"])
        self.assertEqual(sum(s["repo"] == "community/repo0" for s in selected), 1)
        self.assertEqual(self.select(list(reversed(data + extras))), (selected, total))

    def test_repository_scarcity_fills_remaining_slots_in_rank_order(self):
        data = records(1)
        data += [self.duplicate(data[0], i) for i in range(2, 8)]
        self.labels(data[3], "critical")
        selected, total = self.select(data)
        self.assertEqual((len(selected), total), (5, 7))
        self.assertEqual(selected[0]["url"], data[3]["issue"]["url"])
        self.assertEqual(len({s["repo"] for s in selected}), 1)
        self.assertTrue(all("ranked fill" in s["selection"] for s in selected[1:]))
        self.assertEqual(self.select(data[::-1]), (selected, total))

    def test_shared_pr_and_comment_deduplicated_across_issues_and_signal_areas(self):
        data = records(3, body="Routine maintenance", owner="Microsoft")
        pr = records(1, body="Windows containers DNS resolution fails and authentication fails.")[0]["issue"]
        pr["url"] = "https://github.com/community/repo0/pull/7"
        pr["comments"]["nodes"] = [
            dict(body="Windows containers DNS resolution fails and tooling build fails.",
                 url=pr["url"] + "#issuecomment-7",
                 createdAt=NOW.isoformat(), updatedAt=NOW.isoformat())
        ]
        pr["labels"] = {"nodes": [{"name": "priority: P1"}]}
        for item in data:
            item["pr"] = copy.deepcopy(pr)
        selected, total = self.select(data)
        self.assertEqual((len(selected), total), (2, 2))
        self.assertEqual({s["repo"] for s in selected}, {"community/repo0"})
        self.assertEqual({s["source"] for s in selected}, {"pull_request", "pull_request_comment"})
        self.assertTrue(all(len(s["keys"]) >= 2 for s in selected))
        self.assertTrue(all(s["urgency"] == 1 and s["activity"] == 1 for s in selected))
        _, lines = evolve(data)
        text = "\n".join(lines)
        self.assertEqual(text.count("Current source |"), 2)
        self.assertEqual(text.count("Context excerpt:"), 2)
        self.assertIn("repository: community/repo0", text)
        self.assertNotIn("repository: microsoft/", text)

    def test_only_eligible_support_not_contrary_inconclusive_or_unknown_public(self):
        data = records(1)
        data += records(1, owner="contrary", body="Windows containers DNS is resolved.")
        data += records(1, owner="uncertain", body="Windows containers DNS networking.")
        data += records(1, owner="Microsoft")
        data += records(1, owner="private", private=True)
        data += records(1, owner="unknown", private=None)
        selected, total = self.select(data)
        self.assertEqual((len(selected), total), (1, 1))
        self.assertEqual(selected[0]["repo"], "community/repo0")
        _, lines = evolve(data)
        text = "\n".join(lines)
        self.assertIn("Other evidence - contradicting: 1 candidate signals", text)
        self.assertIn("Other evidence - inconclusive: 1 candidate signals", text)
        self.assertNotIn("DNS is resolved.", text)
        self.assertNotIn("DNS networking.", text)

    def test_explicit_urgency_labels_case_insensitive_unknown_last_no_prose_guessing(self):
        labels = ["p4", "Priority/P2", "SeVerItY: CRITICAL", "P1", "p3", "unrecognized"]
        data = records(len(labels))
        for item, label in zip(data, labels):
            self.labels(item, label)
        data[-1]["issue"]["title"] = "URGENT CRITICAL P0 Windows containers"
        selected, _ = self.select(data)
        self.assertEqual([s["urgency"] for s in selected], [0, 1, 2, 3, 4])
        self.assertNotIn(data[-1]["issue"]["url"], [s["url"] for s in selected])
        for label, expected in evolution.URGENCY.items():
            with self.subTest(label=label):
                self.labels(data[0], label.upper())
                self.assertEqual(self.select(data[:1])[0][0]["urgency"], expected)

    def test_current_hypothesis_keys_precede_other_supporting_topics(self):
        data = records(5) + records(1, owner="other", body="Windows containers authentication fails.")
        self.labels(data[-1], "p0")
        self.assertEqual(self.select(data)[0][0]["repo"], "community/repo0")
        selected, _ = self.select(data, ("identity:authentication",))
        self.assertEqual(selected[0]["repo"], "other/repo0")
        self.assertTrue(selected[0]["relevant"])

    def test_recent_created_activity_breaks_urgency_ties_not_edits_or_future(self):
        data = records(4)
        self.comments(data[0], [NOW.isoformat()])
        self.comments(data[1], [NOW.isoformat(), (NOW - timedelta(days=1)).isoformat()])
        self.comments(data[2], [(NOW - timedelta(days=30)).isoformat()] * 10)
        self.comments(data[3], [(NOW + timedelta(seconds=1)).isoformat()] * 20)
        selected, _ = self.select(data)
        self.assertEqual([s["activity"] for s in selected], [2, 1, 0, 0])
        self.assertEqual([s["repo"] for s in selected],
                         ["community/repo1", "community/repo0", "community/repo2", "community/repo3"])
        self.comments(data[0], [(NOW - timedelta(days=7)).isoformat()])
        self.assertEqual(self.select(data[:1])[0][0]["activity"], 0)

    def test_duplicate_comment_url_does_not_inflate_discussion(self):
        data = records(1)
        self.comments(data[0], [NOW.isoformat()])
        data[0]["issue"]["comments"]["nodes"] *= 5
        self.assertEqual(self.select(data)[0][0]["activity"], 1)

    def test_update_recency_then_url_are_stable_tiebreaks_future_source_excluded(self):
        data = records(4)
        data[0]["issue"]["updatedAt"] = (NOW - timedelta(days=1)).isoformat()
        data[3]["issue"]["updatedAt"] = (NOW + timedelta(seconds=1)).isoformat()
        selected, total = self.select(data[::-1])
        self.assertEqual(total, 3)
        self.assertEqual([s["repo"] for s in selected],
                         ["community/repo1", "community/repo2", "community/repo0"])
        self.assertEqual(self.select(data), (selected, total))

    def test_aggregates_supporting_keys_and_limits_excerpt_without_body_duplication(self):
        body = "Windows containers DNS fails and authentication fails. " + "context " * 150
        data = records(1, body=body)
        data[0]["issue"]["body"] += "\n\nWindows containers tooling build fails."
        selected, total = self.select(data)
        self.assertEqual(total, 1)
        self.assertEqual(selected[0]["keys"],
                         ["identity:authentication", "networking:dns", "tooling:build"])
        self.assertLessEqual(len(selected[0]["excerpt"]), 500)
        self.assertTrue(selected[0]["excerpt"].endswith("..."))
        _, lines = evolve(data)
        self.assertEqual("\n".join(lines).count("Context excerpt:"), 1)
        self.assertNotIn(body, "\n".join(lines))

    def test_unshown_evidence_still_qualifies_and_display_never_changes_state(self):
        data = records(6) + records(3, owner="other", body="Windows containers authentication fails.")
        for item in data[:6]:
            self.labels(item, "p0")
        state, lines = evolve(data)
        self.assertEqual([s["fresh_repos"] for s in state["slots"]], [6, 3, 0, 0, 0])
        self.assertEqual([s["status"] for s in state["slots"]], ["testing", "testing"] + ["deferred"] * 3)
        selected, total = self.select(data, (s["key"] for s in state["slots"]))
        self.assertEqual(total, 9)
        self.assertFalse(any(s["repo"].startswith("other/") for s in selected))
        self.assertEqual("\n".join(lines).count("Current source |"), 5)
        with patch.object(evolution, "_source_lines", return_value=[]):
            unrendered, _ = evolve(data)
        self.assertEqual(state, unrendered)
        selector = evolution.select_supporting_sources
        with patch.object(evolution, "select_supporting_sources", side_effect=lambda *args: selector(*args, limit=1)):
            one_source_state, one_source_lines = evolve(data)
        self.assertEqual(state, one_source_state)
        self.assertEqual("\n".join(one_source_lines).count("Current source |"), 1)
        next_week = NOW + timedelta(weeks=1)
        next_data = records(6, now=next_week) + records(
            3, owner="other", now=next_week, body="Windows containers authentication fails.")
        expected, _ = evolve(next_data, next_week, state)
        with patch.object(evolution, "_source_lines", return_value=[]):
            actual, _ = evolve(next_data, next_week, unrendered)
        self.assertEqual(expected, actual)
        with patch.object(evolution, "select_supporting_sources", side_effect=lambda *args: selector(*args, limit=1)):
            actual, _ = evolve(next_data, next_week, one_source_state)
        self.assertEqual(expected, actual)

    def test_no_support_never_pads_showcase(self):
        for data in ([], records(3, body="Windows containers DNS is resolved."), records(owner="Azure")):
            with self.subTest(data=data):
                self.assertEqual(self.select(data), ([], 0))
                _, lines = evolve(data)
                text = "\n".join(lines)
                self.assertIn("Supporting sources shown: 0 of 0", text)
                self.assertNotIn("Current source |", text)

    def test_423_analyzed_issues_have_only_five_current_details_no_appendix(self):
        data = records(423)
        state, adaptive = evolve(data)
        text = "\n".join(report.build_report(data, NOW, "export.json", adaptive))
        self.assertIn("Total analyzed issues: 423", text)
        self.assertIn("Supporting sources shown: 5 of 423", text)
        self.assertEqual(text.count("Current source |"), 5)
        self.assertEqual(text.count("Context excerpt:"), 5)
        self.assertNotIn("appendix", text)
        self.assertNotIn("Issue body excerpt", text)
        self.assertNotIn("Supporting repositories:", text)
        self.assertNotIn("community/repo422", text)
        self.assertEqual(state["slots"][0]["fresh_repos"], 423)
        self.assertEqual(len(state["slots"][0]["witnesses"]), 3)
        self.assertIn("historical audit references only", text)


class PersistenceBoundaryTests(unittest.TestCase):
    def test_authoritative_completed_decision_and_areas_precede_seed_reference_no_appendix(self):
        state = None
        for week in range(4):
            now = NOW + timedelta(weeks=week)
            data = records(now=now)
            state, _ = evolve(data, now, state)
        with patch.object(report, "load_scored_issues", return_value=[{}]), \
                patch.object(report, "collect_issue", return_value=data[0]), \
                patch.object(Path, "mkdir"), patch.object(report, "_write_pdf") as writer:
            report.generate_report(generated_at=now, previous_state=state)
        text = "\n".join(writer.call_args.args[0])
        self.assertLess(text.index("Generated:"), text.index("Adaptive weekly hypotheses"))
        self.assertLess(text.index("outcome: supported"), text.index("Researched areas"))
        self.assertLess(text.index("Researched areas"), text.index("Adaptive method and limitations"))
        self.assertLess(text.index("Researched areas"), text.index("Original seed hypotheses"))
        self.assertNotIn("Issue and linked PR appendix", text)
        self.assertNotIn("insufficient evidence", text)
        self.assertNotIn("Hypothesis 1 candidate signals", text)

    def test_callback_only_after_successful_pdf_and_return_api_unchanged(self):
        callback = Mock()
        order = []
        callback.side_effect = lambda state: order.append("callback")
        with patch.object(report, "load_scored_issues", return_value=[]), \
                patch.object(Path, "mkdir"), \
                patch.object(report, "_write_pdf", side_effect=lambda *args, **kwargs: order.append("pdf")):
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
