"""Actual pinned JiraCloud control flow, with final Requests preparation offline."""
import json
import logging
import socket
from importlib.metadata import version
from urllib.parse import urlsplit

import pytest
import requests
from atlassian import JiraCloud

from lib.jira_client import (
    JiraClient, JiraAmbiguousError, JiraRateLimitError, JiraTransientError,
    JiraValidationError, JiraPermanentError,
)

SITE = 'https://jira.example.test'
CANARY = 'SDK_SECRET_CANARY'


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('network/discovery forbidden')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(requests.Session, 'send', forbidden)
    monkeypatch.setattr(requests.sessions, 'get_netrc_auth', forbidden)
    monkeypatch.setenv('HTTPS_PROXY', 'http://SYNTHETIC_PROXY.invalid:9')


class WireSession(requests.Session):
    def __init__(self, replies):
        super().__init__()
        self.replies = list(replies)
        self.wire = []
        self.prepared = []

    def prepare_request(self, request):
        prepared = super().prepare_request(request)
        self.prepared.append(prepared)
        return prepared

    def send(self, request, **kwargs):
        self.wire.append((request, kwargs))
        result = self.replies.pop(0)
        if isinstance(result, Exception):
            raise result
        status, body, headers = result
        response = requests.Response()
        response.status_code = status
        response._content = body
        response.headers.update(headers)
        response.request = request
        response.url = request.url
        return response


def reply(body=None, status=200, headers=None):
    return status, b'' if body is None else json.dumps(body).encode(), headers or {}


def client(session):
    return JiraClient(SITE, 'dummy@example.test', CANARY, session=session, timeout=0.75)


# Pinned Jira SDK operations must preserve REST-v3 wire contracts, literal null and retry
# isolation.
def test_actual_sdk_native_methods_and_final_wire(monkeypatch):
    assert version('atlassian-python-api') == '5.0.5'
    creations = []
    original = JiraCloud.create_issue
    def record_create(self, *args, **kwargs):
        assert self.api_version == 3
        assert self.backoff_and_retry is False and self.retry_with_header is False
        creations.append(kwargs['data'])
        return original(self, *args, **kwargs)
    monkeypatch.setattr(JiraCloud, 'create_issue', record_create)
    session = WireSession([
        reply({'key': 'OTHER-1', 'fields': {}}), reply({'issues': [], 'isLast': True}),
        reply({'key': 'OTHER-1'}, 201), reply(status=204),
        reply({'key': 'state/name', 'value': None}), reply(status=204),
        reply({'transitions': []}), reply(status=204),
    ])
    api = client(session)
    assert not session.wire  # No construction discovery.
    api.get_issue('OTHER-1', fields=['summary', 'status'])
    api.search('project = OTHER')
    api.create_issue({'summary': 'x'}, properties=[{'key': 'state', 'value': {'x': 1}}])
    api.update_issue('OTHER-1', update={'labels': [{'add': 'label'}]})
    assert api.get_issue_property('OTHER-1', 'state/name')['value'] is None
    api.put_issue_property('OTHER-1', 'state/name', None)
    api.transitions('OTHER-1')
    api.apply_transition('OTHER-1', '9')
    assert creations == [{'fields': {'summary': 'x'},
                          'properties': [{'key': 'state', 'value': {'x': 1}}]}]
    assert [(req.method, urlsplit(req.url).path) for req, _ in session.wire] == [
        ('GET', '/rest/api/3/issue/OTHER-1'),
        ('POST', '/rest/api/3/search/jql'),
        ('POST', '/rest/api/3/issue'),
        ('PUT', '/rest/api/3/issue/OTHER-1'),
        ('GET', '/rest/api/3/issue/OTHER-1/properties/state%2Fname'),
        ('PUT', '/rest/api/3/issue/OTHER-1/properties/state%2Fname'),
        ('GET', '/rest/api/3/issue/OTHER-1/transitions'),
        ('POST', '/rest/api/3/issue/OTHER-1/transitions'),
    ]
    for req, options in session.wire:
        assert req.url.startswith(SITE + '/rest/api/3/')
        assert options['allow_redirects'] is False
        assert options['timeout'] == 0.75  # SDK int(timeout) must not truncate public API.
        assert not options['proxies']
        assert 'allow_redirects' not in req.url
    assert session.trust_env is False
    assert session.wire[0][0].body is None
    assert 'Content-Type' not in session.wire[0][0].headers
    assert 'fields=summary,status' in session.wire[0][0].url
    assert json.loads(session.wire[1][0].body)['fields'] == ['summary']
    assert json.loads(session.wire[2][0].body) == {
        'fields': {'summary': 'x'}, 'properties': [{'key': 'state', 'value': {'x': 1}}]}
    assert session.wire[5][0].body == 'null'
    assert session.wire[5][0].headers['Content-Type'] == 'application/json'
    assert session.wire[5][0].url.endswith('/properties/state%2Fname')
    assert json.loads(session.wire[3][0].body) == {'update': {'labels': [{'add': 'label'}]}}
    assert 'expand=transitions.fields' in session.wire[6][0].url
    assert json.loads(session.wire[7][0].body) == {'transition': {'id': '9'}}


# SDK property serialization must preserve supported JSON values and the JSON content type.
@pytest.mark.parametrize('value', [None, False, 0, 1.25, -1e300, '', 'text', [1, None], {'x': None}])
def test_actual_sdk_property_json_values(value):
    session = WireSession([reply(status=204)])
    client(session).put_issue_property('OTHER-1', 'state', value)
    assert json.loads(session.wire[0][0].body) == value
    assert session.wire[0][0].headers['Content-Type'] == 'application/json'


# SDK errors must retain bounded metadata without retries, redirects or sensitive payload logs.
@pytest.mark.parametrize('result,error,metadata', [
    pytest.param(reply({'error': 'rate_limited'}, 429, {'Retry-After': '0'}),
                 JiraRateLimitError, (429, 'rate_limited', 0, (), ()), id='rate-limit-zero'),
    pytest.param(reply({'error': 'ratelimited'}, 429, {'Retry-After': '2.5'}),
                 JiraRateLimitError, (429, 'ratelimited', 2.5, (), ()), id='rate-limit-alias-fraction'),
    pytest.param(reply({'errors': {'summary': CANARY, 'description': CANARY},
                       'errorMessages': ['BAD_INPUT', 'unsafe ' + CANARY]}, 400),
                 JiraValidationError, (400, None, None, ('description', 'summary'), ('BAD_INPUT',)),
                 id='validation-safe-fields-codes'),
    pytest.param(reply({'message': CANARY}, 403), JiraPermanentError,
                 (403, None, None, (), ()), id='permanent-rejection'),
    pytest.param(reply({'message': CANARY}, 503), JiraAmbiguousError,
                 (503, None, None, (), ()), id='uncertain-write'),
    pytest.param(reply({}, 302, {'Location': 'https://redirect.invalid/' + CANARY}),
                 JiraAmbiguousError, (None, None, None, (), ()), id='no-redirect'),
    pytest.param(requests.ConnectionError(CANARY), JiraAmbiguousError,
                 (None, None, None, (), ()), id='connection-loss'),
    pytest.param(requests.Timeout(CANARY), JiraAmbiguousError,
                 (None, None, None, (), ()), id='timeout'),
    pytest.param((201, b'not-json ' + CANARY.encode(), {}), JiraAmbiguousError,
                 (None, None, None, (), ()), id='malformed-success'),
])
def test_no_retries_redirects_or_payload_logs(result, error, metadata, caplog):
    caplog.set_level(logging.DEBUG)
    session = WireSession([result])
    with pytest.raises(error) as caught:
        client(session).create_issue({'summary': CANARY})
    assert len(session.wire) == 1
    assert type(caught.value) is error
    assert (caught.value.status_code, caught.value.error, caught.value.retry_after,
            caught.value.field_errors, caught.value.error_codes) == metadata
    assert CANARY not in str(caught.value)
    assert CANARY not in caplog.text
    assert caught.value.__cause__ is None


# A POST-based search timeout is a transient read failure, not an ambiguous write.
def test_post_search_connection_loss_is_read():
    session = WireSession([requests.ConnectionError(CANARY)])
    with pytest.raises(JiraTransientError):
        client(session).search('project = OTHER')
    assert len(session.wire) == 1


# Non-finite JSON must fail before request preparation or send, without leaking payloads.
@pytest.mark.parametrize('number', [float('nan'), float('inf'), float('-inf')],
                         ids=['nan', 'positive-infinity', 'negative-infinity'])
@pytest.mark.parametrize('operation', ['create', 'initial-property', 'property', 'update', 'transition', 'search'])
def test_nonfinite_json_rejected_before_preparation_or_send(number, operation, caplog):
    caplog.set_level(logging.DEBUG)
    session = WireSession([])
    api = client(session)
    nested = {'outer': [CANARY, {'number': number}]}
    calls = {
        'create': lambda: api.create_issue({'customfield_1': nested}),
        'initial-property': lambda: api.create_issue({'summary': 'x'},
            properties=[{'key': 'state', 'value': nested}]),
        'property': lambda: api.put_issue_property('OTHER-1', 'state', nested),
        'update': lambda: api.update_issue('OTHER-1', fields={'customfield_1': nested}),
        'transition': lambda: api.apply_transition('OTHER-1', '9', fields={'customfield_1': nested}),
        'search': lambda: api.search('project = OTHER', fields=[nested]),
    }
    expected = JiraTransientError if operation == 'search' else JiraAmbiguousError
    with pytest.raises(expected) as caught:
        calls[operation]()
    assert session.wire == []
    assert session.prepared == []
    assert CANARY not in str(caught.value)
    assert CANARY not in caplog.text
    assert caught.value.__cause__ is None


# Successful response content must remain available to callers but absent from SDK logs.
def test_success_response_body_not_logged_but_preserved(caplog):
    caplog.set_level(logging.DEBUG)
    session = WireSession([reply({'key': 'OTHER-1', 'fields': {'summary': CANARY}})])
    assert client(session).get_issue('OTHER-1')['fields']['summary'] == CANARY
    assert CANARY not in caplog.text
