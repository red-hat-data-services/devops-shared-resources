"""Templates own presentation wording; Python retains validation and transport safety."""
from importlib.resources import files
import re

import pytest

from gap_notification_fakes import observation, make_runner
from lib.gap_notifications import notify_tools
from lib.gap_notifications.dispatch_incidents import _summary
from lib.gap_notifications.lookup_tools import inspect_issue


@pytest.mark.parametrize('environment', ['stage', 'production'])
def test_issue_summary_constructs_title_with_raw_component(environment):
    obs = observation(repository='acme/[repo]_*{{ literal }}', component='cpu_[test]')
    title = _summary(obs, jira_environment=environment)
    expected = (
        'JIRA STAGE - Promotion Gate PR Build Failure in cpu_[test]'
        if environment == 'stage'
        else 'Promotion Gate PR Build Failure in cpu_[test]'
    )
    assert title == expected
    # Template body should have no H1; the title lives only in the Jira summary.
    body = notify_tools.description(obs, jira_environment=environment)
    assert not any(node['type'] == 'heading' and node['attrs']['level'] == 1
                   for node in body['content'])


def test_overlong_rendered_title_is_rejected():
    with pytest.raises(ValueError, match='1–250 characters'):
        _summary(observation(component='x' * 250))


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


def test_actionable_summary_and_key_links_appear_before_technical_details():
    obs = observation(evidence={
        'pr_url': 'https://github.com/acme/repo/pull/1',
        'build_url': 'https://build.example/run/1',
    })
    markdown = notify_tools.render_markdown(
        obs, discussion_url='https://slack.example/archives/C123/p456'
    )
    what_happened = markdown.index('## What happened')
    key_links = markdown.index('## Key links')
    slack_links = markdown.index('## Slack discussion')
    what_to_do = markdown.index('## What to do next')
    technical = markdown.index('## Failure Details')
    assert what_happened < key_links < slack_links < what_to_do < technical
    key_section = markdown[key_links:slack_links]
    assert '[Child PR](https://github.com/acme/repo/pull/1)' in key_section
    assert '[Build logs or PipelineRun](https://build.example/run/1)' in key_section
    assert '[Open Slack discussion](https://slack.example/archives/C123/p456)' in markdown[slack_links:what_to_do]


def test_title_is_constructed_from_observation_not_template(monkeypatch):
    replace_template(monkeypatch, 'issue.md.j2', lambda source: '# Custom title\n' + source)
    # Template H1 no longer controls the Jira summary.
    assert _summary(observation()) == 'JIRA STAGE - Promotion Gate PR Build Failure in cpu'


@pytest.mark.parametrize('category', ['build', 'conflict', 'feasibility'])
@pytest.mark.parametrize('title_change', ['human', 'legacy'])
def test_title_changes_preserve_incident_updates_recovery_and_replay(tmp_path, monkeypatch, category, title_change):
    runner, jira, slack = make_runner(tmp_path)
    runner().run([observation(category=category)])
    if title_change == 'legacy':
        jira.issues['RHAI-1']['fields']['summary'] = f'[GAP STAGING TEST] {category} failure: acme/repo'
    else:
        jira.issues['RHAI-1']['fields']['summary'] = 'Title edited by a human'
    report = inspect_issue(jira, 'RHAI-1')
    assert report['property'] == 'valid' and report['identity_match']

    # Title-based dedup: changed title means old issue is not found,
    # so a new issue is created for the new failure event
    updated = runner('update').run([observation(category=category, oid='new-failure')])
    assert updated['outbox'][0]['state'] == 'confirmed'
    assert len(jira.issues) == 2

    recovered = runner('recovery').run([observation('success', 'recovered', category=category)])
    assert recovered['outbox'][0]['state'] == 'confirmed'
    assert jira.closed('RHAI-2')


@pytest.mark.parametrize('tamper', ['scope', 'incident_key', 'episode_label'])
def test_identity_tampering_is_detected_with_matching_title(tmp_path, tamper):
    runner, jira, slack = make_runner(tmp_path)
    runner().run([observation()])
    fields = jira.issues['RHAI-1']['fields']
    # Title-based dedup: keep original summary so the issue is found by find_by_summary
    if tamper == 'episode_label':
        fields['labels'] = [label for label in fields['labels'] if not label.startswith('gap-episode-')]
    else:
        jira.properties['RHAI-1'][tamper] = 'wrong'
    calls_before = list(jira.calls)
    result = runner('tampered').run([observation(oid='new-failure')])
    assert result['outbox'][0]['state'] != 'confirmed'
    assert len(jira.issues) == 1 and len(slack.calls) == 1
    assert jira.calls == calls_before


# Title-based dedup: when _verify_issue fails for a title-matched issue (e.g.
# tampered project or issue type), the issue is skipped and a new one is created
# instead of holding the entire dispatch.
@pytest.mark.parametrize('tamper', ['project', 'issue_type'])
def test_identity_tampering_skips_unverifiable_issue(tmp_path, tamper):
    runner, jira, slack = make_runner(tmp_path)
    runner().run([observation()])
    fields = jira.issues['RHAI-1']['fields']
    if tamper == 'project':
        fields['project']['key'] = 'OTHER'
    elif tamper == 'issue_type':
        fields['issuetype'] = {'name': 'Task', 'id': '99999'}
    result = runner('tampered').run([observation(oid='new-failure')])
    assert result['outbox'][0]['state'] == 'confirmed'
    assert len(jira.issues) == 2


@pytest.mark.parametrize('category,field,context', [
    ('build', 'component', 'PR Build'),
    ('feasibility', 'repository', 'merge-feasibility'),
    ('conflict', 'repository', 'merge-conflict'),
])
def test_issue_summary_per_category(category, field, context):
    obs = observation(category=category, **{field: 'my-comp'})
    title = _summary(obs)
    assert title == f'JIRA STAGE - Promotion Gate {context} Failure in my-comp'


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
