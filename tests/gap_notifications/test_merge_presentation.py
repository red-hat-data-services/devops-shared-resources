"""Pure optional links for structured merge evidence; no services or new timestamps."""
import pytest

from gap_notification_fakes import observation
from lib.gap_notifications.notify_tools import jira_target, slack_message

SHA = 'a' * 40


def merge_observation(category='feasibility'):
    return observation(category=category, repository='github.com/acme/repo', evidence={
        'source': 'github-merge-receipt', 'status': 'failure', 'repository': 'acme/repo',
        'head-sha': SHA, 'pr_url': 'https://github.com/acme/repo/pull/42',
        'merge-receipt': {'head-sha': SHA, 'run-head-sha': SHA, 'run-id': 123, 'attempt': 2},
        'commit_url': 'https://untrusted.example/commit', 'workflow_url': 'https://untrusted.example/run',
    })


def render(obs, *, environment='stage', reply=False):
    target = jira_target(environment)
    return slack_message(obs, jira_url=target.url + '/browse/RHAI-1', reply=reply, **target.options)


# Merge roots and replies must construct trusted revision links in either Jira environment.
@pytest.mark.parametrize('environment', ['stage', 'production'])
@pytest.mark.parametrize('reply', [False, True])
@pytest.mark.parametrize('category', ['conflict', 'feasibility'])
def test_merge_links_are_bound_and_ignore_url_overrides(environment, reply, category):
    text = render(merge_observation(category), environment=environment, reply=reply)
    assert f'<https://github.com/acme/repo/commit/{SHA}|Commit aaaaaaa>' in text
    expected = '<https://github.com/acme/repo/actions/runs/123/attempts/2|Feasibility run>'
    assert (expected in text) == (category == 'feasibility')
    assert '|Child PR>' in text and '|Jira RHAI-1>' in text
    assert 'untrusted.example' not in text
    assert 'Detected at' not in text and 'Build failed at' not in text


# Missing, malformed or inconsistent identities must suppress extra links without hiding Jira.
@pytest.mark.parametrize('field,value', [
    ('source', 'other'), ('status', 'success'), ('repository', 'other/repo'),
    ('repository', None), ('head-sha', 'aaaaaaa'), ('head-sha', {}),
    ('head-sha', '<!channel>'), ('pr_url', 'https://github.com/other/repo/pull/42'),
    ('pr_url', 'https://github.com/acme/repo/pull/42?redirect=evil'), ('pr_url', None),
])
def test_merge_identity_mismatch_omits_optional_links(field, value):
    obs = merge_observation()
    obs['evidence'][field] = value
    text = render(obs)
    assert '|Commit ' not in text and '|Feasibility run>' not in text
    assert '|Jira RHAI-1>' in text


# Arbitrary hosts, path traversal and markup must not become repository-derived URLs.
@pytest.mark.parametrize('repository', ['gitlab.com/acme/repo', 'acme/..', 'acme/repo/extra', '<!here>/repo', None])
def test_unsafe_repository_cannot_supply_merge_links(repository):
    obs = merge_observation()
    obs['repository'] = repository
    assert '|Commit ' not in render(obs) and '|Feasibility run>' not in render(obs)


# Invalid or stale workflow execution identities omit only the workflow link, not a valid commit.
@pytest.mark.parametrize('field,value', [('run-id', 0), ('run-id', True), ('run-id', '123'),
    ('attempt', None), ('attempt', True), ('attempt', -1), ('head-sha', 'b' * 40), ('run-head-sha', None)])
def test_workflow_link_requires_current_revision_and_real_attempt(field, value):
    obs = merge_observation()
    obs['evidence']['merge-receipt'][field] = value
    text = render(obs)
    assert '|Feasibility run>' not in text and '|Commit aaaaaaa>' in text


# Build messages must not inherit workflow links from unrelated merge receipt fields.
def test_merge_links_do_not_apply_to_build_category():
    obs = merge_observation('build')
    text = render(obs)
    assert '|Feasibility run>' not in text


# The same bound revision links remain useful in recovery replies without claiming build health.
def test_recovery_links_use_same_validated_evidence():
    obs = merge_observation()
    obs['status'] = obs['evidence']['status'] = 'success'
    text = render(obs, reply=True)
    assert 'Feasibility restored' in text and '|Commit aaaaaaa>' in text and '|Feasibility run>' in text
    assert 'Successful build' not in text
