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


def aggregate_bulk_build_failures(
    observations: list[dict[str, Any]],
    *,
    threshold: float = BULK_THRESHOLD,
    minimum_builds: int = BULK_MINIMUM_BUILDS,
) -> list[dict[str, Any]]:
    """Return observations unchanged unless a majority of builds failed.

    A majority is computed over all build observations in the supplied batch.
    When reached, failed build observations are replaced by one synthetic bulk
    build observation.  Successful and unknown build observations are retained,
    as are every conflict and feasibility observation.
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

    environments = {obs.get("environment") for obs in builds}
    if len(environments) != 1 or None in environments:
        return observations

    failed = [obs for obs in builds if obs.get("status") == "failure"]
    required = math.ceil(len(builds) * threshold)
    if not failed or len(failed) < required:
        return observations

    first = failed[0]
    evidence = {
        "bulk": True,
        "source": "bulk-aggregation",
        "historical_snapshot": True,
        "test_only": True,
        "note": "majority of promotion-gates component builds failed",
        "total_builds": len(builds),
        "failed_builds": len(failed),
        "threshold": threshold,
        "affected_components": [_affected_component(obs) for obs in failed],
        "leader_url": (first.get("evidence") or {}).get("leader_url"),
        "state_url": (first.get("evidence") or {}).get("state_url"),
    }
    bulk = {
        "environment": first["environment"],
        "repository": BULK_REPOSITORY,
        "source_branch": first["source_branch"],
        "destination_branch": first["destination_branch"],
        "category": "build",
        "component": BULK_COMPONENT,
        "status": "failure",
        "observation_id": _bulk_observation_id(failed),
        "evidence": evidence,
    }
    retained = [obs for obs in observations if not (obs.get("category") == "build" and obs.get("status") == "failure")]
    return retained + [bulk]
