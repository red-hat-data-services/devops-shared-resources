import pytest
from lib.gap_notifications.lookup_tools import inspect_issue
from gap_notification_fakes import observation
from gap_notification_fakes import make_runner
import io
import json

from scripts.gap_notify import main


def args(tmp_path, observations=None):
    source = tmp_path / "observations.json"
    source.write_text(json.dumps(observations or []))
    return ["--observations", str(source), "--ledger", str(tmp_path / "ledger.json")]


# Default CLI preview must need no credentials or clients and create no journal or lock file.
def test_default_dry_run_no_credentials_clients_or_files(tmp_path):
    output = io.StringIO()
    def forbidden(*a, **kw):
        raise AssertionError("client constructed")
    assert main(args(tmp_path), environ={"GAP_SLACK_CHANNEL": "C0883L8FARE", }, jira_factory=forbidden, slack_factory=forbidden, stdout=output) == 0
    assert json.loads(output.getvalue())["dry_run"] is True
    assert not (tmp_path / "ledger.json").exists()
    assert not (tmp_path / "ledger.json.lock").exists()


# Production destinations must fail before either client factory is invoked.
def test_reject_production_before_client_creation(tmp_path):
    constructed = []
    def forbidden(*values):
        constructed.append(values)
        raise AssertionError("client constructed before destination validation")
    env = {"GAP_SLACK_CHANNEL": "C0883L8FARE", "JIRA_USER_EMAIL": "fixture", "JIRA_API_TOKEN": "fake", "SLACK_TOKEN": "fake"}
    assert main(args(tmp_path) + ["--jira-url", "https://redhat.atlassian.net", "--write"],
                environ=env, jira_factory=forbidden, slack_factory=forbidden,
                stdout=io.StringIO()) == 1
    assert constructed == []


# Write mode must require explicit credentials and must not create a journal when they are
# missing.
def test_write_requires_explicit_environment_credentials(tmp_path):
    assert main(args(tmp_path) + ["--write"], environ={"GAP_SLACK_CHANNEL": "C0883L8FARE", }, stdout=io.StringIO()) == 1
    assert not (tmp_path / "ledger.json").exists()


# The email alias must work while adapters remain pinned to the agreed staging destinations.
def test_email_alias_and_exact_staging_adapter_configuration(tmp_path):
    called = []
    def jira(*values):
        called.append(values)
        return object()
    def slack(*values):
        called.append(values)
        return object()
    env = {"GAP_SLACK_CHANNEL": "C0883L8FARE", "JIRA_API_EMAIL": "fixture@example.invalid", "JIRA_API_TOKEN": "fake", "SLACK_BOT_TOKEN": "fake"}
    assert main(args(tmp_path) + ["--write"], environ=env, jira_factory=jira, slack_factory=slack, stdout=io.StringIO()) == 0
    assert called[0][:3] == ("https://stage-redhat.atlassian.net", "RHAI", "Bug")
    assert called[0][3] == env["JIRA_API_EMAIL"]
    assert called[1][1] == "C0883L8FARE"


# The primary Jira email variable must take precedence over its compatibility alias.
def test_user_email_has_precedence(tmp_path):
    got = []
    env = {"GAP_SLACK_CHANNEL": "C0883L8FARE", "JIRA_USER_EMAIL": "primary", "JIRA_API_EMAIL": "fallback", "JIRA_API_TOKEN": "fake", "SLACK_BOT_TOKEN": "fake"}
    assert main(args(tmp_path) + ["--write"], environ=env, jira_factory=lambda *a: got.append(a) or object(), slack_factory=lambda *a: object(), stdout=io.StringIO()) == 0
    assert got[0][3] == "primary"


# CLI failures must not print raw exception text that could contain secrets.
def test_never_print_exception_text(tmp_path):
    out = io.StringIO()
    def fail(*a):
        raise RuntimeError("VERY_SECRET")
    env = {"GAP_SLACK_CHANNEL": "C0883L8FARE", "JIRA_USER_EMAIL": "fixture", "JIRA_API_TOKEN": "fake", "SLACK_BOT_TOKEN": "fake"}
    assert main(args(tmp_path) + ["--write"], environ=env, jira_factory=fail, stdout=out) == 1
    assert "VERY_SECRET" not in out.getvalue()


# The CLI must refuse to use Leader state as its uncertainty journal.
def test_refuse_leader_state_as_ledger(tmp_path):
    values = args(tmp_path)
    values[-1] = str(tmp_path / "state.json")
    assert main(values, stdout=io.StringIO()) == 1


# An incompatible old journal must fail before client construction.
def test_registry_v2_rejects_old_ledger_before_client_construction(tmp_path):
    values = args(tmp_path)
    (tmp_path / "ledger.json").write_text(json.dumps({"schema": 1, "trial_id": "default"}))
    constructed = []
    env = {"GAP_SLACK_CHANNEL": "C0883L8FARE", "JIRA_USER_EMAIL": "fixture", "JIRA_API_TOKEN": "fake", "SLACK_TOKEN": "fake"}
    result = main(values + ["--write"], environ=env, jira_factory=lambda *a: constructed.append(a), stdout=io.StringIO())
    assert result == 1 and constructed == []


# Preview must report candidate incidents without querying remote state or persisting a journal.
def test_dry_run_registry_v2_reports_candidate_without_remote_lookup(tmp_path):
    obs = {"environment": "stage-redhat.atlassian.net/RHAI/C0883L8FARE", "repository": "acme/repo",
           "source_branch": "main", "destination_branch": "stable", "category": "conflict",
           "status": "failure", "observation_id": "fixture-1", "evidence": {"synthetic": True}}
    out = io.StringIO()
    assert main(args(tmp_path, [obs]), environ={"GAP_SLACK_CHANNEL": "C0883L8FARE", }, stdout=out) == 0
    data = json.loads(out.getvalue())
    assert data["registry_version"] == 2 and data["incidents"] == 1
    assert len(data["outbox"]) == 1
    assert not (tmp_path / "ledger.json").exists()


@pytest.fixture
def setup(tmp_path):
    return make_runner(tmp_path)


# Inspection must use Jira reads only, preserve local files and report trial mismatches
# explicitly.
def test_inspect_jira_only_no_ledger_or_slack(setup, monkeypatch, tmp_path):
    runner, jira, _ = setup
    runner().run([observation()])
    # Query must perform only these two reads.
    class ReadOnly:
        get_issue = jira.get_issue
        get_notification_state = jira.get_notification_state
    def forbidden(*a, **kw):
        raise AssertionError('forbidden')
    before_files = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr('lib.gap_notifications.dispatch_incidents.Ledger', forbidden)
    out = io.StringIO()
    assert main(['--inspect', 'RHAI-1'], environ={"GAP_SLACK_CHANNEL": "C0883L8FARE", 'JIRA_API_EMAIL': 'fixture', 'JIRA_API_TOKEN': 'fake'},
                jira_factory=lambda *a: ReadOnly(), slack_factory=forbidden, stdout=out) == 0
    result = json.loads(out.getvalue())
    assert result['trial_id'] == 'trial' and result['trial_match'] is None
    assert result['visible_link'] == 'match' and result['open'] is True
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before_files
    assert inspect_issue(jira, 'RHAI-1', trial='wrong')['property'] == 'trial_mismatch'
    jira.close('RHAI-1')
    assert inspect_issue(jira, 'RHAI-1')['open'] is False


# Inspection must reject write options and production URLs without explicit environment selection.
@pytest.mark.parametrize('extra', [['--write'], ['--ledger', '/not/read'], ['--resolve-receipt', '/not/read'], ['--jira-url', 'https://redhat.atlassian.net']])
def test_inspect_rejects_mutating_or_production_args(extra):
    def forbidden(*a, **kw):
        raise AssertionError('client created')
    assert main(['--inspect', 'RHAI-1'] + extra, environ={"GAP_SLACK_CHANNEL": "C0883L8FARE", }, jira_factory=forbidden, stdout=io.StringIO()) == 1


# Any explicit legacy selector must fail before I/O with sanitized migration guidance.
@pytest.mark.parametrize('legacy', ['', 'stage', 'production', 'unknown'])
@pytest.mark.parametrize('url', [None, 'https://redhat.atlassian.net'])
def test_legacy_selector_rejected_with_migration_guidance_before_io(legacy, url):
    env = {"GAP_SLACK_CHANNEL": "C0883L8FARE", 'GAP_JIRA_ENVIRONMENT': legacy}
    if url is not None:
        env['GAP_JIRA_URL'] = url
    out = io.StringIO()
    def forbidden(*a, **kw):
        raise AssertionError('client created')
    assert main(['--inspect', 'RHAI-1'], environ=env, jira_factory=forbidden, stdout=out) == 1
    result = json.loads(out.getvalue())
    assert result['type'] == 'ValueError' and 'Remove GAP_JIRA_ENVIRONMENT' in result['guidance']


# The deprecated URL flag may assert the selected origin but never route independently.
@pytest.mark.parametrize('name', ['stage', 'production'])
@pytest.mark.parametrize('suffix', ['', '/'])
def test_deprecated_url_flag_is_matching_assertion_only(tmp_path, name, suffix):
    from lib.gap_notifications.notify_tools import routing_target as jira_target
    target = jira_target(name)
    out = io.StringIO()
    assert main(args(tmp_path, [observation(environment=target.scope)]) +
                ['--jira-url', target.url + suffix],
                environ={"GAP_SLACK_CHANNEL": "C0883L8FARE", 'GAP_JIRA_URL': target.url}, stdout=out) == 0
    assert json.loads(out.getvalue())['dry_run'] is True


# The selector alone must not enable writes, construct clients or create a journal.
@pytest.mark.parametrize('environment', ['stage', 'production'])
def test_environment_selection_keeps_cli_preview_read_only(tmp_path, environment):
    from lib.gap_notifications.notify_tools import routing_target as jira_target
    constructed = []
    def forbidden(*a, **kw):
        constructed.append(True)
        raise AssertionError('client construction')
    out = io.StringIO()
    obs = observation(environment=jira_target(environment).scope)
    result = main(args(tmp_path, [obs]), environ={"GAP_SLACK_CHANNEL": "C0883L8FARE", 'GAP_JIRA_URL': jira_target(environment).url},
                  jira_factory=forbidden, slack_factory=forbidden, stdout=out)
    assert result == 0 and json.loads(out.getvalue())['dry_run'] is True
    assert not constructed and not (tmp_path / 'ledger.json').exists()


# Unknown values, foreign observations and stale journals must fail before either factory.
@pytest.mark.parametrize('problem', ['typo', 'empty', 'foreign-observation', 'old-journal', 'url', 'issue-type'])
def test_invalid_environment_configuration_fails_before_clients(tmp_path, problem):
    from lib.gap_notifications.notify_tools import routing_target as jira_target
    obs = observation(environment=jira_target('production').scope)
    env = {"GAP_SLACK_CHANNEL": "C0883L8FARE", 'GAP_JIRA_URL': jira_target('production').url, 'JIRA_USER_EMAIL': 'dummy@example.test',
           'JIRA_API_TOKEN': 'dummy', 'SLACK_TOKEN': 'dummy'}
    if problem in {'typo', 'empty'}: env['GAP_JIRA_URL'] = 'typo' if problem == 'typo' else ''
    if problem == 'foreign-observation': obs = observation()
    values = args(tmp_path, [obs]) + ['--write']
    if problem == 'old-journal':
        (tmp_path / 'ledger.json').write_text(json.dumps({'schema': 2, 'trial_id': 'default'}))
    if problem == 'url': values += ['--jira-url', 'https://stage-redhat.atlassian.net']
    if problem == 'issue-type': values += ['--issue-type', '10016']
    constructed = []
    def forbidden(*a, **kw):
        constructed.append(True)
        raise AssertionError('client construction')
    assert main(values, environ=env, jira_factory=forbidden, slack_factory=forbidden, stdout=io.StringIO()) == 1
    assert not constructed


# Explicit production write and inspect must use the same target, with no Slack access on inspect.
def test_production_cli_write_then_read_only_inspect(tmp_path):
    from gap_notification_fakes import Jira, Slack
    from lib.gap_notifications.notify_tools import routing_target as jira_target
    target = jira_target('production')
    jira, slack = Jira(), Slack()
    jira.base_url = target.url
    env = {"GAP_SLACK_CHANNEL": "C0883L8FARE", 'GAP_JIRA_URL': target.url, 'JIRA_USER_EMAIL': 'dummy@example.test',
           'JIRA_API_TOKEN': 'dummy', 'SLACK_TOKEN': 'dummy'}
    def factory(*a, **kw):
        assert a[:3] == (target.url, 'RHAI', 'Bug') and kw == target.jira.options
        return jira
    values = args(tmp_path, [observation(environment=target.scope)]) + ['--write']
    assert main(values, environ=env, jira_factory=factory, slack_factory=lambda *a: slack, stdout=io.StringIO()) == 0
    assert len(slack.calls) == 1 and jira.properties['RHAI-1']['scope'] == target.scope
    env.pop('SLACK_TOKEN')
    def forbidden(*a): raise AssertionError('inspect must not construct Slack')
    out = io.StringIO()
    assert main(['--inspect', 'RHAI-1'], environ=env, jira_factory=factory, slack_factory=forbidden, stdout=out) == 0
    report = json.loads(out.getvalue())
    assert report['property'] == 'valid' and report['scope'] == target.scope
    assert report['metadata_url'].startswith(target.url + '/') and len(slack.calls) == 1


# The compatibility flag is an assertion, not an alternative routing source.
@pytest.mark.parametrize("channel", ["C012OTHER", "C099ELSE"])
@pytest.mark.parametrize("assertion,expected", [(None, 0), ("same", 0), ("COTHER", 1)])
def test_configured_channel_cli_assertion(tmp_path, channel, assertion, expected):
    from lib.gap_notifications.notify_tools import routing_target
    target = routing_target("stage", channel)
    values = args(tmp_path, [observation(environment=target.scope)])
    if assertion:
        values += ["--channel", channel if assertion == "same" else assertion]
    def forbidden(*a, **kw):
        raise AssertionError("preview must not construct clients")
    assert main(values, environ={"GAP_SLACK_CHANNEL": channel}, jira_factory=forbidden,
                slack_factory=forbidden, stdout=io.StringIO()) == expected


# Missing configuration is rejected before opening inputs or constructing any adapter.
@pytest.mark.parametrize("env", [{}, {"GAP_SLACK_CHANNEL": ""}, {"GAP_SLACK_CHANNEL": " "}])
def test_cli_requires_configured_destination(env):
    def forbidden(*a, **kw):
        raise AssertionError("invalid configuration must not construct clients")
    assert main(["--observations", "/nonexistent", "--ledger", "/nonexistent-journal"],
                environ=env, jira_factory=forbidden, slack_factory=forbidden, stdout=io.StringIO()) == 1
