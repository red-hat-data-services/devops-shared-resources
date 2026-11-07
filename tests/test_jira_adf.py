"""Pure formatting and legacy offline entry points remain credential-free."""
import copy
import inspect

import pytest

from lib import jira_adf
from lib.gap_notifications import notify_tools as gap_presentation
from lib.gap_notifications import notify_tools as gap_jira


# Restricted Markdown must produce the exact supported ADF structure through both entry points.
def test_restricted_markdown_exact_shape():
    assert inspect.isfunction(jira_adf.validate_adf)
    body = jira_adf.markdown_to_adf('# Header\n\nText `code` [Link](https://example.test/a)\n- One\n- Two')
    assert body == {'type': 'doc', 'version': 1, 'content': [
        {'type': 'heading', 'attrs': {'level': 1}, 'content': [{'type': 'text', 'text': 'Header'}]},
        {'type': 'paragraph', 'content': [
            {'type': 'text', 'text': 'Text '},
            {'type': 'text', 'text': 'code', 'marks': [{'type': 'code'}]},
            {'type': 'text', 'text': ' '},
            {'type': 'text', 'text': 'Link', 'marks': [{'type': 'link', 'attrs': {'href': 'https://example.test/a'}}]}]},
        {'type': 'bulletList', 'content': [
            {'type': 'listItem', 'content': [{'type': 'paragraph', 'content': [{'type': 'text', 'text': 'One'}]}]},
            {'type': 'listItem', 'content': [{'type': 'paragraph', 'content': [{'type': 'text', 'text': 'Two'}]}]}]}]}
    jira_adf.validate_adf(body)
    assert gap_presentation.markdown_to_adf('# Header\n\nText `code` [Link](https://example.test/a)\n- One\n- Two') == body


# Unsupported Markdown syntax must remain rejected by both pure and GAP-facing converters.
@pytest.mark.parametrize('markdown', ['**strong**', '_em_', '> quote', '1. numbered', '```fence```', '  nested', '[bad](http://example.test)', '<html>', '---', 'a | b', '**bold**', '```code```', '1. ordered', '+ unordered', '##', '|table|'])
def test_no_new_markdown_syntax(markdown):
    with pytest.raises(ValueError):
        jira_adf.markdown_to_adf(markdown)
    with pytest.raises(ValueError):
        gap_presentation.markdown_to_adf(markdown)


# Pure escaping must remain policy-free while the GAP wrapper applies defaults and size bounds.
def test_pure_escaping_does_not_own_gap_defaults_or_bounds():
    assert jira_adf.escape_literal('* [] _') == r'\* \[\] \_'
    assert jira_adf.escape_literal('') == ''
    assert jira_adf.escape_literal('x' * 2001) == 'x' * 2001
    assert gap_presentation.literal(None) == 'Unavailable'
    assert gap_presentation.literal('  a\n b ') == 'a b'
    with pytest.raises(ValueError):
        gap_presentation.literal('x' * 2001)


# GAP compatibility entry points must enforce their URL bound without restricting the pure
# converter.
def test_legacy_url_bounds_apply_to_direct_markdown_and_inline_exports():
    url = 'https://example.test/' + 'x' * 4096
    assert jira_adf.https_url(url) == url
    assert jira_adf.markdown_to_adf(f'[link]({url})')
    for operation, argument in [(gap_presentation.https_url, url),
        (gap_presentation.markdown_to_adf, f'[link]({url})'),
        (gap_presentation._inline, f'[link]({url})')]:
        with pytest.raises(ValueError):
            operation(argument)


# Pure URL validation must reject unsafe links independently of service clients.
@pytest.mark.parametrize('url', ['http://example.test', 'https://user:pass@example.test',
    'https://example.test/a b', 'https://example.test/[x]', 'https://example.test/`code`'])
def test_link_safety_is_pure(url):
    with pytest.raises(ValueError):
        jira_adf.https_url(url)


# ADF signatures must preserve link targets and reject unsupported document structures.
def test_signature_preserves_link_target_and_supported_structure():
    original = jira_adf.markdown_to_adf('[link](https://example.test/one)')
    changed = copy.deepcopy(original)
    changed['content'][0]['content'][0]['marks'][0]['attrs']['href'] = 'https://example.test/two'
    assert jira_adf.adf_signature(original) != jira_adf.adf_signature(changed)
    assert gap_jira.JiraNotifier._adf_signature(original) == jira_adf.adf_signature(original)
    unsupported = {'type': 'doc', 'version': 1, 'content': [{'type': 'panel'}]}
    for validate in (jira_adf.validate_adf, gap_jira.JiraNotifier._validate_adf):
        with pytest.raises(ValueError):
            validate(unsupported)


# Pure compatibility helpers must work without constructing Jira or Slack clients.
def test_legacy_pure_entrypoints_never_construct_clients(monkeypatch):
    def forbidden(*a, **kw):
        raise AssertionError('offline formatting must not construct an authenticated client')
    monkeypatch.setattr(gap_jira, 'JiraClient', forbidden)
    obs = {'repository': 'acme/repo', 'category': 'build', 'source_branch': 'main',
           'destination_branch': 'stable', 'component': 'cpu', 'observation_id': 'one', 'evidence': {}}
    rendered = gap_presentation.description(obs)
    assert rendered['type'] == 'doc'
    gap_jira.JiraNotifier._validate_adf(rendered)
    gap_jira.JiraNotifier._adf_signature(rendered)
    assert gap_jira.JiraNotifier._validate_labels(['gap-test']) == ['gap-test']
    assert gap_jira.JiraNotifier._issue_key('RHAI-1') == 'RHAI-1'
    assert gap_jira.JiraNotifier._validation_details({'errors': {'summary': 'hidden'}}) == (('summary',), ())
    assert gap_jira.JiraNotifier._retry_after({'Retry-After': '2.5'}) == 2.5
    assert gap_jira.JiraNotifier._safe_error({'error': 'BAD_INPUT'}) == 'BAD_INPUT'
    with pytest.raises(ValueError):
        gap_jira.JiraNotifier._issue_key('OTHER-1')
    with pytest.raises(ValueError):
        gap_jira.JiraNotifier._validate_labels(['bad label'])
    with pytest.raises(ValueError):
        gap_jira.JiraNotifier._normalize_notification_state({'too_big': 'x' * (24 * 1024)})
