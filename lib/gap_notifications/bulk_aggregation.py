"""Conservative bulk build-failure aggregation for GAP notifications.

The monitor normally emits one build observation per component.  When a
promotion batch has a majority of failed builds, callers may replace those
individual failure observations with one stable bulk incident.  Success and
unknown observations are preserved so existing recovery and diagnostics remain
unchanged.

Aggregation is intentionally additive: below the configured threshold the
input is returned unchanged.  The bulk incident reuses the normal build
category and a fixed component identity, so the existing dispatcher, Jira
registry, Slack threading, and title-based deduplication remain in charge of
delivery and recovery.
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any

BULK_COMPONENT = "promotion-gates-bulk"
BULK_REPOSITORY = "github.com/red-hat-data-services/gated-artifacts-promoter"
BULK_THRESHOLD = 0.5
BULK_MINIMUM_BUILDS = 3


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _bulk_observation_id(failed: list[dict[str, Any]]) -> str:
    identity = [
        {
            "repository": item.get("repository"),
            "component": item.get("component"),
            "observation_id": item.get("observation_id"),
        }
        for item in failed
    ]
    return hashlib.sha256(_canonical_json(identity).encode("utf-8")).hexdigest()


def _affected_component(observation: dict[str, Any]) -> dict[str, Any]:
    evidence = observation.get("evidence") or {}
    return {
        "component": observation.get("component"),
        "repository": observation.get("repository"),
        "pr_url": evidence.get("pr_url"),
        "build_url": evidence.get("build_url"),
        "reason": evidence.get("reason"),
        "observation_id": observation.get("observation_id"),
    }


def _bulk_fits(
    bulk: dict[str, Any],
    *,
    jira_environment: str,
    channel: str | None,
) -> bool:
    """Reject a synthetic bulk observation that cannot be validated or rendered."""
    from lib.gap_notifications.dispatch_incidents import validate_observations
    from lib.gap_notifications.notify_tools import render_markdown

    try:
        validate_observations([bulk], jira_environment=jira_environment, channel=channel)
        return len(render_markdown(bulk, jira_environment=jira_environment, channel=channel)) <= 24000
    except Exception:
        return False


def _partition_key(observation: dict[str, Any]) -> tuple[str, str, str]:
    return (
        observation.get("environment", ""),
        observation.get("source_branch", ""),
        observation.get("destination_branch", ""),
    )


def _component_identity(item: dict[str, Any]) -> str:
    return f"{item.get('repository')}/{item.get('component')}"


def aggregate_bulk_build_failures(
    observations: list[dict[str, Any]],
    *,
    threshold: float = BULK_THRESHOLD,
    minimum_builds: int = BULK_MINIMUM_BUILDS,
    jira_environment: str = "stage",
    channel: str | None = None,
) -> list[dict[str, Any]]:
    """Return observations unchanged unless a majority of builds failed.

    Builds are partitioned by environment and promotion stream before the
    threshold is applied.  Eligible failed partitions are then represented by
    one global bulk incident with a stable identity, so recovery and failure use
    the same incident key even when the original batch contains multiple streams.
    Successful and unknown observations are retained, as are conflict and
    feasibility observations.
    """
    if not isinstance(observations, list):
        raise ValueError("observations must be a list")
    if not isinstance(threshold, (int, float)) or not 0 < threshold <= 1:
        raise ValueError("bulk threshold must be between zero and one")
    if not isinstance(minimum_builds, int) or minimum_builds < 1:
        raise ValueError("minimum builds must be a positive integer")

    builds = [obs for obs in observations if obs.get("category") == "build"]
    if len(builds) < minimum_builds:
        return observations

    partitions: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for obs in builds:
        partitions.setdefault(_partition_key(obs), []).append(obs)

    eligible_failed: list[dict[str, Any]] = []
    eligible_total = 0
    for partition in partitions.values():
        failed = [obs for obs in partition if obs.get("status") == "failure"]
        required = math.ceil(len(partition) * threshold)
        if failed and len(failed) >= required:
            eligible_failed.extend(failed)
            eligible_total += len(partition)

    if eligible_failed:
        first = eligible_failed[0]
        evidence = {
            "bulk": True,
            "source": "bulk-aggregation",
            "historical_snapshot": True,
            "test_only": True,
            "note": "majority of promotion-gates component builds failed",
            "total_builds": eligible_total,
            "failed_builds": len(eligible_failed),
            "threshold": threshold,
            "affected_components": [_affected_component(obs) for obs in eligible_failed],
            "leader_url": (first.get("evidence") or {}).get("leader_url"),
            "state_url": (first.get("evidence") or {}).get("state_url"),
        }
        bulk = {
            "environment": first["environment"],
            "repository": BULK_REPOSITORY,
            "source_branch": "global",
            "destination_branch": "global",
            "category": "build",
            "component": BULK_COMPONENT,
            "status": "failure",
            "observation_id": _bulk_observation_id(eligible_failed),
            "evidence": evidence,
        }
        if not _bulk_fits(bulk, jira_environment=jira_environment, channel=channel):
            return observations
        eligible_ids = {id(obs) for obs in eligible_failed}
        retained = [obs for obs in observations if id(obs) not in eligible_ids]
        return retained + [bulk]

    successful = [obs for obs in builds if obs.get("status") == "success"]
    if len(successful) != len(builds):
        return observations

    recovery = {
        "environment": builds[0]["environment"],
        "repository": BULK_REPOSITORY,
        "source_branch": "global",
        "destination_branch": "global",
        "category": "build",
        "component": BULK_COMPONENT,
        "status": "success",
        "observation_id": _bulk_observation_id(successful),
        "evidence": {
            "bulk": True,
            "bulk_recovery": True,
            "source": "bulk-aggregation",
            "historical_snapshot": True,
            "test_only": True,
            "note": "all promotion-gates component builds recovered",
            "total_builds": len(builds),
            "failed_builds": 0,
            "threshold": threshold,
            "affected_components": [_affected_component(obs) for obs in successful],
        },
    }
    if _bulk_fits(recovery, jira_environment=jira_environment, channel=channel):
        return observations + [recovery]
    return observations
