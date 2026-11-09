# Component ownership

The production monitor hook fetches `data/konflux-to-jira-component-map.yaml`
and `data/rhoai-component-data.yaml` from the `main` branch of
`https://gitlab.cee.redhat.com/wznoinsk/rhoai-monitoring` once per active pass
with observations. No production snapshot is embedded. Disabled and monitor
dry-run paths do not fetch; preview fetches mappings but does not contact Jira
or Slack. Direct Dispatcher callers retain previous behavior unless supplied
an ownership policy.

Access must work anonymously from the runner. TLS verification stays enabled;
corporate CA chains may be supplied explicitly through the hook's
`ownership_ca_bundle` argument (a CA bundle path), or the explicit non-secret
`GAP_OWNERSHIP_CA_BUNDLE` environment setting for deployed CLI/workflow runs.
The explicit hook argument wins; preview reads only this CA setting, not credentials.
TLS verification remains enabled with the supplied bundle. No ambient credentials,
netrc, proxies, or redirects are used for mapping fetches. Responses are capped
at 1 MiB and use 5-second connect / 15-second read timeouts. Fetches and schema
or safe-YAML parsing failures isolate the notification pass: delivery stops,
without affecting promotion decisions. No stale-map fallback is used.

Exact component keys win; only the existing numeric multi-segment release
suffix normalization is applied afterward (`api-v3-4`, not intrinsic `api-v2`).
Incident identity remains unchanged. Failed task names and conforma suppression
metadata do not influence ownership. Jira string or list values become Jira
components only on creation; existing issues are never reassigned. Jira ownership
entries come from the root `mappings` object; `_default` is read from the root.
Metadata, task-routing, and suppression keys are ignored. Missing
components (including repository-scoped conflict/feasibility incidents) use
`_default`, or DevOps when absent.

Slack uses `components.<name>.slack_team_handle`, falling back to
`components.default.slack_team_handle` for absent/unknown components or missing
handles, including explicit `unknown` handles. `slack_team_channel` is ignored: explicit notification channel routing
is unchanged. Only root failures include a dedicated mrkdwn section containing
`@mapped-handle` with `verbatim: false`, allowing Slack to parse that team handle.
All rendered body sections use `verbatim: true` and are at most 3000 characters.
Top-level text is unchanged fallback text; global `link_names` parsing is not enabled.
Reserved broadcast handles (`here`, `channel`, `everyone`) and malformed handles
are rejected before delivery. No usergroup lookup API or `usergroups:read` scope
is required; the bot retains its existing message-posting permissions. Slack-side
handle existence and notification behavior are rollout assumptions, not verified
by offline tests. Replies and recovery have no parsing-enabled mention block.
Existing replay suppression is unchanged. Runner Python HTTPS trust/access to
internal GitLab is assumed; no CA provisioning or trust-store changes are made.
TLS verification and the optional CA override remain in place. Tests inject
mapping fetches and fake services; they require no authenticated access.
