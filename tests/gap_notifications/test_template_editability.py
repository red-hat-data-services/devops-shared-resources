"""Templates own presentation wording; Python retains validation and transport safety."""
from importlib.resources import files
import re

import pytest

from gap_notification_fakes import observation, make_runner
from lib.gap_notifications import notify_tools
from lib.gap_notifications.dispatch_incidents import _summary
from lib.gap_notifications.lookup_tools import inspect_issue


@pytest.mark.parametrize('environment', ['stage', 'production'])
def test_first_h1_supplies_jira_title_with_literal_escaping(environment):
    obs = observation(repository='acme/[repo]_*{{ literal }}', component='cpu_[test]')
    title = _summary(obs, jira_environment=environment)
    body = notify_tools.description(obs, jira_environment=environment)
    h1 = next(node for node in body['content'] if node['type'] == 'heading' and node['attrs']['level'] == 1)
    assert title == ''.join(node['text'] for node in h1['content'])
    assert obs['repository'] in title and obs['component'] in title
    assert not any(node.get('marks') for node in h1['content'])


def test_overlong_rendered_title_is_rejected():
    with pytest.raises(ValueError, match='1–250 characters'):
        _summary(observation(repository='acme/' + 'x' * 250))


def replace_template(monkeypatch, name, transform):
    original = files('lib.gap_notifications')
    source = original.joinpath('templates/' + name).read_text()

    class Resources:
        def joinpath(self, path):
            if path == 'templates/' + name:
                return self
            return original.joinpath(path)

        def read_text(self, **kwargs):
            return transform(source)

    monkeypatch.setattr(notify_tools, 'files', lambda package: Resources())


def test_title_is_extracted_from_first_h1_not_rebuilt_in_python(monkeypatch):
    replace_template(monkeypatch, 'issue.md.j2', lambda source: '# Custom title\n' + source)
    assert _summary(observation()) == 'Custom title'


@pytest.mark.parametrize('category', ['build', 'conflict', 'feasibility'])
@pytest.mark.parametrize('title_change', ['template', 'human', 'legacy'])
def test_title_changes_preserve_incident_updates_recovery_and_replay(tmp_path, monkeypatch, category, title_change):
    runner, jira, slack = make_runner(tmp_path)
    runner().run([observation(category=category)])
    root = jira.properties['RHAI-1']['delivery']['root']
    if title_change == 'template':
        replace_template(monkeypatch, 'issue.md.j2', lambda source: '# New title format\n' + source)
    elif title_change == 'legacy':
        jira.issues['RHAI-1']['fields']['summary'] = f'[GAP STAGING TEST] {category} failure: acme/repo'
    else:
        jira.issues['RHAI-1']['fields']['summary'] = 'Title edited by a human'
    stored_title = jira.issues['RHAI-1']['fields']['summary']
    report = inspect_issue(jira, 'RHAI-1')
    assert report['property'] == 'valid' and report['identity_match']
    assert report['root'] == root

    updated = runner('update').run([observation(category=category, oid='new-failure')])
    assert updated['outbox'][0]['state'] == 'confirmed'
    assert len(jira.issues) == 1 and len(slack.calls) == 2
    assert slack.calls[-1][1]['thread_ts'] == root['ts']
    assert jira.issues['RHAI-1']['fields']['summary'] == stored_title

    recovered = runner('recovery').run([observation('success', 'recovered', category=category)])
    assert recovered['outbox'][0]['state'] == 'confirmed'
    assert jira.closed('RHAI-1')
    assert len(slack.calls) == 3 and slack.calls[-1][1]['thread_ts'] == root['ts']
    runner('replay').run([observation('success', 'recovered', category=category)])
    assert len(jira.issues) == 1 and len(slack.calls) == 3


@pytest.mark.parametrize('tamper', ['project', 'issue_type', 'scope', 'incident_key', 'episode_label'])
def test_edited_titles_do_not_bypass_identity_checks(tmp_path, tamper):
    runner, jira, slack = make_runner(tmp_path)
    runner().run([observation()])
    fields = jira.issues['RHAI-1']['fields']
    fields['summary'] = 'Human edited title'
    if tamper == 'project':
        fields['project']['key'] = 'OTHER'
    elif tamper == 'issue_type':
        fields['issuetype'] = {'name': 'Task', 'id': '99999'}
    elif tamper == 'episode_label':
        fields['labels'] = [label for label in fields['labels'] if not label.startswith('gap-episode-')]
    else:
        jira.properties['RHAI-1'][tamper] = 'wrong'
    calls_before = list(jira.calls)
    result = runner('tampered').run([observation(oid='new-failure')])
    assert result['outbox'][0]['state'] != 'confirmed'
    assert len(jira.issues) == 1 and len(slack.calls) == 1
    assert jira.calls == calls_before


@pytest.mark.parametrize('source', ['## No H1', '# ', '# ' + 'x' * 251, '# [Linked title](https://example.test)'])
def test_invalid_title_fails_closed(monkeypatch, source):
    replace_template(monkeypatch, 'issue.md.j2', lambda _: source)
    with pytest.raises(ValueError):
        _summary(observation())


@pytest.mark.parametrize('category', ['build', 'conflict', 'feasibility'])
def test_jira_links_to_ownership_sources(category):
    markdown = notify_tools.render_markdown(observation(category=category))
    assert '/rhoai-component-data.yaml)' in markdown
    assert '/konflux-to-jira-component-map.yaml)' in markdown
    assert ('/automerge_team_mapping.yaml)' in markdown) == (category != 'build')


def test_slack_evidence_links_include_selected_evidence():
    sha = 'a' * 40
    obs = observation(evidence={
        'head-sha': sha, 'selected-run': {'head_sha': sha},
        'build_url': 'https://build.example/run/1',
        'pr_url': 'https://github.com/acme/repo/pull/1',
        'leader_url': 'https://github.com/acme/leader/pull/1',
    })
    jira_url = 'https://stage-redhat.atlassian.net/browse/RHAI-1'
    text = notify_tools.slack_message(obs, jira_url=jira_url)
    urls = re.findall(r'<(https://[^|>]+)\|[^>]+>', text)
    assert set(urls) == {
        obs['evidence']['build_url'], obs['evidence']['pr_url'],
        obs['evidence']['leader_url'], jira_url, f'https://github.com/acme/repo/commit/{sha}',
    }


@pytest.mark.parametrize('mask', range(32))
def test_optional_link_combinations_have_no_dangling_dots(mask):
    kinds = ['build', 'commit', 'feasibility', 'pr', 'leader']
    links = notify_tools._slack_links_context([])
    for index, kind in enumerate(kinds):
        if mask & (1 << index):
            links[kind] = {'url': f'https://example.test/{kind}', 'detail': 'aaaaaaa' if kind == 'commit' else None}
    links['jira'] = {'url': 'https://example.test/jira', 'detail': 'RHAI-1'}
    text = notify_tools.slack_template().render(
        status='failure', category='build', reply=False, jira_environment='stage',
        component='cpu', repository='acme/repo', build_time='', reason=None,
        owning_team=None, synthetic=False, links=links,
    )
    row = next(line for line in text.splitlines() if '<https://' in line)
    parts = row.split('·')
    assert len(parts) == sum(link is not None for link in links.values())
    assert all(re.fullmatch(r'\s*<https://[^|>]+\|[^>]+>\s*', part) for part in parts)
    assert set(re.findall(r'<(https://[^|>]+)\|[^>]+>', row)) == {
        link['url'] for link in links.values() if link
    }
