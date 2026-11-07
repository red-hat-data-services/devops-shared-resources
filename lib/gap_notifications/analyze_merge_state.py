"""Collect accepted structured merge evidence, without live queries or prose routing."""
import copy
import re

from lib.gap_notifications.analyze_leader_state import (
    ObservationCollectionError, _creator_streams, _resolve_creator_stream,
    _parse_pr_url, _observation_id, collect_build_observations,
)


def collect_merge_observations(state, *, environment, leader_url, state_url):
    streams = _creator_streams(state)
    observations = []
    for child in state.get("pull-requests", []):
        receipt = child.get("merge-observations")
        if receipt is None:
            continue
        if not isinstance(receipt, dict) or receipt.get("schema") != 1:
            raise ObservationCollectionError("unsupported merge receipt")
        pr_url = child.get("pr-url") or child.get("pr_url")
        owner, repo, number = _parse_pr_url(pr_url)
        repository = f"{owner}/{repo}".lower()
        stream = _resolve_creator_stream(repository, pr_url, streams)
        valid = (
            receipt.get("repository", "").lower() == repository
            and receipt.get("pr") == number and child.get("repo", "").lower() == repo.lower()
            and receipt.get("fresh") is True and receipt.get("pr-state") == "OPEN"
            and receipt.get("head-sha") == child.get("head-sha")
            and all(re.fullmatch(r"[a-fA-F0-9]{40}", str(receipt.get(k) or ""))
                    for k in ("head-sha", "base-sha"))
            # Creator source is the promotion stream, not necessarily the generated PR head branch.
            and receipt.get("target-branch") == stream["target-branch"]
        )
        for category in ("conflict", "feasibility"):
            facts = receipt.get(category)
            if not isinstance(facts, dict):
                raise ObservationCollectionError("missing structured merge category")
            status = facts.get("status", "unknown") if valid else "unknown"
            if status not in {"success", "failure", "unknown"}:
                status = "unknown"
            if category == "conflict":
                conflict = facts.get("mergeable") == "CONFLICTING" or facts.get("merge-state-status") in {"DIRTY", "CONFLICTING"}
                if (facts.get("mergeable") == "MERGEABLE" and conflict
                        or status == "success" and facts.get("mergeable") != "MERGEABLE"
                        or status == "failure" and not conflict):
                    status = "unknown"
                fingerprint = {"status": status}
            else:
                if facts.get("query-error") or facts.get("query-skipped"):
                    status = "unknown"
                if facts.get("head-sha") != receipt.get("head-sha"):
                    status = "unknown"
                findings, checks = facts.get("findings") or [], facts.get("checks") or []
                jobs = facts.get("jobs") or []
                run_id = facts.get("run-id")
                attempt = facts.get("attempt")
                execution_valid = (type(run_id) is int and run_id > 0
                                   and type(attempt) is int and attempt > 0)
                if status == "success" and not (
                    execution_valid and facts.get("run-head-sha") == receipt.get("head-sha")
                    and facts.get("workflow-status") == "completed"
                    and facts.get("conclusion") == "success" and findings and checks and jobs
                    and all(j.get("status") == "completed" and j.get("conclusion") == "success" for j in jobs)
                    and len(findings) == len(checks)
                    and all(f.get("outcome") == "pass" for f in findings)
                    and all(c.get("head-sha") == receipt.get("head-sha")
                            and c.get("status") == "completed" and c.get("conclusion") == "success"
                            for c in checks)
                ):
                    status = "unknown"
                if status == "failure" and not (
                    (facts.get("timed-out") and any(f.get("outcome") == "fail" for f in findings))
                    or any(f.get("outcome") == "fail" and f.get("status") == "completed"
                           and f.get("revision") == receipt.get("head-sha")
                           and f.get("conclusion") != "success" for f in findings)
                    or (facts.get("run-id") and facts.get("run-head-sha") == receipt.get("head-sha")
                        and facts.get("workflow-status") == "completed"
                        and facts.get("conclusion") not in (None, "", "success"))
                ):
                    status = "unknown"
                fingerprint = {"status": status, "run-id": facts.get("run-id"),
                               "attempt": facts.get("attempt"), "checks": sorted(checks, key=lambda c: str(c.get("id"))),
                               "findings": sorted(
                                   [{k: f.get(k) for k in ("key", "outcome", "status", "conclusion")}
                                    for f in findings], key=lambda f: str(f.get("key"))),
                               "timed-out": facts.get("timed-out", False)}
            observation = {
                "environment": environment, "repository": repository,
                "source_branch": stream["source-branch"], "destination_branch": stream["target-branch"],
                "category": category, "status": status,
            }
            identity = dict(observation, pr=number, head=receipt.get("head-sha"),
                            base=receipt.get("base-sha"), facts=fingerprint, schema=1)
            observation["observation_id"] = _observation_id(identity)
            observation["evidence"] = {
                "source": "github-merge-receipt", "historical_snapshot": True,
                "pr_url": pr_url, "leader_url": leader_url, "state_url": state_url,
                "head-sha": receipt.get("head-sha"), "base-sha": receipt.get("base-sha"),
                "observed-at": receipt.get("observed-at"), "status": status,
                "reason": ("Merge conflict" if category == "conflict" else "Feasibility") + " " + status,
                "merge-receipt": copy.deepcopy(facts),
                "repository": repository,
                "workflow_url": (f"https://github.com/{repository}/actions/runs/{facts['run-id']}"
                                 if category == "feasibility" and type(facts.get("run-id")) is int
                                 and facts["run-id"] > 0 else None),
                "commit_url": (f"https://github.com/{repository}/commit/{receipt['head-sha']}" if valid else None),
            }
            observations.append(observation)
    return observations


def collect_observations(state, **kwargs):
    """Keep build observations and their event identities unchanged."""
    return collect_build_observations(state, **kwargs) + collect_merge_observations(state, **kwargs)
