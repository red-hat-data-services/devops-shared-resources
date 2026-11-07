"""Contract tests for the v2 GAP notification registry dispatcher."""

from __future__ import annotations

import copy
import json

import pytest

from lib.gap_notifications.dispatch_incidents import CHANNEL, Dispatcher, Ledger, incident_label
from lib.gap_notifications.notify_tools import SlackRateLimitError

ENVIRONMENT = "stage-redhat.atlassian.net/RHAI/C0883L8FARE"
TRIAL_ID = "leader84-local-001"
TRANSITION_ID = "41"
TRANSITION_FIELDS = {"resolution": {"id": "1"}}


def observation(*, status="failure", oid="run-1", category="build", destination_branch="stable",
                component="cpu", evidence=None):
    data = {
        "environment": ENVIRONMENT,
        "repository": "acme/repo",
        "source_branch": "main",
        "destination_branch": destination_branch,
        "category": category,
        "status": status,
        "observation_id": oid,
        "evidence": evidence or {
            "historical_snapshot": True,
            "test_only": True,
            "synthetic": True,
            "note": "contract fixture",
            "pr_url": "https://github.com/acme/repo/pull/1",
        },
    }
    if category == "build":
        data["component"] = component
    return data


class FakeJira:
    def __init__(self):
        self.issues = {}
        self.notification_state = {}
        self.calls = []
        self._next_key = 1
        self.fail_put_once = False
        self.fail_find_once = False
        self.fail_put_when = None

    def _issue(self, key):
        return self.issues[key]

    def _matches_label(self, issue, label):
        return label in issue["fields"].get("labels", [])

    def find_by_label(self, label):
        self.calls.append(("find_by_label", label))
        if self.fail_find_once:
            self.fail_find_once = False
            raise RuntimeError("lookup unavailable")
        return [copy.deepcopy(issue) for issue in self.issues.values() if self._matches_label(issue, label)]

    def get_issue(self, key):
        self.calls.append(("get_issue", key))
        return copy.deepcopy(self.issues[key])

    def get_notification_state(self, key):
        self.calls.append(("get_notification_state", key))
        value = self.notification_state.get(key)
        return copy.deepcopy(value)

    def put_notification_state(self, key, value):
        self.calls.append(("put_notification_state", key))
        if self.fail_put_once:
            self.fail_put_once = False
            raise RuntimeError("property write failed")
        if self.fail_put_when is not None and self.fail_put_when(key, value):
            raise RuntimeError("property write failed")
        self.notification_state[key] = copy.deepcopy(value)
        return copy.deepcopy(value)

    def create_bug(self, summary, description, labels, notification_state=None):
        self.calls.append(("create_bug", summary))
        key = f"RHAI-{self._next_key}"
        self._next_key += 1
        self.issues[key] = {
            "key": key,
            "fields": {
                "summary": summary,
                "description": description,
                "labels": list(labels),
                "project": {"key": "RHAI"},
                "issuetype": {"name": "Bug", "id": "10016"},
                "status": {"statusCategory": {"key": "new"}},
            },
        }
        if notification_state is not None:
            self.notification_state[key] = copy.deepcopy(notification_state)
        return {"key": key, "url": f"https://stage-redhat.atlassian.net/browse/{key}"}

    def update_issue(self, key, **fields):
        self.calls.append(("update_issue", key))
        additions = fields.pop("add_labels", [])
        self.issues[key]["fields"]["labels"] = sorted(set(self.issues[key]["fields"]["labels"]) | set(additions))
        self.issues[key]["fields"].update(fields)
        return copy.deepcopy(self.issues[key])

    def transition_to_done(self, key, transition_id, *, fields=None):
        self.calls.append(("transition_to_done", key, transition_id))
        self.issues[key]["fields"]["status"]["statusCategory"]["key"] = "done"
        return copy.deepcopy(self.issues[key])

    def set_manual_closed(self, key):
        self.issues[key]["fields"]["status"]["statusCategory"]["key"] = "done"

    def issue_keys(self):
        return list(self.issues)


class FakeSlack:
    def __init__(self):
        self.calls = []
        self.fail_once = False

    def send(self, text, **kwargs):
        self.calls.append((text, kwargs))
        if self.fail_once:
            self.fail_once = False
            raise TimeoutError("slack send failed")
        if getattr(self, "rate_limit_once", False):
            self.rate_limit_once = False
            raise SlackRateLimitError("rate limit", retry_after=2.5)
        return {"channel": CHANNEL, "ts": f"{len(self.calls)}.456"}


@pytest.fixture
def shared_clients():
    return FakeJira(), FakeSlack()


def make_dispatcher(tmp_path, jira, slack, *, trial_id=TRIAL_ID, name="ledger.json"):
    return Dispatcher(
        Ledger(tmp_path / name),
        jira,
        slack,
        trial_id=trial_id,
        transition_id=TRANSITION_ID,
        transition_fields=TRANSITION_FIELDS,
    )


def test_fresh_ledgers_same_trial_reuse_ticket_and_thread(tmp_path, shared_clients):
    jira, slack = shared_clients
    dispatch1 = make_dispatcher(tmp_path, jira, slack, name="ledger-a.json")
    first = dispatch1.run([observation()])
    assert first["outbox"][0]["state"] == "confirmed"
    assert len(jira.issues) == 1
    assert len(slack.calls) == 1

    dispatch2 = make_dispatcher(tmp_path, jira, slack, name="ledger-b.json")
    second = dispatch2.run([observation()])
    assert len(jira.issues) == 1
    assert len(slack.calls) == 1
    assert second["outbox"][0]["state"] == "confirmed"
    assert second["outbox"][0]["incident_key"] == first["outbox"][0]["incident_key"]


def test_manual_close_same_observation_creates_one_successor_and_replay_stays_one(tmp_path, shared_clients):
    jira, slack = shared_clients
    dispatch = make_dispatcher(tmp_path, jira, slack)
    dispatch.run([observation()])
    first_key = jira.issue_keys()[0]
    jira.set_manual_closed(first_key)

    dispatch.run([observation()])
    assert len(jira.issues) == 2
    second_key = next(key for key in jira.issue_keys() if key != first_key)
    assert any(label.startswith("gap-episode-") for label in jira.issues[second_key]["fields"]["labels"])

    dispatch.run([observation()])
    assert len(jira.issues) == 2
    assert len([call for call in jira.calls if call[0] == "create_bug"]) == 2


def test_manual_close_then_success_then_old_failure_replay_does_not_create_successor(tmp_path, shared_clients):
    jira, slack = shared_clients
    dispatch = make_dispatcher(tmp_path, jira, slack)
    dispatch.run([observation()])
    first_key = jira.issue_keys()[0]
    jira.set_manual_closed(first_key)

    dispatch.run([observation(status="success", oid="success-1")])
    assert len(jira.issues) == 1
    dispatch.run([observation()])
    assert len(jira.issues) == 1
    assert jira.issues[first_key]["fields"]["status"]["statusCategory"]["key"] == "done"


def test_recovery_then_new_failure_creates_successor_and_stale_replay_noops(tmp_path, shared_clients):
    jira, slack = shared_clients
    dispatch = make_dispatcher(tmp_path, jira, slack)
    dispatch.run([observation()])
    dispatch.run([observation(status="success", oid="success-1")])
    dispatch.run([observation(oid="failure-2")])
    assert len(jira.issues) == 2

    dispatch.run([observation()])
    assert len(jira.issues) == 2
    assert any(call[0] == "transition_to_done" for call in jira.calls)


def test_cross_run_success_recovery_closes_open_matching(tmp_path, shared_clients):
    jira, slack = shared_clients
    dispatch1 = make_dispatcher(tmp_path, jira, slack, name="ledger-1.json")
    dispatch1.run([observation()])
    root_thread = slack.calls[0][1]["thread_ts"] if slack.calls[0][1].get("thread_ts") else None

    dispatch2 = make_dispatcher(tmp_path, jira, slack, name="ledger-2.json")
    result = dispatch2.run([observation(status="success", oid="success-1")])
    assert jira.issues["RHAI-1"]["fields"]["status"]["statusCategory"]["key"] == "done"
    assert result["outbox"][0]["state"] == "confirmed"
    assert slack.calls[-1][1]["thread_ts"] == "1.456"


def test_grouped_feasibility_branches_share_identity(tmp_path, shared_clients):
    jira, slack = shared_clients
    dispatch = make_dispatcher(tmp_path, jira, slack)
    first = observation(
        category="feasibility",
        component=None,
        evidence={"targets": {"rhoai-3.7": "failure"}, "historical_snapshot": True, "test_only": True},
    )
    second = observation(
        category="feasibility",
        component=None,
        evidence={"targets": {"rhoai-3.7": "failure", "rhoai-3.8": "failure"}, "historical_snapshot": True, "test_only": True},
    )
    dispatch.run([first])
    dispatch.run([second])
    assert len(jira.issues) == 1
    assert len(slack.calls) == 1
    assert incident_label(jira.issue_keys()[0], TRIAL_ID).startswith("gap-registry-")


def test_missing_property_and_untrusted_scope_hold(tmp_path):
    jira, slack = FakeJira(), FakeSlack()
    dispatch = make_dispatcher(tmp_path, jira, slack)
    dispatch.run([observation()])
    issue_key = jira.issue_keys()[0]

    missing = make_dispatcher(tmp_path, jira, slack, name="ledger-missing.json")
    jira.notification_state.pop(issue_key, None)
    result = missing.run([observation()])
    assert result["outbox"][0]["state"] == "pending"
    assert result["outbox"][0]["hold"] == "registry_or_delivery_unavailable"

    wrong = make_dispatcher(tmp_path, jira, slack, name="ledger-wrong.json")
    jira.issues[issue_key]["fields"]["project"] = {"key": "OTHER"}
    result = wrong.run([observation(oid="run-2")])
    assert result["outbox"][0]["state"] == "pending"
    assert result["outbox"][0]["hold"] == "registry_or_delivery_unavailable"


def test_24kib_property_size_cap_holds_if_practical(tmp_path):
    jira, slack = FakeJira(), FakeSlack()
    dispatch = make_dispatcher(tmp_path, jira, slack)
    result = dispatch.run([observation()])
    key = jira.issue_keys()[0]
    jira.notification_state[key]["seen_event_ids"] = {f"ev-{i}": "failure" for i in range(2000)}

    fresh = make_dispatcher(tmp_path, jira, slack, name="ledger-big.json")
    held = fresh.run([observation(oid="run-2")])
    assert held["outbox"][0]["state"] in {"pending", "reconciliation_required"}
    assert held["outbox"][0]["hold"] == "registry_or_delivery_unavailable"
    assert len(slack.calls) == 1
    assert jira.calls


def test_persisted_slack_inflight_and_fresh_ledger_never_posts_duplicate(tmp_path, shared_clients):
    jira, slack = shared_clients
    slack.fail_once = True
    dispatch1 = make_dispatcher(tmp_path, jira, slack, name="ledger-1.json")
    first = dispatch1.run([observation()])
    assert first["outbox"][0]["state"] in {"pending", "reconciliation_required"}
    assert first["outbox"][0]["hold"] == "registry_or_delivery_unavailable"
    assert len(slack.calls) == 1

    dispatch2 = make_dispatcher(tmp_path, jira, slack, name="ledger-1.json")
    second = dispatch2.run([observation()])
    assert len(slack.calls) == 1
    assert second["outbox"][0]["state"] in {"pending", "reconciliation_required"}


def test_rate_limit_persists_deadline_and_retries_after_it_expires(tmp_path, monkeypatch):
    jira, slack = FakeJira(), FakeSlack()
    slack.rate_limit_once = True
    dispatch = make_dispatcher(tmp_path, jira, slack)
    first = dispatch.run([observation()])
    task = first["outbox"][0]
    assert task["state"] in {"pending", "reconciliation_required"}
    assert task["retry_at"] > 0
    assert len(slack.calls) == 1

    fresh = make_dispatcher(tmp_path, jira, slack)
    before_deadline = fresh.run([])
    assert len(slack.calls) == 1
    assert before_deadline["outbox"][0]["state"] in {"pending", "reconciliation_required"}

    monkeypatch.setattr("lib.gap_notifications.dispatch_incidents.time.time", lambda: task["retry_at"] + 1)
    after_deadline = fresh.run([])
    assert len(slack.calls) == 2
    assert after_deadline["outbox"][0]["state"] == "confirmed"


def test_receipt_persistence_failure_never_loses_prior_thread(tmp_path, shared_clients):
    jira, slack = shared_clients
    dispatch = make_dispatcher(tmp_path, jira, slack)
    dispatch.run([observation()])

    def fail_on_recovery_put(key, value):
        return (
            value.get("jira_intent") is None
            and value.get("delivery", {}).get("root") is not None
            and value.get("delivery", {}).get("intent") is None
            and value.get("delivery", {}).get("last_outcome") == "success"
        )

    jira.fail_put_when = fail_on_recovery_put
    first = dispatch.run([observation(status="success", oid="success-1")])
    assert len(slack.calls) == 2
    assert slack.calls[-1][1]["thread_ts"] == "1.456"
    assert first["outbox"][1]["state"] in {"confirmed", "pending", "reconciliation_required"}

    replay = make_dispatcher(tmp_path, jira, slack)
    result = replay.run([observation(status="success", oid="success-1")])
    assert len(slack.calls) == 2
    assert result["outbox"][1]["state"] == "confirmed"


def test_multiple_open_candidates_and_query_failure_hold(tmp_path):
    jira, slack = FakeJira(), FakeSlack()
    dispatch = make_dispatcher(tmp_path, jira, slack)
    dispatch.run([observation()])
    key = jira.issue_keys()[0]
    second = copy.deepcopy(jira.issues[key])
    second["key"] = "RHAI-99"
    jira.issues["RHAI-99"] = second
    jira.notification_state["RHAI-99"] = copy.deepcopy(jira.notification_state[key])

    ambiguous = make_dispatcher(tmp_path, jira, slack, name="ledger-ambiguous.json")
    result = ambiguous.run([observation(oid="run-2")])
    assert result["outbox"][0]["state"] in {"pending", "reconciliation_required"}
    assert result["outbox"][0]["hold"] == "registry_or_delivery_unavailable"

    jira.fail_find_once = True
    queried = make_dispatcher(tmp_path, jira, slack, name="ledger-query.json")
    result = queried.run([observation(oid="run-3")])
    assert result["outbox"][0]["state"] in {"pending", "reconciliation_required"}
    assert result["outbox"][0]["hold"] == "registry_or_delivery_unavailable"
