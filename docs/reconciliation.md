# Reusable reconciliation worker

`lib/reconciliation.py` is a standalone, standard-library-only callable worker.
Load it directly from **trusted workflow-selected bytes**, without importing
`lib.__init__`. Artifacts never select code, templates, commands, or callbacks.
This is not a YAML validator or a policy authorization engine.

## Planning

```python
# These functions are trusted consumer code, never artifact fields.
def validate(stage, phase):
    flags = {
        "initial": [], "preview": ["--fix", "--dry-run"],
        "fix": ["--fix"], "validation": [], "idempotence": ["--fix"],
    }[phase]
    completed = worker.run_validation(
        trusted_validator_argv(stage) + flags, stage, report / "logs", phase)
    # Consumer handles useful initial drift/nonzero, empty success, semantic
    # warnings, YAML/resource identity, completeness, and policy-specific rules.
    return interpret_validator_result(completed, phase)

manifest = worker.reconcile(
    checkout, scope="pipelineruns", report=report,
    provenance={"source": source_oid, "branch": branch,
                "policy": policy_oid, "validator": validator_oid,
                "publisher": publisher_oid},
    validate=validate,
    select=lambda initial, before: independently_selected_and_deleted(initial, before),
    context_paths=trusted_branch_local_pipeline_paths,
)
```

The report directory must not exist and must be disjoint from the checkout.
The worker inventories exact bytes/modes of tracked existing files, creates an
isolated stage with read-only context files, runs initial → preview → fix →
non-fix validation → idempotence, rereads after each phase (including context
bytes/modes, unchanged stage index, and no out-of-scope writes), enforces exact M/D
scope and unchanged executable modes, leaves the original checkout untouched,
and writes `enforcement.patch` (diagnostic only) plus `bundle/manifest.json` and
`bundle/files/<path>` (transport). Empty/missing scope is a valid no-op. Context
must be tracked and outside the writable scope. `snapshot_paths(root, paths)`
also inventories explicit cross-directory context without YAML parsing.

The trusted validator callback must reject failed checks and perform semantic
interpretation; returning diagnostic reports is not itself authorization. No
callbacks are serialized. `run_validation` captures stdout/stderr and argv/exit
status; do not put secrets in validator arguments. Caller-selected revisions
are recorded in provenance; equality checks do not attest code execution.

## Fresh publisher job

Transfer **only the bundle**, not the stage or planner checkout. In a separate
fresh runner, use trusted central authorization code before acquiring an App
token, and repeat authorization on publication-adapter entry:

```python
manifest = worker.prepare_bundle(
    fresh_checkout, bundle, scope="pipelineruns", base_oid=expected_base,
    expected_provenance=trusted_expected_provenance,
    authorize=independently_authorize_scan_change,
    allowed_statuses=("M",), allow_applied=True,
)
# authorize(row, original_git_bytes, candidate_bytes_or_None) must return True
# for EVERY row, including deletions, after independent policy authorization.
# Then build the existing publisher v1 request using manifest["changes"],
# consumer-selected repository/branches/marker/title/body, and trusted provenance.
# Call validated_pr_publisher.verify/publish; do not skip their checks.
```

Default replay requires a completely clean checkout. `allow_applied=True`
permits only exact manifest candidate bytes/deletions or original Git bytes,
with a clean index and no untracked/unrelated modifications, for repeated
pre-token/post-token verification. Every call rechecks original Git objects,
base HEAD, candidate hashes, modes, paths, scope, and mandatory independent
authorization for all files **before any writes**. Default operations are M only;
consumers supporting explicit template-signalled deletions opt into `("M", "D")`.
A late authorization failure leaves earlier candidate files untouched. As with
ordinary filesystem copying, an OS error during the final writes is not rolled
back; discard the local checkout and retry in a fresh one.

## Version 1 data schema and limits

Manifest fields are exactly `version: 1`, `base_oid`, `provenance` (an object),
and `changes` (at most 10,000 rows). Each row is exactly the existing publisher
change shape: `path`, `status` (M/D), `mode` (100644/100755), `before_sha256`,
`after_sha256` (null for D). Duplicate JSON keys/paths, unsafe paths, additions,
mode changes, symlinks below trusted roots including path ancestors, special files, stale bases,
provenance mismatches and mismatched bytes fail closed. Manifest and each payload
are bounded to 2 MiB. Caller-selected root aliases (such as `/var` on macOS)
are normalized once; artifact-selected descendants may not traverse symlinks.
Unknown manifest/row fields are rejected. Diagnostic
sidecar reports stay consumer-owned. Payloads are copied as bytes; untrusted
patches are never applied or executed.

**Hashes, statuses, modes and claimed revisions are integrity data, not policy
authorization.** Expected security-sensitive provenance must come from trusted
workflow configuration, not be copied from the incoming manifest. Mandatory
`authorize` runs trusted consumer code only, never planner or artifact-selected
code. For generic templates, do not execute untrusted template callbacks with
publication credentials; independently establish the allowed candidate set or
validate before credential acquisition. Retaining planning/publication in one
job remains a security limitation; pinning source alone does not solve it.

Release/inventory selection, applicability and branch-local Pipeline resolution,
scan task-budget deferral, resource floors/preservation, field ownership,
YAML duplicate/multidocument checks, template constraints and PR wording remain
consumer-owned. The shared worker replaces the common phase/file-loop, report
capture, exact delta, patch/bundle transport and replay plumbing. The existing
publisher retains origin/index/staged-object checks, remote-base freshness and
PR lifecycle/no-force-push behavior. No remote calls occur in this worker.
