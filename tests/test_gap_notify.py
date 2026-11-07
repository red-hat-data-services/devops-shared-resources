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


def test_default_dry_run_no_credentials_clients_or_files(tmp_path):
    output = io.StringIO()
    def forbidden(*a, **kw):
        raise AssertionError("client constructed")
    assert main(args(tmp_path), environ={}, jira_factory=forbidden, slack_factory=forbidden, stdout=output) == 0
    assert json.loads(output.getvalue())["dry_run"] is True
    assert not (tmp_path / "ledger.json").exists()
    assert not (tmp_path / "ledger.json.lock").exists()


def test_reject_production_before_client_creation(tmp_path):
    constructed = []
    def forbidden(*values):
        constructed.append(values)
        raise AssertionError("client constructed before destination validation")
    env = {"JIRA_USER_EMAIL": "fixture", "JIRA_API_TOKEN": "fake", "SLACK_TOKEN": "fake"}
    assert main(args(tmp_path) + ["--jira-url", "https://redhat.atlassian.net", "--write"],
                environ=env, jira_factory=forbidden, slack_factory=forbidden,
                stdout=io.StringIO()) == 1
    assert constructed == []


def test_write_requires_explicit_environment_credentials(tmp_path):
    assert main(args(tmp_path) + ["--write"], environ={}, stdout=io.StringIO()) == 1
    assert not (tmp_path / "ledger.json").exists()


def test_email_alias_and_exact_staging_adapter_configuration(tmp_path):
    called = []
    def jira(*values):
        called.append(values)
        return object()
    def slack(*values):
        called.append(values)
        return object()
    env = {"JIRA_API_EMAIL": "fixture@example.invalid", "JIRA_API_TOKEN": "fake", "SLACK_BOT_TOKEN": "fake"}
    assert main(args(tmp_path) + ["--write"], environ=env, jira_factory=jira, slack_factory=slack, stdout=io.StringIO()) == 0
    assert called[0][:3] == ("https://stage-redhat.atlassian.net", "RHAI", "Bug")
    assert called[0][3] == env["JIRA_API_EMAIL"]
    assert called[1][1] == "C0883L8FARE"


def test_user_email_has_precedence(tmp_path):
    got = []
    env = {"JIRA_USER_EMAIL": "primary", "JIRA_API_EMAIL": "fallback", "JIRA_API_TOKEN": "fake", "SLACK_BOT_TOKEN": "fake"}
    assert main(args(tmp_path) + ["--write"], environ=env, jira_factory=lambda *a: got.append(a) or object(), slack_factory=lambda *a: object(), stdout=io.StringIO()) == 0
    assert got[0][3] == "primary"


def test_never_print_exception_text(tmp_path):
    out = io.StringIO()
    def fail(*a):
        raise RuntimeError("VERY_SECRET")
    env = {"JIRA_USER_EMAIL": "fixture", "JIRA_API_TOKEN": "fake", "SLACK_BOT_TOKEN": "fake"}
    assert main(args(tmp_path) + ["--write"], environ=env, jira_factory=fail, stdout=out) == 1
    assert "VERY_SECRET" not in out.getvalue()


def test_refuse_leader_state_as_ledger(tmp_path):
    values = args(tmp_path)
    values[-1] = str(tmp_path / "state.json")
    assert main(values, stdout=io.StringIO()) == 1


def test_registry_v2_rejects_old_ledger_before_client_construction(tmp_path):
    values = args(tmp_path)
    (tmp_path / "ledger.json").write_text(json.dumps({"schema": 1, "trial_id": "default"}))
    constructed = []
    env = {"JIRA_USER_EMAIL": "fixture", "JIRA_API_TOKEN": "fake", "SLACK_TOKEN": "fake"}
    result = main(values + ["--write"], environ=env, jira_factory=lambda *a: constructed.append(a), stdout=io.StringIO())
    assert result == 1 and constructed == []


def test_dry_run_registry_v2_reports_candidate_without_remote_lookup(tmp_path):
    obs = {"environment": "stage-redhat.atlassian.net/RHAI/C0883L8FARE", "repository": "acme/repo",
           "source_branch": "main", "destination_branch": "stable", "category": "conflict",
           "status": "failure", "observation_id": "fixture-1", "evidence": {"synthetic": True}}
    out = io.StringIO()
    assert main(args(tmp_path, [obs]), environ={}, stdout=out) == 0
    data = json.loads(out.getvalue())
    assert data["registry_version"] == 2 and data["incidents"] == 1
    assert len(data["outbox"]) == 1
    assert not (tmp_path / "ledger.json").exists()


@pytest.fixture
def setup(tmp_path):
    return make_runner(tmp_path)


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
    assert main(['--inspect', 'RHAI-1'], environ={'JIRA_API_EMAIL': 'fixture', 'JIRA_API_TOKEN': 'fake'},
                jira_factory=lambda *a: ReadOnly(), slack_factory=forbidden, stdout=out) == 0
    result = json.loads(out.getvalue())
    assert result['trial_id'] == 'trial' and result['trial_match'] is None
    assert result['visible_link'] == 'match' and result['open'] is True
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before_files
    assert inspect_issue(jira, 'RHAI-1', trial='wrong')['property'] == 'trial_mismatch'
    jira.close('RHAI-1')
    assert inspect_issue(jira, 'RHAI-1')['open'] is False


@pytest.mark.parametrize('extra', [['--write'], ['--ledger', '/not/read'], ['--resolve-receipt', '/not/read'], ['--jira-url', 'https://redhat.atlassian.net']])
def test_inspect_rejects_mutating_or_production_args(extra):
    def forbidden(*a, **kw):
        raise AssertionError('client created')
    assert main(['--inspect', 'RHAI-1'] + extra, environ={}, jira_factory=forbidden, stdout=io.StringIO()) == 1
