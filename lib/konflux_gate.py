"""Adapt shared Konflux observations to the existing GAP gate/result contract."""
from __future__ import annotations

from datetime import datetime, timezone
from lib.github_check_runs import CheckFinding, GateResult, OUTCOME_FAIL, OUTCOME_PASS, OUTCOME_PENDING
from lib.konflux_builds import KonfluxClient, KonfluxQueryError


def evaluate_konflux_gate(*, repo, number, sha, expected, client=None, query_error=None):
    """Translate expected Konflux builds into a gate verdict and evidence receipt."""
    evidence = {"source": "konflux", "head-sha": sha, "repository": repo, "pr": number,
                "observed-at": datetime.now(timezone.utc).isoformat(),
                "expected-components": sorted({item.key for item in expected}),
                "runs": []}
    findings = []
    try:
        if query_error:
            raise KonfluxQueryError(query_error)
        if expected:
            client = client or KonfluxClient.from_environment()
        for item in expected:
            component = item.key
            observation = client.observe(repo=repo, number=number, sha=sha, components={component},
                                         pipeline_names={item.name_suffix} if item.name_suffix else None)
            evidence["runs"].append(observation.to_dict())
            outcome = OUTCOME_PASS if observation.status == "success" else (
                OUTCOME_FAIL if observation.status in {"failure", "timed_out"} else OUTCOME_PENDING)
            findings.append(CheckFinding(
                key=component, outcome=outcome,
                reason=f"Konflux {observation.status}: {observation.reason}",
                link=observation.url or None, revision=sha,
                status=observation.status, conclusion=observation.status,
                name=observation.name or component,
            ))
    except (KonfluxQueryError, ValueError, TypeError) as exc:
        evidence["query-error"] = str(exc)
        findings.append(CheckFinding(key="konflux-query", outcome=OUTCOME_PENDING,
                                     reason=str(exc), revision=sha, status="unknown"))
    outcome = OUTCOME_FAIL if any(f.outcome == OUTCOME_FAIL for f in findings) else (
        OUTCOME_PENDING if any(f.outcome == OUTCOME_PENDING for f in findings) else OUTCOME_PASS)
    link = next((f.link for f in findings if f.outcome == outcome and f.link), None)
    return GateResult(outcome=outcome, findings=tuple(findings), timed_out=False, link=link), evidence
