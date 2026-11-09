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
        with pytest.raises(ValueError): ownership.fetch_mapping('test.yaml', ca_bundle=ca)
    else:
        assert ownership.fetch_mapping('test.yaml', ca_bundle=ca) == body
    assert session.closed


def test_network_timeout_closes_session(monkeypatch):
    class Session:
        closed = False
        def get(self, *args, **kwargs): raise TimeoutError('offline')
        def close(self): self.closed = True
    session = Session()
    monkeypatch.setattr(ownership.requests, 'Session', lambda: session)
    with pytest.raises(TimeoutError): ownership.fetch_mapping('test.yaml')
    assert session.closed
