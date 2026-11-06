"""Staging Jira adapter for the GAP notification prototype."""

from __future__ import annotations

import copy
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from lib.jira_client import (JiraClient, JiraError, JiraRateLimitError, JiraTransientError,
    JiraAmbiguousError, JiraPermanentError, JiraValidationError)
from lib import jira_adf

STAGING_JIRA_ORIGIN = "https://stage-redhat.atlassian.net"
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

    def __post_init__(self) -> None:
        self.base_url = self._normalize_base_url(self.base_url)
        self._validate_project(self.project)
        self._validate_issue_type(self.issue_type)
        self._validate_credential(self.email, "email")
        self._validate_credential(self.token, "token")
        self._validate_timeout(self.timeout)
        self.session = self._client.session

    @property
    def _client(self):
        # Public session/auth/timeout fields were mutable in the original facade.
        # Derive a lightweight delegate from current values, not stale copies.
        return JiraClient(self._normalize_base_url(self.base_url), self.email, self.token,
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
