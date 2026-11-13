"""Templates own presentation wording; Python retains validation and transport safety."""
from importlib.resources import files

import pytest

from gap_notification_fakes import observation, make_runner
from lib.gap_notifications import notify_tools
from lib.gap_notifications.dispatch_incidents import _summary
from lib.gap_notifications.lookup_tools import inspect_issue
from lib.gap_notifications.ownership import OwnershipPolicy


@pytest.mark.parametrize('environment,prefix', [('stage', 'JIRA STAGE - '), ('production', '')])
@pytest.mark.parametrize('category', ['build', 'conflict', 'feasibility'])
@pytest.mark.parametrize('repository', ['acme/repo', 'acme/[repo]_*{{ literal }}'])
@pytest.mark.parametrize('component', [None, 'cpu', 'cpu_[test]{{ literal }}'])
def test_first_h1_supplies_jira_title_with_literal_escaping(environment, prefix, category, repository, component):
    obs = observation(category=category, repository=repository)
    if component is None:
        obs.pop('component', None)
    else:
        obs['component'] = component
    context = 'merge feasibility' if category in ['conflict', 'feasibility'] else component or 'Unavailable'
    expected = f'{prefix}Promotion Gate Failure in {repository} ({context})'
    assert _summary(obs, jira_environment=environment) == expected
    body = notify_tools.description(obs, jira_environment=environment)
    title = next(node for node in body['content'] if node['type'] == 'heading' and node['attrs']['level'] == 1)
    assert ''.join(node['text'] for node in title['content']) == expected


@pytest.mark.parametrize('environment', ['stage', 'production'])
@pytest.mark.parametrize('category', ['build', 'conflict', 'feasibility'])
@pytest.mark.parametrize('component', [None, 'cpu'])
def test_build_explanation_and_component_details_are_build_only(environment, category, component):
    obs = observation(category=category)
    if component is None:
        obs.pop('component', None)
    else:
        obs['component'] = component
    markdown = notify_tools.render_markdown(obs, jira_environment=environment)
    body = notify_tools.description(obs, jira_environment=environment)
    notify_tools.JiraNotifier._validate_adf(body)
    explanation = ('This incident tracks a failed or timed-out component build required by the promotion gate. '
                   'Inspect the linked build logs and reported reason to diagnose the failure.')
    headings = [node['content'][0]['text'] for node in body['content'] if node['type'] == 'heading']
    assert ('## Component build failure' in markdown) == (category == 'build')
    assert ('Component build failure' in headings) == (category == 'build')
    assert (explanation in markdown) == (category == 'build')
    component_lines = [line for line in markdown.splitlines() if line.startswith('- Component:')]
    assert component_lines == ([f'- Component: {component or "Unavailable"}'] if category == 'build' else [])
    details = [item['content'][0]['content'][0]['text']
               for node in body['content'] if node['type'] == 'bulletList' for item in node['content']]
    assert any(text.startswith('Component:') for text in details) == (category == 'build')
    assert 'TODO' not in markdown


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
    assert '[Slack teams](https://gitlab.cee.redhat.com/' in markdown
    assert '[Jira components](https://gitlab.cee.redhat.com/' in markdown
    assert ('[Repository ownership](https://github.com/' in markdown) == (category != 'build')


@pytest.mark.parametrize('category', ['build', 'conflict', 'feasibility'])
def test_ownership_text_comes_from_plain_template(category):
    heading, owner, mappings = notify_tools.slack_ownership('devops', category)
    assert heading == '🔴 *GAP · Promotion gate failure*'
    assert owner == '*Owning team:* `@devops`'
    assert mappings.startswith('Ownership mappings:')
    assert ('|Repository ownership>' in mappings) == (category != 'build')


@pytest.mark.parametrize('change', ['extra-line', 'oversized', 'replace-content'])
def test_invalid_ownership_template_changes_fail_closed(monkeypatch, change):
    def alter(source):
        if change == 'extra-line':
            return source.replace('*Owning team:* `@{{ owning_team }}`',
                                  '*Owning team:* `@{{ owning_team }}`\nUnexpected ownership text')
        if change == 'oversized':
            return source.replace('*Owning team:*', 'x' * 3000)
        return source.replace('🔴 *GAP · Promotion gate failure*',
                              "{% if not owning_team %}🔴 *GAP · Promotion gate failure*{% endif %}")
    replace_template(monkeypatch, 'slack.md.j2', alter)
    with pytest.raises(ValueError):
        notify_tools.slack_ownership('devops', 'build')


def test_slack_complete_preview_follows_display_order():
    preview = notify_tools.slack_template().render(
        status='failure', category='build', reply=False, jira_environment='stage',
        component='cpu', repository='acme/repo', build_time='Unavailable',
        reason=None, links=notify_tools._slack_links_context([]), synthetic=False,
        owning_team='devops',
    )
    assert preview.index('Promotion gate failure') < preview.index('*Component:*')
    assert preview.index('*Component:*') < preview.index('*Owning team:*')
    assert preview.index('*Owning team:*') < preview.index('*Repository:*')
    assert preview.index('*Repository:*') < preview.index('Ownership mappings:')


def test_slack_evidence_links_are_explicit_dot_separated():
    sha = 'a' * 40
    obs = observation(evidence={
        'head-sha': sha, 'selected-run': {'head_sha': sha},
        'build_url': 'https://build.example/run/1',
        'pr_url': 'https://github.com/acme/repo/pull/1',
        'leader_url': 'https://github.com/acme/leader/pull/1',
    })
    text = notify_tools.slack_message(obs, jira_url='https://stage-redhat.atlassian.net/browse/RHAI-1')
    assert [line for line in text.splitlines() if line.startswith('<https://')] == [' · '.join([
        '<https://build.example/run/1|Build logs>',
        f'<https://github.com/acme/repo/commit/{sha}|Commit aaaaaaa>',
        '<https://github.com/acme/repo/pull/1|Child PR>',
        '<https://stage-redhat.atlassian.net/browse/RHAI-1|Jira RHAI-1>',
        '<https://github.com/acme/leader/pull/1|GAP Leader>',
    ])]


@pytest.mark.parametrize('mask', range(32))
def test_optional_link_combinations_have_no_dangling_dots(mask):
    kinds = ['build', 'commit', 'feasibility', 'pr', 'leader']
    labels = {'build': 'Build logs', 'commit': 'Commit aaaaaaa',
              'feasibility': 'Feasibility Workflow', 'pr': 'Child PR', 'leader': 'GAP Leader', 'jira': 'Jira RHAI-1'}
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
    order = ['build', 'commit', 'feasibility', 'pr', 'jira', 'leader']
    expected = [f"<{links[kind]['url']}|{labels[kind]}>" for kind in order if links[kind]]
    assert row == ' · '.join(expected)


def test_slack_wording_and_ownership_are_editable_in_one_template(monkeypatch):
    replace_template(monkeypatch, 'slack.md.j2', lambda source: source.replace(
        'Promotion gate failure', 'Build needs attention').replace('Owning team:', 'Responsible team:').replace(
        'Ownership mappings:', 'Ownership sources:').replace('Build logs', 'Build evidence'))
    obs = observation(evidence={'build_url': 'https://build.example/run/1'})
    text = notify_tools.slack_message(obs, jira_url='https://stage-redhat.atlassian.net/browse/RHAI-1')
    policy = OwnershipPolicy({'mappings': {}}, {'components': {'default': {'slack_team_handle': 'devops'}}})
    blocks = policy.blocks(obs, text, root=True)
    assert 'Build needs attention' in text
    assert '|Build evidence>' in text
    assert blocks[1]['text'] == {'type': 'mrkdwn', 'text': '*Responsible team:* `@devops`\n', 'verbatim': True}
    assert blocks[-1]['elements'][0]['text'].startswith('Ownership sources:')
    assert all(block['text']['verbatim'] for block in blocks[:-1])
    for root, status in [(False, 'failure'), (True, 'success'), (False, 'success')]:
        other = policy.blocks(dict(obs, status=status), text, root=root)
        assert all(block['text']['verbatim'] for block in other)
        assert all('Responsible team:' not in block['text']['text'] for block in other)
