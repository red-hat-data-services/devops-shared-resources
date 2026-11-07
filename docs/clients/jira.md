# Explicit Jira Cloud client and pure ADF utilities

`lib.jira_client.JiraClient` provides concrete **Jira Cloud REST v3 site-origin**
operations, without GAP notification policy. It is not an OAuth/discovery SDK,
Data Center compatibility layer, or `api.atlassian.com/ex/jira/<cloud-id>` gateway
client. Service operations use pinned `atlassian-python-api==5.0.5`'s `JiraCloud`
with explicit REST v3. GAP lifecycle/content policy remains outside the SDK.

## Configuration and operations

```python
from lib.jira_client import JiraClient

# Values must be supplied by an independently authorized caller. No environment
# lookup occurs here. Pass an injected fake session for offline tests.
client = JiraClient(base_url, email, token, session=fake_session, timeout=20)
```

The origin must be HTTPS, with a hostname and no URL credentials, explicit port,
path, query or fragment. Non-GAP sites/projects/types/properties are supported;
configuration is **not authorization** to access or mutate those services.
Credentials are excluded from the client's dataclass repr. Requests use explicit
Basic auth, disabled redirects, an explicit timeout, and `trust_env=False` where
the session supports it. There is no credential discovery or secret-store access.

| Operation | Input / result |
| --- | --- |
| `get_issue(key, fields=None)` | Optional sequence of requested field names; verified key/fields response mapping |
| `search(jql, fields=None)` | Caller JQL/field sequence; omitted fields explicitly request `summary` (enhanced search otherwise defaults to IDs only); complete list of issue mappings or an error, never partial success |
| `create_issue(fields, properties=None)` | Caller fields plus optional Jira property entries in the **same POST**; acknowledged `{key, url}` |
| `update_issue(key, fields=None, update=None)` | Caller field replacements/update operations; raw response data (often `None` on 204) |
| `get_issue_property(key, name)` | Explicit property name; `{key, value}` response envelope, or `None` on 404; a property's JSON `null` value remains distinguishable from absence |
| `put_issue_property(key, name, value)` | Caller JSON value, including `None` sent as literal JSON `null` with `application/json`; raw acknowledgement data |
| `transitions(key)` | Transition list with expanded fields |
| `apply_transition(key, transition_id, fields=None)` | Explicit transition and optional fields; raw acknowledgement data, not a claim of reaching a particular status |

Issue keys are validated structurally, not restricted to RHAI. The client does
not inspect arbitrary Jira fields for GAP's supported ADF subset, coerce bodies,
choose Bug/default fields, prohibit a generic workflow transition, or interpret
incident metadata. The create result confirms a service response, **not** GAP's
readback guarantees. The GAP adapter performs its own subsequent readbacks.

Search uses bounded pagination (50 results/page, at most 20 pages), rejects
incomplete/looping pagination and duplicate issue results, and raises rather
than returning a truncated inventory. Property names are encoded as URL path
segments, not interpreted as caller-supplied resource paths.

## SDK boundary and offline validation

All eight operations use native JiraCloud methods: `get_issue`,
`search_and_reconsile_issues_using_jql_post` (SDK spelling), `create_issue`,
`edit_issue`, `get_issue_property`, `set_issue_property`, `get_transitions`, and
`do_transition`. Whole-body creation preserves sibling `fields`/`properties` in
one POST; no SDK prefetch follows creation. Our bounded search loop remains;
the SDK all-results convenience iterator does not establish completeness.

A small injected transport bridge retains the public session's `get/post/put`
protocol while enforcing explicit auth, the exact (including fractional) timeout,
and disabled redirects at the final Requests boundary. Do not pass
`allow_redirects=False` to SDK native methods: in this pinned version extra kwargs
become query parameters. SDK backoff and Retry-After retries are both disabled.
Managed sessions have no transport retries; custom injected sessions remain
caller-owned and must likewise avoid retries or discovery internally.

Literal-null property writes use the native method with `data='null', files={}`:
this selects the pinned SDK's raw-body serialization path without multipart
content. This is a version-sensitive public-parameter combination, not a separate
raw-requests fallback. Tests assert the final Requests prepared body is exactly
`null`; they also cover booleans, strings, lists, numbers and objects. Pin upgrades
require rerunning these actual-SDK contract tests.

The instance subclass preflights JSON with `allow_nan=False` before the SDK's
serializer: nested NaN/Infinity values are rejected without preparing/sending an
HTTP request, preserving Requests' original semantic read/write error classes.
It also overrides `log_curl_debug` (a public SDK helper) to suppress
payload diagnostics, and the injected transport returns a response view whose
text/reason are empty for SDK logging but whose JSON and status/headers retain
original application evidence. Raw advanced mode avoids SDK exception formatting.
No global logger or environment monkeypatch is used in production. Request paths
may still appear in SDK debug logs; bodies and authentication do not. Generic
classification, shape checks, readbacks and sanitized errors remain ours.

`tests/test_jira_sdk_contract.py` executes the installed pinned SDK using real
Requests preparation and an in-memory send override. Outbound sockets, default
Session.send and netrc discovery are blocked by scoped test fixtures. No service
access or real credentials are needed. Existing semantic tests retain their
assertions; their fake transport normalizes serialized JSON/query strings for
inspection. Pure formatting does not construct an SDK client.

## Errors and uncertain writes

Errors retain sanitized messages and existing status/retry/key metadata:
`JiraError`, `JiraRateLimitError`, `JiraTransientError`, `JiraAmbiguousError`,
`JiraPermanentError`, and `JiraValidationError`. They are re-exported from the
old GAP adapter, preserving class identity for existing imports.

- Read timeout/request failure or 5xx: transient. **POST search is semantically
  a read**, despite its HTTP verb.
- Mutation timeout/request failure or 5xx: ambiguous, not safe-to-retry success
  or definite rejection. Malformed/unverifiable responses also fail closed.
- 429/rate-limit responses expose numeric Retry-After metadata; 400 exposes
  validation metadata; other 4xx are permanent rejections.
- There are **no automatic retries**, sleeps or backoff, including for create,
  issue/property updates and transitions. Retry-After is not permission to resend
  an uncertain write. No exactly-once or atomic search-plus-create is claimed.

This extraction intentionally retains the existing parser/metadata behavior; it
is not an unrelated error-parser or validation-hardening change.

## Pure formatting: `lib.jira_adf`

These functions need no client, auth, session or network:

- `escape_literal(text)`: escape punctuation in already-selected caller text;
  does not supply GAP's missing-value default, whitespace normalization or caps.
- `https_url(value)`: validate the existing restricted explicit-HTTPS link
  syntax, without GAP's evidence-field length limit.
- `markdown_to_adf(markdown, url_validator=https_url)`: the existing restricted
  converter: one-line paragraphs, headings 1–6, flat bullets, explicit HTTPS
  links, inline code, and punctuation backslash escapes. **Not full Markdown**:
  no emphasis, tables, nested lists, blockquotes or fenced-code parsing.
- `validate_adf(node, path="description")` and `adf_signature(node)`: the
  existing restricted ADF validation/canonical comparison. ADF validation also
  recognizes the preexisting blockquote/ordered-list/codeBlock/hardBreak node
  forms, but the Markdown converter does not generate those extra forms. Only
  code/link marks are supported; this is **not full ADF schema validation**.
  Signatures preserve link targets and supported structural content.

## Compatible GAP policy facade

`lib.gap_notifications.notify_tools.JiraNotifier` retains its constructor and eight
existing operations, including their return shapes and fake-session injection.
Constructor validation remains immediate. Later valid updates to its public
session/email/token/timeout fields are honored by a lightweight delegate derived
from current values rather than a cached copy of authentication/configuration.
It delegates service calls to `JiraClient` while retaining:

- Exact staging origin, RHAI project/key scope, Bug/10016, fixed fields and
  incident-label search; no silent production enablement.
- `gap.notification.v1`, application JSON/size policies, protected create fields,
  atomic initial properties and exact property readbacks.
- Description/label mutation refused on completed issues; explicit done-target
  transition validation, no reopening, status/field-preserving property writes
  even on a closed issue.
- Creation/update ADF/label/project readback checks and known issue keys on
  ambiguous post-create readback failures.

`JiraTerminalIssueError` remains a GAP policy error. The old pure private helpers
(`_validate_adf`, `_adf_signature`, `_validate_labels`, `_issue_key`, etc.) remain
callable without constructing an authenticated client. Dispatcher, diagnostics,
monitor and CLI callers were not changed.

`lib.gap_notifications.notify_tools` still owns the GAP template, category labels,
observation context, missing-value defaults and application caps. Its existing
`literal`, `https_url`, `_inline`, `markdown_to_adf`, `render_markdown` and
`description` exports retain behavior and delegate pure mechanics. In particular,
the old **direct** Markdown/inline entry points still enforce the existing
4096-character GAP URL bound, not only the higher-level template path. GAP's
2000-character field and 24000-character rendered-body bounds remain local.

All validation uses fake transports or offline formatting. No live credentials,
service calls, production operations or notification destination changes are
part of this extraction.
