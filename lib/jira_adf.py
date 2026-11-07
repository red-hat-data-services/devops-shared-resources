"""Pure restricted Markdown/ADF mechanics, independent of authenticated clients.

Escape literal punctuation, validate explicit HTTPS links, convert the supported
Markdown subset to ADF, validate supported ADF nodes/marks and compute structural
signatures for application readback comparisons. Markdown supports one-line
paragraphs, headings, flat bullets, links and inline code; the ADF validator also
recognizes existing node forms that the converter does not generate.

This is neither a full Markdown parser nor full ADF schema validation. Callers
own templates, missing-value defaults and application size limits; an optional
URL validator lets legacy GAP wrappers retain their stricter bounds. No network,
credential access or service-client construction is needed for these utilities.
"""
import re
import string
from typing import Any
from urllib.parse import urlsplit

VALID_MARK_TYPES = {"code", "link"}
VALID_NODE_TYPES = {
    "blockquote",
    "bulletList",
    "codeBlock",
    "doc",
    "hardBreak",
    "heading",
    "listItem",
    "orderedList",
    "paragraph",
    "text",
}


def escape_literal(text):
    """Escape punctuation in caller-supplied text without application defaults."""
    return "".join("\\" + c if c in string.punctuation else c for c in text)

def https_url(value):
    if not isinstance(value, str):
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


def _inline(text, *, url_validator=https_url):
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
                {"type": "link", "attrs": {"href": url_validator(match[2])}}]})
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
            code = _inline(text[i + 1:end], url_validator=url_validator)
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


def markdown_to_adf(markdown, *, url_validator=https_url):
    content = []
    for line in markdown.splitlines():
        if not line:
            continue
        heading = re.fullmatch(r"(#{1,6}) (.+)", line)
        if heading:
            content.append({"type": "heading", "attrs": {"level": len(heading[1])}, "content": _inline(heading[2], url_validator=url_validator)})
        elif line.startswith("- "):
            item = {"type": "listItem", "content": [{"type": "paragraph", "content": _inline(line[2:], url_validator=url_validator)}]}
            if not content or content[-1]["type"] != "bulletList":
                content.append({"type": "bulletList", "content": []})
            content[-1]["content"].append(item)
        else:
            if line.startswith((" ", "\t", ">", "+ ")) or re.match(r"\d+[.)] ", line):
                raise ValueError("unsupported Markdown block")
            content.append({"type": "paragraph", "content": _inline(line, url_validator=url_validator)})
    return {"type": "doc", "version": 1, "content": content}


def validate_adf(node: Any, *, path: str = "description") -> None:
    if not isinstance(node, dict):
        raise ValueError(f"{path} must be an ADF mapping")
    node_type = node.get("type")
    if not isinstance(node_type, str) or not node_type:
        raise ValueError(f"{path}.type is required")
    if node_type not in VALID_NODE_TYPES:
        raise ValueError(f"{path}.type is not supported")
    if node_type == "doc":
        if node.get("version") != 1:
            raise ValueError("description.version must be 1")
        content = node.get("content")
        if not isinstance(content, list):
            raise ValueError("description.content must be a list")
        for index, child in enumerate(content):
            validate_adf(child, path=f"{path}.content[{index}]")
        return
    if node_type == "heading":
        attrs = node.get("attrs")
        level = attrs.get("level") if isinstance(attrs, dict) else None
        if not isinstance(level, int) or not (1 <= level <= 6):
            raise ValueError(f"{path}.attrs.level must be between 1 and 6")
        content = node.get("content")
        if not isinstance(content, list):
            raise ValueError(f"{path}.content must be a list")
        for index, child in enumerate(content):
            validate_adf(child, path=f"{path}.content[{index}]")
        return
    if node_type in {"paragraph", "blockquote", "bulletList", "orderedList", "listItem", "codeBlock"}:
        content = node.get("content")
        if not isinstance(content, list):
            raise ValueError(f"{path}.content must be a list")
        for index, child in enumerate(content):
            validate_adf(child, path=f"{path}.content[{index}]")
        if node_type in {"bulletList", "orderedList", "listItem"} and not content:
            raise ValueError(f"{path}.content must not be empty")
        if node_type == "codeBlock":
            attrs = node.get("attrs")
            if attrs is not None and not isinstance(attrs, dict):
                raise ValueError(f"{path}.attrs must be a mapping")
        return
    if node_type == "hardBreak":
        if "content" in node and node["content"] not in (None, []):
            raise ValueError(f"{path}.content must be omitted")
        return
    if node_type == "text":
        text = node.get("text")
        if not isinstance(text, str):
            raise ValueError(f"{path}.text must be a string")
        marks = node.get("marks")
        if marks is None:
            return
        if not isinstance(marks, list):
            raise ValueError(f"{path}.marks must be a list")
        for index, mark in enumerate(marks):
            if not isinstance(mark, dict):
                raise ValueError(f"{path}.marks[{index}] must be a mapping")
            mark_type = mark.get("type")
            if mark_type not in VALID_MARK_TYPES:
                raise ValueError(f"{path}.marks[{index}].type is not supported")
            if mark_type == "link":
                attrs = mark.get("attrs")
                if not isinstance(attrs, dict) or not isinstance(attrs.get("href"), str) or not attrs.get("href").strip():
                    raise ValueError(f"{path}.marks[{index}].attrs.href is required")
            if mark_type == "code" and "attrs" in mark and mark["attrs"] not in (None, {}):
                raise ValueError(f"{path}.marks[{index}].attrs must be omitted")
        return
    raise ValueError(f"{path}.type is not supported")


def adf_signature(node: Any) -> Any:
    if not isinstance(node, dict):
        return node
    node_type = node.get("type")
    content = node.get("content") if isinstance(node.get("content"), list) else None
    if node_type == "doc":
        return {
            "type": "doc",
            "version": node.get("version"),
            "content": [adf_signature(child) for child in content or []],
        }
    if node_type == "heading":
        attrs = node.get("attrs") if isinstance(node.get("attrs"), dict) else {}
        return {
            "type": "heading",
            "attrs": {"level": attrs.get("level")},
            "content": [adf_signature(child) for child in content or []],
        }
    if node_type in {"paragraph", "blockquote", "bulletList", "orderedList", "listItem", "codeBlock"}:
        result = {
            "type": node_type,
            "content": [adf_signature(child) for child in content or []],
        }
        attrs = node.get("attrs") if isinstance(node.get("attrs"), dict) else None
        if node_type == "codeBlock" and attrs:
            result["attrs"] = {key: attrs[key] for key in sorted(attrs) if isinstance(key, str)}
        return result
    if node_type == "hardBreak":
        return {"type": "hardBreak"}
    if node_type == "text":
        result = {"type": "text", "text": node.get("text")}
        marks = node.get("marks")
        if isinstance(marks, list):
            normalized = []
            for mark in marks:
                if not isinstance(mark, dict):
                    normalized.append(mark)
                    continue
                mark_type = mark.get("type")
                if mark_type == "link":
                    attrs = mark.get("attrs") if isinstance(mark.get("attrs"), dict) else {}
                    normalized.append({"type": "link", "attrs": {"href": attrs.get("href")}})
                elif mark_type == "code":
                    normalized.append({"type": "code"})
                else:
                    normalized.append({"type": mark_type})
            result["marks"] = normalized
        return result
    return node
