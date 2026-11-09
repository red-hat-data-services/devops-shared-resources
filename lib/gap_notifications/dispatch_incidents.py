"""Single-writer environment-scoped notification lifecycle backed by the Jira registry.

Ingest explicit normalized observations into a trial-scoped local outbox, then
resolve incidents through validated Jira labels/properties and issue status.
Failures create or reuse episodes; explicit positive observations can recover
them with a configured done transition. Unknown evidence does not establish
recovery, and continued failure after manual closure creates a successor rather
than reopening an issue. Confirmed Slack receipts and event IDs support replay
deduplication; uncertain writes retain durable intents/holds for reconciliation.

Jira gap.notification.v1 is routing authority. The flock/atomic-replace ledger
is a cache, audit and uncertainty journal, not distributed coordination. Callers
supply adapters and must ensure a single writer. Search-plus-create is non-atomic;
this prototype provides neither exactly-once delivery nor concurrent-writer
safety. Presentation links are cosmetic and cannot authorize lifecycle actions.

Canonical incident normalization and shared registry/receipt/link validation
live here so read-only lookup can validate independently of delivery execution.
Cosmetic ADF mechanics live in notify_tools; delivery never imports lookup_tools.
"""
from __future__ import annotations

from lib.gap_notifications.notify_tools import (JiraNotifier, category_labels, description,
    project_body, slack_message, routing_target, configured_channel, SLACK_ORIGIN, SlackRateLimitError, SlackPermanentError)
import copy
import json
from typing import Any
import fcntl
import hashlib
import math
import os
from pathlib import Path
import re
import tempfile
import time
import uuid
from urllib.parse import urlsplit
from contextlib import contextmanager
from datetime import datetime, timezone


VALID_CATEGORIES = frozenset({"build", "conflict", "feasibility"})
VALID_STATUSES = frozenset({"failure", "success", "unknown"})


class IncidentError(ValueError):
    """Raised when an observation payload is invalid."""


def _mapping(value: Any, *, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise IncidentError(f"{path} must be a mapping")
    return value


def _jsonable(value: Any, *, path: str) -> Any:
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise IncidentError(f"{path} must be JSON-compatible") from exc
    return value


def _text(value: Any, *, path: str) -> str:
    if not isinstance(value, str):
        raise IncidentError(f"{path} must be a string")
    text = value.strip()
    if not text:
        raise IncidentError(f"{path} is required")
    return text


def _repository(value: Any) -> str:
    repo = _text(value, path="repository")
    parts = [part.strip().lower() for part in repo.split("/")]
    if len(parts) == 2:
        owner, name = parts
        if not owner or not name:
            raise IncidentError("repository must be owner/repo or host/owner/repo")
        return f"github.com/{owner}/{name}"
    if len(parts) == 3:
        host, owner, name = parts
        if not host or not owner or not name:
            raise IncidentError("repository must be owner/repo or host/owner/repo")
        return f"{host}/{owner}/{name}"
    raise IncidentError("repository must be owner/repo or host/owner/repo")


def _normalize_observation(observation: dict[str, Any]) -> dict[str, Any]:
    data = _mapping(observation, path="observation")
    environment = _text(data.get("environment"), path="environment")
    repository = _repository(data.get("repository"))
    source_branch = _text(data.get("source_branch"), path="source_branch")
    destination_branch = _text(data.get("destination_branch"), path="destination_branch")
    category = _text(data.get("category"), path="category")
    if category not in VALID_CATEGORIES:
        raise IncidentError(f"category must be one of {sorted(VALID_CATEGORIES)}")
    status = _text(data.get("status"), path="status")
    if status not in VALID_STATUSES:
        raise IncidentError(f"status must be one of {sorted(VALID_STATUSES)}")
    observation_id = _text(data.get("observation_id"), path="observation_id")
    evidence = _jsonable(_mapping(data.get("evidence"), path="evidence"), path="evidence")

    normalized: dict[str, Any] = {
        "environment": environment,
        "repository": repository,
        "source_branch": source_branch,
        "destination_branch": destination_branch,
        "category": category,
        "status": status,
        "observation_id": observation_id,
        "evidence": copy.deepcopy(evidence),
    }
    if category == "build":
        component = _text(data.get("component"), path="component")
        normalized["component"] = component
        pipeline = data.get("pipeline")
        if pipeline is not None:
            normalized["pipeline"] = _text(pipeline, path="pipeline")
    return normalized


def _incident_identity(observation: dict[str, Any]) -> dict[str, Any]:
    identity: dict[str, Any] = {
        "environment": observation["environment"],
        "repository": observation["repository"],
        "source_branch": observation["source_branch"],
        "destination_branch": observation["destination_branch"],
        "category": observation["category"],
    }
    if observation["category"] == "build":
        identity["component"] = observation["component"]
        if "pipeline" in observation:
            identity["pipeline"] = observation["pipeline"]
    return identity


def incident_key(observation: dict) -> str:
    """Return a deterministic, human-inspectable incident identity."""
    normalized = _normalize_observation(observation)
    return json.dumps(_incident_identity(normalized), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


STAGE = "https://stage-redhat.atlassian.net"
LEGACY_ENVIRONMENT = "stage-redhat.atlassian.net/RHAI/C0883L8FARE"
PROPERTY_LIMIT = 24 * 1024


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _stamp():
    return datetime.now(timezone.utc).isoformat()


def validate_observations(observations, *, jira_environment="stage", channel=None):
    target = routing_target(jira_environment, channel)
    if not isinstance(observations, list) or len(observations) > 1000:
        raise ValueError("expected at most 1000 normalized observations")
    for obs in observations:
        incident_key(obs)
        if obs["environment"] != target.scope:
            raise ValueError("incorrect Jira environment scope")
        if len(json.dumps(obs, allow_nan=False).encode()) > 65536:
            raise ValueError("observation exceeds 64 KiB")
    return observations


def bind_trial(ledger, trial_id, *, jira_environment="stage", channel=None):
    if not isinstance(trial_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", trial_id):
        raise ValueError("invalid trial ID")
    if ledger and (ledger.get("schema") != 2 or ledger.get("trial_id") != trial_id):
        raise ValueError("registry v2 requires a new schema2 ledger with matching trial")
    target = routing_target(jira_environment, channel)
    # Journals without a binding predate production support and are staging-only.
    if ledger and ledger.get("scope", LEGACY_ENVIRONMENT) != target.scope:
        raise ValueError("journal Jira environment mismatch")
    for task in ledger.get("outbox", []):
        validate_observations([task["observation"]], **target.options)
        if task["incident_key"] != incident_key(task["observation"]):
            raise ValueError("journal incident identity mismatch")
    for identity in ledger.get("creates", {}):
        if json.loads(identity).get("environment") != target.scope:
            raise ValueError("journal create environment mismatch")
    result = copy.deepcopy(ledger)
    result.update(schema=2, trial_id=trial_id, scope=target.scope)
    return result


def incident_label(key, trial_id="default"):
    return "gap-registry-" + _hash(trial_id + ":" + key)[:40]


def ingest(ledger, observations, *, trial_id="default", jira_environment="stage", channel=None):
    target = routing_target(jira_environment, channel)
    validate_observations(observations, **target.options)
    data = bind_trial(ledger, trial_id, **target.options)
    for obs in observations:
        key = incident_key(obs)
        task_id = _hash(trial_id + ":" + key + ":" + obs["observation_id"])
        task = next((t for t in data.get("outbox", []) if t["id"] == task_id), None)
        if task is None:
            task = {"id": task_id, "incident_key": key, "system": "registry"}
            if obs["status"] != "unknown":
                for older in data.get("outbox", []):
                    if older["incident_key"] == key and older["state"] != "confirmed":
                        older["state"] = "superseded"
            data.setdefault("outbox", []).append(task)
        # Never local-dedup definitive observations: manual closure must be discovered.
        task.update(observation=copy.deepcopy(obs), state="pending")
        for field in ("hold", "error_type", "retry_at"):
            task.pop(field, None)
    return data


class Ledger:
    """flock + atomic replace; lock spans dispatch. No distributed coordination."""
    def __init__(self, path):
        self.path = Path(path)

    def read(self):
        if not self.path.exists():
            return {}
        if self.path.stat().st_size > 16 * 1024 * 1024:
            raise ValueError("ledger exceeds prototype size limit")
        data = json.loads(self.path.read_text())
        if not isinstance(data, dict):
            raise ValueError("ledger must be an object")
        return data

    @contextmanager
    def locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(str(self.path) + ".lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield self

    def save(self, data):
        raw = json.dumps(data, indent=2, allow_nan=False) + "\n"
        if len(raw.encode()) > 16 * 1024 * 1024:
            raise ValueError("ledger exceeds prototype size limit")
        fd, name = tempfile.mkstemp(prefix=".gap-ledger-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(name):
                os.unlink(name)


def _closed(issue):
    return issue.get("fields", {}).get("status", {}).get("statusCategory", {}).get("key") == "done"


def _summary(observation, *, jira_environment="stage", channel=None):
    return f"[{routing_target(jira_environment, channel).banner}] {observation['category']} failure: {observation['repository']}"[:250]


def _verify_issue(issue, episode, *, jira_environment="stage", channel=None):
    fields = issue.get("fields", {})
    _key(issue.get("key"))
    if (fields.get("project", {}).get("key") != "RHAI"
            or not (fields.get("issuetype", {}).get("name") == "Bug"
                    or (jira_environment == "stage" and fields.get("issuetype", {}).get("id") == "10016"))
            or episode["label"] not in fields.get("labels", [])
            or fields.get("summary") != _summary(episode["latest"], jira_environment=jira_environment)
            or fields.get("status", {}).get("statusCategory", {}).get("key") not in {"new", "indeterminate", "done"}):
        raise ValueError("Jira episode identity could not be verified")
    return _key(issue["key"])


def _key(value):
    if not isinstance(value, str) or not re.fullmatch(r"RHAI-[1-9][0-9]*", value):
        raise ValueError("invalid Jira receipt key")
    return value


def _description(observation, *, discussion_url=None, metadata_url=None, jira_environment="stage", channel=None):
    return description(observation, discussion_url=discussion_url, metadata_url=metadata_url,
                       **routing_target(jira_environment, channel).options)


def _presentation(observation, existing_labels, *, discussion_url=None, metadata_url=None, jira_environment="stage", channel=None):
    """Finish deterministic rendering/adapter validation before durable write intent."""
    body = _description(observation, discussion_url=discussion_url, metadata_url=metadata_url,
                        **routing_target(jira_environment, channel).options)
    labels = sorted(set(existing_labels) | set(category_labels(observation["category"])))
    JiraNotifier._validate_adf(body)
    JiraNotifier._validate_labels(labels)
    return body, labels


def _event(obs):
    return _hash(obs["observation_id"])


def _bounded(state):
    if len(json.dumps(state, allow_nan=False).encode()) >= PROPERTY_LIMIT:
        raise ValueError("registry property exceeds 24 KiB; operator archival required")
    return state


ABSENT_RECEIPT = object()


def validate_resolution(resolution, *, channel=None):
    """Validate explicit reconciliation shape before ingest, locking or clients."""
    if not isinstance(resolution, dict) or not isinstance(resolution.get("task_id"), str) or not resolution["task_id"].strip():
        raise ValueError("receipt task ID required")
    value = resolution.get("receipt")
    if not isinstance(value, dict) or not isinstance(value.get("reason"), str) or not value["reason"].strip():
        raise ValueError("explicit receipt audit reason required")
    _key(value.get("key"))
    if value.get("system") == "slack":
        _receipt(value, channel=channel)
    elif value.get("system") == "jira":
        if "done" in value and not isinstance(value["done"], bool):
            raise ValueError("invalid done attestation")
    else:
        raise ValueError("receipt system must be jira or slack")


def _receipt(value, *, channel=None):
    channel = configured_channel(channel)
    if not isinstance(value, dict) or value.get("channel") != channel or not re.fullmatch(r"[0-9]+\.[0-9]+", str(value.get("ts", ""))):
        raise ValueError("invalid Slack receipt")
    return {"channel": channel, "ts": value["ts"]}


def _validate_state(value, key, trial, label, *, jira_environment="stage", channel=None):
    if not isinstance(value, dict):
        raise ValueError("registry property missing")
    _bounded(value)
    if not {"schema", "scope", "trial_id", "incident_key", "episode_id", "seen_event_ids", "last_event", "last_outcome", "jira_intent", "delivery"}.issubset(value):
        raise ValueError("incomplete registry property")
    target = routing_target(jira_environment, channel)
    if (value.get("schema") != 1 or value.get("scope") != target.scope
            or json.loads(key).get("environment") != target.scope
            or value.get("trial_id") != trial or value.get("incident_key") != key
            or incident_label(key, trial) != label):
        raise ValueError("registry scope/identity mismatch")
    uuid.UUID(value["episode_id"])
    seen = value.get("seen_event_ids")
    if not isinstance(seen, dict) or any(not re.fullmatch(r"[0-9a-f]{64}", h) or s not in {"failure", "success", "unknown"} for h, s in seen.items()):
        raise ValueError("malformed seen-event registry")
    if value.get("last_outcome") not in {None, "failure", "success"}:
        raise ValueError("malformed outcome")
    if value.get("last_event") is not None and value["last_event"] not in seen:
        raise ValueError("missing event receipt")
    delivery = value.get("delivery")
    if not isinstance(delivery, dict) or not {"root", "last_event", "intent"}.issubset(delivery):
        raise ValueError("missing delivery registry")
    if delivery["last_event"] is not None and delivery["last_event"] not in seen:
        raise ValueError("unknown delivered event")
    if delivery.get("root") is not None:
        _receipt(delivery["root"], channel=channel)
    for intent in (value.get("jira_intent"), delivery.get("intent")):
        if intent is not None:
            if not isinstance(intent, dict) or intent.get("outcome") not in {"failure", "success"} or not re.fullmatch(r"[0-9a-f]{64}", str(intent.get("event_id", ""))):
                raise ValueError("malformed delivery intent")
    if delivery["intent"] is not None:
        uuid.UUID(delivery["intent"]["id"])
        if delivery["intent"].get("state") not in {"inflight", "reconciliation_required", "rejected", "rate_limited"}:
            raise ValueError("invalid intent state")
        if delivery["intent"].get("state") == "rate_limited":
            deadline = delivery["intent"].get("retry_at")
            if not isinstance(deadline, (int, float)) or isinstance(deadline, bool) or not math.isfinite(deadline) or deadline <= 0:
                raise ValueError("invalid retry deadline")
    return copy.deepcopy(value)


class RegistryHold(RuntimeError):
    pass


class LedgerPersistenceError(RuntimeError):
    """Stop dispatch when durable local state cannot be acknowledged."""


def _create_journal(value):
    if value is None:
        return None
    if not isinstance(value, dict) or value.get("state") not in {"confirmed", "inflight", "reconciliation_required"}:
        raise RegistryHold("invalid create journal")
    try:
        uuid.UUID(value["episode_id"])
        attempts = value.get("attempts", [])
        if not isinstance(attempts, list):
            raise ValueError()
        for attempt in attempts:
            uuid.UUID(attempt["attempt_id"])
            uuid.UUID(attempt["episode_id"])
            if not isinstance(attempt.get("task_id"), str) or not attempt["task_id"]:
                raise ValueError()
            if attempt.get("state") not in {"confirmed", "inflight", "reconciliation_required"}:
                raise ValueError()
            if "started_at" in attempt and (type(attempt["started_at"]) not in (int, float) or not math.isfinite(attempt["started_at"]) or attempt["started_at"] < 0):
                raise ValueError()
            if attempt.get("key"):
                _key(attempt["key"])
        if len({a["attempt_id"] for a in attempts}) != len(attempts):
            raise ValueError()
        count = value.get("consecutive_attempts", 0)
        if type(count) is not int or count < 0 or count > len(attempts):
            raise ValueError()
        deadline = value.get("next_retry_at", 0)
        if type(deadline) not in (int, float) or not math.isfinite(deadline) or deadline < 0:
            raise ValueError()
        for episode in value.get("active_episodes", [value["episode_id"]]):
            uuid.UUID(episode)
    except (ValueError, TypeError, KeyError, AttributeError):
        raise RegistryHold("invalid create journal") from None
    return value


class Dispatcher:
    """Single writer only: Jira properties are not compare-and-swap locks."""
    def __init__(self, ledger, jira, slack, *, transition_id=None, transition_fields=None, trial_id="default", jira_environment="stage", channel=None):
        self.target = routing_target(jira_environment, channel)
        if getattr(jira, "base_url", self.target.url) != self.target.url:
            raise ValueError("Jira adapter environment mismatch")
        bind_trial({}, trial_id, **self.target.options)
        self.ledger, self.jira, self.slack = ledger, jira, slack
        self.trial_id = trial_id
        self.transition_id, self.transition_fields = transition_id, transition_fields

    def _save(self):
        try:
            self.ledger.save(self.data)
        except Exception:
            raise LedgerPersistenceError("local persistence unavailable; dispatch stopped") from None

    def _put(self, key, state):
        _bounded(state)
        verified = self.jira.put_notification_state(key, state)
        if verified != state:
            raise RegistryHold("registry readback mismatch")
        return state

    def _registry(self, task):
        obs, identity = task["observation"], task["incident_key"]
        label = incident_label(identity, self.trial_id)
        issues = self.jira.find_by_label(label)  # Adapter guarantees complete pagination.
        if not isinstance(issues, list):
            raise RegistryHold("incomplete registry")
        journal = _create_journal(self.data.get("creates", {}).get(identity))
        known_keys = {a["key"] for a in (journal or {}).get("attempts", []) if a.get("key")}
        if (journal or {}).get("key"):
            known_keys.add(journal["key"])
        for known in sorted(known_keys):
            _key(known)
            if not any(i.get("key") == known for i in issues):
                issues.append(self.jira.get_issue(known))  # Known key beats eventual JQL visibility.
        records = []
        for issue in issues:
            _verify_issue(issue, {"label": label, "latest": obs}, **self.target.options)
            state = _validate_state(self.jira.get_notification_state(issue["key"]), identity, self.trial_id, label, **self.target.options)
            if "gap-episode-" + uuid.UUID(state["episode_id"]).hex not in issue["fields"]["labels"]:
                raise RegistryHold("episode property/label mismatch")
            expected_episodes = {a["episode_id"] for a in (journal or {}).get("attempts", []) if a.get("key") == issue["key"]}
            if expected_episodes and state["episode_id"] not in expected_episodes:
                raise RegistryHold("known create key episode mismatch")
            records.append((issue, state))
        if (len({i["key"] for i, p in records}) != len(records)
                or len({p["episode_id"] for i, p in records}) != len(records)):
            raise RegistryHold("duplicate registry results or episodes")
        if len([i for i, p in records if not _closed(i)]) > 1:
            raise RegistryHold("multiple open incidents")
        return sorted(records, key=lambda pair: int(pair[0]["key"].split("-")[1]))

    def _new_state(self, task, previous):
        return _bounded({
            "schema": 1, "scope": self.target.scope, "trial_id": self.trial_id,
            "incident_key": task["incident_key"], "episode_id": str(uuid.uuid4()),
            "seen_event_ids": copy.deepcopy(previous.get("seen_event_ids", {})),
            "last_event": None, "last_outcome": None, "jira_intent": None,
            "delivery": {"root": None, "last_event": None, "intent": None},
        })

    def _create(self, task, previous):
        identity, obs = task["incident_key"], task["observation"]
        journal = _create_journal(self.data.get("creates", {}).get(identity))
        if identity in self.attempted_creates:
            raise RegistryHold("one create attempt per incident per invocation")
        if journal and journal["state"] != "confirmed" and time.time() < journal.get("next_retry_at", 0):
            # Keep reconciliation eligible on every invocation; only POST is delayed.
            raise RegistryHold("create retry cooldown")
        state = self._new_state(task, previous)
        if previous:
            state["predecessor"] = {"key": previous["issue_key"], "episode_id": previous["episode_id"]}
        event = _event(obs)
        state["seen_event_ids"][event] = "failure"
        state.update(last_event=event, last_outcome="failure")
        label = incident_label(identity, self.trial_id)
        body, labels = _presentation(obs, [label, "gap-episode-" + uuid.UUID(state["episode_id"]).hex], **self.target.options)
        attempts = copy.deepcopy((journal or {}).get("attempts", []))
        if journal and not attempts:
            attempts.append({"attempt_id": journal["episode_id"], "episode_id": journal["episode_id"],
                             "task_id": task["id"], "state": journal["state"],
                             **({"key": journal["key"]} if journal.get("key") else {}), "legacy": True})
        pending = journal and journal["state"] != "confirmed"
        active = list(journal.get("active_episodes", [journal["episode_id"]])) if pending else []
        count = (journal.get("consecutive_attempts", len(attempts)) if pending else 0) + 1
        now = time.time()
        deadline = now + min(60 * 2 ** min(count - 1, 4), 900)
        attempt = {"attempt_id": str(uuid.uuid4()), "episode_id": state["episode_id"],
                   "task_id": task["id"], "event_id": event, "started_at": now,
                   "next_retry_at": deadline, "state": "inflight"}
        attempts.append(attempt)
        self.data.setdefault("creates", {})[identity] = {
            "state": "inflight", "episode_id": state["episode_id"], "attempts": attempts,
            "active_episodes": active + [state["episode_id"]], "consecutive_attempts": count,
            "next_retry_at": deadline}
        self.attempted_creates.add(identity)
        task["operation"] = "create"
        self._save()  # Local intent BEFORE Jira create; property is atomic with issue.
        try:
            result = self.jira.create_bug(_summary(obs, **self.target.options), body, labels,
                notification_state=_bounded(state))
            key = _key(result["key"])
            self.data["creates"][identity].update(state="confirmed", key=key)
            attempt.update(state="confirmed", key=key)
            self._save()
        except LedgerPersistenceError:
            raise
        except Exception as exc:
            known = getattr(exc, "key", None)
            current = self.data["creates"][identity]
            current["state"] = "reconciliation_required"
            attempt.update(state="reconciliation_required", error_type=type(exc).__name__)
            if known:
                current["key"] = attempt["key"] = _key(known)
            delay = getattr(exc, "retry_after", None)
            if type(delay) in (int, float) and math.isfinite(delay) and delay >= 0:
                current["next_retry_at"] = max(current["next_retry_at"], time.time() + delay)
            attempt["next_retry_at"] = current["next_retry_at"]
            status = getattr(exc, "status_code", None)
            if type(status) is int and 100 <= status <= 599:
                attempt["status_code"] = status
            self._save()
            raise
        issue = self.jira.get_issue(key)
        _verify_issue(issue, {"label": label, "latest": obs}, **self.target.options)
        actual = _validate_state(self.jira.get_notification_state(key), identity, self.trial_id, label, **self.target.options)
        if actual != state:
            raise RegistryHold("atomic create property mismatch")
        return issue, actual

    def _resolve(self, resolution):
        value = resolution.get("receipt", {})
        if not isinstance(value.get("reason"), str) or not value["reason"].strip():
            raise ValueError("explicit receipt audit reason required")
        key = _key(value.get("key"))
        task = next((t for t in self.data.get("outbox", []) if t["id"] == resolution.get("task_id")), None)
        if task is None:
            raise ValueError("unknown task ID")
        issue = self.jira.get_issue(key)
        identity = task["incident_key"]
        label = incident_label(identity, self.trial_id)
        _verify_issue(issue, {"label": label, "latest": task["observation"]}, **self.target.options)
        state = _validate_state(self.jira.get_notification_state(key), identity, self.trial_id, label, **self.target.options)
        if "gap-episode-" + uuid.UUID(state["episode_id"]).hex not in issue["fields"]["labels"]:
            raise ValueError("receipt episode identity mismatch")
        if task.get("jira_key") not in (None, key):
            raise ValueError("receipt conflicts with known Jira key")
        if value.get("system") == "slack":
            intent = state["delivery"].get("intent")
            if not intent or intent["event_id"] != _event(task["observation"]):
                raise ValueError("no matching Slack uncertainty to resolve")
            receipt = _receipt(value, channel=self.target.channel)
            if not state["delivery"]["root"]:
                state["delivery"]["root"] = receipt
            state["delivery"].update(last_event=intent["event_id"], intent=None)
        elif value.get("system") == "jira":
            intent = state.get("jira_intent")
            if not intent or intent["event_id"] != _event(task["observation"]):
                raise ValueError("no matching Jira uncertainty to resolve")
            if intent["outcome"] == "success" and (not _closed(issue) or value.get("done") is not True):
                raise ValueError("recovery attestation requires verified done issue")
            state["seen_event_ids"][intent["event_id"]] = intent["outcome"]
            state.update(last_event=intent["event_id"], last_outcome=intent["outcome"], jira_intent=None)
        else:
            raise ValueError("receipt system must be jira or slack")
        self._put(key, state)
        self.data.setdefault("audit", []).append({"at": _stamp(), "key": key, "receipt": copy.deepcopy(value)})
        self._save()

    def run(self, observations, *, receipt=ABSENT_RECEIPT):
        if receipt is not ABSENT_RECEIPT:
            validate_resolution(receipt, channel=self.target.channel)
        if getattr(self.jira, "base_url", self.target.url) != self.target.url:
            raise ValueError("Jira adapter environment mismatch")
        validate_observations(observations, **self.target.options)
        if getattr(self.slack, "channel", self.target.channel) != self.target.channel:
            raise ValueError("Slack adapter channel mismatch")
        self.attempted_creates = set()
        with self.ledger.locked():
            self.data = ingest(self.ledger.read(), observations, trial_id=self.trial_id, **self.target.options)
            self._save()
            if receipt is not ABSENT_RECEIPT:
                self._resolve(receipt)
            for task in self.data.get("outbox", []):
                if task["state"] in {"confirmed", "superseded"} or task.get("retry_at", 0) > time.time():
                    continue
                try:
                    self._process(task)
                    task["state"] = "confirmed"
                    for field in ("hold", "error_type", "retry_at"):
                        task.pop(field, None)
                except LedgerPersistenceError:
                    raise
                except Exception as exc:
                    task["state"] = "reconciliation_required" if isinstance(exc, RegistryHold) else "pending"
                    task["hold"] = "registry_or_delivery_unavailable"
                    task["error_type"] = type(exc).__name__
                self._save()
            return copy.deepcopy(self.data)

    def _process(self, task):
        obs = task["observation"]
        if obs["status"] == "unknown":
            return  # Unknown never creates, updates, closes, or delivers.
        records = self._registry(task)
        identity, event = task["incident_key"], _event(obs)
        orphan_success = _hash(identity + ":" + event)
        if obs["status"] == "success" and orphan_success in self.data.get("no_issue_successes", {}):
            return
        journal = _create_journal(self.data.get("creates", {}).get(identity))
        if journal and journal["state"] != "confirmed":
            active = journal.get("active_episodes", [journal["episode_id"]])
            matches = [(i, p) for i, p in records if p["episode_id"] in active]
            if len(matches) > 1:
                raise RegistryHold("multiple attributable create episodes")
            if matches:
                journal.update(state="confirmed", key=matches[0][0]["key"])
                self._save()
            # A successful complete no-match permits later bounded CREATE retries.
            # This deliberately accepts duplicates if prior creation is not yet visible.
        # Only positively rejected rate-limited sends may retry automatically.
        for item, prop in records:
            if prop.get("jira_intent"):
                raise RegistryHold("durable Jira intent requires receipt reconciliation")
            intent = prop["delivery"].get("intent")
            if intent:
                if intent["state"] != "rate_limited":
                    raise RegistryHold("durable Slack intent requires receipt reconciliation")
                if intent["retry_at"] > time.time():
                    task["retry_at"] = intent["retry_at"]
                    raise RegistryHold("confirmed rate rejection: wait until retry deadline")
                prop["delivery"]["intent"] = None
                self._put(item["key"], prop)
        opened = [(i, p) for i, p in records if not _closed(i)]
        previous = records[-1] if records else None
        if opened:
            issue, state = opened[0]
        elif obs["status"] == "failure":
            if previous and previous[1]["last_outcome"] == "success" and event in previous[1]["seen_event_ids"]:
                return  # Old pre-recovery failure, not a new recurrence.
            prior = dict(previous[1], issue_key=previous[0]["key"]) if previous else {}
            issue, state = self._create(task, prior)
        else:
            # Resume recovery Slack after close using matching positive event.
            if not previous:
                self.data.setdefault("no_issue_successes", {})[orphan_success] = True
                return
            # Explicit success also resolves a manually-closed unresolved episode
            # through property metadata only; never transition a done issue.
            issue, state = previous
            if state["last_outcome"] == "success" and event not in state["seen_event_ids"]:
                return  # New positive evidence cannot repeat an episode's recovery.
        key = issue["key"]
        task["jira_key"] = key
        if event not in state["seen_event_ids"]:
            if obs["status"] == "failure":
                root = state["delivery"]["root"]  # Jira registry only, never evidence.
                validated_property(issue, state, trial=self.trial_id, identity=task["incident_key"], **self.target.options)
                body, labels = _presentation(obs, [], discussion_url=discussion_url(root, channel=self.target.channel) if root else None,
                                             metadata_url=metadata_url(key, **self.target.options), **self.target.options)
            if obs["status"] == "success" and not _closed(issue) and not self.transition_id:
                raise RegistryHold("explicit done transition required")
            state["jira_intent"] = {"event_id": event, "outcome": obs["status"]}
            state["seen_event_ids"][event] = obs["status"]
            _bounded(state)
            task["operation"] = "close" if obs["status"] == "success" else "update"
            self._save()
            self._put(key, state)  # Remote intent BEFORE Jira mutation.
            if obs["status"] == "success":
                if not _closed(issue):
                    self.jira.transition_to_done(key, self.transition_id, fields=self.transition_fields)
            else:
                self.jira.update_issue(key, description=body, add_labels=labels)
            state["seen_event_ids"][event] = obs["status"]
            state.update(last_event=event, last_outcome=obs["status"], jira_intent=None)
            self._put(key, state)
        elif state["last_event"] != event or state["last_outcome"] != obs["status"]:
            return  # Historical event cannot replace newer accepted observation.
        try:
            self._slack(task, key, state)
        finally:
            # Cosmetic failure is swallowed inside helper; original delivery result survives.
            self._project_discussion(task, key)

    def _project_discussion(self, task, key):
        """Cosmetic projection is isolated from core receipt/intents and cannot cause resend."""
        try:
            issue = self.jira.get_issue(key)
            if _closed(issue):
                return
            state = validated_property(issue, self.jira.get_notification_state(key),
                                       trial=self.trial_id, identity=task["incident_key"], **self.target.options)
            root = state["delivery"]["root"]
            current = issue["fields"].get("description")
            projected = project_body(current, discussion_url(root, channel=self.target.channel) if root else None,
                                     metadata=metadata_url(key, **self.target.options), **self.target.options)
            if JiraNotifier._adf_signature(current) != JiraNotifier._adf_signature(projected):
                self.jira.update_issue(key, description=projected)
            task.pop("presentation_warning", None)
        except Exception:
            # No raw exception text, no core state/intent/delivery changes.
            task["presentation_warning"] = "discussion_projection_unavailable"

    def _slack(self, task, key, state):
        obs, event = task["observation"], _event(task["observation"])
        delivery = state["delivery"]
        if delivery["last_event"] == event:
            return
        if obs["status"] == "success" and not delivery["root"]:
            delivery["last_event"] = event
            self._put(key, state)
            return  # No unrelated recovery root.
        text = slack_message(obs, jira_url=f"{self.target.url}/browse/{key}", reply=delivery["root"] is not None,
                             **self.target.options)
        intent = {"id": str(uuid.uuid4()), "event_id": event, "outcome": obs["status"], "state": "inflight"}
        delivery["intent"] = intent
        _bounded(state)
        self._put(key, state)  # Durable remote intent BEFORE Slack POST.
        task.update(operation="slack", delivery_id=intent["id"])
        self._save()
        try:
            result = _receipt(self.slack.send(text, thread_ts=(delivery["root"] or {}).get("ts"), client_msg_id=intent["id"]), channel=self.target.channel)
        except Exception as exc:
            # Definite rejection can be recorded; all ambiguous outcomes retain inflight.
            if isinstance(exc, SlackRateLimitError):
                deadline = time.time() + min(max(exc.retry_after or 60, 1), 3600)
                delivery["intent"].update(state="rate_limited", retry_at=deadline)
                task["retry_at"] = deadline
                self._put(key, state)
            elif isinstance(exc, SlackPermanentError):
                delivery["intent"]["state"] = "rejected"
                self._put(key, state)
            raise RegistryHold("Slack outcome requires explicit resolution") from None
        task["slack_receipt"] = result
        self._save()  # Preserve known response even if property receipt persistence fails.
        if not delivery["root"]:
            delivery["root"] = result
        delivery.update(last_event=event, intent=None)
        self._put(key, state)


def discussion_url(root, *, channel=None):
    receipt = _receipt(root, channel=channel)
    return f"{SLACK_ORIGIN}/archives/{receipt['channel']}/p{receipt['ts'].replace('.', '')}"


def validated_property(issue, state, *, trial=None, identity=None, jira_environment="stage", channel=None):
    """Validate full stored scope/identity before using routing data; never description."""
    if not isinstance(state, dict):
        raise ValueError("missing property")
    stored_trial = state.get("trial_id")
    bind_trial({}, stored_trial, jira_environment=jira_environment, channel=channel)
    if trial is not None and trial != stored_trial:
        raise ValueError("trial mismatch")
    stored_identity = state.get("incident_key")
    parsed = json.loads(stored_identity)
    obs = dict(parsed, status="failure", observation_id="diagnostic", evidence={})
    if incident_key(obs) != stored_identity or (identity is not None and identity != stored_identity):
        raise ValueError("incident mismatch")
    label = incident_label(stored_identity, stored_trial)
    target = routing_target(jira_environment, channel)
    checked = _validate_state(state, stored_identity, stored_trial, label, **target.options)
    # Summary retains caller repository spelling; identity canonicalizes it.
    # Validate that spelling against the stored identity, never against body links.
    prefix = f"[{target.banner}] {obs['category']} failure: "
    summary = issue.get("fields", {}).get("summary", "")
    if not isinstance(summary, str) or not summary.startswith(prefix):
        raise ValueError("summary identity mismatch")
    summary_obs = dict(obs, repository=summary[len(prefix):])
    if incident_key(summary_obs) != stored_identity:
        raise ValueError("summary repository mismatch")
    _verify_issue(issue, {"label": label, "latest": summary_obs}, **target.options)
    if "gap-episode-" + uuid.UUID(checked["episode_id"]).hex not in issue["fields"]["labels"]:
        raise ValueError("episode mismatch")
    if checked["delivery"]["root"] is not None and checked["delivery"]["last_event"] is None:
        raise ValueError("root lacks confirmed delivery event")
    return checked


def metadata_url(key, *, jira_environment="stage", channel=None):
    return f"{routing_target(jira_environment, channel).url}/rest/api/3/issue/{_key(key)}/properties/gap.notification.v1"
