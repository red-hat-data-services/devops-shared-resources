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
# PLACEHOLDER: confirm P01 hosts / app slug with Deepak before merge.
DEFAULT_P01_API = "https://api.stone-prod-p01.wcfb.p1.openshiftapps.com:6443"
DEFAULT_P01_UI = "https://konflux-ui.apps.stone-prod-p01.wcfb.p1.openshiftapps.com"
DEFAULT_P01_ARCHIVE = (
    "https://kubearchive-api-server-product-kubearchive"
    ".apps.stone-prod-p01.wcfb.p1.openshiftapps.com"
)
DEFAULT_P02_ARCHIVE = (
    "https://kubearchive-api-server-product-kubearchive"
    ".apps.stone-prod-p02.hjvn.p1.openshiftapps.com"
)
CLUSTER_P01 = "p01"
CLUSTER_P02 = "p02"
DEFAULT_CLUSTER = CLUSTER_P02
# PLACEHOLDER P01 GitHub App slug — meeting: "Konflux Internal" (no -p02).
# Confirm exact slug via: gh api .../check-runs --jq '.check_runs[].app.slug'
DEFAULT_P01_APP_SLUG = "konflux-internal"
DEFAULT_P02_APP_SLUG = "konflux-internal-p02"
NAME = re.compile(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?")
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
SHA = re.compile(r"[0-9a-f]{40}")
RELEASE_SUFFIX = re.compile(r"-v[0-9]+-[0-9]+(?:-ea-[0-9]+)?$")
CLUSTER_ID = re.compile(r"^[a-z][a-z0-9-]{0,31}$")

# PaC labels that authorize a PR-associated build attempt.
# Keep this tight: same repo/PR/SHA/type=build checks still apply below.
# Only add GitOps "all pipelines" siblings of already-allowed single-pipeline
# comments (Leader #101: retest-all-comment was rejected and froze children).
PR_ASSOCIATED_EVENTS = frozenset(
    {
        "pull_request",
        "on-comment",
        "retest-comment",
        "retest-all-comment",  # /retest  (was missing → unknown trigger)
        "test-comment",
        "test-all-comment",  # /test   (pair of already-allowed test-comment)
    }
)
# Cancel ops are PR-scoped but must not be selected as build evidence.
CANCEL_EVENTS = frozenset({"cancel", "cancel-comment", "cancel-all-comment"})


class KonfluxQueryError(RuntimeError):
    """Unavailable or ambiguous evidence must never authorize promotion."""


def build_status_source() -> str:
    """Explicit rollout switch, never a fallback after a Konflux query error."""
    source = os.environ.get("GAP_BUILD_STATUS_SOURCE", "github").strip().lower()
    if source not in {"github", "konflux"}:
        raise KonfluxQueryError("GAP_BUILD_STATUS_SOURCE must be github or konflux")
    return source


def default_konflux_cluster() -> str:
    """Cluster used when no GitHub App slug is available yet."""
    raw = os.environ.get("KONFLUX_DEFAULT_CLUSTER", DEFAULT_CLUSTER).strip().lower()
    return raw or DEFAULT_CLUSTER


def _slug_cluster_overrides() -> dict[str, str]:
    """Optional ``slug:cluster,slug:cluster`` map from ``KONFLUX_APP_SLUG_CLUSTER_MAP``."""
    raw = os.environ.get("KONFLUX_APP_SLUG_CLUSTER_MAP", "").strip()
    if not raw:
        return {
            DEFAULT_P02_APP_SLUG: CLUSTER_P02,
            DEFAULT_P01_APP_SLUG: CLUSTER_P01,
        }
    mapping: dict[str, str] = {}
    for part in raw.split(","):
        piece = part.strip()
        if not piece:
            continue
        if ":" not in piece:
            raise KonfluxQueryError(
                "KONFLUX_APP_SLUG_CLUSTER_MAP entries must be slug:cluster"
            )
        slug, cluster = piece.split(":", 1)
        slug, cluster = slug.strip().lower(), cluster.strip().lower()
        if not slug or not CLUSTER_ID.fullmatch(cluster):
            raise KonfluxQueryError(
                f"Invalid KONFLUX_APP_SLUG_CLUSTER_MAP entry {piece!r}"
            )
        mapping[slug] = cluster
    return mapping


def cluster_id_for_app_slug(app_slug: str | None) -> str:
    """Map a Konflux GitHub App slug to a configured cluster id.

    Meeting rule of thumb: slug containing ``p02`` → p02; otherwise known
    Konflux Internal apps → p01. Explicit ``KONFLUX_APP_SLUG_CLUSTER_MAP``
    wins when set.
    """
    slug = (app_slug or "").strip().lower()
    if not slug:
        return default_konflux_cluster()
    overrides = _slug_cluster_overrides()
    if slug in overrides:
        return overrides[slug]
    if "p02" in slug:
        return CLUSTER_P02
    if "konflux" in slug or "p01" in slug:
        return CLUSTER_P01
    return default_konflux_cluster()


def cluster_id_from_check_runs(check_runs) -> str:
    """Pick a cluster from GitHub check-run app slugs already fetched for a PR.

    Prefer an explicit p02 producer when both appear (mixed cutover). Otherwise
    use the first recognizable Konflux app slug. No Konflux checks → default.
    """
    slugs: list[str] = []
    for run in check_runs or ():
        slug = getattr(run, "app_slug", None)
        if slug is None and isinstance(run, dict):
            app = run.get("app") if isinstance(run.get("app"), dict) else {}
            slug = app.get("slug")
        text = str(slug or "").strip().lower()
        if text:
            slugs.append(text)
    if not slugs:
        return default_konflux_cluster()
    for slug in slugs:
        if "p02" in slug or _slug_cluster_overrides().get(slug) == CLUSTER_P02:
            return CLUSTER_P02
    for slug in slugs:
        cluster = cluster_id_for_app_slug(slug)
        if cluster != default_konflux_cluster() or "konflux" in slug:
            return cluster
    return default_konflux_cluster()


def _env_for_cluster(cluster_id: str, suffix: str, *legacy_keys: str, default: str = "") -> str:
    """Read ``KONFLUX_<CLUSTER>_<SUFFIX>``, then legacy unprefixed keys, then default."""
    cluster = cluster_id.strip().lower()
    if not CLUSTER_ID.fullmatch(cluster):
        raise KonfluxQueryError(f"Invalid Konflux cluster id {cluster_id!r}")
    primary = os.environ.get(f"KONFLUX_{cluster.upper()}_{suffix}", "").strip()
    if primary:
        return primary
    for key in legacy_keys:
        value = os.environ.get(key, "").strip()
        if value:
            return value
    return default


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
    pipeline_name: str = ""
    completed_at: str = ""
    message: str = ""

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
    if event in CANCEL_EVENTS:
        return False
    if event not in PR_ASSOCIATED_EVENTS:
        run_name = metadata.get("name") or "unknown"
        raise KonfluxQueryError(
            "PR-associated PipelineRun "
            f"{run_name!r} has an unknown trigger event {event!r}"
        )
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
            # PaC keeps the full template name in the annotation; its label is
            # shortened for Kubernetes. Use the label only for legacy records.
            original = (metadata.get("annotations") or {}).get(PAC + "original-prname") or labels.get(PAC + "original-prname")
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
    origins = {(r[1]["metadata"].get("annotations") or {}).get(PAC + "original-prname")
               or (r[1]["metadata"].get("labels") or {}).get(PAC + "original-prname")
               for r in selected.values()}
    if not pipeline_names and len(origins - {None}) > 1:
        raise KonfluxQueryError("Multiple PR pipeline identities match this Component")
    if not selected:
        return BuildObservation(next(iter(sorted(names)), ""), "missing",
                                "No matching current-head Konflux PipelineRun found", sha)
    ordered = sorted(selected.values(), key=lambda x: x[0], reverse=True)
    if len(ordered) > 1 and ordered[0][0] == ordered[1][0]:
        raise KonfluxQueryError("Latest PipelineRun attempt is ambiguous")
    return _observation(ordered[0][1], sha, namespace, ui_url)


def _observation(run, sha, namespace, ui_url):
    """Translate a run after its caller has verified the full build identity."""
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
    # Optional display metadata must not change gate verdicts or attempt selection.
    completed_at = ""
    if result in {"success", "failure", "timed_out", "cancelled"}:
        try:
            completed = _timestamp(status.get("completionTime"))
            if completed >= _timestamp(metadata.get("creationTimestamp")):
                completed_at = completed.isoformat()
        except (KonfluxQueryError, OverflowError):
            pass
    ui = _endpoint(ui_url)
    return BuildObservation(labels[COMPONENT], result, reason, sha, metadata["name"],
                            metadata["uid"], namespace, metadata["creationTimestamp"],
                            f"{ui}/ns/{quote(namespace)}/pipelinerun/{quote(metadata['name'])}",
                            pipeline_name=str((metadata.get("annotations") or {}).get(PAC + "original-prname")
                                or labels.get(PAC + "original-prname") or ""),
                            completed_at=completed_at,
                            message=str(conditions[0].get("message") or "")[:4000] if conditions else "")


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
    def from_environment(cls, *, deadline=None, cluster=None):
        """Build a read-only client from deployment environment settings.

        ``cluster`` selects prefixed env vars (``KONFLUX_P01_*`` / ``KONFLUX_P02_*``).
        Unprefixed ``KONFLUX_TOKEN`` / ``KONFLUX_API_URL`` / … remain the P02
        backward-compatible fallback. Defaults without env still target P02.
        """
        cluster_id = (cluster or default_konflux_cluster()).strip().lower()
        if not CLUSTER_ID.fullmatch(cluster_id):
            raise KonfluxQueryError(f"Invalid Konflux cluster id {cluster!r}")
        if cluster_id == CLUSTER_P01:
            default_api, default_ui, default_archive = (
                DEFAULT_P01_API, DEFAULT_P01_UI, DEFAULT_P01_ARCHIVE,
            )
            legacy_token_keys: tuple[str, ...] = ()
            legacy_api_keys: tuple[str, ...] = ()
            legacy_archive_keys: tuple[str, ...] = ()
            legacy_ui_keys: tuple[str, ...] = ()
            legacy_ns_keys: tuple[str, ...] = ()
            legacy_ca_keys: tuple[str, ...] = ()
        else:
            # p02 and any future id: prefer prefixed, else legacy unprefixed.
            default_api, default_ui, default_archive = (
                DEFAULT_API, DEFAULT_UI, DEFAULT_P02_ARCHIVE,
            )
            legacy_token_keys = ("KONFLUX_TOKEN",)
            legacy_api_keys = ("KONFLUX_API_URL",)
            legacy_archive_keys = ("KONFLUX_KUBEARCHIVE_URL",)
            legacy_ui_keys = ("KONFLUX_UI_URL",)
            legacy_ns_keys = ("KONFLUX_NAMESPACE",)
            legacy_ca_keys = ("KONFLUX_CA_FILE",)
        token = _env_for_cluster(cluster_id, "TOKEN", *legacy_token_keys)
        token_file_legacy = ("KONFLUX_TOKEN_FILE",) if cluster_id == CLUSTER_P02 else ()
        token_file = _env_for_cluster(cluster_id, "TOKEN_FILE", *token_file_legacy)
        try:
            if token_file:
                token = Path(token_file).read_text().strip()
            return cls(
                _env_for_cluster(cluster_id, "API_URL", *legacy_api_keys, default=default_api),
                _env_for_cluster(
                    cluster_id, "KUBEARCHIVE_URL", *legacy_archive_keys, default=default_archive,
                ),
                token,
                namespace=_env_for_cluster(
                    cluster_id, "NAMESPACE", *legacy_ns_keys, default="rhoai-tenant",
                ),
                ui_url=_env_for_cluster(
                    cluster_id, "UI_URL", *legacy_ui_keys, default=default_ui,
                ),
                ca_file=_env_for_cluster(cluster_id, "CA_FILE", *legacy_ca_keys) or None,
                deadline=deadline,
            )
        except (OSError, ValueError, ssl.SSLError):
            raise KonfluxQueryError("Konflux read credential or TLS configuration is unavailable") from None

    @classmethod
    def for_cluster(cls, cluster_id, *, deadline=None):
        """Explicit multi-cluster entrypoint used by the PR monitor."""
        return cls.from_environment(deadline=deadline, cluster=cluster_id)

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
        self._cache[key] = self._list_resources(selector)
        self._errors.pop(key, None)
        return self._cache[key]

    def _list_resources(self, selector, resource="pipelineruns"):
        """Read a complete bounded live/archive listing; never trust a partial page."""
        path = f"/apis/tekton.dev/v1/namespaces/{quote(self.namespace)}/{resource}"
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
        return list(by_uid.values())

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
