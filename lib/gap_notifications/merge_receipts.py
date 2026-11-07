"""Structured notification evidence; never an input to promotion verdicts."""
from dataclasses import asdict


def conflict_status(pr):
    conflict = pr.mergeable == "CONFLICTING" or pr.merge_state_status in {"DIRTY", "CONFLICTING"}
    contradictory = pr.mergeable == "MERGEABLE" and conflict
    if pr.state != "OPEN" or contradictory:
        return "unknown"
    if conflict:
        return "failure"
    return "success" if pr.mergeable == "MERGEABLE" else "unknown"


def make_receipt(pr, *, now, feasibility=None, gate=None, checks=(), query_error=None):
    """Optional evidence failures cannot change a repository gate verdict."""
    try:
        return _make_receipt(pr, now=now, feasibility=feasibility, gate=gate,
                             checks=checks, query_error=query_error)
    except (ValueError, TypeError, AttributeError, KeyError):
        return None


def _make_receipt(pr, *, now, feasibility=None, gate=None, checks=(), query_error=None):
    conflict = {"status": conflict_status(pr), "mergeable": pr.mergeable,
                "merge-state-status": pr.merge_state_status}
    feasibility_evidence = {"status": "unknown", "query-skipped": True}
    if feasibility is not None and gate is not None:
        error = bool(query_error or feasibility.query_error)
        status = {"pass": "success", "fail": "failure"}.get(gate.outcome, "unknown")
        # A query failure cannot become authoritative merely by aging out.
        if error:
            status = "unknown"
        if status == "success" and not (
            feasibility.found and feasibility.jobs and feasibility.run_id
            and feasibility.head_sha == pr.head_sha
            and feasibility.source_head_sha == pr.head_sha
            and feasibility.status == "completed" and feasibility.conclusion == "success"
            and all(job.status == "completed" and job.conclusion == "success" for job in feasibility.jobs)
        ):
            status = "unknown"
        if feasibility.head_sha != pr.head_sha:
            status = "unknown"
        from lib.gap_check_selection import select_feasibility_checks
        from lib.github_check_runs import latest_matching
        selected = [latest_matching(expected, [c for c in checks if c.head_sha == pr.head_sha])
                    for expected in select_feasibility_checks(feasibility)]
        selected = [c for c in selected if c is not None]
        if status == "success" and len(selected) != len(select_feasibility_checks(feasibility)):
            status = "unknown"
        if status == "failure" and any(
            f.status == "completed" and f.revision != pr.head_sha for f in gate.findings
        ):
            status = "unknown"
        feasibility_evidence = {
            "status": status, "query-error": error, "timed-out": gate.timed_out,
            "head-sha": feasibility.head_sha, "run-head-sha": feasibility.source_head_sha,
            "run-id": feasibility.run_id,
            "attempt": feasibility.attempt, "url": feasibility.url,
            "workflow-status": feasibility.status, "conclusion": feasibility.conclusion,
            "jobs": [{"name": j.name, "status": j.status, "conclusion": j.conclusion}
                     for j in feasibility.jobs],
            "findings": [asdict(f) for f in gate.findings],
            "checks": [{"id": c.id, "head-sha": c.head_sha, "name": c.name,
                        "status": c.status, "conclusion": c.conclusion} for c in selected],
        }
    return {"schema": 1, "repository": f"{pr.owner}/{pr.repo}", "pr": pr.number,
            "head-sha": pr.head_sha, "base-sha": pr.base_sha,
            "source-branch": pr.source_branch, "target-branch": pr.base_ref,
            "pr-state": pr.state, "observed-at": now.isoformat(),
            "fresh": False, "conflict": conflict, "feasibility": feasibility_evidence}


def unavailable_receipt(previous):
    """Bounded unknown evidence preserves valid definitive history, never old success."""
    import re
    result = {"schema": 1, "fresh": False, "conflict": {"status": "unknown"},
              "feasibility": {"status": "unknown"}}
    prior = previous.get("last-conflict") if isinstance(previous, dict) else None
    if (isinstance(prior, dict) and prior.get("status") in {"success", "failure"}
            and all(re.fullmatch(r"[a-fA-F0-9]{40}", str(prior.get(k) or ""))
                    for k in ("head-sha", "base-sha"))):
        result["last-conflict"] = {k: prior[k] for k in ("head-sha", "base-sha", "status")}
    return result


def bind_receipt(receipt, current, previous=None):
    """Validate notification freshness only; never defer ordinary child processing."""
    import copy
    result = copy.deepcopy(receipt)
    result["fresh"] = bool(current and result.get("head-sha") and result.get("base-sha")
                           and current.head_sha == result["head-sha"]
                           and current.base_sha == result["base-sha"]
                           and current.state == result["pr-state"]
                           and current.source_branch == result.get("source-branch")
                           and current.base_ref == result.get("target-branch")
                           and current.number == result["pr"]
                           and f"{current.owner}/{current.repo}" == result["repository"])
    if not result["fresh"]:
        # An unknown/moving snapshot must not erase available definitive history.
        if isinstance(previous, dict):
            prior = previous.get("last-conflict")
            if isinstance(prior, dict):
                result["last-conflict"] = {k: prior.get(k) for k in ("head-sha", "base-sha", "status")}
        return result
    status = result["conflict"]["status"]
    observed = conflict_status(current)
    if observed != status:
        result["conflict"]["status"] = "unknown"
    previous = previous if isinstance(previous, dict) else {}
    prior = previous.get("last-conflict")
    if not isinstance(prior, dict):
        prior = previous
    prior_conflict = prior.get("conflict")
    prior_conflict = prior_conflict if isinstance(prior_conflict, dict) else {}
    if (prior.get("head-sha"), prior.get("base-sha")) == (result["head-sha"], result["base-sha"]):
        prior_status = prior.get("status") or prior_conflict.get("status")
        if prior_status in {"success", "failure"} and status in {"success", "failure"} and prior_status != status:
            result["conflict"]["status"] = "unknown"
    if result["conflict"]["status"] in {"success", "failure"}:
        result["last-conflict"] = {"head-sha": result["head-sha"], "base-sha": result["base-sha"],
                                   "status": result["conflict"]["status"]}
    elif prior:
        result["last-conflict"] = {"head-sha": prior.get("head-sha"), "base-sha": prior.get("base-sha"),
                                   "status": prior.get("status") or prior_conflict.get("status")}
    return result
