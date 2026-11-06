"""Staging Jira adapter for the GAP notification prototype."""

from __future__ import annotations

import copy
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import requests

STAGING_JIRA_ORIGIN = "https://stage-redhat.atlassian.net"
STAGING_JIRA_PROJECT = "RHAI"
STAGING_JIRA_ISSUE_TYPE_BUG = "Bug"
STAGING_JIRA_ISSUE_TYPE_ID = "10016"
ISSUE_KEY_RE = re.compile(r"RHAI-[0-9]+\Z")
LABEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
SAFE_ERROR_CODE_RE = re.compile(r"[A-Z0-9_:-]{1,64}\Z")
VALID_MARK_TYPES = {"code", "link"}
VALID_NODE_TYPES = {
    "blockquote",
    "bulletList",
    "codeBlock",
    "doc",
    "hardBreak",
    "heading",
    "listItem",
    "orderedList",
    "paragraph",
    "text",
}
SEARCH_FIELDS = ["summary", "description", "labels", "status", "project", "issuetype"]
NOTIFICATION_STATE_PROPERTY_KEY = "gap.notification.v1"
MAX_SEARCH_PAGE_SIZE = 50
MAX_SEARCH_PAGES = 20
MAX_JSON_DEPTH = 8
MAX_JSON_NODES = 256
MAX_JSON_STRING_LENGTH = 4096
MAX_JSON_LIST_LENGTH = 128
MAX_JSON_DICT_KEYS = 128


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


class JiraTerminalIssueError(JiraPermanentError):
    """The issue is already closed/done and must not be mutated."""


@dataclass
class JiraNotifier:
    base_url: str
    project: str
    issue_type: str
    email: str = field(repr=False)
    token: str = field(repr=False)
    session: Any = None
    timeout: int = 20

    def __post_init__(self) -> None:
        self.base_url = self._normalize_base_url(self.base_url)
        self._validate_project(self.project)
        self._validate_issue_type(self.issue_type)
        self._validate_credential(self.email, "email")
        self._validate_credential(self.token, "token")
        self._validate_timeout(self.timeout)
        if self.session is None:
            self.session = requests.Session()
            if hasattr(self.session, "trust_env"):
                self.session.trust_env = False
        if not all(hasattr(self.session, attr) for attr in ("get", "post", "put")):
            raise TypeError("session must provide get(), post(), and put()")
        if hasattr(self.session, "trust_env"):
            try:
                self.session.trust_env = False
            except Exception:
                pass

    @staticmethod
    def _validate_timeout(value: Any) -> None:
        if not isinstance(value, (int, float)) or value <= 0:
            raise ValueError("timeout must be positive")

    @staticmethod
    def _validate_credential(value: Any, name: str) -> None:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a non-empty string")

    @staticmethod
    def _validate_project(project: Any) -> None:
        if project != STAGING_JIRA_PROJECT:
            raise ValueError("project must be RHAI")

    @staticmethod
    def _validate_issue_type(issue_type: Any) -> None:
        if issue_type not in {STAGING_JIRA_ISSUE_TYPE_BUG, STAGING_JIRA_ISSUE_TYPE_ID}:
            raise ValueError("issue_type must be Bug or 10016")

    @staticmethod
    def _normalize_base_url(base_url: Any) -> str:
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("base_url must be a non-empty string")
        split = urlsplit(base_url.strip())
        if split.scheme != "https":
            raise ValueError("base_url must use https")
        if split.username or split.password:
            raise ValueError("base_url must not include credentials")
        if split.hostname != "stage-redhat.atlassian.net":
            raise ValueError("base_url must target the staging Jira origin")
        if split.port is not None:
            raise ValueError("base_url must not include a port")
        if split.query or split.fragment:
            raise ValueError("base_url must not include query or fragment components")
        if split.path not in {"", "/"}:
            raise ValueError("base_url must not include a path")
        return STAGING_JIRA_ORIGIN

    @staticmethod
    def _issue_key(key: Any) -> str:
        if not isinstance(key, str) or ISSUE_KEY_RE.fullmatch(key) is None:
            raise ValueError("key must match RHAI-<digits>")
        return key

    @staticmethod
    def _label(label: Any) -> str:
        if not isinstance(label, str) or LABEL_RE.fullmatch(label) is None:
            raise ValueError("label must be a deterministic automation label")
        return label

    @staticmethod
    def _summary(summary: Any) -> str:
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError("summary must be a non-empty string")
        return summary

    @staticmethod
    def _mapping(value: Any, *, name: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError(f"{name} must be a mapping")
        return value

    def _issue_url(self, key: str) -> str:
        return f"{self.base_url}/rest/api/3/issue/{key}"

    def _search_url(self) -> str:
        return f"{self.base_url}/rest/api/3/search/jql"

    def _transitions_url(self, key: str) -> str:
        return f"{self._issue_url(key)}/transitions"

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
        return JiraNotifier._safe_error(data) in {"rate_limited", "ratelimited"}

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

    @staticmethod
    def _validate_adf(node: Any, *, path: str = "description") -> None:
        if not isinstance(node, dict):
            raise ValueError(f"{path} must be an ADF mapping")
        node_type = node.get("type")
        if not isinstance(node_type, str) or not node_type:
            raise ValueError(f"{path}.type is required")
        if node_type not in VALID_NODE_TYPES:
            raise ValueError(f"{path}.type is not supported")
        if node_type == "doc":
            if node.get("version") != 1:
                raise ValueError("description.version must be 1")
            content = node.get("content")
            if not isinstance(content, list):
                raise ValueError("description.content must be a list")
            for index, child in enumerate(content):
                JiraNotifier._validate_adf(child, path=f"{path}.content[{index}]")
            return
        if node_type == "heading":
            attrs = node.get("attrs")
            level = attrs.get("level") if isinstance(attrs, dict) else None
            if not isinstance(level, int) or not (1 <= level <= 6):
                raise ValueError(f"{path}.attrs.level must be between 1 and 6")
            content = node.get("content")
            if not isinstance(content, list):
                raise ValueError(f"{path}.content must be a list")
            for index, child in enumerate(content):
                JiraNotifier._validate_adf(child, path=f"{path}.content[{index}]")
            return
        if node_type in {"paragraph", "blockquote", "bulletList", "orderedList", "listItem", "codeBlock"}:
            content = node.get("content")
            if not isinstance(content, list):
                raise ValueError(f"{path}.content must be a list")
            for index, child in enumerate(content):
                JiraNotifier._validate_adf(child, path=f"{path}.content[{index}]")
            if node_type in {"bulletList", "orderedList", "listItem"} and not content:
                raise ValueError(f"{path}.content must not be empty")
            if node_type == "codeBlock":
                attrs = node.get("attrs")
                if attrs is not None and not isinstance(attrs, dict):
                    raise ValueError(f"{path}.attrs must be a mapping")
            return
        if node_type == "hardBreak":
            if "content" in node and node["content"] not in (None, []):
                raise ValueError(f"{path}.content must be omitted")
            return
        if node_type == "text":
            text = node.get("text")
            if not isinstance(text, str):
                raise ValueError(f"{path}.text must be a string")
            marks = node.get("marks")
            if marks is None:
                return
            if not isinstance(marks, list):
                raise ValueError(f"{path}.marks must be a list")
            for index, mark in enumerate(marks):
                if not isinstance(mark, dict):
                    raise ValueError(f"{path}.marks[{index}] must be a mapping")
                mark_type = mark.get("type")
                if mark_type not in VALID_MARK_TYPES:
                    raise ValueError(f"{path}.marks[{index}].type is not supported")
                if mark_type == "link":
                    attrs = mark.get("attrs")
                    if not isinstance(attrs, dict) or not isinstance(attrs.get("href"), str) or not attrs.get("href").strip():
                        raise ValueError(f"{path}.marks[{index}].attrs.href is required")
                if mark_type == "code" and "attrs" in mark and mark["attrs"] not in (None, {}):
                    raise ValueError(f"{path}.marks[{index}].attrs must be omitted")
            return
        raise ValueError(f"{path}.type is not supported")

    def _session_request(
        self,
        method: str,
        url: str,
        *,
        operation: str,
        write: bool,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
    ) -> tuple[Any, Any]:
        request = getattr(self.session, method)
        try:
            response = request(
                url,
                params=params,
                json=json_body,
                timeout=self.timeout,
                allow_redirects=False,
                auth=(self.email, self.token),
            )
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

    @staticmethod
    def _status_category(issue: dict[str, Any]) -> str | None:
        fields = issue.get("fields")
        if not isinstance(fields, dict):
            return None
        status = fields.get("status")
        if not isinstance(status, dict):
            return None
        category = status.get("statusCategory")
        if not isinstance(category, dict):
            return None
        value = category.get("key")
        return value if isinstance(value, str) else None

    @staticmethod
    def _project_key(issue: dict[str, Any]) -> str | None:
        fields = issue.get("fields")
        if not isinstance(fields, dict):
            return None
        project = fields.get("project")
        if not isinstance(project, dict):
            return None
        key = project.get("key")
        return key if isinstance(key, str) else None

    @staticmethod
    def _issue_type_value(issue: dict[str, Any]) -> tuple[str | None, str | None]:
        fields = issue.get("fields")
        if not isinstance(fields, dict):
            return None, None
        issuetype = fields.get("issuetype")
        if not isinstance(issuetype, dict):
            return None, None
        return (
            issuetype.get("name") if isinstance(issuetype.get("name"), str) else None,
            issuetype.get("id") if isinstance(issuetype.get("id"), str) else None,
        )

    @staticmethod
    def _labels(issue: dict[str, Any]) -> list[str] | None:
        fields = issue.get("fields")
        if not isinstance(fields, dict):
            return None
        labels = fields.get("labels")
        if not isinstance(labels, list):
            return None
        if not all(isinstance(label, str) for label in labels):
            return None
        return labels

    @staticmethod
    def _description(issue: dict[str, Any]) -> Any:
        fields = issue.get("fields")
        if not isinstance(fields, dict):
            return None
        return fields.get("description")

    @staticmethod
    def _adf_signature(node: Any) -> Any:
        if not isinstance(node, dict):
            return node
        node_type = node.get("type")
        content = node.get("content") if isinstance(node.get("content"), list) else None
        if node_type == "doc":
            return {
                "type": "doc",
                "version": node.get("version"),
                "content": [JiraNotifier._adf_signature(child) for child in content or []],
            }
        if node_type == "heading":
            attrs = node.get("attrs") if isinstance(node.get("attrs"), dict) else {}
            return {
                "type": "heading",
                "attrs": {"level": attrs.get("level")},
                "content": [JiraNotifier._adf_signature(child) for child in content or []],
            }
        if node_type in {"paragraph", "blockquote", "bulletList", "orderedList", "listItem", "codeBlock"}:
            result = {
                "type": node_type,
                "content": [JiraNotifier._adf_signature(child) for child in content or []],
            }
            attrs = node.get("attrs") if isinstance(node.get("attrs"), dict) else None
            if node_type == "codeBlock" and attrs:
                result["attrs"] = {key: attrs[key] for key in sorted(attrs) if isinstance(key, str)}
            return result
        if node_type == "hardBreak":
            return {"type": "hardBreak"}
        if node_type == "text":
            result = {"type": "text", "text": node.get("text")}
            marks = node.get("marks")
            if isinstance(marks, list):
                normalized = []
                for mark in marks:
                    if not isinstance(mark, dict):
                        normalized.append(mark)
                        continue
                    mark_type = mark.get("type")
                    if mark_type == "link":
                        attrs = mark.get("attrs") if isinstance(mark.get("attrs"), dict) else {}
                        normalized.append({"type": "link", "attrs": {"href": attrs.get("href")}})
                    elif mark_type == "code":
                        normalized.append({"type": "code"})
                    else:
                        normalized.append({"type": mark_type})
                result["marks"] = normalized
            return result
        return node

    @staticmethod
    def _same_labels(left: Any, right: Any) -> bool:
        return isinstance(left, list) and isinstance(right, list) and set(left) == set(right)

    @staticmethod
    def _normalize_json(value: Any, *, path: str = "notification_state", depth: int = 0, budget: dict[str, int] | None = None) -> Any:
        if budget is None:
            budget = {"nodes": 0}
        budget["nodes"] += 1
        if budget["nodes"] > MAX_JSON_NODES:
            raise ValueError(f"{path} is too large")
        if depth > MAX_JSON_DEPTH:
            raise ValueError(f"{path} exceeds maximum nesting depth")
        if value is None or isinstance(value, bool):
            return value
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError(f"{path} must be finite")
            return value
        if isinstance(value, str):
            if len(value) > MAX_JSON_STRING_LENGTH:
                raise ValueError(f"{path} string value is too long")
            return value
        if isinstance(value, list):
            if len(value) > MAX_JSON_LIST_LENGTH:
                raise ValueError(f"{path} list is too long")
            return [JiraNotifier._normalize_json(item, path=f"{path}[{index}]", depth=depth + 1, budget=budget)
                    for index, item in enumerate(value)]
        if isinstance(value, dict):
            if len(value) > MAX_JSON_DICT_KEYS:
                raise ValueError(f"{path} object has too many keys")
            normalized: dict[str, Any] = {}
            for key, item in value.items():
                if not isinstance(key, str) or not key.strip():
                    raise ValueError(f"{path} keys must be non-empty strings")
                if len(key) > MAX_JSON_STRING_LENGTH:
                    raise ValueError(f"{path} key is too long")
                normalized[key] = JiraNotifier._normalize_json(item, path=f"{path}.{key}", depth=depth + 1, budget=budget)
            return normalized
        raise ValueError(f"{path} must be valid JSON")

    @staticmethod
    def _notification_state_size(value: Any) -> int:
        return len(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False))

    @staticmethod
    def _normalize_notification_state(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError("notification_state must be a mapping")
        normalized = JiraNotifier._normalize_json(copy.deepcopy(value))
        if not isinstance(normalized, dict):
            raise ValueError("notification_state must be a mapping")
        if JiraNotifier._notification_state_size(normalized) > 24 * 1024:
            raise ValueError("notification_state exceeds the property size cap")
        return normalized

    def _notification_state_url(self, key: str) -> str:
        return f"{self._issue_url(key)}/properties/{NOTIFICATION_STATE_PROPERTY_KEY}"

    @staticmethod
    def _status_category_or_error(issue: dict[str, Any], *, key: str) -> str:
        category = JiraNotifier._status_category(issue)
        if category not in {"new", "indeterminate", "done"}:
            raise JiraPermanentError("Jira issue status category is unavailable", key=key)
        return category

    def get_issue(self, key: str) -> dict[str, Any]:
        key = self._issue_key(key)
        _, data = self._session_request(
            "get",
            self._issue_url(key),
            operation="issue read",
            write=False,
            params={"fields": ",".join(SEARCH_FIELDS)},
        )
        issue = self._issue_fields(data)
        if issue["key"] != key:
            raise JiraAmbiguousError("Jira issue response could not be verified")
        return issue

    def find_by_label(self, label: str) -> list[dict[str, Any]]:
        label = self._label(label)
        jql = f'project = {self.project} AND labels = "{self._escape_jql(label)}" ORDER BY key ASC'
        issues: list[dict[str, Any]] = []
        seen: set[str] = set()
        seen_tokens: set[str] = set()
        token: str | None = None
        for _ in range(MAX_SEARCH_PAGES):
            payload: dict[str, Any] = {
                "jql": jql,
                "fields": SEARCH_FIELDS,
                "maxResults": MAX_SEARCH_PAGE_SIZE,
            }
            if token is not None:
                payload["nextPageToken"] = token
            _, data = self._session_request(
                "post",
                self._search_url(),
                operation="issue search",
                write=False,
                json_body=payload,
            )
            if not isinstance(data, dict):
                raise JiraAmbiguousError("Jira search response could not be verified")
            batch = data.get("issues")
            is_last = data.get("isLast")
            next_token = data.get("nextPageToken")
            if not isinstance(batch, list):
                raise JiraAmbiguousError("Jira search pagination could not be verified")
            if not isinstance(is_last, bool):
                raise JiraAmbiguousError("Jira search pagination could not be verified")
            if next_token is not None and not isinstance(next_token, str):
                raise JiraAmbiguousError("Jira search pagination could not be verified")
            if len(batch) > MAX_SEARCH_PAGE_SIZE:
                raise JiraAmbiguousError("Jira search pagination could not be verified")
            if not batch and not is_last:
                raise JiraAmbiguousError("Jira search pagination could not be verified")
            for item in batch:
                issue = self._issue_fields(item)
                issue_key = issue["key"]
                if issue_key in seen:
                    raise JiraAmbiguousError("Jira search pagination could not be verified")
                seen.add(issue_key)
                issues.append(issue)
            if is_last:
                if next_token not in (None, ""):
                    raise JiraAmbiguousError("Jira search pagination could not be verified")
                return issues
            if not isinstance(next_token, str) or not next_token.strip():
                raise JiraAmbiguousError("Jira search pagination could not be verified")
            if next_token in seen_tokens:
                raise JiraAmbiguousError("Jira search pagination could not be verified")
            seen_tokens.add(next_token)
            token = next_token
        raise JiraAmbiguousError("Jira search pagination could not be verified")

    def create_bug(
        self,
        summary: str,
        description: dict,
        labels: list[str],
        *,
        extra_fields: dict | None = None,
        notification_state: dict | None = None,
    ) -> dict[str, str]:
        summary = self._summary(summary)
        self._validate_adf(description)
        labels = self._validate_labels(labels)
        extra_fields = self._validate_extra_fields(extra_fields)
        normalized_state = None if notification_state is None else self._normalize_notification_state(notification_state)
        payload_fields: dict[str, Any] = {
            "project": {"key": self.project},
            "issuetype": {"name": self.issue_type} if self.issue_type == STAGING_JIRA_ISSUE_TYPE_BUG else {"id": self.issue_type},
            "summary": summary,
            "description": copy.deepcopy(description),
            "labels": list(labels),
        }
        payload = {"fields": payload_fields}
        payload_fields.update(extra_fields)
        if normalized_state is not None:
            payload["properties"] = [{"key": NOTIFICATION_STATE_PROPERTY_KEY, "value": copy.deepcopy(normalized_state)}]
        _, data = self._session_request(
            "post",
            f"{self.base_url}/rest/api/3/issue",
            operation="issue creation",
            write=True,
            json_body=payload,
        )
        if not isinstance(data, dict):
            raise JiraAmbiguousError("Jira creation outcome could not be established")
        key = data.get("key")
        if not isinstance(key, str) or ISSUE_KEY_RE.fullmatch(key) is None:
            raise JiraAmbiguousError("Jira creation outcome could not be established")
        current = self._readback_created_issue(key, summary, description, labels)
        self._verify_created_issue(current, summary, description, labels)
        if normalized_state is not None:
            try:
                created_state = self.get_notification_state(key)
            except JiraError as exc:
                raise JiraAmbiguousError(
                    "Jira creation may have succeeded; notification state readback could not be verified",
                    key=key,
                    status_code=exc.status_code,
                    error=exc.error,
                    retry_after=exc.retry_after,
                ) from None
            if created_state != normalized_state:
                raise JiraAmbiguousError("Jira creation readback did not match", key=key)
        return {"key": key, "url": f"{self.base_url}/browse/{key}"}

    def _readback_created_issue(
        self,
        key: str,
        summary: str,
        description: dict,
        labels: list[str],
    ) -> dict[str, Any]:
        try:
            return self.get_issue(key)
        except JiraError as exc:
            raise JiraAmbiguousError(
                "Jira creation may have succeeded; readback could not be verified",
                key=key,
                status_code=exc.status_code,
                error=exc.error,
                retry_after=exc.retry_after,
            ) from None

    def _read_notification_state_value(self, key: str) -> dict[str, Any] | None:
        try:
            _, data = self._session_request(
                "get",
                self._notification_state_url(key),
                operation="notification state read",
                write=False,
            )
        except JiraPermanentError as exc:
            if exc.status_code == 404:
                return None
            raise
        if not isinstance(data, dict):
            raise JiraAmbiguousError("Jira notification state response could not be verified", key=key)
        if data.get("key") != NOTIFICATION_STATE_PROPERTY_KEY or "value" not in data:
            raise JiraAmbiguousError("Jira notification state response could not be verified", key=key)
        try:
            return self._normalize_notification_state(data.get("value"))
        except ValueError as exc:
            raise JiraAmbiguousError("Jira notification state response could not be verified", key=key) from exc

    def get_notification_state(self, key: str) -> dict[str, Any] | None:
        key = self._issue_key(key)
        return self._read_notification_state_value(key)

    def put_notification_state(self, key: str, value: dict) -> dict[str, Any]:
        key = self._issue_key(key)
        normalized = self._normalize_notification_state(value)
        current_issue = self.get_issue(key)
        current_category = self._status_category_or_error(current_issue, key=key)
        self._session_request(
            "put",
            self._notification_state_url(key),
            operation="notification state write",
            write=True,
            json_body=copy.deepcopy(normalized),
        )
        current_state = self._read_notification_state_value(key)
        if current_state != normalized:
            raise JiraAmbiguousError("Jira notification state readback did not match", key=key)
        updated_issue = self.get_issue(key)
        updated_category = self._status_category_or_error(updated_issue, key=key)
        if updated_category != current_category:
            raise JiraAmbiguousError("Jira notification state write changed issue status", key=key)
        if updated_issue.get("fields") != current_issue.get("fields"):
            raise JiraAmbiguousError("Jira notification state write changed issue fields", key=key)
        return current_state

    def _verify_created_issue(
        self,
        issue: dict[str, Any],
        summary: str,
        description: dict,
        labels: list[str],
    ) -> None:
        if issue["fields"].get("summary") != summary:
            raise JiraAmbiguousError("Jira creation readback did not match", key=issue["key"])
        if self._adf_signature(issue["fields"].get("description")) != self._adf_signature(description):
            raise JiraAmbiguousError("Jira creation readback did not match", key=issue["key"])
        if not self._same_labels(issue["fields"].get("labels"), labels):
            raise JiraAmbiguousError("Jira creation readback did not match", key=issue["key"])
        if self._project_key(issue) != self.project:
            raise JiraAmbiguousError("Jira creation readback did not match", key=issue["key"])
        name, type_id = self._issue_type_value(issue)
        if self.issue_type == STAGING_JIRA_ISSUE_TYPE_BUG:
            if name != STAGING_JIRA_ISSUE_TYPE_BUG:
                raise JiraAmbiguousError("Jira creation readback did not match", key=issue["key"])
        elif type_id != STAGING_JIRA_ISSUE_TYPE_ID:
            raise JiraAmbiguousError("Jira creation readback did not match", key=issue["key"])

    def update_issue(self, key: str, *, description: dict | None = None, labels: list[str] | None = None) -> dict[str, Any]:
        key = self._issue_key(key)
        current = self.get_issue(key)
        current_category = self._status_category_or_error(current, key=key)
        if current_category == "done":
            raise JiraTerminalIssueError("Jira issue is already done and must not be mutated", key=key)
        payload_fields: dict[str, Any] = {}
        if description is not None:
            self._validate_adf(description)
            payload_fields["description"] = copy.deepcopy(description)
        if labels is not None:
            payload_fields["labels"] = self._validate_labels(labels)
        if not payload_fields:
            return current
        self._session_request(
            "put",
            self._issue_url(key),
            operation="issue update",
            write=True,
            json_body={"fields": payload_fields},
        )
        updated = self._readback_written_issue(key)
        self._verify_updated_issue(updated, current, payload_fields)
        return updated

    def transitions(self, key: str) -> list[dict[str, Any]]:
        key = self._issue_key(key)
        _, data = self._session_request(
            "get",
            self._transitions_url(key),
            operation="issue transitions",
            write=False,
            params={"expand": "transitions.fields"},
        )
        if not isinstance(data, dict):
            raise JiraAmbiguousError("Jira transitions response could not be verified")
        transitions = data.get("transitions")
        if not isinstance(transitions, list):
            raise JiraAmbiguousError("Jira transitions response could not be verified")
        return transitions

    def transition_to_done(
        self,
        key: str,
        transition_id: str,
        *,
        fields: dict | None = None,
    ) -> dict[str, Any]:
        key = self._issue_key(key)
        if not isinstance(transition_id, str) or not transition_id.strip():
            raise ValueError("transition_id must be a non-empty string")
        current = self.get_issue(key)
        current_category = self._status_category_or_error(current, key=key)
        if current_category == "done":
            return current
        available = self.transitions(key)
        selected = [transition for transition in available if transition.get("id") == transition_id]
        if len(selected) != 1:
            raise JiraPermanentError("Jira transition was rejected", key=key)
        transition = selected[0]
        target = transition.get("to")
        if not isinstance(target, dict):
            raise JiraPermanentError("Jira transition was rejected", key=key)
        category = target.get("statusCategory")
        if not isinstance(category, dict) or category.get("key") != "done":
            raise JiraPermanentError("Jira transition was rejected", key=key)
        payload: dict[str, Any] = {"transition": {"id": transition_id}}
        if fields is not None:
            payload["fields"] = self._mapping(fields, name="fields")
        self._session_request(
            "post",
            self._transitions_url(key),
            operation="issue transition",
            write=True,
            json_body=payload,
        )
        updated = self._readback_written_issue(key)
        if self._status_category_or_error(updated, key=key) != "done":
            raise JiraAmbiguousError("Jira transition readback did not reach done", key=key)
        return updated

    def _readback_written_issue(self, key: str) -> dict[str, Any]:
        try:
            return self.get_issue(key)
        except JiraError as exc:
            raise JiraAmbiguousError(
                "Jira write may have succeeded; readback could not be verified",
                key=key,
                status_code=exc.status_code,
                error=exc.error,
                retry_after=exc.retry_after,
            ) from None

    def _verify_updated_issue(
        self,
        updated: dict[str, Any],
        current: dict[str, Any],
        payload_fields: dict[str, Any],
    ) -> None:
        if self._project_key(updated) != self.project:
            raise JiraAmbiguousError("Jira update readback did not match", key=updated["key"])
        if self._status_category_or_error(updated, key=updated["key"]) == "done":
            raise JiraAmbiguousError("Jira update readback did not match", key=updated["key"])
        if "description" in payload_fields and self._adf_signature(updated["fields"].get("description")) != self._adf_signature(payload_fields["description"]):
            raise JiraAmbiguousError("Jira update readback did not match", key=updated["key"])
        if "labels" in payload_fields and not self._same_labels(updated["fields"].get("labels"), payload_fields["labels"]):
            raise JiraAmbiguousError("Jira update readback did not match", key=updated["key"])
        if current["key"] != updated["key"]:
            raise JiraAmbiguousError("Jira update readback did not match", key=updated["key"])

    @staticmethod
    def _validate_labels(labels: Any) -> list[str]:
        if not isinstance(labels, list):
            raise ValueError("labels must be a list")
        normalized: list[str] = []
        seen: set[str] = set()
        for label in labels:
            value = JiraNotifier._label(label)
            if value in seen:
                raise ValueError("labels must be unique")
            seen.add(value)
            normalized.append(value)
        return normalized

    @staticmethod
    def _validate_extra_fields(extra_fields: Any) -> dict[str, Any]:
        if extra_fields is None:
            return {}
        if not isinstance(extra_fields, dict):
            raise ValueError("extra_fields must be a mapping")
        protected = {"project", "issuetype", "summary", "description", "labels", "fields", "properties"}
        for key in extra_fields:
            if key in protected:
                raise ValueError("extra_fields must not override protected fields")
        return copy.deepcopy(extra_fields)

    @staticmethod
    def _escape_jql(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"')
