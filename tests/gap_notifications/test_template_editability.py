"""Templates own presentation wording; Python retains validation and transport safety."""
from importlib.resources import files

import pytest

from gap_notification_fakes import observation
from lib.gap_notifications import notify_tools
from lib.gap_notifications.dispatch_incidents import _summary
from lib.gap_notifications.ownership import OwnershipPolicy


@pytest.mark.parametrize('environment,banner', [('stage', 'GAP STAGING TEST'), ('production', 'GAP PRODUCTION')])
@pytest.mark.parametrize('category', ['build', 'conflict', 'feasibility'])
@pytest.mark.parametrize('repository', ['acme/repo', 'acme/' + 'x' * 300, 'acme/[repo]_*{{ literal }}'])
def test_first_h1_preserves_existing_jira_titles(environment, banner, category, repository):
    obs = observation(category=category, repository=repository)
    expected = f'[{banner}] {category} failure: {repository}'[:250]
    assert _summary(obs, jira_environment=environment) == expected
    body = notify_tools.description(obs, jira_environment=environment)
    title = next(node for node in body['content'] if node['type'] == 'heading' and node['attrs']['level'] == 1)
    assert ''.join(node['text'] for node in title['content']) == expected


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


def test_slack_wording_and_ownership_are_editable_in_one_template(monkeypatch):
    replace_template(monkeypatch, 'slack.md.j2', lambda source: source.replace(
        'Build failed', 'Build needs attention').replace('Owning team:', 'Responsible team:').replace(
        'Ownership mappings:', 'Ownership sources:').replace('Failed build', 'Build evidence'))
    obs = observation(evidence={'build_url': 'https://build.example/run/1'})
    text = notify_tools.slack_message(obs, jira_url='https://stage-redhat.atlassian.net/browse/RHAI-1')
    policy = OwnershipPolicy({'mappings': {}}, {'components': {'default': {'slack_team_handle': 'devops'}}})
    blocks = policy.blocks(obs, text, root=True)
    assert 'Build needs attention' in text
    assert '|Build evidence>' in text
    assert blocks[1]['text'] == {'type': 'mrkdwn', 'text': '*Responsible team:* @devops\n', 'verbatim': False}
    assert blocks[-1]['elements'][0]['text'].startswith('Ownership sources:')
    assert all(block['text']['verbatim'] for block in blocks[:-1] if block is not blocks[1])
    for root, status in [(False, 'failure'), (True, 'success'), (False, 'success')]:
        other = policy.blocks(dict(obs, status=status), text, root=root)
        assert all(block['text']['verbatim'] for block in other)
        assert all('Responsible team:' not in block['text']['text'] for block in other)
