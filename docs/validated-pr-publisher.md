# Validated PR publisher

`lib/validated_pr_publisher.py` shares post-validation publication mechanics
without owning reconciliation or template policy. The CLI is
`scripts/publish_validated_pr.py`. It uses only Python's standard library plus
installed `git` and `gh` executables; it does not install packages, render
Jinja, import the validator, change repository settings, or enable auto-merge.

## Trust boundary

A **trusted caller** must independently validate candidate contents, allowed
paths/operations, policy ownership, completeness, exceptions, and validator
provenance before constructing this request. Planner artifacts and their hashes
are not authorization. The publisher cannot prove that a claimed validation
actually ran. Do not pass an untrusted planner manifest straight to this CLI.

Use a fresh publisher checkout and trusted tooling, separate from planning.
Transfer data, not an executable planner workspace. Do not import planner code
or execute templates with publication credentials. Repository Git configuration
and hooks must be controlled by the calling workflow. The request cannot choose
commands, callbacks, credentials, or validator implementations.

The CLI imports the standalone module from this checkout's `lib` directory,
bypassing `lib.__init__` and its unrelated eager dependencies. Python callers
can load the standalone module similarly; ordinary `lib.*` imports also execute
the package initializer. Publisher code must come from a trusted workflow-selected
revision, never a revision selected by the artifact. Record that resolved revision
in the caller's provenance alongside policy/validator revisions.

## Version 1 request

```json
{
  "version": 1,
  "repository": "example/component",
  "base": "rhoai-3.6",
  "base_oid": "0123456789012345678901234567890123456789",
  "head": "automation/component-reconciliation/rhoai-3.6",
  "lifecycle": "create-or-report",
  "marker": "<!-- unique-policy-identity -->",
  "title": "chore: reconcile PipelineRuns",
  "body": "Policy-specific review notes and validation results.",
  "provenance": {"validator": "resolved revision", "policy": "resolved revision"},
  "changes": [
    {
      "path": ".tekton/example.yaml",
      "status": "M",
      "mode": "100644",
      "before_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "after_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    }
  ]
}
```

Only existing regular-file modifications (`M`) and explicit deletions (`D`) are
supported. Deletions require `after_sha256: null`. Modes must remain unchanged
(`100644` or `100755`); additions, renames, symlinks, unsafe/duplicate paths, and
changes outside the manifest are rejected. Hashes describe exact bytes, including
trailing newlines. A caller may impose stricter restrictions, such as disallowing
all deletions. The shared publisher has no release allowlist: callers own release
selection, while publication always targets a distinct `automation/*` head.

## Preparation and execution

The caller independently checks and applies the exact candidate changes to a
checkout at `base_oid`, leaving the **index clean**. The origin must match the
requested GitHub repository. Store request/report files outside that checkout;
untracked files block publication. An empty exact plan is a no-op.

```sh
python scripts/publish_validated_pr.py --path /tmp/target --request /tmp/request.json
python scripts/publish_validated_pr.py --path /tmp/target --request /tmp/request.json --publish
```

Use your existing Python environment (for example `uv run --no-project` with an
existing interpreter). Without `--publish`, only local verification runs. With
`--publish`, caller-supplied Git/gh authentication is used; the tool does not mint
or discover credentials. The remote release tip is checked before publication,
and local contents/scope are rechecked before staging. Staged blob hashes/modes
are checked as well, so Git filters cannot silently alter validated bytes.

The tool creates signed-off Git objects without executing commit hooks, pushes
normally (never force), and creates/updates a PR. HEAD stays at the original base;
the index contains the staged changes afterward. A failed push leaves local Git
objects/index state and may require a fresh checkout. PR failure after a successful
push leaves the remote branch; callers must report that partial outcome and
review/retry deliberately. No rollback or automatic branch deletion is attempted.

## Lifecycle modes

- **create-or-report:** an existing open PR with the same head or policy marker
  is reported unchanged. A prior closed/merged PR on the exact head, or an
  existing head without a matching open PR, requires explicit disposition.
- **create-or-update:** reuse the configured head, checking the prior commit's
  generated marker. New commits have the fresh base and previous automation tip
  as parents, preserving append-only history. Identical tree/base pairs reuse the
  existing commit. An existing matching PR is updated; otherwise one is created.

Use a unique marker per policy/release identity, not a generic marker shared by
all policies. Marker checks prevent accidents; they are not hostile-actor
attestation. Multiple matching PRs and potentially truncated inventories fail
closed. Non-force push rejection handles concurrent automation-branch updates;
a release branch may still advance after its tip check, so review base freshness
again before merge. The caller owns concurrency control and merge decisions.

JSON output reports `verified`, `no-changes`, `existing-pr`, or `published`;
published receipts include commit, branch, base, PR URL, and supplied provenance.
Normal PR CI may start after publication. No workflow dispatch, deployment,
repository-setting mutation, or merge is requested.
