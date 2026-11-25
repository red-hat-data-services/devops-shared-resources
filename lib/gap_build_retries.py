"""Bounded PaC retries; Konflux observations remain the only build authority.

The serialized GAP monitor is the sole writer. A single /retest comment contains
both command and receipt, so a crash before state publication cannot lose the
retry budget. Receipts on the child PR also deduplicate across different leaders.
"""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import os
import re

from lib.github_check_runs import (
    CheckFinding, CheckQueryError, OUTCOME_FAIL, OUTCOME_PASS, OUTCOME_PENDING,
    collect_github_pages, github_api_json,
)
from lib.github_cli import GhCommandError
from lib.konflux_builds import KonfluxQueryError, NAME

MAX_RETRIGGERS = 5
START_GRACE = timedelta(hours=2)
MARKER = re.compile(r"<!-- gap-build-retry:v1 (\{[^\n]+\}) -->")
STALE_VERIFIABLE_STATUSES = {"failure", "timed_out", "cancelled"}


class RetryObservationChanged(KonfluxQueryError):
    """The failed attempt is obsolete; never classify its replacement as failed."""


def retries_enabled():
    """Explicit opt-in rollout; legacy monitors do not post retry comments."""
    return os.environ.get("GAP_AUTO_RETRY_BUILDS", "false").lower() == "true"


def _moment(value):
    """Read an offset-aware receipt/controller time, rejecting malformed data."""
    if not isinstance(value, str):
        raise ValueError("Retry timestamp must be text")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Retry receipt has no timezone")
    return parsed


class BuildRetryManager:
    """Apply retries only on the monitor's final, fresh batch evaluation."""

    def __init__(self, runner, payload, *, dry_run=False, client_factory=None, clock=None):
        """Configure a serialized monitor writer and recover its saved receipts.

        ``client_factory`` is accepted for backward compatibility but no
        longer used; retries always go through /retest.
        """
        self.runner, self.dry_run = runner, dry_run
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        _default_actor = "rhods-ci-bot" if os.environ.get("GAP_RETRY_COMMENT_TOKEN") else "rhods-devops-app[bot]"
        self.actor = os.environ.get("GAP_BUILD_RETRY_ACTOR", _default_actor)
        self.saved = {}
        self.ignored_urls = set()
        for entry in payload.get("pull-requests", []):
            if not isinstance(entry, dict):
                continue
            url = entry.get("pr-url")
            receipt = entry.get("build-observations") or {}
            self.saved[url] = deepcopy(receipt.get("retry-history", []))
            if entry.get("ignore") is True:
                self.ignored_urls.add(url)

    def _history(self, pr):
        """Reconcile trusted child comments with state across leaders/restarts."""
        endpoint = f"repos/{pr.owner}/{pr.repo}/issues/{pr.number}/comments"
        comments = collect_github_pages(self.runner, endpoint, None, 100)
        # When migrating actors (GAP_RETRY_COMMENT_TOKEN set + different actor),
        # ignore saved retry records: they don't store the author, so old-actor
        # saved records would otherwise block the new actor's budget.  Comment-
        # based records are properly author-filtered below and will re-supply
        # any new-actor receipts that were previously saved.
        if os.environ.get("GAP_RETRY_COMMENT_TOKEN") and self.actor != "rhods-devops-app[bot]":
            history = []
        else:
            history = list(self.saved.get(pr.url, []))
        for comment in comments:
            user = comment.get("user") or {}
            body = str(comment.get("body") or "")
            first_line = body.splitlines()[0].strip() if body.splitlines() else ""
            if user.get("login") != self.actor:
                # Ignore human quotes/spoofed markers. A bot-issued retry with
                # a different identity may be a changed deployment actor; stop
                # rather than silently resetting its cross-leader budget.
                if user.get("type") != "Bot" or not (
                    first_line in {"/retest", "/test"}
                    or first_line.startswith("/retest ")
                    or first_line.startswith("/test ")
                    or first_line.startswith("/build-konflux ")
                ):
                    continue
            for match in MARKER.finditer(body):
                record = json.loads(match[1])
                if record.get("repository") == f"{pr.owner}/{pr.repo}" and record.get("pr") == pr.number:
                    if (comment.get("user") or {}).get("login") != self.actor:
                        if not os.environ.get("GAP_RETRY_COMMENT_TOKEN"):
                            raise ValueError("Retry receipt author differs from GAP_BUILD_RETRY_ACTOR; reconcile before retrying")
                        # Intentional actor migration: skip old-actor receipts
                        # without counting them against the new actor's budget.
                        continue
                    record.update(comment_url=comment.get("html_url"), comment_id=comment.get("id"))
                    # GitHub time, not a timestamp copied into the comment body.
                    record["requested_at"] = comment["created_at"]
                    history.append(record)
        unique = {}
        for record in history:
            if (not isinstance(record, dict) or not record.get("run_uid") or not record.get("component")
                    or not record.get("sha") or not record.get("pipeline")):
                raise ValueError("Invalid build retry history")
            _moment(record["requested_at"])
            unique[(record["component"], record["sha"], record["run_uid"])] = record
        return list(unique.values())

    def _pipeline_check_exists(self, pr, pipeline):
        """Return whether GitHub already has a current-head check for a pipeline."""
        endpoint = f"repos/{pr.owner}/{pr.repo}/commits/{pr.head_sha}/check-runs"
        try:
            checks = collect_github_pages(self.runner, endpoint, "check_runs", 100)
        except (CheckQueryError, GhCommandError) as exc:
            raise CheckQueryError(f"Could not read current-head checks for {pr.url}: {exc}") from exc
        for check in checks:
            if not isinstance(check, dict) or check.get("head_sha") != pr.head_sha:
                continue
            name = str(check.get("name") or "")
            suffix = name.split(" / ")[-1]
            if suffix == pipeline:
                return True
        return False

    def _retry_missing(self, pr, observation, history, pipeline, *, cel_evaluated,
                        bare_retry_state=None, pipeline_alternatives=None,
                        observation_status="missing"):
        """Retrigger an expected component via /retest.

        Handles both missing builds (never appeared) and completed failures
        (failure, timed_out, cancelled).  For missing builds, suppress the
        retry when a check already appeared on GitHub (Konflux archive lag).
        For completed failures the existing check is the failed one, so the
        guard is skipped.
        """
        now = self.clock()
        _pipelines = set(pipeline_alternatives) if pipeline_alternatives else {pipeline}
        records = [
            r for r in history
            if r["component"] == observation["component"]
            and r["sha"] == pr.head_sha
            and r["pipeline"] in _pipelines
        ]
        count = len(records)
        info = {"retrigger_count": count, "status": "not-retried"}
        if not cel_evaluated:
            info["reason"] = "Build retry requires authoritative CEL evidence"
            return info, True
        if records:
            last = max(records, key=lambda r: _moment(r["requested_at"]))
            waiting = now - _moment(last["requested_at"]) < START_GRACE
            # Verify the retrigger actually produced a new PipelineRun.
            # Compare the current observation UID with the retry record's run_uid.
            # If they match, PaC didn't create a new PipelineRun from the /retest.
            RETRIGGER_VERIFY_GRACE = timedelta(minutes=15)
            elapsed = now - _moment(last["requested_at"])
            retried_uid = last.get("run_uid", "")
            current_uid = observation.get("uid", "")
            if (waiting
                    and elapsed > RETRIGGER_VERIFY_GRACE
                    and current_uid and retried_uid
                    and current_uid == retried_uid
                    and observation_status in STALE_VERIFIABLE_STATUSES):
                info.update(
                    status="retrigger-stale",
                    reason=f"No new PipelineRun after {int(elapsed.total_seconds() / 60)}min; original {observation_status} persists",
                    requested_at=last["requested_at"],
                    comment_url=last.get("comment_url"),
                    retrigger_count=count,
                    run_uid=current_uid,
                )
                return info, False  # waiting=False → OUTCOME_FAIL
            if waiting:
                info.update(
                    status="build-retrigger-pending",
                    reason="Waiting for a new Konflux attempt",
                    requested_at=last["requested_at"], comment_url=last.get("comment_url"),
                )
                return info, True
            if count >= MAX_RETRIGGERS:
                info.update(
                    status="retry-exhausted",
                    reason="Five automatic retries already used for this component and SHA",
                    requested_at=last["requested_at"], comment_url=last.get("comment_url"),
                )
                return info, False
        if count >= MAX_RETRIGGERS:
            info["status"] = "retry-exhausted"
            info["reason"] = "Two automatic retries already used for this component and SHA"
            return info, False
        if not pipeline or not NAME.fullmatch(pipeline):
            info["reason"] = "Missing verified expected PipelineRun identity"
            return info, True
        # For missing builds, a check may exist while the Konflux archive is
        # still catching up — suppress /retest in that race.  For completed
        # failures the existing check IS the failed one; always allow retry.
        if observation_status == "missing":
            if any(self._pipeline_check_exists(pr, p) for p in _pipelines):
                info.update(status="build-trigger-pending",
                            reason="Current GitHub check exists; waiting for Konflux PipelineRun")
                return info, True
        current = github_api_json(self.runner, f"repos/{pr.owner}/{pr.repo}/pulls/{pr.number}")
        if current.get("state") != "open" or (current.get("head") or {}).get("sha") != pr.head_sha:
            raise RetryObservationChanged("PR closed or changed head before retry; awaiting monitor refresh")
        if observation_status == "missing":
            reason = "Expected PR build was not triggered; CEL expected this component"
            run_uid = f"missing:{pipeline}:{pr.head_sha}:{count + 1}"
        else:
            reason = observation.get("reason") or f"Build {observation_status}"
            run_uid = observation.get("uid") or f"retry:{pipeline}:{pr.head_sha}:{count + 1}"
        record = {
            "repository": f"{pr.owner}/{pr.repo}", "pr": pr.number,
            "component": observation["component"], "sha": pr.head_sha,
            "pipeline": pipeline, "run_uid": run_uid,
            "retrigger_count": count + 1, "requested_at": now.isoformat(), "reason": reason,
        }
        if self.dry_run:
            info.update(status="would-retrigger", reason=reason)
            return info, True
        # A bare /retest reruns every eligible missing pipeline on the PR.
        # Persist one marker per affected component in the same comment so a
        # restart can recover the shared attempt and every component budget.
        command = "/retest"
        record["command"] = command
        if bare_retry_state and bare_retry_state.get("receipt"):
            prior = bare_retry_state["receipt"]
            record.update(comment_url=prior["comment_url"], comment_id=prior["comment_id"],
                          requested_at=prior["requested_at"])
            history.append(record)
            self.saved[pr.url] = deepcopy(history)
            info.update(status="build-retrigger-pending", retrigger_count=count + 1,
                        requested_at=record["requested_at"], comment_url=record["comment_url"], reason=reason)
            return info, True
        batch_records = [record]
        exhausted_components = []
        grace_components = []
        for item in (bare_retry_state or {}).get("components", []):
            component, item_pipeline = item["component"], item["pipeline"]
            if component == observation["component"]:
                continue
            previous = [r for r in history if r["component"] == component and r["sha"] == pr.head_sha]
            if len(previous) >= MAX_RETRIGGERS:
                exhausted_components.append(component)
                continue
            if previous:
                last = max(previous, key=lambda r: _moment(r["requested_at"]))
                if now - _moment(last["requested_at"]) < START_GRACE:
                    grace_components.append(component)
                    continue
            batch_records.append({
                "repository": f"{pr.owner}/{pr.repo}", "pr": pr.number,
                "component": component, "sha": pr.head_sha,
                "pipeline": item_pipeline,
                "run_uid": item.get("run_uid") or f"missing:{item_pipeline}:{pr.head_sha}:{len(previous) + 1}",
                "retrigger_count": len(previous) + 1,
                "requested_at": now.isoformat(),
                "reason": item.get("reason") or reason, "command": command,
            })
        if exhausted_components:
            info.update(
                status="retry-exhausted",
                reason=("Missing-build retry budget exhausted for: "
                        + ", ".join(sorted(exhausted_components))),
            )
            return info, False
        if grace_components:
            info.update(
                status="build-retrigger-pending",
                reason=("Waiting for retry grace period for: "
                        + ", ".join(sorted(grace_components))),
            )
            return info, True
        marker_text = "\n".join(
            f"<!-- gap-build-retry:v1 {json.dumps(item, sort_keys=True)} -->"
            for item in batch_records
        )
        body = f"{command}\n\n{marker_text}"
        # Use a dedicated token for /retest comments when available so the
        # comment is posted as a PaC-authorized collaborator (rhods-ci-bot)
        # instead of the GitHub App bot which PaC does not authorize.
        retry_token = os.environ.get("GAP_RETRY_COMMENT_TOKEN")
        original_gh_token = os.environ.get("GH_TOKEN")
        if retry_token:
            os.environ["GH_TOKEN"] = retry_token
        try:
            response = json.loads(self.runner([
                "api", f"repos/{pr.owner}/{pr.repo}/issues/{pr.number}/comments",
                "--method", "POST", "-f", "body=" + body,
            ]))
            if (response.get("user") or {}).get("login") != self.actor:
                raise RuntimeError("Retry comment author differs from GAP_BUILD_RETRY_ACTOR; stop automatic retries")
            receipt = {"comment_url": response["html_url"], "comment_id": response["id"],
                       "requested_at": response["created_at"]}
            if bare_retry_state is not None:
                bare_retry_state["receipt"] = receipt
            for item in batch_records:
                item.update(receipt)
        except (RuntimeError, ValueError, TypeError, KeyError) as exc:
            raise RuntimeError("Could not confirm retry delivery; reconcile child comments on the next poll") from exc
        finally:
            if retry_token:
                if original_gh_token is not None:
                    os.environ["GH_TOKEN"] = original_gh_token
                else:
                    os.environ.pop("GH_TOKEN", None)
        history.extend(batch_records)
        self.saved[pr.url] = deepcopy(history)
        info.update(status="build-retrigger-pending", retrigger_count=count + 1,
                    requested_at=record["requested_at"], comment_url=record["comment_url"], reason=reason)
        return info, True

    def apply(self, pr, gate, evidence):
        """Keep raw observations intact; pending retry never authorizes EG images."""
        if pr.state != "OPEN":
            return gate, evidence
        # Case-by-case ignore: operators opted this child out of gating; do not
        # keep firing automatic /retest while it remains ignored.
        # getattr: some tests construct via object.__new__ without __init__.
        if pr.url in getattr(self, "ignored_urls", ()):
            return gate, evidence
        evidence = deepcopy(evidence)
        if evidence.get("query-error"):
            # A partial read cannot establish that an in-flight retry finished.
            evidence["retry-history"] = deepcopy(self.saved.get(pr.url, []))
            return replace(gate, outcome=OUTCOME_PENDING), evidence
        if not evidence.get("runs"):
            expected_components = set(evidence.get("expected-components") or [])
            if expected_components and evidence.get("cel-evaluated") is True:
                evidence["runs"] = [
                    {
                        "component": component,
                        "status": "missing",
                        "reason": "No matching current-head Konflux PipelineRun found",
                        "head_sha": pr.head_sha,
                        "name": "", "uid": "", "namespace": "",
                        "created_at": "", "url": "", "pipeline_name": "",
                        "completed_at": "", "message": "",
                    }
                    for component in sorted(expected_components)
                ]
            else:
                return gate, evidence
        # Reconcile even after success: the process may have died after POST
        # and the replacement may have finished before this monitor cycle.
        history = self._history(pr)
        evidence["retry-history"] = history
        findings = list(gate.findings)
        # Evidence and gate findings should normally be one-to-one. Keep the
        # monitor fail-closed if a partial/reconciled response contains an
        # observation without a corresponding finding.
        for observation in evidence["runs"][len(findings):]:
            status = observation.get("status")
            outcome = OUTCOME_PENDING if status in {"missing", "pending", "running", "queued"} else OUTCOME_FAIL
            findings.append(CheckFinding(
                key=str(observation.get("component") or "unknown"),
                outcome=outcome,
                reason=str(observation.get("reason") or "Unmatched build observation"),
                revision=observation.get("head_sha"),
                status=status,
                conclusion=status,
                name=observation.get("name") or observation.get("component"),
            ))
        awaiting = False
        bare_retry_state = {}
        components = [r["component"] for r in evidence["runs"]]
        expected_components = set(evidence.get("expected-components") or [])
        cel_evaluated = evidence.get("cel-evaluated") is True
        required_pipelines = {
            item.get("component"): item.get("pipeline_name")
            for item in (evidence.get("required-pipelines") or {}).get("pipelines", [])
            if isinstance(item, dict)
        }
        # Multi-cluster alternatives: component → tuple of all pipeline names.
        required_pipeline_alts: dict[str, tuple[str, ...]] = {}
        for item in (evidence.get("required-pipelines") or {}).get("pipelines", []):
            if isinstance(item, dict):
                comp = item.get("component")
                alts = item.get("pipeline_names")
                if comp and isinstance(alts, list) and len(alts) > 1:
                    required_pipeline_alts[comp] = tuple(alts)
        bare_retry_state["components"] = []
        seen_retry_components = set()
        retryable_statuses = {"missing", "failure", "timed_out", "cancelled"}
        for item in evidence["runs"]:
            component = item.get("component")
            if (item.get("status") in retryable_statuses and component in expected_components
                    and components.count(component) == 1 and component not in seen_retry_components):
                pipeline = item.get("pipeline_name") or required_pipelines.get(component, "")
                if pipeline and NAME.fullmatch(pipeline):
                    bare_retry_state["components"].append({
                        "component": component, "pipeline": pipeline,
                        # Keep missing candidates unassigned until the batch
                        # builder can use their current per-component count.
                        "run_uid": item.get("uid") if item.get("status") != "missing" else None,
                        "reason": item.get("reason") or "Expected PR build was not triggered; CEL expected this component",
                    })
                    seen_retry_components.add(component)
        for i, observation in enumerate(evidence["runs"]):
            status = observation["status"]
            if status in retryable_statuses:
                component = observation["component"]
                pipeline = observation.get("pipeline_name") or required_pipelines.get(component, "")
                can_retry = (component in expected_components
                             and components.count(component) == 1)
                if can_retry:
                    try:
                        info, waiting = self._retry_missing(
                            pr, observation, history, pipeline, cel_evaluated=cel_evaluated,
                            bare_retry_state=bare_retry_state,
                            pipeline_alternatives=required_pipeline_alts.get(component),
                            observation_status=status,
                        )
                    except (RetryObservationChanged, KonfluxQueryError, CheckQueryError, ValueError, TypeError, KeyError) as exc:
                        info, waiting = {"status": "retry-evidence-unavailable", "reason": str(exc)}, True
                    observation["retry"] = info
                    if observation.get("status") != "missing" or info.get("status") == "retry-evidence-unavailable":
                        awaiting |= waiting
                    findings[i] = replace(
                        findings[i], outcome=OUTCOME_PASS if (observation.get("status") == "missing" and info.get("status") != "retry-evidence-unavailable") else (OUTCOME_PENDING if waiting else OUTCOME_FAIL),
                        reason=f"{findings[i].reason}; {info.get('reason', '')}",
                    )
                elif status == "missing":
                    observation["retry"] = {
                        "status": "not-retried",
                        "reason": "Missing build trigger is not retryable without matching expected component and CEL evidence",
                    }
                    # Do NOT set awaiting = True for missing components
                    findings[i] = replace(
                        findings[i], outcome=OUTCOME_PASS,
                        reason=f"{findings[i].reason}; missing build trigger requires authoritative evidence",
                    )
                else:
                    observation["retry"] = {
                        "status": "not-retried",
                        "reason": ("Multiple PR pipelines share this component"
                                   if components.count(component) != 1
                                   else "Build failure is not retryable without matching expected component evidence"),
                    }
                continue
            # Non-retryable statuses (success, pending, running, queued).
            records = [r for r in history if r["component"] == observation["component"] and r["sha"] == pr.head_sha]
            if records and observation["status"] != "success":
                awaiting = True
                observation["retry"] = {"status": "build-retrigger-pending", "retrigger_count": len(records),
                                        "reason": "Retry attempt is pending or its result is not yet known"}
        evidence["retry-history"] = deepcopy(history)
        # Hold the whole repository while a retry is in flight, even if another
        # component has a persistent failure. It must not be archived mid-retry.
        # When all findings are PASS (e.g. missing builds treated as green), the
        # original gate.outcome may still be PENDING from the initial evaluation;
        # use OUTCOME_PASS so missing-only PRs are not held indefinitely.
        outcome = OUTCOME_PENDING if awaiting or any(
            f.outcome == OUTCOME_PENDING for f in findings
        ) else (
            OUTCOME_FAIL if any(f.outcome == OUTCOME_FAIL for f in findings) else OUTCOME_PASS)
        return replace(gate, outcome=outcome, findings=tuple(findings)), evidence
