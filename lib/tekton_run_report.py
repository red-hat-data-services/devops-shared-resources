#!/usr/bin/env python3
"""Render diagnostic artifacts as a fleet report; never publish or execute them."""

import argparse
from collections import Counter
import html
import json
from pathlib import Path
import re


SUMMARY_LIMIT = 900_000


def escape(value):
    text = html.escape(str(value), quote=True)
    for character in "|[]`*_":
        text = text.replace(character, f"&#{ord(character)};")
    return text


def read_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def pr_link(url, owner, repository):
    if isinstance(url, str) and re.fullmatch(
        rf"https://github\.com/{re.escape(owner)}/{re.escape(repository)}/pull/[0-9]+", url
    ):
        return f"[PR #{url.rsplit('/', 1)[-1]}]({url})"
    return "—"


def collect(artifacts, jobs, artifact_prefix, owner, dry_run):
    # gh api --paginate --slurp produces an array of pages.
    pages = jobs if isinstance(jobs, list) else [jobs]
    if any(not isinstance(page, dict) or not isinstance(page.get("jobs"), list)
           for page in pages):
        raise ValueError("invalid jobs API response")
    rows = []
    for page in pages:
        for job in page.get("jobs", []):
            match = re.match(r"^reconcile \(([^,]+),", job.get("name", ""))
            if not match:
                continue
            repository = match[1]
            # Repository names come from job metadata, not artifact-controlled paths.
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", repository):
                continue
            root = artifacts / f"{artifact_prefix}{repository}"
            report = root / "enforcement-report"
            result = read_json(report / "result.json") or {}
            if (not isinstance(result, dict)
                    or not isinstance(result.get("changes", []), list)
                    or any(not isinstance(change, dict)
                           or not isinstance(change.get("path"), str)
                           or not isinstance(change.get("status"), str)
                           for change in result.get("changes", []))):
                result = {}
            publication = read_json(report / "publication.json") or result.get("publication") or {}
            if not isinstance(publication, dict):
                publication = {}
            status = result.get("status", "missing report")
            if job.get("conclusion") != "success":
                outcome = f"job {job.get('conclusion') or job.get('status', 'unknown')}"
            elif publication.get("status") == "published":
                outcome = "PR created"
            elif publication.get("status") == "existing-pr":
                outcome = "existing PR"
            elif status == "no-changes":
                outcome = "no changes"
            elif dry_run and status == "ready":
                outcome = "would create/report PR"
            else:
                outcome = f"publication {publication.get('status', 'not confirmed')} ({status})"
            rows.append({
                "repository": repository, "outcome": outcome, "result": result,
                "publication": publication, "root": root,
                "failed_steps": [step.get("name", "unknown") for step in job.get("steps", [])
                                 if step.get("conclusion") == "failure"],
                "pr": pr_link(publication.get("url"), owner, repository),
            })
    return sorted(rows, key=lambda row: row["repository"].lower())


def inline(value):
    return escape(value).replace("\n", "<br>").replace("\r", "")


def overview(rows, run_url, dry_run):
    counts = Counter(row["outcome"] for row in rows)
    lines = ["# Component reconciliation report", "",
             f"[Workflow run]({run_url}) · {'Dry run' if dry_run else 'Publication run'}", ""]
    lines.extend(f"- **{inline(status)}:** {count}" for status, count in sorted(counts.items()))
    operations = Counter(change["status"] for row in rows
                         for change in row["result"].get("changes", []))
    lines.append(f"\nPlanned files: **{sum(operations.values())}** "
                 f"(deleted: {operations['D']}; modified: {operations['M']}; added: {operations['A']}).")
    if not rows:
        lines.append("No reconciliation job metadata available; inventory may have failed.")
    lines.extend(["", "## PRs", "", "| Repository | Outcome | PR |", "|---|---|---|"])
    linked = [row for row in rows if row["pr"] != "—"]
    for row in linked:
        lines.append(f"| {inline(row['repository'])} | {inline(row['outcome'])} | {row['pr']} |")
    if not linked:
        lines.append("| — | No PR creation or existing PR confirmed by reports | — |")
    lines.extend(["", "## All repositories", "", "| Repository | Outcome | Planned files | PR |",
                  "|---|---|---:|---|"])
    for row in rows:
        lines.append(f"| {inline(row['repository'])} | {inline(row['outcome'])} | "
                     f"{len(row['result'].get('changes', []))} | {row['pr']} |")
    return "\n".join(lines) + "\n"


def render(rows, run_url, dry_run):
    intro = overview(rows, run_url, dry_run)
    sections = [intro, "\n## Change details\n",
                "Changes below are planned candidates, not proof of publication or merge. "
                "For an existing PR, its contents may differ from this run's candidate.\n"]
    for row in rows:
        sections.extend([f"\n### {escape(row['repository'])}\n",
                         f"**Outcome:** {inline(row['outcome'])} · {row['pr']}\n"])
        if row["failed_steps"]:
            sections.append("**Failed steps:** " + ", ".join(
                f"<code>{escape(step)}</code>" for step in row["failed_steps"]) + "\n")
        result = row["result"]
        changes = result.get("changes", [])
        if not result:
            sections.append("No result report was uploaded (for example, checkout failed).\n")
        for operation, heading in [("D", "Files deleted"), ("M", "Files modified"), ("A", "Files added")]:
            paths = [change["path"] for change in changes if change.get("status") == operation]
            if paths:
                sections.append(f"\n**{heading}**\n\n" + "\n".join(
                    f"- <code>{escape(path)}</code>" for path in paths) + "\n")
        if any(change.get("status") != "D" for change in changes):
            patch_path = row["root"] / "enforcement-report" / "enforcement.patch"
            patch = patch_path.read_text(errors="replace") if patch_path.exists() else ""
            # Git emits one block per path. Deleted files are listed above rather
            # than filling the report with their entire removed contents.
            blocks = re.split(r"(?=^diff --git )", patch, flags=re.MULTILINE)
            diffs = [block for block in blocks if block.startswith("diff --git ")
                     and not re.search(r"^deleted file mode ", block, re.MULTILINE)]
            for block in diffs:
                label = block.splitlines()[0]
                sections.append("\n<details>\n<summary>" + escape(label) + "</summary>\n\n<pre>"
                                + escape(block) + "</pre>\n\n</details>\n")
            if not diffs:
                sections.append("\nDiff unavailable; inspect the repository diagnostic artifact.\n")
        log_path = row["root"] / "enforcement.log"
        if row["outcome"].startswith("job ") and log_path.exists():
            errors = [line for line in log_path.read_text(errors="replace").splitlines()
                      if line.startswith("error:")][:5]
            if errors:
                sections.append("\n**Diagnostic:** <pre>" + escape("\n".join(errors)[:4000]) + "</pre>\n")
    summary = intro + f"\nDownload **component-reconciliation-report** from the [run artifacts]({run_url}#artifacts) "
    summary += "for the full Markdown report, deleted-file lists and modified-file diffs.\n"
    if len(summary.encode()) > SUMMARY_LIMIT:
        summary = ("# Component reconciliation report\n\nSummary exceeds the display limit. "
                   f"Download **component-reconciliation-report** from [run artifacts]({run_url}#artifacts).\n")
    return "\n".join(sections), summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--jobs", type=Path, required=True)
    parser.add_argument("--artifact-prefix", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--dry-run", choices=["true", "false"], required=True)
    parser.add_argument("--run-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", args.artifact_prefix):
        parser.error("invalid artifact prefix")
    if not re.fullmatch(r"[A-Za-z0-9-]+", args.owner):
        parser.error("invalid repository owner")
    if not re.fullmatch(r"https://github\.com/[\w.-]+/[\w.-]+/actions/runs/[0-9]+", args.run_url):
        parser.error("invalid run URL")
    jobs = read_json(args.jobs)
    if jobs is None:
        parser.error("job metadata missing or invalid")
    rows = collect(args.artifacts, jobs, args.artifact_prefix, args.owner, args.dry_run == "true")
    report, summary = render(rows, args.run_url, args.dry_run == "true")
    args.output.write_text(report)
    with args.summary.open("a") as stream:
        stream.write(summary)


if __name__ == "__main__":
    main()
