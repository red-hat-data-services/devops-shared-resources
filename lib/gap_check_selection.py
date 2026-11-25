"""Select the GitHub checks a GAP repository PR must pass.

Build checks come from the repository's PipelineRun configuration. Feasibility
checks come from the main-to-release feasibility workflow. Both selectors
return ``ExpectedCheck`` values; they do not interpret check conclusions.
"""

from __future__ import annotations

import base64
import logging
import os
import re
import time
from dataclasses import dataclass, replace
from typing import Any, Sequence
from urllib.parse import urlparse

import yaml

from lib.pac_cel import TriggerParseError, evaluate_expressions

from lib.github_check_runs import (
    CheckQueryError,
    ExpectedCheck,
    GhRunner,
    collect_github_pages,
    github_api_json,
)
from lib.github_cli import GhCommandError
from lib.github_diff import fetch_comparison_files
from lib.gap_component_map import component_is_allowed

PAC_PREFIX = "pipelinesascode.tekton.dev/"
COMPONENT_LABEL = "appstudio.openshift.io/component"
PR_NUMBER_TEMPLATE = "{{pull_request_number}}"
TARGET_BRANCH_TEMPLATE = "{{target_branch}}"

DEFAULT_BUILD_APP_SLUG = "konflux-internal-p02"
ENV_BUILD_APP_SLUG = "GAP_BUILD_CHECK_APP_SLUG"
ENV_BUILD_APP_ID = "GAP_BUILD_CHECK_APP_ID"

KONFLUX_CHECK_NAME_PREFIX = "konflux internal"
KONFLUX_P02_CHECK_PREFIX = "konflux internal p02"

FEASIBILITY_WORKFLOW_NAME = "Main-to-release feasibility"
FEASIBILITY_WORKFLOW_FILE = "main-release-feasibility.yml"
FEASIBILITY_SETUP_JOB = "Select release sync inputs"
FEASIBILITY_APP_SLUG = "github-actions"

logger = logging.getLogger(__name__)

# Retry parameters for transient GitHub Actions API errors, matching the
# GH_READ_RETRY_ATTEMPTS / GH_READ_RETRY_DELAYS pattern in github_cli.py
# and _RETRY_MAX_ATTEMPTS / _RETRY_DELAYS in konflux_builds.py.
_FEASIBILITY_RETRY_MAX = 3
_FEASIBILITY_RETRY_DELAYS = (1.0, 3.0)
_TRANSIENT_MARKERS = (
    "403", "429", "500", "502", "503", "504",
    "timeout", "rate limit", "temporarily unavailable",
    "try again later", "connection reset",
)


def _is_transient_error(exc: Exception) -> bool:
    """Return whether *exc* looks like a transient GitHub API failure."""
    text = str(exc).lower()
    return any(marker in text for marker in _TRANSIENT_MARKERS)


@dataclass(frozen=True)
class PullRequestContext:
    """Facts used to decide which PipelineRuns a pull request will run."""

    number: int
    base_ref: str
    head_sha: str
    labels: frozenset[str]
    changed_files: tuple[str, ...]
    source_branch: str | None = None
    base_sha: str | None = None
    title: str | None = None
    source_url: str | None = None
    target_url: str | None = None
    file_collections: dict[str, tuple[str, ...]] | None = None
    konflux_cluster: str | None = None

    def cel_variables(self) -> dict[str, Any]:
        # This is a PR-state snapshot, not a reconstructed webhook. In
        # particular do not synthesize body, headers, or an event_type.
        values: dict[str, Any] = {
            "event": "pull_request",
            "files": {"all": list(self.changed_files)},
        }
        for name, value in (
            ("target_branch", self.base_ref), ("source_branch", self.source_branch),
            ("event_title", self.title), ("source_url", self.source_url),
            ("target_url", self.target_url),
        ):
            if value is not None:
                values[name] = value
        if self.file_collections is not None:
            values["files"].update({k: list(v) for k, v in self.file_collections.items()})
        cluster = self.konflux_cluster or configured_konflux_cluster()
        if cluster:
            values["konflux_cluster"] = cluster
        return values


def configured_konflux_cluster() -> str | None:
    """Return the configured Konflux cluster name for PaC CEL expressions.

    ``KONFLUX_API_URL`` is ordinary endpoint configuration, not a secret. The
    API host convention is ``api.<cluster>.<domain>``; callers can override
    the derived value with ``KONFLUX_CLUSTER`` when a deployment uses another
    host layout.
    """
    explicit = os.environ.get("KONFLUX_CLUSTER", "").strip()
    if explicit:
        return explicit
    raw_url = os.environ.get("KONFLUX_API_URL", "").strip()
    if not raw_url:
        return None
    host = urlparse(raw_url).hostname or ""
    match = re.match(r"^api\.([^.]+)(?:\.|$)", host)
    return match.group(1) if match else None


def cluster_from_check_name(name: str) -> str | None:
    """Determine the Konflux cluster ID from a GitHub check run name prefix.

    Check run names from Konflux Internal apps start with the app name:
    ``Konflux Internal p02 / …`` → ``"p02"`` and
    ``Konflux Internal / …`` (without ``p02``) → ``"p01"``.
    """
    lower = name.lower()
    if lower.startswith(KONFLUX_P02_CHECK_PREFIX):
        return "p02"
    if lower.startswith(KONFLUX_CHECK_NAME_PREFIX):
        return "p01"
    return None


def is_konflux_internal_check(name: str) -> bool:
    """Return True if the check run name indicates a Konflux Internal app."""
    return name.lower().startswith(KONFLUX_CHECK_NAME_PREFIX)


def multi_cluster_enabled() -> bool:
    """Return True when multi-cluster build detection is active.

    Multi-cluster mode is the default.  Setting ``GAP_BUILD_CHECK_APP_SLUG``
    forces single-app mode for backward compatibility.
    """
    return not os.environ.get(ENV_BUILD_APP_SLUG, "").strip()


@dataclass(frozen=True)
class WorkflowJob:
    name: str
    status: str
    conclusion: str | None
    html_url: str | None
    started_at: str | None


@dataclass(frozen=True)
class FeasibilityRun:
    """Latest main-to-release feasibility workflow run for one commit."""

    found: bool
    status: str | None
    conclusion: str | None
    head_sha: str | None
    jobs: tuple[WorkflowJob, ...]
    query_error: str | None = None
    run_id: int | None = None
    attempt: int | None = None
    url: str | None = None
    source_head_sha: str | None = None


def default_build_producer() -> tuple[str, int | None]:
    """Return the Konflux GitHub App slug and optional app id to match.

    The example producer is ``konflux-internal-p02``. Set
    ``GAP_BUILD_CHECK_APP_SLUG`` (and optionally ``GAP_BUILD_CHECK_APP_ID``)
    when the pipeline environment uses a different app.
    """
    slug = os.environ.get(ENV_BUILD_APP_SLUG, DEFAULT_BUILD_APP_SLUG).strip()
    slug = slug or DEFAULT_BUILD_APP_SLUG
    raw_id = os.environ.get(ENV_BUILD_APP_ID, "").strip()
    if not raw_id:
        return slug, None
    try:
        return slug, int(raw_id)
    except ValueError as exc:
        raise TriggerParseError(
            f"{ENV_BUILD_APP_ID} must be an integer, got {raw_id!r}"
        ) from exc


def resolved_pipeline_name(raw_name: str, pr_number: int) -> str:
    """Expand the PipelineRun name the way Pipelines-as-Code presents it.

    ``{{pull_request_number}}`` becomes the pull request number. A stable name
    such as ``odh-eval-hub-v3-6-on-pull-request`` is left unchanged. The
    component label is not substituted for the PipelineRun name.
    """
    return raw_name.replace(PR_NUMBER_TEMPLATE, str(pr_number)).strip()


def load_pipelineruns(documents: Sequence[str]) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    for index, document in enumerate(documents):
        try:
            loaded = list(yaml.safe_load_all(document))
        except yaml.YAMLError as exc:
            raise TriggerParseError(f"invalid PipelineRun YAML in document {index}: {exc}") from exc
        for item in loaded:
            if isinstance(item, dict) and item.get("kind") == "PipelineRun":
                runs.append(item)
    return runs


def select_build_checks(
    documents: Sequence[str],
    context: PullRequestContext,
    *,
    app_slug: str | None,
    app_id: int | None = None,
    allowed_components: frozenset[str] | None = None,
) -> list[ExpectedCheck]:
    """Return one expected check per PipelineRun this pull request will run.

    A repository with several components produces several checks. Pipelines
    that the trigger configuration will not run or the repository allowlist
    excludes are omitted. A missing GitHub check is never a no-build signal.
    None preserves unfiltered selection for callers without an allowlist;
    the GAP monitor always supplies a repository-scoped set. Every selected
    PipelineRun must declare a nonempty component label; names are not inferred.

    When ``app_slug`` is ``None``, multi-cluster mode is used: CEL expressions
    are evaluated with both P01 and P02 cluster values and the results are
    unioned.  The returned checks use ``check_name_prefix`` instead of
    ``app_slug`` to match any Konflux Internal app.
    """
    if app_slug is None:
        return _select_build_checks_multi_cluster(
            documents, context, allowed_components=allowed_components,
        )
    checks: list[ExpectedCheck] = []
    pipelines = load_pipelineruns(documents)
    expressions: list[str] = []
    for pipeline in pipelines:
        metadata = pipeline.get("metadata")
        annotations = metadata.get("annotations") if isinstance(metadata, dict) else None
        expression = annotations.get(f"{PAC_PREFIX}on-cel-expression") if isinstance(annotations, dict) else None
        if isinstance(expression, str) and expression.strip():
            expressions.append(expression)
    expressions = list(dict.fromkeys(expressions))
    cel_results = dict(zip(
        expressions, evaluate_expressions(expressions, context.cel_variables()),
    ))
    for pipeline in pipelines:
        if not pipeline_is_expected(pipeline, context, cel_results=cel_results):
            continue
        metadata = pipeline.get("metadata") if isinstance(pipeline.get("metadata"), dict) else {}
        raw_name = str(metadata.get("generateName") or metadata.get("name") or "").strip()
        if not raw_name:
            raise TriggerParseError("PipelineRun is missing metadata.name")
        # pipelineRef.revision may be a CEL expression that falls back to main.
        # That value only chooses the shared pipeline file. The GitHub check
        # name still comes from metadata.name.
        pipeline_name = resolved_pipeline_name(raw_name, context.number)
        labels = metadata.get("labels") if isinstance(metadata.get("labels"), dict) else {}
        component = labels.get(COMPONENT_LABEL)
        if not isinstance(component, str) or not component.strip():
            raise TriggerParseError(
                f"PipelineRun {pipeline_name} is missing a nonempty {COMPONENT_LABEL} label"
            )
        component = component.strip()
        if allowed_components is not None and not component_is_allowed(component, allowed_components):
            logging.getLogger(__name__).warning(
                "Ignoring Konflux build %s (component %s): not mapped for this repository",
                pipeline_name, component,
            )
            continue
        checks.append(
            ExpectedCheck(
                key=component,
                name_suffix=pipeline_name,
                app_slug=app_slug,
                app_id=app_id,
            )
        )
    return checks


def _select_build_checks_multi_cluster(
    documents: Sequence[str],
    context: PullRequestContext,
    *,
    allowed_components: frozenset[str] | None = None,
) -> list[ExpectedCheck]:
    """Evaluate CEL with both cluster values and union the results.

    Each cluster produces its own CEL evaluation.  A PipelineRun that
    matches *either* cluster is included.  Returned checks use
    ``check_name_prefix`` so they match any Konflux Internal app on
    GitHub, regardless of which cluster runs the build.

    When P01 and P02 select different PipelineRun names for the same
    component, both names are retained as alternatives.  A check run
    matching *any* retained name satisfies the component.  When both
    clusters select the same template, only one entry is kept.
    """
    cluster_cel_names = [
        ("stone-prod-p01",),
        ("stone-prod-p02",),
    ]

    # Collect all distinct pipeline names per component.
    component_suffixes: dict[str, list[str]] = {}
    for (cluster_name,) in cluster_cel_names:
        cluster_context = replace(context, konflux_cluster=cluster_name)
        checks = select_build_checks(
            documents,
            cluster_context,
            app_slug=DEFAULT_BUILD_APP_SLUG,  # placeholder, replaced below
            allowed_components=allowed_components,
        )
        for check in checks:
            if check.key not in component_suffixes:
                component_suffixes[check.key] = []
            if check.name_suffix and check.name_suffix not in component_suffixes[check.key]:
                component_suffixes[check.key].append(check.name_suffix)

    result: list[ExpectedCheck] = []
    for component, suffixes in component_suffixes.items():
        if len(suffixes) == 1:
            result.append(ExpectedCheck(
                key=component,
                name_suffix=suffixes[0],
                app_slug=None,
                app_id=None,
                check_name_prefix=KONFLUX_CHECK_NAME_PREFIX,
            ))
        else:
            # Different pipeline names across clusters: retain all as
            # alternatives.  One matching check satisfies the component.
            result.append(ExpectedCheck(
                key=component,
                name_suffixes=tuple(suffixes),
                app_slug=None,
                app_id=None,
                check_name_prefix=KONFLUX_CHECK_NAME_PREFIX,
            ))
    return result


def pipeline_is_expected(
    pipeline: dict[str, Any], context: PullRequestContext, *,
    cel_results: dict[str, bool | TriggerParseError] | None = None,
) -> bool:
    """Return whether Pipelines-as-Code will run this PipelineRun.

    ``on-cel-expression`` takes precedence over ``on-event``, ``on-label``, and
    ``on-target-branch``. An expression this selector cannot parse is treated
    as a match so an unrecognized trigger cannot become a no-build pass.
    """
    try:
        return _pipeline_is_expected(pipeline, context, cel_results=cel_results)
    except TriggerParseError as exc:
        logger.warning("Cannot evaluate PipelineRun trigger; retaining expected check: %s", exc)
        return True


def select_feasibility_checks(
    run: FeasibilityRun,
    *,
    app_slug: str = FEASIBILITY_APP_SLUG,
) -> list[ExpectedCheck]:
    """Require the setup job and every job in a discovered feasibility run.

    One successful release-matrix job is not enough: each job in that run
    becomes its own expected check. An undiscovered run, or a run whose job
    inventory is still empty, does not collapse to the setup job. Callers
    treat that empty selection as unresolved.
    """
    if run.query_error or not run.found or not run.jobs:
        return []
    setup = _feasibility_expected(FEASIBILITY_SETUP_JOB, app_slug)
    checks = [_feasibility_expected(job.name, app_slug) for job in run.jobs]
    if not any(item.key == FEASIBILITY_SETUP_JOB for item in checks):
        checks.insert(0, setup)
    return checks


def _feasibility_expected(job_name: str, app_slug: str) -> ExpectedCheck:
    """Match a feasibility job whether or not GitHub prefixes the workflow name.

    Check runs are recorded as the job name, for example
    ``Select release sync inputs``. A name of the form
    ``Main-to-release feasibility / <job>`` is the same job.
    """
    return ExpectedCheck(key=job_name, name_suffix=job_name, app_slug=app_slug)


def fetch_tekton_documents(owner: str, repo: str, sha: str, runner: GhRunner) -> list[str]:
    """Read ``.tekton`` YAML from ``sha``. A missing directory means no builds."""
    listing_endpoint = f"repos/{owner}/{repo}/contents/.tekton?ref={sha}"
    try:
        listing = github_api_json(runner, listing_endpoint)
    except GhCommandError as exc:
        if _is_not_found(exc):
            return []
        raise CheckQueryError(f"failed to list .tekton for {owner}/{repo}@{sha}: {exc}") from exc
    except CheckQueryError as exc:
        raise CheckQueryError(f"failed to list .tekton for {owner}/{repo}@{sha}: {exc}") from exc

    if isinstance(listing, dict):
        entries = [listing]
    elif isinstance(listing, list):
        entries = listing
    else:
        raise CheckQueryError(f"unexpected .tekton listing for {owner}/{repo}@{sha}")

    documents: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "")
        if entry.get("type") not in (None, "file"):
            continue
        if not name.endswith((".yaml", ".yml")):
            continue
        documents.append(_read_contents_file(owner, repo, sha, f".tekton/{name}", runner))
    return documents


def fetch_changed_files(owner: str, repo: str, number: int, runner: GhRunner) -> tuple[str, ...]:
    return fetch_file_collections(owner, repo, number, runner)["all"]


def fetch_file_collections(
    owner: str, repo: str, number: int, runner: GhRunner,
    *, base_sha: str | None = None, head_sha: str | None = None,
) -> dict[str, tuple[str, ...]]:
    """Collect file statuses; pinned snapshots never use the live PR endpoint."""
    if base_sha is not None or head_sha is not None:
        return fetch_comparison_files(owner, repo, base_sha, head_sha, runner)
    endpoint = f"repos/{owner}/{repo}/pulls/{number}/files"
    try:
        payload = collect_github_pages(runner, endpoint, None, 100)
    except (GhCommandError, CheckQueryError) as exc:
        raise CheckQueryError(
            f"failed to list files for {owner}/{repo}#{number}: {exc}"
        ) from exc
    collections: dict[str, list[str]] = {
        name: [] for name in ("all", "added", "deleted", "modified", "renamed")
    }
    statuses = {"added": "added", "removed": "deleted", "modified": "modified", "renamed": "renamed"}
    complete_statuses = True
    for item in payload:
        if not isinstance(item, dict) or not item.get("filename"):
            raise CheckQueryError(f"pull file listing for {owner}/{repo}#{number} is incomplete")
        name = str(item["filename"])
        collections["all"].append(name)
        status = item.get("status")
        if status in statuses:
            collections[statuses[status]].append(name)
        elif not isinstance(status, str):
            complete_statuses = False
    # GitHub caps this endpoint at 3000 files. Never trust a possibly
    # truncated response as evidence that a path-scoped pipeline can be skipped.
    if len(collections["all"]) >= 3000:
        raise CheckQueryError("PR file listing may be truncated at GitHub's 3000-file limit")
    if not complete_statuses:
        return {"all": tuple(collections["all"])}
    return {key: tuple(names) for key, names in collections.items()}


def _workflow_execution_key(run: dict[str, Any]) -> tuple[str, int]:
    """Return when this workflow run's latest attempt started.

    ``run_started_at`` is that attempt's start time and moves forward when the
    run is rerun. ``run_attempt`` only counts retries of one run id, so it does
    not order distinct runs. The run id breaks timestamp ties.
    """
    started = (
        _optional_str(run.get("run_started_at"))
        or _optional_str(run.get("updated_at"))
        or _optional_str(run.get("created_at"))
        or ""
    )
    return (started, int(run.get("id") or 0))


def fetch_feasibility_run(owner: str, repo: str, sha: str, runner: GhRunner) -> FeasibilityRun:
    """Load the latest feasibility execution for this commit, if any.

    Transient API errors (403, 429, 5xx, timeouts) are retried up to
    ``_FEASIBILITY_RETRY_MAX`` times with backoff delays matching the
    ``GH_READ_RETRY_DELAYS`` pattern in :mod:`lib.github_cli`.
    """
    endpoint = f"repos/{owner}/{repo}/actions/runs?head_sha={sha}"

    # --- runs query with retry ---
    runs: list[Any] | None = None
    last_runs_error: Exception | None = None
    for attempt in range(1, _FEASIBILITY_RETRY_MAX + 1):
        try:
            runs = collect_github_pages(runner, endpoint, "workflow_runs", 100)
            last_runs_error = None
            break
        except (GhCommandError, CheckQueryError) as exc:
            last_runs_error = exc
            if attempt < _FEASIBILITY_RETRY_MAX and _is_transient_error(exc):
                delay = _FEASIBILITY_RETRY_DELAYS[min(attempt - 1, len(_FEASIBILITY_RETRY_DELAYS) - 1)]
                logger.warning(
                    "Feasibility runs query attempt %d/%d failed (%s), retrying in %.1fs",
                    attempt, _FEASIBILITY_RETRY_MAX, exc, delay,
                )
                time.sleep(delay)
                continue
            break

    if runs is None:
        return FeasibilityRun(
            found=False,
            status=None,
            conclusion=None,
            head_sha=sha,
            jobs=(),
            query_error=f"failed to list feasibility workflow runs: {last_runs_error}",
        )

    matching = []
    for run in runs:
        if not isinstance(run, dict):
            continue
        head = str(run.get("head_sha") or "")
        if head and head != sha:
            continue
        name = str(run.get("name") or "")
        path = str(run.get("path") or "")
        if name == FEASIBILITY_WORKFLOW_NAME or path.endswith(FEASIBILITY_WORKFLOW_FILE):
            matching.append(run)
    if not matching:
        return FeasibilityRun(
            found=False,
            status=None,
            conclusion=None,
            head_sha=sha,
            jobs=(),
        )

    latest = max(matching, key=_workflow_execution_key)
    run_id = latest.get("id")
    if not run_id:
        return FeasibilityRun(
            found=False,
            status=None,
            conclusion=None,
            head_sha=sha,
            jobs=(),
            query_error="feasibility workflow run is missing an id",
        )

    # --- jobs query with retry ---
    jobs_endpoint = f"repos/{owner}/{repo}/actions/runs/{run_id}/jobs"
    raw_jobs: list[Any] | None = None
    last_jobs_error: Exception | None = None
    for attempt in range(1, _FEASIBILITY_RETRY_MAX + 1):
        try:
            raw_jobs = collect_github_pages(runner, jobs_endpoint, "jobs", 100)
            last_jobs_error = None
            break
        except (GhCommandError, CheckQueryError) as exc:
            last_jobs_error = exc
            if attempt < _FEASIBILITY_RETRY_MAX and _is_transient_error(exc):
                delay = _FEASIBILITY_RETRY_DELAYS[min(attempt - 1, len(_FEASIBILITY_RETRY_DELAYS) - 1)]
                logger.warning(
                    "Feasibility jobs query attempt %d/%d failed (%s), retrying in %.1fs",
                    attempt, _FEASIBILITY_RETRY_MAX, exc, delay,
                )
                time.sleep(delay)
                continue
            break

    run_conclusion = _optional_str(latest.get("conclusion"))

    if raw_jobs is None:
        # When the jobs query fails but the run-level conclusion is
        # authoritative (success), do not propagate the query error — the
        # run itself succeeded and the caller can evaluate at the run level.
        jobs_query_error: str | None
        fallback_jobs: tuple[WorkflowJob, ...] = ()
        if run_conclusion and run_conclusion.lower() == "success":
            logger.info(
                "Feasibility jobs query failed but run conclusion is 'success'; "
                "clearing query_error (run %s)",
                run_id,
            )
            jobs_query_error = None
            # Synthetic job signals that the run-level conclusion is
            # authoritative so _evaluate_feasibility does not treat the
            # empty inventory as undiscovered.
            fallback_jobs = (
                WorkflowJob(
                    name=FEASIBILITY_SETUP_JOB,
                    status="completed",
                    conclusion="success",
                    html_url=None,
                    started_at=None,
                ),
            )
        else:
            jobs_query_error = f"failed to list feasibility workflow jobs: {last_jobs_error}"
        return FeasibilityRun(
            found=True,
            status=_optional_str(latest.get("status")),
            conclusion=run_conclusion,
            head_sha=sha,
            jobs=fallback_jobs,
            query_error=jobs_query_error,
        )

    jobs: list[WorkflowJob] = []
    for job in raw_jobs:
        if not isinstance(job, dict):
            continue
        job_sha = str(job.get("head_sha") or "")
        if job_sha and job_sha != sha:
            continue
        jobs.append(
            WorkflowJob(
                name=str(job.get("name") or ""),
                status=str(job.get("status") or "").lower(),
                conclusion=_optional_str(job.get("conclusion")),
                html_url=_optional_str(job.get("html_url")),
                started_at=_optional_str(job.get("started_at")),
            )
        )
    return FeasibilityRun(
        found=True,
        status=_optional_str(latest.get("status")),
        conclusion=run_conclusion,
        head_sha=sha,
        jobs=tuple(jobs),
        run_id=run_id,
        attempt=latest.get("run_attempt"),
        url=_optional_str(latest.get("html_url")),
        source_head_sha=_optional_str(latest.get("head_sha")),
    )


def _pipeline_is_expected(
    pipeline: dict[str, Any], context: PullRequestContext, *,
    cel_results: dict[str, bool | TriggerParseError] | None = None,
) -> bool:
    metadata = pipeline.get("metadata") if isinstance(pipeline.get("metadata"), dict) else {}
    annotations = metadata.get("annotations") if isinstance(metadata.get("annotations"), dict) else {}
    cel = annotations.get(f"{PAC_PREFIX}on-cel-expression")
    if isinstance(cel, str) and cel.strip():
        if cel_results is None:
            return cel_matches(cel, context)
        result = cel_results[cel]
        if isinstance(result, TriggerParseError):
            raise result
        return result

    on_event = annotations.get(f"{PAC_PREFIX}on-event")
    on_target = annotations.get(f"{PAC_PREFIX}on-target-branch")
    if not isinstance(on_event, str) or not isinstance(on_target, str):
        return False
    if not _annotation_matches(on_event, ("pull_request",), branch=False):
        return False
    if not _annotation_matches(on_target, (context.base_ref,), branch=True):
        return False

    on_label = annotations.get(f"{PAC_PREFIX}on-label")
    if isinstance(on_label, str) and on_label.strip():
        required = set(_annotation_values(on_label))
        if not required.intersection(context.labels):
            return False

    on_path = annotations.get(f"{PAC_PREFIX}on-path-change")
    if isinstance(on_path, str) and on_path.strip():
        patterns = _annotation_values(on_path)
        if not any(_path_changed(pattern, context.changed_files) for pattern in patterns):
            return False

    on_ignore = annotations.get(f"{PAC_PREFIX}on-path-change-ignore")
    if isinstance(on_ignore, str) and on_ignore.strip() and context.changed_files:
        patterns = _annotation_values(on_ignore)
        if all(any(_glob_match(pattern, path) for pattern in patterns) for path in context.changed_files):
            return False
    return True


def cel_matches(expression: str, context: PullRequestContext) -> bool:
    result = evaluate_expressions([expression], context.cel_variables())[0]
    if isinstance(result, TriggerParseError):
        raise result
    return result


def _annotation_matches(raw: str, candidates: Sequence[str], *, branch: bool) -> bool:
    values = _annotation_values(raw)
    for value in values:
        for candidate in candidates:
            if branch and _branch_match(value, candidate):
                return True
            if not branch and value == candidate:
                return True
    return False


def _annotation_values(raw: str) -> list[str]:
    text = raw.strip()
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            raise TriggerParseError(f"empty annotation list: {raw}")
        return [part.strip().strip("'\"") for part in inner.split(",") if part.strip()]
    return [text.strip("'\"")]


def _branch_match(pattern: str, base_ref: str) -> bool:
    if pattern == TARGET_BRANCH_TEMPLATE:
        return True
    pattern = pattern.removeprefix("refs/heads/")
    base = base_ref.removeprefix("refs/heads/")
    if any(char in pattern for char in "*?["):
        return _glob_match(pattern, base)
    return pattern == base


def _path_changed(pattern: str, files: Sequence[str]) -> bool:
    return any(_glob_match(pattern, path) for path in files)


def _glob_match(pattern: str, path: str) -> bool:
    return re.fullmatch(_glob_to_regex(pattern), path) is not None


def _glob_to_regex(pattern: str) -> str:
    parts = ["^"]
    index = 0
    while index < len(pattern):
        if pattern.startswith("**/", index):
            parts.append("(?:.*/)?")
            index += 3
        elif pattern.startswith("**", index):
            parts.append(".*")
            index += 2
        elif pattern[index] == "*":
            parts.append("[^/]*")
            index += 1
        elif pattern[index] == "?":
            parts.append("[^/]")
            index += 1
        else:
            parts.append(re.escape(pattern[index]))
            index += 1
    parts.append("$")
    return "".join(parts)


def _read_contents_file(
    owner: str,
    repo: str,
    sha: str,
    path: str,
    runner: GhRunner,
) -> str:
    endpoint = f"repos/{owner}/{repo}/contents/{path}?ref={sha}"
    try:
        payload = github_api_json(runner, endpoint)
    except (GhCommandError, CheckQueryError) as exc:
        raise CheckQueryError(f"failed to read {path} at {sha}: {exc}") from exc
    if not isinstance(payload, dict):
        raise CheckQueryError(f"unexpected contents payload for {path}")
    content = payload.get("content")
    if payload.get("encoding") != "base64" or not isinstance(content, str):
        raise CheckQueryError(f"cannot decode {path} at {sha}")
    try:
        return base64.b64decode(content).decode("utf-8")
    except (ValueError, UnicodeError) as exc:
        raise CheckQueryError(f"cannot decode {path} at {sha}: {exc}") from exc


def _is_not_found(exc: GhCommandError) -> bool:
    text = exc.output.lower()
    return exc.returncode == 404 or "404" in text or "not found" in text


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
