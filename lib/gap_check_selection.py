"""Select the GitHub checks a GAP repository PR must pass.

Build checks come from the repository's PipelineRun configuration. Feasibility
checks come from the main-to-release feasibility workflow. Both selectors
return ``ExpectedCheck`` values; they do not interpret check conclusions.
"""

from __future__ import annotations

import base64
import os
import re
from dataclasses import dataclass
from typing import Any, Callable, Sequence

import yaml

from lib.github_check_runs import (
    CheckQueryError,
    ExpectedCheck,
    GhRunner,
    collect_github_pages,
    github_api_json,
)
from lib.github_cli import GhCommandError

PAC_PREFIX = "pipelinesascode.tekton.dev/"
COMPONENT_LABEL = "appstudio.openshift.io/component"
PR_NUMBER_TEMPLATE = "{{pull_request_number}}"
TARGET_BRANCH_TEMPLATE = "{{target_branch}}"

DEFAULT_BUILD_APP_SLUG = "konflux-internal-p02"
ENV_BUILD_APP_SLUG = "GAP_BUILD_CHECK_APP_SLUG"
ENV_BUILD_APP_ID = "GAP_BUILD_CHECK_APP_ID"

FEASIBILITY_WORKFLOW_NAME = "Main-to-release feasibility"
FEASIBILITY_WORKFLOW_FILE = "main-release-feasibility.yml"
FEASIBILITY_SETUP_JOB = "Select release sync inputs"
FEASIBILITY_APP_SLUG = "github-actions"

_TOKEN_RE = re.compile(
    r"""\s+|(?P<string>'(?:\\.|[^'])*'|"(?:\\.|[^"])*")"""
    r"""|(?P<op>==|&&|\|\|)|(?P<sym>[().,!])|(?P<ident>[A-Za-z_][A-Za-z0-9_]*)"""
    r"""|(?P<bad>\S)"""
)


class TriggerParseError(ValueError):
    """A PipelineRun trigger could not be evaluated."""


@dataclass(frozen=True)
class PullRequestContext:
    """Facts used to decide which PipelineRuns a pull request will run."""

    number: int
    base_ref: str
    head_sha: str
    labels: frozenset[str]
    changed_files: tuple[str, ...]


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
    app_slug: str,
    app_id: int | None = None,
) -> list[ExpectedCheck]:
    """Return one expected check per PipelineRun this pull request will run.

    A repository with several components produces several checks. Pipelines
    that the trigger configuration will not run are omitted. That omission is
    the only no-build signal; a missing GitHub check is not.
    """
    checks: list[ExpectedCheck] = []
    for pipeline in load_pipelineruns(documents):
        if not pipeline_is_expected(pipeline, context):
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
        component = str(labels.get(COMPONENT_LABEL) or pipeline_name).strip() or pipeline_name
        checks.append(
            ExpectedCheck(
                key=component,
                name_suffix=pipeline_name,
                app_slug=app_slug,
                app_id=app_id,
            )
        )
    return checks


def pipeline_is_expected(pipeline: dict[str, Any], context: PullRequestContext) -> bool:
    """Return whether Pipelines-as-Code will run this PipelineRun.

    ``on-cel-expression`` takes precedence over ``on-event``, ``on-label``, and
    ``on-target-branch``. An expression this selector cannot parse is treated
    as a match so an unrecognized trigger cannot become a no-build pass.
    """
    try:
        return _pipeline_is_expected(pipeline, context)
    except TriggerParseError:
        return True


def select_feasibility_checks(
    run: FeasibilityRun,
    *,
    app_slug: str = FEASIBILITY_APP_SLUG,
) -> list[ExpectedCheck]:
    """Require the setup job and every job in the latest feasibility run.

    One successful release-matrix job is not enough: each job returned for
    that run becomes its own expected check. When the run has not appeared
    yet, the setup job is still required.
    """
    setup = _feasibility_expected(FEASIBILITY_SETUP_JOB, app_slug)
    if run.query_error or not run.found or not run.jobs:
        return [setup]
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
    endpoint = f"repos/{owner}/{repo}/pulls/{number}/files"
    try:
        payload = collect_github_pages(runner, endpoint, None, 100)
    except (GhCommandError, CheckQueryError) as exc:
        raise CheckQueryError(
            f"failed to list files for {owner}/{repo}#{number}: {exc}"
        ) from exc
    names: list[str] = []
    for item in payload:
        if not isinstance(item, dict) or not item.get("filename"):
            raise CheckQueryError(f"pull file listing for {owner}/{repo}#{number} is incomplete")
        names.append(str(item["filename"]))
    return tuple(names)


def fetch_feasibility_run(owner: str, repo: str, sha: str, runner: GhRunner) -> FeasibilityRun:
    """Load the newest feasibility workflow run for this commit, if any."""
    endpoint = f"repos/{owner}/{repo}/actions/runs?head_sha={sha}"
    try:
        runs = collect_github_pages(runner, endpoint, "workflow_runs", 100)
    except (GhCommandError, CheckQueryError) as exc:
        return FeasibilityRun(
            found=False,
            status=None,
            conclusion=None,
            head_sha=sha,
            jobs=(),
            query_error=f"failed to list feasibility workflow runs: {exc}",
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

    latest = max(
        matching,
        key=lambda item: (int(item.get("run_attempt") or 0), int(item.get("id") or 0)),
    )
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
    jobs_endpoint = f"repos/{owner}/{repo}/actions/runs/{run_id}/jobs"
    try:
        raw_jobs = collect_github_pages(runner, jobs_endpoint, "jobs", 100)
    except (GhCommandError, CheckQueryError) as exc:
        return FeasibilityRun(
            found=True,
            status=_optional_str(latest.get("status")),
            conclusion=_optional_str(latest.get("conclusion")),
            head_sha=sha,
            jobs=(),
            query_error=f"failed to list feasibility workflow jobs: {exc}",
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
        conclusion=_optional_str(latest.get("conclusion")),
        head_sha=sha,
        jobs=tuple(jobs),
    )


def _pipeline_is_expected(pipeline: dict[str, Any], context: PullRequestContext) -> bool:
    metadata = pipeline.get("metadata") if isinstance(pipeline.get("metadata"), dict) else {}
    annotations = metadata.get("annotations") if isinstance(metadata.get("annotations"), dict) else {}
    cel = annotations.get(f"{PAC_PREFIX}on-cel-expression")
    if isinstance(cel, str) and cel.strip():
        return cel_matches(cel, context)

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
    tokens = _tokenize(expression)
    parser = _CelParser(tokens, context)
    value = parser.parse_or()
    if parser.peek() is not None:
        raise TriggerParseError(f"unexpected token in CEL expression: {parser.peek()}")
    return value


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


def _tokenize(expression: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    for match in _TOKEN_RE.finditer(expression):
        if match.group("bad"):
            raise TriggerParseError(f"unsupported CEL token {match.group('bad')!r}")
        if match.group("string") is not None:
            tokens.append(("string", match.group("string")))
        elif match.group("op") is not None:
            tokens.append(("op", match.group("op")))
        elif match.group("sym") is not None:
            tokens.append(("sym", match.group("sym")))
        elif match.group("ident") is not None:
            tokens.append(("ident", match.group("ident")))
    return tokens


class _CelParser:
    def __init__(self, tokens: list[tuple[str, str]], context: PullRequestContext) -> None:
        self.tokens = tokens
        self.context = context
        self.index = 0

    def peek(self) -> tuple[str, str] | None:
        if self.index >= len(self.tokens):
            return None
        return self.tokens[self.index]

    def pop(self) -> tuple[str, str]:
        token = self.peek()
        if token is None:
            raise TriggerParseError("unexpected end of CEL expression")
        self.index += 1
        return token

    def expect(self, kind: str, value: str) -> None:
        token = self.pop()
        if token != (kind, value):
            raise TriggerParseError(f"expected {value}, got {token[1]}")

    def parse_or(self) -> bool:
        value = self.parse_and()
        while self.peek() == ("op", "||"):
            self.pop()
            value = self.parse_and() or value
        return value

    def parse_and(self) -> bool:
        value = self.parse_unary()
        while self.peek() == ("op", "&&"):
            self.pop()
            value = self.parse_unary() and value
        return value

    def parse_unary(self) -> bool:
        if self.peek() == ("sym", "!"):
            self.pop()
            return not self.parse_unary()
        return self.parse_primary()

    def parse_primary(self) -> bool:
        if self.peek() == ("sym", "("):
            self.pop()
            value = self.parse_or()
            self.expect("sym", ")")
            return value
        token = self.peek()
        if token is None:
            raise TriggerParseError("unexpected end of CEL expression")
        if token[0] == "string":
            return self._parse_path_changed()
        if token == ("ident", "files"):
            return self._parse_files()
        if token[0] == "ident":
            return self._parse_comparison()
        raise TriggerParseError(f"unsupported CEL expression starting at {token[1]}")

    def _parse_path_changed(self) -> bool:
        pattern = _cel_string(self.pop()[1])
        self.expect("sym", ".")
        self.expect("ident", "pathChanged")
        self.expect("sym", "(")
        self.expect("sym", ")")
        return _path_changed(pattern, self.context.changed_files)

    def _parse_comparison(self) -> bool:
        name = self.pop()[1]
        if self.peek() == ("sym", "."):
            self.expect("sym", ".")
            self.expect("ident", "matches")
            self.expect("sym", "(")
            pattern = _cel_string(self.pop()[1])
            self.expect("sym", ")")
            if name != "target_branch":
                raise TriggerParseError(f"unsupported .matches() receiver {name}")
            return re.search(pattern, self.context.base_ref) is not None
        self.expect("op", "==")
        raw = self.pop()
        if raw[0] != "string":
            raise TriggerParseError("CEL comparison must be against a string")
        value = _cel_string(raw[1])
        if name == "event":
            return value == "pull_request"
        if name == "target_branch":
            return value == self.context.base_ref
        raise TriggerParseError(f"unsupported CEL comparison on {name}")

    def _parse_files(self) -> bool:
        self.expect("ident", "files")
        self.expect("sym", ".")
        kind = self.pop()
        if kind[0] != "ident" or kind[1] not in {"all", "exists"}:
            raise TriggerParseError("unsupported files macro")
        quantifier = kind[1]
        if quantifier == "all" and self.peek() == ("sym", "."):
            self.expect("sym", ".")
            self.expect("ident", "exists")
        self.expect("sym", "(")
        variable = self.pop()
        if variable[0] != "ident":
            raise TriggerParseError("files macro is missing a variable")
        self.expect("sym", ",")
        predicate = self._parse_file_predicate(variable[1])
        self.expect("sym", ")")
        results = [predicate(path) for path in self.context.changed_files]
        if quantifier == "all":
            return all(results) if results else True
        return any(results)

    def _parse_file_predicate(self, variable: str) -> Callable[[str], bool]:
        negate = False
        if self.peek() == ("sym", "!"):
            self.pop()
            negate = True
        name = self.pop()
        if name != ("ident", variable):
            raise TriggerParseError("files predicate must use its bound variable")
        self.expect("sym", ".")
        self.expect("ident", "matches")
        self.expect("sym", "(")
        pattern = _cel_string(self.pop()[1])
        self.expect("sym", ")")

        def matches(path: str) -> bool:
            found = re.search(pattern, path) is not None
            return (not found) if negate else found

        return matches


def _cel_string(token: str) -> str:
    body = token[1:-1]
    try:
        return body.encode("utf-8").decode("unicode_escape")
    except UnicodeError as exc:
        raise TriggerParseError(f"invalid CEL string {token}") from exc


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
