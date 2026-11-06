"""Bounded literal evidence rendered through a restricted Markdown template.

Subset: one-line paragraphs, headings, flat bullets, explicit HTTPS links and
inline code. Backslash escapes punctuation. Other markup fails closed.
"""
from importlib.resources import files
import re
from lib import jira_adf

from jinja2 import Environment, StrictUndefined


def literal(value):
    if value is None or value == "":
        value = "Unavailable"
    text = " ".join(str(value).split())
    if len(text) > 2000:
        raise ValueError("presentation field exceeds 2000 characters")
    return jira_adf.escape_literal(text)


def https_url(value):
    if not isinstance(value, str) or len(value) > 4096:
        raise ValueError("invalid evidence URL")
    return jira_adf.https_url(value)


def _inline(text):
    return jira_adf._inline(text, url_validator=https_url)


def markdown_to_adf(markdown):
    # Preserve the old direct export\'s GAP URL length bound too.
    return jira_adf.markdown_to_adf(markdown, url_validator=https_url)


def category_labels(category):
    return {"build": ["gap-build-failure"],
            "conflict": ["gap-sync-failure", "gap-merge-conflict"],
            "feasibility": ["gap-sync-failure", "gap-feasibility-failure"]}[category]


def render_markdown(observation, *, discussion_url=None, metadata_url=None):
    e = observation["evidence"]
    run = e.get("selected-run") or {}
    creator = e.get("creator-outcome") or {}
    context = {key: literal(observation.get(key)) for key in (
        "repository", "category", "source_branch", "destination_branch", "component", "pipeline", "observation_id")}
    context.update(pipeline=literal(observation.get("pipeline") or run.get("pipeline")),
                   reason=literal(e.get("reason") or run.get("reason") or creator.get("reason")),
                   note=literal(e.get("note")),
                   synthetic=literal(e.get("synthetic")), historical=literal(e.get("historical_snapshot")),
                   revision=literal(e.get("head_sha") or e.get("head-sha")), targets=[], links=[],
                   discussion_url=https_url(discussion_url) if discussion_url else None,
                   metadata_url=https_url(metadata_url) if metadata_url else None)
    targets = e.get("affected_targets", [])
    if not isinstance(targets, list) or len(targets) > 40:
        raise ValueError("affected_targets must be a list of at most 40 entries")
    for target in targets:
        if not isinstance(target, dict) or not re.fullmatch(r"rhoai-[A-Za-z0-9._-]+", str(target.get("branch", ""))):
            raise ValueError("affected target requires an explicit rhoai-X branch")
        context["targets"].append({k: literal(target.get(k)) for k in ("branch", "reason", "component", "pipeline")})
    for label, value in [("Child PR", e.get("pr_url")), ("Leader", e.get("leader_url")),
                         ("Build logs or PipelineRun", e.get("build_url")), ("State snapshot", e.get("state_url")),
                         ("Creator workflow", e.get("workflow_url") or creator.get("workflow-url")),
                         ("Creator PR", creator.get("pr-url"))]:
        if value:
            context["links"].append({"label": label, "url": https_url(value)})
    template = files("lib").joinpath("templates/gap_notification_issue.md.j2").read_text(encoding="utf-8")
    rendered = Environment(undefined=StrictUndefined, autoescape=False, keep_trailing_newline=True).from_string(template).render(**context)
    if len(rendered) > 24000:
        raise ValueError("presentation exceeds 24000 characters")
    return rendered


def description(observation, *, discussion_url=None, metadata_url=None):
    return markdown_to_adf(render_markdown(observation, discussion_url=discussion_url, metadata_url=metadata_url))
