"""Bounded, data-only GitHub Actions presentation for successful previews."""
from collections import Counter
import html
from pathlib import Path
import sys

# Leave headroom below GitHub's 1 MiB per-step summary limit, including prior text.
FILE_LIMIT = 900_000
SUMMARY_LIMIT = 220_000
PATH_LIMIT = 48_000
DIFF_LIMIT = 160_000


def escaped_prefix(value, limit):
    """Limit *escaped UTF-8 bytes*, without splitting a character or HTML entity."""
    pieces = []
    size = 0
    for character in value:
        # Keep line breaks/tabs; make other control characters visible as data.
        if (ord(character) < 32 and character not in "\n\t") or ord(character) == 127:
            character = repr(character)[1:-1]
        escaped = html.escape(character, quote=True)
        encoded_size = len(escaped.encode("utf-8", errors="replace"))
        if size + encoded_size > limit:
            return "".join(pieces), True
        pieces.append(escaped)
        size += encoded_size
    return "".join(pieces), False


def render_preview_summary(result, patch, *, limit=SUMMARY_LIMIT, patch_truncated=False):
    """Render a bounded summary; all report values and patch contents are data."""
    changes = result.get("changes", [])
    counts = Counter(row["status"] for row in changes)
    status, status_cut = escaped_prefix(str(result.get("status", "unknown")), 512)
    diagnostics = result.get("diagnostics", {}).get("counts", {})
    deferred, _ = escaped_prefix(
        f"Manual: {diagnostics.get('manual', 'not reported')}; "
        f"unresolved: {diagnostics.get('unresolved', 'not reported')}.", 512)
    header = (
        "\n## Tekton reconciliation preview\n\n"
        f"Status: <code>{status}</code>{' (truncated)' if status_cut else ''}. "
        f"Planned files: **{len(changes)}** "
        f"(modified: {counts['M']}; deleted: {counts['D']}; "
        f"other: {len(changes) - counts['M'] - counts['D']}).\n\n"
        f"Deferred cases: <code>{deferred}</code> "
        "Ready describes the planned changes, not complete branch alignment. "
        "See result.json for diagnostics.\n\n"
        "**Full diff:** Download this job's report artifact from the run's "
        "**Artifacts** section and open <code>enforcement.patch</code> "
        "(alongside <code>result.json</code>). The full patch is preserved; "
        "the inline preview below may be truncated.\n\n"
    )
    # Reserve enough room for all closing markup and explicit notices before data.
    available = max(0, limit - len(header.encode("utf-8")) - 1400)
    paths_budget = min(PATH_LIMIT, available // 3)
    paths = []
    paths_size = 0
    paths_cut = False
    for row in changes:
        line, cut = escaped_prefix(f"{row['status']}  {row['path']}\n", paths_budget - paths_size)
        paths.append(line)
        paths_size += len(line.encode("utf-8"))
        if cut:
            paths_cut = True
            break
    paths_text = "".join(paths) if changes else "No planned file changes.\n"
    diff_budget = min(DIFF_LIMIT, max(0, available - paths_size))
    diff, diff_cut = escaped_prefix(patch, diff_budget)
    diff_cut = diff_cut or patch_truncated
    if not patch:
        diff = "No diff (no planned file changes)." if not changes else "Patch unavailable; see the report artifact."
    rendered = (
        header
        + "<details>\n<summary>Planned paths</summary>\n\n<pre>" + paths_text + "</pre>\n\n"
        + ("**Paths truncated by UTF-8 byte limit; see result.json in the full artifact.**\n\n" if paths_cut else "")
        + "</details>\n\n<details>\n<summary>Planned diff</summary>\n\n<pre>" + diff + "</pre>\n\n"
        + ("**Diff truncated by UTF-8 byte limit; download enforcement.patch for the full diff.**\n\n" if diff_cut else "")
        + "</details>\n"
    )
    if len(rendered.encode("utf-8")) > limit:
        raise ValueError("insufficient summary byte budget")
    return rendered


def append_preview_summary(result, report, environ):
    """Best-effort presentation must not change preview success or JSON output."""
    destination = environ.get("GITHUB_STEP_SUMMARY")
    if not destination:
        return
    try:
        path = Path(destination)
        used = path.stat().st_size if path.exists() else 0
        budget = min(SUMMARY_LIMIT, FILE_LIMIT - used)
        if budget < 4096:
            raise ValueError("existing step summary leaves insufficient byte budget")
        # Read only a bounded prefix; report truncation separately from its text.
        with (Path(report) / "enforcement.patch").open("rb") as stream:
            raw = stream.read(DIFF_LIMIT + 1)
        patch = raw[:DIFF_LIMIT].decode("utf-8", errors="replace")
        rendered = render_preview_summary(result, patch, limit=budget,
                                          patch_truncated=len(raw) > DIFF_LIMIT)
        with path.open("ab") as stream:
            stream.write(rendered.encode("utf-8"))
    except Exception as exc:
        print(f"warning: could not append Tekton preview summary: {exc}", file=sys.stderr)
