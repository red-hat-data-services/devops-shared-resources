# PaC CEL trigger helper

A network-free JSON-in/JSON-out executable wrapping CEL-Go and gobwas/glob.
It does not import the PaC controller or read credentials, environment variables,
repository files, or GitHub APIs.

## Upstream baseline

The environment follows [PaC's trigger evaluator at a53fa582b3815ecb680700e8bd636002e206c8f1](https://github.com/openshift-pipelines/pipelines-as-code/blob/a53fa582b3815ecb680700e8bd636002e206c8f1/pkg/matcher/cel.go):
CEL-Go v0.32.0 and gobwas/glob v1.0.0. This is an upstream baseline, not
a claim about the version deployed in any particular cluster. Review the
environment and compatibility cases when updating those pins.

The actual trigger evaluator is unexported upstream. We reproduce its small
variable/function registration adapter, **not** its parser, evaluator, or glob
implementation. The helper does not add embedded-CEL-only variables or extensions.

## Build and test

From this directory, with Go 1.27 or newer:

```sh
go build -o ../../bin/pac-cel-evaluate .
go test ./...
go vet ./...
```

Run Python integration tests from the repository root with
`uv run pytest tests/test_pac_cel.py tests/test_gap_repository_gates.py`.
Their session fixture builds a temporary binary. Without Go those integration
tests are explicitly skipped; protocol-failure unit tests still run.

## Protocol

One request per invocation, with a result for each expression in order:

```json
{
  "expressions": [
    "event == \"pull_request\" && \"src/*.go\".pathChanged()",
    "files.all.exists(p, p.endsWith(\".go\"))"
  ],
  "variables": {
    "event": "pull_request",
    "target_branch": "stable",
    "source_branch": "feature/example",
    "files": {"all": ["src/nested/main.go"]}
  },
  "custom_params": {"enable_ci": "true"}
}
```

Response:

```json
[{"value": true}, {"value": true}]
```

An individual failure returns `{"error": "..."}` in its position without
discarding other results. Malformed request JSON produces a nonzero exit.
All successful results must be CEL booleans. Evaluation has a 100,000 cost-unit
limit; Python also applies a 15-second process timeout.

Standard variables: event, event_type, event_title, target_branch,
source_branch, target_url, source_url, body, headers, files.
Only supplied values are activated. Optional custom parameters are strings
and cannot override built-ins. A supplied body/headers map must be authoritative
for the intended event: partial synthetic payloads can turn unknown fields into
false absence tests.

File collections are all/added/deleted/modified/renamed. Unknown collections may
be omitted, but expressions referencing or probing them are rejected rather
than treating them as known-empty. A complete empty collection is `[]`.

`pathChanged()` uses gobwas/glob without separator arguments, exactly as the
upstream trigger function: a single `*` can cross `/`; character classes
and brace alternatives work. Invalid glob patterns and unavailable files
produce errors. Unlike PaC's current implementation, file-query failures do
not become false: the caller conservatively retains the expected check.

## Limits

This wraps the expression engine, not PaC's complete webhook dispatcher.
It does not implement push branch-prefix rewriting, label-event-specific
dispatch checks, Repository parameter fetching/filtering, or webhook parsing.
The current Python monitor supplies PR-state facts and deliberately leaves
webhook-only data unresolved. Exact webhook replay would require authoritative
payloads, headers, custom parameters, and matching dispatcher behavior.
