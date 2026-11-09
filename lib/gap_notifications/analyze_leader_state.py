"""Normalize supplied Leader component-build evidence into lifecycle observations.

Resolve each child's promotion stream from structured creator outcomes and
validate repository/PR/current-head receipt consistency. Select component runs
from the recorded expected-component evidence and emit deterministic
failure/success/unknown observations with explicit provenance and event IDs.

Collection is offline: it does not refresh GitHub/Konflux, construct service
clients, or infer recovery from aggregate PR status, absence or closure. The
monitor must restrict input to accepted current decisions. This producer emits
build observations only; it does not invent conflict or feasibility evidence
or choose notification destinations/trial namespaces.
"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

OBSERVATION_SOURCE = "konflux"
BUILD_CATEGORY = "build"
ALLOWED_BUILD_STATUSES = frozenset({"success", "failure", "timed_out", "pending", "missing", "unknown", "cancelled"})


class ObservationCollectionError(ValueError):
    """Raised when a Leader snapshot cannot be normalized safely."""


def _mapping(value: Any, *, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ObservationCollectionError(f"{path} must be an object")
    return value


def _text(value: Any, *, path: str) -> str:
    if not isinstance(value, str):
        raise ObservationCollectionError(f"{path} must be a string")
    text = value.strip()
    if not text:
        raise ObservationCollectionError(f"{path} is required")
    return text


def _jsonable(value: Any, *, path: str) -> Any:
    try:
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ObservationCollectionError(f"{path} must be JSON-compatible") from exc
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _canonical_repo(value: Any) -> str:
    repo = _text(value, path="repository")
    if "://" in repo:
        repo = urlparse(repo).path.lstrip("/")
    return repo.removesuffix(".git").lower()


def _parse_pr_url(url: str) -> tuple[str, str, int]:
    text = _text(url, path="pr-url")
    parsed = urlparse(text)
    parts = [part for part in parsed.path.split("/") if part]
    if parsed.netloc.lower() != "github.com" or len(parts) != 4 or parts[2] != "pull":
        raise ObservationCollectionError(f"{text!r} is not a GitHub pull request URL")
    try:
        number = int(parts[3])
    except ValueError as exc:
        raise ObservationCollectionError(f"{text!r} is not a GitHub pull request URL") from exc
    return parts[0].lower(), parts[1].lower(), number


def _observation_id(identity: dict[str, Any]) -> str:
    payload = _canonical_json(identity)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _creator_streams(state: dict[str, Any]) -> list[dict[str, Any]]:
    raw = state.get("creator-outcomes", [])
    if not isinstance(raw, list):
        raise ObservationCollectionError("creator-outcomes must be a list when present")
    streams: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        record = _mapping(item, path=f"creator-outcomes[{index}]")
        repo = _text(record.get("repo"), path=f"creator-outcomes[{index}].repo")
        source = _text(record.get("source-branch"), path=f"creator-outcomes[{index}].source-branch")
        target = _text(record.get("target-branch"), path=f"creator-outcomes[{index}].target-branch")
        streams.append({
            "repo": repo.lower(),
            "source-branch": source,
            "target-branch": target,
            "outcome": _text(record.get("outcome"), path=f"creator-outcomes[{index}].outcome"),
            "reason": _text(record.get("reason"), path=f"creator-outcomes[{index}].reason"),
            "observed-at": _text(record.get("observed-at"), path=f"creator-outcomes[{index}].observed-at"),
            "pr-url": str(record.get("pr-url") or "").strip(),
            "workflow-url": str(record.get("workflow-url") or "").strip(),
        })
    return streams


def _resolve_creator_stream(repo: str, pr_url: str, streams: list[dict[str, Any]]) -> dict[str, Any]:
    matches = [item for item in streams if item["repo"] == repo]
    exact = [item for item in matches if item["pr-url"] == pr_url]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise ObservationCollectionError(f"ambiguous creator stream for {pr_url}")
    if matches:
        raise ObservationCollectionError(f"missing exact creator stream for {pr_url}")
    raise ObservationCollectionError(f"missing creator stream for {repo}")


def _created_at_key(value: Any) -> datetime:
    text = str(value or "").strip()
    if not text:
        return datetime.min
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ObservationCollectionError(f"invalid created_at timestamp: {text}") from exc


def _select_component_run(runs: list[dict[str, Any]], component: str) -> dict[str, Any] | None:
    matches = [run for run in runs if str(run.get("component") or "").strip() == component]
    if not matches:
        return None
    if len(matches) > 1:
        raise ObservationCollectionError(f"ambiguous current receipt for component {component}")
    return matches[0]


def _required_runs(receipt, runs, component, child):
    """Validate authoritative inventory before aggregating current required evidence."""
    inventory = receipt.get("required-pipelines")
    if not isinstance(inventory, dict) or inventory.get("schema") != 1 or inventory.get("source") != "revision-pinned-tekton":
        return [], "Invalid required pipeline inventory"
    if any(inventory.get(k) != receipt.get(k) for k in ("repository", "pr", "head-sha")):
        return [], "Required pipeline inventory binding mismatch"
    if child.get("head-sha") and child["head-sha"] != receipt.get("head-sha"):
        return [], "Child head does not match receipt"
    pairs = inventory.get("pipelines")
    if not isinstance(pairs, list) or not pairs:
        return [], "Missing required pipeline inventory"
    keys = []
    for pair in pairs:
        if not isinstance(pair, dict) or any(not isinstance(pair.get(k), str) or not pair[k].strip() for k in ("component", "pipeline_name")):
            return [], "Invalid required pipeline identity"
        keys.append((pair["component"], pair["pipeline_name"]))
    if len(keys) != len(set(keys)) or set(c for c, p in keys) != set(receipt.get("expected-components") or []):
        return [], "Conflicting required pipeline inventory"
    selected = []
    for c, pipeline in sorted(keys):
        if c != component:
            continue
        matches = [r for r in runs if r.get("component") == c and r.get("pipeline_name") == pipeline]
        # Ambiguity in one pair cannot erase a proven failure in another pair.
        selected.append((pipeline, matches[0] if len(matches) == 1 else None))
    return selected, None


def _child_repo(child: dict[str, Any]) -> str:
    repo = str(child.get("repo") or "").strip()
    if not repo:
        raise ObservationCollectionError("pull-requests entry is missing repo")
    return repo.lower()


def _child_pr_url(child: dict[str, Any]) -> str:
    return _text(child.get("pr-url") or child.get("pr_url") or child.get("url"), path="pr-url")


def _normalized_evidence(*, environment: str, leader_url: str, state_url: str, pr_url: str,
                         child_repo: str, pr_status: str | None, stream: dict[str, Any],
                         build_observations: dict[str, Any], component: str,
                         selected_run: dict[str, Any] | None, build_url: str | None) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "source": OBSERVATION_SOURCE,
        "historical_snapshot": True,
        "test_only": True,
        "note": "historical snapshot evidence for collector validation",
        "provenance": {
            "kind": "historical-snapshot",
            "leader_url": leader_url,
            "state_url": state_url,
            "pr_url": pr_url,
            "build_url": build_url,
        },
        "environment": environment,
        "leader_url": leader_url,
        "state_url": state_url,
        "pr_url": pr_url,
        "build_url": build_url,
        "repository": build_observations.get("repository") or child_repo,
        "pr": build_observations.get("pr"),
        "head-sha": build_observations.get("head-sha"),
        "observed-at": build_observations.get("observed-at"),
        "pr-state": build_observations.get("pr-state"),
        "pr-status": pr_status,
        "pr_status": pr_status,
        "source-branch": stream["source-branch"],
        "target-branch": stream["target-branch"],
        "creator-outcome": {
            "outcome": stream["outcome"],
            "reason": stream["reason"],
            "observed-at": stream["observed-at"],
            "pr-url": stream["pr-url"],
            "workflow-url": stream["workflow-url"],
        },
        "expected-components": list(build_observations.get("expected-components") or []),
        "query-error": build_observations.get("query-error"),
        "query-skipped": build_observations.get("query-skipped"),
        "selected-component": component,
    }
    if selected_run is not None:
        evidence["selected-run"] = copy.deepcopy(selected_run)
    return _jsonable(evidence, path="evidence")


def _status_for_observation(build_observations: dict[str, Any], selected_run: dict[str, Any] | None, *, pr_number: int) -> tuple[str, str]:
    if build_observations.get("query-error"):
        return "unknown", str(build_observations["query-error"])
    if build_observations.get("query-skipped"):
        return "unknown", str(build_observations["query-skipped"])
    if str(build_observations.get("pr") or "").strip() != str(pr_number):
        return "unknown", "Receipt PR number does not match the pull request URL"
    if selected_run is None:
        return "unknown", "Expected component is missing from the current receipt"
    run_uid = str(selected_run.get("uid") or "").strip()
    if not run_uid:
        return "unknown", "Current receipt UID is missing"
    run_status = str(selected_run.get("status") or "unknown").strip() or "unknown"
    if run_status not in ALLOWED_BUILD_STATUSES:
        return "unknown", f"Unsupported build status: {run_status}"
    head_sha = str(build_observations.get("head-sha") or "").strip()
    run_head_sha = str(selected_run.get("head_sha") or "").strip()
    if not head_sha or not run_head_sha:
        return "unknown", "Current receipt SHA is missing"
    if run_head_sha != head_sha:
        return "unknown", "Current receipt SHA does not match the leader snapshot"
    if run_status in {"failure", "timed_out"}:
        return "failure", str(selected_run.get("reason") or run_status)
    if run_status == "success":
        return "success", str(selected_run.get("reason") or run_status)
    return "unknown", str(selected_run.get("reason") or run_status)


def _normalize_pull_request(child: dict[str, Any], *, environment: str, leader_url: str, state_url: str,
                            streams: list[dict[str, Any]]) -> list[dict[str, Any]]:
    pr_url = _child_pr_url(child)
    pr_owner, pr_repo, pr_number = _parse_pr_url(pr_url)
    child_repo = _child_repo(child)
    build_observations = _mapping(child.get("build-observations"), path="build-observations")
    repo = str(build_observations.get("repository") or f"{pr_owner}/{pr_repo}").strip().lower()
    if repo != f"{pr_owner}/{pr_repo}":
        raise ObservationCollectionError(f"pull request repository mismatch for {pr_url}")
    stream = _resolve_creator_stream(repo, pr_url, streams)
    if str(build_observations.get("source") or "").strip() != OBSERVATION_SOURCE:
        return []
    expected_components = [str(item) for item in build_observations.get("expected-components") or [] if str(item).strip()]
    if not expected_components:
        return []
    runs_raw = build_observations.get("runs") or []
    if not isinstance(runs_raw, list):
        raise ObservationCollectionError("build-observations.runs must be a list")
    runs = [_mapping(run, path="build-observations.runs") for run in runs_raw]
    observations: list[dict[str, Any]] = []
    for component in expected_components:
        aggregate = None
        if "required-pipelines" in build_observations:
            aggregate, inventory_error = _required_runs(build_observations, runs, component, child)
            outcomes = [_status_for_observation(build_observations, r, pr_number=pr_number) for p, r in aggregate]
            if inventory_error or not outcomes:
                status, reason = "unknown", inventory_error or "Missing required pipelines"
            elif any(s == "failure" for s, _ in outcomes):
                status, reason = "failure", "Required pipeline failure"
            elif all(s == "success" for s, _ in outcomes):
                status, reason = "success", "All required pipelines succeeded"
            else:
                status, reason = "unknown", "Incomplete required pipeline success coverage"
            selected_run = next((r for (p, r), (s, _) in zip(aggregate, outcomes) if s == status and r), None)
            if len(aggregate) == 1 and not inventory_error:
                selected_run = aggregate[0][1]
                status, reason = outcomes[0]
        else:
            selected_run = _select_component_run(runs, component)
            status, reason = _status_for_observation(build_observations, selected_run, pr_number=pr_number)
        if child.get("head-sha") and child["head-sha"] != build_observations.get("head-sha"):
            status, reason = "unknown", "Child head does not match receipt"
        if selected_run is not None and selected_run.get("component") and str(selected_run["component"]).strip() != component:
            status, reason = "unknown", "Selected receipt does not match the expected component"
        build_url = str((selected_run or {}).get("url") or "").strip() or None
        evidence = _normalized_evidence(
            environment=environment, leader_url=leader_url, state_url=state_url, pr_url=pr_url,
            child_repo=child_repo, pr_status=str(child.get("pr-status") or child.get("pr_status") or "").strip() or None,
            stream=stream, build_observations=build_observations, component=component,
            selected_run=selected_run, build_url=build_url,
        )
        evidence["status"] = status
        evidence["reason"] = reason
        identity = {
            "environment": environment,
            "repository": repo,
            "source_branch": stream["source-branch"],
            "destination_branch": stream["target-branch"],
            "category": BUILD_CATEGORY,
            "component": component,
            "head-sha": build_observations.get("head-sha"),
            "status": status,
            "selected-run": {
                "uid": (selected_run or {}).get("uid"),
                "status": (selected_run or {}).get("status"),
                "component": (selected_run or {}).get("component"),
            },
        }
        if build_observations.get("query-error"):
            identity["query-kind"] = "query-error"
        elif build_observations.get("query-skipped"):
            identity["query-kind"] = "query-skipped"
        else:
            identity["query-kind"] = "receipt"
        if aggregate is not None:
            evidence["required-pipelines"] = copy.deepcopy(build_observations.get("required-pipelines"))
            evidence["selected-runs"] = [copy.deepcopy(r) for p, r in aggregate if r is not None]
            if len(aggregate) != 1:
                identity.pop("selected-run")
                identity["required-runs"] = [{"pipeline_name": p, "uid": (r or {}).get("uid"),
                                              "status": (r or {}).get("status")} for p, r in aggregate]
        observation = {
            "environment": environment,
            "repository": repo,
            "source_branch": stream["source-branch"],
            "destination_branch": stream["target-branch"],
            "category": BUILD_CATEGORY,
            "component": component,
            "status": status,
            "evidence": evidence,
        }
        observation["observation_id"] = _observation_id(identity)
        observations.append(observation)
    return observations


def collect_build_observations(state: dict[str, Any], *, environment: str, leader_url: str, state_url: str) -> list[dict[str, Any]]:
    """Collect normalized build observations from a validated Leader snapshot."""
    payload = _mapping(state, path="state")
    environment = _text(environment, path="environment")
    leader_url = _text(leader_url, path="leader_url")
    state_url = _text(state_url, path="state_url")
    streams = _creator_streams(payload)
    observations: list[dict[str, Any]] = []
    pull_requests = payload.get("pull-requests", [])
    if not isinstance(pull_requests, list):
        raise ObservationCollectionError("pull-requests must be a list")
    for index, child in enumerate(pull_requests):
        if not isinstance(child, dict):
            raise ObservationCollectionError(f"pull-requests[{index}] must be an object")
        build_observations = child.get("build-observations")
        if not isinstance(build_observations, dict):
            continue
        observations.extend(_normalize_pull_request(child, environment=environment, leader_url=leader_url, state_url=state_url, streams=streams))
    return observations
