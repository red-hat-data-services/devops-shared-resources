"""GAP service facades, bounded issue content and pure managed ADF sections.

JiraNotifier enforces explicitly selected origin/project/Bug scope, atomic initial properties,
restricted content and stronger readbacks; completed issues are not reopened or
content-edited. SlackNotifier preserves explicit-channel sends and mutable
configuration; its callers supply the configured notification destination. Generic Jira,
Slack and ADF operations remain outside this package.

Content helpers escape/bound supplied evidence, render the packaged issue
Markdown template and delegate restricted ADF conversion. Managed discussion
and metadata sections are cosmetic preparation/inspection, not routing data.
Pure helpers require no authenticated instance or network. Callers own registry
selection, lifecycle, deduplication and persistence; this module imports no
lifecycle or lookup execution.
"""
from __future__ import annotations

import copy
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit
from datetime import datetime, timezone
import unicodedata
from lib.jira_client import (JiraClient, JiraError, JiraRateLimitError, JiraTransientError,
    JiraAmbiguousError, JiraPermanentError, JiraValidationError)
from lib import jira_adf
from lib.slack_client import (
    SLACK_CHAT_POST_MESSAGE,
    SLACK_MAX_RETRY_AFTER,
    SlackAmbiguousError,
    SlackClient,
    SlackError,
    SlackPermanentError,
    SlackRateLimitError,
    SlackTransientError,
)
from importlib.resources import files
from jinja2 import Environment, StrictUndefined


@dataclass(frozen=True)
class JiraTarget:
    """Allowlisted destination; evidence never selects credentials or routing."""
    name: str
    url: str
    banner: str

    @property
    def scope(self):
        return self.url.removeprefix("https://") + "/RHAI/" + configured_channel()

    @property
    def options(self):
        # Preserve the default call shape for existing injected staging factories.
        return {} if self.name == "stage" else {"jira_environment": self.name}


def configured_channel(channel=None):
    """Require an explicit configured Slack ID, never infer it from evidence."""
    if channel is None:
        import os
        channel = os.environ.get("GAP_SLACK_CHANNEL")
    if not isinstance(channel, str) or not re.fullmatch(r"[CG][A-Z0-9]+", channel):
        raise ValueError("GAP_SLACK_CHANNEL must be a nonblank Slack channel ID")
    return channel


@dataclass(frozen=True)
class RoutingTarget:
    jira: JiraTarget
    channel: str

    def __getattr__(self, name):
        return getattr(self.jira, name)

    @property
    def scope(self):
        return self.jira.url.removeprefix("https://") + "/RHAI/" + self.channel

    @property
    def options(self):
        return {**self.jira.options, "channel": self.channel}


def routing_target(name="stage", channel=None):
    return RoutingTarget(jira_target(name), configured_channel(channel))


def jira_target(name="stage"):
    """Resolve an explicit selector without reading environment variables or secrets."""
    if not isinstance(name, str) or name not in {"stage", "production"}:
        raise ValueError("Jira environment must be stage or production")
    host = "stage-redhat.atlassian.net" if name == "stage" else "redhat.atlassian.net"
    return JiraTarget(name, "https://" + host, "GAP STAGING TEST" if name == "stage" else "GAP PRODUCTION")


STAGING_JIRA_ORIGIN = "https://stage-redhat.atlassian.net"


def jira_target_from_url(url=STAGING_JIRA_ORIGIN):
    """Map only allowlisted operator origins to the existing internal identity."""
    if not isinstance(url, str):
        raise ValueError("GAP_JIRA_URL must be an allowlisted HTTPS origin")
    for name in ("stage", "production"):
        target = jira_target(name)
        if url in (target.url, target.url + "/"):
            return target
    raise ValueError("GAP_JIRA_URL must be an allowlisted HTTPS origin")


STAGING_JIRA_PROJECT = "RHAI"
STAGING_JIRA_ISSUE_TYPE_BUG = "Bug"
STAGING_JIRA_ISSUE_TYPE_ID = "10016"
ISSUE_KEY_RE = re.compile(r"RHAI-[0-9]+\Z")
LABEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
SAFE_ERROR_CODE_RE = re.compile(r"[A-Z0-9_:-]{1,64}\Z")
VALID_MARK_TYPES = jira_adf.VALID_MARK_TYPES
VALID_NODE_TYPES = jira_adf.VALID_NODE_TYPES
SEARCH_FIELDS = ["summary", "description", "labels", "status", "project", "issuetype"]
NOTIFICATION_STATE_PROPERTY_KEY = "gap.notification.v1"
MAX_SEARCH_PAGE_SIZE = 50
MAX_SEARCH_PAGES = 20
MAX_JSON_DEPTH = 8
MAX_JSON_NODES = 256
MAX_JSON_STRING_LENGTH = 4096
MAX_JSON_LIST_LENGTH = 128
MAX_JSON_DICT_KEYS = 128


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

    jira_environment: str = "stage"

    def __post_init__(self) -> None:
        self.base_url = self._normalize_base_url(self.base_url, jira_environment=self.jira_environment)
        self._validate_project(self.project)
        self._validate_issue_type(self.issue_type, jira_environment=self.jira_environment)
        self._validate_credential(self.email, "email")
        self._validate_credential(self.token, "token")
        self._validate_timeout(self.timeout)
        self.session = self._client.session

    @property
    def _client(self):
        # Public session/auth/timeout fields were mutable in the original facade.
        # Derive a lightweight delegate from current values, not stale copies.
        self._validate_issue_type(self.issue_type, jira_environment=self.jira_environment)
        return JiraClient(self._normalize_base_url(self.base_url, jira_environment=self.jira_environment), self.email, self.token,
                          session=self.session, timeout=self.timeout)

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
    def _validate_issue_type(issue_type: Any, *, jira_environment="stage") -> None:
        target = jira_target(jira_environment)
        allowed = {STAGING_JIRA_ISSUE_TYPE_BUG}
        if target.name == "stage":
            allowed.add(STAGING_JIRA_ISSUE_TYPE_ID)
        if issue_type not in allowed:
            raise ValueError("issue_type must be Bug; only staging also accepts 10016")

    @staticmethod
    def _normalize_base_url(base_url: Any, *, jira_environment="stage") -> str:
        target = jira_target(jira_environment)
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("base_url must be a non-empty string")
        split = urlsplit(base_url.strip())
        if split.scheme != "https":
            raise ValueError("base_url must use https")
        if split.username or split.password:
            raise ValueError("base_url must not include credentials")
        if split.hostname != urlsplit(target.url).hostname:
            raise ValueError("base_url must match the selected Jira environment")
        if split.port is not None:
            raise ValueError("base_url must not include a port")
        if split.query or split.fragment:
            raise ValueError("base_url must not include query or fragment components")
        if split.path not in {"", "/"}:
            raise ValueError("base_url must not include a path")
        return target.url

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

    # Preserve old pure helper entry points without constructing a client.
    _response_json = staticmethod(JiraClient._response_json)
    _retry_after = staticmethod(JiraClient._retry_after)
    _safe_error = staticmethod(JiraClient._safe_error)
    _rate_limited = staticmethod(JiraClient._rate_limited)
    _validation_details = staticmethod(JiraClient._validation_details)

    @staticmethod
    def _validate_adf(node: Any, *, path: str = "description") -> None:
        jira_adf.validate_adf(node, path=path)

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
        return jira_adf.adf_signature(node)

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
        data = self._client.get_issue(key, fields=SEARCH_FIELDS)
        issue = self._issue_fields(data)
        if issue["key"] != key:
            raise JiraAmbiguousError("Jira issue response could not be verified")
        return issue

    def find_by_label(self, label: str) -> list[dict[str, Any]]:
        label = self._label(label)
        jql = f'project = {self.project} AND labels = "{self._escape_jql(label)}" ORDER BY key ASC'
        return [self._issue_fields(issue) for issue in self._client.search(jql, fields=SEARCH_FIELDS)]

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
        data = self._client.create_issue(payload_fields, properties=payload.get("properties"))
        key = data["key"]
        if ISSUE_KEY_RE.fullmatch(key) is None:
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
        data = self._client.get_issue_property(key, NOTIFICATION_STATE_PROPERTY_KEY)
        if data is None:
            return None
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
        self._client.put_issue_property(key, NOTIFICATION_STATE_PROPERTY_KEY, normalized)
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

    def update_issue(self, key: str, *, description: dict | None = None, labels: list[str] | None = None,
                     add_labels: list[str] | None = None) -> dict[str, Any]:
        """Replace labels when requested, or atomically add without snapshot replacement."""
        if labels is not None and add_labels is not None:
            raise ValueError("labels and add_labels are mutually exclusive")
        additions = [] if add_labels is None else self._validate_labels(add_labels)
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
        if not payload_fields and not additions:
            return current
        payload = {"fields": payload_fields}
        if additions:
            payload["update"] = {"labels": [{"add": label} for label in additions]}
        self._client.update_issue(key, fields=payload_fields, update=payload.get("update"))
        updated = self._readback_written_issue(key)
        self._verify_updated_issue(updated, current, payload_fields)
        if not set(additions).issubset(updated["fields"].get("labels", [])):
            raise JiraAmbiguousError("Jira additive label readback did not match", key=key)
        return updated

    def transitions(self, key: str) -> list[dict[str, Any]]:
        key = self._issue_key(key)
        return self._client.transitions(key)

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
        self._client.apply_transition(key, transition_id, fields=payload.get("fields"))
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


@dataclass
class SlackNotifier:
    token: str = field(repr=False)
    channel: str
    session: Any = None
    timeout: int = 20

    def __post_init__(self) -> None:
        self._validate_inputs()
        self.session = self._client().session

    def _validate_inputs(self) -> None:
        if not isinstance(self.token, str) or not self.token.strip():
            raise ValueError("token must be a non-empty string")
        if not isinstance(self.channel, str) or not self.channel.strip():
            raise ValueError("channel must be a non-empty string")
        if self.session is not None and not hasattr(self.session, "post"):
            raise TypeError("session must provide post()")
        if not isinstance(self.timeout, (int, float)) or self.timeout <= 0:
            raise ValueError("timeout must be positive")

    def _client(self) -> SlackClient:
        return SlackClient(self.token, session=self.session, timeout=self.timeout)

    def send(self, text: str, *, thread_ts: str | None = None, client_msg_id: str | None = None) -> dict[str, str]:
        self._validate_inputs()
        return self._client().post_message(
            self.channel,
            text,
            thread_ts=thread_ts,
            client_msg_id=client_msg_id,
        )


def _slack_text(value, limit=160):
    """Bound literal inline-code text; neutralize mentions, code fences and controls."""
    text = " ".join((value if isinstance(value, str) else "Unavailable").split())
    text = "".join(c for c in text if not unicodedata.category(c).startswith("C"))
    text = text or "Unavailable"
    if len(text) > limit:
        text = text[:limit - 1] + "…"
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("`", "ˋ")


def _slack_link(value, label):
    """Optional bounded HTTPS link with a static label, never evidence-supplied markup."""
    try:
        url = https_url(value)
    except ValueError:
        return None
    if any(unicodedata.category(c).startswith("C") for c in url):
        return None
    url = url.replace("|", "%7C").replace("&", "&amp;")
    if len(url) > 700:
        return None
    return f"<{url}|{label}>"


def _slack_commit_link(observation, run):
    """Link only a full selected-build SHA agreeing with the receipt's revision/repository."""
    evidence = observation["evidence"]
    sha = run.get("head_sha")
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha) or sha != evidence.get("head-sha"):
        return None

    def repository(value):
        if not isinstance(value, str):
            return None
        value = value.lower().removeprefix("github.com/")
        if not re.fullmatch(r"[a-z0-9_-]+/[a-z0-9_.-]+", value) or value.split("/")[1] in {".", ".."}:
            return None
        return value

    repo = repository(observation.get("repository"))
    if repo is None or ("repository" in evidence and repository(evidence["repository"]) != repo):
        return None
    return _slack_link(f"https://github.com/{repo}/commit/{sha}", f"Commit {sha[:7]}")


def _slack_build_time(run, status):
    """Display the selected run's completion time, never the polling/creation time."""
    value = "Unavailable"
    try:
        completed = datetime.fromisoformat(run.get("completed_at", "").replace("Z", "+00:00"))
        if completed.tzinfo is not None:
            value = completed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except (ValueError, TypeError, AttributeError, OverflowError):
        pass
    label = "Build failed at" if status == "failure" else "Build completed at"
    return f"*{label}:* {value}"


def _slack_merge_links(observation):
    """Construct merge-evidence links from bound identities, never supplied URL overrides."""
    if observation.get("category") not in {"conflict", "feasibility"}:
        return []
    evidence = observation["evidence"]
    if evidence.get("source") != "github-merge-receipt" or evidence.get("status") != observation.get("status"):
        return []
    repo = observation.get("repository")
    if not isinstance(repo, str):
        return []
    repo = repo.lower().removeprefix("github.com/")
    if not re.fullmatch(r"[a-z0-9_-]+/[a-z0-9_.-]+", repo) or repo.split("/")[1] in {".", ".."}:
        return []
    evidence_repo = evidence.get("repository")
    pr_url, sha = evidence.get("pr_url"), evidence.get("head-sha")
    if (not isinstance(evidence_repo, str) or evidence_repo.lower().removeprefix("github.com/") != repo
            or not isinstance(pr_url, str)
            or not re.fullmatch(r"https://github\.com/" + re.escape(repo) + r"/pull/[1-9][0-9]*", pr_url.lower())
            or not isinstance(sha, str) or not re.fullmatch(r"[a-fA-F0-9]{40}", sha)):
        return []
    links = [_slack_link(f"https://github.com/{repo}/commit/{sha}", f"Commit {sha[:7]}")]
    facts = evidence.get("merge-receipt")
    if observation["category"] == "feasibility" and isinstance(facts, dict):
        run_id, attempt = facts.get("run-id"), facts.get("attempt")
        if (type(run_id) is int and run_id > 0 and type(attempt) is int and attempt > 0
                and facts.get("head-sha") == sha and facts.get("run-head-sha") == sha):
            links.append(_slack_link(f"https://github.com/{repo}/actions/runs/{run_id}/attempts/{attempt}",
                                     "Feasibility run"))
    return links


def slack_message(observation, *, jira_url, reply=False, jira_environment="stage", channel=None):
    """Render an explicitly scoped notification without clients, lookups or delivery-state changes.

    Only selected evidence is displayed; this is not a general secret scrubber.
    Existing roots/receipts, not message content, determine threading and replay.
    Optional unsafe/oversized URLs are omitted rather than emitted as Slack markup.
    """
    evidence = observation["evidence"]
    category, status = observation["category"], observation["status"]
    failure = {"build": "Build failed", "conflict": "Merge conflict", "feasibility": "Promotion blocked"}
    success = {"build": "Successful build observed", "conflict": "Conflict resolved", "feasibility": "Feasibility restored"}
    if status not in {"failure", "success"}:
        raise ValueError("Slack notifications require a definitive outcome")
    heading = success[category] if status == "success" else ("Failure update" if reply else failure[category])
    target = jira_target(jira_environment)
    destination = "staging test" if target.name == "stage" else "production Jira"
    lines = [f"{'🟢' if status == 'success' else '🔴'} *GAP · {heading} — {destination}*"]
    if category == "build":
        lines.append(f"*Component:* `{_slack_text(observation.get('component'))}`")
    if not reply:
        lines.append(f"*Repository:* `{_slack_text(observation.get('repository'))}`")
    run = evidence.get("selected-run")
    run = run if isinstance(run, dict) else {}
    if category == "build":
        lines.append(_slack_build_time(run, status))
    reason = evidence.get("reason") or run.get("reason")
    if isinstance(reason, str) and reason.strip() and reason.strip().lower() not in {"failed", "failure", "success", "succeeded", "completed"}:
        lines.append(f"*Reported reason:* `{_slack_text(reason, 240)}`")
    jira_key = re.search(r"/browse/(RHAI-[1-9][0-9]{0,15})$", jira_url) if isinstance(jira_url, str) else None
    jira = _slack_link(jira_url, f"Jira {jira_key[1]}" if jira_key else "Jira")
    if jira is None:
        raise ValueError("a safe Jira link is required")
    links = [
        _slack_link(evidence.get("build_url"), "Successful build" if status == "success" else "Failed build") if category == "build" else None,
        _slack_commit_link(observation, run) if category == "build" else None,
        *_slack_merge_links(observation),
        _slack_link(evidence.get("pr_url"), "Child PR"), jira,
    ]
    if not reply:
        links.append(_slack_link(evidence.get("leader_url"), "GAP Leader"))
    lines.extend(["", " · ".join(link for link in links if link)])
    if evidence.get("synthetic") is True:
        lines.extend(["", "Synthetic evidence"])
    if status == "success":
        lines.append("Staging result only; does not establish production recovery." if target.name == "stage"
                     else "Reported component outcome only; not a claim that the whole promotion is healthy.")
    return "\n".join(lines)


def literal(value):
    if value is None or value == "":
        value = "Unavailable"
    text = " ".join(str(value).split())
    if len(text) > 2000:
        raise ValueError("presentation field exceeds 2000 characters")
    return jira_adf.escape_literal(text)


def https_url(value):
    if not isinstance(value, str) or len(value) > 4096:
        raise ValueError("invalid evidence URL")
    return jira_adf.https_url(value)


def _inline(text):
    return jira_adf._inline(text, url_validator=https_url)


def markdown_to_adf(markdown):
    # Preserve the old direct export\'s GAP URL length bound too.
    return jira_adf.markdown_to_adf(markdown, url_validator=https_url)


def category_labels(category):
    return {"build": ["gap-build-failure"],
            "conflict": ["gap-sync-failure", "gap-merge-conflict"],
            "feasibility": ["gap-sync-failure", "gap-feasibility-failure"]}[category]


def render_markdown(observation, *, discussion_url=None, metadata_url=None, jira_environment="stage", channel=None):
    target = jira_target(jira_environment)
    e = observation["evidence"]
    run = e.get("selected-run") or {}
    creator = e.get("creator-outcome") or {}
    context = {key: literal(observation.get(key)) for key in (
        "repository", "category", "source_branch", "destination_branch", "component", "pipeline", "observation_id")}
    context.update(notification_heading=target.banner,
                   destination_notice=("Staging notification only. This does not establish production failure or recovery."
                                       if target.name == "stage" else
                                       "Production Jira destination. Outcomes reflect only the explicitly supplied evidence."),
                   pipeline=literal(observation.get("pipeline") or run.get("pipeline")),
                   reason=literal(e.get("reason") or run.get("reason") or creator.get("reason")),
                   note=literal(e.get("note")),
                   synthetic=literal(e.get("synthetic")), historical=literal(e.get("historical_snapshot")),
                   revision=literal(e.get("head_sha") or e.get("head-sha")), targets=[], links=[],
                   discussion_url=https_url(discussion_url) if discussion_url else None,
                   metadata_url=https_url(metadata_url) if metadata_url else None)
    targets = e.get("affected_targets", [])
    if not isinstance(targets, list) or len(targets) > 40:
        raise ValueError("affected_targets must be a list of at most 40 entries")
    for target in targets:
        if not isinstance(target, dict) or not re.fullmatch(r"rhoai-[A-Za-z0-9._-]+", str(target.get("branch", ""))):
            raise ValueError("affected target requires an explicit rhoai-X branch")
        context["targets"].append({k: literal(target.get(k)) for k in ("branch", "reason", "component", "pipeline")})
    for label, value in [("Child PR", e.get("pr_url")), ("Leader", e.get("leader_url")),
                         ("Build logs or PipelineRun", e.get("build_url")), ("State snapshot", e.get("state_url")),
                         ("Creator workflow", e.get("workflow_url") or creator.get("workflow-url")),
                         ("Creator PR", creator.get("pr-url"))]:
        if value:
            context["links"].append({"label": label, "url": https_url(value)})
    template = files("lib.gap_notifications").joinpath("templates/issue.md.j2").read_text(encoding="utf-8")
    rendered = Environment(undefined=StrictUndefined, autoescape=False, keep_trailing_newline=True).from_string(template).render(**context)
    if len(rendered) > 24000:
        raise ValueError("presentation exceeds 24000 characters")
    return rendered


def description(observation, *, discussion_url=None, metadata_url=None, jira_environment="stage", channel=None):
    return markdown_to_adf(render_markdown(observation, discussion_url=discussion_url, metadata_url=metadata_url,
                                         **jira_target(jira_environment).options))


SLACK_ORIGIN = "https://redhat-internal.slack.com"
HEADING = {"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": "Slack discussion"}]}


def section(url, *, metadata=False):
    heading = copy.deepcopy(HEADING)
    if metadata:
        heading["content"][0]["text"] = "Notification metadata"
    content = [{"type": "text", "text": "View notification metadata (JSON — source of truth)" if metadata else "Open Slack discussion",
                "marks": [{"type": "link", "attrs": {"href": url}}]}]
    if metadata:
        content.append({"type": "text", "text": " Jira permissions required."})
    return [heading, {"type": "paragraph", "content": content}]


def _managed_indices(body, *, metadata=False, jira_environment="stage", channel=None):
    if not isinstance(body, dict) or body.get("type") != "doc" or not isinstance(body.get("content"), list):
        raise ValueError("description unavailable")
    nodes = body["content"]
    indices = [i for i, n in enumerate(nodes) if n.get("type") == "heading" and
               any(c.get("text") == ("Notification metadata" if metadata else "Slack discussion") for c in n.get("content", []))]
    if len(indices) > 1:
        raise ValueError("ambiguous discussion sections")
    if indices:
        i = indices[0]
        if i + 1 >= len(nodes) or nodes[i] != section("", metadata=metadata)[0]:
            raise ValueError("edited discussion section")
        paragraph = nodes[i + 1]
        try:
            url = paragraph["content"][0]["marks"][0]["attrs"]["href"]
            pattern = (re.escape(jira_target(jira_environment).url) + r"/rest/api/3/issue/RHAI-[1-9][0-9]*/properties/gap\.notification\.v1"
                       if metadata else re.escape(SLACK_ORIGIN) + r"/archives/" + re.escape(configured_channel(channel)) + r"/p[0-9]+")
            if not re.fullmatch(pattern, url) or paragraph != section(url, metadata=metadata)[1]:
                raise ValueError("edited discussion section")
        except (KeyError, IndexError, TypeError):
            raise ValueError("edited discussion section") from None
    return indices


def project_body(body, url, *, metadata=None, jira_environment="stage", channel=None):
    updated = copy.deepcopy(body)
    # Plan both sections before returning any mutation; edits/duplicates fail closed.
    for value, is_metadata in ((url, False), (metadata, True)):
        if value is None:
            continue
        indices = _managed_indices(updated, metadata=is_metadata, **routing_target(jira_environment, channel).options)
        nodes = section(value, metadata=is_metadata)
        if indices:
            updated["content"][indices[0]:indices[0] + 2] = nodes
        else:
            updated["content"].extend(nodes)
    JiraNotifier._validate_adf(updated)
    return updated


def visible_link_status(body, url, *, metadata=False, jira_environment="stage", channel=None):
    try:
        indices = _managed_indices(body, metadata=metadata, **routing_target(jira_environment, channel).options)
        if not indices:
            return "missing"
        return "match" if body["content"][indices[0] + 1] == section(url, metadata=metadata)[1] else "mismatch"
    except ValueError:
        return "ambiguous_or_edited"
