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
    CheckFinding, CheckQueryError, OUTCOME_FAIL, OUTCOME_PENDING,
    collect_github_pages, github_api_json,
)
from lib.github_cli import GhCommandError
from lib.konflux_builds import KonfluxClient, KonfluxQueryError, NAME
from lib.konflux_retry_evidence import component_push_branch, failure_details, recent_push_health

MAX_RETRIGGERS = 2
START_GRACE = timedelta(hours=2)
MARKER = re.compile(r"<!-- gap-build-retry:v1 (\{[^\n]+\}) -->")
PERSISTENT = re.compile(
    r"fips[-_ ]?check|no such file|missing (?:file|path)|cannot find (?:file|package)|"
    r"go\.mod requires go|requires go >=|go version mismatch|unsupported go version|"
    r"undefined:|compilation failed|syntax error|dockerfile.*not found", re.I)
TRANSIENT = re.compile(
    r"resource quota evaluation timed out|TLS handshake timeout|i/o timeout|"
    r"connection reset by peer|context deadline exceeded|ImagePullBackOff|"
    r"node.*(?:not ready|unreachable)|PodEvicted|Evicted|FailedScheduling|"
    r"no space left on device|temporary failure in name resolution", re.I)


class RetryObservationChanged(KonfluxQueryError):
    """The failed attempt is obsolete; never classify its replacement as failed."""


def retries_enabled():
    """Explicit opt-in rollout; legacy monitors do not post retry comments."""
    return os.environ.get("GAP_AUTO_RETRY_BUILDS", "false").lower() == "true"


def classify_failure(observation, details="", *, push_healthy=False, task_evidence=False):
    """A healthy push alone does not prove a failing PR has no code defect."""
    text = "\n".join((observation.get("reason", ""), observation.get("message", ""), details))
    if observation.get("status") not in {"failure", "timed_out", "cancelled"}:
        return False, "Build is not a completed failure"
    if PERSISTENT.search(text):
        return False, "Known persistent code/configuration or FIPS failure"
    if observation.get("status") == "timed_out":
        return True, "Completed PipelineRun timed out"
    if observation.get("status") == "cancelled":
        return True, "Latest current-head PipelineRun was cancelled"
    if task_evidence and push_healthy:
        if TRANSIENT.search(text):
            return True, "Transient infrastructure failure with three healthy same-release push builds"
        # Some Konflux failures expose only a generic PipelineRun/TaskRun
        # message even though the exact failed task is verified. Healthy
        # same-release push builds provide the missing cross-check; retry once
        # rather than leaving a safe transient failure permanently blocked.
        return True, "Completed PR failure with verified task evidence and three healthy same-release push builds"
    return False, "No verified transient failure with healthy same-release push builds; needs investigation"


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
        """Configure a serialized monitor writer and recover its saved receipts."""
        self.runner, self.dry_run = runner, dry_run
        self.client_factory = client_factory or KonfluxClient.from_environment
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.actor = os.environ.get("GAP_BUILD_RETRY_ACTOR", "rhods-devops-app[bot]")
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
        history = list(self.saved.get(pr.url, []))
        for comment in comments:
            user = comment.get("user") or {}
            body = str(comment.get("body") or "")
            if user.get("login") != self.actor:
                # Ignore human quotes/spoofed markers. A bot-issued retry with
                # a different identity may be a changed deployment actor; stop
                # rather than silently resetting its cross-leader budget.
                if user.get("type") != "Bot" or not body.startswith("/retest "):
                    continue
            for match in MARKER.finditer(body):
                record = json.loads(match[1])
                if record.get("repository") == f"{pr.owner}/{pr.repo}" and record.get("pr") == pr.number:
                    if (comment.get("user") or {}).get("login") != self.actor:
                        raise ValueError("Retry receipt author differs from GAP_BUILD_RETRY_ACTOR; reconcile before retrying")
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

    def _retry(self, pr, observation, history, client):
        """Plan or send one targeted retry after identity and budget checks."""
        now = self.clock()
        records = [r for r in history if r["component"] == observation["component"] and r["sha"] == pr.head_sha]
        count = len(records)
        info = {"retrigger_count": count, "status": "not-retried"}
        if observation["status"] not in {"failure", "timed_out", "cancelled"}:
            return info, False
        if records:
            last = max(records, key=lambda r: _moment(r["requested_at"]))
            if (observation["uid"] == last["run_uid"]
                    or _moment(observation["created_at"]) < _moment(last["requested_at"])):
                waiting = now - _moment(last["requested_at"]) < START_GRACE
                info.update(status="build-retrigger-pending" if waiting else "retry-not-started",
                            reason="Waiting for a new Konflux attempt" if waiting else
                                   "Retry did not produce a newer attempt within two hours; manual investigation required",
                            requested_at=last["requested_at"], comment_url=last.get("comment_url"))
                return info, waiting
        if count >= MAX_RETRIGGERS:
            info.update(status="retry-exhausted", reason="Two automatic retries already used for this component and SHA")
            return info, False
        pipeline = observation.get("pipeline_name", "")
        if not pipeline or not NAME.fullmatch(pipeline) or not observation.get("uid") or not observation.get("completed_at"):
            info["reason"] = "Missing verified PaC template or completed attempt identity"
            return info, False
        # Known persistent task errors take precedence even when the parent
        # eventually times out or is cancelled during cleanup.
        details, known_tasks = failure_details(client, observation)
        if PERSISTENT.search(details):
            info["reason"] = "Known persistent code/configuration or FIPS failure"
            return info, False
        if observation["status"] == "failure":
            branch = component_push_branch(client, repo=f"{pr.owner}/{pr.repo}", component=observation["component"])
            health = recent_push_health(client, repo=f"{pr.owner}/{pr.repo}",
                                        component=observation["component"], branch=branch, now=now)
            info["push_health"] = health
        eligible, reason = classify_failure(observation, details,
                                            push_healthy=info.get("push_health", {}).get("healthy", False),
                                            task_evidence=known_tasks)
        info["reason"] = reason
        if not eligible:
            return info, False
        # Close the evaluation/action gap: never re-run an older failure when a
        # newer attempt is already queued, running, or successful.
        fresh = client.observe(repo=f"{pr.owner}/{pr.repo}", number=pr.number, sha=pr.head_sha,
                               components={observation["component"]}, pipeline_names={pipeline}, refresh=True)
        if fresh.uid != observation["uid"] or fresh.status != observation["status"]:
            raise RetryObservationChanged("Build attempt changed before retry; awaiting monitor refresh")
        current = github_api_json(self.runner, f"repos/{pr.owner}/{pr.repo}/pulls/{pr.number}")
        if current.get("state") != "open" or (current.get("head") or {}).get("sha") != pr.head_sha:
            raise RetryObservationChanged("PR closed or changed head before retry; awaiting monitor refresh")
        record = {"repository": f"{pr.owner}/{pr.repo}", "pr": pr.number,
                  "component": observation["component"], "sha": pr.head_sha,
                  "pipeline": pipeline, "run_uid": observation["uid"],
                  "retrigger_count": count + 1, "requested_at": now.isoformat(), "reason": reason}
        if self.dry_run:
            info.update(status="would-retrigger", reason=reason)
            return info, True
        body = (f"/retest {pipeline}\n\nGAP automatic retry {count + 1}/{MAX_RETRIGGERS} "
                f"for `{observation['component']}` at `{pr.head_sha}`. {reason}.\n\n"
                f"<!-- gap-build-retry:v1 {json.dumps(record, sort_keys=True)} -->")
        # One external write for both command and audit receipt. Do not retry
        # this POST on errors: the next poll reads comments before acting again.
        try:
            response = json.loads(self.runner(["api", f"repos/{pr.owner}/{pr.repo}/issues/{pr.number}/comments",
                                               "--method", "POST", "-f", "body=" + body]))
            if (response.get("user") or {}).get("login") != self.actor:
                raise RuntimeError("Retry comment author differs from GAP_BUILD_RETRY_ACTOR; stop automatic retries")
            record.update(comment_url=response["html_url"], comment_id=response["id"],
                          requested_at=response["created_at"])
        except (RuntimeError, ValueError, TypeError, KeyError) as exc:
            # A POST may have reached GitHub even if its response was lost or
            # malformed. Stop publication; the next poll reconciles receipts.
            raise RuntimeError("Could not confirm retry delivery; reconcile child comments on the next poll") from exc
        history.append(record)
        self.saved[pr.url] = deepcopy(history)
        info.update(status="build-retrigger-pending", retrigger_count=count + 1,
                    requested_at=record["requested_at"], comment_url=record["comment_url"])
        return info, True

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

    def _retry_missing(self, pr, observation, history, pipeline, *, cel_evaluated):
        """Retrigger an expected component whose PR check never appeared."""
        now = self.clock()
        records = [
            r for r in history
            if r["component"] == observation["component"]
            and r["sha"] == pr.head_sha
            and r["pipeline"] == pipeline
        ]
        count = len(records)
        info = {"retrigger_count": count, "status": "not-retried"}
        if not cel_evaluated:
            info["reason"] = "Missing build trigger cannot be retried without authoritative CEL evidence"
            return info, True
        if records:
            last = max(records, key=lambda r: _moment(r["requested_at"]))
            waiting = now - _moment(last["requested_at"]) < START_GRACE
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
                    reason="Two automatic retries already used for this component and SHA",
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
        # A check may exist while the Konflux archive is still catching up.
        # Do not post a second /retest in that race.
        if self._pipeline_check_exists(pr, pipeline):
            info.update(status="build-trigger-pending",
                        reason="Current GitHub check exists; waiting for Konflux PipelineRun")
            return info, True
        current = github_api_json(self.runner, f"repos/{pr.owner}/{pr.repo}/pulls/{pr.number}")
        if current.get("state") != "open" or (current.get("head") or {}).get("sha") != pr.head_sha:
            raise RetryObservationChanged("PR closed or changed head before retry; awaiting monitor refresh")
        reason = "Expected PR build was not triggered; CEL expected this component"
        run_uid = f"missing:{pipeline}:{pr.head_sha}:{count + 1}"
        record = {
            "repository": f"{pr.owner}/{pr.repo}", "pr": pr.number,
            "component": observation["component"], "sha": pr.head_sha,
            "pipeline": pipeline, "run_uid": run_uid,
            "retrigger_count": count + 1, "requested_at": now.isoformat(), "reason": reason,
        }
        if self.dry_run:
            info.update(status="would-retrigger", reason=reason)
            return info, True
        body = (
            f"/retest {pipeline}\n\nGAP automatic retry {count + 1}/{MAX_RETRIGGERS} "
            f"for `{observation['component']}` at `{pr.head_sha}`. {reason}.\n\n"
            f"<!-- gap-build-retry:v1 {json.dumps(record, sort_keys=True)} -->"
        )
        try:
            response = json.loads(self.runner([
                "api", f"repos/{pr.owner}/{pr.repo}/issues/{pr.number}/comments",
                "--method", "POST", "-f", "body=" + body,
            ]))
            if (response.get("user") or {}).get("login") != self.actor:
                raise RuntimeError("Retry comment author differs from GAP_BUILD_RETRY_ACTOR; stop automatic retries")
            record.update(comment_url=response["html_url"], comment_id=response["id"],
                          requested_at=response["created_at"])
        except (RuntimeError, ValueError, TypeError, KeyError) as exc:
            raise RuntimeError("Could not confirm retry delivery; reconcile child comments on the next poll") from exc
        history.append(record)
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
        components = [r["component"] for r in evidence["runs"]]
        expected_components = set(evidence.get("expected-components") or [])
        cel_evaluated = evidence.get("cel-evaluated") is True
        required_pipelines = {
            item.get("component"): item.get("pipeline_name")
            for item in (evidence.get("required-pipelines") or {}).get("pipelines", [])
            if isinstance(item, dict)
        }
        for i, observation in enumerate(evidence["runs"]):
            if observation["status"] == "missing":
                pipeline = observation.get("pipeline_name") or required_pipelines.get(observation["component"], "")
                if observation["component"] in expected_components and components.count(observation["component"]) == 1:
                    try:
                        info, waiting = self._retry_missing(
                            pr, observation, history, pipeline, cel_evaluated=cel_evaluated
                        )
                    except (RetryObservationChanged, KonfluxQueryError, CheckQueryError, ValueError, TypeError, KeyError) as exc:
                        info, waiting = {"status": "retry-evidence-unavailable", "reason": str(exc)}, True
                    observation["retry"] = info
                    awaiting |= waiting
                    findings[i] = replace(
                        findings[i], outcome=OUTCOME_PENDING if waiting else OUTCOME_FAIL,
                        reason=f"{findings[i].reason}; {info.get('reason', '')}",
                    )
                else:
                    observation["retry"] = {
                        "status": "not-retried",
                        "reason": "Missing build trigger is not retryable without matching expected component and CEL evidence",
                    }
                    awaiting = True
                    findings[i] = replace(
                        findings[i], outcome=OUTCOME_PENDING,
                        reason=f"{findings[i].reason}; missing build trigger requires authoritative evidence",
                    )
                continue
            if observation["status"] not in {"failure", "timed_out", "cancelled"}:
                records = [r for r in history if r["component"] == observation["component"] and r["sha"] == pr.head_sha]
                if records and observation["status"] != "success":
                    awaiting = True
                    observation["retry"] = {"status": "build-retrigger-pending", "retrigger_count": len(records),
                                            "reason": "Retry attempt is pending or its result is not yet known"}
                continue
            if components.count(observation["component"]) != 1:
                observation["retry"] = {"status": "not-retried", "reason": "Multiple PR pipelines share this component"}
                continue
            try:
                # Each component has its own lookup deadline. One slow archive
                # query must not exhaust every subsequent component's budget.
                client = self.client_factory()
                info, waiting = self._retry(pr, observation, history, client)
            except RetryObservationChanged as exc:
                info, waiting = {"status": "retry-observation-changed", "reason": str(exc)}, True
            except (KonfluxQueryError, ValueError, TypeError, KeyError) as exc:
                # Retry eligibility is optional; a broken optional read must
                # not hold a completed failure open forever. This timeout never
                # converts a known newer attempt/head into an old failure.
                try:
                    age = self.clock() - _moment(observation.get("completed_at"))
                    waiting = timedelta(0) <= age < START_GRACE
                except ValueError:
                    waiting = False
                reason = str(exc) + ("" if waiting else "; retry evaluation window expired; manual investigation required")
                info = {"status": "retry-evidence-unavailable", "reason": reason}
            observation["retry"] = info
            awaiting |= waiting
            # A cancelled attempt is terminal when its retry budget is spent.
            findings[i] = replace(findings[i], outcome=OUTCOME_PENDING if waiting else OUTCOME_FAIL,
                                  reason=f"{findings[i].reason}; {info.get('reason', '')}")
        evidence["retry-history"] = deepcopy(history)
        # Hold the whole repository while a retry is in flight, even if another
        # component has a persistent failure. It must not be archived mid-retry.
        outcome = OUTCOME_PENDING if awaiting or any(
            f.outcome == OUTCOME_PENDING for f in findings
        ) else (
            OUTCOME_FAIL if any(f.outcome == OUTCOME_FAIL for f in findings) else gate.outcome)
        return replace(gate, outcome=outcome, findings=tuple(findings)), evidence
