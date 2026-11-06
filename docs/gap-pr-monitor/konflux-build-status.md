# Authoritative Konflux build status (RHAI-19805)

## Shared lookup

`lib/konflux_builds.py` is the standard-library-only client shared with the
EG snapshot task. It reads live PipelineRuns plus KubeArchive, the supported
archive used by rhoai-monitoring, rather than mirrored GitHub build checks.
It never reads a GitHub check conclusion to decide a Konflux build.

Match the target repository, PR number, full current head SHA and expected
Component from controller metadata. PaC's runtime target-repository identity
handles forks and renamed repositories; a stale static build URL must not
overrule it. Do not infer PR identity from numeric PipelineRun-name suffixes.
The monitor also matches PaC's original pipeline identity when multiple
configured PR templates build the same Component.

Select attempts by PipelineRun creation timestamp, including queued retries.
Different UIDs with indistinguishable latest timestamps are unknown, not
arbitrarily ordered by UID or resourceVersion. Enumerate bounded, paginated
live/archive listings scoped to the current PR/head, and merge duplicate UIDs with live state taking
precedence over a lagging archived copy. A query error or exhausted bound
cannot authorize an older success. Same-head evidence is refreshed before
status publication; the existing moved-head guard still applies.

Succeeded=True plus completionTime passes. Succeeded=False failures/timeouts
fail. Queued/running, cancelled, absent or unknown evidence stays pending in
the monitor. Strict EG admission blocks every non-success outcome according
to its existing readiness deadline and no-active-PR fallback policy. GitHub
still owns PR metadata, mergeability and main-to-release feasibility checks.

## State and Leader report

Each monitored child has optional `build-observations` containing source,
repository, PR, head SHA, observation time, expected Components and selected
PipelineRun name/UID/component/status/reason/URL. Query errors are explicit.
The existing image `builds` list is unchanged. Creator retries and history
round-trip this additive evidence.

The maintained Leader report consumes those observations instead of querying
GitHub build checks again. It validates repository/PR/head and PipelineRun
URLs, includes both pull-request naming forms, and withholds stale-head links.
Evidence is an observation at the recorded time, not an assertion that no new
attempt could have started afterward. A later monitor reconciles new runs.

## Configuration and staged rollout

- `GAP_BUILD_STATUS_SOURCE=konflux` selects direct Konflux reads. The default
  `github` retains existing consumers until deployment credentials are ready.
  This is an explicit rollback/rollout setting, never an error fallback.
- `KONFLUX_TOKEN` or `KONFLUX_TOKEN_FILE`: dedicated read-only bearer token.
- `KONFLUX_API_URL`: target Kubernetes API; defaults to stone-prod-p02.
- `KONFLUX_KUBEARCHIVE_URL`: archive endpoint. Set explicitly in CI; otherwise
  read the product-kubearchive/kubearchive-api-url ConfigMap using the same
  API credential. No endpoint is guessed from a hostname.
- `KONFLUX_NAMESPACE`: defaults to rhoai-tenant.
- `KONFLUX_UI_URL` and `KONFLUX_CA_FILE`: optional UI URL and trusted CA.

TLS verification is enabled; authenticated redirects are rejected. Token
values and raw HTTP error bodies are not reported. The client performs only
GET requests and requires no create, patch, delete, rerun or push access.

The GAP deployment PR must provision/validate the read credential before
setting the new mode. Pin the same reviewed shared-resource revision in the
Actions checkout and snapshot helper parameter. Validate success, genuine
failure, pending retry and unavailable-archive cases on a controlled Leader
before general rollout. The change has not triggered any live promotion,
merged any PR, provisioned credentials or sent Slack notifications.

## Reporting reuse versus EG admission

Immediately after the monitor publishes state, its summary renderer reuses the
recorded PR metadata and build evidence instead of scanning the children again.
A standalone reporter still reads the current PR head to avoid displaying an
old-head failure. Closed-PR explanations can still require reading discussion.

EG admission is a different checkpoint: it revalidates requested PRs and builds
before selecting images and before publishing the trusted artifact. This catches
manual EG invocations, moved heads and newer attempts since Stage-1 monitoring.
Snapshot admission prefetches up to four unique PR/head lookups concurrently,
then reuses each result for all matching components within that polling pass.
The shared helper contract is version 2.

Both checkpoints use the same client/selection rules. They do not need separate
implementations or a second build scan merely to render a comment.

Read-only live validation matched an archived Spark failure, a current-head
dashboard success and the notebook codeserver pull-request failure. Current-head
selectors and small archive pages avoid scanning a long-lived PR's old commits.
