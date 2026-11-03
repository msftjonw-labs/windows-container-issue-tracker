"""Deterministic weekly evidence observations; persistence belongs to the caller."""

import copy
from datetime import date, datetime, timedelta, timezone
import os
import re


VERSION = 4
SLOT_COUNT = 5
SLOT_IDS = tuple(f"H{index}" for index in range(1, SLOT_COUNT + 1))
UNASSIGNED = "unassigned"
TRIAL_WEEKS = 4
COOLDOWN_WEEKS = 8
SUPPORT_THRESHOLD = 3
REPO_CAP = 10
RETIRED_CAP = 16
HISTORY_CAP = 12
OUTCOMES = ("supported", "contradicted", "inconclusive")
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
# Context qualifiers refine a base signal into a narrower, separately testable
# hypothesis (base@context) using only text already fetched for the base signal.
CONTEXTS = (
    ("ltsc2019", r"\bltsc[\s-]?2019\b|\bserver[\s-]+2019\b|\b17763\b", "on Windows Server 2019/LTSC 2019"),
    ("ltsc2022", r"\bltsc[\s-]?2022\b|\bserver[\s-]+2022\b|\b20348\b", "on Windows Server 2022/LTSC 2022"),
    ("ltsc2025", r"\bltsc[\s-]?2025\b|\bserver[\s-]+2025\b|\b26100\b", "on Windows Server 2025/LTSC 2025"),
    ("hyperv", r"\bhyper[\s-]?v\b", "involving Hyper-V"),
    ("process", r"\bprocess[\s-]+isolat\w*|\bisolation[\s=:]+process\b", "with process isolation"),
    ("kubernetes", r"\b(kubernetes|k8s|aks|kubelet)\b", "on Kubernetes"),
    ("containerd", r"\bcontainerd\b", "on containerd"),
    ("docker", r"\bdocker\b", "on Docker"),
)
AREAS = tuple(dict.fromkeys(item[1] for item in TAXONOMY))
BASE_KEYS = {row[0]: row for row in TAXONOMY}
CONTEXT_KEYS = {row[0]: row for row in CONTEXTS}
ALIASES = {"original:h1": {"licensing:cost", "community:adoption"},
           "original:h2": {"images:nano-api"}}
BARRIER = re.compile(
    r"\b(fail\w*|broken|missing|lack\w*|unsupported|unavailable|limit\w*|"
    r"blocked?|blocking|barriers?|cannot|can't|unable|too expensive|not free|"
    r"prevents?|avoid\w*|unpopular|doesn't work|does not work)\b", re.I)
CONTRARY = re.compile(
    r"\b(not (?:a |an? )?(?:barrier|problem|limitation)|no longer|"
    r"(?:is |was |are |has been )?(?:resolved|fixed)|tests pass|now supports|"
    r"works (?:now|correctly)|does not prevent|do not avoid)\b", re.I)
WINDOWS = re.compile(r"\b(windows[\s-]+(?:containers?|server)|nano[\s-]*server|hcsshim|runhcs)\b", re.I)
SLIM_FIELDS = ("id", "key", "status", "trial_week", "fresh_repos")
SLOT_FIELDS = {*SLIM_FIELDS, "title", "area", "started_week", "supporting", "contradicting", "witnesses"}
RETIRED_FIELDS = {"id", "key", "outcome", "started_week", "ended_week",
                  "supporting", "contradicting", "witnesses"}


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


def _base(key):
    return key.split("@", 1)[0]


def _valid_key(key):
    if not isinstance(key, str):
        return False
    if key in ALIASES:
        return True
    base, separator, context = key.partition("@")
    return base in BASE_KEYS and (not separator or context in CONTEXT_KEYS)


def _area(key):
    if key == UNASSIGNED:
        return UNASSIGNED
    return "original" if key in ALIASES else BASE_KEYS[_base(key)][1]


def _title(key, hypotheses):
    if key == UNASSIGNED:
        return "No eligible candidate; slot deferred"
    if key in ALIASES:
        return hypotheses[int(key[-1]) - 1]
    base, _, context = key.partition("@")
    title = "Windows container " + BASE_KEYS[base][3]
    return title + (" " + CONTEXT_KEYS[context][2] if context else "")


def _overlap(key):
    """Keys that must not be tested concurrently with this one."""
    base = _base(key)
    keys = {key, base}
    for original, aliases in ALIASES.items():
        if base == original or base in aliases:
            keys |= {original} | aliases
    return keys


def _outcome(supporting, contradicting):
    if supporting >= SUPPORT_THRESHOLD and supporting > contradicting:
        return "supported"
    if contradicting and contradicting >= supporting:
        return "contradicted"
    return "inconclusive"


def _available_week(ended_week):
    return _week(_monday(ended_week) + timedelta(weeks=COOLDOWN_WEEKS))


def _cooling(retired, week):
    current = _monday(week)
    return {item["key"]: _available_week(item["ended_week"]) for item in retired
            if current < _monday(item["ended_week"]) + timedelta(weeks=COOLDOWN_WEEKS)}


def _repo(value):
    return (isinstance(value, str) and len(value) <= 160 and value == value.casefold()
            and bool(re.fullmatch(r"[\w.-]+/[\w.-]+", value, re.ASCII)))


def _validate_witnesses(witnesses, observed_at, allowed):
    if not isinstance(witnesses, list) or len(witnesses) > 3:
        raise ValueError("Invalid bounded hypothesis witnesses")
    repositories = set()
    for witness in witnesses:
        if not isinstance(witness, dict) or set(witness) != {"repo", "url", "date", "source"}:
            raise ValueError("Invalid witness fields")
        repo = witness["repo"]
        if not _repo(repo) or repo in repositories or (allowed is not None and repo not in allowed):
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


def validate_state(state):
    """Validate the bounded JSON v4 schema rather than silently resetting it."""
    def integer(value, minimum, maximum):
        return type(value) is int and minimum <= value <= maximum

    def slim(item, expected_id):
        if not isinstance(item, dict) or set(item) != set(SLIM_FIELDS):
            raise ValueError("Invalid hypothesis slot fields")
        if item["id"] != expected_id:
            raise ValueError("Invalid hypothesis slot id")
        if not (_valid_key(item["key"]) or (item["key"] == UNASSIGNED and item["status"] == "deferred")):
            raise ValueError("Unknown hypothesis signal id")
        if item["status"] not in ("testing", "deferred"):
            raise ValueError("Invalid hypothesis status")
        if (not integer(item["trial_week"], 0, TRIAL_WEEKS - 1)
                or not integer(item["fresh_repos"], 0, 1000000)):
            raise ValueError("Invalid hypothesis counts")
        if ((item["status"] == "deferred") != (item["trial_week"] == 0)
                or (item["status"] == "deferred" and item["fresh_repos"])):
            raise ValueError("Inconsistent hypothesis status/trial")

    def repositories(value):
        if (not isinstance(value, list) or len(value) > REPO_CAP
                or not all(_repo(item) for item in value) or value != sorted(set(value))):
            raise ValueError("Invalid bounded trial repositories")

    def slot(item, expected_id, observed, observed_at):
        if not isinstance(item, dict) or set(item) != SLOT_FIELDS:
            raise ValueError("Invalid hypothesis slot fields")
        slim({field: item[field] for field in SLIM_FIELDS}, expected_id)
        key = item["key"]
        if (not isinstance(item["title"], str) or not 1 <= len(item["title"]) <= 400
                or not item["title"].isascii()
                or (key not in ALIASES and item["title"] != _title(key, ()))):
            raise ValueError("Invalid hypothesis title")
        if item["area"] != _area(key):
            raise ValueError("Hypothesis area does not match signal")
        repositories(item["supporting"])
        repositories(item["contradicting"])
        if item["status"] == "deferred":
            if (key not in (*ALIASES, UNASSIGNED) or item["started_week"] is not None or item["supporting"]
                    or item["contradicting"] or item["witnesses"]):
                raise ValueError("Deferred hypothesis must be an untested placeholder")
        else:
            started = _monday(item["started_week"])
            if started > observed or (observed - started).days // 7 + 1 < item["trial_week"]:
                raise ValueError("Invalid hypothesis trial start")
            if item["fresh_repos"] and not item["supporting"]:
                raise ValueError("Fresh support missing from trial repositories")
        _validate_witnesses(item["witnesses"], observed_at, set(item["supporting"]))
        if len(item["witnesses"]) != min(3, len(item["supporting"])):
            raise ValueError("Invalid bounded hypothesis witnesses")

    if not isinstance(state, dict) or set(state) != {
            "version", "observed_week", "observed_at", "slots", "history", "retired"}:
        raise ValueError("Invalid evolution state fields")
    if type(state["version"]) is not int or state["version"] != VERSION:
        raise ValueError("Unsupported evolution state version")
    observed = _monday(state["observed_week"])
    observed_at = _time(state["observed_at"])
    if _week(observed_at) != state["observed_week"]:
        raise ValueError("Observation timestamp does not match UTC ISO week")
    if not isinstance(state["slots"], list) or len(state["slots"]) != SLOT_COUNT:
        raise ValueError(f"Expected slots {', '.join(SLOT_IDS)}")
    for item, identity in zip(state["slots"], SLOT_IDS):
        slot(item, identity, observed, observed_at)
    _unique(state["slots"], "Duplicate hypotheses")
    testing = [item["key"] for item in state["slots"] if item["status"] == "testing"]
    for index, key in enumerate(testing):
        if any(_overlap(key) & _overlap(other) for other in testing[index + 1:]):
            raise ValueError("Overlapping hypotheses tested concurrently")
    history = state["history"]
    if not isinstance(history, list) or not 1 <= len(history) <= HISTORY_CAP:
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
        if not isinstance(entry["slots"], list) or len(entry["slots"]) != SLOT_COUNT:
            raise ValueError("Invalid historical slots")
        for item, identity in zip(entry["slots"], SLOT_IDS):
            slim(item, identity)
        _unique(entry["slots"], "Duplicate historical hypotheses")
        last = current
        last_at = current_at
    if history[-1] != {"week": state["observed_week"], "observed_at": state["observed_at"],
                       "slots": [{field: item[field] for field in SLIM_FIELDS}
                                 for item in state["slots"]]}:
        raise ValueError("Latest history differs from frozen decision")
    retired = state["retired"]
    if not isinstance(retired, list) or len(retired) > RETIRED_CAP:
        raise ValueError("Invalid bounded retired hypotheses")
    last = None
    for item in retired:
        if not isinstance(item, dict) or set(item) != RETIRED_FIELDS:
            raise ValueError("Invalid retired hypothesis fields")
        if item["id"] not in SLOT_IDS or not _valid_key(item["key"]):
            raise ValueError("Invalid retired hypothesis identity")
        started, ended = _monday(item["started_week"]), _monday(item["ended_week"])
        if started > ended or ended > observed or (last is not None and ended < last):
            raise ValueError("Invalid retired hypothesis chronology")
        if (not integer(item["supporting"], 0, REPO_CAP)
                or not integer(item["contradicting"], 0, REPO_CAP)
                or item["outcome"] != _outcome(item["supporting"], item["contradicting"])):
            raise ValueError("Inconsistent retired hypothesis outcome")
        _validate_witnesses(item["witnesses"], observed_at, None)
        if len(item["witnesses"]) != min(3, item["supporting"]):
            raise ValueError("Invalid retired hypothesis witnesses")
        last = ended
    return state


def _unique(slots, message):
    keys = [item["key"] for item in slots if item["key"] != UNASSIGNED]
    if len(keys) != len(set(keys)):
        raise ValueError(message)


def _placeholder(index, used, hypotheses):
    """Deferred slots show an unused seed hypothesis, else an unassigned marker."""
    key = next((seed for seed in sorted(ALIASES) if seed not in used), UNASSIGNED)
    return dict(id=SLOT_IDS[index], key=key, title=_title(key, hypotheses), area=_area(key),
                status="deferred", trial_week=0, started_week=None, fresh_repos=0,
                supporting=[], contradicting=[], witnesses=[])


def _migrate_v3(state, hypotheses):
    """Carry v3 provisional/qualifying runs forward as v4 trials without new evidence."""
    try:
        runs = {}
        history = []
        for entry in state["history"]:
            slims = []
            for item in entry["slots"]:
                previous = runs.get(item["id"])
                if item["status"] in ("provisional", "qualifying"):
                    if previous and previous["length"] and previous["key"] == item["key"]:
                        run = dict(previous, length=previous["length"] + 1)
                    else:
                        run = dict(key=item["key"], length=1, started=entry["week"], witnesses=[])
                    seen = {witness["repo"] for witness in run["witnesses"]}
                    run["witnesses"] = run["witnesses"] + [
                        witness for witness in item["witnesses"] if witness["repo"] not in seen]
                    runs[item["id"]] = run
                    slims.append(dict(id=item["id"], key=item["key"], status="testing",
                                      trial_week=min(run["length"], TRIAL_WEEKS - 1),
                                      fresh_repos=item["fresh_repos"]))
                else:
                    runs[item["id"]] = dict(key=item["key"], length=0, started=entry["week"], witnesses=[])
                    slims.append(dict(id=item["id"], key=item["key"], status="deferred",
                                      trial_week=0, fresh_repos=0))
            slims += [dict(id=identity, key=UNASSIGNED, status="deferred", trial_week=0, fresh_repos=0)
                      for identity in SLOT_IDS[len(slims):]]
            history.append(dict(week=entry["week"], observed_at=entry["observed_at"], slots=slims))
        slots, retired = [None] * SLOT_COUNT, []
        for index, item in enumerate(state["slots"]):
            run = runs.get(item["id"], {"witnesses": [], "started": state["observed_week"]})
            repos = sorted({witness["repo"] for witness in run["witnesses"]})[:REPO_CAP]
            witnesses = [witness for witness in run["witnesses"] if witness["repo"] in repos][:3]
            if item["status"] == "completed":
                retired.append(dict(id=item["id"], key=item["key"], outcome=_outcome(len(repos), 0),
                                    started_week=run["started"], ended_week=state["observed_week"],
                                    supporting=len(repos), contradicting=0, witnesses=witnesses))
            elif item["status"] in ("provisional", "qualifying"):
                slots[index] = dict(
                    id=item["id"], key=item["key"], area=_area(item["key"]),
                    title=item["title"] if item["key"] in ALIASES else _title(item["key"], hypotheses),
                    status="testing", trial_week=min(run["length"], TRIAL_WEEKS - 1),
                    started_week=run["started"], fresh_repos=item["fresh_repos"],
                    supporting=repos, contradicting=[], witnesses=witnesses)
        used = {item["key"] for item in slots if item}
        for index in range(SLOT_COUNT):
            if slots[index] is None:
                slots[index] = _placeholder(index, used, hypotheses)
                used.add(slots[index]["key"])
        history = history[-HISTORY_CAP:]
        history[-1]["slots"] = [{field: item[field] for field in SLIM_FIELDS} for item in slots]
        return dict(version=VERSION, observed_week=state["observed_week"],
                    observed_at=state["observed_at"], slots=slots, history=history, retired=retired)
    except (KeyError, TypeError, AttributeError, IndexError) as error:
        raise ValueError("Invalid v3 evolution state") from error


def upgrade_state(state, hypotheses):
    """Validate current state, migrating the previous v3 schema once."""
    if isinstance(state, dict) and state.get("version") == 3 and type(state.get("version")) is int:
        state = _migrate_v3(state, hypotheses)
    return validate_state(state)


def _eligible(repository):
    name = repository.get("nameWithOwner", "").casefold()
    excluded = MS_OWNERS | {
        item.strip().casefold() for item in
        os.environ.get(EXCLUDED_OWNERS_ENV, "").split(",") if item.strip()
    }
    return (repository.get("isPrivate") is False and "/" in name
            and name.split("/")[0] not in excluded)


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
                    contexts = [row[0] for row in CONTEXTS
                                if re.search(row[1], paragraph + "\n" + node["title"], re.I)]
                    # More specific intents take precedence within the same area.
                    areas = set()
                    for key, area, _, _ in matched:
                        if area in areas:
                            continue
                        areas.add(area)
                        signal = dict(key=key, area=area, title=_title(key, ()),
                                      category=_category(paragraph), repo=name, eligible=_eligible(repo),
                                      fresh=window_start < when <= now,
                                      url=url, date=updated, excerpt=" ".join(paragraph.split())[:1800],
                                      source=source_type)
                        signals.append(signal)
                        for context in contexts:
                            refined = f"{key}@{context}"
                            signals.append(dict(signal, key=refined, title=_title(refined, ())))
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


URGENCY = {
    "p0": 0, "critical": 0, "blocker": 0,
    "p1": 1, "high": 1, "urgent": 1,
    "p2": 2, "medium": 2,
    "p3": 3, "low": 3,
    "p4": 4, "lowest": 4,
}


def _source_metadata(records, now):
    """Display metadata only; never alter the evidence used for weekly decisions."""
    metadata = {}
    for record in records:
        for node in (record["issue"], record["pr"]):
            if node is None:
                continue
            labels = []
            for label in node.get("labels", {}).get("nodes", []):
                match = re.fullmatch(
                    r"(?:(?:priority|severity)\s*[:/]\s*)?"
                    r"(p[0-4]|critical|blocker|high|urgent|medium|low|lowest)",
                    label["name"].strip(), re.I)
                if match:
                    labels.append((URGENCY[match[1].casefold()], label["name"]))
            urgency, urgency_label = min(labels) if labels else (5, "unlabeled/unknown")
            recent = {
                comment["url"] for comment in node["comments"]["nodes"]
                if comment.get("createdAt")
                and now - timedelta(days=7) < _time(comment["createdAt"]) <= now
            }
            info = dict(urgency=urgency, urgency_label=urgency_label, activity=len(recent),
                        title=" ".join(node["title"].split())[:160])
            for url in [node["url"], *(c["url"] for c in node["comments"]["nodes"])]:
                candidates = [info, metadata[url]] if url in metadata else [info]
                metadata[url] = min(candidates, key=lambda item: (
                    item["urgency"], -item["activity"], item["urgency_label"], item["title"]))
    return metadata


def select_supporting_sources(records, signals, now, current_keys=(), limit=5):
    """Rank unique source URLs, then prefer repository diversity, capped at five."""
    if type(limit) is not int or not 1 <= limit <= 5:
        raise ValueError("Source display limit must be between 1 and 5")
    metadata = _source_metadata(records, now)
    sources = {}
    current_keys = set(current_keys)
    for signal in signals:
        if (not signal["eligible"] or signal["category"] != "supporting"
                or _time(signal["date"]) > now):
            continue
        source = sources.setdefault(signal["url"], dict(
            url=signal["url"], repo=signal["repo"], source=signal["source"],
            date=signal["date"], keys=set(), excerpts=set(),
            **metadata[signal["url"]]))
        source["keys"].add(signal["key"])
        source["excerpts"].add(signal["excerpt"])
        source["date"] = max(source["date"], signal["date"], key=_time)
    ranked = sorted(sources.values(), key=lambda source: (
        not bool(source["keys"] & current_keys),
        source["urgency"], -source["activity"], -_time(source["date"]).timestamp(),
        source["url"]))
    selected, repositories = [], set()
    for source in ranked:
        if source["repo"] not in repositories:
            selected.append(dict(source, selection="best-ranked source for a distinct repository"))
            repositories.add(source["repo"])
        if len(selected) == limit:
            break
    selected_urls = {source["url"] for source in selected}
    for source in ranked:
        if len(selected) == limit:
            break
        if source["url"] not in selected_urls:
            selected.append(dict(source, selection="ranked fill after repository diversity"))
            selected_urls.add(source["url"])
    for source in selected:
        source["relevant"] = bool(source["keys"] & current_keys)
        source["keys"] = sorted(source["keys"])
        text = " | ".join(sorted(source.pop("excerpts")))
        source["excerpt"] = text if len(text) <= 500 else text[:497] + "..."
    return selected, len(sources)


def _source_lines(records, signals, now, current_keys):
    selected, total = select_supporting_sources(records, signals, now, current_keys)
    lines = [
        "Current supporting source showcase (separate from frozen historical decision references)",
        f"Supporting sources shown: {len(selected)} of {total} eligible unique URLs (maximum 5 globally).",
        "Selection uses exclusively verified public, non-Microsoft/non-excluded-owner sources.",
        "Source rank: current hypothesis key first; urgency label; recent discussion; updatedAt; URL.",
        "Urgency (case-insensitive exact labels): p0/critical/blocker > p1/high/urgent >",
        "p2/medium > p3/low > p4/lowest > unknown; optional 'priority:'/'priority/'",
        "or 'severity:'/'severity/' prefixes. No urgency inferred from prose or issue score.",
        "Diversity first: best ranked source per distinct repository, then ranked fills to five.",
        "Discussion = unique comments created in (now - 7 days, now], among latest 100 fetched;",
        "edits and future-created comments do not add activity. No author diversity is inferred.",
        "Comment urgency/activity use their parent issue/PR; source repository is always its own.",
    ]
    for source in selected:
        lines.extend([
            f"Current source | {source['source']} | repository: {source['repo']} | updated: {source['date']}",
            "Source title: " + source["title"],
            source["url"],
            "Supporting signal keys: " + ", ".join(source["keys"]),
            f"Selection reason: {source['selection']}; "
            f"{'matches current hypothesis' if source['relevant'] else 'other supporting hypothesis'}; "
            f"urgency: {source['urgency_label']}; recent 7-day comments: {source['activity']}.",
            "Context excerpt: " + source["excerpt"],
        ])
    if not selected:
        lines.append("No eligible supporting sources; showcase left empty, without padding.")
    for category in ("contradicting", "inconclusive"):
        matches = [signal for signal in signals if signal["category"] == category]
        lines.append(f"Other evidence - {category}: {len(matches)} candidate signals across "
                     f"{len({s['url'] for s in matches})} unique source URLs "
                     "(all researched sources, including excluded context; not supporting votes).")
    return lines




def _accumulate(slot, group):
    """Add this week's fresh, eligible repositories to a trial without reusing older evidence."""
    slot["fresh_repos"] = len(group["fresh"])
    for field, fresh in (("supporting", group["fresh"]), ("contradicting", group["fresh_contra"])):
        repositories = list(slot[field])
        for repo in sorted(fresh - set(repositories)):
            if len(repositories) < REPO_CAP:
                repositories.append(repo)
        slot[field] = sorted(repositories)
    witnessed = {witness["repo"] for witness in slot["witnesses"]}
    for repo in sorted(group["witnesses"]):
        if len(slot["witnesses"]) < 3 and repo not in witnessed and repo in slot["supporting"]:
            slot["witnesses"].append(dict(group["witnesses"][repo]))
            witnessed.add(repo)


def _decide(previous_state, groups, now, week, hypotheses):
    empty = dict(fresh=set(), fresh_contra=set(), witnesses={})
    retired = copy.deepcopy(previous_state["retired"]) if previous_state else []
    slots = (copy.deepcopy(previous_state["slots"]) if previous_state else
             [_placeholder(index, set(), hypotheses) for index in range(SLOT_COUNT)])
    for index, slot in enumerate(slots):
        if slot["status"] != "testing":
            slots[index] = None
            continue
        _accumulate(slot, groups.get(slot["key"], empty))
        slot["trial_week"] += 1
        if slot["trial_week"] >= TRIAL_WEEKS:
            supporting, contradicting = len(slot["supporting"]), len(slot["contradicting"])
            retired.append(dict(id=slot["id"], key=slot["key"],
                                outcome=_outcome(supporting, contradicting),
                                started_week=slot["started_week"], ended_week=week,
                                supporting=supporting, contradicting=contradicting,
                                witnesses=slot["witnesses"]))
            slots[index] = None
    retired = retired[-RETIRED_CAP:]
    cooling = _cooling(retired, week)
    tested = {item["key"] for item in retired}
    for index in range(SLOT_COUNT):
        if slots[index] is not None:
            continue
        blocked = set()
        for other in slots:
            if other is not None:
                blocked |= _overlap(other["key"])
        candidates = [key for key, group in groups.items()
                      if group["repos"] and key not in cooling and not _overlap(key) & blocked]
        if not candidates:
            continue
        # Fresh evidence first, then untested ideas, then all-time reach; seeds win ties.
        key = min(candidates, key=lambda key: (
            -len(groups[key]["fresh"]), key in tested, -len(groups[key]["repos"]),
            -len(groups[key]["issues"]), key not in ALIASES, key))
        slot = dict(id=f"H{index + 1}", key=key, title=_title(key, hypotheses), area=_area(key),
                    status="testing", trial_week=1, started_week=week, fresh_repos=0,
                    supporting=[], contradicting=[], witnesses=[])
        _accumulate(slot, groups[key])
        slots[index] = slot
    used = {slot["key"] for slot in slots if slot is not None}
    for index in range(SLOT_COUNT):
        if slots[index] is None:
            slots[index] = _placeholder(index, used, hypotheses)
            used.add(slots[index]["key"])
    history = copy.deepcopy(previous_state["history"]) if previous_state else []
    history.append({"week": week, "observed_at": now.isoformat(),
                    "slots": [{field: slot[field] for field in SLIM_FIELDS} for slot in slots]})
    state = dict(version=VERSION, observed_week=week, observed_at=now.isoformat(),
                 slots=slots, history=history[-HISTORY_CAP:], retired=retired)
    return validate_state(state)


def _witness_lines(witnesses, indent="  "):
    lines = []
    for witness in witnesses:
        lines.extend([f"{indent}{witness['repo']} | {witness['source']} | {witness['date']}",
                      indent + witness["url"]])
    return lines


def evolve(records, now, previous_state, hypotheses, triage):
    """Return a bounded JSON state and current evidence report, without I/O."""
    now = now.astimezone(timezone.utc)
    week = _week(now)
    if previous_state is not None:
        previous_state = upgrade_state(previous_state, hypotheses)
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
        group = groups.setdefault(signal["key"], dict(
            repos=set(), fresh=set(), contra=set(), fresh_contra=set(), issues=set(),
            witnesses={}, area=signal["area"]))
        if not signal["eligible"]:
            continue
        if signal["category"] == "supporting":
            group["repos"].add(signal["repo"])
            if signal["fresh"]:
                group["fresh"].add(signal["repo"])
            if "/issues/" in signal["url"]:
                group["issues"].add(signal["url"].split("#")[0])
        elif signal["category"] == "contradicting":
            group["contra"].add(signal["repo"])
            if signal["fresh"]:
                group["fresh_contra"].add(signal["repo"])
    for signal in sorted(signals, key=lambda s: (s["repo"], s["url"], s["date"], s["source"])):
        if signal["eligible"] and signal["fresh"] and signal["category"] == "supporting":
            groups[signal["key"]]["witnesses"].setdefault(signal["repo"], {
                "repo": signal["repo"], "url": signal["url"],
                "date": signal["date"], "source": signal["source"],
            })
    frozen = previous_state is not None and previous_state["observed_week"] == week
    state = (copy.deepcopy(previous_state) if frozen else
             _decide(previous_state, groups, now, week, hypotheses))
    cooling = _cooling(state["retired"], state["observed_week"])
    active = {slot["key"]: slot["id"] for slot in state["slots"] if slot["status"] == "testing"}
    lines = [
        "", "Adaptive weekly hypotheses (authoritative latest decisions)",
        f"Observation week: {state['observed_week']} | first successful observation is frozen.",
        "Same-week rerun: frozen decision; evidence below is current." if frozen else
        f"New weekly observation; each tested hypothesis accumulates fresh evidence over a {TRIAL_WEEKS}-week trial.",
    ]
    for slot in state["slots"]:
        lines.extend([
            f"{slot['id']} [{slot['key']}] {slot['title']}",
            f"Decision: {slot['status']} | area: {slot['area']} | trial week: {slot['trial_week']}/{TRIAL_WEEKS} | "
            f"started: {slot['started_week'] or 'not started'} | trial supporting repositories: "
            f"{len(slot['supporting'])} | trial contradicting repositories: {len(slot['contradicting'])} | "
            f"fresh this week: {slot['fresh_repos']}",
            "Trial witnesses (historical audit references only, up to 3 per slot; not current showcase):",
        ])
        lines.extend(_witness_lines(slot["witnesses"]) or
                     ["  None; no fresh repository support was recorded during this trial."])
    lines.append(f"Retired hypotheses (latest first; outcome after {TRIAL_WEEKS}-week trial; "
                 f"{COOLDOWN_WEEKS}-week cooldown before retesting):")
    for item in reversed(state["retired"]):
        lines.extend([
            f"  {item['ended_week']} {item['id']} [{item['key']}] outcome: {item['outcome']} | "
            f"supporting repositories: {item['supporting']} | contradicting repositories: "
            f"{item['contradicting']} | trial started: {item['started_week']} | "
            f"eligible again: {_available_week(item['ended_week'])}",
            *_witness_lines(item["witnesses"], "    "),
        ])
    if not state["retired"]:
        lines.append("  None yet; no trial has finished.")
    lines.append("Researched areas (current evidence, not validated scientific findings):")
    lines.append("Area totals describe research coverage only; trial outcomes require >=3 fresh eligible "
                 "repositories for the SAME signal, accumulated over its trial.")
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
            lifecycle = (f"testing in {active[key]}" if key in active else
                         f"cooldown until {cooling[key]}" if key in cooling else
                         "candidate" if group["repos"] else "no eligible support")
            lines.append(f"    {key}: eligible supporting repositories={len(group['repos'])}; "
                         f"fresh={len(group['fresh'])}; contradicting={len(group['contra'])}; "
                         f"lifecycle: {lifecycle}.")
    lines.extend([
        "Adaptive method and limitations",
        f"Each hypothesis is tested for {TRIAL_WEEKS} weekly observations; only fresh eligible evidence",
        "observed during the trial accumulates, by distinct repository, across those weeks.",
        f"Trial outcome: supported = >={SUPPORT_THRESHOLD} supporting repositories and more supporting than",
        "contradicting; contradicted = contradicting repositories present and >= supporting; otherwise inconclusive.",
        f"Finished trials retire, free their slot the same week and cannot be retested for {COOLDOWN_WEEKS} weeks.",
        "Candidates rank by fresh supporting repositories, then untested before previously tested, then",
        "all-time eligible supporting repositories, unique issue URLs (not comments/PR boosts), seed",
        "hypotheses before other signals, and signal id.",
        "Historical sources are discovery only: they nominate candidates but never count as trial evidence.",
        "Overlapping hypotheses (same base signal, or a seed and its aliases) are never tested together.",
        "Context signals (signal@context) refine a base signal with Windows version, isolation or",
        "orchestrator terms found in the same paragraph or source title; they are narrower hypotheses.",
        "This is deterministic evidence triage, not scientific proof, validation or causal inference.",
        "Independent means distinct full repository names, NOT distinct owners or independent people.",
        "Only API isPrivate=false sources count; Microsoft/AKS sources remain context only.",
        f"Excluded owners: {', '.join(sorted(MS_OWNERS))}; additional owners: {EXCLUDED_OWNERS_ENV}.",
        f"Current evidence window (UTC): ({window_start.isoformat()}, {now.isoformat()}].",
        "Freshness uses source/comment updatedAt in the trailing 7 days, excluding the lower boundary.",
        "For consecutive weekly observations, the window starts no earlier than the prior observed_at.",
        "This prevents overlapping evidence reuse; missed weeks contribute no evidence for the gap.",
        "ISO weeks freeze decisions, not source dates. Future dates never count.",
        "Issue/PR updatedAt may reflect unrelated metadata, not a changed claim; comment dates are source-specific.",
        "Only latest 100 comments and most recently updated linked PR are examined.",
        "Taxonomy: area + explicit barrier intent; specific DNS/gMSA/Nano intents take precedence.",
        "Negation/resolution/non-barriers are separate contrary/inconclusive evidence, never supporting votes.",
    ])
    lines.append("Signal taxonomy: " + "; ".join(f"{key} = {description}" for key, _, _, description in TAXONOMY))
    lines.append("Context qualifiers: " + "; ".join(f"@{key} = {phrase}" for key, _, phrase in CONTEXTS))
    lines.extend(_source_lines(records, signals, now, [slot["key"] for slot in state["slots"]]))
    if not signals:
        lines.append("No relevant evidence; defer without inventing a hypothesis.")
    return state, lines
