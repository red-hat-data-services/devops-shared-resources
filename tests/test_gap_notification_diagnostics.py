import copy
import io
import json

import pytest

from lib.gap_notification_diagnostics import discussion_url, inspect_issue, project_body, section, visible_link_status, metadata_url
from lib.gap_notification_slack import SlackAmbiguousError, SlackPermanentError, SlackRateLimitError
from scripts.gap_notify import main
from test_gap_notification_dispatch import observation, setup


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


def test_inspect_retry_deadline_and_visible_missing(setup):
    runner, jira, _ = setup
    runner().run([observation()])
    state = jira.properties['RHAI-1']
    state['delivery']['intent'] = {'id': state['episode_id'], 'event_id': state['last_event'],
        'outcome': 'failure', 'state': 'rate_limited', 'retry_at': 1900000000}
    nodes = jira.issues['RHAI-1']['fields']['description']['content']
    index = next(i for i, node in enumerate(nodes) if node == section('')[0])
    del nodes[index:index + 2]
    report = inspect_issue(jira, 'RHAI-1')
    assert report['visible_link'] == 'missing'
    assert report['slack_intent']['retry_at'] == 1900000000


@pytest.mark.parametrize('field,value', [('scope', 'wrong'), ('trial_id', 'different'), ('episode_id', 'not-uuid')])
def test_malformed_identity_never_projects(setup, field, value):
    runner, jira, _ = setup
    runner().run([observation()])
    jira.properties['RHAI-1'][field] = value
    assert inspect_issue(jira, 'RHAI-1')['property'] == 'malformed_or_unavailable'


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
    monkeypatch.setattr('lib.gap_notification_dispatch.time.time', lambda: 1800000000)
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


def test_both_sections_preserve_body_idempotent_and_reject_edited_metadata():
    body = {'type': 'doc', 'version': 1, 'content': [{'type': 'paragraph', 'content': [{'type': 'text', 'text': 'human text'}]}]}
    slack = discussion_url({'channel': 'C0883L8FARE', 'ts': '123.000001'})
    meta = metadata_url('RHAI-123')
    updated = project_body(body, slack, metadata=meta)
    assert updated['content'][:1] == body['content']
    assert project_body(updated, slack, metadata=meta) == updated
    assert visible_link_status(updated, slack) == 'match'
    assert visible_link_status(updated, meta, metadata=True) == 'match'
    updated['content'].extend(section(meta, metadata=True))
    with pytest.raises(ValueError):
        project_body(updated, slack, metadata=meta)
    updated['content'] = updated['content'][:-2]
    updated['content'][-1]['content'][-1]['text'] = 'human changed note'
    with pytest.raises(ValueError):
        project_body(updated, slack, metadata=meta)


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


def test_projection_preserves_nodes_and_rejects_edited_duplicates():
    body = {'type': 'doc', 'version': 1, 'content': [{'type': 'paragraph', 'content': [{'type': 'text', 'text': 'human'}]}]}
    url = discussion_url({'channel': 'C0883L8FARE', 'ts': '123.000001'})
    updated = project_body(body, url)
    assert updated['content'][:1] == body['content']
    assert project_body(updated, url) == updated
    duplicate = copy.deepcopy(updated)
    duplicate['content'].extend(section(url))
    with pytest.raises(ValueError):
        project_body(duplicate, url)
    updated['content'][-1]['content'][0]['text'] = 'edited by human'
    with pytest.raises(ValueError):
        project_body(updated, url)


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
    monkeypatch.setattr('lib.gap_notification_dispatch.Ledger', forbidden)
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


@pytest.mark.parametrize('state', [None, {}, {'trial_id': 'SECRET', 'incident_key': 'SECRET'}])
def test_inspect_missing_malformed_safe(setup, state):
    runner, jira, _ = setup
    runner().run([observation()])
    jira.properties['RHAI-1'] = state
    report = inspect_issue(jira, 'RHAI-1')
    assert report['property'] == ('missing' if state is None else 'malformed_or_unavailable')
    assert 'SECRET' not in json.dumps(report)


@pytest.mark.parametrize('extra', [['--write'], ['--ledger', '/not/read'], ['--resolve-receipt', '/not/read'], ['--jira-url', 'https://redhat.atlassian.net']])
def test_inspect_rejects_mutating_or_production_args(extra):
    def forbidden(*a, **kw):
        raise AssertionError('client created')
    assert main(['--inspect', 'RHAI-1'] + extra, environ={}, jira_factory=forbidden, stdout=io.StringIO()) == 1
