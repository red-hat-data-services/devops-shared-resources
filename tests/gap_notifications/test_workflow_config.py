"""Offline contracts for shared workflow policy and destination isolation."""
import copy
import importlib.util
from pathlib import Path

import pytest

from gap_notification_fakes import Jira, Slack, observation
from lib.gap_notifications.dispatch_incidents import Dispatcher, Ledger, bind_trial, discussion_url, incident_key, validate_resolution
from lib.gap_notifications.lookup_tools import project_tracking, tracking_cell
from lib.gap_notifications.notify_tools import routing_target

spec = importlib.util.spec_from_file_location("workflow_config", Path(__file__).resolve().parents[2] / "scripts/gap_monitor_workflow.py")
workflow = importlib.util.module_from_spec(spec)
spec.loader.exec_module(workflow)


# Every configured destination must retain lifecycle, dedup, and exact permalink validation.
@pytest.mark.parametrize("channel", ["C012OTHER", "C099ELSE"])
@pytest.mark.parametrize("environment", ["stage", "production"])
def test_configured_channel_lifecycle(tmp_path, channel, environment):
    target = routing_target(environment, channel)
    jira = Jira()
    jira.base_url = target.url
    class Destination(Slack):
        def __init__(self):
            super().__init__()
            self.channel = channel
        def send(self, text, **kwargs):
            self.calls.append((text, kwargs))
            return {"channel": channel, "ts": f"123.{len(self.calls):06}"}
    slack = Destination()
    runner = Dispatcher(Ledger(tmp_path / "journal.json"), jira, slack, trial_id="trial", transition_id="41", **target.options)
    obs = observation(environment=target.scope, evidence={"pr_url": "https://github.com/acme/repo/pull/1"})
    runner.run([obs])
    runner.run([obs])
    assert len(slack.calls) == 1
    projection = project_tracking([obs], jira, trial_id="trial", **target.options)
    assert f"/archives/{channel}/" in tracking_cell(projection, obs['evidence']['pr_url'])
    assert not runner.run([observation('success', 'recovery', environment=target.scope)])['outbox'][-1].get('hold')
    assert jira.closed('RHAI-1') and len(slack.calls) == 2
    assert 'thread_ts' in slack.calls[-1][1]
    assert incident_key(obs) != incident_key(dict(obs, environment=routing_target(environment, "C0883L8FARE").scope))
    with pytest.raises(ValueError):
        bind_trial(runner.data, "trial", jira_environment=environment, channel="COTHER")
    with pytest.raises(ValueError):
        discussion_url({'channel': 'COTHER', 'ts': '123.456'}, channel=channel)
    with pytest.raises(ValueError):
        validate_resolution({'task_id': 'task', 'receipt': {'system': 'slack', 'key': 'RHAI-1', 'reason': 'verified', 'channel': 'COTHER', 'ts': '123.456'}}, channel=channel)
    wrong = copy.deepcopy(projection)
    wrong['channel'] = 'COTHER'
    assert tracking_cell(wrong, obs['evidence']['pr_url']) == 'Unavailable'


# Missing/blank configuration fails closed without touching secrets, clients or journals.
@pytest.mark.parametrize("channel", [None, "", " ", "\n"])
def test_missing_channel_disabled(channel):
    env = {'GAP_NOTIFICATION_MODE': 'write'}
    if channel is not None:
        env['GAP_SLACK_CHANNEL'] = channel
    assert workflow.monitor_arguments(env) == (["--publish-state"], "invalid-configuration")
    assert workflow.monitor_arguments({**env, 'MONITOR_DRY_RUN': 'true'}) == (["--dry-run"], "monitor-dry-run")
    assert workflow.monitor_arguments({**env, 'GAP_NOTIFICATION_MODE': 'disabled'}) == (["--publish-state"], "disabled")


# Environment-specific defaults belong to shared policy, with production recovery held.
@pytest.mark.parametrize("environment", ["stage", "production"])
def test_monitor_configuration(environment):
    env = {'GAP_NOTIFICATION_MODE': 'write', 'GAP_SLACK_CHANNEL': 'C012OTHER',
           'GAP_JIRA_URL': routing_target(environment, 'C012OTHER').url,
           'RUNNER_TEMP': '/tmp/runner', 'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '2'}
    args, reason = workflow.monitor_arguments(env)
    assert reason == 'configured-write'
    assert ('--notification-transition-id' in args) == (environment == 'stage')
    assert '/123-2/journal.json' in args[args.index('--notification-ledger') + 1]


# Receipt policy preserves event, attempt, author, subject and path boundaries directly.
@pytest.mark.parametrize("change,expected", [({}, True), ({'MONITOR_RUN_ATTEMPT': '2'}, False),
    ({'MONITOR_EVENT': 'workflow_dispatch'}, False), ({'MONITOR_ACTION': 'opened'}, False)])
def test_receipt_events(change, expected):
    env = {'MONITOR_EVENT': 'pull_request', 'MONITOR_ACTION': 'synchronize', 'MONITOR_RUN_ATTEMPT': '1', **change}
    def git(*args):
        return {'%s': b'Update GAP state from PR monitor (RHOAIENG-97052)\n',
                '%ae': b'github-actions[bot]@users.noreply.github.com\n'}.get(args[-1].removeprefix('--format='), b'GAP Leaders/day/state.json\0')
    assert workflow.receipt_only(env, git=git) is expected


# Unexpected paths, no changed files, wrong author and wrong subject must all remain eligible.
@pytest.mark.parametrize("subject,author,paths", [('wrong', 'github-actions[bot]@users.noreply.github.com', 'GAP Leaders/day/state.json\0'),
    ('Update GAP state from PR monitor (RHOAIENG-97052)', 'person@example.test', 'GAP Leaders/day/state.json\0'),
    ('Update GAP state from PR monitor (RHOAIENG-97052)', 'github-actions[bot]@users.noreply.github.com', ''),
    ('Update GAP state from PR monitor (RHOAIENG-97052)', 'github-actions[bot]@users.noreply.github.com', 'README.md\0')])
def test_receipt_boundaries(subject, author, paths):
    def git(*args):
        return (subject if args[-1] == '--format=%s' else author if args[-1] == '--format=%ae' else paths).encode()
    assert not workflow.receipt_only({'MONITOR_EVENT': 'pull_request', 'MONITOR_ACTION': 'synchronize'}, git=git)


# Injected adapters cannot silently redirect; mismatched acknowledgements leave durable uncertainty.
@pytest.mark.parametrize("wrong_adapter", [True, False])
def test_wrong_adapter_or_ack(tmp_path, wrong_adapter):
    channel = "C012OTHER"
    target = routing_target("stage", channel)
    jira = Jira()
    class WrongSlack(Slack):
        channel = "COTHER" if wrong_adapter else "C012OTHER"
        def send(self, text, **kwargs):
            self.calls.append((text, kwargs))
            return {"channel": "COTHER", "ts": "123.456"}
    slack = WrongSlack()
    runner = Dispatcher(Ledger(tmp_path / "journal"), jira, slack, **target.options)
    obs = observation(environment=target.scope)
    if wrong_adapter:
        with pytest.raises(ValueError, match="adapter channel"):
            runner.run([obs])
        assert not jira.calls and not slack.calls
    else:
        result = runner.run([obs])
        assert result["outbox"][0]["state"] == "reconciliation_required"
        assert jira.properties["RHAI-1"]["delivery"]["intent"]
        runner.run([obs])
        assert len(slack.calls) == 1


# Old-channel remote roots cannot be used even if every other property field matches.
def test_wrong_root_held(tmp_path):
    channel = "C0883L8FARE"
    jira, slack = Jira(), Slack()
    runner = Dispatcher(Ledger(tmp_path / "journal"), jira, slack, channel=channel)
    runner.run([observation()])
    jira.properties["RHAI-1"]["delivery"]["root"]["channel"] = "COTHER"
    result = runner.run([observation(oid="run-2")])
    assert result["outbox"][-1]["state"] in {"pending", "reconciliation_required"}
    assert len(slack.calls) == 1


# Historical destination preserves the exact namespace and accepts unbound staging journals only there.
def test_legacy_scope_identity():
    target = routing_target("stage", "C0883L8FARE")
    obs = observation()
    assert target.scope == obs["environment"]
    assert incident_key(obs) == incident_key(dict(obs, environment=target.scope))
    legacy = {"schema": 2, "trial_id": "trial"}
    assert bind_trial(legacy, "trial", channel=target.channel)["scope"] == target.scope
    with pytest.raises(ValueError):
        bind_trial(legacy, "trial", channel="C012OTHER")


# Migrated promoter 15-origin x 2-dry-run cases, preserving every former mode/channel combination.
@pytest.mark.parametrize("jira_url", [None, "https://stage-redhat.atlassian.net",
    "https://stage-redhat.atlassian.net/", "https://redhat.atlassian.net",
    "https://redhat.atlassian.net/", "", "https://example.test",
    "https://redhat.atlassian.net:443", "https://redhat.atlassian.net/path",
    "https://redhat.atlassian.net//", " https://redhat.atlassian.net",
    "https://redhat.atlassian.net?", "https://user@redhat.atlassian.net",
    "https://REDHAT.atlassian.net", "http://redhat.atlassian.net"])
@pytest.mark.parametrize("dry_run,expected,forbidden", [
    ("true", "--dry-run", "--publish-state"), ("false", "--publish-state", "--dry-run"),
])
def test_migrated_workflow_notification_matrix(tmp_path, dry_run, expected, forbidden, jira_url):
    import json
    for mode, channel in [("", ""), ("disabled", ""), ("preview", "C0883L8FARE"),
                          ("write", "C0883L8FARE"), ("write", ""), ("write", "C_OTHER"),
                          ("preview", "C_OTHER"), ("invalid", "C0883L8FARE"),
                          ("write", "C012OTHER"), ("preview", "C099ELSE")]:
        env = {"MONITOR_DRY_RUN": dry_run, "GAP_NOTIFICATION_MODE": mode,
               "GAP_SLACK_CHANNEL": channel, "RUNNER_TEMP": str(tmp_path / "runner-temp"),
               "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "2",
               **({"GAP_JIRA_URL": jira_url} if jira_url is not None else {})}
        actual, reason = workflow.monitor_arguments(env)
        assert expected in actual and forbidden not in actual
        stage = jira_url in {None, "https://stage-redhat.atlassian.net", "https://stage-redhat.atlassian.net/"}
        production = jira_url in {"https://redhat.atlassian.net", "https://redhat.atlassian.net/"}
        scope = "staging" if stage else "production"
        enabled = dry_run == "false" and mode in {"preview", "write"} and channel in {
            "C0883L8FARE", "C012OTHER", "C099ELSE"} and (stage or production)
        assert ("--notification-mode" in actual) is enabled
        if enabled:
            assert reason == "configured-" + mode
            assert actual[actual.index("--notification-mode") + 1] == mode
            assert actual[actual.index("--notification-trial-id") + 1] == f"gap-monitor-{scope}-v1"
        write = enabled and mode == "write"
        assert ("--notification-ledger" in actual) is write
        assert ("--notification-transition-id" in actual) is (write and stage)
        assert ("--notification-transition-fields" in actual) is (write and stage)
        if write:
            assert actual[actual.index("--notification-ledger") + 1] == str(
                tmp_path / f"runner-temp/gap-notifications/{scope}/123-2/journal.json")
            if stage:
                assert actual[actual.index("--notification-transition-id") + 1] == "41"
                assert json.loads(actual[actual.index("--notification-transition-fields") + 1]) == {
                    "resolution": {"id": "10000"}}
        if not enabled:
            assert reason in {"monitor-dry-run", "disabled", "invalid-mode", "invalid-configuration"}


# Migrated all ten promoter receipt cases plus explicit retry, with no commits/hook overrides.
@pytest.mark.parametrize("event,action,message,paths,email,attempt,skip", [
    ("pull_request", "synchronize", "monitor", ["GAP Leaders/run/state.json"], "bot", "1", True),
    ("pull_request", "synchronize", "monitor", ["GAP Leaders/run/state.json", "GAP History/successful/run/state.json"], "bot", "1", True),
    ("pull_request", "synchronize", "monitor", ["GAP Leaders/run/state.json", "GAP History/unsuccessful/run/state.json"], "bot", "1", True),
    ("pull_request", "synchronize", "ordinary", ["GAP Leaders/run/state.json"], "bot", "1", False),
    ("pull_request", "synchronize", "monitor", ["README.md"], "bot", "1", False),
    ("pull_request", "synchronize", "monitor", [], "bot", "1", False),
    ("pull_request", "synchronize", "monitor", ["GAP Leaders/run/state.json"], "human", "1", False),
    ("pull_request", "opened", "monitor", ["GAP Leaders/run/state.json"], "bot", "1", False),
    ("schedule", "", "monitor", ["GAP Leaders/run/state.json"], "bot", "1", False),
    ("workflow_dispatch", "", "monitor", ["GAP Leaders/run/state.json"], "bot", "1", False),
    ("pull_request", "synchronize", "monitor", ["GAP Leaders/run/state.json"], "bot", "2", False),
])
def test_migrated_receipt_policy(event, action, message, paths, email, attempt, skip):
    def git(*args):
        if args[-1] == "--format=%s":
            return ("Update GAP state from PR monitor (RHOAIENG-97052)" if message == "monitor" else "Change requested group").encode()
        if args[-1] == "--format=%ae":
            return ("github-actions[bot]@users.noreply.github.com" if email == "bot" else "human@example.com").encode()
        return ("".join(path + "\0" for path in paths)).encode()
    assert workflow.receipt_only({"MONITOR_EVENT": event, "MONITOR_ACTION": action,
                                  "MONITOR_RUN_ATTEMPT": attempt}, git=git) is skip


# Shared run command forwards resolved arguments and bounded diagnostics to the operational entrypoint.
@pytest.mark.parametrize("mode,dry_run", [
    ("disabled", "false"), ("preview", "false"), ("write", "false"),
    ("invalid", "false"), ("write", "true"),
])
def test_shared_run_entrypoint(monkeypatch, tmp_path, capsys, mode, dry_run):
    env = {"GAP_NOTIFICATION_MODE": mode, "MONITOR_DRY_RUN": dry_run,
           "GAP_SLACK_CHANNEL": "C012OTHER", "RUNNER_TEMP": str(tmp_path),
           "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "2"}
    expected, reason = workflow.monitor_arguments(env)
    monkeypatch.setattr(workflow.os, "environ", env)
    seen = []
    monkeypatch.setattr(workflow.subprocess, "call", lambda args: seen.append(args) or 17)
    assert workflow.main(["run"]) == 17
    assert seen == [[workflow.sys.executable, str(workflow.ROOT / "scripts/gap_pr_monitor.py"),
                     "--repo-root", ".", "--allow-gap-dir-fallback", *expected]]
    assert capsys.readouterr().out.strip() == "GAP notifications configuration: " + reason


# Stdlib receipt command preserves exact git protocol and writes the GitHub step output.
def test_shared_receipt_entrypoint(monkeypatch, tmp_path, capsys):
    calls = []
    def check_output(args):
        calls.append(args)
        if "--format=%s" in args:
            return b"Update GAP state from PR monitor (RHOAIENG-97052)\n"
        if "--format=%ae" in args:
            return b"github-actions[bot]@users.noreply.github.com\n"
        return b"GAP Leaders/run/state.json\0"
    monkeypatch.setattr(workflow.subprocess, "check_output", check_output)
    output = tmp_path / "output"
    monkeypatch.setattr(workflow.os, "environ", {"MONITOR_EVENT": "pull_request",
        "MONITOR_ACTION": "synchronize", "MONITOR_RUN_ATTEMPT": "1", "GITHUB_OUTPUT": str(output)})
    assert workflow.main(["receipt"]) == 0
    assert output.read_text() == "skip=true\n"
    assert calls == [["git", "log", "-1", "--format=%s"], ["git", "log", "-1", "--format=%ae"],
                     ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", "-z", "HEAD"]]
    assert capsys.readouterr().out.strip() == "GAP monitor: receipt-only update skipped"
