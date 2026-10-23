"""Deterministic weekly evidence observations; persistence belongs to the caller."""

import copy
from datetime import date, datetime, timedelta, timezone
import os
import re


VERSION = 3
EXCLUDED_OWNERS_ENV = "HYPOTHESIS_EXCLUDED_OWNERS"
MS_OWNERS = {
    "microsoft", "azure", "aks", "microsoftdocs", "azure-samples",
    "azuread", "azure-devops", "dotnet", "powershell", "windows",
    "windowsserverdocs", "microsoftgraph", "microsoft-docker",
}
# A signal is an area AND a concrete barrier intent, not a topic frequency.
TAXONOMY = (
    ("networking:dns", "networking", r"\bdns\b|name resolution", "DNS resolution failures"),
    ("networking:connectivity", "networking", r"\b(network\w*|connectivity|nat|proxy|ports?|hns)\b", "network connectivity barriers"),
    ("identity:gmsa", "identity", r"\bgmsa\b|group managed service", "gMSA authentication barriers"),
    ("identity:authentication", "identity", r"\b(authenticat\w*|credentials?|kerberos|identity)\b", "authentication barriers"),
    ("images:nano-api", "images/Nano", r"\bnano[\s-]*server\b", "Nano Server API/testing limitations"),
    ("runtime:compatibility", "runtime/compatibility", r"\b(compatib\w*|kernel|host version|hcsshim|runhcs|runtime|isolation)\b", "runtime/host compatibility barriers"),
    ("images:size", "images/Nano", r"\b(image size|large images?|image pull|base image)\b", "image size/distribution barriers"),
    ("tooling:build", "tooling/build", r"\b(build\w*|dockerfile|tooling|compiler|ci pipeline)\b", "build/tooling failures"),
    ("licensing:cost", "licensing/cost", r"\b(licen[cs]\w*|costs?|expensive|paid|not free)\b", "licensing/cost barriers"),
    ("community:adoption", "community", r"\b(community|adoption|unpopular|open.source)\b", "community/adoption barriers"),
)
AREAS = tuple(dict.fromkeys(item[1] for item in TAXONOMY))
BARRIER = re.compile(
    r"\b(fail\w*|broken|missing|lack\w*|unsupported|unavailable|limit\w*|"
    r"blocked?|blocking|barriers?|cannot|can't|unable|too expensive|not free|"
    r"prevents?|avoid\w*|unpopular|doesn't work|does not work)\b", re.I)
CONTRARY = re.compile(
    r"\b(not (?:a |an? )?(?:barrier|problem|limitation)|no longer|"
    r"(?:is |was |are |has been )?(?:resolved|fixed)|tests pass|now supports|"
    r"works (?:now|correctly)|does not prevent|do not avoid)\b", re.I)
WINDOWS = re.compile(r"\b(windows[\s-]+(?:containers?|server)|nano[\s-]*server|hcsshim|runhcs)\b", re.I)


def _time(value):
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError("Invalid observation timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Timestamp must be timezone aware")
    return parsed.astimezone(timezone.utc)


def _week(value):
    iso = value.isocalendar()
    return f"{iso.year:04d}-W{iso.week:02d}"


def _monday(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-W\d{2}", value):
        raise ValueError("Invalid observation week")
    year, week = value.split("-W")
    return date.fromisocalendar(int(year), int(week), 1)


def validate_state(state):
    """Validate the bounded JSON v3 schema rather than silently resetting it."""
    def integer(value, minimum, maximum):
        return type(value) is int and minimum <= value <= maximum

    def slot(item, expected_id, observed_at):
        if not isinstance(item, dict) or set(item) != {
                "id", "key", "title", "area", "status", "streak", "fresh_repos", "witnesses"}:
            raise ValueError("Invalid hypothesis slot fields")
        if item["id"] != expected_id:
            raise ValueError("Invalid hypothesis slot id")
        key = item["key"]
        if not isinstance(key, str) or key not in {"original:h1", "original:h2", *(row[0] for row in TAXONOMY)}:
            raise ValueError("Unknown hypothesis signal id")
        if key.startswith("original:") and key != f"original:{expected_id.lower()}":
            raise ValueError("Original hypothesis belongs to another slot")
        if (not isinstance(item["title"], str) or not 1 <= len(item["title"]) <= 400
                or not item["title"].isascii()):
            raise ValueError("Invalid hypothesis title")
        if item["area"] not in (*AREAS, "original"):
            raise ValueError("Invalid hypothesis area")
        expected_area = "original" if key.startswith("original:") else next(
            row[1] for row in TAXONOMY if row[0] == key)
        if item["area"] != expected_area:
            raise ValueError("Hypothesis area does not match signal")
        if item["status"] not in ("deferred", "provisional", "qualifying", "completed"):
            raise ValueError("Invalid hypothesis status")
        if not integer(item["streak"], 0, 3) or not integer(item["fresh_repos"], 0, 1000000):
            raise ValueError("Invalid hypothesis counts")
        if ((item["status"] == "completed") != (item["streak"] == 3)
                or (item["status"] == "qualifying") != (item["streak"] in (1, 2))
                or (item["streak"] and item["fresh_repos"] < 3)
                or (item["status"] == "deferred" and item["fresh_repos"])):
            raise ValueError("Inconsistent hypothesis status/streak")
        witnesses = item["witnesses"]
        if not isinstance(witnesses, list) or len(witnesses) != min(3, item["fresh_repos"]):
            raise ValueError("Invalid bounded hypothesis witnesses")
        repositories = set()
        for witness in witnesses:
            if not isinstance(witness, dict) or set(witness) != {"repo", "url", "date", "source"}:
                raise ValueError("Invalid witness fields")
            repo = witness["repo"]
            if (not isinstance(repo, str) or len(repo) > 160
                    or repo != repo.casefold() or not re.fullmatch(r"[\w.-]+/[\w.-]+", repo, re.ASCII)
                    or repo in repositories):
                raise ValueError("Invalid or duplicate witness repository")
            repositories.add(repo)
            url = witness["url"]
            if (not isinstance(url, str) or len(url) > 240 or not url.isascii()
                    or not url.casefold().startswith(f"https://github.com/{repo}/")):
                raise ValueError("Invalid bounded witness URL")
            if (not isinstance(witness["date"], str) or len(witness["date"]) > 40
                    or _time(witness["date"]) > observed_at):
                raise ValueError("Invalid witness timestamp")
            if witness["source"] not in ("issue", "issue_comment", "pull_request", "pull_request_comment"):
                raise ValueError("Invalid witness source type")

    if not isinstance(state, dict) or set(state) != {
            "version", "observed_week", "observed_at", "slots", "history", "completed"}:
        raise ValueError("Invalid evolution state fields")
    if type(state["version"]) is not int or state["version"] != VERSION:
        raise ValueError("Unsupported evolution state version")
    observed = _monday(state["observed_week"])
    observed_at = _time(state["observed_at"])
    if _week(observed_at) != state["observed_week"]:
        raise ValueError("Observation timestamp does not match UTC ISO week")
    if not isinstance(state["slots"], list) or len(state["slots"]) != 2:
        raise ValueError("Expected H1 and H2")
    for item, identity in zip(state["slots"], ("H1", "H2")):
        slot(item, identity, observed_at)
    if state["slots"][0]["key"] == state["slots"][1]["key"]:
        raise ValueError("Duplicate hypotheses")
    history = state["history"]
    if not isinstance(history, list) or not 1 <= len(history) <= 12:
        raise ValueError("Invalid bounded observation history")
    last = None
    last_at = None
    for entry in history:
        if not isinstance(entry, dict) or set(entry) != {"week", "observed_at", "slots"}:
            raise ValueError("Invalid historical observation")
        current = _monday(entry["week"])
        if current > observed or (last is not None and current <= last):
            raise ValueError("Invalid history chronology")
        current_at = _time(entry["observed_at"])
        if (_week(current_at) != entry["week"] or current_at > observed_at
                or (last_at is not None and current_at <= last_at)):
            raise ValueError("Invalid observation timestamp chronology")
        if not isinstance(entry["slots"], list) or len(entry["slots"]) != 2:
            raise ValueError("Invalid historical slots")
        for item, identity in zip(entry["slots"], ("H1", "H2")):
            slot(item, identity, current_at)
        if entry["slots"][0]["key"] == entry["slots"][1]["key"]:
            raise ValueError("Duplicate historical hypotheses")
        last = current
        last_at = current_at
    if history[-1] != {"week": state["observed_week"], "observed_at": state["observed_at"],
                       "slots": state["slots"]}:
        raise ValueError("Latest history differs from frozen decision")
    completed = state["completed"]
    expected = [{"id": item["id"], "key": item["key"]} for item in state["slots"]
                if item["status"] == "completed"]
    if completed != expected:
        raise ValueError("Invalid completed archive")
    return state


def _eligible(repository):
    name = repository.get("nameWithOwner", "").casefold()
    excluded = MS_OWNERS | {
        item.strip().casefold() for item in
        os.environ.get(EXCLUDED_OWNERS_ENV, "").split(",") if item.strip()
    }
    return (repository.get("isPrivate") is False and "/" in name
            and name.split("/")[0] not in excluded)


def _reserved_keys(item):
    aliases = {"original:h1": {"licensing:cost", "community:adoption"},
               "original:h2": {"images:nano-api"}}
    return {item["key"]} | aliases.get(item["key"], set())


def _category(paragraph):
    # Preserve affirmative failure/cost wording; otherwise err toward uncertainty.
    normalized = re.sub(r"\b(not free|does not work|doesn't work)\b", "barrier", paragraph, flags=re.I)
    negated = re.search(r"\b(no|not|never|without|isn't|aren't|doesn't|don't)\b", normalized, re.I)
    contrary = CONTRARY.search(paragraph)
    if contrary and not re.search(r"\b(not|never)\s+(?:yet\s+)?(?:fixed|resolved)\b", paragraph, re.I):
        return "contradicting"
    return "supporting" if BARRIER.search(paragraph) and not negated else "inconclusive"


def _signals(records, now, triage, window_start):
    signals = []
    seen = set()
    for record in records:
        for label, node in (("Issue", record["issue"]), ("PR", record["pr"])):
            if node is None:
                continue
            repo = node.get("repository", {})
            name = repo.get("nameWithOwner", "").casefold()
            context = node["title"] + "\n\n" + node["body"]
            docs = [(context, node["url"], node["updatedAt"])]
            docs += [(c["body"], c["url"], c["updatedAt"]) for c in node["comments"]["nodes"]]
            # Each source carries its OWN repository, never its referring issue's.
            local = dict(record, issue=node, pr=None)
            original = {i: triage(local, i) for i in (1, 2)}
            for text, url, updated in docs:
                if not WINDOWS.search(context + "\n" + text):
                    continue
                when = _time(updated)
                source_type = ("issue" if label == "Issue" else "pull_request")
                if url != node["url"]:
                    source_type += "_comment"
                for paragraph in re.split(r"\n\s*\n", text):
                    matched = [row for row in TAXONOMY if re.search(row[2], paragraph, re.I)]
                    # More specific intents take precedence within the same area.
                    areas = set()
                    for key, area, _, description in matched:
                        if area in areas:
                            continue
                        areas.add(area)
                        category = _category(paragraph)
                        signals.append(dict(key=key, area=area, title="Windows container " + description,
                                            category=category, repo=name, eligible=_eligible(repo),
                                            fresh=window_start < when <= now,
                                            url=url, date=updated, excerpt=" ".join(paragraph.split())[:1800],
                                            source=source_type))
                for i in (1, 2):
                    for candidate in original[i]:
                        if candidate["url"] == url:
                            signals.append(dict(candidate, key=f"original:h{i}", area="original",
                                                title="", repo=name, eligible=_eligible(repo),
                                                fresh=window_start < when <= now, source=source_type))
    result = []
    for signal in signals:
        identity = (signal["key"], signal["url"], signal["excerpt"], signal["category"])
        if identity not in seen:
            seen.add(identity)
            result.append(signal)
    return result


def evolve(records, now, previous_state, hypotheses, triage):
    """Return a bounded JSON state and current evidence report, without I/O."""
    now = now.astimezone(timezone.utc)
    week = _week(now)
    if previous_state is not None:
        validate_state(previous_state)
        if _time(previous_state["observed_at"]) > now:
            raise ValueError("Observation time precedes durable state")
    consecutive = previous_state is not None and (
        _monday(week) - _monday(previous_state["observed_week"]) == timedelta(days=7))
    window_start = now - timedelta(days=7)
    if consecutive:
        window_start = max(window_start, _time(previous_state["observed_at"]))
    signals = _signals(records, now, triage, window_start)
    groups = {}
    for signal in signals:
        group = groups.setdefault(signal["key"], {"repos": set(), "fresh": set(), "issues": set(),
                                                  "area": signal["area"], "title": signal["title"]})
        if signal["eligible"] and signal["category"] == "supporting":
            group["repos"].add(signal["repo"])
            if signal["fresh"]:
                group["fresh"].add(signal["repo"])
            if "/issues/" in signal["url"]:
                group["issues"].add(signal["url"].split("#")[0])
    ranked = sorted((key for key, group in groups.items()
                     if not key.startswith("original:") and group["repos"]),
                    key=lambda key: (-len(groups[key]["repos"]), -len(groups[key]["issues"]), key))
    frozen = previous_state is not None and previous_state["observed_week"] == week
    if frozen:
        state = copy.deepcopy(previous_state)
    else:
        old = previous_state["slots"] if previous_state else [
            dict(id=f"H{i}", key=f"original:h{i}", title=title, area="original",
                 status="deferred", streak=0, fresh_repos=0, witnesses=[])
            for i, title in enumerate(hypotheses, 1)]
        # Reserve supported/completed slots before replacing either original.
        reserved = set()
        for item in old:
            group = groups.get(item["key"], {})
            if item["status"] == "completed" or len(group.get("fresh", ())) >= 3:
                reserved.update(_reserved_keys(item))
        slots = []
        for item in old:
            item = copy.deepcopy(item)
            if item["status"] == "completed":
                slots.append(item)
                continue
            group = groups.get(item["key"], {})
            count = len(group.get("fresh", ()))
            if count >= 3 and (previous_state is not None or item["key"].startswith("original:")):
                item["streak"] = (item["streak"] if consecutive else 0) + 1
                item["status"] = "completed" if item["streak"] == 3 else "qualifying"
            else:
                # Historical discovery is exploratory; fresh replacement evidence
                # can qualify this week, never any earlier observation.
                chosen = next((key for key in ranked if key not in reserved), None)
                if chosen is not None:
                    group = groups[chosen]
                    item.update(key=chosen, title=group["title"], area=group["area"])
                    count = len(group["fresh"])
                elif item["key"] in reserved or item["key"].startswith("original:"):
                    index = int(item["id"][1:]) - 1
                    item.update(key=f"original:h{index + 1}", title=hypotheses[index], area="original")
                    group = {}
                    count = 0
                item["streak"] = 1 if count >= 3 else 0
                item["status"] = ("qualifying" if count >= 3 else
                                  "provisional" if (chosen or group.get("repos")) else "deferred")
            item["fresh_repos"] = count
            witnesses = {}
            for signal in sorted(signals, key=lambda s: (s["repo"], s["url"], s["date"], s["source"])):
                if (signal["key"] == item["key"] and signal["eligible"] and signal["fresh"]
                        and signal["category"] == "supporting"):
                    witnesses.setdefault(signal["repo"], {
                        "repo": signal["repo"], "url": signal["url"],
                        "date": signal["date"], "source": signal["source"],
                    })
            item["witnesses"] = list(witnesses.values())[:3] if count else []
            reserved.update(_reserved_keys(item))
            slots.append(item)
        history = copy.deepcopy(previous_state["history"]) if previous_state else []
        history.append({"week": week, "observed_at": now.isoformat(), "slots": copy.deepcopy(slots)})
        state = dict(version=VERSION, observed_week=week, observed_at=now.isoformat(),
                     slots=slots, history=history[-12:],
                     completed=[{"id": s["id"], "key": s["key"]} for s in slots if s["status"] == "completed"])
        validate_state(state)
    lines = [
        "", "Adaptive weekly hypotheses (authoritative latest decisions)",
        f"Observation week: {state['observed_week']} | first successful observation is frozen.",
        "Same-week rerun: frozen decision; evidence below is current." if frozen else
        "New weekly observation; any original/proposed/revised signal with >=3 fresh eligible repositories qualifies week one.",
    ]
    for slot in state["slots"]:
        lines.extend([f"{slot['id']} [{slot['key']}] {slot['title']}",
                      f"Decision: {slot['status']} | area: {slot['area']} | streak: {slot['streak']}/3 | frozen fresh repositories: {slot['fresh_repos']}"])
        lines.append("Frozen decision witnesses (up to 3 distinct eligible supporting repositories):")
        for witness in slot["witnesses"]:
            lines.extend([f"  {witness['repo']} | {witness['source']} | {witness['date']}",
                          "  " + witness["url"]])
        if not slot["witnesses"]:
            lines.append("  None; no fresh repository support was recorded for this decision.")
    lines.append("Researched areas (current evidence, not validated scientific findings):")
    lines.append("Area totals describe research coverage only; qualification requires >=3 fresh eligible repositories for the SAME signal.")
    for area in AREAS:
        matches = [s for s in signals if s["area"] == area]
        if not matches:
            lines.append(f"  {area}: no detected evidence; deferred")
            continue
        supporting = [s for s in matches if s["eligible"] and s["category"] == "supporting"]
        repositories = {s["repo"] for s in supporting}
        fresh = {s["repo"] for s in supporting if s["fresh"]}
        lines.append(f"  {area}: {len(repositories)} distinct eligible supporting repositories; "
                     f"{len(fresh)} fresh. Area totals are not qualifications or validations.")
        for key in sorted({s["key"] for s in matches}):
            group = groups[key]
            status = "MET" if len(group["fresh"]) >= 3 else "NOT MET (provisional / insufficient fresh recurrence)"
            lines.extend([
                f"    {key}: eligible supporting repositories={len(group['repos'])}; "
                f"fresh={len(group['fresh'])}; current >=3 fresh repository threshold: {status}.",
                "    Supporting repositories: " + (", ".join(sorted(group["repos"])) or "none"),
                "    Fresh supporting repositories: " + (", ".join(sorted(group["fresh"])) or "none"),
            ])
    lines.extend([
        "Adaptive method and limitations",
        "Qualifying = same signal in >=3 eligible public repositories with fresh source evidence.",
        "Retained candidates need 3 consecutive qualifying weeks to complete; missing weeks reset streaks.",
        "Completed hypotheses exit further evolution/streak increments; reports continue.",
        "This is deterministic evidence triage, not scientific proof, validation or causal inference.",
        "Independent means distinct full repository names, NOT distinct owners or independent people.",
        "Only API isPrivate=false sources count; Microsoft/AKS sources remain context only.",
        f"Excluded owners: {', '.join(sorted(MS_OWNERS))}; additional owners: {EXCLUDED_OWNERS_ENV}.",
        f"Current evidence window (UTC): ({window_start.isoformat()}, {now.isoformat()}].",
        "Freshness uses source/comment updatedAt in the trailing 7 days, excluding the lower boundary.",
        "For consecutive weekly observations, the window starts no earlier than the prior observed_at.",
        "This prevents overlapping evidence reuse; missing weeks use only the last 7 days and reset streaks.",
        "ISO weeks freeze decisions, not source dates. Historical sources are discovery only; future dates never count.",
        "Issue/PR updatedAt may reflect unrelated metadata, not a changed claim; comment dates are source-specific.",
        "Only latest 100 comments and most recently updated linked PR are examined.",
        "Ranking: unique eligible supporting repositories, then unique issue URLs (not comments/PR boosts), then signal id.",
        "Taxonomy: area + explicit barrier intent; specific DNS/gMSA/Nano intents take precedence.",
        "Negation/resolution/non-barriers are separate contrary/inconclusive evidence, never supporting votes.",
    ])
    lines.append("Signal taxonomy: " + "; ".join(f"{key} = {description}" for key, _, _, description in TAXONOMY))
    lines.append("Current evidence (separate from frozen weekly decision):")
    for signal in sorted(signals, key=lambda s: (s["key"], s["repo"], s["url"], s["excerpt"])):
        lines.extend([
            f"{signal['key']} | {signal['category']} | repository: {signal['repo']} | "
            f"{'eligible public' if signal['eligible'] else 'context only / excluded or unverified public'} | "
            f"{'fresh' if signal['fresh'] else 'historical'} | {signal['date']}",
            signal["url"], "Context excerpt: " + signal["excerpt"],
        ])
    if not signals:
        lines.append("No relevant evidence; defer without inventing a hypothesis.")
    return state, lines
