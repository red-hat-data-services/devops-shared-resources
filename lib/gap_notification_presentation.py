"""Bounded literal evidence rendered through a restricted Markdown template.

Subset: one-line paragraphs, headings, flat bullets, explicit HTTPS links and
inline code. Backslash escapes punctuation. Other markup fails closed.
"""
from importlib.resources import files
import re
import string
from urllib.parse import urlsplit

from jinja2 import Environment, StrictUndefined


def literal(value):
    if value is None or value == "":
        value = "Unavailable"
    text = " ".join(str(value).split())
    if len(text) > 2000:
        raise ValueError("presentation field exceeds 2000 characters")
    return "".join("\\" + c if c in string.punctuation else c for c in text)


def https_url(value):
    if not isinstance(value, str) or len(value) > 4096:
        raise ValueError("invalid evidence URL")
    if any(c.isspace() or ord(c) < 32 or c in '<>\\`[]()\"' for c in value):
        raise ValueError("invalid evidence URL syntax")
    try:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("evidence URL must be HTTPS without credentials")
        parsed.port
    except ValueError:
        raise ValueError("invalid evidence URL") from None
    return value


def _inline(text):
    nodes, buf = [], ""
    def flush():
        nonlocal buf
        if buf:
            nodes.append({"type": "text", "text": buf})
            buf = ""
    i = 0
    while i < len(text):
        c = text[i]
        if c == "\\":
            if i + 1 >= len(text) or text[i + 1] not in string.punctuation:
                raise ValueError("invalid Markdown escape")
            buf += text[i + 1]
            i += 2
            continue
        if c == "[":
            match = re.match(r"\[([^\[\]\\]+)\]\(([^)]+)\)", text[i:])
            if not match:
                raise ValueError("unsupported Markdown link")
            flush()
            nodes.append({"type": "text", "text": match[1], "marks": [
                {"type": "link", "attrs": {"href": https_url(match[2])}}]})
            i += match.end()
            continue
        if c == "`":
            end = i + 1
            while end < len(text) and text[end] != "`":
                # An escaped punctuation character is literal, including a backtick.
                if text[end] == "\\" and end + 1 < len(text):
                    end += 2
                else:
                    end += 1
            if end >= len(text) or end <= i + 1:
                raise ValueError("invalid inline code")
            flush()
            code = _inline(text[i + 1:end])
            if any(n.get("marks") for n in code):
                raise ValueError("unsupported nested markup")
            nodes.append({"type": "text", "text": "".join(n["text"] for n in code), "marks": [{"type": "code"}]})
            i = end + 1
            continue
        if c in "*_[]<>#!|~" or text[i:i+3] == "---":
            raise ValueError("unsupported Markdown syntax")
        buf += c
        i += 1
    flush()
    return nodes


def markdown_to_adf(markdown):
    content = []
    for line in markdown.splitlines():
        if not line:
            continue
        heading = re.fullmatch(r"(#{1,6}) (.+)", line)
        if heading:
            content.append({"type": "heading", "attrs": {"level": len(heading[1])}, "content": _inline(heading[2])})
        elif line.startswith("- "):
            item = {"type": "listItem", "content": [{"type": "paragraph", "content": _inline(line[2:])}]}
            if not content or content[-1]["type"] != "bulletList":
                content.append({"type": "bulletList", "content": []})
            content[-1]["content"].append(item)
        else:
            if line.startswith((" ", "\t", ">", "+ ")) or re.match(r"\d+[.)] ", line):
                raise ValueError("unsupported Markdown block")
            content.append({"type": "paragraph", "content": _inline(line)})
    return {"type": "doc", "version": 1, "content": content}


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
