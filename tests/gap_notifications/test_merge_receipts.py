"""Offline producer and collector contracts for independent merge evidence."""
import copy
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from gap_notification_fakes import state, LEADER_URL
from lib.gap_check_selection import FeasibilityRun, WorkflowJob
from lib.github_check_runs import GateResult, CheckFinding, CheckRun
from lib.gap_notifications.merge_receipts import make_receipt, bind_receipt
from lib.gap_notifications.analyze_merge_state import collect_merge_observations, collect_observations
from lib.gap_notifications.analyze_leader_state import collect_build_observations, ObservationCollectionError
from lib.state_file import StatePullRequest

NOW = datetime(2026, 11, 6, tzinfo=timezone.utc)
SHA, BASE = "a" * 40, "b" * 40


def pr(**changes):
    values = dict(owner="acme", repo="demo", number=42, head_sha=SHA, base_sha=BASE,
                  state="OPEN", mergeable="MERGEABLE", merge_state_status="CLEAN",
                  source_branch="feature/x", base_ref="main")
    return SimpleNamespace(**(values | changes))


def receipt(status="success", **changes):
    p = pr(**changes)
    run = FeasibilityRun(True, "completed", "success", SHA,
                         (WorkflowJob("setup", "completed", status, None, None),),
                         run_id=123, attempt=1, url="https://github.com/acme/demo/actions/runs/123")
    # Match the actual mandatory selection, including the setup selector.
    from lib.gap_check_selection import FEASIBILITY_SETUP_JOB
    check = CheckRun(1, FEASIBILITY_SETUP_JOB, "completed", status, SHA, None, None, None,
                     "github-actions", 1, None)
    run = FeasibilityRun(True, "completed", "success", SHA,
                         (WorkflowJob(FEASIBILITY_SETUP_JOB, "completed", status, None, None),),
                         run_id=123, attempt=1, url=run.url, source_head_sha=SHA)
    outcome = "pass" if status == "success" else "fail"
    gate = GateResult(outcome, (CheckFinding(FEASIBILITY_SETUP_JOB, outcome, "display",
                                            revision=SHA, status="completed", conclusion=status),), False, None)
    return bind_receipt(make_receipt(p, now=NOW, feasibility=run, gate=gate, checks=[check]), p)


def payload(r):
    s = state(expected_components=[], runs=[])
    s["pull-requests"][0].update({"head-sha": SHA, "merge-observations": r})
    return s


def collect(r):
    return collect_merge_observations(payload(r), environment="stage-redhat.atlassian.net/RHAI/C0883L8FARE",
                                      leader_url=LEADER_URL, state_url="https://github.com/acme/demo/blob/" + SHA + "/state.json")


@pytest.mark.parametrize("conclusion", ["failure", "skipped", "cancelled", "neutral", "timed_out"])
def test_completed_nonpassing_feasibility_is_failure(conclusion):
    obs = collect(receipt(conclusion))
    assert [o["status"] for o in obs] == ["success", "failure"]


def test_independent_success_and_stable_ids_ignore_clock_and_links():
    r = receipt()
    first = collect(r)
    r["observed-at"] = "later"
    r["feasibility"]["url"] = "https://github.com/acme/demo/actions/runs/other"
    assert [o["observation_id"] for o in first] == [o["observation_id"] for o in collect(r)]
    assert [o["status"] for o in first] == ["success", "success"]
    r["feasibility"]["attempt"] = 2
    assert collect(r)[1]["observation_id"] != first[1]["observation_id"]
    assert collect(r)[0]["observation_id"] == first[0]["observation_id"]


@pytest.mark.parametrize("field,value", [("head_sha", "c" * 40), ("base_sha", "c" * 40), ("base_sha", None)])
def test_freshness_never_recovers_moved_or_missing_revisions(field, value):
    r = bind_receipt(receipt(), pr(**{field: value}))
    assert all(o["status"] == "unknown" for o in collect(r))


def test_short_circuit_and_contradictory_conflicts_are_unknown():
    p = pr(mergeable="CONFLICTING", merge_state_status="DIRTY")
    r = bind_receipt(make_receipt(p, now=NOW), p)
    assert [o["status"] for o in collect(r)] == ["failure", "unknown"]
    flipped = bind_receipt(make_receipt(pr(), now=NOW), pr(), r)
    assert collect(flipped)[0]["status"] == "unknown"
    # Unknown polls must not forget the earlier definitive conflict.
    again = bind_receipt(make_receipt(pr(), now=NOW), pr(), flipped)
    assert collect(again)[0]["status"] == "unknown"
    stale = bind_receipt(make_receipt(pr(), now=NOW), pr(base_sha="d" * 40), r)
    retry = bind_receipt(make_receipt(pr(), now=NOW), pr(), stale)
    assert collect(retry)[0]["status"] == "unknown"
    contradictory = pr(mergeable="MERGEABLE", merge_state_status="DIRTY")
    assert collect(bind_receipt(make_receipt(contradictory, now=NOW), contradictory))[0]["status"] == "unknown"


@pytest.mark.parametrize("change", ["query-error", "query-skipped", "missing-run", "missing-check", "wrong-revision", "job-skipped", "missing-job", "missing-run-head"])
def test_incomplete_feasibility_never_recovers(change):
    r = receipt()
    f = r["feasibility"]
    if change.startswith("query-"):
        f[change] = True
    elif change == "missing-run":
        f["run-id"] = None
    elif change == "missing-check":
        f["checks"] = []
    elif change == "job-skipped":
        f["jobs"][0]["conclusion"] = "skipped"
    elif change == "missing-job":
        f["jobs"] = []
    elif change == "missing-run-head":
        f["run-head-sha"] = None
    else:
        f["head-sha"] = "c" * 40
    assert collect(r)[1]["status"] == "unknown"


def test_state_roundtrip_and_build_api_unchanged():
    s = payload(receipt())
    child = s["pull-requests"][0]
    restored = StatePullRequest.from_mapping(child, index=0).to_dict()
    assert restored["merge-observations"] == child["merge-observations"]
    kwargs = dict(environment="stage-redhat.atlassian.net/RHAI/C0883L8FARE", leader_url=LEADER_URL, state_url="snapshot")
    assert [o for o in collect_observations(s, **kwargs) if o["category"] == "build"] == collect_build_observations(s, **kwargs)
    del child["merge-observations"]
    assert collect_merge_observations(s, **kwargs) == []


@pytest.mark.parametrize("environment", ["stage-redhat.atlassian.net/RHAI/C0883L8FARE", "redhat.atlassian.net/RHAI/C0883L8FARE"])
def test_lifecycle_category_isolation_replay_and_changed_pair_recovery(tmp_path, environment):
    from gap_notification_fakes import Jira, Slack
    from lib.gap_notifications.dispatch_incidents import Dispatcher, Ledger
    jira, slack = Jira(), Slack()
    options = {} if environment.startswith("stage-") else {"jira_environment": "production"}
    # Fake receipts must match the selected environment.
    if options:
        original_create = jira.create_bug
        def create(*args, **kwargs):
            result = original_create(*args, **kwargs)
            result["url"] = result["url"].replace("stage-redhat", "redhat")
            return result
        jira.create_bug = create
    dispatcher = Dispatcher(Ledger(str(tmp_path / "journal.json")), jira, slack,
                            trial_id="merge-test", transition_id="41", **options)
    def send(r):
        observations = collect(r)
        for o in observations:
            o["environment"] = environment
        dispatcher.run(observations)
    failed = receipt("failure", mergeable="CONFLICTING", merge_state_status="DIRTY")
    send(failed)
    assert len(jira.issues) == len(slack.calls) == 2
    send(failed)
    assert len(slack.calls) == 2
    recovered = receipt("failure", base_sha="c" * 40)
    send(recovered)
    # Conflict closes independently while feasibility remains failed.
    assert jira.closed("RHAI-1") and not jira.closed("RHAI-2")
    send(failed)
    assert len(jira.issues) == 2
    new_failure = receipt("failure", base_sha="d" * 40, mergeable="CONFLICTING", merge_state_status="DIRTY")
    send(new_failure)
    assert len(jira.issues) == 3


def test_missing_exact_stream_refused():
    s = payload(receipt())
    s["creator-outcomes"][0]["pr-url"] = "https://github.com/acme/demo/pull/99"
    with pytest.raises(ObservationCollectionError):
        collect_merge_observations(s, environment="stage", leader_url=LEADER_URL, state_url="snapshot")


def test_temporary_pr_head_is_not_logical_promotion_source():
    r = receipt(source_branch="sync-main-to-main-42")
    assert [o["status"] for o in collect(r)] == ["success", "success"]
    assert collect(r)[0]["source_branch"] == "feature/x"


def test_fingerprint_order_and_safe_links():
    r = receipt("failure")
    f = r["feasibility"]
    f["checks"].append(dict(f["checks"][0], id=2, name="extra"))
    f["findings"].append(dict(f["findings"][0], key="extra"))
    before = collect(r)[1]["observation_id"]
    f["checks"].reverse()
    f["findings"].reverse()
    f["url"] = "https://evil.example/actions/runs/123"
    after = collect(r)[1]
    assert after["observation_id"] == before
    assert after["evidence"]["workflow_url"] == "https://github.com/acme/demo/actions/runs/123"
    assert after["evidence"]["commit_url"] == "https://github.com/acme/demo/commit/" + SHA


@pytest.mark.parametrize("previous", [42, "invalid", {"last-conflict": 42}, {"conflict": "bad"}])
def test_malformed_optional_history_does_not_break_binding(previous):
    r = bind_receipt(receipt(), pr(), previous)
    assert collect(r)[0]["status"] == "success"


def test_malformed_optional_receipts_do_not_break_ordinary_state():
    child = payload(receipt())["pull-requests"][0]
    child["merge-observations"] = {"schema": 99}
    parsed = StatePullRequest.from_mapping(child, index=0)
    assert parsed.merge_observations is None
    assert parsed.pr_status == child["pr-status"]


def test_producer_optional_error_drops_evidence_not_gate():
    p = pr()
    assert make_receipt(p, now=NOW, feasibility=object(), gate=object()) is None
