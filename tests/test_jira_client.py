"""Reusable Jira operations tested exclusively with injected fake sessions."""
import copy
import json

import pytest
import requests

from lib import gap_notification_jira as gap
from lib.jira_client import (JiraClient, JiraAmbiguousError, JiraPermanentError,
    JiraRateLimitError, JiraTransientError, JiraValidationError)
from test_gap_notification_jira import FakeResponse, FakeSession

SITE = 'https://example-jira.test'


def client(session):
    return JiraClient(SITE, 'fake@example.test', 'FAKE_TOKEN', session=session, timeout=7)


def test_other_site_project_type_and_unrestricted_fields_atomic_properties():
    session = FakeSession(responses={'post': [FakeResponse({'key': 'OTHER-2'}, 201)]})
    api = client(session)
    # This unsupported-by-GAP rich text is deliberately opaque to the client.
    fields = {'project': {'key': 'OTHER'}, 'issuetype': {'name': 'Task'},
              'description': {'type': 'doc', 'content': [{'type': 'panel'}]}, 'customfield_1': ['anything']}
    properties = [{'key': 'other.app.v2', 'value': ['not', 'GAP metadata']}]
    before = copy.deepcopy((fields, properties))
    assert api.create_issue(fields, properties=properties) == {'key': 'OTHER-2', 'url': SITE + '/browse/OTHER-2'}
    assert len(session.calls) == 1  # No follow-up PUT or GAP readback.
    method, url, kwargs = session.calls[0]
    assert method == 'post' and url == SITE + '/rest/api/3/issue'
    assert kwargs['json'] == {'fields': fields, 'properties': properties}
    assert (fields, properties) == before
    assert kwargs['auth'] == ('fake@example.test', 'FAKE_TOKEN')
    assert kwargs['timeout'] == 7 and kwargs['allow_redirects'] is False
    assert session.trust_env is False
    assert 'FAKE_TOKEN' not in repr(api) and 'fake@example.test' not in repr(api)


def test_get_requested_fields_and_pure_acknowledgements():
    issue = {'key': 'OTHER-1', 'fields': {'customfield_2': 'value'}}
    session = FakeSession(responses={'get': [FakeResponse(issue)], 'put': [FakeResponse(None, 204)]})
    api = client(session)
    assert api.get_issue('OTHER-1', fields=['customfield_2']) == issue
    assert session.calls[0][2]['params'] == {'fields': 'customfield_2'}
    assert api.update_issue('OTHER-1', fields={'description': 'plain'}, update={'labels': [{'add': 'human'}]}) is None
    assert session.calls[1][2]['json'] == {'fields': {'description': 'plain'}, 'update': {'labels': [{'add': 'human'}]}}
    assert len(session.calls) == 2  # Generic update does not assert readback guarantees.


def test_arbitrary_property_name_json_and_missing_property():
    envelope = {'key': 'other/app.v2', 'value': ['any', None, 3]}
    session = FakeSession(responses={'get': [FakeResponse(envelope), FakeResponse({}, 404)],
                                     'put': [FakeResponse(None, 204)]})
    api = client(session)
    assert api.get_issue_property('OTHER-1', 'other/app.v2') == envelope
    assert session.calls[0][1].endswith('/properties/other%2Fapp.v2')
    assert api.put_issue_property('OTHER-1', 'other/app.v2', [1, None]) is None
    assert session.calls[1][2]['json'] == [1, None]
    assert api.get_issue_property('OTHER-1', 'other/app.v2') is None


def test_property_null_prepared_body_and_get_no_body(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('network or credential discovery is forbidden')

    monkeypatch.setattr(requests.Session, 'send', forbidden)
    monkeypatch.setattr(requests.sessions, 'get_netrc_auth', forbidden)

    class PreparingSession(requests.Session):
        def __init__(self):
            super().__init__()
            self.prepared = []

        def request(self, method, url, **kwargs):
            # Real requests serialization, stopped BEFORE Session.send/network.
            selected = {k: kwargs[k] for k in ('params', 'json', 'data', 'headers', 'auth') if k in kwargs}
            self.prepared.append(requests.Request(method, url, **selected).prepare())
            if method.lower() == 'get':
                return FakeResponse({'key': 'OTHER-1', 'fields': {}})
            return FakeResponse(None, 204)

    session = PreparingSession()
    api = client(session)
    assert api.put_issue_property('OTHER-1', 'other.app', None) is None
    null_request = session.prepared[-1]
    assert null_request.body == 'null'
    assert null_request.headers['Content-Type'] == 'application/json'
    api.get_issue('OTHER-1')
    assert session.prepared[-1].body is None
    assert 'Content-Type' not in session.prepared[-1].headers
    for value in ({'v': 1}, [1, None], False, 0, 'text'):
        api.put_issue_property('OTHER-1', 'other.app', value)
        assert json.loads(session.prepared[-1].body) == value
        assert session.prepared[-1].headers['Content-Type'] == 'application/json'
    assert session.trust_env is False


def test_generic_transitions_are_not_done_only():
    transition = {'id': '9', 'to': {'statusCategory': {'key': 'indeterminate'}}}
    session = FakeSession(responses={'get': [FakeResponse({'transitions': [transition]})],
                                     'post': [FakeResponse(None, 204)]})
    api = client(session)
    assert api.transitions('OTHER-1') == [transition]
    assert api.apply_transition('OTHER-1', '9', fields={'customfield_3': 'value'}) is None
    assert session.calls[1][2]['json'] == {'transition': {'id': '9'}, 'fields': {'customfield_3': 'value'}}


def test_search_is_caller_jql_fields_and_complete():
    first, second = {'key': 'OTHER-1', 'fields': {}}, {'key': 'ELSE-2', 'fields': {}}
    session = FakeSession(responses={'post': [
        FakeResponse({'issues': [first], 'isLast': False, 'nextPageToken': 'next'}),
        FakeResponse({'issues': [second], 'isLast': True})]})
    assert client(session).search('project in (OTHER, ELSE)', fields=['customfield_9']) == [first, second]
    assert session.calls[0][2]['json'] == {'jql': 'project in (OTHER, ELSE)', 'fields': ['customfield_9'], 'maxResults': 50}
    assert session.calls[1][2]['json']['nextPageToken'] == 'next'


def test_search_omitted_fields_explicitly_requests_summary_on_all_pages():
    first = {'key': 'OTHER-1', 'fields': {'summary': 'First'}}
    second = {'key': 'OTHER-2', 'fields': {'summary': 'Second'}}
    session = FakeSession(responses={'post': [
        FakeResponse({'issues': [first], 'isLast': False, 'nextPageToken': 'next'}),
        FakeResponse({'issues': [second], 'isLast': True})]})
    assert client(session).search('project = OTHER') == [first, second]
    assert len(session.calls) == 2
    for _, _, kwargs in session.calls:
        assert kwargs['json']['fields'] == ['summary']
    assert session.calls[1][2]['json']['nextPageToken'] == 'next'


@pytest.mark.parametrize('pages', [
    [{'issues': [], 'isLast': False}],
    [{'issues': [], 'isLast': 'yes'}],
    [{'issues': [], 'isLast': True, 'nextPageToken': 'unexpected'}],
    [{'issues': [{'key': 'OTHER-1', 'fields': {}}], 'isLast': False, 'nextPageToken': 'a'},
     {'issues': [{'key': 'OTHER-1', 'fields': {}}], 'isLast': True}],
    [{'issues': [{'key': 'OTHER-1', 'fields': {}}], 'isLast': False, 'nextPageToken': 'a'},
     {'issues': [{'key': 'OTHER-2', 'fields': {}}], 'isLast': False, 'nextPageToken': 'a'}],
])
def test_search_never_returns_partial_results(pages):
    session = FakeSession(responses={'post': [FakeResponse(p) for p in pages]})
    with pytest.raises(JiraAmbiguousError):
        client(session).search('anything')
    assert len(session.calls) == len(pages)


def test_pagination_budget_fails_closed():
    pages = [FakeResponse({'issues': [{'key': f'OTHER-{i}', 'fields': {}}],
             'isLast': False, 'nextPageToken': str(i)}) for i in range(20)]
    session = FakeSession(responses={'post': pages})
    with pytest.raises(JiraAmbiguousError):
        client(session).search('anything')
    assert len(session.calls) == 20


@pytest.mark.parametrize('method,args,verb,error_type', [
    ('get_issue', ('OTHER-1',), 'get', JiraTransientError),
    ('search', ('project = OTHER',), 'post', JiraTransientError),
    ('create_issue', ({'summary': 'x'},), 'post', JiraAmbiguousError),
    ('update_issue', ('OTHER-1',), 'put', JiraAmbiguousError),
    ('put_issue_property', ('OTHER-1', 'app.state', {}), 'put', JiraAmbiguousError),
    ('apply_transition', ('OTHER-1', '9'), 'post', JiraAmbiguousError),
])
def test_timeout_classification_is_semantic_and_never_retries(method, args, verb, error_type):
    session = FakeSession(side_effects={verb: [requests.Timeout('FAKE_TOKEN fake@example.test')]})
    with pytest.raises(error_type) as raised:
        getattr(client(session), method)(*args)
    assert len(session.calls) == 1
    assert 'FAKE_TOKEN' not in str(raised.value) and 'fake@example.test' not in str(raised.value)


@pytest.mark.parametrize('status,body,error_type', [
    (429, {'error': 'rate_limited'}, JiraRateLimitError),
    (400, {'errors': {'summary': 'FAKE_TOKEN'}, 'errorMessages': ['BAD_INPUT', 'FAKE TOKEN']}, JiraValidationError),
    (403, {'message': 'FAKE_TOKEN'}, JiraPermanentError),
    (503, {'message': 'FAKE_TOKEN'}, JiraAmbiguousError),
    (302, {}, JiraAmbiguousError),
])
def test_rejections_and_uncertain_status_metadata(status, body, error_type):
    session = FakeSession(responses={'post': [FakeResponse(body, status, {'Retry-After': '2.5'})]})
    with pytest.raises(error_type) as raised:
        client(session).create_issue({'summary': 'x'})
    assert len(session.calls) == 1 and 'FAKE_TOKEN' not in str(raised.value)
    if status == 429:
        assert raised.value.retry_after == 2.5 and raised.value.status_code == 429
    if status == 400:
        assert raised.value.field_errors == ('summary',) and raised.value.error_codes == ('BAD_INPUT',)


def test_gap_facade_honors_mutated_public_session_auth_timeout():
    original = FakeSession()
    replacement = FakeSession(responses={'get': [FakeResponse({'key': 'RHAI-1', 'fields': {}})]})
    facade = gap.JiraNotifier(gap.STAGING_JIRA_ORIGIN, 'RHAI', 'Bug',
                              'old@example.test', 'OLD_FAKE', session=original)
    facade.session, facade.email, facade.token, facade.timeout = replacement, 'new@example.test', 'NEW_FAKE', 19
    assert facade.get_issue('RHAI-1')['key'] == 'RHAI-1'
    assert not original.calls and len(replacement.calls) == 1
    assert replacement.calls[0][2]['auth'] == ('new@example.test', 'NEW_FAKE')
    assert replacement.calls[0][2]['timeout'] == 19
    assert replacement.trust_env is False


@pytest.mark.parametrize('email,token,timeout', [('', 'fake', 1), ('fake', '', 1), ('fake', 'fake', 0)])
def test_constructor_validation_is_immediate(email, token, timeout):
    session = FakeSession()
    with pytest.raises(ValueError):
        JiraClient(SITE, email, token, timeout=timeout, session=session)
    with pytest.raises(ValueError):
        gap.JiraNotifier(gap.STAGING_JIRA_ORIGIN, 'RHAI', 'Bug', email, token,
                         timeout=timeout, session=session)
    assert not session.calls


def test_legacy_error_exports_have_same_identity():
    assert gap.JiraAmbiguousError is JiraAmbiguousError
    assert gap.JiraPermanentError is JiraPermanentError
    assert issubclass(gap.JiraTerminalIssueError, JiraPermanentError)


@pytest.mark.parametrize('origin', ['http://example.test', 'https://user:secret@example.test',
    'https://example.test:443', 'https://example.test/path', 'https://example.test?q=1'])
def test_generic_origin_safety_without_staging_restriction(origin):
    with pytest.raises(ValueError):
        JiraClient(origin, 'fake', 'fake', session=FakeSession())


def test_wrong_issue_key_and_malformed_property_fail_closed():
    session = FakeSession(responses={'get': [FakeResponse({'key': 'OTHER-2', 'fields': {}}),
                                            FakeResponse({'key': 'wrong', 'value': {}})]})
    api = client(session)
    with pytest.raises(JiraAmbiguousError):
        api.get_issue('OTHER-1')
    with pytest.raises(JiraAmbiguousError) as raised:
        api.get_issue_property('OTHER-1', 'app.state')
    assert raised.value.key == 'OTHER-1'
