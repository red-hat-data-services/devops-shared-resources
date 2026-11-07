import json
from lib.gap_notifications.dispatch_incidents import discussion_url, RegistryHold, validated_property
from lib.gap_notifications.lookup_tools import inspect_issue
from lib.gap_notifications.notify_tools import visible_link_status, SlackAmbiguousError, SlackPermanentError, SlackRateLimitError
from gap_notification_fakes import Jira, Slack, observation
from gap_notification_fakes import make_runner
import copy

import pytest

from lib.gap_notifications.dispatch_incidents import CHANNEL, Dispatcher, Ledger, ingest, incident_label
from lib.gap_notifications.dispatch_incidents import incident_key


def test_fresh_ledger_second_leader_reuses_jira_and_thread(setup):
    runner, jira, slack = setup
    runner().run([observation()])
    first_root = jira.properties["RHAI-1"]["delivery"]["root"]
    runner("fresh").run([observation(oid="second-leader", evidence={"leader": "new", "synthetic": True})])
    assert len(jira.issues) == 1 and len(slack.calls) == 2
    assert slack.calls[-1][1]["thread_ts"] == first_root["ts"]
    runner("third").run([observation(oid="second-leader")])
    assert len(slack.calls) == 2


def test_manual_closure_same_event_creates_one_successor(setup):
    runner, jira, slack = setup
    runner().run([observation()])
    jira.close("RHAI-1")
    runner("fresh").run([observation()])
    runner("fresh2").run([observation()])
    assert len(jira.issues) == 2 and len(slack.calls) == 2
    assert slack.calls[1][1]["thread_ts"] is None


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


def test_newer_recovery_overrides_old_manually_closed_history(setup):
    runner, jira, slack = setup
    runner().run([observation()])
    jira.close("RHAI-1")
    runner("b").run([observation()])
    runner("c").run([observation("success", "ok")])
    runner("d").run([observation()])
    assert len(jira.issues) == 2


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


def test_receipt_persistence_failure_cannot_repost_on_fresh_runner(setup):
    runner, jira, slack = setup
    jira.fail_receipt = True
    result = runner().run([observation()])
    assert result["outbox"][0]["slack_receipt"]["ts"] == "123.000001"
    jira.fail_receipt = False
    runner("fresh").run([observation()])
    assert len(slack.calls) == 1


def test_create_ambiguity_never_blindly_retries_same_journal(setup):
    runner, jira, slack = setup
    jira.fail_create = True
    dispatch = runner()
    dispatch.run([observation()])
    dispatch.run([observation()])
    assert jira.calls == ["create"] and slack.calls == []
    assert "SECRET" not in dispatch.ledger.path.read_text()


def test_known_create_key_reconciles_when_search_lags(setup):
    runner, jira, slack = setup
    jira.fail_readback = True
    dispatch = runner()
    dispatch.run([observation()])
    jira.find_by_label = lambda label: []
    dispatch.run([observation()])
    assert jira.calls.count("create") == 1 and len(slack.calls) == 1


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


def test_wrong_property_scope_holds_without_send(setup):
    runner, jira, slack = setup
    runner().run([observation()])
    jira.properties["RHAI-1"]["scope"] = "wrong"
    result = runner("fresh").run([observation(oid="new")])
    assert len(slack.calls) == 1
    assert result["outbox"][0]["state"] != "confirmed"


def test_trial_and_v1_ledger_isolation():
    obs = observation()
    assert incident_label(incident_key(obs), "one") != incident_label(incident_key(obs), "two")
    ledger = ingest({}, [obs], trial_id="one")
    with pytest.raises(ValueError):
        ingest(ledger, [], trial_id="two")
    with pytest.raises(ValueError):
        ingest({"schema": 1, "trial_id": "one"}, [], trial_id="one")


@pytest.mark.parametrize("evidence", [{"bad": {1}}, {"bad": float("nan")}, {"bad": object()}])
def test_non_json_evidence_rejected_before_network(setup, evidence):
    runner, jira, slack = setup
    with pytest.raises((ValueError, TypeError)):
        runner().run([observation(evidence=evidence)])
    assert jira.calls == slack.calls == []


def test_durable_jira_intent_holds_before_mutation(setup):
    runner, jira, slack = setup
    runner().run([observation()])
    state = jira.properties["RHAI-1"]
    before_calls = list(jira.calls)
    state["jira_intent"] = {"event_id": "a" * 64, "outcome": "success"}
    result = runner("fresh").run([observation(oid="next")])
    assert result["outbox"][0]["state"] == "reconciliation_required"
    assert jira.calls == before_calls and len(slack.calls) == 1


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


def test_uncertain_root_no_projection_or_spoofed_evidence(setup):
    runner, jira, slack = setup
    slack.fail = True
    runner().run([observation(evidence={'discussion_url': 'https://evil.test', 'root': {'channel': 'x', 'ts': '1.2'}})])
    assert 'Slack discussion' not in json.dumps(jira.issues['RHAI-1']['fields']['description'])
    report = inspect_issue(jira, 'RHAI-1')
    assert report['discussion_url'] is None and report['slack_intent']['state'] == 'inflight'


def test_successful_spoofed_root_ignored(setup):
    runner, jira, _ = setup
    runner().run([observation(evidence={'root': {'channel': 'EVIL', 'ts': '1.2'}, 'discussion_url': 'https://evil.test', 'metadata_url': 'https://evil.test'})])
    report = inspect_issue(jira, 'RHAI-1')
    assert report['discussion_url'] == 'https://redhat-internal.slack.com/archives/C0883L8FARE/p123000001'
    assert 'evil.test' not in json.dumps(jira.issues['RHAI-1']['fields']['description'])


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


def test_root_receipt_write_failure_does_not_project_discussion(setup):
    runner, jira, _ = setup
    jira.fail_receipt = True
    runner().run([observation()])
    assert jira.properties['RHAI-1']['delivery']['root'] is None
    assert 'Slack discussion' not in json.dumps(jira.issues['RHAI-1']['fields']['description'])


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
