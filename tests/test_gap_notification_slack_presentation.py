"""Slack text contracts and dispatch integration; no live transports or credentials."""
import pytest

from gap_notification_fakes import make_runner, observation
from lib.gap_notifications import dispatch_incidents as dispatch
from lib.gap_notifications.notify_tools import slack_message

JIRA = "https://stage-redhat.atlassian.net/browse/RHAI-177"


def render(**changes):
    return slack_message(observation(**changes), jira_url=JIRA)


def test_root_is_readable_and_selective():
    text = render(evidence={
        "reason": "Failed", "build_url": "https://build.example/run/1",
        "pr_url": "https://github.com/acme/repo/pull/1",
        "leader_url": "https://github.com/acme/leader/pull/84",
        "historical_snapshot": True, "observed-at": "2026-11-07T01:23:10+01:00",
        "unrelated_debug_data": {"token": "NOT_FOR_DISPLAY"},
    })
    assert "*GAP · Build failed — staging test*" in text
    assert "*Component:* `cpu`" in text and "*Repository:* `acme/repo`" in text
    assert "`main` → `stable`" in text
    assert "<https://build.example/run/1|Failed build>" in text
    assert "<https://github.com/acme/repo/pull/1|Child PR>" in text
    assert "<https://github.com/acme/leader/pull/84|GAP Leader>" in text
    assert f"<{JIRA}|Jira RHAI-177>" in text and "2026-11-07 00:23 UTC" in text
    assert "Recorded snapshot — not a live build check" in text
    assert "Reported reason" not in text and "NOT_FOR_DISPLAY" not in text
    assert "test_only=" not in text and "observation_id" not in text


@pytest.mark.parametrize("category,heading", [("build", "Build failed"), ("conflict", "Merge conflict"), ("feasibility", "Promotion blocked")])
def test_supported_categories_and_missing_evidence(category, heading):
    text = render(category=category, evidence={})
    assert heading in text and "Supplied evidence — not a fresh build check" in text
    assert ("*Component:*" in text) == (category == "build")
    assert "Failed build>" not in text


@pytest.mark.parametrize("category,heading", [("build", "Successful build observed"), ("conflict", "Conflict resolved"), ("feasibility", "Feasibility restored")])
def test_recovery_reply_is_short_and_scoped(category, heading):
    text = slack_message(observation(status="success", category=category, evidence={"build_url": "https://build.example/run/2"}), jira_url=JIRA, reply=True)
    assert heading in text and "does not establish production recovery" in text
    assert "*Repository:*" not in text and "*Promotion:*" not in text
    if category == "build":
        assert "|Successful build>" in text
    assert "|Failed build>" not in text


def test_literal_text_cannot_inject_mentions_newlines_or_code_fences():
    text = render(component="cpu`\n<!channel>\u202e", evidence={"reason": "```<@U123> & <!here> *bold*"})
    assert "<!channel>" not in text and "<@U123>" not in text and "<!here>" not in text
    assert "&lt;!channel&gt;" in text and "&lt;@U123&gt;" in text
    assert "```" not in text and "\u202e" not in text
    assert "*Reported reason:* `ˋˋˋ&lt;@U123&gt; &amp; &lt;!here&gt; *bold*`" in text


@pytest.mark.parametrize("url", ["http://example.test", "https://user:pass@example.test", "https://example.test/<!here>", "https://example.test/\u202e", "https://example.test/" + "a" * 701, {"url": "https://example.test"}])
def test_invalid_optional_urls_are_omitted(url):
    text = render(evidence={"build_url": url})
    assert "|Failed build>" not in text
    assert JIRA in text


def test_slack_url_delimiters_and_entities_are_escaped():
    text = render(evidence={"build_url": "https://example.test/a|b?q=1&x=2"})
    assert "<https://example.test/a%7Cb?q=1&amp;x=2|Failed build>" in text


@pytest.mark.parametrize("timestamp", [None, {}, "invalid", "2026-11-07T01:23:00", "0001-01-01T00:00:00+23:00"])
def test_invalid_or_timezone_free_observation_time_does_not_claim_freshness(timestamp):
    text = render(evidence={"observed-at": timestamp})
    assert "UTC" not in text and "not a fresh build check" in text


@pytest.mark.parametrize("historical", [False, True])
def test_synthetic_label_cannot_be_hidden_by_test_only_flag(historical):
    text = render(evidence={"synthetic": True, "historical_snapshot": historical, "test_only": False})
    assert "staging test" in text and "Synthetic evidence" in text
    assert ("Recorded snapshot" in text) == historical


def test_structured_reason_is_not_dumped_as_debug_data():
    text = render(evidence={"reason": {"internal": "NOT_FOR_DISPLAY"}})
    assert "Reported reason" not in text and "NOT_FOR_DISPLAY" not in text


def test_message_is_bounded_even_after_entity_expansion():
    text = render(repository="&" * 2000, component="&" * 2000, source_branch="&" * 2000,
                  destination_branch="&" * 2000, evidence={"reason": "&" * 2000,
                  **{key: "https://example.test/" + "a" * 670 for key in ("build_url", "pr_url", "leader_url")}})
    assert len(text) < 8000 and "…" in text


def test_invalid_required_link_and_unknown_outcome_fail_closed():
    with pytest.raises(ValueError):
        slack_message(observation(), jira_url="https://example.test/<!here>")
    with pytest.raises(ValueError):
        render(status="unknown")


def test_format_upgrade_does_not_resend_old_receipts_and_new_events_stay_threaded(tmp_path, monkeypatch):
    runner, jira, slack = make_runner(tmp_path)
    formatter = dispatch.slack_message
    monkeypatch.setattr(dispatch, "slack_message", lambda *a, **kw: "Legacy root text")
    runner().run([observation()])
    original_root = jira.properties["RHAI-1"]["delivery"]["root"].copy()
    def forbidden(*a, **kw):
        raise AssertionError("already delivered events must not be re-rendered")
    monkeypatch.setattr(dispatch, "slack_message", forbidden)
    replay = runner("fresh").run([observation()])
    assert replay["outbox"][0]["state"] == "confirmed" and len(slack.calls) == 1
    monkeypatch.setattr(dispatch, "slack_message", formatter)
    runner("update").run([observation(oid="new-build")])
    assert "Failure update" in slack.calls[-1][0]
    assert slack.calls[-1][1]["thread_ts"] == original_root["ts"]
    runner("recovery").run([observation("success", "passing-build")])
    assert "Successful build observed" in slack.calls[-1][0]
    assert slack.calls[-1][1]["thread_ts"] == original_root["ts"]
    assert jira.properties["RHAI-1"]["delivery"]["root"] == original_root
    assert len(slack.calls) == 3 and len(jira.issues) == 1


def test_format_failure_does_not_leave_a_slack_inflight_intent(tmp_path, monkeypatch):
    runner, jira, slack = make_runner(tmp_path)
    def invalid(*a, **kw):
        raise ValueError("invalid presentation")
    monkeypatch.setattr(dispatch, "slack_message", invalid)
    result = runner().run([observation()])
    assert result["outbox"][0]["state"] == "pending"
    assert jira.properties["RHAI-1"]["delivery"]["intent"] is None
    assert not slack.calls
