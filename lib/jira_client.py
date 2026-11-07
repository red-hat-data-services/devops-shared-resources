"""Explicit JiraCloud SDK REST v3 site-origin operations without GAP policy.

Use caller-supplied origin, credentials, session and timeout for issue get/search/
create/update, named properties and transitions. Search completes bounded
pagination or fails rather than returning partial inventory; omitted search
fields explicitly request summary. Create can include properties atomically in
its POST. Generic fields/JSON are not interpreted as GAP content or restricted
ADF, and results are service data/acknowledgements, not GAP-verified success.

Classify failures by semantic read versus write (POST search is a read), retain
sanitized error metadata and treat uncertain mutations as ambiguous. Never
automatically retry writes or discover authentication. This is a site-origin
Cloud client, not a gateway/discovery SDK; destination authorization, incident
policy and stronger application readbacks remain caller responsibilities.
"""
from __future__ import annotations
import copy
import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlsplit
import re
import requests
from atlassian import JiraCloud


class _SafeResponse:
    """Keep service bodies out of SDK debug logging, retaining raw JSON access."""

    def __init__(self, response):
        self._response = response
        self.status_code = getattr(response, "status_code", None)
        self.headers = getattr(response, "headers", {})
        self.text = ""
        self.reason = ""

    def json(self):
        return self._response.json()


class _JiraTransport:
    """Bridge the public verb-session contract to the SDK's request protocol.

    The SDK serializes bodies and builds endpoints; this boundary enforces
    caller-owned authentication, exact timeout, and no redirects. It does not
    implement retries, parsing or a second HTTP stack.
    """

    verify = True

    def __init__(self, client):
        self.client = client

    def request(self, method, url, *, data=None, json=None, headers=None, **kwargs):
        client = self.client
        if not url.startswith(client.base_url + "/rest/api/3/"):
            raise ValueError("Unexpected Jira SDK destination")
        headers = dict(headers or {})
        if data is None and json is None:
            headers.pop("Content-Type", None)
        response = getattr(client.session, method.lower())(
            url, data=data, **({"json": json} if json is not None else {}),
            headers=headers, timeout=client.timeout, allow_redirects=False,
            auth=(client.email, client.token),
        )
        return _SafeResponse(response)


class _QuietJiraCloud(JiraCloud):
    """Enforce strict JSON and disable payload diagnostics for this instance."""

    def request(self, *args, **kwargs):
        # Requests' original json= path rejects NaN/Infinity before sending.
        # The SDK's data= serializer otherwise accepts those non-JSON tokens.
        # Preserve InvalidJSONError's semantic read/write classification.
        for value in (kwargs.get("data"), kwargs.get("json")):
            try:
                json.dumps(value, allow_nan=False)
            except ValueError:
                raise requests.exceptions.InvalidJSONError("Invalid Jira JSON payload") from None
        return super().request(*args, **kwargs)

    def log_curl_debug(self, *args, **kwargs):
        pass

ISSUE_KEY_RE = re.compile(r"[A-Z][A-Z0-9_]*-[0-9]+\Z")
SAFE_ERROR_CODE_RE = re.compile(r"[A-Z0-9_:-]{1,64}\Z")
MAX_SEARCH_PAGE_SIZE = 50
MAX_SEARCH_PAGES = 20

class JiraError(RuntimeError):
    """Base class for sanitized Jira adapter errors."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error: str | None = None,
        retry_after: float | None = None,
        key: str | None = None,
        field_errors: tuple[str, ...] | None = None,
        error_codes: tuple[str, ...] | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.error = error
        self.retry_after = retry_after
        self.key = key
        self.field_errors = field_errors or ()
        self.error_codes = error_codes or ()


class JiraRateLimitError(JiraError):
    """The server asked the caller to back off."""


class JiraTransientError(JiraError):
    """A read-only call could not be established safely."""


class JiraAmbiguousError(JiraError):
    """The write/readback outcome could not be established safely."""


class JiraPermanentError(JiraError):
    """The request was rejected in a non-retryable way."""


class JiraValidationError(JiraPermanentError):
    """The request failed validation with bounded field metadata."""


@dataclass
class JiraClient:
    base_url: str
    email: str = field(repr=False)
    token: str = field(repr=False)
    session: Any = None
    timeout: int = 20

    def __post_init__(self):
        self.base_url = self._normalize_base_url(self.base_url)
        self._validate_credential(self.email, "email")
        self._validate_credential(self.token, "token")
        self._validate_timeout(self.timeout)
        if self.session is None:
            self.session = requests.Session()
        if not all(hasattr(self.session, attr) for attr in ("get", "post", "put")):
            raise TypeError("session must provide get(), post(), and put()")
        if hasattr(self.session, "trust_env"):
            try:
                self.session.trust_env = False
            except Exception:
                pass

    @staticmethod
    def _normalize_base_url(base_url: Any) -> str:
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("base_url must be a non-empty string")
        split = urlsplit(base_url.strip())
        if split.scheme != "https":
            raise ValueError("base_url must use https")
        if split.username or split.password:
            raise ValueError("base_url must not include credentials")
        if not split.hostname:
            raise ValueError("base_url must include a hostname")
        if split.port is not None:
            raise ValueError("base_url must not include a port")
        if split.query or split.fragment:
            raise ValueError("base_url must not include query or fragment components")
        if split.path not in {"", "/"}:
            raise ValueError("base_url must not include a path")
        return base_url.strip().rstrip("/")


    @staticmethod
    def _validate_credential(value: Any, name: str) -> None:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a non-empty string")


    @staticmethod
    def _validate_timeout(value: Any) -> None:
        if not isinstance(value, (int, float)) or value <= 0:
            raise ValueError("timeout must be positive")


    @staticmethod
    def _issue_key(key: Any) -> str:
        if not isinstance(key, str) or ISSUE_KEY_RE.fullmatch(key) is None:
            raise ValueError("key must be a Jira issue key")
        return key


    @staticmethod
    def _response_json(response: Any) -> Any:
        try:
            return response.json()
        except Exception:
            return None


    @staticmethod
    def _retry_after(headers: Any) -> float | None:
        if not hasattr(headers, "get"):
            return None
        value = headers.get("Retry-After", headers.get("retry-after"))
        if isinstance(value, list):
            value = value[0] if len(value) == 1 else None
        try:
            retry_after = float(value)
        except (TypeError, ValueError):
            return None
        if retry_after < 0 or retry_after == float("inf"):
            return None
        return retry_after


    @staticmethod
    def _safe_error(data: Any) -> str | None:
        if isinstance(data, dict):
            error = data.get("error")
            if isinstance(error, str):
                value = error.strip()
                if value and (SAFE_ERROR_CODE_RE.fullmatch(value) or value in {"rate_limited", "ratelimited"}):
                    return value
        return None


    @staticmethod
    def _rate_limited(data: Any) -> bool:
        return JiraClient._safe_error(data) in {"rate_limited", "ratelimited"}


    @staticmethod
    def _validation_details(data: Any) -> tuple[tuple[str, ...], tuple[str, ...]]:
        if not isinstance(data, dict):
            return (), ()
        fields: list[str] = []
        errors = data.get("errors")
        if isinstance(errors, dict):
            for name in errors:
                if isinstance(name, str) and name.strip():
                    fields.append(name)
        codes: list[str] = []
        messages = data.get("errorMessages")
        if isinstance(messages, list):
            for value in messages:
                if isinstance(value, str) and SAFE_ERROR_CODE_RE.fullmatch(value):
                    codes.append(value)
        return tuple(sorted(dict.fromkeys(fields))), tuple(sorted(dict.fromkeys(codes)))


    def _session_request(
        self,
        *,
        operation: str,
        write: bool,
        sdk_call,
    ) -> tuple[Any, Any]:
        try:
            sdk = _QuietJiraCloud(
                self.base_url, api_version=3, session=_JiraTransport(self),
                timeout=self.timeout, advanced_mode=True,
                backoff_and_retry=False, retry_with_header=False,
            )
            response = sdk_call(sdk)
        except requests.Timeout:
            if write:
                raise JiraAmbiguousError(f"Jira {operation} outcome could not be established") from None
            raise JiraTransientError(f"Jira {operation} outcome could not be established") from None
        except requests.RequestException:
            if write:
                raise JiraAmbiguousError(f"Jira {operation} outcome could not be established") from None
            raise JiraTransientError(f"Jira {operation} outcome could not be established") from None
        except Exception:
            raise JiraAmbiguousError(f"Jira {operation} outcome could not be established") from None

        data = self._response_json(response)
        status_code = getattr(response, "status_code", None)
        headers = getattr(response, "headers", {})
        error = self._safe_error(data)

        if status_code == 429 or self._rate_limited(data):
            raise JiraRateLimitError(
                f"Jira {operation} rate limit reached",
                status_code=status_code,
                error=error,
                retry_after=self._retry_after(headers),
            )

        if not isinstance(status_code, int):
            raise JiraAmbiguousError(f"Jira {operation} outcome could not be established")

        if 200 <= status_code < 300:
            return response, data

        if status_code == 400:
            field_errors, error_codes = self._validation_details(data)
            raise JiraValidationError(
                f"Jira {operation} was rejected",
                status_code=status_code,
                error=error,
                field_errors=field_errors,
                error_codes=error_codes,
            )

        if 400 <= status_code < 500:
            raise JiraPermanentError(
                f"Jira {operation} was rejected",
                status_code=status_code,
                error=error,
            )

        if status_code >= 500:
            error_cls = JiraAmbiguousError if write else JiraTransientError
            raise error_cls(
                f"Jira {operation} outcome could not be established",
                status_code=status_code,
                error=error,
            )

        raise JiraAmbiguousError(f"Jira {operation} outcome could not be established")


    @staticmethod
    def _issue_fields(data: Any) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise JiraAmbiguousError("Jira issue response could not be verified")
        fields = data.get("fields")
        if not isinstance(fields, dict):
            raise JiraAmbiguousError("Jira issue response could not be verified")
        key = data.get("key")
        if not isinstance(key, str) or not ISSUE_KEY_RE.fullmatch(key):
            raise JiraAmbiguousError("Jira issue response could not be verified")
        return data


    def _url(self, path):
        return f"{self.base_url}/rest/api/3/{path}"

    def get_issue(self, key, *, fields=None):
        key = self._issue_key(key)
        _, data = self._session_request(
            operation="issue read", write=False,
            sdk_call=lambda sdk: sdk.get_issue(key, fields=None if fields is None else ",".join(fields)))
        issue = self._issue_fields(data)
        if issue["key"] != key:
            raise JiraAmbiguousError("Jira issue response could not be verified")
        return issue

    def search(self, jql, *, fields=None):
        if not isinstance(jql, str) or not jql.strip():
            raise ValueError("jql must be a non-empty string")
        issues, seen, tokens, token = [], set(), set(), None
        for _ in range(MAX_SEARCH_PAGES):
            # Enhanced search defaults to IDs only; require a usable fields mapping.
            payload = {"jql": jql, "maxResults": MAX_SEARCH_PAGE_SIZE,
                       "fields": ["summary"] if fields is None else list(fields)}
            if token is not None:
                payload["nextPageToken"] = token
            _, data = self._session_request(
                operation="issue search", write=False,
                sdk_call=lambda sdk: sdk.search_and_reconsile_issues_using_jql_post(data=payload))
            if not isinstance(data, dict):
                raise JiraAmbiguousError("Jira search response could not be verified")
            batch, last, next_token = data.get("issues"), data.get("isLast"), data.get("nextPageToken")
            if (not isinstance(batch, list) or not isinstance(last, bool)
                or (next_token is not None and not isinstance(next_token, str))
                or len(batch) > MAX_SEARCH_PAGE_SIZE or (not batch and not last)):
                raise JiraAmbiguousError("Jira search pagination could not be verified")
            for item in batch:
                issue = self._issue_fields(item)
                if issue["key"] in seen:
                    raise JiraAmbiguousError("Jira search pagination could not be verified")
                seen.add(issue["key"])
                issues.append(issue)
            if last:
                if next_token not in (None, ""):
                    raise JiraAmbiguousError("Jira search pagination could not be verified")
                return issues
            if not isinstance(next_token, str) or not next_token.strip() or next_token in tokens:
                raise JiraAmbiguousError("Jira search pagination could not be verified")
            tokens.add(next_token)
            token = next_token
        raise JiraAmbiguousError("Jira search pagination could not be verified")

    def create_issue(self, fields, *, properties=None):
        if not isinstance(fields, dict):
            raise ValueError("fields must be a mapping")
        payload = {"fields": copy.deepcopy(fields)}
        if properties is not None:
            payload["properties"] = copy.deepcopy(properties)
        _, data = self._session_request(
            operation="issue creation", write=True,
            sdk_call=lambda sdk: sdk.create_issue(data=payload))
        if not isinstance(data, dict):
            raise JiraAmbiguousError("Jira creation outcome could not be established")
        key = data.get("key")
        if not isinstance(key, str) or ISSUE_KEY_RE.fullmatch(key) is None:
            raise JiraAmbiguousError("Jira creation outcome could not be established")
        return {"key": key, "url": f"{self.base_url}/browse/{key}"}

    def update_issue(self, key, *, fields=None, update=None):
        key = self._issue_key(key)
        payload = {}
        for name, value in (("fields", fields), ("update", update)):
            if value is not None:
                if not isinstance(value, dict):
                    raise ValueError(f"{name} must be a mapping")
                payload[name] = copy.deepcopy(value)
        _, data = self._session_request(
            operation="issue update", write=True,
            sdk_call=lambda sdk: sdk.edit_issue(key, data=payload))
        return data

    def _property_url(self, key, name):
        key = self._issue_key(key)
        if not isinstance(name, str) or not name.strip():
            raise ValueError("property name must be a non-empty string")
        return self._url(f"issue/{key}/properties/{quote(name, safe='')}")

    def get_issue_property(self, key, name):
        self._property_url(key, name)  # Validate key/name before constructing SDK.
        try:
            _, data = self._session_request(operation="issue property read", write=False,
                sdk_call=lambda sdk: sdk.get_issue_property(key, quote(name, safe="")))
        except JiraPermanentError as exc:
            if exc.status_code == 404:
                return None
            raise
        if not isinstance(data, dict) or data.get("key") != name or "value" not in data:
            raise JiraAmbiguousError("Jira issue property response could not be verified", key=key)
        return data

    def put_issue_property(self, key, name, value):
        self._property_url(key, name)
        _, data = self._session_request(
            operation="issue property write", write=True,
            # files={} selects the SDK's raw-body path without multipart data.
            # Unlike data=None (no body), this sends the JSON literal null.
            sdk_call=lambda sdk: sdk.set_issue_property(key, quote(name, safe=""),
                data="null" if value is None else copy.deepcopy(value),
                **({"files": {}} if value is None else {})))
        return data

    def transitions(self, key):
        key = self._issue_key(key)
        _, data = self._session_request(
            operation="issue transitions", write=False,
            sdk_call=lambda sdk: sdk.get_transitions(key, expand="transitions.fields"))
        if not isinstance(data, dict) or not isinstance(data.get("transitions"), list):
            raise JiraAmbiguousError("Jira transitions response could not be verified")
        return data["transitions"]

    def apply_transition(self, key, transition_id, *, fields=None):
        key = self._issue_key(key)
        if not isinstance(transition_id, str) or not transition_id.strip():
            raise ValueError("transition_id must be a non-empty string")
        payload = {"transition": {"id": transition_id}}
        if fields is not None:
            if not isinstance(fields, dict):
                raise ValueError("fields must be a mapping")
            payload["fields"] = copy.deepcopy(fields)
        _, data = self._session_request(
            operation="issue transition", write=True,
            sdk_call=lambda sdk: sdk.do_transition(key, data=payload))
        return data
