import copy
import json

import pytest
from jinja2 import UndefinedError
import lib.gap_notifications.notify_tools as presentation

from lib.gap_notifications.dispatch_incidents import _description, _summary
from lib.gap_notifications.notify_tools import JiraNotifier
from lib.gap_notifications.notify_tools import category_labels, markdown_to_adf, render_markdown
from test_gap_notification_dispatch import observation, setup
import test_gap_notification_jira as adapter_tests
from test_gap_notification_jira import FakeResponse, FakeSession


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


@pytest.mark.parametrize('md', ['**bold**', '<html>', '> quote', '```code```', '1. ordered', '+ unordered', '  nested', '[bad](http://example.test)', '##', '|table|'])
def test_unsupported_syntax_fails(md):
    with pytest.raises(ValueError):
        markdown_to_adf(md)


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


def test_additive_adapter_missing_label_readback_fails():
    helper = adapter_tests.JiraNotifierTests()
    current = helper._issue(labels=['human'])
    session = FakeSession(responses={'get': [FakeResponse(current), FakeResponse(current)],
                                    'put': [FakeResponse(None, 204)]})
    with pytest.raises(adapter_tests.JiraAmbiguousError):
        helper._notifier(session).update_issue('RHAI-1', add_labels=['gap-build-failure'])


def test_additive_adapter_rejects_replace_and_add_together():
    helper = adapter_tests.JiraNotifierTests()
    session = FakeSession()
    with pytest.raises(ValueError):
        helper._notifier(session).update_issue('RHAI-1', labels=['one'], add_labels=['two'])
    assert session.calls == []


def test_additive_adapter_update_preserves_racing_human_label():
    helper = adapter_tests.JiraNotifierTests()
    adf = _description(observation())
    # The registry search snapshot lacks human-new. It appears before adapter GET.
    current = helper._issue(description=adf, labels=['gap-registry-id', 'human-new'])
    updated = copy.deepcopy(current)
    updated['fields']['labels'].extend(['gap-build-failure', 'human-even-newer'])
    session = FakeSession(responses={'get': [FakeResponse(current), FakeResponse(updated)],
                                    'put': [FakeResponse(None, 204)]})
    actual = helper._notifier(session).update_issue('RHAI-1', description=adf, add_labels=['gap-build-failure'])
    assert set(actual['fields']['labels']) == set(updated['fields']['labels'])
    payload = next(kwargs['json'] for method, _, kwargs in session.calls if method == 'put')
    assert 'labels' not in payload['fields']
    assert payload['update']['labels'] == [{'add': 'gap-build-failure'}]


def test_actual_adapter_fake_transport_roundtrip():
    helper = adapter_tests.JiraNotifierTests()
    obs = observation(evidence={'leader_url': 'https://example.test/leader/1', 'head_sha': 'abc123'})
    adf = _description(obs)
    issue = helper._issue(summary=_summary(obs), description=adf, labels=['gap-build-failure'])
    session = FakeSession(responses={'post': [FakeResponse({'key': 'RHAI-1'})], 'get': [FakeResponse(issue)]})
    result = helper._notifier(session).create_bug(_summary(obs), adf, ['gap-build-failure'])
    assert result['key'] == 'RHAI-1'
    assert JiraNotifier._adf_signature(adf) == JiraNotifier._adf_signature(issue['fields']['description'])
    assert '"code"' in json.dumps(adf) and '"href": "https://example.test/leader/1"' in json.dumps(adf)
