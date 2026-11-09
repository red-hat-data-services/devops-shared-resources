"""Tests for notify_tools responsibilities."""
from __future__ import annotations

from gap_notification_fakes import observation
from service_client_fakes import FakeResponse, FakeSession, notifier_for, issue_for
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch
import requests
from lib.gap_notifications.dispatch_incidents import _description as dispatch_description
from lib.jira_client import JiraClient
from lib.gap_notifications.notify_tools import (
    STAGING_JIRA_ORIGIN,
    JiraAmbiguousError,
    JiraNotifier,
    JiraPermanentError,
    JiraRateLimitError,
    JiraTerminalIssueError,
    JiraTransientError,
    JiraValidationError,
)
from unittest.mock import Mock, patch
from lib import slack_client
from lib.gap_notifications import notify_tools
from lib.gap_notifications.notify_tools import SlackNotifier
from lib.gap_notifications.dispatch_incidents import discussion_url, metadata_url
from lib.gap_notifications.notify_tools import project_body, section, visible_link_status
from gap_notification_fakes import make_runner
import copy
import json
import pytest
from jinja2 import UndefinedError
import lib.gap_notifications.notify_tools as presentation
from lib.gap_notifications.dispatch_incidents import _description, _summary
from lib.gap_notifications.notify_tools import JiraNotifier
from lib.gap_notifications.notify_tools import category_labels, render_markdown
from gap_notification_fakes import make_runner, observation
from lib.gap_notifications import dispatch_incidents as dispatch
from lib.gap_notifications.notify_tools import slack_message


# GAP Jira facade

class JiraNotifierTests(unittest.TestCase):
    def _notifier(self, session):
        return notifier_for(session)

    def _trial_observation(self):
        return {
            "environment": "staging-trial",
            "repository": "redhat-data-services/gated-artifacts-promoter",
            "source_branch": "feature/trial-failure",
            "destination_branch": "main",
            "category": "build",
            "component": "leader",
            "pipeline": "gated-artifacts-promoter",
            "status": "failure",
            "observation_id": "trial-failure-001",
            "evidence": {
                "leader_url": "https://github.com/red-hat-data-services/gated-artifacts-promoter/pull/84",
                "note": "Dispatcher-generated trial failure evidence.",
                "test_only": True,
                "synthetic": False,
                "historical_snapshot": True,
            },
        }

    def _trial_description(self):
        return dispatch_description(self._trial_observation())

    def _notification_state(self, *, outcome="failure", root=None):
        return {
            "schema": 1,
            "scope": "staging-trial",
            "trial_id": "trial-42",
            "incident_key": "{\"category\":\"build\"}",
            "episode_id": "0db0a3f6-8df8-4d48-bb6f-6d60f2e7f9b1",
            "last_event": None,
            "last_outcome": outcome,
            "seen_event_ids": {"sha256:abcd": "failure"},
            "jira_intent": {"event_id": "sha256:abcd", "outcome": outcome},
            "delivery": {"root": root, "last_event": None, "intent": None},
        }

    def _issue(self, key="RHAI-1", *, summary="summary", description=None, labels=None, status_category="new"):
        if description is None:
            description = self._trial_description()
        return issue_for(key, summary=summary, description=description, labels=labels, status_category=status_category)

    # The staging facade must reject unsafe or unapproved Jira destinations.
    def test_constructor_rejects_invalid_destination(self):
        session = FakeSession()
        for base_url in (
            "http://stage-redhat.atlassian.net",
            "https://user:pass@stage-redhat.atlassian.net",
            "https://evil.example.net",
            "https://stage-redhat.atlassian.net/path",
            "https://stage-redhat.atlassian.net?x=1",
        ):
            with self.subTest(base_url=base_url):
                with self.assertRaises(ValueError):
                    JiraNotifier(base_url, "RHAI", "Bug", "jira-bot@example.com", "secret-token", session=session)

    # The staging facade must reject other projects and issue types.
    def test_constructor_rejects_wrong_project_or_issue_type(self):
        session = FakeSession()
        with self.assertRaises(ValueError):
            JiraNotifier(STAGING_JIRA_ORIGIN, "OTHER", "Bug", "jira-bot@example.com", "secret-token", session=session)
        with self.assertRaises(ValueError):
            JiraNotifier(STAGING_JIRA_ORIGIN, "RHAI", "Task", "jira-bot@example.com", "secret-token", session=session)

    # Session setup must disable ambient discovery and keep credentials out of object
    # representations.
    def test_constructor_disables_trust_env_and_hides_credentials_in_repr(self):
        session = FakeSession()
        notifier = self._notifier(session)
        self.assertFalse(session.trust_env)
        self.assertEqual(notifier.base_url, STAGING_JIRA_ORIGIN)
        self.assertNotIn("secret-token", repr(notifier))
        self.assertNotIn("jira-bot@example.com", repr(notifier))

    # Issue reads must request the required fields with explicit authentication and no
    # redirects.
    def test_get_issue_returns_requested_fields(self):
        issue = self._issue()
        session = FakeSession(responses={"get": [FakeResponse(issue, 200)]})
        notifier = self._notifier(session)

        result = notifier.get_issue("RHAI-1")

        self.assertEqual(result, issue)
        method, url, kwargs = session.calls[0]
        self.assertEqual(method, "get")
        self.assertEqual(url, f"{STAGING_JIRA_ORIGIN}/rest/api/3/issue/RHAI-1")
        self.assertEqual(kwargs["params"]["fields"], "summary,description,labels,status,project,issuetype")
        self.assertEqual(kwargs["auth"], ("jira-bot@example.com", "secret-token"))
        self.assertFalse(kwargs["allow_redirects"])

    # An absent notification property is represented as missing, not as an empty valid registry.
    def test_get_notification_state_missing_returns_none(self):
        session = FakeSession(responses={"get": [FakeResponse({"error": "not found"}, 404)]})
        notifier = self._notifier(session)

        result = notifier.get_notification_state("RHAI-1")

        self.assertIsNone(result)
        self.assertEqual(session.calls[0][1], f"{STAGING_JIRA_ORIGIN}/rest/api/3/issue/RHAI-1/properties/gap.notification.v1")

    # A valid property envelope must return its stored notification state.
    def test_get_notification_state_reads_property_value(self):
        state = self._notification_state()
        session = FakeSession(responses={"get": [FakeResponse({"key": "gap.notification.v1", "value": state}, 200)]})
        notifier = self._notifier(session)

        result = notifier.get_notification_state("RHAI-1")

        self.assertEqual(result, state)

    # A malformed property envelope must retain the issue key in a safe ambiguity error.
    def test_get_notification_state_rejects_malformed_property_shape(self):
        session = FakeSession(responses={"get": [FakeResponse({"value": self._notification_state()}, 200)]})
        notifier = self._notifier(session)

        with self.assertRaises(JiraAmbiguousError) as raised:
            notifier.get_notification_state("RHAI-1")

        self.assertEqual(raised.exception.key, "RHAI-1")

    # A property for the wrong key must not be accepted as notification state.
    def test_get_notification_state_rejects_wrong_property_key(self):
        session = FakeSession(responses={"get": [FakeResponse({"key": "gap.notification.other", "value": self._notification_state()}, 200)]})
        notifier = self._notifier(session)

        with self.assertRaises(JiraAmbiguousError) as raised:
            notifier.get_notification_state("RHAI-1")

        self.assertEqual(raised.exception.key, "RHAI-1")

    # Issue creation must preserve supplied fields and verify the created issue through
    # readback.
    def test_create_bug_success_readback_and_extra_fields(self):
        description = self._trial_description()
        create_response = FakeResponse({"key": "RHAI-2", "self": f"{STAGING_JIRA_ORIGIN}/rest/api/3/issue/RHAI-2"}, 201)
        readback = self._issue("RHAI-2", summary="new summary", description=description, labels=["triage", "gap-staging"], status_category="new")
        session = FakeSession(responses={"post": [create_response], "get": [FakeResponse(readback, 200)]})
        notifier = self._notifier(session)
        payload_description = deepcopy(description)

        result = notifier.create_bug(
            "new summary",
            payload_description,
            ["gap-staging", "triage"],
            extra_fields={"components": [{"id": "123", "name": "core"}]},
        )

        self.assertEqual(result, {"key": "RHAI-2", "url": f"{STAGING_JIRA_ORIGIN}/browse/RHAI-2"})
        self.assertEqual(len(session.calls), 2)
        method, url, kwargs = session.calls[0]
        self.assertEqual(method, "post")
        self.assertEqual(url, f"{STAGING_JIRA_ORIGIN}/rest/api/3/issue")
        self.assertEqual(kwargs["json"]["fields"]["project"], {"key": "RHAI"})
        self.assertEqual(kwargs["json"]["fields"]["issuetype"], {"name": "Bug"})
        self.assertEqual(kwargs["json"]["fields"]["components"], [{"id": "123", "name": "core"}])
        self.assertEqual(kwargs["json"]["fields"]["description"], description)
        self.assertEqual(session.calls[1][0], "get")
        self.assertEqual(payload_description, description)

    # Initial registry state must be created atomically with the issue and then verified.
    def test_create_bug_with_notification_state_sends_and_verifies_property(self):
        description = self._trial_description()
        state = self._notification_state()
        create_response = FakeResponse({"key": "RHAI-21", "self": f"{STAGING_JIRA_ORIGIN}/rest/api/3/issue/RHAI-21"}, 201)
        readback = self._issue("RHAI-21", summary="tracked summary", description=description, labels=["gap-staging"], status_category="new")
        session = FakeSession(
            responses={
                "post": [create_response],
                "get": [FakeResponse(readback, 200), FakeResponse({"key": "gap.notification.v1", "value": state}, 200)],
            }
        )
        notifier = self._notifier(session)

        result = notifier.create_bug("tracked summary", description, ["gap-staging"], notification_state=state)

        self.assertEqual(result, {"key": "RHAI-21", "url": f"{STAGING_JIRA_ORIGIN}/browse/RHAI-21"})
        self.assertEqual(session.calls[0][2]["json"]["properties"], [{"key": "gap.notification.v1", "value": state}])
        self.assertEqual(session.calls[1][0], "get")
        self.assertEqual(session.calls[2][1], f"{STAGING_JIRA_ORIGIN}/rest/api/3/issue/RHAI-21/properties/gap.notification.v1")

    # A mismatched initial property must preserve the created key for reconciliation.
    def test_create_bug_with_notification_state_preserves_key_on_property_mismatch(self):
        description = self._trial_description()
        state = self._notification_state()
        create_response = FakeResponse({"key": "RHAI-23", "self": f"{STAGING_JIRA_ORIGIN}/rest/api/3/issue/RHAI-23"}, 201)
        readback = self._issue("RHAI-23", summary="tracked summary", description=description, labels=["gap-staging"], status_category="new")
        mismatched = self._notification_state(outcome="success")
        session = FakeSession(
            responses={
                "post": [create_response],
                "get": [FakeResponse(readback, 200), FakeResponse({"key": "gap.notification.v1", "value": mismatched}, 200)],
            }
        )
        notifier = self._notifier(session)

        with self.assertRaises(JiraAmbiguousError) as raised:
            notifier.create_bug("tracked summary", description, ["gap-staging"], notification_state=state)

        self.assertEqual(raised.exception.key, "RHAI-23")

    # Malformed property readback must preserve the created key rather than encourage another
    # create.
    def test_create_bug_with_notification_state_preserves_key_on_malformed_property_response(self):
        description = self._trial_description()
        state = self._notification_state()
        create_response = FakeResponse({"key": "RHAI-24", "self": f"{STAGING_JIRA_ORIGIN}/rest/api/3/issue/RHAI-24"}, 201)
        readback = self._issue("RHAI-24", summary="tracked summary", description=description, labels=["gap-staging"], status_category="new")
        session = FakeSession(
            responses={
                "post": [create_response],
                "get": [FakeResponse(readback, 200), FakeResponse({"key": "gap.notification.v1", "value": []}, 200)],
            }
        )
        notifier = self._notifier(session)

        with self.assertRaises(JiraAmbiguousError) as raised:
            notifier.create_bug("tracked summary", description, ["gap-staging"], notification_state=state)

        self.assertEqual(raised.exception.key, "RHAI-24")

    # Oversized property readback must report ambiguity without losing the created issue key.
    def test_create_bug_with_notification_state_preserves_key_on_oversized_property_response(self):
        description = self._trial_description()
        state = self._notification_state()
        create_response = FakeResponse({"key": "RHAI-25", "self": f"{STAGING_JIRA_ORIGIN}/rest/api/3/issue/RHAI-25"}, 201)
        readback = self._issue("RHAI-25", summary="tracked summary", description=description, labels=["gap-staging"], status_category="new")
        oversized = {"key": "gap.notification.v1", "value": {"blob": "x" * (24 * 1024 + 1)}}
        session = FakeSession(
            responses={
                "post": [create_response],
                "get": [FakeResponse(readback, 200), FakeResponse(oversized, 200)],
            }
        )
        notifier = self._notifier(session)

        with self.assertRaises(JiraAmbiguousError) as raised:
            notifier.create_bug("tracked summary", description, ["gap-staging"], notification_state=state)

        self.assertEqual(raised.exception.key, "RHAI-25")

    # A mismatched created issue must retain its key without leaking credentials in the error.
    def test_create_bug_readback_mismatch_preserves_key(self):
        description = self._issue()["fields"]["description"]
        create_response = FakeResponse({"key": "RHAI-3", "self": f"{STAGING_JIRA_ORIGIN}/rest/api/3/issue/RHAI-3"}, 201)
        mismatch = self._issue("RHAI-3", summary="different summary", description=description, labels=["gap-staging"])
        session = FakeSession(responses={"post": [create_response], "get": [FakeResponse(mismatch, 200)]})
        notifier = self._notifier(session)

        with self.assertRaises(JiraAmbiguousError) as raised:
            notifier.create_bug("expected summary", description, ["gap-staging"])

        self.assertEqual(raised.exception.key, "RHAI-3")
        self.assertNotIn("secret-token", str(raised.exception))

    # A changed ADF link target must fail creation readback verification.
    def test_create_bug_rejects_href_change_in_adf_readback(self):
        description = self._issue()["fields"]["description"]
        create_response = FakeResponse({"key": "RHAI-20", "self": f"{STAGING_JIRA_ORIGIN}/rest/api/3/issue/RHAI-20"}, 201)
        changed = deepcopy(self._issue("RHAI-20", summary="summary", description=description, labels=["gap-staging"]))
        def change_link(node):
            for mark in node.get("marks", []):
                if mark["type"] == "link":
                    mark["attrs"]["href"] = "https://changed.invalid"
                    return True
            return any(change_link(child) for child in node.get("content", []))
        self.assertTrue(change_link(changed["fields"]["description"]))
        session = FakeSession(responses={"post": [create_response], "get": [FakeResponse(changed, 200)]})
        notifier = self._notifier(session)

        with self.assertRaises(JiraAmbiguousError):
            notifier.create_bug("summary", description, ["gap-staging"])

    # Definitive create validation errors must propagate unchanged without speculative
    # readbacks.
    def test_create_bug_propagates_validation_error_without_readback(self):
        session = FakeSession()
        notifier = self._notifier(session)
        description = self._issue()["fields"]["description"]
        error = JiraValidationError("rejected", status_code=400,
                                    field_errors=("description", "summary"), error_codes=("BAD_INPUT",))
        with patch.object(JiraClient, "create_issue", side_effect=error) as create, \
             patch.object(JiraClient, "get_issue") as get, \
             patch.object(JiraClient, "get_issue_property") as get_property:
            with self.assertRaises(JiraValidationError) as raised:
                notifier.create_bug("summary", description, ["gap-staging"])
            self.assertIs(raised.exception, error)
            create.assert_called_once()
            get.assert_not_called()
            get_property.assert_not_called()
        self.assertEqual(session.calls, [])

    # Issue-read errors from the reusable client must retain their exact exception identity.
    def test_get_issue_propagates_client_error_unchanged(self):
        session = FakeSession()
        notifier = self._notifier(session)
        error = JiraRateLimitError("back off", status_code=429, error="ratelimited", retry_after=2.5)
        with patch.object(JiraClient, "get_issue", side_effect=error) as get:
            with self.assertRaises(JiraRateLimitError) as raised:
                notifier.get_issue("RHAI-4")
            self.assertIs(raised.exception, error)
            get.assert_called_once_with("RHAI-4", fields=[
                "summary", "description", "labels", "status", "project", "issuetype"])
        self.assertEqual(session.calls, [])

    # A create timeout is ambiguous and must not trigger an automatic second request.
    def test_create_bug_timeout_is_ambiguous(self):
        session = FakeSession(side_effects={"post": [requests.Timeout("jira-bot@example.com secret-token") ]})
        notifier = self._notifier(session)
        description = self._issue()["fields"]["description"]

        with self.assertRaises(JiraAmbiguousError) as raised:
            notifier.create_bug("summary", description, ["gap-staging"])

        self.assertIsNone(raised.exception.key)
        self.assertNotIn("secret-token", str(raised.exception))
        self.assertEqual([method for method, _, _ in session.calls], ["post"])  # No readback.

    # A complete empty label search is a valid no-match result scoped to the staging project.
    def test_find_by_label_zero_matches_returns_empty_list(self):
        session = FakeSession(responses={"post": [FakeResponse({"issues": [], "isLast": True}, 200)]})
        notifier = self._notifier(session)

        result = notifier.find_by_label("gap-staging")

        self.assertEqual(result, [])
        self.assertEqual(session.calls[0][0], "post")
        self.assertEqual(session.calls[0][1], f"{STAGING_JIRA_ORIGIN}/rest/api/3/search/jql")
        self.assertEqual(session.calls[0][2]["json"], {
            "jql": 'project = RHAI AND labels = "gap-staging" ORDER BY key ASC',
            "fields": ["summary", "description", "labels", "status", "project", "issuetype"],
            "maxResults": 50,
        })
        self.assertNotIn("startAt", session.calls[0][2]["json"])

    # Label lookup must follow pagination tokens and return every matching issue.
    def test_find_by_label_paginates_completely(self):
        first = self._issue("RHAI-10")
        second = self._issue("RHAI-11")
        session = FakeSession(
            responses={
                "post": [
                    FakeResponse({"issues": [first], "isLast": False, "nextPageToken": "tok-1"}, 200),
                    FakeResponse({"issues": [second], "isLast": True}, 200),
                ]
            }
        )
        notifier = self._notifier(session)

        result = notifier.find_by_label("gap-staging")

        self.assertEqual(result, [first, second])
        self.assertEqual(session.calls[0][0], "post")
        self.assertEqual(session.calls[0][1], f"{STAGING_JIRA_ORIGIN}/rest/api/3/search/jql")
        self.assertEqual(session.calls[0][2]["json"]["jql"], 'project = RHAI AND labels = "gap-staging" ORDER BY key ASC')
        self.assertEqual(session.calls[0][2]["json"]["fields"], ["summary", "description", "labels", "status", "project", "issuetype"])
        self.assertEqual(session.calls[0][2]["json"]["maxResults"], 50)
        self.assertNotIn("nextPageToken", session.calls[0][2]["json"])
        self.assertEqual(session.calls[1][2]["json"]["nextPageToken"], "tok-1")

    # Label-search errors must propagate unchanged with the required search fields intact.
    def test_find_by_label_propagates_client_error_unchanged(self):
        session = FakeSession()
        notifier = self._notifier(session)
        error = JiraAmbiguousError("incomplete inventory")
        with patch.object(JiraClient, "search", side_effect=error) as search:
            with self.assertRaises(JiraAmbiguousError) as raised:
                notifier.find_by_label("gap-staging")
            self.assertIs(raised.exception, error)
            search.assert_called_once_with(
                'project = RHAI AND labels = "gap-staging" ORDER BY key ASC',
                fields=["summary", "description", "labels", "status", "project", "issuetype"])
        self.assertEqual(session.calls, [])

    # Ordinary issue updates must refuse closed tickets before sending a PUT.
    def test_update_issue_rejects_done_issue_without_put(self):
        session = FakeSession(responses={"get": [FakeResponse(self._issue("RHAI-13", status_category="done"), 200)]})
        notifier = self._notifier(session)

        with self.assertRaises(JiraTerminalIssueError):
            notifier.update_issue("RHAI-13", description=self._issue()["fields"]["description"])

        self.assertEqual([call[0] for call in session.calls], ["get"])

    # Missing status-category evidence must prevent an ordinary issue update.
    def test_update_issue_rejects_missing_status_category(self):
        issue = self._issue("RHAI-17")
        del issue["fields"]["status"]["statusCategory"]
        session = FakeSession(responses={"get": [FakeResponse(issue, 200)]})
        notifier = self._notifier(session)

        with self.assertRaises(JiraPermanentError):
            notifier.update_issue("RHAI-17", labels=["gap-staging"])

    # An open-issue update must verify that the requested changes were actually stored.
    def test_update_issue_changes_open_issue_and_verifies_readback(self):
        original = self._issue("RHAI-14", summary="before", labels=["gap-staging"])
        updated = self._issue("RHAI-14", summary="before", labels=["new-label", "gap-staging"])
        updated["fields"]["description"] = original["fields"]["description"]
        session = FakeSession(
            responses={
                "get": [FakeResponse(original, 200), FakeResponse(updated, 200)],
                "put": [FakeResponse({}, 204)],
            }
        )
        notifier = self._notifier(session)

        result = notifier.update_issue("RHAI-14", labels=["gap-staging", "new-label"])

        self.assertEqual(result, updated)
        self.assertEqual(session.calls[1][0], "put")
        self.assertEqual(session.calls[1][2]["json"]["fields"]["labels"], ["gap-staging", "new-label"])

    # Registry properties may update a closed issue without reopening or transitioning it.
    def test_put_notification_state_updates_closed_issue_without_reopening(self):
        original = self._issue("RHAI-22", status_category="done")
        state = self._notification_state(outcome="success")
        session = FakeSession(
            responses={
                "get": [
                    FakeResponse(original, 200),
                    FakeResponse({"key": "gap.notification.v1", "value": state}, 200),
                    FakeResponse(original, 200),
                ],
                "put": [FakeResponse({}, 204)],
            }
        )
        notifier = self._notifier(session)

        result = notifier.put_notification_state("RHAI-22", state)

        self.assertEqual(result, state)
        self.assertEqual(session.calls[1][0], "put")
        self.assertEqual(session.calls[1][2]["json"], state)
        self.assertEqual(session.calls[1][1], f"{STAGING_JIRA_ORIGIN}/rest/api/3/issue/RHAI-22/properties/gap.notification.v1")
        self.assertEqual(session.calls[2][1], f"{STAGING_JIRA_ORIGIN}/rest/api/3/issue/RHAI-22/properties/gap.notification.v1")

    # Oversized notification properties must be rejected before a write can be accepted.
    def test_put_notification_state_rejects_oversize_json(self):
        notifier = self._notifier(FakeSession())
        with self.assertRaises(ValueError):
            notifier.put_notification_state("RHAI-22", {"payload": "x" * (24 * 1024)})

    # Closure must use the selected done transition and its required fields, then verify status.
    def test_transition_to_done_uses_selected_done_transition(self):
        current = self._issue("RHAI-15", status_category="indeterminate")
        done = self._issue("RHAI-15", status_category="done")
        session = FakeSession(
            responses={
                "get": [FakeResponse(current, 200), FakeResponse({"transitions": [{"id": "31", "to": {"statusCategory": {"key": "done"}}}]}, 200), FakeResponse(done, 200)],
                "post": [FakeResponse({}, 204)],
            }
        )
        notifier = self._notifier(session)

        result = notifier.transition_to_done("RHAI-15", "31", fields={"resolution": {"id": "1"}})

        self.assertEqual(result["fields"]["status"]["statusCategory"]["key"], "done")
        self.assertEqual(session.calls[1][0], "get")
        self.assertEqual(session.calls[2][0], "post")
        self.assertEqual(session.calls[2][2]["json"], {"transition": {"id": "31"}, "fields": {"resolution": {"id": "1"}}})

    # An already closed issue requires no additional transition request.
    def test_transition_to_done_noops_when_already_done(self):
        done = self._issue("RHAI-16", status_category="done")
        session = FakeSession(responses={"get": [FakeResponse(done, 200)]})
        notifier = self._notifier(session)

        result = notifier.transition_to_done("RHAI-16", "31")

        self.assertEqual(result, done)
        self.assertEqual(len(session.calls), 1)

    # Closure must fail when the current status category cannot be established.
    def test_transition_to_done_rejects_missing_status_category(self):
        issue = self._issue("RHAI-18")
        del issue["fields"]["status"]["statusCategory"]
        session = FakeSession(responses={"get": [FakeResponse(issue, 200)]})
        notifier = self._notifier(session)

        with self.assertRaises(JiraPermanentError):
            notifier.transition_to_done("RHAI-18", "31")

    # Invalid summaries, labels, descriptions or extra fields must not pass create validation.
    def test_create_bug_rejects_invalid_inputs(self):
        session = FakeSession()
        notifier = self._notifier(session)
        description = self._issue()["fields"]["description"]
        with self.assertRaises(ValueError):
            notifier.create_bug("", description, ["gap-staging"])
        with self.assertRaises(ValueError):
            notifier.create_bug("summary", {"type": "doc", "version": 2, "content": []}, ["gap-staging"])
        with self.assertRaises(ValueError):
            notifier.create_bug("summary", description, ["bad label space"])
        with self.assertRaises(ValueError):
            notifier.create_bug("summary", description, ["gap-staging"], extra_fields={"project": {"key": "OTHER"}})

    # Validation and ambiguous errors must not expose the configured email or token.
    def test_no_secret_values_leak_from_validation_and_ambiguous_errors(self):
        session = FakeSession(side_effects={"post": [requests.ConnectionError("jira-bot@example.com secret-token")]})
        notifier = self._notifier(session)
        description = self._issue()["fields"]["description"]

        with self.assertRaises(JiraAmbiguousError) as raised:
            notifier.create_bug("summary", description, ["gap-staging"])

        self.assertNotIn("jira-bot@example.com", str(raised.exception))
        self.assertNotIn("secret-token", str(raised.exception))


    # A missing added label in readback must be treated as an ambiguous update.
    def test_additive_adapter_missing_label_readback_fails(self):
        current = self._issue(labels=['human'])
        session = FakeSession(responses={'get': [FakeResponse(current), FakeResponse(current)],
                                        'put': [FakeResponse(None, 204)]})
        with self.assertRaises(JiraAmbiguousError):
            self._notifier(session).update_issue('RHAI-1', add_labels=['gap-build-failure'])


    # Replacing and adding labels in the same call must fail before any request.
    def test_additive_adapter_rejects_replace_and_add_together(self):
        session = FakeSession()
        with self.assertRaises(ValueError):
            self._notifier(session).update_issue('RHAI-1', labels=['one'], add_labels=['two'])
        assert session.calls == []


    # Additive label updates must preserve a human label introduced after the original read.
    def test_additive_adapter_update_preserves_racing_human_label(self):
        adf = dispatch_description(observation())
        # The registry search snapshot lacks human-new. It appears before adapter GET.
        current = self._issue(description=adf, labels=['gap-registry-id', 'human-new'])
        updated = deepcopy(current)
        updated['fields']['labels'].extend(['gap-build-failure', 'human-even-newer'])
        session = FakeSession(responses={'get': [FakeResponse(current), FakeResponse(updated)],
                                        'put': [FakeResponse(None, 204)]})
        actual = self._notifier(session).update_issue('RHAI-1', description=adf, add_labels=['gap-build-failure'])
        assert set(actual['fields']['labels']) == set(updated['fields']['labels'])
        payload = next(kwargs['json'] for method, _, kwargs in session.calls if method == 'put')
        assert 'labels' not in payload['fields']
        assert payload['update']['labels'] == [{'add': 'gap-build-failure'}]


# GAP Slack facade

def session_for(channel="C123", ts="1700000000.123"):
    response = SimpleNamespace(
        status_code=200, headers={},
        json=lambda: {"ok": True, "channel": channel, "ts": ts},
    )
    return SimpleNamespace(post=Mock(return_value=response))


class SlackNotifierTests(unittest.TestCase):
    # The Slack facade must delegate arguments and preserve returned receipts and exception
    # identity.
    def test_send_delegates_arguments_receipt_and_error_instance(self):
        session = session_for()
        receipt = {"channel": "C123", "ts": "1700000000.123"}
        client = Mock(session=session)
        client.post_message.return_value = receipt
        with patch.object(notify_tools, "SlackClient", return_value=client) as factory:
            notifier = SlackNotifier("dummy-token", "C123", session=session, timeout=11)
            client.post_message.assert_not_called()
            factory.reset_mock()
            self.assertIs(notifier.send("hello", thread_ts="1.1", client_msg_id="msg-1"), receipt)
            factory.assert_called_once_with("dummy-token", session=session, timeout=11)
            client.post_message.assert_called_once_with("C123", "hello", thread_ts="1.1", client_msg_id="msg-1")
            self.assertNotIn("token", repr(notifier))

            # Facade must not swallow/reclassify the underlying error instance.
            error = slack_client.SlackAmbiguousError("safe ambiguity", status_code=503, error="unknown_error")
            client.post_message.side_effect = error
            with self.assertRaises(slack_client.SlackAmbiguousError) as raised:
                notifier.send("again")
            self.assertIs(raised.exception, error)
            self.assertEqual(client.post_message.call_count, 2)

    # Invalid channels must fail before even constructing a default session.
    def test_invalid_channel_is_rejected_before_session_factory_runs(self):
        for channel in (None, "", "   ", 123):
            with self.subTest(channel=channel):
                with patch("requests.Session") as factory:
                    with self.assertRaises((TypeError, ValueError)):
                        SlackNotifier("xoxb-secret-token", channel)
                    factory.assert_not_called()

    # The facade must initialize one public session and reuse it without ambient discovery.
    def test_constructor_initializes_public_session_once_and_reuses_it_for_multiple_sends(self):
        with patch("requests.Session") as factory:
            session = session_for()
            factory.return_value = session
            notifier = SlackNotifier("dummy-token", "C123")
            self.assertIs(notifier.session, session)
            self.assertFalse(session.trust_env)
            session.post.assert_not_called()
            first = notifier.send("hello")
            second = notifier.send("world")
        factory.assert_called_once()
        self.assertEqual(first, {"channel": "C123", "ts": "1700000000.123"})
        self.assertEqual(second, first)
        self.assertEqual(session.post.call_count, 2)

    # Updated public credentials, channel, session and timeout must govern the next send.
    def test_send_uses_updated_public_fields_and_does_not_touch_original_session(self):
        original_session = session_for("C111")
        replacement_session = session_for("C456")
        notifier = SlackNotifier("dummy-first", "C123", session=original_session, timeout=11)
        notifier.token = "dummy-updated"
        notifier.channel = "C456"
        notifier.session = replacement_session
        notifier.timeout = 17
        receipt = notifier.send("hello")
        self.assertEqual(receipt, {"channel": "C456", "ts": "1700000000.123"})
        original_session.post.assert_not_called()
        replacement_session.post.assert_called_once()
        args, kwargs = replacement_session.post.call_args
        self.assertEqual(args, (slack_client.SLACK_CHAT_POST_MESSAGE,))
        self.assertEqual(kwargs["timeout"], 17)
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer dummy-updated")
        self.assertEqual(kwargs["json"]["channel"], "C456")

    # Legacy Slack exception and constant exports must remain aliases of the reusable client
    # contracts.
    def test_shared_exception_and_constant_exports(self):
        for name in ("SlackAmbiguousError", "SlackPermanentError", "SlackRateLimitError", "SlackTransientError"):
            self.assertIs(getattr(notify_tools, name), getattr(slack_client, name))
        self.assertEqual(notify_tools.SLACK_MAX_RETRY_AFTER, 3600.0)
        self.assertEqual(notify_tools.SLACK_MAX_RETRY_AFTER, slack_client.SLACK_MAX_RETRY_AFTER)
        self.assertEqual(notify_tools.SLACK_CHAT_POST_MESSAGE, slack_client.SLACK_CHAT_POST_MESSAGE)


# Jira content and managed-link projection

# Category labels and summaries must be deterministic, additive and explicit about missing
# values.
@pytest.mark.parametrize('category', ['build', 'conflict', 'feasibility'])
def test_categories_labels_and_missing(category, setup):
    runner, jira, _ = setup
    obs = observation(category=category)
    runner().run([obs])
    issue = jira.issues['RHAI-1']
    assert set(category_labels(category)) <= set(issue['fields']['labels'])
    issue['fields']['labels'].append('user-label')
    original = set(issue['fields']['labels'])
    # Existing no-op remains a no-op, including presentation labels.
    issue['fields']['labels'].remove(category_labels(category)[0])
    before = copy.deepcopy(issue)
    runner().run([obs])
    assert issue == before
    runner().run([observation(category=category, oid='new')])
    assert original <= set(issue['fields']['labels'])
    assert issue['fields']['summary'] == _summary(obs)
    assert 'Unavailable' in render_markdown(obs)


# Jira explains feasibility without confusing the promotion branch with the sync source,
# labels the workflow link accurately, and keeps the explanation out of other categories.
@pytest.mark.parametrize('category', ['build', 'conflict', 'feasibility'])
@pytest.mark.parametrize('status', ['failure', 'success'])
@pytest.mark.parametrize('environment', ['stage', 'production'])
def test_jira_feasibility_explanation(category, status, environment):
    workflow_url = 'https://github.com/acme/repo/actions/runs/123'
    obs = observation(category=category, status=status, source_branch='promotion-source',
                      evidence={'workflow_url': workflow_url})
    markdown = render_markdown(obs, jira_environment=environment)
    adf = _description(obs, jira_environment=environment)
    JiraNotifier._validate_adf(adf)
    text = json.dumps(adf)
    explanation = ('The main to release feasibility job has failed. This job dry-runs the normal sync from the '
                   'current configured source branch to configured active release branches (It does not test the '
                   'proposed stable PR contents or actually push changes).')
    guidance = 'Please inspect the feasibility job for merge conflicts or configuration/workflow errors.'
    assert ('## Main-to-release feasibility' in markdown) == (category == 'feasibility')
    assert (explanation in markdown) == (category == 'feasibility')
    assert (explanation in text) == (category == 'feasibility')
    assert (guidance in markdown) == (category == 'feasibility')
    assert (guidance in text) == (category == 'feasibility')
    label = 'Feasibility run' if category == 'feasibility' else 'Creator workflow'
    assert f'[{label}]({workflow_url})' in markdown
    assert f'"text": "{label}"' in text
    assert workflow_url in text


# Jira omits provenance boilerplate regardless of the evidence flags, without losing its
# destination banner or evidence links.
@pytest.mark.parametrize('environment', ['stage', 'production'])
@pytest.mark.parametrize('flags', [{}, {'synthetic': False, 'historical_snapshot': False},
                                   {'synthetic': True, 'historical_snapshot': True}])
def test_jira_omits_provenance_boilerplate(environment, flags):
    obs = observation(evidence=dict(flags, pr_url='https://github.com/acme/repo/pull/1'))
    markdown = render_markdown(obs, jira_environment=environment)
    adf = _description(obs, jira_environment=environment)
    JiraNotifier._validate_adf(adf)
    for text in (markdown, json.dumps(adf)):
        assert 'Historical snapshot:' not in text
        assert 'synthetic evidence:' not in text
        assert 'Missing flags' not in text
        assert 'https://github.com/acme/repo/pull/1' in text
    assert markdown.startswith('# GAP STAGING TEST' if environment == 'stage' else '# GAP PRODUCTION')


# Injected Markdown or template text must remain literal.
def test_literal_injection(monkeypatch, tmp_path):
    obs = observation(evidence={'synthetic': True, 'reason': '# injected\n[evil](https://evil.test) <script> {{ missing }}',
        'leader_url': 'https://example.test/pr/1'})
    monkeypatch.chdir(tmp_path)
    md = render_markdown(obs)
    adf = _description(obs)
    JiraNotifier._validate_adf(adf)
    headings = [n for n in adf['content'] if n['type'] == 'heading']
    assert len(headings) == 4
    assert 'Actual promotion stream: main → stable' in md
    raw = json.dumps(adf)
    assert '# injected [evil](https://evil.test) <script> {{ missing }}' in raw
    assert raw.count('"href"') == 1
    assert 'GAP STAGING TEST' in raw


# Unsafe evidence URLs must be rejected rather than rendered as links.
@pytest.mark.parametrize('url', ['http://example.test', 'https://u:p@example.test', 'https://example.test/\n#evil', 'https://example.test/[x]', 'https://example.test:bad'])
def test_bad_urls_fail(url):
    with pytest.raises(ValueError):
        _description(observation(evidence={'leader_url': url}))


# The removed affected-targets section must not appear, even with legacy supplied evidence.
@pytest.mark.parametrize('category', ['build', 'conflict', 'feasibility'])
@pytest.mark.parametrize('evidence', [{}, {'affected_targets': []},
                                   {'affected_targets': [{'branch': 'rhoai-3.2', 'reason': 'target-only reason'}]}])
def test_jira_omits_affected_targets(category, evidence):
    obs = observation(category=category, evidence=evidence)
    markdown = render_markdown(obs)
    adf = _description(obs)
    JiraNotifier._validate_adf(adf)
    for text in (markdown, json.dumps(adf)):
        assert 'Explicitly affected targets' not in text
        assert 'No targets inferred' not in text
        assert 'rhoai' not in text
        assert 'target-only reason' not in text
        assert 'FEEDBACK' not in text
        assert 'Evidence links' in text and 'Diagnostics' in text


# Presentation text remains bounded after removal of affected-targets rendering.
def test_jira_reason_is_bounded():
    with pytest.raises(ValueError):
        _description(observation(evidence={'reason': 'x' * 2001}))


# Missing required template fields must fail instead of silently rendering incomplete content.
@pytest.mark.parametrize('renderer', [render_markdown, lambda obs: slack_message(obs, jira_url=JIRA)])
def test_strict_template_missing_fields(monkeypatch, renderer):
    class MissingTemplate:
        def joinpath(self, path):
            return self
        def read_text(self, **kwargs):
            return '# GAP STAGING TEST\n\n{{ required_missing }}'
    monkeypatch.setattr(presentation, 'files', lambda package: MissingTemplate())
    with pytest.raises(UndefinedError):
        renderer(observation())


# Invalid presentation must leave no write intent; corrected evidence may proceed safely.
@pytest.mark.parametrize('existing', [False, True])
@pytest.mark.parametrize('invalid', ['url', 'size', 'template', 'labels'])
def test_invalid_presentation_has_no_write_intent_and_corrected_can_proceed(setup, monkeypatch, existing, invalid):
    runner, jira, slack = setup
    if existing:
        runner().run([observation()])
    before_issues = copy.deepcopy(jira.issues)
    before_properties = copy.deepcopy(jira.properties)
    before_calls = list(jira.calls)
    before_slack = list(slack.calls)
    obs = observation(oid='render-review')
    with monkeypatch.context() as patch:
        if invalid == 'url':
            obs['evidence']['leader_url'] = 'http://invalid.test'
        elif invalid == 'size':
            obs['evidence']['reason'] = 'x' * 2001
        elif invalid == 'template':
            class InvalidTemplate:
                def joinpath(self, path):
                    return self
                def read_text(self, **kwargs):
                    return '**unsupported**'
            patch.setattr(presentation, 'files', lambda package: InvalidTemplate())
        else:
            patch.setattr('lib.gap_notifications.dispatch_incidents.category_labels', lambda category: ['invalid label'])
        failed = runner().run([obs])
    assert failed['outbox'][-1]['error_type'] == 'ValueError'
    if not existing:
        assert not failed.get('creates')
    assert jira.issues == before_issues
    assert jira.properties == before_properties
    assert jira.calls == before_calls and slack.calls == before_slack
    corrected = runner().run([observation(oid='render-review')])
    assert corrected['outbox'][-1]['state'] == 'confirmed'
    assert jira.calls[len(before_calls):] == (['update'] if existing else ['create', 'update'])
    assert jira.properties['RHAI-1']['jira_intent'] is None


# Escaped backticks and backslashes must round-trip as literal inline-code text.
@pytest.mark.parametrize('value', ['a`b', 'a\\\\`b', 'a`b\\\\', '`\\\\`'])
def test_inline_code_escaped_backtick_and_backslash_literal(value):
    adf = _description(observation(oid=value, evidence={'head_sha': value}))
    JiraNotifier._validate_adf(adf)
    def code_values(node):
        values = [node['text']] if {'type': 'code'} in node.get('marks', []) else []
        for child in node.get('content', []):
            values.extend(code_values(child))
        return values
    assert code_values(adf) == [value, value]


# Dispatcher label additions must preserve human changes made after a stale registry search.
def test_dispatcher_only_adds_labels_despite_stale_registry_snapshot(setup, monkeypatch):
    runner, jira, _ = setup
    runner().run([observation()])
    update = jira.update_issue
    def raced_update(key, **kwargs):
        assert 'labels' not in kwargs
        assert kwargs['add_labels'] == ['gap-build-failure']
        jira.issues[key]['fields']['labels'].append('human-after-search')
        return update(key, **kwargs)
    monkeypatch.setattr(jira, 'update_issue', raced_update)
    runner().run([observation(oid='racing-update')])
    assert 'human-after-search' in jira.issues['RHAI-1']['fields']['labels']


# Real ADF generation must round-trip through the adapter without losing code or link structure.
def test_actual_adapter_fake_transport_roundtrip():
    obs = observation(evidence={'leader_url': 'https://example.test/leader/1', 'head_sha': 'abc123'})
    adf = _description(obs)
    issue = issue_for(summary=_summary(obs), description=adf, labels=['gap-build-failure'])
    session = FakeSession(responses={'post': [FakeResponse({'key': 'RHAI-1'})], 'get': [FakeResponse(issue)]})
    result = notifier_for(session).create_bug(_summary(obs), adf, ['gap-build-failure'])
    assert result['key'] == 'RHAI-1'
    assert JiraNotifier._adf_signature(adf) == JiraNotifier._adf_signature(issue['fields']['description'])
    assert '"code"' in json.dumps(adf) and '"href": "https://example.test/leader/1"' in json.dumps(adf)


@pytest.fixture
def setup(tmp_path):
    return make_runner(tmp_path)


# Managed link sections must be idempotent and preserve unrelated content, rejecting edited
# metadata.
def test_both_sections_preserve_body_idempotent_and_reject_edited_metadata():
    body = {'type': 'doc', 'version': 1, 'content': [{'type': 'paragraph', 'content': [{'type': 'text', 'text': 'human text'}]}]}
    slack = discussion_url({'channel': 'C0883L8FARE', 'ts': '123.000001'})
    meta = metadata_url('RHAI-123')
    updated = project_body(body, slack, metadata=meta)
    assert updated['content'][:1] == body['content']
    assert project_body(updated, slack, metadata=meta) == updated
    assert visible_link_status(updated, slack) == 'match'
    assert visible_link_status(updated, meta, metadata=True) == 'match'
    updated['content'].extend(section(meta, metadata=True))
    with pytest.raises(ValueError):
        project_body(updated, slack, metadata=meta)
    updated['content'] = updated['content'][:-2]
    updated['content'][-1]['content'][-1]['text'] = 'human changed note'
    with pytest.raises(ValueError):
        project_body(updated, slack, metadata=meta)


# Exact old/new metadata suffixes are supported; unrelated edits still fail closed.
@pytest.mark.parametrize('environment', ['stage', 'production'])
@pytest.mark.parametrize('suffix', [' Jira permissions required.', ' (Jira permissions required)'])
def test_metadata_wording_compatibility(environment, suffix):
    meta = metadata_url('RHAI-123', jira_environment=environment)
    obs = observation()
    body = _description(obs, metadata_url=meta, jira_environment=environment)
    assert body['content'][-1]['content'][-1]['text'] == ' (Jira permissions required)'
    body['content'][-1]['content'][-1]['text'] = suffix
    original = copy.deepcopy(body)
    assert visible_link_status(body, meta, metadata=True, jira_environment=environment) == 'match'
    other = metadata_url('RHAI-124', jira_environment=environment)
    assert visible_link_status(body, other, metadata=True, jira_environment=environment) == 'mismatch'
    projected = project_body(body, None, metadata=meta, jira_environment=environment)
    assert body == original
    assert projected['content'][:-2] == body['content'][:-2]
    assert projected['content'][-2:] == section(meta, metadata=True)
    assert project_body(projected, None, metadata=meta, jira_environment=environment) == projected
    for field in ('suffix', 'label', 'extra'):
        edited = copy.deepcopy(body)
        content = edited['content'][-1]['content']
        if field == 'suffix':
            content[-1]['text'] += ' edited'
        elif field == 'label':
            content[0]['text'] = 'edited label'
        else:
            content.append({'type': 'text', 'text': 'extra text'})
        assert visible_link_status(edited, meta, metadata=True, jira_environment=environment) == 'ambiguous_or_edited'
        with pytest.raises(ValueError):
            project_body(edited, None, metadata=meta, jira_environment=environment)


# Cosmetic projection must preserve user nodes and refuse edited or duplicated managed sections.
def test_projection_preserves_nodes_and_rejects_edited_duplicates():
    body = {'type': 'doc', 'version': 1, 'content': [{'type': 'paragraph', 'content': [{'type': 'text', 'text': 'human'}]}]}
    url = discussion_url({'channel': 'C0883L8FARE', 'ts': '123.000001'})
    updated = project_body(body, url)
    assert updated['content'][:1] == body['content']
    assert project_body(updated, url) == updated
    duplicate = copy.deepcopy(updated)
    duplicate['content'].extend(section(url))
    with pytest.raises(ValueError):
        project_body(duplicate, url)
    updated['content'][-1]['content'][0]['text'] = 'edited by human'
    with pytest.raises(ValueError):
        project_body(updated, url)


# Slack text and delivery integration

JIRA = "https://stage-redhat.atlassian.net/browse/RHAI-177"


def render(**changes):
    return slack_message(observation(**changes), jira_url=JIRA)


# The packaged Slack template preserves exact root/reply layout for every outcome and destination.
@pytest.mark.parametrize("category", ["build", "conflict", "feasibility"])
@pytest.mark.parametrize("status", ["failure", "success"])
@pytest.mark.parametrize("reply", [False, True])
@pytest.mark.parametrize("environment", ["stage", "production"])
@pytest.mark.parametrize("synthetic", [False, True])
def test_slack_template_layout(category, status, reply, environment, synthetic):
    headings = {
        "failure": {"build": "Build failed", "conflict": "Merge conflict", "feasibility": "Promotion blocked"},
        "success": {"build": "Successful build observed", "conflict": "Conflict resolved", "feasibility": "Feasibility restored"},
    }
    heading = "Failure update" if reply and status == "failure" else headings[status][category]
    destination = "staging test" if environment == "stage" else "production Jira"
    expected = [f"{'🟢' if status == 'success' else '🔴'} *GAP · {heading} — {destination}*"]
    if category == "build":
        expected.append("*Component:* `cpu`")
    if not reply:
        expected.append("*Repository:* `acme/repo`")
    if category == "build":
        expected.append(f"*Build {'failed' if status == 'failure' else 'completed'} at:* Unavailable")
    expected.append("*Reported reason:* `A useful reason`")
    if category == "feasibility" and not reply:
        explanation = ('This job dry-runs the normal sync from the current configured source branch to configured '
                       'active release branches (It does not test the proposed stable PR contents or actually push changes).')
        if status == "failure":
            explanation = ('The main to release feasibility job has failed. ' + explanation
                           + ' Please inspect the feasibility job for merge conflicts or configuration/workflow errors.')
        expected.extend(["", "*Main-to-release feasibility:* " + explanation])
    expected.extend(["", f"<{JIRA}|Jira RHAI-177>"])
    if synthetic:
        expected.extend(["", "Synthetic evidence"])
    if status == "success":
        expected.append("Staging result only; does not establish production recovery." if environment == "stage"
                        else "Reported component outcome only; not a claim that the whole promotion is healthy.")
    assert slack_message(observation(category=category, status=status,
                         evidence={"reason": "A useful reason", "synthetic": synthetic}),
                         jira_url=JIRA, reply=reply, jira_environment=environment) == "\n".join(expected)


# Failure roots share the user's Jira paragraph verbatim; recovery does not report failure.
@pytest.mark.parametrize('environment', ['stage', 'production'])
def test_slack_feasibility_wording_matches_jira(environment):
    obs = observation(category='feasibility')
    markdown = render_markdown(obs, jira_environment=environment)
    jira_paragraph = next(line for line in markdown.splitlines()
                          if line.startswith('The main to release feasibility job has failed.'))
    failure = slack_message(obs, jira_url=JIRA, jira_environment=environment)
    assert '*Main-to-release feasibility:* ' + jira_paragraph in failure
    assert 'usually' not in failure
    recovery = slack_message(dict(obs, status='success'), jira_url=JIRA, jira_environment=environment)
    assert 'This job dry-runs the normal sync from the current configured source branch' in recovery
    assert 'has failed' not in recovery and 'Please inspect' not in recovery
    for status in ('failure', 'success'):
        reply = slack_message(dict(obs, status=status), jira_url=JIRA, reply=True, jira_environment=environment)
        assert 'This job dry-runs' not in reply


# Template expressions supplied as evidence remain literal, never a second rendering pass.
def test_slack_template_evidence_is_not_evaluated():
    text = render(component="{{ 7 * 7 }}", evidence={"reason": "{% if true %}literal{% endif %}"})
    assert "`{{ 7 * 7 }}`" in text
    assert "`{% if true %}literal{% endif %}`" in text


# A root message must show useful context and provenance without dumping debug data or generic
# reasons.
def test_root_is_readable_and_selective():
    text = render(evidence={
        "reason": "Failed", "build_url": "https://build.example/run/1",
        "pr_url": "https://github.com/acme/repo/pull/1",
        "leader_url": "https://github.com/acme/leader/pull/84",
        "historical_snapshot": True, "observed-at": "2026-11-07T01:23:10+01:00",
        "head-sha": "a" * 40,
        "selected-run": {"head_sha": "a" * 40, "completed_at": "2026-11-06T23:15:31+01:00"},
        "unrelated_debug_data": {"token": "NOT_FOR_DISPLAY"},
    })
    assert "*GAP · Build failed — staging test*" in text
    assert "*Component:* `cpu`" in text and "*Repository:* `acme/repo`" in text
    assert "*Promotion:*" not in text and "`main` → `stable`" not in text
    assert "<https://build.example/run/1|Failed build>" in text
    assert "<https://github.com/acme/repo/pull/1|Child PR>" in text
    assert "<https://github.com/acme/leader/pull/84|GAP Leader>" in text
    assert f"<{JIRA}|Jira RHAI-177>" in text
    assert "*Build failed at:* 2026-11-06 22:15:31 UTC" in text and "2026-11-07" not in text
    assert f"<https://github.com/acme/repo/commit/{'a' * 40}|Commit aaaaaaa>" in text
    assert "Recorded snapshot" not in text and "not a live build check" not in text
    assert "Reported reason" not in text and "NOT_FOR_DISPLAY" not in text
    assert "test_only=" not in text and "observation_id" not in text


# Every supported category must render sensibly even when optional evidence is absent.
@pytest.mark.parametrize("category,heading", [("build", "Build failed"), ("conflict", "Merge conflict"), ("feasibility", "Promotion blocked")])
def test_supported_categories_and_missing_evidence(category, heading):
    text = render(category=category, evidence={})
    assert heading in text and "Supplied evidence" not in text
    assert ("*Build failed at:* Unavailable" in text) == (category == "build")
    assert ("*Component:*" in text) == (category == "build")
    assert "Failed build>" not in text


# Recovery replies must stay concise and must not claim production recovery.
@pytest.mark.parametrize("category,heading", [("build", "Successful build observed"), ("conflict", "Conflict resolved"), ("feasibility", "Feasibility restored")])
def test_recovery_reply_is_short_and_scoped(category, heading):
    text = slack_message(observation(status="success", category=category, evidence={"build_url": "https://build.example/run/2"}), jira_url=JIRA, reply=True)
    assert heading in text and "does not establish production recovery" in text
    assert "*Repository:*" not in text and "*Promotion:*" not in text
    if category == "build":
        assert "|Successful build>" in text
    assert "|Failed build>" not in text


# Evidence text must not inject mentions, control characters or Slack code fences.
def test_literal_text_cannot_inject_mentions_newlines_or_code_fences():
    text = render(component="cpu`\n<!channel>\u202e", evidence={"reason": "```<@U123> & <!here> *bold*"})
    assert "<!channel>" not in text and "<@U123>" not in text and "<!here>" not in text
    assert "&lt;!channel&gt;" in text and "&lt;@U123&gt;" in text
    assert "```" not in text and "\u202e" not in text
    assert "*Reported reason:* `ˋˋˋ&lt;@U123&gt; &amp; &lt;!here&gt; *bold*`" in text


# Unsafe or oversized optional links must be omitted without losing the Jira link.
@pytest.mark.parametrize("url", ["http://example.test", "https://user:pass@example.test", "https://example.test/<!here>", "https://example.test/\u202e", "https://example.test/" + "a" * 701, {"url": "https://example.test"}])
def test_invalid_optional_urls_are_omitted(url):
    text = render(evidence={"build_url": url})
    assert "|Failed build>" not in text
    assert JIRA in text


# URL delimiters and entities must not escape the controlled Slack link syntax.
def test_slack_url_delimiters_and_entities_are_escaped():
    text = render(evidence={"build_url": "https://example.test/a|b?q=1&x=2"})
    assert "<https://example.test/a%7Cb?q=1&amp;x=2|Failed build>" in text


# Invalid or timezone-free completion times must never fall back to a check/creation timestamp.
@pytest.mark.parametrize("timestamp", [None, {}, "invalid", "2026-11-07T01:23:00", "0001-01-01T00:00:00+23:00"])
def test_invalid_or_timezone_free_completion_time_does_not_use_check_time(timestamp):
    text = render(evidence={"observed-at": "2026-11-07T01:23:00Z",
        "selected-run": {"completed_at": timestamp, "created_at": "2026-11-06T23:00:00Z"}})
    assert "UTC" not in text and "*Build failed at:* Unavailable" in text


# Synthetic evidence must remain labeled even when another flag suggests a non-test context.
@pytest.mark.parametrize("historical", [False, True])
def test_synthetic_label_cannot_be_hidden_by_test_only_flag(historical):
    text = render(evidence={"synthetic": True, "historical_snapshot": historical, "test_only": False})
    assert "staging test" in text and "Synthetic evidence" in text
    assert "Recorded snapshot" not in text and "Supplied evidence" not in text


# Structured reasons must not be dumped into the human-facing message.
def test_structured_reason_is_not_dumped_as_debug_data():
    text = render(evidence={"reason": {"internal": "NOT_FOR_DISPLAY"}})
    assert "Reported reason" not in text and "NOT_FOR_DISPLAY" not in text


# Message length must remain bounded even when escaping expands hostile text.
def test_message_is_bounded_even_after_entity_expansion():
    text = render(repository="&" * 2000, component="&" * 2000, source_branch="&" * 2000,
                  destination_branch="&" * 2000, evidence={"reason": "&" * 2000,
                  **{key: "https://example.test/" + "a" * 670 for key in ("build_url", "pr_url", "leader_url")}})
    assert len(text) < 8000 and "…" in text


# An unsafe required Jira link or non-definitive outcome must fail closed.
def test_invalid_required_link_and_unknown_outcome_fail_closed():
    with pytest.raises(ValueError):
        slack_message(observation(), jira_url="https://example.test/<!here>")
    with pytest.raises(ValueError):
        render(status="unknown")


# Formatting upgrades must not resend old events; later failure and recovery updates stay
# threaded.
def test_format_upgrade_does_not_resend_old_receipts_and_new_events_stay_threaded(tmp_path, monkeypatch):
    runner, jira, slack = make_runner(tmp_path)
    formatter = dispatch.slack_message
    monkeypatch.setattr(dispatch, "slack_message", lambda *a, **kw: "Legacy root text")
    runner().run([observation()])
    original_root = jira.properties["RHAI-1"]["delivery"]["root"].copy()
    def forbidden(*a, **kw):
        raise AssertionError("already delivered events must not be re-rendered")
    monkeypatch.setattr(dispatch, "slack_message", forbidden)
    enriched = observation(evidence={"head-sha": "a" * 40,
        "selected-run": {"head_sha": "a" * 40, "completed_at": "2026-11-06T23:00:00Z"}})
    before = copy.deepcopy(jira.properties)
    replay = runner("fresh").run([enriched])
    assert replay["outbox"][0]["state"] == "confirmed" and len(slack.calls) == 1
    assert jira.properties == before
    monkeypatch.setattr(dispatch, "slack_message", formatter)
    runner("update").run([observation(oid="new-build")])
    assert "Failure update" in slack.calls[-1][0]
    assert slack.calls[-1][1]["thread_ts"] == original_root["ts"]
    runner("recovery").run([observation("success", "passing-build")])
    assert "Successful build observed" in slack.calls[-1][0]
    assert slack.calls[-1][1]["thread_ts"] == original_root["ts"]
    assert jira.properties["RHAI-1"]["delivery"]["root"] == original_root
    assert len(slack.calls) == 3 and len(jira.issues) == 1


# Rendering failure must leave work pending without sending or persisting a Slack inflight
# intent.
def test_format_failure_does_not_leave_a_slack_inflight_intent(tmp_path, monkeypatch):
    runner, jira, slack = make_runner(tmp_path)
    def invalid(*a, **kw):
        raise ValueError("invalid presentation")
    monkeypatch.setattr(dispatch, "slack_message", invalid)
    result = runner().run([observation()])
    assert result["outbox"][0]["state"] == "pending"
    assert jira.properties["RHAI-1"]["delivery"]["intent"] is None
    assert not slack.calls


# Allowlisted URL spellings must resolve to the unchanged environment-bound target.
@pytest.mark.parametrize('name', ['stage', 'production'])
@pytest.mark.parametrize('suffix', ['', '/'])
def test_operator_jira_url_preserves_internal_target(name, suffix):
    from lib.gap_notifications.notify_tools import jira_target, jira_target_from_url
    assert jira_target_from_url(jira_target(name).url + suffix) == jira_target(name)
    assert jira_target_from_url() == jira_target('stage')


# Nonallowlisted URLs must fail closed rather than normalize into a destination.
@pytest.mark.parametrize('value', ['', None, {}, True, 'stage', 'production',
    ' https://redhat.atlassian.net', 'https://redhat.atlassian.net ',
    'https://REDHAT.atlassian.net', 'http://redhat.atlassian.net',
    'https://redhat.atlassian.net//', 'https://redhat.atlassian.net/path',
    'https://redhat.atlassian.net?', 'https://redhat.atlassian.net#',
    'https://redhat.atlassian.net:443', 'https://user@redhat.atlassian.net',
    'https://redhat.atlassian.net.evil.test', 'https://redhat.atlassian.net/?x=1'])
def test_operator_jira_url_rejects_nonallowlisted_values(value):
    from lib.gap_notifications.notify_tools import jira_target_from_url
    with pytest.raises(ValueError, match='GAP_JIRA_URL'):
        jira_target_from_url(value)


# Unknown internal selectors must fail explicitly rather than silently fall back to staging or production.
@pytest.mark.parametrize('value', ['', 'prod', 'Stage', ' production ', None, {}, True])
def test_jira_environment_selector_rejects_invalid_values(value):
    from lib.gap_notifications.notify_tools import jira_target
    with pytest.raises(ValueError, match='environment'):
        jira_target(value)


# Production requires explicit opt-in and must use the selected HTTPS origin with Bug by name.
def test_production_jira_facade_uses_explicit_destination():
    from lib.gap_notifications.notify_tools import jira_target
    target = jira_target('production')
    issue = issue_for(description={'type': 'doc', 'version': 1, 'content': []})
    issue['fields']['issuetype']['id'] = '99999'
    session = FakeSession(responses={'get': [FakeResponse(issue)]})
    notifier = JiraNotifier(target.url, 'RHAI', 'Bug', 'dummy@example.test', 'dummy',
                            session=session, **target.options)
    assert notifier.get_issue('RHAI-1') == issue
    assert session.calls[0][1] == target.url + '/rest/api/3/issue/RHAI-1'
    assert session.trust_env is False
    with pytest.raises(ValueError, match='environment'):
        JiraNotifier(target.url, 'RHAI', 'Bug', 'dummy@example.test', 'dummy', session=session)
    with pytest.raises(ValueError):
        JiraNotifier(target.url, 'RHAI', '10016', 'dummy@example.test', 'dummy', session=session, **target.options)
    assert len(session.calls) == 1


# Environment mismatch or a staging-only type introduced by mutation must not reach HTTP.
def test_mutated_production_facade_revalidates_target_and_issue_type():
    session = FakeSession()
    notifier = JiraNotifier('https://redhat.atlassian.net', 'RHAI', 'Bug', 'dummy@example.test', 'dummy',
                            session=session, jira_environment='production')
    notifier.issue_type = '10016'
    with pytest.raises(ValueError): notifier.get_issue('RHAI-1')
    notifier.issue_type = 'Bug'
    notifier.base_url = 'https://stage-redhat.atlassian.net'
    with pytest.raises(ValueError): notifier.get_issue('RHAI-1')
    assert not session.calls


# Slack production content retains synthetic labeling and avoids claiming global recovery.
def test_production_presentation_is_labeled_without_claiming_global_recovery():
    from lib.gap_notifications.notify_tools import jira_target, slack_message
    target = jira_target('production')
    obs = observation(environment=target.scope, evidence={'synthetic': True, 'historical_snapshot': True})
    markdown = render_markdown(obs, **target.options)
    assert markdown.startswith('# GAP PRODUCTION') and 'Staging notification only' not in markdown
    assert 'Historical snapshot:' not in markdown and 'synthetic evidence:' not in markdown
    text = slack_message(dict(obs, status='success'), jira_url=target.url + '/browse/RHAI-1', **target.options)
    assert 'production Jira' in text and 'staging test' not in text
    assert 'Synthetic evidence' in text and 'Recorded snapshot' not in text
    assert 'not a claim that the whole promotion is healthy' in text


# Production creates must use Bug by name and atomically verify initial properties on that site.
def test_production_create_readback_accepts_site_specific_bug_id():
    from lib.gap_notifications.notify_tools import jira_target
    target = jira_target('production')
    obs = observation(environment=target.scope)
    body = _description(obs, **target.options)
    summary = _summary(obs, **target.options)
    state = {'scope': target.scope, 'test': True}
    issue = issue_for(description=body, summary=summary, labels=['gap-test'])
    issue['fields']['issuetype']['id'] = '99999'
    session = FakeSession(responses={
        'post': [FakeResponse({'key': 'RHAI-1'}, status_code=201)],
        'get': [FakeResponse(issue), FakeResponse({'key': 'gap.notification.v1', 'value': state})],
    })
    notifier = JiraNotifier(target.url, 'RHAI', 'Bug', 'dummy@example.test', 'dummy',
                            session=session, **target.options)
    assert notifier.create_bug(summary, body, ['gap-test'], notification_state=state) == {
        'key': 'RHAI-1', 'url': target.url + '/browse/RHAI-1'}
    assert [method for method, _, _ in session.calls] == ['post', 'get', 'get']
    assert all(url.startswith(target.url + '/') for _, url, _ in session.calls)
    payload = session.calls[0][2]['json']
    assert payload['fields']['issuetype'] == {'name': 'Bug'}
    assert payload['properties'] == [{'key': 'gap.notification.v1', 'value': state}]


# A managed metadata link from the other site must be treated as edited, not silently rewritten.
@pytest.mark.parametrize('selected,other', [('stage', 'production'), ('production', 'stage')])
def test_managed_metadata_remains_environment_scoped(selected, other):
    from lib.gap_notifications.notify_tools import jira_target
    correct = metadata_url('RHAI-1', jira_environment=selected)
    foreign = metadata_url('RHAI-1', jira_environment=other)
    body = {'type': 'doc', 'version': 1, 'content': section(foreign, metadata=True)}
    assert visible_link_status(body, correct, metadata=True, jira_environment=selected) == 'ambiguous_or_edited'
    with pytest.raises(ValueError, match='edited'):
        project_body(body, None, metadata=correct, **jira_target(selected).options)


# Commit links require matching full selected-run/receipt SHAs and a safe consistent GitHub repo.
@pytest.mark.parametrize('problem', ['short', 'mismatch', 'missing-run-sha', 'missing-receipt-sha', 'non-string',
    'markup', 'other-host', 'traversal', 'repo-mismatch', 'bad-evidence-repo'])
def test_commit_link_omitted_for_missing_or_inconsistent_identity(problem):
    evidence = {'head-sha': 'a' * 40, 'selected-run': {'head_sha': 'a' * 40}}
    repo = 'github.com/acme/repo'
    if problem == 'short': evidence['selected-run']['head_sha'] = 'aaaaaaa'
    if problem == 'mismatch': evidence['selected-run']['head_sha'] = 'b' * 40
    if problem == 'missing-run-sha': evidence['selected-run'] = {}
    if problem == 'missing-receipt-sha': evidence.pop('head-sha')
    if problem == 'non-string': evidence['selected-run']['head_sha'] = {'sha': 'a' * 40}
    if problem == 'markup': evidence['selected-run']['head_sha'] = '<!channel>'
    if problem == 'other-host': repo = 'gitlab.com/acme/repo'
    if problem == 'traversal': repo = 'acme/..'
    if problem == 'repo-mismatch': evidence['repository'] = 'other/repo'
    if problem == 'bad-evidence-repo': evidence['repository'] = ['acme/repo']
    text = render(repository=repo, evidence=evidence)
    assert '|Commit ' not in text and '/commit/' not in text
    assert '|Jira RHAI-177>' in text


# Verified commit links must work for canonical or short GitHub repos in roots and replies.
@pytest.mark.parametrize('repo', ['acme/repo', 'github.com/acme/repo', 'GitHub.com/Acme/Repo'])
@pytest.mark.parametrize('reply', [False, True])
def test_commit_link_uses_full_sha_and_short_label(repo, reply):
    evidence = {'head-sha': 'b' * 40, 'repository': 'acme/repo', 'selected-run': {'head_sha': 'b' * 40}}
    text = slack_message(observation(repository=repo, evidence=evidence), jira_url=JIRA, reply=reply)
    assert f'<https://github.com/acme/repo/commit/{"b" * 40}|Commit bbbbbbb>' in text


# Both failure and recovery use the selected run's completion timestamp, in UTC, even in replies.
@pytest.mark.parametrize('status,label', [('failure', 'Build failed at'), ('success', 'Build completed at')])
@pytest.mark.parametrize('reply', [False, True])
def test_build_completion_display_is_scoped_to_outcome(status, label, reply):
    text = slack_message(observation(status=status, evidence={'observed-at': '2030-01-01T00:00:00Z',
        'selected-run': {'completed_at': '2026-11-07T01:23:10+01:00'}}), jira_url=JIRA, reply=reply)
    assert f'*{label}:* 2026-11-07 00:23:10 UTC' in text and '2030' not in text
    assert '*Promotion:*' not in text and 'Recorded snapshot' not in text


# Build-specific timestamps/commit links must not leak into conflict or feasibility messages.
@pytest.mark.parametrize('category', ['conflict', 'feasibility'])
def test_other_categories_do_not_use_build_metadata(category):
    text = render(category=category, evidence={'head-sha': 'a' * 40,
        'selected-run': {'head_sha': 'a' * 40, 'completed_at': '2026-11-07T00:00:00Z'}})
    assert 'UTC' not in text and '|Commit ' not in text and '*Build failed at:*' not in text


if __name__ == "__main__":
    unittest.main()
