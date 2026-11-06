"""Read-only Konflux build evidence shared by GAP monitors and Early Gate.

Select the latest PR-associated attempt from live Tekton and KubeArchive together.
No GitHub check status, PipelineRun-name suffix or resourceVersion ordering is
used as build truth. This module uses only the Python standard library so a
revision-pinned copy can also run inside the snapshot task image.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import ssl
import time
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

KONFLUX_BUILD_API_VERSION = 2

PAC = "pipelinesascode.tekton.dev/"
COMPONENT = "appstudio.openshift.io/component"
DEFAULT_API = "https://api.stone-prod-p02.hjvn.p1.openshiftapps.com:6443"
DEFAULT_UI = "https://konflux-ui.apps.stone-prod-p02.hjvn.p1.openshiftapps.com"
NAME = re.compile(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?")
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
SHA = re.compile(r"[0-9a-f]{40}")
RELEASE_SUFFIX = re.compile(r"-v[0-9]+-[0-9]+(?:-ea-[0-9]+)?$")


class KonfluxQueryError(RuntimeError):
    """Unavailable or ambiguous evidence must never authorize promotion."""


def build_status_source() -> str:
    """Explicit rollout switch, never a fallback after a Konflux query error."""
    source = os.environ.get("GAP_BUILD_STATUS_SOURCE", "github").strip().lower()
    if source not in {"github", "konflux"}:
        raise KonfluxQueryError("GAP_BUILD_STATUS_SOURCE must be github or konflux")
    return source


def _timestamp(value):
    """Parse a controller timestamp as UTC; malformed evidence is unknown."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.astimezone(timezone.utc)
    except (AttributeError, TypeError, ValueError):
        raise KonfluxQueryError("PipelineRun has missing or invalid timestamps") from None


def _repository(value):
    """Extract an owner/repository identity from a GitHub HTTPS URL."""
    if not isinstance(value, str):
        return None
    parsed = urlsplit(value)
    path = parsed.path.strip("/").removesuffix(".git")
    if parsed.scheme == "https" and parsed.hostname == "github.com" and REPO.fullmatch(path):
        return path.lower()
    return None


def _endpoint(value, *, path=False):
    """Validate a configured HTTPS endpoint before attaching credentials."""
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment
            or (not path and parsed.path.strip("/"))):
        raise KonfluxQueryError("Konflux endpoints must be trusted HTTPS URLs")
    return value.rstrip("/")


@dataclass(frozen=True)
class BuildObservation:
    component: str
    status: str
    reason: str
    head_sha: str
    name: str = ""
    uid: str = ""
    namespace: str = ""
    created_at: str = ""
    url: str = ""

    def to_dict(self):
        """Serialize the selected build observation for state persistence."""
        return asdict(self)


def _facts(run):
    """Extract and cross-check the controller identity for one PR build."""
    metadata = run.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise KonfluxQueryError("PipelineRun metadata is malformed")
    labels = metadata.get("labels") or {}
    annotations = metadata.get("annotations") or {}
    if not isinstance(labels, dict) or not isinstance(annotations, dict):
        raise KonfluxQueryError("PipelineRun metadata is malformed")
    return metadata, labels, annotations


def _identity(run, repo, number, sha):
    """Correlate target repository, PR and commit; fork source URLs may differ."""
    metadata, labels, annotations = _facts(run)
    # PaC writes the target repository at execution time. Static build
    # annotations can retain a repository's old name after a GitHub rename.
    repos = [_repository(annotations.get(PAC + "repo-url"))]
    owner = labels.get(PAC + "url-org")
    name = labels.get(PAC + "url-repository")
    if owner and name:
        repos.append(str(owner).lower() + "/" + str(name).lower())
    if not any(repos):
        repos.append(_repository(annotations.get("build.appstudio.openshift.io/repo")))
    prs = [str(v) for v in (labels.get(PAC + "pull-request"),
                           annotations.get(PAC + "pull-request"),
                           annotations.get("build.appstudio.redhat.com/pull_request_number")) if v is not None]
    shas = [str(v) for v in (labels.get(PAC + "sha"), annotations.get(PAC + "sha"),
                            annotations.get("build.appstudio.redhat.com/commit_sha")) if v is not None]
    spec = run.get("spec") or {}
    if not isinstance(spec, dict) or not isinstance(spec.get("params", []), list):
        raise KonfluxQueryError("PipelineRun parameters are malformed")
    params = {p.get("name"): p.get("value") for p in spec.get("params", []) if isinstance(p, dict)}
    revision = params.get("revision")
    if isinstance(revision, str) and SHA.fullmatch(revision):
        shas.append(revision)
    repos = [r for r in repos if r]
    event = labels.get(PAC + "event-type") or annotations.get(PAC + "event-type")
    if event in {"push", "incoming"}:
        return False
    if repo.lower() in repos and str(number) in prs and not shas:
        raise KonfluxQueryError("PR-associated PipelineRun has no verifiable revision")
    if repo.lower() in repos and sha in shas and not prs:
        raise KonfluxQueryError("Current-head PipelineRun has no verifiable PR association")
    if repo.lower() not in repos or str(number) not in prs or sha not in shas:
        return False
    if any(r != repo.lower() for r in repos) or any(p != str(number) for p in prs) or any(s != sha for s in shas):
        raise KonfluxQueryError("Conflicting repository/PR/revision identity on a matching PipelineRun")
    build_type = labels.get("pipelines.appstudio.openshift.io/type")
    if not build_type:
        raise KonfluxQueryError("PR-associated PipelineRun has no verifiable build type")
    if build_type != "build":
        return False
    if event not in {"pull_request", "on-comment", "retest-comment", "test-comment"}:
        raise KonfluxQueryError("PR-associated PipelineRun has an unknown trigger event")
    return True


def select_latest(runs, *, repo, number, sha, components, namespace, ui_url=DEFAULT_UI,
                  base_names=False, pipeline_names=None):
    """Return one latest matching run, refusing ambiguous release/attempt identity.

    Monitor components are exact labels selected from revision-pinned .tekton.
    Snapshot keys are release-qualified artifact keys, not necessarily actual
    Konflux labels; base_names permits a unique matching physical Component.
    Multiple physical Components are ambiguous rather than sorted across releases.
    """
    if not REPO.fullmatch(repo) or not SHA.fullmatch(sha) or not str(number).isdigit():
        raise KonfluxQueryError("Invalid build query identity")
    names = set(components)
    selected = {}
    for run in runs:
        if not isinstance(run, dict) or run.get("kind") != "PipelineRun":
            raise KonfluxQueryError("Expected PipelineRun records")
        metadata, labels, _ = _facts(run)
        component = labels.get(COMPONENT)
        key = RELEASE_SUFFIX.sub("", component) if base_names and isinstance(component, str) else component
        if key not in names or not _identity(run, repo, number, sha):
            continue
        if pipeline_names:
            original = labels.get(PAC + "original-prname") or (metadata.get("annotations") or {}).get(PAC + "original-prname")
            if not original:
                raise KonfluxQueryError("Matching PipelineRun has no original PR pipeline identity")
            if original not in pipeline_names:
                continue
        if metadata.get("namespace") != namespace:
            raise KonfluxQueryError("Matching PipelineRun belongs to another namespace")
        name, uid = metadata.get("name"), metadata.get("uid")
        if not isinstance(name, str) or not NAME.fullmatch(name) or not isinstance(uid, str) or not uid:
            raise KonfluxQueryError("Matching PipelineRun has missing name/UID")
        created = _timestamp(metadata.get("creationTimestamp"))
        # Client merge gives live data precedence over an archived copy of the
        # same UID. A different run must never replace it merely by name.
        selected[uid] = (created, run)
    physical = {r[1]["metadata"]["labels"][COMPONENT] for r in selected.values()}
    if base_names and len(physical) > 1:
        raise KonfluxQueryError("Multiple version-specific Components match this snapshot key")
    origins = {(r[1]["metadata"].get("labels") or {}).get(PAC + "original-prname")
               or (r[1]["metadata"].get("annotations") or {}).get(PAC + "original-prname")
               for r in selected.values()}
    if not pipeline_names and len(origins - {None}) > 1:
        raise KonfluxQueryError("Multiple PR pipeline identities match this Component")
    if not selected:
        return BuildObservation(next(iter(sorted(names)), ""), "missing",
                                "No matching current-head Konflux PipelineRun found", sha)
    ordered = sorted(selected.values(), key=lambda x: x[0], reverse=True)
    if len(ordered) > 1 and ordered[0][0] == ordered[1][0]:
        raise KonfluxQueryError("Latest PipelineRun attempt is ambiguous")
    run = ordered[0][1]
    metadata, labels, _ = _facts(run)
    status = run.get("status") or {}
    if not isinstance(status, dict) or not isinstance(status.get("conditions", []), list):
        raise KonfluxQueryError("Latest PipelineRun status is malformed")
    conditions = [c for c in status.get("conditions", []) if isinstance(c, dict) and c.get("type") == "Succeeded"]
    result, reason = "pending", "PipelineRun has not completed"
    if len(conditions) > 1:
        raise KonfluxQueryError("Multiple Succeeded conditions on the latest PipelineRun")
    if conditions:
        condition = conditions[0]
        reason = str(condition.get("reason") or "Unknown")[:160]
        value = condition.get("status")
        if value == "True":
            result = "success" if status.get("completionTime") else "unknown"
        elif value == "False":
            if not status.get("completionTime"):
                result = "unknown"
            elif "cancel" in reason.lower() or "stop" in reason.lower():
                result = "cancelled"
            elif "timeout" in reason.lower() or "timedout" in reason.lower():
                result = "timed_out"
            else:
                result = "failure"
        elif value != "Unknown":
            result = "unknown"
    if metadata.get("deletionTimestamp") and result not in {"success", "failure", "timed_out", "cancelled"}:
        result, reason = "unknown", "Latest PipelineRun is being deleted before a terminal observation"
    ui = _endpoint(ui_url)
    return BuildObservation(labels[COMPONENT], result, reason, sha, metadata["name"],
                            metadata["uid"], namespace, metadata["creationTimestamp"],
                            f"{ui}/ns/{quote(namespace)}/pipelinerun/{quote(metadata['name'])}")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        """Reject redirects rather than forwarding the bearer credential."""
        return None


class KonfluxClient:
    """Bounded, paginated, read-only live + KubeArchive API reader."""

    def __init__(self, api_url, archive_url, token, *, namespace="rhoai-tenant",
                 ui_url=DEFAULT_UI, ca_file=None, deadline=None, request_json=None):
        """Configure authenticated read endpoints, TLS and the lookup deadline."""
        self.api_url = _endpoint(api_url)
        self.archive_url = _endpoint(archive_url, path=True) if archive_url else None
        self.ui_url = _endpoint(ui_url)
        if not NAME.fullmatch(namespace) or not token:
            raise KonfluxQueryError("Konflux namespace or read credential is unavailable")
        self.namespace, self._token = namespace, token
        self.deadline = deadline if deadline is not None else time.monotonic() + 60
        self._cache, self._errors = {}, {}
        self._tls_context = ssl.create_default_context(cafile=ca_file)
        self._request_json = request_json or self._get

    @classmethod
    def from_environment(cls, *, deadline=None):
        """Build a read-only client from deployment environment settings."""
        token = os.environ.get("KONFLUX_TOKEN", "").strip()
        token_file = os.environ.get("KONFLUX_TOKEN_FILE")
        try:
            if token_file:
                token = Path(token_file).read_text().strip()
            return cls(os.environ.get("KONFLUX_API_URL", DEFAULT_API),
                       os.environ.get("KONFLUX_KUBEARCHIVE_URL", ""), token,
                       namespace=os.environ.get("KONFLUX_NAMESPACE", "rhoai-tenant"),
                       ui_url=os.environ.get("KONFLUX_UI_URL", DEFAULT_UI),
                       ca_file=os.environ.get("KONFLUX_CA_FILE") or None, deadline=deadline)
        except (OSError, ValueError, ssl.SSLError):
            raise KonfluxQueryError("Konflux read credential or TLS configuration is unavailable") from None

    def _get(self, url):
        """Read authenticated JSON within the deadline without leaking error bodies."""
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise KonfluxQueryError("Konflux lookup deadline reached")
        try:
            request = Request(url, headers={"Authorization": "Bearer " + self._token,
                                           "Accept": "application/json"})
            # Each concurrent request gets its own opener/handlers. The TLS
            # context is immutable after construction and safe to share.
            opener = build_opener(_NoRedirect(), HTTPSHandler(context=self._tls_context))
            with opener.open(request, timeout=min(10, remaining)) as response:
                return json.load(response)
        except HTTPError as exc:
            raise KonfluxQueryError(f"Konflux lookup failed (HTTP {exc.code}); build state is unknown") from None
        except Exception:
            raise KonfluxQueryError("Konflux lookup or response failed; build state is unknown") from None

    def runs_for_pr(self, repo, number, sha, *, refresh=False):
        """Enumerate live and archived attempts for one current PR head."""
        if not REPO.fullmatch(repo) or not SHA.fullmatch(sha) or not re.fullmatch(r"[0-9]+", str(number)):
            raise KonfluxQueryError("Invalid repository/PR/head query")
        key = (repo.lower(), str(number), sha)
        if key in self._errors and not refresh:
            raise KonfluxQueryError(self._errors[key])
        if key in self._cache and not refresh:
            return self._cache[key]
        # Query only this PR revision; archive payloads for long-lived notebook
        # PRs span many components/commits and can otherwise exhaust the budget.
        selector = PAC + "pull-request=" + str(number) + "," + PAC + "sha=" + sha
        path = f"/apis/tekton.dev/v1/namespaces/{quote(self.namespace)}/pipelineruns"
        live, continuation, seen, version = [], "", set(), None
        for _ in range(40):
            query = {"labelSelector": selector, "limit": 100}
            if continuation:
                query["continue"] = continuation
            page = self._request_json(self.api_url + path + "?" + urlencode(query))
            if not isinstance(page, dict) or not isinstance(page.get("items"), list) or not isinstance(page.get("metadata"), dict):
                raise KonfluxQueryError("Incomplete live PipelineRun listing")
            meta = page["metadata"]
            current_version = meta.get("resourceVersion")
            if not current_version or (version is not None and version != current_version):
                raise KonfluxQueryError("Live PipelineRun pagination snapshot changed")
            version = current_version
            live.extend(page["items"])
            continuation = meta.get("continue", "")
            if not isinstance(continuation, str) or continuation in seen:
                raise KonfluxQueryError("Invalid/repeated live continuation token")
            if not continuation:
                break
            seen.add(continuation)
        else:
            raise KonfluxQueryError("Live PipelineRun pagination limit reached")
        # Archive enumeration is mandatory: an older retained live run must not
        # hide a newer attempt which has already been archived/deleted.
        if self.archive_url is None:
            discovery = self._request_json(
                self.api_url + "/api/v1/namespaces/product-kubearchive/configmaps/kubearchive-api-url")
            try:
                self.archive_url = _endpoint(discovery["data"]["URL"], path=True)
            except (KeyError, TypeError):
                raise KonfluxQueryError("Set KONFLUX_KUBEARCHIVE_URL; archive discovery is unavailable") from None
        # Use the same identity scope for both sources. Filtering type here
        # could hide a newer matching run whose build-type metadata is missing.
        archive_selector = selector
        records, continuation, seen = [], "", set()
        for _ in range(40):
            query = {"labelSelector": archive_selector, "limit": 20}
            if continuation:
                query["continue"] = continuation
            page = self._request_json(self.archive_url + path + "?" + urlencode(query))
            if (not isinstance(page, dict) or not isinstance(page.get("items"), list)
                    or not isinstance(page.get("metadata"), dict)):
                raise KonfluxQueryError("Incomplete archived PipelineRun listing")
            records.extend(page["items"])
            continuation = page["metadata"].get("continue", "")
            if not isinstance(continuation, str) or continuation in seen:
                raise KonfluxQueryError("Invalid/repeated archive continuation token")
            if not continuation:
                break
            seen.add(continuation)
        else:
            raise KonfluxQueryError("Archived PipelineRun pagination limit reached")
        # KubeArchive can lag a live object. Merge by UID, letting live data win.
        by_uid = {}
        for run in records + live:
            if not isinstance(run, dict):
                raise KonfluxQueryError("Malformed PipelineRun record")
            uid = (run.get("metadata") or {}).get("uid")
            if not uid:
                raise KonfluxQueryError("PipelineRun record has no UID")
            by_uid[uid] = run
        self._cache[key] = list(by_uid.values())
        self._errors.pop(key, None)
        return self._cache[key]

    def prefetch_prs(self, queries, *, max_workers=4):
        """Read each unique PR/head once with bounded parallelism.

        Callers wait for completion before consuming the cache. No two workers
        own the same cache key; requests never share mutable opener handlers.
        Failed reads are cached for this polling observation, never as success.
        """
        if not isinstance(max_workers, int) or not 1 <= max_workers <= 8:
            raise KonfluxQueryError("Konflux prefetch concurrency must be between 1 and 8")
        unique = sorted({(repo.lower(), str(number), sha) for repo, number, sha in queries})
        # Resolve the optional endpoint once, before workers start.
        if unique and self.archive_url is None:
            try:
                discovery = self._request_json(
                    self.api_url + "/api/v1/namespaces/product-kubearchive/configmaps/kubearchive-api-url")
                self.archive_url = _endpoint(discovery["data"]["URL"], path=True)
            except (KonfluxQueryError, KeyError, TypeError) as exc:
                reason = str(exc) if isinstance(exc, KonfluxQueryError) else "KubeArchive endpoint discovery unavailable"
                self._errors.update({key: reason for key in unique})
                return
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            jobs = {executor.submit(self.runs_for_pr, *key): key for key in unique}
            for job in as_completed(jobs):
                try:
                    job.result()
                except KonfluxQueryError as exc:
                    key = jobs[job]
                    self._cache.pop(key, None)
                    self._errors[key] = str(exc)

    def clear_cache(self):
        """Start a fresh polling observation without reusing prior run lists."""
        self._cache.clear()
        self._errors.clear()

    def observe(self, *, repo, number, sha, components, base_names=False, refresh=False, pipeline_names=None):
        """Select the latest identity-matching build from the cached PR snapshot."""
        return select_latest(self.runs_for_pr(repo, number, sha, refresh=refresh),
                             repo=repo, number=number, sha=sha, components=components,
                             namespace=self.namespace, ui_url=self.ui_url, base_names=base_names,
                             pipeline_names=pipeline_names)
