"""Tests for monitor_hook responsibilities."""
from __future__ import annotations

import copy
from dataclasses import replace
import pytest
from lib.gap_notifications.monitor_hook import NotificationConfig, notify_after_publish
from gap_notification_fakes import Jira, Slack
from gap_notification_fakes import state, run, LEADER_URL
from scripts import gap_pr_monitor as monitor


# Monitor

PR = "https://github.com/acme/demo/pull/42"


@pytest.fixture
def hook(tmp_path):
    root = tmp_path / "checkout"
    root.mkdir()
    jira, slack = Jira(), Slack()
    calls = []
    def jira_factory(*args):
        calls.append(args)
        assert args[:3] == ("https://stage-redhat.atlassian.net", "RHAI", "Bug")
        return jira
    def slack_factory(token, channel):
        assert channel == "C0883L8FARE"
        return slack
    defaults = dict(accepted_urls=[PR], leader_url=LEADER_URL, published_sha="b" * 40,
                    state_path=root / "state.json", repo_root=root,
                    config=NotificationConfig("write", str(tmp_path / "journal.json"), "monitor-test", "41"),
                    jira_factory=jira_factory, slack_factory=slack_factory,
                    environ={"JIRA_API_EMAIL": "fake@example.test", "JIRA_API_TOKEN": "fake", "SLACK_TOKEN": "fake"})
    def invoke(payload=None, **changes):
        return notify_after_publish(payload if payload is not None else state(expected_components=["api"], runs=[run("api", "failure")]),
                                    **(defaults | changes))
    return invoke, jira, slack, defaults, calls


# The monitor must deduplicate failure, ignore unknown outcomes and close only on explicit
# recovery.
def test_real_collector_failure_dedup_unknown_explicit_recovery(hook):
    invoke, jira, slack, opts, calls = hook
    assert invoke()["status"] == "settled"
    assert len(jira.issues) == len(slack.calls) == 1
    root = slack.calls[0]
    assert invoke()["status"] == "settled"
    assert len(slack.calls) == 1
    for payload in [state(expected_components=["api"], runs=[], pr_status="success"),
                    state(expected_components=["api"], runs=[run("api", "pending")], pr_status="success")]:
        invoke(payload)
        assert not jira.closed("RHAI-1") and len(slack.calls) == 1
    result = invoke(state(expected_components=["api"], runs=[run("api", "success", uid="recovery")]))
    assert result["status"] == "settled"
    assert jira.closed("RHAI-1") and len(slack.calls) == 2
    assert slack.calls[1][1]["thread_ts"] == jira.properties["RHAI-1"]["delivery"]["root"]["ts"]
    invoke(state(expected_components=["api"], runs=[run("api", "success", uid="recovery")]))
    invoke()  # historical failure replay cannot create a successor
    assert len(jira.issues) == 1 and len(slack.calls) == 2


# A manually closed monitored failure gets a successor root instead of reopening the old issue.
def test_manual_close_same_failure_creates_successor(hook):
    invoke, jira, slack, _, _ = hook
    invoke()
    jira.close("RHAI-1")
    invoke()
    assert len(jira.issues) == 2 and len(slack.calls) == 2
    assert slack.calls[1][1]["thread_ts"] is None


# Preview mode must avoid credentials, clients, journals and external mutations.
def test_preview_no_clients_credentials_or_journal(hook):
    invoke, jira, slack, defaults, calls = hook
    result = invoke(config=replace(defaults["config"], mode="preview"), environ=Bomb())
    assert result == {"status": "preview", "counts": {"failure": 1, "success": 0, "unknown": 0}}
    assert not calls and not jira.issues and not slack.calls
    from pathlib import Path
    assert not Path(defaults["config"].ledger).exists()


class Bomb:
    def get(self, *a):
        raise AssertionError("must not access credentials")


# Disabled, dry-run or otherwise ineligible hooks must not touch their service dependencies.
@pytest.mark.parametrize("changes,status", [({"config": None}, "disabled"),
    ({"config": NotificationConfig()}, "disabled"), ({"dry_run": True}, "monitor-dry-run"),
    ({"accepted_urls": []}, "no-accepted-decisions")])
def test_noop_guards_do_not_touch_dependencies(hook, changes, status):
    invoke, jira, slack, _, calls = hook
    assert invoke(environ=Bomb(), **changes)["status"] == status
    assert not calls and not jira.issues and not slack.calls


# Deferred children must not regain notification authority through stale persisted receipts.
def test_deferred_children_never_reuse_persisted_receipts(hook):
    invoke, jira, slack, opts, _ = hook
    observed = []
    payload = state(expected_components=["api"], runs=[run("api", "failure")])
    old = copy.deepcopy(payload["pull-requests"][0])
    old["pr-url"] = "https://github.com/acme/demo/pull/43"
    payload["pull-requests"].append(old)
    before = copy.deepcopy(payload)
    def collector(filtered, **kw):
        observed.extend(filtered["pull-requests"])
        return []
    invoke(payload, config=replace(opts["config"], mode="preview"), collector=collector)
    assert [o["pr-url"] for o in observed] == [PR]
    tracking = {}
    invoke(payload, tracking=tracking)
    assert set(tracking["rows"]) == {PR}
    from lib.gap_notifications.lookup_tools import tracking_cell
    assert tracking_cell(tracking, old["pr-url"]) == "Not checked"
    assert tracking["rows"][PR][0]["jira_key"] == "RHAI-1"
    assert len(jira.issues) == len(slack.calls) == 1
    assert payload == before


# Ambiguous transport outcomes must remain visibly held rather than appear settled or resend.
def test_transport_ambiguity_visible_not_reported_as_success(hook):
    invoke, jira, slack, _, _ = hook
    slack.fail = True
    assert invoke()["status"] == "held"
    assert len(slack.calls) == 1
    assert invoke()["status"] == "held"
    assert len(slack.calls) == 1


# Confirmed delivery must remain settled while cosmetic warnings stay visible and sanitized.
def test_confirmed_core_retains_cosmetic_warning_visibility(hook, monkeypatch, capsys):
    invoke, jira, slack, _, _ = hook
    def broken_projection(key, **fields):
        # First failure creates the issue atomically; this call is only the
        # post-delivery cosmetic discussion projection, not a core update.
        assert "add_labels" not in fields
        raise RuntimeError("SECRET cosmetic failure")
    monkeypatch.setattr(jira, "update_issue", broken_projection)
    result = invoke()
    assert result["status"] == "settled"
    assert result["delivery_state_counts"]["confirmed"] == 1
    assert result["presentation_warning_count"] == 1
    assert result["delivery_task_count"] == 1
    assert len(jira.issues) == len(slack.calls) == 1
    assert jira.properties["RHAI-1"]["delivery"]["root"]
    assert jira.properties["RHAI-1"]["delivery"]["intent"] is None
    assert "SECRET" not in capsys.readouterr().err
    # Cosmetic failure must not trigger another core send on replay.
    replay = invoke()
    assert replay["status"] == "settled" and replay["presentation_warning_count"] == 1
    assert len(slack.calls) == 1


# Summary size must remain bounded even when the historical outbox is large or contains unknown
# states.
@pytest.mark.parametrize("historical_count", [100, 10000])
def test_large_historical_outbox_summary_is_fixed_shape(hook, capsys, historical_count):
    import json
    invoke, _, _, _, _ = hook
    outbox = [{"state": "confirmed", "jira_key": "RHAI-999", "presentation_warning": "SECRET warning", "evidence": "X" * 1000}
              for _ in range(historical_count)]
    outbox += [{"state": "superseded"}, {"state": "pending"}, {"state": "SECRET unknown state"}]
    class HistoricalDispatcher:
        def __init__(self, *args, **kwargs):
            pass
        def run(self, observations):
            return {"outbox": outbox}
    tracking = {}
    result = invoke(dispatcher_factory=HistoricalDispatcher, tracking=tracking)
    assert set(tracking["rows"]) == {PR}
    assert tracking["rows"][PR][0]["status"] == "not_found"
    assert result["status"] == "held"
    assert result["delivery_task_count"] == historical_count + 3
    assert result["presentation_warning_count"] == historical_count
    assert result["delivery_state_counts"] == {
        "confirmed": historical_count, "superseded": 1, "pending": 1,
        "inflight": 0, "reconciliation_required": 0, "rejected": 0, "other": 1}
    assert "delivery_states" not in result
    output = capsys.readouterr().err
    assert len(json.dumps(result)) < 500 and len(output) < 550
    assert "SECRET" not in output and "evidence" not in output
    # Summarizing must not mutate the dispatcher's audit records.
    assert outbox[-1]["state"] == "SECRET unknown state"


# Notification failures must return a sanitized nonblocking result rather than disrupt the
# monitor.
def test_failure_is_sanitized_nonblocking(hook, capsys):
    invoke, _, _, _, _ = hook
    def broken(*a, **kw):
        raise RuntimeError("SECRET transport payload")
    assert invoke(collector=broken) == {"status": "error", "error_type": "RuntimeError"}
    assert "SECRET" not in capsys.readouterr().err


# Recovery without an explicit done transition must remain held and leave the issue open.
def test_without_transition_recovery_remains_visible_hold(hook):
    invoke, jira, _, opts, _ = hook
    invoke()
    result = invoke(state(expected_components=["api"], runs=[run("api", "success", uid="recovery")]),
                    config=replace(opts["config"], transition_id=None))
    assert result["status"] == "held" and not jira.closed("RHAI-1")


# Missing publication identity must prevent notification delivery.
@pytest.mark.parametrize("published_sha,leader", [(None, LEADER_URL), ("b" * 40, None), ("b" * 40, "https://evil.test/o/r/pull/1")])
def test_incomplete_publication_identity_does_not_deliver(hook, published_sha, leader):
    invoke, jira, slack, _, _ = hook
    assert invoke(published_sha=published_sha, leader_url=leader)["status"] == "error"
    assert not jira.issues and not slack.calls


# Publish first, notify second, summarize last; notification problems must not rewrite gate
# results.
def test_coordinator_only_invokes_after_publish_and_preserves_gate_result(tmp_path, monkeypatch):
    from lib.gap_notifications.monitor_hook import NotificationConfig
    path = tmp_path / "state.json"
    payload = state(expected_components=["api"], runs=[run("api", "failure")])
    monitor.save_state(path, payload)
    child = monitor.MonitorResult(path, [], [], [], [], False, overall_status="pending", notification_urls=(PR,))
    monkeypatch.setattr(monitor, "run_monitor", lambda *a, **kw: child)
    order = []
    summaries = []
    def summary(**kw):
        order.append("summary")
        summaries.append(copy.deepcopy(kw))
    monkeypatch.setattr(monitor, "_post_leader_failure_summary", summary)
    def publish(*a, **kw):
        order.append("publish")
        return monitor.PublishResult("b" * 40, True)
    monkeypatch.setattr(monitor, "publish_state", publish)
    def notify(*a, **kw):
        order.append("notify")
        assert kw["accepted_urls"] == (PR,) and kw["published_sha"] == "b" * 40
        kw["tracking"].update(status="checked", rows={PR: [{"component": "api", "status": "slack_pending", "jira_key": "RHAI-1"}]})
        return {"status": "held"}
    monkeypatch.setattr("lib.gap_notifications.monitor_hook.notify_after_publish", notify)
    opts = dict(repo_root=tmp_path, leader_pr_url=LEADER_URL, head_ref="leader")
    # No publication => no hook, even if configured.
    assert monitor.run_monitor_publish_and_signal(path, **opts, publish=False, notification_config=NotificationConfig("preview")) is child
    assert not order
    # Disabled => no hook (ordinary monitor result unchanged).
    result = monitor.run_monitor_publish_and_signal(path, **opts, publish=True)
    assert order == ["publish", "summary"] and result.notification_result is None
    assert summaries[-1]["tracking"] == {"status": "not_checked"}
    order.clear()
    result = monitor.run_monitor_publish_and_signal(path, **opts, publish=True, notification_config=NotificationConfig("preview"))
    assert order == ["publish", "notify", "summary"]
    assert summaries[-1]["tracking"]["rows"][PR][0]["jira_key"] == "RHAI-1"
    assert result.overall_status == child.overall_status and result.notification_result == {"status": "held"}
    # Even unexpected helper failures remain isolated.
    def broken(*a, **kw):
        raise RuntimeError("SECRET")
    monkeypatch.setattr("lib.gap_notifications.monitor_hook.notify_after_publish", broken)
    result = monitor.run_monitor_publish_and_signal(path, **opts, publish=True, notification_config=NotificationConfig("preview"))
    assert result.overall_status == child.overall_status and result.notification_result["status"] == "error"


# Tracking is a separate projection whose failures cannot resend notifications or hide valid
# receipts.
def test_tracking_separate_reused_pending_and_failure_isolation(hook, monkeypatch, capsys):
    invoke, jira, slack, opts, _ = hook
    tracking = {}
    result = invoke(tracking=tracking)
    assert result["status"] == "settled"
    assert tracking["rows"][PR][0]["jira_key"] == "RHAI-1"
    assert tracking["rows"][PR][0]["slack_url"]
    assert "tracking" not in result and "slack_url" not in capsys.readouterr().err
    replay = {}
    invoke(tracking=replay)
    assert replay == tracking and len(slack.calls) == 1
    slack.fail = True
    payload = state(expected_components=["api", "gpu"], runs=[run("api", "failure"), run("gpu", "failure")])
    held = invoke(payload, tracking=tracking)
    assert held["status"] == "held"
    assert tracking["rows"][PR][0]["slack_url"]
    pending = tracking["rows"][PR][1]
    assert pending["jira_key"] == "RHAI-2" and pending["status"] == "held"
    assert "slack_url" not in pending
    sends = len(slack.calls)
    def fail(*a, **kw):
        raise RuntimeError("SECRET")
    monkeypatch.setattr("lib.gap_notifications.lookup_tools.project_tracking", fail)
    result = invoke(payload, tracking=tracking)
    assert result["status"] == "held" and tracking == {"status": "unavailable"}
    assert len(slack.calls) == sends
    assert "SECRET" not in capsys.readouterr().err


# Inactive tracking must remain unchecked without creating clients or a journal.
@pytest.mark.parametrize("mode,dry_run", [("disabled", False), ("preview", False), ("write", True)])
def test_tracking_noop_still_no_clients_credentials_ledger(hook, mode, dry_run):
    invoke, jira, slack, opts, calls = hook
    tracking = {"status": "checked", "rows": {"OLD": []}}
    invoke(config=replace(opts["config"], mode=mode), dry_run=dry_run, environ=Bomb(), tracking=tracking)
    assert tracking == {"status": "not_checked"} and not calls
    from pathlib import Path
    assert not Path(opts["config"].ledger).exists()


# Notifications must remain disabled unless explicitly enabled by configuration.
def test_parser_default_disabled():
    assert monitor.build_parser().parse_args([]).notification_mode == "disabled"


# Production config must drive collection, factories, registry scope and tracking together.
def test_production_hook_passes_explicit_target_without_changing_delivery_rules(hook):
    from lib.gap_notifications.notify_tools import routing_target as jira_target
    invoke, jira, slack, opts, calls = hook
    target = jira_target('production')
    jira.base_url = target.url
    def factory(*args, **kwargs):
        calls.append((args, kwargs))
        assert args[:3] == (target.url, 'RHAI', 'Bug')
        assert kwargs == target.jira.options
        return jira
    config = replace(opts['config'], jira_environment='production', transition_id=None)
    tracking = {}
    changes = dict(config=config, jira_factory=factory, tracking=tracking)
    first = invoke(**changes)
    assert first['status'] == 'settled' and first['presentation_warning_count'] == 0
    assert jira.properties['RHAI-1']['scope'] == target.scope
    assert tracking['jira_origin'] == target.url
    replay = invoke(**changes)
    assert replay['status'] == 'settled' and replay['presentation_warning_count'] == 0 and len(slack.calls) == 1
    recovery = state(expected_components=['api'], runs=[run('api', 'success', uid='passing')])
    assert invoke(recovery, **changes)['status'] == 'held' and not jira.closed('RHAI-1')
    changes['config'] = replace(config, transition_id='999', transition_fields={'resolution': {'id': '999'}})
    assert invoke(recovery, **changes)['status'] == 'settled' and jira.closed('RHAI-1')
    assert len(slack.calls) == 2


# Production preview must collect the right scope without reading credentials or a journal.
def test_production_preview_has_no_clients_credentials_or_journal_access(hook):
    from pathlib import Path
    from lib.gap_notifications.analyze_leader_state import collect_build_observations
    from lib.gap_notifications.notify_tools import routing_target as jira_target
    invoke, _, _, opts, calls = hook
    seen = []
    def collector(payload, **kwargs):
        seen.append(kwargs['environment'])
        return collect_build_observations(payload, **kwargs)
    journal = Path(opts['config'].ledger)
    journal.write_text('not a valid journal')
    config = replace(opts['config'], mode='preview', jira_environment='production')
    result = invoke(config=config, environ=Bomb(), collector=collector)
    assert result['status'] == 'preview' and seen == [jira_target('production').scope]
    assert not calls and journal.read_text() == 'not a valid journal'


# Invalid selectors must hold notification work before credentials or factories are touched.
def test_invalid_hook_environment_fails_before_dependencies(hook):
    invoke, _, _, opts, calls = hook
    result = invoke(config=replace(opts['config'], jira_environment='prod'), environ=Bomb())
    assert result == {'status': 'error', 'error_type': 'ValueError'} and not calls


# The workflow-facing environment variable must reach config, but must not enable notifications.
@pytest.mark.parametrize('selector,mode,expected', [(None, 'preview', 'stage'),
    ('https://stage-redhat.atlassian.net', 'preview', 'stage'),
    ('https://redhat.atlassian.net/', 'preview', 'production'),
    ('https://redhat.atlassian.net', 'disabled', None), ('typo', 'preview', None),
    ('', 'preview', None), ('legacy', 'preview', None), ('legacy', 'disabled', None)])
def test_monitor_cli_reads_jira_url_only_for_enabled_notifications(tmp_path, monkeypatch, selector, mode, expected):
    monkeypatch.delenv('GAP_JIRA_ENVIRONMENT', raising=False)
    if selector == 'legacy':
        monkeypatch.setenv('GAP_JIRA_ENVIRONMENT', 'production')
        selector = 'https://redhat.atlassian.net'
    if selector is None: monkeypatch.delenv('GAP_JIRA_URL', raising=False)
    else: monkeypatch.setenv('GAP_JIRA_URL', selector)
    if mode == 'disabled':
        class NoSelectorReads(dict):
            def get(self, key, *default):
                assert key not in {'GAP_JIRA_URL', 'GAP_JIRA_ENVIRONMENT'}
                return super().get(key, *default)
            def __contains__(self, key):
                assert key not in {'GAP_JIRA_URL', 'GAP_JIRA_ENVIRONMENT'}
                return super().__contains__(key)
        monkeypatch.setattr(monitor.os, 'environ', NoSelectorReads(monitor.os.environ))
    path = tmp_path / 'state.json'
    captured = []
    monkeypatch.setattr(monitor, 'resolve_inputs_for_cli', lambda **kw: (str(path), None, [], None))
    monkeypatch.setattr(monitor, 'resolve_state_file', lambda **kw: path)
    monkeypatch.setattr(monitor, 'append_github_output', lambda *a: None)
    monkeypatch.setattr(monitor, 'should_run_schedule_mode', lambda **kw: False)
    def coordinator(*args, **kwargs):
        captured.append(kwargs.get('notification_config'))
        return monitor.MonitorResult(path, [], [], [], [], True)
    monkeypatch.setattr(monitor, 'run_monitor_publish_and_signal', coordinator)
    assert monitor.main(['--repo-root', str(tmp_path), '--notification-mode', mode]) == 0
    assert len(captured) == 1
    assert (captured[0].jira_environment if captured[0] else None) == expected


def test_default_composite_hook_conflict_recovery_never_closes_build(hook):
    invoke, jira, slack, opts, _ = hook
    payload = state(expected_components=['api'], runs=[run('api', 'failure')])
    child = payload['pull-requests'][0]
    child['head-sha'] = 'a' * 40
    receipt = dict(schema=1, repository='acme/demo', pr=42,
                   **{'head-sha': 'a' * 40, 'base-sha': 'b' * 40,
                      'source-branch': 'temporary-sync-head', 'target-branch': 'main',
                      'pr-state': 'OPEN', 'fresh': True,
                      'conflict': {'status': 'failure', 'mergeable': 'CONFLICTING', 'merge-state-status': 'DIRTY'},
                      'feasibility': {'status': 'unknown', 'query-skipped': True}})
    child['merge-observations'] = receipt
    assert invoke(payload)['status'] == 'settled'
    assert len(jira.issues) == len(slack.calls) == 2
    invoke(payload)
    assert len(slack.calls) == 2
    # New base pair with explicit success recovers conflict, not the still failing build.
    receipt['base-sha'] = 'c' * 40
    receipt['conflict'] = {'status': 'success', 'mergeable': 'MERGEABLE', 'merge-state-status': 'CLEAN'}
    invoke(payload)
    assert not jira.closed('RHAI-1') and jira.closed('RHAI-2')
    assert slack.calls[-1][1]['thread_ts'] == jira.properties['RHAI-2']['delivery']['root']['ts']
    receipt['base-sha'] = 'b' * 40
    receipt['conflict'] = {'status': 'failure', 'mergeable': 'CONFLICTING', 'merge-state-status': 'DIRTY'}
    invoke(payload)
    assert len(jira.issues) == 2  # old pre-recovery replay is not recurrence
