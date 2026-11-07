from lib.gap_notifications.dispatch_incidents import discussion_url, metadata_url
from lib.gap_notifications.notify_tools import project_body, section, visible_link_status
from gap_notification_fakes import make_runner
import copy
import json

import pytest
from jinja2 import UndefinedError
import lib.gap_notifications.notify_tools as presentation

from lib.gap_notifications.dispatch_incidents import _description, _summary
from lib.gap_notifications.notify_tools import JiraNotifier
from lib.gap_notifications.notify_tools import category_labels, render_markdown
from gap_notification_fakes import observation
from service_client_fakes import FakeResponse, FakeSession, notifier_for, issue_for


@pytest.mark.parametrize('category', ['build', 'conflict', 'feasibility'])
def test_categories_labels_and_missing(category, setup):
    runner, jira, _ = setup
    obs = observation(category=category)
    runner().run([obs])
    issue = jira.issues['RHAI-1']
    assert set(category_labels(category)) <= set(issue['fields']['labels'])
    issue['fields']['labels'].append('user-label')
    original = set(issue['fields']['labels'])
    # Existing no-op remains a no-op, including presentation labels.
    issue['fields']['labels'].remove(category_labels(category)[0])
    before = copy.deepcopy(issue)
    runner().run([obs])
    assert issue == before
    runner().run([observation(category=category, oid='new')])
    assert original <= set(issue['fields']['labels'])
    assert issue['fields']['summary'] == _summary(obs)
    assert 'Unavailable' in render_markdown(obs)


def test_grouped_targets_and_literal_injection(monkeypatch, tmp_path):
    obs = observation(evidence={'synthetic': True, 'reason': '# injected\n[evil](https://evil.test) <script> {{ missing }}',
        'affected_targets': [{'branch': 'rhoai-3.2', 'reason': 'conflict'}, {'branch': 'rhoai-3.3', 'component': 'cpu'}],
        'leader_url': 'https://example.test/pr/1'})
    monkeypatch.chdir(tmp_path)
    md = render_markdown(obs)
    adf = _description(obs)
    JiraNotifier._validate_adf(adf)
    headings = [n for n in adf['content'] if n['type'] == 'heading']
    assert len(headings) == 5
    assert r'rhoai\-3' in md and 'Actual promotion stream: main → stable' in md
    raw = json.dumps(adf)
    assert '# injected [evil](https://evil.test) <script> {{ missing }}' in raw
    assert raw.count('"href"') == 1
    assert 'GAP STAGING TEST' in raw


@pytest.mark.parametrize('url', ['http://example.test', 'https://u:p@example.test', 'https://example.test/\n#evil', 'https://example.test/[x]', 'https://example.test:bad'])
def test_bad_urls_fail(url):
    with pytest.raises(ValueError):
        _description(observation(evidence={'leader_url': url}))


def test_explicit_targets_only_and_bounded():
    assert 'rhoai-' not in render_markdown(observation(destination_branch='stable', evidence={'reason': 'plain'}))
    with pytest.raises(ValueError):
        _description(observation(evidence={'affected_targets': [{'branch': 'stable'}]}))
    with pytest.raises(ValueError):
        _description(observation(evidence={'reason': 'x' * 2001}))


def test_strict_template_missing_fields(monkeypatch):
    class MissingTemplate:
        def joinpath(self, path):
            return self
        def read_text(self, **kwargs):
            return '# GAP STAGING TEST\n\n{{ required_missing }}'
    monkeypatch.setattr(presentation, 'files', lambda package: MissingTemplate())
    with pytest.raises(UndefinedError):
        render_markdown(observation())


@pytest.mark.parametrize('existing', [False, True])
@pytest.mark.parametrize('invalid', ['url', 'size', 'template', 'labels'])
def test_invalid_presentation_has_no_write_intent_and_corrected_can_proceed(setup, monkeypatch, existing, invalid):
    runner, jira, slack = setup
    if existing:
        runner().run([observation()])
    before_issues = copy.deepcopy(jira.issues)
    before_properties = copy.deepcopy(jira.properties)
    before_calls = list(jira.calls)
    before_slack = list(slack.calls)
    obs = observation(oid='render-review')
    with monkeypatch.context() as patch:
        if invalid == 'url':
            obs['evidence']['leader_url'] = 'http://invalid.test'
        elif invalid == 'size':
            obs['evidence']['reason'] = 'x' * 2001
        elif invalid == 'template':
            class InvalidTemplate:
                def joinpath(self, path):
                    return self
                def read_text(self, **kwargs):
                    return '**unsupported**'
            patch.setattr(presentation, 'files', lambda package: InvalidTemplate())
        else:
            patch.setattr('lib.gap_notifications.dispatch_incidents.category_labels', lambda category: ['invalid label'])
        failed = runner().run([obs])
    assert failed['outbox'][-1]['error_type'] == 'ValueError'
    if not existing:
        assert not failed.get('creates')
    assert jira.issues == before_issues
    assert jira.properties == before_properties
    assert jira.calls == before_calls and slack.calls == before_slack
    corrected = runner().run([observation(oid='render-review')])
    assert corrected['outbox'][-1]['state'] == 'confirmed'
    assert jira.calls[len(before_calls):] == (['update'] if existing else ['create', 'update'])
    assert jira.properties['RHAI-1']['jira_intent'] is None


@pytest.mark.parametrize('value', ['a`b', 'a\\\\`b', 'a`b\\\\', '`\\\\`'])
def test_inline_code_escaped_backtick_and_backslash_literal(value):
    adf = _description(observation(oid=value, evidence={'head_sha': value}))
    JiraNotifier._validate_adf(adf)
    def code_values(node):
        values = [node['text']] if {'type': 'code'} in node.get('marks', []) else []
        for child in node.get('content', []):
            values.extend(code_values(child))
        return values
    assert code_values(adf) == [value, value]


def test_dispatcher_only_adds_labels_despite_stale_registry_snapshot(setup, monkeypatch):
    runner, jira, _ = setup
    runner().run([observation()])
    update = jira.update_issue
    def raced_update(key, **kwargs):
        assert 'labels' not in kwargs
        assert kwargs['add_labels'] == ['gap-build-failure']
        jira.issues[key]['fields']['labels'].append('human-after-search')
        return update(key, **kwargs)
    monkeypatch.setattr(jira, 'update_issue', raced_update)
    runner().run([observation(oid='racing-update')])
    assert 'human-after-search' in jira.issues['RHAI-1']['fields']['labels']


def test_actual_adapter_fake_transport_roundtrip():
    obs = observation(evidence={'leader_url': 'https://example.test/leader/1', 'head_sha': 'abc123'})
    adf = _description(obs)
    issue = issue_for(summary=_summary(obs), description=adf, labels=['gap-build-failure'])
    session = FakeSession(responses={'post': [FakeResponse({'key': 'RHAI-1'})], 'get': [FakeResponse(issue)]})
    result = notifier_for(session).create_bug(_summary(obs), adf, ['gap-build-failure'])
    assert result['key'] == 'RHAI-1'
    assert JiraNotifier._adf_signature(adf) == JiraNotifier._adf_signature(issue['fields']['description'])
    assert '"code"' in json.dumps(adf) and '"href": "https://example.test/leader/1"' in json.dumps(adf)


@pytest.fixture
def setup(tmp_path):
    return make_runner(tmp_path)


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
