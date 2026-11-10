import pytest
from lib.gap_notifications import ownership


@pytest.mark.parametrize('status,body,ca,error', [
    (200, b'mappings: {}', None, False),
    (200, b'mappings: {}', '/fake/ca.pem', False),
    (302, b'', None, True), (500, b'', None, True),
    (200, b'x' * (ownership.LIMIT + 1), None, True),
])
def test_mapping_transport(monkeypatch, status, body, ca, error):
    class Response:
        status_code = status
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def iter_content(self, size):
            assert size == 8192
            yield body
    class Session:
        closed = False
        def get(self, url, **kwargs):
            assert self.trust_env is False
            assert url == ownership.BASE + 'test.yaml'
            assert kwargs == dict(timeout=(5, 15), allow_redirects=False, stream=True, verify=ca or True)
            return Response()
        def close(self): self.closed = True
    session = Session()
    monkeypatch.setattr(ownership.requests, 'Session', lambda: session)
    if error:
        with pytest.raises(ValueError):
            ownership.fetch_mapping('test.yaml', ca_bundle=ca, github_token='fake-github-token')
    else:
        assert ownership.fetch_mapping('test.yaml', ca_bundle=ca, github_token='fake-github-token') == body
    assert session.closed


@pytest.mark.parametrize('token', [None, '', '   '])
def test_private_mapping_requires_explicit_token_before_network(monkeypatch, token):
    def session():
        pytest.fail('missing token must not start an anonymous request')
    monkeypatch.setattr(ownership.requests, 'Session', session)
    with pytest.raises(ValueError, match='^GitHub token required for repository ownership mapping$'):
        ownership.fetch_mapping('automerge_team_mapping.yaml', github_token=token)


def test_default_loader_forwards_explicit_ca_and_token(monkeypatch):
    seen = []
    def fetch(name, *, ca_bundle=None, github_token=None):
        seen.append((name, ca_bundle, github_token))
        if name == 'automerge_team_mapping.yaml':
            return 'repo_to_component: {}'
        return ('mappings: {}' if name.startswith('konflux')
                else 'components: {default: {slack_team_handle: devops}}')
    monkeypatch.setattr(ownership, 'fetch_mapping', fetch)
    ownership.OwnershipPolicy.load(ca_bundle='/fake/ca.pem', github_token='fake-github-token')
    assert seen == [(name, '/fake/ca.pem', 'fake-github-token') for name in
                    ('konflux-to-jira-component-map.yaml', 'rhoai-component-data.yaml',
                     'automerge_team_mapping.yaml')]


def test_network_timeout_closes_session(monkeypatch):
    class Session:
        closed = False
        def get(self, *args, **kwargs): raise TimeoutError('offline')
        def close(self): self.closed = True
    session = Session()
    monkeypatch.setattr(ownership.requests, 'Session', lambda: session)
    with pytest.raises(TimeoutError): ownership.fetch_mapping('test.yaml')
    assert session.closed
