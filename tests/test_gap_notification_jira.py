"""Offline tests for the staging Jira adapter."""

from __future__ import annotations

import unittest
from copy import deepcopy
from types import SimpleNamespace

import requests

from lib.gap_notifications.dispatch_incidents import _description as dispatch_description
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


class JiraNotifierTests(unittest.TestCase):
    def _notifier(self, session):
        return JiraNotifier(
            STAGING_JIRA_ORIGIN,
            "RHAI",
            "Bug",
            "jira-bot@example.com",
            "secret-token",
            session=session,
            timeout=11,
        )

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

    def test_constructor_rejects_wrong_project_or_issue_type(self):
        session = FakeSession()
        with self.assertRaises(ValueError):
            JiraNotifier(STAGING_JIRA_ORIGIN, "OTHER", "Bug", "jira-bot@example.com", "secret-token", session=session)
        with self.assertRaises(ValueError):
            JiraNotifier(STAGING_JIRA_ORIGIN, "RHAI", "Task", "jira-bot@example.com", "secret-token", session=session)

    def test_constructor_disables_trust_env_and_hides_credentials_in_repr(self):
        session = FakeSession()
        notifier = self._notifier(session)
        self.assertFalse(session.trust_env)
        self.assertEqual(notifier.base_url, STAGING_JIRA_ORIGIN)
        self.assertNotIn("secret-token", repr(notifier))
        self.assertNotIn("jira-bot@example.com", repr(notifier))

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

    def test_get_notification_state_missing_returns_none(self):
        session = FakeSession(responses={"get": [FakeResponse({"error": "not found"}, 404)]})
        notifier = self._notifier(session)

        result = notifier.get_notification_state("RHAI-1")

        self.assertIsNone(result)
        self.assertEqual(session.calls[0][1], f"{STAGING_JIRA_ORIGIN}/rest/api/3/issue/RHAI-1/properties/gap.notification.v1")

    def test_get_notification_state_reads_property_value(self):
        state = self._notification_state()
        session = FakeSession(responses={"get": [FakeResponse({"key": "gap.notification.v1", "value": state}, 200)]})
        notifier = self._notifier(session)

        result = notifier.get_notification_state("RHAI-1")

        self.assertEqual(result, state)

    def test_get_notification_state_rejects_malformed_property_shape(self):
        session = FakeSession(responses={"get": [FakeResponse({"value": self._notification_state()}, 200)]})
        notifier = self._notifier(session)

        with self.assertRaises(JiraAmbiguousError) as raised:
            notifier.get_notification_state("RHAI-1")

        self.assertEqual(raised.exception.key, "RHAI-1")

    def test_get_notification_state_rejects_wrong_property_key(self):
        session = FakeSession(responses={"get": [FakeResponse({"key": "gap.notification.other", "value": self._notification_state()}, 200)]})
        notifier = self._notifier(session)

        with self.assertRaises(JiraAmbiguousError) as raised:
            notifier.get_notification_state("RHAI-1")

        self.assertEqual(raised.exception.key, "RHAI-1")

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

    def test_create_bug_validation_error_reports_safe_fields(self):
        session = FakeSession(responses={"post": [FakeResponse({"errors": {"summary": "bad", "description": "bad"}, "errorMessages": ["BAD_INPUT", "not-safe"]}, 400)]})
        notifier = self._notifier(session)
        description = self._issue()["fields"]["description"]

        with self.assertRaises(JiraValidationError) as raised:
            notifier.create_bug("summary", description, ["gap-staging"])

        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(raised.exception.field_errors, ("description", "summary"))
        self.assertEqual(raised.exception.error_codes, ("BAD_INPUT",))

    def test_get_issue_timeout_is_transient_and_redacted(self):
        session = FakeSession(side_effects={"get": [requests.Timeout("jira-bot@example.com secret-token") ]})
        notifier = self._notifier(session)

        with self.assertRaises(JiraTransientError) as raised:
            notifier.get_issue("RHAI-4")

        self.assertNotIn("jira-bot@example.com", str(raised.exception))
        self.assertNotIn("secret-token", str(raised.exception))

    def test_create_bug_timeout_is_ambiguous(self):
        session = FakeSession(side_effects={"post": [requests.Timeout("jira-bot@example.com secret-token") ]})
        notifier = self._notifier(session)
        description = self._issue()["fields"]["description"]

        with self.assertRaises(JiraAmbiguousError) as raised:
            notifier.create_bug("summary", description, ["gap-staging"])

        self.assertIsNone(raised.exception.key)
        self.assertNotIn("secret-token", str(raised.exception))

    def test_rate_limit_reports_retry_after(self):
        session = FakeSession(responses={"get": [FakeResponse({"error": "ratelimited"}, 429, {"Retry-After": "2.5"})]})
        notifier = self._notifier(session)

        with self.assertRaises(JiraRateLimitError) as raised:
            notifier.get_issue("RHAI-5")

        self.assertEqual(raised.exception.retry_after, 2.5)
        self.assertEqual(raised.exception.status_code, 429)
        self.assertEqual(raised.exception.error, "ratelimited")

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

    def test_find_by_label_incomplete_pagination_fails_closed(self):
        first = self._issue("RHAI-12")
        session = FakeSession(responses={"post": [FakeResponse({"issues": [first], "isLast": False}, 200)]})
        notifier = self._notifier(session)

        with self.assertRaises(JiraAmbiguousError):
            notifier.find_by_label("gap-staging")

    def test_find_by_label_rejects_looping_next_page_token(self):
        first = self._issue("RHAI-19")
        session = FakeSession(
            responses={
                "post": [
                    FakeResponse({"issues": [first], "isLast": False, "nextPageToken": "tok-dup"}, 200),
                    FakeResponse({"issues": [first], "isLast": False, "nextPageToken": "tok-dup"}, 200),
                ]
            }
        )
        notifier = self._notifier(session)

        with self.assertRaises(JiraAmbiguousError):
            notifier.find_by_label("gap-staging")

    def test_update_issue_rejects_done_issue_without_put(self):
        session = FakeSession(responses={"get": [FakeResponse(self._issue("RHAI-13", status_category="done"), 200)]})
        notifier = self._notifier(session)

        with self.assertRaises(JiraTerminalIssueError):
            notifier.update_issue("RHAI-13", description=self._issue()["fields"]["description"])

        self.assertEqual([call[0] for call in session.calls], ["get"])

    def test_update_issue_rejects_missing_status_category(self):
        issue = self._issue("RHAI-17")
        del issue["fields"]["status"]["statusCategory"]
        session = FakeSession(responses={"get": [FakeResponse(issue, 200)]})
        notifier = self._notifier(session)

        with self.assertRaises(JiraPermanentError):
            notifier.update_issue("RHAI-17", labels=["gap-staging"])

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

    def test_put_notification_state_rejects_oversize_json(self):
        notifier = self._notifier(FakeSession())
        with self.assertRaises(ValueError):
            notifier.put_notification_state("RHAI-22", {"payload": "x" * (24 * 1024)})

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

    def test_transition_to_done_noops_when_already_done(self):
        done = self._issue("RHAI-16", status_category="done")
        session = FakeSession(responses={"get": [FakeResponse(done, 200)]})
        notifier = self._notifier(session)

        result = notifier.transition_to_done("RHAI-16", "31")

        self.assertEqual(result, done)
        self.assertEqual(len(session.calls), 1)

    def test_transition_to_done_rejects_missing_status_category(self):
        issue = self._issue("RHAI-18")
        del issue["fields"]["status"]["statusCategory"]
        session = FakeSession(responses={"get": [FakeResponse(issue, 200)]})
        notifier = self._notifier(session)

        with self.assertRaises(JiraPermanentError):
            notifier.transition_to_done("RHAI-18", "31")

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

    def test_no_secret_values_leak_from_validation_and_ambiguous_errors(self):
        session = FakeSession(side_effects={"post": [requests.ConnectionError("jira-bot@example.com secret-token")]})
        notifier = self._notifier(session)
        description = self._issue()["fields"]["description"]

        with self.assertRaises(JiraAmbiguousError) as raised:
            notifier.create_bug("summary", description, ["gap-staging"])

        self.assertNotIn("jira-bot@example.com", str(raised.exception))
        self.assertNotIn("secret-token", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
