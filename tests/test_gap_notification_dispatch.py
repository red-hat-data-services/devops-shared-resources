import copy

import pytest

from lib.gap_notification_dispatch import CHANNEL, Dispatcher, Ledger, ingest, incident_label
from lib.gap_notification_incidents import incident_key


def observation(status="failure", oid="run-1", **changes):
    value = dict(environment="stage-redhat.atlassian.net/RHAI/C0883L8FARE", repository="acme/repo",
                 source_branch="main", destination_branch="stable", category="build", component="cpu",
                 status=status, observation_id=oid, evidence={"test_only": True, "synthetic": True})
    value.update(changes)
    return value


class Jira:
    def __init__(self):
        self.issues, self.properties, self.calls = {}, {}, []
        self.fail_create = self.fail_readback = self.fail_receipt = False

    def find_by_label(self, label):
        return [copy.deepcopy(i) for i in self.issues.values() if label in i["fields"]["labels"]]

    def get_issue(self, key):
        return copy.deepcopy(self.issues[key])

    def get_notification_state(self, key):
        return copy.deepcopy(self.properties.get(key))

    def put_notification_state(self, key, value):
        if self.fail_receipt and value["delivery"]["root"]:
            raise TimeoutError("receipt persistence unavailable")
        self.properties[key] = copy.deepcopy(value)
        return copy.deepcopy(value)

    def create_bug(self, summary, description, labels, *, notification_state):
        self.calls.append("create")
        if self.fail_create:
            raise TimeoutError("SECRET")
        key = "RHAI-" + str(len(self.issues) + 1)
        self.issues[key] = {"key": key, "fields": {"summary": summary, "project": {"key": "RHAI"},
            "issuetype": {"name": "Bug", "id": "10016"}, "description": description,
            "labels": labels, "status": {"statusCategory": {"key": "new"}}}}
        self.properties[key] = copy.deepcopy(notification_state)
        if self.fail_readback:
            error = TimeoutError("SECRET")
            error.key = key
            raise error
        return {"key": key, "url": "https://stage-redhat.atlassian.net/browse/" + key}

    def update_issue(self, key, **fields):
        self.calls.append("update")
        assert not self.closed(key)
        self.issues[key]["fields"].update(fields)
        return self.get_issue(key)

    def closed(self, key):
        return self.issues[key]["fields"]["status"]["statusCategory"]["key"] == "done"

    def close(self, key):
        self.issues[key]["fields"]["status"]["statusCategory"]["key"] = "done"

    def transition_to_done(self, key, transition_id, *, fields=None):
        self.calls.append(("close", transition_id, fields))
        self.close(key)
        return self.get_issue(key)


class Slack:
    def __init__(self):
        self.calls, self.fail = [], False

    def send(self, text, **kwargs):
        self.calls.append((text, kwargs))
        if self.fail:
            raise TimeoutError("SECRET")
        return {"channel": CHANNEL, "ts": f"123.{len(self.calls):06}"}


@pytest.fixture
def setup(tmp_path):
    jira, slack = Jira(), Slack()
    def runner(name="ledger", trial="trial"):
        return Dispatcher(Ledger(tmp_path / (name + ".json")), jira, slack,
            trial_id=trial, transition_id="41", transition_fields={"resolution": {"id": "10000"}})
    return runner, jira, slack


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
    runner("b").run([observation("success", "ok")])
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
    from lib.gap_notification_slack import SlackRateLimitError
    import lib.gap_notification_dispatch as module
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


def test_multiple_open_or_malformed_property_holds(setup):
    runner, jira, slack = setup
    runner().run([observation()])
    jira.issues["RHAI-2"] = copy.deepcopy(jira.issues["RHAI-1"])
    jira.issues["RHAI-2"]["key"] = "RHAI-2"
    jira.properties["RHAI-2"] = copy.deepcopy(jira.properties["RHAI-1"])
    result = runner("fresh").run([observation(oid="new")])
    assert result["outbox"][0]["state"] == "reconciliation_required"
    assert len(slack.calls) == 1
    del jira.issues["RHAI-2"]
    jira.properties["RHAI-1"]["scope"] = "wrong"
    result = runner("fresh2").run([observation(oid="new")])
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


def test_property_capacity_or_durable_jira_intent_holds_before_mutation(setup):
    runner, jira, slack = setup
    runner().run([observation()])
    state = jira.properties["RHAI-1"]
    state["jira_intent"] = {"event_id": "a" * 64, "outcome": "success"}
    result = runner("fresh").run([observation(oid="next")])
    assert result["outbox"][0]["state"] == "reconciliation_required"
    assert jira.calls == ["create"] and len(slack.calls) == 1
    state["jira_intent"] = None
    state["seen_event_ids"].update({f"{i:064x}": "failure" for i in range(400)})
    result = runner("capacity").run([observation(oid="next")])
    assert result["outbox"][0]["state"] != "confirmed"
    assert jira.calls == ["create"] and len(slack.calls) == 1


def test_query_failure_does_not_create_or_post(setup):
    runner, jira, slack = setup
    def fail(label):
        raise RuntimeError("query unavailable")
    jira.find_by_label = fail
    result = runner().run([observation()])
    assert result["outbox"][0]["state"] == "pending"
    assert jira.calls == slack.calls == []
