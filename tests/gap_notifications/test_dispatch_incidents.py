"""Tests for dispatch_incidents responsibilities."""
from __future__ import annotations

import copy
import json
import pytest
from lib.gap_notifications.dispatch_incidents import IncidentError, incident_key
from lib.gap_notifications.dispatch_incidents import discussion_url, RegistryHold, validated_property
from lib.gap_notifications.lookup_tools import inspect_issue
from lib.gap_notifications.notify_tools import visible_link_status, SlackAmbiguousError, SlackPermanentError, SlackRateLimitError
from gap_notification_fakes import Jira, Slack, observation
from gap_notification_fakes import make_runner
from lib.gap_notifications.dispatch_incidents import Dispatcher, Ledger, ingest, incident_label
from lib.gap_notifications.dispatch_incidents import incident_key
from lib.gap_notifications.dispatch_incidents import Dispatcher, Ledger, incident_label
from lib.gap_notifications.notify_tools import SlackRateLimitError


# Incident identity and input validation

def identity_observation(*, environment="stage-redhat", repository="acme/gap", source_branch="feature",
                destination_branch="main", category="build", status="failure",
                observation_id="obs-1", evidence=None, component="api", pipeline=None):
    data = {
        "environment": environment,
        "repository": repository,
        "source_branch": source_branch,
        "destination_branch": destination_branch,
        "category": category,
        "status": status,
        "observation_id": observation_id,
        "evidence": evidence or {"summary": "failure"},
    }
    if component is not None:
        data["component"] = component
    if pipeline is not None:
        data["pipeline"] = pipeline
    return data


# Repository spelling is canonicalized, while environments remain separate incident identities.
def test_incident_key_is_canonical_and_environment_scoped():
    base = identity_observation(repository="acme/gap", pipeline="build-a")
    same_repo = identity_observation(repository="github.com/acme/gap", pipeline="build-a")
    mixed_case = identity_observation(repository="GitHub.com/AcMe/GaP", pipeline="build-a")
    other_env = identity_observation(environment="stage-green", repository="github.com/acme/gap", pipeline="build-a")
    assert incident_key(base) == incident_key(same_repo) == incident_key(mixed_case)
    assert incident_key(base) != incident_key(other_env)


# Distinct destination branches must remain distinct incident streams for every category.
def test_branch_stream_separation_retains_destination_branch_for_all_categories():
    build = identity_observation(destination_branch="main", pipeline="build-a")
    build_other_branch = identity_observation(destination_branch="release-1", pipeline="build-a")
    conflict = identity_observation(category="conflict", component=None, destination_branch="main")
    conflict_other_branch = identity_observation(category="conflict", component=None, destination_branch="release-1")
    feasibility = identity_observation(category="feasibility", component=None, destination_branch="rhoai-1")
    feasibility_other_branch = identity_observation(category="feasibility", component=None, destination_branch="rhoai-2")

    assert incident_key(build) != incident_key(build_other_branch)
    assert incident_key(conflict) != incident_key(conflict_other_branch)
    assert incident_key(feasibility) != incident_key(feasibility_other_branch)


# Identity calculation must be deterministic and leave the supplied observation untouched.
def test_incident_key_is_stable_and_does_not_mutate_payload():
    payload = identity_observation(
        repository="GitHub.com/AcMe/GaP",
        pipeline="build-a",
        evidence={"summary": "failure", "details": ["x", "y"]},
    )
    before = copy.deepcopy(payload)
    key = incident_key(payload)
    assert payload == before
    assert json.loads(key) == {
        "environment": "stage-redhat",
        "repository": "github.com/acme/gap",
        "source_branch": "feature",
        "destination_branch": "main",
        "category": "build",
        "component": "api",
        "pipeline": "build-a",
    }


# Missing identity fields or invalid outcomes must fail explicit observation validation.
@pytest.mark.parametrize("payload,match", [
    ({"repository": "acme/gap", "source_branch": "feature", "destination_branch": "main",
      "category": "build", "status": "failure", "observation_id": "obs-1", "evidence": {}},
     "environment"),
    ({"environment": "stage-redhat", "source_branch": "feature", "destination_branch": "main",
      "category": "build", "status": "failure", "observation_id": "obs-1", "evidence": {}},
     "repository"),
    ({"environment": "stage-redhat", "repository": "acme/gap", "destination_branch": "main",
      "category": "build", "status": "failure", "observation_id": "obs-1", "evidence": {}},
     "source_branch"),
    ({"environment": "stage-redhat", "repository": "acme/gap", "source_branch": "feature",
      "category": "build", "status": "failure", "observation_id": "obs-1", "evidence": {}},
     "destination_branch"),
    ({"environment": "stage-redhat", "repository": "acme/gap", "source_branch": "feature",
      "destination_branch": "main", "category": "build", "status": "failure",
      "observation_id": "obs-1", "evidence": {}},
     "component"),
    ({"environment": "stage-redhat", "repository": "acme/gap", "source_branch": "feature",
      "destination_branch": "main", "category": "build", "status": "bad",
      "observation_id": "obs-1", "evidence": {}, "component": "api"},
     "status"),
])
def test_validation_rejects_missing_or_invalid_observation_fields(payload, match):
    with pytest.raises(IncidentError, match=match):
        incident_key(payload)


# A valid build identity preserves its fields without inventing an optional pipeline.
def test_validation_accepts_build_observation():
    payload = identity_observation()
    before = copy.deepcopy(payload)
    key = incident_key(payload)
    assert payload == before
    assert json.loads(key) == {
        "environment": "stage-redhat",
        "repository": "github.com/acme/gap",
        "source_branch": "feature",
        "destination_branch": "main",
        "category": "build",
        "component": "api",
    }
    assert "pipeline" not in json.loads(key)


# Non-serializable or non-finite evidence must fail identity validation.
@pytest.mark.parametrize("evidence", [
    {"summary": {"nested": set()}},
    {"summary": float("nan")},
])
def test_validation_rejects_non_json_evidence(evidence):
    payload = identity_observation(evidence=evidence)
    with pytest.raises(IncidentError, match="evidence"):
        incident_key(payload)


# Lifecycle, delivery and cosmetic isolation

# A new Leader event reuses the incident thread; replaying that event sends nothing further.
def test_fresh_ledger_second_leader_reuses_jira_and_thread(setup):
    runner, jira, slack = setup
    runner().run([observation()])
    first_root = jira.properties["RHAI-1"]["delivery"]["root"]
    runner("fresh").run([observation(oid="second-leader", evidence={"leader": "new", "synthetic": True})])
    assert len(jira.issues) == 1 and len(slack.calls) == 2
    assert slack.calls[-1][1]["thread_ts"] == first_root["ts"]
    runner("third").run([observation(oid="second-leader")])
    assert len(slack.calls) == 2


# Manual closure creates one successor root for the failure, not another on each replay.
def test_manual_closure_same_event_creates_one_successor(setup):
    runner, jira, slack = setup
    runner().run([observation()])
    jira.close("RHAI-1")
    runner("fresh").run([observation()])
    runner("fresh2").run([observation()])
    assert len(jira.issues) == 2 and len(slack.calls) == 2
    assert slack.calls[1][1]["thread_ts"] is None


# Recovery closes and replies once; recurrence creates a successor that stale recovery cannot
# close.
def test_recovery_replay_and_recurrence_across_ledgers(setup):
    runner, jira, slack = setup
    runner().run([observation()])
    recovered = runner("b").run([observation("success", "ok")])
    assert recovered["outbox"][0]["state"] == "confirmed"
    assert jira.closed("RHAI-1")
    assert slack.calls[-1][1]["thread_ts"] == "123.000001"
    runner("c").run([observation()])
    assert len(jira.issues) == 1
    runner("d").run([observation(oid="genuinely-new")])
    assert len(jira.issues) == 2
    runner("e").run([observation("success", "ok")])  # inherited old recovery event
    assert not jira.closed("RHAI-2")


# New recovery evidence must prevent old manually closed history from spawning extra incidents.
def test_newer_recovery_overrides_old_manually_closed_history(setup):
    runner, jira, slack = setup
    runner().run([observation()])
    jira.close("RHAI-1")
    runner("b").run([observation()])
    runner("c").run([observation("success", "ok")])
    runner("d").run([observation()])
    assert len(jira.issues) == 2


# Positive recovery after manual closure updates metadata and the thread without reopening Jira.
def test_manual_close_then_positive_recovery_prevents_old_failure_recreation(setup):
    runner, jira, slack = setup
    runner().run([observation()])
    jira.close("RHAI-1")
    runner("recovery").run([observation("success", "ok")])
    assert jira.properties["RHAI-1"]["last_outcome"] == "success"
    assert not any(isinstance(call, tuple) for call in jira.calls)  # No status mutation on done.
    assert slack.calls[-1][1]["thread_ts"] == "123.000001"
    runner("old-replay").run([observation()])
    assert len(jira.issues) == 1


# A definite rate rejection may retry only after its persisted deadline, even with a fresh
# journal.
def test_definite_rate_limit_retries_after_remote_deadline_across_fresh_ledgers(setup, monkeypatch):
    from lib.gap_notifications.notify_tools import SlackRateLimitError
    import lib.gap_notifications.dispatch_incidents as module
    runner, jira, slack = setup
    monkeypatch.setattr(module.time, "time", lambda: 1000)
    original = slack.send
    attempted = []
    def limited(*args, **kwargs):
        attempted.append(True)
        raise SlackRateLimitError("rate limited", retry_after=20)
    slack.send = limited
    runner().run([observation()])
    assert jira.properties["RHAI-1"]["delivery"]["intent"]["retry_at"] == 1020
    runner("before").run([observation()])
    assert len(attempted) == 1 and slack.calls == []
    monkeypatch.setattr(module.time, "time", lambda: 1021)
    slack.send = original
    runner("after").run([observation()])
    assert len(jira.issues) == 1 and len(slack.calls) == 1
    assert jira.properties["RHAI-1"]["delivery"]["intent"] is None


# Unknown observations and orphan successes must not create extra incidents or Slack sends.
def test_unknown_no_mutations_and_initial_success_replay_locally(setup):
    runner, jira, slack = setup
    dispatch = runner()
    dispatch.run([observation("success", "initial")])
    dispatch.run([observation()])
    jira.close("RHAI-1")
    runner("unknown").run([observation("unknown", "unknown")])
    assert len(jira.issues) == 1
    dispatch.run([observation("success", "initial")])
    assert len(slack.calls) == 1


# An uncertain Slack send must hold until an operator supplies a receipt, never blindly repost.
def test_ambiguous_slack_holds_fresh_runner_until_manual_receipt(setup):
    runner, jira, slack = setup
    slack.fail = True
    result = runner().run([observation()])
    assert jira.properties["RHAI-1"]["delivery"]["intent"]
    fresh = runner("fresh")
    result = fresh.run([observation()])
    assert len(slack.calls) == 1
    slack.fail = False
    result = fresh.run([], receipt={"task_id": result["outbox"][0]["id"], "receipt": {
        "key": "RHAI-1", "system": "slack", "channel": CHANNEL, "ts": "123.000001", "reason": "operator verified"}})
    assert result["audit"]
    assert len(slack.calls) == 1
    assert jira.properties["RHAI-1"]["delivery"]["intent"] is None


# A confirmed local receipt must prevent another post when remote receipt persistence fails.
def test_receipt_persistence_failure_cannot_repost_on_fresh_runner(setup):
    runner, jira, slack = setup
    jira.fail_receipt = True
    result = runner().run([observation()])
    assert result["outbox"][0]["slack_receipt"]["ts"] == "123.000001"
    jira.fail_receipt = False
    runner("fresh").run([observation()])
    assert len(slack.calls) == 1


# An uncertain create must not be repeated from the same journal or expose transport secrets.
def test_create_ambiguity_never_blindly_retries_same_journal(setup):
    runner, jira, slack = setup
    jira.fail_create = True
    dispatch = runner()
    dispatch.run([observation()])
    dispatch.run([observation()])
    assert jira.calls == ["create"] and slack.calls == []
    assert "SECRET" not in dispatch.ledger.path.read_text()


# A known created key must reconcile through readback despite delayed search indexing.
def test_known_create_key_reconciles_when_search_lags(setup):
    runner, jira, slack = setup
    jira.fail_readback = True
    dispatch = runner()
    dispatch.run([observation()])
    jira.find_by_label = lambda label: []
    dispatch.run([observation()])
    assert jira.calls.count("create") == 1 and len(slack.calls) == 1


# Duplicate or competing open episodes must hold without changing Jira state or sending Slack.
@pytest.mark.parametrize("distinct_episodes", [False, True], ids=["duplicate-episode", "distinct-open-episodes"])
def test_ambiguous_registry_episodes_hold_without_mutation(setup, distinct_episodes):
    runner, jira, slack = setup
    runner().run([observation()])
    jira.issues["RHAI-2"] = copy.deepcopy(jira.issues["RHAI-1"])
    jira.issues["RHAI-2"]["key"] = "RHAI-2"
    jira.properties["RHAI-2"] = copy.deepcopy(jira.properties["RHAI-1"])
    if distinct_episodes:
        episode = "00000000-0000-4000-8000-000000000002"
        assert episode != jira.properties["RHAI-1"]["episode_id"]
        jira.properties["RHAI-2"]["episode_id"] = episode
        labels = jira.issues["RHAI-2"]["fields"]["labels"]
        labels[:] = [label for label in labels if not label.startswith("gap-episode-")]
        labels.append("gap-episode-" + episode.replace("-", ""))
    # Both individual records are valid: the collection-level guard must hold.
    for key in ("RHAI-1", "RHAI-2"):
        validated_property(jira.issues[key], jira.properties[key], trial="trial")
        assert not jira.closed(key)
    before = copy.deepcopy((jira.calls, jira.properties))
    dispatch = runner("fresh")
    result = dispatch.run([observation(oid="new")])
    task = result["outbox"][0]
    assert task["state"] == "reconciliation_required"
    assert task["hold"] == "registry_or_delivery_unavailable"
    expected = "multiple open incidents" if distinct_episodes else "duplicate registry results or episodes"
    with pytest.raises(RegistryHold, match=expected):
        dispatch._registry(task)
    assert (jira.calls, jira.properties) == before and len(slack.calls) == 1


# A registry property from another scope must not authorize delivery.
def test_wrong_property_scope_holds_without_send(setup):
    runner, jira, slack = setup
    runner().run([observation()])
    jira.properties["RHAI-1"]["scope"] = "wrong"
    result = runner("fresh").run([observation(oid="new")])
    assert len(slack.calls) == 1
    assert result["outbox"][0]["state"] != "confirmed"


# Trial namespaces stay distinct, and incompatible journals must be rejected.
def test_trial_and_v1_ledger_isolation():
    obs = observation()
    assert incident_label(incident_key(obs), "one") != incident_label(incident_key(obs), "two")
    ledger = ingest({}, [obs], trial_id="one")
    with pytest.raises(ValueError):
        ingest(ledger, [], trial_id="two")
    with pytest.raises(ValueError):
        ingest({"schema": 1, "trial_id": "one"}, [], trial_id="one")


# Non-JSON evidence must be rejected without Jira mutations or Slack sends.
@pytest.mark.parametrize("evidence", [{"bad": {1}}, {"bad": float("nan")}, {"bad": object()}])
def test_non_json_evidence_rejected_before_network(setup, evidence):
    runner, jira, slack = setup
    with pytest.raises((ValueError, TypeError)):
        runner().run([observation(evidence=evidence)])
    assert jira.calls == slack.calls == []


# A durable unresolved Jira intent must block further mutation until reconciliation.
def test_durable_jira_intent_holds_before_mutation(setup):
    runner, jira, slack = setup
    runner().run([observation()])
    state = jira.properties["RHAI-1"]
    before_calls = list(jira.calls)
    state["jira_intent"] = {"event_id": "a" * 64, "outcome": "success"}
    result = runner("fresh").run([observation(oid="next")])
    assert result["outbox"][0]["state"] == "reconciliation_required"
    assert jira.calls == before_calls and len(slack.calls) == 1


# Even structurally valid registry data must hold when it exceeds the property size limit.
def test_valid_property_exceeding_capacity_holds_before_mutation(setup):
    runner, jira, slack = setup
    runner().run([observation()])
    state = jira.properties["RHAI-1"]
    validated_property(jira.issues["RHAI-1"], state, trial="trial")
    before_calls = list(jira.calls)
    # Change only the number of valid event receipts, preserving current events.
    state["seen_event_ids"].update({f"{i:064x}": "failure" for i in range(400)})
    assert state["last_event"] in state["seen_event_ids"]
    assert state["delivery"]["last_event"] in state["seen_event_ids"]
    assert all(len(event) == 64 and set(event) <= set("0123456789abcdef")
               for event in state["seen_event_ids"])
    assert len(json.dumps(state, separators=(",", ":")).encode()) > 24 * 1024
    result = runner("capacity").run([observation(oid="next")])
    assert result["outbox"][0]["state"] != "confirmed"
    assert result["outbox"][0]["hold"] == "registry_or_delivery_unavailable"
    assert jira.calls == before_calls and len(slack.calls) == 1


# Registry lookup failure must leave work pending rather than create an incident or send Slack.
def test_query_failure_does_not_create_or_post(setup):
    runner, jira, slack = setup
    def fail(label):
        raise RuntimeError("query unavailable")
    jira.find_by_label = fail
    result = runner().run([observation()])
    assert result["outbox"][0]["state"] == "pending"
    assert jira.calls == slack.calls == []


@pytest.fixture
def setup(tmp_path):
    return make_runner(tmp_path)


# Cosmetic link repair must preserve confirmed delivery and never resend the Slack message.
def test_projection_failure_then_same_event_repair_no_resend(setup, monkeypatch):
    runner, jira, slack = setup
    original = jira.update_issue
    def fail(*a, **kw):
        raise TimeoutError('SECRET')
    monkeypatch.setattr(jira, 'update_issue', fail)
    result = runner().run([observation()])
    task = result['outbox'][-1]
    assert task['state'] == 'confirmed' and task['presentation_warning'] == 'discussion_projection_unavailable'
    prop = copy.deepcopy(jira.properties['RHAI-1'])
    assert prop['delivery']['root'] and prop['delivery']['intent'] is None
    assert prop['jira_intent'] is None and len(slack.calls) == 1
    monkeypatch.setattr(jira, 'update_issue', original)
    repaired = runner().run([observation()])
    assert 'presentation_warning' not in repaired['outbox'][-1]
    assert jira.properties['RHAI-1'] == prop and len(slack.calls) == 1
    url = discussion_url(prop['delivery']['root'])
    assert visible_link_status(jira.issues['RHAI-1']['fields']['description'], url) == 'match'
    runner().run([observation(oid='next')])
    assert visible_link_status(jira.issues['RHAI-1']['fields']['description'], url) == 'match'
    assert inspect_issue(jira, 'RHAI-1')['visible_metadata_link'] == 'match'


# Uncertain delivery must not project a thread URL supplied by untrusted evidence.
def test_uncertain_root_no_projection_or_spoofed_evidence(setup):
    runner, jira, slack = setup
    slack.fail = True
    runner().run([observation(evidence={'discussion_url': 'https://evil.test', 'root': {'channel': 'x', 'ts': '1.2'}})])
    assert 'Slack discussion' not in json.dumps(jira.issues['RHAI-1']['fields']['description'])
    report = inspect_issue(jira, 'RHAI-1')
    assert report['discussion_url'] is None and report['slack_intent']['state'] == 'inflight'


# Only the confirmed receipt may determine the projected Slack thread, not evidence hints.
def test_successful_spoofed_root_ignored(setup):
    runner, jira, _ = setup
    runner().run([observation(evidence={'root': {'channel': 'EVIL', 'ts': '1.2'}, 'discussion_url': 'https://evil.test', 'metadata_url': 'https://evil.test'})])
    report = inspect_issue(jira, 'RHAI-1')
    assert report['discussion_url'] == 'https://redhat-internal.slack.com/archives/C0883L8FARE/p123000001'
    assert 'evil.test' not in json.dumps(jira.issues['RHAI-1']['fields']['description'])


# A closure race during cosmetic projection must not undo a confirmed delivery.
def test_closed_race_projection_is_cosmetic_only(setup, monkeypatch):
    runner, jira, slack = setup
    original = jira.update_issue
    def close_then_update(key, **kwargs):
        jira.close(key)
        return original(key, **kwargs)  # Fake adapter refuses mutation of done issue.
    monkeypatch.setattr(jira, 'update_issue', close_then_update)
    result = runner().run([observation()])
    assert result['outbox'][-1]['state'] == 'confirmed'
    assert result['outbox'][-1]['presentation_warning']
    assert len(slack.calls) == 1 and jira.properties['RHAI-1']['delivery']['root']
    assert jira.properties['RHAI-1']['delivery']['intent'] is None


# Cosmetic metadata work must preserve the original delivery hold and sanitized error state.
@pytest.mark.parametrize('error,state', [(SlackAmbiguousError, 'inflight'), (SlackPermanentError, 'rejected'), (SlackRateLimitError, 'rate_limited')])
@pytest.mark.parametrize('projection_fails', [False, True])
def test_metadata_on_delivery_failure_preserves_original_core_state(setup, monkeypatch, error, state, projection_fails):
    runner, jira, slack = setup
    monkeypatch.setattr('lib.gap_notifications.dispatch_incidents.time.time', lambda: 1800000000)
    def send(*a, **kw):
        raise error('ORIGINAL_SECRET', retry_after=30)
    monkeypatch.setattr(slack, 'send', send)
    if projection_fails:
        def fail(*a, **kw):
            raise RuntimeError('PROJECTION_SECRET')
        monkeypatch.setattr(jira, 'update_issue', fail)
    result = runner().run([observation(evidence={'metadata_url': 'https://evil.test'})])
    task = result['outbox'][-1]
    prop = jira.properties['RHAI-1']
    assert task['state'] == 'reconciliation_required'
    assert task['error_type'] == 'RegistryHold' and task['hold'] == 'registry_or_delivery_unavailable'
    assert prop['delivery']['root'] is None and prop['delivery']['intent']['state'] == state
    if state == 'rate_limited':
        assert prop['delivery']['intent']['retry_at'] == 1800000030
        assert task['retry_at'] == 1800000030
    if projection_fails:
        assert task['presentation_warning'] == 'discussion_projection_unavailable'
    else:
        report = inspect_issue(jira, 'RHAI-1')
        assert report['discussion_url'] is None and report['visible_metadata_link'] == 'match'
        assert report['metadata_url'] == 'https://stage-redhat.atlassian.net/rest/api/3/issue/RHAI-1/properties/gap.notification.v1'
        assert 'Jira permissions required.' in json.dumps(jira.issues['RHAI-1']['fields']['description'])
    assert 'SECRET' not in json.dumps(result)


# Failed fresh property validation must suppress cosmetic writes, not invalidate delivery.
def test_fresh_property_read_failure_prevents_projection(setup, monkeypatch):
    runner, jira, _ = setup
    original = jira.get_notification_state
    reads = []
    def read(key):
        reads.append(key)
        if len(reads) > 1:
            raise RuntimeError('unavailable')
        return original(key)
    monkeypatch.setattr(jira, 'get_notification_state', read)
    result = runner().run([observation()])
    assert result['outbox'][-1]['state'] == 'confirmed'
    assert result['outbox'][-1]['presentation_warning']
    assert jira.calls == ['create']


# A root not persisted in Jira must not appear as a confirmed discussion link.
def test_root_receipt_write_failure_does_not_project_discussion(setup):
    runner, jira, _ = setup
    jira.fail_receipt = True
    runner().run([observation()])
    assert jira.properties['RHAI-1']['delivery']['root'] is None
    assert 'Slack discussion' not in json.dumps(jira.issues['RHAI-1']['fields']['description'])


# Closed issues must not receive retrospective cosmetic backfills.
def test_done_never_backfilled(setup, monkeypatch):
    runner, jira, _ = setup
    original = jira.update_issue
    monkeypatch.setattr(jira, 'update_issue', lambda *a, **kw: (_ for _ in ()).throw(TimeoutError()))
    runner().run([observation()])
    monkeypatch.setattr(jira, 'update_issue', original)
    jira.close('RHAI-1')
    before = copy.deepcopy(jira.issues['RHAI-1'])
    runner().run([observation(status='success', oid='recovered')])
    assert jira.issues['RHAI-1'] == before


# Durable registry and restart contracts

ENVIRONMENT = "stage-redhat.atlassian.net/RHAI/C0883L8FARE"
TRIAL_ID = "leader84-local-001"
TRANSITION_ID = "41"
TRANSITION_FIELDS = {"resolution": {"id": "1"}}


def registry_observation(*, status="failure", oid="run-1", category="build", destination_branch="stable",
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


# A fresh journal in the same trial must reuse the confirmed ticket and thread without another
# send.
def test_fresh_ledgers_same_trial_reuse_ticket_and_thread(tmp_path, shared_clients):
    jira, slack = shared_clients
    dispatch1 = make_dispatcher(tmp_path, jira, slack, name="ledger-a.json")
    first = dispatch1.run([registry_observation()])
    assert first["outbox"][0]["state"] == "confirmed"
    assert len(jira.issues) == 1
    assert len(slack.calls) == 1

    dispatch2 = make_dispatcher(tmp_path, jira, slack, name="ledger-b.json")
    second = dispatch2.run([registry_observation()])
    assert len(jira.issues) == 1
    assert len(slack.calls) == 1
    assert second["outbox"][0]["state"] == "confirmed"
    assert second["outbox"][0]["incident_key"] == first["outbox"][0]["incident_key"]


# A manually closed episode gets exactly one successor, including on fresh replay.
def test_manual_close_same_observation_creates_one_successor_and_replay_stays_one(tmp_path, shared_clients):
    jira, slack = shared_clients
    dispatch = make_dispatcher(tmp_path, jira, slack)
    dispatch.run([registry_observation()])
    first_key = jira.issue_keys()[0]
    jira.set_manual_closed(first_key)

    dispatch.run([registry_observation()])
    assert len(jira.issues) == 2
    second_key = next(key for key in jira.issue_keys() if key != first_key)
    assert any(label.startswith("gap-episode-") for label in jira.issues[second_key]["fields"]["labels"])

    dispatch.run([registry_observation()])
    assert len(jira.issues) == 2
    assert len([call for call in jira.calls if call[0] == "create_bug"]) == 2


# Explicit recovery after manual closure must make old failure replay a no-op.
def test_manual_close_then_success_then_old_failure_replay_does_not_create_successor(tmp_path, shared_clients):
    jira, slack = shared_clients
    dispatch = make_dispatcher(tmp_path, jira, slack)
    dispatch.run([registry_observation()])
    first_key = jira.issue_keys()[0]
    jira.set_manual_closed(first_key)

    dispatch.run([registry_observation(status="success", oid="success-1")])
    assert len(jira.issues) == 1
    dispatch.run([registry_observation()])
    assert len(jira.issues) == 1
    assert jira.issues[first_key]["fields"]["status"]["statusCategory"]["key"] == "done"


# New failure after recovery creates one successor; replay must not create a third episode.
def test_recovery_then_new_failure_creates_successor_and_stale_replay_noops(tmp_path, shared_clients):
    jira, slack = shared_clients
    dispatch = make_dispatcher(tmp_path, jira, slack)
    dispatch.run([registry_observation()])
    dispatch.run([registry_observation(status="success", oid="success-1")])
    dispatch.run([registry_observation(oid="failure-2")])
    assert len(jira.issues) == 2

    dispatch.run([registry_observation()])
    assert len(jira.issues) == 2
    assert any(call[0] == "transition_to_done" for call in jira.calls)


# Grouped target evidence must reuse one feasibility ticket and Slack root.
def test_grouped_feasibility_branches_share_identity(tmp_path, shared_clients):
    jira, slack = shared_clients
    dispatch = make_dispatcher(tmp_path, jira, slack)
    first = registry_observation(
        category="feasibility",
        component=None,
        evidence={"targets": {"rhoai-3.7": "failure"}, "historical_snapshot": True, "test_only": True},
    )
    second = registry_observation(
        category="feasibility",
        component=None,
        evidence={"targets": {"rhoai-3.7": "failure", "rhoai-3.8": "failure"}, "historical_snapshot": True, "test_only": True},
    )
    dispatch.run([first])
    dispatch.run([second])
    assert len(jira.issues) == 1
    assert len(slack.calls) == 1
    assert incident_label(jira.issue_keys()[0], TRIAL_ID).startswith("gap-registry-")


# Missing or untrusted registry properties must hold delivery rather than establish authority.
def test_missing_property_and_untrusted_scope_hold(tmp_path):
    jira, slack = FakeJira(), FakeSlack()
    dispatch = make_dispatcher(tmp_path, jira, slack)
    dispatch.run([registry_observation()])
    issue_key = jira.issue_keys()[0]

    missing = make_dispatcher(tmp_path, jira, slack, name="ledger-missing.json")
    jira.notification_state.pop(issue_key, None)
    result = missing.run([registry_observation()])
    assert result["outbox"][0]["state"] == "pending"
    assert result["outbox"][0]["hold"] == "registry_or_delivery_unavailable"

    wrong = make_dispatcher(tmp_path, jira, slack, name="ledger-wrong.json")
    jira.issues[issue_key]["fields"]["project"] = {"key": "OTHER"}
    result = wrong.run([registry_observation(oid="run-2")])
    assert result["outbox"][0]["state"] == "pending"
    assert result["outbox"][0]["hold"] == "registry_or_delivery_unavailable"


# Restarting with a persisted Slack inflight intent must not post another message.
def test_persisted_slack_inflight_same_journal_restart_never_posts_duplicate(tmp_path, shared_clients):
    jira, slack = shared_clients
    slack.fail_once = True
    dispatch1 = make_dispatcher(tmp_path, jira, slack, name="ledger-1.json")
    first = dispatch1.run([registry_observation()])
    assert first["outbox"][0]["state"] in {"pending", "reconciliation_required"}
    assert first["outbox"][0]["hold"] == "registry_or_delivery_unavailable"
    assert len(slack.calls) == 1

    dispatch2 = make_dispatcher(tmp_path, jira, slack, name="ledger-1.json")
    second = dispatch2.run([registry_observation()])
    assert len(slack.calls) == 1
    assert second["outbox"][0]["state"] in {"pending", "reconciliation_required"}


# A confirmed rate rejection must persist its deadline and retry only after it expires.
def test_rate_limit_persists_deadline_and_retries_after_it_expires(tmp_path, monkeypatch):
    jira, slack = FakeJira(), FakeSlack()
    slack.rate_limit_once = True
    dispatch = make_dispatcher(tmp_path, jira, slack)
    first = dispatch.run([registry_observation()])
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


# Failed reply-ack persistence must retain the original root and local receipt, then hold
# without resend.
def test_receipt_persistence_failure_never_loses_prior_thread(tmp_path, shared_clients):
    jira, slack = shared_clients
    dispatch = make_dispatcher(tmp_path, jira, slack)
    dispatch.run([registry_observation()])
    original_root = copy.deepcopy(jira.notification_state["RHAI-1"]["delivery"]["root"])
    original_event = jira.notification_state["RHAI-1"]["delivery"]["last_event"]
    fired_faults = []

    def fail_on_recovery_put(key, value):
        delivery = value["delivery"]
        final_reply_ack = (
            value.get("jira_intent") is None and value.get("last_outcome") == "success"
            and delivery["root"] == original_root and delivery["intent"] is None
            and delivery["last_event"] == value["last_event"]
        )
        if final_reply_ack and not fired_faults:
            assert len(slack.calls) == 2
            assert slack.calls[-1][1]["thread_ts"] == original_root["ts"]
            fired_faults.append(copy.deepcopy(value))
            return True
        return False

    jira.fail_put_when = fail_on_recovery_put
    first = dispatch.run([registry_observation(status="success", oid="success-1")])
    assert len(fired_faults) == 1
    assert fired_faults[0]["delivery"]["last_event"] != original_event
    task = first["outbox"][1]
    assert task["slack_receipt"] == {"channel": CHANNEL, "ts": "2.456"}
    assert task["state"] == "pending"
    assert task["hold"] == "registry_or_delivery_unavailable"
    assert task["error_type"] == "RuntimeError"
    delivery = copy.deepcopy(jira.notification_state["RHAI-1"]["delivery"])
    assert delivery["root"] == original_root and delivery["last_event"] == original_event
    assert delivery["intent"]["state"] == "inflight"
    assert len(slack.calls) == 2

    replay = make_dispatcher(tmp_path, jira, slack)
    result = replay.run([registry_observation(status="success", oid="success-1")])
    assert len(fired_faults) == 1 and len(slack.calls) == 2
    assert result["outbox"][1]["state"] == "reconciliation_required"
    assert result["outbox"][1]["hold"] == "registry_or_delivery_unavailable"
    assert jira.notification_state["RHAI-1"]["delivery"] == delivery


# Lookup failure against a populated registry must leave issues, properties and Slack unchanged.
def test_query_failure_with_existing_registry_holds(tmp_path):
    jira, slack = FakeJira(), FakeSlack()
    dispatch = make_dispatcher(tmp_path, jira, slack)
    dispatch.run([registry_observation()])
    before = copy.deepcopy((jira.issues, jira.notification_state, slack.calls))
    jira.fail_find_once = True
    queried = make_dispatcher(tmp_path, jira, slack, name="ledger-query.json")
    result = queried.run([registry_observation(oid="run-3")])
    assert result["outbox"][0]["state"] in {"pending", "reconciliation_required"}
    assert result["outbox"][0]["hold"] == "registry_or_delivery_unavailable"
    assert (jira.issues, jira.notification_state, slack.calls) == before


# Explicit production selection must isolate identity while retaining replay and recovery rules.
@pytest.mark.parametrize('environment', ['stage', 'production'])
def test_environment_scoped_lifecycle(tmp_path, environment):
    from lib.gap_notifications.notify_tools import routing_target as jira_target
    target = jira_target(environment)
    jira, slack = Jira(), Slack()
    jira.base_url = target.url
    obs = observation(environment=target.scope)
    def runner(name, transition=None):
        return Dispatcher(Ledger(tmp_path / (name + '.json')), jira, slack,
                          trial_id='same-trial', transition_id=transition, **target.options)
    first = runner('first').run([obs])
    assert first['outbox'][0]['state'] == 'confirmed'
    assert not first['outbox'][0].get('presentation_warning')
    assert jira.properties['RHAI-1']['scope'] == target.scope
    original = copy.deepcopy(jira.properties)
    root = original['RHAI-1']['delivery']['root']
    replay = runner('fresh').run([obs])
    assert not replay['outbox'][0].get('presentation_warning')
    assert jira.properties == original and len(slack.calls) == 1
    assert target.url + '/browse/RHAI-1' in slack.calls[0][0]
    assert target.url + '/rest/api/3/issue/RHAI-1/properties/' in json.dumps(jira.issues)
    assert target.banner in jira.issues['RHAI-1']['fields']['summary']
    success = dict(obs, status='success', observation_id='passing-run')
    held = runner('no-transition').run([success])
    assert held['outbox'][0]['state'] != 'confirmed' and not jira.closed('RHAI-1')
    runner('recovery', '999').run([success])
    assert jira.closed('RHAI-1') and slack.calls[-1][1]['thread_ts'] == root['ts']
    runner('recurrence').run([dict(obs, observation_id='new-failure')])
    assert len(jira.issues) == 2 and slack.calls[-1][1]['thread_ts'] is None


# Stage and production must never share incident labels, even with the same trial and event.
def test_environment_is_part_of_incident_identity():
    from lib.gap_notifications.notify_tools import routing_target as jira_target
    keys = [incident_key(observation(environment=jira_target(env).scope)) for env in ('stage', 'production')]
    assert keys[0] != keys[1]
    assert incident_label(keys[0], 'trial') != incident_label(keys[1], 'trial')


# A selected target must reject foreign observations before any service lookup or write.
@pytest.mark.parametrize('selected,observed', [('stage', 'production'), ('production', 'stage')])
def test_cross_environment_observation_rejected_before_services(tmp_path, selected, observed):
    from lib.gap_notifications.notify_tools import routing_target as jira_target
    class NoServices:
        def __getattr__(self, name):
            if name == 'base_url': return jira_target(selected).url
            raise AssertionError('service access')
    runner = Dispatcher(Ledger(tmp_path / 'journal.json'), NoServices(), NoServices(), jira_environment=selected)
    with pytest.raises(ValueError, match='scope'):
        runner.run([observation(environment=jira_target(observed).scope)])
    assert not runner.ledger.path.exists()


# Rebinding a journal must not let pending or confirmed staging work drain into production.
@pytest.mark.parametrize('tamper', ['none', 'scope-only', 'create-only'])
def test_cross_environment_journal_rejected(tmp_path, tamper):
    from lib.gap_notifications.dispatch_incidents import bind_trial
    from lib.gap_notifications.notify_tools import routing_target as jira_target
    data = ingest({}, [observation()], trial_id='trial')
    if tamper != 'none': data['scope'] = jira_target('production').scope
    if tamper == 'create-only':
        data['creates'] = {data['outbox'][0]['incident_key']: {'state': 'inflight'}}
        data['outbox'] = []
    with pytest.raises(ValueError, match='scope|environment'):
        bind_trial(data, 'trial', jira_environment='production')


# Unbound legacy journals remain staging-only and retain their existing delivery receipts.
def test_legacy_stage_journal_remains_compatible(tmp_path):
    from lib.gap_notifications.dispatch_incidents import bind_trial
    runner, jira, slack = make_runner(tmp_path)
    first = runner().run([observation()])
    first.pop('scope')
    before = copy.deepcopy(jira.properties)
    assert bind_trial(first, 'trial')['scope'] == observation()['environment']
    with pytest.raises(ValueError, match='environment'):
        bind_trial(first, 'trial', jira_environment='production')
    ledger = Ledger(tmp_path / 'legacy.json')
    ledger.save(first)
    result = Dispatcher(ledger, jira, slack, trial_id='trial').run([observation()])
    assert result['outbox'][0]['state'] == 'confirmed'
    assert jira.properties == before and len(slack.calls) == 1


# A forged property scope cannot hide a foreign environment inside its stored incident identity.
def test_cross_environment_property_is_rejected(tmp_path):
    from lib.gap_notifications.notify_tools import routing_target as jira_target
    runner, jira, _ = make_runner(tmp_path)
    runner().run([observation()])
    state = copy.deepcopy(jira.properties['RHAI-1'])
    state['scope'] = jira_target('production').scope
    with pytest.raises(ValueError, match='scope'):
        validated_property(jira.issues['RHAI-1'], state, jira_environment='production')


# A mismatched or later-mutated adapter origin must fail before the dispatcher can query it.
def test_adapter_origin_must_match_explicit_environment(tmp_path):
    jira, slack = Jira(), Slack()
    jira.base_url = 'https://stage-redhat.atlassian.net'
    with pytest.raises(ValueError, match='environment'):
        Dispatcher(Ledger(tmp_path / 'p.json'), jira, slack, jira_environment='production')
    runner = Dispatcher(Ledger(tmp_path / 's.json'), jira, slack)
    jira.base_url = 'https://redhat.atlassian.net'
    with pytest.raises(ValueError, match='environment'):
        runner.run([])
    assert not jira.calls and not slack.calls

CHANNEL = "C0883L8FARE"
