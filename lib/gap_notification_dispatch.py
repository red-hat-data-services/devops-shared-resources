"""Single-writer staging Jira registry; local ledger is only a cache/uncertainty journal."""
from __future__ import annotations
import copy
import fcntl
import hashlib
import json
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
from lib.gap_notification_incidents import incident_key
from lib.gap_notification_presentation import category_labels, description
from lib.gap_notification_jira import JiraNotifier

STAGE = "https://stage-redhat.atlassian.net"
CHANNEL = "C0883L8FARE"
ENVIRONMENT = "stage-redhat.atlassian.net/RHAI/C0883L8FARE"
PROPERTY_LIMIT = 24 * 1024

def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()

def _stamp():
    return datetime.now(timezone.utc).isoformat()

def validate_observations(observations):
    if not isinstance(observations, list) or len(observations) > 1000:
        raise ValueError("expected at most 1000 normalized observations")
    for obs in observations:
        incident_key(obs)
        if obs["environment"] != ENVIRONMENT:
            raise ValueError("incorrect staging scope")
        if len(json.dumps(obs, allow_nan=False).encode()) > 65536:
            raise ValueError("observation exceeds 64 KiB")
    return observations

def bind_trial(ledger, trial_id):
    if not isinstance(trial_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", trial_id):
        raise ValueError("invalid trial ID")
    if ledger and (ledger.get("schema") != 2 or ledger.get("trial_id") != trial_id):
        raise ValueError("registry v2 requires a new schema2 ledger with matching trial")
    result = copy.deepcopy(ledger)
    result.update(schema=2, trial_id=trial_id)
    return result

def incident_label(key, trial_id="default"):
    return "gap-registry-" + _hash(trial_id + ":" + key)[:40]

def ingest(ledger, observations, *, trial_id="default"):
    validate_observations(observations)
    data = bind_trial(ledger, trial_id)
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


def _summary(observation):
    return f"[GAP STAGING TEST] {observation['category']} failure: {observation['repository']}"[:250]


def _verify_issue(issue, episode):
    fields = issue.get("fields", {})
    _key(issue.get("key"))
    if (fields.get("project", {}).get("key") != "RHAI"
            or not (fields.get("issuetype", {}).get("name") == "Bug" or fields.get("issuetype", {}).get("id") == "10016")
            or episode["label"] not in fields.get("labels", [])
            or fields.get("summary") != _summary(episode["latest"])
            or fields.get("status", {}).get("statusCategory", {}).get("key") not in {"new", "indeterminate", "done"}):
        raise ValueError("Jira episode identity could not be verified")
    return _key(issue["key"])


def _key(value):
    if not isinstance(value, str) or not re.fullmatch(r"RHAI-[1-9][0-9]*", value):
        raise ValueError("invalid Jira receipt key")
    return value


def _notice(observation):
    evidence = observation["evidence"]
    return (f"test_only={evidence.get('test_only', True)}; synthetic={evidence.get('synthetic', False)}; "
            f"historical_snapshot={evidence.get('historical_snapshot', False)}\n"
            f"{evidence.get('note', 'Explicit supplied evidence; not a live production evaluation.')}\n"
            "This staging trial does not establish production recovery.")


def _description(observation, *, discussion_url=None, metadata_url=None):
    return description(observation, discussion_url=discussion_url, metadata_url=metadata_url)


def _presentation(observation, existing_labels, *, discussion_url=None, metadata_url=None):
    """Finish deterministic rendering/adapter validation before durable write intent."""
    body = _description(observation, discussion_url=discussion_url, metadata_url=metadata_url)
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

def _receipt(value):
    if not isinstance(value, dict) or value.get("channel") != CHANNEL or not re.fullmatch(r"[0-9]+\.[0-9]+", str(value.get("ts", ""))):
        raise ValueError("invalid Slack receipt")
    return {"channel": CHANNEL, "ts": value["ts"]}

def _validate_state(value, key, trial, label):
    if not isinstance(value, dict):
        raise ValueError("registry property missing")
    _bounded(value)
    if not {"schema", "scope", "trial_id", "incident_key", "episode_id", "seen_event_ids", "last_event", "last_outcome", "jira_intent", "delivery"}.issubset(value):
        raise ValueError("incomplete registry property")
    if (value.get("schema") != 1 or value.get("scope") != ENVIRONMENT
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
        _receipt(delivery["root"])
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

class Dispatcher:
    """Single writer only: Jira properties are not compare-and-swap locks."""
    def __init__(self, ledger, jira, slack, *, transition_id=None, transition_fields=None, trial_id="default"):
        bind_trial({}, trial_id)
        self.ledger, self.jira, self.slack = ledger, jira, slack
        self.trial_id = trial_id
        self.transition_id, self.transition_fields = transition_id, transition_fields

    def _save(self):
        self.ledger.save(self.data)

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
        known = self.data.get("creates", {}).get(identity, {}).get("key")
        if known and not any(i.get("key") == known for i in issues):
            issues.append(self.jira.get_issue(known))  # Known key beats eventual JQL visibility.
        records = []
        for issue in issues:
            _verify_issue(issue, {"label": label, "latest": obs})
            state = _validate_state(self.jira.get_notification_state(issue["key"]), identity, self.trial_id, label)
            if "gap-episode-" + uuid.UUID(state["episode_id"]).hex not in issue["fields"]["labels"]:
                raise RegistryHold("episode property/label mismatch")
            records.append((issue, state))
        if (len({i["key"] for i, p in records}) != len(records)
                or len({p["episode_id"] for i, p in records}) != len(records)):
            raise RegistryHold("duplicate registry results or episodes")
        if len([i for i, p in records if not _closed(i)]) > 1:
            raise RegistryHold("multiple open incidents")
        return sorted(records, key=lambda pair: int(pair[0]["key"].split("-")[1]))

    def _new_state(self, task, previous):
        return _bounded({
            "schema": 1, "scope": ENVIRONMENT, "trial_id": self.trial_id,
            "incident_key": task["incident_key"], "episode_id": str(uuid.uuid4()),
            "seen_event_ids": copy.deepcopy(previous.get("seen_event_ids", {})),
            "last_event": None, "last_outcome": None, "jira_intent": None,
            "delivery": {"root": None, "last_event": None, "intent": None},
        })

    def _create(self, task, previous):
        identity, obs = task["incident_key"], task["observation"]
        journal = self.data.get("creates", {}).get(identity)
        if journal and journal["state"] != "confirmed":
            raise RegistryHold("ambiguous create; absence from search never authorizes retry")
        state = self._new_state(task, previous)
        if previous:
            state["predecessor"] = {"key": previous["issue_key"], "episode_id": previous["episode_id"]}
        event = _event(obs)
        state["seen_event_ids"][event] = "failure"
        state.update(last_event=event, last_outcome="failure")
        label = incident_label(identity, self.trial_id)
        body, labels = _presentation(obs, [label, "gap-episode-" + uuid.UUID(state["episode_id"]).hex])
        self.data.setdefault("creates", {})[identity] = {"state": "inflight", "episode_id": state["episode_id"]}
        task["operation"] = "create"
        self._save()  # Local intent BEFORE Jira create; property is atomic with issue.
        try:
            result = self.jira.create_bug(_summary(obs), body, labels,
                notification_state=_bounded(state))
            key = _key(result["key"])
            self.data["creates"][identity].update(state="confirmed", key=key)
            self._save()
        except Exception as exc:
            known = getattr(exc, "key", None)
            self.data["creates"][identity]["state"] = "reconciliation_required"
            if known:
                self.data["creates"][identity]["key"] = _key(known)
            self._save()
            raise
        issue = self.jira.get_issue(key)
        _verify_issue(issue, {"label": label, "latest": obs})
        actual = _validate_state(self.jira.get_notification_state(key), identity, self.trial_id, label)
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
        _verify_issue(issue, {"label": label, "latest": task["observation"]})
        state = _validate_state(self.jira.get_notification_state(key), identity, self.trial_id, label)
        if "gap-episode-" + uuid.UUID(state["episode_id"]).hex not in issue["fields"]["labels"]:
            raise ValueError("receipt episode identity mismatch")
        if task.get("jira_key") not in (None, key):
            raise ValueError("receipt conflicts with known Jira key")
        if value.get("system") == "slack":
            intent = state["delivery"].get("intent")
            if not intent or intent["event_id"] != _event(task["observation"]):
                raise ValueError("no matching Slack uncertainty to resolve")
            receipt = _receipt(value)
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

    def run(self, observations, *, receipt=None):
        validate_observations(observations)
        with self.ledger.locked():
            self.data = ingest(self.ledger.read(), observations, trial_id=self.trial_id)
            self._save()
            if receipt:
                self._resolve(receipt)
            for task in self.data.get("outbox", []):
                if task["state"] in {"confirmed", "superseded"} or task.get("retry_at", 0) > time.time():
                    continue
                try:
                    self._process(task)
                    task["state"] = "confirmed"
                    for field in ("hold", "error_type", "retry_at"):
                        task.pop(field, None)
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
        journal = self.data.get("creates", {}).get(identity)
        if journal and journal["state"] != "confirmed":
            matches = [(i, p) for i, p in records if p["episode_id"] == journal["episode_id"]]
            if len(matches) != 1:
                raise RegistryHold("create reconciliation required")
            journal.update(state="confirmed", key=matches[0][0]["key"])
            self._save()
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
        key = issue["key"]
        task["jira_key"] = key
        if event not in state["seen_event_ids"]:
            if obs["status"] == "failure":
                from lib.gap_notification_diagnostics import discussion_url, validated_property, metadata_url
                root = state["delivery"]["root"]  # Jira registry only, never evidence.
                validated_property(issue, state, trial=self.trial_id, identity=task["incident_key"])
                body, labels = _presentation(obs, [], discussion_url=discussion_url(root) if root else None,
                                             metadata_url=metadata_url(key))
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
            from lib.gap_notification_diagnostics import validated_property, discussion_url, project_body, metadata_url
            issue = self.jira.get_issue(key)
            if _closed(issue):
                return
            state = validated_property(issue, self.jira.get_notification_state(key),
                                       trial=self.trial_id, identity=task["incident_key"])
            root = state["delivery"]["root"]
            current = issue["fields"].get("description")
            projected = project_body(current, discussion_url(root) if root else None, metadata=metadata_url(key))
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
        intent = {"id": str(uuid.uuid4()), "event_id": event, "outcome": obs["status"], "state": "inflight"}
        delivery["intent"] = intent
        _bounded(state)
        self._put(key, state)  # Durable remote intent BEFORE Slack POST.
        task.update(operation="slack", delivery_id=intent["id"])
        self._save()
        text = f"[GAP STAGING TEST] {obs['status']}: {obs['repository']} {obs['category']}\n{_notice(obs)}\n{STAGE}/browse/{key}\n" + json.dumps(obs["evidence"], sort_keys=True)[:2500]
        try:
            result = _receipt(self.slack.send(text, thread_ts=(delivery["root"] or {}).get("ts"), client_msg_id=intent["id"]))
        except Exception as exc:
            # Definite rejection can be recorded; all ambiguous outcomes retain inflight.
            from lib.gap_notification_slack import SlackRateLimitError, SlackPermanentError
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
