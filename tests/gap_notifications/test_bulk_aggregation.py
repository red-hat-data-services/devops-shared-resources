"""Bulk build-failure aggregation preserves existing notification behavior."""
from __future__ import annotations

import pytest

from gap_notification_fakes import Jira, Slack, observation
from lib.gap_notifications.monitor_hook import NotificationConfig, notify_after_publish
from lib.gap_notifications.bulk_aggregation import (
    BULK_COMPONENT,
    aggregate_bulk_build_failures,
)
from lib.gap_notifications.notify_tools import description, issue_summary, render_markdown


@pytest.fixture
def hook(tmp_path):
    root = tmp_path / "checkout"
    root.mkdir()
    jira, slack = Jira(), Slack()
    config = NotificationConfig(
        "write", str(tmp_path / "journal.json"), "bulk-test", "41"
    )
    defaults = dict(
        accepted_urls=["https://github.com/acme/demo/pull/42"],
        leader_url="https://github.com/red-hat-data-services/gated-artifacts-promoter/pull/84",
        published_sha="b" * 40,
        state_path=root / "state.json",
        repo_root=root,
        config=config,
        jira_factory=lambda *args: jira,
        slack_factory=lambda token, channel: slack,
        environ={
            "JIRA_API_EMAIL": "fake@example.test",
            "JIRA_API_TOKEN": "fake",
            "SLACK_TOKEN": "fake",
        },
    )

    def invoke(payload=None, **changes):
        return notify_after_publish(
            payload if payload is not None else {},
            **(defaults | changes),
        )

    return invoke, jira, slack, defaults, []


def build_observation(component, status="failure", *, oid=None, evidence=None):
    return observation(
        component=component,
        status=status,
        oid=oid or f"{component}-{status}",
        evidence=evidence or {
            "pr_url": f"https://github.com/acme/repo/pull/{component}",
            "build_url": f"https://build.example/run/{component}",
            "reason": f"{component} failure",
        },
    )


def test_majority_build_failures_are_replaced_by_one_bulk_observation():
    failed_a = build_observation("api")
    failed_b = build_observation("web")
    success = build_observation("gpu", "success")
    result = aggregate_bulk_build_failures([failed_a, failed_b, success])

    assert len(result) == 2
    assert result[0] is success
    bulk = result[1]
    assert bulk["category"] == "build"
    assert bulk["component"] == BULK_COMPONENT
    assert bulk["status"] == "failure"
    assert bulk["evidence"]["bulk"] is True
    assert bulk["evidence"]["total_builds"] == 3
    assert bulk["evidence"]["failed_builds"] == 2
    assert [item["component"] for item in bulk["evidence"]["affected_components"]] == ["api", "web"]
    assert issue_summary(bulk) == "Promotion Gates Bulk PR Build Failures"


def test_non_majority_and_small_batches_are_unchanged():
    one_failure = [build_observation("api")]
    assert aggregate_bulk_build_failures(one_failure) is one_failure

    two_failures = [build_observation("api"), build_observation("web")]
    assert aggregate_bulk_build_failures(two_failures) is two_failures

    minority = [
        build_observation("api"),
        build_observation("web", "success"),
        build_observation("gpu", "success"),
    ]
    assert aggregate_bulk_build_failures(minority) is minority


def test_bulk_description_lists_all_affected_components_and_links():
    failed_a = build_observation("api")
    failed_b = build_observation("web")
    success = build_observation("gpu", "success")
    bulk = aggregate_bulk_build_failures([failed_a, failed_b, success])[-1]

    markdown = render_markdown(bulk)
    adf = description(bulk)
    assert "2 of 3 required component builds failed" in markdown
    assert "Suggested owner: AI Productization Team." in markdown
    assert "[PR](https://github.com/acme/repo/pull/api)" in markdown
    assert "[Build](https://build.example/run/api)" in markdown
    assert "[PR](https://github.com/acme/repo/pull/web)" in markdown
    assert "[Build](https://build.example/run/web)" in markdown
    assert "- api" in markdown and "api failure" in markdown
    assert "- web" in markdown and "web failure" in markdown
    assert adf["type"] == "doc"


def test_majority_bulk_uses_stable_title_based_deduplication():
    first = aggregate_bulk_build_failures([
        build_observation("api"), build_observation("web"), build_observation("gpu", "success")
    ])[-1]
    second = aggregate_bulk_build_failures([
        build_observation("api", oid="api-new"),
        build_observation("web", oid="web-new"),
        build_observation("gpu", "success", oid="gpu-new"),
    ])[-1]

    assert issue_summary(first) == issue_summary(second)
    assert first["observation_id"] != second["observation_id"]


def test_monitor_hook_aggregates_majority_build_failures(hook):
    invoke, jira, slack, opts, _ = hook
    observations = [
        build_observation("api"),
        build_observation("web"),
        build_observation("gpu", "success"),
    ]
    result = invoke(
        config=opts["config"],
        collector=lambda *args, **kwargs: observations,
        ownership_fetch=lambda name: (
            "repo_to_component: {}\nrepo_to_slack_handle: {}"
            if name == "automerge_team_mapping.yaml"
            else "_default: DevOps\nmappings: {api: [API], web: [Web], promotion-gates-bulk: [DevOps]}"
            if name.startswith("konflux")
            else "components:\n  default:\n    slack_team_handle: devops"
        ),
    )

    assert result["status"] == "settled"
    assert result["counts"] == {"failure": 1, "success": 1, "unknown": 0}
    assert len(jira.issues) == 1
    assert jira.issues["RHAI-1"]["fields"]["summary"] == "Promotion Gates Bulk PR Build Failures"
    assert len(slack.calls) == 1


def test_monitor_hook_preserves_component_tickets_below_threshold(hook):
    invoke, jira, slack, opts, _ = hook
    observations = [
        build_observation("api"),
        build_observation("web"),
        build_observation("gpu", "success"),
        build_observation("memory", "success"),
        build_observation("storage", "success"),
    ]
    result = invoke(
        config=opts["config"],
        collector=lambda *args, **kwargs: observations,
        ownership_fetch=lambda name: (
            "repo_to_component: {}\nrepo_to_slack_handle: {}"
            if name == "automerge_team_mapping.yaml"
            else "_default: DevOps\nmappings: {api: [API], web: [Web], promotion-gates-bulk: [DevOps]}"
            if name.startswith("konflux")
            else "components:\n  default:\n    slack_team_handle: devops"
        ),
    )

    assert result["status"] == "settled"
    assert result["counts"] == {"failure": 2, "success": 3, "unknown": 0}
    assert len(jira.issues) == 2
    assert {issue["fields"]["summary"] for issue in jira.issues.values()} == {
        "JIRA STAGE - Promotion Gate PR Build Failure in api",
        "JIRA STAGE - Promotion Gate PR Build Failure in web",
    }


def test_oversized_bulk_evidence_falls_back_to_individual_observations():
    observations = [
        build_observation(
            f"component-{index}",
            evidence={
                "pr_url": f"https://github.com/acme/repo/pull/{index}",
                "build_url": f"https://build.example/run/{index}",
                "reason": "x" * 1500,
            },
        )
        for index in range(20)
    ]
    result = aggregate_bulk_build_failures(observations)
    assert result is observations


def test_all_success_batch_adds_bulk_recovery_observation():
    successes = [
        build_observation("api", "success"),
        build_observation("web", "success"),
        build_observation("gpu", "success"),
    ]
    result = aggregate_bulk_build_failures(successes)
    assert result[:3] == successes
    recovery = result[-1]
    assert recovery["component"] == BULK_COMPONENT
    assert recovery["status"] == "success"
    assert recovery["evidence"]["bulk_recovery"] is True
    assert issue_summary(recovery) == "Promotion Gates Bulk PR Build Failures"


def test_recovered_component_builds_close_existing_bulk_jira(hook):
    invoke, jira, slack, opts, _ = hook
    failure_observations = [
        build_observation("api"),
        build_observation("web"),
        build_observation("gpu", "success"),
    ]
    ownership_fetch = lambda name: (
        "repo_to_component: {}\nrepo_to_slack_handle: {}"
        if name == "automerge_team_mapping.yaml"
        else "_default: DevOps\nmappings: {api: [API], web: [Web], promotion-gates-bulk: [DevOps]}"
        if name.startswith("konflux")
        else "components:\n  default:\n    slack_team_handle: devops"
    )
    invoke(
        config=opts["config"],
        collector=lambda *args, **kwargs: failure_observations,
        ownership_fetch=ownership_fetch,
    )
    assert len(jira.issues) == 1
    assert jira.issues["RHAI-1"]["fields"]["summary"] == "Promotion Gates Bulk PR Build Failures"

    recovery_observations = [
        build_observation("api", "success"),
        build_observation("web", "success"),
        build_observation("gpu", "success"),
    ]
    result = invoke(
        config=opts["config"],
        collector=lambda *args, **kwargs: recovery_observations,
        ownership_fetch=ownership_fetch,
    )
    assert result["status"] == "settled"
    assert result["counts"] == {"failure": 0, "success": 3, "unknown": 0}
    assert jira.closed("RHAI-1")
    assert len(slack.calls) == 2
    assert slack.calls[-1][1]["thread_ts"] == jira.properties["RHAI-1"]["delivery"]["root"]["ts"]


def test_mixed_promotion_streams_are_partitioned_before_threshold():
    stream_a = [
        build_observation("api"),
        build_observation("web"),
        build_observation("gpu", "success"),
    ]
    stream_b = [
        build_observation("memory", "success", oid="memory-success"),
        build_observation("storage", "success", oid="storage-success"),
    ]
    for obs in stream_b:
        obs["source_branch"] = "release-next"
        obs["destination_branch"] = "stable-next"

    result = aggregate_bulk_build_failures([*stream_a, *stream_b])
    bulk = result[-1]
    assert bulk["source_branch"] == "global"
    assert bulk["destination_branch"] == "global"
    assert bulk["evidence"]["total_builds"] == 3
    assert bulk["evidence"]["failed_builds"] == 2
    assert [item["component"] for item in bulk["evidence"]["affected_components"]] == ["api", "web"]
    assert all(obs in result for obs in stream_b)


def test_incomplete_recovery_does_not_close_bulk_jira(hook):
    invoke, jira, slack, opts, _ = hook
    failure_observations = [
        build_observation("api"),
        build_observation("web"),
        build_observation("gpu", "success"),
    ]
    ownership_fetch = lambda name: (
        "repo_to_component: {}\nrepo_to_slack_handle: {}"
        if name == "automerge_team_mapping.yaml"
        else "_default: DevOps\nmappings: {api: [API], web: [Web], promotion-gates-bulk: [DevOps]}"
        if name.startswith("konflux")
        else "components:\n  default:\n    slack_team_handle: devops"
    )
    invoke(
        config=opts["config"],
        collector=lambda *args, **kwargs: failure_observations,
        ownership_fetch=ownership_fetch,
    )
    assert len(jira.issues) == 1

    incomplete_recovery = [
        build_observation("api", "success"),
        build_observation("gpu", "success"),
        build_observation("memory", "success"),
    ]
    result = invoke(
        config=opts["config"],
        collector=lambda *args, **kwargs: incomplete_recovery,
        ownership_fetch=ownership_fetch,
    )
    assert result["status"] == "held"
    assert not jira.closed("RHAI-1")
