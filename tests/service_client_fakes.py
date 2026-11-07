"""Requests-shaped test responses, queued Jira session and Jira issue builders.

The queued session normalizes SDK wire JSON/query strings for semantic tests;
SDK contract suites separately record actual PreparedRequests. No networking.
"""

class FakeResponse:
    def __init__(self, data=None, status_code=200, headers=None):
        self._data = data
        self.status_code = status_code
        self.headers = headers or {}

    def json(self):
        return self._data


class FakeSession:
    def __init__(self, *, responses=None, side_effects=None):
        self.responses = {method: list(values) for method, values in (responses or {}).items()}
        self.side_effects = {method: list(values) for method, values in (side_effects or {}).items()}
        self.calls = []
        self.trust_env = True

    def _call(self, method, url, **kwargs):
        # Normalize SDK-serialized wire data for the existing semantic assertions.
        import json
        from urllib.parse import urlsplit, urlunsplit, parse_qsl
        split = urlsplit(url)
        if split.query:
            kwargs['params'] = dict(parse_qsl(split.query))
            url = urlunsplit((split.scheme, split.netloc, split.path, '', ''))
        if kwargs.get('data') is not None:
            kwargs['json'] = json.loads(kwargs['data'])
        self.calls.append((method, url, kwargs))
        effects = self.side_effects.get(method)
        if effects:
            effect = effects.pop(0)
            if isinstance(effect, BaseException):
                raise effect
            raise effect
        responses = self.responses.get(method)
        if not responses:
            raise AssertionError(f"unexpected {method} call to {url}")
        response = responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    def get(self, url, **kwargs):
        return self._call("get", url, **kwargs)

    def post(self, url, **kwargs):
        return self._call("post", url, **kwargs)

    def put(self, url, **kwargs):
        return self._call("put", url, **kwargs)


def notifier_for(session):
    from lib.gap_notifications.notify_tools import JiraNotifier, STAGING_JIRA_ORIGIN
    return JiraNotifier(
        STAGING_JIRA_ORIGIN,
        "RHAI",
        "Bug",
        "jira-bot@example.com",
        "secret-token",
        session=session,
        timeout=11,
    )


def issue_for(key="RHAI-1", *, description, summary="summary", labels=None, status_category="new"):
    if labels is None:
        labels = ["gap-staging"]
    return {
        "key": key,
        "fields": {
            "summary": summary,
            "description": description,
            "labels": labels,
            "project": {"key": "RHAI"},
            "issuetype": {"name": "Bug", "id": "10016"},
            "status": {"statusCategory": {"key": status_category}},
        },
    }
